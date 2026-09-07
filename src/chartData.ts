import type { Market, Preview, TradeRow } from './types'

/** UTC seconds and numeric prices, independent of any chart library. */
export type ChartCandle = {
  time: number
  open: number
  high: number
  low: number
  close: number
  /** Keep these when merging successive quotes, including within one minute. */
  quoteTime?: number
  quoteReceivedAt?: number
}

export type ChartOverlay = {
  id: string
  price: number
  title: string
  color: string
  lineStyle: 'dashed' | 'dotted' | 'solid'
  kind: 'preview' | 'local' | 'order' | 'position' | 'take_profit' | 'stop_loss' | 'order_take_profit' | 'order_stop_loss'
  axisLabelVisible: boolean
}

type Row = Record<string, unknown>
const record = (value: unknown): Row | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Row : null
const positive = (value: unknown): number | null => {
  if (typeof value !== 'string' && typeof value !== 'number') return null
  if (typeof value === 'string' && !/^\s*\+?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?\s*$/i.test(value)) return null
  const result = Number(value)
  return Number.isFinite(result) && result > 0 ? result : null
}

/** Numeric timestamps may be seconds or milliseconds. Zone-less ISO is UTC. */
function utcSeconds(value: unknown): number | null {
  if (typeof value === 'string' && !/^\s*\d+(?:\.\d+)?\s*$/.test(value)) {
    const iso = value.trim()
    if (!/^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/i.test(iso)) return null
    const [year, month, day] = iso.slice(0, 10).split('-').map(Number)
    if (month < 1 || month > 12 || day < 1 || day > new Date(Date.UTC(year, month, 0)).getUTCDate()) return null
    const explicitZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso)
    const timestamp = Date.parse(iso.length > 10 && !explicitZone ? `${iso.replace(' ', 'T')}Z` : iso)
    return Number.isFinite(timestamp) && timestamp > 0 ? timestamp / 1000 : null
  }
  const numeric = positive(value)
  if (numeric === null) return null
  const result = numeric >= 100_000_000_000 ? numeric / 1000 : numeric
  return result <= 8_640_000_000_000 ? result : null
}

/** Sort ascending; for duplicate seconds, the last valid input row wins. */
export function normalizeCandles(input: readonly unknown[] | null | undefined): ChartCandle[] {
  const byTime = new Map<number, ChartCandle>()
  for (const item of input ?? []) {
    const row = record(item)
    if (!row) continue
    const stamp = utcSeconds(row.time)
    const open = positive(row.open), high = positive(row.high), low = positive(row.low), close = positive(row.close)
    if (stamp === null || open === null || high === null || low === null || close === null) continue
    if (low > Math.min(open, close) || high < Math.max(open, close) || high < low) continue
    const candle: ChartCandle = { time: Math.floor(stamp), open, high, low, close }
    const quoteTime = utcSeconds(row.quoteTime), receivedAt = utcSeconds(row.quoteReceivedAt)
    if (quoteTime !== null) candle.quoteTime = quoteTime
    if (receivedAt !== null) candle.quoteReceivedAt = receivedAt
    byTime.set(candle.time, candle)
  }
  return [...byTime.values()].sort((a, b) => a.time - b.time)
}

/**
 * Official history replaces completed bars. The final historical minute can
 * still be forming, so preserve its observed quote extremes and newer close.
 * History has no intra-minute update timestamp: a sampled close stays until a
 * later quote or until official history advances beyond that minute.
 */
export function reconcileCandles(history: readonly unknown[] | null | undefined, previous: readonly ChartCandle[]): ChartCandle[] {
  const official = normalizeCandles(history)
  const sampled = normalizeCandles(previous)
  const last = official.at(-1)
  if (!last) return sampled
  const previousLast = sampled.find(candle => candle.time === last.time)
  if (previousLast?.quoteTime !== undefined && previousLast.quoteTime >= last.time && previousLast.quoteTime < last.time + 60) {
    const previousIsNewer = last.quoteTime === undefined || previousLast.quoteTime > last.quoteTime
    official[official.length - 1] = {
      ...last,
      high: Math.max(last.high, previousLast.high),
      low: Math.min(last.low, previousLast.low),
      ...(previousIsNewer ? {
        close: previousLast.close,
        quoteTime: previousLast.quoteTime,
        ...(previousLast.quoteReceivedAt === undefined ? {} : { quoteReceivedAt: previousLast.quoteReceivedAt }),
      } : {}),
    }
  }
  return [...official, ...sampled.filter(candle => candle.time > last.time && candle.quoteTime !== undefined)]
}

