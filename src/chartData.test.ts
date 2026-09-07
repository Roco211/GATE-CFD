import assert from 'node:assert/strict'
import test from 'node:test'
import { makeChartOverlays, mergeQuoteIntoCandles, normalizeCandles, reconcileCandles } from './chartData.ts'
import type { ChartCandle } from './chartData.ts'
import type { Market, Preview, TradeRow } from './types.ts'
import { managedTradeId, protectionPatch } from './api.ts'

const minute = 1_800_000_000
const candle = (time: unknown, changes: Record<string, unknown> = {}) => ({ time, open: '100', high: '110', low: '95', close: '105', ...changes })
const quote = (last: string | null, seconds = minute + 10, changes: Partial<Market> = {}): Market => ({ symbol: 'XAUUSD', last, bid: '104', ask: '106', change: null, high: null, low: null, candles: [], source: 'gate', status: 'open', received_at: seconds + 0.1, quote_timestamp: seconds * 1000, ...changes })
const preview = (levels: string[], symbol = 'XAUUSD'): Preview => ({ levels, cells: [], warnings: [], can_start: false, sufficient_margin: false, config: { symbol } })
const row = (changes: Record<string, unknown> = {}): TradeRow => ({ id: 'grid:1', symbol: 'XAUUSD', side: 'buy', price: '100', status: 'armed', source: 'local_trigger', grid_index: 1, ...changes } as TradeRow)

test('candles are numeric, sorted, deduplicated by the last valid row and leave input untouched', () => {
  const input = [candle(minute + 60), candle(minute), candle(String(minute), { close: '108' }), candle(minute, { high: '90' })]
  const before = structuredClone(input)
  assert.deepEqual(normalizeCandles(input), [
    { time: minute, open: 100, high: 110, low: 95, close: 108 },
    { time: minute + 60, open: 100, high: 110, low: 95, close: 105 },
  ])
  assert.deepEqual(input, before)
})

test('milliseconds, second strings and ISO dates use the same UTC second', () => {
  const iso = new Date(minute * 1000).toISOString()
  for (const time of [minute, minute * 1000, String(minute), String(minute * 1000), iso, iso.replace('Z', '')]) {
    assert.equal(normalizeCandles([candle(time)])[0]?.time, minute)
  }
  assert.equal(normalizeCandles([candle(`${minute}.9`)])[0]?.time, minute)
})

test('invalid timestamp, price, and inconsistent OHLC rows are discarded', () => {
  const invalid = [null, {}, candle('bad'), candle('2026-02-30T12:00:00Z'), candle(true), candle(0), candle(-1), candle(Infinity),
    ...[null, '', ' ', false, '-1', '0', 'NaN', 'Infinity', '0x64'].map(close => candle(minute, { close })),
    candle(minute, { high: '99' }), candle(minute, { low: '106' }), candle(minute, { high: '94', low: '95' })]
  assert.deepEqual(normalizeCandles(invalid), [])
  assert.deepEqual(normalizeCandles(null), [])
})

test('quotes update the current minute OHLC without changing its opening price', () => {
  const original = normalizeCandles([candle(minute)])
  const higher = mergeQuoteIntoCandles(original, quote('112', minute + 10))
  const lower = mergeQuoteIntoCandles(higher, quote('94', minute + 20))
  assert.deepEqual([lower[0].open, lower[0].high, lower[0].low, lower[0].close], [100, 112, 94, 94])
  assert.equal(lower[0].quoteTime, minute + 20)
  assert.equal(original[0].close, 105)
})

test('new minutes open at an observed quote and never fill missing minutes', () => {
  const result = mergeQuoteIntoCandles(normalizeCandles([candle(minute)]), quote('120', minute + 190))
  assert.deepEqual(result.map(item => item.time), [minute, minute + 180])
  assert.deepEqual([result[1].open, result[1].high, result[1].low, result[1].close], [120, 120, 120, 120])
  assert.equal(mergeQuoteIntoCandles([], quote('120'))[0].time, minute)
})

