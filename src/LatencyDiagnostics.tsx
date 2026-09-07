import { useEffect, useRef, useState } from 'react'
import { Activity, Download, Gauge, Square, X } from 'lucide-react'
import { Button } from '@/components/motion/button/base'
import { StatefulButton } from '@/components/motion/button/stateful'
import { Drawer } from '@/components/motion/drawer'
import { Table, type TableColumn } from '@/components/motion/table'
import { api, clockTime } from './api'
import { completePhaseMetrics, detailValue, jobRunning, latencyMarkdown, ms, type ClientProbe, type DiagnosticJob, type EndpointTiming } from './latencyReport'

type Props = {open:boolean; onClose:()=>void; symbol:string}
const labels:Record<string,string> = {queued:'等待测试',running:'正在测试',completed:'测试完成',failed:'测试未完整完成',cancelled:'已停止测试'}
export default function LatencyDiagnostics({open,onClose,symbol}:Props) {
  const [job,setJob] = useState<DiagnosticJob|null>(null)
  const [samples,setSamples] = useState<3|5>(3)
  const [submitting,setSubmitting] = useState(false)
  const [cancelling,setCancelling] = useState(false)
  const [error,setError] = useState('')
  const [probe,setProbe] = useState<{jobId:string; value:ClientProbe}|null>(null)
  const inner = useRef<HTMLDivElement>(null)
  const actionBusy = useRef(false)
  const actionVersion = useRef(0)
  const active = jobRunning(job)
  const report = job?.report
  const clientProbe = probe?.jobId === job?.id ? probe?.value ?? null : null

  useEffect(()=>{
    if(!open) return
    const controller=new AbortController()
    const version=actionVersion.current
    void api<{job:DiagnosticJob|null}>('/diagnostics/latest','GET',undefined,controller.signal).then(value=>{if(!controller.signal.aborted && version===actionVersion.current && !actionBusy.current)setJob(value.job)}).catch(e=>{if(!controller.signal.aborted && version===actionVersion.current)setError(e instanceof Error?e.message:'无法读取最近报告。')})
    return ()=>controller.abort()
  },[open])
  useEffect(()=>{
    if(!open || !active || !job) return
    const controller=new AbortController()
    let timer:ReturnType<typeof setTimeout>
    const poll=async()=>{
      try {const next=await api<DiagnosticJob>(`/diagnostics/${encodeURIComponent(job.id)}`,'GET',undefined,controller.signal);if(!controller.signal.aborted){setJob(current=>current?.id!==job.id||next.id!==job.id||(!jobRunning(current)&&jobRunning(next))?current:next);setError('')}}
      catch(e){if(!controller.signal.aborted)setError(e instanceof Error?e.message:'诊断进度读取失败，稍后重试。')}
      finally{if(!controller.signal.aborted)timer=setTimeout(poll,1000)}
    }
    void poll()
    return()=>{controller.abort();clearTimeout(timer)}
  },[open,active,job?.id])
  useEffect(()=>{
    if(!open)return
    const saved=document.activeElement as HTMLElement|null
    const frame=requestAnimationFrame(()=>inner.current?.querySelector<HTMLElement>('button')?.focus())
    const handler=(event:KeyboardEvent)=>{
      if(event.key==='Escape'){onClose();return}
      if(event.key!=='Tab')return
      const nodes=Array.from(inner.current?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],summary,[tabindex="0"]')??[]).filter(node=>node.getClientRects().length)
      if(event.shiftKey && document.activeElement===nodes[0]){event.preventDefault();nodes.at(-1)?.focus()}
      else if(!event.shiftKey && document.activeElement===nodes.at(-1)){event.preventDefault();nodes[0]?.focus()}
    }
    document.addEventListener('keydown',handler)
    return()=>{cancelAnimationFrame(frame);document.removeEventListener('keydown',handler);saved?.focus()}
  },[open,onClose])

  async function start() {
    if(actionBusy.current || active)return
    actionBusy.current=true;actionVersion.current+=1;setSubmitting(true);setError('')
    const local:ClientProbe={source:'browser',measured_at:new Date().toISOString(),samples:[]}
    try {
      for(let i=0;i<3;i++){
        const began=performance.now();let success=false
        try{await api('/health','GET',undefined,AbortSignal.timeout(5000));success=true}catch{/* Record failure without leaking raw transport data. */}
        local.samples.push({elapsed_ms:Math.round((performance.now()-began)*100)/100,success})
      }
      const next=await api<DiagnosticJob>('/diagnostics/start','POST',{symbol,samples})
      setProbe({jobId:next.id,value:local});setJob(next)
    }catch(e){setError(e instanceof Error?e.message:'无法启动测试，请检查后台连接。')}
    finally{actionBusy.current=false;setSubmitting(false)}
  }
  async function cancel(){
    if(!job||actionBusy.current)return
    const id=job.id,version=++actionVersion.current
    actionBusy.current=true;setCancelling(true)
    try{const next=await api<DiagnosticJob>(`/diagnostics/${id}/cancel`,'POST');if(version===actionVersion.current){setJob(current=>current?.id===id?next:current);setError('')}}
    catch(e){if(version===actionVersion.current)setError(e instanceof Error?e.message:'无法停止测试。')}
    finally{if(version===actionVersion.current){actionBusy.current=false;setCancelling(false)}}
  }
  function download(kind:'json'|'md'){
    if(!job?.report)return
    const content=kind==='json'?JSON.stringify({...job,client_probe:clientProbe},null,2):latencyMarkdown(job,clientProbe)
    const url=URL.createObjectURL(new Blob([content],{type:kind==='json'?'application/json':'text/markdown;charset=utf-8'}))
    const link=document.createElement('a');link.href=url;link.download=`gate-latency-${job.symbol}-${job.id}.${kind}`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)
  }
  const columns:TableColumn<EndpointTiming>[]=[
    {key:'endpoint',header:'测试接口',cell:row=><div className="latency-endpoint"><strong>{row.label}</strong><small>{row.endpoint}</small></div>},
    {key:'success',header:'成功',cell:row=>`${row.success}/${row.count}`},
    {key:'median',header:'总耗时 P50 / P95',cell:row=><div>{ms(row.stats.total_ms?.p50)}<small className="latency-subvalue">{ms(row.stats.total_ms?.p95)}</small></div>},
    {key:'network',header:'联网均值',cell:row=>ms(row.stats.network_ms?.avg)},
    {key:'queue',header:'排队均值',cell:row=>ms(row.stats.throttle_wait_ms?.avg)},
    {key:'backoff',header:'重试等待',cell:row=>ms(row.stats.backoff_ms?.avg)},
  ]
  return <Drawer open={open} onOpenChange={value=>!value&&onClose()} ariaLabel="延迟诊断报告" className="settings-drawer latency-drawer"><div ref={inner} className="drawer-inner">
    <div className="drawer-heading"><div><span className="eyebrow">PERFORMANCE · {symbol}</span><h2><Gauge size={22}/>延迟诊断</h2></div><Button variant="ghost" size="icon" onClick={onClose} aria-label="关闭延迟诊断"><X size={20}/></Button></div>
    <p className="drawer-subtext">分别测量页面到当前后台、Gate 接口和后台排队时间。与命令行脚本使用同一套测试，可下载完整报告。</p>
    <div className="latency-run-options"><div className="inline-segments">{([3,5] as const).map(value=><button key={value} disabled={active||submitting||cancelling} className={value===samples?'selected':''} onClick={()=>setSamples(value)}>每接口 {value} 次</button>)}</div><span>最多约 90 秒 · 共享现有限流</span></div>
    <StatefulButton className="drawer-primary" state={submitting||active||cancelling?'loading':'idle'} loadingText={cancelling?'正在停止测试…':submitting?'连接后台…':`${labels[job?.status??'running']} ${job?.progress.completed??0}/${job?.progress.total??0}`} disabled={submitting||active||cancelling} icon={<Activity size={16}/>} onClick={()=>void start()}>一键测试延迟</StatefulButton>
    <p className="field-help">只读取真实接口，不创建或取消订单。真实下单请求仅作被动观测；HTTP 返回时间与最终委托确认时间分开说明。</p>
    {active&&job&&<div className="latency-progress" role="status"><progress max={Math.max(1,job.progress.total)} value={job.progress.completed}/><div><span>{job.progress.stage}</span><Button variant="ghost" disabled={cancelling} onClick={()=>void cancel()}><Square size={12}/>停止测试</Button></div></div>}
    {error&&<p className="form-error" role="alert">{error}</p>}
    {job&&<div className="latency-report-meta"><span>{job.symbol} · {labels[job.status]}</span><time>{clockTime(job.finished_at??job.started_at??job.created_at)}</time></div>}
    {report&&<>
      <div className="latency-findings">{report.findings.map((finding,index)=><div className={`latency-finding ${finding.severity}`} key={index}><strong>{finding.title}</strong><p>{finding.detail}</p></div>)}</div>
      <div className="latency-section-heading"><h3>接口耗时</h3><span>单位毫秒 · 总测试 {ms(report.duration_ms)}</span></div>
      <div className="latency-table"><Table data={report.endpoint_stats} columns={columns} getRowId={row=>row.endpoint} height={350} rowHeight={62} className="data-table"/></div>
      <p className="field-help">联网耗时包含连接、传输和 Gate 处理；本次不单独测 DNS、TLS 或服务端执行。P95 为本次小样本分位数。</p>
      <div className="latency-section-heading"><h3>时间花在哪里</h3><span>均值分解</span></div>
      <div className="latency-bars">{report.endpoint_stats.map(row=>{
        const segments=[{label:'联网',value:row.stats.network_ms?.avg??0,color:'network'},{label:'排队',value:row.stats.throttle_wait_ms?.avg??0,color:'queue'},{label:'校时',value:row.stats.clock_wait_ms?.avg??0,color:'clock'},{label:'重试等待',value:row.stats.backoff_ms?.avg??0,color:'backoff'},{label:'本地处理',value:row.stats.processing_ms?.avg??0,color:'processing'}]
        const sum=segments.reduce((total,s)=>total+s.value,0)
        return <div key={row.endpoint}><div className="latency-bar-label"><span>{row.label}</span><span>{ms(row.stats.total_ms?.avg)}</span></div>{completePhaseMetrics(row)?<div className="latency-bar" aria-label={`${row.label}耗时分解`}>{segments.filter(s=>s.value>0).map(s=><span key={s.label} className={s.color} style={{width:`${sum>0?s.value/sum*100:0}%`}} title={`${s.label} ${ms(s.value)}`}/>)}</div>:<small>部分分项未测得，暂不显示占比。</small>}</div>
      })}</div><div className="latency-legend">{['联网','排队','校时','重试等待','本地处理'].map((label,index)=><span key={label}><i className={['network','queue','clock','backoff','processing'][index]}/>{label}</span>)}</div>
      <div className="latency-section-heading"><h3>客户端与后台</h3></div><div className="latency-stages">{clientProbe&&<div><strong>{clientProbe.source==='browser'?'浏览器':'脚本'} → 当前后台</strong><p>{clientProbe.samples.map(s=>s.success?ms(s.elapsed_ms):'请求失败').join(' / ')}</p><small>包含客户端发起、回传与读取响应；与后台阶段计时口径不同。</small></div>}{report.stages.map(stage=><div key={stage.name}><strong>{stage.label}<span>{ms(stage.duration_ms)}</span></strong><p>{stage.note}</p></div>)}</div>
      <div className="latency-section-heading"><h3>真实委托请求观测</h3></div><p className="field-help">{report.recent_real_requests.note}</p>{report.recent_real_requests.items.length?<div className="latency-stages">{report.recent_real_requests.items.map((row,index)=><div key={`${row.sequence}:${index}`}><strong>{row.method} {row.endpoint}<span>{ms(row.total_ms)}</span></strong><p>联网 {ms(row.network_ms)} · 排队 {ms(row.throttle_wait_ms)} · {row.outcome} · {row.attempts} 次请求</p></div>)}</div>:<p className="latency-empty">暂无真实委托计时样本。查询速度不能代替下单接受或最终确认速度。</p>}
      {!!report.estimates.length&&<><div className="latency-section-heading"><h3>请求流程耗时推算</h3><span>推算值，非真实委托测试</span></div><div className="latency-stages">{report.estimates.map((estimate,index)=><div key={index}><strong>{estimate.label}<span>{ms(estimate.value_ms)}</span></strong><p>{estimate.basis}</p></div>)}</div></>}
      <details className="latency-details"><summary>逐次样本、校时与测试限制</summary>{report.endpoint_stats.map(row=><div key={row.endpoint}><strong>{row.label}</strong><p>校时均值 {ms(row.stats.clock_wait_ms?.avg)} · 最快 {ms(row.stats.total_ms?.min)} · 最慢 {ms(row.stats.total_ms?.max)}</p>{row.samples.map(s=><p key={s.index}>#{s.index} · {ms(s.elapsed_ms)} · {s.status}{s.status_code!=null?` · HTTP ${s.status_code}`:''}{s.attempts!=null?` · 请求 ${s.attempts} 次`:''}{s.error_code?` · ${s.error_code}`:''}{s.rate_limit?` · 限流响应 ${detailValue(s.rate_limit)}`:''}</p>)}</div>)}{Object.entries(report.limits).map(([key,value])=><p key={key}>{key}: {detailValue(value)}</p>)}</details>
      <div className="latency-downloads"><Button variant="outline" onClick={()=>download('md')}><Download size={14}/>下载报告</Button><Button variant="ghost" onClick={()=>download('json')}><Download size={14}/>原始 JSON</Button></div>
    </>}
    {!job&&!submitting&&!error&&<div className="latency-empty"><Gauge size={30}/><p>点击测试，查看到底慢在网络、排队还是后台环节。</p></div>}
  </div></Drawer>
}
