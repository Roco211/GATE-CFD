import test from 'node:test'
import assert from 'node:assert/strict'
import { completePhaseMetrics, latencyMarkdown, ms, jobRunning, type DiagnosticJob, type EndpointTiming } from './latencyReport.ts'

const job:DiagnosticJob={id:'diag_a',symbol:'XAUUSD',samples:3,status:'completed',created_at:1800000000,started_at:1800000000,finished_at:1800000010,progress:{completed:15,total:15,stage:'完成'},report:{source:'gate',read_only:true,duration_ms:10000,endpoint_stats:[],stages:[],findings:[{severity:'warning',title:'样本|不足',detail:'未做真实下单\n测试'}],recent_real_requests:{status:'not_measured',items:[],note:'HTTP返回不等于最终委托确认'},limits:{min_request_interval_ms:250},estimates:[]}}
test('reports distinguish missing trade measurements from zero latency',()=>{
  const text=latencyMarkdown(job,null)
  assert.match(text,/未测得真实委托请求/)
  assert.match(text,/HTTP返回不等于最终委托确认/)
  assert.equal(ms(null),'—');assert.equal(ms(0),'0 ms')
  assert.match(text,/样本\\\|不足/)
})
test('report includes local client samples without mixing them into Gate timing',()=>{
  const text=latencyMarkdown(job,{source:'browser',measured_at:'2026-09-07',samples:[{elapsed_ms:17.5,success:true},{elapsed_ms:5000,success:false}]})
  assert.match(text,/17.5 ms · 成功/);assert.match(text,/5,000 ms · 失败/)
  assert.equal(jobRunning({...job,status:'queued'}),true);assert.equal(jobRunning(job),false)
})
test('an incomplete job exports its actual incomplete status and not a success report',()=>{
  const text=latencyMarkdown({...job,status:'cancelled',report:null},null)
  assert.match(text,/cancelled/);assert.match(text,/尚无完整报告/)
})
test('server ISO timestamps export correctly without Unix timestamp coercion',()=>{
  assert.match(latencyMarkdown({...job,finished_at:'2026-09-07T14:00:00+00:00'},null),/2026-09-07T14:00:00.000Z/)
})

test('incomplete phase samples cannot be presented as a complete percentage breakdown',()=>{
  const metric={count:3,min:1,p50:1,p95:1,max:1,avg:1,total:3}
  const row:EndpointTiming={endpoint:'/test',label:'test',method:'GET',count:3,success:3,failed:0,samples:[],stats:Object.fromEntries(['total_ms','network_ms','throttle_wait_ms','clock_wait_ms','backoff_ms','processing_ms'].map(key=>[key,metric]))}
  assert.equal(completePhaseMetrics(row),true)
  row.stats.network_ms={...metric,count:1}
  assert.equal(completePhaseMetrics(row),false)
  row.stats.network_ms=null
  assert.equal(completePhaseMetrics(row),false)
})

test('nested numeric rate limits remain legible in Markdown exports',()=>{
  const text=latencyMarkdown({...job,report:{...job.report!,limits:{observed_gate_limits:[{endpoint:'/tradfi/orders',remaining:4}]}}},null)
  assert.match(text,/"remaining":4/)
  assert.doesNotMatch(text,/\[object Object\]/)
})
