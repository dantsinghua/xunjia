<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api, type SessionResetStatus } from '../api'
import { imBoundAccount } from '../lib/imLink'

const props = withDefaults(defineProps<{ offline?: boolean }>(), { offline: false })
const status = ref<SessionResetStatus | null>(null)
const armed = ref(false), loading = ref(false), submitting = ref(false), error = ref('')
const stale = ref(false)
const peerFilter = ref(''), visiblePeers = ref(50)
let generation = 0, readRequest = 0, disposed = false, requestId = ''
let timer: ReturnType<typeof setTimeout> | undefined
const pairKey = computed(() => {
  const b = imBoundAccount.value
  return b ? JSON.stringify([b.workbench_id, b.account_uid, b.version]) : ''
})
const pending = computed(() => !!status.value && !['complete', 'blocked'].includes(status.value.state))
const labels: Record<SessionResetStatus['state'], string> = {
  requested: '已提交，等待采集端建立新会话版本',
  waiting_native: '等待采集端结束在途操作并返回实际进度',
  local_applied: '工作台已应用，等待采集端最终确认',
  complete: '双端会话已清空并完成同步',
  blocked: '同步未完成，请查看原因后重试',
}
const peers = computed(() => Object.entries(status.value?.snapshot?.peers || {})
  .filter(([id]) => id.includes(peerFilter.value.trim())))
function clearTimer() {
  if (timer !== undefined) clearTimeout(timer)
  timer = undefined
}
function schedule() {
  clearTimer()
  if (!props.offline && !disposed && (stale.value || (status.value && status.value.state !== 'complete'))) timer = setTimeout(refresh, 1500)
}
function validateScope(value: SessionResetStatus | null) {
  const b = imBoundAccount.value
  if (value && (!b || value.scope.workbench_id !== b.workbench_id || value.scope.account_uid !== b.account_uid)) {
    throw Error('返回的同步状态不属于当前采集端和账号，请刷新后核对')
  }
}
async function refresh() {
  if (props.offline || !pairKey.value || submitting.value || disposed) return
  clearTimer()
  const current = generation, request = ++readRequest
  loading.value = true
  try {
    const result = await api.sessionReset()
    if (disposed || current !== generation || request !== readRequest) return
    if (!result.ok) throw Error(result.hint || '读取会话同步状态失败')
    validateScope(result.data)
    status.value = result.data
    stale.value = false
    error.value = ''
  } catch (e) {
    if (!disposed && current === generation && request === readRequest) {
      error.value = e instanceof Error ? e.message : '读取会话同步状态失败'
      stale.value = true
    }
  } finally {
    if (!disposed && current === generation && request === readRequest) {
      loading.value = false
      schedule()
    }
  }
}
function prepare() {
  if (props.offline || !imBoundAccount.value || pending.value || submitting.value) return
  armed.value = true
  error.value = ''
  if (!requestId) requestId = crypto.randomUUID()
}
async function confirm() {
  const b = imBoundAccount.value
  if (props.offline || !b || !armed.value || submitting.value || pending.value) return
  if (!Number.isInteger(b.version) || !b.version || b.version < 1) {
    error.value = '账号绑定版本尚未读取，请先刷新通道状态'
    return
  }
  const current = generation
  readRequest++
  clearTimer()
  submitting.value = true
  loading.value = false
  error.value = ''
  try {
    const result = await api.startSessionReset(requestId, b.version)
    if (disposed || current !== generation) return
    if (!result.ok) throw Error(result.hint || '提交会话清空请求失败')
    validateScope(result.data)
    status.value = result.data
    stale.value = false
    armed.value = false
    requestId = ''
  } catch (e) {
    if (!disposed && current === generation) {
      error.value = `${e instanceof Error ? e.message : '请求未完成'}；可刷新状态核对，重试将沿用同一请求。`
      stale.value = true
    }
  } finally {
    if (!disposed && current === generation) {
      submitting.value = false
      schedule()
    }
  }
}
watch(pairKey, () => {
  generation++; readRequest++; clearTimer()
  status.value = null; armed.value = false; error.value = ''; requestId = ''; stale.value = false
  loading.value = false; submitting.value = false; peerFilter.value = ''; visiblePeers.value = 50
  void refresh()
}, { immediate: true })
watch(() => props.offline, offline => { if (offline) { clearTimer(); armed.value = false } else void refresh() })
onBeforeUnmount(() => { disposed = true; generation++; readRequest++; clearTimer() })
</script>

