"""Shared cache of real Gate CFD REST quotes, without generated market data.

The documented CFD API exposes REST tickers and OHLC candles. No public TradFi
WebSocket channel has been verified. ``quote_timestamp`` is Gate's response time
in milliseconds, not a trade sequence timestamp; these are sampled quotes, not
an exhaustive tick stream. A successful fetch is the only event that advances
``received_at``. Consumers can publish these cached snapshots through local SSE.

Live responses on 2026-09-07 reported a 5 qps ticker/candle limit. GateClient owns
shared request pacing and response-header cooldowns. This layer additionally
deduplicates concurrent readers and keeps each symbol's poll interval >= 1 sec.
"""

from __future__ import annotations

import asyncio
import copy
import math
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .gate import GateClient, GateError


def _symbol(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("交易品种格式无效。")
    return value


def _decimal(value: Any, *, positive: bool = False) -> str:
    if value is None or isinstance(value, bool):
        raise ValueError("Gate 返回的数值字段无效。")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Gate 返回的数值字段无效。") from None
    if not number.is_finite() or abs(number) > Decimal("1e30") or number.as_tuple().exponent < -30:
        raise ValueError("Gate 返回的数值字段无效。")
    if positive and number <= 0:
        raise ValueError("Gate 尚未提供有效的正数报价或品种规格。")
    return format(number, "f")


def _integer(value: Any, *, minimum: int = 0, maximum: int = 9_999_999_999_999) -> int:
    number = Decimal(_decimal(value))
    if number != number.to_integral_value() or not minimum <= number <= maximum:
        raise ValueError("Gate 返回的整数或时间戳字段无效。")
    return int(number)


def _data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Gate 返回的数据结构无效。")
    return payload["data"]


def _rows(payload: Any) -> list[dict[str, Any]]:
    rows = _data(payload).get("list")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Gate 返回的列表结构无效。")
    return rows


def normalize_symbol(row: dict[str, Any]) -> dict[str, Any]:
    """Public metadata has no contract volume/minimum/step; never invent them."""
    symbol = _symbol(row.get("symbol"))
    precision = None if row.get("price_precision") is None else _integer(row["price_precision"], maximum=16)
    return {
        "symbol": symbol, "name": str(row.get("symbol_desc") or symbol),
        "status": row.get("status"), "trading_status": row.get("status"),
        "trade_mode": str(row["trade_mode"]) if row.get("trade_mode") is not None else None,
        "price_precision": precision, "currency": row.get("settlement_currency"),
        "settlement_currency": row.get("settlement_currency"),
        "currency_symbol": row.get("settlement_currency_symbol"),
        "category_id": row.get("category_id"), "icon_link": row.get("icon_link"),
        "open_time": row.get("open_time"), "close_time": row.get("close_time"),
        "next_open_time": row.get("next_open_time"), "source": "gate",
        # Returned by the live list; this does not imply a leverage-setting API.
        "leverages": [str(item) for item in row.get("leverages", [])] if isinstance(row.get("leverages"), list) else [],
    }


def normalize_spec(row: dict[str, Any], metadata: dict[str, Any], received_at: float) -> dict[str, Any]:
    symbol = _symbol(row.get("symbol"))
    if metadata.get("symbol") not in (None, symbol):
        raise ValueError("Gate 品种详情与所选品种不一致。")
    precision = _integer(row.get("price_precision"), maximum=16)
    volume_min = _decimal(row.get("min_order_volume"), positive=True)
    volume_max = _decimal(row.get("max_order_volume"), positive=True)
    if Decimal(volume_max) < Decimal(volume_min):
        raise ValueError("Gate 返回的手数范围无效。")
    currency = row.get("settlement_currency") or metadata.get("currency")
    if not isinstance(currency, str) or not currency:
        raise ValueError("Gate 未返回品种结算币种。")
    return {
        **metadata, "symbol": symbol, "name": str(row.get("symbol_desc") or metadata.get("name") or symbol),
        "price_precision": precision, "tick_size": format(Decimal(1).scaleb(-precision), "f"),
        "volume_min": volume_min, "volume_max": volume_max, "volume_step": None,
        "contract_size": _decimal(row.get("contract_volume"), positive=True),
        "leverage": _decimal(row.get("leverage"), positive=True),
        "currency": currency, "settlement_currency": currency,
        "trade_mode": str(row["trade_mode"]) if row.get("trade_mode") is not None else metadata.get("trade_mode"),
        "price_sl_level": row.get("price_sl_level"), "category_name": row.get("category_name"),
        "swap_cost_type": row.get("swap_cost_type"), "buy_swap_cost_rate": row.get("buy_swap_cost_rate"),
        "sell_swap_cost_rate": row.get("sell_swap_cost_rate"), "swap_cost_3day": row.get("swap_cost_3day"),
        "trade_timezone": row.get("trade_timezone"), "received_at": received_at,
        "complete": True, "stale": False, "source": "gate",
    }


def normalize_ticker(
    symbol: str, payload: dict[str, Any], received_at: float, *, interval: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row, metadata = _data(payload), metadata or {}
    bid, ask, last = (_decimal(row.get(field), positive=True) for field in ("bid_price", "ask_price", "last_price"))
    if Decimal(ask) < Decimal(bid):
        raise ValueError("Gate 返回的买卖报价倒挂，暂停使用该报价。")
    stamp = None if payload.get("timestamp") is None else _integer(payload["timestamp"], minimum=1)
    return {
        "symbol": _symbol(symbol), "last": last, "bid": bid, "ask": ask,
        "spread": format(Decimal(ask) - Decimal(bid), "f"),
        "change": None if row.get("price_change") is None else _decimal(row["price_change"]),
        "high": None if row.get("highest_price") is None else _decimal(row["highest_price"], positive=True),
        "low": None if row.get("lowest_price") is None else _decimal(row["lowest_price"], positive=True),
        "updated_at": datetime.fromtimestamp(received_at, timezone.utc).isoformat(),
        "received_at": received_at, "quote_timestamp": stamp, "quote_time_kind": "server_response",
        "transport": "rest", "interval_ms": round(interval * 1000), "source": "gate",
        "stale": False, "status": row.get("status"), "trading_status": row.get("status"),
        "trade_mode": str(row["trade_mode"]) if row.get("trade_mode") is not None else None,
        "currency": row.get("settlement_currency") or metadata.get("currency"),
        "settlement_currency": row.get("settlement_currency") or metadata.get("currency"),
        "exchange_rate": row.get("exchange_rate"), "category_name": row.get("category_name"),
        "open_time": row.get("open_time"), "close_time": row.get("close_time"),
        "next_open_time": row.get("next_open_time"), "candles": [],
    }


def normalize_candles(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candles: dict[int, dict[str, Any]] = {}
    for row in _rows(payload):
        candle = {"time": _integer(row.get("t"), minimum=1)}
        for original, field in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close")):
            candle[field] = _decimal(row.get(original), positive=True)
        if (Decimal(candle["high"]) < max(Decimal(candle["open"]), Decimal(candle["close"]))
                or Decimal(candle["low"]) > min(Decimal(candle["open"]), Decimal(candle["close"]))):
            raise ValueError("Gate 返回的 K 线价格关系无效。")
        candles[candle["time"]] = candle
    return [candles[key] for key in sorted(candles)]


class LiveMarket:
    """One cache shared by UI readers and the strategy engine; no HTTP server here.

    ``errors[symbol]`` concerns quotes, ``errors[symbol + ':detail']`` concerns
    contract specifications and ``errors[symbol + ':klines']`` concerns history.
    ``errors['symbols']`` concerns the directory. A history error does not make a
    fresh ticker unusable. ``get`` computes stale age each time it is called.
    """

    def __init__(self, gate: GateClient, interval: float = 1.0, clock: Callable[[], float] = time.time):
        if not math.isfinite(interval) or interval < 1:
            raise ValueError("行情刷新间隔不得少于 1 秒。")
        self.gate, self.interval, self.clock = gate, float(interval), clock
        self.symbols: list[dict[str, Any]] = []
        self.specs: dict[str, dict[str, Any]] = {}
        self.markets: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, str] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._due: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._candles: dict[str, list[dict[str, Any]]] = {}
        self._generation = 0
        self._symbols_interval, self._detail_interval, self._candles_interval = 300.0, 600.0, 30.0

    def set_client(self, gate: GateClient) -> None:
        """Caller must hold its operation/market locks while swapping credentials.

        Public observations survive a connection swap. Signed symbol details do
        not, because the new account can have different applicable specifications.
        The caller owns closing the replaced GateClient.
        """
        self.gate = gate
        self._generation += 1
        self.specs.clear()
        for key in list(self._due):
            if key.endswith(":detail"):
                self._due.pop(key, None)
                self._failures.pop(key, None)
                self.errors.pop(key, None)

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def _ready(self, key: str) -> bool:
        return self.clock() >= self._due.get(key, float("-inf"))

    def _success(self, key: str, interval: float) -> None:
        self._due[key] = self.clock() + interval
        self._failures.pop(key, None)
        self.errors.pop(key, None)

    def _failure(self, key: str, exc: Exception) -> None:
        count = min(self._failures.get(key, 0) + 1, 5)
        self._failures[key] = count
        retry_after = exc.retry_after if isinstance(exc, GateError) else None
        delay = max(self.interval, min(30.0, float(2 ** count)), retry_after or 0.0)
        self._due[key] = self.clock() + delay
        self.errors[key] = str(exc) if isinstance(exc, GateError) else "Gate 行情或品种数据无效，正在等待重新同步。"

    async def refresh_symbols(self) -> list[dict[str, Any]]:
        async with self._lock("symbols"):
            if not self._ready("symbols"):
                return copy.deepcopy(self.symbols)
            try:
                rows = _rows(await self.gate.symbols())
                normalized = [normalize_symbol(row) for row in rows]
                if not normalized:
                    raise ValueError("Gate 品种列表为空。")
                self.symbols = normalized
                self._metadata = {row["symbol"]: row for row in normalized}
                for symbol, spec in list(self.specs.items()):
                    metadata = self._metadata.get(symbol)
                    if metadata is None or metadata["price_precision"] != spec["price_precision"]:
                        self.specs.pop(symbol, None)
                        self._due.pop(symbol + ":detail", None)
                    else:
                        for key in ("status", "trading_status", "trade_mode", "open_time", "close_time", "next_open_time"):
                            spec[key] = metadata.get(key)
                self._success("symbols", self._symbols_interval)
            except Exception as exc:
                self._failure("symbols", exc)
            return copy.deepcopy(self.symbols)

    async def _ticker(self, symbol: str) -> None:
        if not self._ready(symbol):
            return
        try:
            payload = await self.gate.ticker(symbol)
            quote = normalize_ticker(symbol, payload, self.clock(), interval=self.interval,
                                     metadata=self._metadata.get(symbol))
            quote["candles"] = copy.deepcopy(self._candles.get(symbol, []))
            self.markets[symbol] = quote
            self._success(symbol, self.interval)
        except Exception as exc:
            self._failure(symbol, exc)

    async def _history(self, symbol: str) -> None:
        key = symbol + ":klines"
        if not self._ready(key):
            return
        try:
            candles = normalize_candles(await self.gate.klines(symbol, "1m", 120))
            self._candles[symbol] = candles
            if symbol in self.markets:
                self.markets[symbol]["candles"] = copy.deepcopy(candles)
            self._success(key, self._candles_interval)
        except Exception as exc:
            self._failure(key, exc)

    async def _detail(self, symbol: str) -> None:
        key = symbol + ":detail"
        if not self._ready(key):
            return
        if not self.gate.credentials_set:
            self.specs.pop(symbol, None)
            self.errors[key] = "配置并连接 Gate API 后才能读取该账户适用的品种规格。"
            return
        generation = self._generation
        try:
            rows = _rows(await self.gate.symbol_detail(symbol))
            row = next((row for row in rows if row.get("symbol") == symbol), None)
            if row is None:
                raise ValueError("Gate 未返回所选品种的详情。")
            spec = normalize_spec(row, self._metadata.get(symbol, {}), self.clock())
            if generation != self._generation:
                return
            self.specs[symbol] = spec
            self._success(key, self._detail_interval)
        except Exception as exc:
            if generation == self._generation:
                self._failure(key, exc)

    async def ensure(self, symbol: str, detail: bool = False) -> dict[str, Any] | None:
        symbol = _symbol(symbol)
        await self.refresh_symbols()
        async with self._lock(symbol):
            await self._ticker(symbol)
            await self._history(symbol)
            if detail:
                await self._detail(symbol)
        return self.get(symbol)

    async def poll(self, symbols: list[str]) -> None:
        # Bound concurrency: GateClient also paces all attempts, including account
        # and strategy reads sharing this client. The interval is a minimum, not a
        # promise that a large watchlist can bypass the exchange's request limits.
        await self.refresh_symbols()
        for symbol in dict.fromkeys(_symbol(value) for value in symbols):
            async with self._lock(symbol):
                await self._ticker(symbol)
                await self._history(symbol)

    def get(self, symbol: str) -> dict[str, Any] | None:
        result = copy.deepcopy(self.markets.get(symbol))
        if result is None:
            return None
        age = self.clock() - result["received_at"]
        result["stale"] = symbol in self.errors or age < 0 or age > max(5.0, self.interval * 3)
        result["age_ms"] = max(0, round(age * 1000))
        result["error"] = self.errors.get(symbol)
        result["candles_error"] = self.errors.get(symbol + ":klines")
        return result

    def spec(self, symbol: str) -> dict[str, Any] | None:
        result = self.specs.get(symbol)
        if result is None or symbol + ":detail" in self.errors:
            return None
        age = self.clock() - result["received_at"]
        if age < 0 or age >= self._detail_interval:
            return None
        return copy.deepcopy(result)
