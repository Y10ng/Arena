<template>
  <section ref="page" class="verification-theme verification-page verification-list-page" aria-label="漏洞复现与复测">
    <h1 class="sr-only">漏洞复现与复测</h1>
    <div class="verification-list-toolbar">
      <span class="verification-filter-count" role="status">{{ data?.total ?? 0 }} 条记录<span v-if="isFetching"> · 同步中…</span></span>
      <div class="verification-actions">
        <IconButton label="刷新复测记录" :loading="isFetching" @click="refresh"><RefreshCw :size="18" /></IconButton>
        <Button variant="secondary" @click="router.push({ name: 'VerificationConfiguration' })">漏洞复测配置</Button>
        <Button data-verification-create @click="openCreate"><template #icon><Plus :size="18" /></template>新建复测</Button>
      </div>
    </div>
    <form class="verification-filters" aria-label="复测筛选" @submit.prevent="applyFilters">
      <Input v-model="draft.source_number" aria-label="来源漏洞编号" placeholder="来源漏洞编号，如 proj_070/v001" />
      <Input id="verification-external" v-model="draft.external_id" aria-label="外部漏洞编号" placeholder="外部漏洞完整编号，精确匹配" />
      <Button type="submit" variant="secondary" size="sm">查询</Button>
      <SearchFilter v-model="draft.outcome" label="结论" clear-value="all" :options="outcomeOptions" @update:model-value="applyFilters" />
      <SearchFilter v-model="draft.mode" label="模式" clear-value="all" :options="modeOptions" @update:model-value="applyFilters" />
      <SearchFilter v-model="draft.execution_status" label="执行状态" clear-value="all" :options="statusOptions" @update:model-value="applyFilters" />
      <Button v-if="filters.source_number || filters.external_id || filters.mode || filters.execution_status || filters.outcome" variant="ghost" size="sm" @click="router.push({ name: 'Verifications' })">清除筛选</Button>
    </form>
    <Alert v-if="actionError" tone="danger" title="操作未完成">{{ actionError }}<template #actions><Button variant="ghost" @click="actionError = ''">关闭提示</Button></template></Alert>
    <Alert v-if="isError && data" tone="warning" title="刷新失败">保留上次取得的记录，最新状态尚未确认。{{ errorMessage(error) }}</Alert>
    <Skeleton v-if="isLoading" aria-label="正在加载复测记录" />
    <ErrorState v-else-if="isError && !data" title="复测列表加载失败" :description="errorMessage(error)" @retry="refresh" />
    <EmptyState v-else-if="!data?.items.length" title="暂无匹配的复测记录" />
    <div v-else class="verification-list-table">
      <p v-if="(filters.execution_status || filters.outcome) && data.items.some((item) => item.purge_state === 'pending')" class="verification-muted" role="status">筛选结果包含清理未完成记录，可在操作中重试。</p>
      <DataTable :rows="data.items" :columns="columns" :row-key="(row) => row.id" caption="复测记录">
        <template #cell-title="{ row }"><div class="verification-row-identity"><RouterLink v-slot="{ href }" :to="detailLocation(row.id)" custom><a :href="href" :title="row.title" @click="openDetail($event, row.id)">{{ row.title }}</a></RouterLink><VulnerabilityNumber v-if="row.input_source" :number="row.input_source.number" /><span v-if="row.external_id" class="verification-muted verification-external-id" :title="row.external_id">{{ row.external_id }}</span></div><div v-if="row.purge_state === 'pending'" role="status">清理未完成，可在操作中重试。{{ row.purge_error }}</div></template>
        <template #cell-source="{ row }"><span v-if="row.purge_state !== 'pending'" class="verification-row-source" :title="row.source.name">{{ row.input_source ? '平台漏洞' : row.source.type === 'integration' ? '外部调用' : '手工资料' }} · {{ row.source.name }}</span><span v-else class="verification-muted">—</span></template>
        <template #cell-mode="{ row }">{{ modeLabels[row.mode] }}</template>
        <template #cell-status="{ row }"><Badge dot :tone="statusTone(row.execution_status)">{{ statusLabels[row.execution_status] }}</Badge></template>
        <template #cell-result="{ row }"><Badge v-if="row.result" dot :tone="outcomeTone(row.result.outcome)">{{ outcomeLabels[row.result.outcome] }}</Badge><span v-else class="verification-muted">{{ row.purge_state === 'pending' ? '—' : row.error ? '执行错误' : '尚无结果' }}</span></template>
        <template #cell-created="{ row }"><span class="verification-muted">{{ dateTime(row.created_at) }}</span></template>
        <template #cell-actions="{ row }"><VerificationActions :request="row" @rerun="openAgain(row.id)" @deleted="afterDeleted" @error="actionError = $event" /></template>
      </DataTable>
    </div>
    <footer class="verification-actions verification-list-footer">
      <span class="verification-muted verification-page-count">共 {{ data?.total ?? 0 }} 条 · 第 {{ Math.floor(offset / limit) + 1 }} 页</span>
      <span v-if="autoPaused" class="verification-muted" role="status">自动更新已暂停，可手动刷新</span>
      <Button variant="secondary" size="sm" :disabled="offset === 0 || isFetching" @click="changePage(offset - limit)">上一页</Button>
      <Button variant="secondary" size="sm" :disabled="!data || offset + limit >= data.total || isFetching" @click="changePage(offset + limit)">下一页</Button>
    </footer>
    <NewVerification v-if="createOpen" :key="authIdentity" @close="closeCreate" />
  </section>
