import test from 'node:test'
import assert from 'node:assert/strict'
import { applyPlan, pickedRange } from './plannerUi.ts'
import type { GridConfig } from './types.ts'

const original: GridConfig = { symbol: 'XAUUSD', direction: 'long', spacing: 'arithmetic', lower_price: '100', upper_price: '120', grid_count: 4, volume: '0.01', stop_loss: '90', repeat: false }
const planned: GridConfig = { ...original, lower_price: '102', grid_count: 5, repeat: true, stop_loss: null }
test('application preserves repeat and stop protection instead of replacing them with calculator defaults', () => {
  const applied = applyPlan(original, planned, false)
  assert.equal(applied.stop_loss, '90'); assert.equal(applied.repeat, false); assert.equal(applied.grid_count, 5)
  assert.equal(original.grid_count, 4)
})
test('stale plans cannot mutate a running strategy or another symbol', () => {
  assert.throws(() => applyPlan(original, planned, true), /锁定/)
  assert.throws(() => applyPlan({ ...original, symbol: 'EURUSD' }, planned, false), /品种/)
})
test('new ranges never silently erase incompatible long or short protection', () => {
  assert.throws(() => applyPlan(original, { ...planned, lower_price: '80' }, false), /止损/)
  assert.throws(() => applyPlan({ ...original, direction: 'short', stop_loss: '130' }, { ...planned, direction: 'short', upper_price: '140' }, false), /止损/)
})
test('invalid result counts, ranges and quantities cannot be applied', () => {
  for (const bad of [{grid_count: 101}, {grid_count: 2.5}, {upper_price: '90'}, {volume: 'NaN'}, {lower_price: ''}]) assert.throws(() => applyPlan(original, { ...planned, ...bad }, false))
})
test('two chart clicks sort prices while preserving exact tick strings', () => {
  assert.deepEqual(pickedRange('4400.05', '4380.00'), {lower_price: '4380.00', upper_price: '4400.05'})
  assert.throws(() => pickedRange('100.0', '100.00'))
  assert.throws(() => pickedRange('NaN', '100'))
})
