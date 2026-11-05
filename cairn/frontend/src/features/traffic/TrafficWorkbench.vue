<template>
  <section class="traffic-workbench" aria-label="HTTP 流量工作台">
    <h1 class="sr-only">流量</h1>
    <header class="traffic-toolbar">
      <RouterLink v-if="sourceVulnerability" class="traffic-return" :to="sourceVulnerability"><ArrowLeft :size="18" />返回漏洞</RouterLink>
      <form v-show="!readerExpanded" class="traffic-filters" aria-label="筛选流量" @submit.prevent="applySearch">
      <div class="traffic-search">
        <Input v-model="searchDraft" aria-label="搜索 URL、Host 或路径" placeholder="搜索 URL、Host 或路径" />
        <Button type="submit" size="sm" variant="ghost" aria-label="搜索流量"><Search :size="16" /></Button>
      </div>
      <Select v-if="!fixedProjectId" class="traffic-filter" :class="{ 'traffic-filter--active': queryText('project') }" aria-label="筛选项目" :model-value="queryText('project') || '__all__'" :options="projectOptions" @update:model-value="setFilter('project', $event)" />
      <Select class="traffic-filter" :class="{ 'traffic-filter--active': queryText('method') }" aria-label="筛选方法" :model-value="queryText('method') || '__all__'" :options="methodOptions" @update:model-value="setFilter('method', $event)" />
      <Select class="traffic-filter" :class="{ 'traffic-filter--active': queryText('status_family') }" aria-label="筛选状态码" :model-value="queryText('status_family') || '__all__'" :options="statusOptions" @update:model-value="setFilter('status_family', $event)" />
      <Popover align="end" size="md">
        <template #trigger><Button class="traffic-filter" :class="{ 'traffic-filter--active': extraFilterCount }" size="sm" variant="secondary"><SlidersHorizontal :size="15" />更多筛选<span v-if="extraFilterCount"> · {{ extraFilterCount }}</span></Button></template>
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
      <div class="traffic-actions">
        <span v-if="readerExpanded" class="traffic-refresh-state" role="status">{{ refreshLabel }}</span>
        <Button variant="secondary" :aria-pressed="paused" @click="togglePause"><Pause v-if="!paused" :size="18" /><Play v-else :size="18" />{{ paused ? '恢复刷新' : '暂停刷新' }}</Button>
        <Button ref="metadataButton" variant="secondary" @click="openMetadata($event)"><Info :size="18" />采集信息</Button>
        <Button :loading="loading" @click="retry"><RefreshCw :size="18" />刷新</Button>
      </div>
    </header>
    <p v-if="paginationNotice" class="traffic-notice" role="status">{{ paginationNotice }}</p>
    <p v-if="projectError" class="traffic-notice" role="alert">项目列表读取失败，仍可查看当前范围。<Button size="sm" variant="ghost" @click="loadProjects">重试</Button></p>
    <p v-if="error" class="traffic-notice" role="alert">{{ error }}<Button size="sm" variant="ghost" @click="retry">重试</Button><Button v-if="requestedPage > 1" size="sm" variant="ghost" @click="goLatest">返回最新页</Button></p>
    <div class="traffic-body">
      <div ref="splitElement" class="traffic-main" :style="{ gridTemplateRows: readerExpanded ? 'minmax(0, 1fr)' : !selectedFlowId ? 'minmax(0, 1fr) auto' : `minmax(200px, ${splitRatio}fr) 12px minmax(240px, ${100 - splitRatio}fr)` }">
        <div v-show="!readerExpanded" class="traffic-list">
          <TrafficTable :rows="page?.items || []" :selected-id="selectedFlowId" :selected-project-id="selectedProjectId" :loading="loading" :empty-label="error ? '列表未能读取' : hasFilters ? '没有匹配当前筛选的流量' : '尚无流量记录'" @select="selectFlow($event)" @associations="openAssociations" />
          <Pagination v-if="page" :page="page.page" :limit="page.limit" :total="page.total" :total-pages="page.total_pages" :loading="loading" @page="goPage" @limit="setFilter('limit', String($event))">
            <span class="traffic-refresh-state">{{ refreshLabel }}</span>
            <Button v-if="hasNew || page.page > 1" size="sm" :variant="hasNew ? 'secondary' : 'ghost'" :disabled="loading" @click="goLatest">{{ hasNew ? '有新流量 · 返回最新页' : '返回最新页' }}</Button>
          </Pagination>
        </div>
        <div v-show="!readerExpanded && selectedFlowId" class="traffic-splitter" role="separator" tabindex="0" aria-label="调整列表与阅读器高度" aria-orientation="horizontal" :aria-valuenow="splitRatio" :aria-valuemin="splitBounds.min" :aria-valuemax="splitBounds.max" @pointerdown="startResize" @pointermove="resize" @pointerup="endResize" @pointercancel="endResize" @keydown="resizeKey" />
        <div class="traffic-reading">
          <p v-if="selectedDetail?.error" class="traffic-notice" role="alert">采集错误：{{ selectedDetail.error.code }}<span v-if="selectedDetail.error.message">{{ selectedDetail.error.message }}</span></p>
          <p v-if="selectedDetail?.location?.address_issue" class="traffic-notice" role="alert">地址待确认：{{ selectedDetail.location.address_issue }}</p>
          <EvidenceReader v-if="selectedFlowId && selectedProjectId && !accessDenied" :key="selectionKey" ref="reader" :entity-key="selectionKey" :project-id="selectedProjectId" :items="readerItems" :load-preview="loadEvidencePreview" :original-url="originalUrl" :fill-height="true" :auto-refresh="false" @flow-loaded="onFlowLoaded" @expanded-change="readerExpanded = $event">
            <template #header-context><span v-if="!selectedInPage" class="traffic-selection-context">当前定位流量不在本页结果中</span></template>
            <template #header-actions><Button size="sm" variant="ghost" @click="clearSelection">清除选择</Button></template>
          </EvidenceReader>
          <p v-else-if="accessDenied" class="traffic-notice" role="alert">当前范围不可访问，已停止展示流量正文。<Button v-if="selectedFlowId" size="sm" variant="ghost" @click="clearSelection">清除选择</Button></p>
          <p v-else-if="selectedFlowId" class="traffic-notice" role="alert">定位链接缺少项目，请从流量列表重新选择。<Button size="sm" variant="ghost" @click="clearSelection">清除选择</Button></p>
          <p v-else class="traffic-empty">未选择流量</p>
        </div>
      </div>
    </div>
    <Dialog v-model:open="metadataOpen" title="采集信息" size="md" @open-auto-focus="focusMetadata" @close-auto-focus="restoreMetadataFocus">
      <div class="traffic-metadata">
        <section v-if="selectedFlowId" ref="associationsElement" class="traffic-association-section" aria-label="关联漏洞" tabindex="-1">
          <h4>关联漏洞</h4>
          <nav v-if="selectedDetail?.observations?.length" class="traffic-associations">
            <RouterLink v-for="association in selectedDetail.observations" :key="`${association.vulnerability_id}:${association.observation_id || 'record'}`" :to="{ name: 'Findings', query: { vulnerabilityProjectId: selectedProjectId, vulnerabilityId: association.vulnerability_id, vulnerabilityObservationId: association.observation_id || undefined } }">{{ association.vulnerability_id }} · 已关联<span class="traffic-secondary">{{ association.observation_id ? `Observation ${association.observation_id}` : '漏洞记录补充' }}</span></RouterLink>
          </nav>
          <p v-else class="traffic-secondary">{{ selectedDetail ? '未记录已接受的关联发现' : '等待读取当前流量' }}</p>
        </section>
        <p v-else class="traffic-secondary">尚未选择流量</p>
        <template v-if="selectedDetail">
          <h4>流量来源</h4>
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
          <p class="traffic-scope-summary">{{ scopesLoading ? '正在读取采集状态…' : scopeLabel }}</p>
          <details v-for="scope in scopes" :key="scope.id">
            <summary>{{ trafficScopeLabel(scope) }} · {{ scope.task_run_id }}</summary>
            <dl><dt>上下文</dt><dd>{{ scope.id }}</dd><dt>最近更新</dt><dd>{{ scope.status_updated_at ? dateTime(scope.status_updated_at) : '未提供' }}</dd><dt>待同步</dt><dd>{{ pendingLabel(scope) }}</dd><template v-for="axis in stopAxes" :key="axis.key"><dt>{{ axis.label }}</dt><dd>{{ booleanLabel(scope.status?.[axis.key]) }}</dd></template><template v-if="scope.status?.code"><dt>诊断</dt><dd>{{ scope.status.code }}</dd></template><template v-if="scope.status?.failure_code"><dt>历史故障</dt><dd>{{ scope.status.failure_code }}</dd></template></dl>
          </details>
          <p v-if="scopesTruncated" class="traffic-secondary">仅展示前 100 个采集上下文，不能据此判断整个项目已停止。</p>
        </template>
      </div>
      <template #footer><Button variant="secondary" @click="metadataOpen = false">返回流量</Button></template>
    </Dialog>
  </section>
