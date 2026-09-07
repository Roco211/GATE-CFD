"""Isolated adapter tests: no credentials or requests ever leave MockTransport."""

import asyncio
import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import pytest

from app.gate import (
    GateClient, GateClockError, GateCredentialsError, GateError, GateTransportError,
    GateUnknownOutcome, live_capabilities,
)


NOW = 1_800_000_000
KEY = "test-key-never-real"
SECRET = "test-secret-never-real"


def envelope(data=None, **extra):
    return {"data": {} if data is None else data, "timestamp": NOW * 1000, **extra}


def client(handler, **kwargs):
    return GateClient(KEY, SECRET, transport=httpx.MockTransport(handler), clock=lambda: NOW,
                      min_request_interval=0, **kwargs)


def expected_signature(request):
    canonical = "\n".join([
        request.method, request.url.path, request.url.query.decode(),
        hashlib.sha512(request.content).hexdigest(), request.headers["Timestamp"],
    ])
    return hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha512).hexdigest()


def run(coro):
    return asyncio.run(coro)


def test_exact_signature_body_and_queue_id_is_not_renamed():
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=envelope({"list": []}))
        return httpx.Response(200, json=envelope({"id": 9_007_199_254_740_993}))

    async def scenario():
        async with client(handler) as api:
            result = await api.create_order(
                symbol="EURUSD", side=2, price=Decimal("1.02000"), volume="0.01",
                price_tp="1.04000", price_sl="0",
            )
            assert result["data"] == {"id": "9007199254740993"}

    run(scenario())
    preflight, order = seen
    assert "KEY" not in preflight.headers
    assert order.url == "https://api.gateio.ws/api/v4/tradfi/orders"
    assert order.content == b'{"price":"1.02000","price_type":"trigger","side":2,"symbol":"EURUSD","volume":"0.01","price_tp":"1.04000"}'
    assert order.headers["KEY"] == KEY
    assert order.headers["SIGN"] == expected_signature(order)
    assert order.headers["Timestamp"] == str(NOW)


def test_unsigned_public_market_and_signed_detail_exact_query():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=envelope({"list": []}))

    async def scenario():
        async with client(handler) as api:
            await api.symbols()
            await api.ticker("XAUUSD")
            await api.klines("EURUSD", "15m", 80, begin_time=NOW - 1000, end_time=NOW)
            await api.symbol_detail(["EURUSD", "XAGUSD"])
            await api.order_history(begin_time=NOW - 100, end_time=NOW, symbol="XAUUSD", side=1)

    run(scenario())
    for request in seen[:3]:
        assert not {"key", "sign", "timestamp"} & set(request.headers)
    detail = seen[3]
    assert detail.url.query == b"symbols=EURUSD,XAGUSD"
    assert detail.headers["SIGN"] == expected_signature(detail)
    history = seen[4]
    assert history.url.query == b"begin_time=1799999900&end_time=1800000000&symbol=XAUUSD&side=1"
    assert history.headers["SIGN"] == expected_signature(history)


def test_commissions_uses_required_live_auth_and_preserves_envelope_and_extra_fields():
    seen = []
    payload = envelope({"list": [{"symbol": "XAUUSD", "category_code": "metal",
                                  "fee_per_lot": "6.0000", "future_fee_detail": {"currency": "USDx"}}]},
                       future_metadata="preserved")

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=payload)

    async def scenario():
        async with client(handler) as api:
            assert await api.commissions(["XAUUSD", "EURUSD"]) == payload
            assert await api.commissions("XAUUSD", category_code="metal") == payload
            assert await api.commissions(category_code=["metal", "forex"]) == payload

    run(scenario())
    preflight, *requests = seen
    assert not {"key", "sign", "timestamp"} & set(preflight.headers)
    assert [request.url.query for request in requests] == [
        b"symbols=XAUUSD,EURUSD", b"symbols=XAUUSD&category_code=metal", b"category_code=metal,forex",
    ]
    for request in requests:
        assert request.method == "GET"
        assert request.url.path == "/api/v4/tradfi/symbols/commissions"
        assert request.headers["KEY"] == KEY
        assert request.headers["Timestamp"] == str(NOW)
        assert request.headers["SIGN"] == expected_signature(request)


