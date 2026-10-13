<template>
  <div class="projects-page">
    <div class="view-header view-header--actions">
      <div class="header-right">
        <div class="refresh-indicator" title="实时刷新">
          <span class="live-dot live-success" :class="{ 'animate-pulse-live': isFetching }"></span>
          <span class="refresh-label text-faint">{{ isFetching ? '正在刷新' : '实时刷新' }}</span>
        </div>
        <Button @click="$router.push('/projects/new')">
          <template #icon><Plus :size="14" /></template>
          新建项目
        </Button>
      </div>
    </div>

    <!-- 分组已由左侧导航栏（FrontLayout）承载，本页不再重复侧栏，列表区域整宽展示 -->
    <div class="projects-container">
      <FilterBar class="toolbar" label="项目筛选">
        <div class="segmented-control" aria-label="项目状态">
          <button
            v-for="seg in segments"
            :key="seg.value"
            type="button"
            class="segment-btn"
            :class="{ active: activeSegment === seg.value }"
            :aria-pressed="activeSegment === seg.value"
            @click="activeSegment = seg.value"
          >
            {{ seg.label }}
            <Badge v-if="getSegmentCount(seg.value) > 0" size="sm">
              {{ getSegmentCount(seg.value) }}
            </Badge>
          </button>
        </div>

        <FormField class="project-search-field" label="搜索" for="project-search">
          <Input
            id="project-search"
            v-model="searchQuery"
            :placeholder="
              showTenantAttribution ? '搜索项目、Tenant、分组或场景...' : '搜索项目、分组或场景...'
            "
            @keydown.esc="searchQuery = ''"
          />
        </FormField>

        <FormField
          v-if="showTenantAttribution && tenantOptions.length > 1"
          class="project-filter-field"
          label="Tenant"
          for="project-tenant-filter"
        >
          <Select
            id="project-tenant-filter"
            v-model="selectedTenantId"
            :options="tenantFilterOptions"
          />
        </FormField>

        <FormField class="project-filter-field" label="排序" for="project-sort">
          <Select id="project-sort" v-model="sortBy" :options="sortOptions" />
        </FormField>

        <template #status>
          <span v-if="isFetching">正在获取最新项目数据…</span>
          <span v-else>{{ filteredProjects.length }} 个项目</span>
        </template>
      </FilterBar>

      <Alert
        v-if="hasStaleProjectsError"
        class="projects-stale-alert"
        tone="warning"
        title="项目刷新失败"
      >
        当前显示最近一次成功获取的数据。
        <template #actions>
          <Button size="sm" variant="secondary" @click="refetch()">重试刷新</Button>
        </template>
      </Alert>

      <Alert v-if="actionError" tone="danger" title="项目操作失败">{{ actionError }}</Alert>

      <!-- Projects List -->
      <div class="projects-list-wrapper">
        <div
          v-if="isLoading && !hasProjectsSnapshot"
          class="skeleton-list"
          aria-label="正在加载项目"
        >
          <div v-for="i in 5" :key="i" class="skeleton-row">
            <Skeleton shape="circle" class="skeleton-circle" />
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
          @retry="refetch()"
        />

        <EmptyState
          v-else-if="filteredProjects.length === 0"
          title="没有找到匹配的项目"
          :description="
            searchQuery || activeSegment !== 'all'
              ? '尝试调整您的搜索关键字或状态筛选器。'
              : '您还没有创建任何项目，开始创建一个新的探索工作空间吧！'
          "
        >
          <template v-if="!searchQuery && activeSegment === 'all'" #actions>
            <Button @click="$router.push('/projects/new')">新建项目</Button>
          </template>
        </EmptyState>

        <!-- Loaded Projects Table/List -->
        <div v-else class="project-rows" role="list" ref="listElement">
          <div
            v-for="(p, index) in filteredProjects"
            :key="p.id"
            class="project-row"
            :class="{ focused: index === focusedIndex, 'menu-open': openDropdownId === p.id }"
            role="listitem"
          >
            <button
              type="button"
              class="project-row-main"
              :aria-label="`进入工作空间 ${p.title}`"
              @focus="focusedIndex = index"
              @click="navigateToWorkspace(p.id)"
              @keydown.down.prevent="moveFocus(1)"
              @keydown.up.prevent="moveFocus(-1)"
            >
              <!-- Left Status + Title -->
              <div class="row-left">
                <span
                  class="status-indicator"
                  :class="getStatusClass(p.status)"
                  :title="getProjectRuntime(p).label"
                ></span>
                <div class="title-section">
                  <div class="title-row">
                    <span class="project-title">{{ p.title }}</span>
                    <span class="project-id mono text-faint">#{{ p.id }}</span>
                  </div>
                </div>
              </div>

              <!-- Middle Meta Scenario & Stats -->
              <div class="row-middle">
                <div class="scenario-badge-wrapper">
                  <Badge tone="info" size="sm" :title="`项目组: ${getGroupName(p)}`">
                    {{ getGroupName(p) }}
                  </Badge>
                  <Badge size="sm" :title="`场景方案: ${getScenarioLabel(p)}`">
                    <Wrench :size="10" aria-hidden="true" />
                    {{ getScenarioLabel(p) }}
                  </Badge>
                </div>

                <div
                  v-if="showTenantAttribution"
                  class="tenant-attribution text-muted"
                  :title="`Tenant: ${p.tenant_name} (${p.tenant_id})${p.tenant_owner_login ? ` · ${p.tenant_owner_login}` : ''}`"
                >
                  <span class="meta-label">Tenant</span>
                  <span>{{ p.tenant_name }}</span>
                  <span class="mono tenant-id">{{ p.tenant_id }}</span>
                  <span v-if="p.tenant_owner_login" class="tenant-owner">{{
                    p.tenant_owner_login
                  }}</span>
                </div>

                <div class="stats-group mono">
                  <span class="stat-item" title="事实数量">
                    <span class="stat-num">{{ p.fact_count }}</span>
                    <span class="stat-lbl">Facts</span>
                  </span>
                  <span class="stat-item" title="决策意图活跃/未领取">
                    <span class="stat-num text-intent"
                      >{{ p.working_intent_count }}<span class="separator">/</span
                      >{{ p.unclaimed_intent_count }}</span
                    >
                    <span class="stat-lbl">Intents</span>
                  </span>
                  <span class="stat-item" title="人工提示数量">
                    <span class="stat-num">{{ p.hint_count }}</span>
                    <span class="stat-lbl">Hints</span>
                  </span>
                </div>
              </div>
            </button>

            <!-- Right Reason Claim & Actions -->
            <div class="row-right">
              <div class="project-row-status" aria-label="项目运行状态">
                <Badge :tone="getProjectRuntime(p).tone" size="sm">
                  {{ getProjectRuntime(p).label }}
                </Badge>
                <span class="project-row-reason" :title="getProjectRuntime(p).detail">
                  {{ getProjectRuntime(p).detail }}
                </span>
              </div>

              <div class="created-at text-faint" :title="p.created_at">
                {{ formatRelativeTime(p.created_at) }}
              </div>

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
                  <FolderInput v-else-if="item.value === 'move'" :size="14" />
                  <Pencil v-else-if="item.value === 'rename'" :size="14" />
                  <Download v-else-if="item.value === 'export'" :size="14" />
                  <Trash2 v-else-if="item.value === 'delete'" :size="14" />
                </template>
              </DropdownMenu>
            </div>
          </div>
        </div>
      </div>

      <Dialog
        :open="Boolean(renameTarget)"
        title="重命名项目"
        description="更新项目在列表和工作空间中显示的名称。"
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

      <Dialog
        :open="Boolean(moveTarget)"
        title="移动到分组"
        :description="moveDialogDescription"
        :dismissible="!moveSaving"
        @update:open="handleMoveDialogOpenChange"
      >
        <label class="project-dialog-label" for="project-move-group">目标分组</label>
        <Select
          id="project-move-group"
          v-model="moveGroupSelection"
          :options="moveGroupOptions"
          :disabled="moveSaving"
          aria-label="目标分组"
        />

        <template #footer>
          <Button variant="secondary" :disabled="moveSaving" @click="closeMoveDialog">
            取消
          </Button>
          <Button :loading="moveSaving" @click="saveMove">移动</Button>
        </template>
      </Dialog>

      <ConfirmAction
        :open="Boolean(finishTarget)"
        title="结束评估"
        description="结束后不能恢复本次评估；未完成工作将进入收尾，可基于已有证据生成部分报告。确认结束吗？"
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
    <!-- /.projects-container -->
  </div>
  <!-- /.projects-page -->
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  Download,
  ExternalLink,
  FolderInput,
  MoreHorizontal,
  Pause,
  Pencil,
  Play,
  Plus,
  ShieldCheck,
  Trash2,
  Wrench,
} from '@lucide/vue'
import { ConfirmAction, ErrorState, FilterBar } from '@/components/patterns'
import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Dialog,
  DropdownMenu,
  EmptyState,
  FormField,
  IconButton,
  Input,
  Select,
  Skeleton,
} from '@/components/ui'
import type { DropdownMenuOption, SelectOption } from '@/components/ui'
import type { ProjectSummary } from '@/api/types'
import { useProjectsPage } from './useProjectsPage'

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
  getGroupName,
  getProjectRuntime,
  runtimeById,
  getScenarioLabel,
  getSegmentCount,
  getStatusClass,
  hasProjectsSnapshot,
  hasStaleProjectsError,
  isError,
  isFetching,
  isLoading,
  listElement,
  moveFocus,
  moveGroups,
  moveSaving,
  moveTarget,
  moveTargetGroupId,
  navigateToWorkspace,
  openDropdownId,
  openMoveDialog,
  openRenameDialog,
  refetch,
  renameNewTitle,
  renameSaving,
  renameTarget,
  saveDelete,
  saveMove,
  saveRename,
  searchQuery,
  segments,
  showTenantAttribution,
  selectedTenantId,
  sortBy,
  tenantOptions,
} = useProjectsPage()