test('old and duplicate quotes cannot roll back a minute or its close', () => {
  const latest = mergeQuoteIntoCandles([], quote('108', minute + 20))
  for (const stale of [quote('90', minute - 1), quote('90', minute + 10), quote('90', minute + 20), quote('90', minute + 30, { received_at: minute + 10 })]) {
    assert.deepEqual(mergeQuoteIntoCandles(latest, stale), latest)
  }
  assert.deepEqual(mergeQuoteIntoCandles(normalizeCandles([candle(minute + 60)]), quote('90', minute + 10)), normalizeCandles([candle(minute + 60)]))
})

test('only a valid actual last quote is aggregated, with no bid/ask fallback', () => {
  const base = normalizeCandles([candle(minute)])
  for (const invalid of [null, quote(null), quote('0'), quote('NaN'), quote('110', minute + 10, { source: 'simulation' }), quote('110', minute + 10, { stale: true }), quote('110', minute + 10, { received_at: NaN, quote_timestamp: NaN, updated_at: 'bad' })]) {
    assert.deepEqual(mergeQuoteIntoCandles(base, invalid), base)
  }
  assert.deepEqual(mergeQuoteIntoCandles(base, quote('109', minute + 10, { quote_timestamp: null, updated_at: new Date((minute + 15) * 1000).toISOString() })), base)
  assert.deepEqual(mergeQuoteIntoCandles(base, quote('109', minute + 10, { quote_timestamp: null })), base)
})

test('closed or stale quotes never create flat candles in later minutes', () => {
  const base = mergeQuoteIntoCandles([], quote('105'))
  for (const status of ['closed', 'close', '闭市', 'unknown', undefined]) {
    assert.deepEqual(mergeQuoteIntoCandles(base, quote('105', minute + 130, { status })), base)
  }
  assert.deepEqual(mergeQuoteIntoCandles(base, quote('105', minute + 130, { stale: true })), base)
  assert.deepEqual(mergeQuoteIntoCandles(base, { ...quote('105', minute + 130), trading_status: 'closed' } as Market), base)
})

test('a newer receipt time alone cannot create a quote or advance a candle', () => {
  const base = mergeQuoteIntoCandles([], quote('105'))
  assert.deepEqual(mergeQuoteIntoCandles(base, quote('105', minute + 10, { received_at: minute + 130, updated_at: new Date((minute + 130) * 1000).toISOString() })), base)
  assert.deepEqual(mergeQuoteIntoCandles(base, quote('105', minute + 130, { quote_timestamp: null })), base)
})

test('official history replaces closed sampled bars but preserves newer live minutes without gaps', () => {
  const previous = mergeQuoteIntoCandles(mergeQuoteIntoCandles([], quote('115')), quote('125', minute + 190))
  const official = [candle(minute, { close: '103' }), candle(minute + 60)]
  const result = reconcileCandles(official, previous)
  assert.deepEqual(result.map(item => item.time), [minute, minute + 60, minute + 180])
  assert.deepEqual(result[0], { time: minute, open: 100, high: 110, low: 95, close: 103 })
  assert.equal(result[2].close, 125)
  assert.equal(result[2].quoteTime, minute + 190)
  assert.deepEqual(reconcileCandles([], previous), previous)
})

test('the forming history bar keeps official open, sampled extremes and newer sampled close', () => {
  const previous = mergeQuoteIntoCandles(mergeQuoteIntoCandles([], quote('112')), quote('93', minute + 20))
  const official = [candle(minute)]
  const result = reconcileCandles(official, previous)
  assert.deepEqual([result[0].open, result[0].high, result[0].low, result[0].close], [100, 112, 93, 93])
  assert.equal(result[0].quoteTime, minute + 20)
  assert.equal(result[0].quoteReceivedAt, minute + 20.1)
  assert.deepEqual(mergeQuoteIntoCandles(result, quote('101', minute + 15)), result)
  assert.deepEqual(reconcileCandles([candle(minute), candle(minute + 60)], previous)[0], { time: minute, open: 100, high: 110, low: 95, close: 105 })
})

test('history reconciliation does not replace a newer known close with older sampled data', () => {
  const newer = mergeQuoteIntoCandles(normalizeCandles([candle(minute)]), quote('109', minute + 30))
  const older = mergeQuoteIntoCandles([], quote('112', minute + 20))
  const result = reconcileCandles(newer, older)
  assert.equal(result[0].close, 109)
  assert.equal(result[0].high, 112)
  assert.equal(result[0].quoteTime, minute + 30)
  assert.equal(result[0].quoteReceivedAt, minute + 30.1)
})

