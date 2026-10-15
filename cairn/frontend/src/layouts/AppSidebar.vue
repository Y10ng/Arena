<template>
  <aside
    class="sidebar"
    :class="{ 'sidebar-collapsed': uiStore.isSidebarCollapsed }"
    aria-label="主导航"
  >
    <div class="brand">
      <div class="logo">
        <span class="brand-logo-mark" aria-hidden="true">A</span>
      </div>
      <span class="brand-text">Arena</span>
      <IconButton class="mobile-sidebar-close" label="关闭主导航" size="sm" @click="closeSidebar">
        <X :size="16" />
      </IconButton>
    </div>

    <nav class="nav-links" aria-label="应用导航">
      <template v-for="item in primaryItems" :key="item.routeName">
        <div
          v-if="item.routeName === 'Projects'"
          class="nav-group"
          :class="{ 'group-open': isTasksOpen }"
        >
          <div class="nav-group-header-wrapper">
            <button
              type="button"
              class="nav-item nav-group-header"
              :class="{ active: route.path === '/projects' }"
              @click="toggleTasks"
            >
              <span class="icon"
                ><component :is="iconFor(item.icon)" :size="16" aria-hidden="true"
              /></span>
              <span class="label">{{ item.label }}</span>
              <span v-if="!uiStore.isSidebarCollapsed" class="arrow-icon">
                <ChevronDown :size="12" aria-hidden="true" />
              </span>
            </button>
            <IconButton
              v-if="!uiStore.isSidebarCollapsed"
              class="btn-group-add-inline"
              label="新建项目组"
              size="sm"
              @click="openCreateGroupModal"
            >
              <Plus :size="14" />
            </IconButton>
          </div>

          <div v-show="isTasksOpen && !uiStore.isSidebarCollapsed" class="nav-sub-items">
            <div v-for="group in projectGroupsNav" :key="group.id" class="sub-item-wrapper">
              <RouterLink
                :to="`/projects?group=${group.id}`"
                class="nav-sub-item"
                :class="{ active: route.path === '/projects' && route.query.group === group.id }"
                @click="closeSidebarAfterNavigation"
              >
                <span class="icon"><Folder :size="14" aria-hidden="true" /></span>
                <span class="sub-label text-truncate">{{ group.name }}</span>
              </RouterLink>
              <DropdownMenu
                :open="activeGroupActionId === group.id"
                :items="groupActionItems"
                align="end"
                @update:open="handleGroupActionOpenChange(group.id, $event)"
                @select="handleGroupActionSelect(group, $event)"
              >
                <template #trigger>
                  <button
                    type="button"
                    class="btn-sub-more"
                    :aria-label="`项目组选项：${group.name}`"
                    @click.stop
                  >
                    <MoreHorizontal :size="14" aria-hidden="true" />
                  </button>
                </template>
                <template #icon><Trash2 :size="13" aria-hidden="true" /></template>
              </DropdownMenu>
            </div>
          </div>
        </div>

        <RouterLink
          v-else
          :to="{ name: item.routeName }"
          class="nav-item"
          active-class=""
          exact-active-class="active"
          :class="{ active: route.name === item.routeName || (item.routeName === 'Verifications' && route.path.startsWith('/verifications/')) }"
          @click="closeSidebarAfterNavigation"
        >
          <span class="icon"
            ><component :is="iconFor(item.icon)" :size="16" aria-hidden="true"
          /></span>
          <span class="label">{{ item.label }}</span>
        </RouterLink>
      </template>

      <div v-if="scenarioItems.length" class="nav-group" :class="{ 'group-open': isScenariosOpen }">
        <button
          type="button"
          class="nav-item nav-group-header"
          :class="{ active: scenarioItems.some((item) => item.routeName === route.name) }"
          @click="toggleScenarios"
        >
          <span class="icon"><Layers3 :size="16" aria-hidden="true" /></span>
          <span class="label">探索场景</span>
          <span v-if="!uiStore.isSidebarCollapsed" class="arrow-icon"
            ><ChevronDown :size="12" aria-hidden="true"
          /></span>
        </button>
        <div v-show="isScenariosOpen && !uiStore.isSidebarCollapsed" class="nav-sub-items">
          <RouterLink
            v-for="item in scenarioItems"
            :key="item.routeName"
            :to="{ name: item.routeName }"
            class="nav-sub-item"
            :class="{ active: route.name === item.routeName }"
            @click="closeSidebarAfterNavigation"
          >
            <span class="icon"
              ><component :is="iconFor(item.icon)" :size="14" aria-hidden="true"
            /></span>
            <span>{{ item.label }}</span>
          </RouterLink>
        </div>
      </div>

      <RouterLink
        v-for="item in standaloneItems"
        :key="item.routeName"
        :to="{ name: item.routeName }"
        class="nav-item"
        active-class=""
        exact-active-class="active"
        :class="{ active: route.name === item.routeName }"
        @click="closeSidebarAfterNavigation"
      >
        <span class="icon"
          ><component :is="iconFor(item.icon)" :size="16" aria-hidden="true"
        /></span>
        <span class="label">{{ item.label }}</span>
      </RouterLink>

      <div class="nav-group" :class="{ 'group-open': isSysMgmtOpen }">
        <button
          type="button"
          class="nav-item nav-group-header"
          :class="{ active: systemItems.some((item) => item.routeName === route.name) }"
          @click="toggleSysMgmt"
        >
          <span class="icon"><Settings :size="16" aria-hidden="true" /></span>
          <span class="label">系统管理</span>
          <span v-if="!uiStore.isSidebarCollapsed" class="arrow-icon"
            ><ChevronDown :size="12" aria-hidden="true"
          /></span>
        </button>
        <div v-show="isSysMgmtOpen && !uiStore.isSidebarCollapsed" class="nav-sub-items">
          <RouterLink
            v-for="item in systemItems"
            :key="item.routeName"
            :to="{ name: item.routeName }"
            class="nav-sub-item"
            :class="{ active: route.name === item.routeName }"
            @click="closeSidebarAfterNavigation"
          >
            <span class="icon"
              ><component :is="iconFor(item.icon)" :size="14" aria-hidden="true"
            /></span>
            <span>{{ item.label }}</span>
          </RouterLink>
        </div>
      </div>
    </nav>

    <button
      v-if="authStore.isSuperAdmin"
      type="button"
      class="dispatcher-status"
      :aria-label="`打开监控，调度器状态: ${dispatcherStatusText}`"
      @click="goToMonitoring"
    >
      <div class="live-dot-container">
        <span class="live-dot" :class="dispatcherColorClass"></span>
        <span class="live-pulse" :class="dispatcherPulseClass"></span>
      </div>
      <div class="dispatcher-info">
        <div class="dispatcher-title">调度器: {{ dispatcherStatusText }}</div>
        <div v-if="dispatcherSummary" class="dispatcher-sub">
          最近循环 {{ lastLoopTimeText }} · {{ dispatcherSummary.last_loop_duration_ms || 0 }}ms
        </div>
        <div v-else class="dispatcher-sub">未连接</div>
      </div>
    </button>

    <Dialog
      :open="showCreateGroupModal"
      title="新建项目组"
      description="创建项目组后，可在任务管理侧栏中集中筛选和管理项目。"
      size="sm"
      :dismissible="!isCreatingGroup"
      @update:open="handleCreateGroupOpenChange"
      @open-auto-focus="focusCreateGroupInput"
    >
      <form id="create-project-group-form" @submit.prevent="handleCreateNavGroup">
        <label class="layout-dialog-label" for="project-group-name">项目组名称</label>
        <Input
          id="project-group-name"
          ref="modalInputRef"
          v-model="newGroupNameInput"
          placeholder="例如: 核心业务组件组"
          :disabled="isCreatingGroup"
        />
      </form>
      <template #footer>
        <Button variant="secondary" :disabled="isCreatingGroup" @click="closeCreateGroupModal"
          >取消</Button
        >
        <Button
          form="create-project-group-form"
          type="submit"
          :loading="isCreatingGroup"
          :disabled="!newGroupNameInput.trim()"
          >确认创建</Button
        >
      </template>
    </Dialog>

    <ConfirmAction
      :open="Boolean(groupToDelete)"
      title="删除项目组"
      :description="deleteGroupDescription"
      confirm-label="确认删除"
      tone="danger"
      :loading="isDeletingGroup"
      @update:open="handleDeleteGroupOpenChange"
      @confirm="handleDeleteNavGroup"
    />
  </aside>