@pytest.mark.parametrize("filters", [
    {}, {"symbols": []}, {"symbols": ""}, {"symbols": ["XAUUSD", ""]},
    {"symbols": "XAUUSD&category_code=metal"}, {"symbols": 12},
    {"category_code": []}, {"category_code": b"metal"}, {"category_code": [None]},
])
def test_commissions_rejects_empty_or_unsafe_filters_before_http(filters):
    def handler(request):
        raise AssertionError("Invalid fee filters must not issue an HTTP request")

    async def scenario():
        async with client(handler) as api:
            with pytest.raises(ValueError):
                await api.commissions(**filters)

    run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "503", "408", "malformed", "missing_queue_id"])
def test_ambiguous_create_is_never_retried(failure):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=envelope())
        if failure == "timeout":
            raise httpx.ReadTimeout(f"LEAK {KEY} {SECRET}", request=request)
        if failure in {"503", "408"}:
            return httpx.Response(int(failure), text=f"LEAK {KEY} {SECRET}")
        if failure == "missing_queue_id":
            return httpx.Response(200, json=envelope())
        return httpx.Response(200, text=f"LEAK {KEY} {SECRET}")

    async def scenario():
        async with client(handler) as api:
            with pytest.raises(GateUnknownOutcome) as caught:
                await api.create_order(symbol="XAUUSD", side=2, price="5000", volume="0.01")
            details = caught.value.safe_detail()
            assert details["pending"]["path"] == "/api/v4/tradfi/orders"
            assert details["pending"]["symbol"] == "XAUUSD"
            assert len(details["pending"]["body_sha512"]) == 128
            assert KEY not in json.dumps(details)
            assert SECRET not in json.dumps(details)
            assert "LEAK" not in str(caught.value)

    run(scenario())
    assert [r.method for r in calls] == ["GET", "POST"]


def test_cancel_and_close_use_the_given_independent_identifiers():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=envelope())

    async def scenario():
        async with client(handler) as api:
            await api.cancel_order("9007199254740993")
            await api.close_position("9007199254740994")
            await api.close_position("9007199254740994", "0.02")
            await api.order_log("777")

    run(scenario())
    assert seen[1].method == "DELETE"
    assert seen[1].url.path == "/api/v4/tradfi/orders/9007199254740993"
    assert seen[1].content == b""
    assert seen[2].url.path == "/api/v4/tradfi/positions/9007199254740994/close"
    assert seen[2].content == b'{"close_type":2}'
    assert seen[3].content == b'{"close_type":1,"close_volume":"0.02"}'
    assert seen[4].url.path == "/api/v4/tradfi/orders/log/777"
    for request in seen[1:]:
        assert request.headers["SIGN"] == expected_signature(request)


@pytest.mark.parametrize("verb", ["delete", "close"])
def test_cancel_and_close_503_are_unknown_and_not_retried(verb):
    writes = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=envelope())
        writes.append(request)
        return httpx.Response(503, json={"message": SECRET})

    async def scenario():
        async with client(handler) as api:
            with pytest.raises(GateUnknownOutcome):
                if verb == "delete":
                    await api.cancel_order("123")
                else:
                    await api.close_position("123")

    run(scenario())
    assert len(writes) == 1


@pytest.mark.parametrize("payload, auth", [
    ({"code": 3, "message": SECRET}, False),
    ({"label": "INVALID_KEY", "message": SECRET}, True),
    ({"label": KEY, "message": SECRET}, False),
    ({"label": {"secret": SECRET}, "message": KEY}, False),
])
def test_http_200_business_errors_are_failures_and_redacted(payload, auth):
    async def scenario():
        async with client(lambda _: httpx.Response(200, json=payload)) as api:
            with pytest.raises(GateError) as caught:
                await api.symbols()
            error = caught.value
            assert error.auth_invalid is auth
            assert error.status_code == 200
            assert KEY not in json.dumps(error.safe_detail())
            assert SECRET not in json.dumps(error.safe_detail())

    run(scenario())


