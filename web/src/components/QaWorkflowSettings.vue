<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api, type QaWorkflowState } from '../api'

const state = ref<QaWorkflowState | null>(null)
const form = reactive({enabled: false, base_url: '', tool_base_url: '', app_key: ''})
const busy = ref(false), error = ref(''), notice = ref('')
let disposed = false
let controller: AbortController | undefined
function fill(value: QaWorkflowState) {
  state.value = value
  form.enabled = value.enabled; form.base_url = value.base_url; form.tool_base_url = value.tool_base_url
  form.app_key = ''
}
async function downloadDsl() {
  error.value=''
  try { await api.qaWorkflowDsl() }
  catch(e) { error.value=e instanceof Error ? e.message : '工作流下载失败' }
}
async function act(save = false) {
  if (busy.value || disposed || save && !state.value?.available) return
  busy.value = true; error.value = ''; notice.value = ''
  controller = new AbortController()
  const timer = setTimeout(() => controller?.abort(), 20000)
  try {
    const response = save
      ? await api.saveQaWorkflow({...form, expected_revision: state.value?.revision || ''}, controller.signal)
      : await api.qaWorkflow(controller.signal)
    if (disposed) return
    if (!response.ok || !response.data) throw Error(response.hint || '工作流配置读取失败')
    fill(response.data)
    if (save) notice.value = '配置已保存。尚未代你导入或发布 Dify 应用；请用只读问题检查实际连通性。'
  } catch (e) { if (!disposed) error.value = e instanceof Error ? e.message : '工作流配置操作失败' }
  finally { clearTimeout(timer); busy.value = false; form.app_key = '' }
}
onMounted(() => { void act() })
onBeforeUnmount(() => { disposed = true; controller?.abort() })
</script>

<template>
  <section class="qa-workflow-settings" aria-labelledby="qa-workflow-title" data-testid="qa-workflow-settings">
    <h3 id="qa-workflow-title">智能问答工作流</h3>
    <p>使用独立 Dify Workflow 识别闲聊、查券、当日报价和询价意图。询价仍须在问答页策略弹窗选择对手并确认，不修改现有分析 A / 回复 B。</p>
    <p v-if="state" role="status">{{ state.hint }}</p>
    <p v-if="state && !state.available" class="warning">未确定受管安装目录，请先由技术人员配置应用安装前缀。</p>
    <form @submit.prevent="act(true)">
      <label>Dify API 地址<input v-model="form.base_url" placeholder="https://dify.example/v1" autocomplete="off" :disabled="busy" /></label>
      <label>新工作流 App API Key<input v-model="form.app_key" type="password" autocomplete="new-password" :placeholder="state?.app_key_set ? '已保存，留空保留' : '只填写新 Workflow 的应用密钥'" :disabled="busy" /></label>
      <label>Dify 可访问的询价应用地址<input v-model="form.tool_base_url" placeholder="https://app.example/xunjia" autocomplete="off" :disabled="busy" /></label>
      <p>填写 Dify 容器能够访问的应用地址，保留实际部署前缀。回调路径：<code>{{ state?.tool_path || '/api/agent/tools/qa_market/query' }}</code>。</p>
      <label class="enable"><input v-model="form.enabled" type="checkbox" :disabled="busy" />启用智能问答工作流</label>
      <p class="tip">先导入并发布随包提供的 <code>dify/qa_workflow/workflow.yml</code>，选择已配置模型，再填写新应用密钥。密钥不会回显。</p>
      <div class="actions settings-actions">
        <button class="btn-primary" type="submit" :disabled="busy || !state?.available">{{ busy ? '正在处理…' : '保存工作流配置' }}</button>
        <button class="btn-ghost" type="button" :disabled="busy" @click="act()">重新读取</button>
        <button class="btn-ghost" type="button" @click="downloadDsl">下载问答工作流</button>
      </div>
    </form>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
  </section>
</template>

<style scoped>
.qa-workflow-settings{margin:20px 0;padding:18px;border:1px solid #e8e2dc;border-radius:10px}h3{margin:0 0 12px}p{font-size:13px;line-height:1.7;color:#70665e;overflow-wrap:anywhere}form{display:grid;gap:12px}label{display:grid;gap:6px;font-size:13px}input:not([type=checkbox]){padding:9px 12px;border:1px solid #d8cfc5;border-radius:7px;max-width:100%;font:inherit}.enable{display:flex;align-items:center}.actions{display:flex;gap:10px;flex-wrap:wrap}button{padding:8px 14px;border-radius:8px;cursor:pointer;font:inherit}button:disabled{opacity:.5;cursor:not-allowed}.error{color:#a02e2e}.warning{color:#885600}
</style>
