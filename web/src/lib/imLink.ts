/** 当前采集端。意向询价和机器人监控读同一份：测试通过的那一条。 */
import { computed, ref } from 'vue'
import { authFetch } from './http'

export interface ImAccount {
  id: string
  label: string
  self_uid: string
  state: string
  account_nick?: string
}
export interface ImPerson { uin: string; name: string; remark: string }
export interface OrgDepartment { id: string; parent_id: string; name: string; order: number }
export interface OrgMember {
  uin: string
  name: string
  account: string
  department_id: string
  order: number
}
export interface ImOrg { departments: OrgDepartment[]; members: OrgMember[] }
export interface OrgTreeNode {
  id: string
  name: string
  order: number
  children: OrgTreeNode[]
  members: OrgMember[]
}
export interface ImLink {
  id: string
  name: string
  mode: 'local' | 'lan' | 'public' | 'oss' | 'mail'
  mode_label: string
  account_id: string
  workbench_id?: string
  account_uid?: string
  account_nick?: string
  catalog_stale?: boolean
  endpoint: string
  callback_url: string
  secret_set: boolean
  tested: boolean
  tested_at: string | null
  test_note: string
  account: ImAccount | null
  contacts: ImPerson[]
  contacts_source: string
  active: boolean
  kicked_by: string
  displaced: string[]
  form: Record<string, Record<string, unknown>>
}

export const imLinks = ref<ImLink[]>([])
export const imHints = ref<Record<string, string>>({})
export const imContacts = ref<ImPerson[]>([])
export const imOrg = ref<ImOrg>({ departments: [], members: [] })
export const imTarget = ref<ImPerson | null>(null)

function orderOf(value: unknown): number {
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}

function sortByOrder<T extends { order: number }>(rows: T[]): T[] {
  return rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => a.row.order - b.row.order || a.index - b.index)
    .map(item => item.row)
}

/** 把平铺的部门和成员收成树。同一个人在多个部门会有多行，各挂各的。空部门号进「未分组」。 */
export function buildOrgTree(org: ImOrg | null | undefined): OrgTreeNode[] {
  const departments = Array.isArray(org?.departments) ? org.departments : []
  const members = Array.isArray(org?.members) ? org.members : []
  const byId = new Map<string, OrgTreeNode>()
  for (const item of departments) {
    const id = String(item?.id || '').trim()
    if (!id || byId.has(id)) continue
    byId.set(id, {
      id,
      name: String(item.name || '').trim() || id,
      order: orderOf(item.order),
      children: [],
      members: [],
    })
  }
  const parentOf = new Map<string, string>()
  for (const item of departments) {
    const id = String(item?.id || '').trim()
    if (!byId.has(id) || parentOf.has(id)) continue
    const pid = String(item.parent_id || '').trim()
    if (!pid || pid === id || !byId.has(pid)) continue
    let cursor = pid
    const seen = new Set<string>([id])
    let cycle = false
    while (cursor) {
      if (seen.has(cursor)) { cycle = true; break }
      seen.add(cursor)
      cursor = parentOf.get(cursor) || ''
    }
    if (!cycle) parentOf.set(id, pid)
  }
  const roots: OrgTreeNode[] = []
  for (const [id, node] of byId) {
    const pid = parentOf.get(id)
    if (pid) byId.get(pid)?.children.push(node)
    else roots.push(node)
  }
  for (const node of byId.values()) node.children = sortByOrder(node.children)
  const ungrouped: OrgMember[] = []
  for (const item of members) {
    const uin = String(item?.uin || '').trim()
    if (!uin) continue
    const row: OrgMember = {
      uin,
      name: String(item.name || '').trim() || uin,
      account: String(item.account || ''),
      department_id: String(item.department_id || '').trim(),
      order: orderOf(item.order),
    }
    const dept = row.department_id ? byId.get(row.department_id) : undefined
    if (dept) dept.members.push(row)
    else ungrouped.push(row)
  }
  for (const node of byId.values()) node.members = sortByOrder(node.members)
  const tree = sortByOrder(roots)
  if (ungrouped.length) {
    tree.push({
      id: 'ungrouped',
      name: '未分组',
      order: Number.POSITIVE_INFINITY,
      children: [],
      members: sortByOrder(ungrouped),
    })
  }
  return tree
}

export interface VisibleOrgNode {
  id: string
  name: string
  opened: boolean
  childCount: number
  memberCount: number
  members: OrgMember[]
  children: VisibleOrgNode[]
}

export function visibleOrgBranch(nodes: OrgTreeNode[], openIds: string[]): VisibleOrgNode[] {
  const open = new Set(openIds)
  const map = (node: OrgTreeNode): VisibleOrgNode => {
    const opened = open.has(node.id)
    return {
      id: node.id,
      name: node.name,
      opened,
      childCount: node.children.length,
      memberCount: node.members.length,
      members: opened ? node.members : [],
      children: opened ? node.children.map(map) : [],
    }
  }
  return (nodes || []).map(map)
}

