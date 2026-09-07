import type { GridConfig } from './types.ts'

export type PlanMode = 'count' | 'range' | 'volume' | 'evaluate'
export type CostProfile = { symbol: string; fee_per_lot: string | null; minimum_fee: string | null; category_code: string | null; source: 'gate'; checked_at: number; notes: string[] }
export type PlanResult = {
  feasible: boolean; reason: string | null; config: GridConfig | null;
  cells: { index: number; entry_price: string; take_profit: string; gross_profit: string; fee: string; spread_cost: string; slippage_cost: string; net_profit: string }[];
  summary: { grid_count: number; volume: string; total_volume: string; min_net_profit: string; max_net_profit: string; min_gross_profit: string; max_gross_profit: string; target_satisfied: boolean | null } | null;
  costs: { spread_budget: string; slippage_budget: string; round_trip_fee_per_lot: string; minimum_fee: string } | null;
  constraints: string[]; warnings: string[]; estimate_notice: string;
}

/** Applying a calculation edits a draft only, and must preserve its protections. */
export function applyPlan(current: GridConfig, planned: GridConfig, locked: boolean): GridConfig {
  if (locked) throw new Error('当前策略参数已锁定。可继续试算，停止策略后再应用。')
  if (current.symbol !== planned.symbol) throw new Error('交易品种已变化，请重新计算。')
  const lower = Number(planned.lower_price), upper = Number(planned.upper_price)
  if (!Number.isFinite(lower) || !Number.isFinite(upper) || lower <= 0 || upper <= lower || !Number.isFinite(Number(planned.volume)) || Number(planned.volume) <= 0 || !Number.isInteger(planned.grid_count) || planned.grid_count < 2 || planned.grid_count > 100) throw new Error('计算结果无效，请重新计算。')
  const stop = current.stop_loss ? Number(current.stop_loss) : null
  if (stop !== null && (!Number.isFinite(stop) || stop <= 0 || (planned.direction === 'long' ? stop >= lower : stop <= upper))) throw new Error('新范围与现有止损价冲突，请先调整止损价后再应用。')
  return { ...current, lower_price: planned.lower_price, upper_price: planned.upper_price, grid_count: planned.grid_count, volume: planned.volume, direction: planned.direction, spacing: planned.spacing }
}

export function pickedRange(first: string, second: string): { lower_price: string; upper_price: string } {
  const a = Number(first), b = Number(second)
  if (!Number.isFinite(a) || !Number.isFinite(b) || a <= 0 || b <= 0 || a === b) throw new Error('请选择两个不同的有效价格。')
  return a < b ? { lower_price: first, upper_price: second } : { lower_price: second, upper_price: first }
}
