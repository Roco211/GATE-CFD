"""Native execution tests: temporary databases and injected fakes, no network."""
import asyncio
import copy
import inspect
import tempfile
import unittest
from pathlib import Path

from app.core import GridConfig
from app.gate import GateError, GateUnknownOutcome
from app.live import LiveEngine, _list


class Clock:
    value = 1_800_000_000.0
    def __call__(self):
        return self.value


SPEC = {'symbol': 'XAUUSD', 'name': 'Gold', 'tick_size': '0.01', 'price_precision': 2,
        'volume_min': '0.01', 'volume_max': '100', 'volume_step': None, 'contract_size': '100',
        'leverage': '100', 'settlement_currency': 'USD', 'currency': 'USD', 'source': 'gate',
        'complete': True, 'status': 'open', 'trade_mode': '4'}


def cfg(**changes):
    return GridConfig.model_validate({'symbol': 'XAUUSD', 'lower_price': '100', 'upper_price': '120',
                                     'volume': '0.01', 'grid_count': 4, 'direction': 'long',
                                     'repeat': True, 'stop_loss': '90', **changes})


def market(clock, ask='108', bid='107.8', **changes):
    return {'symbol': 'XAUUSD', 'last': bid, 'ask': ask, 'bid': bid, 'received_at': clock(),
            'source': 'gate', 'stale': False, 'trading_status': 'open', 'trade_mode': '4', 'candles': [], **changes}


