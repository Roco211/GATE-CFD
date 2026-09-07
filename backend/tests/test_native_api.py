"""Native-order HTTP flows through the real adapter and an in-memory exchange.

The dotenv loader is disabled before importing the application. Every app owns a
temporary database, no credential store is read, workers are disabled, and every
Gate request is intercepted by MockTransport. No real account is accessed.
"""
import asyncio
import copy
import hashlib
import hmac
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

with patch('dotenv.load_dotenv', return_value=False):
    from app import main

from app.gate import GateClient
from app.live import LiveEngine
from app.market import LiveMarket


HEADERS = {'X-Grid-Client': 'grid-studio', 'Origin': 'http://127.0.0.1:18473'}
KEY, SECRET, UID = 'native-test-key', 'native-test-secret', '70001'
CONFIG = {'symbol': 'XAUUSD', 'direction': 'long', 'lower_price': '100',
          'upper_price': '120', 'volume': '0.01', 'grid_count': 4,
          'spacing': 'arithmetic', 'repeat': True, 'stop_loss': '90'}
SYMBOL = {'symbol': 'XAUUSD', 'symbol_desc': 'Gold', 'status': 'open',
          'trade_mode': '4', 'settlement_currency': 'USD', 'price_precision': 2}
DETAIL = {**SYMBOL, 'contract_volume': '100', 'min_order_volume': '0.01',
          'max_order_volume': '100', 'leverage': '100', 'price_sl_level': '0'}


