"""Bounded, in-memory diagnostics using only existing Gate GET adapters.

No trading engine mutation or response payload is used by the job. Timing
scopes isolate these samples from the account's normal background traffic.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import nullcontext, suppress
import copy
from datetime import datetime, timezone
import math
import re
import time
from typing import Any, Callable, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .gate import GateError


class DiagnosticsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(default="XAUUSD", min_length=1, max_length=30)
    samples: Literal[3, 5] = 3

    @field_validator("symbol")
    @classmethod
    def symbol_format(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]{0,29}", value):
            raise ValueError("交易品种格式无效")
        return value

    @field_validator("samples", mode="before")
    @classmethod
    def sample_count(cls, value: Any) -> int:
        if type(value) is not int or value not in (3, 5):
            raise ValueError("采样次数只能为 3 或 5")
        return value


ENDPOINTS = (
    ("ticker", "实时买卖报价", "/tradfi/symbols/{symbol}/tickers"),
    ("account", "账户状态", "/tradfi/users/mt5-account"),
    ("assets", "账户资产", "/tradfi/users/assets"),
    ("orders", "当前委托", "/tradfi/orders"),
    ("positions", "当前持仓", "/tradfi/positions"),
)
PHASES = ("total_ms", "network_ms", "throttle_wait_ms", "clock_wait_ms", "backoff_ms", "processing_ms")
SAFE_ENDPOINTS = {path for _, _, path in ENDPOINTS} | {
    "/tradfi/symbols", "/tradfi/symbols/detail", "/tradfi/symbols/commissions",
    "/tradfi/symbols/{symbol}/klines", "/tradfi/orders/{order_id}",
    "/tradfi/positions/{position_id}", "/tradfi/orders/log/{log_id}",
    "/tradfi/orders/history", "/tradfi/positions/history", "/tradfi/positions/{position_id}/close",
}
SAFE_ERRORS = {"credentials_missing", "clock_out_of_sync", "gate_disconnected", "rate_limited",
               "gate_error", "gate_rejected", "authentication_failed", "invalid_response", "http_error", "gate_rate_limited"}


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _stats(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {"count": len(ordered), "min": round(ordered[0], 3),
            "p50": round(ordered[max(0, math.ceil(len(ordered) * .5) - 1)], 3),
            "p95": round(ordered[max(0, math.ceil(len(ordered) * .95) - 1)], 3),
            "max": round(ordered[-1], 3), "avg": round(sum(ordered) / len(ordered), 3),
            "total": round(sum(ordered), 3)}


def submission_intervals(intents: list[dict]) -> dict:
    """Summarize persisted preparation times without exporting IDs or payloads."""
    earliest: dict[int, float] = {}
    for intent in intents:
        if intent.get('generation') != 0 or intent.get('not_sent'):
            continue
        index, timestamp = intent.get('grid_index'), _number(intent.get('submitted_at'))
        if type(index) is not int or not 0 <= index < 100 or timestamp is None:
            continue
        earliest[index] = min(timestamp, earliest.get(index, timestamp))
    stamps = sorted(earliest.values())
    values = [round((b - a) * 1000, 3) for a, b in zip(stamps, stamps[1:])]
    return {'status': 'measured' if values else 'not_measured', 'count': len(values),
            'values_ms': values, 'stats': _stats(values),
            'note': '取当前选中策略每格首轮持久化提交准备时间的相邻间隔；可能包含行情条件等待、后台对账和队列等待，不等同 HTTP 或成交确认延迟。'}


def _safe_timing(row: Any) -> dict[str, Any] | None:
    """Defence in depth: never copy unknown telemetry fields or concrete IDs."""
    if not isinstance(row, dict) or row.get("endpoint") not in SAFE_ENDPOINTS:
        return None
    method = row.get("method")
    if method not in ("GET", "POST", "PUT", "DELETE"):
        return None
    safe = {"method": method, "endpoint": row["endpoint"],
            "is_root": row.get("is_root", row.get("parent_sequence") is None) is True}
    for key in (*PHASES, "started_at", "attempts", "status_code"):
        value = _number(row.get(key))
        if value is not None:
            safe[key] = value
    if row.get("outcome") in {"success", "error", "failed", "cancelled", "rejected", "timeout", "unknown"}:
        safe["outcome"] = row["outcome"]
    limit = row.get("rate_limit")
    if isinstance(limit, dict):
        safe["rate_limit"] = {key: value for key in ("limit", "remaining", "reset")
                              if (value := _number(limit.get(key))) is not None}
    return safe


class DiagnosticsService:
    """One cancellable job shared by all UI/CLI clients of a running server."""
    def __init__(self, gate_provider: Callable[[], Any], *,
                 local_probe: Callable[[str], dict[str, Any]] | None = None,
                 locks: dict[str, asyncio.Lock] | None = None,
                 overall_timeout: float = 90, request_timeout: float = 8,
                 lock_timeout: float = .25, max_reports: int = 10):
        self._gate_provider, self._local_probe = gate_provider, local_probe
        self._locks = dict(locks or {})
        self._overall_timeout, self._request_timeout = overall_timeout, request_timeout
        self._lock_timeout, self._max_reports = lock_timeout, max_reports
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._task: asyncio.Task | None = None
        self._active_id: str | None = None
        self._closed = False

    def start(self, request: DiagnosticsRequest | dict[str, Any]) -> dict[str, Any]:
        request = request if isinstance(request, DiagnosticsRequest) else DiagnosticsRequest.model_validate(request)
        if self._closed:
            raise ValueError("后台正在关闭，无法启动诊断")
        # This method contains no await: allocation and deduplication are atomic
        # on the application's event loop, including simultaneous HTTP clicks.
        if self._task is not None and not self._task.done() and self._active_id:
            return self.get(self._active_id)
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "symbol": request.symbol, "samples": request.samples,
               "status": "queued", "created_at": _stamp(), "started_at": None, "finished_at": None,
               "progress": {"completed": 0, "total": len(ENDPOINTS) * request.samples, "stage": "等待诊断"},
               "report": None}
        self._jobs[job_id] = job
        while len(self._jobs) > self._max_reports:
            self._jobs.popitem(last=False)
        self._active_id = job_id
        self._task = asyncio.create_task(self._run(job_id, self._gate_provider()), name=f"readonly-diagnostics-{job_id}")
        return copy.deepcopy(job)

    def get(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            raise KeyError("诊断报告不存在或已超出保留数量")
        return copy.deepcopy(self._jobs[job_id])

    def latest(self) -> dict[str, Any] | None:
        return copy.deepcopy(next(reversed(self._jobs.values()))) if self._jobs else None

    async def cancel(self, job_id: str, *, reason: str = "user_cancelled") -> dict[str, Any]:
        if job_id not in self._jobs:
            raise KeyError("诊断报告不存在或已超出保留数量")
        if self._active_id == job_id and self._task is not None and not self._task.done():
            self._jobs[job_id]["cancel_reason"] = reason if reason in {"user_cancelled", "connection_changed", "shutdown"} else "user_cancelled"
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            # Cancellation before the task's first scheduling point never runs
            # its body/finally; retain a truthful empty partial report as well.
            if self._jobs[job_id]["status"] == "queued":
                job = self._jobs[job_id]
                job.update(status="cancelled", finished_at=_stamp())
                job["report"] = self._new_report(job, None)
                job["progress"]["stage"] = "诊断已取消"
        return self.get(job_id)

    async def cancel_active(self, *, reason: str = "user_cancelled") -> None:
        if self._active_id:
            await self.cancel(self._active_id, reason=reason)

    async def aclose(self) -> None:
        self._closed = True
        await self.cancel_active(reason="shutdown")

    def _new_report(self, job: dict, gate: Any) -> dict:
        interval = _number(getattr(gate, "_min_request_interval", None))
        return {"source": "gate", "read_only": True, "duration_ms": 0,
                "endpoint_stats": [{"endpoint": path, "label": label, "method": "GET", "count": 0,
                                     "success": 0, "failed": 0, "stats": {phase: None for phase in PHASES}, "samples": []}
                                    for _, label, path in ENDPOINTS],
                "stages": [], "findings": [], "estimates": [], "local_state": None,
                "recent_real_requests": {"status": "not_measured", "items": [],
                                         "note": "只展示已有真实写请求的 HTTP 耗时；单个 HTTP 响应不等于订单已接受、已挂出或已成交。"},
                "limits": {"overall_timeout_ms": self._overall_timeout * 1000,
                           "request_timeout_ms": self._request_timeout * 1000,
                           "requested_samples": job["samples"], "sample_limit": 5,
                           "read_requests_planned": job["progress"]["total"], "retained_reports": self._max_reports,
                           "rate_policy": "shared_gate_client", "min_request_interval_ms": None if interval is None else interval * 1000,
                           "observed_gate_limits": [],
                           "configured_max_qps": 1 / interval if interval else None,
                           "configured_max_qps_note": "仅由本机最小请求间隔推算，不代表 Gate 配额或可达到的吞吐量。",
                           "notes": ["逐个读取，不并发压测；与行情和交易后台共享节流。", "需要校时时可能附加只读 symbols 请求；原客户端的有限 GET 重试仍适用。",
                                     "DNS、TCP、TLS、交易所内部排队及订单确认耗时未分别测量。", "network_ms 包含 HTTP 往返与响应体读取；processing_ms 是其它客户端时间，不是纯 CPU 时间。",
                                     "rate_limit.reset 保留 Gate 响应头原始数字，未推测其秒或毫秒单位。", "3 或 5 次小样本的 p95 采用最近秩法，不代表长期延迟分布。", "报告仅保留在当前后台内存中，页面重开可查询，后台重启后清空。"],
                           "execution_read_model": {'kind': 'estimate', 'full_refresh_requests': 'P+5+W', 'per_grid_requests': '2*P+12+2*W',
                                                    'definitions': 'P=持仓历史页数，W=本次到期历史窗口查询数（0 至 2）；单格含前后两次完整读取、一次提交和一次队列日志核对。',
                                                    'assumptions': '该公式对应当前原生铺单代码的通常路径；未包含其它挂单日志核对、校时、重试、行情条件等待或后台竞争。'}}}

    @staticmethod
    def _records(gate, *, after: int = 0, scope: str | None = None) -> list[dict]:
        reader = getattr(gate, "timing_snapshot", None)
        if not callable(reader):
            return []
        try:
            return [safe for row in reader(after_sequence=after, scope=scope) if (safe := _safe_timing(row)) is not None]
        except Exception:
            return []

    async def _probe_local(self, job: dict, report: dict) -> None:
        for name, lock in self._locks.items():
            started = time.perf_counter()
            acquired = False
            try:
                async with asyncio.timeout(self._lock_timeout):
                    await lock.acquire()
                    acquired = True
                status, note = "measured", "取得后立即释放；未在锁内等待网络请求。"
            except TimeoutError:
                status, note = "timeout", "观察窗口内仍在等待；实际等待时长至少为本次测量值，未长时间排队。"
            finally:
                if acquired:
                    lock.release()
            report["stages"].append({"name": name, "label": {"op_lock": "操作队列等待", "market_lock": "行情队列等待"}.get(name, "本地队列等待"),
                                     "duration_ms": round((time.perf_counter() - started) * 1000, 3), "status": status, "note": note})
        if self._local_probe is not None:
            started = time.perf_counter()
            try:
                # Explicitly synchronous and cached-only; never await an engine
                # operation and never record raw account/strategy dictionaries.
                local = self._local_probe(job["symbol"])
                report["local_state"] = local.get("summary")
                report["stages"].extend(local.get("stages", []))
                report["stages"].append({"name": "cached_snapshot", "label": "本地缓存快照", "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                                         "status": "measured", "note": "仅读取现有缓存，没有发起对账、刷新账户或策略执行。"})
            except Exception:
                report["stages"].append({"name": "cached_snapshot", "label": "本地缓存快照", "duration_ms": None,
                                         "status": "not_measured", "note": "当前缓存不足，未进行此项测量。"})

    async def _read_sample(self, gate: Any, scope: str, name: str, symbol: str, index: int) -> tuple[dict, dict]:
        before = getattr(gate, "timing_sequence", 0)
        before = before if type(before) is int else 0
        scope_factory = getattr(gate, "timing_scope", None)
        context = scope_factory(scope) if callable(scope_factory) else nullcontext()
        started = time.perf_counter()
        status, error, status_code = "success", None, None
        try:
            with context:
                async with asyncio.timeout(self._request_timeout):
                    # This fixed dispatch table is the only place making Gate
                    # calls. No arbitrary method, engine operation or write is accepted.
                    if name == "ticker":
                        await gate.ticker(symbol)
                    elif name == "account":
                        await gate.account()
                    elif name == "assets":
                        await gate.assets()
                    elif name == "orders":
                        await gate.orders()
                    elif name == "positions":
                        await gate.positions()
        except TimeoutError:
            status, error = "timeout", "request_timeout"
        except GateError as exc:
            status, status_code = "failed", exc.status_code
            if exc.code == 'credentials_missing':
                error = 'credentials_missing'
            elif status_code == 429:
                error = 'gate_rate_limited'
            elif status_code in (401, 403) or exc.auth_invalid:
                error = 'authentication_failed'
            elif type(status_code) is int and status_code >= 500:
                error = 'gate_server_error'
            else:
                error = exc.code if exc.code in SAFE_ERRORS else "gate_error"
        except Exception:
            status, error = "failed", "read_failed"
        elapsed = (time.perf_counter() - started) * 1000
        records = [row for row in self._records(gate, after=before, scope=scope) if row.get("is_root")]
        phases = {phase: sum(row[phase] for row in records if phase in row) if records and all(phase in row for row in records) else None for phase in PHASES}
        if records and all(row.get('attempts') == 0 for row in records):
            # Validation/cooldown failures have a measured zero network phase,
            # but no HTTP latency sample. Do not make their p50 appear faster.
            phases['network_ms'] = None
        # End-to-end elapsed includes client validation and remains measurable
        # even when old adapters or early credential failures have no phase row.
        phases["total_ms"] = elapsed
        codes = [row['status_code'] for row in records if 'status_code' in row]
        rate_limits = [row['rate_limit'] for row in records if row.get('rate_limit')]
        attempts = sum(row['attempts'] for row in records if 'attempts' in row) if records and all('attempts' in row for row in records) else None
        return {"index": index, "status": status, "elapsed_ms": round(elapsed, 3), "error_code": error,
                "status_code": codes[-1] if codes else status_code if type(status_code) is int else None,
                "attempts": attempts, "rate_limit": rate_limits[-1] if rate_limits else None}, phases

    def _finalize(self, job: dict, gate: Any, started: float) -> None:
        report = job["report"]
        report["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        passive = [row for row in self._records(gate) if row.get("is_root") and row["method"] != "GET"][-50:]
        report["recent_real_requests"].update(status="measured" if passive else "not_measured", items=passive)
        observed = []
        for row in self._records(gate, scope=f"diag:{job['id']}"):
            if row.get('rate_limit'):
                item = {'endpoint': row['endpoint'], **row['rate_limit']}
                if item not in observed:
                    observed.append(item)
        report['limits']['observed_gate_limits'] = observed[-50:]
        failures = sum(row["failed"] for row in report["endpoint_stats"])
        if failures:
            report["findings"].append({"severity": "warning", "title": "部分只读采样未成功", "detail": f"共 {failures} 次读取失败或超时；详细错误类别已列在对应接口下，未保存响应内容。"})
        sample_errors = {sample['error_code'] for row in report['endpoint_stats'] for sample in row['samples']}
        if 'gate_rate_limited' in sample_errors:
            report['findings'].append({'severity': 'warning', 'title': '观察到 Gate 限流', 'detail': '接口采样触发或遇到了 Gate 的限流响应/冷却窗口；这与本机固定请求间隔等待是不同阶段，诊断没有绕过共享限流。'})
        if 'authentication_failed' in sample_errors:
            report['findings'].append({'severity': 'warning', 'title': '账户读取认证未通过', 'detail': '至少一次账户接口读取遇到认证或权限错误；不能将这类失败解释为网络延迟。'})
        phases = {phase: sum(row["stats"][phase]["total"] for row in report["endpoint_stats"] if row["stats"][phase] is not None) for phase in PHASES}
        if phases["throttle_wait_ms"] > 1:
            report["findings"].append({"severity": "info", "title": "测得共享节流等待", "detail": f"本次读取合计等待本机共享节流约 {phases['throttle_wait_ms']:.0f} 毫秒；正常后台请求也会占用同一队列。"})
        if phases["clock_wait_ms"] > 1:
            report["findings"].append({"severity": "info", "title": "测得校时等待", "detail": f"合计约 {phases['clock_wait_ms']:.0f} 毫秒，可能包含一次公共校时读取；已避免父子记录重复计入总时长。"})
        if any(stage["status"] == "timeout" for stage in report["stages"]):
            report["findings"].append({"severity": "warning", "title": "本地队列在观察时繁忙", "detail": "短暂探测未取得至少一个后台锁；本次结果是等待下界，不是完整队列延迟。"})
        if not passive:
            report["findings"].append({"severity": "info", "title": "真实交易请求耗时未测得", "detail": "当前客户端没有可用的近期写请求计时。诊断未发送任何测试订单，也不能由读取耗时推断成交确认速度。"})
        means = [row["stats"]["total_ms"]["avg"] for row in report["endpoint_stats"] if row["success"] and row["stats"]["total_ms"]]
        if len(means) == len(ENDPOINTS):
            report["estimates"].append({"label": "同样五个端点串行读取一轮", "value_ms": round(sum(means), 3), "kind": "estimate",
                                         "basis": "将各接口本次平均耗时相加；假设网络与队列负载不变，仅估算读取阶段，不代表下单或确认耗时。"})
        network_count = sum(row['stats']['network_ms']['count'] for row in report['endpoint_stats'] if row['stats']['network_ms'])
        if network_count:
            attempts = sum(sample.get('attempts') or 0 for row in report['endpoint_stats'] for sample in row['samples'])
            network_mean = phases['network_ms'] / (attempts or network_count)
            interval = report['limits']['min_request_interval_ms']
            if interval is not None:
                for pages, windows in ((1, 0), (3, 0), (3, 2)):
                    requests = 2 * pages + 12 + 2 * windows
                    report['estimates'].append({'label': f'每格准备与提交示例：P={pages}、W={windows}，约 {requests} 次请求',
                                                'value_ms': round(requests * max(interval, network_mean), 3), 'kind': 'estimate',
                                                'basis': f'请求数按 2×P+12+2×W；暂以 max(本机最小间隔 {interval:.0f}ms, 本次平均 GET HTTP {network_mean:.1f}ms) 作为单请求预算。将 GET 样本代入尚未测量的其它端点，仅作结构推算；未含重试、额外日志、CPU、行情等待及后台竞争，不能当作真实每格确认时延。'})
        if not any(row["stats"]["network_ms"] is not None for row in report["endpoint_stats"]):
            report["findings"].append({"severity": "info", "title": "细分计时不可用", "detail": "只测得调用总耗时；网络、节流和校时分项标为未测得，没有填入推测值。"})

    async def _run(self, job_id: str, gate: Any) -> None:
        job = self._jobs[job_id]
        started = time.perf_counter()
        job.update(status="running", started_at=_stamp(), report=self._new_report(job, gate))
        report = job["report"]
        values = [{phase: [] for phase in PHASES} for _ in ENDPOINTS]
        try:
            async with asyncio.timeout(self._overall_timeout):
                job["progress"]["stage"] = "读取本地队列与缓存"
                await self._probe_local(job, report)
                for index in range(1, job["samples"] + 1):
                    for position, (name, label, _) in enumerate(ENDPOINTS):
                        if self._gate_provider() is not gate:
                            job["cancel_reason"] = "connection_changed"
                            raise asyncio.CancelledError()
                        job["progress"]["stage"] = f"{label} · {index}/{job['samples']}"
                        sample, phases = await self._read_sample(gate, f"diag:{job_id}", name, job["symbol"], index)
                        endpoint = report["endpoint_stats"][position]
                        endpoint["samples"].append(sample)
                        endpoint["count"] += 1
                        endpoint["success" if sample["status"] == "success" else "failed"] += 1
                        for phase, value in phases.items():
                            if value is not None:
                                values[position][phase].append(value)
                            endpoint["stats"][phase] = _stats(values[position][phase])
                        job["progress"]["completed"] += 1
                        # Yield between reads even with synchronous test transports.
                        await asyncio.sleep(0)
                job["status"] = "completed"
                job["progress"]["stage"] = "诊断完成"
        except TimeoutError:
            job["status"] = "failed"
            job["progress"]["stage"] = "达到诊断时长上限"
            report["findings"].append({"severity": "warning", "title": "诊断已到时间上限", "detail": "保留已完成的采样结果；未继续等待或发送更多请求。"})
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            job["progress"]["stage"] = "诊断已取消"
        except Exception:
            job["status"] = "failed"
            job["progress"]["stage"] = "诊断未完成"
            report["findings"].append({"severity": "error", "title": "诊断过程未完成", "detail": "已保留部分计时。内部异常内容未写入报告。"})
        finally:
            self._finalize(job, gate, started)
            job["finished_at"] = _stamp()