/**
 * Add only a real Gate last-price observation. Preserve this result between
 * updates so an older quote cannot overwrite a later close in the same minute.
 * A new minute opens at its first observed quote; unobserved minutes stay absent.
 */
export function mergeQuoteIntoCandles(candles: readonly ChartCandle[], market: Market | null | undefined): ChartCandle[] {
  const result = normalizeCandles(candles)
  if (!market || market.source !== 'gate' || market.stale) return result
  const tradingStatus = text(record(market)?.trading_status ?? market.status).toLowerCase()
  if (tradingStatus !== 'open') return result
  const price = positive(market.last)
  // updated_at and received_at are local receipt times in app/market.py. They
  // cannot turn a cached last price into another source quote or a new candle.
  const stamp = utcSeconds(market.quote_timestamp)
  if (price === null || stamp === null) return result
  const receivedAt = utcSeconds(market.received_at)
  const latest = result.at(-1)
  if (latest && (stamp < latest.time || (latest.quoteTime !== undefined && stamp <= latest.quoteTime))) return result
  if (latest?.quoteReceivedAt !== undefined && receivedAt !== null && receivedAt < latest.quoteReceivedAt) return result
  const minute = Math.floor(stamp / 60) * 60
  if (latest && minute < latest.time) return result
  const metadata = { quoteTime: stamp, ...(receivedAt === null ? {} : { quoteReceivedAt: receivedAt }) }
  if (latest?.time === minute) {
    result[result.length - 1] = { ...latest, high: Math.max(latest.high, price), low: Math.min(latest.low, price), close: price, ...metadata }
  } else {
    result.push({ time: minute, open: price, high: price, low: price, close: price, ...metadata })
  }
  return result
}

const finished = new Set(['cancelled', 'canceled', 'filled', 'rejected', 'expired', 'closed', 'done', 'stopped', 'completed', 'finished'])
const uncertain = new Set(['unknown', 'reconciling', 'prepared', 'submitted', 'submitting', 'partial', 'awaiting_close'])
const text = (value: unknown) => typeof value === 'string' || typeof value === 'number' ? String(value).trim() : ''
const symbolOf = (value: unknown) => text(value).toUpperCase()
const actualId = (...values: unknown[]) => values.map(text).find(value => value !== '' && value !== '0')
const directionOf = (row: Row) => ['buy', 'long', '2'].includes(text(row.direction ?? row.side).toLowerCase()) ? 'long' : ['sell', 'short', '1'].includes(text(row.direction ?? row.side).toLowerCase()) ? 'short' : null
const colorOf = (direction: ReturnType<typeof directionOf>) => direction === 'long' ? '#63cbb0' : direction === 'short' ? '#e47586' : '#a3adbb'
const isFinished = (row: Row) => finished.has(text(row.status).toLowerCase()) || ((row.source === 'gate' || actualId(row.order_id, row.real_order_id, row.remote_order_id) !== undefined) && ['2', '4', '5'].includes(text(row.remote_state))) || text(row.finished) === '1'

/**
 * Local rows are trigger levels, never exchange resting orders. Gate provenance
 * requires source=gate or an explicit exchange order ID, not merely row.id.
 * An untagged preview belongs to the supplied symbol; tagged previews are checked.
 * Passing a replacement snapshot (including empty orders) removes old overlays.
 */
