"""Persistent native pending-order grids against Gate's official CFD REST API.

Only the injected GateClient performs network operations. This module never reads
credentials and never places simulated orders. Every remote write is persisted
before transmission and never retried after an uncertain outcome. Queue/log IDs
are not assumed to be position IDs. Position attribution uses a strict pre/post
snapshot difference, including recently closed positions; concurrent identical
external trading cannot be cryptographically attributed by the available API.

References: https://www.gate.com/docs/developers/apiv4/zh_CN/cfd/
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from .core import GridConfig, identifier, now, rounded
from .gate import GateError, GateUnknownOutcome


D = Decimal
CAPACITY = 300
QUOTE_MAX_AGE = 5.0
SNAPSHOT_MAX_AGE = 3.0
EXIT_HISTORY_REQUEST_BUDGET = 2
ORDER_HISTORY_RESULT_LIMIT = 10
EXIT_HISTORY_RETRY_SECONDS = 5.0
MATCH_TIMEOUT = 30.0
BATCH_SIZE = 4
UNRESOLVED = {"prepared", "submitted", "reconciling", "unknown", "partial", "awaiting_position", "awaiting_close", "awaiting_exit_evidence"}
ACTIVE = {"starting", "running", "recovering", "paused", "stopping", "closing", "legacy_paused"}


class LivePreflightAbort(ValueError):
    """The final pre-send callback stopped an operation before its HTTP request."""


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("交易数值格式无效")
    try:
        result = D(str(value))
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError("交易数值格式无效") from None
    if not result.is_finite() or abs(result.adjusted()) > 80 or len(result.as_tuple().digits) > 200:
        raise ValueError("交易数值必须为有效有限数")
    return result


def _text(value: Any) -> str:
    return format(_decimal(value), "f")


def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(D("0.01")), "f")


def _uid(value: Any) -> str:
    result = str(value or "")
    if not result.isascii() or not result.isdigit() or int(result) <= 0:
        raise ValueError("需要已验证的 MT5 账户编号")
    return result


def _id(value: Any) -> str | None:
    try:
        return _uid(value)
    except ValueError:
        return None


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("data")
    if not isinstance(result, dict):
        raise ValueError("Gate 返回的数据结构不完整，已禁止新开仓")
    return result


def _list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = _data(payload)
    result = data.get("list")
    # Gate sometimes returns an explicit null list for an explicitly empty
    # account page. Missing fields and contradictory counts remain failures.
    if "list" in data and result is None and "total" in data:
        try:
            if _decimal(data["total"]) == 0:
                return []
        except ValueError:
            pass
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise ValueError("Gate 返回的账户列表不完整，已禁止新开仓")
    return result


def _epoch(value: Any) -> float | None:
    try:
        result = float(value)
        if math.isfinite(result) and result > 0:
            return result / 1000 if result > 100_000_000_000 else result
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def _iso(value: Any) -> str | None:
    stamp = _epoch(value)
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp else None


def _direction(value: Any) -> str | None:
    if str(value).lower() in {"long", "2", "buy"}:
        return "long"
    if str(value).lower() in {"short", "1", "sell"}:
        return "short"
    return None


def _spec_value(spec: dict[str, Any], *names: str) -> Any:
    return next((spec[name] for name in names if spec.get(name) is not None), None)


def _estimate(price: Decimal, volume: Decimal, spec: dict[str, Any]) -> Decimal | None:
    if _spec_value(spec, "settlement_currency", "currency") != "USD":
        return None
    try:
        contract = _decimal(_spec_value(spec, "contract_size", "contract_volume"))
        leverage = _decimal(spec.get("leverage"))
        return price * volume * contract / leverage if leverage > 0 and contract > 0 else None
    except ValueError:
        return None


class _LiveBase:
    def __init__(self, db_path: str | Path, *, clock=time.time):
        self._clock = clock
        self._lock = asyncio.Lock()
        self._read_lock = threading.RLock()
        self._stop_requests: set[str] = set()
        self._disabled_cells: set[tuple[str, int]] = set()
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._markets: dict[str, dict[str, Any]] = {}
        self._specs: dict[str, dict[str, Any]] = {}
        self.quote_provider = None
        self.current_uid: str | None = None
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS live_state(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)")
        row = self._db.execute("SELECT payload FROM live_state WHERE id=1").fetchone()
        self._state_data = json.loads(row[0]) if row else {"version": 2, "strategies": [], "intents": [], "receipts": {}, "logs": [], "templates": [], "operations": []}
        if self._state_data.get("version") not in {1, 2}:
            self._db.close()
            raise ValueError("实盘策略数据版本不兼容")
        for strategy in self._state_data["strategies"]:
            if not strategy.get("execution_mode"):
                strategy["execution_mode"] = "legacy_local"
                if strategy["status"] in ACTIVE:
                    strategy["status"] = "legacy_paused"
                    strategy["error"] = "旧版本地触发策略已暂停，不会自动转换或补单；请停止旧策略后重新创建原生挂单策略。"
            elif strategy["status"] in {"running", "starting"}:
                strategy["status"] = "recovering"
            for cell in strategy["cells"]:
                cell.setdefault("enabled", True)
                cell.setdefault("generation", cell.get("completed_cycles", 0))
        for intent in self._state_data["intents"]:
            intent.setdefault("position_ids", [intent["position_id"]] if intent.get("position_id") else [])
            if intent["status"] == "prepared":
                # A crash after persisting but before response is indistinguishable
                # from a transmitted request. Never replay the write.
                intent["status"] = "unknown"
        self._state_data["version"] = 2
        self._state_data.setdefault("operations", [])
        self._state_data.setdefault("history_windows", {})
        self._state_data.setdefault("owned_order_history", {})
        for operation in self._state_data["operations"]:
            if operation["status"] == "prepared":
                operation["status"] = "unknown"
        self._persist()

    def _persist(self):
        with self._read_lock, self._db:
            self._db.execute("INSERT INTO live_state(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (json.dumps(self._state_data, ensure_ascii=False, allow_nan=False, separators=(",", ":")),))

    def _log(self, account_id: str, message: str, level="info"):
        self._state_data["logs"].append({"id": identifier("log"), "time": now(), "account_id": account_id, "level": level, "message": message})
        self._state_data["logs"] = self._state_data["logs"][-500:]

    def has_unresolved_execution(self, account_id: str | None = None) -> bool:
        return any((account_id is None or intent["account_id"] == str(account_id)) and intent["status"] in UNRESOLVED for intent in self._state_data["intents"]) or any((account_id is None or operation["account_id"] == str(account_id)) and operation["status"] in {"prepared", "submitted", "unknown"} for operation in self._state_data["operations"])

    def has_active_strategies(self, account_id: str | None = None) -> bool:
        return any((account_id is None or strategy["account_id"] == str(account_id)) and strategy["status"] in ACTIVE for strategy in self._state_data["strategies"])

    def request_stop(self, strategy_id: str, account_id: str) -> None:
        """Signal cancellation before an outer application operation lock is held."""
        uid = _uid(account_id)
        if not any(strategy["id"] == strategy_id for strategy in self._strategies(uid)):
            raise ValueError("策略不存在或不属于当前已连接账户")
        self._stop_requests.add(strategy_id)

    def _strategies(self, account_id: str) -> list[dict[str, Any]]:
        return [strategy for strategy in self._state_data["strategies"] if strategy["account_id"] == account_id]

    def _intents(self, account_id: str) -> list[dict[str, Any]]:
        return [intent for intent in self._state_data["intents"] if intent["account_id"] == account_id]

    def _snapshot_fresh(self, account_id: str) -> bool:
        snapshot = self._snapshots.get(account_id)
        return bool(snapshot and not snapshot.get("preview_only") and self._account_fresh(account_id))

    def _account_fresh(self, account_id: str) -> bool:
        snapshot = self._snapshots.get(account_id)
        return bool(snapshot and snapshot.get("valid") and 0 <= self._clock() - snapshot["fetched_at"] <= SNAPSHOT_MAX_AGE)

    def _account(self, account_id: str | None) -> dict[str, Any] | None:
        snapshot = self._snapshots.get(str(account_id))
        if not snapshot:
            return None
        assets = snapshot["assets"]
        realized = sum((_decimal(strategy["realized_pnl"]) for strategy in self._strategies(str(account_id))), D(0))
        return {"account_id": str(account_id), "currency": "USD", "balance": assets.get("balance"), "equity": assets.get("equity"), "margin": assets.get("margin"), "free_margin": assets.get("margin_free"), "unrealized_pnl": assets.get("unrealized_pnl"), "realized_pnl": _money(realized), "realized_pnl_scope": "console_owned_positions", "reserved_margin": None, "available_for_new_strategy": assets.get("margin_free"), "status": snapshot["account"].get("status"), "source": "gate", "stale": not self._account_fresh(str(account_id)), "updated_at": _iso(snapshot["fetched_at"])}

    def _capacity(self, account_id: str, requested: int) -> dict[str, Any]:
        snapshot = self._snapshots.get(account_id, {})
        positions = snapshot.get("positions", [])
        owned_order_ids = {intent.get("remote_order_id") for intent in self._intents(account_id)}
        external_orders = sum(str(order.get("order_id")) not in owned_order_ids for order in snapshot.get("orders", []))
        reserved = len(positions) + external_orders
        for strategy in self._strategies(account_id):
            for cell in strategy["cells"]:
                if cell["status"] in {"pending", "submitting", "reconciling", "unknown", "partial"} or (strategy["status"] in ACTIVE and cell.get("enabled", True) and cell["status"] in {"waiting", "armed", "ready"}):
                    reserved += 1
        return {"limit": CAPACITY, "reserved": reserved, "requested": requested, "available": max(0, CAPACITY - reserved), "sufficient": reserved + requested <= CAPACITY}

    def _quote_problem(self, config: GridConfig, spec: dict[str, Any], market: dict[str, Any]) -> str | None:
        if market.get("symbol") != config.symbol or spec.get("symbol") != config.symbol:
            return "行情或品种详情与当前策略不一致"
        stamp = _epoch(market.get("received_at"))
        if market.get("stale") or stamp is None or not 0 <= self._clock() - stamp <= QUOTE_MAX_AGE:
            return "行情已过期，等待最新买卖报价后才能新开仓"
        if market.get("source") != "gate" or spec.get("source") != "gate":
            return "实盘策略需要 Gate 官方行情与真实品种详情"
        if market.get("trading_status", market.get("status")) != "open":
            return "当前品种已休市或交易状态不可用"
        mode = str(market.get("trade_mode", spec.get("trade_mode", "0")))
        if mode not in ({"1", "4"} if config.direction == "long" else {"2", "4"}):
            return "当前品种交易模式不允许该方向新开仓"
        try:
            bid, ask = _decimal(market.get("bid")), _decimal(market.get("ask"))
            if bid <= 0 or ask < bid:
                return "买卖报价无效"
        except ValueError:
            return "买卖报价无效"
        return None

    def preview(self, config: GridConfig | dict[str, Any], spec: dict[str, Any], market: dict[str, Any], connected: bool | dict[str, Any] = False, account_id: str | None = None) -> dict[str, Any]:
        config = config if isinstance(config, GridConfig) else GridConfig.model_validate(config)
        if not spec or spec.get("symbol") != config.symbol or not spec.get("complete", False):
            raise ValueError("请先连接 Gate 账户以读取该品种真实交易详情")
        tick = _decimal(spec.get("tick_size"))
        minimum = _decimal(_spec_value(spec, "volume_min", "min_order_volume"))
        maximum = _decimal(_spec_value(spec, "volume_max", "max_order_volume"))
        if tick <= 0 or minimum <= 0 or maximum < minimum:
            raise ValueError("Gate 品种精度或手数范围无效")
        if not minimum <= config.volume <= maximum:
            raise ValueError(f"每格手数必须介于 {minimum} 与 {maximum} 之间")
        volume_step = spec.get("volume_step")
        if volume_step is not None and (step := _decimal(volume_step)) > 0 and config.volume % step != 0:
            raise ValueError(f"每格手数必须符合官方数量步长 {step}")
        for label, value in (("区间下限", config.lower_price), ("区间上限", config.upper_price), ("止损价", config.stop_loss)):
            if value is not None and value % tick:
                raise ValueError(f"{label}必须符合官方价格精度 {tick}")
        with localcontext() as ctx:
            ctx.prec = 48
            if config.spacing == "arithmetic":
                increment = (config.upper_price - config.lower_price) / config.grid_count
                levels = [rounded(config.lower_price + increment * index, tick) for index in range(config.grid_count + 1)]
            else:
                ratio = config.upper_price / config.lower_price
                levels = [rounded(config.lower_price * ctx.power(ratio, D(index) / config.grid_count), tick) for index in range(config.grid_count + 1)]
        levels[0], levels[-1] = config.lower_price, config.upper_price
        if any(a >= b for a, b in zip(levels, levels[1:])):
            raise ValueError("价格区间不足以容纳网格，请减少格数或扩大区间")
        bid, ask = _decimal(market.get("bid")), _decimal(market.get("ask"))
        cells, estimates = [], []
        currency = _spec_value(spec, "settlement_currency", "currency")
        for index in range(config.grid_count):
            entry, target = (levels[index], levels[index + 1]) if config.direction == "long" else (levels[index + 1], levels[index])
            margin = _estimate(entry, config.volume, spec)
            estimates.append(margin)
            try:
                gross = abs(target - entry) * config.volume * _decimal(spec.get("contract_size")) if currency == "USD" else None
            except ValueError:
                gross = None
            cells.append({"index": index, "entry_price": _text(entry), "take_profit": _text(target), "eligible": entry < ask if config.direction == "long" else entry > bid, "estimated_margin": _money(margin), "gross_profit": _money(gross)})
        estimated = sum(estimates, D(0)) if all(value is not None for value in estimates) else None
        uid = str(account_id or (connected.get("account_id") if isinstance(connected, dict) else "") or self.current_uid or "")
        account = self._account(uid)
        blockers, blocker_codes = [], []
        def block(code, message):
            blocker_codes.append(code)
            blockers.append(message)
        warnings = ["已确认的原生挂单与仓位止盈止损由 Gate 执行，关闭后台不会撤单，停机期间仍可能开仓；新一轮补单与未提交格位需要后台在线。", "止盈委托与仓位全平记录核对一致后补回原生挂单，实际成交价可能含滑点；普通平仓、强平或原因待核对时不自动补单。"]
        if volume_step is None:
            warnings.append("官方未提供数量步长；已校验最小和最大手数，最终由 Gate 校验下单数量。")
        if estimated is None:
            warnings.append("当前品种无法可靠换算美元保证金，未作资金占用估算；交易所会在下单时检查实际保证金。")
        quote_problem = self._quote_problem(config, spec, market)
        if quote_problem:
            block("market_unavailable", quote_problem)
        connected_ok = bool(connected.get("connected")) if isinstance(connected, dict) else bool(connected)
        fresh_account = bool(uid and self._account_fresh(uid))
        free = _decimal(account["free_margin"]) if account and account.get("free_margin") is not None else None
        sufficient_margin = free >= estimated if free is not None and estimated is not None else None
        if not connected_ok or not uid:
            block("connection_required", "请连接并核验真实账户，才能开始策略。")
        elif not fresh_account:
            block("account_sync_required", "Gate 已连接，账户状态正在同步，请稍后重试预览。")
        if fresh_account and free is None:
            block("account_assets_unavailable", "账户可用保证金数据尚未取得，请重新同步账户。")
        if free is not None and (free <= 0 or sufficient_margin is False):
            block("insufficient_margin", "真实账户可用保证金不足。")
        capacity = self._capacity(uid, config.grid_count)
        if not capacity["sufficient"]:
            block("capacity_exceeded", f"账户现有仓位、委托与本地待触发格位已预留 {capacity['reserved']} 个名额，新增后将超过 {CAPACITY} 个。")
        in_use = self._symbol_in_use(config.symbol, uid)
        if in_use:
            block("symbol_in_use", "该品种已有活动策略、待核对执行或策略残留仓位。")
        unresolved = self.has_unresolved_execution(uid) if uid else False
        if unresolved:
            block("execution_unresolved", "账户存在执行结果待核对的操作，核对完成前禁止新开仓。")
        profits = [_decimal(cell["gross_profit"]) for cell in cells if cell["gross_profit"] is not None]
        return {"config": config.model_dump(mode="json"), "levels": [_text(level) for level in levels], "cells": cells, "grid_count": config.grid_count, "level_count": len(levels), "eligible_count": sum(cell["eligible"] for cell in cells), "waiting_count": sum(not cell["eligible"] for cell in cells), "total_volume": _text(config.volume * config.grid_count), "estimated_margin": _money(estimated), "estimated_max_margin": _money(estimated), "profit_per_grid_min": _money(min(profits)) if profits else None, "profit_per_grid_max": _money(max(profits)) if profits else None, "leverage": spec.get("leverage"), "leverage_editable": False, "source": "gate", "mode": "live", "account": account, "sufficient_margin": sufficient_margin, "sufficient_capacity": capacity["sufficient"], "capacity": capacity, "can_start": not blockers, "blockers": blockers, "blocker_codes": blocker_codes, "warnings": warnings, "estimate_notice": "仅在美元结算时估算名义保证金；不含手续费、隔夜费、滑点或交易所动态风险要求。"}

    def _symbol_in_use(self, symbol: str, account_id: str) -> bool:
        return any(strategy["config"]["symbol"] == symbol and strategy["status"] in ACTIVE for strategy in self._strategies(account_id)) or any(intent["symbol"] == symbol and intent["status"] not in {"closed", "closed_partial", "rejected", "cancelled"} for intent in self._intents(account_id))

    async def refresh_preview_account(self, gate, account_id: str):
        """Read current account data without history pagination or reconciliation.

        This snapshot authorizes a preview only. Execution still requires the
        full reconciliation path and its unchanged three-second preflight.
        """
        uid = _uid(account_id)
        async with self._lock:
            if not self._account_fresh(uid):
                await self._refresh(gate, uid, force=True, include_history=False)

    def exit_history_coverage(self, position, account_id):
        snapshot = self._snapshots.get(account_id, {})
        coverage = snapshot.get("exit_history_coverage")
        if coverage is None:
            # Complete snapshots injected by callers/tests predate window metadata.
            return {"complete": True}
        item = coverage.get(str(position.get("position_id")))
        if item is not None:
            return item
        return {"complete": bool(snapshot.get("order_history_default_complete")),
                "reason": "默认订单历史可能仅含最近 10 条，尚未取得该平仓时段的完整记录"}

    @staticmethod
    def _order_in_window(row, window):
        # Gate filters creation time; exit attribution separately uses time_done.
        stamp = _epoch(row.get("time_setup"))
        return (row.get("symbol") == window["symbol"] and str(row.get("side")) == str(window["side"])
                and stamp is not None and window["begin_time"] <= int(stamp) <= window["end_time"])

    async def _order_exit_windows(self, gate, account_id, history, default_orders):
        """Bounded, account-scoped queries for owned closed positions.

        Keep every raw row returned by a window, including external and incomplete
        candidates. Dropping competitors would manufacture a unique TP match.
        """
        windows = copy.deepcopy(self._state_data["history_windows"].get(account_id, {}))
        strategies = {s["id"]: s for s in self._strategies(account_id)}
        owned = {pid: i for i in self._intents(account_id) for pid in i.get("position_ids", [])}
        history_by_pid = {str(row.get("position_id")): row for row in history}
        targets = {}
        for row in history:
            pid = str(row.get("position_id"))
            intent = owned.get(pid)
            stamp = _epoch(row.get("time_close"))
            direction = _direction(row.get("position_dir"))
            if intent is None or stamp is None or direction is None or str(row.get("position_status")) != "1":
                continue
            side, second = (1 if direction == "long" else 2), int(stamp)
            key = f"{row['symbol']}:{side}:{second - 1}:{second + 1}"
            target = targets.setdefault(key, {"symbol": row["symbol"], "side": side, "begin_time": second - 1,
                                              "end_time": second + 1, "position_ids": [], "priority": 1})
            target["position_ids"].append(pid)
            if strategies[intent["strategy_id"]]["status"] not in {"stopped", "completed"}:
                target["priority"] = 0

        due = []
        for key, target in targets.items():
            entry = windows.get(key)
            if entry:
                if 'response_order_ids' not in entry:
                    entry.update(frozen=False, complete=False, retry_after=0)
                recorded = {str(row.get("order_id")): row for row in entry.get("rows", [])}
                changed = any(self._order_in_window(row, entry) and recorded.get(str(row.get("order_id"))) != row
                              for row in default_orders)
                if changed:
                    entry["frozen"] = False
                    entry["complete"] = False
                    entry["retry_after"] = 0
                if set(target["position_ids"]) != set(entry.get("position_ids", [])):
                    entry["frozen"] = False
                    entry["retry_after"] = 0
            if entry and entry.get("frozen"):
                continue
            if not entry or self._clock() >= entry.get("retry_after", 0):
                due.append((target["priority"], entry.get("queried_at", 0) if entry else 0, key))
        for _, _, key in sorted(due)[:EXIT_HISTORY_REQUEST_BUDGET]:
            target = targets[key]
            prior = windows.get(key, {})
            radius = prior.get("radius", 1)
            response_has_candidate = False
            for row in prior.get("raw_response_rows", []):
                order_key = self._partial_exit_key(row, order=True)
                for pid in target["position_ids"]:
                    position = history_by_pid[pid]
                    position_key = self._partial_exit_key(position)
                    if (order_key is not None and position_key is not None
                            and all(a is None or b is None or a == b for a, b in zip(order_key, position_key))
                            and self._exit_trigger_possible(row, position)):
                        response_has_candidate = True
            if prior and prior.get("response_count") is not None and prior["response_count"] < ORDER_HISTORY_RESULT_LIMIT and not response_has_candidate:
                radius = 5 if radius < 5 else 30
            center = target["begin_time"] + 1
            # Close orders can be created several seconds before execution.
            # Expand the look-back, retaining a small upper bound after close.
            entry = {**target, "begin_time": center - radius, "radius": radius,
                     "queried_at": self._clock(), "retry_after": self._clock() + EXIT_HISTORY_RETRY_SECONDS,
                     "complete": False, "frozen": False, "rows": prior.get("rows", [])}
            try:
                response = await gate.order_history(symbol=target["symbol"], side=target["side"],
                                                    begin_time=entry["begin_time"], end_time=entry["end_time"])
                rows = _list(response)
                # A ten-row result can itself be truncated. Never infer complete
                # coverage from deduplicating it or from a larger merged result.
                valid_scope = all(self._order_in_window(row, entry) for row in rows)
                returned_ids = {str(row.get("order_id")) for row in rows if _id(row.get("order_id"))}
                valid_ids = len(returned_ids) == len(rows)
                known = {str(row["order_id"]): row for row in prior.get("rows", []) if _id(row.get("order_id"))}
                known.update({str(row["order_id"]): row for row in default_orders if _id(row.get("order_id")) and self._order_in_window(row, entry)})
                required_ids = {oid for oid, row in known.items() if self._order_in_window(row, entry)
                                and str(row.get("order_opt_type")) not in {"1", "2"}}
                contradicted = bool(required_ids - returned_ids)
                kept_rows = dict(known)
                kept_rows.update({str(row["order_id"]): row for row in rows if _id(row.get("order_id"))})
                # A filtered response omitting an already-observed same-window
                # record is not proof of completeness; retain that raw evidence.
                entry.update(rows=list(kept_rows.values()), raw_response_rows=copy.deepcopy(rows), response_count=len(rows), response_order_ids=sorted(returned_ids),
                             complete=len(rows) < ORDER_HISTORY_RESULT_LIMIT and valid_scope and valid_ids and not contradicted)
                entry["reason"] = ("该平仓时间窗口返回至少 10 条记录，可能被截断，需继续核对" if len(rows) >= ORDER_HISTORY_RESULT_LIMIT
                                   else "时间窗口内订单编号缺失或重复，暂不能确认唯一关联" if not valid_ids
                                   else "历史接口返回了窗口范围外记录，暂不能确认覆盖完整" if not valid_scope
                                   else "该时间窗口缺少已经观测到的订单，覆盖范围仍待核对" if contradicted else None)
            except (GateError, ValueError):
                entry["reason"] = "该平仓时段历史读取暂未完成，后台将继续核对"
            windows[key] = entry

        merged = dict(self._state_data["owned_order_history"].get(account_id, {}))
        for entry in windows.values():
            merged.update({str(row["order_id"]): row for row in entry.get("rows", []) if _id(row.get("order_id"))})
        merged.update({str(row["order_id"]): row for row in default_orders if _id(row.get("order_id"))})
        coverage = {}
        for key, target in targets.items():
            entry = windows.get(key, {})
            for pid in target["position_ids"]:
                coverage[pid] = {"complete": bool(entry.get("complete")), "window": key,
                                 "order_ids": entry.get("response_order_ids", []),
                                 "reason": entry.get("reason") or "该平仓时段历史正在排队核对"}
        return list(merged.values()), windows, coverage, targets

    def _save_history_evidence(self, account_id, windows, targets, history, orders):
        by_position = {str(row.get("position_id")): row for row in history}
        owned = {pid: intent for intent in self._intents(account_id) for pid in intent.get("position_ids", [])}
        for key, target in targets.items():
            entry = windows.get(key)
            if not entry or not entry.get("complete") or entry.get("frozen"):
                continue
            confirmed = []
            for pid in target["position_ids"]:
                intent = owned[pid]
                expected = intent.get("position_protection", {}).get(pid, intent["request"])
                result = self.classify_exit(by_position[pid], account_id, expected)
                confirmed.append(result["confirmed"] and result.get("order_id") in entry.get("response_order_ids", []))
            if confirmed and all(confirmed):
                entry.update(frozen=True, position_ids=target["position_ids"], validated_at=self._clock())
        owned_ids = {intent.get("remote_order_id") for intent in self._intents(account_id) if intent.get("remote_order_id")}
        saved_orders = dict(self._state_data["owned_order_history"].get(account_id, {}))
        for row in orders:
            if str(row.get("order_id")) in owned_ids and str(row.get("state")) in {"2", "4", "5"}:
                saved_orders[str(row["order_id"])] = copy.deepcopy(row)
        if windows != self._state_data["history_windows"].get(account_id, {}) or saved_orders != self._state_data["owned_order_history"].get(account_id, {}):
            self._state_data["history_windows"][account_id] = windows
            self._state_data["owned_order_history"][account_id] = saved_orders
            self._persist()

    async def _refresh(self, gate, account_id: str, *, force=False, include_history=True):
        if not force and self._snapshot_fresh(account_id):
            return
        try:
            previous = self._snapshots.get(account_id, {})
            history, order_history = previous.get("history", []), previous.get("order_history", [])
            history_complete = previous.get("history_complete", False)
            coverage = previous.get("exit_history_coverage", {})
            default_complete = previous.get("order_history_default_complete", False)
            windows, targets = None, None
            if include_history:
                history_response, order_history_response = await asyncio.gather(gate.position_history(page=1, page_size=100), gate.order_history())
                history = _list(history_response)
                pages = int(_data(history_response).get("total_page", 1) or 1)
                for page in range(2, min(pages, 10) + 1):
                    history.extend(_list(await gate.position_history(page=page, page_size=100)))
                default_orders, history_complete = _list(order_history_response), pages <= 10
                default_complete = len(default_orders) < ORDER_HISTORY_RESULT_LIMIT
                order_history, windows, coverage, targets = await self._order_exit_windows(gate, account_id, history, default_orders)

            # History can involve many pages. Current assets and inventory must
            # be read afterwards, not made to appear fresh by a late history read.
            received_at = []
            async def current_read(call):
                response = await call()
                received_at.append(self._clock())
                return response
            account_response, asset_response, position_response, order_response = await asyncio.gather(
                current_read(gate.account), current_read(gate.assets), current_read(gate.positions), current_read(gate.orders))
            account, assets = _data(account_response), _data(asset_response)
            if _uid(account.get("mt5_uid")) != account_id or _uid(assets.get("mt5_uid")) != account_id:
                raise ValueError("当前 API 密钥与策略绑定的 MT5 账户不一致，已禁止交易")
            if str(account.get("status")) != "3":
                raise ValueError("Gate CFD 账户状态不允许交易")
            positions, orders = _list(position_response), _list(order_response)
            if any(not _id(position.get("position_id")) for position in positions) or any(not _id(order.get("order_id")) for order in orders):
                raise ValueError("Gate 仓位或订单编号不完整，已禁止新开仓")
            _decimal(assets.get("margin_free"))
            exchange_time = _epoch(position_response.get("timestamp") or _data(position_response).get("timestamp") or asset_response.get("timestamp"))
            if exchange_time is None:
                raise ValueError("Gate 快照缺少服务端时间，无法可靠关联新仓位")
            self._snapshots[account_id] = {"account": account, "assets": assets, "positions": positions, "orders": orders, "history": history, "order_history": order_history, "history_complete": history_complete, "exchange_time": exchange_time, "fetched_at": min(received_at), "valid": True, "preview_only": not include_history, "exit_history_coverage": coverage, "order_history_default_complete": default_complete}
            self.current_uid = account_id
            if include_history:
                self._save_history_evidence(account_id, windows, targets, history, order_history)
        except BaseException:
            if account_id in self._snapshots:
                self._snapshots[account_id]["valid"] = False
            raise

    async def reconcile(self, gate, account_id: str) -> dict[str, Any]:
        uid = _uid(account_id)
        async with self._lock:
            await self._refresh(gate, uid, force=True)
            await self._resolve_all(gate, uid)
            self._persist()
            return self.state(next((s["config"]["symbol"] for s in reversed(self._strategies(uid))), "XAUUSD"), uid)

    async def start(self, config: GridConfig | dict[str, Any], request_id: str, gate, market: dict[str, Any], spec: dict[str, Any], account_id: str) -> dict[str, Any]:
        config = config if isinstance(config, GridConfig) else GridConfig.model_validate(config)
        uid = _uid(account_id)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise ValueError("启动请求编号无效")
        fingerprint = hashlib.sha256(json.dumps(config.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
        receipt_key = uid + ":" + request_id
        async with self._lock:
            await self._refresh(gate, uid, force=True)
            await self._resolve_all(gate, uid)
            if callable(self.quote_provider):
                market = self.quote_provider(config.symbol) or market
            self._markets[config.symbol], self._specs[config.symbol] = copy.deepcopy(market), copy.deepcopy(spec)
            receipt = self._state_data["receipts"].get(receipt_key)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise ValueError("同一请求编号不能用于不同参数")
                return {**self.state(config.symbol, uid), "strategy_id": receipt["strategy_id"], "idempotent_replay": True}
            preview = self.preview(config, spec, market, True, uid)
            if not preview["can_start"]:
                raise ValueError(preview["blockers"][-1] if preview["blockers"] else "当前条件不允许启动实盘策略")
            current = market["ask"] if config.direction == "long" else market["bid"]
            strategy = {"id": identifier("live"), "account_id": uid, "config": config.model_dump(mode="json"), "execution_mode": "native_trigger", "status": "starting", "created_at": now(), "stopped_at": None, "source": "gate", "levels": preview["levels"], "cells": [{**cell, "status": "ready" if cell["eligible"] else "waiting", "intent_id": None, "completed_cycles": 0, "generation": 0, "enabled": True} for cell in preview["cells"]], "realized_pnl": "0", "completed_cycles": 0, "close_requested": False}
            self._state_data["strategies"].append(strategy)
            self._state_data["receipts"][receipt_key] = {"fingerprint": fingerprint, "strategy_id": strategy["id"]}
            self._log(uid, f"{config.symbol} 原生网格已创建，后台将分批提交 {preview['eligible_count']} 个当前适格的 Gate 挂单。")
            self._persist()
            return {**self.state(config.symbol, uid), "strategy_id": strategy["id"], "idempotent_replay": False}

    def _pause(self, strategy, message: str):
        previous_error = strategy.get("error")
        if strategy["status"] not in {"stopped", "completed", "stopping", "closing", "legacy_paused"}:
            strategy["status"] = "paused"
        strategy["error"] = message
        if previous_error != message:
            self._log(strategy["account_id"], message, "warning")

    def _stop_local(self, strategy):
        strategy["status"] = "closing" if strategy.get("close_requested") else "stopping"
        strategy["stop_requested"] = True
        for cell in strategy["cells"]:
            if cell["status"] in {"armed", "waiting", "ready"}:
                cell["status"] = "cancelled"

    def state(self, symbol: str = "XAUUSD", account_id: str | None = None) -> dict[str, Any]:
        uid = str(account_id or self.current_uid or "")
        with self._read_lock:
            strategies = copy.deepcopy(self._strategies(uid))
            intents = self._intents(uid)
            snapshot = self._snapshots.get(uid, {})
            by_position = {pid: intent for intent in intents for pid in intent.get("position_ids", [])}
            by_order = {intent["remote_order_id"]: intent for intent in intents if intent.get("remote_order_id")}
            positions = []
            for position in snapshot.get("positions", []):
                pid = str(position["position_id"])
                intent = by_position.get(pid)
                positions.append({"id": pid, "position_id": pid, "symbol": position["symbol"], "strategy_id": intent["strategy_id"] if intent else None, "grid_index": intent["grid_index"] if intent else None, "direction": _direction(position.get("position_dir")), "entry_price": position.get("price_open"), "take_profit": position.get("price_tp"), "stop_loss": position.get("price_sl"), "volume": position.get("volume"), "unrealized_pnl": position.get("unrealized_pnl"), "margin": position.get("margin"), "opened_at": _iso(position.get("time_create")), "source": "gate", "managed": bool(intent)})
            orders = []
            for strategy in strategies:
                own = [p for p in positions if p["strategy_id"] == strategy["id"]]
                strategy["pending_count"] = sum(by_order.get(str(row.get("order_id")), {}).get("strategy_id") == strategy["id"] for row in snapshot.get("orders", []))
                strategy["waiting_count"] = sum(cell["status"] == "waiting" for cell in strategy["cells"])
                planned = [cell for cell in strategy["cells"] if cell.get("enabled", True)]
                submitted = sum(cell.get("intent_id") is not None for cell in planned)
                strategy["placement"] = {"total": len(planned), "eligible": sum(cell["status"] != "waiting" for cell in planned), "submitted": submitted, "confirmed": strategy["pending_count"], "waiting": strategy["waiting_count"], "remaining": sum(cell["status"] in {"ready", "submitting", "reconciling"} for cell in planned)}
                strategy["planned_count"], strategy["placed_count"] = len(planned), submitted
                strategy["position_count"] = len(own)
                strategy["unrealized_pnl"] = _money(sum((_decimal(p["unrealized_pnl"]) for p in own if p["unrealized_pnl"] is not None), D(0)))
                strategy.update(self.resume_eligibility(strategy, uid))
                for cell in strategy["cells"]:
                    if cell["status"] not in {"armed", "ready", "waiting", "submitting", "reconciling", "unknown"}:
                        continue
                    cell_intent = next((i for i in intents if i["id"] == cell.get("intent_id")), None)
                    if cell_intent and any(str(row.get("order_id")) == cell_intent.get("remote_order_id") for row in snapshot.get("orders", [])):
                        continue
                    orders.append({"id": f"{strategy['id']}:{cell['index']}", "strategy_id": strategy["id"], "symbol": strategy["config"]["symbol"], "grid_index": cell["index"], "side": "buy" if strategy["config"]["direction"] == "long" else "sell", "price": cell["entry_price"], "take_profit": cell["take_profit"], "stop_loss": cell.get("stop_loss", strategy["config"].get("stop_loss")), "volume": strategy["config"]["volume"], "status": cell["status"], "source": "local_plan", "managed": True, "created_at": strategy["created_at"]})
            for order in snapshot.get("orders", []):
                oid = str(order["order_id"])
                intent = by_order.get(oid)
                orders.append({"id": oid, "order_id": oid, "strategy_id": intent["strategy_id"] if intent else None, "symbol": order.get("symbol"), "grid_index": intent["grid_index"] if intent else None, "side": "buy" if str(order.get("side")) == "2" else "sell", "price": order.get("price"), "take_profit": order.get("price_tp"), "stop_loss": order.get("price_sl"), "volume": order.get("volume"), "status": "partial" if str(order.get("state")) == "3" else "pending", "remote_state": order.get("state"), "source": "gate", "managed": bool(intent), "created_at": _iso(order.get("time_setup"))})
            fills = []
            # Gate pages can arrive in either order. Present the newest closed
            # position first, with a stable ticket tie-breaker for equal times.
            history_rows = sorted(snapshot.get("history", []), key=lambda row: (
                _epoch(row.get("time_close")) or 0,
                int(_id(row.get("position_id")) or 0)), reverse=True)
            for row in history_rows:
                pid = str(row.get("position_id"))
                intent = by_position.get(pid)
                expected_protection = intent.get("position_protection", {}).get(pid, intent["request"]) if intent else None
                exit_evidence = self.classify_exit(row, uid, expected_protection)
                fills.append({"id": pid, "position_id": pid, "strategy_id": intent["strategy_id"] if intent else None, "grid_index": intent["grid_index"] if intent else None, "symbol": row.get("symbol"), "direction": _direction(row.get("position_dir")), "entry_price": row.get("price_open"), "price": row.get("close_price"), "volume": row.get("volume_closed"), "pnl": row.get("realized_pnl"), "time": _iso(row.get("time_close")), "type": exit_evidence["type"], "exit_confirmed": exit_evidence["confirmed"], "exit_order_id": exit_evidence.get("order_id"), "exit_source": exit_evidence.get("source"), "exit_reason": exit_evidence.get("reason"), "source": "gate", "managed": bool(intent)})
            selected = next((strategy for strategy in reversed(strategies) if strategy["config"]["symbol"] == symbol), None)
            unresolved_operations = [{key: op.get(key) for key in ("id", "strategy_id", "kind", "resource_id", "status", "error")} for op in self._state_data["operations"] if op["account_id"] == uid and op["status"] in {"prepared", "submitted", "unknown"}]
            return copy.deepcopy({"mode": "live", "source": "gate", "account_id": uid or None, "current_uid": self.current_uid, "account": self._account(uid), "market": self._markets.get(symbol), "spec": self._specs.get(symbol), "symbols": list(self._specs.values()), "strategies": strategies, "selected_strategy": selected, "orders": orders, "positions": positions, "fills": fills, "logs": [log for log in self._state_data["logs"] if log["account_id"] == uid], "templates": self.list_templates(), "unresolved_operations": unresolved_operations, "has_unresolved_execution": self.has_unresolved_execution(uid) if uid else False, "server_time": now()})

    def list_templates(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._state_data["templates"])

    def save_template(self, name: str, config: GridConfig | dict[str, Any]) -> dict[str, Any]:
        config = config if isinstance(config, GridConfig) else GridConfig.model_validate(config)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError("模板名称应为 1–60 个字符")
        if len(self._state_data["templates"]) >= 100:
            raise ValueError("最多保存 100 个参数模板")
        template = {"id": identifier("tpl"), "name": name.strip(), "config": config.model_dump(mode="json"), "created_at": now()}
        self._state_data["templates"].append(template)
        self._persist()
        return copy.deepcopy(template)

    def delete_template(self, template_id: str) -> dict[str, Any]:
        if not any(template["id"] == template_id for template in self._state_data["templates"]):
            raise ValueError("模板不存在")
        self._state_data["templates"] = [template for template in self._state_data["templates"] if template["id"] != template_id]
        self._persist()
        return {"deleted": True, "id": template_id}

    def close(self):
        with self._read_lock:
            self._db.close()


from .native import NativeRuntime


class LiveEngine(NativeRuntime, _LiveBase):
    pass
