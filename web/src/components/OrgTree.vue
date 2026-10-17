<script setup lang="ts">
/** 组织架构树。收起的那一支不渲染，避免一次把上万人画成按钮。 */
import { ref } from 'vue'
import { imTarget, selectImTarget, visibleOrgBranch, type OrgMember, type OrgTreeNode } from '../lib/imLink'

defineOptions({ name: 'OrgTree' })
const props = defineProps<{ nodes: OrgTreeNode[]; nested?: boolean }>()

const openIds = ref<string[]>([])

function opened(id: string) {
  return visibleOrgBranch(props.nodes, openIds.value).find(node => node.id === id)?.opened || false
}
function toggle(id: string) {
  openIds.value = opened(id) ? openIds.value.filter(item => item !== id) : [...openIds.value, id]
}
function pick(member: OrgMember) {
  selectImTarget({ uin: member.uin, name: member.name, remark: '' })
}
</script>

<template>
  <div class="org-tree" :class="{ root: !nested }">
    <div v-for="node in nodes" :key="node.id" class="org-branch">
      <button
        type="button"
        class="org-dept"
        :data-dept="node.id"
        :aria-expanded="opened(node.id)"
        @click="toggle(node.id)"
      >
        <span class="caret">{{ opened(node.id) ? '▾' : '▸' }}</span>
        <span>{{ node.name }}</span>
        <em v-if="node.children.length || node.members.length" class="mono">
          <template v-if="node.children.length">{{ node.children.length }} 个下级</template>
          <template v-if="node.members.length">{{ node.members.length }} 人</template>
        </em>
      </button>
      <div v-if="opened(node.id)" class="org-kids">
        <OrgTree v-if="node.children.length" :nodes="node.children" nested />
        <button
          v-for="(member, index) in node.members"
          :key="node.id + ':' + member.uin + ':' + index"
          type="button"
          class="org-person"
          :data-uin="member.uin"
          :class="{ on: imTarget?.uin === member.uin }"
          @click="pick(member)"
        >
          {{ member.name }} · <em class="mono">{{ member.uin }}</em>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.org-tree { width: 100%; display: flex; flex-direction: column; gap: 4px; }
.org-tree.root { max-height: 320px; overflow: auto; }
.org-dept {
  width: 100%; text-align: left; cursor: pointer; font-family: inherit;
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; font-weight: 700; color: #B01C24;
  background: rgba(176, 28, 36, .08); border: 1px solid rgba(176, 28, 36, .12);
  border-radius: 8px; padding: 4px 8px;
}
.org-dept .caret { width: 12px; }
.org-dept em { margin-left: auto; font-style: normal; color: #8A7F76; font-size: 11px; font-weight: 600; }
.org-kids { margin: 2px 0 2px 18px; display: flex; flex-direction: column; align-items: flex-start; gap: 4px; }
.org-person {
  font-size: 12px; padding: 2px 8px; border-radius: 999px; cursor: pointer;
  background: rgba(255, 255, 255, .8); border: 1px solid rgba(176, 28, 36, .12);
  font-family: inherit; color: inherit;
}
.org-person.on { color: #fff; background: #B01C24; border-color: transparent; }
.org-person.on em { color: rgba(255, 255, 255, .8); }
.org-person em { margin-left: 6px; font-style: normal; color: #8A7F76; font-size: 11px; }
</style>
