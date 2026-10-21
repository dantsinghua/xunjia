/** Independent SSO contracts, with Node crypto as an external MD5 oracle. */
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { test } from 'node:test'
import { md5 } from '../src/lib/md5.ts'
import { consumeSsoLocation, createSsoProof } from '../src/lib/sso.ts'

for (const value of ['', 'abc', '独立验收甲', '交易员😀', 'x'.repeat(1000)]) {
  test(`MD5 UTF-8 oracle: ${value.slice(0, 12)}`, () => {
    assert.equal(md5(value), createHash('md5').update(value, 'utf8').digest('hex'))
  })
}

test('proof signs current epoch seconds and never returns the secret', () => {
  const secret = 'synthetic-secret-只在内存😀'
  const before = Math.floor(Date.now() / 1000)
  const proof = createSsoProof('qa-dev-a', '独立验收甲', secret)
  const after = Math.floor(Date.now() / 1000)
  assert.equal(proof.userName, 'qa-dev-a')
  assert.equal(proof.nickName, '独立验收甲')
  assert.match(proof.ts, /^\d+$/)
  assert.ok(Number(proof.ts) >= before && Number(proof.ts) <= after)
  assert.equal(proof.sign, createHash('md5').update(`qa-dev-a|独立验收甲|${proof.ts}|${secret}`).digest('hex'))
  assert.deepEqual(Object.keys(proof).sort(), ['nickName', 'sign', 'ts', 'userName'])
  assert.ok(!JSON.stringify(proof).includes(secret))
})

const identities = [
  { userName: 'qa-dev-a', nickName: '独立验收甲' },
  { user_name: 'qa-dev-a', nick_name: '独立验收甲' },
  { userId: 'qa-dev-a', userName: '独立验收甲' },
  { user_id: 'qa-dev-a', user_name: '独立验收甲' },
]
for (const identity of identities) {
  for (const timestamp of ['ts', 'timestamp']) {
    for (const signature of ['sign', 'md5', 'sig']) {
      for (const source of ['search', 'hash']) {
        test(`consume and scrub ${JSON.stringify(identity)} ${timestamp}/${signature} from ${source}`, () => {
          const query = new URLSearchParams({ ...identity, [timestamp]: '1800000000', [signature]: 'a'.repeat(32), keep: 'safe' })
          const location = { pathname: '/workbench', search: source === 'search' ? `?${query}` : '?outer=kept', hash: source === 'hash' ? `#/chat?${query}` : '#/chat?inner=kept' }
          const urls: string[] = []
          const result = consumeSsoLocation(location, { replaceState: (_state: unknown, _unused: string, url?: string | URL | null) => { urls.push(String(url)) } })
          assert.ok(result, 'The portal proof must be consumed')
          assert.equal(result.present, true)
          assert.deepEqual(result.proof, { userName: 'qa-dev-a', nickName: '独立验收甲', ts: '1800000000', sign: 'a'.repeat(32) })
          assert.equal(urls.length, 1)
          assert.ok(urls[0].startsWith('/workbench'))
          assert.ok(urls[0].includes('#/chat'))
          assert.ok(urls[0].includes('keep=safe'))
          assert.ok(urls[0].includes(source === 'search' ? 'inner=kept' : 'outer=kept'))
          for (const key of ['userName', 'nickName', 'user_name', 'nick_name', 'userId', 'user_id', 'ts', 'timestamp', 'sign', 'md5', 'sig']) {
            assert.ok(!new RegExp(`[?&]${key}=`).test(urls[0]), `Leaked ${key}`)
          }
        })
      }
    }
  }
}

test('partial portal parameters and accidental raw secrets are scrubbed from both URL scopes', () => {
  const urls: string[] = []
  const result = consumeSsoLocation({ pathname: '/', search: '?userName=bad&secret=synthetic-raw-secret&safe=yes', hash: '#/link?nick_name=bad&token=synthetic-old-token&sig=bad&keep=ok' },
    { replaceState: (_state: unknown, _unused: string, url?: string | URL | null) => { urls.push(String(url)) } })
  assert.equal(result.present, true)
  assert.equal(result.proof, null)
  assert.equal(urls.length, 1)
  assert.ok(urls[0].includes('safe=yes') && urls[0].includes('keep=ok'))
  for (const leaked of ['userName=', 'nick_name=', 'secret=', 'token=', 'sig=', 'synthetic-raw-secret', 'synthetic-old-token']) assert.ok(!urls[0].includes(leaked), leaked)
})

test('normal navigation is not treated as portal authentication', () => {
  const urls: string[] = []
  const result = consumeSsoLocation({ pathname: '/', search: '?keep=safe', hash: '#/chat' },
    { replaceState: (_state: unknown, _unused: string, url?: string | URL | null) => { urls.push(String(url)) } })
  assert.deepEqual(result, { present: false, proof: null })
  assert.deepEqual(urls, [])
})
