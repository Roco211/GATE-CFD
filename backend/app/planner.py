"""Pure, cost-budgeted grid planning. This module never contacts an exchange.

Prices in the draft are native order prices. The requested spread deduction is
an ADDITIONAL conservative planning reserve, not another exchange charge on an
already executable entry/exit pair. Every result is a budget estimate.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, localcontext
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


D = Decimal
ZERO = D(0)
MAX_VALUE = D("1e15")
INPUT_QUANTUM = D("1e-16")
NOTICE = "按输入成本预算计算，不保证实际净收益；实际成交、手续费、滑点、隔夜费及汇率可能改变结果。"
Mode = Literal["count", "range", "volume", "evaluate"]


def _decimal(value: Any, label: str = "数值") -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是有效数字")
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label}必须是有效数字") from exc
    if not result.is_finite() or abs(result) > MAX_VALUE:
        raise ValueError(f"{label}必须是有限数字且不超过 10^15")
    if result.as_tuple().exponent < -16:
        raise ValueError(f"{label}最多支持 16 位小数")
    return result


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlannerCosts(_Model):
    # An omitted spread can be derived only from a fresh real bid/ask quote.
    spread_budget: Decimal | None = None
    slippage_budget: Decimal
    round_trip_fee_per_lot: Decimal
    minimum_fee: Decimal
    fee_source: Literal["user", "gate"] = "user"

    @field_validator("spread_budget", "slippage_budget", "round_trip_fee_per_lot", "minimum_fee", mode="before")
    @classmethod
    def valid_cost(cls, value: Any) -> Any:
        if value is None:
            return value
        number = _decimal(value, "成本预算")
        if number < 0:
            raise ValueError("成本预算不得为负数；确认无此费用时可显式填写 0")
        return number


class PlannerRequest(_Model):
    mode: Mode
    symbol: str = Field(min_length=1, max_length=80)
    direction: Literal["long", "short"] = "long"
    spacing: Literal["arithmetic", "geometric"] = "arithmetic"
    lower_price: Decimal | None = None
    upper_price: Decimal | None = None
    grid_count: int | None = Field(default=None, ge=2, le=100, strict=True)
    volume: Decimal | None = None
    target_net_profit: Decimal | None = None
    anchor: Literal["center", "lower", "upper"] = "center"
    anchor_price: Decimal | None = None
    repeat: bool = True
    stop_loss: Decimal | None = None
    costs: PlannerCosts

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("请选择品种")
        return value

    @field_validator("lower_price", "upper_price", "volume", "target_net_profit", "anchor_price", "stop_loss", mode="before")
    @classmethod
    def valid_number(cls, value: Any) -> Any:
        if value is None:
            return None
        number = _decimal(value)
        if number <= 0:
            raise ValueError("价格、手数及目标净预算必须大于 0")
        return number

    @model_validator(mode="after")
    def mode_inputs(self) -> "PlannerRequest":
        required = {
            "count": ("lower_price", "upper_price", "volume", "target_net_profit"),
            "range": ("grid_count", "volume", "target_net_profit"),
            "volume": ("lower_price", "upper_price", "grid_count", "target_net_profit"),
            "evaluate": ("lower_price", "upper_price", "grid_count", "volume"),
        }[self.mode]
        missing = [key for key in required if getattr(self, key) is None]
        if missing:
            raise ValueError("该规划模式缺少参数：" + "、".join(missing))
        if self.lower_price is not None and self.upper_price is not None and self.lower_price >= self.upper_price:
            raise ValueError("价格上限必须大于下限")
        return self


class GridDraft(_Model):
    symbol: str
    direction: Literal["long", "short"]
    spacing: Literal["arithmetic", "geometric"]
    lower_price: Decimal
    upper_price: Decimal
    volume: Decimal
    grid_count: int
    repeat: bool
    stop_loss: Decimal | None


class PlannerCell(_Model):
    index: int
    entry_price: Decimal
    take_profit: Decimal
    price_gap: Decimal
    gross_profit: Decimal
    fee: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    net_profit: Decimal


class ResolvedCosts(_Model):
    spread_budget: Decimal
    slippage_budget: Decimal
    round_trip_fee_per_lot: Decimal
    minimum_fee: Decimal
    spread_source: Literal["user", "gate_quote"]
    fee_source: Literal["user", "gate"]
    slippage_source: Literal["user"] = "user"
    currency: Literal["USD"] = "USD"
    reference_basis: Literal["native_order_prices"] = "native_order_prices"
    spread_treatment: Literal["additional_conservative_budget"] = "additional_conservative_budget"


class PlannerSummary(_Model):
    grid_count: int
    volume: Decimal
    total_volume: Decimal
    min_price_gap: Decimal
    max_price_gap: Decimal
    min_gross_profit: Decimal
    max_gross_profit: Decimal
    min_net_profit: Decimal
    max_net_profit: Decimal
    target_net_profit: Decimal | None
    target_satisfied: bool | None


class PlannerResponse(_Model):
    feasible: bool
    mode: Mode
    reason: str | None = None
    reason_code: str | None = None
    config: GridDraft | None = None
    levels: list[Decimal] = Field(default_factory=list)
    cells: list[PlannerCell] = Field(default_factory=list)
    summary: PlannerSummary | None = None
    costs: ResolvedCosts | None = None
    constraints: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    requires_gate_volume_validation: bool = False
    estimate_notice: str = NOTICE


class _NoPlan(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _positive_spec(spec: dict[str, Any], key: str) -> Decimal:
    try:
        value = _decimal(spec.get(key), key)
    except ValueError as exc:
        raise _NoPlan("spec_incomplete", "Gate 品种规格缺失或无效，无法规划") from exc
    if value <= 0:
        raise _NoPlan("spec_incomplete", "Gate 品种规格缺失或无效，无法规划")
    return value


def _spec_values(request: PlannerRequest, spec: dict[str, Any] | None) -> tuple[Decimal, Decimal, Decimal, Decimal | None, Decimal]:
    if not spec or spec.get("source") != "gate" or spec.get("complete") is not True or spec.get("symbol") != request.symbol:
        raise _NoPlan("spec_unavailable", "请先取得该品种完整的 Gate 实盘规格")
    currency = spec.get("settlement_currency", spec.get("currency"))
    if currency != "USD" or spec.get("currency", currency) != currency:
        raise _NoPlan("unsupported_currency", "当前规划器仅支持美元结算品种；缺少可靠汇率时不换算净预算")
    tick, minimum, maximum, contract = (_positive_spec(spec, key) for key in ("tick_size", "volume_min", "volume_max", "contract_size"))
    step = None if spec.get("volume_step") is None else _positive_spec(spec, "volume_step")
    if maximum < minimum:
        raise _NoPlan("spec_incomplete", "Gate 手数上下限无效")
    return tick, minimum, maximum, step, contract


def _quote(request: PlannerRequest, market: dict[str, Any] | None, now: float) -> tuple[Decimal, Decimal]:
    if not market or market.get("source") != "gate" or market.get("symbol") != request.symbol or market.get("stale") is True:
        raise _NoPlan("quote_unavailable", "缺少该品种新鲜的 Gate 买卖报价，请填写价差预算和参考价或刷新行情")
    try:
        bid, ask = _decimal(market.get("bid")), _decimal(market.get("ask"))
        received = float(market.get("received_at"))
    except (ValueError, TypeError, OverflowError) as exc:
        raise _NoPlan("quote_unavailable", "Gate 报价不完整，不能自动取得价差或参考价") from exc
    if bid <= 0 or ask < bid or not (-1 <= now - received <= 5):
        raise _NoPlan("quote_unavailable", "Gate 报价无效或已过期，不能自动取得价差或参考价")
    return bid, ask


def _costs(request: PlannerRequest, market: dict[str, Any] | None, now: float) -> ResolvedCosts:
    spread = request.costs.spread_budget
    if spread is None:
        bid, ask = _quote(request, market, now)
        spread = ask - bid
    return ResolvedCosts(spread_budget=spread, slippage_budget=request.costs.slippage_budget,
                         round_trip_fee_per_lot=request.costs.round_trip_fee_per_lot,
                         minimum_fee=request.costs.minimum_fee,
                         spread_source="gate_quote" if request.costs.spread_budget is None else "user",
                         fee_source=request.costs.fee_source)


def _round(value: Decimal, tick: Decimal, direction: str = ROUND_HALF_UP) -> Decimal:
    return (value / tick).to_integral_value(rounding=direction) * tick


def _levels(lower: Decimal, upper: Decimal, count: int, spacing: str, tick: Decimal) -> list[Decimal]:
    if lower < tick or upper > MAX_VALUE or upper <= lower:
        raise _NoPlan("range_out_of_bounds", "无法在正价格及支持范围内构造该区间")
    with localcontext() as context:
        # Match the native grid preview's Decimal precision and tick rounding.
        context.prec = 48
        if spacing == "arithmetic":
            distance = (upper - lower) / count
            result = [_round(lower + distance * index, tick) for index in range(count + 1)]
        else:
            ratio = upper / lower
            result = [_round(lower * context.power(ratio, D(index) / count), tick) for index in range(count + 1)]
    result[0], result[-1] = lower, upper
    if any(a >= b for a, b in zip(result, result[1:])):
        raise _NoPlan("collapsed_levels", "取整后有重复格位，请扩大区间或减少格数")
    return result


def _volume_check(volume: Decimal, minimum: Decimal, maximum: Decimal, step: Decimal | None) -> None:
    if volume < minimum or volume > maximum:
        raise _NoPlan("volume_out_of_bounds", f"所需或输入手数不在 Gate 允许范围 {minimum} 至 {maximum} 内")
    if step is not None and volume % step:
        raise _NoPlan("volume_step", f"手数必须符合 Gate 公布的步长 {step}")


def _cells(levels: list[Decimal], volume: Decimal, contract: Decimal, costs: ResolvedCosts, direction: str) -> list[PlannerCell]:
    fee = max(costs.round_trip_fee_per_lot * volume, costs.minimum_fee)
    spread_cost = costs.spread_budget * contract * volume
    slippage_cost = costs.slippage_budget * contract * volume
    rows = []
    for index, (lower, upper) in enumerate(zip(levels, levels[1:])):
        gross = (upper - lower) * contract * volume
        entry, target = (lower, upper) if direction == "long" else (upper, lower)
        rows.append(PlannerCell(index=index, entry_price=entry, take_profit=target, price_gap=upper - lower,
                                gross_profit=gross, fee=fee, spread_cost=spread_cost,
                                slippage_cost=slippage_cost, net_profit=gross - fee - spread_cost - slippage_cost))
    return rows


def _sufficient(levels: list[Decimal], gap: Decimal) -> bool:
    return all(b - a >= gap for a, b in zip(levels, levels[1:]))


def _bounds(anchor: Decimal, kind: str, width_ticks: int, tick: Decimal) -> tuple[Decimal, Decimal]:
    if kind == "lower":
        return anchor, anchor + width_ticks * tick
    if kind == "upper":
        return anchor - width_ticks * tick, anchor
    # An even tick width preserves the exact center and tick-aligned endpoints.
    width_ticks += width_ticks % 2
    half = width_ticks * tick / 2
    return anchor - half, anchor + half


def _range(request: PlannerRequest, anchor: Decimal, tick: Decimal, required_gap: Decimal) -> list[Decimal]:
    count = request.grid_count
    assert count is not None
    gap = _round(required_gap, tick, ROUND_CEILING)
    width_ticks = int((gap / tick) * count)
    if request.spacing == "arithmetic":
        return _levels(*_bounds(anchor, request.anchor, width_ticks, tick), count, request.spacing, tick)

    # For fixed upper/center anchors, a geometric grid's smallest spacing has
    # a maximum before the lower bound reaches zero. Widening past that point
    # makes the first spacing smaller, so an unbounded doubling search is wrong.
    with localcontext() as context:
        context.prec = 64
        if request.anchor == "lower":
            # gap is already a whole number of ticks. Rounding is monotone and
            # commutes with an integer tick translation, so continuous gaps
            # >= gap stay >= gap after rounding. An unconditional extra tick
            # would incorrectly reject valid grids at the maximum price.
            ratio = 1 + gap / anchor
            if count * context.ln(ratio) > context.ln(MAX_VALUE / anchor):
                raise _NoPlan("range_out_of_bounds", "达到目标需要的等比区间超过支持价格范围")
            upper = _round(anchor * context.power(ratio, count), tick, ROUND_CEILING)
            return _levels(anchor, upper, count, request.spacing, tick)
        if request.anchor == "upper":
            peak_ratio = D(count) / (count - 1)
            peak_lower = anchor / context.power(peak_ratio, count)
            peak_width = anchor - peak_lower
        else:
            lo, hi = D(count) / (count - 1), D(count + 1) / (count - 1)
            for _ in range(100):
                ratio = (lo + hi) / 2
                derivative = context.power(ratio, count - 1) * ((count - 1) * ratio - count) - 1
                if derivative < 0:
                    lo = ratio
                else:
                    hi = ratio
            peak_ratio = (lo + hi) / 2
            peak_lower = 2 * anchor / (1 + context.power(peak_ratio, count))
            peak_width = 2 * (anchor - peak_lower)
        peak_gap = peak_lower * (peak_ratio - 1)
        if peak_gap + tick / 2 < gap:
            raise _NoPlan("anchor_capacity", "该固定锚点下的等比区间无法提供足够的最小格距；请提高参考价、降低目标或减少格数")

        # First try a spacing with a full tick of rounding reserve, then the
        # requested spacing. A few neighboring endpoints cover tick ties at the
        # continuous optimum. Every candidate is checked across all cells.
        widths: list[int] = []
        for desired in (gap + tick, gap, gap - tick / 2):
            lo, hi = D(0), peak_width
            if desired <= peak_gap:
                for _ in range(100):
                    width = (lo + hi) / 2
                    lower = anchor - width if request.anchor == "upper" else anchor - width / 2
                    upper = anchor if request.anchor == "upper" else anchor + width / 2
                    first_gap = lower * (context.power(upper / lower, D(1) / count) - 1)
                    if first_gap < desired:
                        lo = width
                    else:
                        hi = width
                widths.append(int((hi / tick).to_integral_value(rounding=ROUND_CEILING)))
        widths.append(int((peak_width / tick).to_integral_value(rounding=ROUND_HALF_UP)))
    tried: set[int] = set()
    for width in widths:
        for offset in range(-2, 2 * count + 5):
            candidate = width + offset
            if candidate in tried or candidate <= 0:
                continue
            tried.add(candidate)
            try:
                levels = _levels(*_bounds(anchor, request.anchor, candidate, tick), count, request.spacing, tick)
            except _NoPlan:
                continue
            if _sufficient(levels, required_gap):
                return levels
    raise _NoPlan("anchor_rounding", "该锚点下未能构造逐格达到目标的等比区间；请调整参考价、目标或格数")


def plan_grid(request: PlannerRequest | dict[str, Any], spec: dict[str, Any] | None,
              market: dict[str, Any] | None = None, *, now: float | None = None) -> PlannerResponse:
    """Return a reviewable draft; invalid shape raises Pydantic validation error.

    Missing real specifications/quotes and infeasible mathematical constraints
    return feasible=False with a stable reason_code and a Chinese reason.
    """
    request = request if isinstance(request, PlannerRequest) else PlannerRequest.model_validate(request)
    result = PlannerResponse(feasible=False, mode=request.mode)
    try:
        with localcontext() as context:
            context.prec = 80
            tick, minimum, maximum, step, contract = _spec_values(request, spec)
            result.requires_gate_volume_validation = step is None
            result.constraints = [f"格数范围：2 至 100；价格步长：{tick}", f"每格手数范围：{minimum} 至 {maximum}", "仅使用美元结算的真实 Gate 品种规格；杠杆与账户保证金不由本规划器修改"]
            result.warnings = ["价差预算是原生委托价格之外额外预留的保守预算，并非声称 Gate 会另扣一次价差。", "往返滑点须覆盖进场与退出；每轮佣金为 max(每手单轮佣金 × 手数，每单最低佣金)，开仓收取的官方佣金不重复乘 2。", "未包含隔夜持仓费用；本结果只生成参数草稿，启动前仍须核验行情、账户、保证金与 Gate 委托限制。"]
            if step is None:
                result.warnings.append("Gate 未公布手数步长；手数是数学候选值，是否可委托仍需 Gate 校验，未把最小手数当作步长。")
            else:
                result.constraints.append(f"手数步长：{step}")
            costs = result.costs = _costs(request, market, time.time() if now is None else now)
            for key in ("lower_price", "upper_price", "stop_loss"):
                value = getattr(request, key)
                if value is not None and value % tick:
                    raise _NoPlan("price_step", f"{key} 必须符合价格步长 {tick}")
            volume = request.volume
            if request.mode != "volume":
                assert volume is not None
                _volume_check(volume, minimum, maximum, step)
            target = request.target_net_profit
            if request.mode == "range":
                anchor = request.anchor_price
                if anchor is None:
                    bid, ask = _quote(request, market, time.time() if now is None else now)
                    anchor = _round((bid + ask) / 2, tick)
                    result.constraints.append(f"参考价取新鲜买卖中间价并按价格步长取整：{anchor}")
                elif anchor % tick:
                    raise _NoPlan("price_step", f"参考价必须符合价格步长 {tick}")
                fee = max(costs.round_trip_fee_per_lot * volume, costs.minimum_fee)
                required_gap = costs.spread_budget + costs.slippage_budget + (target + fee) / (contract * volume)
                levels = _range(request, anchor, tick, required_gap)
            elif request.mode == "count":
                levels = []
                for count in range(100, 1, -1):
                    try:
                        candidate = _levels(request.lower_price, request.upper_price, count, request.spacing, tick)
                    except _NoPlan:
                        continue
                    if min(row.net_profit for row in _cells(candidate, volume, contract, costs, request.direction)) >= target:
                        levels = candidate
                        break
                if not levels:
                    raise _NoPlan("target_unreachable", "在该区间、手数和成本预算下，即使 2 格也无法逐格达到目标")
            else:
                levels = _levels(request.lower_price, request.upper_price, request.grid_count, request.spacing, tick)
            if request.mode == "volume":
                min_gap = min(b - a for a, b in zip(levels, levels[1:]))
                before_fee_per_lot = (min_gap - costs.spread_budget - costs.slippage_budget) * contract
                after_rate_per_lot = before_fee_per_lot - costs.round_trip_fee_per_lot
                if before_fee_per_lot <= 0 or after_rate_per_lot <= 0:
                    raise _NoPlan("costs_exceed_spacing", "最小格距扣除价差、滑点和每手佣金后不为正；增加手数也无法达到目标")
                needed = max(minimum, target / after_rate_per_lot, (target + costs.minimum_fee) / before_fee_per_lot)
                volume = _round(needed, step or INPUT_QUANTUM, ROUND_CEILING)
                _volume_check(volume, minimum, maximum, step)
            rows = _cells(levels, volume, contract, costs, request.direction)
            minimum_net = min(row.net_profit for row in rows)
            if request.mode != "evaluate" and target is not None and minimum_net < target:
                raise _NoPlan("target_unreachable", "取整后的最小每格净预算未达到目标，请调整参数")
            lower, upper = levels[0], levels[-1]
            if request.stop_loss is not None and ((request.direction == "long" and request.stop_loss >= lower) or (request.direction == "short" and request.stop_loss <= upper)):
                raise _NoPlan("stop_loss_range", "止损价必须在生成区间之外：做多低于下限，做空高于上限")
            result.config = GridDraft(symbol=request.symbol, direction=request.direction, spacing=request.spacing,
                                      lower_price=lower, upper_price=upper, volume=volume, grid_count=len(rows),
                                      repeat=request.repeat, stop_loss=request.stop_loss)
            result.levels, result.cells = levels, rows
            result.summary = PlannerSummary(grid_count=len(rows), volume=volume, total_volume=volume * len(rows),
                                            min_price_gap=min(row.price_gap for row in rows), max_price_gap=max(row.price_gap for row in rows),
                                            min_gross_profit=min(row.gross_profit for row in rows), max_gross_profit=max(row.gross_profit for row in rows),
                                            min_net_profit=minimum_net, max_net_profit=max(row.net_profit for row in rows),
                                            target_net_profit=target, target_satisfied=None if target is None else minimum_net >= target)
            result.feasible = True
            if minimum_net <= 0:
                result.warnings.append("至少一格的净预算不为正；当前成本预算下不存在正的每格收益空间。")
            elif target is not None and minimum_net < target:
                result.warnings.append("评估结果低于输入的目标净预算；请扩大格距、调整手数或重新规划。")
            return result
    except _NoPlan as exc:
        result.reason, result.reason_code = str(exc), exc.code
        return result
