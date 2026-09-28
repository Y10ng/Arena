<template>
  <section class="report-management" aria-labelledby="report-management-title">
    <h3 id="report-management-title" class="sr-only">报告管理</h3>

    <FilterBar label="报告筛选" class="enterprise-filter-bar report-filter-bar">
      <FormField label="搜索"
        ><template #default="{ controlAttrs }"
          ><Input
            v-model="searchSelection"
            v-bind="controlAttrs"
            placeholder="报告名或项目名" /></template
      ></FormField>
      <FormField label="项目 ID"
        ><template #default="{ controlAttrs }"
          ><Input
            v-model="projectSelection"
            v-bind="controlAttrs"
            placeholder="精确项目 ID" /></template
      ></FormField>
      <FormField label="模板"
        ><template #default="{ controlAttrs }"
          ><Input
            v-model="templateSelection"
            v-bind="controlAttrs"
            placeholder="模板 key" /></template
      ></FormField>
      <FormField label="开始日期"
        ><template #default="{ controlAttrs }"
          ><Input v-model="dateFromSelection" v-bind="controlAttrs" type="date" /></template
      ></FormField>
      <FormField label="结束日期"
        ><template #default="{ controlAttrs }"
          ><Input v-model="dateToSelection" v-bind="controlAttrs" type="date" /></template
      ></FormField>
      <template v-if="isFetching && reports.length" #status>同步中…</template>
      <template #actions>
        <Button variant="secondary" size="sm" :loading="isFetching" @click="refetch()">刷新</Button>
      </template>
    </FilterBar>

    <MasterDetail class="report-workspace">
      <template #master>
        <Card class="report-list-card" padding="none">
          <div class="report-list-summary">
            <strong>已完成报告</strong><span>{{ total }} 份</span>
          </div>
          <div
            v-if="isLoading && !reports.length"
            class="report-list-loading"
            role="status"
            aria-label="正在加载报告"
          >
            <Skeleton v-for="index in 5" :key="index" shape="text" />
          </div>
          <ErrorState
            v-else-if="isError && !reports.length"
            title="报告加载失败"
            :description="listErrorMessage"
            @retry="refetch()"
          />
          <EmptyState
            v-else-if="!reports.length"
            title="暂无已完成报告"
            description="项目报告生成完成后会出现在这里。"
          />
          <template v-else>
            <ErrorState
              v-if="isError"
              class="report-list-background-error"
              title="报告同步失败"
              :description="listErrorMessage"
              @retry="refetch()"
            />
            <DataTable
              :rows="reports"
              :columns="columns"
              :row-key="reportRowKey"
              :is-row-active="isSelected"
              active-row-aria="current"
              :action-label="reportActionLabel"
              action-column-label="审阅"
              caption="租户评估报告列表"
              density="compact"
              @row-action="selectReport"
            >
              <template #cell-report="{ row }"
                ><div class="report-list-item">
                  <strong :title="row.artifact_uri">{{ reportFilename(row) }}</strong
                  ><span :title="row.project_title">{{ row.project_title }}</span>
                </div></template
              >
              <template #cell-created="{ row }"
                ><span class="finding-time" :title="row.created_at">{{
                  formatRelativeTime(row.created_at)
                }}</span></template
              >
            </DataTable>
          </template>
          <footer class="report-pagination" aria-label="报告分页">
            <Button
              variant="secondary"
              size="sm"
              :disabled="page <= 1 || isFetching"
              @click="setPage(page - 1)"
              >上一页</Button
            >
            <span>第 {{ page }} / {{ pageCount }} 页</span>
            <Button
              variant="secondary"
              size="sm"
              :disabled="page >= pageCount || isFetching"
              @click="setPage(page + 1)"
              >下一页</Button
            >
            <FormField label="每页"
              ><template #default="{ controlAttrs }"
                ><Select
                  v-model="pageSizeSelection"
                  v-bind="controlAttrs"
                  :options="pageSizeOptions" /></template
            ></FormField>
          </footer>
        </Card>
      </template>

      <template #detail>
        <Card class="report-preview-card" padding="none">
          <EmptyState
            v-if="!selectedReport"
            title="选择一份报告开始审阅"
            description="从左侧列表选择报告后，可查看内容与生成溯源。"
          />
          <template v-else>
            <header class="report-preview-header">
              <div>
                <p class="report-preview-eyebrow">{{ selectedReport.project_title }}</p>
                <h3>{{ reportFilename(selectedReport) }}</h3>
                <div class="report-preview-meta">
                  <span>{{ bytes(selectedReport.artifact_size) }}</span>
                  <span>生成于 {{ formatRelativeTime(selectedReport.created_at) }}</span>
                  <span :title="selectedReport.artifact_sha256"
                    >SHA256 {{ abbreviatedHash(selectedReport.artifact_sha256) }}</span
                  >
                </div>
              </div>
              <div class="report-preview-actions">
                <RouterLink
                  class="report-download-link"
                  :to="{
                    name: 'WorkspaceVulnerabilities',
                    params: { id: selectedReport.project_id },
                  }"
                  >打开项目</RouterLink
                >
                <a class="report-download-link" :href="reportContentUrl(selectedReport)" download
                  >下载完整报告</a
                >
              </div>
            </header>

            <Alert
              v-if="validPreview?.truncated"
              class="report-preview-alert"
              tone="info"
              title="当前仅显示报告开头"
            >
              已读取 {{ bytes(validPreview.bytes_read) }}，完整报告共
              {{ bytes(validPreview.size) }}。请下载原文件获取全部内容。
            </Alert>

            <div class="report-preview-body" aria-live="polite">
              <div v-if="isPreviewLoading" class="report-preview-loading" role="status">
                <Spinner /><span>正在载入报告预览…</span>
              </div>
              <ErrorState
                v-else-if="isPreviewError || previewIdentityMismatch"
                :title="previewIdentityMismatch ? '报告预览身份不匹配' : '报告预览载入失败'"
                :description="
                  previewIdentityMismatch
                    ? '服务端返回的项目或 Artifact 身份与所选报告不一致，已阻止渲染。'
                    : previewErrorMessage
                "
                @retry="refetchPreview()"
              />
              <Tabs
                v-else-if="validPreview"
                v-model="previewMode"
                class="report-preview-tabs"
                :options="previewOptions"
                variant="pill"
              >
                <template #panel="{ option }">
                  <SafeReportMarkdown
                    v-if="option.value === 'rendered'"
                    :content="validPreview.content"
                  />
                  <pre v-else class="report-raw-preview">{{ validPreview.content }}</pre>
                </template>
              </Tabs>
            </div>
          </template>
        </Card>
      </template>
    </MasterDetail>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { fetchArtifactPreview, listReports, type ArtifactPreview } from '@/api/client'
