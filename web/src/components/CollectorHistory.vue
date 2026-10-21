<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api, type NativeHistoryPage, type NativeHistoryMedia } from '../api'
import { authFetch } from '../lib/http'
import { imBoundAccount } from '../lib/imLink'
const props = defineProps<{ online: boolean }>()
const page = ref<NativeHistoryPage | null>(null), error = ref(''), busy = ref(false), peer = ref('')
const mediaBusy = ref(''), previews = ref<Record<string, string>>({})
const pair = computed(() => JSON.stringify([imBoundAccount.value?.workbench_id, imBoundAccount.value?.account_uid, imBoundAccount.value?.version]))
let generation = 0, disposed = false
const requests = new Set<AbortController>()
function cleanMedia() { for (const u of Object.values(previews.value)) URL.revokeObjectURL(u); previews.value = {}; for (const c of requests) c.abort(); requests.clear(); mediaBusy.value = '' }
function when(v: number) { return new Date(v * 1000).toLocaleString() }
async function load(more = false) {
  if (!props.online || busy.value || !imBoundAccount.value || (more && !page.value?.next_cursor)) return
  const b = { ...imBoundAccount.value }, current = ++generation, scope = pair.value
  busy.value = true; error.value = ''
  try {
    const r = await api.imHistory(more ? page.value?.next_cursor || undefined : undefined, peer.value.trim() || undefined)
    if (disposed || current !== generation || scope !== pair.value) return
    if (!r.ok || !r.data) throw Error(r.hint || '读取主库历史失败')
    const data = r.data
    if (data.workbench_id !== b.workbench_id || data.account_uid !== b.account_uid || data.binding_version !== b.version || !Array.isArray(data.items)) throw Error('历史消息与当前所选企点号不一致')
    page.value = more && page.value ? { ...data, items: [...page.value.items, ...data.items] } : data
  } catch (e) { if (current === generation && !disposed) error.value = e instanceof Error ? e.message : '读取历史失败' }
  finally { if (current === generation) busy.value = false }
}
async function media(item: NativeHistoryMedia, preview: boolean) {
  if (!props.online || !item.download_ready || !item.sha256 || mediaBusy.value) return
  const scope = pair.value, current = generation, controller = new AbortController()
  requests.add(controller); mediaBusy.value = item.sha256; error.value = ''
  try {
    const r = await authFetch('/api/im/media/' + encodeURIComponent(item.sha256), { signal: controller.signal })
    const data = await r.arrayBuffer()
    if (disposed || scope !== pair.value || current !== generation || !props.online) return
    if (data.byteLength !== item.size || data.byteLength > 50 * 1024 * 1024) throw Error('附件字节数量与来源记录不一致')
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', data))).map(v => v.toString(16).padStart(2, '0')).join('')
    if (hash !== item.sha256) throw Error('附件内容校验失败')
    if (disposed || scope !== pair.value || current !== generation || !props.online) return
    const safeImage = ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp'].includes(item.mime)
    const url = URL.createObjectURL(new Blob([data], { type: safeImage ? item.mime : 'application/octet-stream' }))
    if (preview && safeImage) {
      if (previews.value[item.sha256]) URL.revokeObjectURL(previews.value[item.sha256])
      if (Object.keys(previews.value).length >= 5) { const key = Object.keys(previews.value)[0]!; URL.revokeObjectURL(previews.value[key]!); delete previews.value[key] }
      previews.value = { ...previews.value, [item.sha256]: url }
    } else {
      const a = document.createElement('a'); a.href = url; a.download = item.name || item.sha256; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1500)
    }
  } catch (e) { if (!disposed && scope === pair.value && current === generation && !controller.signal.aborted) error.value = e instanceof Error ? e.message : '附件读取失败' }
  finally { requests.delete(controller); if (scope === pair.value) mediaBusy.value = '' }
}
watch(pair, () => { generation++; cleanMedia(); page.value = null; error.value = ''; busy.value = false; peer.value = ''; void load() }, { immediate: true })
watch(() => props.online, value => { if (value && !page.value) void load(); if (!value) { generation++; busy.value = false; cleanMedia() } })
onBeforeUnmount(() => { disposed = true; generation++; cleanMedia() })
</script>
<template>
  <section class="native-history" data-testid="native-history">
    <h3>最近7天企点主库历史</h3>
    <p>直接读取采集端企点主库后同步；按原消息ID去重，历史补传不会触发机器人回复。群聊只展示，不自动回复。</p>
    <p v-if="!online" class="warning" role="status">已离线，历史与附件操作已停用；请在采集端恢复登录。</p>
    <div class="actions">
      <input v-model="peer" aria-label="历史对手账号" placeholder="对手账号；群聊使用 g_群号" :disabled="!online || busy" />
      <button type="button" class="btn-ghost" :disabled="!online || busy" @click="load()">刷新主库历史</button>
    </div>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <div v-if="page?.items.length" class="messages">
      <article v-for="row in page.items" :key="row.id" class="message" :data-native-id="row.native_id">
        <header>{{ when(row.occurred_at) }} · {{ row.direction === 'in' ? '收到' : '发出' }} · {{ row.peer_id }} · {{ row.session_kind === 'group' ? '群聊（只读）' : '私聊' }}</header>
        <p class="text">{{ row.text }}</p>
        <div v-for="(item, index) in row.media" :key="item.sha256 || String(index)" class="media">
          <span>{{ item.name || (row.type === 'image' ? '图片' : '文件') }} · {{ item.download_ready ? '文件已就绪' : item.state === 'unavailable' ? '原生文件尚不可读' : '等待完整文件' }}</span>
          <button v-if="item.mime.startsWith('image/')" type="button" :disabled="!online || !item.download_ready || !!mediaBusy" @click="media(item, true)">查看实际图片</button>
          <button type="button" :disabled="!online || !item.download_ready || !!mediaBusy" @click="media(item, false)">下载原文件</button>
          <img v-if="item.sha256 && previews[item.sha256]" :src="previews[item.sha256]" :alt="item.name" @error="error = '图片文件解码失败；未使用占位图替代'" />
        </div>
      </article>
    </div>
    <p v-else-if="page && !busy">最近7天暂无已接收的主库历史。</p>
    <button v-if="page?.has_more" type="button" class="btn-ghost" :disabled="!online || busy" @click="load(true)">加载更早历史</button>
    <p v-if="busy" role="status">正在读取历史…</p>
  </section>
</template>
<style scoped>
.native-history{margin:20px 0;padding:18px;border:1px solid #e8e2dc;border-radius:10px;min-width:0}h3{margin:0 0 12px}p{line-height:1.7;color:#70665e;overflow-wrap:anywhere}.error{color:#a02e2e}.warning{color:#885600}.actions,.media{display:flex;gap:10px;align-items:center;flex-wrap:wrap}input{padding:8px;min-height:36px;min-width:220px;max-width:100%;box-sizing:border-box;border:1px solid #d9d0c8;border-radius:7px}.messages{max-height:65vh;overflow:auto;margin:14px 0}.message{padding:14px 0;border-bottom:1px solid #e8e2dc}.message header{font-size:13px;color:#70665e;overflow-wrap:anywhere}.text{white-space:pre-wrap;color:#2b2320}.media{margin:8px 0}.media img{display:block;max-width:100%;max-height:480px;object-fit:contain}.media span{overflow-wrap:anywhere}button{font:inherit;padding:7px 12px;min-height:36px;border-radius:7px;cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}
</style>