test('normalization preserves quote ordering metadata across chart rerenders', () => {
  const result = mergeQuoteIntoCandles([], quote('110'))
  assert.deepEqual(normalizeCandles(result), result)
  const frozen: readonly ChartCandle[] = Object.freeze(result.map(item => Object.freeze(item)))
  assert.equal(mergeQuoteIntoCandles(frozen, quote('111', minute + 20))[0].close, 111)
})

test('every N+1 preview level is a faint dashed line, including 101 levels', () => {
  const levels = Array.from({ length: 101 }, (_, index) => String(100 + index))
  const lines = makeChartOverlays('XAUUSD', preview(levels), [])
  assert.equal(lines.length, 101)
  assert.ok(lines.every(line => line.kind === 'preview' && line.lineStyle === 'dashed' && !line.axisLabelVisible))
  assert.equal(new Set(lines.map(line => line.id)).size, 101)
  assert.equal(makeChartOverlays('XAUUSD', preview(['100', 'bad', '0', '110']), []).length, 2)
})

test('all overlays are isolated by symbol, including tagged previews and positions', () => {
  const lines = makeChartOverlays('XAUUSD', preview(['1', '2'], 'EURUSD'), [row(), row({ id: 'other', symbol: 'EURUSD' })], [row({ source: 'gate', position_id: '1', symbol: 'EURUSD', entry_price: '100' })])
  assert.equal(lines.length, 1)
  assert.equal(lines[0].kind, 'local')
  assert.deepEqual(makeChartOverlays('', preview(['100']), [row()]), [])
})

test('local plans and disabled legacy levels never claim to be Gate resting orders', () => {
  const lines = makeChartOverlays('XAUUSD', null, [row(), row({ id: 'grid:2', status: 'waiting' }), row({ id: 'grid:3', status: 'unknown' })])
  assert.ok(lines.every(line => line.kind === 'local' && !line.title.includes('Gate') && line.lineStyle === 'dotted'))
  assert.match(lines[0].title, /旧格位.*已停用/)
  assert.match(lines[1].title, /待挂条件/)
  assert.match(lines[2].title, /待核对/)
})

test('exchange orders require explicit provenance or a real order identifier', () => {
  const lines = makeChartOverlays('XAUUSD', null, [
    row({ id: '201', source: 'gate', order_id: '201', status: 'pending', remote_state: '1' }),
    row({ id: '202', source: undefined, remote_order_id: '202', status: 'unknown' }),
    row({ id: '203', source: undefined, real_order_id: '203', status: 'pending' }),
    row({ id: '204', source: undefined, status: 'pending' }),
  ])
  assert.deepEqual(lines.map(line => line.kind), ['order', 'order', 'order', 'local'])
  assert.match(lines[1].title, /待核对/)
  assert.doesNotMatch(lines[3].title, /Gate/)
})

test('cancelled or finished lines disappear and an empty replacement snapshot keeps no old orders', () => {
  const endings = ['cancelled', 'canceled', 'filled', 'rejected', 'closed', 'expired', 'done', 'stopped', 'completed']
  assert.deepEqual(makeChartOverlays('XAUUSD', null, endings.map(status => row({ status }))), [])
  assert.deepEqual(makeChartOverlays('XAUUSD', null, ['2', '4', '5'].map(remote_state => row({ source: 'gate', status: 'pending', remote_state }))), [])
  assert.deepEqual(makeChartOverlays('XAUUSD', null, [row({ source: undefined, order_id: '201', status: 'pending', remote_state: '2' })]), [])
  assert.equal(makeChartOverlays('XAUUSD', null, [row()]).length, 1)
  assert.deepEqual(makeChartOverlays('XAUUSD', null, []), [])
})

