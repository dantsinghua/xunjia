import { test } from 'node:test'
import assert from 'node:assert/strict'
import { monitorTradeDay, isCurrentInquiry, canResumeInquiry } from '../src/lib/monitorBonds.ts'
const current = { tradeDay: '2026-10-22', stage: '询价中', groupState: 'OPEN', memberSendAllowed: true, groupSendAllowed: true } as any

test('监控日期固定按中国交易日，跨UTC日期不误删当日债券', () => {
  assert.equal(monitorTradeDay(new Date('2026-10-21T16:00:00Z')), '2026-10-22')
  assert.equal(monitorTradeDay(new Date('2026-10-22T15:59:59Z')), '2026-10-22')
  assert.equal(monitorTradeDay(new Date('2026-10-22T16:00:00Z')), '2026-10-23')
})
test('仅展示当天询价中，不把缺日期、历史、未来、暂停及终态当作在聊', () => {
  assert.equal(isCurrentInquiry(current, '2026-10-22'), true)
  for (const change of [{tradeDay:'2026-10-21'}, {tradeDay:'2026-10-23'}, {tradeDay:undefined},
    {stage:'已暂停'}, {stage:'待询价'}, {stage:'已放弃'}, {stage:'待回执'}, {stage:'待核对'},
    {stage:'待人工确认'}, {groupState:'HANDOFF'}, {groupSendAllowed:false}, {memberSendAllowed:false}]) {
    assert.equal(isCurrentInquiry({...current, ...change}, '2026-10-22'), false, JSON.stringify(change))
  }
  assert.equal(isCurrentInquiry({...current,sendAllowed:false}, '2026-10-22'), true, '全局暂停不改写单券状态')
})
test('恢复入口只引用今日暂停的原member，不复活过期轮次或整个券组', () => {
  const paused={...current,stage:'已暂停',memberSendAllowed:false,sessionId:'original-member'}
  assert.equal(canResumeInquiry(paused,'2026-10-22'),true)
  assert.equal(isCurrentInquiry(paused,'2026-10-22'),false)
  for (const change of [{tradeDay:'2026-10-21'},{groupState:'HANDOFF'},{groupSendAllowed:false},{sessionId:undefined}])
    assert.equal(canResumeInquiry({...paused,...change},'2026-10-22'),false)
})
test('筛选不修改原始历史对象或当前价量', () => {
  const raw=Object.freeze({...current,quotes:Object.freeze({ofr:{price:{value:'1.52'},volume_wan:'500'}})})
  const before=JSON.stringify(raw)
  assert.equal([raw].filter(b=>isCurrentInquiry(b,'2026-10-23')).length,0)
  assert.equal(JSON.stringify(raw),before)
})
