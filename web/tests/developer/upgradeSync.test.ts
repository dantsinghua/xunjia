import { afterEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { effectScope } from 'vue'
import Settings from '../../src/components/UpgradeSyncSettings.vue'
import { api, type UpgradeSyncStatus } from '../../src/api'
import { HttpError, setAuthUserId } from '../../src/lib/http'

const originals = new Map(['fetch', 'localStorage', 'sessionStorage', 'location'].map(name =>
  [name, Object.getOwnPropertyDescriptor(globalThis, name)]))
afterEach(() => {
  for (const [name, descriptor] of originals) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor)
    else Reflect.deleteProperty(globalThis, name)
  }
  setAuthUserId('')
})
function storage() {
  const values = new Map<string, string>()
  return { getItem: (name: string) => values.get(name) ?? null,
    setItem: (name: string, value: string) => values.set(name, value),
    removeItem: (name: string) => values.delete(name) }
}
function setupGlobals(pathname = '/xunjia/link') {
  const local = storage(), session = storage()
  local.setItem('xj_auth_token', 'synthetic-upgrade-token')
  for (const [name, value] of Object.entries({ localStorage: local, sessionStorage: session, location: { pathname } }))
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true })
  setAuthUserId('synthetic-upgrade-owner')
  return session
}
const status = (overrides: Partial<UpgradeSyncStatus> = {}): UpgradeSyncStatus => ({
  enabled: true, can_upgrade: true, current_build: 'a'.repeat(64), retention_days: 30, job: null, ...overrides,
})
const response = (data: UpgradeSyncStatus) => new Response(JSON.stringify({ ok: true, data }))
const job = (state: 'queued' | 'unknown' | 'succeeded', id = 'new-job') => ({ id, state, message: state, created_at: 1, updated_at: 2 })
function component() {
  const scope = effectScope()
  const vm: any = scope.run(() => (Settings as any).setup({}, { expose() {} }))
  return { scope, vm }
}

test('upgrade GET and POST keep auth, empty request body and application subpath', async () => {
  setupGlobals()
  const calls: { url: string; init?: RequestInit }[] = []
  globalThis.fetch = async (url, init) => { calls.push({ url: String(url), init }); return response(status()) }
  await api.upgradeStatus(); await api.syncUpgrade()
  assert.deepEqual(calls.map(call => call.url), ['/xunjia/api/system/upgrade', '/xunjia/api/system/upgrade'])
  for (const call of calls) {
    assert.equal(new Headers(call.init?.headers).get('Authorization'), 'Bearer synthetic-upgrade-token')
    assert.equal(new Headers(call.init?.headers).get('X-Auth-Token'), 'synthetic-upgrade-token')
  }
  assert.equal(calls[1].init?.method, 'POST')
  assert.deepEqual(JSON.parse(String(calls[1].init?.body)), {})
})

test('upgrade requests forward AbortSignal and preserve HTTP permission rejection', async () => {
  setupGlobals('/')
  const controller = new AbortController()
  globalThis.fetch = async (url, init) => {
    assert.equal(String(url), '/api/system/upgrade')
    assert.equal(init?.signal, controller.signal)
    return new Response(JSON.stringify({ ok: false, hint: 'synthetic forbidden' }), { status: 403 })
  }
  await assert.rejects(api.syncUpgrade(controller.signal), (error: unknown) =>
    error instanceof HttpError && error.status === 403 && error.message === 'synthetic forbidden')
})

for (const disabled of [status({ enabled: false }), status({ can_upgrade: false }), status({ job: job('unknown') })]) {
  test(`disabled upgrade state never sends a POST: ${JSON.stringify(disabled)}`, async () => {
    setupGlobals(); let calls = 0
    globalThis.fetch = async () => { calls++; throw Error('Unexpected request') }
    const { vm, scope } = component()
    try {
      vm.applyStatus(disabled)
      assert.equal(vm.canStart.value, false)
      await vm.synchronize()
      assert.equal(calls, 0)
    } finally { scope.stop() }
  })
}

test('two synchronous submit attempts consume one POST and keep queued action disabled', async () => {
  setupGlobals(); let writes = 0
  let complete!: (value: Response) => void
  const pending = new Promise<Response>(resolve => { complete = resolve })
  globalThis.fetch = async (_url, init) => {
    if (init?.method === 'POST') { writes++; return pending }
    return response(status({ job: job('queued') }))
  }
  const { vm, scope } = component()
  try {
    vm.applyStatus(status())
    const first = vm.synchronize()
    await vm.synchronize()
    assert.equal(writes, 1)
    assert.equal(vm.canStart.value, false)
    complete(response(status({ job: job('queued') })))
    await first
    assert.equal(writes, 1)
    assert.equal(vm.active.value, true)
  } finally { scope.stop() }
})

test('lost submission response persists uncertainty until a different durable job is observed', async () => {
  const session = setupGlobals(); let writes = 0
  globalThis.fetch = async (_url, init) => {
    if (init?.method === 'POST') { writes++; throw new TypeError('synthetic connection lost') }
    return response(status({ job: job('succeeded', 'prior-job') }))
  }
  const { vm, scope } = component()
  try {
    vm.applyStatus(status({ job: job('succeeded', 'prior-job') }))
    await vm.synchronize()
    assert.equal(session.getItem('xj_upgrade_pending:synthetic-upgrade-owner'), 'prior-job')
    assert.equal(vm.unknown.value, true)
    await vm.synchronize()
    assert.equal(writes, 1)
    vm.applyStatus(status({ job: job('queued') }))
    assert.equal(session.getItem('xj_upgrade_pending:synthetic-upgrade-owner'), null)
    assert.equal(vm.unknown.value, false)
    assert.equal(vm.canStart.value, false)
  } finally { scope.stop() }
})

test('definite permission rejection clears local uncertainty without automatic resubmission', async () => {
  const session = setupGlobals(); let writes = 0
  globalThis.fetch = async (_url, init) => {
    if (init?.method === 'POST') {
      writes++
      return new Response(JSON.stringify({ ok: false, hint: 'synthetic rejected' }), { status: 403 })
    }
    return response(status())
  }
  const { vm, scope } = component()
  try {
    vm.applyStatus(status()); await vm.synchronize()
    assert.equal(writes, 1)
    assert.equal(vm.submitError.value, 'synthetic rejected')
    assert.equal(session.getItem('xj_upgrade_pending:synthetic-upgrade-owner'), null)
    assert.equal(vm.unknown.value, false)
  } finally { scope.stop() }
})
