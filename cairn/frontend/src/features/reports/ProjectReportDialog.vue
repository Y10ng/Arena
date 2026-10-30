<template>
  <Dialog
    :open="open"
    title="项目报告"
    description="查看已有报告，或选择模板生成项目总报告。"
    size="lg"
    close-label="关闭项目报告"
    :dismissible="!requestPending"
    @update:open="handleOpenChange"
    @close-auto-focus="emit('closeAutoFocus', $event)"
  >
    <section
      v-if="inventoryLoading || inventoryQuery.isError.value || availableReport"
      class="report-available"
      aria-label="已有报告"
    >
      <header class="report-section-heading">
        <h2>已有报告</h2>
        <RouterLink
          v-if="availableReport && inventoryTotal > 1"
          :to="reportListLink"
          @click="handleOpenChange(false)"
        >
          全部报告
        </RouterLink>
      </header>
      <div v-if="inventoryLoading" class="report-inline-skeleton" role="status">
        <Skeleton height="3rem" /><span class="sr-only">正在读取已有报告…</span>
      </div>
      <ErrorState
        v-else-if="inventoryQuery.isError.value && !availableReport"
        title="已有报告加载失败"
        :description="errorMessage(inventoryQuery.error.value)"
        @retry="inventoryQuery.refetch()"
      />
      <template v-if="availableReport">
        <div class="report-available-content">
          <strong class="report-available-name" :title="availableReportFilename">{{
            availableReportFilename
          }}</strong>
          <Badge v-if="availableReport.source_status === 'deleted'" tone="neutral" size="sm">历史档案 · 来源已删除</Badge>
          <Badge v-else-if="availableReport.source_status === 'unknown'" tone="neutral" size="sm">历史档案 · 来源状态未知</Badge>
          <div class="report-available-meta">
            <time :datetime="availableReport.created_at">{{
              dateTime(availableReport.created_at)
            }}</time>
            <span
              >模板：{{ availableReport.template_key
              }}<template v-if="availableReport.template_version != null">
                · v{{ availableReport.template_version }}</template
              ></span
            >
          </div>
          <div class="report-result-actions">
            <RouterLink
              :to="availableReportLink"
              class="report-read-link"
              @click="handleOpenChange(false)"
              >阅读报告</RouterLink
            >
            <ReportExportMenu
              :identity="`${projectId}:${availableReport.artifact_id}`"
              :filename="availableReportFilename.replace(/\.md$/i, '')"
              :load-markdown="loadReportMarkdown"
            />
          </div>
        </div>
        <Alert v-if="inventoryQuery.isError.value" tone="warning" title="已有报告刷新失败">
          {{ errorMessage(inventoryQuery.error.value) }}
        </Alert>
      </template>
    </section>

    <section class="report-generation" aria-label="本次生成">
      <header class="report-section-heading">
        <h2>本次生成</h2>
        <div class="report-heading-status">
          <Spinner v-if="reportBackgroundFetching" size="sm" decorative />
          <Badge :tone="reportMeta.tone" size="sm">{{ reportMeta.label }}</Badge>
          <Badge v-if="report?.source_status === 'deleted'" tone="neutral" size="sm">历史档案 · 来源已删除</Badge>
          <Button
            v-if="reportEligible"
            variant="ghost"
            size="sm"
            :loading="reportBackgroundFetching"
            :disabled="reportInitialLoading || requestPending"
            @click="retryReport"
            >刷新状态</Button
          >
        </div>
      </header>
      <Alert v-if="!capability.supported" tone="neutral" title="当前场景未启用报告能力">
        {{ capability.reason || '该项目的冻结场景未声明手动报告能力。' }}
      </Alert>
      <Alert v-else-if="projectStatus === 'active'" tone="neutral">
        请先结束评估或等待项目完成，再基于已记录成果手动生成报告。
      </Alert>
      <template v-else>
        <Alert v-if="projectStatus === 'paused'" tone="neutral">
          请先结束评估或等待项目完成，再基于已记录成果手动生成报告。暂停仅保留未完成工作，不会结束评估。
        </Alert>
        <Alert v-if="projectStatus === 'stopped'" tone="warning" title="将生成结束时点报告">
          报告基于结束前已记录成果和冻结输入快照，属于未完成评估报告，不会恢复自治探索。若旧 Worker
          尚未完全退出，请等待任务静默后重试。
        </Alert>
        <ErrorState
          v-if="reportInitialError"
          title="报告状态加载失败"
          :description="errorMessage(reportQuery.error.value)"
          @retry="retryReport"
        />
        <div v-else-if="reportInitialLoading" class="report-inline-skeleton" role="status">
          <Skeleton height="6rem" />
          <span class="sr-only">正在读取报告状态…</span>
        </div>
        <template v-else>
          <Alert v-if="reportStaleError" tone="warning" title="报告状态刷新失败">
            {{ errorMessage(reportQuery.error.value) }}
          </Alert>
          <template v-if="eligible">
            <ErrorState
              v-if="catalogError"
              title="报告模板目录加载失败"
              :description="errorMessage(catalogQuery.error.value)"
              @retry="retryCatalog"
            />
            <div v-else-if="catalogLoading" class="report-inline-skeleton">
              <Skeleton height="4rem" />
            </div>
            <Alert v-else-if="catalog.length === 0" tone="warning" title="暂无可用报告模板">
              请联系超级管理员添加含正文的兼容模板。
            </Alert>
            <Alert
              v-else-if="compatibleTemplates.length === 0"
              tone="warning"
              title="没有兼容的报告模板"
            >
              当前项目需要兼容 profile：{{ capability.template_profile || '未知' }}。
            </Alert>
            <Alert v-else-if="previousTemplateUnavailable" tone="warning">
              上次使用的模板已不兼容，本次已选择系统默认模板；历史报告不变。
            </Alert>
            <div
              v-if="!catalogError && !catalogLoading && compatibleTemplates.length"
              class="report-controls"
            >
              <FormField
                v-slot="{ controlAttrs }"
                label="报告模板"
                :error="
                  selectedTemplateMissing ? '所选模板已删除或不再兼容，请重新选择。' : undefined
                "
              >
                <Select
                  v-model="selectedTemplateValue"
                  v-bind="controlAttrs"
                  :options="templateOptions"
                  :disabled="generationActive || requestPending"
                />
              </FormField>
              <Button :loading="requestPending" :disabled="!canRequest" @click="requestReport">
                {{ actionLabel }}
              </Button>
            </div>
          </template>
          <Alert
            v-if="requestError"
            :tone="requestUncertain ? 'warning' : 'danger'"
            :title="requestUncertain ? '报告请求结果未确认' : '报告请求失败'"
          >
            {{ requestError }}
            <template v-if="requestUncertain" #actions>
              <Button
                variant="secondary"
                size="sm"
                :loading="reportBackgroundFetching"
                @click="retryReport"
                >刷新报告状态</Button
              >
            </template>
          </Alert>

          <p v-if="nextRetryAt" class="report-retry-time">下次重试：{{ dateTime(nextRetryAt) }}</p>
          <Alert v-if="report?.last_error" tone="danger" title="报告生成错误">
            {{ generationErrorSummary }}
          </Alert>
          <details v-if="report" class="report-technical-details">
            <summary>生成详情</summary>
            <dl class="report-detail-list">
              <div>
                <dt>报告尝试</dt>
                <dd>{{ report?.attempt_count ?? 0 }}</dd>
              </div>
              <div v-if="report?.requested_at">
                <dt>请求时间</dt>
                <dd>{{ dateTime(report.requested_at) }}</dd>
              </div>
              <div v-if="report?.requested_by">
                <dt>请求人</dt>
                <dd>{{ report.requested_by }}</dd>
              </div>
              <div v-if="report?.template_key">
                <dt>模板来源</dt>
                <dd>
                  <strong
                    >{{ report.template_key
                    }}<template v-if="report.template_version != null">
                      · v{{ report.template_version }}</template
                    ></strong
                  >
                  <code v-if="report.template_digest_sha256" class="report-digest">
                    {{ report.template_digest_sha256 }}
                  </code>
                </dd>
              </div>
              <div v-if="report?.report_execution_plan_sha256">
                <dt>执行方案</dt>
                <dd>
                  <code class="report-digest">{{ report.report_execution_plan_sha256 }}</code>
                </dd>
              </div>
              <div v-if="report?.input_snapshot_artifact_id">
                <dt>输入快照</dt>
                <dd>
                  <RouterLink :to="artifactLink(report.input_snapshot_artifact_id)">
                    {{ report.input_snapshot_artifact_id }}
                  </RouterLink>
                </dd>
              </div>
              <div v-if="report?.artifact_id">
                <dt>关联报告 Artifact</dt>
                <dd>
                  <RouterLink :to="artifactLink(report.artifact_id)">{{
                    report.artifact_id
                  }}</RouterLink>
                </dd>
              </div>
              <div v-if="report.last_error_code">
                <dt>错误码</dt>
                <dd>
                  <code>{{ report.last_error_code }}</code>
                </dd>
              </div>
              <div v-if="report.last_error">
                <dt>原始错误</dt>
                <dd class="report-full-text">{{ report.last_error }}</dd>
              </div>
            </dl>
          </details>
        </template>
      </template>
    </section>
  </Dialog>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'