import type { ReportInventoryFilters, ReportInventoryItem, ReportInventoryPage } from '@/api/types'
import {
  DataTable,
  ErrorState,
  FilterBar,
  MasterDetail,
  type DataTableColumn,
} from '@/components/patterns'
import {
  Alert,
  Button,
  Card,
  EmptyState,
  FormField,
  Input,
  Select,
  Skeleton,
  Spinner,
  Tabs,
  type TabsOption,
} from '@/components/ui'
import SafeReportMarkdown from '@/features/reports/SafeReportMarkdown.vue'
import { reportKeys } from '@/features/reports/reportQueries'
import { bytes, formatRelativeTime } from '@/lib/format'

const DEFAULT_PAGE_SIZE = 20
const VALID_PAGE_SIZES = [10, 20, 50] as const
const route = useRoute()
const router = useRouter()
const queryString = (value: unknown) => (typeof value === 'string' ? value : '')
const positiveInteger = (value: unknown, fallback: number) => {
  const parsed = Number(queryString(value))
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback
}
const replaceQuery = (changes: Record<string, string | undefined>) =>
  void router.replace({ query: { ...route.query, ...changes } })
const filterModel = (key: string) =>
  computed({
    get: () => queryString(route.query[key]),
    set: (value: string) =>
      replaceQuery({
        [key]: value.trim() || undefined,
        page: undefined,
        reportId: undefined,
        reportProjectId: undefined,
      }),
  })

const searchSelection = filterModel('reportQ')
const projectSelection = filterModel('reportProject')
const templateSelection = filterModel('reportTemplate')
const dateFromSelection = filterModel('dateFrom')
const dateToSelection = filterModel('dateTo')
const pageSize = computed(() => {
  const value = positiveInteger(route.query.pageSize, DEFAULT_PAGE_SIZE)
  return VALID_PAGE_SIZES.includes(value as (typeof VALID_PAGE_SIZES)[number])
    ? value
    : DEFAULT_PAGE_SIZE
})
const page = computed(() => positiveInteger(route.query.page, 1))
const pageSizeSelection = computed({
  get: () => String(pageSize.value),
  set: (value: string) =>
    replaceQuery({
      pageSize: value === String(DEFAULT_PAGE_SIZE) ? undefined : value,
      page: undefined,
      reportId: undefined,
      reportProjectId: undefined,
    }),
})
const pageSizeOptions = VALID_PAGE_SIZES.map((value) => ({
  value: String(value),
  label: `${value} 条`,
}))
const requestFilters = computed<ReportInventoryFilters>(() => ({
  q: searchSelection.value || undefined,
  project_id: projectSelection.value || undefined,
  template_key: templateSelection.value || undefined,
  date_from: dateFromSelection.value ? `${dateFromSelection.value}T00:00:00Z` : undefined,
  date_to: dateToSelection.value ? `${dateToSelection.value}T23:59:59Z` : undefined,
  page: page.value,
  page_size: pageSize.value,
}))