class NativeExchange:
    def __init__(self):
        self.now = 1_800_000_000.0
        self.orders, self.positions, self.logs = {}, {}, {}
        self.order_history, self.position_history = [], []
        self.calls, self.writes = [], []
        self.sequence = 0
        self.mismatch_log_id = False
        self.fail_next_update = None
        self.block_create = False
        self.create_started = threading.Event()
        self.release_create = asyncio.Event()

    def clock(self):
        return self.now

    def response(self, data):
        return httpx.Response(200, json={'data': copy.deepcopy(data), 'timestamp': int(self.now * 1000)})

    async def intercepted_transport(self, request):
        if self.block_create and request.method == 'POST' and request.url.path == '/api/v4/tradfi/orders':
            self.create_started.set()
            await self.release_create.wait()
        return self.transport(request)

    def transport(self, request):
        path = request.url.path.removeprefix('/api/v4')
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        assert request.url.host == 'api.gateio.ws'
        public = path == '/tradfi/symbols' or path.endswith(('/tickers', '/klines'))
        if not public:
            assert request.headers['KEY'] == KEY
            signature = '\n'.join([request.method, request.url.path, request.url.query.decode(),
                                   hashlib.sha512(request.content).hexdigest(), request.headers['Timestamp']])
            assert request.headers['SIGN'] == hmac.new(SECRET.encode(), signature.encode(), hashlib.sha512).hexdigest()
        if request.method != 'GET':
            self.writes.append((request.method, path, copy.deepcopy(body)))
            return self.write(request.method, path, body)
        if path == '/tradfi/symbols':
            return self.response({'list': [SYMBOL]})
        if path == '/tradfi/symbols/detail':
            return self.response({'list': [DETAIL]})
        if path.endswith('/tickers'):
            return self.response({**SYMBOL, 'last_price': '107.90', 'bid_price': '107.80',
                                  'ask_price': '108.00', 'highest_price': '120',
                                  'lowest_price': '100', 'price_change': '0.2'})
        if path.endswith('/klines'):
            return self.response({'list': [{'t': int(self.now) // 60 * 60, 'o': '107',
                                           'h': '109', 'l': '106', 'c': '107.9'}]})
        if path == '/tradfi/users/mt5-account':
            return self.response({'mt5_uid': UID, 'status': 3, 'leverage': '100'})
        if path == '/tradfi/users/assets':
            return self.response({'mt5_uid': UID, 'balance': '100000', 'equity': '100000',
                                  'margin': '0', 'margin_free': '100000', 'unrealized_pnl': '0'})
        if path == '/tradfi/orders':
            return self.response({'list': list(self.orders.values())})
        if path == '/tradfi/positions':
            return self.response({'list': list(self.positions.values())})
        if path == '/tradfi/orders/history':
            query = request.url.params
            rows = self.order_history
            if 'symbol' in query:
                rows = [row for row in rows if row.get('symbol') == query['symbol']]
            if 'side' in query:
                rows = [row for row in rows if str(row.get('side')) == query['side']]
            # Gate filters setup time even when a pending order fills much later.
            if 'begin_time' in query:
                rows = [row for row in rows if int(row.get('time_setup') or row['time_done']) >= int(query['begin_time'])]
            if 'end_time' in query:
                rows = [row for row in rows if int(row.get('time_setup') or row['time_done']) <= int(query['end_time'])]
            return self.response({'list': rows})
        if path == '/tradfi/positions/history':
            return self.response({'list': self.position_history, 'total_page': 1,
                                  'total': len(self.position_history)})
        if path.startswith('/tradfi/orders/log/'):
            candidate = path.rsplit('/', 1)[1]
            if candidate not in self.logs:
                return httpx.Response(404, json={'label': 'ORDER_NOT_FOUND', 'message': 'test log unavailable'})
            log = copy.deepcopy(self.logs[candidate])
            if self.mismatch_log_id:
                log['log_id'] = '999888777'
            return self.response(log)
        raise AssertionError(f'Unexpected intercepted request {request.method} {path}')

    def write(self, method, path, body):
        if method == 'PUT' and self.fail_next_update is not None:
            status, self.fail_next_update = self.fail_next_update, None
            return httpx.Response(status, json={'label': 'INVALID_ARGUMENT', 'message': 'test update outcome'})
        if method == 'POST' and path == '/tradfi/orders':
            assert body['price_type'] == 'trigger', 'Native grids must send Gate pending orders'
            self.sequence += 1
            queue_id = str(1000 + self.sequence)
            order_id = str(9007199254740992 + self.sequence)
            self.orders[order_id] = {'price_tp': '0', 'price_sl': '0', **body, 'order_id': order_id, 'state': 1,
                                     'finished': 0, 'time_setup': int(self.now)}
            # Official log.price is the average execution price; an unfilled
            # order need not expose its trigger or protection prices here.
            self.logs[queue_id] = {'order_id': order_id, 'log_id': queue_id, 'state': 1,
                                   'symbol': body['symbol'], 'price_type': body['price_type'],
                                   'side': body['side'], 'volume': body['volume'], 'price': '0'}
            return self.response({'id': queue_id})
        if path.startswith('/tradfi/orders/'):
            order_id = path.rsplit('/', 1)[1]
            assert order_id in self.orders, 'Only an existing pending order may be written'
            if method == 'PUT':
                assert set(body) == {'price', 'price_tp', 'price_sl'}
                assert all(isinstance(value, str) for value in body.values())
                self.orders[order_id].update(body)
                return self.response(self.orders[order_id])
            if method == 'DELETE':
                row = self.orders.pop(order_id)
                self.order_history.append({**row, 'state': 2, 'finished': 1,
                                           'order_opt_type': row['side'], 'close_pnl': '0',
                                           'trigger_price': row['price'], 'fill_volume': '0',
                                           'time_done': int(self.now)})
                for log in self.logs.values():
                    if log['order_id'] == order_id:
                        log['state'] = 2
                return httpx.Response(200, json={})
        if path.startswith('/tradfi/positions/'):
            position_id = path.split('/')[3]
            assert position_id in self.positions, 'Only an existing position may be written'
            if method == 'PUT':
                assert set(body) == {'price_tp', 'price_sl'}
                assert all(isinstance(value, str) for value in body.values())
                self.positions[position_id].update(body)
                return self.response({})
            if method == 'POST' and path.endswith('/close'):
                assert body == {'close_type': 2}
                row = self.positions.pop(position_id)
                self.position_history.append({**row, 'volume_closed': row['volume'],
                                               'close_price': '107.80', 'time_close': int(self.now),
                                               'position_status': '1', 'realized_pnl': '0.12'})
                return self.response({})
        raise AssertionError(f'Unexpected intercepted write {method} {path}')

    def fill(self, order_id, price=None):
        row = self.orders.pop(order_id)
        fill_price = price or row['price']
        position_id = str(9007199254750000 + len(self.positions) + 1)
        position = {'position_id': position_id, 'symbol': row['symbol'],
                    'position_dir': 'Long' if row['side'] == 2 else 'Short',
                    'volume': row['volume'], 'price_open': fill_price,
                    'price_tp': row['price_tp'], 'price_sl': row['price_sl'],
                    'time_create': int(self.now), 'unrealized_pnl': '0.1', 'margin': '1.00'}
        self.positions[position_id] = position
        self.order_history.append({**row, 'state': 4, 'finished': 1, 'price': fill_price,
                                   'order_opt_type': row['side'], 'close_pnl': '0',
                                   'trigger_price': row['price'], 'fill_volume': row['volume'],
                                   'time_done': int(self.now)})
        for log in self.logs.values():
            if log['order_id'] == order_id:
                log['state'] = 4
                log['price'] = fill_price
        return position_id

    def add_external(self):
        self.orders['800001'] = {'order_id': '800001', 'symbol': 'XAUUSD', 'price_type': 'trigger',
                                 'side': 2, 'volume': '0.02', 'price': '99.00', 'price_tp': '110.00',
                                 'price_sl': '80.00', 'state': 1, 'finished': 0, 'time_setup': int(self.now) - 3600}
        self.positions['800002'] = {'position_id': '800002', 'symbol': 'XAUUSD', 'position_dir': 'Long',
                                    'volume': '0.02', 'price_open': '99.00', 'price_tp': '110.00',
                                    'price_sl': '80.00', 'time_create': int(self.now) - 3600,
                                    'unrealized_pnl': '1', 'margin': '2'}


