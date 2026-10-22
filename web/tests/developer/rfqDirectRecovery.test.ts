import { test } from 'node:test'
import assert from 'node:assert/strict'
import { effectScope, reactive } from 'vue'
import Rfq from '../../src/components/RfqAssignModal.vue'
import { authFetch } from '../../src/lib/http'
import { imLinks, imContacts } from '../../src/lib/imLink'
import { buildBonds } from './fixtures'

Object.defineProperty(globalThis, 'window', { value: { innerWidth: 1440 }, configurable: true })
Object.defineProperty(globalThis, 'document', { value: { documentElement: { clientWidth: 1440 }, body: { style: { setProperty() {}, removeProperty() {} }, classList: { add() {}, remove() {} } } }, configurable: true })
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })
const accepted = { ok: true, data: { batch_id: 'recovery-batch', commands: [{ command_id: 'recovery-command', peer_id: 'peer1', status: 'queued' }] } }
const rejected = { ok: false, submitted: false, code: 'SUBMIT_UNAVAILABLE', hint: '合成通道尚未运行', field: 'channel', bond_code: 'SYN-A' }

function setup() {
  globalThis.fetch = async () => response({ ok: true, data: {} })
  imLinks.value = [{ id: 'recovery-link', active: true, tested: true, account_uid: 'recovery-account', workbench_id: 'recovery-wb', version: 1 }] as any
  imContacts.value = [{ uin: 'peer1', name: '合成对手', remark: '' }]
  const scope = effectScope(), events: any[] = []
  const props = reactive({ open: true, bonds: buildBonds().slice(0, 1), floors: {}, savedFloors: {} })
  const vm: any = scope.run(() => (Rfq as any).setup(props, { expose() {}, emit: (...args: any[]) => events.push(args) }))
  vm.drafts.value = [{ ...vm.seed(props.bonds[0]), uins: ['peer1'], our_side: 'BUY', volume_wan: '1000', opening_yield_pct: '1.1', limit_yield_pct: '2', step_bp: '2' }]
  return { vm, scope, events, props }
}

test('HTTP rejection retains status and machine-readable submission metadata', async () => {
  globalThis.fetch = async () => response(rejected, 409)
  await assert.rejects(authFetch('/synthetic'), (error: any) => {
    assert.equal(error.status, 409)
    assert.equal(error.submitted, false)
    assert.equal(error.code, rejected.code)
    assert.equal(error.field, rejected.field)
    assert.equal(error.bond_code, rejected.bond_code)
    assert.equal(error.message, rejected.hint)
    return true
  })
})

for (const status of [200, 409]) test(`explicit submitted:false at HTTP ${status} unlocks unchanged prices and a new deliberate request`, async () => {
  const { vm, scope } = setup(), calls: any[] = []
  globalThis.fetch = async (_url, init) => { calls.push(JSON.parse(String(init?.body))); return calls.length === 1 ? response(rejected, status) : response(accepted) }
  try {
    await vm.sendDirect()
    assert.equal(vm.editingLocked.value, false)
    assert.equal(vm.submission.value, null)
    assert.equal(vm.drafts.value[0].opening_yield_pct, '1.1')
    assert.match(vm.formError.value, /合成通道尚未运行/)
    assert.doesNotMatch(vm.formError.value, /提交结果未确认/)
    vm.drafts.value[0].volume_wan = '2000'
    await vm.sendDirect()
    assert.notEqual(calls[0].request_id, calls[1].request_id)
    assert.equal(calls[1].items[0].volume_wan, '2000')
  } finally { scope.stop() }
})

for (const failure of ['network', 'unclassified-http', 'string-false']) test(`${failure} preserves unknown outcome and original request`, async () => {
  const { vm, scope, events } = setup(), calls: any[] = []
  globalThis.fetch = async (_url, init) => {
    calls.push(JSON.parse(String(init?.body)))
    if (calls.length > 1) return response(accepted)
    if (failure === 'network') throw new TypeError('synthetic lost response')
    return response(failure === 'string-false' ? { ...rejected, submitted: 'false' } : { ok: false, hint: 'synthetic unknown' }, 503)
  }
  try {
    await vm.sendDirect()
    assert.equal(vm.editingLocked.value, true)
    assert.match(vm.formError.value, /提交结果未确认/)
    await vm.sendDirect()
    assert.deepEqual(calls[0], calls[1])
    assert.equal(events.some(event => event[0] === 'sent'), false)
  } finally { scope.stop() }
})

test('explicit confirmation rejection releases preview for correction without changing trader prices', async () => {
  const { vm, scope } = setup()
  globalThis.fetch = async url => response(String(url).endsWith('/preview') ? { ok: true, data: { batch_id: 'preview', version: 1, status: 'PREVIEW', items: [], commands_preview: [{ peer_id: 'peer1', text: 'synthetic initial' }] } } : rejected, String(url).endsWith('/preview') ? 200 : 409)
  try {
    await vm.requestPreview(); assert.ok(vm.preview.value)
    await vm.confirm()
    assert.equal(vm.editingLocked.value, false)
    assert.equal(vm.preview.value, null)
    assert.equal(vm.drafts.value[0].opening_yield_pct, '1.1')
  } finally { scope.stop() }
})

test('unknown request retries frozen payload even when current draft reference is no longer valid', async () => {
  const { vm, scope, props } = setup(), calls: any[] = []
  const code = vm.drafts.value[0].code
  props.savedFloors = { [code]: { kind: 'bp', value: 5 } }
  vm.drafts.value[0].pricing_basis = 'bp'
  globalThis.fetch = async (_url, init) => {
    calls.push(JSON.parse(String(init?.body)))
    if (calls.length === 1) throw new TypeError('synthetic unknown')
    return response(accepted)
  }
  try {
    await vm.sendDirect(); assert.equal(calls.length, 1)
    props.savedFloors = {}
    assert.notEqual(vm.validationError(), '')
    await vm.sendDirect()
    assert.deepEqual(calls[0], calls[1])
  } finally { scope.stop() }
})

test('late rejection from an old binding cannot apply to the new form or reuse its request', async () => {
  const { vm, scope } = setup()
  const complete: ((value: Response) => void)[] = [], calls: any[] = []
  globalThis.fetch = async (_url, init) => { calls.push(JSON.parse(String(init?.body))); return new Promise(resolve => complete.push(resolve)) }
  try {
    const old = vm.sendDirect()
    imLinks.value = [{ id: 'new-link', active: true, tested: true, account_uid: 'new-account', workbench_id: 'new-wb', version: 2 }] as any
    complete[0](response(rejected, 409)); await old
    assert.equal(vm.formError.value, '')
    assert.equal(vm.submission.value, null)
    vm.drafts.value[0].uins = ['peer1']
    const current = vm.sendDirect()
    assert.equal(vm.editingLocked.value, true)
    complete[1](response(accepted)); await current
    assert.equal(calls.length, 2)
    assert.notEqual(calls[0].request_id, calls[1].request_id)
    assert.equal(vm.submission.value.batch_id, 'recovery-batch')
  } finally { scope.stop() }
})
