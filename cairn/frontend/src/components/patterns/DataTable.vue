<template>
  <div v-if="loading && rows.length === 0" class="data-table__state" role="status" aria-live="polite">
    {{ loadingLabel }}
  </div>
  <div v-else-if="rows.length === 0" class="data-table__state">
    {{ emptyLabel }}
  </div>
  <div
    v-else
    ref="scrollContainer"
    class="data-table__scroll"
    role="region"
    :aria-label="caption"
    :aria-busy="loading ? 'true' : undefined"
    tabindex="0"
  >
    <table
      class="data-table"
      :class="[`data-table--${density}`, { 'data-table--resizable': resizableColumns }]"
      :style="resizableColumns ? { width: `${tableWidth}px` } : undefined"
    >
      <caption class="data-table__caption">
        {{
          caption
        }}
      </caption>
      <colgroup v-if="resizableColumns">
        <col
          v-for="column in columns"
          :key="column.key"
          :style="{ width: `${columnWidth(column)}px` }"
        />
        <col v-if="hasActions" style="width: 96px" />
      </colgroup>
      <thead>
        <tr>
          <th
            v-for="(column, columnIndex) in columns"
            :key="column.key"
            scope="col"
            :class="`data-table__cell--${column.align ?? 'start'}`"
          >
            <span v-if="resizableColumns" class="data-table__cell-content" :title="column.label">{{
              column.label
            }}</span>
            <template v-else>{{ column.label }}</template>
            <span
              v-if="resizableColumns && columnIndex < columns.length - 1"
              class="data-table__column-resizer"
              role="separator"
              tabindex="0"
              :aria-label="`调整${column.label}列宽`"
              aria-orientation="vertical"
              :aria-valuenow="columnWidth(column)"
              :aria-valuemin="minimumWidth(column)"
              :aria-valuemax="boundaryMaximum(columnIndex)"
              @pointerdown.stop.prevent="startColumnResize($event, columnIndex)"
              @pointermove.stop="resizeColumn"
              @pointerup.stop="endColumnResize"
              @pointercancel.stop="endColumnResize"
              @lostpointercapture="columnDrag = null"
              @click.stop.prevent
              @dblclick.stop.prevent
              @keydown.stop="resizeColumnKey($event, columnIndex)"
            />
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
              <div
                v-if="resizableColumns"
                class="data-table__cell-content"
                :title="String(column.value?.(row, rowIndex) ?? '')"
              >
                <slot
                  :name="`cell-${column.key}`"
                  :row="row"
                  :column="column"
                  :row-index="rowIndex"
                >
                  {{ column.value?.(row, rowIndex) }}
                </slot>
              </div>
              <slot
                v-else
                :name="`cell-${column.key}`"
                :row="row"
                :column="column"
                :row-index="rowIndex"
              >
                {{ column.value?.(row, rowIndex) }}
              </slot>
            </td>
            <td v-if="hasActions" class="data-table__action-cell">
              <div class="data-table__actions">
                <Button
                  class="data-table__action-trigger"
                  variant="ghost"
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
                  <span v-if="!actionIconOnly">{{
                    isRowExpanded
                      ? isRowExpanded(row, rowIndex)
                        ? '收起'
                        : '展开'
                      : actionColumnLabel
                  }}</span>
                </Button>
                <slot name="row-actions" :row="row" :row-index="rowIndex" />
              </div>
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
import { computed, ref, watch } from 'vue'
import type { HTMLAttributes } from 'vue'
import { toLogicalPixels } from '@/lib/uiScale'
import Button from '../ui/Button.vue'
import { ChevronDown } from '@lucide/vue'

export type DataTableDensity = 'default' | 'compact' | 'traffic'
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
  width?: number
  minWidth?: number
  flexible?: boolean
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
    resizableColumns?: boolean
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
    actionIconOnly?: boolean
  }>(),
  {
    density: 'default',
    resizableColumns: false,
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
    'row-actions'?: (props: { row: Row; rowIndex: number }) => unknown
  }
>()

const emit = defineEmits<{
  (event: 'row-action', row: Row): void
}>()

