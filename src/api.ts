import type { TradeRow } from './types'

export async function api<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
  const timer = AbortSignal.timeout(16000)
  const response = await fetch(`/api${path}`, {
    method, headers: { 'Content-Type': 'application/json', 'X-Grid-Client': 'grid-studio' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: signal ? AbortSignal.any([signal, timer]) : timer,
  })
  const data = await response.json().catch(() => ({ detail: '后台返回了无法识别的数据，请重新连接。' }))
  if(response.status===401 && data.code==='access_required')window.location.replace('/login')
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求未完成，请检查输入。')
  return data as T
}

/** Management is allowed only for explicitly attributed Gate rows. */
export function managedTradeId(row: TradeRow | null | undefined, kind: 'order' | 'position', action: 'modify' | 'cancel' = 'modify'): string | null {
  if (!row || row.source !== 'gate' || row.managed !== true || !row.strategy_id) return null
  const id = String(kind === 'order' ? row.order_id ?? row.real_order_id ?? row.remote_order_id ?? row.id : row.position_id ?? row.id)
  if (!/^[1-9]\d*$/.test(id)) return null
  if (kind === 'order') {
    const partial = ['partial', 'partially_filled'].includes(row.status ?? '') || String(row.remote_state) === '3'
    if (partial && (action !== 'cancel' || String(row.remote_state) !== '3')) return null
    if (!['pending', 'open', 'partial', 'partially_filled'].includes(row.status ?? '') || ['2', '4', '5'].includes(String(row.remote_state))) return null
  }
  if (kind === 'position' && (['closed', 'cancelled', 'canceled', 'done', 'unknown', 'reconciling'].includes(row.status ?? '') || (row.volume != null && (!Number.isFinite(Number(row.volume)) || Number(row.volume) <= 0)))) return null
  return id
}

export type ProtectionInput = { takeProfit: string; stopLoss: string; clearTakeProfit: boolean; clearStopLoss: boolean }
/** Omission means keep; only explicit zero or the clear control clears a leg. */
export function protectionPatch(input: ProtectionInput): { price_tp?: string; price_sl?: string } {
  const body: { price_tp?: string; price_sl?: string } = {}
  for (const [key, raw, clear] of [['price_tp', input.takeProfit, input.clearTakeProfit], ['price_sl', input.stopLoss, input.clearStopLoss]] as const) {
    const value = raw.trim()
    if (clear) { body[key] = '0'; continue }
    if (!value) continue
    if (!/^\d+(?:\.\d+)?$/.test(value) || !Number.isFinite(Number(value))) throw new Error('止盈止损请输入有效价格；留空保持，输入 0 清空。')
    body[key] = Number(value) === 0 ? '0' : value
  }
  return body
}
export const number = (value: unknown, digits = 2) => value === undefined || value === null || value === '' || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
export function clockTime(value: string | number | undefined) {
  if (!value) return '—'
  const parsed = typeof value === 'string' && /^\d+(\.\d+)?$/.test(value) ? Number(value) : value
  const date = new Date(typeof parsed === 'number' ? (parsed < 1e12 ? parsed * 1000 : parsed) : parsed)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString('zh-CN', { hour12: false })
}
