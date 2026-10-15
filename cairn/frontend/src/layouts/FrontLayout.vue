<template>
  <div class="app-layout" :class="{ 'sidebar-collapsed': uiStore.isSidebarCollapsed }">
    <AppSidebar />

    <!-- Main Container -->
    <div class="main-container">
      <!-- TopBar -->
      <header class="topbar">
        <div class="left-section">
          <button class="collapse-btn" @click="uiStore.toggleSidebar" title="折叠/展开侧栏 ([)">
            <svg
              v-if="uiStore.isSidebarCollapsed"
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
              <line x1="9" y1="3" x2="9" y2="21"></line>
              <path d="M14 9l3 3-3 3"></path>
            </svg>
            <svg
              v-else
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
              <line x1="9" y1="3" x2="9" y2="21"></line>
              <path d="M15 15l-3-3 3-3"></path>
            </svg>
          </button>
          <nav class="breadcrumbs" aria-label="面包屑">
            <ol>
              <li v-for="(crumb, idx) in breadcrumbs" :key="crumb.key">
                <span v-if="idx > 0" class="separator" aria-hidden="true">/</span>
                <RouterLink
                  v-if="crumb.to && !crumb.current"
                  :to="crumb.to"
                  class="crumb clickable"
                >
                  {{ crumb.text }}
                </RouterLink>
                <span v-else class="crumb" :aria-current="crumb.current ? 'page' : undefined">
                  {{ crumb.text }}
                </span>
              </li>
            </ol>
          </nav>
        </div>

        <div class="right-section">
          <!-- Command Trigger -->
          <button class="btn-command" @click="openCommandPalette" title="命令面板 (⌘K)">
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <circle cx="11" cy="11" r="8"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
            <span>搜索...</span>
            <kbd>⌘K</kbd>
          </button>

          <!-- Refresh Button -->
          <button
            class="btn-icon"
            @click="refreshData"
            title="刷新页面数据"
            :class="{ spinning: isRefreshing }"
          >
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67" />
            </svg>
          </button>

          <button
            class="btn-icon"
            type="button"
            :aria-label="uiStore.theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'"
            :title="uiStore.theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'"
            @click="uiStore.toggleTheme"
          >
            <svg
              v-if="uiStore.theme === 'dark'"
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <circle cx="12" cy="12" r="4"></circle>
              <path d="M12 2v2"></path>
              <path d="M12 20v2"></path>
              <path d="m4.93 4.93 1.41 1.41"></path>
              <path d="m17.66 17.66 1.41 1.41"></path>
              <path d="M2 12h2"></path>
              <path d="M20 12h2"></path>
              <path d="m6.34 17.66-1.41 1.41"></path>
              <path d="m19.07 4.93-1.41 1.41"></path>
            </svg>
            <svg
              v-else
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"></path>
            </svg>
          </button>

          <!-- Authenticated profile -->
          <Popover
            :open="isActorMenuOpen"
            align="end"
            size="sm"
            @update:open="isActorMenuOpen = $event"
          >
            <template #trigger>
              <button
                class="actor-trigger"
                :aria-expanded="isActorMenuOpen"
                :title="`当前账户: ${authStore.displayName}`"
              >
                <span class="actor-avatar">{{
                  authStore.displayName.slice(0, 1).toUpperCase()
                }}</span>
                <span class="actor-name">{{ authStore.displayName }}</span>
              </button>
            </template>

            <section class="account-popover" aria-label="账户信息">
              <div class="account-role">
                {{ authStore.isSuperAdmin ? '超级管理员' : '个人账户' }}
              </div>
              <div class="account-tenant mono">Tenant {{ authStore.session?.tenant_id }}</div>
              <Button block variant="secondary" size="sm" @click="handleLogout">退出登录</Button>
            </section>
          </Popover>
        </div>
      </header>

      <!-- Viewport -->
      <main class="content-viewport">
        <router-view></router-view>
      </main>
    </div>

    <Dialog
      :open="uiStore.isCommandPaletteOpen"
      title="命令面板"
      description="搜索并执行导航命令、当前项目操作和系统管理功能。"
      size="md"
      :show-close="false"
      @update:open="handleCommandPaletteOpenChange"
      @open-auto-focus="focusCommandPaletteInput"
      @close-auto-focus="restoreCommandPaletteFocus"
    >
      <div class="palette-content">
        <div class="palette-input-row">
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            class="search-icon"
          >
            <circle cx="11" cy="11" r="8"></circle>
            <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
          </svg>
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
            placeholder="搜索功能、项目、运行..."
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
            @click="runPaletteItem(item)"
          >
            <span class="item-kind">{{ item.kindLabel }}</span>
            <div class="item-text">
              <span class="item-title">{{ item.title }}</span>
              <span class="item-subtitle" v-if="item.subtitle">{{ item.subtitle }}</span>
            </div>
            <span class="item-badge" v-if="item.badge">{{ item.badge }}</span>
          </button>
          <div v-if="filteredCommands.length === 0" class="palette-empty" role="status">
            没有找到匹配项
          </div>
        </div>
        <div class="palette-footer">
          <span>上下移动: ↑↓</span>
          <span>选择: Enter</span>
          <span>关闭: Esc</span>
        </div>
      </div>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
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

