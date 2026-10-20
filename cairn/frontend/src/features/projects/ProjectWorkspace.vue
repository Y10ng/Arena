<template>
  <div class="workspace-layout">
    <Card
      v-if="workspaceStore.isLoading && !workspaceStore.projectDetail"
      class="loading-state"
      padding="lg"
    >
      <Skeleton
        shape="text"
        class="workspace-loading-title"
        :decorative="false"
        label="正在加载工作空间"
      />
      <Skeleton v-for="i in 4" :key="i" class="workspace-loading-row" />
    </Card>

    <ErrorState
      v-else-if="workspaceStore.error && !workspaceStore.projectDetail"
      title="工作空间加载失败"
      :description="workspaceStore.error"
      retry-label="返回项目列表"
      @retry="goBackToList"
    />

    <!-- Main Workspace Content -->
    <template v-else-if="project">
      <header class="workspace-header">
        <div class="header-left">
          <IconButton label="返回项目列表" size="sm" variant="secondary" @click="goBackToList">
            <ArrowLeft :size="16" />
          </IconButton>

          <div class="title-edit-container">
            <div v-if="isEditingTitle" class="edit-row">
              <Input
                ref="titleInput"
                v-model="editTitleValue"
                class="title-input"
                aria-label="项目名称"
                :disabled="titleSaving"
                @blur="saveTitle"
                @keyup.enter="saveTitle"
                @keyup.esc="cancelEditTitle"
              />
              <span v-if="titleSaving" class="title-saving" role="status">正在保存…</span>
            </div>
            <h2 v-else class="project-title-heading-wrap">
              <button
                type="button"
                class="project-title-heading"
                :disabled="titleSaving"
                @dblclick="startEditTitle"
                @click="startEditTitle"
                title="双击就地编辑项目名称"
              >
                {{ project.title }}
                <Pencil :size="12" class="edit-icon" />
              </button>
            </h2>
          </div>
          <Badge v-if="projectStatus" :tone="projectStatus.tone" size="sm" aria-label="项目状态">{{
            projectStatus.label
          }}</Badge>
        </div>

        <div class="header-right">
          <Button
            v-if="project.schedule_id || lifecycleStatus === 'pending'"
            size="sm"
            variant="secondary"
            @click="
              router.push(
                project.schedule_id
                  ? `/project-schedules/${project.schedule_id}`
                  : pendingSchedule
                    ? `/project-schedules/${pendingSchedule.id}`
                    : '/projects?view=schedules',
              )
            "
            >管理启动计划</Button
          >
          <Button
            v-if="lifecycleStatus === 'pending' && runtimeData?.actions?.reauthorize?.allowed"
            size="sm"
            variant="warning"
            :disabled="!canPerform('reauthorize')"
            @click="openReauthorizationDialog"
            >重新确认授权</Button
          >
          <Button size="sm" variant="secondary" @click="reportDialogOpen = true"> 查看成果 </Button>
          <Button
            v-if="lifecycleStatus === 'active'"
            size="sm"
            variant="warning"
            :loading="statusSaving"
            :disabled="!canPerform('pause')"
            :title="disabledReason('pause')"
            @click="changeStatus('pause')"
          >
            <template #icon><Pause :size="12" /></template>
            暂停项目
          </Button>
          <template v-else-if="lifecycleStatus === 'paused'">
            <Button
              v-if="runtimeData?.actions?.reauthorize?.allowed"
              size="sm"
              variant="warning"
              :loading="statusSaving"
              :disabled="!canPerform('reauthorize')"
              :title="disabledReason('reauthorize')"
              @click="openReauthorizationDialog"
            >
              <template #icon><ShieldCheck :size="12" /></template>
              重新确认授权
            </Button>
            <Button
              size="sm"
              variant="success"
              :loading="statusSaving"
              :disabled="!canPerform('resume')"
              :title="disabledReason('resume')"
              @click="changeStatus('resume')"
            >
              <template #icon><Play :size="12" /></template>
              继续项目
            </Button>
          </template>
          <Button
            v-if="['active', 'paused'].includes(lifecycleStatus)"
            size="sm"
            variant="danger"
            :disabled="statusSaving || !canPerform('finish')"
            :title="disabledReason('finish')"
            @click="showFinishDialog = true"
            >结束并生成报告</Button
          >

          <Button v-if="canReopen" size="sm" @click="openReopenPrompt">
            <template #icon><RotateCcw :size="12" /></template>
            重新开启 (Reopen)
          </Button>

          <DropdownMenu
            v-model:open="isMoreOpen"
            :items="moreMenuItems"
            @select="handleMoreMenuSelect"
          >
            <template #trigger>
              <Button size="sm" variant="secondary" aria-label="项目更多操作">
                <template #icon><MoreHorizontal :size="14" /></template>
                更多
              </Button>
            </template>
            <template #icon="{ item }">
              <Trash2 v-if="item.value === 'delete-project'" :size="12" />
              <Download v-else-if="item.value.startsWith('export-')" :size="12" />
              <UsersRound v-else-if="item.value === 'workers'" :size="12" />
              <FolderOpen v-else :size="12" />
            </template>
          </DropdownMenu>
        </div>

        <div v-if="lifecycleActionWarnings.length" class="header-action-reasons" role="status">
          <span v-for="warning in lifecycleActionWarnings" :key="warning.action">
            {{ warning.action }}：{{ warning.reason }}
          </span>
        </div>
      </header>
      <Alert v-if="pendingSchedule?.block_reason || pendingScheduleError" tone="warning">
        {{ pendingSchedule?.block_reason || '启动计划查询失败，可进入计划任务视图重试。' }}
      </Alert>
      <Dialog
        v-model:open="projectInfoOpen"
        title="项目信息"
        description="项目归属、场景与执行期限。"
        size="sm"
      >
        <dl class="workspace-project-information">
          <div>
            <dt>场景</dt>
            <dd>{{ getScenarioLabel(project) }}</dd>
          </div>
          <div>
            <dt>执行停止时间</dt>
            <dd>{{ project.explore_deadline || '不限' }}</dd>
          </div>
          <div v-if="pendingSchedule">
            <dt>启动计划</dt>
            <dd>
              {{ scheduleRuleLabel(pendingSchedule) }} ·
              {{ scheduleLocalTime(pendingSchedule.next_run_at, pendingSchedule.timezone) }} ·
              {{ pendingSchedule.timezone }} · {{ scheduleStatusLabels[pendingSchedule.status] }}
            </dd>
          </div>
          <template v-if="authStore.isSuperAdmin">
            <div>
              <dt>Tenant</dt>
              <dd>{{ project.tenant_name }}</dd>
            </div>
            <div>
              <dt>Tenant ID</dt>
              <dd class="mono">{{ project.tenant_id }}</dd>
            </div>
            <div v-if="project.tenant_owner_login">
              <dt>所有者</dt>
              <dd>{{ project.tenant_owner_login }}</dd>
            </div>
          </template>
        </dl>
      </Dialog>
      <Alert
        v-if="workspaceStore.error"
        class="workspace-refresh-warning"
        tone="warning"
        title="实时状态刷新失败"
      >
        当前显示最近一次成功数据：{{ workspaceStore.error }}
      </Alert>

      <Alert v-if="actionError" tone="danger" title="项目操作失败">{{ actionError }}</Alert>
      <Alert v-if="finishAndReportPending" tone="info" title="正在确认评估已静默">
        服务端已接受结束请求。确认项目已停止或完成且没有活动运行后，将自动打开报告流程。
      </Alert>
      <ConfirmAction
        :open="showFinishDialog"
        title="结束并生成报告"
        description="结束后不能恢复本次评估；系统会等待服务端确认所有运行静默，再基于已有证据进入报告流程。确认结束吗？"
        confirm-label="确认结束并等待报告"
        tone="danger"
        :loading="statusSaving"
        @update:open="
          (open) => {
            if (!statusSaving) showFinishDialog = open
          }
        "
        :confirm-disabled="!canPerform('finish')"
        @confirm="finishAndOpenReport"
      >
        <Alert v-if="actionError" tone="danger" title="项目操作失败">{{ actionError }}</Alert>
      </ConfirmAction>
      <ConfirmAction
        :open="Boolean(reauthorizationTarget)"
        title="重新确认项目授权"
        :description="`为当前冻结范围明确设置新的绝对授权期限，或显式选择无限期授权。已有事实、证据与累计消耗保持不变；执行停止时间不变，授权更新后项目仍${lifecycleStatus === 'pending' ? '保持待启动，需通过启动计划处理' : '保持暂停，需要单独恢复执行'}。`"
        confirm-label="确认重新授权"
        tone="warning"
        :loading="statusSaving"
        :confirm-disabled="
          !reauthorizationTarget ||
          !reauthorizationConfirmed ||
          Boolean(reauthorizationExpiryError) ||
          !canPerform('reauthorize')
        "
        @update:open="handleReauthorizationDialogOpenChange"
        @confirm="saveReauthorization"
      >
        <template v-if="reauthorizationTarget">
          <dl class="authorization-summary">
            <div>
              <dt>授权 epoch</dt>
              <dd>{{ reauthorizationTarget.payload.expected_authorization_epoch }}</dd>
            </div>
            <div>
              <dt>当前范围摘要</dt>
              <dd class="mono">{{ reauthorizationTarget.payload.expected_scope_digest }}</dd>
            </div>
            <div>
              <dt>累计 Explore 次数</dt>
              <dd>{{ reauthorizationTarget.project.explore_attempts_started ?? 0 }}</dd>
            </div>
            <div>
              <dt>执行停止时间</dt>
              <dd>{{ reauthorizationTarget.project.explore_deadline || '不限' }}</dd>
            </div>
            <div>
              <dt>原授权过期时间</dt>
              <dd>{{ reauthorizationTarget.project.authorization_expires_at || '无限期' }}</dd>
            </div>
          </dl>
          <FormField
            label="新的授权过期时间"
            for="project-reauthorization-expires-at"
            :error="reauthorizationExpiryError"
            hint="必须明确选择未来时间或无限期；不会沿用或静默延长原期限。"
          >
            <Input
              id="project-reauthorization-expires-at"
              v-model="reauthorizationExpiresAt"
              type="datetime-local"
              :disabled="statusSaving || reauthorizationUnlimited"
            />
          </FormField>
          <label class="authorization-confirmation">
            <Checkbox
              v-model="reauthorizationUnlimited"
              :disabled="statusSaving"
              aria-label="授权无限期有效"
            />
            <span>明确授权为无限期；仅在真实授权没有到期时间时选择。</span>
          </label>
        </template>
        <label class="authorization-confirmation">
          <Checkbox
            v-model="reauthorizationConfirmed"
            :disabled="statusSaving"
            aria-label="确认当前项目授权范围"
          />
          <span
            >我已核对当前项目范围，并确认重新授权后{{
              lifecycleStatus === 'pending'
                ? '仍保持待启动，不会提前激活'
                : '仍需单独点击“继续项目”'
            }}。</span
          >
        </label>
        <Alert v-if="actionError" tone="danger" title="项目操作失败">{{ actionError }}</Alert>
      </ConfirmAction>

      <nav class="workspace-tabs" aria-label="项目页面">
        <router-link
          v-for="tab in tabs"
          :key="tab.routeName"
          :to="{ name: tab.routeName, params: { id: project.id } }"
          class="tab-item"
          exact-active-class="active"
        >
          {{ tab.label }}
          <span class="tab-badge" v-if="tab.badgeCount && tab.badgeCount.value > 0">
            {{ tab.badgeCount.value }}
          </span>
        </router-link>
      </nav>

      <!-- Viewport area -->
      <main class="workspace-viewport">
        <router-view></router-view>
      </main>

      <!-- 项目 Worker 重绑：只允许选择当前 Tenant 的可用 Worker。 -->
      <Dialog
        :open="showWorkersDialog"
        title="更新项目 Workers"
        description="保存后刷新 durable binding 和 Worker revision；项目不会回退到全局 Worker。"
        :dismissible="!workersSaving"
        @update:open="handleWorkersDialogOpenChange"
      >
        <div v-if="workersLoading" class="project-worker-options" role="status" aria-live="polite">
          <p class="worker-loading-message">正在加载可用 Worker...</p>
          <Skeleton v-for="i in 2" :key="i" class="worker-dialog-skeleton" />
        </div>
        <EmptyState v-else-if="availableWorkers.length === 0" title="暂无可用 Worker">
          <template #description>
            请先前往 <router-link to="/console/workers">Workers</router-link> 完成配置。
          </template>
        </EmptyState>
        <div v-else class="project-worker-options">
          <div
            v-for="worker in availableWorkers"
            :key="worker.id"
            class="project-worker-option"
            :class="{ disabled: workersSaving }"
          >
            <Checkbox
              :id="`workspace-worker-${worker.id}`"
              :model-value="selectedWorkerIds.includes(worker.id)"
              :disabled="workersSaving"
              :aria-label="`选择 Worker ${worker.display_name}`"
              @update:model-value="setWorkerSelection(worker.id, $event)"
            />
            <label class="project-worker-option-content" :for="`workspace-worker-${worker.id}`">
              <strong>{{ worker.display_name }}</strong>
              <small
                >{{ worker.model_display_name || worker.model_id }} ·
                {{ worker.task_types.join('/') }}</small
              >
            </label>
          </div>
        </div>

        <template #footer>
          <Button variant="secondary" :disabled="workersSaving" @click="showWorkersDialog = false">
            取消
          </Button>
          <Button
            :loading="workersSaving"
            :disabled="selectedWorkerIds.length === 0"
            @click="saveProjectWorkers"
          >
            保存绑定
          </Button>
        </template>
      </Dialog>

      <Dialog
        :open="showResourcesDialog"
        title="项目辅助资料"
        description="任意文件只读提供给后续 TaskRun，允许携带授权测试凭据；Worker 必须按需读取并以实时证据验证，不会自动执行脚本或二进制文件。"
        size="lg"
        :dismissible="!resourcesSaving"
        @update:open="handleResourcesDialogOpenChange"
      >
        <section class="workspace-library-bindings">
          <strong>冻结资料库</strong>
          <div
            v-for="binding in projectLibraryBindings"
            :key="binding.library_key"
            class="workspace-library-binding"
          >
            <span>{{ binding.library_name }}</span>
            <span class="mono">{{ binding.library_key }} · v{{ binding.version }}</span>
            <span>{{ binding.file_count }} 文件</span>
            <small class="mono">{{ binding.digest_sha256.slice(0, 12) }}…</small>
          </div>
          <div v-if="projectLibraryBindings.length === 0" class="empty-inline">
            本项目未冻结资料库。
          </div>
        </section>

        <div class="resource-mutation-note" :class="{ blocked: !canMutateResources }">
          {{ resourceMutationMessage }}
        </div>

        <strong>项目本地资料</strong>
        <div class="workspace-resource-list">
          <div
            v-for="resource in projectResources"
            :key="resource.id"
            class="workspace-resource-item"
          >
            <div>
              <strong class="mono">{{ resource.path }}</strong>
              <span
                >{{ projectResourceKindLabel(resource.kind) }} ·
                {{ formatProjectResourceSize(resource.size_bytes) }}</span
              >
              <small class="mono">SHA-256 {{ resource.sha256.slice(0, 12) }}…</small>
            </div>
            <Button
              size="sm"
              variant="secondary"
              :disabled="!canMutateResources || resourcesSaving"
              @click="deactivateResource(resource.id)"
            >
              停用
            </Button>
          </div>
          <div v-if="projectResources.length === 0" class="empty-inline">
            当前没有活动辅助资料。
          </div>
        </div>

        <div class="resource-upload-panel">
          <ProjectResourceBatchUploader
            ref="workspaceResourceUploader"
            v-model="resourceBatch"
            :disabled="!canMutateResources || resourceCommitSaving"
            :existing-count="projectResources.length"
            :existing-bytes="projectResourceBytes"
            :existing-paths="projectResourcePaths"
            @busy="resourceUploadBusy = $event"
            @pending="resourceUploadPending = $event"
          />
        </div>

        <template #footer>
          <Button variant="secondary" :disabled="resourcesSaving" @click="closeResourcesDialog">
            关闭
          </Button>
          <Button
            :loading="resourceCommitSaving"
            :disabled="
              !canMutateResources ||
              resourcesSaving ||
              resourceUploadPending ||
              !resourceBatch?.members.length
            "
            @click="saveProjectResources"
          >
            原子导入资料
          </Button>
        </template>
      </Dialog>

      <Dialog
        :open="showReopenDialog"
        title="重新开启项目"
        description="项目目前处于已完成状态。重开将撤销原 Goal 的完成状态，并允许加入新的已知事实让 Agent 继续推进探索。"
        :dismissible="!reopenSaving"
        @update:open="handleReopenDialogOpenChange"
      >
        <FormField label="追加的已知反馈事实描述" for="workspace-reopen-feedback" required>
          <Textarea
            id="workspace-reopen-feedback"
            v-model="reopenFeedback"
            placeholder="例如: 经人工审核，确认目标服务仍存在其它绕过路径，请继续分析..."
            :rows="3"
          />
        </FormField>

        <template #footer>
          <Button variant="secondary" :disabled="reopenSaving" @click="showReopenDialog = false">
            取消
          </Button>
          <Button
            :loading="reopenSaving"
            :disabled="!canReopen || !reopenFeedback.trim()"
            @click="saveReopen"
          >
            重开项目
          </Button>
        </template>
      </Dialog>

      <ProjectReportDialog
        v-model:open="reportDialogOpen"
        :project-id="project.id"
        :project-status="project.status"
        :capability="project.report_capability"
      />

      <ConfirmAction
        :open="showDeleteDialog"
        title="删除工作空间"
        :description="deleteProjectDescription"
        confirm-label="确认删除"
        tone="danger"
        :loading="deleteSaving"
        @update:open="handleDeleteDialogOpenChange"
        @confirm="saveDelete"
      />
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, nextTick, onMounted, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import {
  ArrowLeft,
  Download,
  FolderOpen,
  MoreHorizontal,
  Pause,
  Pencil,
  Play,
  RotateCcw,
  ShieldCheck,
  Trash2,
  UsersRound,
} from '@lucide/vue'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'
import { useUIStore } from '@/stores/ui'
import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  Dialog,
  DropdownMenu,
  EmptyState,
  FormField,
  IconButton,
  Input,
  Skeleton,
  Textarea,
  type DropdownMenuOption,
} from '@/components/ui'
import { ConfirmAction, ErrorState } from '@/components/patterns'
import {
  api,
  commitProjectResourceBatch,
  deactivateProjectResource,
  fetchText,
  getProjectManagementOptions,
  listVulnerabilities,
  updateProjectWorkers,
  type Vulnerability,
} from '@/api/client'
import type {
  ProjectDetail,
  ProjectResourceBatch,
  ReauthorizeProjectRequest,
  Scenario,
  TenantWorker,
} from '@/api/types'
import ProjectReportDialog from '@/features/reports/ProjectReportDialog.vue'
import { vulnerabilityKeys } from '@/features/vulnerabilities/vulnerabilityQueries'
import {
  canMutateProjectResources,
  formatProjectResourceSize,
  projectResourceKindLabel,
} from './projectResources'
import ProjectResourceBatchUploader from './ProjectResourceBatchUploader.vue'
import { resolveProjectStatus } from './workspaceStatus'
import {
  useProjectRuntime,
  useProjectLifecycle,
  projectActionAllowed,
  actionReasonText,
  isSupportedProjectRuntime,
} from './projectRuntime'
import type { ProjectLifecycleAction } from '@/api/types'
import {
  getProjectSchedule,
  listProjectSchedules,
  scheduleLocalTime,
  scheduleRuleLabel,
  scheduleStatusLabels,
} from '@/api/projectSchedules'

