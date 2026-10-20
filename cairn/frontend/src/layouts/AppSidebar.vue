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
      <RouterLink
        v-for="item in primaryItems"
        :key="item.routeName"
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
  RadioTower,
  ScrollText,
  Settings,
  ShieldAlert,
  Users,
  Workflow,
  X,
  type LucideIcon,
} from '@lucide/vue'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { api } from '@/api/client'
import type { MonitoringSummary } from '@/api/types'
import { IconButton } from '@/components/ui'
import { dispatcherEffectiveStatus } from '@/features/monitoring/monitoringModel'
import { useAuthStore } from '@/stores/auth'
import { useUIStore } from '@/stores/ui'
import { sidebarNavigation, type NavigationIcon } from '@/router/navigationRegistry'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const uiStore = useUIStore()

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

const isScenariosOpen = ref(scenarioItems.value.some((item) => item.routeName === route.name))
const isSysMgmtOpen = ref(systemItems.value.some((item) => item.routeName === route.name))

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
const toggleScenarios = () => {
  if (expandCollapsedSidebar()) isScenariosOpen.value = true
  else isScenariosOpen.value = !isScenariosOpen.value
}
const toggleSysMgmt = () => {
  if (expandCollapsedSidebar()) isSysMgmtOpen.value = true
  else isSysMgmtOpen.value = !isSysMgmtOpen.value
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
