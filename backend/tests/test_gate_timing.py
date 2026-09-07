"""Passive timing tests: exclusively fake clocks and isolated MockTransport."""

import asyncio
import json

import httpx
import pytest

from app.gate import GateClient, GateCredentialsError, GateError, GateUnknownOutcome
from app.telemetry import TimingRecorder, endpoint_template


NOW = 1_800_000_000
KEY = "fake-timing-key-never-real"
SECRET = "fake-timing-secret-never-real"
PHASES = ("clock_wait_ms", "throttle_wait_ms", "network_ms", "backoff_ms", "processing_ms")


class ManualTime:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []

    def timer(self):
        return self.elapsed

    def wall(self):
        return NOW + self.elapsed

    def advance(self, seconds):
        self.elapsed += seconds

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


def envelope(fake, data=None, **extra):
    return {"data": {} if data is None else data, "timestamp": fake.wall() * 1000, **extra}


def client(fake, handler, *, capacity=1000, credentials=True, **kwargs):
    return GateClient(KEY if credentials else "", SECRET if credentials else "",
                      transport=httpx.MockTransport(handler), clock=fake.wall,
                      sleep=fake.sleep, monotonic=fake.timer,
                      timing_recorder=TimingRecorder(capacity=capacity, timer=fake.timer, wall_clock=fake.wall),
                      **kwargs)


def assert_partition(row):
    assert all(row[name] >= 0 for name in PHASES)
    assert row["total_ms"] == pytest.approx(sum(row[name] for name in PHASES), abs=1e-9)


def test_signed_call_partitions_clock_pacing_network_preflight_without_double_counting():
    fake = ManualTime()
    requests, checkpoints = [], []

    def handler(request):
        requests.append((request.method, request.url.path))
        fake.advance(0.020 if request.method == "GET" else 0.050)
        return httpx.Response(200, json=envelope(fake, {"id": 999_888_777_666}), headers={
            "X-Gate-RateLimit-Limit": "5", "X-Gate-RateLimit-Remaining": "4",
            "X-Gate-RateLimit-Reset-Timestamp": "1800000000000",
            "Authorization": SECRET, "x-unrelated-order-id": "999888777666",
        })

    async def scenario():
        async with client(fake, handler, min_request_interval=0.1) as gate:
            def preflight():
                # The clock child has completed; its caller has not. Completion
                # cursors must still deliver the caller after this checkpoint.
                checkpoints.append(gate.timing_sequence)
                fake.advance(0.030)

            with gate.timing_scope("diag:partition"):
                await gate.create_order(symbol="PRIVATEASSET", side=2, volume="0.01", price="100",
                                        before_send=preflight)
            rows = gate.timing_snapshot(scope="diag:partition")
            child, parent = rows
            assert child["sequence"] == 1 and parent["sequence"] == 2
            assert child["parent_call_id"] == parent["call_id"]
            assert child["is_root"] is False and parent["is_root"] is True
            assert child["endpoint"] == "/tradfi/symbols" and child["signed"] is False
            assert parent["endpoint"] == "/tradfi/orders" and parent["signed"] is True
            assert parent["clock_wait_ms"] == pytest.approx(20)
            assert parent["throttle_wait_ms"] == pytest.approx(80)
            assert parent["network_ms"] == pytest.approx(50)
            assert parent["backoff_ms"] == 0
            assert parent["processing_ms"] == pytest.approx(30)
            assert parent["total_ms"] == pytest.approx(180)
            assert sum(row["total_ms"] for row in rows if row["is_root"]) == pytest.approx(180)
            assert child["network_ms"] == pytest.approx(20)
            assert parent["attempts"] == 1 and parent["outcome"] == "success"
            assert parent["started_at"] == NOW
            assert parent["rate_limit"] == {"limit": 5, "remaining": 4, "reset": 1_800_000_000_000}
            assert gate.timing_snapshot(after_sequence=checkpoints[0]) == [parent]
            for row in rows:
                assert_partition(row)
            encoded = json.dumps(rows)
            for sensitive in (KEY, SECRET, "PRIVATEASSET", "999888777666", "Authorization", "body_sha512"):
                assert sensitive not in encoded

    asyncio.run(scenario())
    assert requests == [("GET", "/api/v4/tradfi/symbols"), ("POST", "/api/v4/tradfi/orders")]


