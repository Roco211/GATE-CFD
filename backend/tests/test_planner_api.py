"""Planner routes run only against the existing fully intercepted test exchange."""
import copy
import time
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from test_native_api import HEADERS, UID, native_session


PLAN = {'mode': 'evaluate', 'symbol': 'XAUUSD', 'direction': 'long',
        'spacing': 'arithmetic', 'lower_price': '100', 'upper_price': '120',
        'grid_count': 4, 'volume': '0.01',
        'costs': {'spread_budget': '0.20', 'slippage_budget': '0.10',
                  'round_trip_fee_per_lot': '5.4', 'minimum_fee': '0', 'fee_source': 'gate'}}


def no_execution(monkeypatch, session):
    engine = session.app.state.engine
    for name in ('start', 'cycle', 'reconcile', 'refresh_preview_account', 'modify_order', 'modify_position', 'cancel', 'close_strategy'):
        monkeypatch.setattr(engine, name, AsyncMock(side_effect=AssertionError(f'Planner invoked {name}')))


def commission_mock(monkeypatch, session, rows):
    reader = AsyncMock(return_value={'data': {'list': rows}, 'timestamp': 1_800_000_000_000})
    monkeypatch.setattr(session.app.state.gate, 'commissions', reader)
    return reader


def test_plan_reads_real_schema_spec_and_returns_only_a_draft(native_session, monkeypatch):
    session, exchange = native_session
    no_execution(monkeypatch, session)
    before = session.app.state.engine.state('XAUUSD', UID)['strategies']
    exchange.calls.clear()
    response = session.post('/api/grid/plan', headers=HEADERS, json=PLAN)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['feasible'] is True
    assert result['config']['symbol'] == 'XAUUSD'
    assert result['config']['grid_count'] == 4
    assert len(result['levels']) == 5
    assert Decimal(result['cells'][0]['net_profit']) == Decimal('4.646')
    assert Decimal(result['cells'][0]['fee']) == Decimal('0.054'), 'Opening commission must not be doubled or discounted'
    assert result['requires_gate_volume_validation'] is True
    assert session.app.state.engine.state('XAUUSD', UID)['strategies'] == before
    assert any(path == '/tradfi/symbols/detail' for _, path, _ in exchange.calls)
    assert all(method == 'GET' for method, _, _ in exchange.calls)
    assert not exchange.writes


