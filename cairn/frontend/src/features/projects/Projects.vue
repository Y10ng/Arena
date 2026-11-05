<template>
  <div class="projects-page">
    <div v-if="!showSchedules" class="view-header">
      <h1 class="sr-only">{{ showSchedules ? '启动计划' : '项目' }}</h1>
      <nav class="project-views" aria-label="项目与计划视图">
        <RouterLink
          to="/projects"
          :class="{ active: !showSchedules }"
          :aria-current="!showSchedules ? 'page' : undefined"
          >项目</RouterLink
        >
        <RouterLink
          to="/project-schedules"
          :class="{ active: showSchedules }"
          :aria-current="showSchedules ? 'page' : undefined"
          >启动计划</RouterLink
        >
      </nav>

      <div class="header-right">
        <Button @click="openNewProject($event)">
          <template #icon><Plus :size="14" /></template>
          新建项目
        </Button>
      </div>

      <FilterBar v-if="!showSchedules" label="项目筛选" class="project-filter-bar">
        <div class="project-search-field">
          <Search :size="16" aria-hidden="true" />
          <Input
            id="project-search"
            v-model="searchQuery"
            aria-label="搜索项目"
            :placeholder="showTenantAttribution ? '搜索名称、场景或账户…' : '搜索名称或场景…'"
            @keydown.esc="searchQuery = ''"
          />
        </div>
        <SearchFilter
          v-model="activeSegment"
          label="项目状态"
          :options="segments"
          clear-value="all"
        />
        <SearchFilter
          v-if="showTenantAttribution && tenantOptions.length > 1"
          v-model="selectedTenantId"
          label="所属账户"
          :options="tenantFilterOptions"
          clear-value="all"
        />
        <Button v-if="hasFilters" variant="ghost" size="sm" @click="clearFilters">清除筛选</Button>
        <template #actions>
          <div class="project-sort-field">
            <Select
              id="project-sort"
              v-model="sortBy"
              aria-label="项目排序"
              :options="sortOptions"
            />
          </div>
        </template>
      </FilterBar>
    </div>

    <CreationRequestRecovery v-if="!newProjectSession" />
    <Alert v-if="newProjectSession" v-show="!newProjectOpen" tone="warning" title="创建结果未确认">
      内容仅保留在本次页面会话中，请核实服务端结果；不要按标题猜测项目归属或重复创建。
      <template #actions
        ><Button size="sm" variant="secondary" @click="openNewProject($event)"
          >查看保留内容</Button
        ></template
      >
    </Alert>
    <NewProject
      v-if="newProjectSession"
      :open="newProjectOpen"
      :return-focus="newProjectTrigger"
      @update:open="newProjectOpen = $event"
      @discarded="closeNewProject"
    />

    <ProjectSchedules v-if="showSchedules">
      <template #views>
        <h1 class="sr-only">启动计划</h1>
        <nav class="project-views" aria-label="项目与计划视图">
          <RouterLink to="/projects">项目</RouterLink>
          <RouterLink to="/project-schedules" class="active" aria-current="page">启动计划</RouterLink>
        </nav>
      </template>
      <template #actions>
        <Button @click="openNewProject($event)">
          <template #icon><Plus :size="14" /></template>新建项目
        </Button>
      </template>
    </ProjectSchedules>
    <div v-else class="projects-container">


      <Alert
        v-if="hasStaleProjectsError"
        class="projects-stale-alert"
        tone="warning"
        title="项目刷新失败"
      >
        项目列表仍显示最近一次成功获取的数据。
        <template #actions>
          <Button size="sm" variant="secondary" :loading="retryingRefresh" @click="retryRefresh()"
            >重试刷新</Button
          >
        </template>
      </Alert>

      <Alert v-if="actionError" tone="danger" title="项目操作失败">{{ actionError }}</Alert>

      <!-- 列表状态保留查询入口，刷新失败不清空已有结果。 -->
      <div class="projects-list-wrapper">
        <div
          v-if="isLoading && !hasProjectsSnapshot"
          class="skeleton-list"
          aria-label="正在加载项目"
        >
          <div v-for="i in 5" :key="i" class="skeleton-row">
            <div class="skeleton-text-group">
              <Skeleton shape="text" class="skeleton-line title" />
              <Skeleton shape="text" class="skeleton-line subtitle" />
            </div>
            <Skeleton class="skeleton-chip" />
            <Skeleton class="skeleton-stats" />
          </div>
        </div>

        <ErrorState
          v-else-if="isError && !hasProjectsSnapshot"
          title="项目数据加载失败"
          description="无法连接至平台后端服务器。请检查服务端状态。"
          retry-label="重新加载"
          @retry="retryRefresh()"
        />

        <EmptyState
          v-else-if="filteredProjects.length === 0"
          :title="hasFilters ? '没有匹配的项目' : '还没有项目'"
        >
          <template v-if="!hasFilters" #actions>
            <Button @click="openNewProject($event)">新建项目</Button>
          </template>
          <template v-else #actions
            ><Button variant="secondary" @click="clearFilters">清除筛选</Button></template
          >
        </EmptyState>

        <div v-else class="project-table-scroll" ref="listElement">
          <table class="project-table" aria-label="项目列表">
            <thead>
              <tr>
                <th scope="col" class="project-name-column">项目名称</th>
                <th scope="col" class="project-status-column">状态</th>
                <th scope="col" class="project-scenario-column">探索场景</th>
                <th v-if="showTenantAttribution" scope="col" class="project-tenant-column">
                  所属账户
                </th>
                <th scope="col" class="project-token-column">累计 Token</th>
                <th scope="col" class="project-time-column">创建时间</th>
                <th scope="col" class="project-actions-column">
                  <span class="sr-only">操作</span>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(p, index) in filteredProjects"
                :key="p.id"
                class="project-row"
                :class="{ focused: index === focusedIndex }"
              >
                <td>
                  <RouterLink
                    :to="`/projects/${p.id}`"
                    class="project-row-main"
                    :title="p.title"
                    :aria-label="`进入工作空间 ${p.title}，${getProjectStatus(p).label}`"
                    @focus="focusedIndex = index"
                    @keydown.down.prevent="moveFocus(1)"
                    @keydown.up.prevent="moveFocus(-1)"
                  >
                    {{ p.title }}
                  </RouterLink>
                </td>
                <td>
                  <div
                    class="project-row-status"
                    :class="{ 'project-row-status--scheduled': p.status === 'pending' }"
                  >
                    <ProjectStatusObservation
                      :project="p"
                      :control-waiting-expired="observationExpired(p.id)"
                      :runtime="runtimeById.get(p.id)"
                      :data-updated-at="runtimeUpdatedAtFor(p.id)"
                      :failed="runtimeError"
                      :fetching="runtimeFetching"
                      :now="runtimeNow"
                      @refresh="refreshRuntimeObservation"
                    />
                    <span v-if="isResumeRetry(p) && !canPerform(p.id, 'resume')">
                      重试继续：{{ actionReasonText(runtimeError ? 'runtime_unavailable' : runtimeById.get(p.id)?.actions?.resume?.reason) }}
                    </span>
                    <RouterLink
                      v-if="p.status === 'pending' && scheduleFor(p)"
                      :to="`/project-schedules/${scheduleFor(p)!.id}`"
                      class="pending-schedule-summary"
                    >
                      {{ scheduleLocalTime(scheduleFor(p)!.next_run_at, scheduleFor(p)!.timezone) }}
                      · {{ scheduleFor(p)!.timezone }} ·
                      {{ scheduleStatusLabels[scheduleFor(p)!.status] }}
                    </RouterLink>
                  </div>
                </td>
                <td class="project-scenario">{{ getScenarioLabel(p) }}</td>
                <td
                  v-if="showTenantAttribution"
                  :title="`${p.tenant_name} (${p.tenant_id})${p.tenant_owner_login ? ` · ${p.tenant_owner_login}` : ''}`"
                >
                  {{ p.tenant_name }}
                </td>
                <td class="project-token-column"><ProjectConsumption :consumption="p.consumption" /></td>
                <td class="created-at" :title="p.created_at">
                  {{ formatRelativeTime(p.created_at) }}
                </td>
                <td class="project-actions">
                  <DropdownMenu
                    :open="openDropdownId === p.id"
                    :items="getProjectMenuItems(p)"
                    @update:open="handleProjectMenuOpenChange(p.id, $event)"
                    @select="handleProjectMenuSelect(p, $event)"
                  >
                    <template #trigger>
                      <IconButton size="sm" label="项目操作菜单">
                        <MoreHorizontal :size="16" aria-hidden="true" />
                      </IconButton>
                    </template>
                    <template #icon="{ item }">
                      <ExternalLink v-if="item.value === 'open'" :size="14" />
                      <Pause v-else-if="item.value === 'stop'" :size="14" />
                      <Play v-else-if="item.value === 'resume'" :size="14" />
                      <ShieldCheck v-else-if="item.value === 'reauthorize'" :size="14" />
                      <Pencil v-else-if="item.value === 'rename'" :size="14" />
                      <Download v-else-if="item.value === 'export'" :size="14" />
                      <Trash2 v-else-if="item.value === 'delete'" :size="14" />
                    </template>
                  </DropdownMenu>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <p v-if="hasProjectsSnapshot" class="project-count" role="status" aria-live="polite">
        {{ filteredProjects.length }} 个项目
      </p>

      <Dialog
        :open="Boolean(renameTarget)"
        title="重命名项目"
        :dismissible="!renameSaving"
        @update:open="handleRenameDialogOpenChange"
      >
        <label class="project-dialog-label" for="project-rename-title">项目新名称</label>
        <Input
          id="project-rename-title"
          v-model="renameNewTitle"
          placeholder="新标题名称"
          :disabled="renameSaving"
          @keyup.enter="saveRename"
        />

        <template #footer>
          <Button variant="secondary" :disabled="renameSaving" @click="closeRenameDialog">
            取消
          </Button>
          <Button :loading="renameSaving" :disabled="!renameNewTitle.trim()" @click="saveRename">
            保存
          </Button>
        </template>
      </Dialog>

      <ConfirmAction
        :open="Boolean(resumeRetryTarget)"
        title="重试继续项目"
        description="将重新核验并恢复原现场，不重跑已成功的 Bootstrap。物理恢复确认后可能继续后续研究并产生模型费用。"
        confirm-label="确认重试继续"
        tone="warning"
        :loading="Boolean(pendingProjectId)"
        :confirm-disabled="!resumeRetryTarget || !canPerform(resumeRetryTarget.id, 'resume')"
        @update:open="(open) => { if (!open && !pendingProjectId) resumeRetryTarget = null }"
        @confirm="confirmResumeRetry"
      >
        <Alert v-if="actionError" tone="danger" title="项目操作失败">{{ actionError }}</Alert>
      </ConfirmAction>
      <ConfirmAction
        :open="Boolean(finishTarget)"
        title="结束评估"
        description="结束后不能恢复本次评估。系统将终止所属研究执行，保留已保存材料；独立报告、聊天和复测不受影响，不会自动生成报告。确认结束吗？"
        confirm-label="确认结束评估"
        tone="danger"
        :loading="Boolean(pendingProjectId)"
        @update:open="
          (open) => {
            if (!open && !pendingProjectId) finishTarget = null
          }
        "
        :confirm-disabled="!finishTarget || !canPerform(finishTarget.id, 'finish')"
        @confirm="saveFinish"
      >
        <Alert v-if="actionError" tone="danger" title="项目操作失败">{{ actionError }}</Alert>
      </ConfirmAction>
      <ConfirmAction
        :open="Boolean(reauthorizationTarget)"
        title="重新确认项目授权"
        description="为当前冻结范围创建新的绝对授权时间窗。已有 Facts、Evidence、Coverage 和累计 Explore 次数保持不变；执行停止时间不变，授权更新后项目仍保持暂停，需要单独恢复执行。"
        confirm-label="确认重新授权"
        tone="warning"
        :loading="Boolean(pendingProjectId)"
        :confirm-disabled="
          !reauthorizationTarget ||
          !reauthorizationConfirmed ||
          !canPerform(reauthorizationTarget.project.id, 'reauthorize')
        "
        @update:open="
          (open) => {
            if (!open && !pendingProjectId) {
              reauthorizationTarget = null
              reauthorizationConfirmed = false
            }
          }
        "
        @confirm="saveReauthorization"
      >
        <dl v-if="reauthorizationTarget" class="authorization-summary">
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
            <dt>新窗口长度</dt>
            <dd>{{ formatAuthorizationWindow(reauthorizationTarget.project) }}</dd>
          </div>
        </dl>
        <label class="authorization-confirmation">
          <Checkbox
            v-model="reauthorizationConfirmed"
            :disabled="Boolean(pendingProjectId)"
            aria-label="确认当前项目授权范围"
          />
          <span>我已核对当前项目范围，并确认重新授权后仍需单独点击“继续项目”。</span>
        </label>
        <Alert v-if="actionError" tone="danger" title="项目操作失败">{{ actionError }}</Alert>
      </ConfirmAction>
      <ConfirmAction
        :open="Boolean(deleteTarget)"
        title="删除工作空间"
        :description="deleteProjectDescription"
        confirm-label="确认删除"
        tone="danger"
        :loading="deleteSaving"
        @update:open="handleDeleteDialogOpenChange"
        @confirm="saveDelete"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, shallowRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import {
  listProjectSchedules,
  scheduleLocalTime,
  scheduleStatusLabels,
} from '@/api/projectSchedules'
import {
  Download,
  ExternalLink,
  MoreHorizontal,
  Pause,
  Pencil,
  Play,
  Plus,
  ShieldCheck,
  Search,
  Trash2,
} from '@lucide/vue'
import { ConfirmAction, ErrorState, FilterBar } from '@/components/patterns'
import SearchFilter from '@/components/patterns/SearchFilter.vue'
import {
  Alert,
  Button,
  Checkbox,
  Dialog,
  DropdownMenu,
  EmptyState,
  IconButton,
  Input,
  Select,
  Skeleton,
} from '@/components/ui'
import type { DropdownMenuOption, SelectOption } from '@/components/ui'
import type { ProjectSummary } from '@/api/types'
import { useProjectsPage } from './useProjectsPage'
import ProjectStatusObservation from './ProjectStatusObservation.vue'
import ProjectConsumption from './ProjectConsumption.vue'
import { actionReasonText, isResumeRetry, projectResumeLabel } from './projectRuntime'
import NewProject from './NewProject.vue'
import ProjectSchedules from './ProjectSchedules.vue'
import CreationRequestRecovery from './CreationRequestRecovery.vue'

