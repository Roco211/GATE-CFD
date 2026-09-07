"""Planner tests use constructed real-schema specifications, never an account."""

from copy import deepcopy
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from app.planner import PlannerRequest, PlannerResponse, plan_grid


SPEC = {"symbol": "XAUUSD", "source": "gate", "complete": True,
        "currency": "USD", "settlement_currency": "USD", "tick_size": "0.01",
        "volume_min": "0.01", "volume_max": "100", "volume_step": "0.01",
        "contract_size": "100", "leverage": "500"}
NOW = 1_800_000_000
MARKET = {"symbol": "XAUUSD", "source": "gate", "bid": "109.80", "ask": "110.20",
          "received_at": NOW, "stale": False}
COSTS = {"spread_budget": "0.30", "slippage_budget": "0.20",
         "round_trip_fee_per_lot": "6", "minimum_fee": "0"}


def request(mode="evaluate", **overrides):
    return {"mode": mode, "symbol": "XAUUSD", "lower_price": "100", "upper_price": "120",
            "grid_count": 4, "volume": "0.01", "costs": deepcopy(COSTS), **overrides}


def plan(payload, spec=None, market=None):
    return plan_grid(payload, deepcopy(SPEC) if spec is None else spec, market, now=NOW)


def assert_math(result):
    assert result.feasible, (result.reason_code, result.reason)
    assert result.config.grid_count + 1 == len(result.levels)
    assert len(result.cells) == result.config.grid_count
    assert all(price % D(SPEC["tick_size"]) == 0 for price in result.levels)
    costs, volume = result.costs, result.config.volume
    for index, row in enumerate(result.cells):
        gap = result.levels[index + 1] - result.levels[index]
        assert gap > 0
        assert row.gross_profit == gap * 100 * volume
        assert row.fee == max(costs.round_trip_fee_per_lot * volume, costs.minimum_fee)
        assert row.net_profit == row.gross_profit - row.fee - row.spread_cost - row.slippage_cost
    assert result.summary.min_net_profit == min(row.net_profit for row in result.cells)
    if result.mode != "evaluate":
        assert all(row.net_profit >= result.summary.target_net_profit for row in result.cells)


def test_evaluate_explicit_single_round_costs_and_decimal_serialization():
    payload = request()
    result = plan(payload)
    assert_math(result)
    row = result.cells[0]
    assert (row.gross_profit, row.fee, row.spread_cost, row.slippage_cost, row.net_profit) == (
        D("5"), D("0.06"), D("0.30"), D("0.20"), D("4.44"))
    assert result.costs.reference_basis == "native_order_prices"
    assert result.costs.spread_treatment == "additional_conservative_budget"
    data = result.model_dump(mode="json")
    assert isinstance(data["cells"][0]["net_profit"], str)
    assert PlannerResponse.model_validate(data) == result
    assert payload == request()  # no mutation of caller's inputs


def test_count_returns_largest_feasible_count_after_rounding_every_geometric_cell():
    payload = request("count", target_net_profit="0.69", spacing="geometric")
    result = plan(payload)
    assert_math(result)
    for count in range(result.config.grid_count + 1, 101):
        evaluated = plan({**payload, "mode": "evaluate", "grid_count": count})
        assert not evaluated.feasible or evaluated.summary.min_net_profit < D("0.69")
    assert len(set(row.price_gap for row in result.cells)) > 1


def test_arithmetic_count_budget_and_count_limits():
    result = plan(request("count", target_net_profit="1.44"))
    assert_math(result)
    assert result.config.grid_count == 10
    assert result.summary.min_net_profit == D("1.44")
    capped = plan(request("count", upper_price="1000", target_net_profit="0.01"))
    assert capped.config.grid_count == 100
    no_plan = plan(request("count", upper_price="100.01", target_net_profit="0.01"))
    assert not no_plan.feasible
    assert no_plan.reason_code == "target_unreachable"
    assert no_plan.config is None


@pytest.mark.parametrize("spacing", ["arithmetic", "geometric"])
@pytest.mark.parametrize("anchor", ["center", "lower", "upper"])
def test_range_all_anchors_both_spacings_preserve_anchor_and_target(spacing, anchor):
    result = plan(request("range", grid_count=7, target_net_profit="1.111", anchor=anchor,
                          anchor_price="110", spacing=spacing))
    assert_math(result)
    actual_anchor = {"center": (result.config.lower_price + result.config.upper_price) / 2,
                     "lower": result.config.lower_price, "upper": result.config.upper_price}[anchor]
    assert actual_anchor == D("110")
    assert min(row.net_profit for row in result.cells) >= D("1.111")


def test_range_arithmetic_center_odd_width_rounds_outward():
    result = plan(request("range", grid_count=3, target_net_profit="0.01", anchor_price="110",
                          costs={**COSTS, "spread_budget": "0", "slippage_budget": "0", "round_trip_fee_per_lot": "0"}))
    assert_math(result)
    assert result.config.lower_price == D("109.98")
    assert result.config.upper_price == D("110.02")