import {
  ApiError,
  getAssessmentReportFinalization,
  listReports,
  listReportTemplateCatalog,
  requestAssessmentReportFinalization,
} from '@/api/client'
import type {
  AssessmentReportFinalization,
  ProjectDetail,
  ReportCapability,
  ReportInventoryPage,
  ReportTemplateCatalogEntry,
} from '@/api/types'
import { ErrorState } from '@/components/patterns'
import { Alert, Badge, Button, Dialog, FormField, Select, Skeleton, Spinner } from '@/components/ui'
import { dateTime } from '@/lib/format'
import { useUIStore } from '@/stores/ui'
import { reportNextRetryAt, reportPollInterval, reportStateMeta } from './reportModel'
import { reportKeys } from './reportQueries'
import ReportExportMenu from './ReportExportMenu.vue'
import { fetchReportMarkdown } from './reportExport'

const props = defineProps<{
  open: boolean
  projectId: string
  projectStatus: ProjectDetail['project']['status']
  capability: ReportCapability
}>()

const emit = defineEmits<{
  (event: 'update:open', value: boolean): void
  (event: 'closeAutoFocus', value: Event): void
}>()

const uiStore = useUIStore()
const queryClient = useQueryClient()
const selectedTemplateValue = ref('')
const requestPending = ref(false)
const requestError = ref('')
const requestUncertain = ref(false)
let requestGeneration = 0
const eligible = computed(
  () =>
    props.capability.supported &&
    (props.projectStatus === 'completed' || props.projectStatus === 'stopped'),
)
const dialogEligible = computed(() => props.open && eligible.value && Boolean(props.projectId))
const reportEligible = computed(
  () => eligible.value || (props.capability.supported && props.projectStatus === 'paused'),
)
const dialogReportEligible = computed(
  () => props.open && reportEligible.value && Boolean(props.projectId),
)