const route = useRoute()
const router = useRouter()
const showSchedules = computed(
  () => route.name === 'ProjectSchedules' || route.query.view === 'schedules',
)
const newProjectSession = ref(false)
const newProjectOpen = ref(false)
const newProjectTrigger = shallowRef<HTMLElement | null>(null)

function openNewProject(event?: Event) {
  newProjectTrigger.value = event?.currentTarget instanceof HTMLElement ? event.currentTarget : null
  newProjectSession.value = true
  newProjectOpen.value = true
}

function closeNewProject() {
  newProjectOpen.value = false
  newProjectSession.value = false
}

const {
  activeSegment,
  actionError,
  pendingProjectId,
  canPerform,
  confirmFinish,
  finishTarget,
  saveFinish,
  confirmReauthorization,
  reauthorizationConfirmed,
  reauthorizationTarget,
  saveReauthorization,
  changeStatus,
  confirmDeleteProject,
  deleteSaving,
  deleteTarget,
  exportProjectYaml,
  filteredProjects,
  focusedIndex,
  formatRelativeTime,
  getProjectStatus,
  runtimeById,
  runtimeUpdatedAtFor,
  observationExpired,
  runtimeError,
  runtimeFetching,
  runtimeNow,
  refreshRuntimeObservation,
  getScenarioLabel,
  hasProjectsSnapshot,
  hasStaleProjectsError,
  isError,
  isLoading,
  retryRefresh,
  retryingRefresh,
  listElement,
  moveFocus,
  navigateToWorkspace,
  openDropdownId,
  openRenameDialog,
  renameNewTitle,
  renameSaving,
  renameTarget,
  saveDelete,
  saveRename,
  searchQuery,
  segments,
  showTenantAttribution,
  selectedTenantId,
  sortBy,
  tenantOptions,
} = useProjectsPage()
const hasFilters = computed(
  () =>
    Boolean(searchQuery.value.trim()) ||
    activeSegment.value !== 'all' ||
    selectedTenantId.value !== 'all',
)
const { data: pendingSchedules } = useQuery({
  queryKey: ['project-schedules'],
  queryFn: listProjectSchedules,
  enabled: computed(() => filteredProjects.value.some((project) => project.status === 'pending')),
  refetchInterval: 8000,
  retry: false,
})
const scheduleFor = (project: ProjectSummary) =>
  pendingSchedules.value?.find(
    (schedule) => schedule.id === project.schedule_id || schedule.project_id === project.id,
  )