const isRefreshing = ref(false)
const isActorMenuOpen = ref(false)

// Authenticated profile menu
const closeActorMenu = () => {
  isActorMenuOpen.value = false
}
const handleLogout = async () => {
  closeActorMenu()
  try {
    await authStore.logout()
  } finally {
    await router.replace({ name: 'Login' })
  }
}

// Refresh Page Data
const refreshData = async () => {
  isRefreshing.value = true
  uiStore.triggerGlobalRefresh()
  try {
    await queryClient.invalidateQueries()
    uiStore.showToast('数据已刷新', 'success')
  } catch {
    uiStore.showToast('刷新失败', 'danger')
  } finally {
    isRefreshing.value = false
  }
}

// 仅普通项目工作区的 id 可用于项目加载、命令和快捷键，复测记录不属于该上下文。
const workspaceProjectId = computed(() =>
  route.matched.some((record) => record.path === '/projects/:id') && typeof route.params.id === 'string'
    ? route.params.id
    : undefined,
)

// Navigation / Breadcrumbs
const breadcrumbs = computed(() =>
  breadcrumbsFromRegistry(
    route.name,
    workspaceProjectId.value,
    workspaceStore.projectDetail?.project?.title,
  ),
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
  commandItemsFromRegistry(
    authStore.isSuperAdmin,
    workspaceProjectId.value,
  ).map((item) => ({
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

// Global Shortcuts
let keyBuffer = ''
let keyTimeout: number | null = null

const handleGlobalKeydown = (e: KeyboardEvent) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    if (uiStore.isCommandPaletteOpen) {
      uiStore.closeCommandPalette()
    } else {
      openCommandPalette()
    }
    return
  }

  if (
    e.key === '[' &&
    document.activeElement?.tagName !== 'INPUT' &&
    document.activeElement?.tagName !== 'TEXTAREA'
  ) {
    e.preventDefault()
    uiStore.toggleSidebar()
    return
  }

  if (
    document.activeElement?.tagName === 'INPUT' ||
    document.activeElement?.tagName === 'TEXTAREA'
  ) {
    return
  }

  if (keyBuffer === 'g') {
    const key = e.key.toLowerCase()
    keyBuffer = ''
    if (keyTimeout) clearTimeout(keyTimeout)

    if (key === 'o') {
      router.push('/')
    } else if (key === 'p') {
      router.push('/projects')
    } else if (key === 'n') {
      router.push('/projects/new')
    } else if (key === 'm') {
      router.push('/monitoring')
    } else if (key === 's') {
      router.push('/console/models')
    } else if (workspaceProjectId.value) {
      const pId = workspaceProjectId.value
      if (key === 'g') {
        router.push(`/projects/${pId}`)
      } else if (key === 'r') {
        router.push(`/projects/${pId}/runs`)
      } else if (key === 'v') {
        router.push(`/projects/${pId}/vulnerabilities`)
      } else if (key === 'h') {
        router.push(`/projects/${pId}/hints`)
      } else if (key === 'a') {
        router.push(`/projects/${pId}/artifacts`)
      } else if (key === 'u') {
        router.push(`/projects/${pId}/audit`)
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
})
</script>

<style src="./FrontLayout.css" scoped></style>

<!-- TEST_COMPATIBILITY_ASSERTIONS:
项目列表 任务运行审计 错误聚合 执行器选择 目标范围 终端视图 实时刷新 钩子审计摘要 已搁置 提示词审计摘要 钩子操作 累计标准错误 接收字节 字节数 拒绝原因 网络流 产物角色 接收 字节 最近网络流 网络流状态 流 ID 钩子风险 容器状态 运行配置档 运行隔离 容器镜像 容器镜像摘要 知识库 知识库模式 知识库格式 统一开发环境 工作空间 模块划分 审计链条 证据 产物 工具
-->
