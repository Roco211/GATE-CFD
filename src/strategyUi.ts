import type { Preview, Strategy } from './types'

export type StartRequestState = 'idle' | 'loading' | 'error'
export type StartPresentation = {
  state: 'idle' | 'loading' | 'error'
  label: string
  tone: 'action' | 'working' | 'running' | 'paused'
}

/** A saved creation request is not evidence that Gate accepted any orders. */
export function startPresentation(strategy: Pick<Strategy, 'status' | 'pending_count' | 'position_count'> | null | undefined, request: StartRequestState = 'idle'): StartPresentation {
  switch (strategy?.status) {
    case 'paused': case 'error': return { state: 'idle', label: '策略已暂停', tone: 'paused' }
    case 'legacy_paused': return { state: 'idle', label: '旧策略已停用', tone: 'paused' }
    case 'starting': return { state: 'loading', label: '正在铺设 Gate 委托…', tone: 'working' }
    case 'recovering': return { state: 'loading', label: '正在核对 Gate 状态…', tone: 'working' }
    case 'stopping': return { state: 'loading', label: '正在撤销委托…', tone: 'working' }
    case 'closing': return { state: 'loading', label: '正在撤单并平仓…', tone: 'working' }
    case 'running':
      return Number(strategy.pending_count ?? 0) + Number(strategy.position_count ?? 0) > 0
        ? { state: 'idle', label: '策略运行中', tone: 'running' }
        : { state: 'idle', label: '等待挂单条件', tone: 'working' }
  }
  return request === 'loading' ? { state: 'loading', label: '正在创建策略…', tone: 'working' }
    : { state: request, label: request === 'error' ? '重试开始' : '快速开始', tone: 'action' }
}

export function creationNotice(strategy: Pick<Strategy, 'status' | 'pending_count' | 'error'> | null | undefined): { title: string; description: string; status: 'info' | 'error' } {
  if (strategy?.status === 'paused' || strategy?.status === 'error') return { title: '策略已暂停', description: strategy.error || 'Gate 委托尚未完成，请查看策略状态。', status: 'error' }
  return {
    title: strategy?.status === 'running' ? '策略运行状态已同步' : '策略已创建，等待 Gate 委托确认',
    description: `已确认 ${strategy?.pending_count ?? 0} 笔 Gate 委托；后续以铺单进度和策略状态为准。`,
    status: 'info',
  }
}

export function resumePresentation(strategy: Pick<Strategy, 'status' | 'execution_mode' | 'recoverable' | 'resume_blockers'> | null | undefined, context: { online: boolean; connected: boolean; ready: boolean }) {
  const visible = strategy?.status === 'paused' && strategy.execution_mode === 'native_trigger'
  const verified = visible && strategy?.recoverable === true
  const blockers = [...new Set(strategy?.resume_blockers?.filter(message => typeof message === 'string' && !!message.trim()) ?? [])]
  if (!context.online) blockers.unshift('后台连接中断，恢复连接后再核验。')
  else if (!context.connected || !context.ready) blockers.unshift('Gate 账户尚未就绪，请先核验连接。')
  if (visible && strategy?.recoverable !== true && !blockers.length) blockers.push('正在核验恢复条件，请稍后重新同步。')
  return { visible, allowed: verified && blockers.length === 0 && context.online && context.connected && context.ready, blockers, readyMessage: verified ? '核验已通过，可以恢复当前网格继续补单。' : '', pauseReasonLabel: verified ? '此前暂停原因' : '暂停原因' }
}

const strings = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && !!item.trim()) : []

export const PREVIEW_MAX_AGE_MS = 5000

/** New preview evidence may supersede an older account poll, but always expires. */
export function isPreviewFresh(preview: Preview | null | undefined, receivedAt: number | null, now: number): boolean {
  if (!preview || receivedAt === null || !Number.isFinite(receivedAt) || now < receivedAt || now - receivedAt >= PREVIEW_MAX_AGE_MS || preview.account?.stale !== false) return false
  const stamp = preview.account.updated_at
  if (stamp === undefined || stamp === null || stamp === '') return false
  const numeric = typeof stamp === 'number' ? stamp : /^\d+(?:\.\d+)?$/.test(stamp) ? Number(stamp) : NaN
  const updatedAt = Number.isFinite(numeric) ? numeric < 1e12 ? numeric * 1000 : numeric : Date.parse(String(stamp))
  // Receipt-time expiry also bounds a backend clock ahead of the browser clock.
  return Number.isFinite(updatedAt) && now - updatedAt < PREVIEW_MAX_AGE_MS
}

/** Only explicit blockers are errors; legacy mixed warnings never become errors. */
export function previewMessages(preview: Preview | null | undefined, context: { connected: boolean; accountStale: boolean }) {
  const blockers = strings(preview?.blockers)
  const codes = Array.isArray(preview?.blocker_codes) ? preview.blocker_codes : []
  const syncing = context.connected && (context.accountStale || codes.includes('account_sync_required'))
  const blocking = blockers.filter((_, index) => codes[index] !== 'account_sync_required')
  const information = Array.isArray(preview?.blockers) ? strings(preview?.warnings).filter(message => !blockers.includes(message)) : []
  return {
    blockers: [...new Set(blocking)],
    information: [...new Set(information)],
    syncMessage: syncing ? 'Gate 已连接，正在同步账户状态，完成后即可重新核验。' : '',
    fallbackMessage: preview && !preview.can_start && !blockers.length && !syncing ? '正在核验启动条件，请稍后查看最新预览。' : '',
  }
}