def test_read_retries_respect_retry_after_and_are_bounded():
    calls = []
    sleeps = []
    monotonic_time = [0.0]

    async def sleep(delay):
        sleeps.append(delay)
        monotonic_time[0] += delay

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        if len(calls) == 2:
            return httpx.Response(503, headers={"X-Gate-RateLimit-Reset-Timestamp": str((NOW + 3) * 1000)}, json={})
        return httpx.Response(200, json=envelope({"list": []}))

    async def scenario():
        async with client(handler, sleep=sleep, monotonic=lambda: monotonic_time[0]) as api:
            assert (await api.symbols())["data"] == {"list": []}

    run(scenario())
    assert len(calls) == 3
    assert sleeps == [2, 3]


def test_retry_delay_over_cap_fails_without_early_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "90"}, json={})

    async def scenario():
        async with client(handler) as api:
            with pytest.raises(GateError) as caught:
                await api.symbols()
            assert caught.value.status_code == 429

    run(scenario())
    assert len(calls) == 1


def test_repeated_network_failure_is_disconnected_not_invalid_credentials():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError(f"LEAK {SECRET}", request=request)

    async def sleep(_):
        pass

    async def scenario():
        async with client(handler, sleep=sleep) as api:
            with pytest.raises(GateTransportError) as caught:
                await api.symbols()
            assert caught.value.auth_invalid is False
            assert SECRET not in str(caught.value)

    run(scenario())
    assert len(calls) == 3


@pytest.mark.parametrize("drift", [-65, 50, 65])
def test_clock_drift_blocks_before_sending_any_credentials(drift):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"timestamp": (NOW + drift) * 1000, "data": {}})

    async def scenario():
        async with client(handler) as api:
            with pytest.raises(GateClockError):
                await api.account()

    run(scenario())
    assert len(seen) == 1
    assert "KEY" not in seen[0].headers


def test_missing_credentials_fail_locally():
    def handler(_):
        pytest.fail("No network request should be made")

    async def scenario():
        async with GateClient(transport=httpx.MockTransport(handler)) as api:
            with pytest.raises(GateCredentialsError):
                await api.assets()
            with pytest.raises(GateCredentialsError):
                await api.commissions("XAUUSD")

    run(scenario())


def test_response_ids_keep_precision_and_sensitive_values_are_redacted():
    async def scenario():
        payload = envelope({"list": [{
            "position_id": 9007199254740993, "price": "1.2000", "side": 2,
            "secret": SECRET, "description": f"credentials {KEY} {SECRET}",
        }]})
        async with client(lambda _: httpx.Response(200, json=payload)) as api:
            result = await api.positions()
            row = result["data"]["list"][0]
            assert row["position_id"] == "9007199254740993"
            assert row["price"] == "1.2000"
            assert row["side"] == 2
            assert row["secret"] == "[redacted]"
            assert KEY not in json.dumps(result)
            assert SECRET not in json.dumps(result)

    run(scenario())


def test_redirect_is_not_followed():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://example.com/steal"}, json={})

    async def scenario():
        async with client(handler) as api:
            with pytest.raises(GateError):
                await api.symbols()

    run(scenario())
    assert len(seen) == 1
    assert seen[0].url.host == "api.gateio.ws"


def test_invalid_symbols_ids_and_float_prices_rejected_before_network():
    def handler(_):
        pytest.fail("Validation must happen before network")

    async def scenario():
        async with client(handler) as api:
            for bad in ["../orders", "EURUSD?side=2", "https://evil.com", "EUR USD"]:
                with pytest.raises(ValueError):
                    await api.ticker(bad)
            with pytest.raises(ValueError):
                await api.cancel_order("1/close")
            with pytest.raises(ValueError):
                await api.create_order(symbol="EURUSD", side=2, price=1.2, volume="0.01")
            with pytest.raises(ValueError):
                await api.create_order(symbol="EURUSD", side=2, price="NaN", volume="0.01")
            with pytest.raises(ValueError):
                await api.create_order(symbol="EURUSD", side=True, price="1.2", volume="0.01")

    run(scenario())


