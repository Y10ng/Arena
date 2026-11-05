<template>
  <footer class="pagination" :aria-label="label">
    <div class="pagination__summary" role="status"><span>{{ total ? `第 ${number(start)}–${number(end)} 条，共 ${number(total)} 条` : '共 0 条 · 共 0 页' }}</span><slot /></div>
    <div class="pagination__controls">
      <Select :aria-label="`${label}每页条数`" :model-value="String(limit)" :options="sizes.map(value => ({ value: String(value), label: `${value} 条/页` }))" :disabled="loading" @update:model-value="emit('limit', Number($event))" />
      <span class="pagination__position">{{ totalPages ? `第 ${page}/${number(totalPages)} 页` : '共 0 页' }}</span>
      <nav class="pagination__pages" :aria-label="`${label}页码`">
        <Button size="sm" variant="ghost" :disabled="loading || page <= 1 || !totalPages" aria-label="上一页" @click="emit('page', page - 1)"><ChevronLeft :size="16" /></Button>
        <template v-for="(value, index) in pages" :key="`${index}:${value}`">
          <span v-if="value === '…'" class="pagination__ellipsis" aria-hidden="true">…</span>
          <Button v-else size="sm" variant="ghost" :disabled="loading" :aria-label="`第 ${value} 页`" :aria-current="value === page ? 'page' : undefined" :class="{ 'is-current': value === page }" @click="emit('page', value)">{{ value }}</Button>
        </template>
        <Button size="sm" variant="ghost" :disabled="loading || !totalPages || page >= totalPages" aria-label="下一页" @click="emit('page', page + 1)"><ChevronRight :size="16" /></Button>
      </nav>
      <form class="pagination__jump" @submit.prevent="jump"><label>跳至<Input v-model="draft" type="number" inputmode="numeric" min="1" :max="Math.max(1, totalPages)" :disabled="loading || !totalPages" :aria-label="`${label}跳至页码`" />页</label></form>
    </div>
  </footer>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ChevronLeft, ChevronRight } from '@lucide/vue'
import { Button, Input, Select } from '@/components/ui'
const props = withDefaults(defineProps<{ page: number; limit: number; total: number; totalPages: number; loading?: boolean; sizes?: readonly number[]; label?: string }>(), { sizes: () => [50, 100, 200], label: '流量分页' })
const emit = defineEmits<{ (event: 'page', page: number): void; (event: 'limit', limit: number): void }>()
const draft = ref('')
watch(() => props.page, () => { draft.value = '' })
const number = (value: number) => value.toLocaleString('zh-CN')
const start = computed(() => (props.page - 1) * props.limit + 1)
const end = computed(() => Math.min(props.total, props.page * props.limit))
const pages = computed<(number | '…')[]>(() => {
  if (props.totalPages <= 7) return Array.from({ length: props.totalPages }, (_, index) => index + 1)
  const selected = new Set([1, props.totalPages])
  const from = Math.max(2, Math.min(props.page - 1, props.totalPages - 4))
  const to = Math.min(props.totalPages - 1, Math.max(props.page + 1, 5))
  for (let value = from; value <= to; value++) selected.add(value)
  const result: (number | '…')[] = []
  let previous = 0
  for (const value of [...selected].sort((a, b) => a - b)) {
    if (value - previous > 1) result.push('…')
    result.push(value)
    previous = value
  }
  return result
})
function jump() {
  if (props.loading || !/^\d+$/.test(draft.value)) return
  const value = Number(draft.value)
  if (Number.isSafeInteger(value) && value >= 1 && value <= props.totalPages && value !== props.page) emit('page', value)
}
</script>

<style scoped>
.pagination { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-3) var(--space-5); padding-block: var(--space-2); min-width: 0; flex: 0 0 auto; color: var(--text-muted); font-size: var(--text-sm); font-variant-numeric: tabular-nums; --field-control-height: var(--control-height-filter); }
.pagination__summary { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-3); margin-inline-end: auto; }
.pagination__controls, .pagination__pages, .pagination__jump label { display: flex; align-items: center; gap: var(--space-1); }
.pagination__controls { flex-wrap: wrap; gap: var(--space-2) var(--space-3); }
.pagination__controls :deep(.ui-select__trigger) { width: 112px; }
.pagination__position { white-space: nowrap; }
.pagination__pages :deep(.ui-button) { min-width: 30px; min-height: var(--control-height-filter); padding-inline: var(--space-2); font-size: inherit; }
.pagination__pages :deep(.is-current) { background: var(--surface-selected); color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.pagination__ellipsis { min-width: 24px; text-align: center; }
.pagination__jump { margin: 0; }
.pagination__jump label { white-space: nowrap; gap: var(--space-2); }
.pagination__jump :deep(input) { width: 56px; padding-inline: var(--space-2); font-size: inherit; }
</style>
