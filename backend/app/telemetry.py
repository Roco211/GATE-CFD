"""Bounded, passive request timings containing no exchange payloads or IDs.

``sequence`` is a completion cursor; ``call_id`` is a separate local start ID.
Nested clock probes link to ``parent_call_id``. Their elapsed time is already in
the parent's clock phase: use only ``is_root`` rows when aggregating logical-call
latency. Concurrent calls may overlap; summed request latency is not wall time.

The five phase counters partition each call's duration. ``processing_ms`` is
all time outside explicit clock/pacing/network/backoff regions, including final
preflight callbacks; it is not a CPU-time measurement. Network time includes the
HTTP client's complete request and response-body read, not just socket latency.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import math
import re
import time
from typing import Any


_SCOPE: ContextVar[str | None] = ContextVar("gate_timing_scope", default=None)
_PARENT: ContextVar[TimingSpan | None] = ContextVar("gate_timing_parent", default=None)
_PHASES = ("clock_wait_ms", "throttle_wait_ms", "network_ms", "backoff_ms", "processing_ms")
_STATIC_PATHS = frozenset({
    "/tradfi/symbols", "/tradfi/symbols/categories", "/tradfi/symbols/commissions",
    "/tradfi/symbols/detail", "/tradfi/users/mt5-account", "/tradfi/users/assets",
    "/tradfi/orders", "/tradfi/orders/history", "/tradfi/positions", "/tradfi/positions/history",
})
_DYNAMIC_PATHS = (
    (re.compile(r"/tradfi/symbols/[^/]+/tickers"), "/tradfi/symbols/{symbol}/tickers"),
    (re.compile(r"/tradfi/symbols/[^/]+/klines"), "/tradfi/symbols/{symbol}/klines"),
    (re.compile(r"/tradfi/orders/log/[^/]+"), "/tradfi/orders/log/{log_id}"),
    (re.compile(r"/tradfi/orders/[^/]+"), "/tradfi/orders/{order_id}"),
    (re.compile(r"/tradfi/positions/[^/]+/close"), "/tradfi/positions/{position_id}/close"),
    (re.compile(r"/tradfi/positions/[^/]+"), "/tradfi/positions/{position_id}"),
)


def endpoint_template(path: str) -> str:
    """Only return constants; an unknown path must never become diagnostic text."""
    if not isinstance(path, str):
        return "/tradfi/{endpoint}"
    path = path.split("?", 1)[0].split("#", 1)[0]
    if path in _STATIC_PATHS:
        return path
    for pattern, template in _DYNAMIC_PATHS:
        if pattern.fullmatch(path):
            return template
    return "/tradfi/{endpoint}"


@contextmanager
def timing_scope(label: str) -> Iterator[None]:
    """Label only this async context; labels must be application-generated IDs."""
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,95}", label):
        raise ValueError("Use a short application-generated timing scope")
    token = _SCOPE.set(label)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _rate_limit(headers: Mapping[str, str]) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for name, header in (
        ("limit", "x-gate-ratelimit-limit"),
        ("remaining", "x-gate-ratelimit-remaining"),
        ("reset", "x-gate-ratelimit-reset-timestamp"),
    ):
        value = headers.get(header)
        if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(r"\d+(?:\.\d+)?", value):
            continue
        number = float(value)
        if math.isfinite(number) and 0 <= number <= 1e16:
            # Reset is the raw numeric upstream timestamp, without unit guessing.
            result[name] = int(number) if number.is_integer() else number
    return result


class TimingRecorder:
    def __init__(self, *, capacity: int = 1000, timer: Callable[[], float] = time.perf_counter,
                 wall_clock: Callable[[], float] = time.time) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 1000:
            raise ValueError("Timing capacity must be between 1 and 1000")
        self._rows: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._timer, self._wall_clock = timer, wall_clock
        self._sequence = self._call_id = 0

    @property
    def sequence(self) -> int:
        return self._sequence

    def begin(self, method: str, path: str, signed: bool, *, redactions: Sequence[str] = ()) -> TimingSpan:
        self._call_id += 1
        parent = _PARENT.get()
        parent_id = parent.call_id if parent and parent.recorder is self and not parent.finished else None
        scope = _SCOPE.get()
        if scope and any(value and value in scope for value in redactions):
            scope = None
        return TimingSpan(self, self._call_id, parent_id, scope, method, path, signed)

    def snapshot(self, after_sequence: int = 0, scope: str | None = None) -> list[dict[str, Any]]:
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise ValueError("Invalid timing cursor")
        return deepcopy([row for row in self._rows
                         if row["sequence"] > after_sequence and (scope is None or row["scope"] == scope)])


class TimingSpan:
    def __init__(self, recorder: TimingRecorder, call_id: int, parent_id: int | None,
                 scope: str | None, method: str, path: str, signed: bool) -> None:
        self.recorder, self.call_id = recorder, call_id
        self.finished = False
        self._last = recorder._timer()
        self._phase = "processing_ms"
        self._elapsed = dict.fromkeys(_PHASES, 0.0)
        self._row = {
            "call_id": call_id, "parent_call_id": parent_id, "is_root": parent_id is None,
            "scope": scope, "started_at": recorder._wall_clock(),
            "method": method if method in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"} else "OTHER",
            "endpoint": endpoint_template(path), "signed": bool(signed),
            "attempts": 0, "status_code": None, "outcome": "error", "rate_limit": {},
        }
        self._token = _PARENT.set(self)

    def _advance(self, phase: str) -> None:
        current = self.recorder._timer()
        self._elapsed[self._phase] += max(0.0, current - self._last) * 1000
        self._last, self._phase = current, phase

    @contextmanager
    def measure(self, phase: str) -> Iterator[None]:
        previous = self._phase
        self._advance(phase)
        try:
            yield
        finally:
            self._advance(previous)

    def attempt(self) -> None:
        self._row["attempts"] += 1
        self._row["status_code"] = None
        self._row["rate_limit"] = {}

    def response(self, status: int, headers: Mapping[str, str]) -> None:
        self._row["status_code"] = status
        self._row["rate_limit"] = _rate_limit(headers)

    def finish(self, outcome: str) -> None:
        if self.finished:
            return
        self._advance(self._phase)
        self.finished = True
        _PARENT.reset(self._token)
        self.recorder._sequence += 1
        self._row.update(self._elapsed)
        self._row.update(sequence=self.recorder._sequence, total_ms=sum(self._elapsed.values()), outcome=outcome)
        self.recorder._rows.append(self._row)
