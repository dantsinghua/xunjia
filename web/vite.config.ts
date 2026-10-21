import { defineConfig } from 'vite'
import { randomUUID } from 'node:crypto'
import vue from '@vitejs/plugin-vue'

const buildVersion = randomUUID()

export default defineConfig(({ command }) => ({
  define: { __XUNJIA_BUILD_VERSION__: JSON.stringify(command === 'build' ? buildVersion : 'development') },
  plugins: [vue(), {
    name: 'xunjia-build-version',
    apply: 'build',
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'assets/build-version.json', source: JSON.stringify({ version: buildVersion }) })
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
