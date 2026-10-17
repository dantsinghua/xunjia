/** 开始询价的两行正文。与 imlink/rfq_assign.py 同一套空字段和换行规则。 */

export const RFQ_MERGED_LIMIT = 1500
export const RFQ_MERGED_LIMIT_HINT = '拼合后超过 1500 字，没有发送'

export interface RfqFields {
  price: string
  side: string
  tenor: string
  name: string
  placement: string
  bid: string
  ofr: string
  bid_vol: string
  ofr_vol: string
  coupon: string
  issuer_rating: string
  bond_rating: string
  cnbd: string
  balance: string
  maturity: string
  broker: string
  quoted_at: string
}

const BLANK = new Set(['', '--', '—', '–', '-'])

export function dash(value: unknown): string {
  const text = String(value ?? '').trim()
  if (BLANK.has(text)) return '--'
  return text
}

export function money2(value: unknown): string {
  const text = dash(value)
  if (text === '--') return text
  const number = Number(String(value ?? '').replace(/,/g, ''))
  if (!Number.isFinite(number)) return text
  return number.toFixed(2)
}

export function renderQuote(code: string, fields: Partial<RfqFields> | null | undefined): string {
  const src = fields || {}
  const line1 = `${money2(src.price)}*${dash(src.side)}`
  const parts = [
    dash(src.tenor),
    dash(code),
    dash(src.name),
    dash(src.placement),
    `${money2(src.bid)}/${money2(src.ofr)}`,
    `${dash(src.bid_vol)}/${dash(src.ofr_vol)}(*)`,
    `票面${dash(src.coupon)}`,
    `${dash(src.issuer_rating)}/${dash(src.bond_rating)}`,
    `中债${money2(src.cnbd)}`,
    `余额${dash(src.balance)}`,
    `到期日${dash(src.maturity)}`,
    dash(src.broker),
    dash(src.quoted_at),
  ]
  return `${line1}\n${parts.join('  ')}`
}

export interface RfqItem {
  code: string
  fields: Partial<RfqFields>
  uins: string[]
}

/** 同一 uin 只留一批，多只债的正文用换行接起来。uin 顺序按第一次出现。 */
export function mergeByUin(items: RfqItem[]): { uin: string; text: string; codes: string[] }[] {
  const order: string[] = []
  const texts = new Map<string, string[]>()
  const codes = new Map<string, string[]>()
  for (const item of items) {
    const body = renderQuote(item.code, item.fields)
    const seen = new Set<string>()
    for (const raw of item.uins || []) {
      const uin = String(raw ?? '').trim()
      if (!uin || seen.has(uin)) continue
      seen.add(uin)
      if (!texts.has(uin)) {
        order.push(uin)
        texts.set(uin, [])
        codes.set(uin, [])
      }
      texts.get(uin)!.push(body)
      const list = codes.get(uin)!
      if (!list.includes(item.code)) list.push(item.code)
    }
  }
  return order.map(uin => ({
    uin,
    text: (texts.get(uin) || []).join('\n'),
    codes: codes.get(uin) || [],
  }))
}

/** 按将要发出的原文拼合同一收件人。超过 1500 字返回错误文案，调用方不得发送。 */
export function mergedOverLimit(items: { code: string; text: string; uins: string[] }[]): string | null {
  const order: string[] = []
  const texts = new Map<string, string[]>()
  for (const item of items) {
    const body = String(item.text ?? '')
    const seen = new Set<string>()
    for (const raw of item.uins || []) {
      const uin = String(raw ?? '').trim()
      if (!uin || seen.has(uin)) continue
      seen.add(uin)
      if (!texts.has(uin)) {
        order.push(uin)
        texts.set(uin, [])
      }
      texts.get(uin)!.push(body)
    }
  }
  for (const uin of order) {
    const text = (texts.get(uin) || []).join('\n')
    if ([...text].length > RFQ_MERGED_LIMIT) return RFQ_MERGED_LIMIT_HINT
  }
  return null
}
/** Match the core's decimal add-BP-then-ROUND_HALF_UP contract without binary float rounding. */
export function yieldAfterBp(valuation:string,bp:string):string {
  const parse=(text:string)=>{
    if(!/^-?\d+(?:\.\d+)?$/.test(text))throw new Error('估值和BP须是十进制数字')
    const negative=text.startsWith('-'),[integer,fraction='']=text.replace(/^-/,'').split('.')
    return {value:BigInt(integer+fraction)*(negative?-1n:1n),scale:fraction.length}
  }
  const a=parse(valuation),b=parse(bp),scale=Math.max(a.scale,b.scale+2,2)
  const value=a.value*10n**BigInt(scale-a.scale)+b.value*10n**BigInt(scale-b.scale-2)
  const divisor=10n**BigInt(scale-2),absolute=value<0n?-value:value
  const cents=absolute/divisor+(absolute%divisor*2n>=divisor?1n:0n)
  return `${value<0n&&cents>0n?'-':''}${cents/100n}.${String(cents%100n).padStart(2,'0')}`
}
