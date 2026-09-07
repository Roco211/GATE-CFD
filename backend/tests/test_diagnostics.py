"""Only in-memory fakes and the existing fully intercepted HTTP fixture."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import json
import time
from unittest.mock import AsyncMock

import pytest
import httpx
from pydantic import ValidationError

from app.diagnostics import DiagnosticsRequest, DiagnosticsService, ENDPOINTS, _safe_timing, _stats, submission_intervals
from app.gate import GateClient, GateError
from test_native_api import HEADERS, UID, native_session


SCOPE = ContextVar('diagnostic_test_scope', default=None)
PATHS = {name: path for name, _, path in ENDPOINTS}


class ReadGate:
    def __init__(self, delay=0):
        self.delay, self.calls, self.records = delay, [], []
        self._min_request_interval = .25
        self.started = asyncio.Event()
        self.block = None
        self.fail = set()
        self.cancelled = 0
        self.closed = False

    @property
    def timing_sequence(self):
        return len(self.records)

    @contextmanager
    def timing_scope(self, scope):
        token = SCOPE.set(scope)
        try:
            yield
        finally:
            SCOPE.reset(token)

    def timing_snapshot(self, after_sequence=0, scope=None):
        return copy.deepcopy([row for row in self.records if row['sequence'] > after_sequence
                              and (scope is None or row['scope'] == scope)])

    async def _read(self, name):
        self.calls.append(name)
        self.started.set()
        try:
            if self.block is not None:
                await self.block.wait()
            await asyncio.sleep(self.delay)
            if name in self.fail:
                raise GateError('gate_error', 'SECRET-IN-ERROR api_key=do-not-expose')
            return {'data': {'secret': 'SECRET-IN-BODY', 'mt5_uid': 'account-id-never-in-report'}}
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            scope = SCOPE.get()
            # A nested clock record must not be double-counted with its parent.
            self.records.append({'sequence': len(self.records) + 1, 'scope': scope, 'is_root': False,
                                 'method': 'GET', 'endpoint': '/tradfi/symbols', 'total_ms': 5,
                                 'network_ms': 5, 'clock_wait_ms': 0, 'throttle_wait_ms': 0,
                                 'backoff_ms': 0, 'processing_ms': 0})
            self.records.append({'sequence': len(self.records) + 1, 'scope': scope, 'is_root': True,
                                 'method': 'GET', 'endpoint': PATHS[name], 'total_ms': 10,
                                 'network_ms': 2, 'clock_wait_ms': 5, 'throttle_wait_ms': 1,
                                 'backoff_ms': 1, 'processing_ms': 1, 'status_code': 200,
                                 'outcome': 'success', 'secret': 'TELEMETRY-SECRET',
                                 'body': {'id': 'position-secret-id'}, 'rate_limit': {'limit': 5, 'remaining': 4}})

    async def ticker(self, symbol):
        return await self._read('ticker')

    async def account(self):
        return await self._read('account')

    async def assets(self):
        return await self._read('assets')

    async def orders(self):
        return await self._read('orders')

    async def positions(self):
        return await self._read('positions')

    def __getattr__(self, name):
        if name in {'create_order', 'cancel_order', 'update_order', 'close_position', 'update_position'}:
            raise AssertionError(f'Diagnostics accessed forbidden write method {name}')
        raise AttributeError(name)


async def finished(service):
    await service._task
    return service.latest()


def test_stats_small_sample_percentiles_are_defined():
    assert _stats([]) is None
    assert _stats([1, 7, 3]) == {'count': 3, 'min': 1, 'p50': 3, 'p95': 7, 'max': 7, 'avg': 3.667, 'total': 11}


def test_complete_job_is_serial_scoped_and_payload_free():
    async def run():
        gate = ReadGate()
        service = DiagnosticsService(lambda: gate)
        started = service.start({'symbol': 'xauusd', 'samples': 3})
        assert started['status'] == 'queued'
        assert not gate.calls, 'HTTP route allocation itself must not perform reads'
        job = await finished(service)
        assert job['status'] == 'completed'
        assert job['progress']['completed'] == job['progress']['total'] == 15
        assert gate.calls == [name for _ in range(3) for name, _, _ in ENDPOINTS]
        report = job['report']
        assert report['read_only'] is True and report['source'] == 'gate'
        for row in report['endpoint_stats']:
            assert row['count'] == row['success'] == 3 and row['failed'] == 0
            assert row['stats']['network_ms']['total'] == 6, 'Nested clock HTTP must not be added again'
            assert row['stats']['clock_wait_ms']['total'] == 15
        assert report['recent_real_requests']['status'] == 'not_measured'
        assert report['estimates'][0]['kind'] == 'estimate'
        serialized = json.dumps(job)
        for forbidden in ('SECRET-IN-BODY', 'account-id-never-in-report', 'TELEMETRY-SECRET', 'position-secret-id'):
            assert forbidden not in serialized
        job['report']['read_only'] = False
        assert service.latest()['report']['read_only'] is True
        await service.aclose()
    asyncio.run(run())


def test_concurrent_clicks_reuse_job_and_cancel_retains_partial_report():
    async def run():
        gate = ReadGate()
        gate.block = asyncio.Event()
        service = DiagnosticsService(lambda: gate)
        first = service.start({'symbol': 'XAUUSD', 'samples': 3})
        await gate.started.wait()
        second = service.start({'symbol': 'EURUSD', 'samples': 5})
        assert first['id'] == second['id'] and second['symbol'] == 'XAUUSD'
        cancelled = await service.cancel(first['id'])
        assert cancelled['status'] == 'cancelled'
        assert cancelled['report']['read_only'] is True
        assert len(gate.calls) == gate.cancelled == 1
        assert service.latest()['id'] == first['id']
        assert (await service.cancel(first['id']))['status'] == 'cancelled'
        await service.aclose()
    asyncio.run(run())


def test_cancel_before_scheduling_and_shutdown_cleanup():
    async def run():
        gate = ReadGate()
        service = DiagnosticsService(lambda: gate)
        job = service.start({'samples': 3})
        await service.aclose()
        assert service._task.done()
        assert not gate.calls
        assert service.get(job['id'])['status'] == 'cancelled'
        assert service.get(job['id'])['report']['read_only']
        with pytest.raises(ValueError):
            service.start({'samples': 3})
    asyncio.run(run())


def test_request_timeout_and_whole_job_budget_are_bounded():
    async def run():
        gate = ReadGate(delay=10)
        service = DiagnosticsService(lambda: gate, request_timeout=.015, overall_timeout=.06)
        started = time.monotonic()
        service.start({'samples': 5})
        job = await finished(service)
        assert time.monotonic() - started < .5
        assert job['status'] == 'failed'
        assert 1 <= job['progress']['completed'] < 25
        assert len(gate.calls) < 25
        samples = [sample for row in job['report']['endpoint_stats'] for sample in row['samples']]
        assert all(sample['status'] == 'timeout' for sample in samples)
        assert gate.cancelled >= 1
        await service.aclose()
    asyncio.run(run())


def test_read_failures_are_counted_but_do_not_echo_errors_or_prevent_other_samples():
    async def run():
        gate = ReadGate()
        gate.fail = {'assets'}
        service = DiagnosticsService(lambda: gate)
        service.start({'samples': 3})
        job = await finished(service)
        assert job['status'] == 'completed'
        failed = next(row for row in job['report']['endpoint_stats'] if row['endpoint'].endswith('/assets'))
        assert failed['failed'] == 3 and failed['success'] == 0
        assert all(sample['error_code'] == 'gate_error' for sample in failed['samples'])
        assert 'SECRET-IN-ERROR' not in json.dumps(job)
        assert 'do-not-expose' not in json.dumps(job)
        await service.aclose()
    asyncio.run(run())


def test_only_last_ten_reports_are_retained_and_reopen_uses_latest():
    async def run():
        service = DiagnosticsService(lambda: ReadGate())
        # Stable provider identity per job is required, just like app.state.gate.
        gate = ReadGate()
        service._gate_provider = lambda: gate
        first = None
        for _ in range(12):
            job = service.start({'samples': 3})
            first = first or job['id']
            await finished(service)
        assert len(service._jobs) == 10
        assert service.latest()['id'] == job['id']
        assert service.get(job['id'])['status'] == 'completed'
        with pytest.raises(KeyError):
            service.get(first)
        await service.aclose()
    asyncio.run(run())


def test_connection_change_cancels_without_reading_new_client():
    async def run():
        old, new = ReadGate(), ReadGate()
        holder = [old]
        service = DiagnosticsService(lambda: holder[0])
        original = old.ticker
        async def switching(symbol):
            value = await original(symbol)
            holder[0] = new
            return value
        old.ticker = switching
        service.start({'samples': 3})
        job = await finished(service)
        assert job['status'] == 'cancelled'
        assert job['cancel_reason'] == 'connection_changed'
        assert old.calls == ['ticker'] and not new.calls
        await service.aclose()
    asyncio.run(run())


def test_lock_probe_has_short_timeout_releases_acquired_locks_and_never_holds_during_get():
    async def run():
        busy, available = asyncio.Lock(), asyncio.Lock()
        await busy.acquire()
        gate = ReadGate()
        original = gate._read
        async def checking(name):
            assert not available.locked()
            return await original(name)
        gate._read = checking
        service = DiagnosticsService(lambda: gate, locks={'op_lock': busy, 'market_lock': available}, lock_timeout=.01)
        service.start({'samples': 3})
        job = await finished(service)
        assert job['report']['stages'][0]['status'] == 'timeout'
        assert job['report']['stages'][1]['status'] == 'measured'
        assert not available.locked() and busy.locked()
        busy.release()
        await service.aclose()
    asyncio.run(run())


def test_passive_writes_are_existing_telemetry_only_and_ids_are_not_exposed():
    async def run():
        gate = ReadGate()
        gate.records = [{'sequence': 1, 'scope': None, 'is_root': True, 'method': 'POST',
                         'endpoint': '/tradfi/orders', 'total_ms': 80, 'network_ms': 70,
                         'throttle_wait_ms': 10, 'status_code': 200, 'outcome': 'success',
                         'order_id': 'sensitive-order-id', 'request': {'key': 'private-key'}}]
        service = DiagnosticsService(lambda: gate)
        service.start({'samples': 3})
        job = await finished(service)
        recent = job['report']['recent_real_requests']
        assert recent['status'] == 'measured' and len(recent['items']) == 1
        assert recent['items'][0]['total_ms'] == 80
        assert 'sensitive-order-id' not in json.dumps(job)
        assert 'private-key' not in json.dumps(job)
        assert all(name in PATHS for name in gate.calls)
        await service.aclose()
    asyncio.run(run())


def test_unknown_timing_fields_and_concrete_resource_paths_are_not_allowed():
    assert _safe_timing({'method': 'POST', 'endpoint': '/tradfi/orders/123456'}) is None
    clean = _safe_timing({'method': 'DELETE', 'endpoint': '/tradfi/orders/{order_id}',
                          'total_ms': float('nan'), 'network_ms': True, 'scope': 'secret',
                          'outcome': 'SECRET', 'rate_limit': {'limit': 'secret'}, 'headers': {'KEY': 'secret'}})
    assert clean == {'method': 'DELETE', 'endpoint': '/tradfi/orders/{order_id}', 'is_root': True, 'rate_limit': {}}


@pytest.mark.parametrize('values', [{'samples': 1}, {'samples': 100}, {'samples': 3.0}, {'samples': '3'},
                                   {'samples': True}, {'symbol': '../orders'}, {'write': True}])
def test_request_cannot_inject_methods_or_increase_load(values):
    with pytest.raises(ValidationError):
        DiagnosticsRequest.model_validate(values)


def test_http_job_routes_are_guarded_recoverable_and_never_execute_engine(native_session, monkeypatch):
    session, exchange = native_session
    engine = session.app.state.engine
    for name in ('start', 'cycle', 'reconcile', '_refresh', 'refresh_preview_account', 'cancel', 'close_strategy', 'resume_strategy'):
        monkeypatch.setattr(engine, name, AsyncMock(side_effect=AssertionError(f'Diagnostics invoked {name}')))
    assert session.get('/api/diagnostics/latest').json() == {'job': None}
    assert session.post('/api/diagnostics/start', json={'samples': 3}).status_code == 403
    exchange.calls.clear()
    response = session.post('/api/diagnostics/start', json={'symbol': 'XAUUSD', 'samples': 3}, headers=HEADERS)
    assert response.status_code == 200, response.text
    job_id = response.json()['id']
    async def wait_job():
        await session.app.state.diagnostics._task
    session.portal.call(wait_job)
    result = session.get(f'/api/diagnostics/{job_id}').json()
    assert result['status'] == 'completed'
    assert session.get('/api/diagnostics/latest').json()['job']['id'] == job_id
    assert result['report']['local_state']['strategy_count'] == 0
    assert all(method == 'GET' for method, _, _ in exchange.calls)
    assert not exchange.writes
    assert all(name not in json.dumps(result) for name in ('native-test-key', 'native-test-secret', UID))
    assert session.get('/api/diagnostics/does-not-exist').status_code == 404
    assert session.post(f'/api/diagnostics/{job_id}/cancel', headers=HEADERS).json()['status'] == 'completed'


def test_http_invalid_samples_fail_before_gets(native_session):
    session, exchange = native_session
    exchange.calls.clear()
    result = session.post('/api/diagnostics/start', headers=HEADERS, json={'samples': 10})
    assert result.status_code == 422
    assert not exchange.calls


@pytest.mark.parametrize('http_status,error_code', [(429, 'gate_rate_limited'), (401, 'authentication_failed'), (503, 'gate_server_error')])
def test_actual_adapter_errors_keep_numeric_status_attempts_and_safe_limit_headers(http_status, error_code):
    calls = []
    def handler(request):
        assert request.method == 'GET'
        calls.append(request.url.path)
        if request.url.path.endswith('/assets'):
            return httpx.Response(http_status, json={'label': 'SOME_ERROR', 'message': 'api_key=hidden-diagnostic-key SECRET-BODY'},
                                  headers={'Retry-After': '10', 'X-Gate-RateLimit-Limit': '5',
                                           'X-Gate-RateLimit-Remaining': '0', 'X-Gate-RateLimit-Reset-Timestamp': '1800000000000',
                                           'authorization': 'secret-header'})
        return httpx.Response(200, json={'data': {}, 'timestamp': time.time() * 1000})
    async def run():
        async with GateClient('hidden-diagnostic-key', 'hidden-diagnostic-secret', transport=httpx.MockTransport(handler),
                              read_retries=0, min_request_interval=0) as gate:
            service = DiagnosticsService(lambda: gate)
            service.start({'samples': 3})
            job = await finished(service)
            assets = next(row for row in job['report']['endpoint_stats'] if row['endpoint'].endswith('/assets'))
            sample = assets['samples'][0]
            assert sample['status'] == 'failed'
            assert sample['error_code'] == error_code
            assert sample['status_code'] == http_status
            assert sample['attempts'] == 1
            assert sample['rate_limit'] == {'limit': 5, 'remaining': 0, 'reset': 1800000000000}
            assert job['report']['limits']['observed_gate_limits']
            assert assets['stats']['network_ms'] is not None
            serialized = json.dumps(job)
            assert all(secret not in serialized for secret in ('hidden-diagnostic-key', 'hidden-diagnostic-secret', 'SECRET-BODY', 'secret-header'))
            if http_status == 429:
                assert any(item['title'] == '观察到 Gate 限流' for item in job['report']['findings'])
                assert sum(path.endswith('/assets') for path in calls) == 1, 'Do not probe through shared cooldown'
                assert assets['stats']['network_ms']['count'] == 1, 'A cooldown rejection is not a zero-latency HTTP sample'
            await service.aclose()
    asyncio.run(run())


def test_existing_submission_intervals_are_preparation_gaps_without_ids_or_payloads():
    result = submission_intervals([
        {'id': 'private-intent', 'grid_index': 0, 'generation': 0, 'submitted_at': 100, 'request': {'secret': 'do-not-export'}},
        {'grid_index': 1, 'generation': 0, 'submitted_at': 109.288},
        {'grid_index': 2, 'generation': 0, 'submitted_at': 118.550},
        {'grid_index': 2, 'generation': 0, 'submitted_at': 119},
        {'grid_index': 3, 'generation': 1, 'submitted_at': 150},
        {'grid_index': 4, 'generation': 0, 'submitted_at': 151, 'not_sent': True},
    ])
    assert result['status'] == 'measured' and result['count'] == 2
    assert result['values_ms'] == [9288, 9262]
    assert result['stats']['p50'] == 9262
    assert 'private-intent' not in json.dumps(result)
    assert 'do-not-export' not in json.dumps(result)
    assert submission_intervals([])['status'] == 'not_measured'


def test_execution_estimate_includes_two_reconciliations_and_is_not_called_a_measurement():
    async def run():
        gate = ReadGate()
        service = DiagnosticsService(lambda: gate)
        service.start({'samples': 3})
        job = await finished(service)
        report = job['report']
        assert report['limits']['execution_read_model']['per_grid_requests'] == '2*P+12+2*W'
        examples = [item for item in report['estimates'] if item['label'].startswith('每格')]
        assert len(examples) == 3
        assert all(item['kind'] == 'estimate' for item in examples)
        assert '14 次请求' in examples[0]['label']
        assert examples[0]['value_ms'] == 14 * 250
        assert '不能当作真实' in examples[0]['basis']
        await service.aclose()
    asyncio.run(run())


def test_cancel_keeps_already_completed_samples():
    async def run():
        gate = ReadGate()
        original = gate.orders
        async def blocked_orders():
            gate.block = asyncio.Event()
            return await original()
        gate.orders = blocked_orders
        service = DiagnosticsService(lambda: gate)
        first = service.start({'samples': 3})
        while len(gate.calls) < 4:
            await asyncio.sleep(0)
        result = await service.cancel(first['id'])
        assert result['status'] == 'cancelled'
        assert result['progress']['completed'] == 3
        assert sum(row['count'] for row in result['report']['endpoint_stats']) == 3
        assert service._task.done()
        await service.aclose()
    asyncio.run(run())


def test_unconfigured_client_reports_credentials_missing_not_zero_network_latency():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        assert request.method == 'GET' and '/symbols/' in request.url.path
        assert 'KEY' not in request.headers
        return httpx.Response(200, json={'data': {}, 'timestamp': time.time() * 1000})
    async def run():
        async with GateClient(transport=httpx.MockTransport(handler), min_request_interval=0) as gate:
            service = DiagnosticsService(lambda: gate)
            service.start({'samples': 3})
            job = await finished(service)
            account = next(row for row in job['report']['endpoint_stats'] if row['endpoint'].endswith('/mt5-account'))
            assert account['failed'] == 3
            assert account['stats']['network_ms'] is None
            assert all(sample['error_code'] == 'credentials_missing' and sample['attempts'] == 0 for sample in account['samples'])
            assert len(calls) == 3
            await service.aclose()
    asyncio.run(run())