@pytest.fixture
def native_session(tmp_path, monkeypatch):
    monkeypatch.setenv('GATE_API_KEY', '')
    monkeypatch.setenv('GATE_API_SECRET', '')
    exchange = NativeExchange()

    def intercepted_client(key='', secret=''):
        assert (key, secret) in {('', ''), (KEY, SECRET)}, 'Never accept non-test credentials'
        return GateClient(key, secret, transport=httpx.MockTransport(exchange.intercepted_transport),
                          clock=exchange.clock, min_request_interval=0)

    monkeypatch.setattr(main, 'GateClient', intercepted_client)
    monkeypatch.setattr(main, 'LiveEngine', lambda path: LiveEngine(path, clock=exchange.clock))
    monkeypatch.setattr(main, 'LiveMarket', lambda gate, interval: LiveMarket(gate, interval, clock=exchange.clock))
    monkeypatch.setattr(main.CredentialStore, 'load', lambda _: None)
    monkeypatch.setattr(main.CredentialStore, 'clear', lambda _: None)
    app = main.create_app(tmp_path / 'native.sqlite3', auto_tick=False)
    with TestClient(app) as session:
        result = session.post('/api/connection/check', headers=HEADERS,
                              json={'key': KEY, 'secret': SECRET, 'remember': False})
        assert result.status_code == 200, result.text
        assert result.json()['account_id'] == UID
        yield session, exchange


def start(session, config=None):
    result = session.post('/api/strategies', headers=HEADERS,
                          json={'config': config or CONFIG, 'request_id': 'native-integration-start-01'})
    assert result.status_code == 200, result.text
    assert result.json()['mode'] == 'live'
    return result.json()['strategies'][0]['id']


def cycle(session, exchange):
    async def one_cycle():
        exchange.now += 1
        state = session.app.state
        async with state.op_lock:
            await state.market.ensure('XAUUSD', detail=True)
            quote, spec = state.market.get('XAUUSD'), state.market.spec('XAUUSD')
            await state.engine.cycle(state.gate, {'XAUUSD': quote}, {'XAUUSD': spec}, UID)
            await state.engine.reconcile(state.gate, UID)
    session.portal.call(one_cycle)
    return session.get('/api/state').json()


