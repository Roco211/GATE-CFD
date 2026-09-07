"""HTTP integration uses an intercepted official HTTP client; no external requests."""
import copy
import json
import time
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.gate import GateClient

HEADERS = {'X-Grid-Client': 'grid-studio', 'Origin': 'http://127.0.0.1:18473'}
CONFIG = {'symbol': 'XAUUSD', 'direction': 'long', 'lower_price': '4200',
          'upper_price': '4400', 'volume': '0.01', 'grid_count': 10,
          'spacing': 'arithmetic', 'repeat': True, 'stop_loss': None}
SYMBOL = {'symbol': 'XAUUSD', 'symbol_desc': 'Gold', 'status': 'open', 'trade_mode': '4',
          'settlement_currency': 'USD', 'price_precision': 2}
DETAIL = {**SYMBOL, 'contract_volume': '100', 'min_order_volume': '0.01',
          'max_order_volume': '100', 'leverage': '500', 'price_sl_level': '0'}
TICKER = {'last_price': '4397.80', 'bid_price': '4397.78', 'ask_price': '4397.88',
          'highest_price': '4400', 'lowest_price': '4390', 'price_change': '-0.2',
          'status': 'open', 'trade_mode': '4', 'settlement_currency': 'USD'}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setenv('GATE_API_KEY', '')
    monkeypatch.setenv('GATE_API_SECRET', '')


class GateFixture:
    def __init__(self):
        self.calls = []
        self.positions = []
        self.orders = []
        self.history = []
        self.failed_key = 'rejected-test-key'
        self.uid = '12345'

    def transport(self, request):
        self.calls.append((request.method, request.url.path, dict(request.url.params)))
        path = request.url.path.removeprefix('/api/v4')
        if request.headers.get('KEY') == self.failed_key:
            return httpx.Response(401, json={'label': 'INVALID_KEY', 'message': 'invalid'})
        if path == '/tradfi/symbols':
            data = {'list': [SYMBOL]}
        elif path == '/tradfi/symbols/detail':
            assert request.headers.get('SIGN')
            data = {'list': [DETAIL]}
        elif path.endswith('/tickers'):
            data = copy.deepcopy(TICKER)
        elif path.endswith('/klines'):
            data = {'list': [{'t': int(time.time()) // 60 * 60, 'o': '4398',
                              'h': '4400', 'l': '4390', 'c': '4397.8'}]}
        elif path == '/tradfi/users/mt5-account':
            data = {'status': 3, 'mt5_uid': self.uid, 'leverage': 500}
        elif path == '/tradfi/users/assets':
            data = {'equity': '2000', 'balance': '2000', 'margin': '0',
                    'margin_free': '2000', 'unrealized_pnl': '0', 'mt5_uid': self.uid}
        elif path == '/tradfi/positions':
            data = {'list': self.positions}
        elif path == '/tradfi/orders':
            assert request.method == 'GET', 'API tests must never auto-start market orders'
            data = {'list': self.orders}
        elif path == '/tradfi/positions/history':
            data = {'list': self.history, 'total_page': 1, 'total': len(self.history)}
        elif path == '/tradfi/orders/history':
            data = {'list': [], 'total_page': 1, 'total': 0}
        else:
            raise AssertionError(f'Unexpected endpoint {request.method} {path}')
        return httpx.Response(200, json={'data': data, 'timestamp': int(time.time() * 1000)})

    def client(self, key='', secret=''):
        return GateClient(key, secret, transport=httpx.MockTransport(self.transport), min_request_interval=0)


@pytest.fixture
def gate_fixture(monkeypatch):
    fixture = GateFixture()
    monkeypatch.setattr(main, 'GateClient', fixture.client)
    return fixture


@pytest.fixture
def client(tmp_path, gate_fixture):
    app = main.create_app(tmp_path / 'live.sqlite3', auto_tick=False)
    with TestClient(app) as session:
        yield session


def connect(client, **values):
    return client.post('/api/connection/check', headers=HEADERS,
                       json={'key': 'test-public-key', 'secret': 'test-private-secret',
                             'remember': False, **values})


