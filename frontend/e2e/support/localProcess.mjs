import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

function waitForExit(child) {
  if (child.exitCode !== null) return Promise.resolve()
  return new Promise((resolve) => child.once('exit', resolve))
}

export async function stopSpawnedProcess(child, {
  platform = process.platform,
  taskkill = (arguments_) => execFileAsync('taskkill.exe', arguments_, { windowsHide: true }),
} = {}) {
  if (!child || child.exitCode !== null) return

  const exited = waitForExit(child)
  if (platform === 'win32') {
    if (!Number.isInteger(child.pid) || child.pid <= 0) {
      throw new Error('Windows fixture process has no spawned PID')
    }
    try {
      await taskkill(['/PID', String(child.pid), '/T', '/F'])
    } catch (error) {
      if (child.exitCode === null) throw error
    }
  } else {
    child.kill()
  }
  await exited
}
