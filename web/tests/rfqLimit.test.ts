/** 拼合超过 1500 字不得发送。测试句本身可以发。 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { RFQ_MERGED_LIMIT, RFQ_MERGED_LIMIT_HINT, mergedOverLimit } from '../src/lib/rfqAssign.ts'

const PROBE = 'xunjia-agent发送功能与询价功能测试，请勿回复'

test('指定测试句不超过 1500 字', () => {
  assert.ok(PROBE.length < RFQ_MERGED_LIMIT)
  assert.equal(mergedOverLimit([{ code: '240202.IB', text: PROBE, uins: ['415011447'] }]), null)
})

test('同一收件人拼合超过 1500 字就报错', () => {
  const items = [0, 1].map(i => ({ code: '24020' + i + '.IB', text: '债'.repeat(800), uins: ['415011447'] }))
  assert.equal(mergedOverLimit(items), RFQ_MERGED_LIMIT_HINT)
  assert.ok((items[0].text + '\n' + items[1].text).length > RFQ_MERGED_LIMIT)
})