def laid_grid(session, exchange, config=None):
    sid = start(session, config)
    assert exchange.writes == [], 'Start only persists; execution belongs to the worker'
    snapshot = None
    for _ in range(4):
        snapshot = cycle(session, exchange)
        if len([row for row in snapshot['orders'] if row.get('strategy_id') == sid
                and row.get('source') == 'gate' and row.get('order_id')]) == 2:
            break
    managed = [row for row in snapshot['orders'] if row.get('strategy_id') == sid
               and row.get('source') == 'gate' and row.get('order_id')]
    assert len(managed) == 2, snapshot
    assert {Decimal(row['price']) for row in managed} == {Decimal('100'), Decimal('105')}
    assert all(row['managed'] and row['status'] == 'pending' for row in managed)
    assert len([w for w in exchange.writes if w[:2] == ('POST', '/tradfi/orders')]) == 2
    return sid, managed


def test_native_create_modify_and_cancel_order_never_replenishes_cancelled_cell(native_session):
    session, exchange = native_session
    sid, orders = laid_grid(session, exchange)
    order_id = orders[0]['order_id']
    current = copy.deepcopy(exchange.orders[order_id])
    response = session.put(f'/api/strategies/{sid}/orders/{order_id}', headers=HEADERS,
                           json={'price': current['price'], 'price_tp': '109.00'})
    assert response.status_code == 200, response.text
    assert exchange.writes[-1] == ('PUT', f'/tradfi/orders/{order_id}',
                                   {'price': current['price'], 'price_tp': '109.00', 'price_sl': '90'})
    assert response.json()['mode'] == 'live'
    changed = next(row for row in response.json()['orders'] if row.get('order_id') == order_id)
    assert changed['take_profit'] == '109.00'
    cancelled = session.delete(f'/api/strategies/{sid}/orders/{order_id}', headers=HEADERS)
    assert cancelled.status_code == 200, cancelled.text
    writes_after_cancel = len(exchange.writes)
    for _ in range(3):
        snapshot = cycle(session, exchange)
    assert len(exchange.writes) == writes_after_cancel
    assert order_id not in exchange.orders
    assert all(row.get('order_id') != order_id for row in snapshot['orders'])
    assert len([row for row in snapshot['orders'] if row.get('strategy_id') == sid
                and row.get('source') == 'gate' and row.get('order_id')]) == 1


def test_unset_strategy_stop_is_omitted_on_create_and_preserved_as_zero_on_update(native_session):
    session, exchange = native_session
    sid, orders = laid_grid(session, exchange, {**CONFIG, 'stop_loss': None})
    opening_writes = [write for write in exchange.writes if write[:2] == ('POST', '/tradfi/orders')]
    assert all('price_sl' not in body for _, _, body in opening_writes)
    assert all(order['price_sl'] == '0' for order in exchange.orders.values())
    intents = session.app.state.engine._state_data['intents']
    assert all(intent['request']['price_sl'] == '0' for intent in intents)
    order_id = orders[0]['order_id']
    response = session.put(f'/api/strategies/{sid}/orders/{order_id}', headers=HEADERS,
                           json={'price': orders[0]['price'], 'price_tp': '109.00'})
    assert response.status_code == 200, response.text
    assert exchange.writes[-1] == ('PUT', f'/tradfi/orders/{order_id}',
                                   {'price': orders[0]['price'], 'price_tp': '109.00', 'price_sl': '0'})


def test_many_eligible_cells_are_submitted_in_bounded_batches_across_cycles(native_session):
    session, exchange = native_session
    sid = start(session, {**CONFIG, 'grid_count': 20})
    assert exchange.writes == []
    cycle(session, exchange)
    first_count = len(exchange.writes)
    assert 0 < first_count < 8, 'A cycle must not transmit the whole eligible grid at once'
    cycle(session, exchange)
    assert first_count < len(exchange.writes) <= 8
    for _ in range(8):
        if len(exchange.orders) == 8:
            break
        cycle(session, exchange)
    snapshot = session.get('/api/state').json()
    managed = [row for row in snapshot['orders'] if row.get('strategy_id') == sid
               and row.get('source') == 'gate' and row.get('order_id')]
    assert len(managed) == 8
    assert len(exchange.writes) == 8
    assert all(method == 'POST' and path == '/tradfi/orders' and body['price_type'] == 'trigger'
               for method, path, body in exchange.writes)


