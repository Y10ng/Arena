<template>
  <DataTable class="traffic-table" :rows="rows" :columns="columns" :row-key="rowKey" :caption="caption"
    density="traffic" resizable-columns :loading="loading" :empty-label="emptyLabel"
    :is-row-active="isSelected" active-row-aria="current" :row-attrs="rowAttrs">
    <template #cell-host="{ row }"><span class="traffic-table__ellipsis" :title="row.display_host">{{ row.display_host || '地址缺失' }}</span><span v-if="row.address_issue" class="traffic-table__warning">地址待确认</span></template>
    <template #cell-method="{ row }"><span class="traffic-table__method">{{ row.method }}</span></template>
    <template #cell-url="{ row }"><button type="button" class="traffic-table__target" :title="row.display_target" :aria-label="`阅读 ${row.method} ${row.display_host}${row.display_target}`" @click.stop="emit('select', row)">{{ row.display_target }}</button></template>
    <template #cell-status="{ row }"><span class="traffic-table__status" :class="{ 'is-success': row.status_code !== null && row.status_code >= 200 && row.status_code < 300, 'is-warning': row.status_code !== null && row.status_code >= 400 && row.status_code < 500, 'is-danger': row.status_code !== null && row.status_code >= 500 }">{{ row.status_code ?? '无响应' }}</span><span v-if="row.state !== 'complete'" class="traffic-table__warning" :title="row.error_code || undefined">{{ stateLabels[row.state] }}</span></template>
    <template #cell-bytes="{ row }"><span>{{ responseBytes(row) }}</span><span v-if="row.source_state === 'partial'" class="traffic-table__warning">部分采集</span><span v-if="['pending', 'receiving'].includes(row.sync.bodies.response.status)" class="traffic-table__warning">待同步</span></template>
    <template #cell-project="{ row }"><span class="traffic-table__ellipsis" :title="row.project_title">{{ row.project_title }}</span></template>
    <template #cell-time="{ row }"><span class="traffic-table__muted" :title="`平台首次接收：${row.received_at}`">{{ dateTime(row.received_at) }}</span></template>
    <template #cell-associations="{ row }"><div class="traffic-table__associations" @click.stop @keydown.stop><slot name="associations" :row="row"><button v-if="row.associations.vulnerability_count" type="button" class="traffic-table__link" :aria-label="`查看 ${row.associations.vulnerability_count} 个关联漏洞`" @click="emit('associations', row, $event)">已关联 {{ row.associations.vulnerability_count }}</button><span v-else class="traffic-table__muted">未关联</span></slot></div></template>
  </DataTable>
</template>

<script setup lang="ts">
import type { TrafficSummary } from '@/api/traffic'
import DataTable, { type DataTableColumn, type DataTableRowAttrs } from '@/components/patterns/DataTable.vue'
import { dateTime } from '@/lib/format'

