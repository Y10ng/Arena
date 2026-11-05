<template>
  <div class="vuln-page">
    <h1 class="sr-only">漏洞成果</h1>
    <VulnerabilityFilters
      v-model:search="searchSelection"
      v-model:severity="severitySelection"
      v-model:status="statusSelection"
      v-model:sort="sortSelection"
      :count="visibleVulns.length"
      :fetching="isFetching"
      @refresh="refetch()"
    >
      <template #actions>
        <Button variant="secondary" :disabled="!currentProjectDetail || createOpen" @click="openReport">生成报告</Button>
        <Button :disabled="reportDialogOpen" @click="openCreate">录入漏洞</Button>
      </template>
    </VulnerabilityFilters>

    <div
      v-if="isLoading && !hasCurrentSnapshot"
      class="vuln-skeleton"
      role="status"
      aria-label="正在加载漏洞"
    >
      <div v-for="index in 4" :key="index" class="vuln-skeleton__row"><Skeleton class="vuln-skeleton__line" shape="text" /></div>
    </div>

    <ErrorState
      v-else-if="isError && !hasCurrentSnapshot"
      title="漏洞加载失败"
      :description="queryErrorMessage"
      @retry="refetch()"
    />

    <EmptyState v-else-if="!vulns.length" :title="hasActiveFilters ? '没有符合条件的漏洞' : '暂无漏洞'">
      <template #icon><ShieldCheck :size="24" /></template>
      <template #actions>
        <Button size="sm" @click="openCreate">录入漏洞</Button>
      </template>
    </EmptyState>

    <ErrorState
      v-else-if="isError"
      title="漏洞同步失败"
      :description="queryErrorMessage"
      @retry="refetch()"
    />

    <ProjectReportDialog
      v-if="currentProjectDetail"
      v-model:open="reportDialogOpen"
      :project-id="projectId"
      :project-status="currentProjectDetail.project.status"
      :capability="currentProjectDetail.project.report_capability"
      @close-auto-focus="restoreReportTriggerFocus"
    />

    <EmptyState v-if="vulns.length && !visibleVulns.length" title="没有符合条件的漏洞" />
    <VulnerabilityTable
      v-if="visibleVulns.length || selectedId"
      :rows="visibleVulns"
      :selected-project-id="projectId"
      :selected-id="selectedId"
      @select="selectVuln"
      @status-changed="onStatusChanged"
    />

    <Dialog
      :open="createOpen"
      title="录入漏洞"
      description=""
      size="lg"
      close-label="关闭录入漏洞"
      :dismissible="!creating"
      @update:open="handleCreateOpenChange"
      @open-auto-focus="focusCreateTitle"
      @close-auto-focus="restoreCreateTriggerFocus"
    >
      <Alert v-if="createError" tone="danger" title="录入未成功">{{ createError }}</Alert>
      <form id="create-vulnerability-form" class="vuln-create-form" @submit.prevent="submitCreate">
        <section class="vuln-create-section" aria-label="发现与影响">
          <h3>发现与影响</h3>
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

        <div class="vuln-create-basics">
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

        </div>
        <FormField label="影响目标">
          <template #default="{ controlAttrs }">
            <Input
              v-model="createForm.target"
              v-bind="controlAttrs"
              placeholder="如：POST /api/login param:urlInput"
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
        </section>
        <section class="vuln-create-section" aria-label="复现证据">
          <h3>复现证据</h3>
        <FormField label="复现请求（原始 HTTP 包，可选）">
          <template #default="{ controlAttrs }">
            <Textarea
              v-model="createForm.requestText"
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
              v-model="createForm.responseText"
              v-bind="controlAttrs"
              class="mono"
              :rows="3"
              placeholder="HTTP/1.1 200 OK&#10;..."
            />
          </template>
        </FormField>

        </section>
        <section class="vuln-create-section" aria-label="修复建议">
          <h3>修复建议</h3>
        <FormField label="建议内容">
          <template #default="{ controlAttrs }">
            <Textarea
              v-model="createForm.remediation"
              v-bind="controlAttrs"
              :rows="3"
              placeholder="修复建议（支持 markdown）"
            />
          </template>
        </FormField>

        </section>
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
    <ConfirmAction :open="discardCreateOpen" title="放弃录入？" description="未保存的漏洞正文和 HTTP 原文将丢失。" confirm-label="放弃录入" cancel-label="继续编辑" tone="warning" @update:open="!$event && resolveDiscardCreate(false)" @confirm="resolveDiscardCreate(true)" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { ShieldCheck } from '@lucide/vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import {
  createVulnerability,
  listVulnerabilities,
  type CreateVulnerabilityRequest,
  type Vulnerability,
} from '@/api/client'
import { ConfirmAction, ErrorState } from '@/components/patterns'
import VulnerabilityFilters from './VulnerabilityFilters.vue'
import { sortVulnerabilities } from './vulnerabilitySorting'
import {
  Alert,
  Button,
  Dialog,
  EmptyState,
  FormField,
  Input,
  Select,
  Skeleton,
  Textarea,
} from '@/components/ui'
import ProjectReportDialog from '@/features/reports/ProjectReportDialog.vue'
import { useUIStore } from '@/stores/ui'
import { useWorkspaceStore } from '@/stores/workspace'
import VulnerabilityTable from './VulnerabilityTable.vue'
import {
  VULNERABILITY_SEVERITIES,
  vulnerabilitySeverityMeta,
} from './vulnerabilityMetadata'
import { invalidateVulnerabilityLists, vulnerabilityKeys } from './vulnerabilityQueries'

