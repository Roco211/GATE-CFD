"""Rejection details survive the trading journal and process restart."""
import asyncio
from unittest.mock import AsyncMock

from app.gate import GateError
from app.live import LiveEngine
from test_live import Clock, FakeGate, SPEC, cfg, market


def test_rejected_order_keeps_diagnostics_after_restart_without_replay(tmp_path):
    async def scenario():
        clock = Clock()
        gate = FakeGate(clock)
        gate.create_order = AsyncMock(side_effect=GateError('INVALID_STOPS', '止损参数无效', status_code=400))
        path = tmp_path / 'diagnostics.sqlite3'
        engine = LiveEngine(path, clock=clock)
        result = await engine.start(cfg(), 'diagnostics-start', gate, market(clock), SPEC, gate.uid)
        sid = result['strategy_id']
        await engine.cycle(gate, {'XAUUSD': market(clock)}, {'XAUUSD': SPEC}, gate.uid)
        original = engine._state_data['intents'][0]['error_detail']
        assert original['code'] == 'INVALID_STOPS' and original['status_code'] == 400
        assert engine.state('XAUUSD', gate.uid)['strategies'][0]['last_execution_error'] == original
        assert engine.state('XAUUSD', gate.uid)['strategies'][0]['status'] == 'paused'
        engine.close()
        recovered = LiveEngine(path, clock=clock)
        try:
            await recovered.cycle(gate, {'XAUUSD': market(clock)}, {'XAUUSD': SPEC}, gate.uid)
            assert gate.create_order.await_count == 1
            assert recovered._state_data['intents'][0]['error_detail'] == original
            strategy = next(s for s in recovered.state('XAUUSD', gate.uid)['strategies'] if s['id'] == sid)
            assert strategy['last_execution_error'] == original
        finally:
            recovered.close()
    asyncio.run(scenario())


def test_rejected_modification_keeps_official_error_without_changing_remote(tmp_path):
    async def scenario():
        clock, gate = Clock(), None
        gate = FakeGate(clock)
        engine = LiveEngine(tmp_path / 'modification.sqlite3', clock=clock)
        try:
            result = await engine.start(cfg(), 'diagnostics-modify', gate, market(clock), SPEC, gate.uid)
            await engine.cycle(gate, {'XAUUSD': market(clock)}, {'XAUUSD': SPEC}, gate.uid)
            gate.update_order = AsyncMock(side_effect=GateError('INVALID_STOPS', '止盈距离不足', status_code=400))
            result = await engine.modify_order(result['strategy_id'], '201', '100', '109', None, gate, gate.uid)
            operation = result['operation']
            assert operation['status'] == 'rejected'
            assert operation['error_detail']['code'] == 'INVALID_STOPS'
            assert operation['error_detail']['status_code'] == 400
            assert gate.current_orders[0]['price_tp'] == '105.00'
            assert engine._state_data['operations'][-1]['error_detail'] == operation['error_detail']
        finally:
            engine.close()
    asyncio.run(scenario())
