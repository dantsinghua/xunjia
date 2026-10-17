/** 询价应用独立登录态；所有业务 HTTP 共用认证与错误语义。 */
let currentUserId = ''
export function authUserId(): string { return currentUserId }
export function setAuthUserId(value: string): void { currentUserId = value }
export const AUTH_TOKEN_KEY = 'xj_auth_token'
export function authToken(): string { return typeof localStorage === 'undefined' ? '' : localStorage.getItem(AUTH_TOKEN_KEY) || '' }
export function setAuthToken(token: string): void { if (token) localStorage.setItem(AUTH_TOKEN_KEY, token); else localStorage.removeItem(AUTH_TOKEN_KEY) }
export async function authFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers)
  const token = authToken()
  if (token) { headers.set('Authorization', `Bearer ${token}`); headers.set('X-Auth-Token', token) }
  const r = await fetch(url, { ...init, headers })
  if (token !== authToken()) throw new Error('登录身份已变化，请重新读取')
  if (!r.ok) {
    if (r.status === 401 && typeof window !== 'undefined') {
      setAuthToken(''); setAuthUserId(''); window.dispatchEvent(new Event('xj-auth-expired'))
    }
    const data = await r.clone().json().catch(() => ({}))
    throw new Error(data.hint || data.detail || `HTTP ${r.status}`)
  }
  return r
}
export async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  return (await authFetch(url, init)).json()
}
