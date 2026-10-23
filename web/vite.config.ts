import { defineConfig } from 'vite'
import { createHash, randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import vue from '@vitejs/plugin-vue'
// @ts-expect-error The build-only helper is plain ESM, not application TypeScript.
import { publicAssets, sourceHash } from './build-source.mjs'

const buildVersion = randomUUID()
const webRoot = fileURLToPath(new URL('.', import.meta.url))
let buildSourceHash = ''

export default defineConfig(({ command }) => ({
  define: { __XUNJIA_BUILD_VERSION__: JSON.stringify(command === 'build' ? buildVersion : 'development') },
  plugins: [vue(), {
    name: 'xunjia-build-version',
    apply: 'build',
    enforce: 'post',
    buildStart() { buildSourceHash = sourceHash(webRoot) },
    generateBundle(_options, bundle) {
      const root = webRoot
      if (sourceHash(root) !== buildSourceHash) throw new Error('FRONTEND_SOURCE_CHANGED_DURING_BUILD')
      const assets = { ...publicAssets(root), ...Object.fromEntries(Object.entries(bundle).map(([name, item]) => [name,
        createHash('sha256').update(item.type === 'chunk' ? item.code : item.source).digest('hex')])) }
      this.emitFile({ type: 'asset', fileName: 'assets/build-version.json', source: JSON.stringify({
        version: buildVersion, source_hash: buildSourceHash, assets,
      }) })
    },
  }],
  server: {
    port: 5190,
    // 默认只绑 [::1],127.0.0.1 会 ERR_CONNECTION_REFUSED;绑 0.0.0.0 两个栈都通
    host: '0.0.0.0',
    // Vite 6 拦未知 Host(403 Blocked request),经 frp 挂公网必须显式放行该域名
    allowedHosts: ['xunjia.greydan.xin'],
    proxy: {
      // 正式开发后端监听 IPv4；使用明确的回环地址。
      '/api': 'http://127.0.0.1:8120',
      '/ws': { target: 'ws://127.0.0.1:8120', ws: true },
    },
  },
  build: { outDir: 'dist', chunkSizeWarningLimit: 2000 },
}))