const inventoryQuery = useQuery<ReportInventoryPage>({
  queryKey: computed(() => reportKeys.list({ project_id: props.projectId, page: 1, page_size: 1 })),
  queryFn: ({ queryKey, signal }) => {
    const [, , , projectId] = queryKey as ReturnType<typeof reportKeys.list>
    return listReports({ project_id: projectId, page: 1, page_size: 1 }, signal)
  },
  enabled: computed(() => props.open && Boolean(props.projectId)),
  refetchOnWindowFocus: false,
})
const availableReport = computed(() => inventoryQuery.data.value?.items[0] ?? null)
const inventoryTotal = computed(() => inventoryQuery.data.value?.total ?? 0)
const inventoryLoading = computed(() => props.open && inventoryQuery.isPending.value)
const availableReportFilename = computed(() => {
  const uri = availableReport.value?.artifact_uri ?? ''
  const filename = uri.split('/').pop() || uri
  try {
    return decodeURIComponent(filename)
  } catch {
    return filename
  }
})
const reportListLink = computed(() => ({
  name: 'Reports',
  query: { reportProject: props.projectId },
}))
const availableReportLink = computed(() => ({
  ...reportListLink.value,
  query: {
    ...reportListLink.value.query,
    reportProjectId: props.projectId,
    reportId: availableReport.value?.artifact_id,
  },
}))

