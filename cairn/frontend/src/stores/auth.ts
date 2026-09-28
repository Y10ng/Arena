import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { changePassword, getSession, loginSession, logoutSession, refreshCsrfToken } from '@/api/auth'
import type { AuthSession } from '@/api/auth'
import { getCsrfToken, setCsrfToken } from '@/api/client'
import { queryClient } from '@/lib/queryClient'
import { useWorkspaceStore } from '@/stores/workspace'

export const useAuthStore = defineStore('auth', () => {
  const session = ref<AuthSession | null>(null)
  const initialized = ref(false)
  const authGeneration = ref(0)
  let initialization: Promise<boolean> | null = null

  const isAuthenticated = computed(() => session.value !== null)
  const isSuperAdmin = computed(() => session.value?.role === 'super_admin')
  const mustChangePassword = computed(() => session.value?.must_change_password === true)
  const displayName = computed(() => session.value?.user_id || '')

  const clearSession = () => {
    authGeneration.value += 1
    session.value = null
    setCsrfToken(null)
    queryClient.clear()
    useWorkspaceStore().clearProject()
  }

  const clearSessionIfCurrent = (requestGeneration: number) => {
    if (requestGeneration !== authGeneration.value) return false
    clearSession()
    return true
  }

  const initialize = async () => {
    if (initialized.value) return isAuthenticated.value
    if (initialization) return initialization
    const initializationGeneration = authGeneration.value
    initialization = (async () => {
      try {
        let restored = await getSession()
        // 标签页优先复用同源共享的最新 CSRF token；缺失时才通过可信 Origin 轮换。
        if (!getCsrfToken()) {
          restored = await refreshCsrfToken()
        }
        if (initializationGeneration === authGeneration.value) {
          setCsrfToken(restored.csrf_token || getCsrfToken())
          session.value = restored
        }
      } catch {
        clearSessionIfCurrent(initializationGeneration)
      } finally {
        if (initializationGeneration === authGeneration.value) initialized.value = true
      }
      return isAuthenticated.value
    })()
    try {
      return await initialization
    } finally {
      initialization = null
    }
  }

  const login = async (login: string, password: string) => {
    const nextSession = await loginSession(login, password)
    authGeneration.value += 1
    setCsrfToken(nextSession.csrf_token || null)
    session.value = nextSession
    initialized.value = true
    queryClient.clear()
    useWorkspaceStore().clearProject()
  }

  const updatePassword = async (currentPassword: string, newPassword: string) => {
    const nextSession = await changePassword({
      current_password: currentPassword,
      new_password: newPassword,
    })
    authGeneration.value += 1
    setCsrfToken(nextSession.csrf_token || getCsrfToken())
    session.value = nextSession
    initialized.value = true
    queryClient.clear()
    useWorkspaceStore().clearProject()
  }

  const logout = async () => {
    try {
      await logoutSession()
    } finally {
      clearSession()
      initialized.value = true
    }
  }

  return {
    session,
    initialized,
    authGeneration,
    isAuthenticated,
    isSuperAdmin,
    mustChangePassword,
    displayName,
    initialize,
    login,
    updatePassword,
    logout,
    clearSession,
    clearSessionIfCurrent,
  }
})
