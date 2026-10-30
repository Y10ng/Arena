<template>
  <section class="traffic-workbench" aria-label="HTTP 流量工作台">
    <header class="traffic-toolbar">
      <h2>流量</h2>
      <RouterLink v-if="sourceVulnerability" class="traffic-return" :to="sourceVulnerability">返回漏洞</RouterLink>
      <Select v-if="!fixedProjectId" aria-label="筛选项目" :model-value="queryText('project') || '__all__'" :options="projectOptions" @update:model-value="setFilter('project', $event)" />
      <span class="traffic-refresh-state" role="status">{{ refreshLabel }}</span>
      <Button size="sm" variant="secondary" :aria-pressed="paused" @click="togglePause"><Pause v-if="!paused" :size="14" /><Play v-else :size="14" />{{ paused ? '恢复刷新' : '暂停刷新' }}</Button>
      <Button size="sm" variant="ghost" :disabled="loading" @click="retry"><RefreshCw :size="14" />刷新</Button>
      <Button size="sm" variant="ghost" :aria-expanded="inspectorOpen" @click="inspectorOpen = !inspectorOpen"><PanelRight :size="14" />详情</Button>
    </header>
    <form class="traffic-filters" @submit.prevent="applySearch">
      <Input v-model="searchDraft" aria-label="搜索 URL、Host 或路径" placeholder="搜索 URL、Host 或路径" />
      <Button type="submit" size="sm" variant="secondary">搜索</Button>
      <Select aria-label="筛选方法" :model-value="queryText('method') || '__all__'" :options="methodOptions" @update:model-value="setFilter('method', $event)" />
      <Select aria-label="筛选状态码" :model-value="queryText('status_family') || '__all__'" :options="statusOptions" @update:model-value="setFilter('status_family', $event)" />
      <Popover align="end" size="md">
        <template #trigger><Button size="sm" variant="ghost">更多筛选<span v-if="extraFilterCount"> · {{ extraFilterCount }}</span></Button></template>
        <div class="traffic-extra-filters" role="group" aria-label="更多流量筛选">
          <label>采集完整性<Select aria-label="筛选采集完整性" :model-value="queryText('state') || '__all__'" :options="stateOptions" @update:model-value="setFilter('state', $event)" /></label>
          <label>关联<Select aria-label="筛选关联" :model-value="queryText('associated') || '__all__'" :options="associationOptions" @update:model-value="setFilter('associated', $event)" /></label>
          <label>TaskRun<Input v-model="taskRunDraft" aria-label="筛选 TaskRun" @keydown.enter.prevent.stop="applySearch" /></label>
          <label>精确状态码<Input v-model="statusCodeDraft" type="number" min="100" max="999" aria-label="精确状态码" @keydown.enter.prevent.stop="applySearch" /></label>
          <label>采集错误<Select aria-label="筛选采集错误" :model-value="queryText('error_code') || '__all__'" :options="errorOptions" @update:model-value="setFilter('error_code', $event)" /></label>
          <Button size="sm" variant="secondary" @click="applySearch">应用</Button>
        </div>
      </Popover>
      <Button size="sm" variant="ghost" :disabled="!canClearFilters" @click="clearFilters">清除筛选</Button>
    </form>
    <p v-if="paginationNotice" class="traffic-notice" role="status">{{ paginationNotice }}</p>
    <p v-if="projectError" class="traffic-notice" role="alert">项目列表读取失败，仍可查看当前范围。<Button size="sm" variant="ghost" @click="loadProjects">重试</Button></p>
    <p v-if="error" class="traffic-notice" role="alert">{{ error }}<Button size="sm" variant="ghost" @click="retry">重试</Button><Button v-if="cursor" size="sm" variant="ghost" @click="goLatest">返回最新页</Button></p>
    <p v-if="scopeProjectId && !scopesLoading" class="traffic-scope-summary">{{ scopeLabel }}</p>
    <div class="traffic-body" :class="{ 'traffic-body--inspector': inspectorOpen }">
      <div ref="splitElement" class="traffic-main" :style="{ gridTemplateRows: `minmax(150px, ${splitRatio}fr) 8px minmax(180px, ${100 - splitRatio}fr)` }">
        <div class="traffic-list">
          <DataTable
            :rows="page?.items || []" :columns="columns" :row-key="rowKey" caption="HTTP 流量"
            density="traffic" resizable-columns :loading="loading && !page" :empty-label="error ? '列表未能读取' : hasFilters ? '没有匹配当前筛选的流量' : '尚无流量记录'"
            :is-row-active="isSelected" active-row-aria="current" :row-attrs="rowAttrs"
          >
            <template #cell-time="{ row }"><span class="traffic-secondary" :title="row.received_at">{{ dateTime(row.received_at) }}</span></template>
            <template #cell-project="{ row }"><span class="traffic-project" :title="row.project_title">{{ row.project_title }}</span></template>
            <template #cell-method="{ row }"><span class="traffic-method">{{ row.method }}</span></template>
            <template #cell-target="{ row }"><button type="button" class="traffic-target" :title="row.url" :aria-label="`阅读 ${row.method} ${row.host}${row.path}`" @click.stop="selectFlow(row)"><span v-if="row.address_issue" class="traffic-warning">地址待确认 · </span><span class="traffic-secondary">{{ row.host }}</span><span>{{ row.path }}</span></button></template>
            <template #cell-state="{ row }"><span :class="{ 'traffic-secondary': row.state === 'complete', 'traffic-warning': row.state !== 'complete' }">{{ stateLabels[row.state] }}</span><span v-if="row.error_code" class="traffic-warning"> · {{ row.error_code }}</span></template>
            <template #cell-bytes="{ row }"><span class="traffic-secondary">{{ capturedBytes(row) }} B</span><span v-if="syncLabel(row) === '待同步'" class="traffic-warning"> · 待同步</span></template>
            <template #cell-associations="{ row }"><button v-if="row.associations.vulnerability_count" type="button" class="traffic-association-link" :aria-label="`查看 ${row.method} ${row.host}${row.path} 的 ${row.associations.vulnerability_count} 个关联漏洞`" @click.stop="openAssociations(row)">已关联 {{ row.associations.vulnerability_count }}</button><span v-else class="traffic-secondary">未关联</span></template>
          </DataTable>
          <footer class="traffic-pagination">
            <span v-if="page" class="traffic-page-position" role="status">{{ pageNumber === null ? '历史定位' : `第 ${pageNumber} 页` }} · 本页 {{ page.items.length }} 条</span>
            <Button size="sm" variant="ghost" :disabled="!page?.has_previous || loading" @click="goPage(page?.previous_cursor, -1)">上一页</Button>
            <Button size="sm" variant="ghost" :disabled="!page?.has_more || loading" @click="goPage(page?.next_cursor, 1)">下一页</Button>
            <Select aria-label="每页流量数" :model-value="String(filters.limit)" :options="pageSizeOptions" @update:model-value="setFilter('limit', $event)" />
            <Button v-if="hasNew || cursor" size="sm" :variant="hasNew ? 'secondary' : 'ghost'" :disabled="loading" @click="goLatest">{{ hasNew ? '有新流量 · 返回最新页' : '返回最新页' }}</Button>
          </footer>
        </div>
        <div class="traffic-splitter" role="separator" tabindex="0" aria-label="调整列表与阅读器高度" aria-orientation="horizontal" :aria-valuenow="splitRatio" :aria-valuemin="splitBounds.min" :aria-valuemax="splitBounds.max" @pointerdown="startResize" @pointermove="resize" @pointerup="endResize" @pointercancel="endResize" @keydown="resizeKey" />
        <div class="traffic-reading">
          <EvidenceReader v-if="selectedFlowId && selectedProjectId && !accessDenied" :key="selectionKey" ref="reader" :entity-key="selectionKey" :project-id="selectedProjectId" :items="readerItems" :load-preview="loadEvidencePreview" :original-url="originalUrl" :fill-height="true" :auto-refresh="false" @flow-loaded="onFlowLoaded">
            <template #header-context><span v-if="!selectedInPage" class="traffic-selection-context">当前定位流量不在本页结果中</span></template>
            <template #header-actions><Button size="sm" variant="ghost" @click="clearSelection">清除选择</Button></template>
          </EvidenceReader>
          <p v-else-if="accessDenied" class="traffic-notice" role="alert">当前范围不可访问，已停止展示流量正文。<Button v-if="selectedFlowId" size="sm" variant="ghost" @click="clearSelection">清除选择</Button></p>
          <p v-else-if="selectedFlowId" class="traffic-notice" role="alert">定位链接缺少项目，请从流量列表重新选择。<Button size="sm" variant="ghost" @click="clearSelection">清除选择</Button></p>
          <p v-else class="traffic-empty">选择一条流量阅读请求与响应。</p>
        </div>
      </div>
      <aside v-if="inspectorOpen" class="traffic-inspector" aria-label="流量详情">
        <header><h3>流量详情</h3><Button size="sm" variant="ghost" @click="inspectorOpen = false">收起</Button></header>
        <section v-if="selectedFlowId" ref="associationsElement" class="traffic-association-section" aria-label="关联漏洞" tabindex="-1">
          <h4>关联漏洞</h4>
          <nav v-if="selectedDetail?.observations?.length" class="traffic-associations">
            <RouterLink v-for="association in selectedDetail.observations" :key="association.observation_id" :to="{ name: 'Findings', query: { vulnerabilityProjectId: selectedProjectId, vulnerabilityId: association.vulnerability_id, vulnerabilityObservationId: association.observation_id } }">{{ association.vulnerability_id }} · 已关联<span class="traffic-secondary">Observation {{ association.observation_id }}</span></RouterLink>
          </nav>
          <p v-else class="traffic-secondary">{{ selectedDetail ? '未记录已接受的关联发现' : '等待读取当前流量' }}</p>
        </section>
        <p v-else class="traffic-secondary">尚未选择流量</p>
        <template v-if="selectedDetail">
          <dl>
            <dt>项目</dt><dd>{{ selectedProjectId }}</dd>
            <dt>flow / revision</dt><dd>{{ selectedDetail.id }} / {{ selectedDetail.revision }}</dd>
            <template v-if="selectedDetail.location">
              <dt>请求地址</dt><dd>{{ selectedDetail.location.request_url || '未确认' }}</dd>
              <dt>连接地址</dt><dd>{{ selectedDetail.location.connection_host || '未提供' }}</dd>
              <template v-if="selectedDetail.location.address_issue"><dt>地址待确认</dt><dd>{{ selectedDetail.location.address_issue }}</dd></template>
            </template>
            <dt>来源 TaskRun</dt><dd>{{ selectedDetail.scope.task_run_id }}</dd>
            <dt>采集上下文</dt><dd>{{ selectedDetail.scope.id }}</dd>
            <dt>采集 / 可读状态</dt><dd>{{ stateLabels[selectedDetail.source_state] }} / {{ stateLabels[selectedDetail.state] }}</dd>
            <dt>首次接收</dt><dd>{{ selectedDetail.received_at ? dateTime(selectedDetail.received_at) : '未提供' }}</dd>
            <template v-for="side in sides" :key="side.value"><dt>{{ side.label }}正文</dt><dd>{{ bodyStatusLabels[selectedDetail.sync.bodies[side.value].status] }} · 已接收 {{ selectedDetail.sync.bodies[side.value].accepted_bytes ?? 0 }} B / 已采集 {{ selectedDetail.sync.bodies[side.value].captured_bytes ?? 0 }} B</dd></template>
            <template v-if="selectedDetail.error"><dt>采集错误</dt><dd>{{ selectedDetail.error.code }}<span v-if="selectedDetail.error.message"> · {{ selectedDetail.error.message }}</span></dd></template>
          </dl>
        </template>
        <template v-if="scopeProjectId">
          <h4>项目采集状态</h4>
          <p v-if="!scopes.length" class="traffic-secondary">{{ scopeLabel }}</p>
          <details v-for="scope in scopes" :key="scope.id">
            <summary>{{ trafficScopeLabel(scope) }} · {{ scope.task_run_id }}</summary>
            <dl><dt>上下文</dt><dd>{{ scope.id }}</dd><dt>最近更新</dt><dd>{{ scope.status_updated_at ? dateTime(scope.status_updated_at) : '未提供' }}</dd><dt>待同步</dt><dd>{{ pendingLabel(scope) }}</dd><template v-for="axis in stopAxes" :key="axis.key"><dt>{{ axis.label }}</dt><dd>{{ booleanLabel(scope.status?.[axis.key]) }}</dd></template><template v-if="scope.status?.code"><dt>诊断</dt><dd>{{ scope.status.code }}</dd></template><template v-if="scope.status?.failure_code"><dt>历史故障</dt><dd>{{ scope.status.failure_code }}</dd></template></dl>
          </details>
          <p v-if="scopesTruncated" class="traffic-secondary">仅展示前 100 个采集上下文，不能据此判断整个项目已停止。</p>
        </template>
      </aside>
    </div>
  </section>
