import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Check, Crosshair, RefreshCw, Sparkles, X } from 'lucide-react'
import { Button } from '@/components/motion/button/base'
import { Input } from '@/components/motion/input'
import { Drawer } from '@/components/motion/drawer'
import { api, number, clockTime } from './api'
import type { GridConfig, Market } from './types'
import type { CostProfile, PlanMode, PlanResult } from './plannerUi'

const fields = { field: 'field-shell', input: 'field-input', label: 'field-label' }
const modes: {value: PlanMode; title: string; hint: string}[] = [
  {value: 'count', title: '求网格数量', hint: '区间 + 手数 + 目标净收益'},
  {value: 'range', title: '求价格区间', hint: '格数 + 手数 + 目标净收益'},
  {value: 'volume', title: '求每格手数', hint: '区间 + 格数 + 目标净收益'},
  {value: 'evaluate', title: '收益试算', hint: '按现有区间、格数与手数'},
]
type Props = {
  open: boolean; onClose: () => void; config: GridConfig; market: Market | null;
  locked: boolean; connected: boolean; onApply: (config: GridConfig) => string | null;
  onPickAnchor: () => void; anchorPick: {value: string; seq: number; symbol: string} | null;
}
export default function SmartPlanner({open, onClose, config, market, locked, connected, onApply, onPickAnchor, anchorPick}: Props) {
  const [mode, setMode] = useState<PlanMode>('count')
  const [draft, setDraft] = useState(config)
  const [target, setTarget] = useState('')
  const [anchor, setAnchor] = useState<'center'|'lower'|'upper'>('center')
  const [anchorPrice, setAnchorPrice] = useState('')
  const [spreadMode, setSpreadMode] = useState<'live'|'manual'|'off'>('live')
  const [spread, setSpread] = useState('')
  const [slippage, setSlippage] = useState('0')
  const [fee, setFee] = useState('')
  const [minimum, setMinimum] = useState('')
  const [feeSource, setFeeSource] = useState<'gate'|'user'>('gate')
  const [profile, setProfile] = useState<CostProfile | null>(null)
  const [feeError, setFeeError] = useState('')
  const [feeBusy, setFeeBusy] = useState(false)
  const [feeRetry, setFeeRetry] = useState(0)
  const [retry, setRetry] = useState(0)
  const [result, setResult] = useState<{key:string; value:PlanResult; receivedAt: number} | null>(null)
  const [error, setError] = useState('')
  const [applyingError, setApplyingError] = useState('')
  const [pending, setPending] = useState(false)
  const inner = useRef<HTMLDivElement>(null)
  const sourceRef = useRef(feeSource)
  sourceRef.current = feeSource
  const update = <K extends keyof GridConfig>(key: K, value: GridConfig[K]) => setDraft(c => ({...c, [key]:value}))
  const configKey = JSON.stringify(config)
  useEffect(() => { if (!open) setDraft(JSON.parse(configKey) as GridConfig) }, [configKey])

  useEffect(() => { if (anchorPick?.symbol === config.symbol) setAnchorPrice(anchorPick.value) }, [anchorPick])
  useEffect(() => {
    if (!open || !connected) return
    const controller = new AbortController()
    setFeeBusy(true); setFeeError('')
    void api<CostProfile>(`/grid/costs?symbol=${encodeURIComponent(config.symbol)}`, 'GET', undefined, controller.signal).then(value => {
      if (controller.signal.aborted || value.symbol !== config.symbol) return
      setProfile(value)
      if (sourceRef.current === 'gate') setFee(value.fee_per_lot ?? '')
      if (value.fee_per_lot === null) setFeeError(value.notes.join(' ') || 'Gate 未返回费率，请手动填写。')
    }).catch(e => { if (!controller.signal.aborted) { setProfile(null); if (sourceRef.current === 'gate') setFee(''); setFeeError(e instanceof Error ? e.message : '费率读取失败，请重试或手动填写。') } }).finally(() => { if (!controller.signal.aborted) setFeeBusy(false) })
    return () => controller.abort()
  }, [open, connected, config.symbol, feeRetry])

  useEffect(() => {
    if (!open) return
    const saved = document.activeElement as HTMLElement | null
    const frame = requestAnimationFrame(() => inner.current?.querySelector<HTMLElement>('button')?.focus())
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); onClose(); return }
      if (event.key !== 'Tab') return
      const nodes = Array.from(inner.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),a[href],summary,[tabindex="0"]') ?? []).filter(el => el.getClientRects().length)
      const first = nodes[0], last = nodes.at(-1)
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    document.addEventListener('keydown', handler)
    return () => { cancelAnimationFrame(frame); document.removeEventListener('keydown', handler); saved?.focus() }
  }, [open, onClose])

  const needed = [fee, minimum, slippage, ...(spreadMode === 'manual' ? [spread] : []), ...(mode !== 'evaluate' ? [target] : []), ...(mode !== 'range' ? [draft.lower_price, draft.upper_price] : []), ...(mode !== 'volume' ? [draft.volume] : [])]
  const ready = connected && needed.every(v => v.trim() !== '') && (mode === 'count' || Number.isInteger(draft.grid_count)) && !(feeSource === 'gate' && feeBusy)
  const request = {
    mode, symbol: config.symbol, direction: draft.direction, spacing: draft.spacing,
    ...(mode !== 'range' ? {lower_price: draft.lower_price, upper_price: draft.upper_price} : {anchor, anchor_price: anchorPrice || null}),
    ...(mode !== 'count' ? {grid_count: draft.grid_count} : {}),
    ...(mode !== 'volume' ? {volume: draft.volume} : {}),
    target_net_profit: mode === 'evaluate' ? null : target,
    costs: {round_trip_fee_per_lot: fee, minimum_fee: minimum, fee_source: feeSource, slippage_budget: slippage, spread_budget: spreadMode === 'live' ? null : spreadMode === 'off' ? '0' : spread},
  }
  const key = JSON.stringify(request)
  const keyRef = useRef(key)
  keyRef.current = key
  useEffect(() => {
    setResult(null); setError(''); setApplyingError(''); setPending(false)
    if (!open || !ready) return
    const controller = new AbortController()
    setPending(true)
    const timer = setTimeout(() => {
      void api<PlanResult>('/grid/plan', 'POST', JSON.parse(key), controller.signal).then(value => {
        if (!controller.signal.aborted && keyRef.current === key) setResult({key,value,receivedAt: Date.now()})
      }).catch(e => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '计算失败，请重试。') }).finally(() => { if (!controller.signal.aborted) setPending(false) })
    }, 450)
    return () => { clearTimeout(timer); controller.abort() }
  }, [key, ready, open, retry])
  const plan = result?.key === key ? result.value : null
  const summary = plan?.summary
  const cost = plan?.cells[0]
  const range = (a: string, b: string, digits = 4) => a === b ? number(a, digits) : `${number(a, digits)} – ${number(b, digits)}`

  return <Drawer open={open} onOpenChange={value => !value && onClose()} ariaLabel="智能组合计算" className="settings-drawer planner-drawer"><div className="drawer-inner" ref={inner}>
    <div className="drawer-heading"><div><span className="eyebrow">SMART GRID · {config.symbol}</span><h2><Sparkles size={22}/>智能组合</h2></div><Button variant="ghost" size="icon" aria-label="关闭智能组合" onClick={onClose}><X size={20}/></Button></div>
    <p className="planner-intro">选定已知参数，自动补齐其余配置。净收益按每格完成一次开仓与止盈计算。</p>
    <div className="planner-modes" role="group" aria-label="智能计算方式">{modes.map(item => <Button key={item.value} variant="outline" className={mode === item.value ? 'selected' : ''} aria-pressed={mode === item.value} onClick={() => setMode(item.value)}><strong>{item.title}{mode === item.value && <Check size={13}/>}</strong><span>{item.hint}</span></Button>)}</div>
    <div className="planner-section-heading"><span>已知参数</span><Button variant="ghost" onClick={() => { setDraft(config); setApplyingError('') }}>载入当前配置</Button></div>
    <div className="planner-inline-options"><div className="inline-segments">{(['long','short'] as const).map(value => <button key={value} className={draft.direction === value ? 'selected' : ''} onClick={() => update('direction', value)}>{value === 'long' ? '做多' : '做空'}</button>)}</div><div className="inline-segments">{(['arithmetic','geometric'] as const).map(value => <button key={value} className={draft.spacing === value ? 'selected' : ''} onClick={() => update('spacing', value)}>{value === 'arithmetic' ? '等差' : '等比'}</button>)}</div></div>
    <div className="planner-fields">
      {mode !== 'range' && <div className="planner-two-fields"><Input label="区间下限" aria-label="智能区间下限" value={draft.lower_price} onChange={v => update('lower_price', v)} inputMode="decimal" classNames={fields}/><Input label="区间上限" aria-label="智能区间上限" value={draft.upper_price} onChange={v => update('upper_price', v)} inputMode="decimal" classNames={fields}/></div>}
      <div className="planner-two-fields">{mode !== 'count' && <Input label="网格数量" aria-label="智能网格数量" type="number" min={2} max={100} step={1} value={String(draft.grid_count)} onChange={v => update('grid_count', v === '' ? 0 : Number(v))} classNames={fields}/>} {mode !== 'volume' && <Input label="每格手数" aria-label="智能每格手数" value={draft.volume} onChange={v => update('volume', v)} inputMode="decimal" classNames={fields}/>}</div>
      {mode !== 'evaluate' && <Input label="每格目标净收益 · USD" aria-label="每格目标净收益" value={target} onChange={setTarget} placeholder="例如 1.00" inputMode="decimal" classNames={fields}/>}
      {mode === 'range' && <div className="planner-anchor"><div className="label-with-action"><span className="form-label">固定参考位置</span><div className="inline-segments">{(['center','lower','upper'] as const).map(value => <button key={value} className={anchor === value ? 'selected' : ''} onClick={() => setAnchor(value)}>{value === 'center' ? '中心' : value === 'lower' ? '下限' : '上限'}</button>)}</div></div><Input aria-label="区间参考价" value={anchorPrice} onChange={setAnchorPrice} placeholder="留空使用计算时买卖中间价" inputMode="decimal" classNames={fields}/><div className="planner-small-actions"><Button variant="ghost" onClick={onPickAnchor}><Crosshair size={13}/>图表选参考价</Button><Button variant="ghost" onClick={() => setAnchorPrice('')}>跟随计算时行情</Button></div></div>}
    </div>
    <details className="planner-costs" open><summary>手续费与成交成本 <span>{feeSource === 'gate' ? 'Gate 费率' : '手动费率'}</span></summary><div className="planner-fields">
      <div><div className="label-with-action"><span className="form-label">每手一轮佣金 · USD</span><Button variant="ghost" disabled={!connected || feeBusy} onClick={() => { setFeeSource('gate'); setFeeRetry(v => v + 1) }}><RefreshCw size={12}/>{feeBusy ? '读取中' : '读取 Gate'}</Button></div><Input aria-label="每手一轮佣金" value={fee} onChange={v => { setFeeSource('user'); setFee(v) }} placeholder="读取失败时可手动填写" inputMode="decimal" classNames={fields}/><p className="field-help">{feeSource === 'gate' && profile?.fee_per_lot != null ? `Gate API：${profile.fee_per_lot} USD / 手，开仓收取一次。` : '手动输入每手完成一轮的佣金；Gate 开仓收取，不再乘二。'}</p>{feeError && <p className="field-help warning">{feeError}</p>}</div>
      <div><Input label="每单最低佣金 · USD" aria-label="每单最低佣金" value={minimum} onChange={setMinimum} placeholder="API 未提供，请填写；无则填 0" inputMode="decimal" classNames={fields}/><div className="planner-small-actions"><Button variant="ghost" onClick={() => setMinimum('0')}>无最低收费，填 0</Button></div><p className="field-help">按「每手佣金 × 手数」与最低额中较大者计算。股票等品种请填写适用的最低额。</p></div>
      <div><span className="form-label">额外点差预算</span><div className="planner-spread-options">{(['live','manual','off'] as const).map(value => <Button key={value} variant="outline" className={spreadMode === value ? 'selected' : ''} aria-pressed={spreadMode === value} onClick={() => setSpreadMode(value)}>{value === 'live' ? '当前点差' : value === 'manual' ? '手动设置' : '不额外预留'}</Button>)}</div>{spreadMode === 'manual' && <Input aria-label="手动点差预算" value={spread} onChange={setSpread} placeholder="价格单位" inputMode="decimal" classNames={fields}/>}<p className="field-help">原生委托价已包含买卖侧差异；此处额外预留一份点差作为保守预算，可关闭。{spreadMode === 'live' && market?.bid && market.ask ? ` 当前报价差 ${number(Number(market.ask) - Number(market.bid), 5)}，计算时重新读取。` : ''}</p></div>
      <Input label="额外滑点预算 · 价格单位" aria-label="额外滑点预算" value={slippage} onChange={setSlippage} inputMode="decimal" classNames={fields}/>
      <p className="field-help">滑点默认 0，表示未预留。预算未含隔夜持仓费；实际费用与成交价以 Gate 为准。</p>
    </div></details>
    <section className="planner-result" aria-label="智能计算结果" aria-live="polite">
      <div className="planner-section-heading"><span><Sparkles size={14}/>计算方案</span><Button variant="ghost" disabled={!ready || pending} onClick={() => setRetry(v => v + 1)}><RefreshCw size={12}/>更新行情并重算</Button></div>
      {!connected ? <p className="field-help">请先连接 Gate，读取真实规格与佣金。</p> : pending ? <p className="planner-pending"><RefreshCw size={15}/>正在核算每一格的净收益…</p> : !ready ? <p className="field-help">补齐已知参数与成本后自动计算。无最低佣金时请明确填 0。</p> : error ? <p className="form-error" role="alert">{error}</p> : plan && !plan.feasible ? <p className="form-error">{plan.reason}</p> : null}
      {plan?.feasible && plan.config && summary && <><p className="field-help">预算快照 {clockTime(result?.receivedAt)} · 本次点差预算 {plan.costs?.spread_budget}（价格单位）；需要时点击重算。</p><div className="planner-net"><span>每格预估净收益</span><strong className={Number(summary.min_net_profit) >= 0 ? 'positive' : 'negative'}>{range(summary.min_net_profit, summary.max_net_profit)}<small> USD</small></strong><span>{mode === 'count' ? '取满足目标的最多格数 · 已逐格核验' : mode === 'evaluate' ? '扣除佣金与额外成本预算' : '按报价单位取整 · 已逐格核验'}</span></div><dl className="planner-breakdown"><div><dt>价格区间</dt><dd>{plan.config.lower_price} — {plan.config.upper_price}</dd></div><div><dt>网格 / 每格手数</dt><dd>{summary.grid_count} 格 / {summary.volume} 手</dd></div><div><dt>每格毛收益</dt><dd>{range(summary.min_gross_profit, summary.max_gross_profit)} USD</dd></div><div><dt>每格佣金 / 点差预算 / 滑点预算</dt><dd>{number(cost?.fee, 4)} / {number(cost?.spread_cost, 4)} / {number(cost?.slippage_cost, 4)} USD</dd></div><div><dt>满格总手数</dt><dd>{summary.total_volume} 手</dd></div></dl>{!!plan.warnings.length && <details className="planner-notes"><summary>规则与计算说明</summary>{plan.warnings.map(message => <p key={message}>{message}</p>)}</details>}<details className="planner-cells"><summary>查看每格明细 · {plan.cells.length} 格</summary><div className="planner-table-wrap"><table><thead><tr><th>格</th><th>开仓 → 止盈</th><th>净收益 USD</th></tr></thead><tbody>{plan.cells.map(cell => <tr key={cell.index}><td>{cell.index + 1}</td><td>{cell.entry_price} → {cell.take_profit}</td><td className={Number(cell.net_profit) >= 0 ? 'positive' : 'negative'}>{number(cell.net_profit, 4)}</td></tr>)}</tbody></table></div></details><p className="field-help">{plan.estimate_notice}</p></>}
    </section>
    {applyingError && <p className="form-error" role="alert">{applyingError}</p>}
    {locked && <p className="field-help">当前策略参数已锁定；可以试算，停止策略后再应用。</p>}
    <Button className="drawer-primary" disabled={locked || pending || !ready || !plan?.feasible || !plan.config} onClick={() => { if (plan?.config) setApplyingError(onApply(plan.config) ?? '') }}>应用到配置<ArrowRight size={15}/></Button>
    <p className="planner-apply-hint">应用仅填写参数。核对预览后，由你点击「快速开始」。</p>
  </div></Drawer>
}
