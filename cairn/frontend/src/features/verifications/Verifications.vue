<template>
  <section ref="page" class="verification-theme verification-page" aria-label="漏洞复现与复测">
    <header class="verification-heading">
      <h1>漏洞复现与复测</h1>
      <Button v-if="auth.isSuperAdmin" variant="secondary" @click="router.push({ name: 'VerificationConfiguration' })">漏洞复测配置</Button>
      <Button @click="openCreate"><template #icon><Plus :size="16" /></template>新建复测</Button>
    </header>
    <form class="verification-filters" @submit.prevent="applyFilters">
      <FormField label="外部漏洞编号" for="verification-external"><Input id="verification-external" v-model="draft.external_id" placeholder="完整编号，精确匹配" /></FormField>
      <FormField label="模式" for="verification-mode"><Select content-class="verification-theme verification-select" id="verification-mode" v-model="draft.mode" :options="modeOptions" /></FormField>
      <FormField label="执行状态" for="verification-status"><Select content-class="verification-theme verification-select" id="verification-status" v-model="draft.execution_status" :options="statusOptions" /></FormField>
      <FormField label="本轮结论" for="verification-outcome"><Select content-class="verification-theme verification-select" id="verification-outcome" v-model="draft.outcome" :options="outcomeOptions" /></FormField>
      <Button type="submit" variant="secondary">筛选</Button>
      <Button variant="secondary" :loading="isFetching" @click="refresh">刷新</Button>
    </form>
    <Alert v-if="isError && data" tone="warning" title="刷新失败">保留上次取得的记录，最新状态尚未确认。{{ errorMessage(error) }}</Alert>
    <Skeleton v-if="isLoading" aria-label="正在加载复测记录" />
    <ErrorState v-else-if="isError && !data" title="复测列表加载失败" :description="errorMessage(error)" @retry="refresh" />
    <EmptyState v-else-if="!data?.items.length" title="暂无匹配的复测记录" description="可调整筛选条件，或新建一次漏洞复现／复测。" />
    <div v-else class="verification-list-table">
      <DataTable :rows="data.items" :columns="columns" :row-key="(row) => row.id" caption="复测历史">
        <template #cell-title="{ row }"><RouterLink v-slot="{ href }" :to="detailLocation(row.id)" custom><a :href="href" @click="openDetail($event, row.id)">{{ row.title }}</a></RouterLink><div class="verification-muted">{{ row.external_id || '无外部编号' }}</div></template>
        <template #cell-source="{ row }"><span class="verification-muted">{{ row.source.type === 'integration' ? '外部调用' : '平台用户' }} · {{ row.source.name }}</span><div class="verification-muted">{{ row.input_source ? `平台漏洞 · ${row.input_source.vulnerability_id}${row.input_source.input_mode === 'copied_snapshot' ? ' · 历史快照' : ''}` : '手工资料' }}</div></template>
        <template #cell-mode="{ row }">{{ modeLabels[row.mode] }}</template>
        <template #cell-status="{ row }"><Badge :tone="statusTone(row.execution_status)">{{ statusLabels[row.execution_status] }}</Badge></template>
        <template #cell-result="{ row }"><Badge v-if="row.result" :tone="outcomeTone(row.result.outcome)">{{ outcomeLabels[row.result.outcome] }}</Badge><span v-else class="verification-muted">{{ row.error ? '执行错误，非业务结论' : '尚无结论' }}</span></template>
        <template #cell-created="{ row }"><span class="verification-muted">{{ dateTime(row.created_at) }}</span></template>
      </DataTable>
    </div>
    <footer class="verification-actions verification-list-footer">
      <span class="verification-muted">共 {{ data?.total ?? 0 }} 条 · 第 {{ Math.floor(offset / limit) + 1 }} 页</span>
      <span class="verification-muted verification-refresh-note">{{ autoPaused ? '自动更新已暂停，可手动刷新继续' : hasActive ? '活动记录每 5 秒更新，最长 10 分钟' : '终态记录不自动更新' }}</span>
      <Button variant="secondary" size="sm" :disabled="offset === 0 || isFetching" @click="changePage(offset - limit)">上一页</Button>
      <Button variant="secondary" size="sm" :disabled="!data || offset + limit >= data.total || isFetching" @click="changePage(offset + limit)">下一页</Button>
    </footer>
    <NewVerification v-if="createOpen" @close="closeCreate" />
  </section>