def test_raw_submission_capability_does_not_claim_order_completion():
    capabilities = live_capabilities()
    assert capabilities["raw_order_submission"] is True
    assert capabilities["order_acceptance_requires_reconciliation"] is True
    assert capabilities["leverage_editable"] is False


def test_all_requests_share_pacing_and_long_rate_limit_cooldown():
    ticks = [0.0]
    call_times = []

    async def sleep(delay):
        ticks[0] += delay

    def handler(request):
        call_times.append(ticks[0])
        if len(call_times) == 3:
            return httpx.Response(429, headers={"Retry-After": "90"}, json={})
        return httpx.Response(200, json=envelope())

    async def scenario():
        async with GateClient(transport=httpx.MockTransport(handler), clock=lambda: NOW,
                              monotonic=lambda: ticks[0], sleep=sleep) as api:
            await api.symbols()
            await api.ticker("EURUSD")
            with pytest.raises(GateError) as caught:
                await api.ticker("XAUUSD")
            assert caught.value.retry_after == 90
            with pytest.raises(GateError) as blocked:
                await api.symbols()
            assert blocked.value.status_code == 429
            assert blocked.value.retry_after == 90
            assert len(call_times) == 3
            ticks[0] += 90
            await api.symbols()

    run(scenario())
    assert call_times == [0.0, 0.25, 0.5, 90.5]


def test_order_final_preflight_after_pacing_can_prevent_http_post():
    ticks, seen, events = [0.0], [], []

    class StaleQuoteBeforeSend(Exception):
        pass

    async def sleep(delay):
        events.append("pacing")
        ticks[0] += delay

    def handler(request):
        seen.append(request.method)
        return httpx.Response(200, json=envelope())

    def final_preflight():
        events.append("preflight")
        assert ticks[0] == 0.25
        raise StaleQuoteBeforeSend("quote expired while waiting")

    async def scenario():
        async with GateClient(KEY, SECRET, transport=httpx.MockTransport(handler),
                              clock=lambda: NOW, monotonic=lambda: ticks[0], sleep=sleep) as api:
            with pytest.raises(StaleQuoteBeforeSend):
                await api.create_order(symbol="XAUUSD", side=2, price="4400", volume="0.01",
                                       before_send=final_preflight)

    run(scenario())
    assert events == ["pacing", "preflight"]
    assert seen == ["GET"]  # Only the unsigned server-clock preflight reached HTTP.


def test_async_cancel_before_send_error_propagates_unchanged_not_unknown():
    seen = []
    abort = httpx.RequestError("cancel_before_send")

    def handler(request):
        seen.append(request.method)
        return httpx.Response(200, json=envelope())

    async def cancel_before_send():
        await asyncio.sleep(0)
        raise abort

    async def scenario():
        async with client(handler) as api:
            with pytest.raises(httpx.RequestError) as caught:
                await api.create_order(symbol="XAUUSD", side=2, price="4400", volume="0.01",
                                       before_send=cancel_before_send)
            assert caught.value is abort
            assert not isinstance(caught.value, GateUnknownOutcome)

    run(scenario())
    assert seen == ["GET"]


