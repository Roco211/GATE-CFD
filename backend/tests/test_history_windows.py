"""Gate's ten-row default history and durable, bounded closing-window reads."""
import copy
import tempfile
import unittest
from pathlib import Path

from app.live import LiveEngine
from test_live import Clock, FakeGate, SPEC, cfg, market


class WindowGate(FakeGate):
    def __init__(self, clock):
        super().__init__(clock)
        self.window_calls = []
        self.window_override = None

    async def order_history(self, **filters):
        rows = sorted(self.order_histories, key=lambda row: int(row.get('time_setup', 0)), reverse=True)
        if filters:
            self.window_calls.append(copy.deepcopy(filters))
            if self.window_override is not None:
                return self.envelope({'list': self.window_override})
            rows = [row for row in rows if row.get('symbol') == filters['symbol'] and str(row.get('side')) == str(filters['side'])
                    and filters['begin_time'] <= int(row.get('time_setup', 0)) <= filters['end_time']]
        return self.envelope({'list': rows[:10]})


class HistoryWindowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'windows.sqlite3'
        self.clock = Clock()
        self.gate = WindowGate(self.clock)
        self.engine = LiveEngine(self.path, clock=self.clock)
        config = cfg(direction='short', lower_price='4380', upper_price='4386', grid_count=5, stop_loss=None)
        result = await self.engine.start(config, 'window-test', self.gate, self.quote(), SPEC, self.gate.uid)
        self.sid = result['strategy_id']
        await self.engine.cycle(self.gate, {'XAUUSD': self.quote()}, {'XAUUSD': SPEC}, self.gate.uid)

    async def asyncTearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def quote(self):
        return market(self.clock, ask='4384.2', bid='4384')

    async def reconcile(self):
        return await self.engine.reconcile(self.gate, self.gate.uid)

    async def close_grid(self, *, stop=True):
        self.gate.fill('201', price='4384.72')
        await self.reconcile()
        if stop:
            await self.engine.cancel(self.sid, self.gate, self.gate.uid)
        self.gate.finish_position('501', price='4383.67')
        return self.gate.closed_positions[-1], self.gate.order_histories[-1]

    def add_newer_noise(self, count=20):
        self.clock.value += 100
        for index in range(count):
            self.gate.order_histories.append({'order_id': str(700000 + len(self.gate.order_histories)),
                'symbol': 'EURUSD', 'price_type': 'market', 'order_opt_type': 1, 'side': 2,
                'state': 4, 'volume': '0.01', 'fill_volume': '0.01', 'price': '1.1',
                'trigger_price': '1.1', 'price_tp': '0', 'price_sl': '0', 'sl_tp_type': 0,
                'time_setup': int(self.clock()) + index, 'time_done': int(self.clock()) + index, 'close_pnl': ''})
        self.clock.value += count

    def entry(self):
        return next(iter(self.engine._state_data['history_windows'][self.gate.uid].values()))

    async def test_old_tp_survives_default_ten_rollover_and_real_database_restart(self):
        row, closing = await self.close_grid()
        self.add_newer_noise()
        self.assertNotIn(closing['order_id'], [r['order_id'] for r in (await self.gate.order_history())['data']['list']])
        state = await self.reconcile()
        self.assertEqual(self.engine.classify_exit(row, self.gate.uid)['type'], 'target_exit')
        self.assertEqual(len(self.gate.window_calls), 1)
        self.assertEqual(self.gate.window_calls[0], {'symbol': 'XAUUSD', 'side': 2,
                         'begin_time': row['time_close'] - 1, 'end_time': row['time_close'] + 1})
        self.assertTrue(self.entry()['frozen'])
        self.assertIn(closing['order_id'], [r['order_id'] for r in self.entry()['rows']])
        self.assertIn('201', self.engine._state_data['owned_order_history'][self.gate.uid])
        await self.reconcile()
        self.engine.close()
        self.engine = LiveEngine(self.path, clock=self.clock)
        state = await self.reconcile()
        self.assertEqual(self.engine.classify_exit(row, self.gate.uid)['type'], 'target_exit')
        self.assertEqual(len(self.gate.window_calls), 1)
        self.assertEqual(state['selected_strategy']['status'], 'stopped')

    async def test_same_second_positions_share_one_window(self):
        self.gate.fill('201', price='4384.72')
        self.gate.fill('202', price='4385.96')
        await self.reconcile()
        await self.engine.cancel(self.sid, self.gate, self.gate.uid)
        self.gate.finish_position('501', price='4383.67')
        self.gate.finish_position('502', price='4384.71')
        self.add_newer_noise()
        await self.reconcile()
        self.assertEqual(len(self.gate.window_calls), 1)
        self.assertEqual(set(self.entry()['position_ids']), {'501', '502'})
        self.assertTrue(all(self.engine.classify_exit(row, self.gate.uid)['type'] == 'target_exit' for row in self.gate.closed_positions))

    async def test_empty_window_never_freezes_from_default_only_evidence(self):
        row, closing = await self.close_grid()
        self.gate.window_override = []
        await self.reconcile()
        self.assertFalse(self.entry()['frozen'])
        self.assertFalse(self.entry()['complete'])
        self.assertFalse(self.engine.classify_exit(row, self.gate.uid)['confirmed'])
        self.add_newer_noise()
        self.gate.window_override = None
        await self.reconcile()
        self.assertEqual(self.engine.classify_exit(row, self.gate.uid)['type'], 'target_exit')
        self.assertTrue(self.entry()['frozen'])

    async def test_empty_window_without_any_evidence_retries_when_late_order_arrives(self):
        row, closing = await self.close_grid()
        self.gate.order_histories.remove(closing)
        await self.reconcile()
        self.assertFalse(self.entry()['frozen'])
        await self.reconcile()
        self.assertEqual(len(self.gate.window_calls), 1)
        self.gate.order_histories.append(closing)
        await self.reconcile()
        self.assertEqual(len(self.gate.window_calls), 2)
        self.assertEqual(self.engine.classify_exit(row, self.gate.uid)['type'], 'target_exit')

    async def test_ten_row_window_is_not_promoted_to_complete_after_merging(self):
        row, closing = await self.close_grid()
        for index in range(9):
            self.gate.order_histories.append({**closing, 'order_id': str(880000 + index), 'price': str(4300 + index)})
        await self.reconcile()
        self.assertEqual(self.entry()['response_count'], 10)
        self.assertFalse(self.entry()['complete'])
        self.assertFalse(self.entry()['frozen'])
        self.assertFalse(self.engine.classify_exit(row, self.gate.uid)['confirmed'])

    async def test_foreign_competitor_and_missing_id_are_preserved_and_block_confirmation(self):
        row, closing = await self.close_grid()
        competitor = {**closing, 'order_id': '881001'}
        competitor.pop('close_pnl')
        self.gate.order_histories.append(competitor)
        await self.reconcile()
        self.assertFalse(self.engine.classify_exit(row, self.gate.uid)['confirmed'])
        self.assertIn('881001', [r['order_id'] for r in self.entry()['rows']])
        self.engine.close()
        self.engine = LiveEngine(self.path, clock=self.clock)
        self.add_newer_noise()
        await self.reconcile()
        self.assertFalse(self.engine.classify_exit(row, self.gate.uid)['confirmed'])
        bad = {**closing}
        bad.pop('order_id')
        self.gate.window_override = [closing, bad]
        self.clock.value += 6
        await self.reconcile()
        self.assertFalse(self.entry()['complete'])
        self.assertEqual(len(self.entry()['raw_response_rows']), 2)

    async def test_window_scope_uses_setup_while_exit_matching_uses_done(self):
        row, closing = await self.close_grid()
        entry = {**closing, 'order_id': '881002', 'price_type': 'trigger', 'order_opt_type': 2,
                 'sl_tp_type': 0, 'time_setup': closing['time_setup'], 'time_done': closing['time_done'] + 60,
                 'close_pnl': ''}
        self.gate.order_histories.append(entry)
        await self.reconcile()
        self.assertTrue(self.entry()['complete'])
        self.assertEqual(self.engine.classify_exit(row, self.gate.uid)['order_id'], closing['order_id'])

    async def test_bad_out_of_scope_response_can_recover_without_losing_prior_raw_rows(self):
        row, closing = await self.close_grid()
        outside = {**closing, 'order_id': '881003', 'time_setup': closing['time_setup'] + 50,
                   'time_done': closing['time_done'] + 50}
        self.gate.window_override = [closing, outside]
        await self.reconcile()
        self.assertFalse(self.entry()['complete'])
        self.gate.window_override = None
        self.clock.value += 6
        await self.reconcile()
        self.assertTrue(self.entry()['complete'])
        self.assertEqual(self.engine.classify_exit(row, self.gate.uid)['type'], 'target_exit')
        self.assertIn('881003', [r['order_id'] for r in self.entry()['rows']])

    async def test_request_budget_two_and_unchecked_windows_get_the_next_turn(self):
        row, closing = await self.close_grid()
        source = copy.deepcopy(self.engine._state_data['intents'][0])
        for index in range(1, 5):
            pid, oid = str(501 + index), str(882000 + index)
            second = int(row['time_close']) + index * 10
            self.gate.closed_positions.append({**copy.deepcopy(row), 'position_id': pid, 'time_close': second})
            self.gate.order_histories.append({**closing, 'order_id': oid, 'time_setup': second, 'time_done': second})
            self.engine._state_data['intents'].append({**copy.deepcopy(source), 'id': f'historical-{index}',
                'position_id': pid, 'position_ids': [pid], 'grid_index': index, 'status': 'closed', 'remote_order_id': None})
        self.clock.value += 100
        self.gate.window_override = []
        await self.engine._refresh(self.gate, self.gate.uid, force=True)
        self.assertEqual(len(self.gate.window_calls), 2)
        await self.engine._refresh(self.gate, self.gate.uid, force=True)
        self.assertEqual(len(self.gate.window_calls), 4)
        await self.engine._refresh(self.gate, self.gate.uid, force=True)
        self.assertEqual(len(self.gate.window_calls), 5)
        self.assertEqual(len({(call['begin_time'], call['end_time']) for call in self.gate.window_calls}), 5)

    async def test_cache_is_scoped_to_account_and_wrong_uid_cannot_commit_new_history(self):
        row, _ = await self.close_grid()
        await self.reconcile()
        saved = copy.deepcopy(self.engine._state_data['history_windows'])
        self.gate.uid = '80002'
        with self.assertRaisesRegex(ValueError, '不一致'):
            await self.engine.reconcile(self.gate, '70001')
        self.assertEqual(self.engine._state_data['history_windows'], saved)
        self.assertNotIn('80002', self.engine._state_data['history_windows'])

    async def test_default_candidate_outside_query_result_cannot_confirm_until_window_expands(self):
        row, closing = await self.close_grid()
        closing['time_setup'] -= 2
        await self.reconcile()
        self.assertEqual(self.entry()['response_order_ids'], [])
        self.assertFalse(self.engine.classify_exit(row, self.gate.uid)['confirmed'])
        self.assertFalse(self.entry()['frozen'])
        self.clock.value += 6
        await self.reconcile()
        self.assertEqual(self.entry()['radius'], 5)
        self.assertEqual(self.engine.classify_exit(row, self.gate.uid)['type'], 'target_exit')
        self.assertIn(closing['order_id'], self.entry()['response_order_ids'])

    async def test_empty_window_expands_to_thirty_seconds_after_default_rollover(self):
        row, closing = await self.close_grid()
        closing['time_setup'] -= 20
        self.add_newer_noise()
        await self.reconcile()
        self.assertEqual(self.entry()['radius'], 1)
        self.assertFalse(self.engine.classify_exit(row, self.gate.uid)['confirmed'])
        self.clock.value += 6
        await self.reconcile()
        self.assertEqual(self.entry()['radius'], 5)
        self.clock.value += 6
        await self.reconcile()
        self.assertEqual(self.entry()['radius'], 30)
        self.assertEqual(self.engine.classify_exit(row, self.gate.uid)['type'], 'target_exit')

    async def test_window_with_unrelated_order_still_expands_to_find_earlier_created_close(self):
        row, closing = await self.close_grid()
        closing['time_setup'] -= 2
        unrelated = {**closing, 'order_id': '889001', 'time_setup': row['time_close'],
                     'time_done': row['time_close'], 'price_type': 'trigger', 'order_opt_type': 2,
                     'sl_tp_type': 0, 'close_pnl': ''}
        self.gate.order_histories.append(unrelated)
        self.add_newer_noise()
        await self.reconcile()
        self.assertEqual(self.entry()['response_count'], 1)
        self.assertFalse(self.engine.classify_exit(row, self.gate.uid)['confirmed'])
        self.clock.value += 6
        await self.reconcile()
        self.assertEqual(self.entry()['radius'], 5)
        self.assertEqual(self.engine.classify_exit(row, self.gate.uid)['type'], 'target_exit')
