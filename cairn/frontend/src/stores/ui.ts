import { defineStore } from 'pinia'
import { ref } from 'vue'

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

  // Global Refresh Counter
  const globalRefreshKey = ref(0)
  const triggerGlobalRefresh = () => {
    globalRefreshKey.value++
  }

  // Toasts
  const toasts = ref<ToastMessage[]>([])
  const toastTimeouts = new Map<string, ReturnType<typeof setTimeout>>()
  const removeToast = (id: string) => {
    const timeout = toastTimeouts.get(id)
    if (timeout !== undefined) {
      clearTimeout(timeout)
      toastTimeouts.delete(id)
    }
    toasts.value = toasts.value.filter((t) => t.id !== id)
  }
  const showToast = (text: string, type: ToastMessage['type'] = 'info', duration = 3000) => {
    const id = Math.random().toString(36).substring(2, 9)
    toasts.value.push({ id, text, type, duration })
    const timeout = setTimeout(() => {
      if (toastTimeouts.get(id) !== timeout) return
      toastTimeouts.delete(id)
      removeToast(id)
    }, duration)
    toastTimeouts.set(id, timeout)
  }

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
    theme,
    setTheme,
    toggleTheme,
    globalRefreshKey,
    triggerGlobalRefresh,
    toasts,
    showToast,
    removeToast,
    isCommandPaletteOpen,
    openCommandPalette,
    closeCommandPalette,
  }
})
