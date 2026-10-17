<script setup lang="ts">
/** 壳:ConfigProvider(南银主题) → a-app → 玻璃拟态顶栏(logo + 三屏导航 + 安琳在线徽标 + 时钟) → router-view。
 *  还原设计稿 现券询价机器人.dc.html 的 header。 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { App as AntApp, ConfigProvider } from 'ant-design-vue'
import zhCN from 'ant-design-vue/es/locale/zh_CN'
import { useRoute } from 'vue-router'
import { antdTheme } from './theme'
import { clearImState, setImBinding } from './lib/imLink'
import { authToken, requestJson, setAuthToken, setAuthUserId } from './lib/http'

const route = useRoute()
const authReady = ref(false)
const authBusy = ref(false)
const authError = ref('')
const identity = ref<{user_id:string;display_name:string;account_uid:string|null;binding:unknown}|null>(null)
async function authenticate() {
  if (authBusy.value) return
  authBusy.value = true; authReady.value = false; authError.value = ''
  try {
    const query = new URLSearchParams(location.search)
    const hashQuery = new URLSearchParams(location.hash.split('?')[1] || '')
    const fields = ['userName','nickName','ts','sign']
    const sso = Object.fromEntries(fields.map(k => [k, query.get(k) ?? hashQuery.get(k) ?? '']))
    for (const k of [...fields,'user_id','userId']) { query.delete(k); hashQuery.delete(k) }
    history.replaceState(null,'',location.pathname + (query.size ? '?' + query : '') + location.hash.split('?')[0] + (hashQuery.size ? '?' + hashQuery : ''))
    if (sso.sign) {
      clearImState(); setAuthToken('')
      const res = await requestJson<{ok:boolean;token?:string;hint?:string}>('/api/auth/sso', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sso)})
      if (!res.ok || !res.token) throw new Error(res.hint || '单点登录失败')
      setAuthToken(res.token)
    }
    if (!authToken()) throw new Error('请从已授权的门户进入询价工作台')
    const me = await requestJson<{ok:boolean;hint?:string;data:NonNullable<typeof identity.value>}>('/api/auth/me')
    if (!me.ok || !me.data?.user_id) throw new Error(me.hint || '身份验证失败')
    identity.value = me.data; setImBinding(me.data.binding, me.data.account_uid); setAuthUserId(me.data.user_id); authReady.value = true
  } catch (e) { identity.value = null; authError.value = e instanceof Error ? e.message : '登录失败' }
  finally { authBusy.value = false }
}
function expired() { clearImState(); setAuthUserId(''); authReady.value = false; identity.value = null; authError.value = '登录已失效，请从门户重新进入' }

function storageChanged(event: StorageEvent) { if (event.key === 'xj_auth_token') { expired(); void authenticate() } }

const now = ref('')
let timer: number | undefined

const NAVS = [
  { label: '意向询价', path: '/quote' },
  { label: '机器人监控', path: '/monitor' },
  { label: '智能问答', path: '/chat' },
  { label: '采集通道', path: '/link' },
]
const activePath = computed(() => route.path)

function tick() {
  const d = new Date(), p = (n: number) => String(n).padStart(2, '0')
  now.value = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
onMounted(() => { tick(); timer = window.setInterval(tick, 1000); window.addEventListener('xj-auth-expired', expired); window.addEventListener('storage', storageChanged); void authenticate() })
onBeforeUnmount(() => { if (timer) clearInterval(timer); window.removeEventListener('xj-auth-expired', expired); window.removeEventListener('storage', storageChanged) })
</script>

<template>
  <ConfigProvider :locale="zhCN" :theme="antdTheme">
    <AntApp>
      <div class="shell">
        <!-- 背景动画光斑 -->
        <div class="bg-blobs">
          <div class="blob a" />
          <div class="blob b" />
          <div class="blob c" />
        </div>

        <header class="mast">
          <div class="brand">
            <img src="/logo.png" alt="南银理财" class="logo" />
            <span class="divider" />
            <span class="title">询价工作台</span>
          </div>
          <nav class="nav">
            <router-link
              v-for="nv in NAVS" :key="nv.path" :to="nv.path"
              class="nav-btn" :class="{ active: activePath === nv.path }">
              {{ nv.label }}
            </router-link>
          </nav>
          <div class="right">
            <span class="online">
              <span class="dot" />
              <b>{{ identity?.display_name || '未登录' }}</b>
            </span>
            <div class="meta">
              <div class="sub">现券交易台 · 内部系统</div>
              <div class="clock mono">{{ now }}</div>
            </div>
          </div>
        </header>

        <main class="pane">
          <router-view v-if="authReady" />
        <main v-else class="glass" style="margin:40px auto;padding:32px;max-width:560px" role="status">
          <h2>询价工作台登录</h2>
          <p>{{ authBusy ? '正在验证身份…' : authError }}</p>
          <button class="btn-primary" :disabled="authBusy" @click="authenticate">重新验证</button>
        </main>
        <p v-if="authReady && !identity?.account_uid" role="alert" style="text-align:center">当前本人账号尚未绑定企点，发送功能不可用。</p>
        </main>
      </div>
    </AntApp>
  </ConfigProvider>
</template>

<style scoped>
.shell { min-height: 100vh; position: relative; overflow: clip; }
/* ⚠️ 光斑动画已停用(2026-09-19)。这三个 blob 在 .board/.dock 的 backdrop-filter
   身后,只要它们动,整张表格区域的 blur 就要逐帧重算 —— 空闲时也压着渲染进程不睡。
   静止后 Chromium 能缓存 backdrop,玻璃质感不变。要恢复动画请先去掉 .board 的
   backdrop-filter,二者不能并存。 */