const hasActions = computed(() => props.actionLabel !== undefined)
// 仅用户调宽更新偏好；容器变窄不覆盖它，恢复空间后仍能还原合理比例。
const preferredWidths = ref<Record<string, number>>({})
const scrollContainer = ref<HTMLElement | null>(null)
const containerWidth = ref(0)
let columnDrag: {
  pointerId: number
  startX: number
  width: number
  pairWidth: number
  index: number
} | null = null
function minimumWidth(column: DataTableColumn<Row, ColumnKey>) {
  return Math.max(48, column.minWidth ?? 80)
}
function preferredWidth(column: DataTableColumn<Row, ColumnKey>) {
  return Math.max(minimumWidth(column), preferredWidths.value[column.key] ?? column.width ?? 160)
}

// 按偏好比例分配组内空间；触底列先锁定，其余列继续分摊，避免长内容撑破外缘。
function fitColumns(columns: readonly DataTableColumn<Row, ColumnKey>[], available: number) {
  const widths: Record<string, number> = {}
  let remaining = [...columns]
  while (remaining.length) {
    const weight = remaining.reduce((total, column) => total + preferredWidth(column), 0)
    const atMinimum = remaining.filter(
      (column) => (available * preferredWidth(column)) / weight < minimumWidth(column),
    )
    if (!atMinimum.length) {
      remaining.forEach((column) => {
        widths[column.key] = (available * preferredWidth(column)) / weight
      })
      break
    }
    for (const column of atMinimum) {
      widths[column.key] = minimumWidth(column)
      available -= widths[column.key]!
    }
    remaining = remaining.filter((column) => !atMinimum.includes(column))
  }
  return widths
}
const fittedWidths = computed(() => {
  const columns = props.columns
  const actionWidth = hasActions.value ? 96 : 0
  const minimum = columns.reduce((total, column) => total + minimumWidth(column), 0)
  const preferred = columns.reduce((total, column) => total + preferredWidth(column), 0)
  const available = Math.max(
    minimum,
    (containerWidth.value || preferred + actionWidth) - actionWidth,
  )
  const flexible = columns.filter((column) => column.flexible)
  if (!flexible.length) return fitColumns(columns, available)
  const fixed = columns.filter((column) => !column.flexible)
  const fixedPreferred = fixed.reduce((total, column) => total + preferredWidth(column), 0)
  const flexibleMinimum = flexible.reduce((total, column) => total + minimumWidth(column), 0)
  const fixedAvailable = Math.min(fixedPreferred, available - flexibleMinimum)
  return {
    ...fitColumns(fixed, fixedAvailable),
    ...fitColumns(flexible, available - fixedAvailable),
  }
})
function columnWidth(column: DataTableColumn<Row, ColumnKey>) {
  return fittedWidths.value[column.key] ?? preferredWidth(column)
}
const tableWidth = computed(() =>
  props.columns.reduce((total, column) => total + columnWidth(column), hasActions.value ? 96 : 0),
)
watch(
  [scrollContainer, () => props.resizableColumns],
  ([element, enabled], _, onCleanup) => {
    if (!element || !enabled) return
    const measure = () => {
      // clientWidth 排除边框与纵向滚动条；只观察容器，不用表格宽度反向驱动测量。
      if (element.clientWidth > 0 && element.clientWidth !== containerWidth.value) {
        columnDrag = null
        containerWidth.value = element.clientWidth
      }
    }
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    measure()
    onCleanup(() => observer.disconnect())
  },
  { flush: 'post' },
)
watch(
  () => props.columns,
  () => {
    columnDrag = null
  },
)

