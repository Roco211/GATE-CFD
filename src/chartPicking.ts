export type PickTarget = 'lower' | 'upper' | 'stop' | 'anchor'
export const pickTargetLabels: Record<PickTarget, string> = { lower: '区间下限', upper: '区间上限', stop: '止损价', anchor: '参考价格' }

type Decimal = { units: bigint; scale: number }
function decimal(value: unknown): Decimal | null {
  if (typeof value !== 'string' && typeof value !== 'number') return null
  const text = String(value).trim()
  const match = /^\+?(\d+)(?:\.(\d*))?(?:e([+-]?\d+))?$/i.exec(text)
  if (!match || !Number.isFinite(Number(text)) || Number(text) <= 0) return null
  const exponent = Number(match[3] ?? 0)
  if (Math.abs(exponent) > 30 || (match[1] + (match[2] ?? '')).length > 40) return null
  const scale = (match[2]?.length ?? 0) - exponent
  if (scale > 30) return null
  return { units: BigInt(match[1] + (match[2] ?? '')) * (scale < 0 ? 10n ** BigInt(-scale) : 1n), scale: Math.max(0, scale) }
}

export function tickDecimals(tick: unknown): number | null {
  const parsed = decimal(tick)
  return parsed ? parsed.scale : null
}

/** Exact decimal tick rounding avoids binary artifacts such as 2345.549999999. */
export function snapPickedPrice(price: unknown, tickSize: unknown, precision = 0): string | null {
  const p = decimal(price), tick = decimal(tickSize)
  if (!p || !tick) return null
  const scale = Math.max(p.scale, tick.scale)
  const priceUnits = p.units * 10n ** BigInt(scale - p.scale)
  const tickUnits = tick.units * 10n ** BigInt(scale - tick.scale)
  const count = (priceUnits + tickUnits / 2n) / tickUnits
  if (count <= 0n) return null
  const digits = Math.max(tick.scale, Number.isFinite(precision) ? Math.min(18, Math.max(0, Math.trunc(precision))) : 0)
  const units = count * tick.units * 10n ** BigInt(digits - tick.scale)
  const text = units.toString().padStart(digits + 1, '0')
  return digits ? `${text.slice(0, -digits)}.${text.slice(-digits)}` : text
}

export type PaneGeometry = {
  left: number; top: number; width: number; height: number
  paneWidth: number; paneHeight: number; leftAxisWidth: number; rightAxisWidth: number
}
export type PickPoint = { x: number; y: number; region: 'pane' | 'price-axis' }

/** DOM bounds locate the pane; only the chart series converts its y coordinate into price. */
export function pickPoint(clientX: number, clientY: number, geometry: PaneGeometry | null): PickPoint | null {
  if (!geometry || !Object.values(geometry).every(Number.isFinite) || !Number.isFinite(clientX) || !Number.isFinite(clientY)) return null
  const { left, top, width, height, paneWidth, paneHeight, leftAxisWidth, rightAxisWidth } = geometry
  if (width <= 0 || height <= 0 || paneWidth <= 0 || paneHeight <= 0 || leftAxisWidth < 0 || rightAxisWidth < 0) return null
  if (clientX < left || clientX >= left + width || clientY < top || clientY >= top + height) return null
  const x = (clientX - left) * (leftAxisWidth + paneWidth + rightAxisWidth) / width - leftAxisWidth
  const y = (clientY - top) * paneHeight / height
  if (x < 0 || x >= paneWidth + rightAxisWidth || y < 0 || y >= paneHeight) return null
  return { x, y, region: x < paneWidth ? 'pane' : 'price-axis' }
}

export type PickPointer = { pointerId: number; clientX: number; clientY: number; button: number; isPrimary: boolean; timeStamp: number }
type PickerOptions = {
  enabled: () => boolean
  geometry: () => PaneGeometry | null
  priceAt: (point: PickPoint) => number | null
  tickSize: () => unknown
  precision: () => number
  onPreview: (price: string | null) => void
  onPick: (price: string) => void
}

/** A single real pointer-up commits a pick; dragging, pinch, wheel and long press never do. */
export function createPricePicker(options: PickerOptions) {
  let gesture: { pointerId: number; x: number; y: number; started: number; moved: boolean } | null = null
  const resolve = (event: PickPointer) => {
    if (!options.enabled()) return null
    const point = pickPoint(event.clientX, event.clientY, options.geometry())
    return point ? snapPickedPrice(options.priceAt(point), options.tickSize(), options.precision()) : null
  }
  const move = (event: PickPointer) => {
    if (gesture && gesture.pointerId === event.pointerId && Math.hypot(event.clientX - gesture.x, event.clientY - gesture.y) >= 4) gesture.moved = true
    options.onPreview(resolve(event))
  }
  const cancel = () => { gesture = null; options.onPreview(null) }
  return {
    down(event: PickPointer) {
      if (gesture || !event.isPrimary || event.button !== 0 || resolve(event) === null) { cancel(); return }
      gesture = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, started: event.timeStamp, moved: false }
      move(event)
    },
    move,
    up(event: PickPointer) {
      const previous = gesture
      gesture = null
      if (!previous || previous.pointerId !== event.pointerId || !event.isPrimary || event.button !== 0 || previous.moved || Math.hypot(event.clientX - previous.x, event.clientY - previous.y) >= 4 || event.timeStamp < previous.started || event.timeStamp - previous.started > 600) return
      const price = resolve(event)
      if (price !== null) options.onPick(price)
    },
    cancel,
  }
}