const authStore = useAuthStore()
const workspaceStore = useWorkspaceStore()
const uiStore = useUIStore()
const router = useRouter()

const project = computed(() => workspaceStore.projectDetail?.project || null)
const hints = computed(() => workspaceStore.projectDetail?.hints || [])
const projectResources = computed(() => workspaceStore.projectDetail?.resources || [])
const projectResourceBytes = computed(() =>
  projectResources.value.reduce((total, resource) => total + resource.size_bytes, 0),
)
const projectResourcePaths = computed(() => projectResources.value.map((resource) => resource.path))
const projectLibraryBindings = computed(() => workspaceStore.projectDetail?.library_bindings || [])
const projectId = computed(() => project.value?.id || '')

const { data: runtimeData, unavailable: runtimeUnavailable } = useProjectRuntime(projectId)
const lifecycleStatus = computed(() => project.value?.status || '')
const { data: pendingSchedule, isError: pendingScheduleError } = useQuery({
  queryKey: computed(() => ['project-start-schedule', projectId.value, project.value?.schedule_id]),
  queryFn: async () =>
    project.value?.schedule_id
      ? getProjectSchedule(project.value.schedule_id)
      : (await listProjectSchedules()).find((schedule) => schedule.project_id === projectId.value),
  enabled: computed(() => lifecycleStatus.value === 'pending'),
  refetchInterval: 8000,
  retry: false,
})
const projectActiveTasks = computed(() =>
  Array.isArray(runtimeData.value?.active_runs) ? runtimeData.value.active_runs : [],
)
const canMutateResources = computed(() =>
  canMutateProjectResources(
    lifecycleStatus.value,
    isSupportedProjectRuntime(runtimeData.value) &&
      Array.isArray(runtimeData.value.active_runs) &&
      !runtimeUnavailable.value &&
      projectActiveTasks.value.length === 0 &&
      ['paused', 'stopped'].includes(runtimeData.value.state),
    projectActiveTasks.value.map((task) => task.status),
  ),
)
const resourceMutationMessage = computed(() => {
  if (!['paused', 'stopped'].includes(lifecycleStatus.value))
    return '请先暂停项目；新增或停用资料仅影响后续 TaskRun。'
  if (!canMutateResources.value) return '尚未确认执行已停止，暂时禁止修改资料。'
  return lifecycleStatus.value === 'paused'
    ? '项目已安全暂停，资料变更对恢复后的 TaskRun 生效。'
    : '项目已结束，资料变更仅用于后续允许的任务，不会恢复评估。'
})
const projectStatus = computed(() =>
  project.value ? resolveProjectStatus(project.value.status) : null,
)
const canReopen = computed(
  () =>
    project.value?.status === 'completed' &&
    isSupportedProjectRuntime(runtimeData.value) &&
    !runtimeUnavailable.value &&
    Array.isArray(runtimeData.value.active_runs) &&
    runtimeData.value.active_runs.length === 0 &&
    runtimeData.value.actions.reopen.allowed,
)

