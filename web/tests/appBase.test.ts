import { test } from 'node:test'
import assert from 'node:assert/strict'
import { appBase, appPath } from '../src/lib/appBase.ts'

test('only the declared xunjia entry selects the application prefix', () => {
  for (const p of ['/xunjia', '/xunjia/', '/xunjia/assets/quoteWorker.js']) assert.equal(appBase(p), '/xunjia/')
  for (const p of ['/', '/assets/quoteWorker.js', '/xunjia-other/', '/njcb/']) assert.equal(appBase(p), '/')
})
test('API, websocket, asset and version paths stay in their application mount', () => {
  for (const p of ['/api/auth/me', '/api/im/send', '/ws/quote', '/logo.png', '/assets/build-version.json?_check=1']) {
    assert.equal(appPath(p, '/xunjia/'), '/xunjia' + p)
    assert.equal(appPath(p, '/'), p)
    assert.equal(appPath('/xunjia' + p, '/xunjia/'), '/xunjia' + p)
  }
})
test('existing absolute and relative overrides are not corrupted', () => {
  for (const p of ['https://example.invalid/api', 'wss://example.invalid/ws', '//example.invalid/a', './relative']) {
    assert.equal(appPath(p, '/xunjia/'), p)
  }
})