def test_retry_backoff_and_pacing_are_disjoint_and_reads_keep_existing_attempt_count():
    fake = ManualTime()
    calls = []

    def handler(request):
        calls.append(request.method)
        fake.advance(0.02 if len(calls) == 1 else 0.03)
        if len(calls) == 1:
            return httpx.Response(503, json=envelope(fake, label="SERVICE_UNAVAILABLE"))
        return httpx.Response(200, json=envelope(fake))

    async def scenario():
        async with client(fake, handler, min_request_interval=0.5, read_retries=1) as gate:
            await gate.ticker("PRIVATEASSET")
            row, = gate.timing_snapshot()
            assert row["endpoint"] == "/tradfi/symbols/{symbol}/tickers"
            assert row["network_ms"] == pytest.approx(50)
            assert row["backoff_ms"] == pytest.approx(250)
            assert row["throttle_wait_ms"] == pytest.approx(230)
            assert row["total_ms"] == pytest.approx(530)
            assert row["attempts"] == 2 and row["status_code"] == 200
            assert row["clock_wait_ms"] == 0 and row["outcome"] == "success"
            assert_partition(row)

    asyncio.run(scenario())
    assert calls == ["GET", "GET"]
    assert fake.sleeps == pytest.approx([0.25, 0.23])


def test_transport_read_retry_is_timed_without_recording_exception_text():
    fake = ManualTime()
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        fake.advance(0.01)
        if calls == 1:
            raise httpx.ReadTimeout(f"{SECRET} https://example.invalid/private", request=request)
        return httpx.Response(200, json=envelope(fake))

    async def scenario():
        async with client(fake, handler, min_request_interval=0, read_retries=1) as gate:
            await gate.symbols()
            row, = gate.timing_snapshot()
            assert row["attempts"] == 2 and row["network_ms"] == pytest.approx(20)
            assert row["backoff_ms"] == pytest.approx(250)
            assert row["total_ms"] == pytest.approx(270)
            assert SECRET not in json.dumps(row) and "example.invalid" not in json.dumps(row)
            assert_partition(row)

    asyncio.run(scenario())


def test_throttle_measurement_includes_waiting_for_another_calls_slot_lock():
    fake = ManualTime()

    async def scenario():
        sleeping, release = asyncio.Event(), asyncio.Event()
        sleep_count = 0

        async def controlled_sleep(seconds):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count == 1:
                sleeping.set()
                await release.wait()
            else:
                fake.advance(seconds)

        def handler(request):
            fake.advance(0.01)
            return httpx.Response(200, json=envelope(fake))

        async with client(fake, handler, min_request_interval=0.1) as gate:
            gate._sleep = controlled_sleep
            await gate.symbols()
            first = asyncio.create_task(gate.categories())
            await sleeping.wait()
            second = asyncio.create_task(gate.ticker("PRIVATEASSET"))
            # Let the second caller enter the lock wait before releasing the
            # first pacing sleep. No real sleep or network is necessary.
            await asyncio.sleep(0)
            fake.advance(0.09)
            release.set()
            await asyncio.gather(first, second)
            rows = gate.timing_snapshot()
            row = next(row for row in rows if row["endpoint"] == "/tradfi/symbols/{symbol}/tickers")
            assert row["is_root"]
            assert row["throttle_wait_ms"] == pytest.approx(190)
            assert row["network_ms"] == pytest.approx(10)
            assert row["total_ms"] == pytest.approx(200)
            assert_partition(row)

    asyncio.run(scenario())


def test_long_rate_limit_remains_one_attempt_and_only_numeric_headers_are_retained():
    fake = ManualTime()
    requests = []

    def handler(request):
        requests.append(request.method)
        fake.advance(0.02)
        return httpx.Response(429, json=envelope(fake, label="TOO_MANY_REQUESTS"), headers={
            "Retry-After": "10", "X-Gate-RateLimit-Limit": "5",
            "X-Gate-RateLimit-Remaining": "0", "X-Gate-RateLimit-Reset-Timestamp": "1800000010",
            "X-Arbitrary": SECRET,
        })

    async def scenario():
        async with client(fake, handler, min_request_interval=0) as gate:
            with pytest.raises(GateError) as caught:
                await gate.ticker("PRIVATEASSET")
            assert caught.value.status_code == 429 and caught.value.retry_after == 10
            row, = gate.timing_snapshot()
            assert row["attempts"] == 1 and row["outcome"] == "error"
            assert row["status_code"] == 429 and row["backoff_ms"] == 0
            assert row["rate_limit"] == {"limit": 5, "remaining": 0, "reset": 1800000010}
            assert_partition(row)
            assert SECRET not in json.dumps(row) and "Retry-After" not in json.dumps(row)

    asyncio.run(scenario())
    assert requests == ["GET"] and fake.sleeps == []


