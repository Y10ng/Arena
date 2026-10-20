<template>
  <div v-if="loading" class="data-table__state" role="status" aria-live="polite">
    {{ loadingLabel }}
  </div>
  <div v-else-if="rows.length === 0" class="data-table__state">
    {{ emptyLabel }}
  </div>
  <div v-else class="data-table__scroll" role="region" :aria-label="caption" tabindex="0">
    <table class="data-table" :class="`data-table--${density}`">
      <caption class="data-table__caption">
        {{
          caption
        }}
      </caption>
      <thead>
        <tr>
          <th
            v-for="column in columns"
            :key="column.key"
            scope="col"
            :class="`data-table__cell--${column.align ?? 'start'}`"
          >
            {{ column.label }}
          </th>
          <th v-if="hasActions" class="data-table__action-heading" scope="col">
            {{ actionColumnLabel }}
          </th>
        </tr>
      </thead>
      <tbody>
        <template v-for="(row, rowIndex) in rows" :key="rowKey(row, rowIndex)">
          <tr v-bind="resolvedRowAttrs(row, rowIndex)">
            <td
              v-for="column in columns"
              :key="column.key"
              :class="`data-table__cell--${column.align ?? 'start'}`"
            >
              <slot :name="`cell-${column.key}`" :row="row" :column="column" :row-index="rowIndex">
                {{ column.value?.(row, rowIndex) }}
              </slot>
            </td>
            <td v-if="hasActions" class="data-table__action-cell">
              <Button
                variant="link"
                size="sm"
                :aria-label="actionLabel?.(row, rowIndex)"
                :aria-expanded="isRowExpanded ? Boolean(isRowExpanded(row, rowIndex)) : undefined"
                :aria-controls="isRowExpanded ? rowDetailId?.(row, rowIndex) : undefined"
                @click.stop="emit('row-action', row)"
              >
                <ChevronDown
                  v-if="isRowExpanded"
                  :size="14"
                  class="data-table__expand-icon"
                  :class="{ 'is-expanded': isRowExpanded(row, rowIndex) }"
                  aria-hidden="true"
                />
                {{
                  isRowExpanded
                    ? isRowExpanded(row, rowIndex)
                      ? '收起'
                      : '展开'
                    : actionColumnLabel
                }}
              </Button>
            </td>
          </tr>
          <tr v-if="isRowExpanded?.(row, rowIndex)" class="data-table__detail-row">
            <td :colspan="columns.length + (hasActions ? 1 : 0)" class="data-table__detail-cell">
              <div
                :id="rowDetailId?.(row, rowIndex)"
                class="data-table__detail"
                role="region"
                :aria-label="actionLabel?.(row, rowIndex)"
              >
                <slot name="row-detail" :row="row" :row-index="rowIndex" />
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
  </div>
</template>

<script setup lang="ts" generic="Row, ColumnKey extends string = string">
import { computed } from 'vue'
import type { HTMLAttributes } from 'vue'
import Button from '../ui/Button.vue'
import { ChevronDown } from '@lucide/vue'

export type DataTableDensity = 'default' | 'compact'
export type DataTableColumnAlign = 'start' | 'center' | 'end'
export type DataTableRowKey = string | number
export type DataTableCellValue = string | number | null | undefined
export type DataTableActiveRowAria = 'current' | 'selected'
export type DataTableRowAttrs = Omit<HTMLAttributes, 'key' | 'ref'> & {
  key?: never
  ref?: never
  [attribute: `data-${string}`]: string | number | boolean | undefined
}

export interface DataTableColumn<Row, ColumnKey extends string = string> {
  key: ColumnKey
  label: string
  align?: DataTableColumnAlign
  value?: (row: Row, rowIndex: number) => DataTableCellValue
}

type DataTableCellSlotProps<Row, ColumnKey extends string> = {
  row: Row
  column: DataTableColumn<Row, ColumnKey>
  rowIndex: number
}

type DataTableSlots<Row, ColumnKey extends string> = {
  [Key in ColumnKey as `cell-${Key}`]?: (props: DataTableCellSlotProps<Row, ColumnKey>) => unknown
}

