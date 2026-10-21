<script setup lang="ts">
/** 意向询价 / 机器人监控顶部的采集端切换。只列出测试通过的通道。 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { App } from 'ant-design-vue'
import OrgTree from './OrgTree.vue'
import {
  activateIm, imContactSource, imContacts, imCurrent, imFeedError, imIdentityText, imModeLabel, imReady,
  imTarget, loadImLinks, orgEmpty, orgTree, imOrg, imOrgStatus, orgEmptyLabel, orgReadNotice, imHealthError, refreshImContacts, selectImTarget, type ImLink,
} from '../lib/imLink'

const props = withDefaults(defineProps<{ showAddressBook?: boolean }>(), { showAddressBook: true })

const { message } = App.useApp()
const busy = ref(false)
const openBook = ref(false)
const showOrg = computed(() => imContactSource.value === 'catalog' || imCurrent.value?.mode === 'oss')

let healthTimer: ReturnType<typeof setInterval> | null = null
let refreshing = false
async function refreshHealth() {
  if (refreshing) return
  refreshing = true
  try { await loadImLinks() }
  catch (e) { imHealthError.value = e instanceof Error ? e.message : '通道状态刷新失败' }
  finally { refreshing = false }
}
onMounted(() => { void refreshHealth(); healthTimer = setInterval(() => void refreshHealth(), 5000) })
onUnmounted(() => { if (healthTimer) clearInterval(healthTimer) })

function optionLabel(link: ImLink) {
  const who = imIdentityText(link)
  const name = link.name || imModeLabel(link)
  return who ? `${name} · ${who}` : name
}

async function onPick(e: Event) {
  const id = (e.target as HTMLSelectElement).value
  if (!id) return
  busy.value = true
  try {
    await activateIm(id)
    openBook.value = false
    message.success('已切换采集端')
  } catch (err) {
    message.error(err instanceof Error ? err.message : '切换失败')
  } finally {
    busy.value = false
  }
}

async function readBook(refresh = false) {
  busy.value = true
  try {
    const got = await refreshImContacts(refresh)
    openBook.value = true
    if (got.org_state === 'error') message.warning(orgReadNotice(imOrg.value, got))
    else if (got.source === 'catalog') {
      message[orgEmptyLabel(got.org, got) ? 'warning' : 'success'](orgReadNotice(got.org, got))
    } else {
      const via = ['contacts', 'its-relay'].includes(got.source) ? '通讯录' : '私聊会话'
      message.success(`已读取 ${got.contacts.length} 人（${via}）`)
    }
  } catch (err) {
    message.error(err instanceof Error ? err.message : '读通讯录失败')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="im-bar">
    <p v-if="imHealthError" class="im-miss" role="alert">{{ imHealthError }}</p>
    <div class="im-row">
      <span class="im-k">采集端</span>
      <select class="ny-input im-sel" :disabled="busy" :value="imCurrent?.id || ''" @change="onPick">
        <option value="">{{ imReady.length ? '选择已测通的账号' : '还没有测通的通道' }}</option>
        <option v-for="l in imReady" :key="l.id" :value="l.id">{{ optionLabel(l) }}</option>
      </select>
      <template v-if="imCurrent">
        <span class="im-tag">{{ imModeLabel(imCurrent) }}</span>
        <span v-if="imCurrent.mode !== 'oss'" class="mono im-ep">{{ imCurrent.endpoint }}</span>
        <span v-else class="mono im-ep">{{ imCurrent.workbench_id }}</span>
        <span v-if="imCurrent.catalog_stale" class="im-stale">目录已过期</span>
        <span v-else class="im-ok">{{ imCurrent.mode === 'oss' ? '目录已校验' : '测试通过' }}</span>
      </template>
      <span v-else class="im-miss">未接入。请到采集通道选择公网上报或邮件中转，选好工作台和企点号，测试通过后在这里切换。</span>
      <span v-if="imFeedError" class="im-stale">{{ imFeedError }}</span>
      <button v-if="props.showAddressBook" class="btn-ghost im-btn" type="button" :disabled="busy || !imCurrent" @click="readBook(false)">读取通讯录</button>
      <button v-if="props.showAddressBook" class="btn-ghost im-btn" type="button" :disabled="busy || !imCurrent" @click="readBook(true)">刷新通讯录</button>
      <router-link class="im-cfg" to="/link">配置通道</router-link>
    </div>
    <div v-if="props.showAddressBook && openBook && imCurrent" class="im-book" :class="{ tree: showOrg }">
      <template v-if="showOrg">
        <div class="im-head">
          <span class="im-k">组织架构</span>
          <span v-if="imTarget" class="im-ok">已选 {{ imTarget.name }}</span>
        </div>
        <p v-if="orgEmpty" class="im-miss" data-org="empty">{{ orgEmptyLabel(imOrg, imOrgStatus) }}</p>
        <OrgTree v-else :nodes="orgTree" />
        <p v-if="imOrgStatus.org_state === 'error' && !orgEmpty" class="im-stale">{{ orgReadNotice(imOrg, imOrgStatus) }}</p>
        <div v-if="imContacts.length" class="im-people">
          <span class="im-k">私人好友 {{ imContacts.length }}</span>
          <button v-for="p in imContacts" :key="p.uin" type="button" class="im-person" :class="{ on: imTarget?.uin === p.uin }" @click="selectImTarget(p)">{{ p.name }} · <em class="mono">{{ p.uin }}</em></button>
        </div>
      </template>
      <template v-else>
        <span class="im-k">询价对象 {{ imContacts.length }}</span><span v-if="imTarget" class="im-ok">已选 {{ imTarget.name }}</span>
        <span v-if="!imContacts.length" class="im-miss">这个账号下还没有可读的联系人</span>
        <button v-for="p in imContacts" :key="p.uin" type="button" class="im-person" :class="{ on: imTarget?.uin === p.uin }" @click="selectImTarget(p)">{{ p.name }} · <em class="mono">{{ p.uin }}</em></button>
      </template>
    </div>
  </div>
</template>

<style scoped>
.im-bar {
  margin: 0; padding: 10px 14px; border-radius: 14px;
  background: rgba(255,255,255,.62); border: 1px solid rgba(176, 28, 36,.12);
}
.im-row { display: flex; align-items: center; gap: 10px; min-width: 0; flex-wrap: wrap; }
.im-k { font-size: 12px; font-weight: 700; color: #B01C24; letter-spacing: 1px; }
.im-sel { height: 32px; min-width: 280px; }
.im-tag {
  font-size: 12px; font-weight: 700; color: #B01C24;
  background: rgba(176, 28, 36,.08); border-radius: 999px; padding: 2px 8px;
}
.im-ep { font-size: 12px; color: #5C5148; }
.im-ok { font-size: 12px; color: #1F6B3E; font-weight: 700; }
.im-miss { font-size: 12px; color: #8A7F76; }
.im-stale { font-size: 12px; color: #B01C24; font-weight: 700; }
.im-btn { height: 32px; padding: 0 12px; border-radius: 8px; font-size: 12.5px; }
.im-cfg { font-size: 12.5px; font-weight: 600; }
.im-book { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; align-items: center; }
.im-book.tree { flex-direction: column; align-items: stretch; flex-wrap: nowrap; }
.im-head, .im-people { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.im-miss { margin: 0; }
.im-person {
  font-size: 12px; padding: 2px 8px; border-radius: 999px; cursor: pointer;
  background: rgba(255,255,255,.8); border: 1px solid rgba(176, 28, 36,.12);
  font-family: inherit; color: inherit;
}
.im-person.on { color: #fff; background: #B01C24; border-color: transparent; }
.im-person.on em { color: rgba(255,255,255,.8); }
.im-person em { margin-left: 6px; font-style: normal; color: #8A7F76; font-size: 11px; }
</style>