def test_plan_auto_spread_uses_the_requested_real_quote(native_session):
    session, exchange = native_session
    exchange.now = time.time()
    body = copy.deepcopy(PLAN)
    body['costs']['spread_budget'] = None
    response = session.post('/api/grid/plan', headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['feasible'] is True
    assert Decimal(result['costs']['spread_budget']) == Decimal('0.20')
    assert not exchange.writes


@pytest.mark.parametrize('change', [{'grid_count': 1}, {'volume': '-1'}, {'mode': 'start'}, {'start': True}, {'symbol': '../orders'}])
def test_invalid_plan_cannot_read_or_write_exchange(native_session, change):
    session, exchange = native_session
    exchange.calls.clear()
    response = session.post('/api/grid/plan', headers=HEADERS, json={**copy.deepcopy(PLAN), **change})
    assert response.status_code in (400, 422), response.text
    assert not exchange.calls
    assert not exchange.writes


def test_plan_requires_explicit_minimum_fee_and_local_request_header(native_session):
    session, exchange = native_session
    body = copy.deepcopy(PLAN)
    body['costs']['minimum_fee'] = None
    assert session.post('/api/grid/plan', headers=HEADERS, json=body).status_code == 422
    assert session.post('/api/grid/plan', json=PLAN).status_code == 403
    assert not exchange.writes


def test_plan_does_not_reuse_another_symbols_cached_spec(native_session):
    session, exchange = native_session
    assert session.post('/api/grid/plan', headers=HEADERS, json=PLAN).status_code == 200
    response = session.post('/api/grid/plan', headers=HEADERS, json={**PLAN, 'symbol': 'EURUSD'})
    assert response.status_code == 409, response.text
    assert 'detail' not in response.json() or isinstance(response.json()['detail'], str)
    assert not exchange.writes


def test_commission_preserves_unique_exact_symbol_rate_without_vip_or_round_trip_math(native_session, monkeypatch):
    session, exchange = native_session
    no_execution(monkeypatch, session)
    reader = commission_mock(monkeypatch, session, [
        {'symbol': 'EURUSD', 'category_code': 'forex', 'fee_per_lot': '60'},
        {'symbol': 'XAUUSD', 'category_code': 'metal', 'fee_per_lot': '5.4', 'minimum_fee': '99', 'vip_discount': '0.9'},
    ])
    result = session.get('/api/grid/costs?symbol=xauusd').json()
    assert result['symbol'] == 'XAUUSD'
    assert result['fee_per_lot'] == '5.4'
    assert result['category_code'] == 'metal'
    assert result['minimum_fee'] is None
    assert result['source'] == 'gate'
    assert abs(result['checked_at'] - time.time()) < 5
    assert any('最低收费' in note for note in result['notes'])
    reader.assert_awaited_once_with(symbols='XAUUSD')
    assert not exchange.writes


@pytest.mark.parametrize('rows', [[], [{'symbol': 'EURUSD', 'fee_per_lot': '6'}],
    [{'symbol': 'xauusd', 'fee_per_lot': '6'}],
    [{'symbol': 'XAUUSD', 'fee_per_lot': '6'}, {'symbol': 'XAUUSD', 'fee_per_lot': '6'}],
    {'symbol': 'XAUUSD', 'fee_per_lot': '6'}])
def test_missing_duplicate_or_wrong_symbol_fee_is_unknown(native_session, monkeypatch, rows):
    session, exchange = native_session
    commission_mock(monkeypatch, session, rows)
    response = session.get('/api/grid/costs?symbol=XAUUSD')
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['fee_per_lot'] is None and result['minimum_fee'] is None
    assert result['category_code'] is None
    assert '唯一费率' in result['notes'][0]
    assert not exchange.writes


@pytest.mark.parametrize('fee', [None, '', 'NaN', 'Infinity', '-1', True, {}, '1e999'])
def test_invalid_fee_never_silently_becomes_zero(native_session, monkeypatch, fee):
    session, _ = native_session
    commission_mock(monkeypatch, session, [{'symbol': 'XAUUSD', 'category_code': 'metal', 'fee_per_lot': fee}])
    result = session.get('/api/grid/costs?symbol=XAUUSD').json()
    assert result['fee_per_lot'] is None
    assert result['category_code'] == 'metal'
    assert '不能视为 0' in result['notes'][0]


def test_explicit_zero_fee_is_distinct_from_missing_fee(native_session, monkeypatch):
    session, _ = native_session
    commission_mock(monkeypatch, session, [{'symbol': 'XAUUSD', 'fee_per_lot': '0'}])
    result = session.get('/api/grid/costs?symbol=XAUUSD').json()
    assert result['fee_per_lot'] == '0' and result['minimum_fee'] is None


def test_invalid_cost_symbol_is_rejected_before_lookup(native_session, monkeypatch):
    session, _ = native_session
    reader = commission_mock(monkeypatch, session, [])
    assert session.get('/api/grid/costs?symbol=../../orders').status_code == 400
    reader.assert_not_awaited()


def test_unauthenticated_planning_and_costs_fail_without_exchange_access(native_session):
    session, exchange = native_session
    # This only clears the fixture's synthetic credentials and temporary store.
    assert session.post('/api/connection/disconnect', headers=HEADERS).status_code == 200
    exchange.calls.clear()
    for response in [session.get('/api/grid/costs?symbol=XAUUSD'), session.post('/api/grid/plan', headers=HEADERS, json=PLAN)]:
        assert response.status_code == 409, response.text
        assert '连接 Gate 账户' in response.json()['detail']
    assert not exchange.calls and not exchange.writes


def test_fee_transport_is_signed_get_with_exact_filter_and_surfaces_gate_failure(native_session, monkeypatch):
    session, exchange = native_session
    original = exchange.transport
    queries = []
    def transport(request):
        if request.url.path.endswith('/symbols/commissions'):
            queries.append(dict(request.url.params))
            assert request.method == 'GET'
            assert 'KEY' in request.headers and 'SIGN' in request.headers
            return httpx.Response(403, json={'label': 'FORBIDDEN', 'message': 'Fee permission unavailable'})
        return original(request)
    monkeypatch.setattr(exchange, 'transport', transport)
    response = session.get('/api/grid/costs?symbol=XAUUSD')
    assert response.status_code == 502, response.text
    assert response.json()['source'] == 'gate'
    assert 'detail' in response.json()
    assert queries == [{'symbols': 'XAUUSD'}]
    assert not exchange.writes