</template>

<script lang="ts">
import type { Router } from 'vue-router'

// 搜索原文只驻留当前应用内存；路由仅携带随机身份，整页刷新后不再可恢复。
const searchContexts = new WeakMap<Router, Map<string, string>>()
</script>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Pause, Play, RefreshCw, PanelRight } from '@lucide/vue'
import { ApiError, api, fetchArtifactPreview } from '@/api/client'
import { getTrafficScopes, listTraffic, type TrafficFilters, type TrafficPage, type TrafficScopeStatus, type TrafficSummary } from '@/api/traffic'
import type { EvidenceItem, EvidenceRef, ProjectSummary, TrafficFlowDetail, TrafficSide, TrafficState } from '@/api/types'
import { Button, Input, Popover, Select } from '@/components/ui'
import DataTable, { type DataTableColumn, type DataTableRowAttrs } from '@/components/patterns/DataTable.vue'
import EvidenceReader from '@/components/patterns/EvidenceReader.vue'
import { dateTime } from '@/lib/format'
import { trafficScopeLabel } from './trafficStatus'

const route = useRoute()
const router = useRouter()
function queryText(key: string) { return typeof route.query[key] === 'string' ? route.query[key] as string : '' }
const fixedProjectId = computed(() => route.name === 'WorkspaceTraffic' ? String(route.params.id || '') : '')
const scopeProjectId = computed(() => fixedProjectId.value || queryText('project'))
const selectedFlowId = computed(() => queryText('flow'))
const selectedProjectId = computed(() => fixedProjectId.value || queryText('flow_project') || queryText('project'))
const selectionKey = computed(() => JSON.stringify([selectedProjectId.value, selectedFlowId.value]))
const sourceVulnerability = computed(() => queryText('vulnerabilityId') && queryText('vulnerabilityProjectId') ? {
  name: 'Findings', query: {
    vulnerabilityId: queryText('vulnerabilityId'),
    vulnerabilityProjectId: queryText('vulnerabilityProjectId'),
    vulnerabilityObservationId: queryText('vulnerabilityObservationId') || undefined,
  },
} : null)
const reader = ref<{ refreshFlow: () => Promise<void> } | null>(null)
const selectedDetail = ref<TrafficFlowDetail | null>(null)
watch(selectionKey, () => { selectedDetail.value = null }, { flush: 'sync' })
const readerItems = computed<EvidenceItem[]>(() => selectedFlowId.value ? [{ id: selectedFlowId.value, kind: 'http', flow_id: selectedFlowId.value, role: 'primary', title: 'HTTP 流量', source: 'capture' }] : [])
function onFlowLoaded(detail: TrafficFlowDetail) {
  if (detail.project_id === selectedProjectId.value && detail.id === selectedFlowId.value) selectedDetail.value = detail
}
function loadEvidencePreview(reference: EvidenceRef, signal: AbortSignal) { return fetchArtifactPreview(selectedProjectId.value, reference.artifact_id, signal) }
function originalUrl(reference: EvidenceRef) { return `/projects/${encodeURIComponent(selectedProjectId.value)}/artifacts/${encodeURIComponent(reference.artifact_id)}/content` }