def test_update_order_and_position_sign_exact_put_bodies_with_explicit_zero_clear():
    seen, callbacks = [], []

    def handler(request):
        seen.append(request)
        if request.method == "PUT" and "/orders/" in request.url.path:
            return httpx.Response(200, json=envelope({"order_id": 9007199254740993}))
        return httpx.Response(200, json=envelope())

    async def scenario():
        async with client(handler) as api:
            result = await api.update_order(
                "9007199254740993", price=Decimal("1.02500"),
                price_tp="1.10000", price_sl="0",
                before_send=lambda: callbacks.append("order"),
            )
            assert result["data"]["order_id"] == "9007199254740993"
            await api.update_position(
                9007199254740994, price_tp="0", price_sl=Decimal("1.00100"),
                before_send=lambda: callbacks.append("position"),
            )

    run(scenario())
    assert callbacks == ["order", "position"]
    assert [request.method for request in seen] == ["GET", "PUT", "PUT"]
    assert seen[1].url.path == "/api/v4/tradfi/orders/9007199254740993"
    assert seen[1].content == b'{"price":"1.02500","price_tp":"1.10000","price_sl":"0"}'
    assert seen[2].url.path == "/api/v4/tradfi/positions/9007199254740994"
    assert seen[2].content == b'{"price_tp":"0","price_sl":"1.00100"}'
    for request in seen[1:]:
        assert request.url.query == b""
        assert request.headers["KEY"] == KEY
        assert request.headers["SIGN"] == expected_signature(request)


@pytest.mark.parametrize("action", ["update_order", "update_position", "modify_order", "modify_position"])
def test_updates_require_complete_valid_protection_prices_before_network(action):
    def handler(_):
        pytest.fail("Invalid or missing protection values must not reach HTTP")

    async def scenario():
        async with client(handler) as api:
            method = getattr(api, action)
            defaults = {"price_tp": "1.1", "price_sl": "0.9"}
            if action.endswith("order"):
                defaults["price"] = "1"
            for field in defaults:
                missing = {name: value for name, value in defaults.items() if name != field}
                with pytest.raises(TypeError):
                    await method("123", **missing)
                for invalid in (None, 0.0, "-0.1", "NaN", "Infinity", "1e9999"):
                    with pytest.raises(ValueError):
                        await method("123", **{**defaults, field: invalid})
            if "price" in defaults:
                with pytest.raises(ValueError):
                    await method("123", **{**defaults, "price": "0"})
            for invalid_id in (None, True, 1.1, 0, "-1", "1/close", "1?price=0", "9" * 41):
                with pytest.raises(ValueError):
                    await method(invalid_id, **defaults)

    run(scenario())


@pytest.mark.parametrize("action", ["update_order", "update_position"])
@pytest.mark.parametrize("failure", ["timeout", "503", "408", "malformed", "business", "429"])
def test_updates_do_not_retry_uncertain_or_rejected_writes(action, failure):
    writes = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=envelope())
        writes.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(f"LEAK {KEY} {SECRET}", request=request)
        if failure in {"503", "408", "429"}:
            return httpx.Response(int(failure), headers={"Retry-After": "1"}, json={})
        if failure == "business":
            return httpx.Response(200, json=envelope(label="INVALID_ARGUMENT", message=SECRET))
        return httpx.Response(200, text=f"LEAK {KEY} {SECRET}")

    async def scenario():
        async with client(handler) as api:
            params = {"price_tp": "0", "price_sl": "0"}
            if action == "update_order":
                params["price"] = "1"
            uncertain = failure in {"timeout", "503", "408", "malformed"}
            with pytest.raises(GateUnknownOutcome if uncertain else GateError) as caught:
                await getattr(api, action)("123", **params)
            if not uncertain:
                assert not isinstance(caught.value, GateUnknownOutcome)
            else:
                assert caught.value.pending["method"] == "PUT"
            detail = json.dumps(caught.value.safe_detail())
            assert KEY not in detail
            assert SECRET not in detail
            assert "LEAK" not in detail

    run(scenario())
    assert len(writes) == 1
    assert writes[0].method == "PUT"


