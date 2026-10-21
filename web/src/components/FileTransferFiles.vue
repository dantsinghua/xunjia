<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { api, type TransferFilePage } from '../api'
import { imBoundAccount } from '../lib/imLink'

const props = withDefaults(defineProps<{ offline?: boolean }>(), { offline: false })
const page = ref<TransferFilePage | null>(null), error = ref(''), busy = ref(false)
const pair = computed(() => JSON.stringify([imBoundAccount.value?.workbench_id, imBoundAccount.value?.account_uid, imBoundAccount.value?.version]))
let generation = 0
const kinds: Record<string, string> = { catalog: '采集端目录', contacts: '通讯录', event: '消息事件', receipt: '执行回执', command: '指令', cancel: '取消指令', query: '查询指令', manifest: '文件索引', session_reset: '会话清空与同步', session_reset_ack: '会话同步确认' }
const states: Record<string, string> = { STORED: '已保全', DELIVERED: '已移交处理', UPLOADED: '已上传', UPLOADING: '上传处理中', UNKNOWN: '结果未知，待对账', FOREIGN: '归属待核对' }
function when(value: number | null) {
  return value != null && Number.isFinite(value) ? new Date(value * 1000).toLocaleString() : '未记录'
}
async function load(more = false) {
  if (props.offline || busy.value || !imBoundAccount.value || (more && !page.value?.next_before)) return
  const request = ++generation, scope = pair.value, binding = { ...imBoundAccount.value }
  busy.value = true; error.value = ''
  try {
    const response = await api.imFiles(more ? page.value?.next_before || undefined : undefined)
    if (request !== generation || scope !== pair.value) return
    if (!response.ok || !response.data) throw Error(response.hint || '读取文件记录失败')
    const result = response.data
    if (result.workbench_id !== binding.workbench_id || result.account_uid !== binding.account_uid
        || result.scope !== 'retained_authenticated_files' || !Array.isArray(result.items)) {
      throw Error('文件记录与当前采集端账号不符，已忽略该响应')
    }
    page.value = more && page.value ? { ...result, items: [...page.value.items, ...result.items] } : result
  } catch (e) {
    if (request === generation) error.value = e instanceof Error ? e.message : '读取文件记录失败'
  } finally { if (request === generation) busy.value = false }
}
watch(pair, () => { generation++; page.value = null; busy.value = false; error.value = ''; void load() }, { immediate: true })
watch(() => props.offline, offline => { if (offline) { generation++; busy.value = false } else void load() })
onBeforeUnmount(() => { generation++ })
</script>

<template>
  <section class="file-history" data-testid="transfer-files" aria-labelledby="transfer-files-title">
    <fieldset :disabled="props.offline" style="border:0;padding:0;margin:0;min-width:0">
    <h3 id="transfer-files-title">当前采集端与账号的文件记录</h3>
    <p>列出本服务已保全或提交、且已验证归属的真实文件。远端接口只提供待收数量，尚未下载的文件名无法在此列出。查看列表不会取件、重发或改变处理状态。</p>
    <p>文件生成时间来自认证报文；接收时间从本次升级后开始记录，旧记录显示“未记录”。已上传、已保全或已移交处理均不代表原生消息已发送。</p>
    <button class="btn-ghost" type="button" :disabled="busy" @click="load()">刷新文件记录</button>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <div v-if="page?.items.length" class="table-scroll">
      <table>
        <caption>{{ page.workbench_id }} / {{ page.account_uid }}</caption>
        <thead><tr><th>实际文件名</th><th>类型</th><th>文件生成时间</th><th>本机接收时间</th><th>处理状态</th></tr></thead>
        <tbody><tr v-for="file in page.items" :key="file.id">
          <td>{{ file.name }}</td><td>{{ kinds[file.kind] || file.kind }}<small>{{ file.extension || '无扩展名' }} · {{ file.lane === 'up' ? '采集端上行' : '工作台下行' }}</small></td>
          <td>{{ when(file.source_created_at) }}</td><td>{{ when(file.received_at) }}</td><td>{{ states[file.state] || file.state }}</td>
        </tr></tbody>
      </table>
    </div>
    <p v-else-if="page && !busy">当前选择暂无已验证归属的文件记录。</p>
    <button v-if="page?.next_before" class="btn-ghost" type="button" :disabled="busy" @click="load(true)">加载更早记录</button>
    <p v-if="busy" role="status">正在读取文件记录…</p>
    </fieldset>
  </section>
</template>

<style scoped>
.file-history{margin:20px 0 24px;padding:18px;border:1px solid #e8e2dc;border-radius:10px;min-width:0}
h3{margin:0 0 12px}p{color:#70665e;line-height:1.7;overflow-wrap:anywhere}.error{color:#a02e2e}
.table-scroll{overflow:auto;max-height:min(480px,55vh);margin:16px 0}table{border-collapse:collapse;width:100%;font-size:13px}caption{text-align:left;color:#70665e;padding:8px;overflow-wrap:anywhere}
thead{position:sticky;top:0;z-index:1}
th,td{text-align:left;padding:10px;border-bottom:1px solid #e8e2dc;vertical-align:top}th{white-space:nowrap;background:#faf8f5}td:first-child{overflow-wrap:anywhere;min-width:180px;max-width:360px}small{display:block;color:#70665e;margin-top:4px}
button{font:inherit;padding:7px 14px;border-radius:8px;min-height:36px}button:disabled{opacity:.5;cursor:not-allowed}
</style>
