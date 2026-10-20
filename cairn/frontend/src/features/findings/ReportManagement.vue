<template>
  <section class="report-management" aria-label="报告">
    <div v-show="!isReading" class="report-filters">
      <FilterBar label="报告筛选" class="report-filter-bar">
        <FormField v-slot="{ controlAttrs }" label="搜索">
          <Input v-model="searchSelection" v-bind="controlAttrs" placeholder="报告名或项目名" />
        </FormField>
        <FormField v-slot="{ controlAttrs }" label="项目 ID">
          <Input v-model="projectSelection" v-bind="controlAttrs" placeholder="精确项目 ID" />
        </FormField>
        <template #status
          >{{ total }} 份报告<span v-if="isFetching && reports.length"> · 同步中…</span></template
        >
        <template #actions>
          <Button v-if="hasFilters" variant="ghost" size="sm" @click="clearFilters"
            >清除筛选</Button
          >
          <Button variant="secondary" size="sm" :loading="isFetching" @click="refetch()"
            >刷新</Button
          >
        </template>
      </FilterBar>
      <details class="report-extra-filters">
        <summary>
          模板与日期<span v-if="secondaryFilterSummary"> · {{ secondaryFilterSummary }}</span>
        </summary>
        <div class="report-extra-filter-fields">
          <FormField v-slot="{ controlAttrs }" label="模板">
            <Input v-model="templateSelection" v-bind="controlAttrs" placeholder="模板 key" />
          </FormField>
          <FormField v-slot="{ controlAttrs }" label="开始日期">
            <Input v-model="dateFromSelection" v-bind="controlAttrs" type="date" />
          </FormField>
          <FormField v-slot="{ controlAttrs }" label="结束日期">
            <Input v-model="dateToSelection" v-bind="controlAttrs" type="date" />
          </FormField>
        </div>
      </details>
    </div>

    <div class="report-workspace" :class="{ 'report-workspace--reading': isReading }">
      <section v-show="!isReading" ref="directory" class="report-directory" aria-label="报告列表">
        <div class="report-directory-scroll">
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
              title="报告同步失败"
              :description="listErrorMessage"
              @retry="refetch()"
            />
            <ul class="report-directory-list">
              <li v-for="report in reports" :key="reportRowKey(report)">
                <button
                  type="button"
                  class="report-directory-item"
                  :aria-current="isSelected(report) ? 'true' : undefined"
                  :aria-label="reportActionLabel(report)"
                  @click="selectReport(report)"
                >
                  <strong :title="reportFilename(report)">{{ reportFilename(report) }}</strong>
                  <span class="report-item-project" :title="report.project_title">{{
                    report.project_title
                  }}</span>
                  <time :datetime="report.created_at" :title="report.created_at">{{
                    formatRelativeTime(report.created_at)
                  }}</time>
                </button>
              </li>
            </ul>
          </template>
        </div>
        <footer class="report-pagination" aria-label="报告分页">
          <div class="report-page-controls">
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
          </div>
          <FormField v-slot="{ controlAttrs }" label="每页">
            <Select v-model="pageSizeSelection" v-bind="controlAttrs" :options="pageSizeOptions" />
          </FormField>
        </footer>
      </section>

      <ReportReader
        v-if="selectedReport"
        v-show="isReading"
        v-model:mode="previewMode"
        :identity="reportRowKey(selectedReport)"
        :content="validPreview?.content ?? ''"
        :ready="
          Boolean(validPreview) && !isPreviewLoading && !isPreviewError && !previewIdentityMismatch
        "
      >
        <template #title>
          <div class="report-preview-title">
            <Button
              class="report-directory-back"
              variant="ghost"
              size="sm"
              @click="returnToDirectory"
              ><ArrowLeft :size="16" aria-hidden="true" />返回列表</Button
            >
            <h1 ref="reportHeading" tabindex="-1">{{ reportFilename(selectedReport) }}</h1>
          </div>
        </template>
        <template #actions>
          <a class="report-download-link" :href="reportContentUrl(selectedReport)" download
            >下载完整报告</a
          >
          <Popover align="end" size="md">
            <template #trigger><Button variant="ghost" size="sm">更多</Button></template>
            <div class="report-file-info">
              <RouterLink
                :to="{
                  name: 'WorkspaceVulnerabilities',
                  params: { id: selectedReport.project_id },
                }"
                >打开项目</RouterLink
              >
              <dl>
                <dt>项目</dt>
                <dd>{{ selectedReport.project_title }}</dd>
                <dt>生成于</dt>
                <dd>{{ selectedReport.created_at }}</dd>
                <dt>大小</dt>
                <dd>{{ bytes(selectedReport.artifact_size) }}</dd>
                <dt>SHA256</dt>
                <dd class="mono">{{ selectedReport.artifact_sha256 || '—' }}</dd>
                <template v-if="selectedReport.template_key">
                  <dt>模板</dt>
                  <dd>
                    {{ selectedReport.template_key
                    }}<template v-if="selectedReport.template_version != null">
                      · v{{ selectedReport.template_version }}</template
                    >
                  </dd>
                </template>
              </dl>
            </div>
          </Popover>
        </template>
        <template #notice>
          <Alert
            v-if="validPreview?.truncated"
            class="report-preview-alert"
            tone="info"
            title="当前仅显示报告开头"
          >
            已读取 {{ bytes(validPreview.bytes_read) }}，完整报告共
            {{ bytes(validPreview.size) }}。目录仅覆盖已载入部分，请下载原文件获取全部内容。
          </Alert>
        </template>
        <template #state>
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
        </template>
      </ReportReader>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft } from '@lucide/vue'