const { data: scenariosData } = useQuery<Scenario[]>({
  queryKey: ['scenarios'],
  queryFn: () => api<Scenario[]>('GET', '/scenarios'),
  refetchInterval: 10000,
})

const scenarioLabels = computed(() => {
  return new Map((scenariosData.value || []).map((scenario) => [scenario.key, scenario.label]))
})

const workingIntentsCount = computed(() => {
  const intents = workspaceStore.projectDetail?.intents || []
  return intents.filter((i: any) => !i.concluded_at && i.worker).length
})

const hintsCount = computed(() => hints.value.length)

// 漏洞 tab 计数徽标：与其它 tab 的 badgeCount 机制一致，10s 轮询
const projectIdForVulns = computed(() => project.value?.id || '')
const { data: vulnerabilitiesData } = useQuery<Vulnerability[]>({
  queryKey: computed(() => vulnerabilityKeys.badge(projectIdForVulns.value)),
  queryFn: ({ queryKey, signal }) => {
    const [, , requestProjectId] = queryKey as ReturnType<typeof vulnerabilityKeys.badge>
    return listVulnerabilities(requestProjectId, undefined, undefined, signal)
  },
  enabled: computed(() => Boolean(projectIdForVulns.value)),
  refetchInterval: 10000,
})
const vulnsCount = computed(() => (vulnerabilitiesData.value || []).length)
const tabs = [
  { label: '图谱', routeName: 'WorkspaceGraph' },
  { label: '运行', routeName: 'WorkspaceRuns', badgeCount: workingIntentsCount },
  { label: '漏洞', routeName: 'WorkspaceVulnerabilities', badgeCount: vulnsCount },
  { label: '产物', routeName: 'WorkspaceArtifacts' },
  { label: '提示', routeName: 'WorkspaceHints', badgeCount: hintsCount },
]

