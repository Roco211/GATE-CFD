import assert from 'node:assert/strict'
import test from 'node:test'
import { fillTime, fillTimestamp, fillTypeLabel, newestFills } from './tradeHistory.ts'
import type { TradeRow } from './types'

const row = (id: string, time: string | number, extra: Partial<TradeRow> = {}): TradeRow => ({ id, symbol: 'XAUUSD', time, ...extra })

test('Gate newest-first history keeps recent exits visible instead of reversing them below old rows', () => {
  const current = row('11636880', '2026-09-07T11:37:38Z', { type: 'target_exit' })
  const prior = row('11637001', '2026-09-07T11:34:10Z', { type: 'closed' })
  const old = Array.from({ length: 38 }, (_, index) => row(`old-${index}`, 1_700_000_000 - index))
  const data = [current, prior, ...old]
  assert.deepEqual(newestFills('XAUUSD', data).slice(0, 2).map(item => item.id), ['11636880', '11637001'])
  assert.deepEqual(newestFills('XAUUSD', [...data].reverse()).slice(0, 2).map(item => item.id), ['11636880', '11637001'])
  assert.equal(data[0], current)
})

test('a new snapshot exposes the latest fill first without retaining a previous array', () => {
  const previous = [row('old', 1_800_000_000)]
  assert.equal(newestFills('XAUUSD', previous)[0].id, 'old')
  assert.equal(newestFills('XAUUSD', [...previous, row('new', 1_800_000_030)])[0].id, 'new')
  assert.deepEqual(newestFills('XAUUSD', []), [])
})

test('mixed timestamps sort by time, isolate the symbol and retain distinct same-second exits', () => {
  const rows = [row('unknown', 'invalid'), row('same-a', '1800000001'), row('other', 1_900_000_000, { symbol: 'EURUSD' }), row('same-b', 1_800_000_001_000), row('old', 1_800_000_000)]
  assert.deepEqual(newestFills('XAUUSD', rows).map(item => item.id), ['same-a', 'same-b', 'old', 'unknown'])
  assert.equal(fillTimestamp('1800000001'), fillTimestamp(1_800_000_001_000))
  assert.match(fillTime('2026-09-07T11:37:38Z'), /19:37:38/)
  assert.equal(fillTime('invalid'), '—')
  assert.equal(fillTime(1e20), '—')
})

test('unknown and ordinary closed positions never appear as confirmed take profit', () => {
  assert.equal(fillTypeLabel({ type: 'target_exit', exit_confirmed: true }), '止盈平仓')
  assert.equal(fillTypeLabel({ type: 'target_exit' }), '已平仓 · 原因待核对')
  assert.equal(fillTypeLabel({ type: 'target_exit', exit_confirmed: false }), '已平仓 · 原因待核对')
  assert.equal(fillTypeLabel({ type: 'awaiting_confirmation' }), '已平仓 · 原因待核对')
  assert.equal(fillTypeLabel({ type: 'manual_close', exit_confirmed: true }), '普通平仓')
  assert.equal(fillTypeLabel({ type: 'ordinary_close', exit_confirmed: true }), '普通平仓')
  assert.equal(fillTypeLabel({ type: 'closed' }), '已平仓')
  assert.equal(fillTypeLabel({ type: 'stop_loss', exit_confirmed: true }), '止损平仓')
})