def test_health_is_live_and_unconfigured_account_is_unknown(client):
    assert client.get('/api/health').json()['mode'] == 'live'
    state = client.get('/api/state').json()
    assert state['account'] is None and state['market'] is None and state['spec'] is None
    assert state['strategies'] == [] and state['positions'] == []
    assert not state['connection']['configured']


def test_public_market_is_real_without_credentials(client, gate_fixture):
    response = client.get('/api/gate/market/XAUUSD')
    assert response.status_code == 200, response.text
    quote = response.json()['market']
    assert quote['source'] == 'gate' and quote['bid'] == '4397.78'
    assert quote['transport'] == 'rest' and quote['interval_ms'] == 1000
    state = client.get('/api/state').json()
    assert state['account'] is None and state['spec'] is None
    assert all('/users/' not in p and p != '/tradfi/symbols/detail' for _, p, _ in gate_fixture.calls)


def test_local_mutation_guard_and_host(client):
    for headers in ({}, {'X-Grid-Client': 'wrong'},
                    {**HEADERS, 'Origin': 'https://attacker.example'}, {**HEADERS, 'Origin': 'null'}):
        assert client.post('/api/grid/preview', json=CONFIG, headers=headers).status_code == 403
    assert client.get('/api/state', headers={'Host': 'attacker.example'}).status_code == 400
    response = client.get('/api/state')
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'


@pytest.mark.parametrize('overrides', [{'lower_price': 'NaN'}, {'upper_price': 'Infinity'},
                                      {'volume': 'no'}, {'grid_count': 0}, {'grid_count': 101},
                                      {'grid_count': 1.5}, {'direction': 'both'}])
def test_invalid_grid_is_rejected_before_gate_calls(client, gate_fixture, overrides):
    response = client.post('/api/grid/preview', headers=HEADERS, json={**CONFIG, **overrides})
    assert response.status_code == 422
    assert all(set(field) == {'field', 'message'} for field in response.json()['fields'])
    assert gate_fixture.calls == []


def test_no_simulation_route_or_paper_start(client):
    assert client.post('/api/simulation/price', headers=HEADERS,
                       json={'symbol': 'XAUUSD', 'price': '1'}).status_code == 404
    response = client.post('/api/strategies', headers=HEADERS,
                           json={'config': CONFIG, 'request_id': 'disabled-paper-01', 'mode': 'paper'})
    assert response.status_code == 422
    response = client.post('/api/strategies', headers=HEADERS,
                           json={'config': CONFIG, 'request_id': 'need-key-000001', 'mode': 'live'})
    assert response.status_code == 409


def test_secret_errors_do_not_echo_inputs(client):
    marker = 'credential-not-to-be-returned-128432'
    for data, status in (({'key': {'nested': marker}, 'secret': marker}, 422),
                         ({'key': marker}, 400),
                         ({'key': marker, 'secret': marker + '\ninside'}, 400)):
        response = client.post('/api/connection/check', headers=HEADERS, json=data)
        assert response.status_code == status
        assert marker not in response.text
    assert marker not in client.get('/api/state').text


def test_connect_account_and_live_preview_then_disconnect(client):
    response = connect(client)
    assert response.status_code == 200, response.text
    assert response.json()['live_trading_ready'] is True
    assert response.json()['account_id'] == '12345'
    assert 'test-private-secret' not in response.text and 'test-public-key' not in response.text
    preview = client.post('/api/grid/preview', headers=HEADERS, json=CONFIG)
    assert preview.status_code == 200, preview.text
    assert preview.json()['can_start'] is True
    state = client.get('/api/state').json()
    assert state['spec']['leverage'] == '500'
    assert state['account']['equity'] == '2000'
    disconnected = client.post('/api/connection/disconnect', headers=HEADERS)
    assert disconnected.status_code == 200
    assert not disconnected.json()['configured']
    assert client.get('/api/state').json()['account'] is None