// Inline title edit state
const isEditingTitle = ref(false)
const editTitleValue = ref('')
const titleSaving = ref(false)
const titleInput = ref<{ focus: (options?: FocusOptions) => void } | null>(null)

const startEditTitle = () => {
  if (!project.value || titleSaving.value) return
  isEditingTitle.value = true
  editTitleValue.value = project.value.title
  nextTick(() => {
    titleInput.value?.focus()
  })
}

const cancelEditTitle = () => {
  if (!titleSaving.value) isEditingTitle.value = false
}

const saveTitle = async () => {
  if (!project.value || !isEditingTitle.value || titleSaving.value) return
  const newTitle = editTitleValue.value.trim()
  if (!newTitle || newTitle === project.value.title) {
    cancelEditTitle()
    return
  }

  const id = project.value.id
  titleSaving.value = true
  try {
    await api('PUT', `/projects/${id}/title`, { title: newTitle })
    isEditingTitle.value = false
    uiStore.showToast('项目名称更新成功', 'success')
    await workspaceStore.loadProject(id)
  } catch (err: any) {
    uiStore.showToast(err.message || '重命名项目失败', 'danger')
  } finally {
    titleSaving.value = false
  }
}

const getScenarioLabel = (currentProject: { scenario_key?: string }): string => {
  const key = currentProject.scenario_key || 'default'
  return scenarioLabels.value.get(key) || key
}

