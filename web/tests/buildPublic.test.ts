import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createHash } from 'node:crypto'
import { publicAssets, sourceHash } from '../build-source.mjs'

test('public files have exact output paths and byte hashes and also bind the source', () => {
  const root = mkdtempSync(join(tmpdir(), 'xj-public-proof-'))
  try {
    mkdirSync(join(root, 'public/nested'), { recursive: true })
    writeFileSync(join(root, 'public/logo.png'), Buffer.from([0, 1, 255]))
    writeFileSync(join(root, 'public/nested/logo.svg'), 'synthetic svg')
    const before = sourceHash(root)
    assert.deepEqual(publicAssets(root), {
      'logo.png': createHash('sha256').update(Buffer.from([0, 1, 255])).digest('hex'),
      'nested/logo.svg': createHash('sha256').update('synthetic svg').digest('hex'),
    })
    writeFileSync(join(root, 'public/logo.png'), 'changed public bytes')
    assert.notEqual(sourceHash(root), before)
    assert.equal(publicAssets(root)['logo.png'], createHash('sha256').update('changed public bytes').digest('hex'))
  } finally { rmSync(root, { recursive: true }) }
})

test('public cannot replace the reserved build manifest', () => {
  const root = mkdtempSync(join(tmpdir(), 'xj-public-reserved-'))
  try {
    mkdirSync(join(root, 'public/assets'), { recursive: true })
    writeFileSync(join(root, 'public/assets/build-version.json'), '{}')
    assert.throws(() => publicAssets(root), /FRONTEND_RESERVED_PUBLIC_PATH/)
  } finally { rmSync(root, { recursive: true }) }
})
