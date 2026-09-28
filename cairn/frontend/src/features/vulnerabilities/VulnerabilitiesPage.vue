<template>
  <div class="vuln-page">
    <div class="vuln-header vuln-header--actions">
      <div class="vuln-header-actions">
        <Button size="sm" :disabled="!currentProjectDetail || createOpen" @click="openReport">
          生成报告
        </Button>
        <Button variant="secondary" size="sm" :disabled="reportDialogOpen" @click="openCreate">
          ＋ 录入漏洞
        </Button>
      </div>
    </div>

    <FilterBar label="漏洞筛选">
      <FormField label="严重度">
        <template #default="{ controlAttrs }">
          <Select
            v-model="severitySelection"
            v-bind="controlAttrs"
            :options="filterSeverityOptions"
          />
        </template>
      </FormField>
      <FormField label="状态">
        <template #default="{ controlAttrs }">
          <Select v-model="statusSelection" v-bind="controlAttrs" :options="statusOptions" />
        </template>
      </FormField>

      <template v-if="isFetching && vulns.length" #status>同步中…</template>
      <template #actions>
        <Button variant="secondary" size="sm" :loading="isFetching" @click="refetch()">
          刷新
        </Button>
      </template>
    </FilterBar>

    <div
      v-if="isLoading && !hasCurrentSnapshot"
      class="vuln-skeleton"
      role="status"
      aria-label="正在加载漏洞"
    >
      <Card v-for="index in 4" :key="index" padding="sm">
        <Skeleton class="vuln-skeleton__line" shape="text" />
      </Card>
    </div>

    <ErrorState
      v-else-if="isError && !hasCurrentSnapshot"
      title="漏洞加载失败"
      :description="queryErrorMessage"
      @retry="refetch()"
    />

    <EmptyState v-else-if="!vulns.length" title="暂无漏洞" :description="emptyDescription">
      <template #icon>◇</template>
      <template #actions>
        <Button size="sm" @click="openCreate">录入漏洞</Button>
      </template>
    </EmptyState>

    <template v-else>
      <ErrorState
        v-if="isError"
        title="漏洞同步失败"
        :description="queryErrorMessage"
        @retry="refetch()"
      />

      <Card padding="none" class="vuln-table-card">
        <DataTable
          :rows="vulns"
          :columns="vulnerabilityColumns"
          :row-key="vulnerabilityRowKey"
          caption="项目漏洞列表"
          density="compact"
          :is-row-active="isVulnerabilityActive"
          active-row-aria="current"
          :action-label="vulnerabilityActionLabel"
          action-column-label="详情"
          @row-action="selectVuln"
        >
          <template #cell-vulnerability="{ row }">
            <div class="vuln-summary-cell">
              <Badge :tone="vulnerabilitySeverityMeta(row.severity).tone" size="sm">
                {{ vulnerabilitySeverityMeta(row.severity).label }}
              </Badge>
              <span class="vuln-title">{{ row.title }}</span>
              <span v-if="row.vuln_type" class="vuln-type">{{ row.vuln_type }}</span>
            </div>
          </template>
          <template #cell-confidence="{ row }">
            <Badge :tone="vulnerabilityConfidenceMeta(row.confidence).tone" size="sm">
              {{ vulnerabilityConfidenceMeta(row.confidence).label }}
            </Badge>
          </template>
          <template #cell-status="{ row }">
            <Badge :tone="vulnerabilityStatusMeta(row.status).tone" size="sm">
              {{ vulnerabilityStatusMeta(row.status).label }}
            </Badge>
          </template>
          <template #cell-created="{ row }">
            <span class="vuln-time">{{ formatRelativeTime(row.created_at) }}</span>
          </template>
        </DataTable>
      </Card>
    </template>

    <ProjectReportDialog
      v-if="currentProjectDetail"
      v-model:open="reportDialogOpen"
      :project-id="projectId"
      :project-status="currentProjectDetail.project.status"
      :capability="currentProjectDetail.project.report_capability"
      @close-auto-focus="restoreReportTriggerFocus"
    />

    <!-- 详情抽屉：复用组件，聚合复现数据包/来源分析/执行日志 -->
    <VulnerabilityDrawer
      :project-id="projectId"
      :vuln-id="selectedId"
      :open="Boolean(selectedId)"
      @close="selectedId = null"
      @select-vulnerability="selectVulnerabilityById"
      @status-changed="onStatusChanged"
    />

    <Dialog
      :open="createOpen"
      title="录入漏洞"
      description="人工录入项目漏洞、影响范围、修复建议与复现证据。"
      size="md"
      close-label="关闭录入漏洞"
      :dismissible="!creating"
      @update:open="handleCreateOpenChange"
      @open-auto-focus="focusCreateTitle"
      @close-auto-focus="restoreCreateTriggerFocus"
    >
      <form id="create-vulnerability-form" class="vuln-create-form" @submit.prevent="submitCreate">
        <FormField label="漏洞名称" required>
          <template #default="{ controlAttrs }">
            <Input
              ref="createTitleInput"
              v-model="createForm.title"
              v-bind="controlAttrs"
              placeholder="如：登录接口 SQL 注入"
            />
          </template>
        </FormField>

        <FormField label="严重度" required>
          <template #default="{ controlAttrs }">
            <Select
              v-model="createForm.severity"
              v-bind="controlAttrs"
              :options="severityOptions"
            />
          </template>
        </FormField>

        <FormField label="类型">
          <template #default="{ controlAttrs }">
            <Input
              v-model="createForm.vuln_type"
              v-bind="controlAttrs"
              placeholder="如：SQLi / XSS / IDOR"
            />
          </template>
        </FormField>

        <FormField label="影响目标">
          <template #default="{ controlAttrs }">
            <Input
              v-model="createForm.target"
              v-bind="controlAttrs"
              placeholder="如：POST /api/login param:urlInput"
            />
          </template>
        </FormField>

        <FormField label="修复建议">
          <template #default="{ controlAttrs }">
            <Textarea
              v-model="createForm.remediation"
              v-bind="controlAttrs"
              :rows="3"
              placeholder="修复建议（支持 markdown）"
            />
          </template>
        </FormField>

        <FormField label="复现请求（原始 HTTP 包，可选）">
          <template #default="{ controlAttrs }">
            <Textarea
              v-model="createForm.repro_request"
              v-bind="controlAttrs"
              class="mono"
              :rows="4"
              placeholder="GET /api/x?params= HTTP/1.1&#10;Host: xxx.com&#10;&#10;body"
            />
          </template>
        </FormField>

        <FormField label="复现响应（原始 HTTP 包，可选）">
          <template #default="{ controlAttrs }">
            <Textarea
              v-model="createForm.repro_response"
              v-bind="controlAttrs"
              class="mono"
              :rows="3"
              placeholder="HTTP/1.1 200 OK&#10;..."
            />
          </template>
        </FormField>

        <FormField label="可信度 (0-1)">
          <template #default="{ id, describedBy, invalid }">
            <input
              :id="id"
              v-model.number="createForm.confidence"
              type="number"
              min="0"
              max="1"
              step="0.1"
              class="vuln-confidence-input"
              :aria-describedby="describedBy"
              :aria-invalid="invalid || undefined"
            />
          </template>
        </FormField>
      </form>

      <template #footer>
        <Button variant="secondary" :disabled="creating" @click="closeCreate">取消</Button>
        <Button
          form="create-vulnerability-form"
          type="submit"
          :loading="creating"
          :disabled="!createForm.title.trim()"
        >
          创建
        </Button>
      </template>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { createVulnerability, listVulnerabilities, type Vulnerability } from '@/api/client'