@pytest.mark.parametrize("failure,expected,status", [("reject", "error", 400), ("server", "unknown", 503), ("transport", "unknown", None)])
def test_write_outcomes_are_recorded_without_automatic_retry(failure, expected, status):
    fake = ManualTime()
    writes = []

    def handler(request):
        fake.advance(0.01)
        if request.method == "GET":
            return httpx.Response(200, json=envelope(fake))
        writes.append(request.method)
        if failure == "transport":
            raise httpx.ReadTimeout(SECRET, request=request)
        return httpx.Response(status, json=envelope(fake, label="INVALID_ARGUMENT", message=SECRET))

    async def scenario():
        async with client(fake, handler, min_request_interval=0) as gate:
            with pytest.raises(GateUnknownOutcome if expected == "unknown" else GateError):
                await gate.cancel_order("999888777666")
            parent, = [row for row in gate.timing_snapshot() if row["is_root"]]
            assert parent["method"] == "DELETE" and parent["endpoint"] == "/tradfi/orders/{order_id}"
            assert parent["outcome"] == expected and parent["status_code"] == status
            assert parent["attempts"] == 1 and parent["network_ms"] == pytest.approx(10)
            assert parent["backoff_ms"] == 0
            assert "999888777666" not in json.dumps(parent) and SECRET not in json.dumps(parent)
            assert_partition(parent)

    asyncio.run(scenario())
    assert writes == ["DELETE"]


def test_before_send_abort_remains_unsent_error_and_preserves_original_exception():
    fake = ManualTime()
    requests = []
    abort = httpx.ReadTimeout("preflight only; " + SECRET)

    def handler(request):
        requests.append(request.method)
        fake.advance(0.01)
        return httpx.Response(200, json=envelope(fake))

    async def scenario():
        async with client(fake, handler, min_request_interval=0) as gate:
            async def preflight():
                fake.advance(0.007)
                await asyncio.sleep(0)
                raise abort

            with pytest.raises(httpx.ReadTimeout) as caught:
                await gate.close_position("999888777666", before_send=preflight)
            assert caught.value is abort
            parent, = [row for row in gate.timing_snapshot() if row["is_root"]]
            assert parent["attempts"] == 0 and parent["status_code"] is None
            assert parent["network_ms"] == 0 and parent["processing_ms"] == pytest.approx(7)
            assert parent["outcome"] == "error"
            assert parent["endpoint"] == "/tradfi/positions/{position_id}/close"
            assert_partition(parent)

    asyncio.run(scenario())
    assert requests == ["GET"]


def test_cancellation_during_clock_probe_records_both_spans_and_resets_context():
    fake = ManualTime()

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            fake.advance(0.012)
            if request.url.path.endswith("/symbols"):
                entered.set()
                await release.wait()
            return httpx.Response(200, json=envelope(fake))

        async with client(fake, handler, min_request_interval=0) as gate:
            with gate.timing_scope("diag:cancelled"):
                task = asyncio.create_task(gate.assets())
                await entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            rows = gate.timing_snapshot()
            assert len(rows) == 2 and all(row["outcome"] == "cancelled" for row in rows)
            child, parent = rows
            assert child["attempts"] == 1 and parent["attempts"] == 0
            assert child["parent_call_id"] == parent["call_id"]
            assert parent["clock_wait_ms"] == pytest.approx(12)
            assert parent["network_ms"] == 0
            for row in rows:
                assert_partition(row)
            await gate.ticker("AFTERCANCEL")
            last = gate.timing_snapshot()[-1]
            assert last["is_root"] and last["scope"] is None and last["outcome"] == "success"

    asyncio.run(scenario())