def test_range_geometric_upper_mathematical_limit_and_exact_rounded_peak():
    no_cost = {key: "0" for key in COSTS}
    at_peak = plan(request("range", spacing="geometric", anchor="upper", anchor_price="100",
                          grid_count=2, target_net_profit="25", costs=no_cost))
    assert_math(at_peak)
    assert at_peak.config.upper_price == D("100")
    impossible = plan(request("range", spacing="geometric", anchor="upper", anchor_price="100",
                             grid_count=2, target_net_profit="25.01", costs=no_cost))
    assert not impossible.feasible
    assert impossible.reason_code == "anchor_capacity"


def test_range_automatic_reference_uses_fresh_gate_mid_and_preserves_tick():
    result = plan(request("range", anchor="center", anchor_price=None, target_net_profit="1"), market=MARKET)
    assert_math(result)
    assert (result.config.lower_price + result.config.upper_price) / 2 == D("110")
    result = plan(request("range", anchor_price=None, target_net_profit="1"), market={**MARKET, "received_at": NOW - 6})
    assert result.reason_code == "quote_unavailable"


def test_volume_ceil_meets_target_and_previous_step_does_not():
    result = plan(request("volume", target_net_profit="10"))
    assert_math(result)
    assert result.config.volume == D("0.03")
    below = plan(request(volume="0.02"))
    assert below.summary.min_net_profit < D("10")


def test_volume_includes_minimum_fee_and_proportional_fee_regions():
    costs = {"spread_budget": "0", "slippage_budget": "0", "round_trip_fee_per_lot": "10", "minimum_fee": "2"}
    minimum_dominated = plan(request("volume", upper_price="104", grid_count=4, target_net_profit="1", costs=costs))
    assert_math(minimum_dominated)
    assert minimum_dominated.config.volume == D("0.03")
    assert minimum_dominated.cells[0].fee == D("2")
    rate_dominated = plan(request("volume", upper_price="104", grid_count=4, target_net_profit="30", costs=costs))
    assert_math(rate_dominated)
    assert rate_dominated.config.volume == D("0.34")
    assert rate_dominated.cells[0].fee == D("3.4")


def test_minimum_fee_is_applied_in_count_and_range_too():
    costs = {**COSTS, "minimum_fee": "2"}
    result = plan(request("count", target_net_profit="1", costs=costs))
    assert_math(result)
    assert result.config.grid_count == 5
    ranged = plan(request("range", anchor="lower", anchor_price="100", grid_count=4,
                          target_net_profit="1", costs=costs))
    assert_math(ranged)
    assert ranged.summary.min_net_profit == D("1")
    assert ranged.config.upper_price == D("114")


def test_unknown_volume_step_is_not_invented_from_minimum():
    spec = {**SPEC, "volume_step": None}
    result = plan(request("volume", target_net_profit="6"), spec)
    assert_math(result)
    assert result.requires_gate_volume_validation is True
    assert result.config.volume % D("0.01") != 0
    assert result.config.volume.as_tuple().exponent >= -16
    assert any("未把最小手数" in warning for warning in result.warnings)
    assert not plan(request(volume="0.015"), SPEC).feasible
    assert plan(request(volume="0.015"), spec).feasible


def test_volume_max_and_unprofitable_gap_return_no_draft():
    too_large = plan(request("volume", target_net_profit="100"), {**SPEC, "volume_max": "0.02"})
    assert too_large.reason_code == "volume_out_of_bounds"
    insufficient_gap = plan(request("volume", upper_price="101", target_net_profit="1"))
    assert insufficient_gap.reason_code == "costs_exceed_spacing"
    assert insufficient_gap.config is None


def test_evaluate_negative_budget_and_optional_target_remain_visible():
    result = plan(request(upper_price="101", target_net_profit="10"))
    assert result.feasible
    assert result.summary.min_net_profit < 0
    assert result.summary.target_satisfied is False
    assert result.cells
    assert any("不为正" in warning for warning in result.warnings)


def test_long_short_have_identical_budget_but_inverse_entry_target():
    long, short = plan(request()), plan(request(direction="short"))
    assert long.summary == short.summary
    for a, b in zip(long.cells, short.cells):
        assert a.entry_price == b.take_profit
        assert a.take_profit == b.entry_price
        assert a.net_profit == b.net_profit


@pytest.mark.parametrize("key", ["slippage_budget", "round_trip_fee_per_lot", "minimum_fee"])
def test_costs_must_be_explicit_never_implicitly_free(key):
    costs = deepcopy(COSTS)
    del costs[key]
    with pytest.raises(ValidationError):
        plan(request(costs=costs))
    with pytest.raises(ValidationError):
        plan(request(costs={**COSTS, key: None}))


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity", True, "-0.1", "1e16", "1e-17"])
def test_invalid_numeric_inputs_fail_before_math(invalid):
    with pytest.raises(ValidationError):
        plan(request(volume=invalid))
    with pytest.raises(ValidationError):
        plan(request(costs={**COSTS, "slippage_budget": invalid}))


