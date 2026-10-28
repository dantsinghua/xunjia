import { test } from 'node:test'
import assert from 'node:assert/strict'
import { newRequestId } from '../src/lib/requestId.ts'

function withCrypto(value: unknown, action: () => void) {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto')!
  Object.defineProperty(globalThis, 'crypto', { value, configurable: true })
  try { action() } finally { Object.defineProperty(globalThis, 'crypto', descriptor) }
}

test('native UUID stays available', () => {
  withCrypto({ randomUUID: () => 'native-id' }, () => assert.equal(newRequestId(), 'native-id'))
})

test('HTTP fallback uses secure random bytes and UUID version and variant bits', () => {
  let calls = 0
  withCrypto({ getRandomValues(bytes: Uint8Array) {
    calls++; assert.equal(bytes.length, 16)
    for (let i = 0; i < bytes.length; i++) bytes[i] = i
    return bytes
  } }, () => assert.equal(newRequestId(), '00010203-0405-4607-8809-0a0b0c0d0e0f'))
  assert.equal(calls, 1)
})

test('missing secure randomness refuses request identity without insecure fallback', () => {
  withCrypto({}, () => assert.throws(newRequestId, /未提交请求/))
})

test('HTTP fallback emits independent v4 request IDs', () => {
  const native = globalThis.crypto
  withCrypto({ getRandomValues: native.getRandomValues.bind(native) }, () => {
    const ids = Array.from({ length: 1000 }, newRequestId)
    assert.equal(new Set(ids).size, ids.length)
    for (const id of ids) assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  })
})
