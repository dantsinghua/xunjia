import type { CpBond } from './types'

/** The desk uses the current China business date, independent of the browser timezone. */
const dayFormatter = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
})
export function monitorTradeDay(now: Date = new Date()): string {
  const parts = dayFormatter.formatToParts(now)
  const part = (name: string) => parts.find(value => value.type === name)?.value || ''
  return `${part('year')}-${part('month')}-${part('day')}`
}

/** Presentation only: raw sessions and historical message associations are retained. */
export function isCurrentInquiry(bond: CpBond, day: string): boolean {
  return bond.tradeDay === day && bond.stage === '询价中'
    && (!bond.groupState || bond.groupState === 'OPEN')
    && bond.groupSendAllowed !== false && bond.memberSendAllowed !== false
}

/** Today's paused or handed-off members keep an explicit recovery action. */
export function canResumeInquiry(bond: CpBond, day: string): boolean {
  return bond.tradeDay === day && ['已暂停', '待人工确认'].includes(bond.stage)
    && bond.groupState === 'OPEN' && bond.groupSendAllowed !== false
    && bond.memberSendAllowed === false && !!bond.sessionId
}
