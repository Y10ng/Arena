<template>
  <div
    class="app-layout"
    :class="{
      'sidebar-collapsed': uiStore.isSidebarCollapsed,
      'app-layout--agent': isAgentWorkbench,
    }"
  >
    <a class="skip-link" href="#main-content">跳到主内容</a>
    <AppSidebar @command="openCommandPalette">
      <template #utilities>
        <div class="sidebar-utilities">
          <button
            class="btn-icon sidebar-toggle"
            type="button"
            :aria-label="uiStore.isSidebarCollapsed ? '展开侧栏' : '折叠侧栏'"
            :title="uiStore.isSidebarCollapsed ? '展开侧栏 ([)' : '折叠侧栏 ([)'"
            @click="uiStore.toggleSidebar"
          ><PanelLeft :size="18" /></button>
          <button
            class="btn-icon"
            type="button"
            aria-label="刷新页面数据"
            title="刷新页面数据"
            :class="{ spinning: isRefreshing }"
            :disabled="isRefreshing"
            :aria-busy="isRefreshing"
            @click="refreshData"
          >
            <RefreshCw :size="17" />
          </button>
          <button
            class="btn-icon"
            type="button"
            :aria-label="uiStore.theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'"
            :title="uiStore.theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'"
            @click="uiStore.toggleTheme"
          >
            <Sun v-if="uiStore.theme === 'dark'" :size="17" /><Moon v-else :size="17" />
          </button>
        </div>

        <div class="sidebar-tools">
          <Popover
            :open="isActorMenuOpen"
            align="start"
            side="top"
            size="sm"
            @update:open="isActorMenuOpen = $event"
          >
            <template #trigger>
              <button
                class="actor-trigger"
                type="button"
                :aria-expanded="isActorMenuOpen"
                :aria-label="`账户：${authStore.displayName}`"
                :title="`账户：${authStore.displayName}`"
              >
                <span class="actor-avatar"><UserRound :size="17" /></span>
                <span class="actor-name">{{ authStore.displayName }}</span>
                <ChevronsUpDown class="actor-chevron" :size="15" aria-hidden="true" />
              </button>
            </template>
            <section class="account-popover" aria-label="账户信息">
              <div class="account-identity">
                <strong>{{ authStore.displayName }}</strong>
                <span class="account-role">{{
                  authStore.isSuperAdmin ? '超级管理员' : '个人账户'
                }}</span>
              </div>
              <Button block variant="ghost" :loading="isLoggingOut" @click="handleLogout">
                <LogOut :size="16" />退出登录
              </Button>
            </section>
          </Popover>
        </div>
      </template>
    </AppSidebar>
    <div class="main-container">
      <header class="topbar" :class="{ 'topbar--navigation-only': !workspaceProjectId && breadcrumbs.length < 2 }">
        <button
          class="btn-icon mobile-nav-toggle"
          type="button"
          aria-label="展开主导航"
          @click="uiStore.toggleSidebar"
        >
          <PanelLeft :size="18" />
        </button>
        <div
          v-show="workspaceProjectId"
          id="workbench-project-header"
          class="workbench-project-header"
        />
        <nav v-if="!workspaceProjectId && breadcrumbs.length > 1" class="breadcrumbs" aria-label="面包屑">
          <ol>
            <li v-for="(crumb, idx) in breadcrumbs" :key="crumb.key">
              <ChevronRight v-if="idx > 0" class="separator" :size="14" aria-hidden="true" />
              <RouterLink v-if="crumb.to && !crumb.current" :to="crumb.to" class="crumb">{{
                crumb.text
              }}</RouterLink>
              <span v-else class="crumb" :aria-current="crumb.current ? 'page' : undefined">{{
                crumb.text
              }}</span>
            </li>
          </ol>
        </nav>
      </header>
      <main
        id="main-content"
        tabindex="-1"
        class="content-viewport"
        :class="{ 'content-viewport--workspace': usesWorkspaceViewport }"
      >
        <router-view />
      </main>
    </div>

    <Dialog
      :open="uiStore.isCommandPaletteOpen"
      title="命令面板"
      size="md"
      :show-close="true"
      @update:open="handleCommandPaletteOpenChange"
      @open-auto-focus="focusCommandPaletteInput"
      @close-auto-focus="restoreCommandPaletteFocus"
    >
      <div class="palette-content">
        <div class="palette-input-row">
          <Search :size="18" class="search-icon" aria-hidden="true" />
          <input
            id="command-palette-search"
            ref="paletteInput"
            v-model="paletteQuery"
            role="combobox"
            aria-label="搜索命令"
            aria-autocomplete="list"
            aria-controls="command-palette-results"
            :aria-expanded="uiStore.isCommandPaletteOpen"
            :aria-activedescendant="activePaletteOptionId || undefined"
            placeholder="搜索页面或当前项目功能…"
            @keydown.down.prevent="focusNextItem"
            @keydown.up.prevent="focusPrevItem"
            @keydown.enter.prevent="executeSelectedItem"
          />
        </div>
        <div id="command-palette-results" class="palette-results" role="listbox">
          <button
            v-for="(item, idx) in filteredCommands"
            :id="`command-palette-option-${idx}`"
            :key="item.id"
            type="button"
            role="option"
            class="palette-item"
            :class="{ active: idx === selectedPaletteIdx }"
            :aria-selected="idx === selectedPaletteIdx"
            tabindex="-1"
            @pointermove="selectedPaletteIdx = idx"
            @click="runPaletteItem(item)"
          >
            <span class="item-kind">{{ item.kindLabel }}</span>
            <div class="item-text">
              <span class="item-title">{{ item.title }}</span>
            </div>
            <CornerDownLeft
              v-if="idx === selectedPaletteIdx"
              :size="16"
              class="item-enter"
              aria-hidden="true"
            />
          </button>
          <div v-if="filteredCommands.length === 0" class="palette-empty" role="status">
            没有找到匹配项
          </div>
        </div>
      </div>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import {
  ChevronRight,
  ChevronsUpDown,
  CornerDownLeft,
  LogOut,
  Moon,
  PanelLeft,
  RefreshCw,
  Search,
  Sun,
  UserRound,
} from '@lucide/vue'
import { useQueryClient } from '@tanstack/vue-query'
import { useUIStore } from '@/stores/ui'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'
import { filterCommandItems } from '@/lib/commandPalette'
import type { CommandItem } from '@/lib/commandPalette'
import AppSidebar from './AppSidebar.vue'
import { breadcrumbsFromRegistry, commandItemsFromRegistry } from '@/router/navigationRegistry'
import { Button, Dialog, Popover } from '@/components/ui'