const catalogQuery = useQuery<ReportTemplateCatalogEntry[]>({
  queryKey: ['reportTemplateCatalog'],
  queryFn: ({ signal }) => listReportTemplateCatalog(signal),
  enabled: dialogEligible,
  staleTime: 60000,
})
const reportQuery = useQuery<AssessmentReportFinalization | null>({
  queryKey: computed(() => ['workspaceReportFinalization', props.projectId]),
  queryFn: ({ queryKey, signal }) => {
    const requestProjectId = String(queryKey[1] || '')
    return getAssessmentReportFinalization(requestProjectId, signal)
  },
  enabled: dialogReportEligible,
  refetchOnWindowFocus: false,
  refetchInterval: (query) =>
    dialogReportEligible.value && !query.state.error ? reportPollInterval(query.state.data) : false,
})

const catalog = computed(() => catalogQuery.data.value || [])
const compatibleTemplates = computed(() => {
  const profile = props.capability.template_profile
  if (!profile) return []
  return catalog.value.filter(
    (template) => !template.validation_error && template.compatibility_profiles.includes(profile),
  )
})
const selectedTemplate = computed(() =>
  compatibleTemplates.value.find((template) => template.key === selectedTemplateValue.value),
)
const selectedTemplateMissing = computed(
  () =>
    Boolean(selectedTemplateValue.value) &&
    !selectedTemplate.value &&
    !catalogQuery.isPending.value &&
    !catalogQuery.isError.value,
)
const templateOptions = computed(() => {
  const options: { value: string; label: string; disabled?: boolean }[] =
    compatibleTemplates.value.map((template) => ({
      value: template.key,
      label: template.name,
    }))
  if (selectedTemplateMissing.value) {
    options.unshift({
      value: selectedTemplateValue.value,
      label: `${selectedTemplateValue.value}（不可用）`,
      disabled: true,
    })
  }
  return options
})
const report = computed(() => (reportEligible.value ? reportQuery.data.value || null : null))
const previousTemplateUnavailable = computed(() => {
  const key = report.value?.template_key
  return Boolean(
    key &&
    selectedTemplate.value?.is_default &&
    !compatibleTemplates.value.some((item) => item.key === key),
  )
})
const loadReportMarkdown = (signal: AbortSignal) => {
  const artifactId = availableReport.value?.artifact_id
  if (!artifactId) return Promise.reject(new Error('报告成品尚未就绪。'))
  return fetchReportMarkdown(
    `/projects/${encodeURIComponent(props.projectId)}/artifacts/${encodeURIComponent(artifactId)}/content`,
    signal,
  )
}
const retryClock = ref(Date.now())
const nextRetryAt = computed(() => reportNextRetryAt(report.value, retryClock.value))
let retryTimer: ReturnType<typeof setTimeout> | undefined
watch(
  [() => report.value?.next_attempt_at, () => report.value?.status, () => props.open],
  () => {
    clearTimeout(retryTimer)
    retryClock.value = Date.now()
    const scheduled = reportNextRetryAt(report.value, retryClock.value)
    if (props.open && scheduled) {
      retryTimer = setTimeout(
        () => {
          retryClock.value = Date.now()
        },
        Date.parse(scheduled) - Date.now() + 1,
      )
    }
  },
  { immediate: true },
)
onBeforeUnmount(() => clearTimeout(retryTimer))
const generationActive = computed(
  () =>
    report.value?.status === 'pending' ||
    report.value?.status === 'running' ||
    Boolean(nextRetryAt.value),
)
const actionLabel = computed(() => {
  if (nextRetryAt.value) return '等待重试'
  if (report.value?.status === 'failed') return '重试生成'
  if (availableReport.value) return '再次生成'
  return '生成报告'
})
const generationErrorSummary = computed(() =>
  report.value?.last_error_code === 'model_output_limit'
    ? '输出达到上限，当前段未完成。'
    : '生成未完成，原因请展开生成详情查看。',
)
const canRequest = computed(
  () =>
    dialogEligible.value &&
    !generationActive.value &&
    !requestPending.value &&
    !requestUncertain.value &&
    !reportQuery.isPending.value &&
    !reportQuery.isError.value &&
    Boolean(selectedTemplate.value),
)
const reportStatus = computed(() => {
  if (!props.capability.supported) return 'not-eligible'
  if (!reportEligible.value) return 'not-started'
  return report.value?.status || 'not-started'
})
const reportMeta = computed(() => {
  if (reportInitialLoading.value) return { label: '读取中', tone: 'neutral' as const }
  if (reportInitialError.value) return { label: '状态未确认', tone: 'warning' as const }
  return nextRetryAt.value
    ? { label: '等待重试', tone: 'warning' as const }
    : reportStateMeta(reportStatus.value)
})
watch([() => report.value?.status, () => report.value?.artifact_id], ([status]) => {
  if (props.open && status === 'ready')
    void queryClient.invalidateQueries({ queryKey: reportKeys.lists() })
})
const reportInitialLoading = computed(
  () =>
    dialogReportEligible.value &&
    reportQuery.isPending.value &&
    reportQuery.data.value === undefined,
)
const reportInitialError = computed(
  () =>
    dialogReportEligible.value && reportQuery.isError.value && reportQuery.data.value === undefined,
)
const reportStaleError = computed(
  () =>
    dialogReportEligible.value && reportQuery.isError.value && reportQuery.data.value !== undefined,
)
const reportBackgroundFetching = computed(
  () =>
    dialogReportEligible.value &&
    reportQuery.isFetching.value &&
    reportQuery.data.value !== undefined,
)
const catalogLoading = computed(() => dialogEligible.value && catalogQuery.isPending.value)
const catalogError = computed(() => dialogEligible.value && catalogQuery.isError.value)

