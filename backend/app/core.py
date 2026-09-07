"""Persistent, deliberately isolated paper-trading engine for the CFD console.

No code in this module sends orders to Gate. Prices, symbol specifications,
account balances and fills are simulated and must be labelled as such by the UI.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


D = Decimal
ZERO = D("0")
INITIAL_BALANCE = D("100000")
CAPACITY_LIMIT = 300


def decimal(value: Any, label: str = "数值") -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是有效数字")
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label}必须是有效数字") from exc
    if not result.is_finite():
        raise ValueError(f"{label}必须是有限数字")
    if abs(result) > D("1e15"):
        raise ValueError(f"{label}超出支持范围")
    if result.as_tuple().exponent < -16:
        raise ValueError(f"{label}最多支持 16 位小数")
    return result


def fmt(value: Any) -> str:
    return format(decimal(value), "f")


def rounded(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).quantize(D("1"), rounding=ROUND_HALF_UP) * step


def money(value: Decimal) -> str:
    return format(value.quantize(D("0.01"), rounding=ROUND_HALF_UP), "f")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class GridConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = "XAUUSD"
    direction: Literal["long", "short"] = "long"
    lower_price: Decimal
    upper_price: Decimal
    volume: Decimal
    grid_count: int = Field(default=12, ge=2, le=100, strict=True)
    spacing: Literal["arithmetic", "geometric"] = "arithmetic"
    repeat: bool = True
    stop_loss: Decimal | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("lower_price", "upper_price", "volume", "stop_loss", mode="before")
    @classmethod
    def valid_decimal(cls, value: Any) -> Any:
        return None if value is None else decimal(value)

    @model_validator(mode="after")
    def valid_range(self) -> "GridConfig":
        if self.lower_price <= 0 or self.upper_price <= self.lower_price:
            raise ValueError("价格下限必须大于 0，且价格上限必须大于下限")
        if self.volume <= 0:
            raise ValueError("每格手数必须大于 0")
        if self.stop_loss is not None:
            if self.stop_loss <= 0:
                raise ValueError("止损价必须大于 0")
            if self.direction == "long" and self.stop_loss >= self.lower_price:
                raise ValueError("做多策略的止损价必须低于区间下限")
            if self.direction == "short" and self.stop_loss <= self.upper_price:
                raise ValueError("做空策略的止损价必须高于区间上限")
        return self


SYMBOL_SPECS: list[dict[str, Any]] = [
    {"symbol": "XAUUSD", "name": "黄金 / 美元", "label": "黄金 / 美元 · 模拟", "tick_size": "0.01", "price_precision": 2, "volume_min": "0.01", "volume_step": "0.01", "volume_max": "100", "contract_size": "100", "leverage": 500, "spread": "0.30", "initial_price": "3356.42", "currency": "USD", "source": "simulation"},
    {"symbol": "EURUSD", "name": "欧元 / 美元", "label": "欧元 / 美元 · 模拟", "tick_size": "0.00001", "price_precision": 5, "volume_min": "0.01", "volume_step": "0.01", "volume_max": "100", "contract_size": "100000", "leverage": 100, "spread": "0.00012", "initial_price": "1.08250", "currency": "USD", "source": "simulation"},
    {"symbol": "USOIL", "name": "原油 / 美元", "label": "原油 / 美元 · 模拟", "tick_size": "0.01", "price_precision": 2, "volume_min": "0.01", "volume_step": "0.01", "volume_max": "100", "contract_size": "1000", "leverage": 50, "spread": "0.04", "initial_price": "77.50", "currency": "USD", "source": "simulation"},
    {"symbol": "NAS100", "name": "纳斯达克 100", "label": "纳斯达克 100 · 模拟", "tick_size": "0.1", "price_precision": 1, "volume_min": "0.1", "volume_step": "0.1", "volume_max": "100", "contract_size": "1", "leverage": 20, "spread": "1.0", "initial_price": "19860.0", "currency": "USD", "source": "simulation"},
]
_SPECS = {item["symbol"]: item for item in SYMBOL_SPECS}


def get_spec(symbol: str) -> dict[str, Any]:
    try:
        return copy.deepcopy(_SPECS[symbol.upper()])
    except KeyError as exc:
        raise ValueError(f"不支持的模拟品种：{symbol}") from exc


def make_preview(config: GridConfig | dict[str, Any], spec: dict[str, Any], ticker: dict[str, Any]) -> dict[str, Any]:
    config = config if isinstance(config, GridConfig) else GridConfig.model_validate(config)
    if config.symbol != spec["symbol"]:
        raise ValueError("品种与报价不匹配")
    tick = decimal(spec["tick_size"])
    volume_min, volume_step = decimal(spec["volume_min"]), decimal(spec["volume_step"])
    if config.volume < volume_min or config.volume % volume_step != 0:
        raise ValueError(f"手数最小为 {volume_min}，且必须符合步长 {volume_step}")
    if config.volume > decimal(spec.get("volume_max", "100000")):
        raise ValueError("每格手数超过该模拟品种允许的上限")
    for label, value in (("下限", config.lower_price), ("上限", config.upper_price), ("止损价", config.stop_loss)):
        if value is not None and value % tick != 0:
            raise ValueError(f"{label}必须符合价格步长 {tick}")
    with localcontext() as ctx:
        ctx.prec = 48
        if config.spacing == "arithmetic":
            step = (config.upper_price - config.lower_price) / config.grid_count
            levels = [rounded(config.lower_price + step * index, tick) for index in range(config.grid_count + 1)]
        else:
            ratio = config.upper_price / config.lower_price
            levels = [rounded(config.lower_price * ctx.power(ratio, D(index) / config.grid_count), tick) for index in range(config.grid_count + 1)]
    levels[0], levels[-1] = config.lower_price, config.upper_price
    if any(a >= b for a, b in zip(levels, levels[1:])):
        raise ValueError("价格区间不足以容纳该网格数量，请扩大区间或减少网格")
    bid, ask = decimal(ticker["bid"]), decimal(ticker["ask"])
    if bid <= 0 or ask < bid:
        raise ValueError("报价无效")
    cells = []
    contract, leverage = decimal(spec["contract_size"]), decimal(spec["leverage"])
    for index in range(config.grid_count):
        entry, tp = (levels[index], levels[index + 1]) if config.direction == "long" else (levels[index + 1], levels[index])
        cells.append({"index": index, "entry_price": fmt(entry), "take_profit": fmt(tp), "eligible": entry < ask if config.direction == "long" else entry > bid, "estimated_margin": money(entry * contract * config.volume / leverage), "gross_profit": money(abs(tp - entry) * contract * config.volume)})
    estimated_margin = sum(decimal(cell["entry_price"]) * contract * config.volume / leverage for cell in cells)
    return {"config": config.model_dump(mode="json"), "levels": [fmt(level) for level in levels], "cells": cells, "grid_count": config.grid_count, "level_count": len(levels), "eligible_count": sum(cell["eligible"] for cell in cells), "waiting_count": sum(not cell["eligible"] for cell in cells), "total_volume": fmt(config.volume * config.grid_count), "estimated_margin": money(estimated_margin), "estimated_max_margin": money(estimated_margin), "leverage": spec["leverage"], "leverage_editable": False, "source": "simulation", "estimate_notice": "模拟参数估算，不含手续费、隔夜费及滑点；保证金与每格毛利均不代表实盘结果。", "warnings": ["当前为本地模拟，不会向 Gate 提交订单。", "杠杆由品种决定，页面只读。"]}


class Engine:
    """All transitions, including tick versus cancel, commit under one lock."""

    def __init__(self, db_path: str | Path):
        self._lock = threading.RLock()
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS paper_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
        row = self._db.execute("SELECT payload FROM paper_state WHERE id=1").fetchone()
        if row:
            self._data = json.loads(row[0])
            if self._data.get("version") != 1:
                raise ValueError("不支持的本地策略数据版本")
        else:
            self._data = self._new_data()
            self._persist()

    def _new_data(self) -> dict[str, Any]:
        timestamp = int(time.time()) // 60 * 60
        markets = {}
        for number, spec in enumerate(SYMBOL_SPECS):
            initial, tick = decimal(spec["initial_price"]), decimal(spec["tick_size"])
            candles, previous = [], initial
            for index in range(120):
                offset = math.sin(index * .11 + number) * .0008 + math.sin(index * .39) * .00015
                close = rounded(initial * (D("1") + D(str(offset))), tick)
                wiggle = max(tick, rounded(initial * D("0.00009"), tick))
                candles.append({"time": timestamp - (120 - index) * 60, "open": fmt(previous), "high": fmt(max(previous, close) + wiggle), "low": fmt(min(previous, close) - wiggle), "close": fmt(close)})
                previous = close
            spread = decimal(spec["spread"])
            bid, ask = rounded(initial - spread / 2, tick), rounded(initial + spread / 2, tick)
            candles.append({"time": timestamp, "open": fmt(previous), "high": fmt(max(previous, initial)), "low": fmt(min(previous, initial)), "close": fmt(initial)})
            markets[spec["symbol"]] = {"symbol": spec["symbol"], "last": fmt(initial), "bid": fmt(bid), "ask": fmt(ask), "spread": fmt(ask - bid), "change": "0.00", "high": fmt(max(decimal(c["high"]) for c in candles)), "low": fmt(min(decimal(c["low"]) for c in candles)), "updated_at": now(), "candles": candles, "source": "simulation", "trading_status": "open", "tick_count": 0}
        return {"version": 1, "markets": markets, "strategies": [], "orders": [], "positions": [], "fills": [], "logs": [{"id": identifier("log"), "time": now(), "level": "info", "message": "本地模拟账户已就绪，所有价格与成交均为模拟。"}], "templates": [], "receipts": {}, "balance": fmt(INITIAL_BALANCE), "realized_pnl": "0"}

    def _persist(self) -> None:
        payload = json.dumps(self._data, ensure_ascii=False, separators=(",", ":"))
        with self._db:
            self._db.execute("INSERT INTO paper_state(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (payload,))

    def _mutate(self, operation):
        backup = copy.deepcopy(self._data)
        try:
            result = operation()
            self._persist()
            return result
        except Exception:
            self._data = backup
            raise

    def _log(self, message: str, level: str = "info") -> None:
        self._data["logs"].append({"id": identifier("log"), "time": now(), "level": level, "message": message})
        self._data["logs"] = self._data["logs"][-300:]

    def _account(self) -> dict[str, str]:
        unrealized, margin, reserved = ZERO, ZERO, ZERO
        for position in self._data["positions"]:
            spec = _SPECS[position["symbol"]]
            market = self._data["markets"][position["symbol"]]
            current = decimal(market["bid"] if position["direction"] == "long" else market["ask"])
            sign = D("1") if position["direction"] == "long" else D("-1")
            amount = decimal(position["volume"]) * decimal(spec["contract_size"])
            unrealized += (current - decimal(position["entry_price"])) * amount * sign
            margin += decimal(position["entry_price"]) * amount / decimal(spec["leverage"])
        for strategy in self._data["strategies"]:
            if strategy["status"] != "running":
                continue
            spec = _SPECS[strategy["config"]["symbol"]]
            for cell in strategy["cells"]:
                if cell["status"] in ("waiting", "pending"):
                    reserved += decimal(cell["entry_price"]) * decimal(strategy["config"]["volume"]) * decimal(spec["contract_size"]) / decimal(spec["leverage"])
        balance = decimal(self._data["balance"])
        equity = balance + unrealized
        return {"currency": "USD", "balance": money(balance), "equity": money(equity), "margin": money(margin), "free_margin": money(equity - margin), "reserved_margin": money(reserved), "available_for_new_strategy": money(equity - margin - reserved), "unrealized_pnl": money(unrealized), "realized_pnl": money(decimal(self._data["realized_pnl"])), "source": "simulation"}

    def _state(self, symbol: str) -> dict[str, Any]:
        spec = get_spec(symbol)
        positions = copy.deepcopy(self._data["positions"])
        for position in positions:
            p_spec, market = _SPECS[position["symbol"]], self._data["markets"][position["symbol"]]
            current = decimal(market["bid"] if position["direction"] == "long" else market["ask"])
            sign = D("1") if position["direction"] == "long" else D("-1")
            position["unrealized_pnl"] = money((current - decimal(position["entry_price"])) * decimal(position["volume"]) * decimal(p_spec["contract_size"]) * sign)
        strategies = copy.deepcopy(self._data["strategies"])
        for strategy in strategies:
            owned_positions = [p for p in positions if p["strategy_id"] == strategy["id"]]
            strategy["pending_count"] = sum(order["status"] == "pending" and order["strategy_id"] == strategy["id"] for order in self._data["orders"])
            strategy["position_count"] = len(owned_positions)
            strategy["waiting_count"] = sum(cell["status"] == "waiting" for cell in strategy["cells"])
            strategy["unrealized_pnl"] = money(sum((decimal(p["unrealized_pnl"]) for p in owned_positions), ZERO))
        selected = next((s for s in reversed(strategies) if s["config"]["symbol"] == symbol), None)
        return copy.deepcopy({"mode": "paper", "source": "simulation", "symbols": SYMBOL_SPECS, "spec": spec, "market": self._data["markets"][symbol], "account": self._account(), "strategies": strategies, "selected_strategy": selected, "orders": self._data["orders"], "positions": positions, "fills": self._data["fills"], "logs": self._data["logs"], "templates": self._data["templates"], "server_time": now()})

    def state(self, symbol: str = "XAUUSD") -> dict[str, Any]:
        with self._lock:
            return self._state(symbol.upper())

    def preview(self, config: GridConfig | dict[str, Any]) -> dict[str, Any]:
        config = config if isinstance(config, GridConfig) else GridConfig.model_validate(config)
        with self._lock:
            spec = get_spec(config.symbol)
            result = make_preview(config, spec, self._data["markets"][config.symbol])
            result["account"] = self._account()
            result["sufficient_margin"] = decimal(result["estimated_margin"]) <= decimal(result["account"]["available_for_new_strategy"])
            result["capacity"] = self._capacity(config.grid_count)
            result["sufficient_capacity"] = result["capacity"]["sufficient"]
            result["can_start"] = result["sufficient_margin"] and result["sufficient_capacity"] and not self._symbol_in_use(config.symbol)
            if self._symbol_in_use(config.symbol):
                result["warnings"].append("该品种已有运行策略或未平仓仓位，请先处理已有策略。")
            if not result["sufficient_margin"]:
                result["warnings"].append("模拟账户可用于新策略的保证金不足。")
            if not result["sufficient_capacity"]:
                result["warnings"].append(f"模拟账户最多预留 {CAPACITY_LIMIT} 个网格或仓位名额；现有策略（含待激活格位）与保留仓位已占用 {result['capacity']['reserved']} 个，剩余 {result['capacity']['available']} 个，不足以启动 {config.grid_count} 格。")
            return result

    def _capacity(self, requested: int) -> dict[str, Any]:
        running = [strategy for strategy in self._data["strategies"] if strategy["status"] == "running"]
        running_ids = {strategy["id"] for strategy in running}
        # Every active cell can become an order or an independent position.
        # Count open cells once; stopped strategies reserve only residual positions.
        reserved = sum(cell["status"] in ("waiting", "pending", "open") for strategy in running for cell in strategy["cells"])
        reserved += sum(position["strategy_id"] not in running_ids for position in self._data["positions"])
        return {"limit": CAPACITY_LIMIT, "reserved": reserved, "available": max(0, CAPACITY_LIMIT - reserved), "requested": requested, "sufficient": reserved + requested <= CAPACITY_LIMIT}

    def _symbol_in_use(self, symbol: str) -> bool:
        return any(s["config"]["symbol"] == symbol and s["status"] == "running" for s in self._data["strategies"]) or any(p["symbol"] == symbol for p in self._data["positions"])

    def _place(self, strategy: dict[str, Any], cell: dict[str, Any]) -> None:
        config = strategy["config"]
        self._data["orders"].append({"id": identifier("ord"), "strategy_id": strategy["id"], "symbol": config["symbol"], "grid_index": cell["index"], "side": "buy" if config["direction"] == "long" else "sell", "price": cell["entry_price"], "take_profit": cell["take_profit"], "stop_loss": config["stop_loss"], "volume": config["volume"], "status": "pending", "created_at": now(), "source": "simulation"})
        cell["status"] = "pending"

    def start(self, config: GridConfig | dict[str, Any], request_id: str) -> dict[str, Any]:
        config = config if isinstance(config, GridConfig) else GridConfig.model_validate(config)
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise ValueError("启动操作需要非空且不超过 200 字符的 request_id")
        payload = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        with self._lock:
            receipt = self._data["receipts"].get(request_id)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise ValueError("同一个 request_id 不能用于不同策略参数")
                result = self._state(config.symbol)
                result.update({"strategy_id": receipt["strategy_id"], "idempotent_replay": True})
                return result
            preview = self.preview(config)
            if self._symbol_in_use(config.symbol):
                raise ValueError("该品种已有运行策略或未平仓仓位，不允许重复启动")
            if not preview["sufficient_margin"]:
                raise ValueError("模拟账户可用于新策略的保证金不足")
            if not preview["sufficient_capacity"]:
                raise ValueError(f"现有策略（含待激活格位）与保留仓位加上新网格超过模拟账户 {CAPACITY_LIMIT} 个预留名额，请减少网格数量或先处理已有策略")

            def operation():
                strategy = {"id": identifier("grid"), "config": config.model_dump(mode="json"), "status": "running", "created_at": now(), "stopped_at": None, "realized_pnl": "0.00", "completed_cycles": 0, "levels": preview["levels"], "cells": [{**cell, "status": "waiting", "completed_cycles": 0} for cell in preview["cells"]], "source": "simulation"}
                self._data["strategies"].append(strategy)
                for cell in strategy["cells"]:
                    if cell["eligible"]:
                        self._place(strategy, cell)
                self._data["receipts"][request_id] = {"fingerprint": fingerprint, "strategy_id": strategy["id"]}
                self._log(f"{config.symbol} 模拟网格已启动，已确认 {preview['eligible_count']} 笔挂单，{preview['waiting_count']} 格等待价格条件。")
                result = self._state(config.symbol)
                result.update({"strategy_id": strategy["id"], "idempotent_replay": False})
                return result

            return self._mutate(operation)

    def _stop(self, strategy: dict[str, Any], reason: str = "用户取消") -> int:
        if strategy["status"] == "running":
            strategy["status"], strategy["stopped_at"] = "stopped", now()
        count = 0
        for order in self._data["orders"]:
            if order["strategy_id"] == strategy["id"] and order["status"] == "pending":
                order["status"], order["cancelled_at"] = "cancelled", now()
                count += 1
        for cell in strategy["cells"]:
            if cell["status"] in ("waiting", "pending"):
                cell["status"] = "cancelled"
        strategy["stop_reason"] = reason
        return count

    def cancel(self, strategy_id: str) -> dict[str, Any]:
        with self._lock:
            strategy = next((s for s in self._data["strategies"] if s["id"] == strategy_id), None)
            if strategy is None:
                raise ValueError("策略不存在")

            def operation():
                count = self._stop(strategy)
                remaining = sum(p["strategy_id"] == strategy_id for p in self._data["positions"])
                self._log(f"{strategy['config']['symbol']} 已停止补单并撤销 {count} 笔挂单；保留 {remaining} 个仓位及其止盈止损。")
                result = self._state(strategy["config"]["symbol"])
                result.update({"strategy_id": strategy_id, "cancelled_count": count, "remaining_positions": remaining})
                return result

            return self._mutate(operation)

    def _close_position(self, position: dict[str, Any], strategy: dict[str, Any], exit_price: Decimal, reason: str) -> None:
        spec = _SPECS[position["symbol"]]
        sign = D("1") if position["direction"] == "long" else D("-1")
        pnl = (exit_price - decimal(position["entry_price"])) * decimal(position["volume"]) * decimal(spec["contract_size"]) * sign
        self._data["balance"] = fmt(decimal(self._data["balance"]) + pnl)
        self._data["realized_pnl"] = fmt(decimal(self._data["realized_pnl"]) + pnl)
        strategy["realized_pnl"] = money(decimal(strategy["realized_pnl"]) + pnl)
        self._data["fills"].append({"id": identifier("fill"), "position_id": position["id"], "strategy_id": strategy["id"], "symbol": position["symbol"], "grid_index": position["grid_index"], "direction": position["direction"], "side": "sell" if position["direction"] == "long" else "buy", "volume": position["volume"], "entry_price": position["entry_price"], "price": fmt(exit_price), "pnl": money(pnl), "time": now(), "type": reason, "source": "simulation"})
        self._data["positions"].remove(position)
        cell = strategy["cells"][position["grid_index"]]
        cell["completed_cycles"] += 1
        strategy["completed_cycles"] += 1
        cell["status"] = "waiting" if strategy["status"] == "running" and strategy["config"]["repeat"] and reason == "take_profit" else "done"
        self._log(f"{position['symbol']} 第 {position['grid_index'] + 1} 格{'止盈' if reason == 'take_profit' else '止损'}平仓，模拟盈亏 {money(pnl)} USD。", "success" if pnl >= 0 else "warning")

    def _step(self, symbol: str, price: Decimal) -> None:
        spec, market = _SPECS[symbol], self._data["markets"][symbol]
        tick, spread = decimal(spec["tick_size"]), decimal(spec["spread"])
        price = rounded(price, tick)
        bid, ask = rounded(price - spread / 2, tick), rounded(price + spread / 2, tick)
        if bid <= 0:
            raise ValueError("模拟价格过低，无法生成有效买卖报价")
        market.update({"last": fmt(price), "bid": fmt(bid), "ask": fmt(ask), "spread": fmt(ask - bid), "updated_at": now(), "tick_count": market["tick_count"] + 1, "change": money((price / decimal(spec["initial_price"]) - 1) * 100)})
        timestamp = int(time.time()) // 60 * 60
        candles = market["candles"]
        if candles[-1]["time"] == timestamp:
            candles[-1].update({"high": fmt(max(decimal(candles[-1]["high"]), price)), "low": fmt(min(decimal(candles[-1]["low"]), price)), "close": fmt(price)})
        else:
            previous = decimal(candles[-1]["close"])
            candles.append({"time": timestamp, "open": fmt(previous), "high": fmt(max(previous, price)), "low": fmt(min(previous, price)), "close": fmt(price)})
        market["candles"] = candles[-180:]
        market["high"] = fmt(max(decimal(c["high"]) for c in market["candles"]))
        market["low"] = fmt(min(decimal(c["low"]) for c in market["candles"]))
        strategies = {s["id"]: s for s in self._data["strategies"] if s["config"]["symbol"] == symbol}
        for strategy in strategies.values():
            cfg = strategy["config"]
            stop = decimal(cfg["stop_loss"]) if cfg["stop_loss"] is not None else None
            stop_hit = stop is not None and (bid <= stop if cfg["direction"] == "long" else ask >= stop)
            if strategy["status"] == "running" and stop_hit:
                count = self._stop(strategy, "触及策略止损价")
                self._log(f"{symbol} 触及策略止损价，已停止补单并撤销 {count} 笔挂单。", "warning")

        # Close only positions that existed before this price update. A gapped
        # tick cannot invent an entry-and-profit round trip within one sample.
        for position in list(self._data["positions"]):
            if position["symbol"] != symbol:
                continue
            strategy = strategies[position["strategy_id"]]
            cfg, tp = strategy["config"], decimal(position["take_profit"])
            stop = decimal(position["stop_loss"]) if position["stop_loss"] is not None else None
            is_long = cfg["direction"] == "long"
            if stop is not None and (bid <= stop if is_long else ask >= stop):
                self._close_position(position, strategy, bid if is_long else ask, "stop_loss")
            elif bid >= tp if is_long else ask <= tp:
                self._close_position(position, strategy, tp, "take_profit")

        for order in list(self._data["orders"]):
            if order["symbol"] != symbol or order["status"] != "pending":
                continue
            strategy = strategies[order["strategy_id"]]
            if strategy["status"] != "running":
                continue
            entry = decimal(order["price"])
            fillable = ask <= entry if order["side"] == "buy" else bid >= entry
            if not fillable:
                continue
            margin = entry * decimal(order["volume"]) * decimal(spec["contract_size"]) / decimal(spec["leverage"])
            if margin > decimal(self._account()["free_margin"]) or len(self._data["positions"]) >= CAPACITY_LIMIT:
                self._stop(strategy, "模拟账户保证金或仓位额度不足")
                self._log(f"{symbol} 模拟账户保证金或仓位额度不足，策略已停止。", "error")
                continue
            order["status"], order["filled_at"] = "filled", now()
            position = {"id": identifier("pos"), "order_id": order["id"], "strategy_id": strategy["id"], "symbol": symbol, "grid_index": order["grid_index"], "direction": strategy["config"]["direction"], "entry_price": order["price"], "take_profit": order["take_profit"], "stop_loss": order["stop_loss"], "volume": order["volume"], "opened_at": now(), "source": "simulation"}
            self._data["positions"].append(position)
            self._data["fills"].append({"id": identifier("fill"), "position_id": position["id"], "strategy_id": strategy["id"], "symbol": symbol, "grid_index": order["grid_index"], "direction": position["direction"], "side": order["side"], "volume": order["volume"], "price": order["price"], "pnl": "0.00", "time": now(), "type": "open", "source": "simulation"})
            strategy["cells"][order["grid_index"]]["status"] = "open"
            self._log(f"{symbol} 第 {order['grid_index'] + 1} 格模拟开仓 {order['volume']} 手，成交价 {order['price']}。")

        for strategy in strategies.values():
            if strategy["status"] != "running":
                continue
            for cell in strategy["cells"]:
                if cell["status"] == "waiting":
                    eligible = decimal(cell["entry_price"]) < ask if strategy["config"]["direction"] == "long" else decimal(cell["entry_price"]) > bid
                    if eligible:
                        if sum(o["status"] == "pending" for o in self._data["orders"]) >= CAPACITY_LIMIT:
                            self._stop(strategy, "模拟账户委托额度不足")
                            self._log(f"{symbol} 模拟委托额度不足，策略已停止。", "error")
                            break
                        self._place(strategy, cell)
            if all(cell["status"] == "done" for cell in strategy["cells"]):
                strategy["status"], strategy["stopped_at"] = "completed", now()
                self._log(f"{symbol} 单轮模拟网格已完成。", "success")
        self._data["fills"] = self._data["fills"][-1000:]
        pending = [o for o in self._data["orders"] if o["status"] == "pending"]
        historical = [o for o in self._data["orders"] if o["status"] != "pending"][-1000:]
        self._data["orders"] = historical + pending

    def step(self, symbol: str, price: Any) -> dict[str, Any]:
        symbol = symbol.upper()
        get_spec(symbol)
        value = decimal(price, "模拟价格")
        if value <= 0:
            raise ValueError("模拟价格必须大于 0")
        with self._lock:
            def operation():
                self._step(symbol, value)
                return self._state(symbol)
            return self._mutate(operation)

    def tick(self) -> None:
        with self._lock:
            def operation():
                for number, spec in enumerate(SYMBOL_SPECS):
                    market = self._data["markets"][spec["symbol"]]
                    count = market["tick_count"]
                    drift = math.sin(count * .17 + number) * .000019 + math.cos(count * .071 + number) * .000013
                    new_price = decimal(market["last"]) + decimal(spec["initial_price"]) * D(str(drift))
                    self._step(spec["symbol"], max(decimal(spec["spread"]) * 2, new_price))
            self._mutate(operation)

    def list_templates(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._data["templates"])

    def save_template(self, name: str, config: GridConfig | dict[str, Any]) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60:
            raise ValueError("模板名称长度应为 1–60 个字符")
        config = config if isinstance(config, GridConfig) else GridConfig.model_validate(config)
        with self._lock:
            self.preview(config)
            def operation():
                if len(self._data["templates"]) >= 100:
                    raise ValueError("最多保存 100 个参数模板")
                template = {"id": identifier("tpl"), "name": name.strip(), "config": config.model_dump(mode="json"), "created_at": now()}
                self._data["templates"].append(template)
                return copy.deepcopy(template)
            return self._mutate(operation)

    def delete_template(self, template_id: str) -> dict[str, Any]:
        with self._lock:
            if not any(t["id"] == template_id for t in self._data["templates"]):
                raise ValueError("模板不存在")
            def operation():
                self._data["templates"] = [t for t in self._data["templates"] if t["id"] != template_id]
                return {"deleted": True, "id": template_id}
            return self._mutate(operation)

    def close(self) -> None:
        with self._lock:
            self._db.close()