</template>

<script setup lang="ts">
import {
  Activity,
  AlertTriangle,
  Boxes,
  Box,
  ChevronDown,
  CirclePlus,
  Clock3,
  FileArchive,
  Folder,
  Gauge,
  GitBranch,
  KeyRound,
  Layers3,
  Lightbulb,
  MoreHorizontal,
  Plus,
  RadioTower,
  ScrollText,
  Settings,
  ShieldAlert,
  Trash2,
  Users,
  Workflow,
  X,
  type LucideIcon,
} from '@lucide/vue'
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { api, createProjectGroup, deleteProjectGroup, listProjectGroups } from '@/api/client'
import type { MonitoringSummary, ProjectGroup } from '@/api/types'
import { ConfirmAction } from '@/components/patterns'
import { Button, Dialog, DropdownMenu, IconButton, Input } from '@/components/ui'
import type { DropdownMenuOption } from '@/components/ui'
import { dispatcherEffectiveStatus } from '@/features/monitoring/monitoringModel'
import { useAuthStore } from '@/stores/auth'
import { useUIStore } from '@/stores/ui'
import { sidebarNavigation, type NavigationIcon } from '@/router/navigationRegistry'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const uiStore = useUIStore()
const queryClient = useQueryClient()

const primaryItems = computed(() => sidebarNavigation('primary', authStore.isSuperAdmin))
const scenarioItems = computed(() => sidebarNavigation('scenario', authStore.isSuperAdmin))
const standaloneItems = computed(() => sidebarNavigation('standalone', authStore.isSuperAdmin))
const systemItems = computed(() => sidebarNavigation('system', authStore.isSuperAdmin))

