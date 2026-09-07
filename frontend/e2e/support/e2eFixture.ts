import { execFile } from 'node:child_process'
import path from 'node:path'
import { promisify } from 'node:util'

import {
  buildAcceptanceEnvironment,
  buildTemporaryResourceName,
} from './acceptanceEnvironment.mjs'

const execFileAsync = promisify(execFile)

export type FixtureCommand = 'preflight' | 'setup' | 'ensure-strategy' | 'cleanup'
export type FixtureScalar = number | string | boolean

export function fixtureTimeoutMs(command: FixtureCommand | 'publish'): number {
  return command === 'cleanup' ? 90_000 : 30_000
}

export function fixtureNames(
  environment = buildAcceptanceEnvironment(process.env),
) {
  const neuronRunId = environment.runId.replaceAll('-', '_')
  return {
    root: environment.writeRoot,
    platformNode: buildTemporaryResourceName(environment, '设备'),
    neuronNode: `zizu_e2e_${neuronRunId}`,
    neuronGroup: 'e2e_data',
    neuronTag: 'e2e_active_power',
    bitTag: 'e2e_fault_bit',
  }
}

export function encodeFixtureScalar(value: FixtureScalar): string {
  if (typeof value === 'number' && !Number.isFinite(value)) {
    throw new Error('E2E fixture only accepts finite numeric values')
  }
  return JSON.stringify(value)
}

export function resolveFixtureScript(
  repositoryRoot: string,
  environment: { baseUrl: string },
  source: NodeJS.ProcessEnv = process.env,
): string {
  const defaultScript = path.join(
    repositoryRoot,
    'backend',
    'scripts',
    'node_management_e2e_fixture.py',
  )
  const localScript = String(source.ZIZU_E2E_LOCAL_FIXTURE_SCRIPT ?? '').trim()
  if (!localScript) return defaultScript

  const site = new URL(environment.baseUrl)
  if (site.protocol !== 'http:' || site.hostname !== '127.0.0.1') {
    throw new Error('Local E2E fixture override requires an HTTP loopback site')
  }
  const remoteOption = Object.entries(source).find(([key, value]) => (
    (key.startsWith('ZIZU_E2E_SSH_') || key === 'ZIZU_E2E_SUDO_PASSWORD')
    && String(value ?? '').trim() !== ''
  ))
  if (remoteOption) {
    throw new Error('Local E2E fixture override refuses SSH and sudo options')
  }
  if (!path.isAbsolute(localScript) || path.extname(localScript).toLowerCase() !== '.py') {
    throw new Error('ZIZU_E2E_LOCAL_FIXTURE_SCRIPT must be an absolute Python script path')
  }
  return path.normalize(localScript)
}

async function executeFixture(
  command: FixtureCommand | 'publish',
  extraArguments: string[],
  environment = buildAcceptanceEnvironment(process.env),
) {
  const repositoryRoot = path.resolve(process.cwd(), '..')
  const script = resolveFixtureScript(repositoryRoot, environment)
  const commandArguments = [script, command, ...extraArguments]
  try {
    const { stdout } = await execFileAsync('python', commandArguments, {
      cwd: repositoryRoot,
      env: {
        ...process.env,
        ZIZU_E2E_RUN_ID: environment.runId,
      },
      timeout: fixtureTimeoutMs(command),
      maxBuffer: 1024 * 1024,
      windowsHide: true,
    })
    const line = stdout.trim().split(/\r?\n/).at(-1)
    if (!line) {
      throw new Error(`E2E fixture ${command} returned no result`)
    }
    return JSON.parse(line)
  } catch (error) {
    const raw = error instanceof Error ? error.message : String(error)
    const safe = raw
      .split(environment.username).join('[REDACTED]')
      .split(environment.password).join('[REDACTED]')
    throw new Error(`E2E fixture ${command} failed: ${safe}`)
  }
}

export async function runFixture(
  command: FixtureCommand,
  environment = buildAcceptanceEnvironment(process.env),
) {
  return executeFixture(command, [], environment)
}

export async function publishRawPoint(
  pointKey: string,
  value: FixtureScalar,
  environment = buildAcceptanceEnvironment(process.env),
) {
  return executeFixture(
    'publish',
    ['--point-key', pointKey, '--value-json', encodeFixtureScalar(value)],
    environment,
  )
}
