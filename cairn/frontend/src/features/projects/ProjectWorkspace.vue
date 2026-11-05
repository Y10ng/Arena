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

    <!-- 身份与动作共用短带，局部导航单独成组，避免与项目操作挤在一排。 -->
    <template v-else-if="project">
      <Teleport :to="compactHeaderTarget || 'body'" :disabled="!compactHeaderTarget">
        <header class="workspace-header">
          <div class="header-left">
            <IconButton label="返回项目列表" size="sm" variant="secondary" @click="goBackToList">
              <ArrowLeft :size="16" />
            </IconButton>

            <h1 class="project-title-heading" :title="project.title">{{ project.title }}</h1>
            <ProjectStatusObservation
              :project="project"
              :control-waiting-expired="observationExpired(project.id)"
              :runtime="runtimeData"
              :data-updated-at="runtimeUpdatedAtFor(project.id)"
              :failed="runtimeError"
              :fetching="runtimeFetching"
              :now="runtimeNow"
              @refresh="refetchRuntime({ cancelRefetch: false })"
            />
          </div>

          <div class="header-right">
            <span v-if="['active', 'paused'].includes(project.status) && !project.verification_request_id && runtimeData?.actions?.finish?.reason !== 'verification_request_managed'" class="workspace-pause-policy">暂停满12小时将自动结束，工作文件保留</span>
            <Button
              v-if="primaryAction.value !== 'report'"
              variant="ghost"
              @click="reportDialogOpen = true"
            >
              查看成果
            </Button>
            <Button
              :loading="statusSaving && primaryAction.lifecycle"
              :disabled="primaryAction.disabled"
              @click="runPrimaryAction"
            >
              <template #icon><component :is="primaryAction.icon" :size="16" /></template>
              {{ primaryAction.label }}
            </Button>
            <DropdownMenu
              v-model:open="isMoreOpen"
              :items="moreMenuItems"
              label="项目管理"
              @select="handleMoreMenuSelect"
            >
              <template #trigger>
                <Button variant="secondary" aria-label="项目更多操作">
                  管理<ChevronDown :size="16" />
                </Button>
              </template>
            </DropdownMenu>
          </div>

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
            <router-link v-if="project.schedule_id" :to="`/project-schedules/${project.schedule_id}`" class="tab-item">计划历史</router-link>
          </nav>
        </header>
      </Teleport>
      <Alert v-if="lifecycleActionWarnings.length" class="header-action-reasons" tone="warning">
        <ul>
          <li v-for="warning in lifecycleActionWarnings" :key="warning.action">
            {{ warning.action }}：{{ warning.reason }}
          </li>
        </ul>
      </Alert>
      <Dialog
        :open="isEditingTitle"
        title="重命名项目"
        description=""
        size="sm"
        :dismissible="!titleSaving"
        :close-on-outside="false"
        @update:open="!$event && cancelEditTitle()"
      >
        <FormField label="项目名称" for="workspace-project-title" required>
          <Input
            id="workspace-project-title"
            ref="titleInput"
            v-model="editTitleValue"
            :disabled="titleSaving"
            @keyup.enter="saveTitle"
          />
        </FormField>
        <Alert v-if="titleError" tone="danger">{{ titleError }}</Alert>
        <template #footer>
          <Button variant="secondary" :disabled="titleSaving" @click="cancelEditTitle">取消</Button>
          <Button :loading="titleSaving" :disabled="!editTitleValue.trim()" @click="saveTitle"
            >保存名称</Button
          >
        </template>
      </Dialog>
      <Alert v-if="pendingSchedule?.block_reason || pendingScheduleError" tone="warning">
        {{ pendingSchedule?.block_reason || '启动计划查询失败，可进入计划任务视图重试。' }}
      </Alert>
      <Dialog v-model:open="projectInfoOpen" title="项目信息" description="" size="md">
        <dl class="workspace-project-information">
          <div><dt>项目名称</dt><dd>{{ project.title }}</dd></div>
          <div>
            <dt>场景</dt>
            <dd>{{ getScenarioLabel(project) }}</dd>
          </div>
          <div>
            <dt>执行停止时间</dt>
            <dd>{{ project.explore_deadline || '不限' }}</dd>
          </div>
          <div>
            <dt>授权过期时间</dt>
            <dd>{{ project.authorization_expires_at || '无限期' }}</dd>
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
      <ConfirmAction
        v-model:open="showResumeRetryDialog"
        title="重试继续项目"
        description="将重新核验并恢复原现场，不重跑已成功的 Bootstrap。物理恢复确认后可能继续后续研究并产生模型费用。"
        confirm-label="确认重试继续"
        tone="warning"
        :loading="statusSaving"
        :confirm-disabled="!canPerform('resume')"
        @confirm="confirmResumeRetry"
      >
        <Alert v-if="actionError" tone="danger" title="项目操作失败">{{ actionError }}</Alert>
      </ConfirmAction>
      <ConfirmAction
        :open="showFinishDialog"
        title="结束项目"
        description="结束后不能恢复本次评估。系统将终止所属研究执行，保留已保存材料；独立报告、聊天和复测不受影响，不会自动生成报告。确认结束吗？"
        confirm-label="确认结束项目"
        tone="danger"
        :loading="statusSaving"
        @update:open="
          (open) => {
            if (!statusSaving) showFinishDialog = open
          }
        "
        :confirm-disabled="!canPerform('finish')"
        @confirm="changeStatus('finish')"
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

      <!-- 保持子页原有路由及各自的滚动容器。 -->
      <main class="workspace-viewport">
        <router-view v-slot="{ Component }"><component :is="Component" v-bind="route.name === 'WorkspaceOverview' ? { canMutateResources, resourceMutationMessage } : {}" /></router-view>
      </main>

      <Dialog
        :open="showReopenDialog"
        title="重新开启项目"
        description="项目目前处于已完成状态。重开将撤销原 Goal 的完成状态，并允许加入新的已知事实让 Agent 继续推进探索。"
        :dismissible="!reopenSaving"
        :close-on-outside="false"
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

      <ProjectTestEgressDialog
        v-if="testEgressOpen"
        :key="project.id"
        :project-id="project.id"
        :value="project.test_egress"
        @close="testEgressOpen = false"
        @saved="project.test_egress = $event"
      />

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
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import {
  ArrowLeft,
  CalendarDays,
  ChevronDown,
  FileText,
  Pause,
  Play,
  RotateCcw,
  ShieldCheck,
} from '@lucide/vue'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'
import { useUIStore } from '@/stores/ui'
import { navigationRegistry } from '@/router/navigationRegistry'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Dialog,
  DropdownMenu,
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
  fetchText,
  listVulnerabilities,
  type Vulnerability,
} from '@/api/client'
import type {
  ProjectDetail,
  ReauthorizeProjectRequest,
  Scenario,
} from '@/api/types'
import ProjectReportDialog from '@/features/reports/ProjectReportDialog.vue'
import { vulnerabilityKeys } from '@/features/vulnerabilities/vulnerabilityQueries'
import { canMutateProjectResources } from './projectResources'
import ProjectTestEgressDialog from './ProjectTestEgressDialog.vue'
import ProjectStatusObservation from './ProjectStatusObservation.vue'
import {
  useProjectRuntime,
  useProjectLifecycle,
  projectActionAllowed,
  actionReasonText,
  isSupportedProjectRuntime,
  isResumeRetry,
  projectResumeLabel,
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
const route = useRoute()
const compactHeaderTarget = computed(() =>
  route.name ? document.getElementById('workbench-project-header') : null,
)

const rawProject = computed(() => workspaceStore.projectDetail?.project || null)
const hints = computed(() => workspaceStore.projectDetail?.hints || [])
const projectId = computed(() => rawProject.value?.id || '')

const {
  data: rawRuntimeData,
  dataUpdatedAt: rawRuntimeUpdatedAt,
  isError: runtimeError,
  isFetching: runtimeFetching,
  refetch: refetchRuntime,
  now: runtimeNow,
} = useProjectRuntime(projectId)
const {
  pendingProjectId,
  actionError,
  perform,
  clear: clearLifecycle,
  projectFor,
  runtimeFor,
  runtimeUpdatedAtFor,
  unavailableFor,
  observationExpired,
} = useProjectLifecycle(
  (id) => (rawRuntimeData.value?.project_id === id ? rawRuntimeData.value : undefined),
  () => ({ dataUpdatedAt: rawRuntimeUpdatedAt.value, failed: runtimeError.value, now: runtimeNow.value }),
  (id) => workspaceStore.loadProject(id),
  () => rawProject.value ? [rawProject.value] : [],
)
const project = computed(() => projectFor(projectId.value) || null)
const runtimeData = computed(() => runtimeFor(projectId.value))
const runtimeUnavailable = computed(() => unavailableFor(projectId.value))
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
const tabBadges = {
  WorkspaceRuns: workingIntentsCount,
  WorkspaceVulnerabilities: vulnsCount,
  WorkspaceHints: hintsCount,
}
const tabs = navigationRegistry
  .filter((item) => item.workspace)
  .sort((left, right) => left.workspace!.order - right.workspace!.order)
  .map((item) => ({
    label: item.label,
    routeName: item.routeName,
    badgeCount: tabBadges[item.routeName as keyof typeof tabBadges],
  }))

const isEditingTitle = ref(false)
const titleError = ref('')
const editTitleValue = ref('')
const titleSaving = ref(false)
const titleInput = ref<{ focus: (options?: FocusOptions) => void } | null>(null)

const startEditTitle = () => {
  if (!project.value || titleSaving.value) return
  titleError.value = ''
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
  titleError.value = ''
  try {
    await api('PUT', `/projects/${id}/title`, { title: newTitle })
    isEditingTitle.value = false
    uiStore.showToast('项目名称更新成功', 'success')
    await workspaceStore.loadProject(id)
  } catch (err: any) {
    titleError.value = err.message || '重命名项目失败，名称输入已保留。'
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

type DirectProjectLifecycleAction = Exclude<ProjectLifecycleAction, 'reauthorize'>
type ReauthorizationTarget = {
  project: ProjectDetail['project']
  payload: ReauthorizeProjectRequest
}

const statusSaving = computed(() => pendingProjectId.value !== null)
const showFinishDialog = ref(false)
const showResumeRetryDialog = ref(false)
const hasResumeControl = computed(() => lifecycleStatus.value === 'active' &&
  project.value?.execution_control?.action === 'resume' &&
  ['failed', 'lost', 'requested'].includes(project.value.execution_control.state || ''))
const reportDialogOpen = ref(false)
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
    ['reauthorize', 'resume'].includes(action) && (!runtimeData.value || runtimeUnavailable.value)
      ? 'runtime_unavailable'
      : runtimeData.value?.actions?.[action]?.reason,
  )
}
// 一个主动作随项目业务状态变化；低频与危险管理不挤占研究正文。
const primaryAction = computed(() => {
  let action: 'pause' | 'resume' | 'reauthorize' | null = null
  if (hasResumeControl.value) action = 'resume'
  else if (lifecycleStatus.value === 'active') action = 'pause'
  else if (
    ['paused', 'pending'].includes(lifecycleStatus.value) &&
    runtimeData.value?.actions?.reauthorize?.allowed
  )
    action = 'reauthorize'
  else if (lifecycleStatus.value === 'paused') action = 'resume'
  if (action) {
    const presentation = {
      pause: { label: '暂停项目', icon: Pause },
      resume: { label: projectResumeLabel(project.value), icon: Play },
      reauthorize: { label: '重新确认授权', icon: ShieldCheck },
    }[action]
    return { value: action, ...presentation, disabled: !canPerform(action), lifecycle: true }
  }
  if (lifecycleStatus.value === 'pending')
    return {
      value: 'schedule',
      label: '管理启动计划',
      icon: CalendarDays,
      disabled: false,
      lifecycle: false,
    }
  if (canReopen.value)
    return {
      value: 'reopen',
      label: '重新开启项目',
      icon: RotateCcw,
      disabled: reopenSaving.value,
      lifecycle: false,
    }
  return { value: 'report', label: '查看成果', icon: FileText, disabled: false, lifecycle: false }
})
function manageStartSchedule() {
  router.push(
    project.value?.schedule_id
      ? `/project-schedules/${project.value.schedule_id}`
      : pendingSchedule.value
        ? `/project-schedules/${pendingSchedule.value.id}`
        : '/projects?view=schedules',
  )
}
function runPrimaryAction() {
  if (primaryAction.value.disabled) return
  switch (primaryAction.value.value) {
    case 'pause':
      void changeStatus('pause')
      break
    case 'resume':
      void changeStatus('resume')
      break
    case 'reauthorize':
      openReauthorizationDialog()
      break
    case 'schedule':
      manageStartSchedule()
      break
    case 'reopen':
      openReopenPrompt()
      break
    default:
      reportDialogOpen.value = true
  }
}
const lifecycleActionWarnings = computed(() => {
  const actions: Array<{ action: ProjectLifecycleAction; label: string }> = []
  if (lifecycleStatus.value === 'active') actions.push({ action: 'pause', label: '暂停项目' })
  if (
    ['paused', 'pending'].includes(lifecycleStatus.value) &&
    runtimeData.value?.actions?.reauthorize?.allowed
  )
    actions.push({ action: 'reauthorize', label: '重新确认授权' })
  if (lifecycleStatus.value === 'paused' || hasResumeControl.value)
    actions.push({ action: 'resume', label: projectResumeLabel(project.value) })
  if (['active', 'paused'].includes(lifecycleStatus.value))
    actions.push({ action: 'finish', label: '结束项目' })
  return actions
    .filter(({ action }) => !canPerform(action))
    .map(({ action, label }) => ({ action: label, reason: disabledReason(action) }))
})
const changeStatus = async (action: DirectProjectLifecycleAction) => {
  if (!projectId.value) return
  if (action === 'resume' && isResumeRetry(project.value)) {
    showResumeRetryDialog.value = true
    return
  }
  if (await perform(projectId.value, action)) showFinishDialog.value = false
}
const confirmResumeRetry = async () => {
  if (projectId.value && await perform(projectId.value, 'resume')) showResumeRetryDialog.value = false
}
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
    uiStore.showToast(
      lifecycleStatus.value === 'pending'
        ? '授权已更新，项目仍保持待启动'
        : '授权已更新，项目仍处于暂停状态',
      'success',
    )
  }
}
watch(projectId, () => {
  clearLifecycle()
  showFinishDialog.value = false
  showResumeRetryDialog.value = false
  reportDialogOpen.value = false
  reauthorizationTarget.value = null
  reauthorizationConfirmed.value = false
  reauthorizationExpiresAt.value = ''
  reauthorizationUnlimited.value = false
})

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
const testEgressOpen = ref(false)
const moreMenuItems = computed<readonly DropdownMenuOption[]>(() => {
  const items: DropdownMenuOption[] = [
    { value: 'project-info', label: '项目信息' },
    { value: 'rename', label: '重命名项目', disabled: titleSaving.value },
    { value: 'test-egress', label: '代理设置', separatorBefore: true },
  ]
  if (
    (project.value?.schedule_id || lifecycleStatus.value === 'pending') &&
    primaryAction.value.value !== 'schedule'
  )
    items.push({ value: 'schedule', label: '管理启动计划' })
  items.push(
    { value: 'export-project-yaml', label: '导出项目配置 YAML', separatorBefore: true },
    { value: 'export-vulnerability-evidence-archive', label: '下载漏洞证据 ZIP（按漏洞分组）' },
  )
  if (lifecycleStatus.value === 'active' && primaryAction.value.value !== 'pause')
    items.push({
      value: 'pause', label: '暂停项目', disabled: !canPerform('pause'),
      description: disabledReason('pause'), separatorBefore: true,
    })
  if (lifecycleStatus.value === 'paused' && primaryAction.value.value !== 'resume')
    items.push({
      value: 'resume',
      label: '继续项目',
      disabled: !canPerform('resume'),
      description: disabledReason('resume'),
      separatorBefore: true,
    })
  if (['active', 'paused'].includes(lifecycleStatus.value))
    items.push({
      value: 'finish',
      label: '结束项目',
      disabled: !canPerform('finish'),
      description: disabledReason('finish'),
      separatorBefore: true,
      tone: 'danger',
    })
  items.push({
    value: 'delete-project',
    label: '彻底删除项目',
    separatorBefore: true,
    tone: 'danger',
  })
  return items
})

function handleMoreMenuSelect(value: string) {
  isMoreOpen.value = false
  if (value === 'project-info') projectInfoOpen.value = true
  else if (value === 'rename') startEditTitle()
  else if (value === 'schedule') manageStartSchedule()
  else if (value === 'resume') void changeStatus('resume')
  else if (value === 'pause') void changeStatus('pause')
  else if (value === 'finish' && canPerform('finish')) showFinishDialog.value = true
  else if (value === 'test-egress') testEgressOpen.value = true
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

// 菜单项关闭后会被卸载，管理浮层不能把焦点交还给已消失的菜单项。
watch(
  () =>
    [
      showDeleteDialog.value,
      isEditingTitle.value,
      projectInfoOpen.value,
      testEgressOpen.value,
      showFinishDialog.value,
    ].some(Boolean),
  async (open, previous) => {
    if (open || !previous) return
    const closedProjectId = projectId.value
    await nextTick()
    requestAnimationFrame(() => {
      if (projectId.value !== closedProjectId) return
      if (!document.activeElement || document.activeElement === document.body) {
        document
          .querySelector<HTMLButtonElement>('.workspace-header [aria-label="项目更多操作"]')
          ?.focus()
      }
    })
  },
)

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
