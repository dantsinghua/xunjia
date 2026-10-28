import { test } from 'node:test'
import assert from 'node:assert/strict'
import { canResumeInquiry, isCurrentInquiry } from '../src/lib/monitorBonds.ts'
const pending={sessionId:'synthetic-member',tradeDay:'2026-10-27',stage:'待人工确认',groupState:'OPEN',groupSendAllowed:true,memberSendAllowed:false} as any

test('单对手待人工保持显式交回机器入口，不当作询价中',()=>{
  assert.equal(canResumeInquiry(pending,'2026-10-27'),true)
  assert.equal(isCurrentInquiry(pending,'2026-10-27'),false)
})

test('组级完成、暂停组、跨日或缺member均不能从单对手入口恢复',()=>{
  for(const change of [{groupState:'HANDOFF'},{groupState:'CLOSED'},{groupSendAllowed:false},{tradeDay:'2026-10-26'},{memberSendAllowed:true},{sessionId:undefined}])
    assert.equal(canResumeInquiry({...pending,...change},'2026-10-27'),false)
})
