/** Portal/dev SSO uses the same ibquote wire contract; raw secrets never leave this module. */
import { ssoSign } from "./md5.ts"

export interface SsoProof { userName: string; nickName: string; ts: string; sign: string }
const KEYS = ["userId", "user_id", "userName", "user_name", "nickName", "nick_name", "ts", "timestamp", "sign", "md5", "sig", "token", "secret"]

export function createSsoProof(userName: string, nickName: string, secret: string): SsoProof {
  const uid = userName.trim(), name = nickName.trim()
  if (!uid || !name || uid.length > 160 || name.length > 160) throw new Error("请填写有效的 userName 和 nickName（最多160字）")
  if (!secret || secret.length > 4096) throw new Error("请填写有效的约定密钥")
  const ts = String(Math.floor(Date.now() / 1000))
  return { userName: uid, nickName: name, ts, sign: ssoSign(uid, name, ts, secret) }
}

export function consumeSsoLocation(loc: Pick<Location, "pathname" | "search" | "hash"> = location,
  hist: Pick<History, "replaceState"> = history): { present: boolean; proof: SsoProof | null } {
  const query = new URLSearchParams(loc.search)
  const split = loc.hash.indexOf("?")
  const hashPath = split < 0 ? loc.hash : loc.hash.slice(0, split)
  const hashQuery = new URLSearchParams(split < 0 ? "" : loc.hash.slice(split + 1))
  const present = KEYS.some(k => query.has(k) || hashQuery.has(k))
  if (!present) return { present: false, proof: null }
  const read = (...keys: string[]) => {
    for (const k of keys) { const v = query.get(k) ?? hashQuery.get(k); if (v) return v.trim() }
    return ""
  }
  const legacy = read("userId", "user_id"), user = read("userName", "user_name")
  const userName = legacy || user
  const nickName = read("nickName", "nick_name") || (legacy ? user : "")
  const ts = read("ts", "timestamp"), sign = read("sign", "md5", "sig", "token")
  for (const k of KEYS) { query.delete(k); hashQuery.delete(k) }
  hist.replaceState(null, "", loc.pathname + (query.size ? "?" + query.toString() : "") + hashPath + (hashQuery.size ? "?" + hashQuery.toString() : ""))
  return { present, proof: userName && nickName && ts && sign ? { userName, nickName, ts, sign } : null }
}
