import concurrent.futures
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app.core import Engine, GridConfig, get_spec, make_preview


def config(**overrides):
    return GridConfig.model_validate({"symbol": "XAUUSD", "direction": "long", "lower_price": "2480", "upper_price": "2500", "volume": "0.01", "grid_count": 4, "spacing": "arithmetic", "repeat": True, **overrides})


class GridMathTests(unittest.TestCase):
    def preview(self, cfg):
        return make_preview(cfg, get_spec(cfg.symbol), {"bid": "2485.35", "ask": "2485.65"})

    def test_grid_intervals_and_eligibility(self):
        result = self.preview(config())
        self.assertEqual(result["levels"], ["2480", "2485.00", "2490.00", "2495.00", "2500"])
        self.assertEqual(len(result["cells"]), 4)
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(result["waiting_count"], 2)

    def test_geometric_rounding_and_endpoints(self):
        result = self.preview(config(spacing="geometric", lower_price="2400", upper_price="2600", grid_count=17))
        levels = list(map(Decimal, result["levels"]))
        self.assertEqual(levels[0], Decimal("2400"))
        self.assertEqual(levels[-1], Decimal("2600"))
        self.assertEqual(len(levels), 18)
        self.assertTrue(all(v % Decimal("0.01") == 0 for v in levels))
        self.assertTrue(all(a < b for a, b in zip(levels, levels[1:])))

    def test_reject_narrow_range_and_misaligned_prices(self):
        for override in ({"lower_price": "2480", "upper_price": "2480.02", "grid_count": 4}, {"lower_price": "2480.001"}):
            with self.assertRaises(ValueError):
                self.preview(config(**override))

    def test_reject_nonfinite_range_and_bad_volume(self):
        for override in ({"lower_price": "NaN"}, {"upper_price": "Infinity"}, {"volume": True}, {"lower_price": "0"}, {"upper_price": "2470"}, {"grid_count": 101}, {"grid_count": 2.5}, {"stop_loss": "2481"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                config(**override)
        for volume in ("0.001", "0.015", "101"):
            with self.subTest(volume=volume), self.assertRaises(ValueError):
                self.preview(config(volume=volume))

    def test_short_tp_is_lower_and_eligible_above_bid(self):
        result = self.preview(config(direction="short"))
        self.assertEqual(result["eligible_count"], 3)
        self.assertTrue(all(Decimal(cell["take_profit"]) < Decimal(cell["entry_price"]) for cell in result["cells"]))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "paper.sqlite3"
        self.engine = Engine(self.path)
        self.engine.step("XAUUSD", "2485.50")

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def test_idempotence_conflict_and_symbol_guard(self):
        first = self.engine.start(config(), "click-1")
        replay = self.engine.start(config(), "click-1")
        self.assertEqual(first["strategy_id"], replay["strategy_id"])
        self.assertEqual(len(first["orders"]), len(replay["orders"]))
        with self.assertRaisesRegex(ValueError, "request_id"):
            self.engine.start(config(grid_count=5), "click-1")
        with self.assertRaisesRegex(ValueError, "重复启动"):
            self.engine.start(config(), "click-2")

    def test_cancel_preserves_position_and_tp_without_replenishment(self):
        started = self.engine.start(config(), "click-1")
        filled = self.engine.step("XAUUSD", "2484")
        self.assertEqual(len(filled["positions"]), 1)
        self.assertEqual(filled["positions"][0]["entry_price"], "2485.00")
        cancelled = self.engine.cancel(started["strategy_id"])
        self.assertEqual(cancelled["remaining_positions"], 1)
        self.assertFalse(any(o["status"] == "pending" for o in cancelled["orders"]))
        self.assertEqual(cancelled["positions"][0]["take_profit"], "2490.00")
        with self.assertRaisesRegex(ValueError, "未平仓"):
            self.engine.start(config(), "click-2")
        closed = self.engine.step("XAUUSD", "2491")
        self.assertEqual(len(closed["positions"]), 0)
        self.assertFalse(any(o["status"] == "pending" for o in closed["orders"]))
        self.assertEqual(closed["account"]["realized_pnl"], "5.00")
        self.assertEqual(closed["selected_strategy"]["status"], "stopped")

    def test_repeat_replenishes_exactly_once_after_profit(self):
        self.engine.start(config(), "click-1")
        self.engine.step("XAUUSD", "2484")
        closed = self.engine.step("XAUUSD", "2491")
        self.assertEqual(len(closed["positions"]), 0)
        replacements = [o for o in closed["orders"] if o["grid_index"] == 1 and o["status"] == "pending"]
        self.assertEqual(len(replacements), 1)
        again = self.engine.step("XAUUSD", "2491")
        self.assertEqual(len([o for o in again["orders"] if o["grid_index"] == 1 and o["status"] == "pending"]), 1)

    def test_single_cycle_does_not_replenish(self):
        self.engine.start(config(repeat=False), "click-1")
        self.engine.step("XAUUSD", "2484")
        closed = self.engine.step("XAUUSD", "2491")
        self.assertFalse(any(o["grid_index"] == 1 and o["status"] == "pending" for o in closed["orders"]))
        self.assertEqual(closed["selected_strategy"]["cells"][1]["status"], "done")

    def test_stop_loss_closes_at_quote_and_prevents_replenishment(self):
        self.engine.start(config(stop_loss="2470"), "click-1")
        self.engine.step("XAUUSD", "2484")
        stopped = self.engine.step("XAUUSD", "2460")
        self.assertEqual(stopped["selected_strategy"]["status"], "stopped")
        self.assertEqual(stopped["positions"], [])
        self.assertFalse(any(o["status"] == "pending" for o in stopped["orders"]))
        self.assertEqual(stopped["fills"][-1]["type"], "stop_loss")
        self.assertEqual(stopped["fills"][-1]["price"], stopped["market"]["bid"])

    def test_restart_persists_orders_positions_and_idempotency(self):
        started = self.engine.start(config(), "click-1")
        prior = self.engine.step("XAUUSD", "2484")
        self.engine.close()
        self.engine = Engine(self.path)
        recovered = self.engine.state()
        self.assertEqual(prior["orders"], recovered["orders"])
        self.assertEqual(prior["positions"], recovered["positions"])
        replay = self.engine.start(config(), "click-1")
        self.assertEqual(replay["strategy_id"], started["strategy_id"])
        self.assertTrue(replay["idempotent_replay"])

    def test_concurrent_cancel_and_profit_never_leave_pending_orders(self):
        started = self.engine.start(config(), "click-1")
        self.engine.step("XAUUSD", "2484")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.engine.cancel, started["strategy_id"]), executor.submit(self.engine.step, "XAUUSD", "2491")]
            for future in futures:
                future.result()
        state = self.engine.state()
        self.assertEqual(state["selected_strategy"]["status"], "stopped")
        self.assertFalse(any(o["status"] == "pending" for o in state["orders"]))

    def test_ineligible_cells_wait_for_price_before_placement(self):
        started = self.engine.start(config(), "click-1")
        self.assertEqual(started["selected_strategy"]["pending_count"], 2)
        self.assertEqual(started["selected_strategy"]["waiting_count"], 2)
        moved = self.engine.step("XAUUSD", "2496")
        self.assertEqual(moved["selected_strategy"]["pending_count"], 4)
        self.assertEqual(moved["positions"], [])

    def test_state_is_detached_and_templates_persist(self):
        state = self.engine.state()
        state["market"]["last"] = "0"
        self.assertNotEqual(self.engine.state()["market"]["last"], "0")
        template = self.engine.save_template("黄金网格", config())
        self.engine.close()
        self.engine = Engine(self.path)
        self.assertEqual(self.engine.list_templates()[0]["id"], template["id"])
        self.engine.delete_template(template["id"])
        self.assertEqual(self.engine.list_templates(), [])

    def test_insufficient_margin_rejected_without_creating_strategy(self):
        with self.assertRaisesRegex(ValueError, "保证金不足"):
            self.engine.start(config(volume="100"), "large")
        self.assertEqual(self.engine.state()["strategies"], [])

    def test_short_position_profit(self):
        self.engine.start(config(direction="short"), "short-1")
        opened = self.engine.step("XAUUSD", "2491")
        self.assertEqual(len(opened["positions"]), 1)
        closed = self.engine.step("XAUUSD", "2484")
        self.assertEqual(len(closed["positions"]), 0)
        self.assertEqual(closed["account"]["realized_pnl"], "5.00")

    def test_preview_and_start_agree_at_300_initial_orders(self):
        for index, (symbol, lower, upper) in enumerate((("XAUUSD", "1000", "1100"), ("EURUSD", "0.90", "1.00"), ("USOIL", "50", "60"))):
            cfg = config(symbol=symbol, lower_price=lower, upper_price=upper, grid_count=100)
            self.assertTrue(self.engine.preview(cfg)["can_start"])
            self.engine.start(cfg, f"capacity-{index}")
        state = self.engine.state()
        self.assertEqual(sum(order["status"] == "pending" for order in state["orders"]), 300)
        candidate = config(symbol="NAS100", lower_price="15000", upper_price="16000", volume="0.1", grid_count=100)
        preview = self.engine.preview(candidate)
        self.assertTrue(preview["sufficient_margin"])
        self.assertFalse(preview["sufficient_capacity"])
        self.assertFalse(preview["can_start"])
        self.assertEqual(preview["capacity"]["available"], 0)
        self.assertTrue(any("300" in warning and "名额" in warning for warning in preview["warnings"]))
        with self.assertRaisesRegex(ValueError, "300"):
            self.engine.start(candidate, "capacity-rejected")
        self.assertEqual(len(self.engine.state()["strategies"]), 3)

    def test_waiting_cells_and_stopped_residual_positions_reserve_capacity(self):
        strategy_ids = []
        for index, (symbol, lower, upper) in enumerate((("XAUUSD", "2400", "2600"), ("EURUSD", "1.07000", "1.09500"), ("USOIL", "70", "85"))):
            cfg = config(symbol=symbol, lower_price=lower, upper_price=upper, grid_count=100)
            strategy_ids.append(self.engine.start(cfg, f"waiting-capacity-{index}")["strategy_id"])
        state = self.engine.state()
        self.assertLess(sum(order["status"] == "pending" for order in state["orders"]), 300)
        self.assertGreater(sum(strategy["waiting_count"] for strategy in state["strategies"]), 0)
        candidate = config(symbol="NAS100", lower_price="19000", upper_price="21000", volume="0.1", grid_count=100)
        preview = self.engine.preview(candidate)
        self.assertEqual(preview["capacity"]["reserved"], 300)
        self.assertFalse(preview["can_start"])
        with self.assertRaisesRegex(ValueError, "300"):
            self.engine.start(candidate, "waiting-capacity-rejected")

        # Stopping releases waiting/pending cells, while retained positions still
        # occupy capacity. A smaller strategy can use exactly the remaining slots.
        self.engine.step("XAUUSD", "2399")
        stopped = self.engine.cancel(strategy_ids[0])
        residual_count = len(stopped["positions"])
        self.assertGreater(residual_count, 0)
        preview = self.engine.preview(candidate)
        self.assertEqual(preview["capacity"]["reserved"], 200 + residual_count)
        self.assertFalse(preview["can_start"])
        smaller = candidate.model_copy(update={"grid_count": preview["capacity"]["available"]})
        self.assertTrue(self.engine.preview(smaller)["can_start"])
        accepted = self.engine.start(smaller, "waiting-capacity-exact")
        self.assertEqual(accepted["selected_strategy"]["status"], "running")
        self.assertEqual(self.engine.preview(candidate)["capacity"]["reserved"], 300)


if __name__ == "__main__":
    unittest.main()