// 工作空间元数据保留原轮询；运行状态查询由共享 query 负责去重与卸载取消。
let projectPollTimer: number | null = null
onMounted(() => {
  projectPollTimer = window.setInterval(() => {
    if (projectId.value) workspaceStore.loadProject(projectId.value)
  }, 10000)
})
onUnmounted(() => {
  if (projectPollTimer) clearInterval(projectPollTimer)
})

const {
  pendingProjectId,
  actionError,
  perform,
  clear: clearLifecycle,
} = useProjectLifecycle(
  (id) => (runtimeData.value?.project_id === id ? runtimeData.value : undefined),
  () => runtimeUnavailable.value,
  (id) => workspaceStore.loadProject(id),
  (id) => (project.value?.id === id ? project.value : undefined),
)
type DirectProjectLifecycleAction = Exclude<ProjectLifecycleAction, 'reauthorize'>
type ReauthorizationTarget = {
  project: ProjectDetail['project']
  payload: ReauthorizeProjectRequest
}

const statusSaving = computed(() => pendingProjectId.value !== null)
const showFinishDialog = ref(false)
const reportDialogOpen = ref(false)
const finishAndReportPending = ref(false)
const reportReadyAfterFinish = computed(() => {
  const currentProject = project.value
  const runtime = runtimeData.value
  if (
    !currentProject ||
    !isSupportedProjectRuntime(runtime) ||
    runtimeUnavailable.value ||
    !['stopped', 'completed'].includes(currentProject.status)
  )
    return false

  return (
    runtime.project_id === currentProject.id &&
    runtime.project_status === currentProject.status &&
    runtime.state === currentProject.status &&
    runtime.dispatcher.freshness === 'fresh' &&
    Array.isArray(runtime.active_runs) &&
    runtime.active_runs.length === 0
  )
})
const reauthorizationTarget = ref<ReauthorizationTarget | null>(null)
const reauthorizationConfirmed = ref(false)
const reauthorizationExpiresAt = ref('')
const reauthorizationUnlimited = ref(false)
const reauthorizationExpiryError = computed(() => {
  if (!reauthorizationTarget.value || reauthorizationUnlimited.value) return ''
  if (!reauthorizationExpiresAt.value) return '请选择未来时间，或明确选择无限期授权。'
  const expiry = new Date(reauthorizationExpiresAt.value)
  if (Number.isNaN(expiry.getTime()) || expiry.getTime() <= Date.now())
    return '授权过期时间必须晚于当前时间。'
  return ''
})
const canPerform = (action: ProjectLifecycleAction) =>
  !statusSaving.value &&
  projectActionAllowed(runtimeData.value, action, runtimeUnavailable.value, project.value)