def test_preview_refreshes_stale_connected_account_after_cancel_without_history_pagination(client, gate_fixture):
    assert connect(client).status_code == 200
    started = client.post('/api/strategies', headers=HEADERS,
                          json={'config': CONFIG, 'request_id': 'preview-after-cancel'})
    assert started.status_code == 200, started.text
    sid = started.json()['selected_strategy']['id']
    cancelled = client.post(f'/api/strategies/{sid}/cancel', headers=HEADERS)
    assert cancelled.status_code == 200, cancelled.text
    engine = client.app.state.engine
    engine._snapshots['12345']['fetched_at'] = engine._clock() - 10
    stale = client.get('/api/state').json()
    assert stale['connection']['connected'] is True and stale['account']['stale'] is True
    gate_fixture.calls.clear()
    preview = client.post('/api/grid/preview', headers=HEADERS, json=CONFIG)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body['can_start'] is True and body['blockers'] == []
    assert body['blocker_codes'] == [] and body['account']['stale'] is False
    assert all(method == 'GET' for method, _, _ in gate_fixture.calls)
    paths = {path for _, path, _ in gate_fixture.calls}
    assert '/api/v4/tradfi/users/assets' in paths
    assert '/api/v4/tradfi/positions' in paths and '/api/v4/tradfi/orders' in paths
    assert not any('/history' in path for path in paths)
    assert not engine._snapshot_fresh('12345'), 'Preview-only refresh must not bypass execution reconciliation'


def test_preview_uses_shared_operation_lock_for_account_refresh(client):
    assert connect(client).status_code == 200
    engine = client.app.state.engine
    original = engine.refresh_preview_account
    async def checked_refresh(*args, **kwargs):
        assert client.app.state.op_lock.locked()
        return await original(*args, **kwargs)
    with patch.object(engine, 'refresh_preview_account', side_effect=checked_refresh):
        response = client.post('/api/grid/preview', headers=HEADERS, json=CONFIG)
    assert response.status_code == 200, response.text


def test_rejected_replacement_key_preserves_working_client(client):
    assert connect(client).status_code == 200
    response = connect(client, key='rejected-test-key')
    assert response.status_code == 502
    assert client.get('/api/connection').json()['connected'] is True
    assert client.post('/api/connection/check', headers=HEADERS, json={}).status_code == 200


def test_expired_current_key_immediately_clears_ready(client, gate_fixture):
    assert connect(client).status_code == 200
    gate_fixture.failed_key = 'test-public-key'
    response = client.post('/api/connection/check', headers=HEADERS, json={})
    assert response.status_code == 502
    connection = client.get('/api/connection').json()
    assert not connection['connected'] and not connection['live_trading_ready']
    blocked = client.post('/api/strategies', headers=HEADERS,
                          json={'config': CONFIG, 'request_id': 'expired-key-test-1'})
    assert blocked.status_code == 409


def test_credential_delete_failure_does_not_break_working_client(client, monkeypatch):
    assert connect(client).status_code == 200
    def cannot_delete(_store):
        raise PermissionError('test-only file lock')
    monkeypatch.setattr(main.CredentialStore, 'clear', cannot_delete)
    response = client.post('/api/connection/disconnect', headers=HEADERS)
    assert response.status_code == 500
    assert client.get('/api/connection').json()['connected'] is True
    assert client.post('/api/connection/check', headers=HEADERS, json={}).status_code == 200


def test_start_is_persisted_idempotent_and_cancel_clears_only_strategy(client, gate_fixture):
    assert connect(client).status_code == 200
    body = {'config': CONFIG, 'request_id': 'start-live-test-1', 'mode': 'live'}
    first = client.post('/api/strategies', headers=HEADERS, json=body)
    assert first.status_code == 200, first.text
    second = client.post('/api/strategies', headers=HEADERS, json=body)
    assert second.status_code == 200, second.text
    strategies = first.json()['strategies']
    assert len(second.json()['strategies']) == 1
    assert second.json()['strategies'][0]['id'] == strategies[0]['id']
    assert all(method == 'GET' for method, _, _ in gate_fixture.calls)
    assert client.post('/api/connection/disconnect', headers=HEADERS).status_code == 409
    cancelled = client.post(f"/api/strategies/{strategies[0]['id']}/cancel", headers=HEADERS)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()['strategies'][0]['status'] == 'stopped'
    assert client.post('/api/connection/disconnect', headers=HEADERS).status_code == 200