const props = withDefaults(defineProps<{
  rows: readonly TrafficSummary[]
  selectedId?: string
  selectedProjectId?: string
  caption?: string
  loading?: boolean
  emptyLabel?: string
}>(), { caption: 'HTTP 流量', emptyLabel: '尚无流量记录' })
const emit = defineEmits<{
  (event: 'select', row: TrafficSummary): void
  (event: 'associations', row: TrafficSummary, trigger: Event): void
}>()
const stateLabels = { receiving: '接收中', complete: '完整', partial: '部分采集', failed: '采集失败' }
function responseBytes(row: TrafficSummary) {
  const bytes = row.sync.bodies.response.captured_bytes
  return bytes == null ? '—' : `${bytes.toLocaleString('zh-CN')} B`
}
function duration(row: TrafficSummary) {
  if (row.finished_at === null) return '未结束'
  const elapsed = (row.finished_at - row.started_at) * 1000
  return Number.isFinite(elapsed) && elapsed >= 0 ? `${Math.round(elapsed).toLocaleString('zh-CN')} ms` : '不可确定'
}
const columns: DataTableColumn<TrafficSummary>[] = [
  { key: 'host', label: 'Host', width: 220, minWidth: 170, value: row => row.display_host },
  { key: 'method', label: '方法', width: 82, minWidth: 72, value: row => row.method },
  { key: 'url', label: 'URL', width: 420, minWidth: 260, flexible: true, value: row => row.display_target },
  { key: 'status', label: '状态码', width: 144, minWidth: 132 },
  { key: 'bytes', label: '正文大小', width: 168, minWidth: 144, align: 'end', value: responseBytes },
  { key: 'project', label: '项目', width: 160, minWidth: 130, value: row => row.project_title },
  { key: 'time', label: '接收时间', width: 178, minWidth: 166 },
  { key: 'duration', label: '耗时', width: 100, minWidth: 88, align: 'end', value: duration },
  { key: 'associations', label: '关联', width: 190, minWidth: 170 },
]
function rowKey(row: TrafficSummary) { return JSON.stringify([row.project_id, row.id]) }
function isSelected(row: TrafficSummary) { return row.id === props.selectedId && row.project_id === props.selectedProjectId }
function rowAttrs(row: TrafficSummary, index: number): DataTableRowAttrs {
  return { tabindex: 0, onClick: () => emit('select', row), onKeydown: (event: KeyboardEvent) => {
    if (event.target !== event.currentTarget) return
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); emit('select', row) }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const next = props.rows[index + (event.key === 'ArrowDown' ? 1 : -1)]
      if (!next) return
      emit('select', next)
      const sibling = event.key === 'ArrowDown' ? (event.currentTarget as HTMLElement).nextElementSibling : (event.currentTarget as HTMLElement).previousElementSibling
      ;(sibling as HTMLElement | null)?.focus()
    }
  } }
}
</script>

<style scoped>
.traffic-table { min-width: 0; min-height: 0; overflow: auto; }
.traffic-table :deep(th) { position: sticky; top: 0; z-index: 1; }
.traffic-table :deep(td), .traffic-table :deep(.data-table__cell-content) { white-space: nowrap; }
.traffic-table :deep(tbody tr) { cursor: pointer; }
.traffic-table__ellipsis, .traffic-table__target { display: block; min-width: 0; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.traffic-table__target { width: 100%; min-height: 28px; padding: 0; border: 0; background: transparent; color: var(--text); font: inherit; text-align: start; cursor: pointer; }
.traffic-table__target:hover { color: var(--accent); }
.traffic-table__method { font-family: var(--font-mono); font-weight: var(--font-medium); }
.traffic-table__status { font-variant-numeric: tabular-nums; }
.traffic-table__status.is-success { color: var(--color-success); }
.traffic-table__status.is-warning, .traffic-table__warning { color: var(--warning); }
.traffic-table__status.is-danger { color: var(--color-danger); }
.traffic-table__warning { margin-inline-start: var(--space-2); }
.traffic-table__muted { color: var(--text-muted); }
.traffic-table__associations { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
.traffic-table :deep(td:nth-child(4) .data-table__cell-content), .traffic-table :deep(td:nth-child(5) .data-table__cell-content) { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-1) var(--space-2); }
.traffic-table :deep(td:nth-child(5) .data-table__cell-content) { justify-content: flex-end; }
.traffic-table :deep(td:nth-child(4)) .traffic-table__warning, .traffic-table :deep(td:nth-child(5)) .traffic-table__warning { margin: 0; }
.traffic-table__link { min-height: 28px; padding: 0; border: 0; background: transparent; color: var(--accent); font: inherit; cursor: pointer; }
.traffic-table__link:hover { text-decoration: underline; }
.traffic-table__target:focus-visible, .traffic-table__link:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.traffic-table :deep(td:nth-child(5)), .traffic-table :deep(td:nth-child(8)) { font-variant-numeric: tabular-nums; }
</style>
