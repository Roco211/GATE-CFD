import assert from 'node:assert/strict'
import test from 'node:test'
import { createPreviewScheduler } from './previewScheduler.ts'

class Clock {
  now = 0
  sequence = 0
  jobs = new Map<number, { at: number; callback: () => void }>()
  schedule = (callback: () => void, delay: number) => {
    const id = ++this.sequence
    this.jobs.set(id, { at: this.now + delay, callback })
    return () => { this.jobs.delete(id) }
  }
  async advance(to: number) {
    while (true) {
      const next = [...this.jobs].sort((a, b) => a[1].at - b[1].at)[0]
      if (!next || next[1].at > to) break
      this.now = next[1].at
      this.jobs.delete(next[0])
      next[1].callback()
      await Promise.resolve()
      await Promise.resolve()
    }
    this.now = to
    await Promise.resolve()
  }
}

test('continuous 2-second refreshes cannot starve 3-second pending preview promises', async () => {
  const clock = new Clock()
  const accepted: { id: number; at: number }[] = []
  const signals: AbortSignal[] = []
  let inFlight = 0, peak = 0, count = 0
  const runner = createPreviewScheduler({
    schedule: clock.schedule, delayMs: 230,
    fetch: signal => {
      signals.push(signal)
      peak = Math.max(peak, ++inFlight)
      const id = ++count
      return new Promise<number>(resolve => { clock.schedule(() => { inFlight--; resolve(id) }, 3000) })
    },
    onResult: id => { accepted.push({ id, at: clock.now }) },
    onError: error => { throw error },
  })
  runner.refresh()
  for (const tick of [2000, 4000, 6000, 8000]) { await clock.advance(tick); runner.refresh() }
  await clock.advance(10000)
  assert.deepEqual(accepted, [{ id: 1, at: 3230 }, { id: 2, at: 6460 }, { id: 3, at: 9690 }])
  assert.equal(peak, 1)
  assert.equal(signals.some(signal => signal.aborted), false)
  assert.equal(count, 4)
  runner.dispose()
})

test('multiple pending refreshes create one follow-up and never overwrite results with old parameters', async () => {
  const clock = new Clock()
  const accepted: string[] = []
  let resolveOld!: (value: string) => void
  let signal!: AbortSignal
  const old = createPreviewScheduler({ schedule: clock.schedule, delayMs: 0, fetch: current => { signal = current; return new Promise<string>(resolve => { resolveOld = resolve }) }, onResult: value => accepted.push(value), onError: () => assert.fail('aborted request must not show an error') })
  old.refresh()
  await clock.advance(0)
  for (let i = 0; i < 10; i++) old.refresh()
  old.dispose()
  assert.equal(signal.aborted, true)
  resolveOld('old config')
  await clock.advance(1)
  const next = createPreviewScheduler({ schedule: clock.schedule, delayMs: 0, fetch: async () => 'new config', onResult: value => accepted.push(value), onError: () => assert.fail() })
  next.refresh()
  await clock.advance(2)
  assert.deepEqual(accepted, ['new config'])
  assert.equal(clock.jobs.size, 0)
  next.dispose()
})

test('a failed preview releases the flight and the coalesced refresh can recover', async () => {
  const clock = new Clock()
  let reject!: (error: Error) => void
  let calls = 0
  const results: string[] = [], errors: unknown[] = []
  const runner = createPreviewScheduler({ schedule: clock.schedule, delayMs: 0, fetch: () => ++calls === 1 ? new Promise<string>((_, fail) => { reject = fail }) : Promise.resolve('fresh'), onResult: value => results.push(value), onError: error => errors.push(error) })
  runner.refresh()
  await clock.advance(0)
  runner.refresh()
  runner.refresh()
  reject(new Error('temporary read failure'))
  await clock.advance(1)
  await clock.advance(2)
  assert.equal(errors.length, 1)
  assert.deepEqual(results, ['fresh'])
  assert.equal(calls, 2)
  runner.dispose()
})
