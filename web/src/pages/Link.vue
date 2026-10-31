<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { App } from 'ant-design-vue'
import { api, type MqConfig } from '../api'
import InnodealingSettings from '../components/InnodealingSettings.vue'
import QTradeSettings from '../components/QTradeSettings.vue'
import MqSettings from '../components/MqSettings.vue'
import UpgradeSyncSettings from '../components/UpgradeSyncSettings.vue'
import QaWorkflowSettings from '../components/QaWorkflowSettings.vue'
import DifyFlowSettings from '../components/DifyFlowSettings.vue'
import CollectorAccountSettings from '../components/CollectorAccountSettings.vue'
import FileTransferFiles from '../components/FileTransferFiles.vue'
import CollectorHistory from '../components/CollectorHistory.vue'
import SessionResetSettings from '../components/SessionResetSettings.vue'
import { imLinks, imBoundAccount, imModeLabel, loadImLinks, refreshImContacts, type ImLink } from '../lib/imLink'

const { message } = App.useApp()
const panels = [
  { id: 'transfer', label: '文件中转' }, { id: 'upgrade', label: '升级包' },
  { id: 'workflow', label: '问答工作流' }, { id: 'account', label: '采集账号' },
  { id: 'analysis', label: '事实解析工作流' }, { id: 'reply', label: '谈判回复工作流' },
  { id: 'innodealing', label: 'Innodealing' }, { id: 'qt', label: 'QT' },
  { id: 'files', label: '文件记录' }, { id: 'history', label: '历史消息' },
  { id: 'reset', label: '会话重置' }, { id: 'channels', label: '收发通道' },
] as const
const activePanel = ref<(typeof panels)[number]['id']>('transfer')
const settingsRoot = ref<HTMLElement | null>(null)
const detailPane = ref<HTMLElement | null>(null)
const actionHeight = ref(0)
let resizeObserver: ResizeObserver | undefined, mutationObserver: MutationObserver | undefined
let observedActions: HTMLElement | null = null
function measureActions() {
  const actions = settingsRoot.value?.querySelector<HTMLElement>(`[data-settings-panel="${activePanel.value}"] .settings-actions`) || null
  if (actions !== observedActions) {
    resizeObserver?.disconnect()
    observedActions = actions
    if (actions) resizeObserver?.observe(actions)
  }
  actionHeight.value = actions?.getBoundingClientRect().height || 0
}
watch(activePanel, async () => {
  await nextTick()
  if (detailPane.value) detailPane.value.scrollTop = 0
  measureActions()
})
onMounted(() => {
  resizeObserver = new ResizeObserver(measureActions)
  mutationObserver = new MutationObserver(measureActions)
  if (settingsRoot.value) mutationObserver.observe(settingsRoot.value, { childList: true, subtree: true })
  measureActions()
})
onBeforeUnmount(() => { resizeObserver?.disconnect(); mutationObserver?.disconnect() })
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
  <section ref="settingsRoot" class="connections settings-layout" aria-label="系统设置" :style="{ '--settings-actions-height': actionHeight + 'px' }">
    <nav class="settings-nav" aria-label="系统设置导航">
      <h1>系统设置</h1>
      <p>连接、服务与历史记录</p>
      <button v-for="panel in panels" :id="'settings-nav-' + panel.id" :key="panel.id" type="button"
        :aria-controls="'settings-panel-' + panel.id" :aria-current="activePanel === panel.id ? 'page' : undefined"
        :class="{ active: activePanel === panel.id }" @click="activePanel = panel.id">{{ panel.label }}</button>
    </nav>
    <div ref="detailPane" class="settings-detail" tabindex="0" aria-label="设置详情">
      <div class="settings-context">
        <p v-if="imBoundAccount">当前企点账号 <b>{{ imBoundAccount.account_uid }}</b> · 工作台 {{ imBoundAccount.workbench_id }}</p>
        <p v-else>尚未选择采集账号，可先配置文件中转，再选择工作台和台内企点号。</p>
      </div>
      <section v-show="activePanel === 'transfer'" id="settings-panel-transfer" data-settings-panel="transfer" aria-labelledby="settings-nav-transfer">
        <MqSettings @status="connectionStatus" />
      </section>
      <section v-show="activePanel === 'upgrade'" id="settings-panel-upgrade" data-settings-panel="upgrade" aria-labelledby="settings-nav-upgrade">
        <UpgradeSyncSettings />
      </section>
      <section v-show="activePanel === 'workflow'" id="settings-panel-workflow" data-settings-panel="workflow" aria-labelledby="settings-nav-workflow">
        <QaWorkflowSettings />
      </section>
      <section v-show="activePanel === 'analysis'" id="settings-panel-analysis" data-settings-panel="analysis" aria-labelledby="settings-nav-analysis"><DifyFlowSettings stage="analysis" /></section>
      <section v-show="activePanel === 'reply'" id="settings-panel-reply" data-settings-panel="reply" aria-labelledby="settings-nav-reply"><DifyFlowSettings stage="reply" /></section>
      <section v-show="activePanel === 'account'" id="settings-panel-account" data-settings-panel="account" aria-labelledby="settings-nav-account">
        <CollectorAccountSettings v-if="connectionReady" ref="collectors" :auto-load="false" @saved="collectorSaved" @institution="institutionMode = $event" @availability="selectedOnline = $event" />
        <p v-else role="status">请先确认文件中转连接已加载，再读取采集端目录、选择账号与通讯录。</p>
      </section>
      <section v-show="activePanel === 'innodealing'" id="settings-panel-innodealing" data-settings-panel="innodealing" aria-labelledby="settings-nav-innodealing"><InnodealingSettings /></section>
      <section v-show="activePanel === 'qt'" id="settings-panel-qt" data-settings-panel="qt" aria-labelledby="settings-nav-qt"><QTradeSettings /></section>
      <section v-show="activePanel === 'files'" id="settings-panel-files" data-settings-panel="files" aria-labelledby="settings-nav-files">
        <FileTransferFiles v-if="connectionReady && imBoundAccount" :offline="accountOffline" />
        <p v-else>请先配置文件中转并选择采集账号。</p>
      </section>
      <section v-show="activePanel === 'history'" id="settings-panel-history" data-settings-panel="history" aria-labelledby="settings-nav-history">
        <CollectorHistory v-if="imBoundAccount" :online="selectedOnline" />
        <p v-else>请先选择采集账号，再读取服务端已入库的历史消息。</p>
      </section>
      <section v-show="activePanel === 'reset'" id="settings-panel-reset" data-settings-panel="reset" aria-labelledby="settings-nav-reset">
        <SessionResetSettings v-if="connectionReady && imBoundAccount" :offline="accountOffline" />
        <p v-else>请先配置文件中转并选择采集账号。</p>
      </section>
      <section v-show="activePanel === 'channels'" id="settings-panel-channels" data-settings-panel="channels" aria-labelledby="settings-nav-channels">
        <h3>本人账号与收发通道</h3>
        <p>按当前选择的工作台与企点账号读取通讯录。文件中转负责当前账号收发；更换账号或连接配置会归档旧工作区，保留历史事实和结果未知记录，不会迁移或重发旧任务。</p>
        <div class="channels">
          <button v-for="link in (imLinks as Channel[])" :key="link.id" class="channel" :class="{ selected:editing===link.id }" type="button" @click="fill(link)">
            <b>{{ imModeLabel(link) }}</b><span>{{ link.active ? '主用' : '备用' }}</span>
            <span>{{ link.state || (link.tested ? '只读检查通过' : '未检查') }}</span>
          </button>
        </div>
        <div class="actions settings-actions">
          <button v-if="selected" class="btn-ghost" type="button" :disabled="busy || !selected.configured || accountOffline" @click="testSelected">检查目录与通讯录</button>
          <button v-if="selected" class="btn-primary" type="button" :disabled="busy || selected.active || !selected.configured || accountOffline" @click="makeCurrent">切换为主用通道</button>
          <button class="btn-ghost" type="button" :disabled="busy" @click="reload">刷新通道状态</button>
        </div>
        <ul v-if="steps.length"><li v-for="step in steps" :key="step.name">{{ step.ok ? '通过' : '未通过' }} · {{ step.name }}：{{ step.detail }}</li></ul>
        <p>目录检查不代表消息已发送。发送状态以采集端执行回执为准，结果未知时先对账。</p>
      </section>
    </div>
  </section>