const iconMap: Record<NavigationIcon, LucideIcon> = {
  dashboard: Gauge,
  finding: AlertTriangle,
  provenance: Activity,
  projects: Folder,
  add: CirclePlus,
  graph: GitBranch,
  runs: ScrollText,
  hints: Lightbulb,
  artifacts: FileArchive,
  vulnerabilities: ShieldAlert,
  monitoring: RadioTower,
  models: Boxes,
  workers: Box,
  users: Users,
  dispatcher: Workflow,
  scenarios: Layers3,
  prompts: ScrollText,
  knowledge: FileArchive,
  timeout: Clock3,
  status: ShieldAlert,
  auth: KeyRound,
}
const iconFor = (icon: NavigationIcon) => iconMap[icon]

const isTasksOpen = ref(route.path === '/projects' || route.query.group !== undefined)
const isScenariosOpen = ref(scenarioItems.value.some((item) => item.routeName === route.name))
const isSysMgmtOpen = ref(systemItems.value.some((item) => item.routeName === route.name))

watch(
  () => route.path,
  (path) => {
    if (path === '/projects') isTasksOpen.value = true
  },
)
watch(
  () => route.name,
  (name) => {
    if (scenarioItems.value.some((item) => item.routeName === name)) isScenariosOpen.value = true
    if (systemItems.value.some((item) => item.routeName === name)) isSysMgmtOpen.value = true
  },
)

const isMobileViewport = () =>
  typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 760px)').matches

const closeSidebar = () => {
  if (!uiStore.isSidebarCollapsed) uiStore.toggleSidebar()
}

const closeSidebarAfterNavigation = () => {
  if (isMobileViewport()) closeSidebar()
}

const goToMonitoring = () => {
  void router.push({ name: 'Monitoring' })
  closeSidebarAfterNavigation()
}

const expandCollapsedSidebar = () => {
  if (!uiStore.isSidebarCollapsed) return false
  uiStore.toggleSidebar()
  return true
}
const toggleTasks = () => {
  if (expandCollapsedSidebar()) {
    isTasksOpen.value = true
    return
  }
  if (isTasksOpen.value && route.path === '/projects' && !route.query.group) {
    isTasksOpen.value = false
  } else {
    isTasksOpen.value = true
    if (route.path !== '/projects' || route.query.group) void router.push('/projects')
  }
}
const toggleScenarios = () => {
  if (expandCollapsedSidebar()) isScenariosOpen.value = true
  else isScenariosOpen.value = !isScenariosOpen.value
}
const toggleSysMgmt = () => {
  if (expandCollapsedSidebar()) isSysMgmtOpen.value = true
  else isSysMgmtOpen.value = !isSysMgmtOpen.value
}

const { data: projectGroupsNavData, refetch: refetchNavGroups } = useQuery<ProjectGroup[]>({
  queryKey: ['project-groups'],
  queryFn: listProjectGroups,
})
const projectGroupsNav = computed(() => projectGroupsNavData.value || [])
const showCreateGroupModal = ref(false)
const modalInputRef = ref<InstanceType<typeof Input> | null>(null)
const newGroupNameInput = ref('')
const isCreatingGroup = ref(false)
const activeGroupActionId = ref<string | null>(null)
const groupToDelete = ref<ProjectGroup | null>(null)
const isDeletingGroup = ref(false)
const groupActionItems: DropdownMenuOption[] = [
  { value: 'delete', label: '删除分组', tone: 'danger' },
]
const getErrorMessage = (error: unknown, fallback: string) =>
  error instanceof Error && error.message ? error.message : fallback
