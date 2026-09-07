import React, { useEffect, useState } from 'react'
import ReactDOM from 'react-dom/client'
import { LockKeyhole, ArrowRight, ShieldCheck } from 'lucide-react'
import { Input } from '@/components/motion/input'
import { StatefulButton } from '@/components/motion/button/stateful'
import './index.css'

function Login() {
  const [token,setToken]=useState('')
  const [busy,setBusy]=useState(false)
  const [error,setError]=useState('')
  const [configured,setConfigured]=useState(true)
  useEffect(()=>{
    const controller=new AbortController()
    void fetch('/api/auth/session',{signal:controller.signal,cache:'no-store'}).then(r=>r.json()).then(data=>{
      if(data.authenticated)window.location.replace('/')
      setConfigured(data.configured!==false)
    }).catch(()=>{})
    return ()=>controller.abort()
  },[])
  async function submit(event:React.FormEvent){
    event.preventDefault()
    if(busy||!token.trim())return
    setBusy(true);setError('')
    try{
      const response=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json','X-Grid-Client':'grid-studio'},body:JSON.stringify({token}),signal:AbortSignal.timeout(15000)})
      const result=await response.json()
      if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'验证未通过，请重试。')
      setToken('');window.location.replace('/')
    }catch(e){setError(e instanceof Error?e.message:'连接失败，请稍后重试。');setBusy(false)}
  }
  return <main className="access-page"><div className="access-brand"><span className="brand-mark"><i/><i/><i/><i/></span>Grid<span>Studio</span></div><section className="access-card" aria-labelledby="access-heading"><div className="access-lock"><LockKeyhole size={26}/></div><span className="eyebrow">PRIVATE CONSOLE</span><h1 id="access-heading">验证后，进入工作台</h1><p>输入访问 token，解锁策略控制台。</p><form onSubmit={event=>void submit(event)}><Input label="访问 token" aria-label="访问 token" type="password" value={token} onChange={setToken} placeholder="请输入访问 token" autoComplete="current-password" disabled={busy||!configured} classNames={{field:'field-input-wrapper',input:'field-input'}}/>{!configured&&<p className="form-error" role="alert">后台尚未配置访问 token，请先完成部署配置。</p>}{error&&<p className="form-error" role="alert">{error}</p>}<StatefulButton type="submit" className="drawer-primary" state={busy?'loading':'idle'} loadingText="正在验证…" disabled={busy||!token.trim()||!configured} icon={<ArrowRight size={16}/>}>验证并进入</StatefulButton></form><div className="access-note"><ShieldCheck size={15}/><span>验证状态保留 12 小时。退出验证不会停止后台策略。</span></div></section><p className="access-footer">GATE CFD · 受保护的策略工作台</p></main>
}

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><Login/></React.StrictMode>)
