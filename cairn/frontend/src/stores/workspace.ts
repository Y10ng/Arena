import { defineStore } from 'pinia'
import { ref } from 'vue'
import { ApiError, getProjectDetail } from '@/api/client'
import type { Hint, ProjectDetail, Target } from '@/api/types'

export const useWorkspaceStore = defineStore('workspace', () => {
  const currentProjectId = ref<string | null>(null)
  const projectDetail = ref<ProjectDetail | null>(null)
  const isLoading = ref(false)
  const error = ref<string | null>(null)
  let requestGeneration = 0

  const loadProject = async (id: string) => {
    const generation = ++requestGeneration
    const projectChanged = currentProjectId.value !== id
    currentProjectId.value = id
    if (projectChanged) {
      projectDetail.value = null
      error.value = null
    }
    isLoading.value = true
    try {
      const detail = await getProjectDetail(id)
      if (generation === requestGeneration) {
        projectDetail.value = detail
        error.value = null
      }
    } catch (err: any) {
      if (generation !== requestGeneration) return
      error.value = err.message || '加载项目失败'
      const terminalReadFailure = err instanceof ApiError && [401, 403, 404].includes(err.status)
      if (projectChanged || terminalReadFailure) projectDetail.value = null
    } finally {
      if (generation === requestGeneration) isLoading.value = false
    }
  }

  const replaceProjectTargets = (projectId: string, targets: readonly Target[]) => {
    if (currentProjectId.value !== projectId || projectDetail.value?.project.id !== projectId)
      return

    projectDetail.value = {
      ...projectDetail.value,
      targets: [...targets],
    }
  }

  const replaceProjectHints = (projectId: string, hints: readonly Hint[]) => {
    if (currentProjectId.value !== projectId || projectDetail.value?.project.id !== projectId)
      return

    projectDetail.value = {
      ...projectDetail.value,
      hints: [...hints],
    }
  }

  const clearProject = () => {
    requestGeneration += 1
    currentProjectId.value = null
    projectDetail.value = null
    isLoading.value = false
    error.value = null
  }

  return {
    currentProjectId,
    projectDetail,
    isLoading,
    error,
    loadProject,
    replaceProjectTargets,
    replaceProjectHints,
    clearProject,
  }
})
