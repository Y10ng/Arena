<template>
  <div class="payloads-page">
    <FilterBar label="Payload 筛选">
      <FormField label="搜索" class="payloads-search">
        <template #default="{ controlAttrs }">
          <Input
            v-model="searchInput"
            v-bind="controlAttrs"
            placeholder="搜索分类、Payload、响应或格式…"
            @keydown.enter="applySearch"
            @keydown.esc="clearSearch"
          />
        </template>
      </FormField>
      <FormField label="注入类型">
        <template #default="{ controlAttrs }">
          <Select v-model="categorySelection" v-bind="controlAttrs" :options="categoryOptions" />
        </template>
      </FormField>
      <FormField label="响应状态">
        <template #default="{ controlAttrs }">
          <Select v-model="statusSelection" v-bind="controlAttrs" :options="statusOptions" />
        </template>
      </FormField>
      <template v-if="isFetching && records.length" #status>同步中…</template>
      <template #actions>
        <Button variant="secondary" size="sm" @click="applySearch">搜索</Button>
        <Button variant="secondary" size="sm" :loading="isFetching" @click="refresh"> 刷新 </Button>
        <a class="payloads-export" :href="exportUrl" download>导出 JSONL</a>
      </template>
    </FilterBar>

    <div
      v-if="isLoading && !hasCurrentSnapshot"
      class="payloads-skeleton"
      role="status"
      aria-label="正在加载 Payload"
    >
      <Card v-for="index in 4" :key="index" padding="sm">
        <Skeleton shape="text" />
      </Card>
    </div>

    <ErrorState
      v-else-if="isError && !hasCurrentSnapshot"
      title="Payload 加载失败"
      :description="errorMessage"
      @retry="refresh"
    />

    <EmptyState
      v-else-if="!records.length"
      title="暂无 Payload 记录"
      :description="emptyDescription"
    >
      <template #icon>⌁</template>
    </EmptyState>

    <template v-else>
      <ErrorState
        v-if="isError"
        title="Payload 同步失败"
        :description="errorMessage"
        @retry="refresh"
      />
      <Card padding="none">
        <DataTable
          :rows="records"
          :columns="columns"
          :row-key="rowKey"
          caption="项目注入 Payload 列表"
          density="compact"
          :is-row-active="isRowActive"
          active-row-aria="current"
          :action-label="actionLabel"
          action-column-label="详情"
          @row-action="selectRecord"
        >
          <template #cell-payload="{ row }">
            <div class="payloads-summary">
              <strong>{{ row.injection_type }}</strong>
              <span class="payloads-preview">{{ visiblePreview(row.payload_preview) }}</span>
              <span class="payloads-format">{{ row.original_format }}</span>
            </div>
          </template>
          <template #cell-status="{ row }">
            <Badge :tone="responseStatusMeta(row.response_status).tone" size="sm">
              {{ responseStatusMeta(row.response_status).label }}
            </Badge>
          </template>
          <template #cell-run="{ row }">
            <Button variant="link" size="sm" @click.stop="goToRun(row.task_run_id)">
              {{ row.task_run_id }}
            </Button>
          </template>
          <template #cell-created="{ row }">
            <span :title="row.created_at">{{ formatRelativeTime(row.created_at) }}</span>
          </template>
        </DataTable>
      </Card>

      <div class="payloads-pagination" aria-label="Payload 分页">
        <span>第 {{ pageNumber }} / {{ pageCount }} 页，共 {{ total }} 条</span>
        <div>
          <Button variant="secondary" size="sm" :disabled="offset === 0" @click="previousPage">
            上一页
          </Button>
          <Button
            variant="secondary"
            size="sm"
            :disabled="offset + pageSize >= total"
            @click="nextPage"
          >
            下一页
          </Button>
        </div>
      </div>
    </template>

    <PayloadDetailDrawer
      :project-id="projectId"
      :payload-id="selectedPayloadId"
      :visit-generation="projectVisitGeneration"
      :open="Boolean(selectedPayloadId)"
      @close="closeDetail"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import {
  listProjectInjectionPayloadCategories,
  listProjectInjectionPayloads,
  projectInjectionPayloadExportUrl,
} from '@/api/client'
import type {
  InjectionPayloadListItem,
  InjectionPayloadListResponse,
  InjectionPayloadResponseStatus,
} from '@/api/types'
import { DataTable, ErrorState, FilterBar, type DataTableColumn } from '@/components/patterns'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  FormField,
  Input,
  Select,
  Skeleton,
  type BadgeTone,
} from '@/components/ui'
import { formatRelativeTime } from '@/lib/format'
import PayloadDetailDrawer from './PayloadDetailDrawer.vue'
import { injectionPayloadKeys } from './payloadQueries'