const stateLabels: Record<TrafficState, string> = { receiving: '接收中', complete: '完整', partial: '部分采集', failed: '采集失败' }
const bodyStatusLabels = { available: '可读', absent: '未记录', pending: '待同步', receiving: '同步中' }
const sides: { value: TrafficSide; label: string }[] = [{ value: 'request', label: '请求' }, { value: 'response', label: '响应' }]
const stopAxes = [{ key: 'control_stopped', label: '控制已停止' }, { key: 'test_stopped', label: '测试已停止' }, { key: 'collector_stopped', label: '采集已停止' }, { key: 'upload_complete', label: '上传已完成' }] as const
function booleanLabel(value: boolean | null | undefined) { return value === true ? '是' : value === false ? '否' : '未确认' }
function pendingLabel(scope: TrafficScopeStatus) {
  return `${scope.status?.pending == null ? '条数未知' : `${scope.status.pending} 条`} / ${scope.status?.pending_bytes == null ? '字节数未知' : `${scope.status.pending_bytes} B`}`
}
const methodOptions = [{ value: '__all__', label: '全部方法' }, ...['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'CONNECT'].map((value) => ({ value, label: value }))]
const statusOptions = [{ value: '__all__', label: '全部状态码' }, ...Array.from({ length: 9 }, (_, index) => ({ value: `${index + 1}xx`, label: `${index + 1}xx` }))]
const stateOptions = [{ value: '__all__', label: '全部采集状态' }, ...Object.entries(stateLabels).map(([value, label]) => ({ value, label }))]
const associationOptions = [{ value: '__all__', label: '全部关联' }, { value: 'true', label: '已关联' }, { value: 'false', label: '未关联' }]
const errorOptions = [{ value: '__all__', label: '全部错误状态' }, { value: 'transport_error', label: '传输错误' }, { value: 'collector_stopped', label: '采集器停止' }]
const pageSizeOptions = [50, 100, 200].map((value) => ({ value: String(value), label: `${value} 条 / 页` }))
const searches = searchContexts.get(router) ?? new Map<string, string>()
searchContexts.set(router, searches)
const search = computed(() => searches.get(queryText('search_context')) ?? '')
const searchDraft = ref(search.value)
const paginationNotice = ref('')
watch(search, (value) => { searchDraft.value = value })
const taskRunDraft = ref(queryText('task_run'))
const statusCodeDraft = ref(queryText('status_code'))
watch(() => [queryText('task_run'), queryText('status_code')], ([taskRun, code]) => { taskRunDraft.value = taskRun!; statusCodeDraft.value = code! })
const cursor = computed(() => queryText('cursor'))
const filters = computed<TrafficFilters>(() => ({
  project_id: scopeProjectId.value || undefined,
  task_run_id: queryText('task_run') || undefined,
  method: queryText('method') || undefined,
  url: search.value || undefined,
  state: (queryText('state') || undefined) as TrafficState | undefined,
  status_family: queryText('status_family') || undefined,
  status_code: queryText('status_code') ? Number(queryText('status_code')) : undefined,
  error_code: queryText('error_code') || undefined,
  associated: queryText('associated') ? queryText('associated') === 'true' : undefined,
  limit: [50, 100, 200].includes(Number(queryText('limit'))) ? Number(queryText('limit')) : 50,
  cursor: cursor.value || undefined,
}))
const filterKeys = ['task_run', 'method', 'state', 'status_family', 'status_code', 'error_code', 'associated'] as const
const hasFilters = computed(() => Boolean(search.value || (!fixedProjectId.value && queryText('project')) || filterKeys.some(queryText)))
const extraFilterCount = computed(() => ['state', 'associated', 'task_run', 'status_code', 'error_code'].filter(queryText).length)
const canClearFilters = computed(() => Boolean(hasFilters.value || cursor.value || searchDraft.value || taskRunDraft.value || statusCodeDraft.value))
async function clearFilters() {
  const query = { ...route.query }
  // 全局项目筛选可清除，但已选报文仍绑定原项目；项目内入口由路由参数固定作用域。
  if (!fixedProjectId.value && selectedFlowId.value) query.flow_project = selectedProjectId.value
  for (const key of [...filterKeys, 'project', 'cursor', 'search_context']) delete query[key]
  searchDraft.value = ''
  taskRunDraft.value = ''
  statusCodeDraft.value = ''
  paginationNotice.value = ''
  await router.push({ query })
}
function setFilter(key: string, value: string) {
  if (value === '__all__') value = ''
  void router.push({ query: { ...route.query, [key]: value || undefined, cursor: undefined,
    flow_project: !fixedProjectId.value && selectedFlowId.value ? selectedProjectId.value : undefined,
  } })
}
function applySearch() {
  const value = searchDraft.value.trim()
  const context = value ? Array.from(crypto.getRandomValues(new Uint32Array(4)), (part) => part.toString(16)).join('-') : undefined
  if (context) searches.set(context, value)
  paginationNotice.value = ''
  // 查询上下文与 cursor 一次切换，避免旧分页先携带新搜索发出请求。
  void router.push({ query: { ...route.query, search_context: context, task_run: taskRunDraft.value.trim() || undefined, status_code: statusCodeDraft.value || undefined, cursor: undefined } })
}
const paginationScope = computed(() => JSON.stringify({ ...filters.value, cursor: undefined, fixedProjectId: fixedProjectId.value }))
const pageNumber = ref<number | null>(null)
// 只记本次浏览成功访问过的 cursor；不从服务端边界推算全库排名或总页数。
const visitedPages = new Map<string, Map<string, number | null>>()
let pageScope = ''
let pageCursor = ''
function rememberPageInHistory() {
  // 只把页序和不含搜索原文的 opaque cursor 绑定到当前历史条目，离页返回也能恢复。
  router.options.history.replace(route.fullPath, { ...router.options.history.state, trafficCursor: pageCursor, trafficPage: pageNumber.value })
}
let pageNavigation: { scope: string; cursor: string; number: number | null; reset?: boolean } | null = null
function goPage(value: string | null | undefined, direction: -1 | 1) {
  if (!value) return
  pageNavigation = { scope: paginationScope.value, cursor: value, number: pageNumber.value === null ? null : Math.max(1, pageNumber.value + direction) }
  if (cursor.value === value) void refresh()
  else void router.push({ query: { ...route.query, cursor: value } })
}
function goLatest() {
  stopped.value = false
  failures = 0
  pageNavigation = { scope: paginationScope.value, cursor: '', number: 1, reset: true }
  if (cursor.value) void router.push({ query: { ...route.query, cursor: undefined } })
  else void refresh()
}
function isSelected(row: TrafficSummary) { return row.project_id === selectedProjectId.value && row.id === selectedFlowId.value }
function rowKey(row: TrafficSummary) { return JSON.stringify([row.project_id, row.id]) }
function selectFlow(row: TrafficSummary, replace = false) {
  return router[replace ? 'replace' : 'push']({ query: { ...route.query, flow: row.id, flow_project: fixedProjectId.value ? undefined : row.project_id, cleared: undefined } })
}
const associationsElement = ref<HTMLElement | null>(null)
async function openAssociations(row: TrafficSummary) {
  await selectFlow(row)
  if (!isSelected(row)) return
  inspectorOpen.value = true
  await nextTick()
  associationsElement.value?.focus({ preventScroll: true })
  associationsElement.value?.scrollIntoView({ block: 'nearest' })
}
function clearSelection() { void router.push({ query: { ...route.query, flow: undefined, flow_project: undefined, cleared: '1' } }) }
function rowAttrs(row: TrafficSummary, index: number): DataTableRowAttrs {
  return { tabindex: 0, onClick: () => selectFlow(row), onKeydown: (event: KeyboardEvent) => {
    if (event.target !== event.currentTarget) return
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectFlow(row) }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const next = page.value?.items[index + (event.key === 'ArrowDown' ? 1 : -1)]
      if (!next) return
      selectFlow(next)
      const sibling = event.key === 'ArrowDown' ? (event.currentTarget as HTMLElement).nextElementSibling : (event.currentTarget as HTMLElement).previousElementSibling
      ;(sibling as HTMLElement | null)?.focus()
    }
  } }
}
function capturedBytes(row: TrafficSummary) { return sides.reduce((total, side) => total + (row.sync.bodies[side.value].captured_bytes ?? 0), 0) }
function syncLabel(row: TrafficSummary) { return sides.some((side) => ['pending', 'receiving'].includes(row.sync.bodies[side.value].status)) ? '待同步' : '已同步' }
const columns = computed<DataTableColumn<TrafficSummary>[]>(() => [
  { key: 'time', label: '接收时间', width: 170, minWidth: 100 },
  ...(!fixedProjectId.value ? [{ key: 'project', label: '项目', width: 160, minWidth: 80, value: (row: TrafficSummary) => row.project_title }] : []),
  { key: 'method', label: '方法', width: 80, minWidth: 64, value: (row) => row.method }, { key: 'target', label: 'Host / 路径', width: 420, minWidth: 160, flexible: true },
  { key: 'status', label: '状态码', width: 80, minWidth: 72, value: (row) => row.status_code ?? '无响应' },
  { key: 'state', label: '采集', width: 120, minWidth: 80, value: (row) => `${stateLabels[row.state]}${row.error_code ? ` · ${row.error_code}` : ''}` },
  { key: 'bytes', label: '正文大小', width: 120, minWidth: 90, align: 'end', value: (row) => `${capturedBytes(row)} B · ${syncLabel(row)}` },
  { key: 'duration', label: '耗时', width: 100, minWidth: 80, align: 'end', value: (row) => row.finished_at === null ? '未结束' : `${Math.max(0, Math.round((row.finished_at - row.started_at) * 1000))} ms` },
  { key: 'associations', label: '关联', width: 100, minWidth: 80 },
])