</template>
<script setup lang="ts">
import VulnerabilityNumber from '@/features/vulnerabilities/VulnerabilityNumber.vue'
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { Plus, RefreshCw } from '@lucide/vue'
import { Alert, Badge, Button, EmptyState, IconButton, Input, Skeleton } from '@/components/ui'
import SearchFilter from '@/components/patterns/SearchFilter.vue'
import { DataTable, ErrorState } from '@/components/patterns'
import type { DataTableColumn } from '@/components/patterns/DataTable.vue'
import { listVerificationRequests, type VerificationFilters, type VerificationSummary } from '@/api/verificationRequests'
import { useAuthStore } from '@/stores/auth'
import { dateTime } from '@/lib/format'
import NewVerification from './NewVerification.vue'
import VerificationActions from './VerificationActions.vue'
import { errorMessage, isActiveVerification, modeLabels, statusLabels, statusTone, outcomeLabels, outcomeTone, verificationListQuery } from './verificationPresentation'
import './verifications.css'
const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const authIdentity = computed(() => JSON.stringify([auth.authGeneration, auth.session?.user_id, auth.session?.tenant_id, auth.session?.role, auth.isSuperAdmin]))
const page = ref<HTMLElement>()
const actionError = ref('')
const all = { value: 'all', label: '全部' }
const modeOptions = [all, ...Object.entries(modeLabels).map(([value, label]) => ({ value, label }))]
const statusOptions = [all, ...Object.entries(statusLabels).map(([value, label]) => ({ value, label }))]
const outcomeOptions = [all, ...Object.entries(outcomeLabels).map(([value, label]) => ({ value, label }))]
const draft = reactive({ source_number: '', external_id: '', mode: 'all', execution_status: 'all', outcome: 'all' })
const limit = 20
const offset = computed(() => {
  const value = Number(route.query.offset)
  return Number.isSafeInteger(value) && value > 0 ? Math.floor(value / limit) * limit : 0
})
const createOpen = computed(() => route.name === 'NewVerification' || route.query.create === '1')
const filters = computed<VerificationFilters>(() => ({
  source_number: typeof route.query.source_number === 'string' ? route.query.source_number : undefined,
  external_id: typeof route.query.external_id === 'string' ? route.query.external_id : undefined,
  mode: typeof route.query.mode === 'string' && route.query.mode in modeLabels ? route.query.mode as VerificationFilters['mode'] : undefined,
  execution_status: typeof route.query.execution_status === 'string' && route.query.execution_status in statusLabels ? route.query.execution_status as VerificationFilters['execution_status'] : undefined,
  outcome: typeof route.query.outcome === 'string' && route.query.outcome in outcomeLabels ? route.query.outcome as VerificationFilters['outcome'] : undefined,
  offset: offset.value, limit,
}))
watch(filters, (value) => Object.assign(draft, { source_number: value.source_number || '', external_id: value.external_id || '', mode: value.mode || 'all', execution_status: value.execution_status || 'all', outcome: value.outcome || 'all' }), { immediate: true })
function applyFilters() {
  void router.push({ name: 'Verifications', query: Object.fromEntries(Object.entries(draft).filter(([, value]) => value && value !== 'all')) })
}
function changePage(value: number) { void router.push({ name: 'Verifications', query: { ...verificationListQuery(route.query), offset: String(value), scroll: undefined } }) }
function listContext() {
  return { ...verificationListQuery(route.query), scroll: String(Math.round(page.value?.closest('.content-viewport')?.scrollTop || 0)) }
}
function detailLocation(id: string) { return { name: 'VerificationDetail', params: { id }, query: listContext() } }
function openDetail(event: MouseEvent, id: string) {
  if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return
  event.preventDefault()
  void router.push(detailLocation(id))
}
function openCreate() { void router.push({ name: 'Verifications', query: { ...listContext(), create: '1' } }) }
function openAgain(id: string) { void router.push({ name: 'Verifications', query: { ...listContext(), create: '1', source_request_id: id, return_to: 'list' } }) }
async function closeCreate() {
  const source = route.query.source_request_id
  if (typeof source === 'string' && route.query.return_to !== 'list') {
    await router.push({ name: 'VerificationDetail', params: { id: source }, query: verificationListQuery(route.query) })
    await nextTick()
    document.querySelector<HTMLElement>(`[data-verification-rerun="${CSS.escape(source)}"]`)?.focus()
  } else if (typeof route.query.source_project_id === 'string' && typeof route.query.source_vulnerability_id === 'string') {
    await router.push({ name: 'Findings', query: { vulnerabilityProjectId: route.query.source_project_id, vulnerabilityId: route.query.source_vulnerability_id } })
  } else {
    await router.push({ name: 'Verifications', query: verificationListQuery(route.query) })
    await nextTick()
    const trigger = typeof source === 'string' ? page.value?.querySelector<HTMLElement>(`[data-verification-actions="${CSS.escape(source)}"]`) : null
    ;(trigger || page.value?.querySelector<HTMLElement>('[data-verification-create]'))?.focus()
  }
}
async function afterDeleted() {
  const identity = authIdentity.value
  actionError.value = ''
  await refetch()
  if (identity !== authIdentity.value) return
  await nextTick()
  ;(page.value?.querySelector<HTMLElement>('[data-verification-actions]') || page.value?.querySelector<HTMLElement>('[data-verification-create]'))?.focus()
}
let refreshUntil = Date.now() + 10 * 60_000
let refreshFailures = 0
const autoPaused = ref(false)
const { data, isLoading, isFetching, isError, error, refetch } = useQuery({
  queryKey: computed(() => ['verification-requests', authIdentity.value, filters.value]),
  queryFn: async ({ signal }) => {
    try { const value = await listVerificationRequests(filters.value, signal); refreshFailures = 0; return value }
    catch (error) { if (!signal.aborted) refreshFailures++; throw error }
  },
  enabled: computed(() => Boolean(auth.session?.tenant_id)),
  retry: false,
  refetchOnWindowFocus: false,
  refetchInterval: (query) => {
    if (!query.state.data?.items.some((item) => isActiveVerification(item.execution_status))) return false
    if (Date.now() >= refreshUntil || refreshFailures >= 3) { autoPaused.value = true; return false }
    return 5000
  },
})
// 列表内删除、详情删除返回及手动刷新共用服务端计数校正，不保留已空的末页。
watch([data, isFetching], ([value, fetching]) => {
  if (!value || fetching || isError.value || offset.value === 0 || offset.value < value.total) return
  void router.replace({ name: route.name || 'Verifications', query: { ...route.query, offset: String(Math.max(0, Math.ceil(value.total / limit) - 1) * limit), scroll: undefined } })
})
function refresh() { refreshUntil = Date.now() + 10 * 60_000; refreshFailures = 0; autoPaused.value = false; void refetch() }
watch([authIdentity, filters], () => { refreshUntil = Date.now() + 10 * 60_000; refreshFailures = 0; autoPaused.value = false; actionError.value = '' })
let restoredContext = ''
watch([() => data.value?.items, () => route.query.scroll, createOpen], async () => {
  if (createOpen.value || !data.value) return
  const context = JSON.stringify([filters.value, route.query.scroll])
  if (restoredContext === context) return
  restoredContext = context
  await nextTick()
  const viewport = page.value?.closest('.content-viewport')
  if (viewport) viewport.scrollTop = Math.max(0, Number(route.query.scroll) || 0)
}, { immediate: true })
const columns: DataTableColumn<VerificationSummary>[] = [
  { key: 'title', label: '复测对象' }, { key: 'source', label: '来源', width: 190 },
  { key: 'mode', label: '模式', width: 140 }, { key: 'status', label: '执行状态', width: 160 },
  { key: 'result', label: '结论', width: 190 }, { key: 'created', label: '创建时间', width: 200 },
  { key: 'actions', label: '操作', align: 'end', width: 100 },
]
</script>