</template>

<style scoped>
.connections{--settings-nav-width:220px;--settings-edge:24px;--settings-top:66px;display:grid;grid-template-columns:var(--settings-nav-width) minmax(0,1fr);height:calc(100vh - var(--settings-top));height:calc(100dvh - var(--settings-top));margin:0 var(--settings-edge);background:#fff;border-left:1px solid #e8e2dc;border-right:1px solid #e8e2dc;box-sizing:border-box;overflow:hidden;min-width:0}
.settings-nav{overflow-y:auto;overscroll-behavior:contain;padding:26px 14px 24px;border-right:1px solid #e8e2dc;background:#faf8f5;min-height:0;box-sizing:border-box}
.settings-nav h1{margin:0 10px 6px;font-size:22px}.settings-nav p{margin:0 10px 22px;font-size:12px}.settings-nav button{display:block;width:100%;padding:13px 14px;margin:4px 0;border:0;border-radius:8px;background:transparent;text-align:left;color:#5c5148;cursor:pointer}.settings-nav button:hover{background:#f1e9e2}.settings-nav button.active{background:#f4e4e4;color:#a31b24;font-weight:700}.settings-nav button:focus-visible{outline:2px solid #b01c24;outline-offset:-2px}
.settings-detail{min-width:0;min-height:0;overflow-y:auto;overflow-x:hidden;overscroll-behavior:contain;padding:24px 32px calc(var(--settings-actions-height,0px) + 32px);box-sizing:border-box}.settings-context{border-bottom:1px solid #e8e2dc;margin-bottom:24px;padding-bottom:8px;font-size:13px}.settings-context p{margin-top:0}
.settings-detail :deep([data-settings-panel]>section){margin:0;padding:0;border:0;border-radius:0;min-width:0}
.settings-detail :deep(.settings-actions){position:fixed;left:calc(var(--settings-edge) + var(--settings-nav-width) + 1px);right:calc(var(--settings-edge) + 1px);bottom:0;z-index:30;display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:0;padding:16px 32px calc(16px + env(safe-area-inset-bottom));box-sizing:border-box;max-height:40dvh;overflow:auto;background:#fff;border-top:1px solid #e8e2dc;box-shadow:0 -4px 18px rgba(43,35,32,.05)}
.settings-detail :deep(.settings-actions button){max-width:100%;white-space:normal}.settings-detail :deep(.messages),.settings-detail :deep(.table-scroll),.settings-detail :deep(.timeline){max-height:none;overflow-y:visible}
.settings-detail :deep(input),.settings-detail :deep(textarea),.settings-detail :deep(select){min-width:0;max-width:100%;box-sizing:border-box}.settings-detail :deep(label),.settings-detail :deep(h3){min-width:0;overflow-wrap:anywhere}
h2{margin-top:0}p{color:#70665e;line-height:1.7}.channels{display:flex;gap:16px;margin:24px 0}.channel{display:flex;flex:1;flex-direction:column;gap:10px;padding:20px;text-align:left;border:1px solid #e8e2dc;background:#faf8f5;border-radius:10px;cursor:pointer}.channel.selected{border-color:#b01c24}.actions{display:flex;gap:12px}.source{border-top:1px solid #eee;margin-top:24px;padding-top:12px}li{line-height:1.8}
button{font:inherit;line-height:1.5} .actions{flex-wrap:wrap}.actions button,.connections>button{padding:8px 14px;border-radius:8px;min-height:36px}.channel{min-width:0}.channel b{font-size:16px;color:#2B2320}.channel span{overflow-wrap:anywhere}.connections>p,li{overflow-wrap:anywhere}
@media(max-width:760px){.connections{--settings-nav-width:152px;--settings-edge:0px}.settings-nav{padding:20px 8px}.settings-nav h1{font-size:19px}.settings-nav p{margin-bottom:16px}.settings-nav button{padding:12px 10px}.settings-detail{padding:20px 18px calc(var(--settings-actions-height,0px) + 24px)}.settings-detail :deep(.settings-actions){padding:12px 18px calc(12px + env(safe-area-inset-bottom))}.channels{flex-direction:column}}
@media(max-width:480px){.connections{--settings-nav-width:112px}.settings-nav{padding:16px 6px}.settings-nav h1{font-size:17px;margin-left:6px}.settings-nav p{display:none}.settings-nav button{font-size:12px;padding:12px 8px}.settings-detail{padding:16px 12px calc(var(--settings-actions-height,0px) + 20px)}.settings-detail :deep(.settings-actions){padding:10px 12px calc(10px + env(safe-area-inset-bottom));gap:8px}.settings-detail :deep(.settings-actions button){font-size:12px;padding:7px 10px}.settings-detail :deep(fieldset){grid-template-columns:minmax(0,1fr)}}
</style>