<template>
  <section class="session-reset" data-testid="session-reset-settings">
    <fieldset :disabled="props.offline" style="border:0;padding:0;margin:0;min-width:0">
    <h3>会话清空与双端同步</h3>
    <p>结束当前选定采集端和企点号下的全部询价会话，与采集端同步实际进度后开始新会话。历史聊天、发送回执和去重记录保留。</p>
    <p v-if="imBoundAccount" data-testid="session-reset-scope">
      采集端：<b>{{ imBoundAccount.workbench_id }}</b> · 企点号：<b>{{ imBoundAccount.account_uid }}</b> · 范围：全部会话
    </p>
    <p v-else>请先选择采集端和企点号。</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <div v-if="status" role="status" data-testid="session-reset-status" class="status">
      <b>{{ stale ? '最新同步状态暂不可读，请等待刷新核对' : labels[status.state] || '同步状态待核对' }}</b>
      <span>会话版本：{{ status.account_epoch }}</span>
      <p v-if="status.code">{{ status.code }}</p>
      <p v-if="status.state === 'complete' && !stale">工作台和采集端已确认同一会话版本。可重新选择债券并确认新询价；旧询价不会自动重发。</p>
      <p v-if="status.snapshot?.unresolved_history?.length">
        保留 {{ status.snapshot.unresolved_history.length }} 条历史未决执行记录；不将未知结果改写为发送成功。
      </p>
      <details v-if="status.snapshot" data-testid="session-reset-progress">
        <summary>查看双端对齐进度（{{ Object.keys(status.snapshot.peers).length }} 个联系人）</summary>
        <p>采集端消息进度：{{ status.snapshot.published_source_highwater }} · 快照时间：{{ new Date(status.snapshot.snapshot_at * 1000).toLocaleString() }}</p>
        <input v-model="peerFilter" class="ny-input" aria-label="筛选同步联系人" placeholder="按联系人号码筛选" />
        <table>
          <thead><tr><th>联系人</th><th>已同步入站进度</th><th>会话屏障版本</th></tr></thead>
          <tbody><tr v-for="[peer, progress] in peers.slice(0, visiblePeers)" :key="peer">
            <td>{{ peer }}</td><td>{{ progress.source_watermark }}</td><td>{{ progress.receiver_fence_version }}</td>
          </tr></tbody>
        </table>
        <button v-if="visiblePeers < peers.length" type="button" class="btn-ghost" @click="visiblePeers += 50">显示更多联系人</button>
      </details>
    </div>
    <div v-if="armed" class="confirmation" data-testid="session-reset-confirmation">
      <p>确认清空上述企点号的全部会话状态？在途原生操作会先结束，双方确认完成前不会开始新一轮发送。</p>
      <button type="button" class="btn-primary" data-testid="session-reset-confirm" :disabled="submitting" @click="confirm">
        {{ submitting ? '正在提交…' : '确认清空并同步' }}
      </button>
      <button type="button" class="btn-ghost" :disabled="submitting" @click="armed = false">取消</button>
    </div>
    <div class="actions">
      <button type="button" class="btn-ghost" data-testid="session-reset-start"
        :disabled="!imBoundAccount || pending || submitting || armed" @click="prepare">清空并同步全部会话</button>
      <button type="button" class="btn-ghost" data-testid="session-reset-refresh"
        :disabled="!imBoundAccount || loading || submitting" @click="refresh">刷新同步状态</button>
    </div>
    </fieldset>
  </section>
</template>

<style scoped>
.session-reset{margin:24px 0;padding:20px;border:1px solid #e8e2dc;border-radius:10px}
h3{margin:0 0 12px}p{line-height:1.7;color:#70665e;overflow-wrap:anywhere}
.actions,.confirmation{display:flex;gap:12px;flex-wrap:wrap;margin-top:16px;align-items:center}
.confirmation{padding:14px;background:#faf4ed;border-radius:8px}.confirmation p{width:100%;margin:0}
button{padding:8px 14px;border-radius:8px;font:inherit;min-height:36px}
.status{padding:14px;background:#faf8f5;border-radius:8px}.status>span{margin-left:16px}
.error{color:#a62d2a}summary{cursor:pointer;margin-top:12px}input{max-width:320px;width:100%}
table{width:100%;margin-top:12px;border-collapse:collapse}th,td{padding:8px;text-align:left;border-bottom:1px solid #e8e2dc}
@media(max-width:760px){.session-reset{padding:14px}.status>span{display:block;margin:8px 0 0}}
</style>