def test_scopes_do_not_label_existing_background_tasks_and_nested_scopes_restore():
    fake = ManualTime()

    async def scenario():
        release = asyncio.Event()

        async def handler(request):
            fake.advance(0.001)
            await asyncio.sleep(0)
            return httpx.Response(200, json=envelope(fake))

        async with client(fake, handler, min_request_interval=0) as gate:
            async def background():
                await release.wait()
                await gate.categories()

            background_task = asyncio.create_task(background())
            with gate.timing_scope("diag:outer"):
                release.set()
                with gate.timing_scope("diag:inner"):
                    await gate.ticker("INNERSYMBOL")
                await gate.symbols()
                await background_task
            rows = gate.timing_snapshot()
            assert all(row["is_root"] for row in rows)
            assert gate.timing_snapshot(scope="diag:inner")[0]["endpoint"] == "/tradfi/symbols/{symbol}/tickers"
            assert gate.timing_snapshot(scope="diag:outer")[0]["endpoint"] == "/tradfi/symbols"
            background_row, = [row for row in rows if row["endpoint"] == "/tradfi/symbols/categories"]
            assert background_row["scope"] is None

    asyncio.run(scenario())


def test_ring_is_bounded_snapshots_are_deep_copies_and_sequence_is_incremental():
    fake = ManualTime()

    def handler(request):
        fake.advance(0.001)
        return httpx.Response(200, json=envelope(fake), headers={"X-Gate-RateLimit-Limit": "5"})

    async def scenario():
        async with client(fake, handler, min_request_interval=0) as gate:
            for _ in range(1003):
                await gate.symbols()
            rows = gate.timing_snapshot()
            assert len(rows) == 1000 and rows[0]["sequence"] == 4
            assert gate.timing_sequence == 1003
            assert [row["sequence"] for row in gate.timing_snapshot(after_sequence=1000)] == [1001, 1002, 1003]
            rows[-1]["rate_limit"]["limit"] = "injected"
            rows[-1]["endpoint"] = "injected"
            assert gate.timing_snapshot()[-1]["rate_limit"] == {"limit": 5}
            assert gate.timing_snapshot()[-1]["endpoint"] == "/tradfi/symbols"

    asyncio.run(scenario())


def test_failures_before_http_and_untrusted_metadata_cannot_reveal_payloads_or_credentials():
    fake = ManualTime()

    def handler(request):
        return httpx.Response(200, json=envelope(fake, {"secret": SECRET}), headers={
            "X-Gate-RateLimit-Limit": SECRET,
            "X-Gate-RateLimit-Remaining": "NaN",
            "X-Gate-RateLimit-Reset-Timestamp": "-1",
            "Set-Cookie": KEY,
        })

    async def scenario():
        async with client(fake, handler, min_request_interval=0) as gate:
            with gate.timing_scope("diag:" + SECRET):
                await gate.ticker("PRIVATEASSET")
            with pytest.raises(ValueError):
                await gate._request(SECRET, "/private/" + KEY + "?" + SECRET, query={KEY: SECRET})
            rows = gate.timing_snapshot()
            assert rows[0]["scope"] is None and rows[0]["rate_limit"] == {}
            assert rows[1]["endpoint"] == "/tradfi/{endpoint}" and rows[1]["method"] == "OTHER"
            assert rows[1]["attempts"] == 0 and rows[1]["outcome"] == "error"
            encoded = json.dumps(rows)
            for value in (SECRET, KEY, "PRIVATEASSET", "Set-Cookie", "/private/"):
                assert value not in encoded
        async with client(fake, handler, credentials=False, min_request_interval=0) as gate:
            with pytest.raises(GateCredentialsError):
                await gate.assets()
            row, = gate.timing_snapshot()
            assert row["attempts"] == 0 and row["outcome"] == "error" and row["status_code"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("path,template", [
    ("/tradfi/orders/123456789", "/tradfi/orders/{order_id}"),
    ("/tradfi/orders/log/123456789", "/tradfi/orders/log/{log_id}"),
    ("/tradfi/orders/history?symbol=PRIVATEASSET", "/tradfi/orders/history"),
    ("/tradfi/positions/123456789", "/tradfi/positions/{position_id}"),
    ("/tradfi/positions/123456789/close", "/tradfi/positions/{position_id}/close"),
    ("/tradfi/symbols/PRIVATEASSET/klines", "/tradfi/symbols/{symbol}/klines"),
    ("/tradfi/private-secret/path", "/tradfi/{endpoint}"),
])
def test_endpoint_templates_are_static_whitelisted_constants(path, template):
    assert endpoint_template(path) == template


@pytest.mark.parametrize("capacity", [0, -1, 1001, True, 1.5])
def test_invalid_ring_capacity_is_rejected(capacity):
    with pytest.raises(ValueError):
        TimingRecorder(capacity=capacity)