def test_http_cancel_signals_while_cycle_holds_lock_and_blocks_further_submissions(native_session, monkeypatch):
    session, exchange = native_session
    sid = start(session, {**CONFIG, 'grid_count': 20})
    exchange.block_create = True
    stop_seen = threading.Event()
    original_stop = session.app.state.engine.request_stop

    def request_stop(strategy_id, uid):
        original_stop(strategy_id, uid)
        stop_seen.set()

    monkeypatch.setattr(session.app.state.engine, 'request_stop', request_stop)
    with ThreadPoolExecutor(max_workers=2) as workers:
        active_cycle = workers.submit(cycle, session, exchange)
        cancellation = None
        try:
            assert exchange.create_started.wait(3), 'The first intercepted order never started'
            cancellation = workers.submit(session.post, f'/api/strategies/{sid}/cancel', headers=HEADERS)
            assert stop_seen.wait(3), 'HTTP cancel must signal stop before waiting on op_lock'
        finally:
            session.portal.call(exchange.release_create.set)
        active_cycle.result(timeout=10)
        response = cancellation.result(timeout=10)
    assert response.status_code == 200, response.text
    opening_writes = [write for write in exchange.writes if write[:2] == ('POST', '/tradfi/orders')]
    assert len(opening_writes) == 1, 'A stop during the first request must prevent the rest of the batch'
    assert not exchange.orders
    before = len(exchange.writes)
    cycle(session, exchange)
    assert len(exchange.writes) == before


def test_stopped_strategy_keeps_owned_position_and_can_modify_its_protection(native_session):
    session, exchange = native_session
    sid, orders = laid_grid(session, exchange)
    # A native order may wait minutes before filling at a slightly different
    # price. Attribution must use verified execution history, not submission age.
    exchange.now += 120
    fill_price = format(Decimal(orders[0]['price']) + Decimal('0.03'), 'f')
    position_id = exchange.fill(orders[0]['order_id'], price=fill_price)
    snapshot = cycle(session, exchange)
    owned = next(row for row in snapshot['positions'] if row['position_id'] == position_id)
    assert owned['managed'] and owned['strategy_id'] == sid
    assert owned['entry_price'] == fill_price
    stopped = session.post(f'/api/strategies/{sid}/cancel', headers=HEADERS)
    assert stopped.status_code == 200, stopped.text
    assert next(row for row in stopped.json()['strategies'] if row['id'] == sid)['status'] == 'stopped'
    assert position_id in exchange.positions
    response = session.put(f'/api/strategies/{sid}/positions/{position_id}', headers=HEADERS,
                           json={'price_tp': '115.00'})
    assert response.status_code == 200, response.text
    assert exchange.writes[-1] == ('PUT', f'/tradfi/positions/{position_id}',
                                   {'price_tp': '115.00', 'price_sl': '90'})
    cleared = session.put(f'/api/strategies/{sid}/positions/{position_id}', headers=HEADERS,
                          json={'price_sl': '0'})
    assert cleared.status_code == 200, cleared.text
    assert exchange.writes[-1] == ('PUT', f'/tradfi/positions/{position_id}',
                                   {'price_tp': '115.00', 'price_sl': '0'})


def test_unknown_and_external_objects_cannot_be_managed_by_a_strategy(native_session):
    session, exchange = native_session
    exchange.add_external()
    sid, _ = laid_grid(session, exchange)
    before = len(exchange.writes)
    for kind, object_id, body in [('orders', '800001', {'price': '99.00', 'price_tp': '109.00'}),
                                  ('orders', '999999', {'price': '99.00'}),
                                  ('positions', '800002', {'price_tp': '115.00'}),
                                  ('positions', '999999', {'price_sl': '0'})]:
        response = session.put(f'/api/strategies/{sid}/{kind}/{object_id}', headers=HEADERS, json=body)
        assert response.status_code in {400, 404, 409}, response.text
        assert len(exchange.writes) == before
    response = session.delete(f'/api/strategies/{sid}/orders/800001', headers=HEADERS)
    assert response.status_code in {400, 404, 409}, response.text
    assert len(exchange.writes) == before
    assert exchange.orders['800001']['price_tp'] == '110.00'
    assert exchange.positions['800002']['price_tp'] == '110.00'


