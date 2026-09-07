export type Metric = {count:number; min:number; p50:number; p95:number; max:number; avg:number; total:number}
export type GateTiming = {sequence?:number; method:string; endpoint:string; total_ms:number; network_ms:number; throttle_wait_ms:number; clock_wait_ms:number; backoff_ms:number; attempts:number; status_code:number|null; outcome:string; started_at:number}
export type EndpointTiming = {endpoint:string; label:string; method:string; count:number; success:number; failed:number; stats:Record<string,Metric|null>; samples:{index:number; status:string; elapsed_ms:number; error_code:string|null; status_code?:number|null; attempts?:number|null; rate_limit?:Record<string,number>|null}[]}
export type LatencyReport = {
  source:string; read_only:boolean; duration_ms:number;
  endpoint_stats:EndpointTiming[];
  stages:{name:string; label:string; duration_ms:number|null; status:string; note:string}[];
  findings:{severity:string; title:string; detail:string}[];
  recent_real_requests:{status:string; items:GateTiming[]; note:string};
  limits:Record<string,unknown>;
  estimates:{label:string; value_ms:number|null; basis:string; kind:'estimate'}[];
}
export type DiagnosticJob = {id:string; symbol:string; samples:number; status:'queued'|'running'|'completed'|'failed'|'cancelled'; created_at:string|number; started_at:string|number|null; finished_at:string|number|null; progress:{completed:number; total:number; stage:string}; report:LatencyReport|null; error?:string|null}
export type ClientProbe = {source:'browser'|'script'; measured_at:string; samples:{elapsed_ms:number; success:boolean}[]}
export const ms = (value:number|null|undefined) => value == null || !Number.isFinite(value) ? '—' : `${value.toLocaleString('en-US',{maximumFractionDigits:1})} ms`
export const jobRunning = (job:DiagnosticJob|null) => job?.status === 'running' || job?.status === 'queued'
export const detailValue = (value:unknown) => value != null && typeof value === 'object' ? JSON.stringify(value) : String(value ?? '—')
const clean = (value:unknown) => detailValue(value).replace(/\|/g,'\\|').replace(/[\r\n]+/g,' ')
export function completePhaseMetrics(row:EndpointTiming):boolean {
  const count = row.stats.total_ms?.count ?? 0
  return count > 0 && ['network_ms','throttle_wait_ms','clock_wait_ms','backoff_ms','processing_ms'].every(name=>row.stats[name]?.count === count)
}

export function latencyMarkdown(job:DiagnosticJob, probe:ClientProbe|null):string {
  const report = job.report
  const stamp = job.finished_at ?? job.created_at
  const date = new Date(typeof stamp === 'number' ? stamp * 1000 : stamp)
  const lines = ['# Grid Studio 延迟诊断报告', '', `品种：${clean(job.symbol)} · 状态：${job.status} · 每接口计划 ${job.samples} 次`, `生成时间：${Number.isNaN(date.getTime()) ? '未知' : date.toISOString()}`, '', '测试只读取行情与账户，不创建、取消或修改订单。']
  if (probe) { lines.push('', '## 客户端到本地后台', '', `来源：${probe.source} · 时间：${probe.measured_at}`, ...probe.samples.map((sample,index) => `- 第 ${index+1} 次：${ms(sample.elapsed_ms)} · ${sample.success?'成功':'失败'}`)) }
  if (!report) return [...lines,'','诊断尚无完整报告。'].join('\n')
  lines.push('', '## 主要发现', '', ...report.findings.map(f => `- **${clean(f.title)}**：${clean(f.detail)}`), '', '## Gate 接口实测', '', '| 接口 | 成功/总数 | 总耗时 P50 | 总耗时 P95 | 网络均值 | 排队均值 | 校时均值 | 重试等待均值 |', '|---|---:|---:|---:|---:|---:|---:|---:|')
  for (const row of report.endpoint_stats) lines.push(`| ${clean(row.label)} ${clean(row.endpoint)} | ${row.success}/${row.count} | ${ms(row.stats.total_ms?.p50)} | ${ms(row.stats.total_ms?.p95)} | ${ms(row.stats.network_ms?.avg)} | ${ms(row.stats.throttle_wait_ms?.avg)} | ${ms(row.stats.clock_wait_ms?.avg)} | ${ms(row.stats.backoff_ms?.avg)} |`)
  lines.push('', '样本数较少时，P95 只代表本次样本的分位数；网络耗时包含连接、传输和 Gate 处理，未分别测量 DNS/TLS/服务端执行。', '', '## 后台环节', '', ...report.stages.map(s=>`- ${clean(s.label)}：${ms(s.duration_ms)} · ${clean(s.status)} · ${clean(s.note)}`), '', '## 当前服务观测到的真实委托请求', '', report.recent_real_requests.note)
  if (!report.recent_real_requests.items.length) lines.push('未测得真实委托请求，不用查询耗时替代下单或最终确认延迟。')
  for(const row of report.recent_real_requests.items) lines.push(`- ${row.method} ${row.endpoint}：${ms(row.total_ms)}（网络 ${ms(row.network_ms)}，排队 ${ms(row.throttle_wait_ms)}），${row.outcome}`)
  lines.push('', '## 推算与限制', '', ...report.estimates.map(e=>`- ${clean(e.label)}：${ms(e.value_ms)}。推算依据：${clean(e.basis)}`), '', ...Object.entries(report.limits).map(([key,value])=>`- ${clean(key)}：${clean(value)}`), '', '原始 JSON 报告包含每次样本与各阶段统计；报告不包含 API Key、签名、余额、订单号或持仓明细。')
  return lines.join('\n')
}
