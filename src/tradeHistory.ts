import type { TradeRow } from './types'

export function fillTimestamp(value: unknown): number | null {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null
  const text = String(value).trim()
  const numeric = typeof value === 'number' ? value : /^[+-]?\d+(?:\.\d+)?$/.test(text) ? Number(text) : NaN
  const stamp = Number.isFinite(numeric) ? numeric < 1e12 ? numeric * 1000 : numeric : Date.parse(text)
  return Number.isFinite(stamp) && stamp > 0 && stamp <= 8.64e15 ? stamp : null
}

/** Gate history may arrive in either direction; never infer chronology from array order. */
export function newestFills(symbol: string, rows: readonly TradeRow[]): TradeRow[] {
  return rows.filter(row => String(row.symbol ?? '').trim().toUpperCase() === symbol.trim().toUpperCase())
    .map((row, index) => ({ row, index, stamp: fillTimestamp(row.time) }))
    .sort((a, b) => (b.stamp ?? -Infinity) - (a.stamp ?? -Infinity) || a.index - b.index)
    .map(item => item.row)
}

export function fillTime(value: unknown): string {
  const stamp = fillTimestamp(value)
  return stamp === null ? '—' : new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(stamp))
}

export function fillTypeLabel(row: Pick<TradeRow, 'type' | 'exit_confirmed'>): string {
  if (row.type === 'awaiting_confirmation' || row.exit_confirmed === false) return '已平仓 · 原因待核对'
  if (row.type === 'target_exit') return row.exit_confirmed === true ? '止盈平仓' : '已平仓 · 原因待核对'
  return ({ open: '开仓', stop_loss: '止损平仓', ordinary_close: '普通平仓', manual_close: '普通平仓', closed: '已平仓', liquidation: '强制平仓' } as Record<string, string>)[row.type ?? ''] ?? '成交'
}