def test_unverified_queue_candidate_never_authorizes_order_management(native_session):
    session, exchange = native_session
    sid = start(session)
    exchange.mismatch_log_id = True
    snapshot = cycle(session, exchange)
    assert len(exchange.orders) == 1
    order_id = next(iter(exchange.orders))
    row = next(row for row in snapshot['orders'] if row.get('order_id') == order_id)
    assert not row['managed'], 'A mismatched log must not establish ownership'
    before = len(exchange.writes)
    response = session.put(f'/api/strategies/{sid}/orders/{order_id}', headers=HEADERS,
                           json={'price': exchange.orders[order_id]['price'], 'price_tp': '109.00'})
    assert response.status_code in {400, 404, 409}, response.text
    assert len(exchange.writes) == before
    for _ in range(2):
        cycle(session, exchange)
    assert len(exchange.writes) == before, 'Unverified submission must not be replayed'


def test_close_strategy_cancels_only_its_orders_and_closes_only_its_positions(native_session):
    session, exchange = native_session
    exchange.add_external()
    sid, orders = laid_grid(session, exchange)
    position_id = exchange.fill(orders[0]['order_id'])
    snapshot = cycle(session, exchange)
    assert next(row for row in snapshot['positions'] if row['position_id'] == position_id)['managed']
    before = len(exchange.writes)
    response = session.post(f'/api/strategies/{sid}/close', headers=HEADERS)
    assert response.status_code == 200, response.text
    for _ in range(3):
        snapshot = cycle(session, exchange)
    writes = exchange.writes[before:]
    assert {write[:2] for write in writes} == {
        ('DELETE', f"/tradfi/orders/{orders[1]['order_id']}"),
        ('POST', f'/tradfi/positions/{position_id}/close'),
    }
    assert len(writes) == 2
    assert set(exchange.orders) == {'800001'}
    assert set(exchange.positions) == {'800002'}
    assert all(not row['managed'] for row in snapshot['positions'])
    assert next(row for row in snapshot['strategies'] if row['id'] == sid)['status'] == 'stopped'


@pytest.mark.parametrize('kind', ['orders', 'positions'])
@pytest.mark.parametrize('upstream_status', [400, 503])
def test_rejected_or_unknown_modify_is_never_reported_as_confirmed_or_blindly_repeated(
        native_session, kind, upstream_status):
    session, exchange = native_session
    sid, orders = laid_grid(session, exchange)
    if kind == 'orders':
        object_id = orders[0]['order_id']
        remote_rows = exchange.orders
        body = {'price': remote_rows[object_id]['price'], 'price_tp': '109.00'}
    else:
        object_id = exchange.fill(orders[0]['order_id'])
        cycle(session, exchange)
        remote_rows = exchange.positions
        body = {'price_tp': '115.00'}
    previous = copy.deepcopy(remote_rows[object_id])
    exchange.fail_next_update = upstream_status
    url = f'/api/strategies/{sid}/{kind}/{object_id}'
    response = session.put(url, headers=HEADERS, json=body)
    if upstream_status == 400:
        assert response.status_code == 502, response.text
    else:
        assert response.status_code == 200, response.text
        assert response.json()['operation']['status'] in {'submitted', 'unknown'}, response.text
        # The uncertain request may already have reached Gate. Repeating the
        # same UI action must not turn into a second upstream modification.
        repeated = session.put(url, headers=HEADERS, json=body)
        if repeated.status_code == 200:
            assert repeated.json()['operation']['status'] in {'submitted', 'unknown'}
        else:
            assert repeated.status_code in {400, 409, 502}, repeated.text
    for _ in range(2):
        cycle(session, exchange)
    writes = [write for write in exchange.writes if write[:2] == ('PUT', f'/tradfi/{kind}/{object_id}')]
    assert len(writes) == 1
    assert remote_rows[object_id] == previous
    snapshot = session.get('/api/state').json()
    row = next(row for row in snapshot[kind] if row.get('order_id' if kind == 'orders' else 'position_id') == object_id)
    assert row['take_profit'] == previous['price_tp']
