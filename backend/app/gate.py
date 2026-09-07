"""Narrow async adapter for the official Gate API v4 CFD REST endpoints.

Every endpoint returns the full response envelope, with identifiers converted to
strings so browser JSON cannot lose integer precision. Prices/volumes are decimal
strings. Credentials stay in this instance; errors retain only sanitized scalar
diagnostics, never response bodies, headers, or raw HTTP exceptions. No logging.

Raw write adapters are provided for the execution engine.
In particular, a create response's ``data.id`` is an unverified queue identifier,
not an order ID. The engine may use it as an order-log lookup candidate, but must
verify the returned ``log_id`` and submitted parameters before associating an
order. The engine owns reconciliation of asynchronous acceptance, completion,
and uncertain outcomes; this adapter never guesses identifier relationships.

References: https://www.gate.com/docs/developers/apiv4/en/cfd/
https://www.gate.com/docs/developers/apiv4/en/#api-signature-string-generation
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import math
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from .telemetry import TimingRecorder, TimingSpan, timing_scope


GATE_BASE_URL = "https://api.gateio.ws/api/v4"
_IDENTIFIER_FIELDS = {"id", "order_id", "log_id", "position_id", "mt5_uid", "user_id", "uid"}
_AUTH_LABELS = {
    "INVALID_KEY", "INVALID_SIGNATURE", "INVALID_CREDENTIALS", "MISSING_REQUIRED_HEADER",
    "IP_FORBIDDEN", "READ_ONLY", "FORBIDDEN", "UNAUTHORIZED", "API_KEY_DISABLED",
}
_KNOWN_LABELS = _AUTH_LABELS | {
    "INVALID_ARGUMENT", "INVALID_PARAM_VALUE", "MISSING_REQUIRED_PARAM", "REQUEST_EXPIRED",
    "TOO_MANY_REQUESTS", "RATE_LIMIT_EXCEEDED", "INTERNAL", "INTERNAL_SERVER_ERROR",
    "NOT_FOUND", "ORDER_NOT_FOUND", "POSITION_NOT_FOUND", "BALANCE_NOT_ENOUGH",
    "INSUFFICIENT_AVAILABLE", "ACCOUNT_LOCKED", "USER_NOT_FOUND", "TRADFI_USER_NOT_FOUND",
    "MARKET_CLOSED", "SERVICE_UNAVAILABLE",
}
_SENSITIVE_FIELDS = {"key", "secret", "api_key", "api_secret", "sign", "signature", "authorization"}
_AUTH_ASSIGNMENT = re.compile(
    r'''(?ix)(?<![\w])(["']?(?:api[-_\s]?key|api[-_\s]?secret|private[-_\s]?key|
    access[-_\s]?token|refresh[-_\s]?token|secret|key|sign(?:ature)?|authorization|token)
    ["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|
    (?:bearer|basic)\s+[^\s,;}\]]+|[^\s,;}\]]+)'''
)
_OPAQUE_TOKEN = re.compile(r"[A-Za-z0-9_+/=.-]{32,}")
_AUTH_SCHEME = re.compile(r'(?i)\b(bearer|basic)\s+[^\s,;}\]]+')
_ERROR_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,95}")


def _diagnostic_scalar(value: Any, credentials: Sequence[str] = (), *, limit: int = 320,
                       preserve_label: bool = False) -> str | None:
    """Extract one bounded human-readable scalar; never stringify containers."""
    if value is None or not isinstance(value, (str, int, float, Decimal, bool)):
        return None
    if isinstance(value, (float, Decimal)) and not math.isfinite(value):
        return None
    result = str(value)
    # Normalize hidden formatting and control characters before redaction so
    # they cannot break a credential match or forge a second diagnostic line.
    result = ''.join(' ' if char.isspace() else '' if unicodedata.category(char).startswith('C')
                     else char for char in result)
    for credential in sorted((item for item in credentials if item), key=len, reverse=True):
        result = result.replace(credential, '[redacted]')
    result = _AUTH_ASSIGNMENT.sub(lambda match: match.group(1) + '[redacted]', result)
    result = _AUTH_SCHEME.sub(lambda match: match.group(1) + ' [redacted]', result)

    def hide_opaque(match: re.Match[str]) -> str:
        token = match.group(0)
        # Structured uppercase error names carry useful context. Long random,
        # hexadecimal, base64, signature, and JWT-like values carry none.
        if preserve_label and result == token and re.fullmatch(r'[A-Z]{2,24}(?:_[A-Z0-9]{1,24})+', token):
            return token
        return '[redacted]'

    result = _OPAQUE_TOKEN.sub(hide_opaque, result)
    result = ' '.join(result.split())
    return (result[:limit - 1] + '…' if len(result) > limit else result) or None


def live_capabilities() -> dict[str, Any]:
    return {
        "raw_order_submission": True,
        "order_acceptance_requires_reconciliation": True,
        "leverage_editable": False,
        "max_orders": 300,
        "max_positions": 300,
    }


class GateError(Exception):
    """Safe diagnostics, including sanitized official error scalars if present."""

    def __init__(
        self, code: str, message: str, *, status_code: int | None = None,
        auth_invalid: bool = False, retry_after: float | None = None,
        official_label: str | None = None, official_code: str | None = None,
        official_message: str | None = None,
    ) -> None:
        self.code = code
        self.official_label = official_label
        self.official_code = official_code
        self.official_message = official_message
        identifiers = list(dict.fromkeys(value for value in (official_label, official_code) if value))
        diagnostics = ' / '.join(identifiers)
        if official_message:
            diagnostics += ('；' if diagnostics else '') + official_message
        self.message = message + (f' 官方返回：{diagnostics}' if diagnostics else '')
        self.status_code = status_code
        self.auth_invalid = auth_invalid
        self.retry_after = retry_after
        super().__init__(self.message)

    def safe_detail(self) -> dict[str, Any]:
        return {
            "code": self.code, "message": self.message,
            "status_code": self.status_code, "auth_invalid": self.auth_invalid,
            "retry_after": self.retry_after,
            "official_label": self.official_label, "official_code": self.official_code,
            "official_message": self.official_message,
        }


class GateCredentialsError(GateError):
    def __init__(self) -> None:
        super().__init__("credentials_missing", "请先配置 Gate API Key 和 Secret。", auth_invalid=True)


class GateClockError(GateError):
    def __init__(self, *, status_code: int | None = None, **diagnostics: str | None) -> None:
        super().__init__("clock_out_of_sync", "无法确认系统时间与 Gate 相差小于 50 秒，请同步系统时间后重试。",
                         status_code=status_code, **diagnostics)


class GateTransportError(GateError):
    def __init__(self) -> None:
        super().__init__("gate_disconnected", "无法连接 Gate，请检查网络连接后重试。")


class GateUnknownOutcome(GateError):
    """The write may have reached Gate. Reconcile before any subsequent action.

    ``pending`` contains only the operation, non-secret identifiers, request body
    hash and timestamp. It does not assert whether a write was accepted or filled.
    """

    def __init__(self, pending: dict[str, Any], status_code: int | None = None,
                 **diagnostics: str | None) -> None:
        super().__init__(
            "write_outcome_unknown", "操作结果尚不确定，必须核对交易所状态，不能直接重复提交。",
            status_code=status_code, **diagnostics,
        )
        self.pending = pending

    def safe_detail(self) -> dict[str, Any]:
        return {**super().safe_detail(), "pending": dict(self.pending)}


def _symbol(value: str) -> str:
    # Constrained ASCII avoids query/signature encoding ambiguities and path injection.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("Invalid trading symbol")
    return value


def _identifier(value: str | int) -> str:
    text = str(value)
    if not re.fullmatch(r"[0-9]{1,40}", text) or int(text) <= 0:
        raise ValueError("A positive decimal identifier is required")
    return text


def _decimal(value: Decimal | str, *, allow_zero: bool = False) -> str:
    if not isinstance(value, (Decimal, str)):
        raise ValueError("Prices and volumes must be decimal strings or Decimal values")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid decimal value") from None
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise ValueError("A finite positive decimal value is required")
    if abs(result.adjusted()) > 50 or len(result.as_tuple().digits) > 100:
        raise ValueError("Decimal value exceeds supported size")
    return format(result, "f")


def _integer(value: int, name: str, lower: int = 0, upper: int = 9_999_999_999) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError(f"Invalid {name}")
    return value


class GateClient:
    """Only sends requests to Gate's fixed official origin; never follows redirects.

    Public market methods omit credentials even when this instance has them.
    Before a signed call, server time is checked using the public symbols endpoint
    and cached for 60 seconds. A 50-second drift guard leaves margin under Gate's
    documented 60-second signature window. No automatic clock offset is invented.

    GET retries are bounded to at most two and delays to five seconds. If Gate asks
    for a longer delay, the call fails rather than retrying before the allowed time.
    POST/PUT/DELETE writes have no automatic retry, including timeout or HTTP 5xx.
    """

    def __init__(
        self, key: str = "", secret: str = "", *, transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        read_retries: int = 2, min_request_interval: float = 0.25,
        monotonic: Callable[[], float] = time.monotonic,
        timing_recorder: TimingRecorder | None = None,
    ) -> None:
        if not isinstance(key, str) or not isinstance(secret, str):
            raise ValueError("Credentials must be strings")
        if any(ord(char) < 33 or ord(char) > 126 for char in key + secret):
            raise ValueError("Credentials must contain only printable ASCII without whitespace")
        self._key = key
        self._secret = secret
        self._clock = clock
        self._sleep = sleep
        if not math.isfinite(min_request_interval) or min_request_interval < 0:
            raise ValueError("Invalid request interval")
        self._min_request_interval = min_request_interval
        self._monotonic = monotonic
        self._request_slot_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._cooldown_until = 0.0
        self._read_retries = _integer(read_retries, "read_retries", 0, 2)
        self._server_offset: float | None = None
        self._clock_checked_at: float | None = None
        self._clock_lock = asyncio.Lock()
        # Timing uses its own monotonic source so observing a call does not
        # consume/change the clock used by pacing, signatures, or test transports.
        self._timings = timing_recorder if timing_recorder is not None else TimingRecorder()
        self._http = httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False, trust_env=False,
        )

    @property
    def credentials_set(self) -> bool:
        return bool(self._key and self._secret)

    @property
    def timing_sequence(self) -> int:
        """Last completed timing cursor, suitable for incremental snapshots."""
        return self._timings.sequence

    def timing_snapshot(self, after_sequence: int = 0, scope: str | None = None) -> list[dict[str, Any]]:
        """Copy bounded timings; root rows exclude clock-probe double counting.

        Only fixed endpoint templates and numeric observations are retained.
        No body, query, credentials, resource IDs, raw headers, or errors appear.
        ``scope=None`` includes ordinary background and scoped diagnostic calls.
        """
        return self._timings.snapshot(after_sequence, scope)

    @staticmethod
    def timing_scope(label: str):
        """Return a synchronous context manager for an application-generated ID."""
        return timing_scope(label)

    async def __aenter__(self) -> GateClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()
        self._key = ""
        self._secret = ""

    close = aclose

    def _clean(self, value: Any, field: str = "") -> Any:
        if field.lower() in _SENSITIVE_FIELDS:
            return "[redacted]"
        if isinstance(value, dict):
            return {str(key): self._clean(item, str(key)) for key, item in value.items()}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        if value is not None and field in _IDENTIFIER_FIELDS:
            value = str(value)
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, str):
            for credential in (self._key, self._secret):
                if credential:
                    value = value.replace(credential, "[redacted]")
        return value

    def _observe_clock(self, payload: dict[str, Any], response: httpx.Response) -> None:
        stamp = payload.get("timestamp")
        if stamp is None and isinstance(payload.get("data"), dict):
            stamp = payload["data"].get("timestamp")
        server_time: float | None = None
        try:
            if stamp is not None and not isinstance(stamp, bool) and float(stamp) > 0:
                server_time = float(stamp) / 1000.0
            elif response.headers.get("date"):
                server_time = parsedate_to_datetime(response.headers["date"]).timestamp()
        except (TypeError, ValueError, OverflowError):
            return
        if server_time is not None and math.isfinite(server_time):
            now = self._clock()
            self._server_offset = server_time - now
            self._clock_checked_at = now

    async def check_clock(self) -> None:
        async with self._clock_lock:
            age = None if self._clock_checked_at is None else self._clock() - self._clock_checked_at
            if age is None or age < 0 or age >= 60:
                await self.symbols()
            if self._server_offset is None or abs(self._server_offset) >= 50:
                raise GateClockError()

    def _retry_delay(self, response: httpx.Response, attempt: int, *, bounded: bool = True) -> float | None:
        delay = 0.25 * (2 ** attempt)
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                try:
                    wait = parsedate_to_datetime(retry_after).timestamp() - self._clock()
                except (ValueError, TypeError, OverflowError):
                    wait = 0.0
            if math.isfinite(wait):
                delay = max(delay, wait)
        reset = response.headers.get("x-gate-ratelimit-reset-timestamp")
        if reset:
            try:
                reset_value = float(reset)
                if reset_value > 100_000_000_000:
                    reset_value /= 1000.0
                if math.isfinite(reset_value):
                    delay = max(delay, reset_value - self._clock())
            except ValueError:
                pass
        return delay if not bounded or delay <= 5 else None

    async def _wait_request_slot(self) -> None:
        # One shared client keeps ticker, account polling, and trade requests below
        # the 5 qps limit observed on the official CFD responses (2026-09-07).
        async with self._request_slot_lock:
            cooldown = self._cooldown_until - self._monotonic()
            if cooldown > 0:
                raise GateError("gate_rate_limited", "Gate 请求频率受限，请等待限流窗口结束。",
                                status_code=429, retry_after=cooldown)
            wait = self._next_request_at - self._monotonic()
            if wait > 0:
                await self._sleep(wait)
            # Another in-flight response may have reported 429 during this wait.
            cooldown = self._cooldown_until - self._monotonic()
            if cooldown > 0:
                raise GateError("gate_rate_limited", "Gate 请求频率受限，请等待限流窗口结束。",
                                status_code=429, retry_after=cooldown)
            self._next_request_at = self._monotonic() + self._min_request_interval

    def _response_error(self, response: httpx.Response, payload: dict[str, Any]) -> GateError:
        error = self._error(response.status_code, payload)
        if response.status_code == 429 or response.status_code >= 500:
            error.retry_after = self._retry_delay(response, 0, bounded=False)
        return error

    def _error(self, status: int, payload: dict[str, Any]) -> GateError:
        diagnostics = self._diagnostics(payload)
        label = diagnostics['official_label'] or ''
        code = label if _ERROR_LABEL.fullmatch(label) else "gate_api_error"
        if label == "REQUEST_EXPIRED":
            return GateClockError(status_code=status, **diagnostics)
        auth_invalid = status in (401, 403) or label in _AUTH_LABELS
        if auth_invalid:
            message = "Gate 拒绝认证，请检查 API Key、Secret、权限和 IP 白名单。"
        elif status == 429:
            message = "Gate 请求频率受限，请稍后重试。"
        elif status >= 500:
            message = "Gate 服务暂时不可用，请稍后重试。"
        elif any(part in label.upper() for part in ('STOP', 'TAKE_PROFIT', 'PRICE_TP', 'PRICE_SL')):
            message = "Gate 拒绝止盈止损参数，请核对价格方向与最小距离。"
        elif any(part in label.upper() for part in ('BALANCE', 'MARGIN', 'FUNDS', 'INSUFFICIENT_AVAILABLE')):
            message = "Gate 拒绝委托，账户可用保证金或余额不足。"
        elif any(part in label.upper() for part in ('VOLUME', 'SIZE', 'AMOUNT')):
            message = "Gate 拒绝下单手数，请核对数量范围与步长。"
        elif 'PRICE' in label.upper():
            message = "Gate 拒绝委托价格，请核对价格精度与允许范围。"
        elif any(part in label.upper() for part in ('MARKET_CLOSED', 'TRADING_DISABLED', 'TRADE_DISABLED')):
            message = "Gate 当前交易状态不允许该操作。"
        else:
            message = "Gate 拒绝了本次请求，请核对账户状态与请求参数。"
        return GateError(code, message, status_code=status, auth_invalid=auth_invalid, **diagnostics)

    def _diagnostics(self, payload: dict[str, Any]) -> dict[str, str | None]:
        credentials = (self._key, self._secret)
        return {
            'official_label': _diagnostic_scalar(payload.get('label'), credentials, limit=96, preserve_label=True),
            'official_code': _diagnostic_scalar(payload.get('code'), credentials, limit=64),
            'official_message': _diagnostic_scalar(payload.get('message'), credentials, limit=320),
        }

    async def _request(
        self, method: str, path: str, *, signed: bool = False,
        query: Mapping[str, Any] | None = None, body: Mapping[str, Any] | None = None,
        before_send: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        timing = self._timings.begin(method, path, signed, redactions=(self._key, self._secret))
        outcome = "error"
        try:
            result = await self._request_impl(method, path, signed=signed, query=query, body=body,
                                              before_send=before_send, timing=timing)
            outcome = "success"
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except GateUnknownOutcome:
            outcome = "unknown"
            raise
        finally:
            timing.finish(outcome)

    async def _request_impl(
        self, method: str, path: str, *, signed: bool,
        query: Mapping[str, Any] | None, body: Mapping[str, Any] | None,
        before_send: Callable[[], Any] | None, timing: TimingSpan,
    ) -> dict[str, Any]:
        if not path.startswith("/tradfi/") or any(part in path for part in ("..", "?", "#", "%")):
            raise ValueError("Unsupported Gate endpoint path")
        write = method != "GET"
        if signed:
            if not self.credentials_set:
                raise GateCredentialsError()
            with timing.measure("clock_wait_ms"):
                await self.check_clock()
        body_bytes = b"" if body is None else json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        query_string = urlencode([(key, value) for key, value in (query or {}).items() if value is not None], safe=",")
        # Public methods constrain all values; no encoded query components are needed.
        # This exactly follows Gate's unencoded canonical query-string requirement.
        if "%" in query_string or "+" in query_string:
            raise ValueError("Unsupported query characters")
        canonical_path = "/api/v4" + path
        url = GATE_BASE_URL + path + ("?" + query_string if query_string else "")
        body_hash = hashlib.sha512(body_bytes).hexdigest()
        attempts = 1 if write else self._read_retries + 1
        for attempt in range(attempts):
            with timing.measure("throttle_wait_ms"):
                await self._wait_request_slot()
            timestamp = str(int(self._clock()))
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            if signed:
                signature_base = "\n".join((method, canonical_path, query_string, body_hash, timestamp))
                headers.update({
                    "KEY": self._key, "Timestamp": timestamp,
                    "SIGN": hmac.new(self._secret.encode(), signature_base.encode(), hashlib.sha512).hexdigest(),
                })
            pending = self._clean({
                "method": method, "path": canonical_path,
                "submitted_at": timestamp, "body_sha512": body_hash,
                **({"symbol": body["symbol"]} if body and "symbol" in body else {}),
            })
            # The engine can stop a prepared order after all client-side waits.
            # Keep this outside the transport exception handler: a failed final
            # preflight proves no request was sent, even if it raises HTTPError.
            if before_send is not None:
                preflight = before_send()
                if inspect.isawaitable(preflight):
                    await preflight
            try:
                with timing.measure("network_ms"):
                    timing.attempt()
                    response = await self._http.request(method, url, content=body_bytes, headers=headers)
            except httpx.HTTPError:
                if write:
                    raise GateUnknownOutcome(pending) from None
                if attempt + 1 < attempts:
                    with timing.measure("backoff_ms"):
                        await self._sleep(0.25 * 2 ** attempt)
                    continue
                raise GateTransportError() from None
            timing.response(response.status_code, response.headers)
            if response.status_code == 429:
                cooldown = self._retry_delay(response, attempt, bounded=False) or 1.0
                self._cooldown_until = max(self._cooldown_until, self._monotonic() + cooldown)
            if not write and (response.status_code == 429 or response.status_code >= 500):
                delay = self._retry_delay(response, attempt)
                if attempt + 1 < attempts and delay is not None:
                    with timing.measure("backoff_ms"):
                        await self._sleep(delay)
                    continue
            try:
                payload = json.loads(response.content, parse_float=Decimal)
            except (ValueError, UnicodeDecodeError):
                payload = None
            if write and (response.status_code >= 500 or response.status_code == 408):
                raise GateUnknownOutcome(pending, response.status_code,
                                         **self._diagnostics(payload if isinstance(payload, dict) else {}))
            if not isinstance(payload, dict):
                if write and 200 <= response.status_code < 300:
                    raise GateUnknownOutcome(pending, response.status_code)
                raise self._response_error(response, {})
            self._observe_clock(payload, response)
            business_error = payload.get("code") not in (None, 0, "0") or bool(payload.get("label"))
            creating = method == 'POST' and path == '/tradfi/orders'
            queue_id = None
            if creating:
                data = payload.get('data')
                try:
                    queue_id = _identifier(data.get('id')) if isinstance(data, dict) else None
                except ValueError:
                    pass
            if creating and 200 <= response.status_code < 300 and business_error and queue_id:
                # A valid task ID contradicts a reported business error. Preserve
                # it only as a lookup candidate; never claim rejection or retry.
                raise GateUnknownOutcome(self._clean({**pending, 'queue_id': queue_id}), response.status_code,
                                         **self._diagnostics(payload))
            if not 200 <= response.status_code < 300 or business_error:
                raise self._response_error(response, payload)
            if creating and queue_id is None:
                raise GateUnknownOutcome(pending, response.status_code, **self._diagnostics(payload))
            return self._clean(payload)
        raise GateTransportError()  # Defensive; the bounded loop always returns/raises.

    async def symbols(self) -> dict[str, Any]:
        return await self._request("GET", "/tradfi/symbols")

    async def categories(self) -> dict[str, Any]:
        return await self._request("GET", "/tradfi/symbols/categories")

    async def commissions(
        self, symbols: Sequence[str] | str | None = None, *,
        category_code: Sequence[str] | str | None = None,
    ) -> dict[str, Any]:
        """Per-lot fee schedule, preserving the official response envelope.

        At least one filter is required. Gate's published schema calls this
        public, but its live endpoint requires Timestamp/KEY authentication.
        The schema does not establish VIP adjustment or minimum/order fee rules.
        """
        query: dict[str, str] = {}
        for name, value in (("symbols", symbols), ("category_code", category_code)):
            if value is None:
                continue
            if not isinstance(value, (str, Sequence)) or isinstance(value, (bytes, bytearray)):
                raise ValueError("Fee filters must be symbol/code strings or sequences")
            values = value.split(",") if isinstance(value, str) else list(value)
            if not values:
                raise ValueError("Fee filters must not be empty")
            query[name] = ",".join(_symbol(item) for item in values)
        if not query:
            raise ValueError("Provide symbols or category_code for the fee schedule")
        return await self._request("GET", "/tradfi/symbols/commissions", signed=True, query=query)

    async def symbol_detail(self, symbols: Sequence[str] | str) -> dict[str, Any]:
        values = symbols.split(",") if isinstance(symbols, str) else list(symbols)
        if not 1 <= len(values) <= 10:
            raise ValueError("Provide between one and ten symbols")
        return await self._request("GET", "/tradfi/symbols/detail", signed=True,
                                   query={"symbols": ",".join(_symbol(item) for item in values)})

    async def ticker(self, symbol: str) -> dict[str, Any]:
        return await self._request("GET", f"/tradfi/symbols/{_symbol(symbol)}/tickers")

    async def klines(
        self, symbol: str, kline_type: str = "1m", limit: int = 120, *,
        begin_time: int | None = None, end_time: int | None = None,
    ) -> dict[str, Any]:
        if kline_type not in {"1m", "15m", "1h", "4h", "1d", "7d", "30d"}:
            raise ValueError("Unsupported kline period")
        return await self._request("GET", f"/tradfi/symbols/{_symbol(symbol)}/klines", query={
            "kline_type": kline_type, "limit": _integer(limit, "limit", 1, 500),
            **self._time_range(begin_time, end_time),
        })

    async def account(self) -> dict[str, Any]:
        return await self._request("GET", "/tradfi/users/mt5-account", signed=True)

    async def assets(self) -> dict[str, Any]:
        return await self._request("GET", "/tradfi/users/assets", signed=True)

    async def orders(self) -> dict[str, Any]:
        return await self._request("GET", "/tradfi/orders", signed=True)

    async def positions(self) -> dict[str, Any]:
        return await self._request("GET", "/tradfi/positions", signed=True)

    @staticmethod
    def _time_range(begin_time: int | None, end_time: int | None) -> dict[str, int]:
        result = {}
        for name, value in (("begin_time", begin_time), ("end_time", end_time)):
            if value is not None:
                result[name] = _integer(value, name)
        if begin_time is not None and end_time is not None and begin_time > end_time:
            raise ValueError("begin_time must not exceed end_time")
        return result

    async def order_history(
        self, *, symbol: str | None = None, side: int | None = None,
        begin_time: int | None = None, end_time: int | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = self._time_range(begin_time, end_time)
        if symbol is not None:
            query["symbol"] = _symbol(symbol)
        if side is not None:
            query["side"] = _integer(side, "side", 1, 2)
        return await self._request("GET", "/tradfi/orders/history", signed=True, query=query)

    async def position_history(
        self, *, page: int = 1, page_size: int = 100, symbol: str | None = None,
        position_dir: str | None = None, begin_time: int | None = None,
        end_time: int | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {
            "page": _integer(page, "page", 1), "page_size": _integer(page_size, "page_size", 1, 100),
            **self._time_range(begin_time, end_time),
        }
        if symbol is not None:
            query["symbol"] = _symbol(symbol)
        if position_dir is not None:
            if position_dir not in {"Long", "Short"}:
                raise ValueError("position_dir must be Long or Short")
            query["position_dir"] = position_dir
        return await self._request("GET", "/tradfi/positions/history", signed=True, query=query)

    async def create_order(
        self, *, symbol: str, side: int, volume: Decimal | str,
        price: Decimal | str, price_type: str = "trigger",
        price_tp: Decimal | str | None = None, price_sl: Decimal | str | None = None,
        before_send: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        """Raw submission; a returned queue ID is not proof of placement/fill.

        ``before_send`` runs after clock checks, pacing, and signature preparation.
        Its sync/async exception propagates unchanged because HTTP has not started.
        Unset or zero TP/SL is omitted on creation, matching the official optional
        fields. Explicit zero clearing remains available through PUT updates.
        """
        if price_type not in {"market", "trigger"}:
            raise ValueError("price_type must be market or trigger")
        body: dict[str, Any] = {
            "price": _decimal(price, allow_zero=price_type == "market"), "price_type": price_type,
            "side": _integer(side, "side", 1, 2), "symbol": _symbol(symbol), "volume": _decimal(volume),
        }
        for field, value in (("price_tp", price_tp), ("price_sl", price_sl)):
            if value is None or (type(value) in (int, float) and value == 0):
                continue
            protection = _decimal(value, allow_zero=True)
            if Decimal(protection) != 0:
                body[field] = protection
        return await self._request("POST", "/tradfi/orders", signed=True, body=body,
                                   before_send=before_send)

    async def update_order(
        self, order_id: str | int, *, price: Decimal | str,
        price_tp: Decimal | str, price_sl: Decimal | str,
        before_send: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        """Update one pending order, explicitly supplying both protection prices.

        Gate clears omitted TP/SL values. To preserve them, the caller must first
        reconcile and supply their existing values; ``None`` is rejected here.
        The decimal string ``"0"`` explicitly clears the corresponding value.
        """
        path = f"/tradfi/orders/{_identifier(order_id)}"
        body = {
            "price": _decimal(price),
            "price_tp": _decimal(price_tp, allow_zero=True),
            "price_sl": _decimal(price_sl, allow_zero=True),
        }
        return await self._request("PUT", path, signed=True, body=body,
                                   before_send=before_send)

    async def update_position(
        self, position_id: str | int, *, price_tp: Decimal | str,
        price_sl: Decimal | str, before_send: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        """Set explicit TP/SL values on one position; ``"0"`` clears, None fails.

        Preserve a field by supplying its reconciled existing value. Both fields
        are mandatory because omission would clear protection at the exchange.
        """
        path = f"/tradfi/positions/{_identifier(position_id)}"
        body = {
            "price_tp": _decimal(price_tp, allow_zero=True),
            "price_sl": _decimal(price_sl, allow_zero=True),
        }
        return await self._request("PUT", path, signed=True, body=body,
                                   before_send=before_send)

    # Match the engine's action naming without changing upstream endpoint names.
    modify_order = update_order
    modify_position = update_position

    async def cancel_order(
        self, order_id: str | int, *, before_send: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request("DELETE", f"/tradfi/orders/{_identifier(order_id)}",
                                   signed=True, before_send=before_send)

    async def order_log(self, log_id: str | int) -> dict[str, Any]:
        """Look up a log ID or a create response's numeric queue-ID candidate.

        A candidate is not verified by sending this request. The engine must
        compare the returned log_id and order parameters before associating it;
        this adapter returns the response unchanged except for safe JSON types.
        """
        return await self._request("GET", f"/tradfi/orders/log/{_identifier(log_id)}", signed=True)

    async def close_position(
        self, position_id: str | int, close_volume: Decimal | str | None = None,
        *, before_send: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        """Close one specific independent position; never sends an opposite opening order."""
        body: dict[str, Any] = {"close_type": 2}
        if close_volume is not None:
            body = {"close_type": 1, "close_volume": _decimal(close_volume)}
        return await self._request("POST", f"/tradfi/positions/{_identifier(position_id)}/close",
                                   signed=True, body=body, before_send=before_send)