const REPORT_REQUEST_ERROR_MESSAGES: Record<string, string> = {
  project_pause_unconfirmed:
    '尚未取得旧执行清理完成的可信证明，暂不能生成报告。请联系管理员核查。（关联码：project_pause_unconfirmed）',
  assessment_report_project_active: '项目仍在运行，请先结束评估或等待项目完成。',
  assessment_report_project_paused: '项目仅暂停，未结束评估。请先结束评估再生成部分报告。',
  project_pause_draining: '旧执行仍在退出或清理，请等待收尾完成后重试。',
  assessment_report_tasks_not_quiescent:
    '仍有评估任务正在排队或运行，请等待 Worker 完全退出后重试。',
  assessment_report_stopped_tasks_not_quiescent:
    '仍有评估任务正在排队或运行，请等待 Worker 完全退出后重试。',
  assessment_report_finalization_in_progress: '报告生成仍在进行，请等待当前任务结束。',
  report_template_not_found: '所选模板已不存在，请刷新目录后重新选择。',
  assessment_report_template_content_missing: '所选模板尚无正文，请联系管理员补全后再生成。',
  assessment_report_template_invalid:
    '所选模板缺少必要槽位或语法不合法，请先在报告模板中完成转换，再重新生成。',
}

const errorMessage = (error: unknown) => {
  const message = error instanceof Error ? error.message : error ? String(error) : '未知错误'
  if (
    error instanceof ApiError &&
    error.data &&
    typeof error.data === 'object' &&
    'detail' in error.data
  ) {
    const detail = error.data.detail
    if (
      detail &&
      typeof detail === 'object' &&
      'code' in detail &&
      detail.code === 'assessment_report_template_invalid'
    ) {
      return `${message} 请先在报告模板中修正格式；已有报告不受影响。`
    }
  }
  return REPORT_REQUEST_ERROR_MESSAGES[message] || message
}
const artifactLink = (artifactId: string) => ({
  name: 'WorkspaceArtifacts',
  params: { id: props.projectId },
  query: { artifactId },
})
const retryReport = async () => {
  const requestProjectId = props.projectId
  const result = await reportQuery.refetch()
  if (props.projectId === requestProjectId && !result.isError && requestUncertain.value) {
    requestUncertain.value = false
    requestError.value = ''
  }
}
const retryCatalog = () => void catalogQuery.refetch()

function handleOpenChange(open: boolean) {
  if (!open && requestPending.value) return
  emit('update:open', open)
}