import { DataTable, ErrorState, FilterBar, type DataTableColumn } from '@/components/patterns'
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  FormField,
  Input,
  Select,
  Skeleton,
  Textarea,
} from '@/components/ui'
import ProjectReportDialog from '@/features/reports/ProjectReportDialog.vue'
import { formatRelativeTime } from '@/lib/format'
import { useUIStore } from '@/stores/ui'
import { useWorkspaceStore } from '@/stores/workspace'
import VulnerabilityDrawer from './VulnerabilityDrawer.vue'
import {
  VULNERABILITY_SEVERITIES,
  VULNERABILITY_STATUSES,
  vulnerabilityConfidenceMeta,
  vulnerabilitySeverityMeta,
  vulnerabilityStatusMeta,
} from './vulnerabilityMetadata'
import { invalidateVulnerabilityLists, vulnerabilityKeys } from './vulnerabilityQueries'

interface CreateVulnerabilityDraft {
  title: string
  severity: string
  vuln_type: string
  target: string
  remediation: string
  confidence: number
  repro_request: string
  repro_response: string
}

interface VulnerabilityListSnapshot {
  projectId: string
  severity: string
  status: string
  visitGeneration: number
  items: Vulnerability[]
}

const ALL_FILTER = '__all__'
const route = useRoute()
const uiStore = useUIStore()
const workspaceStore = useWorkspaceStore()
const queryClient = useQueryClient()

