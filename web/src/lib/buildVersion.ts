/** Detect later deployments without reloading or persisting an unfinished form. */
export function startBuildVersionWatch(currentVersion: string): () => void {
  let stopped = false, checking = false
  let banner: HTMLElement | null = null
  let controller: AbortController | null = null

  function showUpdate() {
    if (banner || stopped) return
    banner = document.createElement('section')
    banner.setAttribute('role', 'status')
    banner.setAttribute('aria-live', 'polite')
    banner.setAttribute('aria-label', '工作台版本更新')
    banner.dataset.testid = 'app-update-banner'
    banner.style.cssText = 'position:fixed;top:80px;right:16px;z-index:150;width:min(600px,calc(100vw - 32px));box-sizing:border-box;padding:16px;display:flex;align-items:center;flex-wrap:wrap;gap:12px;border:1px solid #b01c24;border-radius:12px;background:#fffaf5;color:#2b2320;box-shadow:0 8px 28px #2b232033;font:14px/1.6 system-ui,sans-serif'
    const text = document.createElement('div')
    text.style.cssText = 'flex:1;min-width:200px;overflow-wrap:anywhere'
    const title = document.createElement('strong')
    title.textContent = '工作台已发布新版本，当前页面尚未更新。'
    const note = document.createElement('p')
    note.id = 'app-update-note'
    note.textContent = '请先保留未提交内容，再手动更新。刷新会清除未提交输入，不会自动保存。'
    note.style.cssText = 'margin:4px 0 0;font-size:13px'
    text.append(title, note)
    const button = document.createElement('button')
    button.type = 'button'
    button.textContent = '更新页面'
    button.dataset.testid = 'app-update-reload'
    button.setAttribute('aria-describedby', note.id)
    button.style.cssText = 'padding:8px 16px;min-height:40px;border:0;border-radius:8px;background:#b01c24;color:white;font:inherit;cursor:pointer;flex-shrink:0'
    button.addEventListener('click', () => {
      const yaml = document.querySelector<HTMLTextAreaElement>('textarea[name="mq-config-yaml"]')
      if (yaml && yaml.value.length > 0) {
        note.textContent = '文件中转 config.yaml 仍有未保存内容，已阻止刷新。请先保存或自行保留草稿，再清空输入后更新。'
        note.setAttribute('role', 'alert')
        yaml.focus()
        return
      }
      window.location.reload()
    })
    banner.append(text, button)
    document.body.append(banner)
  }

  async function check() {
    if (stopped || checking || document.visibilityState === 'hidden') return
    checking = true
    controller = new AbortController()
    const timeout = window.setTimeout(() => controller?.abort(), 6000)
    try {
      const url = new URL('assets/build-version.json', document.baseURI)
      url.searchParams.set('_check', String(Date.now()))
      const response = await fetch(url, { cache: 'no-store', credentials: 'same-origin', signal: controller.signal })
      if (!response.ok) return
      const data: unknown = await response.json()
      const version = data && typeof data === 'object' && 'version' in data ? data.version : null
      if (typeof version !== 'string' || !/^[a-zA-Z0-9._-]{1,128}$/.test(version)) return
      if (!stopped && version !== currentVersion) showUpdate()
    } catch {
      // An unavailable manifest is not evidence of a newer deployment.
    } finally {
      window.clearTimeout(timeout)
      controller = null
      checking = false
    }
  }

  const onVisible = () => { if (document.visibilityState === 'visible') void check() }
  const onFocus = () => { void check() }
  const interval = window.setInterval(() => void check(), 30000)
  document.addEventListener('visibilitychange', onVisible)
  window.addEventListener('focus', onFocus)
  void check()
  return () => {
    stopped = true
    window.clearInterval(interval)
    document.removeEventListener('visibilitychange', onVisible)
    window.removeEventListener('focus', onFocus)
    controller?.abort()
    banner?.remove()
  }
}