const ALL_FILTER = '__all__'
const PAGE_SIZE = 50
const route = useRoute()
const router = useRouter()
const projectId = computed(() => String(route.params.id))
const projectVisitGeneration = ref(0)
const categorySelection = ref(ALL_FILTER)
const statusSelection = ref(ALL_FILTER)
const searchInput = ref('')
const appliedSearch = ref('')
const offset = ref(0)
const selectedPayloadId = computed(() => {
  const value = route.query.payloadId
  return typeof value === 'string' && value ? value : null
})
const categoryFilter = computed(() =>
  categorySelection.value === ALL_FILTER ? '' : categorySelection.value,
)
const statusFilter = computed(() =>
  statusSelection.value === ALL_FILTER ? '' : statusSelection.value,
)

const statusMetadata: Record<InjectionPayloadResponseStatus, { label: string; tone: BadgeTone }> = {
  pending: { label: '等待响应', tone: 'info' },
  returned: { label: '已返回', tone: 'success' },
  empty: { label: '空响应', tone: 'neutral' },
  blocked: { label: '被阻止', tone: 'warning' },
  timeout: { label: '超时', tone: 'warning' },
  error: { label: '错误', tone: 'danger' },
  not_sent: { label: '未发送', tone: 'neutral' },
  interrupted: { label: '已中断', tone: 'warning' },
}
const statusOptions = [
  { value: ALL_FILTER, label: '全部状态' },
  ...Object.entries(statusMetadata).map(([value, meta]) => ({ value, label: meta.label })),
]

const { data: categoriesData } = useQuery({
  queryKey: computed(() =>
    injectionPayloadKeys.categories(projectId.value, projectVisitGeneration.value),
  ),
  queryFn: async ({ queryKey, signal }) => {
    const [, , requestProjectId, , requestVisitGeneration] = queryKey as ReturnType<
      typeof injectionPayloadKeys.categories
    >
    return {
      projectId: requestProjectId,
      visitGeneration: requestVisitGeneration,
      items: await listProjectInjectionPayloadCategories(requestProjectId, signal),
    }
  },
  refetchInterval: 10_000,
})
const currentCategories = computed(() => {
  const snapshot = categoriesData.value
  return snapshot?.projectId === projectId.value &&
    snapshot.visitGeneration === projectVisitGeneration.value
    ? snapshot.items
    : []
})
const categoryOptions = computed(() => [
  { value: ALL_FILTER, label: '全部类型' },
  ...currentCategories.value.map((item) => ({
    value: item.injection_type,
    label: `${item.injection_type} (${item.count})`,
  })),
])

const { data, error, isError, isFetching, isLoading, refetch } = useQuery<{
  projectId: string
  injectionType: string
  responseStatus: string
  search: string
  offset: number
  visitGeneration: number
  page: InjectionPayloadListResponse
}>({
  queryKey: computed(() =>
    injectionPayloadKeys.list(
      projectId.value,
      categoryFilter.value,
      statusFilter.value,
      appliedSearch.value,
      PAGE_SIZE,
      offset.value,
      projectVisitGeneration.value,
    ),
  ),
  queryFn: async ({ queryKey, signal }) => {
    const [
      ,
      ,
      requestProjectId,
      ,
      requestInjectionType,
      requestResponseStatus,
      requestSearch,
      requestLimit,
      requestOffset,
      requestVisitGeneration,
    ] = queryKey as ReturnType<typeof injectionPayloadKeys.list>
    const page = await listProjectInjectionPayloads(
      requestProjectId,
      {
        injectionType: requestInjectionType || undefined,
        responseStatus: (requestResponseStatus || undefined) as
          InjectionPayloadResponseStatus | undefined,
        search: requestSearch || undefined,
        limit: requestLimit,
        offset: requestOffset,
      },
      signal,
    )
    return {
      projectId: requestProjectId,
      injectionType: requestInjectionType,
      responseStatus: requestResponseStatus,
      search: requestSearch,
      offset: requestOffset,
      visitGeneration: requestVisitGeneration,
      page,
    }
  },
  refetchInterval: 10_000,
})

