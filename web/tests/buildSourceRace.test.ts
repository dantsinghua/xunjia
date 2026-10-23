import { test } from 'node:test'
import assert from 'node:assert/strict'
import { build } from 'vite'
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

test('a concurrent source edit after module compilation cannot bless old output as new', async () => {
  const root = mkdtempSync(join(tmpdir(), 'xj-compile-race-'))
  const web = fileURLToPath(new URL('..', import.meta.url))
  try {
    mkdirSync(join(root, 'src'))
    writeFileSync(join(root, 'src/main.ts'), 'console.log("SYNTHETIC_OLD_CODE")')
    writeFileSync(join(root, 'index.html'), '<script type="module" src="/src/main.ts"></script>')
    writeFileSync(join(root, 'package.json'), '{"type":"module"}')
    for (const name of ['vite.config.ts', 'build-source.mjs']) copyFileSync(join(web, name), join(root, name))
    symlinkSync(join(web, 'node_modules'), join(root, 'node_modules'), 'dir')
    let changed = false
    await assert.rejects(build({ root, configFile: join(root, 'vite.config.ts'), plugins: [{
      name: 'synthetic-concurrent-edit',
      renderChunk() {
        if (!changed) { changed = true; writeFileSync(join(root, 'src/main.ts'), 'console.log("SYNTHETIC_NEW_CODE")') }
      },
    }] }), /FRONTEND_SOURCE_CHANGED_DURING_BUILD/)
  } finally { rmSync(root, { recursive: true }) }
})