const ungroupedOptionValue = '__ungrouped__'
const sortOptions: SelectOption[] = [
  { value: 'created_desc', label: '最近创建' },
  { value: 'created_asc', label: '最早创建' },
  { value: 'facts_desc', label: '事实数量最多' },
  { value: 'intents_desc', label: '活跃度最高' },
]
const tenantFilterOptions = computed<SelectOption[]>(() => [
  { value: 'all', label: '全部 Tenant' },
  ...tenantOptions.value.map((tenant) => ({
    value: tenant.id,
    label: `${tenant.name} (${tenant.id})`,
  })),
])

const moveGroupOptions = computed<SelectOption[]>(() => [
  { value: ungroupedOptionValue, label: '未分组' },
  ...moveGroups.value.map((group) => ({ value: group.id, label: group.name })),
])

const moveGroupSelection = computed({
  get: () => moveTargetGroupId.value || ungroupedOptionValue,
  set: (value: string) => {
    moveTargetGroupId.value = value === ungroupedOptionValue ? '' : value
  },
})

const moveDialogDescription = computed(() =>
  moveTarget.value ? `将项目「${moveTarget.value.title}」移动到所选分组。` : '选择项目目标分组。',
)

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

function closeMoveDialog() {
  if (!moveSaving.value) moveTarget.value = null
}