@pytest.mark.parametrize("override", [{"grid_count": 1}, {"grid_count": 101}, {"grid_count": True},
                                      {"grid_count": 2.5}, {"lower_price": "0"}, {"lower_price": "121"}])
def test_shape_constraints(override):
    with pytest.raises(ValidationError):
        plan(request(**override))


@pytest.mark.parametrize("spec", [None, {}, {**SPEC, "source": "simulation"}, {**SPEC, "complete": False},
                                  {**SPEC, "symbol": "EURUSD"}, {**SPEC, "contract_size": None},
                                  {**SPEC, "volume_min": "2", "volume_max": "1"}, {**SPEC, "volume_step": "0"}])
def test_only_complete_real_specifications_are_accepted(spec):
    result = plan_grid(request(), spec, now=NOW)
    assert not result.feasible
    assert result.config is None
    assert result.reason_code in {"spec_unavailable", "spec_incomplete"}


@pytest.mark.parametrize("spec", [{**SPEC, "currency": "JPY", "settlement_currency": "JPY"},
                                  {**SPEC, "currency": "USD", "settlement_currency": "JPY"}])
def test_non_usd_is_not_falsely_estimated_in_dollars(spec):
    assert plan(request(), spec).reason_code == "unsupported_currency"


def test_spread_auto_requires_real_fresh_quote_but_explicit_zero_is_supported():
    costs = {**COSTS, "spread_budget": None}
    assert plan(request(costs=costs)).reason_code == "quote_unavailable"
    assert plan(request(costs=costs), market={**MARKET, "source": "simulation"}).reason_code == "quote_unavailable"
    assert plan(request(costs=costs), market={**MARKET, "received_at": NOW - 6}).reason_code == "quote_unavailable"
    assert plan(request(costs=costs), market={**MARKET, "received_at": float("nan")}).reason_code == "quote_unavailable"
    actual = plan(request(costs=costs), market=MARKET)
    assert actual.costs.spread_budget == D("0.40")
    assert actual.costs.spread_source == "gate_quote"
    explicit = plan(request(costs={**COSTS, "spread_budget": "0"}))
    assert explicit.feasible
    assert explicit.cells[0].spread_cost == 0
    assert explicit.costs.spread_source == "user"


def test_prices_must_align_with_tick_and_no_collapsed_grid():
    assert plan(request(lower_price="100.001")).reason_code == "price_step"
    assert plan(request(upper_price="100.02", grid_count=4)).reason_code == "collapsed_levels"
    assert plan(request("range", target_net_profit="1", anchor_price="100.001")).reason_code == "price_step"


def test_stop_loss_is_rechecked_against_generated_range():
    result = plan(request("range", target_net_profit="10", anchor_price="110", stop_loss="109"))
    assert result.reason_code == "stop_loss_range"
    valid = plan(request("range", target_net_profit="1", anchor_price="110", stop_loss="90"))
    assert valid.config.stop_loss == D("90")


def test_forex_small_ticks_high_contract_exact_budget():
    spec = {**SPEC, "symbol": "EURUSD", "tick_size": "0.00001", "contract_size": "100000"}
    result = plan(request("count", symbol="EURUSD", lower_price="1.08000", upper_price="1.09000",
                          target_net_profit="0.5", costs={**COSTS, "spread_budget": "0.00012", "slippage_budget": "0.00003"}), spec)
    assert result.feasible
    assert result.config.grid_count == 14
    assert all(row.net_profit >= D("0.5") for row in result.cells)
    assert all(price % D("0.00001") == 0 for price in result.levels)


def test_large_valid_decimal_inputs_are_bounded_without_float_overflow():
    result = plan(request("range", anchor="lower", anchor_price="1000000000000000",
                          target_net_profit="1000000000000000", spacing="geometric", grid_count=100))
    assert not result.feasible
    assert result.reason_code == "range_out_of_bounds"


def test_geometric_lower_anchor_does_not_invent_an_extra_tick_at_price_ceiling():
    spec = {**SPEC, "tick_size": "1", "contract_size": "1", "volume_min": "1", "volume_step": "1"}
    result = plan(request("range", anchor="lower", anchor_price="250000000000000", volume="1",
                          target_net_profit="250000000000000", grid_count=2, spacing="geometric",
                          costs={key: "0" for key in COSTS}), spec)
    assert result.feasible, result.reason
    assert result.levels == [D("250000000000000"), D("500000000000000"), D("1000000000000000")]
    assert result.summary.min_net_profit == D("250000000000000")