@pytest.mark.parametrize("action", ["modify_order", "modify_position", "cancel_order", "close_position"])
def test_all_native_mutations_can_abort_after_pacing_without_unknown_outcome(action):
    ticks, seen, events = [0.0], [], []
    abort = httpx.RequestError("cancel_before_send")

    async def sleep(delay):
        events.append("pacing")
        ticks[0] += delay

    def handler(request):
        seen.append(request.method)
        return httpx.Response(200, json=envelope())

    async def before_send():
        events.append("preflight")
        assert ticks[0] == 0.25
        await asyncio.sleep(0)
        raise abort

    async def scenario():
        async with GateClient(KEY, SECRET, transport=httpx.MockTransport(handler),
                              clock=lambda: NOW, monotonic=lambda: ticks[0], sleep=sleep) as api:
            params = {"before_send": before_send}
            if action.startswith("modify"):
                params.update(price_tp="1.1", price_sl="0.9")
            if action == "modify_order":
                params["price"] = "1"
            with pytest.raises(httpx.RequestError) as caught:
                await getattr(api, action)("123", **params)
            assert caught.value is abort
            assert not isinstance(caught.value, GateUnknownOutcome)

    run(scenario())
    assert events == ["pacing", "preflight"]
    assert seen == ["GET"]


def test_queue_id_log_lookup_candidate_does_not_rewrite_response_identifiers():
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=envelope({"id": 117}))
        if "/orders/log/" in request.url.path:
            return httpx.Response(200, json=envelope({"log_id": 118, "order_id": 9007199254740993}))
        return httpx.Response(200, json=envelope())

    async def scenario():
        async with client(handler) as api:
            created = await api.create_order(symbol="EURUSD", side=2, price="1", volume="0.01")
            assert created["data"] == {"id": "117"}
            found = await api.order_log(created["data"]["id"])
            # Correlation belongs to the engine; a differing response is not
            # renamed to make it appear verified against the supplied candidate.
            assert found["data"] == {"log_id": "118", "order_id": "9007199254740993"}

    run(scenario())
    assert seen[-1].url.path == "/api/v4/tradfi/orders/log/117"


@pytest.mark.parametrize('zero', [None, '0', '0.0000', '-0', Decimal('0'), 0, 0.0])
def test_create_omits_unset_or_zero_protection_but_does_not_mutate_callers_evidence(zero):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=envelope({'id': '117'} if request.method == 'POST' else {}))

    async def scenario():
        async with client(handler) as api:
            parameters = {'symbol': 'XAUUSD', 'side': 2, 'price': '100', 'volume': '0.01',
                          'price_tp': zero, 'price_sl': zero}
            await api.create_order(**parameters)
            assert parameters['price_tp'] is zero and parameters['price_sl'] is zero

    run(scenario())
    order = seen[-1]
    assert order.content == b'{"price":"100","price_type":"trigger","side":2,"symbol":"XAUUSD","volume":"0.01"}'
    assert order.headers['SIGN'] == expected_signature(order)


def test_create_still_rejects_boolean_or_nonzero_numeric_protection_before_network():
    def handler(_):
        pytest.fail('Invalid prices must not reach HTTP')

    async def scenario():
        async with client(handler) as api:
            for invalid in (False, True, 1, 0.1, float('nan')):
                with pytest.raises(ValueError):
                    await api.create_order(symbol='XAUUSD', side=2, price='100', volume='0.01', price_sl=invalid)

    run(scenario())


def test_official_rejection_preserves_unknown_label_numeric_code_and_readable_reason():
    payload = {'label': 'TRADFI_INVALID_STOP_PRICE', 'code': 10016,
               'message': 'Invalid stops: price_sl must be below bid.'}

    async def scenario():
        async with client(lambda _: httpx.Response(400, json=payload)) as api:
            with pytest.raises(GateError) as caught:
                await api.symbols()
            detail = caught.value.safe_detail()
            assert detail['code'] == detail['official_label'] == payload['label']
            assert detail['official_code'] == '10016'
            assert detail['official_message'] == payload['message']
            assert '止盈止损' in str(caught.value)
            assert payload['message'] in str(caught.value)
            assert '10016' in str(caught.value)
            assert detail['status_code'] == 400

    run(scenario())


