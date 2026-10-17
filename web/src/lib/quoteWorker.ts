/**
 * 实时行情管线 · worker 入口(被 Vite 以 worker 方式单独打包)
 *
 * 同一份脚本同时服务两种宿主:
 *   - SharedWorker:多标签共享。每个标签 connect 进来是一个 MessagePort,各自一个端口状态;
 *     WS 连接、组引用计数、静态表缓存全局只有一份(见 quoteCore.ts)。
 *   - 专用 Worker(降级第 2 级):`self` 本身就是端口,只有一个。
 *
 * 入口只做"识别宿主 + 把端口接到 QuoteCore 上",没有任何业务逻辑,
 * 这样主线程兜底(降级第 3 级)可以 new 同一个 QuoteCore 而不必 import 本文件
 * (import 本文件会在主线程执行下面的 self.onconnect 赋值,虽无害但脏)。
 *
 * 注意:tsconfig 的 lib 只有 DOM,没有 WebWorker(两者同时开会有几十个重复声明冲突),
 * 所以这里不用 SharedWorkerGlobalScope / DedicatedWorkerGlobalScope 的官方类型,自己声明最小形状。
 */
import { QuoteCore } from './quoteCore'
import type { MainToWorkerMsg, WorkerToMainMsg } from './quoteTypes'

interface SharedScope {
  onconnect: ((e: MessageEvent) => void) | null
}
interface DedicatedScope {
  postMessage(msg: WorkerToMainMsg): void
  onmessage: ((e: MessageEvent<MainToWorkerMsg>) => void) | null
}

const g = globalThis as unknown as Record<string, unknown>
const core = new QuoteCore()

/** SharedWorkerGlobalScope 只在 SharedWorker 里存在;专用 Worker 里 undefined */
const isShared = typeof g.SharedWorkerGlobalScope === 'function'
  && self instanceof (g.SharedWorkerGlobalScope as new () => object)

if (isShared) {
  (self as unknown as SharedScope).onconnect = (e: MessageEvent) => {
    const port = e.ports[0]
    const handle = core.connectPort({ postMessage: (m) => port.postMessage(m) })
    // 用 onmessage 而不是 addEventListener:赋 onmessage 会隐式 port.start(),少一个容易漏的调用
    port.onmessage = (ev: MessageEvent<MainToWorkerMsg>) => handle.onMessage(ev.data)
    // 对端标签被关掉/崩溃时端口会被 GC。Chrome 122+ 会派发 close 事件,是唯一能兜住"标签没来得及发 CLOSE 就没了"的手段;
    // 老浏览器没这个事件,只能靠主线程 pagehide 时主动发 CLOSE。
    port.addEventListener('close', () => handle.close())
  }
} else {
  const scope = self as unknown as DedicatedScope
  const handle = core.connectPort({ postMessage: (m) => scope.postMessage(m) })
  scope.onmessage = (ev) => handle.onMessage(ev.data)
}