const projects = ref<ProjectSummary[]>([])
const projectError = ref(false)
const projectController = new AbortController()
const projectOptions = computed(() => [{ value: '__all__', label: '全部授权项目' }, ...projects.value.map((project) => ({ value: project.id, label: project.title }))])
async function loadProjects() {
  projectError.value = false
  try { projects.value = await api<ProjectSummary[]>('GET', '/projects', undefined, projectController.signal) }
  catch { if (!projectController.signal.aborted) projectError.value = true }
}
const page = ref<TrafficPage | null>(null)
const loading = ref(false)
const error = ref('')
const paused = ref(false)
const hidden = ref(document.hidden)
const stopped = ref(false)
const accessDenied = ref(false)
const lastSuccess = ref('')
const hasNew = ref(false)
const scopes = ref<TrafficScopeStatus[]>([])
const scopesLoading = ref(true)
const scopesTruncated = ref(false)
const inspectorOpen = ref(false)
const selectedInPage = computed(() => page.value?.items.some(isSelected))
const scopeLabel = computed(() => scopes.value.length ? `采集上下文 ${scopes.value.length}${scopesTruncated.value ? '+' : ''} · ${[...new Set(scopes.value.map(trafficScopeLabel))].join(' / ')}` : '尚无采集上下文：可能尚未开始执行，或此历史项目未启用采集。')
const refreshLabel = computed(() => `${stopped.value ? '自动刷新已停止' : hidden.value ? '页面隐藏，已暂停' : paused.value ? '视图刷新已暂停' : '每 3 秒刷新'}${lastSuccess.value ? ` · 最近成功 ${lastSuccess.value}` : ''}`)
let timer: ReturnType<typeof setTimeout> | undefined
let controller: AbortController | null = null
let generation = 0
let failures = 0
let disposed = false
let initialSelection = true
let latestSeen = ''
function latestKey(row?: TrafficSummary) { return row ? JSON.stringify([row.received_at, row.project_id, row.id]) : '' }
function schedule() {
  clearTimeout(timer)
  if (disposed || paused.value || hidden.value || stopped.value) return
  timer = setTimeout(() => void refresh(true), Math.min(30000, 3000 * 2 ** failures))
}
async function refresh(automatic = false) {
  if (disposed || loading.value) return
  clearTimeout(timer)
  if (queryText('search_context') && !searches.has(queryText('search_context'))) {
    paginationNotice.value = '搜索上下文已失效，已返回最新页。'
    pageNumber.value = null
    pageNavigation = null
    await router.replace({ query: { ...route.query, search_context: undefined, cursor: undefined }, state: { trafficCursor: '', trafficPage: null } })
    return
  }
  const currentGeneration = generation
  const requestedScope = paginationScope.value
  const requestedCursor = cursor.value
  const navigation = pageNavigation?.scope === requestedScope && pageNavigation.cursor === requestedCursor ? pageNavigation : null
  const request = new AbortController()
  controller = request
  loading.value = true
  const current = () => !disposed && !request.signal.aborted && currentGeneration === generation && (!automatic || (!hidden.value && !paused.value))
  try {
    const result = await listTraffic(filters.value, fixedProjectId.value || undefined, request.signal)
    if (!current()) return
    const visits = navigation?.reset ? new Map<string, number | null>() : visitedPages.get(requestedScope) ?? new Map<string, number | null>()
    const historyState = router.options.history.state
    const historyNumber = historyState.trafficCursor === requestedCursor && typeof historyState.trafficPage === 'number' ? historyState.trafficPage : null
    pageNumber.value = navigation ? navigation.number : visits.get(requestedCursor) ?? (requestedCursor ? historyNumber : 1)
    visits.set(requestedCursor, pageNumber.value)
    visitedPages.set(requestedScope, visits)
    pageScope = requestedScope
    pageCursor = requestedCursor
    rememberPageInHistory()
    if (navigation?.reset) { latestSeen = ''; hasNew.value = false }
    if (navigation === pageNavigation) pageNavigation = null
    page.value = result
    accessDenied.value = false
    error.value = ''
    if (initialSelection) {
      initialSelection = false
      if (!selectedFlowId.value && !queryText('cleared') && result.items[0]) selectFlow(result.items[0], true)
    }
    const newest = cursor.value ? (await listTraffic({ ...filters.value, cursor: undefined, limit: 1 }, fixedProjectId.value || undefined, request.signal)).items[0] : result.items[0]
    if (!current()) return
    const newestKey = latestKey(newest)
    if (latestSeen && newestKey > latestSeen) hasNew.value = true
    if (!latestSeen) latestSeen = newestKey
    if (scopeProjectId.value) {
      const status = await getTrafficScopes(scopeProjectId.value, request.signal)
      if (!current()) return
      scopes.value = status.items
      scopesTruncated.value = status.has_more
      scopesLoading.value = false
    }
    // 终态流量仍可能新增 Observation 关联；阅读器按正文身份决定是否重读。
    await reader.value?.refreshFlow()
    if (!current()) return
    failures = 0
    lastSuccess.value = new Date().toLocaleTimeString('zh-CN')
  } catch (cause) {
    if (!current() || (cause instanceof Error && cause.name === 'AbortError')) return
    failures += 1
    const status = cause instanceof ApiError ? cause.status : undefined
    stopped.value = status === 401 || status === 403 || status === 422 || failures >= 5
    error.value = status === 401 ? '登录已失效，已停止自动刷新。' : status === 403 ? '无权读取当前范围，已停止自动刷新。' : status === 422 ? '分页定位或筛选已失效，请返回最新页或调整筛选。' : `刷新失败，${stopped.value ? '自动重试已停止' : '将延迟重试'}；保留上次成功结果。`
    if (status === 401 || status === 403) { accessDenied.value = true; page.value = null; selectedDetail.value = null; scopes.value = [] }
  } finally {
    if (currentGeneration === generation) { loading.value = false; controller = null; schedule() }
  }
}
function retry() { stopped.value = false; failures = 0; void refresh() }
function togglePause() { paused.value = !paused.value; if (paused.value) clearTimeout(timer); else schedule() }
function visibilityChanged() { hidden.value = document.hidden; if (hidden.value) clearTimeout(timer); else schedule() }
watch(() => JSON.stringify([filters.value, queryText('search_context')]), () => {
  generation += 1
  controller?.abort()
  loading.value = false
  if (pageScope !== paginationScope.value) { page.value = null; pageNumber.value = null }
  error.value = ''
  stopped.value = false
  failures = 0
  void refresh()
}, { immediate: true })
watch(() => route.fullPath, () => {
  if (page.value && pageScope === paginationScope.value && pageCursor === cursor.value) rememberPageInHistory()
})
watch(() => JSON.stringify({ ...filters.value, cursor: undefined }), () => { latestSeen = ''; hasNew.value = false })
watch(scopeProjectId, () => { scopes.value = []; scopesLoading.value = true; scopesTruncated.value = false }, { flush: 'sync' })
watch(fixedProjectId, () => { initialSelection = true })