const requestReport = async () => {
  const template = selectedTemplate.value
  if (!canRequest.value || !template) return
  const requestProjectId = props.projectId
  const generation = ++requestGeneration
  requestPending.value = true
  requestError.value = ''
  try {
    const finalization = await requestAssessmentReportFinalization(requestProjectId, {
      template_key: template.key,
    })
    queryClient.setQueryData(['workspaceReportFinalization', requestProjectId], finalization)
    if (generation === requestGeneration && props.projectId === requestProjectId) {
      uiStore.showToast('报告生成请求已提交', 'success')
    }
  } catch (error) {
    if (generation === requestGeneration && props.projectId === requestProjectId) {
      requestUncertain.value = !(
        error instanceof ApiError &&
        error.status >= 400 &&
        error.status < 500
      )
      requestError.value = requestUncertain.value
        ? `${errorMessage(error)}。服务端可能已受理，请先刷新报告状态，不要重复提交。`
        : errorMessage(error)
      uiStore.showToast(
        `${requestUncertain.value ? '报告请求结果未确认' : '报告生成请求失败'}：${requestError.value}`,
        requestUncertain.value ? 'warn' : 'danger',
      )
      void queryClient.invalidateQueries({ queryKey: ['reportTemplateCatalog'] })
    }
  } finally {
    if (generation === requestGeneration) requestPending.value = false
  }
}

watch(
  [() => props.projectId, compatibleTemplates, report, () => reportQuery.isPending.value],
  ([, templates, finalization, reportPending]) => {
    // 初次保留兼容的历史选择；显式选择失效则留在原位要求重选，不能暗中替换。
    if (selectedTemplateValue.value || reportPending) return
    const pinnedKey = finalization?.template_key
    selectedTemplateValue.value =
      templates.find((item) => item.key === pinnedKey)?.key ??
      templates.find((item) => item.is_default)?.key ??
      ''
  },
  { immediate: true },
)

watch(
  () => props.projectId,
  () => {
    requestGeneration += 1
    requestPending.value = false
    requestError.value = ''
    requestUncertain.value = false
    selectedTemplateValue.value = ''
  },
  { flush: 'sync' },
)
</script>

<style scoped>
.report-available {
  display: grid;
  gap: var(--space-3);
  padding-bottom: var(--space-4);
  margin-bottom: var(--space-4);
  border-bottom: 1px solid var(--border-subtle);
}
.report-section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.report-section-heading h2 {
  margin: 0;
  font-size: var(--text-lg);
  font-weight: var(--font-semibold);
}
.report-section-heading > a {
  color: var(--accent);
}
.report-generation,
.report-available-content {
  display: grid;
  gap: var(--space-3);
  min-width: 0;
}
.report-available-name {
  overflow-wrap: anywhere;
  font-weight: var(--font-medium);
}
.report-available-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2) var(--space-4);
  color: var(--text-muted);
  font-size: var(--text-sm);
}
.report-heading-status,
.report-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
}
.report-controls {
  align-items: flex-end;
}
.report-controls > :first-child {
  flex: 1;
}
.report-result-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
}
.report-read-link {
  display: inline-flex;
  align-items: center;
  min-height: var(--control-height);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--accent);
  text-decoration: none;
}
.report-read-link:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.report-retry-time {
  color: var(--text-muted);
  font-size: var(--text-sm);
}
.report-technical-details {
  margin-top: var(--space-3);
}
.report-technical-details summary {
  width: fit-content;
  padding-block: var(--space-2);
  color: var(--text-muted);
  cursor: pointer;
}
.report-detail-list {
  display: grid;
  gap: var(--space-3);
  margin: var(--space-2) 0 0;
}
.report-detail-list div {
  min-width: 0;
  display: grid;
  grid-template-columns: 80px minmax(0, 1fr);
  gap: var(--space-3);
}
.report-detail-list dt {
  color: var(--text-muted);
  font-size: var(--text-xs);
}
.report-detail-list dd {
  margin: var(--space-1) 0 0;
  overflow-wrap: anywhere;
}
.report-detail-list a {
  color: var(--accent);
  text-decoration: none;
}
.report-detail-list a:hover {
  text-decoration: underline;
}
.report-digest {
  display: block;
  margin-top: var(--space-1);
  color: var(--text-muted);
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
}
.report-full-text {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.report-inline-skeleton {
  display: grid;
  gap: var(--space-3);
}
@media (max-width: 720px) {
  .report-controls {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