const sortOptions: SelectOption[] = [
  { value: 'created_desc', label: '最近创建' },
  { value: 'created_asc', label: '最早创建' },
  { value: 'facts_desc', label: '事实数量最多' },
  { value: 'intents_desc', label: '待处理与执行中工作最多' },
]
const tenantFilterOptions = computed(() => [
  { value: 'all', label: '全部账户' },
  ...tenantOptions.value.map((tenant) => ({
    value: tenant.id,
    label: `${tenant.name} (${tenant.id})`,
  })),
])

function clearFilters() {
  searchQuery.value = ''
  activeSegment.value = 'all'
  selectedTenantId.value = 'all'
}

const deleteProjectDescription = computed(() =>
  deleteTarget.value
    ? `您确定要永久删除项目「${deleteTarget.value.title}」及关联的所有数据吗？此操作不可逆，并将清空所有 Fact、Intent、提示和任务执行审计日志。`
    : '此操作将永久删除项目及关联数据，且不可逆。',
)

function formatAuthorizationWindow(project: ProjectSummary): string {
  const seconds = project.authorization_window_seconds
  if (!seconds || seconds <= 0) return '由服务端冻结策略决定'
  return seconds % 3600 === 0 ? `${seconds / 3600} 小时` : `${Math.round(seconds / 60)} 分钟`
}

