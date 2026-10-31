import { test } from 'node:test'
import assert from 'node:assert/strict'
import { effectScope, nextTick, reactive } from 'vue'
import TraceModal from '../../src/components/TraceModal.vue'
import { getMessageTrace } from '../../src/api'

const response = (data: unknown) => new Response(JSON.stringify({ ok: true, data }))
const idle = () => new Promise(resolve => setTimeout(resolve, 10))
const message = (id: string) => ({ id, text: id, role: 'peer', tag: '', time: '', bond: '', idx: 0, cp: 'peer' })
const trace = (id: string) => ({ id, text: id, status: '已持久化', summary: '数据库证据', dirLabel: '接收', steps: [], meta: [] })
Object.defineProperty(globalThis, 'window', { value: { innerWidth: 1000 }, configurable: true })
Object.defineProperty(globalThis, 'document', { value: {
  documentElement: { clientWidth: 1000 }, body: { style: { setProperty() {}, removeProperty() {} }, classList: { add() {}, remove() {} } },
}, configurable: true })

test('持久溯源接口使用真实消息ID，离线错误保留', async () => {
  let url = ''
  globalThis.fetch = async input => { url = String(input); return response(trace('real-id')) }
  assert.equal((await getMessageTrace('real-id')).id, 'real-id')
  assert.equal(url, '/api/negotiation/messages/real-id/trace')
  globalThis.fetch = async () => { throw new Error('offline') }
  await assert.rejects(getMessageTrace('real-id'), /offline/)
})

test('切换消息后旧请求不能覆盖新消息的持久化证据', async () => {
  const pending: Array<(r: Response) => void> = []
  globalThis.fetch = async () => new Promise(resolve => pending.push(resolve))
  const props = reactive({ msg: message('first') as any })
  const scope = effectScope()
  const vm: any = scope.run(() => (TraceModal as any).setup(props, { expose() {}, emit() {} }))
  assert.equal(vm.trace.value.status, '读取中')
  props.msg = message('second')
  await nextTick()
  pending[1](response(trace('second')))
  await idle()
  pending[0](response(trace('first')))
  await idle()
  assert.equal(vm.trace.value.id, 'second')
  assert.equal(vm.trace.value.status, '已持久化')
  props.msg = null
  await nextTick()
  scope.stop()
})

test('读取失败显示失败而非无审计记录或伪造阶段', async () => {
  globalThis.fetch = async () => { throw new Error('server unavailable') }
  const props = reactive({ msg: message('failed') as any })
  const scope = effectScope()
  const vm: any = scope.run(() => (TraceModal as any).setup(props, { expose() {}, emit() {} }))
  await idle()
  assert.equal(vm.trace.value.status, '读取失败')
  assert.match(vm.trace.value.summary, /server unavailable/)
  assert.deepEqual(vm.trace.value.steps, [])
  props.msg = null
  await nextTick()
  scope.stop()
})