const uiStore = useUIStore()
const authStore = useAuthStore()
const workspaceStore = useWorkspaceStore()
const router = useRouter()
const route = useRoute()
const queryClient = useQueryClient()
const isAgentWorkbench = computed(() => route.name === 'Agent')

const isRefreshing = ref(false)
const isActorMenuOpen = ref(false)
const isLoggingOut = ref(false)

// 账户菜单保留实际身份与退出入口，不暴露内部租户标识。
const closeActorMenu = () => {
  isActorMenuOpen.value = false
}
const handleLogout = async () => {
  if (isLoggingOut.value) return
  isLoggingOut.value = true
  closeActorMenu()
  try {
    await authStore.logout()
  } finally {
    isLoggingOut.value = false
    await router.replace({ name: 'Login' })
  }
}

// 非 Query 页面继续通过刷新事件更新；Query 请求失败必须如实反馈。
const refreshData = async () => {
  if (isRefreshing.value) return
  isRefreshing.value = true
  uiStore.triggerGlobalRefresh()
  try {
    await queryClient.invalidateQueries({}, { throwOnError: true })
  } catch {
    uiStore.showToast('刷新失败', 'danger')
  } finally {
    isRefreshing.value = false
  }
}

// 仅普通项目工作区的 id 可用于项目加载、命令和快捷键，复测记录不属于该上下文。
const workspaceProjectId = computed(() =>
  route.matched.some((record) => record.path === '/projects/:id') &&
  typeof route.params.id === 'string'
    ? route.params.id
    : undefined,
)

// 阅读与编辑工作区继承 Shell 的剩余高度，普通表单页继续使用外层滚动。
const usesWorkspaceViewport = computed(
  () =>
    Boolean(workspaceProjectId.value) ||
    ['Agent', 'Traffic', 'Reports', 'ConsoleKnowledge', 'ConsoleReportTemplates'].includes(
      String(route.name),
    ),
)

// 栏目由主导航说明；项目上下文传送到同一条定位栏，不叠加项目标题。
const breadcrumbs = computed(() =>
  breadcrumbsFromRegistry(
    route.name,
    workspaceProjectId.value,
    workspaceStore.projectDetail?.project?.title,
  ).filter((crumb) => crumb.key !== 'root' && crumb.key !== 'console'),
)

// 离开普通项目工作区时清除上下文，不为其他详情页发起项目查询。
watch(
  workspaceProjectId,
  (newVal) => {
    if (newVal) {
      workspaceStore.loadProject(newVal)
    } else {
      workspaceStore.clearProject()
    }
  },
  { immediate: true },
)

// Command Palette
const paletteQuery = ref('')
const paletteInput = ref<HTMLInputElement | null>(null)
const palettePreviousFocus = ref<HTMLElement | null>(null)
const selectedPaletteIdx = ref(0)

const baseCommands = computed<CommandItem[]>(() =>
  commandItemsFromRegistry(authStore.isSuperAdmin, workspaceProjectId.value).map((item) => ({
    ...item,
    action: () => router.push(item.to),
  })),
)

const filteredCommands = computed(() => {
  return filterCommandItems(baseCommands.value, paletteQuery.value, 10)
})
const activePaletteOptionId = computed(() =>
  filteredCommands.value[selectedPaletteIdx.value]
    ? `command-palette-option-${selectedPaletteIdx.value}`
    : '',
)

