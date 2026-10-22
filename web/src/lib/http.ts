import { channelContext } from './channelContext'
/** 询价应用独立登录态；所有业务 HTTP 共用认证与错误语义。 */
let currentUserId = ''
export function authUserId(): string { return currentUserId }
export function setAuthUserId(value: string): void { currentUserId = value }
export const AUTH_TOKEN_KEY = 'xj_auth_token'
export function authToken(): string { return typeof localStorage === 'undefined' ? '' : localStorage.getItem(AUTH_TOKEN_KEY) || '' }
export function setAuthToken(token: string): void { if (token) localStorage.setItem(AUTH_TOKEN_KEY, token); else localStorage.removeItem(AUTH_TOKEN_KEY) }
export class HttpError extends Error {
  readonly status: number
  readonly data: Record<string, unknown>
  readonly code?: string
  readonly hint?: string
  readonly submitted?: boolean
  readonly field?: string
  readonly bond_code?: string
  constructor(status: number, body: unknown) {
    const data = body && typeof body === 'object' && !Array.isArray(body) ? body as Record<string, unknown> : {}
    super(typeof data.hint === 'string' ? data.hint : typeof data.detail === 'string' ? data.detail : `HTTP ${status}`)
    this.name = 'HttpError'
    this.status = status
    this.data = data
    this.code = typeof data.code === 'string' ? data.code : undefined
    this.hint = typeof data.hint === 'string' ? data.hint : undefined
    this.submitted = typeof data.submitted === 'boolean' ? data.submitted : undefined
    this.field = typeof data.field === 'string' ? data.field : undefined
    this.bond_code = typeof data.bond_code === 'string' ? data.bond_code : undefined
  }
}
export function isSubmitRejected(value: unknown): value is { submitted: false; hint?: string } {
  return !!value && typeof value === 'object' && 'submitted' in value && value.submitted === false
}
export async function authFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers)
  const token = authToken()
  const context = channelContext()
  if (context) headers.set('X-Xunjia-Context', context)
  if (token) { headers.set('Authorization', `Bearer ${token}`); headers.set('X-Auth-Token', token) }
  const r = await fetch(url, { ...init, headers })
  if (token !== authToken()) throw new Error('登录身份已变化，请重新读取')
  if (!r.ok) {
    if (r.status === 401 && typeof window !== 'undefined') {
      setAuthToken(''); setAuthUserId(''); window.dispatchEvent(new Event('xj-auth-expired'))
    }
    const data = await r.clone().json().catch(() => ({}))
    throw new HttpError(r.status, data)
  }
  return r
}
export async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  return (await authFetch(url, init)).json()
}
