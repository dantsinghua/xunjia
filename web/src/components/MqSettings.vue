<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api, type MqConfig, type MqConnectionTest } from '../api'
import { imBoundAccount, clearImAccount } from '../lib/imLink'

const emit = defineEmits<{ saved: []; status: [value: MqConfig, saved: boolean] }>()
const state = ref<MqConfig | null>(null)
const displayedBinding = computed(() => imBoundAccount.value ?? state.value)
const tlsFields = ['client_cert_file', 'client_key_file', 'ca_file'] as const
const form = reactive({ base_url: '', client_cert_file: '', client_key_file: '', ca_file: '' })
const dirty = computed(() => !!state.value && (['base_url', ...tlsFields] as const).some(key => form[key] !== (state.value?.[key] || '')))
const loading = ref(false), busy = ref(false), error = ref(''), notice = ref('')
const tested = ref<MqConnectionTest | null>(null)
let disposed = false

function applyStatus(value: MqConfig) {
  state.value = value
  form.base_url = value.base_url || ''
  for (const key of tlsFields) form[key] = value[key] || ''
}
function changed() { tested.value = null; notice.value = '' }
async function refresh() {
  if (loading.value || busy.value) return
  loading.value = true; error.value = ''
  try {
    const response = await api.mqConfig()
    if (!response.ok || !response.data) throw Error(response.hint || '文件中转配置读取失败')
    if (!disposed) { applyStatus(response.data); changed(); emit('status', response.data, false) }
  } catch (e) { if (!disposed) error.value = e instanceof Error ? e.message : '文件中转配置读取失败' }
  finally { loading.value = false }
}
async function act(kind: 'save' | 'test') {
  if (busy.value || loading.value || !state.value) return
  if (!state.value.credentials_set) { error.value = '服务端尚未配置认证信息，请联系运维完成配置'; return }
  error.value = ''; notice.value = ''; tested.value = null
  busy.value = true
  try {
    const tls = Object.fromEntries(tlsFields.filter(key => form[key] || state.value?.[key]).map(key => [key, form[key]]))
    const body = { base_url: form.base_url, ...tls, expected_revision: state.value.revision }
    if (kind === 'test') {
      const response = await api.testMqConfig(body)
      if (!response.ok || !response.data) throw Error(response.hint || 'pending 连接检查没有返回结果')
      if (!disposed) {
        tested.value = response.data
        notice.value = '本次仅检查 pending 接口，未保存配置；不代表上传、下载或真实消息收发已验证。'
      }
    } else {
      const response = await api.saveMqConfig(body)
      if (!response.ok || !response.data) throw Error(response.hint || '文件中转配置未保存')
      if (!disposed) {
        if (response.data.reset) clearImAccount()
        applyStatus(response.data)
        notice.value = response.data.reset ? response.data.note : response.data.applied
          ? response.data.account_uid
            ? '文件中转配置已保存，现役网关已加载。真实收发须单独验证。'
            : '文件中转配置已保存，现役网关已加载。正在读取采集端目录，随后可选择工作台与企点号。'
          : '配置已保存，现役网关尚未加载，请查看下方状态原因。'
        emit('status', response.data, true); emit('saved')
      }
    }
  } catch (e) { if (!disposed) error.value = e instanceof Error ? e.message : '文件中转操作失败' }
  finally { busy.value = false }
}
onMounted(() => { void refresh() })
onBeforeUnmount(() => { disposed = true })
</script>

