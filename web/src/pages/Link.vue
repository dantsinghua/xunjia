<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { App } from 'ant-design-vue'
import { api, type MqConfig } from '../api'
import InnodealingSettings from '../components/InnodealingSettings.vue'
import MqSettings from '../components/MqSettings.vue'
import CollectorAccountSettings from '../components/CollectorAccountSettings.vue'
import FileTransferFiles from '../components/FileTransferFiles.vue'
import CollectorHistory from '../components/CollectorHistory.vue'
import SessionResetSettings from '../components/SessionResetSettings.vue'
import { imLinks, imBoundAccount, imModeLabel, loadImLinks, refreshImContacts, type ImLink } from '../lib/imLink'

const { message } = App.useApp()
const connectionReady = ref(false)
const institutionMode = ref(false), selectedOnline = ref(false)
const accountOffline = computed(() => institutionMode.value && !selectedOnline.value)
const collectors = ref<InstanceType<typeof CollectorAccountSettings> | null>(null)
async function connectionStatus(value: MqConfig, saved: boolean) {
  connectionReady.value = value.configured && value.applied
  if (!connectionReady.value) return
  await nextTick()
  await collectors.value?.readDirectory(saved)
  await reload()
}
const editing = ref<string | null>(null)
const busy = ref(false)
const steps = ref<{name:string;ok:boolean;detail:string}[]>([])
type Channel = ImLink & { configured?:boolean; version?:number; state?:string }
const selected = computed(() => imLinks.value.find(l => l.id === editing.value) as Channel | undefined)
function fill(link: ImLink) { editing.value = link.id }
async function reload() {
  try {
    await loadImLinks()
    if (!imLinks.value.some(l => l.id === editing.value)) editing.value = imLinks.value.find(l=>l.active)?.id || imLinks.value[0]?.id || null

  } catch(e) { message.error(e instanceof Error ? e.message : '读取连接状态失败') }
}
async function testSelected() {
  if (!selected.value || busy.value || accountOffline.value) return
  const id=selected.value.id; busy.value=true
  try {
    const result = await api.imTest(id) as {ok:boolean;hint?:string;data?:{passed:boolean;steps:typeof steps.value}}
    if (!result.ok) throw Error(result.hint || '检查失败')
    steps.value=result.data?.steps || []
    await reload()
    if (!result.data?.passed) message.warning('只读检查未通过，请核对部署配置和采集端')
  } catch(e) { message.error(e instanceof Error ? e.message : '检查失败') }
  finally {busy.value=false}
}
async function makeCurrent() {
  if (!selected.value || selected.value.active || busy.value || accountOffline.value) return
  const link=selected.value;busy.value=true
  try {
    const result = await api.imActivate(link.id,link.version) as {ok:boolean;hint?:string}
    if (!result.ok) throw Error(result.hint || '切换未完成')
    await reload();message.success('已切换主用通道；请重新确认询价授权后启动')
  } catch(e) {message.error(e instanceof Error ? e.message : '切换未完成')}
  finally {busy.value=false}
}
async function collectorSaved() {
  steps.value = []
  await reload()
  try { await refreshImContacts() }
  catch (e) { message.error(e instanceof Error ? e.message : '读取当前账号通讯录失败') }
}

</script>

<template>
  <section class="connections">
    <h2>本人账号与收发通道</h2>
    <p v-if="imBoundAccount">企点账号 <b>{{ imBoundAccount.account_uid }}</b> · 工作台 {{ imBoundAccount.workbench_id }}</p>
    <p v-else>尚未选择采集账号，请先配置文件中转，再选择工作台和台内企点号。</p>
    <p>按当前选择的工作台与企点账号读取通讯录。文件中转负责当前账号收发；更换账号或连接配置会清空旧业务、暂停及结果未知状态，重新加载当前账号通讯录和原生历史；不会迁移或重发旧任务。</p>
    <MqSettings @status="connectionStatus" />
    <CollectorAccountSettings v-if="connectionReady" ref="collectors" :auto-load="false" @saved="collectorSaved" @institution="institutionMode = $event" @availability="selectedOnline = $event" />
    <p v-else role="status">先保存文件中转 base_url 和 config.yaml，并确认配置已加载，再读取采集端目录、选择账号与通讯录。</p>
    <FileTransferFiles v-if="connectionReady && imBoundAccount" :offline="accountOffline" />
    <CollectorHistory v-if="connectionReady && imBoundAccount && institutionMode" :online="selectedOnline" />
    <SessionResetSettings v-if="connectionReady && imBoundAccount" :offline="accountOffline" />
    <div class="channels">
      <button v-for="link in (imLinks as Channel[])" :key="link.id" class="channel" :class="{ selected:editing===link.id }" type="button" @click="fill(link)">
        <b>{{ imModeLabel(link) }}</b><span>{{ link.active ? '主用' : '备用' }}</span>
        <span>{{ link.state || (link.tested ? '只读检查通过' : '未检查') }}</span>
      </button>
    </div>
    <div v-if="selected" class="actions">
      <button class="btn-ghost" type="button" :disabled="busy || !selected.configured || accountOffline" @click="testSelected">检查目录与通讯录</button>
      <button class="btn-primary" type="button" :disabled="busy || selected.active || !selected.configured || accountOffline" @click="makeCurrent">切换为主用通道</button>
    </div>
    <ul v-if="steps.length"><li v-for="step in steps" :key="step.name">{{ step.ok ? '通过' : '未通过' }} · {{ step.name }}：{{ step.detail }}</li></ul>
    <p>目录检查不代表消息已发送。发送状态以采集端执行回执为准，结果未知时先对账。</p>
    <InnodealingSettings />
    <button class="btn-ghost" type="button" :disabled="busy" @click="reload">刷新通道状态</button>
  </section>
</template>

<style scoped>
.connections{max-width:1120px;margin:24px auto;padding:24px;background:#fff;border:1px solid #e8e2dc;border-radius:14px}
h2{margin-top:0}p{color:#70665e;line-height:1.7}.channels{display:flex;gap:16px;margin:24px 0}.channel{display:flex;flex:1;flex-direction:column;gap:10px;padding:20px;text-align:left;border:1px solid #e8e2dc;background:#faf8f5;border-radius:10px;cursor:pointer}.channel.selected{border-color:#b01c24}.actions{display:flex;gap:12px}.source{border-top:1px solid #eee;margin-top:24px;padding-top:12px}li{line-height:1.8}
button{font:inherit;line-height:1.5} .actions{flex-wrap:wrap}.actions button,.connections>button{padding:8px 14px;border-radius:8px;min-height:36px}.channel{min-width:0}.channel b{font-size:16px;color:#2B2320}.channel span{overflow-wrap:anywhere}.connections>p,li{overflow-wrap:anywhere}
@media(max-width:760px){.connections{margin:16px;padding:18px}.channels{flex-direction:column}}
</style>