function boundaryMaximum(index: number) {
  const left = props.columns[index]!
  const right = props.columns[index + 1]!
  return columnWidth(left) + columnWidth(right) - minimumWidth(right)
}
function setBoundaryWidth(index: number, width: number, pairWidth: number) {
  const left = props.columns[index]!
  const right = props.columns[index + 1]!
  const next = Math.max(minimumWidth(left), Math.min(pairWidth - minimumWidth(right), width))
  // 固定本次实际布局，只交换相邻两列空间；其他列与表格总宽不受拖动影响。
  preferredWidths.value = {
    ...preferredWidths.value,
    ...fittedWidths.value,
    [left.key]: next,
    [right.key]: pairWidth - next,
  }
}
function startColumnResize(event: PointerEvent, index: number) {
  if (event.button !== 0) return
  const left = props.columns[index]!
  const right = props.columns[index + 1]!
  columnDrag = {
    pointerId: event.pointerId,
    startX: event.clientX,
    width: columnWidth(left),
    pairWidth: columnWidth(left) + columnWidth(right),
    index,
  }
  const handle = event.currentTarget as HTMLElement
  handle.focus({ preventScroll: true })
  handle.setPointerCapture(event.pointerId)
}
function resizeColumn(event: PointerEvent) {
  if (columnDrag?.pointerId !== event.pointerId) return
  setBoundaryWidth(
    columnDrag.index,
    columnDrag.width + toLogicalPixels(event.clientX - columnDrag.startX),
    columnDrag.pairWidth,
  )
}
function endColumnResize(event: PointerEvent) {
  if (columnDrag?.pointerId !== event.pointerId) return
  columnDrag = null
  ;(event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId)
}
function resizeColumnKey(event: KeyboardEvent, index: number) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const left = props.columns[index]!
  const right = props.columns[index + 1]!
  const width =
    event.key === 'Home'
      ? minimumWidth(left)
      : event.key === 'End'
        ? boundaryMaximum(index)
        : columnWidth(left) + (event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 40 : 10)
  setBoundaryWidth(index, width, columnWidth(left) + columnWidth(right))
}

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
  padding: var(--space-8) var(--space-5);
  border: 1px solid var(--border);
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

.data-table--resizable {
  table-layout: fixed;
  min-width: 0;
}

.data-table--resizable thead th {
  position: relative;
  padding-inline-end: var(--space-3);
}

.data-table__cell-content {
  display: block;
  min-width: 0;
  max-width: 100%;
  overflow-wrap: anywhere;
  white-space: normal;
}

.data-table__column-resizer {
  position: absolute;
  inset-block: 0;
  inset-inline-end: 0;
  width: 8px;
  cursor: col-resize;
  touch-action: none;
  user-select: none;
}

.data-table__column-resizer::after {
  content: '';
  position: absolute;
  inset-block: 20%;
  inset-inline-end: 2px;
  width: 2px;
  background: var(--color-control-border);
}

.data-table__column-resizer:hover::after,
.data-table__column-resizer:focus-visible::after {
  background: var(--accent);
}
.data-table__column-resizer:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
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
  padding: var(--space-2) var(--space-4);
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: middle;
}

.data-table th {
  height: var(--table-heading-height);
  padding-block: var(--space-2);
  background: var(--bg-inset);
  color: var(--text-muted);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  white-space: nowrap;
}

.data-table tbody tr {
  height: var(--table-row-height);
  transition: background-color var(--dur-fast) var(--ease);
}

.data-table tbody tr:hover {
  background: var(--surface-hover);
}

.data-table tbody tr.data-table__row--active,
.data-table tbody tr.data-table__row--active:hover {
  background: var(--surface-selected);
}

.data-table tbody tr:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
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

.data-table--traffic {
  font-size: var(--text-dense);
  line-height: var(--leading-normal);
}

.data-table--traffic th,
.data-table--traffic td {
  padding: var(--space-1) var(--space-3);
}

.data-table--traffic tbody tr {
  height: var(--control-height);
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

.data-table__actions {
  display: inline-flex;
  flex-wrap: nowrap;
  align-items: center;
  gap: var(--space-1);
  white-space: nowrap;
  vertical-align: middle;
}

.data-table__actions > :deep(*) {
  flex-shrink: 0;
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
  padding: var(--space-5) var(--space-6);
  border-block-start: 1px solid var(--border-subtle);
}

@media (prefers-reduced-motion: reduce) {
  .data-table__expand-icon,
  .data-table tbody tr {
    transition: none;
  }
}
</style>
