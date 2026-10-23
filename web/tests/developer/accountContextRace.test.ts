import { test } from 'node:test'
import assert from 'node:assert/strict'
import { imLinks, imContacts, imBoundAccount, clearImState, setImBinding, loadImLinks, applyCollectorSelection } from '../../src/lib/imLink'
import { channelContext } from '../../src/lib/channelContext'
import { authFetch } from '../../src/lib/http'

const a = { account_uid: 'synthetic-a', workbench_id: 'synthetic-wb', version: 1, context_id: 'synthetic-context-a' }
const b = { ...a, version: 2, context_id: 'synthetic-context-b' }
const link = (binding: typeof a) => ({ ...binding, id: 'synthetic-link', active: true, tested: true, contacts: [{ uin: 'synthetic-peer', name: 'Synthetic' }] }) as any
const response = (binding: typeof a) => new Response(JSON.stringify({ ok: true, data: { links: [link(binding)] } }))

test('refreshed identity invalidates an older active link and its in-flight response', async () => {
  clearImState(); setImBinding(a, a.account_uid); imLinks.value = [link(a)]
  imContacts.value = link(a).contacts
  let complete!: (r: Response) => void
  globalThis.fetch = async () => new Promise(resolve => { complete = resolve })
  const stale = loadImLinks()
  setImBinding(b, b.account_uid)
  assert.equal(channelContext(), b.context_id)
  assert.deepEqual(imContacts.value, [])
  complete(response(a)); await stale
  assert.equal(imBoundAccount.value?.context_id, b.context_id)
  let context = ''
  globalThis.fetch = async (_url, init) => { context = new Headers(init?.headers).get('X-Xunjia-Context') || ''; return new Response('{}') }
  await authFetch('/api/im/rfq/send-direct', { method: 'POST' })
  assert.equal(context, b.context_id)
})

test('identity with no account cannot keep a previous active account', () => {
  clearImState(); setImBinding(a, a.account_uid); imLinks.value = [link(a)]
  setImBinding(null, null)
  assert.equal(imBoundAccount.value, null)
  assert.equal(channelContext(), '')
})

test('unchanged identity refresh preserves contacts and current context', () => {
  clearImState(); setImBinding(a, a.account_uid); imLinks.value = [link(a)]
  imContacts.value = link(a).contacts
  setImBinding(a, a.account_uid)
  assert.equal(channelContext(), a.context_id)
  assert.equal(imContacts.value.length, 1)
})

test('explicit selection already fences late link reloads', async () => {
  clearImState(); setImBinding(a, a.account_uid)
  let complete!: (r: Response) => void
  globalThis.fetch = async () => new Promise(resolve => { complete = resolve })
  const stale = loadImLinks()
  applyCollectorSelection(b, { contacts: [], source: 'synthetic', org: { departments: [], members: [] } })
  complete(response(a)); await stale
  assert.equal(channelContext(), b.context_id)
  clearImState()
})