const props = withDefaults(
  defineProps<{
    rows: readonly Row[]
    columns: readonly DataTableColumn<Row, ColumnKey>[]
    rowKey: (row: Row, rowIndex: number) => DataTableRowKey
    caption: string
    density?: DataTableDensity
    loading?: boolean
    loadingLabel?: string
    emptyLabel?: string
    isRowExpanded?: (row: Row, rowIndex: number) => boolean
    rowDetailId?: (row: Row, rowIndex: number) => string
    isRowActive?: (row: Row, rowIndex: number) => boolean
    activeRowAria?: DataTableActiveRowAria
    rowAttrs?: (row: Row, rowIndex: number) => DataTableRowAttrs
    actionLabel?: (row: Row, rowIndex: number) => string
    actionColumnLabel?: string
  }>(),
  {
    density: 'default',
    loading: false,
    loadingLabel: '正在加载…',
    emptyLabel: '暂无数据',
    isRowActive: undefined,
    activeRowAria: undefined,
    rowAttrs: undefined,
    actionLabel: undefined,
    actionColumnLabel: '操作',
  },
)

defineSlots<
  DataTableSlots<Row, ColumnKey> & {
    'row-detail'?: (props: { row: Row; rowIndex: number }) => unknown
  }
>()

const emit = defineEmits<{
  (event: 'row-action', row: Row): void
}>()

const hasActions = computed(() => props.actionLabel !== undefined)

function resolvedRowAttrs(row: Row, rowIndex: number): DataTableRowAttrs {
  const attrs = { ...(props.rowAttrs?.(row, rowIndex) ?? {}) } as HTMLAttributes & {
    key?: unknown
    ref?: unknown
  }
  Reflect.deleteProperty(attrs, 'key')
  Reflect.deleteProperty(attrs, 'ref')
  const active = props.isRowActive?.(row, rowIndex) ?? false
  attrs.class = [attrs.class, { 'data-table__row--active': active }]

  if (props.activeRowAria === 'current') {
    attrs['aria-current'] = active ? 'true' : undefined
  } else if (props.activeRowAria === 'selected') {
    attrs['aria-selected'] = active ? 'true' : 'false'
  }

  return attrs as DataTableRowAttrs
}

defineOptions({ name: 'DataTable' })
</script>

<style scoped>
.data-table__state {
  width: 100%;
  padding: var(--space-5);
  border: 1px dashed var(--border);
  border-radius: var(--radius-md);
  color: var(--text-faint);
  text-align: center;
}

.data-table__scroll {
  width: 100%;
  min-width: 0;
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
}

.data-table__scroll:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.data-table {
  width: 100%;
  min-width: max-content;
  border-collapse: collapse;
  color: var(--text);
  font-size: var(--text-md);
}

.data-table__caption {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.data-table th,
.data-table td {
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: middle;
}

.data-table th {
  background: var(--bg-inset);
  color: var(--text-muted);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  white-space: nowrap;
}

.data-table tbody tr {
  height: calc(var(--control-height) + var(--space-4));
  transition: background-color var(--dur-fast) var(--ease);
}

.data-table tbody tr:hover {
  background: var(--surface-hover);
}

.data-table__row--active {
  background: var(--surface-selected);
}

.data-table tbody tr:last-child td {
  border-bottom: 0;
}

.data-table--compact th,
.data-table--compact td {
  padding: var(--space-2) var(--space-3);
}

.data-table--compact tbody tr {
  height: calc(var(--control-height) + var(--space-2));
}

.data-table__cell--start {
  text-align: start;
}

.data-table__cell--center {
  text-align: center;
}

.data-table__cell--end,
.data-table__action-heading,
.data-table__action-cell {
  text-align: end;
}

.data-table__action-heading,
.data-table__action-cell {
  position: sticky;
  inset-inline-end: 0;
  width: 1%;
  border-inline-start: 1px solid var(--border-subtle);
  white-space: nowrap;
}

.data-table__action-heading {
  z-index: 2;
  background: var(--bg-inset);
}

.data-table__action-cell {
  z-index: 1;
  background: var(--bg-surface);
}

.data-table tbody tr:hover .data-table__action-cell {
  background: var(--surface-hover);
}

.data-table tbody tr.data-table__row--active .data-table__action-cell,
.data-table tbody tr.data-table__row--active:hover .data-table__action-cell {
  background: var(--surface-selected);
}

.data-table__expand-icon {
  flex-shrink: 0;
  transition: transform var(--dur-fast) var(--ease);
}

.data-table__expand-icon.is-expanded {
  transform: rotate(180deg);
}

.data-table tbody .data-table__detail-row,
.data-table tbody .data-table__detail-row:hover {
  height: auto;
  background: var(--bg-surface);
}

.data-table .data-table__detail-cell {
  padding: 0;
  vertical-align: top;
}

.data-table__detail {
  min-width: 0;
  padding: var(--space-4);
  border-block-start: 1px solid var(--border-subtle);
}

@media (prefers-reduced-motion: reduce) {
  .data-table__expand-icon,
  .data-table tbody tr {
    transition: none;
  }
}
</style>