def test_diagnostics_redact_credentials_assignments_tokens_and_control_characters():
    other_key, other_secret, opaque = 'different-key', 'different-secret', 'a7Bd82Xy' * 8
    upper_token = 'ABCDEFGH_IJKLMNOP_QRSTUVWX_YZABCDEF'
    payload = {
        'label': 'TRADFI_ORDER_PROTECTION_VALIDATION_FAILED', 'code': 10016,
        'message': f'Invalid stops\n{KEY}\r\n{SECRET}\t' +
                   f'"KEY":"{other_key}", api_secret={other_secret}; SIGN="{opaque}"; ' +
                   'Authorization: Bearer short-credential; Bearer another-token; ' +
                   f'opaque={opaque}; {upper_token}; bad\u0000\u202e price ' + 'detail ' * 100,
        'data': {'headers': {'Authorization': SECRET}, 'secret': other_secret},
    }

    async def scenario():
        async with client(lambda _: httpx.Response(400, json=payload)) as api:
            with pytest.raises(GateError) as caught:
                await api.symbols()
            detail = caught.value.safe_detail()
            message = detail['official_message']
            text = json.dumps(detail, ensure_ascii=False) + str(caught.value)
            for hidden in (KEY, SECRET, other_key, other_secret, opaque, upper_token, 'short-credential', 'another-token'):
                assert hidden not in text
            assert detail['official_label'] == payload['label']
            assert len(message) <= 320
            assert not any(char in message for char in '\n\r\t\u0000\u202e')
            assert '[redacted]' in message and 'Invalid stops' in message
            assert 'data' not in detail and 'headers' not in detail

    run(scenario())


@pytest.mark.parametrize('field', ['label', 'code', 'message'])
def test_nested_diagnostic_fields_are_ignored_without_stringifying_secrets(field):
    payload = {'label': 'INVALID_ARGUMENT', field: {'nested': SECRET, 'unexpected': 'do-not-display'}}

    async def scenario():
        async with client(lambda _: httpx.Response(400, json=payload)) as api:
            with pytest.raises(GateError) as caught:
                await api.symbols()
            detail = caught.value.safe_detail()
            assert detail['official_' + field] is None
            assert SECRET not in str(caught.value)
            assert 'do-not-display' not in json.dumps(detail)

    run(scenario())


@pytest.mark.parametrize('error_fields', [
    {'code': 200, 'message': 'Ambiguous application status'},
    {'label': 'INVALID_ARGUMENT', 'message': 'Conflicting task result'},
    {'code': '10016', 'label': 'INVALID_ARGUMENT', 'message': 'Conflicting task result'},
])
def test_http_200_queue_id_with_business_error_is_unknown_and_never_retried(error_fields):
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == 'GET':
            return httpx.Response(200, json=envelope())
        return httpx.Response(200, json=envelope({'id': 9007199254740993}, **error_fields))

    async def scenario():
        async with client(handler) as api:
            with pytest.raises(GateUnknownOutcome) as caught:
                await api.create_order(symbol='XAUUSD', side=2, price='100', volume='0.01')
            detail = caught.value.safe_detail()
            assert detail['code'] == 'write_outcome_unknown'
            assert detail['pending']['queue_id'] == '9007199254740993'
            assert detail['official_message'] == error_fields['message']
            assert detail['status_code'] == 200

    run(scenario())
    assert [request.method for request in seen] == ['GET', 'POST']


def test_nonzero_business_code_without_valid_queue_id_stays_a_definite_rejection():
    async def scenario():
        def handler(request):
            return httpx.Response(200, json=envelope() if request.method == 'GET' else
                                  envelope(None, code=10016, message='Invalid stops'))
        async with client(handler) as api:
            with pytest.raises(GateError) as caught:
                await api.create_order(symbol='XAUUSD', side=2, price='100', volume='0.01')
            assert not isinstance(caught.value, GateUnknownOutcome)
            assert caught.value.safe_detail()['official_code'] == '10016'
            assert 'Invalid stops' in str(caught.value)

    run(scenario())
