"""Closed positions remain visible while their exit order evidence catches up."""
import copy
import asyncio

from app.live import LiveEngine
from test_live import Clock, FakeGate


def test_trade_feed_sorts_newest_first_and_keeps_unconfirmed_exits_visible(tmp_path):
    asyncio.run(check_trade_feed(tmp_path))


async def check_trade_feed(tmp_path):
    clock = Clock()
    gate = FakeGate(clock)
    row = {
        'symbol': 'XAUUSD', 'position_dir': 'Short', 'price_open': '4384.72',
        'close_price': '4383.67', 'price_tp': '4383.6', 'price_sl': '0',
        'volume': '0.01', 'volume_closed': '0.01', 'position_status': '1',
        'realized_pnl': '1', 'realized_pnl_detail': {'closed_pnl': '1.05'},
        'time_create': int(clock()) - 100,
    }
    gate.closed_positions = [
        {**row, 'position_id': '801', 'time_close': int(clock()) - 30},
        {**row, 'position_id': '9007199254740993', 'time_close': int(clock()) - 10},
        {**row, 'position_id': '803', 'time_close': int(clock()) - 20},
        {**row, 'position_id': '9007199254740994', 'time_close': int(clock()) - 10},
    ]
    original = copy.deepcopy(gate.closed_positions)
    engine = LiveEngine(tmp_path / 'feed.sqlite3', clock=clock)
    try:
        state = await engine.reconcile(gate, gate.uid)
        assert [fill['id'] for fill in state['fills']] == [
            '9007199254740994', '9007199254740993', '803', '801',
        ]
        assert all(fill['type'] == 'awaiting_confirmation' for fill in state['fills'])
        assert all(fill['exit_confirmed'] is False for fill in state['fills'])
        assert all(fill['volume'] == '0.01' and fill['pnl'] == '1' for fill in state['fills'])
        assert gate.closed_positions == original
        assert not gate.writes and not gate.close_writes and not gate.cancel_writes
    finally:
        engine.close()