</template>

<script lang="ts">
import type { Router } from 'vue-router'

// 搜索原文只驻留当前应用内存；路由仅携带随机身份，整页刷新后不再可恢复。
const searchContexts = new WeakMap<Router, Map<string, string>>()
</script>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Info, Pause, Play, RefreshCw, Search, SlidersHorizontal } from '@lucide/vue'
import { ApiError, api, fetchArtifactPreview } from '@/api/client'
import { getTrafficScopes, listTraffic, type TrafficFilters, type TrafficPage, type TrafficScopeStatus, type TrafficSummary } from '@/api/traffic'
import type { EvidenceItem, EvidenceRef, ProjectSummary, TrafficFlowDetail, TrafficSide, TrafficState } from '@/api/types'
import { Button, Dialog, Input, Popover, Select } from '@/components/ui'
import TrafficTable from './TrafficTable.vue'
import Pagination from '@/components/patterns/Pagination.vue'
import EvidenceReader from '@/components/patterns/EvidenceReader.vue'
import { dateTime } from '@/lib/format'
import { toLogicalPixels } from '@/lib/uiScale'
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
// 填满工作区时由父级让出列表空间，保留原分隔比例与表格实例。
const readerExpanded = ref(false)
watch(selectionKey, () => { selectedDetail.value = null; readerExpanded.value = false }, { flush: 'sync' })
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
const searches = searchContexts.get(router) ?? new Map<string, string>()
searchContexts.set(router, searches)
const search = computed(() => searches.get(queryText('search_context')) ?? '')
const searchDraft = ref(search.value)
const paginationNotice = ref('')
watch(search, (value) => { searchDraft.value = value })
const taskRunDraft = ref(queryText('task_run'))
const statusCodeDraft = ref(queryText('status_code'))
watch(() => [queryText('task_run'), queryText('status_code')], ([taskRun, code]) => { taskRunDraft.value = taskRun!; statusCodeDraft.value = code! })
const requestedPage = computed(() => /^[1-9]\d*$/.test(queryText('page')) && Number.isSafeInteger(Number(queryText('page'))) ? Number(queryText('page')) : 1)
const filters = computed<TrafficFilters>(() => ({
  project_id: scopeProjectId.value || undefined,
  task_run_id: queryText('task_run') || undefined,
  vulnerability_id: queryText('vulnerability_id') || undefined,
  observation_id: queryText('observation_id') || undefined,
  method: queryText('method') || undefined,
  url: search.value || undefined,
  state: (queryText('state') || undefined) as TrafficState | undefined,
  status_family: queryText('status_family') || undefined,
  status_code: queryText('status_code') ? Number(queryText('status_code')) : undefined,
  error_code: queryText('error_code') || undefined,
  associated: queryText('associated') ? queryText('associated') === 'true' : undefined,
  limit: [50, 100, 200].includes(Number(queryText('limit'))) ? Number(queryText('limit')) : 50,
  page: requestedPage.value,
}))
const filterKeys = ['task_run', 'method', 'state', 'status_family', 'status_code', 'error_code', 'associated', 'vulnerability_id', 'observation_id'] as const
const hasFilters = computed(() => Boolean(search.value || (!fixedProjectId.value && queryText('project')) || filterKeys.some(queryText)))
const extraFilterCount = computed(() => ['state', 'associated', 'task_run', 'status_code', 'error_code'].filter(queryText).length)
const canClearFilters = computed(() => Boolean(hasFilters.value || requestedPage.value > 1 || searchDraft.value || taskRunDraft.value || statusCodeDraft.value))
async function clearFilters() {
  const query = { ...route.query }
  // 全局项目筛选可清除，但已选报文仍绑定原项目；项目内入口由路由参数固定作用域。
  if (!fixedProjectId.value && selectedFlowId.value) query.flow_project = selectedProjectId.value
  for (const key of [...filterKeys, 'project', 'page', 'cursor', 'search_context']) delete query[key]
  searchDraft.value = ''
  taskRunDraft.value = ''
  statusCodeDraft.value = ''
  paginationNotice.value = ''
  await router.push({ query })
}
function setFilter(key: string, value: string) {
  if (value === '__all__') value = ''
  void router.push({ query: { ...route.query, [key]: value || undefined, page: undefined,
    flow_project: !fixedProjectId.value && selectedFlowId.value ? selectedProjectId.value : undefined,
  } })
}
function applySearch() {
  const value = searchDraft.value.trim()
  const context = value ? Array.from(crypto.getRandomValues(new Uint32Array(4)), (part) => part.toString(16)).join('-') : undefined
  if (context) searches.set(context, value)
  paginationNotice.value = ''
  // 查询上下文与页码一次切换，避免旧页先携带新搜索发出请求。
  void router.push({ query: { ...route.query, search_context: context, task_run: taskRunDraft.value.trim() || undefined, status_code: statusCodeDraft.value || undefined, page: undefined } })
}
const paginationScope = computed(() => JSON.stringify({ ...filters.value, page: undefined, fixedProjectId: fixedProjectId.value }))
let successfulQuery = ''
function goPage(value: number) {
  if (loading.value) return
  if (filters.value.page === value) void refresh()
  else void router.push({ query: { ...route.query, page: value === 1 ? undefined : String(value) } })
}
function goLatest() {
  stopped.value = false
  failures = 0
  goPage(1)
}
function isSelected(row: TrafficSummary) { return row.project_id === selectedProjectId.value && row.id === selectedFlowId.value }
function selectFlow(row: TrafficSummary, replace = false) {
  return router[replace ? 'replace' : 'push']({ query: { ...route.query, flow: row.id, flow_project: fixedProjectId.value ? undefined : row.project_id, cleared: undefined } })
}
const associationsElement = ref<HTMLElement | null>(null)
const metadataButton = ref<InstanceType<typeof Button> | null>(null)
let metadataReturnFocus: HTMLElement | null = null
function openMetadata(event: Event) {
  metadataReturnFocus = event.currentTarget as HTMLElement
  metadataOpen.value = true
}
function focusMetadata(event: Event) {
  if (!associationsElement.value) return
  event.preventDefault()
  associationsElement.value.focus({ preventScroll: true })
}
function restoreMetadataFocus(event: Event) {
  const target = metadataReturnFocus?.isConnected ? metadataReturnFocus : metadataButton.value?.$el
  if (!(target instanceof HTMLElement)) return
  event.preventDefault()
  target.focus({ preventScroll: true })
}
async function openAssociations(row: TrafficSummary, event: Event) {
  const trigger = event.currentTarget as HTMLElement
  await selectFlow(row)
  if (!isSelected(row)) return
  metadataReturnFocus = trigger
  metadataOpen.value = true
}
function clearSelection() { void router.push({ query: { ...route.query, flow: undefined, flow_project: undefined, cleared: '1' } }) }

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
watch(accessDenied, (denied) => { if (denied) readerExpanded.value = false }, { flush: 'sync' })
const lastSuccess = ref('')
const hasNew = ref(false)
const scopes = ref<TrafficScopeStatus[]>([])
const scopesLoading = ref(true)
const scopesTruncated = ref(false)
const metadataOpen = ref(false)
const selectedInPage = computed(() => page.value?.items.some(isSelected))
const scopeLabel = computed(() => scopes.value.length ? `采集上下文 ${scopes.value.length}${scopesTruncated.value ? '+' : ''} · ${[...new Set(scopes.value.map(trafficScopeLabel))].join(' / ')}` : '尚无采集上下文：可能尚未开始执行，或此历史项目未启用采集。')
const refreshLabel = computed(() => `${stopped.value ? '自动刷新已停止' : hidden.value ? '页面隐藏，已暂停' : paused.value ? '视图刷新已暂停' : requestedPage.value > 1 ? '历史页保持 · 每 3 秒检查新流量' : '每 3 秒刷新'}${lastSuccess.value ? ` · 最近检查 ${lastSuccess.value}` : ''}`)
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
  if (disposed || controller) return
  clearTimeout(timer)
  if (queryText('cursor') || (queryText('search_context') && !searches.has(queryText('search_context')))) {
    paginationNotice.value = queryText('cursor') ? '原分页定位已失效，已返回第 1 页。' : '搜索上下文已失效，已返回第 1 页。'
    const missingSearch = queryText('search_context') && !searches.has(queryText('search_context'))
    await router.replace({ query: { ...route.query, cursor: undefined, page: undefined, ...(missingSearch ? { search_context: undefined } : {}) } })
    return
  }
  const currentGeneration = generation
  const probeOnly = automatic && requestedPage.value > 1
  const requestedFilters = { ...filters.value }
  const request = new AbortController()
  controller = request
  // 后台检查不禁用分页输入，避免丢失焦点；显式翻页由查询监听取消旧请求。
  loading.value = !automatic
  const current = () => !disposed && !request.signal.aborted && currentGeneration === generation && (!automatic || (!hidden.value && !paused.value))
  try {
    // 历史页只探测首页，不用新的 OFFSET 结果替换正在阅读的行，也不混入探测计数。
    const result = await listTraffic(probeOnly ? { ...requestedFilters, page: 1, limit: 1 } : requestedFilters, fixedProjectId.value || undefined, request.signal)
    if (!current()) return
    if (!probeOnly) {
      page.value = result
      successfulQuery = JSON.stringify({ ...requestedFilters, page: result.page })
      if (result.page !== requestedFilters.page) {
        paginationNotice.value = result.total_pages ? `结果页数已变化，已定位到第 ${result.page} 页。` : '当前筛选无结果，已返回第 1 页。'
        accessDenied.value = false
        error.value = ''
        failures = 0
        lastSuccess.value = new Date().toLocaleTimeString('zh-CN')
        const first = initialSelection && !selectedFlowId.value && !queryText('cleared') ? result.items[0] : undefined
        initialSelection = false
        await router.replace({ query: { ...route.query, page: result.page === 1 ? undefined : String(result.page), ...(first ? { flow: first.id, flow_project: fixedProjectId.value ? undefined : first.project_id } : {}) } })
        return
      }
      if (initialSelection) {
        initialSelection = false
        if (!selectedFlowId.value && !queryText('cleared') && result.items[0]) selectFlow(result.items[0], true)
      }
    }
    accessDenied.value = false
    error.value = ''
    if (probeOnly || result.page === 1) {
      const newestKey = latestKey(result.items[0])
      if (probeOnly && latestSeen && newestKey > latestSeen) hasNew.value = true
      if (!probeOnly) hasNew.value = false
      latestSeen = newestKey
    }
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
watch(() => JSON.stringify([filters.value, queryText('search_context'), queryText('cursor')]), () => {
  generation += 1
  controller?.abort()
  controller = null
  loading.value = false
  error.value = ''
  stopped.value = false
  failures = 0
  if (!queryText('cursor') && (!queryText('search_context') || searches.has(queryText('search_context'))) && successfulQuery === JSON.stringify(filters.value)) { schedule(); return }
  void refresh()
}, { immediate: true })
watch(paginationScope, () => { latestSeen = ''; hasNew.value = false })
watch(scopeProjectId, () => { scopes.value = []; scopesLoading.value = true; scopesTruncated.value = false }, { flush: 'sync' })
watch(fixedProjectId, () => { initialSelection = true })

const splitElement = ref<HTMLElement | null>(null)
// 默认以正文阅读为主；列表仍容纳表头、分页及舒适行高，放大时不销毁它。
const splitRatio = ref(34)
const splitHeight = ref(0)
const splitBounds = computed(() => {
  const available = Math.max(440, splitHeight.value - 12)
  return { min: Math.max(15, 200 / available * 100), max: Math.min(70, (available - 240) / available * 100) }
})
let splitObserver: ResizeObserver | undefined
let dragPointer: number | null = null
function clampSplit(value: number) { splitRatio.value = Math.min(splitBounds.value.max, Math.max(splitBounds.value.min, value)) }
function startResize(event: PointerEvent) { dragPointer = event.pointerId; (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId); event.preventDefault() }
function resize(event: PointerEvent) {
  if (dragPointer !== event.pointerId || !splitElement.value) return
  const rect = splitElement.value.getBoundingClientRect()
  if (rect.height) clampSplit((toLogicalPixels(event.clientY - rect.top) - 6) / Math.max(1, toLogicalPixels(rect.height) - 12) * 100)
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