const currentSnapshot = computed(() => {
  const snapshot = data.value
  if (
    snapshot?.projectId !== projectId.value ||
    snapshot.injectionType !== categoryFilter.value ||
    snapshot.responseStatus !== statusFilter.value ||
    snapshot.search !== appliedSearch.value ||
    snapshot.offset !== offset.value ||
    snapshot.visitGeneration !== projectVisitGeneration.value
  ) {
    return null
  }
  return snapshot
})
const hasCurrentSnapshot = computed(() => currentSnapshot.value !== null)
const records = computed(() => currentSnapshot.value?.page.items || [])
const total = computed(() => currentSnapshot.value?.page.total || 0)
const pageSize = computed(() => currentSnapshot.value?.page.limit || PAGE_SIZE)
const pageNumber = computed(() => Math.floor(offset.value / PAGE_SIZE) + 1)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const hasFilters = computed(() => categoryFilter.value || statusFilter.value || appliedSearch.value)
const emptyDescription = computed(() =>
  hasFilters.value
    ? '当前筛选条件下没有匹配的记录。'
    : 'AI Agent 安全评估在实际投递测试 Payload 后，会在这里保存明文 Payload 与目标直接响应。',
)
const errorMessage = computed(() =>
  error.value instanceof Error && error.value.message ? error.value.message : '请稍后重试。',
)
const exportUrl = computed(() => projectInjectionPayloadExportUrl(projectId.value))

const columns = [
  { key: 'payload', label: 'Payload' },
  { key: 'status', label: '响应状态' },
  { key: 'run', label: 'TaskRun' },
  { key: 'created', label: '记录时间' },
] satisfies readonly DataTableColumn<
  InjectionPayloadListItem,
  'payload' | 'status' | 'run' | 'created'
>[]

watch(projectId, () => {
  projectVisitGeneration.value += 1
  categorySelection.value = ALL_FILTER
  statusSelection.value = ALL_FILTER
  searchInput.value = ''
  appliedSearch.value = ''
  offset.value = 0
  void replacePayloadQuery(null)
})
watch([categorySelection, statusSelection], () => {
  offset.value = 0
})
watch(total, (value) => {
  if (value > 0 && offset.value >= value) {
    offset.value = Math.floor((value - 1) / PAGE_SIZE) * PAGE_SIZE
  }
})
watch(
  [records, selectedPayloadId],
  ([currentRecords, payloadId]) => {
    if (
      payloadId &&
      hasCurrentSnapshot.value &&
      !currentRecords.some((item) => item.id === payloadId)
    ) {
      const selectionMayBeOnAnotherPage = total.value > currentRecords.length
      if (!selectionMayBeOnAnotherPage) void replacePayloadQuery(null)
    }
  },
  { immediate: true },
)

function responseStatusMeta(status: InjectionPayloadResponseStatus) {
  return statusMetadata[status]
}

function visiblePreview(value: string): string {
  return value.replaceAll('\n', ' ↵ ')
}

function rowKey(record: InjectionPayloadListItem): string {
  return record.id
}

function isRowActive(record: InjectionPayloadListItem): boolean {
  return record.id === selectedPayloadId.value
}

function actionLabel(record: InjectionPayloadListItem): string {
  return `查看 Payload 详情：${record.injection_type}`
}

function selectRecord(record: InjectionPayloadListItem) {
  void replacePayloadQuery(record.id)
}

function closeDetail() {
  void replacePayloadQuery(null)
}

function replacePayloadQuery(payloadId: string | null) {
  const query = { ...route.query }
  if (payloadId) query.payloadId = payloadId
  else delete query.payloadId
  return router.replace({ query })
}

function applySearch() {
  appliedSearch.value = searchInput.value.trim()
  offset.value = 0
}

function clearSearch() {
  searchInput.value = ''
  applySearch()
}

function refresh() {
  void refetch()
}

function previousPage() {
  offset.value = Math.max(0, offset.value - PAGE_SIZE)
}

function nextPage() {
  if (offset.value + PAGE_SIZE < total.value) offset.value += PAGE_SIZE
}

function goToRun(taskRunId: string) {
  void router.push({
    name: 'WorkspaceRuns',
    params: { id: projectId.value },
    query: { runId: taskRunId },
  })
}
</script>

<style scoped>
.payloads-page,
.payloads-skeleton {
  display: grid;
  gap: var(--space-4);
}

.payloads-search {
  min-width: min(24rem, 100%);
}

.payloads-export {
  display: inline-flex;
  align-items: center;
  min-height: 2rem;
  padding: 0 var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  font-size: var(--font-size-sm);
  text-decoration: none;
}

.payloads-export:hover {
  border-color: var(--border-strong);
  background: var(--surface-hover);
}

.payloads-summary {
  display: grid;
  min-width: 18rem;
  max-width: 48rem;
  gap: var(--space-1);
}

.payloads-preview,
.payloads-format {
  overflow: hidden;
  color: var(--text-faint);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.payloads-preview {
  color: var(--text-muted);
  font-family: var(--font-mono);
}

.payloads-format {
  font-size: var(--font-size-xs);
}

.payloads-pagination,
.payloads-pagination > div {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.payloads-pagination {
  justify-content: space-between;
  color: var(--text-faint);
  font-size: var(--font-size-sm);
}

@media (max-width: 640px) {
  .payloads-pagination {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
