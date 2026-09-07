import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, ArrowDownLeft, ArrowUpRight, Bookmark, BookOpen, Check, ChevronRight, CircleHelp, Clock3, Copy, Crosshair, Gauge, Grid2X2, Info, Layers3, LockKeyhole, Pencil, Play, PlugZap, RefreshCw, Settings2, ShieldCheck, Sparkles, Square, Trash2, Wallet, X, Zap } from 'lucide-react'
import { Input } from '@/components/motion/input'
import { Button } from '@/components/motion/button/base'
import { StatefulButton, type ButtonState } from '@/components/motion/button/stateful'
import { Tabs, TabsList, TabsTrigger } from '@/components/motion/tabs'
import { Table, type TableColumn } from '@/components/motion/table'
import { Combobox, ComboboxContent, ComboboxEmpty, ComboboxInput, ComboboxItem, ComboboxList, ComboboxTrigger } from '@/components/motion/combobox'
import { RangeSlider } from '@/components/motion/range-slider'
import { Switch } from '@/components/motion/switch'
import { Checkbox } from '@/components/motion/checkbox'
import { Drawer } from '@/components/motion/drawer'
import { AnimatedToastStack, useAnimatedToastStack } from '@/components/motion/animated-toast-stack'
import { api, number, clockTime, managedTradeId, protectionPatch } from './api'
import type { ProtectionInput } from './api'
import { creationNotice, isPreviewFresh, previewMessages, resumePresentation, startPresentation, type StartRequestState } from './strategyUi'
import { createPreviewScheduler, type PreviewScheduler } from './previewScheduler'
import { fillTime, fillTypeLabel, newestFills } from './tradeHistory'
import GridChart from './GridChart'
import SmartPlanner from './SmartPlanner'
import LatencyDiagnostics from './LatencyDiagnostics'
import { applyPlan, pickedRange } from './plannerUi'
import type { PickTarget } from './chartPicking'
import type { Connection, GridCell, GridConfig, Market, MarketEvent, Preview, Snapshot, Template, TradeRow } from './types'

const DEFAULT: GridConfig = { symbol: 'XAUUSD', direction: 'long', lower_price: '', upper_price: '', volume: '', grid_count: 14, spacing: 'arithmetic', repeat: true, stop_loss: null }
const emptyConnection: Connection = { configured: false, connected: false, checked_at: null, error: null, live_trading_ready: false }
const activeStatuses = ['running', 'starting', 'stopping', 'closing', 'paused', 'legacy_paused', 'error', 'recovering']
const stateLabels: Record<string, string> = { ready: '待提交', armed: '旧格位已停用', pending: '已挂 Gate', partial: '部分成交', partially_filled: '部分成交', open: '已持仓', waiting: '待挂条件', submitting: '提交中', reconciling: '核对成交', unknown: '结果待核对', cancelled: '已取消', canceled: '已取消', filled: '已成交', done: '本轮完成', running: '运行中', stopped: '已停止', completed: '已完成', paused: '已暂停', legacy_paused: '旧策略已停用', error: '异常暂停', starting: '正在铺单', stopping: '正在停止', closing: '正在撤单并平仓', recovering: '恢复核对中', closed: '已平仓' }
const blankProtection: ProtectionInput = { takeProfit: '', stopLoss: '', clearTakeProfit: false, clearStopLoss: false }
type ManageTarget = { kind: 'order' | 'position'; row: TradeRow }
const newerMarket = (a?: Market | null, b?: Market | null) => !a ? b ?? null : !b ? a : Number(a.received_at) > Number(b.received_at) ? a : b
const finite = (v: unknown): number | null => v === null || v === undefined || v === '' || !Number.isFinite(Number(v)) ? null : Number(v)
const errorText = (e: unknown) => e instanceof Error ? (e.name === 'TimeoutError' ? '请求超时，操作结果待核对，请查看最新策略状态。' : e.message) : '请求未完成，请重试。'
const fieldClasses = { field: 'field-shell', input: 'field-input', label: 'field-label' }

function Stat({ label, value, hint, accent = false, icon }: { label: string; value: string; hint: string; accent?: boolean; icon: React.ReactNode }) {
  return <div className="stat"><div className="stat-label">{label}{icon}</div><div className={`stat-value ${accent ? 'positive' : ''}`}>{value}<span> USD</span></div><div className="stat-hint">{hint}</div></div>
}

