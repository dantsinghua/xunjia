/** 行状态收敛层 —— 照 DM 的 getUniquePushData。
 *
 *  为什么需要它:改造前「批量 prepend」没有 upsert 语义,同一只券的报价更新会变成
 *  一条新行插到顶上,列表越滚越长且同券重复。DM 的做法是每条报价带服务端单调
 *  `flowId`,以 `bondUniCode` 为行身份做 last-write-wins,并给行设过期。
 *
 *  三条规则(与 DM 一致):
 *   1. 行身份 = bondUniCode,行版本 = flowId
 *   2. 新版本 >= 旧版本才覆盖 —— 容忍乱序到达与重复投递
 *   3. flowId 相等时,REST 拉来的权威数据(无 pushLocalTime)胜过推送数据
 */
import type { BondStatic, QuoteDelta } from './quoteTypes'

export const EXPIRE_MS = 240_000 // 照 DM:240s 没再更新的推送行淘汰

/** 合并后的完整行 = 静态主数据 + 动态报价。静态部分从 worker 缓存 join 进来。 */
export interface QuoteRow extends Partial<BondStatic> {
  u: string // bondUniCode
  f: number // flowId,行版本
  t: number // issueTime
  pushLocalTime?: number // 仅推送来的行有;REST 行没有 —— 用它区分权威性
  bb?: number; bbv?: number; bbs?: string
  bo?: number; bov?: number; bos?: string
  fm?: string; cl?: string; st?: string
  ib?: number; ibv?: number; io?: number; iov?: number
}

/** 哪些字段变了 —— 用来决定闪哪一格,而不是整行重渲染 */
export type ChangedFields = Set<keyof QuoteRow>

export interface UpsertResult {
  /** 本批真正产生变化的行(没变的不在内,避免无谓渲染) */
  changed: Map<string, ChangedFields>
  added: number
  updated: number
  /** 因版本更旧被丢弃的条数;持续 >0 说明服务端乱序严重或时钟有问题 */
  stale: number
}

const WATCH: (keyof QuoteRow)[] = ['bb', 'bbv', 'bbs', 'bo', 'bov', 'bos', 'fm', 'cl', 'st', 'ib', 'ibv', 'io', 'iov']

export class RowStore {
  private map = new Map<string, QuoteRow>()
  /** 插入顺序即展示顺序:新券追加到队首。Map 本身保序,但要支持"置顶"所以另存序列 */
  private order: string[] = []

  get size() { return this.map.size }
  get(u: string) { return this.map.get(u) }

  /** 当前快照,按展示顺序。调用方拿到后不要改 —— 行对象是冻结的。 */
  snapshot(): QuoteRow[] {
    const out: QuoteRow[] = new Array(this.order.length)
    for (let i = 0; i < this.order.length; i++) out[i] = this.map.get(this.order[i])!
    return out
  }

  /** REST 首屏/翻页:权威数据,无 pushLocalTime */
  seed(rows: QuoteRow[], append = false) {
    const codes: string[] = []
    for (const r of rows) {
      const frozen = Object.freeze(r)
      if (!this.map.has(r.u)) codes.push(r.u)
      this.map.set(r.u, frozen)
    }
    this.order = append ? this.order.concat(codes) : codes.concat(this.order.filter(u => !codes.includes(u)))
  }

  /** 推送增量。返回变化明细,渲染层据此只更新/闪烁受影响的行。 */
  upsert(deltas: QuoteDelta[], staticOf?: (u: string) => Partial<BondStatic> | undefined): UpsertResult {
    const res: UpsertResult = { changed: new Map(), added: 0, updated: 0, stale: 0 }
    const now = Date.now()

    for (const d of deltas) {
      const cur = this.map.get(d.u)

      if (cur && d.f < cur.f) { res.stale++; continue } // 乱序旧版本,丢弃
      // flowId 相等:REST 权威数据(无 pushLocalTime)胜,照 DM 的 m(e,t)
      if (cur && d.f === cur.f && cur.pushLocalTime == null) { res.stale++; continue }

      if (!cur) {
        const row = Object.freeze({ ...(staticOf?.(d.u) ?? {}), ...d, pushLocalTime: now }) as QuoteRow
        this.map.set(d.u, row)
        this.order.unshift(d.u) // 新券进列表头部
        res.added++
        res.changed.set(d.u, new Set(WATCH))
        continue
      }

      const diff: ChangedFields = new Set()
      for (const k of WATCH) {
        if (d[k as keyof QuoteDelta] !== undefined && d[k as keyof QuoteDelta] !== cur[k]) diff.add(k)
      }
      // 版本前进但字段没变:更新版本号即可,不触发闪烁
      const merged = Object.freeze({ ...cur, ...d, pushLocalTime: now }) as QuoteRow
      this.map.set(d.u, merged)
      if (diff.size) { res.updated++; res.changed.set(d.u, diff) }
    }
    return res
  }

  /** 淘汰过期推送行。REST 行(无 pushLocalTime)永不淘汰 —— 它们是翻页拿到的历史。 */
  evict(now = Date.now()): number {
    let n = 0
    for (const [u, r] of this.map) {
      if (r.pushLocalTime != null && now - r.pushLocalTime > EXPIRE_MS) { this.map.delete(u); n++ }
    }
    if (n) this.order = this.order.filter(u => this.map.has(u))
    return n
  }

  /** 内存窗口上限:超出从尾部(最旧)丢,它们仍能由服务端游标翻回来 */
  trim(max: number): number {
    if (this.order.length <= max) return 0
    const drop = this.order.splice(max)
    for (const u of drop) this.map.delete(u)
    return drop.length
  }

  clear() { this.map.clear(); this.order = [] }
}
