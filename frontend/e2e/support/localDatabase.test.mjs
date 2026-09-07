import test from 'node:test'
import assert from 'node:assert/strict'
import { localDatabaseEnvironment } from './localDatabase.mjs'

const name = 'zizu_task8_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_test'
const source = { DB_HOST: '127.0.0.1', DB_PORT: '15434', DB_USER: 'postgres', DB_PASSWORD: 'isolated-test-password' }

test('local database fixture uses the supplied local port and credentials', () => {
  assert.deepEqual(localDatabaseEnvironment(source, name), { ...source, DB_NAME: name })
})

test('local database fixture rejects remote hosts and non-disposable database names', () => {
  assert.throws(() => localDatabaseEnvironment({ ...source, DB_HOST: 'e606.hlszh.com' }, name), /本机/)
  assert.throws(() => localDatabaseEnvironment(source, 'zizu'), /隔离/)
})

test('missing credentials or invalid port never falls back to an old test instance', () => {
  assert.throws(() => localDatabaseEnvironment({ ...source, DB_PASSWORD: '' }, name), /凭据/)
  assert.throws(() => localDatabaseEnvironment({ ...source, DB_PORT: '0' }, name), /端口/)
  assert.throws(() => localDatabaseEnvironment({ ...source, DB_PORT: '15434;bad' }, name), /端口/)
})
