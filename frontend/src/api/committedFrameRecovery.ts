type SnapshotLoader<T> = () => Promise<T>
type ActiveCheck = () => boolean
type RetryWait = (delayMs: number) => Promise<void>

const wait: RetryWait = (delayMs) => new Promise((resolve) => {
  window.setTimeout(resolve, delayMs)
})

export async function retryCommittedFrameSnapshot<T>(
  load: SnapshotLoader<T>,
  isActive: ActiveCheck,
  pause: RetryWait = wait,
): Promise<T | null> {
  let attempt = 0
  while (isActive()) {
    try {
      return await load()
    } catch {
      if (!isActive()) return null
      attempt += 1
      await pause(Math.min(5000, attempt * 1000))
    }
  }
  return null
}