class FakeGate:
    credentials_set = True
    uid = '70001'
    free_margin = '100000'

    def __init__(self, clock):
        self.clock = clock
        self.current_positions, self.current_orders, self.closed_positions, self.order_histories = [], [], [], []
        self.logs, self.writes, self.cancel_writes, self.close_writes, self.modifications = {}, [], [], [], []
        self.behavior, self.cancel_behavior, self.modify_behavior, self.close_behavior = 'pending', 'ok', 'ok', 'ok'
        self.block_preflight, self.block_create = None, None
        self.preflight_started, self.create_started = asyncio.Event(), asyncio.Event()
        self.log_reads = 0

    def envelope(self, body):
        return {'data': copy.deepcopy(body), 'timestamp': int(self.clock() * 1000)}

    async def account(self):
        return self.envelope({'mt5_uid': self.uid, 'status': 3, 'leverage': 100})

    async def assets(self):
        return self.envelope({'mt5_uid': self.uid, 'margin_free': self.free_margin, 'balance': '100000',
                              'equity': '100000', 'margin': '0', 'unrealized_pnl': '0'})

    async def positions(self):
        return self.envelope({'list': self.current_positions})

    async def orders(self):
        return self.envelope({'list': self.current_orders})

    async def position_history(self, page=1, page_size=100):
        return self.envelope({'list': self.closed_positions if page == 1 else [], 'total_page': 1})

    async def order_history(self, **filters):
        rows = self.order_histories
        if filters:
            rows = [row for row in rows if row.get('symbol') == filters['symbol']
                    and str(row.get('side')) == str(filters['side'])
                    and filters['begin_time'] <= int(row.get('time_setup', 0)) <= filters['end_time']]
        return self.envelope({'list': rows})

    async def order_log(self, log_id):
        self.log_reads += 1
        if log_id not in self.logs:
            raise GateError('ORDER_NOT_FOUND', '测试日志尚未返回')
        return self.envelope(self.logs[log_id])

    async def guard(self, callback):
        if callback:
            result = callback()
            if inspect.isawaitable(result):
                await result

    async def create_order(self, **request):
        callback = request.pop('before_send', None)
        self.preflight_started.set()
        if self.block_preflight:
            await self.block_preflight.wait()
        await self.guard(callback)
        self.writes.append(copy.deepcopy(request))
        self.create_started.set()
        if self.block_create:
            await self.block_create.wait()
        if self.behavior == 'reject':
            raise GateError('BALANCE_NOT_ENOUGH', '交易所拒绝本次下单')
        if self.behavior == 'unknown':
            raise GateUnknownOutcome({'method': 'POST', 'path': '/api/v4/tradfi/orders'})
        n = len(self.writes)
        queue, oid = str(100 + n), str(200 + n)
        row = {**request, 'order_id': oid, 'state': 1, 'finished': 0, 'time_setup': int(self.clock())}
        self.current_orders.append(row)
        self.logs[queue] = {k: row[k] for k in ('order_id', 'symbol', 'price_type', 'side', 'volume', 'state')}
        self.logs[queue].update(log_id=queue, price='0')
        if self.behavior == 'bad_log':
            self.logs[queue]['log_id'] = '555555'
        if self.behavior == 'instant_close':
            pid = self.fill(oid)
            self.finish_position(pid, request['price_tp'])
        return self.envelope({'id': queue})

    def fill(self, oid='201', *, price=None, volume=None, partial=False):
        row = next(r for r in self.current_orders if r['order_id'] == oid)
        pid = str(300 + int(oid))
        volume, price = volume or row['volume'], price or row['price']
        execution = {**row, 'trigger_price': row['price'], 'price': price, 'fill_volume': volume,
                     'time_done': int(self.clock()), 'state': 3 if partial else 4, 'finished': 0 if partial else 1}
        if partial:
            row.update(state=3, fill_volume=volume, time_done=int(self.clock()))
        else:
            self.current_orders.remove(row)
            self.order_histories.append(execution)
        self.current_positions.append({'position_id': pid, 'symbol': row['symbol'],
                                       'position_dir': 'Long' if row['side'] == 2 else 'Short',
                                       'volume': volume, 'price_open': price, 'price_tp': row['price_tp'],
                                       'price_sl': row['price_sl'], 'time_create': int(self.clock()),
                                       'unrealized_pnl': '-0.2', 'margin': '1.05'})
        for log in self.logs.values():
            if log['order_id'] == oid:
                log.update(state=execution['state'], price=price)
        return pid

    def finish_position(self, pid='501', price='105', status='1', sl_tp_type=2):
        row = next(p for p in self.current_positions if p['position_id'] == pid)
        self.current_positions.remove(row)
        self.closed_positions.append({**row, 'volume_closed': row['volume'], 'close_price': price,
                                      'time_close': int(self.clock()), 'position_status': status, 'realized_pnl': '5',
                                      'realized_pnl_detail': {'closed_pnl': '5', 'fee': '0', 'swap': '0'}})
        self.order_histories.append({'order_id': str(90000 + len(self.closed_positions)), 'symbol': row['symbol'],
                                     'price_type': 'market', 'order_opt_type': 3 if row['position_dir'] == 'Long' else 4,
                                     'side': 1 if row['position_dir'] == 'Long' else 2, 'state': 4,
                                     'volume': row['volume'], 'fill_volume': row['volume'], 'price': price,
                                     'trigger_price': row['price_tp'] if sl_tp_type == 2 else price,
                                     'price_tp': '0', 'price_sl': '0', 'sl_tp_type': sl_tp_type,
                                     'time_setup': int(self.clock()), 'time_done': int(self.clock()), 'close_pnl': '5'})

    async def cancel_order(self, oid, *, before_send=None):
        await self.guard(before_send)
        self.cancel_writes.append(oid)
        if self.cancel_behavior == 'unknown':
            raise GateUnknownOutcome({'method': 'DELETE', 'path': '/api/v4/tradfi/orders/' + oid})
        if self.cancel_behavior == 'fill':
            self.fill(oid)
            raise GateUnknownOutcome({'method': 'DELETE', 'path': '/api/v4/tradfi/orders/' + oid})
        row = next(r for r in self.current_orders if r['order_id'] == oid)
        self.current_orders.remove(row)
        self.order_histories.append({**row, 'trigger_price': row['price'], 'fill_volume': row.get('fill_volume', '0'),
                                     'time_done': int(self.clock()), 'state': 2, 'finished': 1})
        return {}

    async def update_order(self, oid, *, price, price_tp, price_sl, before_send=None):
        await self.guard(before_send)
        self.modifications.append(('order', oid, dict(price=price, price_tp=price_tp, price_sl=price_sl)))
        if self.modify_behavior == 'unknown':
            raise GateUnknownOutcome({'method': 'PUT', 'path': '/api/v4/tradfi/orders/' + oid})
        next(r for r in self.current_orders if r['order_id'] == oid).update(price=price, price_tp=price_tp, price_sl=price_sl)
        if self.modify_behavior == 'instant_fill':
            self.fill(oid)
        return {}

    async def update_position(self, pid, *, price_tp, price_sl, before_send=None):
        await self.guard(before_send)
        self.modifications.append(('position', pid, dict(price_tp=price_tp, price_sl=price_sl)))
        row = next(r for r in self.current_positions if r['position_id'] == pid)
        if self.modify_behavior == 'close_before_apply':
            self.finish_position(pid, row['price_tp'])
        else:
            row.update(price_tp=price_tp, price_sl=price_sl)
        return {}

    async def close_position(self, pid, *, before_send=None):
        await self.guard(before_send)
        self.close_writes.append(pid)
        if self.close_behavior == 'unknown':
            raise GateUnknownOutcome({'method': 'POST', 'path': '/api/v4/tradfi/positions/' + pid + '/close'})
        self.finish_position(pid, sl_tp_type=0)
        return {}


class LiveEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'live.sqlite3'
        self.clock = Clock()
        self.engine = LiveEngine(self.path, clock=self.clock)
        self.gate = FakeGate(self.clock)

    async def asyncTearDown(self):
        self.engine.close()
        self.temp.cleanup()

    async def start(self, configuration=None, request_id='start-001'):
        result = await self.engine.start(configuration or cfg(), request_id, self.gate, market(self.clock), SPEC, self.gate.uid)
        self.sid = result['strategy_id']
        return result

    async def cycle(self, quote=None):
        self.clock.value += 1
        await self.engine.cycle(self.gate, {'XAUUSD': quote or market(self.clock)}, {'XAUUSD': SPEC}, self.gate.uid)
        return self.engine.state('XAUUSD', self.gate.uid)

    async def reconcile(self):
        return await self.engine.reconcile(self.gate, self.gate.uid)

    async def restart(self):
        self.engine.close()
        self.engine = LiveEngine(self.path, clock=self.clock)
        return await self.reconcile()

    async def placed(self):
        await self.start()
        return await self.cycle()

    async def opened(self):
        await self.placed()
        self.gate.fill()
        return await self.reconcile()

    async def test_start_returns_plan_then_native_orders_wait_hours_without_log_polling(self):
        state = await self.start()
        self.assertEqual(state['selected_strategy']['status'], 'starting')
        self.assertEqual(self.gate.writes, [])
        state = await self.cycle()
        self.assertEqual([r['price'] for r in self.gate.writes], ['100', '105.00'])
        self.assertTrue(all(r['price_type'] == 'trigger' and r['side'] == 2 and r['price_sl'] == '90' for r in self.gate.writes))
        self.assertEqual(state['selected_strategy']['pending_count'], 2)
        self.assertFalse(state['has_unresolved_execution'])
        reads = self.gate.log_reads
        self.clock.value += 7200
        state = await self.cycle()
        self.assertEqual(state['selected_strategy']['status'], 'running')
        self.assertEqual(self.gate.log_reads, reads)
        self.assertEqual(len(self.gate.writes), 2)

    async def test_explicit_empty_null_page_is_supported_for_real_account_snapshot(self):
        async def no_positions():
            return self.gate.envelope({'list': None, 'total': '0', 'total_page': 0})
        self.gate.positions = no_positions
        state = await self.reconcile()
        self.assertEqual(state['positions'], [])
        self.assertEqual(state['account']['account_id'], self.gate.uid)
        self.assertEqual(_list({'data': {'list': None, 'total': 0}}), [])

    def test_null_page_requires_explicit_finite_zero_and_rejects_missing_or_conflicting_data(self):
        invalid = [{}, {'total': 0}, {'list': None}, {'list': None, 'total': 1},
                   {'list': None, 'total': -1}, {'list': None, 'total': True},
                   {'list': None, 'total': 'NaN'}, {'list': None, 'total': ''},
                   {'list': None, 'total': None}, {'list': None, 'total': 'zero'}]
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ValueError):
                _list({'data': body})

    async def test_short_places_above_bid_and_waiting_becomes_pending_without_crossing(self):
        await self.start(cfg(direction='short', stop_loss='130'))
        await self.cycle()
        self.assertEqual([r['price'] for r in self.gate.writes], ['110.00', '115.00', '120'])
        self.assertTrue(all(r['side'] == 1 for r in self.gate.writes))
        await self.cycle(market(self.clock, '104.2', '104'))
        self.assertEqual(self.gate.writes[-1]['price'], '105.00')

    async def test_idempotence_account_binding_and_wrong_owner_rejection(self):
        first, second = await self.start(), await self.start()
        self.assertTrue(second['idempotent_replay'])
        self.assertEqual(first['strategy_id'], second['strategy_id'])
        with self.assertRaisesRegex(ValueError, '不同参数'):
            await self.start(cfg(grid_count=5))
        with self.assertRaises(ValueError):
            self.engine.request_stop(self.sid, '80002')
        self.gate.uid = '80002'
        self.engine._snapshots.clear()
        with self.assertRaisesRegex(ValueError, '不一致'):
            await self.engine.cycle(self.gate, {'XAUUSD': market(self.clock)}, {'XAUUSD': SPEC}, '70001')
        self.assertEqual(self.engine.state('XAUUSD', '80002')['strategies'], [])
        self.assertEqual(self.gate.writes, [])

    async def test_unknown_submission_persists_and_never_retries_after_restart(self):
        await self.start()
        self.gate.behavior = 'unknown'
        await self.cycle()
        await self.restart()
        for _ in range(3):
            await self.cycle()
        self.assertEqual(len(self.gate.writes), 1)
        self.assertTrue(self.engine.has_unresolved_execution())

    async def test_restart_adopts_simultaneous_fills_using_execution_time_and_slippage(self):
        await self.placed()
        self.clock.value += 7200
        first, second = self.gate.fill('201', price='100.03'), self.gate.fill('202', price='105.02')
        state = await self.restart()
        self.assertTrue(all(p['managed'] for p in state['positions']))
        self.assertEqual({p['id'] for p in state['positions']}, {first, second})
        self.assertFalse(state['has_unresolved_execution'])
        await self.cycle()
        self.assertEqual(len(self.gate.writes), 2)

    async def test_pending_never_claims_identical_manual_position(self):
        await self.placed()
        self.gate.current_positions.append({'position_id': '999001', 'symbol': 'XAUUSD', 'position_dir': 'Long',
                                            'volume': '0.01', 'price_open': '100', 'price_tp': '105', 'price_sl': '90',
                                            'time_create': int(self.clock()), 'unrealized_pnl': '0'})
        state = await self.reconcile()
        self.assertFalse(state['positions'][0]['managed'])
        self.assertEqual(state['selected_strategy']['pending_count'], 2)

    async def test_ambiguous_positions_pause_without_guessing(self):
        await self.placed()
        self.gate.fill()
        self.gate.current_positions.append({**self.gate.current_positions[0], 'position_id': '999001'})
        state = await self.reconcile()
        self.assertFalse(any(p['managed'] for p in state['positions']))
        self.assertEqual(state['selected_strategy']['status'], 'paused')
        self.assertTrue(state['has_unresolved_execution'])

    async def test_target_close_replenishes_next_generation_exactly_once(self):
        await self.opened()
        self.gate.finish_position()
        state = await self.reconcile()
        self.assertEqual(state['selected_strategy']['cells'][0]['generation'], 1)
        self.assertEqual(state['account']['realized_pnl'], '5.00')
        await self.reconcile()
        await self.cycle()
        self.assertEqual(len(self.gate.writes), 3)
        self.assertEqual(self.engine.state()['selected_strategy']['completed_cycles'], 1)

    async def test_history_captures_instant_open_close_without_duplicate_pnl(self):
        await self.start(cfg(repeat=False))
        self.gate.behavior = 'instant_close'
        state = await self.cycle()
        self.assertEqual(state['positions'], [])
        self.assertEqual(len(state['fills']), 2)
        self.assertTrue(all(f['managed'] for f in state['fills']))
        self.assertEqual(state['account']['realized_pnl'], '10.00')
        state = await self.reconcile()
        self.assertEqual(state['account']['realized_pnl'], '10.00')

    async def test_liquidation_at_target_never_repeats(self):
        await self.opened()
        self.gate.finish_position(status='2')
        state = await self.reconcile()
        self.assertEqual(state['fills'][0]['type'], 'liquidation')
        self.assertEqual(state['selected_strategy']['status'], 'paused')
        await self.cycle()
        self.assertEqual(len(self.gate.writes), 2)

    async def test_cancel_preserves_positions_and_external_orders(self):
        await self.opened()
        self.gate.current_orders.append({'order_id': '999', 'symbol': 'XAUUSD', 'side': 2, 'volume': '0.01', 'price': '95', 'state': 1})
        state = await self.engine.cancel(self.sid, self.gate, self.gate.uid)
        self.assertEqual(self.gate.cancel_writes, ['202'])
        self.assertEqual(state['remaining_positions'], 1)
        self.assertEqual(state['selected_strategy']['status'], 'stopped')
        self.assertEqual(state['positions'][0]['take_profit'], '105.00')
        self.gate.finish_position()
        await self.cycle()
        self.assertEqual(len(self.gate.writes), 2)

    async def test_unknown_cancel_stays_stopping_after_restart_without_retry(self):
        await self.placed()
        self.gate.cancel_behavior = 'unknown'
        state = await self.engine.cancel(self.sid, self.gate, self.gate.uid)
        self.assertEqual(state['selected_strategy']['status'], 'stopping')
        self.assertTrue(state['unresolved'])
        await self.restart()
        await self.cycle()
        self.assertEqual(self.gate.cancel_writes, ['201', '202'])

    async def test_cancel_fill_race_retains_actual_positions(self):
        await self.placed()
        self.gate.cancel_behavior = 'fill'
        state = await self.engine.cancel(self.sid, self.gate, self.gate.uid)
        self.assertEqual(state['selected_strategy']['status'], 'stopped')
        self.assertEqual(state['remaining_positions'], 2)
        self.assertFalse(state['unresolved'])
        self.assertTrue(all(p['managed'] for p in state['positions']))

    async def test_definite_notfound_after_fill_is_resolved_without_cancel_retry(self):
        await self.placed()
        async def cancel_notfound(oid, *, before_send=None):
            await self.gate.guard(before_send)
            self.gate.cancel_writes.append(oid)
            self.gate.fill(oid)
            raise GateError('ORDER_NOT_FOUND', '委托已成交')
        self.gate.cancel_order = cancel_notfound
        state = await self.engine.cancel(self.sid, self.gate, self.gate.uid)
        self.assertEqual(state['selected_strategy']['status'], 'stopped')
        self.assertEqual(state['remaining_positions'], 2)
        await self.engine.cancel(self.sid, self.gate, self.gate.uid)
        self.assertEqual(self.gate.cancel_writes, ['201', '202'])

    async def test_definite_notfound_after_close_is_resolved_from_exact_position_history(self):
        await self.opened()
        async def close_notfound(pid, *, before_send=None):
            await self.gate.guard(before_send)
            self.gate.close_writes.append(pid)
            self.gate.finish_position(pid)
            raise GateError('POSITION_NOT_FOUND', '仓位已关闭')
        self.gate.close_position = close_notfound
        state = await self.engine.close_strategy(self.sid, self.gate, self.gate.uid)
        self.assertEqual(state['selected_strategy']['status'], 'stopped')
        await self.engine.close_strategy(self.sid, self.gate, self.gate.uid)
        self.assertEqual(self.gate.close_writes, ['501'])

    async def test_preflight_stop_blocks_post_and_empty_stop_completes(self):
        await self.start()
        self.gate.block_preflight = asyncio.Event()
        task = asyncio.create_task(self.cycle())
        await self.gate.preflight_started.wait()
        self.engine.request_stop(self.sid, self.gate.uid)
        self.gate.block_preflight.set()
        state = await task
        self.assertEqual(self.gate.writes, [])
        self.assertEqual(state['selected_strategy']['status'], 'stopped')
        self.assertFalse(state['has_unresolved_execution'])

    async def test_preflight_blocks_crossed_direction_and_stale_quote(self):
        await self.start()
        self.engine.quote_provider = lambda symbol: market(self.clock, '99', '98.8')
        await self.cycle()
        self.assertEqual(self.gate.writes, [])
        self.engine.quote_provider = None
        self.gate.block_preflight = asyncio.Event()
        task = asyncio.create_task(self.cycle())
        await self.gate.preflight_started.wait()
        self.clock.value += 10
        self.gate.block_preflight.set()
        await task
        self.assertEqual(self.gate.writes, [])

    async def test_single_cancel_disables_only_that_grid(self):
        await self.placed()
        state = await self.engine.cancel_order(self.sid, '201', self.gate, self.gate.uid)
        self.assertFalse(state['selected_strategy']['cells'][0]['enabled'])
        self.assertEqual(state['selected_strategy']['status'], 'running')
        self.gate.fill('202')
        await self.reconcile()
        self.gate.finish_position('502', '110')
        await self.reconcile()
        await self.cycle()
        self.assertEqual([r['price'] for r in self.gate.writes], ['100', '105.00', '105.00'])

    async def test_modify_pending_preserves_fields_and_next_generation(self):
        await self.placed()
        state = await self.engine.modify_order(self.sid, '201', '99', '109', None, self.gate, self.gate.uid)
        self.assertEqual(self.gate.modifications[-1][2], {'price': '99', 'price_tp': '109', 'price_sl': '90'})
        self.assertEqual(state['operation']['status'], 'confirmed')
        self.gate.fill()
        await self.reconcile()
        self.gate.finish_position(price='109')
        await self.reconcile()
        await self.cycle()
        self.assertEqual(self.gate.writes[-1]['price'], '99')
        self.assertEqual(self.gate.writes[-1]['price_tp'], '109')

    async def test_modify_order_then_instant_fill_confirms_history_values(self):
        await self.placed()
        self.gate.modify_behavior = 'instant_fill'
        state = await self.engine.modify_order(self.sid, '201', '99', '109', None, self.gate, self.gate.uid)
        self.assertEqual(state['operation']['status'], 'confirmed')
        self.assertTrue(state['positions'][0]['managed'])
        self.assertEqual(state['positions'][0]['entry_price'], '99')

    async def test_clear_tp_racing_exit_never_repeats(self):
        await self.opened()
        self.gate.modify_behavior = 'close_before_apply'
        state = await self.engine.modify_position(self.sid, '501', '0', None, self.gate, self.gate.uid)
        self.assertEqual(self.gate.modifications[-1][2], {'price_tp': '0', 'price_sl': '90'})
        self.assertEqual(state['operation']['status'], 'unknown')
        await self.cycle()
        self.assertEqual(len(self.gate.writes), 2)

    async def test_external_position_protection_change_never_uses_old_target_to_repeat(self):
        await self.opened()
        self.gate.current_positions[0]['price_tp'] = '0'
        state = await self.reconcile()
        self.assertEqual(state['selected_strategy']['status'], 'paused')
        self.gate.finish_position(price='105')
        await self.reconcile()
        await self.cycle()
        self.assertEqual(len(self.gate.writes), 2)

    async def test_legacy_active_strategy_pauses_and_cannot_automatically_migrate(self):
        await self.placed()
        strategy = self.engine._state_data['strategies'][0]
        strategy.pop('execution_mode')
        self.engine._persist()
        state = await self.restart()
        self.assertEqual(state['selected_strategy']['status'], 'legacy_paused')
        await self.cycle(market(self.clock, '122', '121.8'))
        self.assertEqual(len(self.gate.writes), 2)

    async def test_unknown_modify_never_reissues_and_conflicting_payload_blocked(self):
        await self.placed()
        self.gate.modify_behavior = 'unknown'
        result = await self.engine.modify_order(self.sid, '201', None, '109', None, self.gate, self.gate.uid)
        self.assertEqual(result['operation']['status'], 'unknown')
        await self.restart()
        self.engine.set_market_context('XAUUSD', market(self.clock), SPEC)
        await self.engine.modify_order(self.sid, '201', None, '109', None, self.gate, self.gate.uid)
        with self.assertRaisesRegex(ValueError, '尚未确认'):
            await self.engine.modify_order(self.sid, '201', None, '110', None, self.gate, self.gate.uid)
        self.assertEqual(len(self.gate.modifications), 1)

    async def test_close_cancels_first_and_only_closes_owned_positions(self):
        await self.opened()
        self.gate.current_positions.append({**self.gate.current_positions[0], 'position_id': '999001'})
        state = await self.engine.close_strategy(self.sid, self.gate, self.gate.uid)
        self.assertEqual(self.gate.cancel_writes, ['202'])
        self.assertEqual(self.gate.close_writes, ['501'])
        self.assertEqual([p['id'] for p in state['positions']], ['999001'])
        self.assertEqual(state['selected_strategy']['status'], 'stopped')

    async def test_unknown_close_persists_without_repeat(self):
        await self.opened()
        self.gate.close_behavior = 'unknown'
        state = await self.engine.close_strategy(self.sid, self.gate, self.gate.uid)
        self.assertEqual(state['selected_strategy']['status'], 'closing')
        await self.restart()
        await self.cycle()
        self.assertEqual(self.gate.close_writes, ['501'])

    async def test_partial_execution_is_cancellable_and_never_repeats(self):
        await self.start(cfg(volume='0.02'))
        await self.cycle()
        self.gate.fill(volume='0.01', partial=True)
        state = await self.reconcile()
        self.assertEqual(state['selected_strategy']['status'], 'paused')
        self.assertTrue(any(o['status'] == 'partial' for o in state['orders']))
        await self.engine.cancel(self.sid, self.gate, self.gate.uid)
        self.assertIn('201', self.gate.cancel_writes)
        self.assertEqual(len(self.gate.writes), 2)

    async def test_stop_line_cancels_orders_preserving_native_sl(self):
        await self.opened()
        state = await self.cycle(market(self.clock, '89.2', '89'))
        self.assertEqual(state['selected_strategy']['status'], 'stopped')
        self.assertEqual(self.gate.cancel_writes, ['202'])
        self.assertEqual(self.gate.modifications, [])
        self.assertEqual(state['positions'][0]['stop_loss'], '90')

    async def test_legacy_stopped_position_survives_migration_and_manual_protection_edit(self):
        await self.opened()
        await self.engine.cancel(self.sid, self.gate, self.gate.uid)
        self.engine._state_data['strategies'][0].pop('execution_mode')
        self.engine._state_data['version'] = 1
        self.engine._persist()
        state = await self.restart()
        self.assertEqual(state['selected_strategy']['execution_mode'], 'legacy_local')
        self.assertTrue(state['positions'][0]['managed'])
        await self.cycle()
        self.assertEqual(len(self.gate.writes), 2)
        await self.engine.modify_position(self.sid, '501', '120', None, self.gate, self.gate.uid)
        self.assertEqual(self.gate.modifications[-1][2], {'price_tp': '120', 'price_sl': '90'})

    async def test_preview_non_usd_unknown_step_staleness_and_rejection(self):
        await self.reconcile()
        spec = {**SPEC, 'currency': 'JPY', 'settlement_currency': 'JPY'}
        preview = self.engine.preview(cfg(volume='0.015'), spec, market(self.clock), True)
        self.assertTrue(preview['can_start'])
        self.assertIsNone(preview['estimated_margin'])
        self.assertIsNone(preview['profit_per_grid_min'])
        await self.start()
        for changes in ({'stale': True}, {'trading_status': 'closed'}, {'trade_mode': '3'}):
            await self.cycle(market(self.clock, **changes))
        self.assertEqual(self.gate.writes, [])
        self.gate.behavior = 'reject'
        await self.cycle()
        await self.cycle()
        self.assertEqual(len(self.gate.writes), 1)

    async def test_connected_stale_preview_has_sync_blocker_not_connection_warning(self):
        await self.reconcile()
        self.clock.value += 10
        preview = self.engine.preview(cfg(), SPEC, market(self.clock), True, self.gate.uid)
        self.assertFalse(preview['can_start'])
        self.assertEqual(preview['blocker_codes'], ['account_sync_required'])
        self.assertIn('Gate 已连接', preview['blockers'][0])
        self.assertFalse(any('连接并核验' in message for message in preview['warnings'] + preview['blockers']))
        disconnected = self.engine.preview(cfg(), SPEC, market(self.clock), False, self.gate.uid)
        self.assertEqual(disconnected['blocker_codes'], ['connection_required'])

    async def test_preview_current_refresh_skips_history_and_does_not_authorize_execution_snapshot(self):
        await self.reconcile()
        self.clock.value += 10
        async def forbidden_history(*args, **kwargs):
            self.fail('Preview must not paginate histories')
        self.gate.position_history = self.gate.order_history = forbidden_history
        await self.engine.refresh_preview_account(self.gate, self.gate.uid)
        preview = self.engine.preview(cfg(), SPEC, market(self.clock), True, self.gate.uid)
        self.assertTrue(preview['can_start'])
        self.assertEqual(preview['blockers'], [])
        self.assertFalse(preview['account']['stale'])
        self.assertFalse(self.engine._snapshot_fresh(self.gate.uid))
        self.assertEqual(self.gate.writes, [])

    async def test_concurrent_preview_current_reads_share_engine_lock_and_fresh_result(self):
        await self.reconcile()
        self.clock.value += 10
        entered, release = asyncio.Event(), asyncio.Event()
        asset_read = self.gate.assets
        calls = 0
        async def delayed_assets():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return await asset_read()
        self.gate.assets = delayed_assets
        first = asyncio.create_task(self.engine.refresh_preview_account(self.gate, self.gate.uid))
        await entered.wait()
        second = asyncio.create_task(self.engine.refresh_preview_account(self.gate, self.gate.uid))
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(calls, 1)

    async def test_slow_history_is_read_before_current_assets_not_used_to_fake_freshness(self):
        original = self.gate.position_history
        async def slow_history(*args, **kwargs):
            self.clock.value += 10
            return await original(*args, **kwargs)
        self.gate.position_history = slow_history
        seen_at = []
        assets = self.gate.assets
        async def record_assets():
            seen_at.append(self.clock())
            return await assets()
        self.gate.assets = record_assets
        await self.reconcile()
        self.assertEqual(seen_at, [self.clock()])
        self.assertTrue(self.engine._snapshot_fresh(self.gate.uid))

    async def test_slow_current_inventory_does_not_relabel_old_assets_as_fresh(self):
        original = self.gate.orders
        async def delayed_orders():
            self.clock.value += 4
            return await original()
        self.gate.orders = delayed_orders
        await self.reconcile()
        preview = self.engine.preview(cfg(), SPEC, market(self.clock), True, self.gate.uid)
        self.assertFalse(preview['can_start'])
        self.assertEqual(preview['blocker_codes'], ['account_sync_required'])
        self.assertFalse(self.engine._snapshot_fresh(self.gate.uid))


if __name__ == '__main__':
    unittest.main()
