import { useEffect, useMemo, useRef, useState } from 'react'
import { Crosshair, Layers3, Maximize2 } from 'lucide-react'
import { CandlestickSeries, ColorType, CrosshairMode, LineStyle, TickMarkType, createChart } from 'lightweight-charts'
import type { AutoscaleInfoProvider, CandlestickData, IChartApi, IPriceLine, ISeriesApi, MouseEventParams, Time, UTCTimestamp } from 'lightweight-charts'
import { Button } from '@/components/motion/button/base'
import type { Market, Preview, TradeRow } from './types'
import { number } from './api'
import { makeChartOverlays, mergeQuoteIntoCandles, reconcileCandles } from './chartData'
import type { ChartCandle, ChartOverlay } from './chartData'
import { createPricePicker, pickTargetLabels, tickDecimals } from './chartPicking'
import type { PickTarget } from './chartPicking'

type Props = { market: Market; stale: boolean; preview: Preview | null; direction: 'long' | 'short'; precision: number; orders: readonly TradeRow[]; positions: readonly TradeRow[]; pickTarget?: PickTarget | null; onPickPrice?: (price: string) => void; pickDisabled?: boolean; tickSize?: string | null }
type ChartRuntime = { chart: IChartApi; series: ISeriesApi<'Candlestick'>; lines: Map<string, IPriceLine>; quote: IPriceLine | null }
const styles = { solid: LineStyle.Solid, dashed: LineStyle.Dashed, dotted: LineStyle.Dotted }
const timeFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit' })
const dateFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit' })
const formatTime = (time: Time, date = false) => typeof time === 'number' ? (date ? `${dateFormat.format(time * 1000)} ` : '') + timeFormat.format(time * 1000) : ''
const chartBar = (bar: ChartCandle): CandlestickData<UTCTimestamp> => ({ time: bar.time as UTCTimestamp, open: bar.open, high: bar.high, low: bar.low, close: bar.close })
const sameBar = (a: ChartCandle, b: ChartCandle) => a.time === b.time && a.open === b.open && a.high === b.high && a.low === b.low && a.close === b.close

function latestRange(chart: IChartApi, count: number, span: number) {
  if (count) chart.timeScale().setVisibleLogicalRange({ from: Math.max(-1, count - span) - 1, to: count - 1 + 8 })
}

