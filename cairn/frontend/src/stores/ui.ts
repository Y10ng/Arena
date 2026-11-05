import { defineStore } from 'pinia'
import { onScopeDispose, ref } from 'vue'

export interface ToastMessage {
  id: string
  type: 'info' | 'success' | 'warn' | 'danger'
  text: string
  duration?: number
}

export type ThemeMode = 'dark' | 'light'

const THEME_STORAGE_KEY = 'cairn.theme'

/** 读取已持久化的主题，缺失或非法值兜底为浅色。 */
function readPersistedTheme(): ThemeMode {
  return localStorage.getItem(THEME_STORAGE_KEY) === 'dark' ? 'dark' : 'light'
}

/** 把主题写到 <html data-theme> 并持久化，供 tokens.css 与 Monaco 联动消费。 */
function applyTheme(mode: ThemeMode): void {
  document.documentElement.setAttribute('data-theme', mode)
  localStorage.setItem(THEME_STORAGE_KEY, mode)
}

export const useUIStore = defineStore('ui', () => {
  // Sidebar state
  const isSidebarCollapsed = ref(localStorage.getItem('cairn.sidebarCollapsed') === 'true')
  const toggleSidebar = () => {
    isSidebarCollapsed.value = !isSidebarCollapsed.value
    localStorage.setItem('cairn.sidebarCollapsed', String(isSidebarCollapsed.value))
  }

  // Theme state：index.html 内联脚本已在首屏前先应用一次，这里再同步保证 store 与 DOM 一致。
  const theme = ref<ThemeMode>(readPersistedTheme())
  applyTheme(theme.value)
  const setTheme = (mode: ThemeMode) => {
    theme.value = mode
    applyTheme(mode)
  }
  const toggleTheme = () => setTheme(theme.value === 'dark' ? 'light' : 'dark')

  // 列表阅读状态仅留在当前标签页内存中，不持久化搜索内容。
  const projectListView = ref({
    authGeneration: -1,
    activeSegment: 'all',
    searchQuery: '',
    selectedTenantId: 'all',
    sortBy: 'created_desc',
    scrollTop: 0,
    scrollLeft: 0,
  })

  // Global Refresh Counter
  const globalRefreshKey = ref(0)
  const triggerGlobalRefresh = () => {
    globalRefreshKey.value++
  }

  // 悬停与键盘焦点独立暂停同一计时器，离开后继续剩余阅读时间。
  const toasts = ref<ToastMessage[]>([])
  const toastTimers = new Map<string, {
    timeout?: ReturnType<typeof setTimeout>
    remaining: number
    startedAt: number
    pauses: Set<'hover' | 'focus'>
  }>()
  const removeToast = (id: string) => {
    clearTimeout(toastTimers.get(id)?.timeout)
    toastTimers.delete(id)
    toasts.value = toasts.value.filter((t) => t.id !== id)
  }
  const startToastTimer = (id: string) => {
    const timer = toastTimers.get(id)
    if (!timer || timer.pauses.size) return
    timer.startedAt = Date.now()
    timer.timeout = setTimeout(() => {
      if (toastTimers.get(id) === timer) removeToast(id)
    }, timer.remaining)
  }
  const pauseToast = (id: string, reason: 'hover' | 'focus') => {
    const timer = toastTimers.get(id)
    if (!timer) return
    if (!timer.pauses.size) {
      clearTimeout(timer.timeout)
      timer.remaining = Math.max(0, timer.remaining - (Date.now() - timer.startedAt))
    }
    timer.pauses.add(reason)
  }
  const resumeToast = (id: string, reason: 'hover' | 'focus') => {
    const timer = toastTimers.get(id)
    if (timer?.pauses.delete(reason) && !timer.pauses.size) startToastTimer(id)
  }
  const showToast = (text: string, type: ToastMessage['type'] = 'info', duration = 3000) => {
    const id = Math.random().toString(36).substring(2, 9)
    toasts.value.push({ id, text, type, duration })
    toastTimers.set(id, { remaining: duration, startedAt: 0, pauses: new Set() })
    startToastTimer(id)
  }
  onScopeDispose(() => {
    for (const timer of toastTimers.values()) clearTimeout(timer.timeout)
    toastTimers.clear()
  })

  // Command Palette
  const isCommandPaletteOpen = ref(false)
  const openCommandPalette = () => {
    isCommandPaletteOpen.value = true
  }
  const closeCommandPalette = () => {
    isCommandPaletteOpen.value = false
  }

  return {
    isSidebarCollapsed,
    toggleSidebar,
    projectListView,
    theme,
    setTheme,
    toggleTheme,
    globalRefreshKey,
    triggerGlobalRefresh,
    toasts,
    showToast,
    removeToast,
    pauseToast,
    resumeToast,
    isCommandPaletteOpen,
    openCommandPalette,
    closeCommandPalette,
  }
})