const disabledReason = (action: ProjectLifecycleAction) => {
  if (canPerform(action)) return undefined
  if (statusSaving.value) return '正在提交项目操作。'
  return actionReasonText(
    action === 'reauthorize' && (!runtimeData.value || runtimeUnavailable.value)
      ? 'runtime_unavailable'
      : runtimeData.value?.actions?.[action]?.reason,
  )
}
const lifecycleActionWarnings = computed(() => {
  const actions: Array<{ action: ProjectLifecycleAction; label: string }> = []
  if (lifecycleStatus.value === 'active') actions.push({ action: 'pause', label: '暂停项目' })
  if (lifecycleStatus.value === 'paused') {
    if (runtimeData.value?.actions?.reauthorize?.allowed)
      actions.push({ action: 'reauthorize', label: '重新确认授权' })
    actions.push({ action: 'resume', label: '继续项目' })
  }
  if (['active', 'paused'].includes(lifecycleStatus.value))
    actions.push({ action: 'finish', label: '结束并生成报告' })
  return actions
    .filter(({ action }) => !canPerform(action))
    .map(({ action, label }) => ({ action: label, reason: disabledReason(action) }))
})
const changeStatus = async (action: DirectProjectLifecycleAction) => {
  if (!projectId.value) return
  if (await perform(projectId.value, action)) showFinishDialog.value = false
}
const finishAndOpenReport = async () => {
  const id = projectId.value
  if (!id) return
  finishAndReportPending.value = false
  if (!(await perform(id, 'finish')) || projectId.value !== id) return

  showFinishDialog.value = false
  finishAndReportPending.value = true
}
watch([finishAndReportPending, reportReadyAfterFinish], ([pending, ready]) => {
  if (!pending || !ready) return
  finishAndReportPending.value = false
  reportDialogOpen.value = true
})
const openReauthorizationDialog = () => {
  if (
    !project.value ||
    !canPerform('reauthorize') ||
    !Number.isInteger(project.value.authorization_epoch) ||
    !project.value.authorization_scope_digest
  )
    return
  reauthorizationConfirmed.value = false
  reauthorizationExpiresAt.value = ''
  reauthorizationUnlimited.value = false
  reauthorizationTarget.value = {
    project: { ...project.value },
    payload: {
      confirmed: true,
      expected_authorization_epoch: project.value.authorization_epoch!,
      expected_scope_digest: project.value.authorization_scope_digest,
    },
  }
}
const handleReauthorizationDialogOpenChange = (open: boolean) => {
  if (!open && !statusSaving.value) {
    reauthorizationTarget.value = null
    reauthorizationConfirmed.value = false
    reauthorizationExpiresAt.value = ''
    reauthorizationUnlimited.value = false
  }
}
const saveReauthorization = async () => {
  const confirmation = reauthorizationTarget.value
  if (
    !confirmation ||
    !reauthorizationConfirmed.value ||
    reauthorizationExpiryError.value ||
    !project.value
  )
    return
  if (
    project.value.id !== confirmation.project.id ||
    project.value.authorization_epoch !== confirmation.payload.expected_authorization_epoch ||
    project.value.authorization_scope_digest !== confirmation.payload.expected_scope_digest
  ) {
    actionError.value = '项目授权版本或范围已变化，请关闭对话框并重新确认。'
    return
  }
  const payload: ReauthorizeProjectRequest = {
    ...confirmation.payload,
    authorization_expires_at: reauthorizationUnlimited.value
      ? null
      : new Date(reauthorizationExpiresAt.value).toISOString(),
  }
  if (await perform(project.value.id, 'reauthorize', payload)) {
    reauthorizationTarget.value = null
    reauthorizationConfirmed.value = false
    reauthorizationExpiresAt.value = ''
    reauthorizationUnlimited.value = false
    uiStore.showToast('授权已更新，项目仍处于暂停状态', 'success')
  }
}
watch(projectId, () => {
  clearLifecycle()
  showFinishDialog.value = false
  reportDialogOpen.value = false
  finishAndReportPending.value = false
  reauthorizationTarget.value = null
  reauthorizationConfirmed.value = false
  reauthorizationExpiresAt.value = ''
  reauthorizationUnlimited.value = false
})