watch(activePaletteOptionId, (optionId) => {
  if (!optionId || !uiStore.isCommandPaletteOpen) return
  void nextTick(() => {
    document.getElementById(optionId)?.scrollIntoView({ block: 'nearest' })
  })
})

const openCommandPalette = () => {
  const activeElement = document.activeElement
  palettePreviousFocus.value = activeElement instanceof HTMLElement ? activeElement : null
  uiStore.openCommandPalette()
}

const handleCommandPaletteOpenChange = (open: boolean) => {
  if (open) uiStore.openCommandPalette()
  else uiStore.closeCommandPalette()
}

const focusCommandPaletteInput = (event: Event) => {
  event.preventDefault()
  void nextTick(() => paletteInput.value?.focus())
}

const restoreCommandPaletteFocus = (event: Event) => {
  event.preventDefault()
  const previousFocus = palettePreviousFocus.value
  palettePreviousFocus.value = null
  if (previousFocus?.isConnected) {
    void nextTick(() => previousFocus.focus())
  }
}

watch(
  () => uiStore.isCommandPaletteOpen,
  (open) => {
    if (open) {
      paletteQuery.value = ''
      selectedPaletteIdx.value = 0
    }
  },
)

watch(paletteQuery, () => {
  selectedPaletteIdx.value = 0
})

watch(
  () => filteredCommands.value.length,
  (length) => {
    selectedPaletteIdx.value = length === 0 ? 0 : Math.min(selectedPaletteIdx.value, length - 1)
  },
)

const focusNextItem = () => {
  if (filteredCommands.value.length === 0) return
  selectedPaletteIdx.value = (selectedPaletteIdx.value + 1) % filteredCommands.value.length
}
const focusPrevItem = () => {
  if (filteredCommands.value.length === 0) return
  selectedPaletteIdx.value =
    (selectedPaletteIdx.value - 1 + filteredCommands.value.length) % filteredCommands.value.length
}
const executeSelectedItem = () => {
  const selected = filteredCommands.value[selectedPaletteIdx.value]
  if (selected) {
    runPaletteItem(selected)
  }
}
const runPaletteItem = (item: CommandItem) => {
  if (item.action) {
    item.action()
  }
  uiStore.closeCommandPalette()
}

// 全局快捷键只导航到真实入口；输入与浮层内不接管单字快捷键。
let keyBuffer = ''
let keyTimeout: number | null = null

const handleGlobalKeydown = (e: KeyboardEvent) => {
  if (e.defaultPrevented || e.isComposing) return
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    if (uiStore.isCommandPaletteOpen) {
      uiStore.closeCommandPalette()
    } else {
      openCommandPalette()
    }
    return
  }

  const focused = document.activeElement
  if (
    e.ctrlKey ||
    e.metaKey ||
    e.altKey ||
    uiStore.isCommandPaletteOpen ||
    isActorMenuOpen.value ||
    (focused instanceof HTMLElement &&
      (focused.isContentEditable ||
        focused.closest('input, textarea, select, [role="dialog"], [role="menu"]')))
  ) {
    keyBuffer = ''
    return
  }

  if (e.key === '[') {
    e.preventDefault()
    uiStore.toggleSidebar()
    return
  }

  if (keyBuffer === 'g') {
    const key = e.key.toLowerCase()
    keyBuffer = ''
    if (keyTimeout) clearTimeout(keyTimeout)

    if (key === 'o') {
      router.push({ name: 'Overview' })
    } else if (key === 'p') {
      router.push('/projects')
    } else if (key === 'm' && authStore.isSuperAdmin) {
      router.push({ name: 'Monitoring' })
    } else if (key === 's') {
      router.push({ name: 'ConsoleWorkers', query: { section: 'models' } })
    } else if (workspaceProjectId.value) {
      const pId = workspaceProjectId.value
      if (key === 'g') {
        router.push({ name: 'WorkspaceGraph', params: { id: pId } })
      } else if (key === 'r') {
        router.push(`/projects/${pId}/runs`)
      } else if (key === 'v') {
        router.push(`/projects/${pId}/vulnerabilities`)
      } else if (key === 'h') {
        router.push(`/projects/${pId}/hints`)
      } else if (key === 'a') {
        router.push(`/projects/${pId}/artifacts`)
      } else if (key === 'u') {
        router.push(`/projects/${pId}/runs`)
      }
    }
    return
  }

  if (e.key === 'g') {
    keyBuffer = 'g'
    if (keyTimeout) clearTimeout(keyTimeout)
    keyTimeout = window.setTimeout(() => {
      keyBuffer = ''
    }, 1000)
  }
}

onMounted(() => {
  window.addEventListener('keydown', handleGlobalKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', handleGlobalKeydown)
  palettePreviousFocus.value = null
  if (keyTimeout) clearTimeout(keyTimeout)
})
</script>

<style src="./FrontLayout.css" scoped></style>
