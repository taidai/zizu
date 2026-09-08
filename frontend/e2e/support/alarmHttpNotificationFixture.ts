import { execFile } from 'node:child_process'
import { statSync } from 'node:fs'
import path from 'node:path'
import { promisify } from 'node:util'

import { buildAcceptanceEnvironment } from './acceptanceEnvironment.mjs'

const execFileAsync = promisify(execFile)

export type AlarmHttpFixtureCommand =
  | 'setup'
  | 'start-receiver'
  | 'receiver-status'
  | 'clear-receiver'
  | 'cleanup'

export interface AlarmHttpFixtureSetup {
  status: 'ready'
  node_id: string
  node_name: string
  tag_id: string
  tag_key: string
  entity_id: string
  entity_name: string
  alarm_name: string
  config_name: string
  rule_set_key: string
}

export interface AlarmHttpReceiverRecord {
  idempotency_key: string
  path: string
  body: Record<string, unknown> | string | null
}

export interface AlarmHttpReceiverStatus {
  status: 'ready'
  records: AlarmHttpReceiverRecord[]
}

export interface AlarmHttpFixtureProcessCommand {
  executable: 'python'
  arguments: string[]
}

export function resolveAlarmHttpFixtureScript(
  repositoryRoot: string,
  environment: { baseUrl: string },
  source: NodeJS.ProcessEnv = process.env,
): string {
  const defaultScript = path.join(
    repositoryRoot,
    'backend',
    'scripts',
    'alarm_http_notification_e2e_fixture.py',
  )
  const localScript = String(
    source.ZIZU_E2E_LOCAL_ALARM_HTTP_FIXTURE_SCRIPT ?? '',
  ).trim()
  if (!localScript) return defaultScript

  let site: URL
  try {
    site = new URL(environment.baseUrl)
  } catch {
    throw new Error('Local alarm HTTP fixture override requires HTTP loopback 127.0.0.1:19027')
  }
  if (
    site.protocol !== 'http:'
    || site.hostname !== '127.0.0.1'
    || site.port !== '19027'
  ) {
    throw new Error('Local alarm HTTP fixture override requires HTTP loopback 127.0.0.1:19027')
  }
  const remoteOption = Object.entries(source).find(([key, value]) => (
    (key.startsWith('ZIZU_E2E_SSH_') || key === 'ZIZU_E2E_SUDO_PASSWORD')
    && String(value ?? '').trim() !== ''
  ))
  if (remoteOption) {
    throw new Error('Local alarm HTTP fixture override refuses SSH or sudo options')
  }
  if (
    !path.isAbsolute(localScript)
    || path.extname(localScript).toLowerCase() !== '.py'
    || !isExistingFile(localScript)
  ) {
    throw new Error(
      'ZIZU_E2E_LOCAL_ALARM_HTTP_FIXTURE_SCRIPT must be an existing absolute Python script path',
    )
  }
  return path.normalize(localScript)
}

function isExistingFile(filePath: string): boolean {
  try {
    return statSync(filePath).isFile()
  } catch {
    return false
  }
}

export function buildAlarmHttpFixtureCommand(
  repositoryRoot: string,
  command: AlarmHttpFixtureCommand | 'force-due',
  extraArguments: string[],
  environment: { baseUrl: string },
  source: NodeJS.ProcessEnv = process.env,
): AlarmHttpFixtureProcessCommand {
  return {
    executable: 'python',
    arguments: [
      resolveAlarmHttpFixtureScript(repositoryRoot, environment, source),
      command,
      ...extraArguments,
    ],
  }
}

async function execute(
  command: AlarmHttpFixtureCommand | 'force-due',
  extraArguments: string[],
  environment = buildAcceptanceEnvironment(process.env),
) {
  const repositoryRoot = path.resolve(process.cwd(), '..')
  const fixtureCommand = buildAlarmHttpFixtureCommand(
    repositoryRoot,
    command,
    extraArguments,
    environment,
  )
  try {
    const { stdout } = await execFileAsync(
      fixtureCommand.executable,
      fixtureCommand.arguments,
      {
        cwd: repositoryRoot,
        env: { ...process.env, ZIZU_E2E_RUN_ID: environment.runId },
        timeout: 180_000,
        maxBuffer: 1024 * 1024,
        windowsHide: true,
      },
    )
    const line = stdout.trim().split(/\r?\n/).at(-1)
    if (!line) throw new Error(`E2E fixture ${command} returned no result`)
    return JSON.parse(line) as Record<string, unknown>
  } catch (error) {
    const raw = error instanceof Error ? error.message : String(error)
    const secrets = [
      environment.username,
      environment.password,
      String(process.env.ZIZU_E2E_SSH_PASSWORD || ''),
    ].filter(Boolean)
    const safe = secrets.reduce(
      (message, secret) => message.split(secret).join('[REDACTED]'),
      raw,
    )
    throw new Error(`Alarm HTTP E2E fixture ${command} failed: ${safe}`)
  }
}

export async function runAlarmHttpFixture(
  command: AlarmHttpFixtureCommand,
  environment = buildAcceptanceEnvironment(process.env),
) {
  return execute(command, [], environment)
}

export async function setupAlarmHttpFixture(
  environment = buildAcceptanceEnvironment(process.env),
): Promise<AlarmHttpFixtureSetup> {
  return execute('setup', [], environment) as Promise<AlarmHttpFixtureSetup>
}

export async function alarmHttpReceiverStatus(
  environment = buildAcceptanceEnvironment(process.env),
): Promise<AlarmHttpReceiverStatus> {
  return execute('receiver-status', [], environment) as Promise<AlarmHttpReceiverStatus>
}

export async function forceAlarmHttpDeliveryDue(
  notificationId: string,
  environment = buildAcceptanceEnvironment(process.env),
) {
  return execute('force-due', ['--notification-id', notificationId], environment)
}