function handleMoveDialogOpenChange(open: boolean) {
  if (!open) closeMoveDialog()
}

function handleDeleteDialogOpenChange(open: boolean) {
  if (!open && !deleteSaving.value) deleteTarget.value = null
}

function getProjectMenuItems(project: ProjectSummary): DropdownMenuOption[] {
  const items: DropdownMenuOption[] = [{ value: 'open', label: '进入工作空间' }]

  if (project.status === 'active') {
    items.push({
      value: 'stop',
      label: '暂停项目',
      disabled: !canPerform(project.id, 'pause'),
      separatorBefore: true,
    })
  } else if (project.status === 'paused') {
    if (runtimeById.value.get(project.id)?.actions?.reauthorize?.allowed) {
      items.push({
        value: 'reauthorize',
        label: '重新确认授权',
        disabled: !canPerform(project.id, 'reauthorize'),
        separatorBefore: true,
      })
    }
    items.push({
      value: 'resume',
      label: '继续项目',
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
    })
  }

  items.push(
    { value: 'move', label: '移动到分组' },
    { value: 'rename', label: '重命名项目' },
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
  if (value === 'open') navigateToWorkspace(project.id)
  else if (value === 'stop') changeStatus(project.id, 'pause')
  else if (value === 'resume') changeStatus(project.id, 'resume')
  else if (value === 'reauthorize') confirmReauthorization(project)
  else if (value === 'finish') confirmFinish(project)
  else if (value === 'move') openMoveDialog(project)
  else if (value === 'rename') openRenameDialog(project)
  else if (value === 'export') exportProjectYaml(project.id, project.title)
  else if (value === 'delete') confirmDeleteProject(project)
}

void listElement
</script>

<style src="./Projects.css" scoped></style>

<style scoped>
/* 项目组筛选徽章（左侧导航分组选中后仍展示归属信息） */
.group-badge {
  background: var(--accent-dim);
  color: var(--accent);
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
