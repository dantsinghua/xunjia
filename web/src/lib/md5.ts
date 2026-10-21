/**
 * 最小 MD5（UTF-8 字符串 → 小写 hex），供门户 SSO 签名联调。
 * 算法实现为公共域参考实现的精简移植，无第三方依赖。
 */
function utf8Bytes(str: string): number[] {
  const out: number[] = []
  for (let i = 0; i < str.length; i++) {
    let c = str.charCodeAt(i)
    if (c < 0x80) out.push(c)
    else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f))
    else if (c < 0xd800 || c >= 0xe000) {
      out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f))
    } else {
      i++
      c = 0x10000 + (((c & 0x3ff) << 10) | (str.charCodeAt(i) & 0x3ff))
      out.push(
        0xf0 | (c >> 18),
        0x80 | ((c >> 12) & 0x3f),
        0x80 | ((c >> 6) & 0x3f),
        0x80 | (c & 0x3f),
      )
    }
  }
  return out
}

function cmn(q: number, a: number, b: number, x: number, s: number, t: number) {
  a = (a + q + x + t) | 0
  return (((a << s) | (a >>> (32 - s))) + b) | 0
}
function ff(a: number, b: number, c: number, d: number, x: number, s: number, t: number) {
  return cmn((b & c) | (~b & d), a, b, x, s, t)
}
function gg(a: number, b: number, c: number, d: number, x: number, s: number, t: number) {
  return cmn((b & d) | (c & ~d), a, b, x, s, t)
}
function hh(a: number, b: number, c: number, d: number, x: number, s: number, t: number) {
  return cmn(b ^ c ^ d, a, b, x, s, t)
}
function ii(a: number, b: number, c: number, d: number, x: number, s: number, t: number) {
  return cmn(c ^ (b | ~d), a, b, x, s, t)
}

export function md5(str: string): string {
  const bytes = utf8Bytes(str)
  const n = bytes.length
  const words: number[] = []
  for (let i = 0; i < n; i++) words[i >> 2] |= bytes[i] << ((i % 4) * 8)
  words[n >> 2] |= 0x80 << ((n % 4) * 8)
  const bits = n * 8
  words[(((bits + 64) >>> 9) << 4) + 14] = bits

  let a0 = 1732584193
  let b0 = -271733879
  let c0 = -1732584194
  let d0 = 271733878

  for (let i = 0; i < words.length; i += 16) {
    let a = a0
    let b = b0
    let c = c0
    let d = d0
    const w = (j: number) => words[i + j] | 0

    a = ff(a, b, c, d, w(0), 7, -680876936)
    d = ff(d, a, b, c, w(1), 12, -389564586)
    c = ff(c, d, a, b, w(2), 17, 606105819)
    b = ff(b, c, d, a, w(3), 22, -1044525330)
    a = ff(a, b, c, d, w(4), 7, -176418897)
    d = ff(d, a, b, c, w(5), 12, 1200080426)
    c = ff(c, d, a, b, w(6), 17, -1473231341)
    b = ff(b, c, d, a, w(7), 22, -45705983)
    a = ff(a, b, c, d, w(8), 7, 1770035416)
    d = ff(d, a, b, c, w(9), 12, -1958414417)
    c = ff(c, d, a, b, w(10), 17, -42063)
    b = ff(b, c, d, a, w(11), 22, -1990404162)
    a = ff(a, b, c, d, w(12), 7, 1804603682)
    d = ff(d, a, b, c, w(13), 12, -40341101)
    c = ff(c, d, a, b, w(14), 17, -1502002290)
    b = ff(b, c, d, a, w(15), 22, 1236535329)

    a = gg(a, b, c, d, w(1), 5, -165796510)
    d = gg(d, a, b, c, w(6), 9, -1069501632)
    c = gg(c, d, a, b, w(11), 14, 643717713)
    b = gg(b, c, d, a, w(0), 20, -373897302)
    a = gg(a, b, c, d, w(5), 5, -701558691)
    d = gg(d, a, b, c, w(10), 9, 38016083)
    c = gg(c, d, a, b, w(15), 14, -660478335)
    b = gg(b, c, d, a, w(4), 20, -405537848)
    a = gg(a, b, c, d, w(9), 5, 568446438)
    d = gg(d, a, b, c, w(14), 9, -1019803690)
    c = gg(c, d, a, b, w(3), 14, -187363961)
    b = gg(b, c, d, a, w(8), 20, 1163531501)
    a = gg(a, b, c, d, w(13), 5, -1444681467)
    d = gg(d, a, b, c, w(2), 9, -51403784)
    c = gg(c, d, a, b, w(7), 14, 1735328473)
    b = gg(b, c, d, a, w(12), 20, -1926607734)

    a = hh(a, b, c, d, w(5), 4, -378558)
    d = hh(d, a, b, c, w(8), 11, -2022574463)
    c = hh(c, d, a, b, w(11), 16, 1839030562)
    b = hh(b, c, d, a, w(14), 23, -35309556)
    a = hh(a, b, c, d, w(1), 4, -1530992060)
    d = hh(d, a, b, c, w(4), 11, 1272893353)
    c = hh(c, d, a, b, w(7), 16, -155497632)
    b = hh(b, c, d, a, w(10), 23, -1094730640)
    a = hh(a, b, c, d, w(13), 4, 681279174)
    d = hh(d, a, b, c, w(0), 11, -358537222)
    c = hh(c, d, a, b, w(3), 16, -722521979)
    b = hh(b, c, d, a, w(6), 23, 76029189)
    a = hh(a, b, c, d, w(9), 4, -640364487)
    d = hh(d, a, b, c, w(12), 11, -421815835)
    c = hh(c, d, a, b, w(15), 16, 530742520)
    b = hh(b, c, d, a, w(2), 23, -995338651)

    a = ii(a, b, c, d, w(0), 6, -198630844)
    d = ii(d, a, b, c, w(7), 10, 1126891415)
    c = ii(c, d, a, b, w(14), 15, -1416354905)
    b = ii(b, c, d, a, w(5), 21, -57434055)
    a = ii(a, b, c, d, w(12), 6, 1700485571)
    d = ii(d, a, b, c, w(3), 10, -1894986606)
    c = ii(c, d, a, b, w(10), 15, -1051523)
    b = ii(b, c, d, a, w(1), 21, -2054922799)
    a = ii(a, b, c, d, w(8), 6, 1873313359)
    d = ii(d, a, b, c, w(15), 10, -30611744)
    c = ii(c, d, a, b, w(6), 15, -1560198380)
    b = ii(b, c, d, a, w(13), 21, 1309151649)
    a = ii(a, b, c, d, w(4), 6, -145523070)
    d = ii(d, a, b, c, w(11), 10, -1120210379)
    c = ii(c, d, a, b, w(2), 15, 718787259)
    b = ii(b, c, d, a, w(9), 21, -343485551)

    a0 = (a0 + a) | 0
    b0 = (b0 + b) | 0
    c0 = (c0 + c) | 0
    d0 = (d0 + d) | 0
  }

  const hex = (n: number) => {
    let s = ''
    for (let j = 0; j < 4; j++) s += ((n >> (j * 8)) & 0xff).toString(16).padStart(2, '0')
    return s
  }
  return hex(a0) + hex(b0) + hex(c0) + hex(d0)
}

/** 与后端 auth.sso_sign 一致：md5(userName|nickName|ts|secret) */
export function ssoSign(userName: string, nickName: string, ts: string, secret: string): string {
  return md5(`${userName}|${nickName}|${ts}|${secret}`)
}