</template>
<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { Plus } from '@lucide/vue'
import { Alert, Badge, Button, EmptyState, FormField, Input, Select, Skeleton } from '@/components/ui'
import { DataTable, ErrorState } from '@/components/patterns'
import type { DataTableColumn } from '@/components/patterns/DataTable.vue'
import { listVerificationRequests, type VerificationFilters, type VerificationSummary } from '@/api/verificationRequests'
import { useAuthStore } from '@/stores/auth'
import { dateTime } from '@/lib/format'
import NewVerification from './NewVerification.vue'
import { errorMessage, isActiveVerification, modeLabels, statusLabels, statusTone, outcomeLabels, outcomeTone, verificationListQuery } from './verificationPresentation'
import './verifications.css'
const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const page = ref<HTMLElement>()
const all = { value: 'all', label: '全部' }
const modeOptions = [all, ...Object.entries(modeLabels).map(([value, label]) => ({ value, label }))]
const statusOptions = [all, ...Object.entries(statusLabels).map(([value, label]) => ({ value, label }))]
const outcomeOptions = [all, ...Object.entries(outcomeLabels).map(([value, label]) => ({ value, label }))]
const draft = reactive({ external_id: '', mode: 'all', execution_status: 'all', outcome: 'all' })
const limit = 20
const offset = computed(() => {
  const value = Number(route.query.offset)
  return Number.isSafeInteger(value) && value > 0 ? Math.floor(value / limit) * limit : 0
})
const createOpen = computed(() => route.name === 'NewVerification' || route.query.create === '1')
const filters = computed<VerificationFilters>(() => ({
  external_id: typeof route.query.external_id === 'string' ? route.query.external_id : undefined,
  mode: typeof route.query.mode === 'string' && route.query.mode in modeLabels ? route.query.mode as VerificationFilters['mode'] : undefined,
  execution_status: typeof route.query.execution_status === 'string' && route.query.execution_status in statusLabels ? route.query.execution_status as VerificationFilters['execution_status'] : undefined,
  outcome: typeof route.query.outcome === 'string' && route.query.outcome in outcomeLabels ? route.query.outcome as VerificationFilters['outcome'] : undefined,
  offset: offset.value, limit,
}))
watch(filters, (value) => Object.assign(draft, { external_id: value.external_id || '', mode: value.mode || 'all', execution_status: value.execution_status || 'all', outcome: value.outcome || 'all' }), { immediate: true })
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
function closeCreate() {
  const source = route.query.source_request_id
  if (typeof source === 'string') void router.push({ name: 'VerificationDetail', params: { id: source }, query: verificationListQuery(route.query) })
  else if (typeof route.query.source_project_id === 'string' && typeof route.query.source_vulnerability_id === 'string') void router.push({ name: 'Findings', query: { vulnerabilityProjectId: route.query.source_project_id, vulnerabilityId: route.query.source_vulnerability_id } })
  else void router.push({ name: 'Verifications', query: verificationListQuery(route.query) })
}
let refreshUntil = Date.now() + 10 * 60_000
let refreshFailures = 0
const autoPaused = ref(false)
const { data, isLoading, isFetching, isError, error, refetch } = useQuery({
  queryKey: ['verification-requests', filters],
  queryFn: async ({ signal }) => {
    try { const value = await listVerificationRequests(filters.value, signal); refreshFailures = 0; return value }
    catch (error) { if (!signal.aborted) refreshFailures++; throw error }
  },
  retry: false,
  refetchOnWindowFocus: false,
  refetchInterval: (query) => {
    if (!query.state.data?.items.some((item) => isActiveVerification(item.execution_status))) return false
    if (Date.now() >= refreshUntil || refreshFailures >= 3) { autoPaused.value = true; return false }
    return 5000
  },
})
const hasActive = computed(() => data.value?.items.some((item) => isActiveVerification(item.execution_status)))
function refresh() { refreshUntil = Date.now() + 10 * 60_000; refreshFailures = 0; autoPaused.value = false; void refetch() }
watch(filters, () => { refreshUntil = Date.now() + 10 * 60_000; refreshFailures = 0; autoPaused.value = false })
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
  { key: 'title', label: '名称 / 外部编号' }, { key: 'source', label: '来源' },
  { key: 'mode', label: '模式' }, { key: 'status', label: '执行状态' },
  { key: 'result', label: '本轮结论' }, { key: 'created', label: '创建时间' },
]
</script>