def test_running_strategy_prevents_switching_to_another_account(client, gate_fixture):
    assert connect(client).status_code == 200
    created = client.post('/api/strategies', headers=HEADERS,
                          json={'config': CONFIG, 'request_id': 'keep-account-test-1'})
    assert created.status_code == 200, created.text
    gate_fixture.uid = '99999'
    response = connect(client, key='another-key')
    assert response.status_code == 400
    assert client.get('/api/connection').json()['account_id'] == '12345'


def test_templates_do_not_trigger_orders(client, gate_fixture):
    response = client.post('/api/templates', headers=HEADERS, json={'name': '黄金', 'config': CONFIG})
    assert response.status_code == 200, response.text
    templates = client.get('/api/templates').json()
    assert len(templates) == 1 and templates[0]['name'] == '黄金'
    assert client.delete('/api/templates/' + templates[0]['id'], headers=HEADERS).status_code == 200
    assert client.get('/api/templates').json() == []
    assert gate_fixture.calls == []


def test_process_lease_rejects_second_owner_and_releases(tmp_path):
    first, second, third = (main.ProcessLease(tmp_path / 'live.lock') for _ in range(3))
    try:
        first.acquire()
        with pytest.raises(RuntimeError, match='只启动一个'):
            second.acquire()
        first.release()
        third.acquire()
    finally:
        for lease in (second, first, third):
            lease.release()


def test_startup_failure_releases_process_lease(tmp_path, gate_fixture):
    database = tmp_path / 'live.sqlite3'
    with patch.object(main, 'LiveEngine', side_effect=ValueError('unsupported state')):
        with pytest.raises(ValueError, match='unsupported state'):
            with TestClient(main.create_app(database, auto_tick=False)):
                pass
    with TestClient(main.create_app(database, auto_tick=False)) as recovered:
        assert recovered.get('/api/health').status_code == 200


def test_sse_contract_is_a_real_market_event(client):
    # Inspect the route's streaming iterator with a deterministic disconnect;
    # no infinite TestClient stream and no network.
    import asyncio
    route = next(r for r in client.app.routes if getattr(r, 'path', None) == '/api/market/stream')
    class RequestDouble:
        cookies = {'grid_access': client.cookies.get('grid_access')}
        async def is_disconnected(self):
            return False
    async def first_event():
        response = await route.endpoint(RequestDouble(), symbol='XAUUSD')
        try:
            event = await anext(response.body_iterator)
            assert event.startswith('event: market\n')
            payload = json.loads(event.split('data: ', 1)[1])
            assert payload['market'] is None
            assert response.media_type == 'text/event-stream'
        finally:
            await response.body_iterator.aclose()
    asyncio.run(first_event())


def make_strategy(client, request_id='native-manage-test-1'):
    assert connect(client).status_code == 200
    response = client.post('/api/strategies', headers=HEADERS,
                           json={'config': CONFIG, 'request_id': request_id})
    assert response.status_code == 200, response.text
    return response.json()['strategies'][0]['id']


@pytest.mark.parametrize('kind,body', [
    ('orders', {'price': 'NaN'}), ('orders', {'price': '0'}),
    ('orders', {'price': '-1'}), ('orders', {'price': True}),
    ('orders', {'price': '4400', 'price_tp': 'Infinity'}),
    ('orders', {'price': '4400', 'price_sl': '-1'}),
    ('orders', {'price': '4400', 'volume': '9'}),
    ('positions', {}), ('positions', {'price_tp': None, 'price_sl': None}),
    ('positions', {'price_tp': 'NaN'}), ('positions', {'price_sl': False}),
    ('positions', {'price_tp': '1', 'symbol': 'EURUSD'}),
])
def test_invalid_management_payload_never_reaches_gate(client, gate_fixture, kind, body):
    response = client.put(f'/api/strategies/missing/{kind}/123', headers=HEADERS, json=body)
    assert response.status_code == 422, response.text
    assert not gate_fixture.calls