function closeRenameDialog() {
  if (!renameSaving.value) renameTarget.value = null
}

function handleRenameDialogOpenChange(open: boolean) {
  if (!open) closeRenameDialog()
}

function handleDeleteDialogOpenChange(open: boolean) {
  if (!open && !deleteSaving.value) deleteTarget.value = null
}

const resumeRetryTarget = ref<ProjectSummary | null>(null)
async function confirmResumeRetry() {
  if (!resumeRetryTarget.value) return
  await changeStatus(resumeRetryTarget.value.id, 'resume')
  if (!actionError.value) resumeRetryTarget.value = null
}

function getProjectMenuItems(project: ProjectSummary): DropdownMenuOption[] {
  const items: DropdownMenuOption[] = [{ value: 'open', label: '进入工作空间' }]
  if (project.schedule_id) items.push({ value: 'schedule', label: '查看启动计划' })

  if (project.status === 'active') {
    items.push({
      value: 'stop',
      label: '暂停项目',
      description: '暂停满12小时将自动结束，工作文件保留',
      disabled: !canPerform(project.id, 'pause'),
      separatorBefore: true,
    })
  }
  if (project.status === 'paused' || isResumeRetry(project) ||
    project.execution_control?.action === 'resume' && project.execution_control.state === 'requested') {
    if (project.status === 'paused' && runtimeById.value.get(project.id)?.actions?.reauthorize?.allowed) {
      items.push({
        value: 'reauthorize',
        label: '重新确认授权',
        disabled: !canPerform(project.id, 'reauthorize'),
        separatorBefore: true,
      })
    }
    items.push({
      value: 'resume',
      label: projectResumeLabel(project),
      disabled: !canPerform(project.id, 'resume'),
      separatorBefore: !runtimeById.value.get(project.id)?.actions?.reauthorize?.allowed,
    })
  }

  if (['active', 'paused'].includes(project.status)) {
    items.push({
      value: 'finish',
      label: '结束评估',
      disabled: !canPerform(project.id, 'finish'),
      tone: 'danger',
      separatorBefore: true,
    })
  }

  items.push(
    { value: 'rename', label: '重命名项目', separatorBefore: true },
    { value: 'export', label: '导出配置 (YAML)' },
    {
      value: 'delete',
      label: '删除项目',
      separatorBefore: true,
      tone: 'danger',
    },
  )
  return items
}