export function makeChartOverlays(symbol: string, preview: Preview | null | undefined, orders: readonly TradeRow[], positions: readonly TradeRow[] = []): ChartOverlay[] {
  const selected = symbolOf(symbol)
  if (!selected) return []
  const overlays = new Map<string, ChartOverlay>()
  const add = (line: ChartOverlay) => overlays.set(line.id, line)
  const previewSymbol = symbolOf(record(preview?.config)?.symbol ?? preview?.symbol)
  if (preview && (!previewSymbol || previewSymbol === selected)) {
    preview.levels.forEach((level, index) => {
      const price = positive(level)
      if (price !== null) add({ id: `preview:${selected}:${index}`, price, title: `预览格位 ${index + 1}`, color: 'rgba(145, 161, 183, 0.28)', lineStyle: 'dashed', kind: 'preview', axisLabelVisible: false })
    })
  }
  for (const item of orders) {
    const row = record(item)
    if (!row || symbolOf(row.symbol) !== selected || isFinished(row) || row.source === 'simulation') continue
    const price = positive(row.price ?? row.entry_price)
    if (price === null) continue
    const remoteId = actualId(row.order_id, row.real_order_id, row.remote_order_id)
    const remote = row.source === 'gate' || remoteId !== undefined
    const status = text(row.status).toLowerCase()
    const direction = directionOf(row)
    const side = direction === 'long' ? '买入' : direction === 'short' ? '卖出' : '方向待核对'
    const grid = typeof row.grid_index === 'number' && Number.isInteger(row.grid_index) && row.grid_index >= 0 ? ` · 第 ${row.grid_index + 1} 格` : ''
    const key = remoteId ?? actualId(row.id) ?? `${text(row.strategy_id)}:${text(row.grid_index)}:${price}`
    if (remote) {
      const needsReview = uncertain.has(status) || !['pending', 'open', 'new', 'active', 'partially_filled'].includes(status)
      const partial = text(row.remote_state) === '3' || status === 'partially_filled'
      add({ id: `order:${selected}:${key}`, price, title: `Gate 委托 · ${needsReview ? '待核对' : partial ? '部分成交' : side}${grid}`, color: needsReview ? '#edb65e' : colorOf(direction), lineStyle: needsReview ? 'dotted' : 'solid', kind: 'order', axisLabelVisible: true })
      const target = positive(row.take_profit ?? row.price_tp), stop = positive(row.stop_loss ?? row.price_sl)
      if (target !== null) add({ id: `order_take_profit:${selected}:${key}`, price: target, title: `委托止盈${needsReview ? ' · 待核对' : ''}${grid}`, color: '#9fc672', lineStyle: 'dotted', kind: 'order_take_profit', axisLabelVisible: true })
      if (stop !== null) add({ id: `order_stop_loss:${selected}:${key}`, price: stop, title: `委托止损${needsReview ? ' · 待核对' : ''}${grid}`, color: '#dc8291', lineStyle: 'dotted', kind: 'order_stop_loss', axisLabelVisible: true })
    } else {
      const label = status === 'armed' ? '旧格位 · 已停用' : status === 'waiting' ? '待挂条件' : status === 'ready' ? '等待提交委托' : status === 'submitting' ? '委托提交中' : '委托归属 · 待核对'
      const needsReview = !['armed', 'waiting', 'ready'].includes(status)
      add({ id: `local:${selected}:${key}`, price, title: `${label} · ${side}${grid}`, color: needsReview ? '#edb65e' : colorOf(direction), lineStyle: 'dotted', kind: 'local', axisLabelVisible: status !== 'waiting' })
    }
  }
  for (const item of positions) {
    const row = record(item)
    if (!row || symbolOf(row.symbol) !== selected || isFinished(row) || row.source === 'simulation' || (row.volume !== undefined && positive(row.volume) === null)) continue
    const positionId = actualId(row.position_id)
    if (row.source !== 'gate' && !positionId) continue
    const key = positionId ?? actualId(row.id)
    if (!key) continue
    const direction = directionOf(row)
    const title = direction === 'long' ? '多单' : direction === 'short' ? '空单' : '方向待核对'
    const entry = positive(row.entry_price ?? row.price_open), target = positive(row.take_profit ?? row.price_tp), stop = positive(row.stop_loss ?? row.price_sl)
    if (entry !== null) add({ id: `position:${selected}:${key}`, price: entry, title: `持仓 · ${title}`, color: colorOf(direction), lineStyle: 'solid', kind: 'position', axisLabelVisible: true })
    if (target !== null) add({ id: `take_profit:${selected}:${key}`, price: target, title: `持仓止盈 · ${title}`, color: '#9fc672', lineStyle: 'dashed', kind: 'take_profit', axisLabelVisible: true })
    if (stop !== null) add({ id: `stop_loss:${selected}:${key}`, price: stop, title: `持仓止损 · ${title}`, color: '#dc8291', lineStyle: 'dashed', kind: 'stop_loss', axisLabelVisible: true })
  }
  return [...overlays.values()]
}