@pytest.mark.parametrize('kind,body', [
    ('orders', {'price': '4400'}), ('positions', {'price_tp': '4410'}),
])
def test_management_requires_connected_account_and_owner(client, gate_fixture, kind, body):
    url = f'/api/strategies/missing/{kind}/123'
    assert client.put(url, headers=HEADERS, json=body).status_code == 409
    assert connect(client).status_code == 200
    gate_fixture.calls.clear()
    assert client.put(url, headers=HEADERS, json=body).status_code == 404
    assert not gate_fixture.calls


@pytest.mark.parametrize('body,expected', [
    ({'price': '4300'}, ('4300', None, None)),
    ({'price': '4300', 'price_tp': '4310'}, ('4300', '4310', None)),
    ({'price': '4300', 'price_tp': '0', 'price_sl': '0'}, ('4300', '0', '0')),
])
def test_order_edit_preserves_omission_and_explicit_clear(client, monkeypatch, body, expected):
    sid = make_strategy(client)
    operation = AsyncMock()
    monkeypatch.setattr(client.app.state.engine, 'modify_order', operation, raising=False)
    response = client.put(f'/api/strategies/{sid}/orders/123', headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    operation.assert_awaited_once_with(sid, '123', *expected, client.app.state.gate, '12345')
    assert response.json()['mode'] == 'live'


@pytest.mark.parametrize('body,expected', [
    ({'price_tp': '4410'}, ('4410', None)),
    ({'price_sl': '0'}, (None, '0')),
])
def test_position_edit_preserves_other_protection(client, monkeypatch, body, expected):
    sid = make_strategy(client)
    operation = AsyncMock()
    monkeypatch.setattr(client.app.state.engine, 'modify_position', operation, raising=False)
    response = client.put(f'/api/strategies/{sid}/positions/456', headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    operation.assert_awaited_once_with(sid, '456', *expected, client.app.state.gate, '12345')


def test_single_cancel_routes_only_the_requested_strategy_order(client, monkeypatch):
    sid = make_strategy(client)
    operation = AsyncMock()
    stop_signal = Mock()
    monkeypatch.setattr(client.app.state.engine, 'request_order_cancel', stop_signal, raising=False)
    monkeypatch.setattr(client.app.state.engine, 'cancel_order', operation, raising=False)
    response = client.delete(f'/api/strategies/{sid}/orders/123', headers=HEADERS)
    assert response.status_code == 200, response.text
    operation.assert_awaited_once_with(sid, '123', client.app.state.gate, '12345')
    stop_signal.assert_called_once_with(sid, '123', '12345')


def test_close_strategy_signals_stop_before_waiting_for_operation(client, monkeypatch):
    sid = make_strategy(client)
    engine = client.app.state.engine
    observed = []
    original_stop = engine.request_stop
    def signal(strategy_id, uid):
        observed.append('stop')
        original_stop(strategy_id, uid)
    async def close(strategy_id, gate, uid):
        assert observed == ['stop']
        assert strategy_id == sid and uid == '12345' and gate is client.app.state.gate
        observed.append('close')
    monkeypatch.setattr(engine, 'request_stop', signal)
    monkeypatch.setattr(engine, 'close_strategy', close, raising=False)
    response = client.post(f'/api/strategies/{sid}/close', headers=HEADERS)
    assert response.status_code == 200, response.text
    assert observed == ['stop', 'close']


def test_management_guard_covers_put_delete_and_close(client, gate_fixture):
    operations = [
        ('PUT', '/api/strategies/unknown/orders/123', {'price': '4300'}),
        ('PUT', '/api/strategies/unknown/positions/123', {'price_tp': '4400'}),
        ('DELETE', '/api/strategies/unknown/orders/123', None),
        ('POST', '/api/strategies/unknown/close', None),
    ]
    for method, path, body in operations:
        response = client.request(method, path, json=body, headers={'Origin': 'https://unrelated.example'})
        assert response.status_code == 403
    assert not gate_fixture.calls
    preflight = client.options('/api/strategies/unknown/orders/123', headers={
        'Origin': HEADERS['Origin'], 'Access-Control-Request-Method': 'PUT',
        'Access-Control-Request-Headers': 'content-type,x-grid-client',
    })
    assert preflight.status_code == 200
    assert 'PUT' in preflight.headers['access-control-allow-methods']