export const orgTree = computed(() => buildOrgTree(imOrg.value))
export const orgEmpty = computed(() => imOrg.value.departments.length === 0 && imOrg.value.members.length === 0)

function asOrg(raw: unknown): ImOrg {
  const obj = raw && typeof raw === 'object' ? raw as ImOrg : { departments: [], members: [] }
  return {
    departments: Array.isArray(obj.departments) ? obj.departments : [],
    members: Array.isArray(obj.members) ? obj.members : [],
  }
}

function applyLinkBook(cur: ImLink | null | undefined) {
  imContacts.value = cur?.contacts || []
  imContactSource.value = cur?.contacts_source || ''
  imOrg.value = { departments: [], members: [] }
  imTarget.value = null
}

export function selectImTarget(person: ImPerson): void {
  imTarget.value = person
}
export const imContactSource = ref('')
export const imFeedError = ref('')
export const imHealthError = ref('')
export function clearImState() {
  linkGeneration++; imBinding.value = null; imLinks.value = []; imHints.value = {}; applyLinkBook(null); imFeedError.value = ''; imHealthError.value = ''
}
export interface BoundAccount { account_uid: string; workbench_id: string; id?: string; version?: number; account_nick?: string }
export const imBinding = ref<BoundAccount | null>(null)
export const imBoundAccount = computed<BoundAccount | null>(() => {
  const link = imLinks.value.find(x => x.active && (x.account_uid || x.account?.self_uid))
  return link ? { id:link.id, account_uid:link.account_uid || link.account?.self_uid || '', workbench_id:link.workbench_id || '', account_nick:link.account_nick, version:(link as ImLink & {version?:number}).version } : imBinding.value
})
export function setImBinding(raw: unknown, accountUid: string | null): void {
  const b = raw && typeof raw === 'object' ? raw as Record<string,unknown> : {}
  imBinding.value = accountUid ? { account_uid:accountUid, workbench_id:String(b.workbench_id || ''), version:typeof b.version === 'number' ? b.version : undefined } : null
}
export const imCurrent = computed(() => imLinks.value.find(x => x.active && x.tested) || null)
export const imReady = computed(() => imLinks.value.filter(x => x.tested))

/** 展示用登录昵称和企点号。不使用内部账号 id，也不使用可编辑备注。 */
export function imIdentity(link: ImLink | null | undefined): { nick: string; uid: string } {
  if (!link) return { nick: '', uid: '' }
  const nick = link.account_nick || link.account?.account_nick || ''
  const uid = link.account_uid || link.account?.self_uid || ''
  return { nick, uid }
}

export function imIdentityText(link: ImLink | null | undefined): string {
  const { nick, uid } = imIdentity(link)
  return [nick, uid].filter(Boolean).join(' · ')
}

export function orgEmptyLabel(org: ImOrg | null | undefined): string {
  return !org?.departments?.length && !org?.members?.length ? '这个账号还没有组织架构' : ''
}
export function orgReadNotice(org: ImOrg | null | undefined): string {
  return orgEmptyLabel(org) || (org?.departments?.length ? `已读取组织架构，${org.departments.length} 个部门` : '已读取组织架构')
}

let linkGeneration = 0
export async function loadImLinks() {
  const request = ++linkGeneration
  const previous = `${imCurrent.value?.id}:${imIdentity(imCurrent.value).uid}:${imCurrent.value?.workbench_id}`
  const r = await authFetch('/api/im/links')
  const data = await r.json()
  if (!r.ok || data.ok === false) throw new Error(data.hint || '读通道失败')
  if (request !== linkGeneration) return
  imLinks.value = data.data.links || []
  imHealthError.value = ''
  imHints.value = data.hints || {}
  if (previous !== `${imCurrent.value?.id}:${imIdentity(imCurrent.value).uid}:${imCurrent.value?.workbench_id}`) applyLinkBook(imCurrent.value)
}

export async function activateIm(id: string) {
  const request = ++linkGeneration
  const r = await authFetch('/api/im/links/' + id + '/activate', { method: 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({expected_version:(imLinks.value.find(l=>l.id===id) as ImLink & {version?:number} | undefined)?.version}) })
  const data = await r.json()
  if (!r.ok || data.ok === false) throw new Error(data.hint || '切换失败')
  if (request !== linkGeneration) return
  imLinks.value = data.data.links || []
  applyLinkBook(imLinks.value.find(x => x.active && x.tested))
}

export async function refreshImContacts(refresh = false) {
  const r = await authFetch('/api/im/contacts' + (refresh ? '?refresh=1' : ''))
  const data = await r.json()
  if (!r.ok || data.ok === false) throw new Error(data.hint || '读通讯录失败')
  imContacts.value = data.data.contacts || []
  imContactSource.value = data.data.source || ''
  imOrg.value = asOrg(data.data.org)
  return data.data as { source: string; contacts: ImPerson[]; org: ImOrg; name: string; account_id: string }
}