<template>
  <section class="mq-settings" aria-labelledby="mq-title" data-testid="mq-config">
    <h3 id="mq-title">文件中转连接配置</h3>
    <p>认证信息由服务端管理。确认文件中转连接后，从采集端目录选择工作台与企点号。</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="state">
      <div class="summary">
        <span>配置：{{ state.configured ? '已保存' : '未完整配置' }}</span>
        <span>运行配置：{{ state.applied ? '已加载' : '未加载' }}</span>
        <span>凭据：{{ state.credentials_set ? '已保存' : '尚未保存' }}</span>
        <span>账号：{{ displayedBinding?.account_uid || '尚未绑定' }}</span>
        <span>工作台：{{ displayedBinding?.workbench_id || '尚未绑定' }}</span>
      </div>
      <p v-if="state.note">{{ state.note }}</p>
      <p v-if="!state.credentials_set" role="alert">服务端尚未配置认证信息，请联系运维完成配置</p>
      <form autocomplete="off" :data-xunjia-unsaved="dirty ? 'true' : undefined" @submit.prevent="act('save')" @input="changed">
        <fieldset :disabled="busy || loading">
          <label>文件中转地址 base_url<input v-model="form.base_url" name="mq-base-url" type="url" autocomplete="off" placeholder="https://its-relay.yingmi.cn" required /></label>
          <label>客户端证书路径（服务端 .crt）<input v-model="form.client_cert_file" name="mq-client-cert" autocomplete="off" placeholder="/etc/its-relay/client.crt" /></label>
          <label>客户端私钥路径（服务端 .key）<input v-model="form.client_key_file" name="mq-client-key" autocomplete="off" placeholder="/etc/its-relay/client.key" /></label>
          <label>服务器 CA 路径（可选）<input v-model="form.ca_file" name="mq-ca-file" autocomplete="off" placeholder="留空使用系统信任库" /></label>
        </fieldset>
        <p class="hint">更换连接配置会归档旧工作区并重新选择账号，保留历史事实和待核对记录；不会迁移或重发旧任务。原样保存不归档。</p>
        <p class="hint">正式环境使用 HTTPS 双向认证与 accesstoken。填写运行服务所在机器上的证书路径，不上传或回显私钥内容。</p>
        <p class="hint">测试只调用 pending 接口，不上传、下载或发送消息；通过不代表真实消息收发全链成功。</p>
        <div class="controls">
          <button class="btn-ghost" type="button" :disabled="busy || loading || !state.credentials_set" data-testid="mq-test" @click="act('test')">测试连接</button>
          <button class="btn-primary" type="submit" :disabled="busy || loading || !state.credentials_set" data-testid="mq-save">保存并连接文件中转</button>
          <button class="btn-ghost" type="button" :disabled="busy || loading" @click="refresh">重新读取配置</button>
        </div>
      </form>
      <div v-if="tested" class="probe" :class="{ error: !tested.passed || !tested.connected }" data-testid="mq-test-result" role="status">
        <b>{{ tested.passed && tested.connected ? 'pending 连接检查通过' : 'pending 连接检查未通过' }}</b>
        <ul><li v-for="(step, index) in tested.steps" :key="index">{{ step.ok ? '通过' : '未通过' }} · {{ step.name }}：{{ step.detail }}</li></ul>
        <p>本次未保存配置。pending 检查不代表上传、下载或真实消息收发全链成功。</p>
      </div>
    </template>
    <button v-else class="btn-ghost" type="button" :disabled="loading" @click="refresh">{{ loading ? '正在读取连接配置…' : '重新读取连接配置' }}</button>
  </section>
</template>

<style scoped>
.mq-settings{margin:20px 0 24px;padding:18px;border:1px solid #e8e2dc;border-radius:10px}
h3{margin:0 0 12px}p{color:#70665e;line-height:1.7}.summary{display:flex;flex-wrap:wrap;gap:14px;padding:12px;background:#faf8f5;border-radius:8px}
fieldset{display:grid;grid-template-columns:minmax(0,1fr);gap:16px;border:0;padding:0;margin:18px 0}
label{min-width:0;display:flex;flex-direction:column;gap:8px;font-weight:600}
input,textarea{box-sizing:border-box;width:100%;min-height:42px;border:1px solid #d9d0c8;border-radius:7px;padding:10px 12px;background:#fff;font:inherit;font-weight:400;color:#2B2320}input:focus,textarea:focus{outline:2px solid #b01c24;outline-offset:1px}
textarea{min-height:160px;max-height:420px;resize:vertical;font-family:var(--font-code);line-height:1.6}
.controls{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}button{min-height:36px;padding:7px 14px;border-radius:8px;font:inherit;line-height:1.5;cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}
.error{color:#a02e2e}.warning{color:#885600}.hint{font-size:13px}.probe{border:1px solid #ded7cf;border-radius:8px;padding:12px}li{line-height:1.8}
p,.summary span,li{overflow-wrap:anywhere}
</style>
