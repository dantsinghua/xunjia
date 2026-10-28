/** The same build supports the internal root URL and the shared /xunjia/ entry. */
export function appBase(pathname = globalThis.location?.pathname || '/'): string {
  return pathname === '/xunjia' || pathname.startsWith('/xunjia/') ? '/xunjia/' : '/'
}

/** Prefix application-owned root paths, including inside the quote worker. */
export function appPath(path: string, pathname?: string): string {
  const base = appBase(pathname)
  if (base === '/' || !path.startsWith('/') || path.startsWith('//')
      || path === base.slice(0, -1) || path.startsWith(base)) return path
  return base.slice(0, -1) + path
}