const projectId = computed(() => String(route.params.id))
const currentProjectDetail = computed(() => {
  const detail = workspaceStore.projectDetail
  return workspaceStore.currentProjectId === projectId.value &&
    detail?.project.id === projectId.value
    ? detail
    : null
})
const projectVisitGeneration = ref(0)
const severitySelection = ref(ALL_FILTER)
const statusSelection = ref(ALL_FILTER)
const severityFilter = computed(() =>
  severitySelection.value === ALL_FILTER ? '' : severitySelection.value,
)
const statusFilter = computed(() =>
  statusSelection.value === ALL_FILTER ? '' : statusSelection.value,
)

const severityOptions = VULNERABILITY_SEVERITIES.map((severity) => ({
  value: severity,
  label: vulnerabilitySeverityMeta(severity).label,
}))
const filterSeverityOptions = [{ value: ALL_FILTER, label: '全部严重度' }, ...severityOptions]
const statusOptions = [
  { value: ALL_FILTER, label: '全部状态' },
  ...VULNERABILITY_STATUSES.map((status) => ({
    value: status,
    label: vulnerabilityStatusMeta(status).label,
  })),
]

const {
  data: vulnsData,
  error: queryError,
  isError,
  isFetching,
  isLoading,
  refetch,
} = useQuery<VulnerabilityListSnapshot>({
  queryKey: computed(() =>
    vulnerabilityKeys.projectList(
      projectId.value,
      severityFilter.value,
      statusFilter.value,
      projectVisitGeneration.value,
    ),
  ),
  queryFn: async ({ queryKey, signal }) => {
    const [, , requestProjectId, requestSeverity, requestStatus, requestVisitGeneration] =
      queryKey as ReturnType<typeof vulnerabilityKeys.projectList>
    const items = await listVulnerabilities(
      requestProjectId,
      requestSeverity || undefined,
      requestStatus || undefined,
      signal,
    )
    return {
      projectId: requestProjectId,
      severity: requestSeverity,
      status: requestStatus,
      visitGeneration: requestVisitGeneration,
      items,
    }
  },
  refetchInterval: 10_000,
})

const currentSnapshot = computed(() => {
  const snapshot = vulnsData.value
  if (
    snapshot?.projectId !== projectId.value ||
    snapshot.severity !== severityFilter.value ||
    snapshot.status !== statusFilter.value ||
    snapshot.visitGeneration !== projectVisitGeneration.value
  ) {
    return null
  }
  return snapshot
})
const hasCurrentSnapshot = computed(() => currentSnapshot.value !== null)
const vulns = computed(() => currentSnapshot.value?.items || [])
const hasActiveFilters = computed(
  () => severitySelection.value !== ALL_FILTER || statusSelection.value !== ALL_FILTER,
)
const emptyDescription = computed(() =>
  hasActiveFilters.value
    ? '当前筛选条件下没有匹配的漏洞记录。'
    : '当前项目暂无漏洞记录；worker 可自动登记，也可手动录入。',
)
const queryErrorMessage = computed(() =>
  queryError.value instanceof Error && queryError.value.message
    ? queryError.value.message
    : '请稍后重试。',
)

const vulnerabilityColumns = [
  { key: 'vulnerability', label: '漏洞' },
  { key: 'confidence', label: '可信度' },
  { key: 'status', label: '状态' },
  { key: 'created', label: '发现时间' },
] satisfies readonly DataTableColumn<
  Vulnerability,
  'vulnerability' | 'confidence' | 'status' | 'created'
