"""TP execution evidence, delayed histories and explicit recovery; all fake I/O."""
import copy
import tempfile
import unittest
from pathlib import Path

from app.live import LiveEngine
from test_live import Clock, FakeGate, SPEC, cfg, market


class ExitRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'exit.sqlite3'
        self.clock, self.gate = Clock(), None
        self.gate = FakeGate(self.clock)
        self.engine = LiveEngine(self.path, clock=self.clock)
        self.config = cfg(direction='short', lower_price='4380', upper_price='4386', grid_count=5, stop_loss=None)
        result = await self.engine.start(self.config, 'recovery-test', self.gate, self.quote(), SPEC, self.gate.uid)
        self.sid = result['strategy_id']
        await self.cycle()
        self.assertEqual(len(self.gate.writes), 2)

    def quote(self):
        return market(self.clock, ask='4384.2', bid='4384')

    async def asyncTearDown(self):
        self.engine.close()
        self.temp.cleanup()

    async def cycle(self):
        await self.engine.cycle(self.gate, {'XAUUSD': self.quote()}, {'XAUUSD': SPEC}, self.gate.uid)

    async def reconcile(self):
        return await self.engine.reconcile(self.gate, self.gate.uid)

    async def close_grid(self, oid='201', price='4383.67', sl_tp_type=2):
        pid = self.gate.fill(oid, price='4384.72' if oid == '201' else '4385.96')
        await self.reconcile()
        self.gate.finish_position(pid, price=price, sl_tp_type=sl_tp_type)
        row = self.gate.closed_positions[-1]
        row.update(realized_pnl='1', realized_pnl_detail={'closed_pnl': '1.05', 'fee': '-0.05', 'swap': '0'})
        closing = self.gate.order_histories[-1]
        closing['close_pnl'] = '1.05'
        return row, closing

    def intent(self):
        return self.engine._state_data['intents'][0]

    def strategy(self):
        return self.engine._state_data['strategies'][0]

    async def test_short_tp_adverse_slippage_uses_trigger_evidence_not_close_price_threshold(self):
        row, closing = await self.close_grid()
        state = await self.reconcile()
        self.assertEqual(row['price_tp'], '4383.60')
        self.assertEqual(row['close_price'], '4383.67')
        evidence = self.engine.classify_exit(row, self.gate.uid)
        self.assertEqual(evidence['type'], 'target_exit')
        self.assertEqual(evidence['order_id'], closing['order_id'])
        self.assertEqual(self.intent()['exit_type'], 'target_exit')
        self.assertEqual(state['selected_strategy']['status'], 'running')
        self.assertEqual(self.strategy()['cells'][3]['generation'], 1)
        self.assertEqual(state['account']['realized_pnl'], '1.00')

    async def test_profit_and_price_beyond_target_do_not_turn_ordinary_close_into_tp(self):
        row, _ = await self.close_grid(price='4383.50', sl_tp_type=0)
        await self.reconcile()
        self.assertEqual(self.engine.classify_exit(row, self.gate.uid)['type'], 'ordinary_close')
        self.assertEqual(self.strategy()['status'], 'paused')
        self.assertEqual(self.strategy()['cells'][3]['generation'], 0)
        self.assertFalse(self.engine.resume_eligibility(self.strategy(), self.gate.uid)['recoverable'])
        with self.assertRaises(ValueError):
            await self.engine.resume_strategy(self.sid, self.gate, self.gate.uid)
        self.assertEqual(len(self.gate.writes), 2)

    async def test_unknown_nonzero_type_is_not_guessed_as_stop_loss(self):
        row, _ = await self.close_grid(sl_tp_type=1)
        await self.reconcile()
        self.assertEqual(self.engine.classify_exit(row, self.gate.uid)['type'], 'awaiting_confirmation')
        self.assertEqual(self.intent()['status'], 'awaiting_exit_evidence')

    async def test_delayed_closing_order_confirms_next_read_and_rearms_once(self):
        row, closing = await self.close_grid()
        self.gate.order_histories.remove(closing)
        state = await self.reconcile()
        self.assertEqual(self.intent()['status'], 'awaiting_exit_evidence')
        self.assertEqual(self.strategy()['status'], 'running')
        self.assertEqual(state['account']['realized_pnl'], '1.00')
        self.assertEqual(self.strategy()['completed_cycles'], 0)
        self.gate.order_histories.insert(0, closing)
        await self.reconcile()
        await self.reconcile()
        self.assertEqual(self.strategy()['completed_cycles'], 1)
        self.assertEqual(self.strategy()['cells'][3]['generation'], 1)
        await self.cycle()
        await self.cycle()
        self.assertEqual(len(self.gate.writes), 3)
        self.assertEqual(self.strategy()['realized_pnl'], '1')

    async def test_two_matching_closing_orders_and_two_matching_positions_are_ambiguous(self):
        row, closing = await self.close_grid()
        duplicate = {**closing, 'order_id': '999999'}
        self.gate.order_histories.append(duplicate)
        await self.reconcile()
        self.assertFalse(self.engine.classify_exit(row, self.gate.uid)['confirmed'])
        self.gate.order_histories.remove(duplicate)
        self.gate.closed_positions.append({**copy.deepcopy(row), 'position_id': '999998'})
        await self.reconcile()
        self.assertFalse(self.engine.classify_exit(row, self.gate.uid)['confirmed'])
        self.assertEqual(self.intent()['status'], 'awaiting_exit_evidence')
        self.assertEqual(self.engine._state_data.get('exit_receipts', {}), {})

    async def test_consumed_closing_ticket_cannot_be_reassigned_after_history_window_changes(self):
        row, closing = await self.close_grid()
        await self.reconcile()
        replacement = {**copy.deepcopy(row), 'position_id': '999998'}
        self.gate.closed_positions[:] = [replacement]
        self.engine.close()
        self.engine = LiveEngine(self.path, clock=self.clock)
        await self.reconcile()
        result = self.engine.classify_exit(replacement, self.gate.uid)
        self.assertFalse(result['confirmed'])
        self.assertIn('已经关联', result['reason'])

    async def test_external_candidate_with_missing_pnl_or_protection_still_prevents_unique_claim(self):
        row, _ = await self.close_grid()
        for missing in ('price_tp', 'realized_pnl_detail'):
            with self.subTest(missing=missing):
                other = {**copy.deepcopy(row), 'position_id': '999997'}
                other.pop(missing)
                self.gate.closed_positions[:] = [row, other]
                await self.reconcile()
                result = self.engine.classify_exit(row, self.gate.uid)
                self.assertFalse(result['confirmed'])
                self.assertEqual(self.engine._state_data.get('exit_receipts', {}), {})

    async def test_matching_close_order_with_missing_pnl_prevents_false_uniqueness(self):
        row, closing = await self.close_grid()
        other = {**closing, 'order_id': '999996'}
        other.pop('close_pnl')
        self.gate.order_histories.append(other)
        await self.reconcile()
        self.assertFalse(self.engine.classify_exit(row, self.gate.uid)['confirmed'])

    async def test_matching_tp_order_with_missing_trigger_remains_a_potential_candidate(self):
        row, closing = await self.close_grid()
        other = {**closing, 'order_id': '999994'}
        other.pop('trigger_price')
        self.gate.order_histories.append(other)
        await self.reconcile()
        self.assertFalse(self.engine.classify_exit(row, self.gate.uid)['confirmed'])

    async def test_explicit_close_finishes_when_position_is_closed_before_deal_history_arrives(self):
        self.gate.fill('201', price='4384.72')
        await self.reconcile()
        async def close_with_late_history(pid, *, before_send=None):
            await self.gate.guard(before_send)
            self.gate.close_writes.append(pid)
            self.gate.finish_position(pid, price='4383.67', sl_tp_type=0)
            self.gate.order_histories.pop()
            return {}
        self.gate.close_position = close_with_late_history
        state = await self.engine.close_strategy(self.sid, self.gate, self.gate.uid)
        self.assertEqual(state['selected_strategy']['status'], 'stopped')
        self.assertEqual(self.intent()['status'], 'closed')
        self.assertEqual(self.intent()['exit_evidence'][0]['source'], 'console_close_operation')
        await self.cycle()
        self.assertEqual(len(self.gate.writes), 2)

    async def test_history_quantity_larger_than_owned_fill_never_rearms(self):
        row, closing = await self.close_grid()
        row.update(volume='0.02', volume_closed='0.02')
        closing.update(volume='0.02', fill_volume='0.02')
        await self.reconcile()
        self.assertEqual(self.intent()['status'], 'awaiting_close')
        self.assertEqual(self.strategy()['cells'][3]['generation'], 0)

    async def test_incomplete_history_and_net_pnl_mismatch_never_claim_tp(self):
        row, closing = await self.close_grid()
        closing['close_pnl'] = row['realized_pnl']
        await self.reconcile()
        self.assertFalse(self.engine.classify_exit(row, self.gate.uid)['confirmed'])
        closing['close_pnl'] = row['realized_pnl_detail']['closed_pnl']
        await self.reconcile()
        self.engine._snapshots[self.gate.uid]['history_complete'] = False
        self.assertFalse(self.engine.classify_exit(row, self.gate.uid)['confirmed'])

    async def test_old_paused_closed_reclassifies_without_recount_or_automatic_resume(self):
        row, _ = await self.close_grid()
        intent, strategy = self.intent(), self.strategy()
        intent.update(status='closed', exit_type='closed', accounted_position_ids=[row['position_id']])
        strategy.update(status='paused', error='策略仓位发生强平或非目标价退出', realized_pnl='1', completed_cycles=1)
        strategy['cells'][3].update(status='done', completed_cycles=1)
        self.engine._persist()
        self.engine.close()
        self.engine = LiveEngine(self.path, clock=self.clock)
        self.engine.set_market_context('XAUUSD', self.quote(), SPEC)
        await self.reconcile()
        self.assertEqual(self.intent()['exit_type'], 'target_exit')
        self.assertEqual(self.strategy()['status'], 'paused')
        self.assertEqual(self.strategy()['completed_cycles'], 1)
        self.assertEqual(self.strategy()['realized_pnl'], '1')
        await self.cycle()
        self.assertEqual(len(self.gate.writes), 2)
        self.assertTrue(self.engine.resume_eligibility(self.strategy(), self.gate.uid)['recoverable'])
        await self.engine.resume_strategy(self.sid, self.gate, self.gate.uid)
        self.assertEqual(len(self.gate.writes), 2)
        self.assertEqual(self.strategy()['cells'][3]['generation'], 1)
        await self.engine.resume_strategy(self.sid, self.gate, self.gate.uid)
        await self.cycle()
        await self.cycle()
        self.assertEqual(len(self.gate.writes), 3)
        self.assertEqual(self.strategy()['completed_cycles'], 1)

    async def test_stopped_strategy_remains_stopped_after_exit_evidence_arrives(self):
        row, _ = await self.close_grid()
        self.strategy().update(status='stopped', stop_requested=True)
        await self.reconcile()
        self.assertEqual(self.strategy()['status'], 'stopped')
        self.assertFalse(self.engine.resume_eligibility(self.strategy(), self.gate.uid)['recoverable'])
        with self.assertRaises(ValueError):
            await self.engine.resume_strategy(self.sid, self.gate, self.gate.uid)
        self.assertEqual(len(self.gate.writes), 2)