const { data, error, isError, isFetching, isLoading, refetch } = useQuery<ReportInventoryPage>({
  queryKey: computed(() => reportKeys.list(requestFilters.value)),
  queryFn: ({ queryKey, signal }) => {
    const [, , q, projectId, templateKey, dateFrom, dateTo, requestPage, requestPageSize] =
      queryKey as ReturnType<typeof reportKeys.list>
    return listReports(
      {
        q: q || undefined,
        project_id: projectId || undefined,
        template_key: templateKey || undefined,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        page: requestPage,
        page_size: requestPageSize,
      },
      signal,
    )
  },
  staleTime: 30_000,
  refetchInterval: 30_000,
  refetchOnWindowFocus: false,
})

const reports = computed(() => data.value?.items ?? [])
const total = computed(() => data.value?.total ?? 0)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const selectedReportId = computed(() => queryString(route.query.reportId))
const selectedProjectId = computed(() => queryString(route.query.reportProjectId))
const selectedReport = computed(
  () =>
    reports.value.find(
      (item) =>
        item.artifact_id === selectedReportId.value && item.project_id === selectedProjectId.value,
    ) ?? null,
)
const listErrorMessage = computed(() =>
  error.value instanceof Error && error.value.message ? error.value.message : '请稍后重试。',
)
const previewMode = ref('rendered')
const previewOptions = [
  { value: 'rendered', label: '渲染视图' },
  { value: 'raw', label: '原始 Markdown' },
] satisfies readonly TabsOption[]

const previewProjectId = computed(() => selectedReport.value?.project_id ?? '')
const previewArtifactId = computed(() => selectedReport.value?.artifact_id ?? '')
const {
  data: preview,
  error: previewError,
  isError: isPreviewError,
  isLoading: isPreviewLoading,
  refetch: refetchPreview,
} = useQuery<ArtifactPreview>({
  queryKey: computed(() => reportKeys.preview(previewProjectId.value, previewArtifactId.value)),
  queryFn: ({ signal }) =>
    fetchArtifactPreview(previewProjectId.value, previewArtifactId.value, signal),
  enabled: computed(() => Boolean(previewProjectId.value && previewArtifactId.value)),
  staleTime: 60_000,
})
const previewIdentityMismatch = computed(() =>
  Boolean(
    preview.value &&
    selectedReport.value &&
    (preview.value.project_id !== selectedReport.value.project_id ||
      preview.value.artifact_id !== selectedReport.value.artifact_id),
  ),
)
const validPreview = computed(() => (previewIdentityMismatch.value ? null : preview.value))
const previewErrorMessage = computed(() =>
  previewError.value instanceof Error && previewError.value.message
    ? previewError.value.message
    : '请稍后重试。',
)
const columns = [
  { key: 'report', label: '报告' },
  { key: 'created', label: '生成时间' },
] satisfies readonly DataTableColumn<ReportInventoryItem, 'report' | 'created'>[]

function reportRowKey(item: ReportInventoryItem) {
  return `${item.project_id}:${item.artifact_id}`
}
function reportFilename(item: ReportInventoryItem) {
  const value = item.artifact_uri.split('/').pop() || item.artifact_uri
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}
function isSelected(item: ReportInventoryItem) {
  return selectedReportId.value === item.artifact_id && selectedProjectId.value === item.project_id
}
function reportActionLabel(item: ReportInventoryItem) {
  return `审阅报告：${reportFilename(item)}`
}
function selectReport(item: ReportInventoryItem) {
  replaceQuery({ reportProjectId: item.project_id, reportId: item.artifact_id })
}
function setPage(nextPage: number) {
  replaceQuery({
    page: nextPage <= 1 ? undefined : String(nextPage),
    reportId: undefined,
    reportProjectId: undefined,
  })
}
function reportContentUrl(item: ReportInventoryItem) {
  return `/projects/${encodeURIComponent(item.project_id)}/artifacts/${encodeURIComponent(item.artifact_id)}/content`
}
function abbreviatedHash(value: string) {
  return value.length > 16 ? `${value.slice(0, 16)}…` : value || '—'
}

watch([pageCount, isFetching], ([count, fetching]) => {
  if (!fetching && page.value > count) setPage(count)
})
watch([reports, isFetching], ([items, fetching]) => {
  if (
    !fetching &&
    selectedReportId.value &&
    selectedProjectId.value &&
    !items.some(
      (item) =>
        item.artifact_id === selectedReportId.value && item.project_id === selectedProjectId.value,
    )
  )
    replaceQuery({ reportId: undefined, reportProjectId: undefined })
})
watch(selectedReport, () => {
  previewMode.value = 'rendered'
})
</script>