export default function App() {
  const [config, setConfig] = useState<GridConfig>(DEFAULT)
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [feed, setFeed] = useState<MarketEvent | null>(null)
  const [streamError, setStreamError] = useState('')
  const [now, setNow] = useState(Date.now())
  const [preview, setPreview] = useState<Preview | null>(null)
  const [previewKey, setPreviewKey] = useState('')
  const [previewReceivedAt, setPreviewReceivedAt] = useState<number | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [online, setOnline] = useState(false)
  const [fetchError, setFetchError] = useState('')
  const [actionError, setActionError] = useState('')
  const [startState, setStartState] = useState<StartRequestState>('idle')
  const [cancelState, setCancelState] = useState<ButtonState>('idle')
  const [resumeState, setResumeState] = useState<ButtonState>('idle')
  const [resumeError, setResumeError] = useState('')
  const [drawer, setDrawer] = useState<'connection' | 'templates' | 'order' | 'position' | 'close' | 'planner' | 'diagnostics' | null>(null)
  const [pricePick, setPricePick] = useState<{target: PickTarget; symbol: string; pair: boolean; first: string | null} | null>(null)
  const [anchorPick, setAnchorPick] = useState<{value: string; seq: number; symbol: string} | null>(null)
  const formLock = useRef(false)
  const closePlanner = useCallback(() => setDrawer(null), [])
  const [manageTarget, setManageTarget] = useState<ManageTarget | null>(null)
  const [managePrice, setManagePrice] = useState('')
  const [protection, setProtection] = useState<ProtectionInput>(blankProtection)
  const [manageState, setManageState] = useState<ButtonState>('idle')
  const [manageError, setManageError] = useState('')
  const [closeStrategyId, setCloseStrategyId] = useState('')
  const [tableTab, setTableTab] = useState('grid')
  const [recordsView, setRecordsView] = useState(0)
  const [apiKey, setApiKey] = useState('')
  const [apiSecret, setApiSecret] = useState('')
  const [remember, setRemember] = useState(true)
  const [connectionActionError, setConnectionActionError] = useState('')
  const [checking, setChecking] = useState(false)
  const [connection, setConnection] = useState<Connection>(emptyConnection)
  const [templates, setTemplates] = useState<Template[]>([])
  const [templateName, setTemplateName] = useState('')
  const [templateBusy, setTemplateBusy] = useState(false)
  const [advanced, setAdvanced] = useState(false)
  const [lastSync, setLastSync] = useState<number | null>(null)
  const [retry, setRetry] = useState(0)
  const operationId = useRef<{ key: string; id: string } | null>(null)
  const busy = useRef(false)
  const previewRunner = useRef<PreviewScheduler | null>(null)
  const panel = useRef<HTMLDivElement>(null)
  const requestedSymbol = useRef(config.symbol)
  const rangeInitialized = useRef<string | null>(null)
  const volumeInitialized = useRef<string | null>(null)
  requestedSymbol.current = config.symbol
  const { toasts, showToast, dismissToast } = useAnimatedToastStack({ limit: 4 })
  const configKey = JSON.stringify(config)

  const adoptState = useCallback((next: Snapshot) => {
    if (next.market && next.market.symbol !== requestedSymbol.current) return
    setSnapshot(previous => ({ ...next, market: newerMarket(previous?.market?.symbol === requestedSymbol.current ? previous.market : null, next.market) })); setOnline(true); setFetchError(''); setLastSync(Date.now())
    const restored = next.strategies.find(s => s.config.symbol === requestedSymbol.current && activeStatuses.includes(s.status))
    if (restored) setConfig(current => current.symbol === requestedSymbol.current ? restored.config : current)
    if (next.connection) setConnection(next.connection)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try { const next = await api<Snapshot>(`/state?symbol=${encodeURIComponent(config.symbol)}`, 'GET', undefined, controller.signal); if (!controller.signal.aborted && requestedSymbol.current === config.symbol) adoptState(next) }
      catch (e) { if (!controller.signal.aborted) { setOnline(false); setFetchError(errorText(e)) } }
      finally { if (!controller.signal.aborted) timer = setTimeout(poll, 2000) }
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [config.symbol, retry, adoptState])

  useEffect(() => {
    const symbol = config.symbol
    const stream = new EventSource(`/api/market/stream?symbol=${encodeURIComponent(symbol)}`)
    setStreamError('')
    const receive = (event: MessageEvent<string>) => {
      if (requestedSymbol.current !== symbol) return
      try {
        const next = JSON.parse(event.data) as MarketEvent
        if (next.market && next.market.symbol !== symbol) return
        setFeed(previous => ({ ...next, market: newerMarket(previous?.market?.symbol === symbol ? previous.market : null, next.market), spec: next.spec ?? previous?.spec, symbols: next.symbols ?? previous?.symbols }))
        setStreamError(next.error ?? '')
      } catch { setStreamError('行情数据暂时无法读取，正在等待下一次更新。') }
    }
    stream.addEventListener('market', receive as EventListener)
    stream.onerror = () => setStreamError('行情连接中断，正在自动重连。')
    return () => { stream.removeEventListener('market', receive as EventListener); stream.close() }
  }, [config.symbol, retry])

  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer) }, [])

  useEffect(() => { if (drawer === 'connection') setRemember(connection.configured ? !!connection.remembered : true) }, [drawer, connection.configured, connection.remembered])

  useEffect(() => {
    setPreview(null); setPreviewKey(''); setPreviewReceivedAt(null); setPreviewError('')
    if (!connection.connected || !config.lower_price || !config.upper_price || !config.volume) return
    const runner = createPreviewScheduler({
      fetch: signal => api<Preview>('/grid/preview', 'POST', config, signal),
      onResult: value => { setPreview(value); setPreviewKey(configKey); setPreviewReceivedAt(Date.now()); setPreviewError('') },
      onError: error => { setPreview(null); setPreviewKey(''); setPreviewReceivedAt(null); setPreviewError(errorText(error)) },
    })
    previewRunner.current = runner
    runner.refresh()
    return () => { runner.dispose(); if (previewRunner.current === runner) previewRunner.current = null }
  }, [configKey, connection.connected, connection.checked_at])

  useEffect(() => {
    previewRunner.current?.refresh()
  }, [lastSync, connection.live_trading_ready, retry])

  useEffect(() => {
    if (!drawer || (drawer === 'planner' || drawer === 'diagnostics')) return
    const savedFocus = document.activeElement as HTMLElement | null
    const frame = requestAnimationFrame(() => panel.current?.querySelector<HTMLElement>('button,input')?.focus())
    const trap = (e: KeyboardEvent) => {
      if (e.key !== 'Tab' || !panel.current) return
      const nodes = Array.from(panel.current.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),a[href],[tabindex="0"]')).filter(x => x.getClientRects().length)
      const first = nodes[0], last = nodes.at(-1)
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus() }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus() }
    }
    document.addEventListener('keydown', trap)
    return () => { cancelAnimationFrame(frame); document.removeEventListener('keydown', trap); savedFocus?.focus() }
  }, [drawer])

  const selected = snapshot
  const market = newerMarket(snapshot?.market?.symbol === config.symbol ? snapshot.market : null, feed?.market?.symbol === config.symbol ? feed.market : null)
  const symbols = feed?.symbols?.length ? feed.symbols : snapshot?.symbols ?? []
  const spec = (snapshot?.spec?.symbol === config.symbol ? snapshot.spec : null) ?? (feed?.spec?.symbol === config.symbol ? feed.spec : null) ?? symbols.find(s => s.symbol === config.symbol)
  const precision = spec?.price_precision ?? Math.min(8, String(market?.last ?? '').split('.')[1]?.length ?? 2)
  const marketAge = market?.received_at ? Math.max(0, now / 1000 - market.received_at) : null
  const stale = !market || market.stale || marketAge === null || marketAge > 5
  const feedError = streamError || selected?.feed_error
  const marketClosed = market?.status === 'closed'
  const active = selected?.strategies.find(s => s.config.symbol === config.symbol && activeStatuses.includes(s.status))
  const latest = active ?? selected?.strategies.filter(s => s.config.symbol === config.symbol).at(-1)
  const relevantOrders = selected?.orders.filter(o => o.symbol === config.symbol) ?? []
  const pendingOrders = relevantOrders.filter(o => ['ready', 'armed', 'pending', 'partial', 'partially_filled', 'open', 'waiting', 'submitting', 'reconciling', 'unknown'].includes(o.status ?? ''))
  const positions = selected?.positions.filter(o => o.symbol === config.symbol) ?? []
  const fills = useMemo(() => newestFills(config.symbol, selected?.fills ?? []), [config.symbol, selected?.fills])
  const formDisabled = !!active || startState === 'loading' || manageState === 'loading' || resumeState === 'loading'
  formLock.current = formDisabled
  useEffect(() => {
    if (pricePick && (pricePick.symbol !== config.symbol || (formDisabled && pricePick.target !== 'anchor'))) setPricePick(null)
  }, [config.symbol, formDisabled, pricePick])
  useEffect(() => {
    if (!pricePick) return
    const cancel = (event: KeyboardEvent) => { if (event.key === 'Escape') setPricePick(null) }
    document.addEventListener('keydown', cancel)
    return () => document.removeEventListener('keydown', cancel)
  }, [pricePick])
  const resumeControl = resumePresentation(active, { online, connected: connection.connected, ready: connection.live_trading_ready })
  const managedOrders = relevantOrders.filter(row => managedTradeId(row, 'order', 'cancel'))
  const managedPositions = positions.filter(row => managedTradeId(row, 'position'))
  const unresolvedOperations = selected?.unresolved_operations?.filter(operation => ['prepared', 'submitted', 'unknown'].includes(operation.status) && selected.strategies.some(strategy => strategy.id === operation.strategy_id && strategy.config.symbol === config.symbol)) ?? []
  const closableStrategies = selected?.strategies.filter(strategy => strategy.config.symbol === config.symbol && (activeStatuses.includes(strategy.status) || [...managedOrders, ...managedPositions].some(row => row.strategy_id === strategy.id))) ?? []
  const closeStrategy = closableStrategies.find(strategy => strategy.id === closeStrategyId)
  const closeOrders = managedOrders.filter(row => row.strategy_id === closeStrategyId)
  const closePositions = managedPositions.filter(row => row.strategy_id === closeStrategyId)
  const currentManagedRow = manageTarget ? (manageTarget.kind === 'order' ? selected?.orders : selected?.positions)?.find(row => row.id === manageTarget.row.id && row.strategy_id === manageTarget.row.strategy_id) : undefined
  const managementAllowed = online && connection.connected && !!manageTarget && !!managedTradeId(currentManagedRow, manageTarget.kind)
  const legacy = active?.status === 'legacy_paused' || active?.execution_mode === 'legacy_local' || active?.execution_mode === 'legacy'
  const basicPreview = useMemo<Preview | null>(() => {
    const lower = finite(config.lower_price), upper = finite(config.upper_price), count = config.grid_count
    if (connection.connected || lower === null || upper === null || lower <= 0 || lower >= upper || !Number.isInteger(count) || count < 2 || count > 100 || spec?.price_precision == null) return null
    const levels = Array.from({ length: count + 1 }, (_, i) => (config.spacing === 'geometric' ? lower * (upper / lower) ** (i / count) : lower + (upper - lower) * i / count).toFixed(precision))
    if (new Set(levels).size !== levels.length) return null
    const quote = finite(config.direction === 'long' ? market?.ask : market?.bid)
    return { can_start: false, sufficient_margin: false, levels, cells: levels.slice(0, -1).map((p, index) => ({ index, entry_price: config.direction === 'long' ? p : levels[index + 1], take_profit: config.direction === 'long' ? levels[index + 1] : p, eligible: quote !== null && (config.direction === 'long' ? Number(p) < quote : Number(levels[index + 1]) > quote) })), warnings: [] }
  }, [configKey, connection.connected, spec?.price_precision, precision, market?.ask, market?.bid])
  const validPreview = previewKey === configKey ? preview : basicPreview
  const previewFresh = isPreviewFresh(validPreview, previewReceivedAt, Math.max(now, previewReceivedAt ?? 0))
  const accountSyncing = connection.connected && !previewFresh && (!selected?.account || selected.account.stale === true || validPreview?.account?.stale === true)
  const previewFeedback = previewMessages(validPreview, { connected: connection.connected, accountStale: accountSyncing })
  const startButton = startPresentation(active ? { ...active, pending_count: active.pending_count ?? managedOrders.filter(row => row.strategy_id === active.id).length, position_count: active.position_count ?? managedPositions.filter(row => row.strategy_id === active.id).length } : null, startState)
  const running = active?.status === 'running'
  const visibleError = actionError || selected?.engine_error || fetchError
  const eligibleCount = validPreview?.cells.filter(c => c.eligible).length ?? 0
  const volume = finite(config.volume)
  const grossValues = validPreview?.cells.map(c => finite(c.gross_profit)).filter((v): v is number => v !== null) ?? []
  const grossMin = grossValues.length ? Math.min(...grossValues) : null
  const grossMax = grossValues.length ? Math.max(...grossValues) : null
  const estimatedMargin = validPreview?.estimated_margin
  const canStart = online && connection.connected && connection.live_trading_ready && previewFresh && !accountSyncing && !stale && !marketClosed && !!validPreview?.can_start && !active && !selected?.engine_error
  const startHint = !connection.connected ? '连接 Gate 账户后可开始实盘策略。' : !connection.live_trading_ready ? '账户暂不具备交易条件，请在连接设置中查看状态。' : previewFeedback.syncMessage || (marketClosed ? '当前品种休市，等待开盘。' : stale ? '等待新鲜报价后可开始。' : !config.volume ? '请输入每网格手数。' : !previewFresh && !previewFeedback.blockers.length ? '正在更新策略预览…' : previewFeedback.fallbackMessage)

  useEffect(() => {
    const quote = finite(market?.last)
    if (quote === null || quote <= 0 || stale || rangeInitialized.current === config.symbol || active) return
    rangeInitialized.current = config.symbol
    setConfig(current => current.lower_price || current.upper_price ? current : { ...current, lower_price: (quote * .98).toFixed(precision), upper_price: (quote * 1.02).toFixed(precision) })
  }, [config.symbol, market?.last, stale, active, precision])

  useEffect(() => {
    if (!spec?.volume_min || volumeInitialized.current === config.symbol || active) return
    volumeInitialized.current = config.symbol
    setConfig(current => current.volume ? current : { ...current, volume: spec.volume_min! })
  }, [config.symbol, spec?.volume_min, active])
  const update = <K extends keyof GridConfig>(key: K, value: GridConfig[K]) => { setConfig(c => ({ ...c, [key]: value })); setActionError(''); setStartState('idle'); setCancelState('idle') }

  function beginPick(target: PickTarget, pair = false) {
    if (formLock.current && target !== 'anchor') return
    setPricePick({target, pair, first: null, symbol: config.symbol})
  }

  function receivePickedPrice(value: string) {
    if (!pricePick || pricePick.symbol !== requestedSymbol.current || (formLock.current && pricePick.target !== 'anchor')) return
    if (pricePick.target === 'anchor') {
      setAnchorPick({value, seq: Date.now(), symbol: requestedSymbol.current}); setPricePick(null); setDrawer('planner'); return
    }
    if (pricePick.pair && !pricePick.first) {
      setPricePick({...pricePick, target: 'upper', first: value}); return
    }
    if (pricePick.pair) {
      try { const range = pickedRange(pricePick.first!, value); setConfig(c => ({...c, ...range})); setActionError(''); setStartState('idle'); setCancelState('idle') }
      catch (e) { setActionError(errorText(e)); return }
    } else update(pricePick.target === 'lower' ? 'lower_price' : pricePick.target === 'upper' ? 'upper_price' : 'stop_loss', value)
    setPricePick(null)
  }

  function applySmartPlan(planned: GridConfig): string | null {
    try {
      const next = applyPlan(config, planned, formLock.current)
      setConfig(next); setPricePick(null); setPreviewKey(''); setActionError(''); setStartState('idle'); setCancelState('idle'); setDrawer(null)
      showToast({title: '智能方案已填入，请核对策略预览', status: 'info'})
      return null
    } catch (e) { return errorText(e) }
  }

  function selectSymbol(symbol: string) {
    setPricePick(null); setAnchorPick(null)
    const existing = snapshot?.strategies.find(s => s.config.symbol === symbol && activeStatuses.includes(s.status))
    if (existing) setConfig(existing.config)
    else {
      setConfig({ ...DEFAULT, symbol, volume: symbols.find(s => s.symbol === symbol)?.volume_min ?? '' })
    }
    rangeInitialized.current = null; volumeInitialized.current = null
    setPreview(null); setPreviewKey(''); setFeed(null); setActionError(''); setStartState('idle'); setCancelState('idle'); setResumeState('idle'); setResumeError(''); operationId.current = null
  }

  async function start() {
    if (busy.current || !canStart) return
    busy.current = true; setStartState('loading'); setCancelState('idle'); setActionError('')
    if (operationId.current?.key !== configKey) operationId.current = { key: configKey, id: crypto.randomUUID() }
    try {
      const result = await api<Snapshot>('/strategies', 'POST', { config, request_id: operationId.current.id })
      adoptState(result); operationId.current = null; setStartState('idle'); setTableTab('orders')
      const strategy = result.strategies.find(item => item.config.symbol === config.symbol && activeStatuses.includes(item.status))
      showToast(creationNotice(strategy))
    } catch (e) { setActionError(errorText(e)); setStartState('error') }
    finally { busy.current = false }
  }

  async function cancel() {
    if (busy.current || !active) return
    busy.current = true; setCancelState('loading'); setActionError(''); setResumeError(''); setResumeState('idle')
    try {
      const result = await api<Snapshot>(`/strategies/${active.id}/cancel`, 'POST')
      adoptState(result); setStartState('idle'); operationId.current = null
      const remaining = result.strategies.find(s => s.id === active.id)
      const finished = !remaining || ['stopped', 'completed'].includes(remaining.status)
      setCancelState(finished ? 'success' : 'idle')
      showToast({ title: finished ? '策略已停止，委托已核对' : '已请求停止，正在撤销并核对委托', description: '已有仓位及其原生止盈止损保留。', status: finished ? 'success' : 'info' })
    } catch (e) { setActionError(errorText(e)); setCancelState('error') }
    finally { busy.current = false }
  }

  async function resumeStrategy() {
    if (busy.current || !active || !resumeControl.allowed) return
    const strategyId = active.id
    busy.current = true; setResumeState('loading'); setResumeError(''); setActionError('')
    try {
      const result = await api<Snapshot>(`/strategies/${encodeURIComponent(strategyId)}/resume`, 'POST')
      adoptState(result); setResumeState('idle'); setStartState('idle'); setCancelState('idle'); setTableTab('orders')
      const strategy = result.strategies.find(item => item.id === strategyId)
      const resumed = strategy && ['running', 'starting'].includes(strategy.status)
      showToast({ title: resumed ? '当前策略已恢复' : strategy?.status === 'paused' ? '策略仍处于暂停状态' : '恢复请求已处理，正在核对状态', description: resumed ? '继续使用当前参数；已核对可补的格位由后台逐步提交。' : strategy?.error || '请查看最新策略状态与恢复条件。', status: 'info' })
    } catch (error) { setResumeError(errorText(error)); setResumeState('error') }
    finally { busy.current = false }
  }

  async function checkConnection() {
    setChecking(true); setConnectionActionError('')
    try {
      const body = apiKey || apiSecret ? { key: apiKey, secret: apiSecret, remember } : { remember }
      const result = await api<Connection>('/connection/check', 'POST', body)
      setConnection(result); setApiKey(''); setApiSecret('')
      setRetry(r => r + 1)
      showToast({ title: result.connected ? 'Gate 账户连接成功' : '账户连接未成功', description: result.error, status: result.connected ? 'success' : 'error' })
    } catch (e) { setConnectionActionError(errorText(e)) }
    finally { setChecking(false) }
  }

  async function disconnect() {
    setChecking(true); setConnectionActionError('')
    try { setConnection(await api<Connection>('/connection/disconnect', 'POST')); setApiKey(''); setApiSecret(''); setRetry(r => r + 1) }
    catch (e) { setConnectionActionError(errorText(e)) }
    finally { setChecking(false) }
  }

  async function openTemplates() {
    setDrawer('templates'); setTemplateName(`${spec?.name ?? config.symbol} · ${config.grid_count} 格`)
    try { setTemplates(await api<Template[]>('/templates')) } catch (e) { setActionError(errorText(e)) }
  }

  async function saveTemplate() {
    setTemplateBusy(true)
    try { await api('/templates', 'POST', { name: templateName, config }); setTemplates(await api<Template[]>('/templates')); showToast({ title: '策略参数已保存', status: 'success' }) }
    catch (e) { setActionError(errorText(e)) }
    finally { setTemplateBusy(false) }
  }

  function openManagement(kind: 'order' | 'position', row: TradeRow) {
    if (!managedTradeId(row, kind) || busy.current) return
    setManageTarget({ kind, row }); setManagePrice(''); setProtection(blankProtection)
    setManageState('idle'); setManageError(''); setDrawer(kind)
  }

  function openCloseStrategy() {
    if (busy.current || !closableStrategies.length) return
    setCloseStrategyId(closableStrategies.some(strategy => strategy.id === active?.id) ? active!.id : closableStrategies[0].id)
    setManageState('idle'); setManageError(''); setDrawer('close')
  }

  async function mutateTrade(kind: 'order' | 'position', original: TradeRow, method: 'PUT' | 'DELETE', body?: unknown) {
    const row = (kind === 'order' ? selected?.orders : selected?.positions)?.find(item => item.id === original.id && item.strategy_id === original.strategy_id)
    const id = managedTradeId(row, kind, method === 'DELETE' ? 'cancel' : 'modify')
    if (busy.current || !online || !connection.connected || !row?.strategy_id || !id) return
    busy.current = true; setManageState('loading'); setManageError(''); setActionError('')
    try {
      const result = await api<Snapshot>(`/strategies/${encodeURIComponent(row.strategy_id)}/${kind === 'order' ? 'orders' : 'positions'}/${encodeURIComponent(id)}`, method, body)
      adoptState(result); setManageState('idle'); setDrawer(null); setManageTarget(null)
      const stillPending = method === 'DELETE' && result.orders.some(item => item.id === row.id && ['pending', 'partial', 'partially_filled', 'unknown', 'reconciling', 'submitting'].includes(item.status ?? ''))
      if (method === 'DELETE') {
        showToast({ title: stillPending ? '撤单结果正在核对' : '单笔撤单已处理', description: '该格位已停止补单，其他格位继续按策略执行。', status: stillPending ? 'info' : 'success' })
      } else {
        const status = result.operation?.status
        showToast({ title: status === 'confirmed' ? '修改已确认' : status === 'superseded' ? '委托已成交或仓位已关闭' : status === 'rejected' ? '修改未成功' : '修改结果待核对', description: status === 'superseded' ? '该资源状态已变化，请查看最新委托与持仓。' : result.operation?.error || '页面仅显示已同步的 Gate 价格；核对完成前不会把输入值视为生效。', status: status === 'confirmed' ? 'success' : status === 'rejected' ? 'error' : 'info' })
      }
    } catch (e) { const message = errorText(e); setManageError(message); setActionError(message); setManageState('error') }
    finally { busy.current = false }
  }

  async function saveManagement() {
    if (!managementAllowed || !manageTarget || !currentManagedRow) return
    try {
      const body: { price?: string; price_tp?: string; price_sl?: string } = protectionPatch(protection)
      if (manageTarget.kind === 'order') {
        const price = managePrice.trim() || currentManagedRow.price || ''
        if (!/^\d+(?:\.\d+)?$/.test(price) || !Number.isFinite(Number(price)) || Number(price) <= 0) throw new Error('请输入有效的委托触发价。')
        body.price = price
      }
      await mutateTrade(manageTarget.kind, currentManagedRow, 'PUT', body)
    } catch (e) { setManageError(errorText(e)); setManageState('error') }
  }

  async function closeStrategyPositions() {
    if (busy.current || !online || !connection.connected || !closeStrategy) return
    busy.current = true; setManageState('loading'); setManageError(''); setActionError('')
    try {
      const result = await api<Snapshot>(`/strategies/${encodeURIComponent(closeStrategy.id)}/close`, 'POST')
      adoptState(result); setManageState('idle'); setDrawer(null); operationId.current = null
      showToast({ title: '撤单并平仓请求已提交', description: `${closeStrategy.config.symbol} · ${closeStrategy.id}，最终结果将持续与 Gate 核对。`, status: 'info' })
    } catch (e) { setManageError(errorText(e)); setManageState('error') }
    finally { busy.current = false }
  }

  const closeDrawer = () => { if (manageState === 'loading') return; setDrawer(null); setManageTarget(null); setManageError(''); setApiKey(''); setApiSecret(''); setConnectionActionError('') }
  const drawerTitle = drawer === 'connection' ? '连接 Gate API' : drawer === 'templates' ? '策略模板' : drawer === 'order' ? '修改原生委托' : drawer === 'position' ? '修改仓位止盈止损' : '撤单并平仓'
  const hasManagementChange = !!(managePrice.trim() || protection.takeProfit.trim() || protection.stopLoss.trim() || protection.clearTakeProfit || protection.clearStopLoss)
  const tradeColumns: TableColumn<TradeRow>[] = [
    { key: 'id', header: '格位 / 委托 / 仓位', width: '170px', cell: r => <span className="mono">{String(r.id).length > 16 ? `${String(r.id).slice(0, 8)}…${String(r.id).slice(-4)}` : r.id}</span> },
    ...(tableTab === 'fills' ? [{ key: 'time', header: '成交时间 · UTC+8', width: '158px', cell: (r: TradeRow) => <time dateTime={typeof r.time === 'string' ? r.time : undefined}>{fillTime(r.time)}</time> }] : []),
    { key: 'side', header: '方向', width: '90px', cell: r => { const side = String(r.direction ?? r.side ?? '').toLowerCase(); return <span className={['long', 'buy', '2'].includes(side) ? 'positive' : ['short', 'sell', '1'].includes(side) ? 'negative' : 'muted'}>{['long', 'buy', '2'].includes(side) ? '做多' : ['short', 'sell', '1'].includes(side) ? '做空' : '—'}</span> } },
    { key: 'price', header: tableTab === 'positions' ? '开仓价' : '价格', align: 'right', cell: r => number(tableTab === 'fills' ? r.price : r.entry_price ?? r.price, precision) },
    tableTab === 'fills' ? { key: 'type', header: '成交类型', width: '162px', cell: (r: TradeRow) => <span title={r.exit_reason ?? undefined} className={r.type === 'awaiting_confirmation' || r.exit_confirmed === false ? 'warning-text' : undefined}>{fillTypeLabel(r)}</span> } : { key: 'take_profit', header: '止盈价', align: 'right', cell: (r: TradeRow) => number(r.take_profit, precision) },
    ...(tableTab === 'fills' ? [] : [{ key: 'stop_loss', header: '止损价', align: 'right' as const, cell: (r: TradeRow) => number(r.stop_loss, precision) }]),
    { key: 'volume', header: '手数', align: 'right', cell: r => number(r.volume, Math.max(2, String(r.volume ?? '').split('.')[1]?.length ?? 0)) },
    tableTab === 'positions' || tableTab === 'fills' ? { key: 'pnl', header: tableTab === 'positions' ? '浮动盈亏' : '已实现盈亏', align: 'right', cell: (r: TradeRow) => { const pnl = finite(r.unrealized_pnl ?? r.pnl); return <span className={pnl === null ? 'muted' : pnl >= 0 ? 'positive' : 'negative'}>{number(pnl)}</span> } } : { key: 'status', header: '状态', align: 'right', cell: (r: TradeRow) => <span className={`order-status ${r.status ?? ''}`}><i/>{stateLabels[r.status ?? ''] ?? r.status}</span> },
    ...(tableTab === 'fills' ? [] : [{ key: 'actions', header: '管理', align: 'right' as const, width: '150px', cell: (r: TradeRow) => {
      const kind = tableTab === 'positions' ? 'position' : 'order'
      const allowed = !!managedTradeId(r, kind)
      const cancellable = kind === 'order' && !!managedTradeId(r, kind, 'cancel')
      return allowed || cancellable ? <div className="trade-row-actions">{allowed && <Button variant="ghost" size="sm" disabled={!online || !connection.connected || manageState === 'loading' || resumeState === 'loading'} aria-label={`${kind === 'order' ? '修改委托' : '修改仓位止盈止损'} ${r.id}`} onClick={() => openManagement(kind, r)}><Pencil size={12}/>{kind === 'order' ? '修改' : '止盈止损'}</Button>}{cancellable && <Button variant="ghost" size="sm" className="trade-cancel" disabled={!online || !connection.connected || manageState === 'loading' || resumeState === 'loading'} aria-label={`撤销委托 ${r.id}`} onClick={() => void mutateTrade('order', r, 'DELETE')}>撤单</Button>}</div> : <span className="row-readonly" title={r.source !== 'gate' ? '尚未确认 Gate 委托，不可执行远端操作' : r.managed !== true ? '该行未确认归属本策略，仅供查看' : '当前状态待核对，暂不可操作'}>{r.source !== 'gate' ? '待挂 / 待核对' : r.managed !== true ? '只读' : '待核对'}</span>
    } }]),
  ]
  const gridColumns: TableColumn<GridCell & { id: string }>[] = [
    { key: 'index', header: '网格', width: '100px', cell: r => <span className="muted mono">#{String(r.index + 1).padStart(2, '0')}</span> },
    { key: 'entry_price', header: config.direction === 'long' ? '买入触发格位' : '卖出触发格位', align: 'right', cell: r => <span className={config.direction === 'long' ? 'positive' : 'negative'}>{number(r.entry_price, precision)}</span> },
    { key: 'take_profit', header: '止盈平仓价', align: 'right', cell: r => number(r.take_profit, precision) },
    { key: 'volume', header: '每格手数', align: 'right', cell: () => `${config.volume || '—'} 手` },
    { key: 'gross', header: '每格毛利估算', align: 'right', cell: r => number(r.gross_profit) },
    { key: 'eligible', header: '初始状态', align: 'right', cell: r => <span className={r.eligible ? 'positive' : 'muted'}>{r.eligible ? '可挂原生委托' : '等待挂单条件'}</span> },
  ]

  const managementContent = drawer === 'order' || drawer === 'position' ? <>
    <div className="management-scope"><span>{manageTarget?.row.symbol} · {drawer === 'order' ? 'Gate 委托' : 'Gate 仓位'}</span><strong className="mono">{manageTarget?.row.id}</strong><small>所属策略 <span className="mono">{manageTarget?.row.strategy_id}</span></small></div>
    <p className="drawer-subtext management-help">留空保持当前设置。输入 0 或勾选“清空”会移除对应的止盈或止损；另一项保持不变。</p>
    <p className="field-help">{drawer === 'order' ? '确认后，这笔委托的新价格及止盈止损也用于该格后续轮次。' : '修改仅作用于当前仓位；下一轮仍使用该格的委托设置。'}清空止盈会禁用该格自动补单。</p>
    <div className="drawer-fields management-fields">
      {drawer === 'order' && <Input label="委托触发价" aria-label="修改委托触发价" inputMode="decimal" value={managePrice} onChange={setManagePrice} placeholder={`保持 ${currentManagedRow?.price ?? '—'}`} disabled={manageState === 'loading'} classNames={fieldClasses}/>}
      <div className="protection-field"><Input label="止盈价" aria-label="修改止盈价" inputMode="decimal" value={protection.takeProfit} onChange={value => setProtection(previous => ({ ...previous, takeProfit: value }))} placeholder={`保持 ${currentManagedRow?.take_profit ?? '未设置'}`} disabled={manageState === 'loading' || protection.clearTakeProfit} classNames={fieldClasses}/><Checkbox label="清空止盈" checked={protection.clearTakeProfit} disabled={manageState === 'loading'} onCheckedChange={checked => setProtection(previous => ({ ...previous, clearTakeProfit: checked }))}/></div>
      <div className="protection-field"><Input label="止损价" aria-label="修改止损价" inputMode="decimal" value={protection.stopLoss} onChange={value => setProtection(previous => ({ ...previous, stopLoss: value }))} placeholder={`保持 ${currentManagedRow?.stop_loss ?? '未设置'}`} disabled={manageState === 'loading' || protection.clearStopLoss} classNames={fieldClasses}/><Checkbox label="清空止损" checked={protection.clearStopLoss} disabled={manageState === 'loading'} onCheckedChange={checked => setProtection(previous => ({ ...previous, clearStopLoss: checked }))}/></div>
    </div>
    {!managementAllowed && <p className="form-error" role="status">连接或该行状态已变化，请等待重新核对后操作。</p>}
    {manageError && <p className="form-error" role="alert">{manageError}</p>}
    <StatefulButton className="drawer-primary" state={manageState} disabled={!managementAllowed || !hasManagementChange} loadingText="正在提交并核对…" icon={<Check size={16}/>} onClick={() => void saveManagement()}>确认修改</StatefulButton>
    {drawer === 'order' && <p className="field-help">原生触发价不保证成交价格。修改后可能立即满足触发条件，请核对价格与当前行情。</p>}
  </> : <>
    <p className="drawer-subtext">停止所选策略的补单，撤销它的未成交委托，再对已确认归属的仓位提交市价平仓。其他策略与归属不明的仓位不在操作范围内。</p>
    <label className="form-label">操作策略</label>
    <Combobox value={closeStrategyId} onValueChange={setCloseStrategyId} disabled={manageState === 'loading'}><ComboboxTrigger className="symbol-trigger"><ComboboxInput aria-label="选择撤单并平仓的策略" placeholder={closeStrategyId}/></ComboboxTrigger><ComboboxContent><ComboboxList ariaLabel="可管理策略">{closableStrategies.map(strategy => <ComboboxItem key={strategy.id} value={strategy.id} textValue={`${strategy.config.symbol} · ${strategy.id}`}><span>{strategy.config.symbol} · {strategy.id.slice(-8)}</span><span className="muted ml-auto">{stateLabels[strategy.status] ?? strategy.status}</span></ComboboxItem>)}</ComboboxList></ComboboxContent></Combobox>
    {closeStrategy && <div className="management-scope"><span>{closeStrategy.config.symbol} · {closeStrategy.config.direction === 'long' ? '做多' : '做空'} · {stateLabels[closeStrategy.status] ?? closeStrategy.status}</span><strong className="mono">{closeStrategy.id}</strong><small>撤销 {closeOrders.length} 笔已确认委托 · 平仓 {closePositions.length} 个已归属仓位</small></div>}
    <div className="close-scope-list" aria-label="本次撤单和平仓范围">
      {closeOrders.map(row => <div key={`order:${row.id}`}><span>撤销委托 <b className="mono">{row.id}</b></span><small>{row.volume ?? '—'} 手 · 触发价 {number(row.price, precision)}</small></div>)}
      {closePositions.map(row => <div key={`position:${row.id}`}><span>平仓 <b className="mono">{row.id}</b></span><small>{row.volume ?? '—'} 手 · 开仓价 {number(row.entry_price, precision)}</small></div>)}
      {!closeOrders.length && !closePositions.length && <p className="muted">尚无已确认委托或已归属仓位；将停止该策略并核对提交中的操作。</p>}
    </div>
    <p className="close-warning">这会向 Gate 提交真实撤单和平仓请求。市价平仓可能产生滑点；最终数量与成交结果以 Gate 核对结果为准。</p>
    {manageError && <p className="form-error" role="alert">{manageError}</p>}
    <StatefulButton className="drawer-primary close-confirm" state={manageState} loadingText="正在撤单并平仓…" disabled={!online || !connection.connected || !closeStrategy} icon={<Square size={14}/>} onClick={() => void closeStrategyPositions()}>确认撤单并平仓 · {closeStrategy?.config.symbol ?? '—'}</StatefulButton>
  </>

  return <div className="app-shell">
    <header className="topbar"><a href="/" className="brand" aria-label="Grid Studio 首页"><span className="brand-mark"><i/><i/><i/><i/></span>Grid<span className="brand-light">Studio</span><span className="brand-divider"/><span className="product-tag">GATE CFD</span></a><div className="topbar-right"><Button variant="ghost" aria-label="退出访问验证" onClick={async()=>{try{await api('/auth/logout','POST');window.location.replace('/login')}catch(e){setActionError(errorText(e))}}}><LockKeyhole size={15}/>退出验证</Button><span className="mode-tag"><Zap size={14}/>实盘交易</span><Button variant="ghost" className="connection-button" onClick={() => setDrawer('connection')}><span className={`status-dot ${connection.connected ? 'is-on' : ''}`}/>{connection.connected ? 'Gate 已连接' : '连接 Gate API'}<ChevronRight size={14}/></Button><a className="icon-link" href="https://www.gate.com/docs/developers/apiv4/zh_CN/cfd/" target="_blank" rel="noreferrer" aria-label="打开 Gate 官方 API 文档"><BookOpen size={18}/></a></div></header>
    <main className="workspace">
      <div className="page-heading"><div><div className="breadcrumb">交易工作台<ChevronRight size={12}/><span>策略控制台</span></div><h1>网格策略<span className="small-tag">CFD</span></h1></div><div className="heading-actions"><span className={`service-status ${online ? '' : 'offline'}`}><span className={`status-dot ${online ? 'is-on' : ''}`}/>{online ? '策略引擎在线' : '等待后台连接'}</span><Button variant="outline" className="template-button latency-open-button" onClick={() => { setPricePick(null); setDrawer('diagnostics') }}><Gauge size={15}/>延迟诊断</Button><Button variant="outline" className="template-button" onClick={() => void openTemplates()}><Bookmark size={15}/>策略模板</Button></div></div>
      <section className="stats-grid" aria-label="Gate CFD 账户概览"><Stat label="账户净值" value={number(selected?.account?.equity)} hint={connection.connected ? "Gate CFD 真实账户 · USD" : "连接账户后显示"} icon={<Wallet size={16}/>}/><Stat label="可用于新策略" value={number(selected?.account?.available_for_new_strategy)} hint={`已用 ${number(selected?.account?.margin)} · 预留 ${number(selected?.account?.reserved_margin)}`} icon={<ShieldCheck size={16}/>}/><Stat label="已实现盈亏" value={number(selected?.account?.realized_pnl)} hint="本控制台已关联仓位的实盘记录" icon={<Activity size={16}/>} accent={Number(selected?.account?.realized_pnl ?? 0) >= 0}/><Stat label="浮动盈亏" value={number(selected?.account?.unrealized_pnl)} hint={connection.connected && selected?.account ? `全账户 ${selected.positions.length} 个持仓` : '连接账户后显示'} icon={<Layers3 size={16}/>} accent={Number(selected?.account?.unrealized_pnl ?? 0) >= 0}/></section>
      {visibleError && <div className="error-banner" role="alert"><Info size={17}/><span>{visibleError}</span><button onClick={() => { setActionError(''); setRetry(r => r + 1) }}><RefreshCw size={14}/>重新同步</button></div>}
      {!!unresolvedOperations.length && <div className="operation-pending-note" role="status"><RefreshCw size={15}/><span>{config.symbol} 有 {unresolvedOperations.length} 笔操作正在与 Gate 核对，不会重复发送。</span></div>}
      <div className="main-grid">
        <section className="panel strategy-panel" aria-labelledby="configuration-heading">
          <div className="panel-heading"><h2 id="configuration-heading"><Settings2 size={17}/>配置策略</h2><span className="step-badge">01</span></div>
          <div className="strategy-form">
            <label className="form-label">交易品种</label><Combobox value={config.symbol} onValueChange={selectSymbol} disabled={startState === 'loading' || cancelState === 'loading' || manageState === 'loading' || resumeState === 'loading'}><ComboboxTrigger className="symbol-trigger"><ComboboxInput aria-label="搜索 CFD 交易品种" placeholder={config.symbol}/></ComboboxTrigger><ComboboxContent><ComboboxList ariaLabel="CFD 品种">{symbols.map(s => <ComboboxItem key={s.symbol} value={s.symbol} textValue={`${s.symbol} · ${s.name}`} keywords={[s.name]}><span>{s.symbol}</span><span className="muted ml-auto">{s.name}</span></ComboboxItem>)}<ComboboxEmpty>没有找到该品种</ComboboxEmpty></ComboboxList></ComboboxContent></Combobox>
            <div className="direction-control"><label className="form-label">策略方向</label><div className="direction-tabs"><Button variant="ghost" disabled={formDisabled} className={`direction-button long ${config.direction === 'long' ? 'selected' : ''}`} onClick={() => update('direction', 'long')}><ArrowUpRight size={16}/>做多{config.direction === 'long' && <Check size={14}/>}</Button><Button variant="ghost" disabled={formDisabled} className={`direction-button short ${config.direction === 'short' ? 'selected' : ''}`} onClick={() => update('direction', 'short')}><ArrowDownLeft size={16}/>做空{config.direction === 'short' && <Check size={14}/>}</Button></div></div>
            <div className="form-section"><div className="label-with-action"><span className="form-label">价格区间</span><span className="muted">{spec?.currency ?? spec?.settlement_currency ?? '—'}</span></div><div className="price-range"><Input aria-label="价格下限" onFocus={() => beginPick('lower')} value={config.lower_price} onChange={v => update('lower_price', v)} placeholder="下限" inputMode="decimal" disabled={formDisabled} classNames={fieldClasses}/><span className="range-connector">—</span><Input aria-label="价格上限" onFocus={() => beginPick('upper')} value={config.upper_price} onChange={v => update('upper_price', v)} placeholder="上限" inputMode="decimal" disabled={formDisabled} classNames={fieldClasses}/></div><div className="range-labels pick-field-labels"><button disabled={formDisabled} className={pricePick?.target === 'lower' ? 'selected' : ''} onClick={() => beginPick('lower')} aria-label="从图表选择下限价格"><Crosshair size={12}/>下限价格</button><button disabled={formDisabled} className={pricePick?.target === 'upper' ? 'selected' : ''} onClick={() => beginPick('upper')} aria-label="从图表选择上限价格">上限价格<Crosshair size={12}/></button></div><div className="quick-config-tools"><Button variant="ghost" disabled={formDisabled} onClick={() => beginPick('lower', true)}><Crosshair size={13}/>图表选区间</Button><Button variant="ghost" onClick={() => { setPricePick(null); setDrawer('planner') }}><Sparkles size={13}/>智能组合</Button></div>{pricePick && pricePick.target !== 'anchor' && <div className="pick-inline-hint" role="status"><span>{pricePick.pair ? pricePick.first ? `已选 ${pricePick.first}，再点第二个价格` : '依次点选两个价格，自动排序上下限' : '在图表或价格轴点选价格'}</span><button aria-label="退出图表选价" onClick={() => setPricePick(null)}><X size={13}/></button></div>}</div>
            <div className="form-section"><div className="label-with-action"><span className="form-label">网格数量</span><div className="inline-segments">{(['arithmetic', 'geometric'] as const).map(v => <button key={v} disabled={formDisabled} className={config.spacing === v ? 'selected' : ''} onClick={() => update('spacing', v)}>{v === 'arithmetic' ? '等差' : '等比'}</button>)}</div></div><Input aria-label="网格数量" type="number" min={2} max={100} step={1} value={String(config.grid_count)} onChange={v => update('grid_count', v === '' ? 0 : Number(v))} rightIcon={<span className="unit">格</span>} disabled={formDisabled} classNames={fieldClasses}/><RangeSlider aria-label="调整网格数量" value={Number.isFinite(config.grid_count) ? config.grid_count : 2} onValueChange={v => update('grid_count', v)} min={2} max={100} step={1} showTicks={false} disabled={formDisabled} className="grid-slider"/><div className="slider-labels">{[2, 25, 50, 75, 100].map(n => <button key={n} disabled={formDisabled} onClick={() => update('grid_count', n)}>{n}</button>)}</div></div>
            <div className="form-section"><Input label="每网格手数" aria-label="每网格手数" value={config.volume} onChange={v => update('volume', v)} inputMode="decimal" rightIcon={<span className="unit">手</span>} disabled={formDisabled} classNames={fieldClasses}/><div className="field-help">最小 {spec?.volume_min ?? '—'} 手 · 步长 {spec?.volume_step ?? '—'} 手</div></div>
            <div className="leverage-display"><div><span>品种杠杆</span><LockKeyhole size={12}/></div><strong>{spec?.leverage ?? '—'}<span>×</span></strong><small>由品种决定</small></div>
            <div className="repeat-row"><div><span>循环补单</span><p>确认 Gate 止盈全平后，重新挂出该格委托</p></div><Switch checked={config.repeat} onCheckedChange={v => update('repeat', v)} disabled={formDisabled} ariaLabel="循环补单"/></div>
            <button className="advanced-trigger" aria-expanded={advanced} onClick={() => setAdvanced(!advanced)}>高级设置<ChevronRight size={14} className={advanced ? 'rotate' : ''}/></button>
            {advanced && <div className="advanced-fields"><Input label="每仓止损价（可选）" aria-label="每仓止损价" onFocus={() => beginPick('stop')} value={config.stop_loss ?? ''} onChange={v => update('stop_loss', v || null)} inputMode="decimal" disabled={formDisabled} classNames={fieldClasses}/><p className="field-help">做多低于区间下限；做空高于区间上限。触及该价格时，后台会停止新单并撤销本策略未成交委托；每仓原生止损由 Gate 执行。异常或亏损退出后暂停补单。</p></div>}
          </div>
          <div className="strategy-actions">
            <div className="funding-estimate"><span>满格保证金估算<CircleHelp size={13}/></span><strong>{number(estimatedMargin)}<small> USD</small></strong></div>
            {previewError && !active && connection.connected && <p className="form-error" role="alert">{previewError}</p>}
            {!active && startHint && <p className="start-hint">{startHint}{!connection.connected && <button onClick={() => setDrawer('connection')}>连接账户<ChevronRight size={12}/></button>}</p>}
            {!active && connection.connected && !!previewFeedback.blockers.length && <div className="preview-blockers" role="status">{previewFeedback.blockers.map(message => <p key={message}>{message}</p>)}</div>}
            {!!previewFeedback.information.length && <details className="preview-information"><summary>交易规则与估算说明</summary><ul>{previewFeedback.information.map(message => <li key={message}>{message}</li>)}</ul></details>}
            {active && <div className={`native-progress ${startButton.tone === 'paused' ? 'is-paused' : ''}`} role="status">
              <div className="active-note"><span className={`status-dot ${running || active.status === 'starting' ? 'is-on' : ''}`}/>{stateLabels[active.status] ?? active.status}{['running', 'starting', 'recovering'].includes(active.status) ? ' · 参数已锁定' : ''}</div>
              {legacy ? <p>旧版本地触发已停用。可管理原有仓位或全部取消；停止后由你重新创建原生策略。</p> : <>
                <div className="placement-counts"><span>已挂 Gate <b>{active.pending_count ?? managedOrders.filter(row => row.strategy_id === active.id).length}</b></span><span>待挂条件 <b>{active.waiting_count ?? '—'}</b></span></div>
                {active.status === 'starting' && active.placement && <><progress max={Math.max(1, active.placement.eligible)} value={Math.min(active.placement.confirmed, Math.max(1, active.placement.eligible))}/><small>初始委托已确认 {active.placement.confirmed} / {active.placement.eligible} · 提交中 {Math.max(0, active.placement.submitted - active.placement.confirmed)}</small></>}
                {active.status === 'paused' && <p className="pause-explanation">补单已暂停，已有 Gate 委托与仓位继续有效。恢复后沿用当前参数；修改区间或手数需先全部取消，再新建策略。</p>}
              </>}
              {resumeControl.readyMessage && <p className="positive">{resumeControl.readyMessage}</p>}
              {(active.error || active.notice) && <div className="pause-reason"><span>{active.status === 'paused' ? resumeControl.pauseReasonLabel : '执行提示'}</span><p>{active.error || active.notice}</p></div>}
              {resumeControl.visible && !resumeControl.allowed && !!resumeControl.blockers.length && <div className="resume-blockers"><span>恢复前需处理</span>{resumeControl.blockers.map(message => <p key={message}>{message}</p>)}</div>}
            </div>}
            {resumeControl.visible ? <StatefulButton state={resumeState} loadingText="正在核验并恢复…" errorText={resumeControl.allowed ? '重试恢复' : '暂不能恢复'} icon={<Play size={16} fill="currentColor"/>} className="resume-button" disabled={!resumeControl.allowed || cancelState === 'loading' || manageState === 'loading'} onClick={() => void resumeStrategy()}>{resumeControl.allowed ? '恢复当前策略' : '暂不能恢复'}</StatefulButton> : <StatefulButton state={startButton.state} loadingText={startButton.label} errorText={startButton.label} icon={active ? <Activity size={16}/> : <Play size={16} fill="currentColor"/>} className={`start-button start-${startButton.tone}`} disabled={!canStart || cancelState === 'loading' || manageState === 'loading' || resumeState === 'loading'} onClick={() => void start()}>{startButton.label}</StatefulButton>}
            {resumeError && <p className="form-error" role="alert">{resumeError}</p>}
            <StatefulButton state={cancelState} loadingText="正在撤销并核对…" successText="已全部取消" errorText="重试取消" icon={<Square size={14}/>} variant="outline" className="cancel-button" disabled={!online || !active || startState === 'loading' || manageState === 'loading' || resumeState === 'loading'} onClick={() => void cancel()}>全部取消</StatefulButton>
            <p className="action-hint"><ShieldCheck size={12}/>撤销本策略未成交委托，停止补单并保留仓位</p>
            <Button variant="ghost" className="close-strategy-button" disabled={!online || !connection.connected || !closableStrategies.length || manageState === 'loading' || startState === 'loading' || cancelState === 'loading' || resumeState === 'loading'} onClick={openCloseStrategy}>撤单并平仓<ChevronRight size={13}/></Button>
          </div>
        </section>
        <div className="market-column">
          <section className="panel market-panel"><div className="market-header"><div className="market-identity"><div className="asset-icon">{config.symbol === 'XAUUSD' ? 'Au' : config.symbol === 'EURUSD' ? '€' : config.symbol === 'USOIL' ? 'Oil' : config.symbol.slice(0, 2)}</div><div><h2>{config.symbol}<span>{spec?.name ?? '—'}</span></h2><span className="market-category">Gate 实时报价 <i/> 1秒轮询</span></div></div><div className="market-price"><strong>{number(market?.last, precision)}</strong><span className={Number(market?.change ?? 0) >= 0 ? 'positive' : 'negative'}>{finite(market?.change) === null ? '—' : `${Number(market?.change) >= 0 ? '+' : ''}${number(market?.change)}%`}</span></div><div className="market-facts"><div><span>今日最高</span><b>{number(market?.high, precision)}</b></div><div><span>今日最低</span><b>{number(market?.low, precision)}</b></div><div><span>当前点差</span><b>{finite(market?.ask) !== null && finite(market?.bid) !== null ? number(Number(market?.ask) - Number(market?.bid), precision) : '—'}</b></div></div></div>
            <div className="quote-strip"><div><span>买价 Bid</span><b>{number(market?.bid, precision)}</b></div><div><span>卖价 Ask</span><b>{number(market?.ask, precision)}</b></div><div className={`quote-status ${stale || feedError ? 'is-stale' : ''}`}><span className={`status-dot ${!stale && !feedError ? 'is-on' : ''}`}/><span>{!market ? '等待报价' : stale ? '报价已陈旧 · 等待恢复' : feedError ? '行情连接重试中' : marketClosed ? '市场休市' : '报价同步中'}</span><time>{market ? `最近报价 ${clockTime(market.quote_timestamp ?? market.received_at)}` : '—'}</time></div></div>{feedError && <p className="feed-warning" role="status">{feedError}</p>}
            {market ? <GridChart market={market} stale={!!stale} preview={validPreview} direction={config.direction} precision={precision} orders={relevantOrders} positions={positions} tickSize={spec?.tick_size} pickTarget={pricePick?.target} pickDisabled={formDisabled && pricePick?.target !== 'anchor'} onPickPrice={receivePickedPrice}/> : <div className="loading-chart"><RefreshCw size={24}/><p>{fetchError ? '后台连接中断' : '正在获取 Gate 实时报价'}</p><span>行情返回后显示真实价格与网格预览</span></div>}
          </section>
          <section className="grid-summary panel"><div><span><Grid2X2 size={14}/>网格结构</span><strong>{Number.isFinite(config.grid_count) ? config.grid_count : '—'} <small>格 / {Number.isFinite(config.grid_count) ? config.grid_count + 1 : '—'} 条价格线</small></strong></div><div><span>初始可挂委托</span><strong>{validPreview ? eligibleCount : '—'}<small> 笔</small></strong></div><div><span>每格毛利估算</span><strong className="positive">{grossMin !== null && grossMax !== null ? (Math.abs(grossMax - grossMin) < .01 ? number(grossMin) : `${number(grossMin)}–${number(grossMax)}`) : '—'}<small> USD</small></strong></div><div><span>满格总手数</span><strong>{number(volume === null ? null : config.grid_count * volume)}<small> 手</small></strong></div></section>
          <div className="estimate-note"><Info size={13}/><span>毛利按相邻委托价估算，未扣佣金、滑点及隔夜费；保证金按 Gate 品种规格估算，未知参数显示 —。实际成交价可能偏离格位。</span></div>
          <section className="panel records-panel"><div className="records-header"><Tabs value={tableTab} onValueChange={setTableTab} variant="underline"><TabsList className="records-tabs">{[['grid', '网格预览', validPreview?.cells.length], ['orders', '原生委托 / 待挂', pendingOrders.length], ['positions', '持仓', positions.length], ['fills', '成交记录', fills.length], ['logs', '运行日志', undefined]].map(([value, label, count]) => <TabsTrigger key={String(value)} value={String(value)} className="record-tab">{label}{count !== undefined && <span className="tab-count">{count}</span>}</TabsTrigger>)}</TabsList></Tabs><span className="records-sync"><RefreshCw size={12}/>自动同步</span></div>
            {tableTab === 'fills' && <div className="fill-history-toolbar"><span>最新在前{fills[0] ? ` · 最近成交 ${fillTime(fills[0].time)}` : ''}</span><Button variant="ghost" size="sm" disabled={!fills.length} onClick={() => setRecordsView(value => value + 1)}>查看最新</Button></div>}
            {tableTab === 'grid' ? <Table data={(validPreview?.cells ?? []).map(c => ({ ...c, id: String(c.index) }))} columns={gridColumns} getRowId={r => r.id} height={238} rowHeight={43} className="data-table" emptyState={<div className="empty-table"><Grid2X2 size={22}/><span>填写有效参数后显示网格价位</span></div>}/> : tableTab === 'logs' ? <div className="log-list" role="log">{selected?.logs.length ? selected.logs.slice(-80).reverse().map(log => <div className="log-line" key={log.id}><time>{clockTime(log.time)}</time><span className={`log-level ${log.level}`}>{log.level === 'error' ? '异常' : log.level === 'warning' ? '提示' : '记录'}</span><p>{log.message}</p></div>) : <div className="empty-table"><Clock3 size={22}/><span>启动策略后，执行过程将记录在这里</span></div>}</div> : <Table key={`${config.symbol}:${tableTab}:${recordsView}`} data={tableTab === 'positions' ? positions : tableTab === 'fills' ? fills : pendingOrders} columns={tradeColumns} getRowId={r => r.id} height={238} rowHeight={43} className="data-table" emptyState={<div className="empty-table"><Layers3 size={23}/><span>{tableTab === 'positions' ? '暂无持仓' : tableTab === 'fills' ? '暂无成交记录' : '暂无原生委托或待挂格位'}</span><small>{tableTab === 'positions' ? 'Gate 确认开仓后，真实仓位会显示在这里' : tableTab === 'fills' ? '开仓与平仓记录从 Gate 自动同步' : '配置完成后，点击「快速开始」'}</small></div>}/>}
            {latest && <div className="strategy-foot"><span><i className={`status-dot ${running ? 'is-on' : ''}`}/>{stateLabels[latest.status] ?? latest.status}</span><span className="mono">{latest.id.slice(0, 12)}</span><span>{latest.config.repeat ? '循环补单' : '单轮执行'}</span>{latest.stop_reason && <span>{latest.stop_reason}</span>}{!active && positions.length > 0 && <span className="warning-text">保留 {positions.length} 个持仓，止盈止损继续有效</span>}</div>}
          </section>
          <section className="execution-strip"><Zap size={17}/><p>提前向 Gate 挂出原生触发委托，附带相邻格位止盈及可选止损。<span>后台停机后，已挂委托及仓位止盈止损仍由 Gate 执行；循环补单需要后台在线。全部取消保留仓位。</span></p></section>
        </div>
      </div>
      <footer className="workspace-footer"><span><LockKeyhole size={12}/>交易密钥仅保留在后台</span><span><span className={`status-dot ${online ? 'is-on' : ''}`}/>{online ? `最后同步 ${clockTime(lastSync ?? undefined)}` : '连接中断，策略状态待核对'}<i/>FastAPI Engine</span></footer>
    </main>
    <Drawer open={drawer !== null && drawer !== 'planner' && drawer !== 'diagnostics'} onOpenChange={open => !open && closeDrawer()} ariaLabel={drawerTitle} className="settings-drawer"><div ref={panel} className="drawer-inner"><div className="drawer-heading"><div><span className="eyebrow">{drawer === 'connection' ? 'CONNECTION' : drawer === 'templates' ? 'TEMPLATES' : 'GATE MANAGEMENT'}</span><h2>{drawerTitle}</h2></div><Button variant="ghost" size="icon" aria-label="关闭设置" disabled={manageState === 'loading'} onClick={closeDrawer}><X size={20}/></Button></div>
      {drawer === 'connection' ? <><div className="connection-explainer"><ShieldCheck size={20}/><p>连接后读取真实账户与可用保证金。点击快速开始后，策略会自动向 Gate 提交真实交易委托。</p></div><div className="drawer-fields"><Input label="API Key" aria-label="Gate API Key" value={apiKey} onChange={setApiKey} placeholder={connection.configured ? '已配置 · 留空以复用' : '输入 API Key'} type="password" autoComplete="off" classNames={fieldClasses}/><Input label="API Secret" aria-label="Gate API Secret" value={apiSecret} onChange={setApiSecret} placeholder={connection.configured ? '留空以复用已有密钥' : '输入 API Secret'} type="password" autoComplete="off" classNames={fieldClasses}/><p className="field-help">密钥只交给当前连接的后台，页面不会保存。勾选后由后台加密保存，重启后可复用；取消勾选则仅在本次运行期间保留。</p><Checkbox checked={remember} onCheckedChange={setRemember} label="在后台加密保存" disabled={checking} className="remember-key"/></div><StatefulButton state={checking ? 'loading' : 'idle'} loadingText="正在验证账户…" icon={<PlugZap size={16}/>} className="drawer-primary" disabled={checking || (!connection.configured && (!apiKey || !apiSecret)) || (!!apiKey !== !!apiSecret)} onClick={() => void checkConnection()}>检查连接</StatefulButton>{(connectionActionError || connection.error) && <p role="alert" className="form-error">{connectionActionError || connection.error}</p>}{connection.connected && <div className="connected-card"><div><span className="status-dot is-on"/>Gate 账户已连接</div><dl><div><dt>真实账户净值</dt><dd>{number(connection.account?.equity)}</dd></div><div><dt>真实可用保证金</dt><dd>{number(connection.account?.free_margin)}</dd></div><div><dt>账户状态</dt><dd>{Number(connection.account?.status) === 3 ? '正常' : Number(connection.account?.status) === 2 ? '审核中' : Number(connection.account?.status) === 1 ? '未开通' : '—'}</dd></div><div><dt>密钥保存</dt><dd>{connection.remembered ? '已在后台加密保存' : '仅本次后台运行'}</dd></div></dl></div>}{connection.configured && <Button variant="ghost" className="disconnect" disabled={checking} onClick={() => void disconnect()}>断开并清除密钥配置</Button>}<div className="live-status-card"><div><Zap size={17}/><strong>实盘自动执行</strong></div><p>策略启动后会提前向 Gate 提交原生触发委托，并附带止盈止损。后台停机不会自动撤销已挂委托；循环补单需要后台在线。密钥仅由当前连接的后台保存。</p></div><a className="docs-link" href="https://www.gate.com/docs/developers/apiv4/zh_CN/cfd/" target="_blank" rel="noreferrer">查看 Gate CFD 官方 API 文档<ArrowUpRight size={15}/></a></> : drawer === 'templates' ? <><p className="drawer-subtext">保存常用参数，下次直接载入。载入模板不会启动策略。</p><Input label="模板名称" value={templateName} onChange={setTemplateName} maxLength={50} classNames={fieldClasses}/><div className="template-summary"><span>{config.symbol} · {config.direction === 'long' ? '做多' : '做空'}</span><span>{config.lower_price} — {config.upper_price}</span><span>{config.grid_count} 格 · {config.volume} 手 / 格</span></div><StatefulButton className="drawer-primary" state={templateBusy ? 'loading' : 'idle'} disabled={!templateName.trim() || !validPreview} loadingText="正在保存…" icon={<Bookmark size={16}/>} onClick={() => void saveTemplate()}>保存当前参数</StatefulButton><div className="saved-templates"><h3>已保存模板 <span>{templates.length}</span></h3>{templates.length === 0 ? <p className="muted">还没有模板，保存第一组常用参数。</p> : templates.map(t => <div className="saved-template" key={t.id}><div><strong>{t.name}</strong><span>{t.config.symbol} · {t.config.grid_count} 格 · {t.config.volume} 手</span></div><Button variant="ghost" size="icon" aria-label={`载入模板 ${t.name}`} disabled={formDisabled} onClick={() => { setConfig(t.config); setPreviewKey(''); closeDrawer(); setActionError(''); setStartState('idle'); setCancelState('idle'); showToast({ title: '模板已载入，请核对预览', status: 'info' }) }}><Copy size={16}/></Button><Button variant="ghost" size="icon" aria-label={`删除模板 ${t.name}`} onClick={async () => { try { await api(`/templates/${t.id}`, 'DELETE'); setTemplates(await api<Template[]>('/templates')) } catch (e) { setActionError(errorText(e)) } }}><Trash2 size={15}/></Button></div>)}</div>{formDisabled && <p className="field-help">当前策略运行中，停止后可载入其他模板。</p>}</> : managementContent}
    </div></Drawer>
    <SmartPlanner key={config.symbol} open={drawer === 'planner'} onClose={closePlanner} config={config} market={market} locked={formDisabled} connected={connection.connected} onApply={applySmartPlan} anchorPick={anchorPick} onPickAnchor={() => { setDrawer(null); beginPick('anchor') }}/>
    <LatencyDiagnostics open={drawer === 'diagnostics'} onClose={closePlanner} symbol={config.symbol}/>
    <AnimatedToastStack toasts={toasts} onDismiss={dismissToast} position="bottom-right" portal/>
  </div>
}