export default function GridChart({ market, stale, preview, direction, precision, orders, positions, pickTarget = null, onPickPrice, pickDisabled = false, tickSize }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const runtime = useRef<ChartRuntime | null>(null)
  const candlesRef = useRef<ChartCandle[]>([])
  const historyKey = useRef('')
  const spanRef = useRef(80)
  const scaleSettings = useRef<{ fit: boolean; lines: ChartOverlay[] }>({ fit: false, lines: [] })
  const [showGrid, setShowGrid] = useState(true)
  const [showOrders, setShowOrders] = useState(true)
  const [showPositions, setShowPositions] = useState(true)
  const [fitGrid, setFitGrid] = useState(false)
  const [span, setSpan] = useState(80)
  const [hover, setHover] = useState<ChartCandle | null>(null)
  const [latest, setLatest] = useState<ChartCandle | null>(null)
  const [candleCount, setCandleCount] = useState(0)
  const [pickedHover, setPickedHover] = useState<string | null>(null)
  const pickCallback = useRef(onPickPrice)
  pickCallback.current = onPickPrice
  const hasCandles = candleCount > 0
  const picking = !!pickTarget && !pickDisabled && hasCandles && tickDecimals(tickSize) !== null && !!onPickPrice
  const pickState = useRef({ picking, target: pickTarget, symbol: market.symbol })
  pickState.current = { picking, target: pickTarget, symbol: market.symbol }
  const allOverlays = useMemo(() => makeChartOverlays(market.symbol, preview, orders, positions), [market.symbol, preview, orders, positions])
  const overlays = useMemo(() => allOverlays.filter(line => line.kind === 'preview' ? showGrid : ['position', 'take_profit', 'stop_loss'].includes(line.kind) ? showPositions : showOrders).map(line => line.kind === 'preview' ? { ...line, color: direction === 'long' ? 'rgba(145, 185, 110, 0.35)' : 'rgba(224, 120, 135, 0.35)' } : line), [allOverlays, showGrid, showOrders, showPositions, direction])
  const localCount = allOverlays.filter(line => line.kind === 'local').length
  const orderCount = allOverlays.filter(line => line.kind === 'order').length
  const autoscale = useMemo<AutoscaleInfoProvider>(() => original => {
    const base = original()
    const prices = scaleSettings.current.fit ? scaleSettings.current.lines.map(line => line.price) : []
    if (!prices.length) return base
    const low = Math.min(...prices, ...(base?.priceRange ? [base.priceRange.minValue] : []))
    const high = Math.max(...prices, ...(base?.priceRange ? [base.priceRange.maxValue] : []))
    return { ...base, priceRange: { minValue: low, maxValue: high } }
  }, [])

  useEffect(() => {
    if (!container.current) return
    const chart = createChart(container.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: '#11151b' }, textColor: '#8191a6', fontFamily: 'Consolas, "Microsoft YaHei", sans-serif', fontSize: 11, attributionLogo: true },
      grid: { vertLines: { color: '#1c2530' }, horzLines: { color: '#222c39' } },
      crosshair: { mode: CrosshairMode.Normal, vertLine: { color: '#8191a6', labelBackgroundColor: '#303e50' }, horzLine: { color: '#8191a6', labelBackgroundColor: '#303e50' } },
      rightPriceScale: { borderColor: '#293340', minimumWidth: 84, scaleMargins: { top: 0.12, bottom: 0.1 } },
      timeScale: { borderColor: '#293340', timeVisible: true, secondsVisible: false, rightOffset: 8, shiftVisibleRangeOnNewBar: true, tickMarkFormatter: (time: Time, type: TickMarkType) => [TickMarkType.DayOfMonth, TickMarkType.Month, TickMarkType.Year].includes(type) && typeof time === 'number' ? dateFormat.format(time * 1000) : formatTime(time) },
      localization: { locale: 'zh-CN', timeFormatter: (time: Time) => formatTime(time, true) },
    })
    const series = chart.addSeries(CandlestickSeries, { upColor: '#5dccb0', downColor: '#de727c', wickUpColor: '#5dccb0', wickDownColor: '#de727c', borderVisible: false, priceLineVisible: false, lastValueVisible: false, autoscaleInfoProvider: autoscale })
    const move = (param: MouseEventParams) => {
      const bar = param.seriesData.get(series)
      setHover(bar && 'open' in bar && typeof bar.time === 'number' ? { time: bar.time, open: bar.open, high: bar.high, low: bar.low, close: bar.close } : null)
    }
    chart.subscribeCrosshairMove(move)
    runtime.current = { chart, series, lines: new Map(), quote: null }
    candlesRef.current = []
    historyKey.current = ''
    setHover(null)
    return () => {
      chart.unsubscribeCrosshairMove(move)
      chart.remove()
      runtime.current = null
      candlesRef.current = []
    }
  }, [market.symbol, autoscale])

  useEffect(() => {
    const rt = runtime.current
    if (!rt) return
    const tickDigits = tickDecimals(tickSize)
    const digits = Math.max(tickDigits ?? 0, Math.min(10, Math.max(0, Math.trunc(precision))))
    rt.series.applyOptions({ priceFormat: { type: 'price', precision: digits, minMove: tickDigits === null ? 10 ** -digits : Number(tickSize) } })
    rt.chart.applyOptions({ localization: { priceFormatter: (price: number) => number(price, digits) } })
  }, [precision, market.symbol, tickSize])

  useEffect(() => {
    const rt = runtime.current, element = container.current
    setPickedHover(null)
    if (!rt || !element || !picking || !pickTarget) return
    let guide: IPriceLine | null = null
    const picker = createPricePicker({
      enabled: () => runtime.current === rt && pickState.current.picking && pickState.current.target === pickTarget && pickState.current.symbol === market.symbol && candlesRef.current.length > 0 && !!pickCallback.current,
      geometry: () => {
        const pane = rt.chart.panes()[0]
        const rect = pane?.getHTMLElement()?.getBoundingClientRect()
        if (!rect) return null
        const size = rt.chart.paneSize(0)
        return { left: rect.left, top: rect.top, width: rect.width, height: rect.height, paneWidth: size.width, paneHeight: size.height, leftAxisWidth: rt.chart.priceScale('left').width(), rightAxisWidth: rt.chart.priceScale('right').width() }
      },
      priceAt: point => rt.series.coordinateToPrice(point.y),
      tickSize: () => tickSize,
      precision: () => precision,
      onPreview: price => {
        setPickedHover(price)
        if (price === null) { if (guide) rt.series.removePriceLine(guide); guide = null; return }
        const options = { price: Number(price), color: '#a8cff4', lineWidth: 1 as const, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: pickTargetLabels[pickTarget], axisLabelTextColor: '#132030' }
        if (guide) guide.applyOptions(options)
        else guide = rt.series.createPriceLine(options)
      },
      onPick: price => pickCallback.current?.(price),
    })
    // Pointer coordinates cover the right price axis too; the library's click event
    // is limited to the plot. No preventDefault/capture means panning stays native.
    const down = (event: PointerEvent) => picker.down(event)
    const move = (event: PointerEvent) => picker.move(event)
    const up = (event: PointerEvent) => picker.up(event)
    const cancel = () => picker.cancel()
    element.addEventListener('pointerdown', down, { passive: true })
    element.addEventListener('pointermove', move, { passive: true })
    element.addEventListener('pointerup', up, { passive: true })
    element.addEventListener('pointercancel', cancel, { passive: true })
    element.addEventListener('pointerleave', cancel, { passive: true })
    element.addEventListener('wheel', cancel, { passive: true })
    return () => {
      element.removeEventListener('pointerdown', down)
      element.removeEventListener('pointermove', move)
      element.removeEventListener('pointerup', up)
      element.removeEventListener('pointercancel', cancel)
      element.removeEventListener('pointerleave', cancel)
      element.removeEventListener('wheel', cancel)
      // The symbol lifetime effect may already have removed the old chart.
      if (runtime.current === rt && guide) rt.series.removePriceLine(guide)
    }
  }, [picking, pickTarget, pickDisabled, tickSize, precision, market.symbol])

  useEffect(() => {
    const rt = runtime.current
    if (!rt) return
    const previous = candlesRef.current
    const key = JSON.stringify(market.candles ?? [])
    const base = key === historyKey.current ? previous : reconcileCandles(market.candles, previous)
    const next = mergeQuoteIntoCandles(base, stale ? { ...market, stale: true } : market).slice(-2000)
    historyKey.current = key
    // Corrected history must retain the user's viewport; live bars update in place.
    const incremental = previous.length > 0 && next.length >= previous.length && next.length <= previous.length + 1 && previous.slice(0, -1).every((bar, i) => sameBar(bar, next[i])) && next[previous.length - 1]?.time === previous.at(-1)?.time
    if (incremental) {
      if (!sameBar(previous[previous.length - 1], next[previous.length - 1])) rt.series.update(chartBar(next[previous.length - 1]))
      if (next.length > previous.length) rt.series.update(chartBar(next[next.length - 1]))
    } else if (next.length !== previous.length || next.some((bar, i) => !previous[i] || !sameBar(bar, previous[i]))) {
      const range = rt.chart.timeScale().getVisibleLogicalRange()
      const anchorIndex = range && previous.length ? Math.max(0, Math.min(previous.length - 1, Math.floor(range.from))) : 0
      const anchor = previous[anchorIndex]?.time
      rt.series.setData(next.map(chartBar))
      if (!previous.length) latestRange(rt.chart, next.length, spanRef.current)
      else if (range && anchor !== undefined) {
        const newIndex = next.findIndex(bar => bar.time === anchor)
        if (newIndex >= 0) rt.chart.timeScale().setVisibleLogicalRange({ from: range.from + newIndex - anchorIndex, to: range.to + newIndex - anchorIndex })
      }
    }
    candlesRef.current = next
    setLatest(next.at(-1) ?? null)
    setCandleCount(next.length)
    const lastPrice = market.last?.trim() ? Number(market.last) : NaN
    if (market.source === 'gate' && Number.isFinite(lastPrice) && lastPrice > 0) {
      const closed = market.status === 'closed'
      const options = { price: lastPrice, color: stale || closed ? '#c9a370' : '#b7f36d', lineWidth: 1 as const, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: stale ? '最近报价 · 陈旧' : closed ? '最近报价 · 休市' : '最新报价', axisLabelTextColor: '#152008' }
      if (rt.quote) rt.quote.applyOptions(options)
      else rt.quote = rt.series.createPriceLine(options)
    } else if (rt.quote) { rt.series.removePriceLine(rt.quote); rt.quote = null }
  }, [market, stale])

  useEffect(() => {
    const rt = runtime.current
    if (!rt) return
    scaleSettings.current = { fit: fitGrid, lines: overlays }
    const ids = new Set(overlays.map(line => line.id))
    for (const [id, line] of rt.lines) {
      if (!ids.has(id)) { rt.series.removePriceLine(line); rt.lines.delete(id) }
    }
    for (const model of overlays) {
      const options = { price: model.price, title: model.kind === 'preview' ? '' : model.title, color: model.color, lineWidth: 1 as const, lineStyle: styles[model.lineStyle], axisLabelVisible: model.axisLabelVisible }
      const line = rt.lines.get(model.id)
      if (line) line.applyOptions(options)
      else rt.lines.set(model.id, rt.series.createPriceLine(options))
    }
    rt.series.applyOptions({ autoscaleInfoProvider: autoscale })
  }, [overlays, fitGrid, market.symbol, autoscale])

  const resetView = (size = span) => {
    const rt = runtime.current
    if (!rt) return
    setSpan(size)
    spanRef.current = size
    latestRange(rt.chart, candlesRef.current.length, size)
    rt.chart.priceScale('right').applyOptions({ autoScale: true })
  }
  const toggleFit = () => {
    setFitGrid(value => !value)
    runtime.current?.chart.priceScale('right').applyOptions({ autoScale: true })
  }
  const candle = hover ?? latest
  return <div className="chart-panel" data-chart-library="tradingview-lightweight-charts" data-candle-count={candleCount} data-overlay-count={overlays.length}>
    <div className="chart-toolbar">
      <div className="chart-timeframes"><span>1分钟</span>{[40, 80, 120].map(n => <Button type="button" variant="ghost" size="sm" key={n} aria-label={`显示最近 ${n} 根 K 线`} aria-pressed={span === n} className={span === n ? 'active' : ''} onClick={() => resetView(n)}>{n} 根</Button>)}</div>
      <div className="chart-tools">
        <Button type="button" variant="ghost" size="sm" aria-label="显示或隐藏网格预览线" aria-pressed={showGrid} className={showGrid ? 'active' : ''} onClick={() => setShowGrid(value => !value)}><Layers3 size={13}/>网格</Button>
        <Button type="button" variant="ghost" size="sm" aria-label="显示或隐藏委托及其止盈止损线" aria-pressed={showOrders} className={showOrders ? 'active' : ''} onClick={() => setShowOrders(value => !value)}>委托</Button>
        <Button type="button" variant="ghost" size="sm" aria-label="显示或隐藏持仓及其止盈止损线" aria-pressed={showPositions} className={showPositions ? 'active' : ''} onClick={() => setShowPositions(value => !value)}>持仓</Button>
        <Button type="button" variant="ghost" size="sm" aria-label="缩放价格轴以显示全部网格" aria-pressed={fitGrid} className={fitGrid ? 'active' : ''} onClick={toggleFit}><Maximize2 size={13}/>适应网格</Button>
        <Button type="button" variant="ghost" size="sm" aria-label="回到最新行情" title="回到最新行情" onClick={() => resetView()}><Crosshair size={14}/></Button>
      </div>
    </div>
    <div className="ohlc"><span>{market.symbol} <i>· Gate OHLC / 最新报价采样</i></span>{candle && <span><time>{formatTime(candle.time as UTCTimestamp, true)}</time> 开 <b>{number(candle.open, precision)}</b> 高 <b>{number(candle.high, precision)}</b> 低 <b>{number(candle.low, precision)}</b> 收 <b>{number(candle.close, precision)}</b></span>}</div>
    {pickTarget && <div className="chart-pick-hint" role="status"><Crosshair size={14}/><span>{pickDisabled ? '当前参数已锁定，无法拾价' : tickDecimals(tickSize) === null ? '等待 Gate 品种报价单位后可拾价' : !hasCandles ? '等待有效 K 线后可拾价' : `点击 K 线区域或右侧价格轴，设置${pickTargetLabels[pickTarget]} · Esc 退出`}</span>{picking && <output aria-label="图表待选价格">{pickedHover ?? '移动鼠标选择价格'}</output>}</div>}
    <div className={`chart-container ${picking ? 'is-picking' : ''}`}>
      <div ref={container} className="tv-chart" role="img" aria-label={`${market.symbol} TradingView 1 分钟 K 线，${candleCount} 根，${overlays.length} 条价格线；支持拖动、缩放和十字光标`}/>
      {!candleCount && <div className="chart-empty">等待 Gate 有效 K 线及报价</div>}
    </div>
    <div className="chart-legend"><span><i className="legend-dash"/>预览格位</span><span><i className="legend-local"/>待挂 / 待核对 {localCount}</span><span><i className="legend-order"/>Gate 委托 {orderCount}</span><span><i className="legend-sl"/>止损</span><span className="chart-tip">拖动平移 · 滚轮缩放 · UTC+8</span></div>
    <div className="chart-attribution"><a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView Lightweight Charts™</a><a href="/assets/tradingview-license.txt" target="_blank" rel="noreferrer">Copyright (с) 2025 TradingView, Inc.</a><span>行情来自 Gate</span></div>
  </div>
}