>[]

function vulnerabilityRowKey(vulnerability: Vulnerability): string {
  return vulnerability.id
}

const selectedId = ref<string | null>(null)

function selectVuln(vulnerability: Vulnerability) {
  selectedId.value = vulnerability.id
}

function selectVulnerabilityById(vulnerabilityId: string) {
  selectedId.value = vulnerabilityId
}

function isVulnerabilityActive(vulnerability: Vulnerability): boolean {
  return selectedId.value === vulnerability.id
}

function vulnerabilityActionLabel(vulnerability: Vulnerability): string {
  return `查看详情：${vulnerability.title}`
}

function onStatusChanged() {
  void invalidateVulnerabilityLists(queryClient, projectId.value)
}

const reportDialogOpen = ref(false)
const reportTrigger = ref<HTMLElement | null>(null)
const createOpen = ref(false)
const creating = ref(false)
const createTitleInput = ref<InstanceType<typeof Input> | null>(null)
let createRequestToken = 0
const createTrigger = ref<HTMLElement | null>(null)
const createForm = ref<CreateVulnerabilityDraft>(createDefaultDraft())

function createDefaultDraft(): CreateVulnerabilityDraft {
  return {
    title: '',
    severity: 'medium',
    vuln_type: '',
    target: '',
    remediation: '',
    confidence: 0.6,
    repro_request: '',
    repro_response: '',
  }
}

function openReport(event: MouseEvent) {
  if (!currentProjectDetail.value) return
  reportTrigger.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  reportDialogOpen.value = true
}

function restoreReportTriggerFocus(event: Event) {
  event.preventDefault()
  reportTrigger.value?.focus()
}

function openCreate(event: MouseEvent) {
  createTrigger.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  createForm.value = createDefaultDraft()
  createOpen.value = true
}

function focusCreateTitle(event: Event) {
  event.preventDefault()
  void nextTick(() => createTitleInput.value?.focus())
}

function restoreCreateTriggerFocus(event: Event) {
  event.preventDefault()
  createTrigger.value?.focus()
}

function handleCreateOpenChange(open: boolean) {
  if (!open) closeCreate()
}

function closeCreate() {
  if (creating.value) return
  createOpen.value = false
}

function errorText(error: unknown) {
  return error instanceof Error && error.message ? error.message : '创建失败'
}

async function submitCreate() {
  if (creating.value || !createForm.value.title.trim()) return

  const requestProjectId = projectId.value
  const requestVisitGeneration = projectVisitGeneration.value
  const requestToken = ++createRequestToken
  const body: CreateVulnerabilityDraft = {
    title: createForm.value.title,
    severity: createForm.value.severity,
    vuln_type: createForm.value.vuln_type,
    target: createForm.value.target,
    remediation: createForm.value.remediation,
    confidence: createForm.value.confidence,
    repro_request: createForm.value.repro_request,
    repro_response: createForm.value.repro_response,
  }

  creating.value = true
  try {
    await createVulnerability(requestProjectId, body)
    await invalidateVulnerabilityLists(queryClient, requestProjectId)
    if (
      requestToken === createRequestToken &&
      projectId.value === requestProjectId &&
      projectVisitGeneration.value === requestVisitGeneration
    ) {
      createOpen.value = false
      uiStore.showToast('漏洞已录入', 'success')
    }
  } catch (error) {
    if (
      requestToken === createRequestToken &&
      projectId.value === requestProjectId &&
      projectVisitGeneration.value === requestVisitGeneration
    ) {
      uiStore.showToast(errorText(error), 'danger')
    }
  } finally {
    if (requestToken === createRequestToken) creating.value = false
  }
}

watch(
  projectId,
  () => {
    projectVisitGeneration.value += 1
    createRequestToken += 1
    creating.value = false
    selectedId.value = null
    reportDialogOpen.value = false
    reportTrigger.value = null
    createOpen.value = false
  },
  { flush: 'sync' },
)
</script>

<style src="./VulnerabilitiesPage.css" scoped></style>