test('real partial orders remain visible and positions expose entry and optional take profit', () => {
  const lines = makeChartOverlays('XAUUSD', null, [row({ source: 'gate', order_id: '201', status: 'pending', remote_state: '3' })], [row({ source: 'gate', position_id: '301', status: undefined, direction: 'long', entry_price: '104', take_profit: '110', volume: '0.01' })])
  assert.deepEqual(lines.map(line => line.kind), ['order', 'position', 'take_profit'])
  assert.match(lines[0].title, /部分成交/)
  assert.deepEqual(lines.slice(1).map(line => line.price), [104, 110])
  assert.deepEqual(makeChartOverlays('XAUUSD', null, [], [row({ source: 'gate', position_id: '301', entry_price: '104', volume: '0' })]), [])
})

test('order and position TP/SL overlays are distinct and disappear with their parent', () => {
  const order = row({ source: 'gate', id: '201', order_id: '201', status: 'pending', take_profit: '110', stop_loss: '90' })
  const position = row({ source: 'gate', id: '301', position_id: '301', status: 'open', entry_price: '104', take_profit: '111', stop_loss: '91' })
  const lines = makeChartOverlays('XAUUSD', null, [order], [position])
  assert.deepEqual(lines.map(line => line.kind), ['order', 'order_take_profit', 'order_stop_loss', 'position', 'take_profit', 'stop_loss'])
  assert.ok(lines.find(line => line.kind === 'order_stop_loss')?.title.includes('委托止损'))
  assert.ok(lines.find(line => line.kind === 'stop_loss')?.title.includes('持仓止损'))
  assert.deepEqual(makeChartOverlays('XAUUSD', null, [{ ...order, status: 'cancelled' }], [{ ...position, status: 'closed' }]), [])
  assert.equal(makeChartOverlays('XAUUSD', null, [{ ...order, take_profit: '0', stop_loss: '0' }]).length, 1)
})

test('trade management requires explicit Gate source, ownership, numeric remote ID and actionable status', () => {
  const order = row({ id: '201', source: 'gate', managed: true, strategy_id: 'strategy-one', status: 'pending' })
  assert.equal(managedTradeId(order, 'order'), '201')
  const partial = { ...order, status: 'partial', remote_state: '3' }
  assert.equal(managedTradeId(partial, 'order'), null)
  assert.equal(managedTradeId(partial, 'order', 'cancel'), '201')
  assert.equal(managedTradeId({ ...partial, remote_state: undefined }, 'order', 'cancel'), null)
  assert.equal(managedTradeId({ ...partial, managed: false }, 'order', 'cancel'), null)
  for (const patch of [{ source: 'local_plan' }, { managed: false }, { strategy_id: null }, { id: 'local:201' }, { status: 'unknown' }, { status: 'cancelled' }, { remote_state: '4' }]) {
    assert.equal(managedTradeId({ ...order, ...patch } as TradeRow, 'order'), null)
  }
  const position = row({ id: '301', source: 'gate', managed: true, strategy_id: 'stopped-strategy', volume: '0.01', status: 'open' })
  assert.equal(managedTradeId(position, 'position'), '301')
  assert.equal(managedTradeId({ ...position, managed: false }, 'position'), null)
  assert.equal(managedTradeId({ ...position, volume: '0' }, 'position'), null)
  assert.equal(managedTradeId({ ...position, volume: 'NaN' }, 'position'), null)
  assert.equal(managedTradeId({ ...position, status: 'canceled' }, 'position'), null)
})

test('protection edits omit untouched fields and clear only explicit legs', () => {
  const input = { takeProfit: '', stopLoss: '', clearTakeProfit: false, clearStopLoss: false }
  assert.deepEqual(protectionPatch(input), {})
  assert.deepEqual(protectionPatch({ ...input, takeProfit: ' 110.25 ' }), { price_tp: '110.25' })
  assert.deepEqual(protectionPatch({ ...input, clearStopLoss: true }), { price_sl: '0' })
  assert.deepEqual(protectionPatch({ ...input, takeProfit: '0', stopLoss: '90' }), { price_tp: '0', price_sl: '90' })
  assert.deepEqual(protectionPatch({ ...input, takeProfit: '110', clearTakeProfit: true }), { price_tp: '0' })
  for (const value of ['-1', 'NaN', 'Infinity', '0x10']) assert.throws(() => protectionPatch({ ...input, stopLoss: value }))
})
