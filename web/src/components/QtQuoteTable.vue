<script setup lang="ts">
import type { BondRow, QtColumn } from '../lib/types'
const props = defineProps<{
  rows: (BondRow & { dispIb?: string; dispIo?: string })[]
  columns: QtColumn[]
  isPicked: (id: string) => boolean
  allChecked: boolean
  emptyReason?: string
}>()
const emit = defineEmits<{ toggle: [id: string]; toggleAll: []; open: [row: BondRow] }>()
function text(row: BondRow, key: string): string {
  const value = row.qtFields?.[key]
  return value == null || value === '' ? '—' : String(value)
}
const states: Record<string,string> = {ACTIVE:'有效报价记录',WITHDRAWN:'已撤回',EXPIRED:'已过期',ENDED:'对话已结束',PREVIOUS_DAY:'往日报价',SCOPE_UNCONFIRMED:'撤销范围待核对'}
</script>

<template>
  <table class="qt-table" data-testid="qt-detail-table">
    <thead><tr>
      <th class="qt-name"><input type="checkbox" :checked="allChecked" aria-label="全选QT明细" @change="emit('toggleAll')" />债券</th>
      <th>bid vol（万）</th><th title="本人已保存底价，不是QT的BidPx">bid底价</th>
      <th title="本人已保存底价，不是QT的OfferPx">ofr底价</th><th>ofr vol（万）</th>
      <th v-for="col in columns" :key="col.key" :data-qt-field="col.key" :title="`${col.key} · FIX ${col.tag}`">{{ col.label }}<small>{{ col.key }}</small></th>
      <th>记录状态</th>
    </tr></thead>
    <tbody>
      <tr v-if="!rows.length"><td :colspan="columns.length + 6" data-testid="channel-note">{{ emptyReason || '本人暂无QT对话报价；不会以示例数据填充。' }}</td></tr>
      <tr v-for="row in props.rows" :key="row.rowId" data-testid="qt-detail-row" :data-row-id="row.rowId">
        <td class="qt-name"><input type="checkbox" :checked="isPicked(row.rowId || '')" :aria-label="`选择QT明细 ${row.qtFields?.NegotiationID || ''}`" @change="emit('toggle', row.rowId || '')" />
          <button type="button" @click="emit('open',row)">{{ row.name }}</button><small>{{ row.code }}</small></td>
        <td data-testid="qt-bid-volume">{{ row.ibv }}</td><td data-testid="qt-bid-floor">{{ row.dispIb || row.ib }}</td>
        <td data-testid="qt-ofr-floor">{{ row.dispIo || row.io }}</td><td>{{ row.iov }}</td>
        <td v-for="col in columns" :key="col.key" :data-qt-field="col.key" :title="text(row,col.key)">{{ text(row,col.key) }}</td>
        <td :title="row.qtStatus">{{ states[row.qtStatus || ''] || row.qtStatus || '待核对' }}</td>
      </tr>
    </tbody>
  </table>
</template>

<style scoped>
.qt-table { border-collapse: separate; border-spacing: 0; width: max-content; min-width: 100%; font-size: 12px; }
.qt-table th, .qt-table td { min-width: 115px; max-width: 280px; padding: 9px 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; border-bottom: 1px solid var(--line, #e9e4de); text-align: left; }
.qt-table th { position: sticky; top: 0; z-index: 2; background: var(--surface, #faf7f2); }
.qt-table .qt-name { position: sticky; left: 0; z-index: 1; min-width: 220px; background: var(--surface, #faf7f2); }
.qt-table th.qt-name { z-index: 3; }
.qt-table small { display: block; font-size: 10px; font-weight: normal; opacity: .65; }
.qt-table input { margin-right: 8px; }
.qt-table button { border: 0; background: transparent; cursor: pointer; font: inherit; padding: 0; }
</style>
