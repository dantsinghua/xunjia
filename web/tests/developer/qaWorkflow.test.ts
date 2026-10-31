import { afterEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { effectScope, nextTick } from 'vue'
import DOMPurify from 'dompurify'
import Chat from '../../src/pages/Chat.vue'
import Settings from '../../src/components/QaWorkflowSettings.vue'
import { api } from '../../src/api'
import { clearImState, setImBinding } from '../../src/lib/imLink'
import { buildBonds } from './fixtures'

const response = (data: unknown) => new Response(JSON.stringify({ok: true, data}))
const originalFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = originalFetch; clearImState() })
// The node runner has no browser DOM; sanitization itself is covered by the browser gate.
Object.assign(DOMPurify, {sanitize: (text: string) => text})
function setup(component: any) {
  const scope = effectScope()
  const vm: any = scope.run(() => component.setup({}, {expose() {}, emit() {}}))
  return {scope, vm}
}
const candidate = {code: '000001.IB', name: '合成债A', current_day: true}
const message = () => ({inquiry: true, selected: [candidate.code], candidates: [candidate]})

test('QA API forwards signal and rejects malformed candidate entries without losing answer', async () => {
  const controller = new AbortController()
  globalThis.fetch = async (url, init) => {
    assert.equal(String(url), '/api/qa')
    assert.equal(init?.signal, controller.signal)
    assert.deepEqual(JSON.parse(String(init?.body)), {question: '我想询价'})
    return response({answer: '可信服务端答案', requires_selection: true, candidates: [null, false, candidate]})
  }
  const result = await api.qa('我想询价', controller.signal)
  assert.equal(result.ok, true)
  assert.equal(result.data.answer, '可信服务端答案')
  assert.deepEqual(result.data.candidates, [candidate])
})

test('opening an inquiry only reads preparation data and never sends', async () => {
  const calls: string[] = []
  const bond = {...buildBonds()[0], code: candidate.code, status: '待询价', hasActiveIntent: false}
  globalThis.fetch = async (url, init) => {
    calls.push(String(url))
    if (String(url).endsWith('/api/bonds/resolve')) return response([bond])
    if (String(url).endsWith('/api/im/inquiry/floor')) return response({items: []})
    return response({})
  }
  const {scope, vm} = setup(Chat)
  try {
    await vm.configureInquiry(message())
    assert.equal(vm.rfqOpen.value, true)
    assert.equal(vm.rfqBonds.value[0].code, candidate.code)
    assert.equal(calls.length, 3)
    assert.ok(calls.every(url => !/send|preview|confirm/.test(url)))
  } finally { scope.stop() }
})

test('same-length resolution with a different bond cannot open inquiry', async () => {
  globalThis.fetch = async url => String(url).endsWith('/api/bonds/resolve')
    ? response([{...buildBonds()[0], code: 'OTHER.IB', status: '待询价', hasActiveIntent: false}])
    : response({items: []})
  const {scope, vm} = setup(Chat)
  try {
    await vm.configureInquiry(message())
    assert.equal(vm.rfqOpen.value, false)
    assert.ok(vm.inquiryError.value)
  } finally { scope.stop() }
})

test('late QA response after a new chat cannot populate the new conversation', async () => {
  let finish!: (value: Response) => void
  globalThis.fetch = async () => new Promise(resolve => {finish = resolve})
  const {scope, vm} = setup(Chat)
  try {
    const pending = vm.send('查券 合成债A')
    vm.newChat()
    finish(response({answer: '旧问题的答案', candidates: [candidate], requires_selection: true}))
    await pending
    assert.deepEqual(vm.messages.value, [])
    assert.equal(vm.busy.value, false)
  } finally { scope.stop() }
})

test('an account context change fences an old inquiry preparation even with same binding version', async () => {
  const binding = {account_uid: 'synthetic-owner', workbench_id: 'synthetic-wb', version: 1, context_id: 'context-a'}
  setImBinding(binding, binding.account_uid)
  let finish!: (value: Response) => void
  globalThis.fetch = async url => String(url).endsWith('/api/bonds/resolve')
    ? new Promise(resolve => {finish = resolve}) : response({items: []})
  const {scope, vm} = setup(Chat)
  try {
    const pending = vm.configureInquiry(message())
    setImBinding({...binding, context_id: 'context-b'}, binding.account_uid)
    await nextTick()
    finish(response([{...buildBonds()[0], code: candidate.code, status: '待询价', hasActiveIntent: false}]))
    await pending
    assert.equal(vm.rfqOpen.value, false)
  } finally { scope.stop() }
})

test('configuration preserves an existing key by sending blank and clears entered secrets on failure', async () => {
  let body: any
  globalThis.fetch = async (_url, init) => {
    body = JSON.parse(String(init?.body))
    return new Response(JSON.stringify({detail: 'synthetic offline'}), {status: 400})
  }
  const {scope, vm} = setup(Settings)
  try {
    vm.fill({available: true, enabled: true, configured: true, app_key_set: true, base_url: 'https://dify.invalid/v1',
      tool_base_url: 'https://app.invalid/xunjia', revision: 'revision-a', tool_path: '/api/agent/tools/qa_market/query', hint: ''})
    assert.equal(vm.form.app_key, '')
    vm.form.app_key = 'synthetic-new-key'
    await vm.act(true)
    assert.equal(body.expected_revision, 'revision-a')
    assert.equal(body.app_key, 'synthetic-new-key')
    assert.equal(vm.form.app_key, '')
    assert.ok(vm.error.value)
  } finally { scope.stop() }
})