// 历史项目显式重绑 Worker；选项由目标项目 Tenant 决定。
const showWorkersDialog = ref(false)
const workersLoading = ref(false)
const workersSaving = ref(false)
const availableWorkers = ref<TenantWorker[]>([])
const selectedWorkerIds = ref<string[]>([])

const handleWorkersDialogOpenChange = (open: boolean) => {
  if (!open && !workersSaving.value) showWorkersDialog.value = false
}

const setWorkerSelection = (workerId: string, selected: boolean) => {
  const index = selectedWorkerIds.value.indexOf(workerId)
  if (selected && index < 0) selectedWorkerIds.value.push(workerId)
  if (!selected && index >= 0) selectedWorkerIds.value.splice(index, 1)
}

const openWorkersDialog = async () => {
  if (!project.value) return
  showWorkersDialog.value = true
  workersLoading.value = true
  availableWorkers.value = []
  selectedWorkerIds.value = []
  try {
    const options = await getProjectManagementOptions(project.value.id)
    availableWorkers.value = options.tenant_workers.filter((worker) => worker.available)
    const availableIds = new Set(availableWorkers.value.map((worker) => worker.id))
    selectedWorkerIds.value = project.value.tenant_workers
      .map((worker) => worker.id)
      .filter((workerId) => availableIds.has(workerId))
  } catch (err: any) {
    showWorkersDialog.value = false
    uiStore.showToast(err.message || '加载 Worker 失败', 'danger')
  } finally {
    workersLoading.value = false
  }
}

const saveProjectWorkers = async () => {
  if (!project.value || selectedWorkerIds.value.length === 0 || workersSaving.value) return
  const id = project.value.id
  workersSaving.value = true
  try {
    await updateProjectWorkers(id, { tenant_worker_ids: selectedWorkerIds.value })
    showWorkersDialog.value = false
    uiStore.showToast('项目 Worker 绑定已更新', 'success')
    await workspaceStore.loadProject(id)
  } catch (err: any) {
    uiStore.showToast(err.message || '更新 Worker 绑定失败', 'danger')
  } finally {
    workersSaving.value = false
  }
}

const showResourcesDialog = ref(false)
const resourceBatch = ref<ProjectResourceBatch | null>(null)
const resourceUploadBusy = ref(false)
const resourceUploadPending = ref(false)
const resourceCommitSaving = ref(false)
const resourceMutationSaving = ref(false)
const resourcesSaving = computed(
  () => resourceUploadBusy.value || resourceCommitSaving.value || resourceMutationSaving.value,
)
const workspaceResourceUploader = ref<{ cancel: () => Promise<void> } | null>(null)

const openResourcesDialog = async () => {
  if (!project.value) return
  showResourcesDialog.value = true
  resourceBatch.value = null
  await workspaceStore.loadProject(project.value.id)
}

const closeResourcesDialog = async () => {
  if (resourcesSaving.value) return
  try {
    await workspaceResourceUploader.value?.cancel()
    resourceBatch.value = null
    showResourcesDialog.value = false
  } catch (err: any) {
    uiStore.showToast(err.message || '取消临时上传批次失败', 'danger')
  }
}

const handleResourcesDialogOpenChange = (open: boolean) => {
  if (!open) void closeResourcesDialog()
}

const saveProjectResources = async () => {
  if (
    !project.value ||
    !resourceBatch.value?.members.length ||
    !canMutateResources.value ||
    resourceUploadPending.value ||
    resourcesSaving.value
  )
    return
  resourceCommitSaving.value = true
  try {
    await commitProjectResourceBatch(project.value.id, resourceBatch.value.id)
    resourceBatch.value = null
    showResourcesDialog.value = false
    await workspaceStore.loadProject(project.value.id)
    uiStore.showToast('辅助资料已原子导入，将用于后续 TaskRun', 'success')
  } catch (err: any) {
    await workspaceStore.loadProject(project.value.id)
    uiStore.showToast(err.message || '导入辅助资料失败', 'danger')
  } finally {
    resourceCommitSaving.value = false
  }
}