.bg-blobs { position: fixed; inset: 0; pointer-events: none; z-index: 0; }
.blob { position: absolute; border-radius: 50%; }
.blob.a { top: -120px; right: -80px; width: 520px; height: 520px; background: radial-gradient(circle, rgba(176, 28, 36,.14), transparent 65%); filter: blur(40px); /* 动画停用:见下方注释 */ }
.blob.b { bottom: -160px; left: -100px; width: 620px; height: 620px; background: radial-gradient(circle, rgba(198,141,88,.13), transparent 65%); filter: blur(48px);  }
.blob.c { top: 40%; left: 35%; width: 380px; height: 380px; background: radial-gradient(circle, rgba(176, 28, 36,.07), transparent 60%); filter: blur(36px);  }

.mast {
  position: sticky; top: 0; z-index: 50; height: 66px; box-sizing: border-box;
  display: flex; align-items: center; gap: 20px; flex-wrap: nowrap; white-space: nowrap;
  padding: 0 28px; background: rgba(250,248,244,.62);
  backdrop-filter: blur(18px) saturate(1.4); -webkit-backdrop-filter: blur(18px) saturate(1.4);
  border-bottom: 1px solid rgba(176, 28, 36,.14); box-shadow: 0 4px 24px rgba(43,35,32,.06);
}
.brand { display: flex; align-items: center; gap: 18px; flex: none; }
.logo { height: 42px; width: auto; display: block; }
.divider { width: 1px; height: 34px; background: rgba(176, 28, 36,.18); }
.title { font-weight: 700; font-size: 20px; letter-spacing: 1px; color: #2B2320; }

.nav { display: flex; gap: 6px; margin: 0 auto; flex: none; }
.nav-btn {
  cursor: pointer; border: none; font-size: 14.5px; font-weight: 500; letter-spacing: 1px;
  padding: 9px 22px; border-radius: 999px; white-space: nowrap; transition: all .25s;
  background: transparent; color: #5C5148;
}
.nav-btn:hover { transform: translateY(-1px); color: #B01C24; }
.nav-btn.active {
  color: #fff; background: linear-gradient(135deg, #C2333B, #8E1620);
  box-shadow: 0 0 16px rgba(176, 28, 36,.5), 0 4px 12px rgba(138,40,35,.3);
  /* glowPulse 已停用:box-shadow 动画不可合成,60fps 样式重算烧一整天,换静态光晕 */
}

.right { display: flex; align-items: center; gap: 12px; flex: none; font-size: 12.5px; color: #6E6258; }
.online {
  display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; border-radius: 999px;
  background: rgba(255,255,255,.55); border: 1px solid rgba(176, 28, 36,.14); backdrop-filter: blur(8px);
}
.online .dot { width: 7px; height: 7px; border-radius: 50%; background: #3DA65E; box-shadow: 0 0 8px rgba(61,166,94,.8); }  /* dotPulse 同理停用 */
.online b { color: #2B2320; font-weight: 600; }
.meta { text-align: right; line-height: 1.35; }
.meta .sub { font-size: 10.5px; color: #A89C90; }
.meta .clock { font-variant-numeric: tabular-nums; font-weight: 500; }

.pane { position: relative; z-index: 1; }
</style>