const splitElement = ref<HTMLElement | null>(null)
// 紧凑头与 30px 行高下，默认兼顾八行列表和十六行以上报文；仍可独立拖动。
const splitRatio = ref(42)
const splitHeight = ref(0)
const splitBounds = computed(() => {
  const available = Math.max(330, splitHeight.value - 8)
  return { min: Math.max(15, 150 / available * 100), max: Math.min(70, (available - 180) / available * 100) }
})
let splitObserver: ResizeObserver | undefined
let dragPointer: number | null = null
function clampSplit(value: number) { splitRatio.value = Math.min(splitBounds.value.max, Math.max(splitBounds.value.min, value)) }
function startResize(event: PointerEvent) { dragPointer = event.pointerId; (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId); event.preventDefault() }
function resize(event: PointerEvent) {
  if (dragPointer !== event.pointerId || !splitElement.value) return
  const rect = splitElement.value.getBoundingClientRect()
  if (rect.height) clampSplit((event.clientY - rect.top - 4) / Math.max(1, rect.height - 8) * 100)
}
function endResize() { dragPointer = null }
function resizeKey(event: KeyboardEvent) {
  if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  clampSplit(event.key === 'Home' ? 15 : event.key === 'End' ? 70 : splitRatio.value + (event.key === 'ArrowUp' ? -5 : 5))
}
onMounted(() => {
  document.addEventListener('visibilitychange', visibilityChanged)
  if (!fixedProjectId.value) void loadProjects()
  splitObserver = new ResizeObserver(([entry]) => {
    if (!entry) return
    splitHeight.value = entry.contentRect.height
    // 缩小容器后仍保留表头、分页和至少一行，同时不挤没阅读区。
    clampSplit(splitRatio.value)
  })
  if (splitElement.value) splitObserver.observe(splitElement.value)
})
onUnmounted(() => { disposed = true; generation += 1; clearTimeout(timer); controller?.abort(); projectController.abort(); splitObserver?.disconnect(); document.removeEventListener('visibilitychange', visibilityChanged) })
</script>

<style scoped src="./TrafficWorkbench.css"></style>
