import assert from 'node:assert/strict'
import test from 'node:test'
import { creationNotice, isPreviewFresh, previewMessages, resumePresentation, startPresentation } from './strategyUi.ts'
import type { Preview } from './types'

const preview = (changes: Partial<Preview> = {}): Preview => ({ can_start: false, sufficient_margin: true, levels: [], cells: [], warnings: [], ...changes })

test('a creation response is informational until Gate execution is confirmed', () => {
  assert.equal(creationNotice({ status: 'starting', pending_count: 0 }).status, 'info')
  assert.equal(startPresentation({ status: 'starting', pending_count: 0 }).state, 'loading')
  assert.doesNotMatch(creationNotice({ status: 'starting' }).title, /已启动|成功/)
})

test('paused Gate rejection overrides the previous request and running presentation', () => {
  assert.equal(startPresentation({ status: 'running', pending_count: 2 }).label, '策略运行中')
  assert.deepEqual(startPresentation({ status: 'paused', pending_count: 0 }, 'idle'), { state: 'idle', label: '策略已暂停', tone: 'paused' })
  assert.equal(startPresentation({ status: 'error' }, 'loading').tone, 'paused')
  assert.equal(creationNotice({ status: 'paused', error: 'Gate 拒绝委托' }).description, 'Gate 拒绝委托')
})

test('a running strategy without confirmed orders or positions only waits for placement', () => {
  assert.equal(startPresentation({ status: 'running', pending_count: 0, position_count: 0 }).label, '等待挂单条件')
  assert.equal(startPresentation({ status: 'running', position_count: 1 }).label, '策略运行中')
  assert.equal(startPresentation({ status: 'stopping' }).label, '正在撤销委托…')
  assert.equal(startPresentation({ status: 'closing' }).label, '正在撤单并平仓…')
  assert.equal(startPresentation({ status: 'stopped' }).label, '快速开始')
})

test('connected account synchronization is neutral and never asks to reconnect', () => {
  const result = previewMessages(preview({ blockers: ['Gate 已连接，账户状态正在同步'], blocker_codes: ['account_sync_required'], warnings: ['原生委托由 Gate 执行', '未提供数量步长'] }), { connected: true, accountStale: true })
  assert.deepEqual(result.blockers, [])
  assert.match(result.syncMessage, /已连接.*同步/)
  assert.doesNotMatch(result.syncMessage, /请连接|失败/)
  assert.equal(result.information.length, 2)
})

test('real blockers remain concise and separate from operating information', () => {
  const result = previewMessages(preview({ blockers: ['保证金不足', '账户正在同步'], blocker_codes: ['insufficient_margin', 'account_sync_required'], warnings: ['后台关闭不会撤单', '保证金不足'] }), { connected: true, accountStale: true })
  assert.deepEqual(result.blockers, ['保证金不足'])
  assert.deepEqual(result.information, ['后台关闭不会撤单'])
})

test('legacy mixed warnings cannot recreate the large error panel after cancellation', () => {
  const result = previewMessages(preview({ warnings: ['后台关闭不会撤单', '官方未提供数量步长', '请连接并核验真实账户'] }), { connected: true, accountStale: true })
  assert.deepEqual(result.blockers, [])
  assert.deepEqual(result.information, [])
  assert.equal(result.fallbackMessage, '')
  assert.match(result.syncMessage, /已连接/)
})

test('fresh account preview clears the temporary synchronization message', () => {
  const result = previewMessages(preview({ can_start: true, blockers: [], blocker_codes: [], warnings: ['仅估算保证金'] }), { connected: true, accountStale: false })
  assert.equal(result.syncMessage, '')
  assert.deepEqual(result.blockers, [])
  assert.deepEqual(result.information, ['仅估算保证金'])
})

test('fresh preview evidence supersedes an older stale account poll and expires', () => {
  const now = 1_800_000_000_000
  const value = preview({ can_start: true, blockers: [], account: { stale: false, updated_at: new Date(now).toISOString() } })
  const fresh = isPreviewFresh(value, now + 100, now + 1000)
  const oldSnapshot = { stale: true }
  assert.equal(fresh, true)
  assert.equal(!fresh && oldSnapshot.stale, false)
  assert.equal(isPreviewFresh(value, now + 100, now + 5000), false)
  assert.equal(isPreviewFresh({ ...value, account: { stale: false, updated_at: new Date(now + 60000).toISOString() } }, now, now + 5000), false)
})

test('a new response cannot make old or explicitly stale account data fresh', () => {
  const now = 1_800_000_000_000
  assert.equal(isPreviewFresh(preview({ account: { stale: false, updated_at: now - 6000 } }), now, now), false)
  assert.equal(isPreviewFresh(preview({ account: { stale: true, updated_at: now } }), now, now), false)
  assert.equal(isPreviewFresh(preview({ account: { stale: false } }), now, now), false)
  assert.equal(isPreviewFresh(preview(), null, now), false)
})

test('only an explicitly recoverable paused native strategy exposes an enabled resume action', () => {
  const connected = { online: true, connected: true, ready: true }
  const strategy = { status: 'paused', execution_mode: 'native_trigger' as const, recoverable: true, resume_blockers: [] }
  assert.equal(resumePresentation(strategy, connected).allowed, true)
  for (const changed of [{ status: 'running' }, { execution_mode: 'legacy_local' as const }, { recoverable: false }, { recoverable: undefined }]) assert.equal(resumePresentation({ ...strategy, ...changed }, connected).allowed, false)
  assert.equal(resumePresentation(strategy, { ...connected, online: false }).allowed, false)
  assert.equal(resumePresentation(strategy, { ...connected, ready: false }).allowed, false)
})

test('resume displays the actual blocker instead of describing locked parameters as the failure', () => {
  const result = resumePresentation({ status: 'paused', execution_mode: 'native_trigger', recoverable: false, resume_blockers: ['平仓原因尚未核对', '平仓原因尚未核对'] }, { online: true, connected: true, ready: true })
  assert.equal(result.allowed, false)
  assert.deepEqual(result.blockers, ['平仓原因尚未核对'])
  assert.doesNotMatch(result.blockers.join(''), /参数.*锁定/)
})

test('verified recovery distinguishes an old pause reason from the current ready state', () => {
  const context = { online: true, connected: true, ready: true }
  const strategy = { status: 'paused', execution_mode: 'native_trigger' as const, recoverable: true, resume_blockers: [] }
  const ready = resumePresentation(strategy, context)
  assert.equal(ready.readyMessage, '核验已通过，可以恢复当前网格继续补单。')
  assert.equal(ready.pauseReasonLabel, '此前暂停原因')
  const blocked = resumePresentation({ ...strategy, recoverable: false, resume_blockers: ['平仓原因待核对'] }, context)
  assert.equal(blocked.readyMessage, '')
  assert.equal(blocked.pauseReasonLabel, '暂停原因')
})
