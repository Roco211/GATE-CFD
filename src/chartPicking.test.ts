import assert from 'node:assert/strict'
import test from 'node:test'
import { createPricePicker, pickPoint, snapPickedPrice, tickDecimals } from './chartPicking.ts'
import type { PaneGeometry, PickPointer } from './chartPicking.ts'

const bounds: PaneGeometry = { left: 100, top: 200, width: 1000, height: 400, paneWidth: 916, paneHeight: 400, leftAxisWidth: 0, rightAxisWidth: 84 }
const pointer = (clientX = 300, clientY = 300, extra: Partial<PickPointer> = {}): PickPointer => ({ pointerId: 1, clientX, clientY, button: 0, isPrimary: true, timeStamp: 100, ...extra })
function harness() {
  let enabled = true
  let hasData = true
  let tick: unknown = '0.05'
  const picks: string[] = []
  const previews: (string | null)[] = []
  const coordinates: number[] = []
  const controller = createPricePicker({
    enabled: () => enabled,
    geometry: () => bounds,
    priceAt: point => { coordinates.push(point.y); return hasData ? 2500 - point.y / 10 : null },
    tickSize: () => tick,
    precision: () => 2,
    onPreview: price => previews.push(price),
    onPick: price => picks.push(price),
  })
  return { controller, picks, previews, coordinates, enable: (value: boolean) => { enabled = value }, data: (value: boolean) => { hasData = value }, tick: (value: unknown) => { tick = value } }
}

test('picked prices use the actual tick, including non-power-of-ten ticks and decimal ties', () => {
  assert.equal(snapPickedPrice(2345.527, '0.05', 2), '2345.55')
  assert.equal(snapPickedPrice(2345.525, '0.05', 2), '2345.55')
  assert.equal(snapPickedPrice(2345.5249, '0.05', 2), '2345.50')
  assert.equal(snapPickedPrice(3.14, '0.25', 2), '3.25')
  assert.equal(snapPickedPrice(101.4, '2', 0), '102')
  assert.equal(snapPickedPrice(0.30000000000000004, '0.1', 2), '0.30')
  assert.equal(snapPickedPrice(0.000012346, '1e-8', 5), '0.00001235')
  assert.equal(tickDecimals('1e-8'), 8)
})

test('invalid values and missing Gate tick never produce a made-up price step', () => {
  for (const tick of [null, undefined, '', 'bad', '0', '-0.1', Infinity, true]) assert.equal(snapPickedPrice(2400, tick, 2), null)
  for (const price of [null, undefined, '', 'bad', 0, -1, NaN, Infinity, true]) assert.equal(snapPickedPrice(price, '0.01', 2), null)
  assert.equal(snapPickedPrice(0.001, '0.01', 2), null)
})

test('plot and right price axis share the same logical y and exclude time axis/outside edges', () => {
  assert.deepEqual(pickPoint(500, 300, bounds), { x: 400, y: 100, region: 'pane' })
  assert.deepEqual(pickPoint(1050, 300, bounds), { x: 950, y: 100, region: 'price-axis' })
  for (const [x, y] of [[500, 600], [500, 199], [1100, 300], [99, 300], [NaN, 300]]) assert.equal(pickPoint(x, y, bounds), null)
  assert.equal(pickPoint(500, 300, { ...bounds, paneHeight: 0 }), null)
})

test('DOM zoom and a visible left scale map into the documented pane coordinate space', () => {
  const scaled = { ...bounds, width: 2000, height: 800, paneWidth: 840, leftAxisWidth: 80, rightAxisWidth: 80 }
  assert.deepEqual(pickPoint(500, 400, scaled), { x: 120, y: 100, region: 'pane' })
  assert.deepEqual(pickPoint(2040, 400, scaled), { x: 890, y: 100, region: 'price-axis' })
  assert.equal(pickPoint(150, 400, scaled), null)
})

test('a real click on either plot or price axis commits once using the series conversion', () => {
  for (const x of [500, 1050]) {
    const h = harness()
    h.controller.down(pointer(x, 305.2))
    h.controller.up(pointer(x, 305.2, { timeStamp: 150 }))
    h.controller.up(pointer(x, 305.2, { timeStamp: 151 }))
    assert.deepEqual(h.picks, ['2489.50'])
    assert.ok(h.coordinates.every(y => Math.abs(y - 105.2) < 1e-9))
  }
})

test('normal browsing and a newly locked form cannot change any draft value', () => {
  const h = harness()
  h.enable(false)
  h.controller.down(pointer())
  h.controller.up(pointer())
  assert.deepEqual(h.coordinates, [])
  h.enable(true)
  h.controller.down(pointer())
  h.enable(false)
  h.controller.up(pointer(300, 300, { timeStamp: 150 }))
  assert.deepEqual(h.picks, [])
})

test('dragging out and back to the starting point is still a drag, not a price pick', () => {
  const h = harness()
  h.controller.down(pointer())
  h.controller.move(pointer(315, 300, { timeStamp: 120 }))
  h.controller.move(pointer(300, 300, { timeStamp: 140 }))
  h.controller.up(pointer(300, 300, { timeStamp: 160 }))
  assert.deepEqual(h.picks, [])
  h.controller.down(pointer())
  h.controller.up(pointer(307, 300, { timeStamp: 180 }))
  assert.deepEqual(h.picks, [])
})

test('wheel, pointer cancellation, long press and multi-touch cannot commit a pick', () => {
  const h = harness()
  h.controller.down(pointer()); h.controller.cancel(); h.controller.up(pointer())
  h.controller.down(pointer()); h.controller.up(pointer(300, 300, { timeStamp: 800 }))
  h.controller.down(pointer()); h.controller.down(pointer(500, 300, { pointerId: 2, isPrimary: false })); h.controller.up(pointer())
  h.controller.down(pointer(300, 300, { button: 2 })); h.controller.up(pointer(300, 300, { button: 2 }))
  assert.deepEqual(h.picks, [])
  assert.equal(h.previews.at(-1), null)
})

test('no series data, unavailable tick or an out-of-pane pointer never picks OHLC or cached price', () => {
  for (const setup of [(h: ReturnType<typeof harness>) => h.data(false), (h: ReturnType<typeof harness>) => h.tick(null)]) {
    const h = harness(); setup(h)
    h.controller.down(pointer()); h.controller.move(pointer()); h.controller.up(pointer())
    assert.deepEqual(h.picks, [])
    assert.equal(h.previews.at(-1), null)
  }
  const h = harness()
  h.controller.down(pointer(500, 620)); h.controller.up(pointer(500, 620))
  assert.deepEqual(h.coordinates, [])
})
