import { execFile } from 'node:child_process'
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

async function execute(
  command: AlarmHttpFixtureCommand | 'force-due',
  extraArguments: string[],
  environment = buildAcceptanceEnvironment(process.env),
) {
  const repositoryRoot = path.resolve(process.cwd(), '..')
  const script = path.join(
    repositoryRoot,
    'backend',
    'scripts',
    'alarm_http_notification_e2e_fixture.py',
  )
  try {
    const { stdout } = await execFileAsync(
      'python',
      [script, command, ...extraArguments],
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
