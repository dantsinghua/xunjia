import { test } from 'node:test'
import assert from 'node:assert/strict'
import { effectScope } from 'vue'
import Settings from '../../src/components/MqSettings.vue'

test('connection UI saves address without accepting or posting a credential document', async () => {
  const scope = effectScope(), calls: any[] = []
  globalThis.fetch = async (_url, init) => { calls.push(JSON.parse(String(init?.body))); return new Response(JSON.stringify({ ok: true, data: { configured: true, applied: true, credentials_set: true } })) }
  const vm: any = scope.run(() => (Settings as any).setup({}, { expose() {}, emit() {} }))
  try {
    vm.applyStatus({ base_url: 'https://synthetic.invalid', credentials_set: true, revision: 'synthetic-revision' })
    assert.equal('config_yaml' in vm.form, false)
    vm.form.base_url = 'https://updated.synthetic.invalid'
    assert.equal(vm.dirty.value, true)
    await vm.act('save')
    assert.equal(calls.length, 1)
    assert.equal('config_yaml' in calls[0], false)
    assert.equal(calls[0].base_url, 'https://updated.synthetic.invalid')
  } finally { scope.stop() }
})

test('absent server auth blocks save and probe without asking for a browser secret', async () => {
  const scope = effectScope()
  globalThis.fetch = async () => { throw new Error('unexpected request') }
  const vm: any = scope.run(() => (Settings as any).setup({}, { expose() {}, emit() {} }))
  try {
    vm.applyStatus({ base_url: 'https://synthetic.invalid', credentials_set: false })
    await vm.act('save'); await vm.act('test')
    assert.match(vm.error.value, /服务端.*认证/)
    assert.equal('config_yaml' in vm.form, false)
  } finally { scope.stop() }
})
