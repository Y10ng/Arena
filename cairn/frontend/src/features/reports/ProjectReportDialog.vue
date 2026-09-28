<template>
  <Dialog
    :open="open"
    title="生成报告"
    description="选择兼容的不可变模板版本，生成项目评估报告。"
    size="lg"
    close-label="关闭生成报告"
    :dismissible="!requestPending"
    @update:open="handleOpenChange"
    @close-auto-focus="emit('closeAutoFocus', $event)"
  >
    <template #header-actions>
      <div class="report-heading-status">
        <Spinner v-if="reportBackgroundFetching" size="sm" decorative />
        <Badge :tone="reportMeta.tone" size="sm">{{ reportMeta.label }}</Badge>
      </div>
    </template>

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
            请联系超级管理员发布至少一个未归档模板版本。
          </Alert>
          <Alert
            v-else-if="compatibleTemplates.length === 0"
            tone="warning"
            title="没有兼容的报告模板"
          >
            当前项目需要兼容 profile：{{ capability.template_profile || '未知' }}。
          </Alert>
          <div v-else class="report-controls">
            <FormField
              v-slot="{ controlAttrs }"
              label="报告模板版本"
              hint="生成请求会固定模板、执行方案、有效 Prompt 与权威输入快照。"
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
        <Alert v-if="requestError" tone="danger" title="报告请求失败">
          {{ requestError }}
        </Alert>

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
          <div v-if="report?.template_key && report.template_version">
            <dt>模板来源</dt>
            <dd>
              <strong>{{ report.template_key }} · v{{ report.template_version }}</strong>
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
          <div v-if="report?.next_attempt_at">
            <dt>下次重试</dt>
            <dd>{{ dateTime(report.next_attempt_at) }}</dd>
          </div>
          <div v-if="report?.artifact_id">
            <dt>报告 Artifact</dt>
            <dd>
              <RouterLink :to="artifactLink(report.artifact_id)">{{
                report.artifact_id
              }}</RouterLink>
            </dd>
          </div>
        </dl>
        <Alert v-if="report?.last_error" tone="danger" title="报告生成错误">
          <span class="report-full-text">{{ report.last_error }}</span>
        </Alert>
      </template>
    </template>
  </Dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'

import {
  getAssessmentReportFinalization,
  listReportTemplateCatalog,
  requestAssessmentReportFinalization,
} from '@/api/client'
import type {
  AssessmentReportFinalization,
  ProjectDetail,
  ReportCapability,
  ReportTemplateCatalogEntry,
} from '@/api/types'
import { ErrorState } from '@/components/patterns'
import { Alert, Badge, Button, Dialog, FormField, Select, Skeleton, Spinner } from '@/components/ui'
import { dateTime } from '@/lib/format'
import { useUIStore } from '@/stores/ui'
import { reportStateMeta } from './reportModel'

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
let requestGeneration = 0
const eligible = computed(
  () =>
    props.capability.supported &&
    (props.projectStatus === 'completed' || props.projectStatus === 'stopped'),
)
const dialogEligible = computed(() => props.open && eligible.value && Boolean(props.projectId))
const reportEligible = computed(
  () =>
    eligible.value ||
    (props.capability.supported && props.projectStatus === 'paused'),
)
const dialogReportEligible = computed(
  () => props.open && reportEligible.value && Boolean(props.projectId),
)

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
  refetchInterval: (query) => {
    const finalization = query.state.data
    return finalization && finalization.status !== 'ready' ? 10000 : false
  },
})

const catalog = computed(() => catalogQuery.data.value || [])
const compatibleTemplates = computed(() => {
  const profile = props.capability.template_profile
  if (!profile) return []
  return catalog.value.filter((template) => template.compatibility_profiles.includes(profile))
})
const templateOptions = computed(() =>
  compatibleTemplates.value.map((template) => ({
    value: `${template.key}@${template.version}`,
    label: `${template.name} · v${template.version}`,
  })),
)
const selectedTemplate = computed(() => {
  const [key, versionText] = selectedTemplateValue.value.split('@')
  const version = Number(versionText)
  return compatibleTemplates.value.find(
    (template) => template.key === key && template.version === version,
  )
})
const report = computed(() => (reportEligible.value ? reportQuery.data.value || null : null))
const generationActive = computed(
  () => report.value?.status === 'pending' || report.value?.status === 'running',
)
const actionLabel = computed(() => {
  if (report.value?.status === 'failed') return '重试生成'
  if (report.value?.status === 'ready') return '生成新版本'
  return '生成报告'
})
const canRequest = computed(
  () =>
    dialogEligible.value &&
    !generationActive.value &&
    !requestPending.value &&
    Boolean(selectedTemplate.value),
)
const reportStatus = computed(() => {
  if (!props.capability.supported) return 'not-eligible'
  if (!reportEligible.value) return 'not-started'
  return report.value?.status || 'not-started'
})
const reportMeta = computed(() => reportStateMeta(reportStatus.value))
const reportInitialLoading = computed(
  () =>
    dialogReportEligible.value &&
    reportQuery.isPending.value &&
    reportQuery.data.value === undefined,
)
const reportInitialError = computed(
  () =>
    dialogReportEligible.value &&
    reportQuery.isError.value &&
    reportQuery.data.value === undefined,
)
const reportStaleError = computed(
  () =>
    dialogReportEligible.value &&
    reportQuery.isError.value &&
    reportQuery.data.value !== undefined,
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
}

const errorMessage = (error: unknown) => {
  const message = error instanceof Error ? error.message : error ? String(error) : '未知错误'
  return REPORT_REQUEST_ERROR_MESSAGES[message] || message
}
const artifactLink = (artifactId: string) => ({
  name: 'WorkspaceArtifacts',
  params: { id: props.projectId },
  query: { artifactId },
})
const retryReport = () => void reportQuery.refetch()
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
      template_version: template.version,
    })
    queryClient.setQueryData(['workspaceReportFinalization', requestProjectId], finalization)
    if (generation === requestGeneration && props.projectId === requestProjectId) {
      uiStore.showToast('报告生成请求已提交', 'success')
    }
  } catch (error) {
    if (generation === requestGeneration && props.projectId === requestProjectId) {
      requestError.value = errorMessage(error)
      uiStore.showToast(`报告生成请求失败：${requestError.value}`, 'danger')
    }
  } finally {
    if (generation === requestGeneration) requestPending.value = false
  }
}

watch(
  [compatibleTemplates, report],
  ([templates, finalization]) => {
    const values = new Set(templates.map((item) => `${item.key}@${item.version}`))
    if (values.has(selectedTemplateValue.value)) return

    const pinnedValue =
      finalization?.template_key && finalization.template_version
        ? `${finalization.template_key}@${finalization.template_version}`
        : ''
    if (pinnedValue && values.has(pinnedValue)) {
      selectedTemplateValue.value = pinnedValue
      return
    }

    const first = templates[0]
    selectedTemplateValue.value = first ? `${first.key}@${first.version}` : ''
  },
  { immediate: true },
)

watch(
  () => props.projectId,
  () => {
    requestGeneration += 1
    requestPending.value = false
    requestError.value = ''
    selectedTemplateValue.value = ''
  },
  { flush: 'sync' },
)
</script>

<style scoped>
.report-heading-status,
.report-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
}
.report-controls {
  align-items: flex-end;
  margin-top: var(--space-3);
}
.report-controls > :first-child {
  flex: 1;
}
.report-detail-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
  gap: var(--space-3);
  margin: var(--space-4) 0 0;
}
.report-detail-list div {
  min-width: 0;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
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
