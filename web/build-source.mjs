import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, lstatSync, existsSync } from 'node:fs'
import { join, relative, sep } from 'node:path'

export function publicAssets(root) {
  const folder = join(root, 'public'), assets = {}
  if (!existsSync(folder)) return assets
  function walk(path) {
    const stat = lstatSync(path)
    if (stat.isSymbolicLink()) throw new Error('FRONTEND_SOURCE_SYMLINK')
    if (stat.isDirectory()) for (const name of readdirSync(path).sort()) walk(join(path, name))
    else if (stat.isFile()) {
      const name = relative(folder, path).split(sep).join('/')
      if (name === 'assets/build-version.json') throw new Error('FRONTEND_RESERVED_PUBLIC_PATH')
      assets[name] = createHash('sha256').update(readFileSync(path)).digest('hex')
    }
  }
  walk(folder)
  return assets
}

export function sourceHash(root) {
  const files = []
  function walk(folder) {
    for (const name of readdirSync(folder)) {
      const path = join(folder, name), stat = lstatSync(path)
      if (stat.isSymbolicLink()) throw new Error('FRONTEND_SOURCE_SYMLINK')
      if (stat.isDirectory()) walk(path)
      else if (stat.isFile()) files.push(path)
    }
  }
  for (const name of readdirSync(root)) {
    const path = join(root, name)
    if (lstatSync(path).isSymbolicLink() && ['src', 'public'].includes(name)) throw new Error('FRONTEND_SOURCE_SYMLINK')
    if (['src', 'public'].includes(name)) walk(path)
    else if (['index.html', 'package.json', 'package-lock.json', 'vite.config.ts', 'build-source.mjs'].includes(name)
      || /^tsconfig.*\.json$/.test(name)) files.push(path)
  }
  const hashes = Object.fromEntries(files.map(path => [relative(root, path).split(sep).join('/'), createHash('sha256').update(readFileSync(path)).digest('hex')]).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0))
  return createHash('sha256').update(JSON.stringify(hashes)).digest('hex')
}
