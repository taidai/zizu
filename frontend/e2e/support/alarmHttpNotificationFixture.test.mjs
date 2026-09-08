import assert from 'node:assert/strict'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import {
  buildAlarmHttpFixtureCommand,
  resolveAlarmHttpFixtureScript,
} from './alarmHttpNotificationFixture.ts'


const repositoryRoot = path.resolve('C:/workspace/zizu')
const localEnvironment = { baseUrl: 'http://127.0.0.1:19027' }

async function withTemporaryPythonFixture(run) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'zizu-alarm-fixture-'))
  const script = path.join(directory, 'fixture.py')
  await writeFile(script, '# private local test fixture\n', 'utf8')
  try {
    await run(script)
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
}

test('alarm fixture keeps the repository Python script and command arguments by default', () => {
  assert.deepEqual(
    buildAlarmHttpFixtureCommand(
      repositoryRoot,
      'force-due',
      ['--notification-id', 'notification-123'],
      { baseUrl: 'https://site.example' },
      {},
    ),
    {
      executable: 'python',
      arguments: [
        path.join(repositoryRoot, 'backend', 'scripts', 'alarm_http_notification_e2e_fixture.py'),
        'force-due',
        '--notification-id',
        'notification-123',
      ],
    },
  )
})

test('alarm fixture accepts an existing absolute local Python override only for the approved loopback target', async () => {
  await withTemporaryPythonFixture((script) => {
    const source = { ZIZU_E2E_LOCAL_ALARM_HTTP_FIXTURE_SCRIPT: script }

    assert.equal(
      resolveAlarmHttpFixtureScript(repositoryRoot, localEnvironment, source),
      script,
    )
    assert.deepEqual(
      buildAlarmHttpFixtureCommand(
        repositoryRoot,
        'setup',
        [],
        localEnvironment,
        source,
      ),
      { executable: 'python', arguments: [script, 'setup'] },
    )
  })
})

test('alarm fixture passes every existing CLI command unchanged to an approved local script', async () => {
  await withTemporaryPythonFixture((script) => {
    const source = { ZIZU_E2E_LOCAL_ALARM_HTTP_FIXTURE_SCRIPT: script }
    const expected = [
      ['setup', []],
      ['start-receiver', []],
      ['receiver-status', []],
      ['clear-receiver', []],
      ['cleanup', []],
      ['force-due', ['--notification-id', 'notification-123']],
    ]

    for (const [command, extraArguments] of expected) {
      assert.deepEqual(
        buildAlarmHttpFixtureCommand(
          repositoryRoot,
          command,
          extraArguments,
          localEnvironment,
          source,
        ),
        { executable: 'python', arguments: [script, command, ...extraArguments] },
        command,
      )
    }
  })
})

test('alarm fixture rejects local override before spawn for every unapproved URL or script', async () => {
  await withTemporaryPythonFixture((script) => {
    const source = { ZIZU_E2E_LOCAL_ALARM_HTTP_FIXTURE_SCRIPT: script }

    for (const baseUrl of [
      'https://127.0.0.1:19027',
      'http://localhost:19027',
      'http://[::1]:19027',
      'http://127.0.0.1:19026',
    ]) {
      assert.throws(
        () => resolveAlarmHttpFixtureScript(repositoryRoot, { baseUrl }, source),
        /loopback.*19027/i,
        baseUrl,
      )
    }
  })

  for (const script of [
    'relative.py',
    path.resolve(os.tmpdir(), 'zizu-missing-alarm-fixture.py'),
    path.resolve(os.tmpdir(), 'zizu-alarm-fixture.txt'),
  ]) {
    assert.throws(
      () => resolveAlarmHttpFixtureScript(
        repositoryRoot,
        localEnvironment,
        { ZIZU_E2E_LOCAL_ALARM_HTTP_FIXTURE_SCRIPT: script },
      ),
      /existing absolute Python script path/i,
      script,
    )
  }
})

test('alarm fixture rejects every SSH or sudo setting before spawning a local override', async () => {
  await withTemporaryPythonFixture((script) => {
    for (const key of [
      'ZIZU_E2E_SSH_HOST',
      'ZIZU_E2E_SSH_PORT',
      'ZIZU_E2E_SSH_USER',
      'ZIZU_E2E_SSH_PASSWORD',
      'ZIZU_E2E_SUDO_PASSWORD',
    ]) {
      assert.throws(
        () => buildAlarmHttpFixtureCommand(
          repositoryRoot,
          'setup',
          [],
          localEnvironment,
          { ZIZU_E2E_LOCAL_ALARM_HTTP_FIXTURE_SCRIPT: script, [key]: 'private-value' },
        ),
        /SSH or sudo options/i,
        key,
      )
    }
  })
})
