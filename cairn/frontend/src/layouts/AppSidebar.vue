<template>
  <aside
    class="sidebar"
    :class="{ 'sidebar-collapsed': uiStore.isSidebarCollapsed }"
    aria-label="主导航"
  >
    <div class="brand">
      <RouterLink :to="{ name: 'Projects' }" class="brand-link" aria-label="Arena 项目">
        <span class="brand-logo-mark" aria-hidden="true">A</span
        ><span class="brand-text">Arena</span>
      </RouterLink>
      <IconButton class="mobile-sidebar-close" label="关闭主导航" size="sm" @click="closeSidebar"
        ><X :size="16"
      /></IconButton>
    </div>
    <button
      class="sidebar-command nav-item"
      type="button"
      aria-label="搜索与命令"
      title="搜索与命令 (Ctrl+K)"
      @click="emit('command')"
    >
      <Search :size="16" /><span class="label">搜索与命令</span
      ><kbd v-if="!uiStore.isSidebarCollapsed">⌃ K</kbd>
    </button>
    <nav class="nav-links" aria-label="应用导航">
      <section
        v-for="group in workGroups"
        :key="group.placement"
        class="nav-section"
        :aria-label="group.label"
      >
        <div v-if="group.items.length" class="nav-section-label">{{ group.label }}</div>
        <RouterLink
          v-for="item in group.items"
          :key="item.routeName"
          :to="{ name: item.routeName }"
          class="nav-item"
          active-class=""
          exact-active-class=""
          :class="{ active: isActive(item.routeName) }"
          :aria-current="isActive(item.routeName) ? 'page' : undefined"
          :title="item.label"
          :aria-label="item.label"
          @click="closeSidebarAfterNavigation"
        >
          <component :is="iconMap[item.icon]" :size="18" aria-hidden="true" /><span class="label">{{
            item.label
          }}</span>
        </RouterLink>
      </section>
    </nav>
    <div class="sidebar-bottom">
      <Popover v-model:open="settingsOpen" side="top" align="start" size="md">
        <template #trigger>
          <button
            type="button"
            class="nav-item nav-group-header"
            :class="{ active: systemItems.some((item) => isActive(item.routeName)) }"
            title="设置"
            aria-label="设置"
          >
            <Settings :size="20" /><span class="label">设置</span>
            <ChevronUp v-if="!uiStore.isSidebarCollapsed" :size="18" class="arrow-icon" :class="{ 'is-open': settingsOpen }" />
          </button>
        </template>
        <nav id="sidebar-settings" class="settings-navigation" aria-label="设置页面">
          <RouterLink
            v-for="item in systemItems"
            :key="item.routeName"
            :to="{ name: item.routeName }"
            class="nav-item"
            active-class=""
            exact-active-class=""
            :class="{ active: isActive(item.routeName) }"
            :aria-current="isActive(item.routeName) ? 'page' : undefined"
            @click="closeSidebarAfterNavigation"
          >
            <component :is="iconMap[item.icon]" :size="20" aria-hidden="true" />
            <span>{{ item.label }}</span>
          </RouterLink>
        </nav>
      </Popover>
      <slot name="utilities" />
    </div>
  </aside>
</template>

<script setup lang="ts">
import {
  AlertTriangle,
  Box,
  CalendarDays,
  ChevronUp,
  CirclePlus,
  FileArchive,
  Folder,
  Gauge,
  GitBranch,
  KeyRound,
  Layers3,
  Lightbulb,
  RadioTower,
  ScrollText,
  Search,
  Settings,
  ShieldAlert,
  Users,
  X,
  type LucideIcon,
} from '@lucide/vue'
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { IconButton, Popover } from '@/components/ui'
import { useAuthStore } from '@/stores/auth'
import { useUIStore } from '@/stores/ui'
import {
  sidebarNavigation,
  type NavigationIcon,
  type ProductionRouteName,
} from '@/router/navigationRegistry'

const emit = defineEmits<{ command: [] }>()
const route = useRoute()
const authStore = useAuthStore()
const uiStore = useUIStore()
const workGroups = computed(() =>
  [
    { placement: 'primary' as const, label: '工作' },
    { placement: 'results' as const, label: '成果' },
    { placement: 'support' as const, label: '研究支持' },
  ].map((group) => ({
    ...group,
    items: sidebarNavigation(group.placement, authStore.isSuperAdmin),
  })),
)
const systemItems = computed(() => sidebarNavigation('system', authStore.isSuperAdmin))
const iconMap: Record<NavigationIcon, LucideIcon> = {
  dashboard: Gauge,
  finding: AlertTriangle,
  projects: Folder,
  add: CirclePlus,
  graph: GitBranch,
  runs: ScrollText,
  hints: Lightbulb,
  artifacts: FileArchive,
  vulnerabilities: ShieldAlert,
  monitoring: RadioTower,
  workers: Box,
  users: Users,
  scenarios: Layers3,
  prompts: ScrollText,
  knowledge: FileArchive,
  status: ShieldAlert,
  auth: KeyRound,
  schedules: CalendarDays,
}
function isActive(name: ProductionRouteName) {
  if (name === 'ProjectSchedules')
    return (
      route.name === name ||
      route.name === 'ProjectSchedule' ||
      (route.name === 'Projects' && route.query.view === 'schedules')
    )
  if (name === 'Projects')
    return route.path.startsWith('/projects') && route.query.view !== 'schedules'
  if (name === 'Verifications') return route.path.startsWith('/verifications')
  return route.name === name
}
const settingsOpen = ref(false)
watch(() => [route.name, route.fullPath], () => { settingsOpen.value = false })
const closeSidebar = () => {
  if (!uiStore.isSidebarCollapsed) uiStore.toggleSidebar()
}
const closeSidebarAfterNavigation = () => {
  settingsOpen.value = false
  if (typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 760px)').matches)
    closeSidebar()
}
</script>

<style src="./FrontLayout-shell.css" scoped></style>