function handleProjectMenuOpenChange(projectId: string, open: boolean) {
  openDropdownId.value = open ? projectId : null
}

function handleProjectMenuSelect(project: ProjectSummary, value: string) {
  if (value === 'schedule' && project.schedule_id)
    void router.push(`/project-schedules/${project.schedule_id}`)
  else if (value === 'open') navigateToWorkspace(project.id)
  else if (value === 'stop') changeStatus(project.id, 'pause')
  else if (value === 'resume') {
    if (isResumeRetry(project) && canPerform(project.id, 'resume')) {
      openDropdownId.value = null
      resumeRetryTarget.value = project
    } else void changeStatus(project.id, 'resume')
  }
  else if (value === 'reauthorize') confirmReauthorization(project)
  else if (value === 'finish') confirmFinish(project)
  else if (value === 'rename') openRenameDialog(project)
  else if (value === 'export') exportProjectYaml(project.id, project.title)
  else if (value === 'delete') confirmDeleteProject(project)
}

void listElement
</script>

<style src="./Projects.css" scoped></style>

<style scoped>
.project-row-status--scheduled {
  display: grid;
  justify-items: start;
}
.pending-schedule-summary {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--text-muted);
  font-size: var(--text-xs);
}
.authorization-summary {
  display: grid;
  gap: var(--space-2);
  margin: 0;
}

.authorization-summary > div {
  display: grid;
  gap: var(--space-1);
}

.authorization-summary dt {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.authorization-summary dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.authorization-confirmation {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  margin-top: var(--space-3);
}
</style>