interface CreateVulnerabilityDraft {
  title: string
  severity: string
  vuln_type: string
  target: string
  remediation: string
  confidence: number
  requestText: string
  responseText: string
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
const router = useRouter()
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
const searchSelection = ref('')
const sortSelection = ref('created')

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
const visibleVulns = computed(() => {
  const needle = searchSelection.value.trim().toLocaleLowerCase()
  return sortVulnerabilities(vulns.value.filter(item => !needle || [item.number, item.id, item.title, item.target, item.vuln_type].some(value => value.toLocaleLowerCase().includes(needle))), sortSelection.value)
})
const hasActiveFilters = computed(
  () => severitySelection.value !== ALL_FILTER || statusSelection.value !== ALL_FILTER,
)
const queryErrorMessage = computed(() =>
  queryError.value instanceof Error && queryError.value.message
    ? queryError.value.message
    : '请稍后重试。',
)

const selectedId = computed(() =>
  (!route.query.vulnerabilityProjectId || route.query.vulnerabilityProjectId === projectId.value) && typeof route.query.vulnerabilityId === 'string'
    ? route.query.vulnerabilityId : null,
)
function selectVuln(_projectId: string, id: string | null) {
  void router.replace({ query: { ...route.query, vulnerabilityProjectId: id ? projectId.value : undefined, vulnerabilityId: id || undefined, vulnerabilityObservationId: undefined } })
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
const createError = ref('')
const discardCreateOpen = ref(false)
let discardCreateResolver: ((value: boolean) => void) | undefined
const createDirty = computed(() => JSON.stringify(createForm.value) !== JSON.stringify(createDefaultDraft()))
function resolveDiscardCreate(discard: boolean) {
  discardCreateOpen.value = false
  discardCreateResolver?.(discard)
  discardCreateResolver = undefined
}
function canLeaveCreate(): boolean | Promise<boolean> {
  if (creating.value) return false
  if (!createOpen.value || !createDirty.value) return true
  if (discardCreateOpen.value) return false
  discardCreateOpen.value = true
  return new Promise<boolean>((resolve) => { discardCreateResolver = resolve })
}
onBeforeRouteLeave(canLeaveCreate)
onBeforeRouteUpdate((to, from) => to.params.id === from.params.id || canLeaveCreate())
function beforeUnload(event: BeforeUnloadEvent) {
  if (!createOpen.value || (!creating.value && !createDirty.value)) return
  event.preventDefault()
  event.returnValue = ''
}
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => { resolveDiscardCreate(false); window.removeEventListener('beforeunload', beforeUnload) })

function createDefaultDraft(): CreateVulnerabilityDraft {
  return {
    title: '',
    severity: 'medium',
    vuln_type: '',
    target: '',
    remediation: '',
    confidence: 0.6,
    requestText: '',
    responseText: '',
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
  createError.value = ''
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

async function closeCreate() {
  if (await canLeaveCreate()) createOpen.value = false
}

function errorText(error: unknown) {
  return error instanceof Error && error.message ? error.message : '创建失败'
}

async function submitCreate() {
  if (creating.value || !createForm.value.title.trim()) return

  const requestProjectId = projectId.value
  const requestVisitGeneration = projectVisitGeneration.value
  const requestToken = ++createRequestToken
  const body: CreateVulnerabilityRequest = {
    title: createForm.value.title,
    severity: createForm.value.severity,
    vuln_type: createForm.value.vuln_type,
    target: createForm.value.target,
    remediation: createForm.value.remediation,
    confidence: createForm.value.confidence,
    evidence_items:
      createForm.value.requestText || createForm.value.responseText
        ? [
            {
              id: 'manual-http',
              kind: 'http',
              role: 'primary',
              title: '人工记录',
              source: 'manual',
              request: null,
              response: null,
              artifact: null,
              text: null,
              request_text: createForm.value.requestText || null,
              response_text: createForm.value.responseText || null,
            },
          ]
        : [],
  }

  creating.value = true
  createError.value = ''
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
      createError.value = errorText(error)
      uiStore.showToast(createError.value, 'danger')
    }
  } finally {
    if (requestToken === createRequestToken) creating.value = false
  }
}

watch(
  projectId,
  () => {
    searchSelection.value = ''
    sortSelection.value = 'created'
    projectVisitGeneration.value += 1
    createRequestToken += 1
    creating.value = false
    reportDialogOpen.value = false
    reportTrigger.value = null
    createOpen.value = false
  },
  { flush: 'sync' },
)
</script>

<style src="./VulnerabilitiesPage.css" scoped></style>
