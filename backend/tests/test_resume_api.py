"""Resume HTTP flow on an intercepted exchange; no real credentials or network."""
from decimal import Decimal

from test_native_api import HEADERS, UID, native_session, laid_grid, cycle


def test_resume_rechecks_old_tp_pause_and_rearms_only_once(native_session):
    session, exchange = native_session
    sid, orders = laid_grid(session, exchange)
    order = min(orders, key=lambda item: Decimal(item['price']))
    position_id = exchange.fill(order['id'])
    cycle(session, exchange)
    row = exchange.positions.pop(position_id)
    price = Decimal(row['price_tp']) - Decimal('0.07')
    gross = (price - Decimal(row['price_open'])) * Decimal(row['volume']) * 100
    exchange.position_history.append({**row, 'volume_closed': row['volume'],
        'close_price': str(price), 'time_close': int(exchange.now), 'position_status': '1',
        'realized_pnl': str(gross - Decimal('0.05')), 'realized_pnl_detail': {'closed_pnl': str(gross)}})
    exchange.order_history.append({'order_id': '980001', 'symbol': row['symbol'],
        'price_type': 'market', 'order_opt_type': 3, 'side': 1, 'state': 4,
        'volume': row['volume'], 'fill_volume': row['volume'], 'price': str(price),
        'trigger_price': row['price_tp'], 'sl_tp_type': 2, 'close_pnl': str(gross),
        'time_setup': int(exchange.now), 'time_done': int(exchange.now)})

    engine = session.app.state.engine
    intent = next(i for i in engine._intents(UID) if position_id in i.get('position_ids', []))
    strategy = engine._strategy(sid, UID)
    cell = strategy['cells'][intent['grid_index']]
    # Reproduce the persisted state produced by the previous strict-price check.
    intent.update(status='closed', exit_type='closed', accounted_position_ids=[position_id])
    cell.update(status='done', completed_cycles=1)
    strategy.update(status='paused', completed_cycles=1,
                    realized_pnl=str(gross - Decimal('0.05')),
                    error='策略仓位发生强平或非目标价退出，已暂停新铺单，请核对仓位。')
    engine._persist()
    before_writes = len(exchange.writes)
    before_generation = cell['generation']
    state = cycle(session, exchange)
    assert state['selected_strategy']['status'] == 'paused'
    assert state['selected_strategy']['recoverable'] is True
    assert len(exchange.writes) == before_writes
    fill = next(fill for fill in state['fills'] if fill['position_id'] == position_id)
    assert fill['type'] == 'target_exit' and fill['exit_order_id'] == '980001'

    response = session.post(f'/api/strategies/{sid}/resume', headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()['selected_strategy']['status'] in {'running', 'starting'}
    assert cell['generation'] == before_generation + 1
    assert cell['intent_id'] is None
    assert strategy['completed_cycles'] == cell['completed_cycles'] == 1
    assert strategy['realized_pnl'] == str(gross - Decimal('0.05'))
    assert len(exchange.writes) == before_writes, 'Resume does not send exchange orders itself'

    duplicate = session.post(f'/api/strategies/{sid}/resume', headers=HEADERS)
    assert duplicate.status_code in {200, 400}, duplicate.text
    assert cell['generation'] == before_generation + 1
    assert len(exchange.writes) == before_writes
    for _ in range(3):
        state = cycle(session, exchange)
    creates = [write for write in exchange.writes[before_writes:] if write[:2] == ('POST', '/tradfi/orders')]
    assert len(creates) == 1
    assert creates[0][2]['price'] == order['price']
    assert len([r for r in state['orders'] if r.get('source') == 'gate' and r.get('price') == order['price']]) == 1


def test_resume_rejects_stopped_unknown_or_unauthorized_requests(native_session):
    session, exchange = native_session
    sid, _ = laid_grid(session, exchange)
    assert session.post(f'/api/strategies/{sid}/cancel', headers=HEADERS).status_code == 200
    before = list(exchange.writes)
    for route, headers in ((sid, HEADERS), ('missing-strategy', HEADERS), (sid, {})):
        response = session.post(f'/api/strategies/{route}/resume', headers=headers)
        assert response.status_code >= 400, response.text
    assert exchange.writes == before