const deactivateResource = async (resourceId: string) => {
  if (!project.value || !canMutateResources.value || resourcesSaving.value) return
  resourceMutationSaving.value = true
  try {
    await deactivateProjectResource(project.value.id, resourceId)
    await workspaceStore.loadProject(project.value.id)
    uiStore.showToast('辅助资料已停用，历史 TaskRun 快照保持不变', 'info')
  } catch (err: any) {
    uiStore.showToast(err.message || '停用辅助资料失败', 'danger')
  } finally {
    resourceMutationSaving.value = false
  }
}

// Reopen finished project
const showReopenDialog = ref(false)
const reopenFeedback = ref('')
const reopenSaving = ref(false)

const handleReopenDialogOpenChange = (open: boolean) => {
  if (!open && !reopenSaving.value) showReopenDialog.value = false
}

const openReopenPrompt = () => {
  if (!canReopen.value) return
  reopenFeedback.value = ''
  showReopenDialog.value = true
}

const saveReopen = async () => {
  if (!project.value || !canReopen.value || !reopenFeedback.value.trim() || reopenSaving.value)
    return
  const id = project.value.id
  reopenSaving.value = true

  try {
    await api('POST', `/projects/${id}/reopen`, {
      description: reopenFeedback.value.trim(),
    })
    showReopenDialog.value = false
    uiStore.showToast('项目已成功重新开启，开启新的分析意图', 'success')
    workspaceStore.loadProject(id)
  } catch (err: any) {
    uiStore.showToast(err.message || '重开失败', 'danger')
  } finally {
    reopenSaving.value = false
  }
}

const isMoreOpen = ref(false)
const projectInfoOpen = ref(false)
const moreMenuItems = computed<readonly DropdownMenuOption[]>(() => [
  { value: 'project-info', label: '项目信息' },
  { value: 'workers', label: 'Workers' },
  {
    value: 'resources',
    label: `资料${projectResources.value.length ? ` (${projectResources.value.length})` : ''}`,
  },
  { value: 'export-project-yaml', label: '导出项目配置 YAML', separatorBefore: true },
  { value: 'export-vulnerability-evidence-archive', label: '下载漏洞证据 ZIP（按漏洞分组）' },
  { value: 'delete-project', label: '彻底删除项目', separatorBefore: true, tone: 'danger' },
])

function handleMoreMenuSelect(value: string) {
  isMoreOpen.value = false
  if (value === 'project-info') projectInfoOpen.value = true
  else if (value === 'workers') void openWorkersDialog()
  else if (value === 'resources') void openResourcesDialog()
  else if (value === 'export-project-yaml') void exportProjectYaml()
  else if (value === 'export-vulnerability-evidence-archive') downloadVulnerabilityEvidenceArchive()
  else if (value === 'delete-project') confirmDeleteProject()
}

function downloadVulnerabilityEvidenceArchive() {
  if (!project.value) return
  const link = document.createElement('a')
  link.href = `/projects/${encodeURIComponent(project.value.id)}/artifacts/archive`
  link.download = ''
  link.target = '_blank'
  link.rel = 'noopener'
  document.body.appendChild(link)
  link.click()
  link.remove()
  uiStore.showToast('已开始生成漏洞证据 ZIP', 'success')
}

// Export YAML config
const exportProjectYaml = async () => {
  if (!project.value) return
  const id = project.value.id
  const title = project.value.title
  try {
    const text = await fetchText(`/projects/${encodeURIComponent(id)}/export?format=yaml`)

    const blob = new Blob([text], { type: 'text/yaml' })
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = `${title || id}_export.yaml`
    link.click()
    URL.revokeObjectURL(link.href)

    uiStore.showToast('项目导出配置下载成功', 'success')
  } catch (e: any) {
    uiStore.showToast(`导出失败: ${e.message}`, 'danger')
  }
}

// Delete project dialog
const showDeleteDialog = ref(false)
const deleteSaving = ref(false)
const deleteProjectDescription = computed(() =>
  project.value
    ? `您确定要永久删除项目「${project.value.title}」及关联的所有数据吗？此操作不可逆。`
    : '此操作将永久删除项目及关联的所有数据，且不可逆。',
)

const handleDeleteDialogOpenChange = (open: boolean) => {
  if (!open && !deleteSaving.value) showDeleteDialog.value = false
}

const confirmDeleteProject = () => {
  showDeleteDialog.value = true
}
const saveDelete = async () => {
  if (!project.value || deleteSaving.value) return
  const id = project.value.id
  deleteSaving.value = true
  try {
    await api('DELETE', `/projects/${id}`)
    showDeleteDialog.value = false
    uiStore.showToast('项目已被彻底删除', 'success')
    router.push('/projects')
  } catch (err: any) {
    uiStore.showToast(err.message || '删除项目失败', 'danger')
  } finally {
    deleteSaving.value = false
  }
}

const goBackToList = () => {
  router.push('/projects')
}

// Global refresh trigger listener
watch(
  () => uiStore.globalRefreshKey,
  () => {
    if (project.value) {
      workspaceStore.loadProject(project.value.id)
    }
  },
)
</script>

<style src="./ProjectWorkspace.css" scoped></style>
