export type PreviewScheduler = { refresh(): void; dispose(): void }
type Schedule = (callback: () => void, delay: number) => () => void

/** One request at a time. Refreshes during a request coalesce into one follow-up. */
export function createPreviewScheduler<T>(options: {
  fetch: (signal: AbortSignal) => Promise<T>
  onResult: (value: T) => void
  onError: (error: unknown) => void
  delayMs?: number
  schedule?: Schedule
}): PreviewScheduler {
  const schedule: Schedule = options.schedule ?? ((callback, delay) => {
    const timer = setTimeout(callback, delay)
    return () => clearTimeout(timer)
  })
  let disposed = false
  let queued = false
  let controller: AbortController | null = null
  let cancelTimer: (() => void) | null = null

  function refresh() {
    if (disposed) return
    if (controller) { queued = true; return }
    if (cancelTimer) return
    cancelTimer = schedule(() => { cancelTimer = null; void execute() }, options.delayMs ?? 230)
  }

  async function execute() {
    if (disposed) return
    const current = new AbortController()
    controller = current
    queued = false
    try {
      const value = await options.fetch(current.signal)
      if (!disposed && !current.signal.aborted) options.onResult(value)
    } catch (error) {
      if (!disposed && !current.signal.aborted) options.onError(error)
    } finally {
      controller = null
      if (!disposed && queued) refresh()
    }
  }

  return {
    refresh,
    dispose() {
      disposed = true
      queued = false
      cancelTimer?.()
      cancelTimer = null
      controller?.abort()
    },
  }
}