const deleteGroupDescription = computed(() =>
  groupToDelete.value
    ? `您确定要删除项目组「${groupToDelete.value.name}」吗？删除后，组内项目将自动设为“未分组”，项目数据不会丢失。`
    : '删除项目组后，组内项目将自动设为“未分组”，项目数据不会丢失。',
)

const openCreateGroupModal = () => {
  newGroupNameInput.value = ''
  showCreateGroupModal.value = true
}
const closeCreateGroupModal = () => {
  if (!isCreatingGroup.value) showCreateGroupModal.value = false
}
const handleCreateGroupOpenChange = (open: boolean) => {
  if (!open) closeCreateGroupModal()
}
const focusCreateGroupInput = (event: Event) => {
  event.preventDefault()
  void nextTick(() => modalInputRef.value?.focus())
}
const handleCreateNavGroup = async () => {
  const name = newGroupNameInput.value.trim()
  if (!name || isCreatingGroup.value) return
  isCreatingGroup.value = true
  try {
    const created = await createProjectGroup({ name })
    uiStore.showToast(`项目组「${name}」已创建`, 'success')
    showCreateGroupModal.value = false
    newGroupNameInput.value = ''
    await refetchNavGroups()
    await queryClient.invalidateQueries({ queryKey: ['project-groups'] })
    await router.push(`/projects?group=${created.id}`)
  } catch (error: unknown) {
    uiStore.showToast(getErrorMessage(error, '创建项目组失败'), 'danger')
  } finally {
    isCreatingGroup.value = false
  }
}
const handleGroupActionOpenChange = (groupId: string, open: boolean) => {
  activeGroupActionId.value = open ? groupId : null
}
const handleGroupActionSelect = (group: ProjectGroup, value: string) => {
  if (value === 'delete') {
    activeGroupActionId.value = null
    groupToDelete.value = group
  }
}
const handleDeleteGroupOpenChange = (open: boolean) => {
  if (!open && !isDeletingGroup.value) groupToDelete.value = null
}
const handleDeleteNavGroup = async () => {
  if (!groupToDelete.value || isDeletingGroup.value) return
  const group = groupToDelete.value
  isDeletingGroup.value = true
  try {
    await deleteProjectGroup(group.id)
    groupToDelete.value = null
    uiStore.showToast(`项目组「${group.name}」已删除`, 'info')
    if (route.query.group === group.id) await router.push('/projects')
    await refetchNavGroups()
    await queryClient.invalidateQueries({ queryKey: ['projects'] })
    await queryClient.invalidateQueries({ queryKey: ['project-groups'] })
  } catch (error: unknown) {
    uiStore.showToast(getErrorMessage(error, '删除项目组失败'), 'danger')
  } finally {
    isDeletingGroup.value = false
  }
}

const { data: monitoringSummary } = useQuery<MonitoringSummary>({
  queryKey: ['monitoringSummary', 'dispatcher'],
  queryFn: () => api<MonitoringSummary>('GET', '/admin/monitoring?profile=dispatcher'),
  enabled: computed(() => authStore.isSuperAdmin),
  refetchInterval: 10000,
})
const dispatcherSummary = computed(() => monitoringSummary.value?.dispatcher || null)
const dispatcherStatus = computed(() => dispatcherEffectiveStatus(dispatcherSummary.value))
const dispatcherStatusText = computed(
  () =>
    ({ running: '正常', idle: '空闲', stale: '过期', error: '异常' })[dispatcherStatus.value] ||
    '未连接',
)
const dispatcherColorClass = computed(() =>
  dispatcherStatus.value === 'running'
    ? 'live-success'
    : dispatcherStatus.value === 'stale' || dispatcherStatus.value === 'error'
      ? 'live-danger'
      : 'live-neutral',
)
const dispatcherPulseClass = computed(() =>
  dispatcherStatus.value === 'running' ? 'animate-pulse-live' : '',
)
const lastLoopTimeText = computed(() => {
  const value =
    dispatcherSummary.value?.last_loop_finished_at || dispatcherSummary.value?.last_loop_started_at
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? '—'
    : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
})
</script>

<style src="./FrontLayout-shell.css" scoped></style>
<style src="./FrontLayout-palette.css" scoped></style>
