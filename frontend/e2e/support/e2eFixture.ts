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

async function executeFixture(
  command: FixtureCommand | 'publish',
  extraArguments: string[],
  environment = buildAcceptanceEnvironment(process.env),
) {
  const repositoryRoot = path.resolve(process.cwd(), '..')
  const script = path.join(repositoryRoot, 'backend', 'scripts', 'node_management_e2e_fixture.py')
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