import { useQuery } from '@tanstack/vue-query'
import { fetchArtifactPreview, listReports, type ArtifactPreview } from '@/api/client'
import type { ReportInventoryFilters, ReportInventoryItem, ReportInventoryPage } from '@/api/types'
import { ErrorState, FilterBar } from '@/components/patterns'
import {
  Alert,
  Button,
  EmptyState,
  FormField,
  Input,
  Popover,
  Select,
  Skeleton,
  Spinner,
} from '@/components/ui'
import ReportReader from '@/features/reports/ReportReader.vue'
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
  router.replace({ query: { ...route.query, ...changes } })
const filterModel = (key: string) =>
  computed({
    get: () => queryString(route.query[key]),
    set: (value: string) =>
      void replaceQuery({
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
const secondaryFilterSummary = computed(() =>
  [
    templateSelection.value ? `模板：${templateSelection.value}` : '',
    dateFromSelection.value ? `起：${dateFromSelection.value}` : '',
    dateToSelection.value ? `止：${dateToSelection.value}` : '',
  ]
    .filter(Boolean)
    .join(' · '),
)
const hasFilters = computed(() =>
  Boolean(searchSelection.value || projectSelection.value || secondaryFilterSummary.value),
)
function clearFilters() {
  void replaceQuery({
    reportQ: undefined,
    reportProject: undefined,
    reportTemplate: undefined,
    dateFrom: undefined,
    dateTo: undefined,
    page: undefined,
    reportId: undefined,
    reportProjectId: undefined,
  })
}
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
    void replaceQuery({
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
const previewMode = ref<'rendered' | 'raw'>('rendered')
// 列表态只收起阅读区，筛选、选择、正文模式与滚动位置都留在原上下文。
const directoryVisible = ref(false)
const directory = ref<HTMLElement | null>(null)
const reportHeading = ref<HTMLElement | null>(null)
const isReading = computed(() => Boolean(selectedReport.value) && !directoryVisible.value)
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
  queryFn: ({ queryKey, signal }) => {
    const [, , projectId, artifactId] = queryKey as ReturnType<typeof reportKeys.preview>
    return fetchArtifactPreview(projectId, artifactId, signal)
  },
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
async function selectReport(item: ReportInventoryItem) {
  await replaceQuery({ reportProjectId: item.project_id, reportId: item.artifact_id })
  if (!isSelected(item)) return
  directoryVisible.value = false
  await nextTick()
  reportHeading.value?.focus({ preventScroll: true })
}
async function returnToDirectory() {
  directoryVisible.value = true
  await nextTick()
  directory.value
    ?.querySelector<HTMLElement>('[aria-current="true"]')
    ?.focus({ preventScroll: true })
}
function setPage(nextPage: number) {
  void replaceQuery({
    page: nextPage <= 1 ? undefined : String(nextPage),
    reportId: undefined,
    reportProjectId: undefined,
  })
}
function reportContentUrl(item: ReportInventoryItem) {
  return `/projects/${encodeURIComponent(item.project_id)}/artifacts/${encodeURIComponent(item.artifact_id)}/content`
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
    void replaceQuery({ reportId: undefined, reportProjectId: undefined })
})
watch([previewProjectId, previewArtifactId], () => {
  previewMode.value = 'rendered'
  directoryVisible.value = false
})
</script>

<style src="./ReportManagement.css"></style>
