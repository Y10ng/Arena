import { computed, nextTick, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { api, fetchText } from '@/api/client'
import type {
  ProjectSummary,
  ReauthorizeProjectRequest,
  Scenario,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useUIStore } from '@/stores/ui'
import { formatRelativeTime } from '@/lib/format'
import { resolveProjectStatus } from './workspaceStatus'
import {
  getProjectRuntimes,
  projectRuntimeBatchKey,
  projectActionAllowed,
  useProjectLifecycle,
} from './projectRuntime'
import type { ProjectLifecycleAction } from '@/api/types'

type DirectProjectLifecycleAction = Exclude<ProjectLifecycleAction, 'reauthorize'>

type ReauthorizationConfirmation = {
  project: ProjectSummary
  payload: ReauthorizeProjectRequest
}

const segments = [
  { label: '全部项目', value: 'all' },
  ...['pending', 'active', 'paused', 'stopped', 'completed'].map((value) => ({
    value,
    label: resolveProjectStatus(value).label,
  })),
]

export const projectMatchesSearch = (
  project: ProjectSummary,
  query: string,
  scenarioLabel: string,
  includeTenantAttribution = false,
): boolean => {
  const normalized = query.trim().toLowerCase()
  if (!normalized) return true
  const values = [
    project.title,
    project.id,
    resolveProjectStatus(project.status).label,
    scenarioLabel,
  ]
  if (includeTenantAttribution) {
    values.push(project.tenant_id, project.tenant_name, project.tenant_owner_login || '')
  }
  return values.some((value) => value.toLowerCase().includes(normalized))
}

export const useProjectsPage = () => {
  const router = useRouter()
  const authStore = useAuthStore()
  const uiStore = useUIStore()
  const queryClient = useQueryClient()

  const {
    data: projectsData,
    isLoading,
    isError,
    refetch,
  } = useQuery<ProjectSummary[]>({
    queryKey: ['projects'],
    queryFn: () => api<ProjectSummary[]>('GET', '/projects'),
    refetchInterval: 6000,
  })

  const runtimeProjectIds = computed(() =>
    (projectsData.value || []).map((project) => project.id).sort(),
  )
  const {
    data: runtimeBatch,
    isError: runtimeError,
    isFetching: runtimeFetching,
    refetch: refetchRuntime,
  } = useQuery({
    queryKey: computed(() => [...projectRuntimeBatchKey, runtimeProjectIds.value] as const),
    queryFn: ({ queryKey, signal }) => getProjectRuntimes([...queryKey[1]], signal),
    enabled: computed(() => runtimeProjectIds.value.length > 0),
    refetchInterval: 8000,
    retry: false,
  })
  const runtimeById = computed(
    () =>
      new Map((runtimeBatch.value?.items || []).map((runtime) => [runtime.project_id, runtime])),
  )

  const { data: scenariosData } = useQuery<Scenario[]>({
    queryKey: ['scenarios'],
    queryFn: () => api<Scenario[]>('GET', '/scenarios'),
    refetchInterval: 10000,
  })

  const projects = computed(() => projectsData.value || [])
  const hasProjectsSnapshot = computed(() => projectsData.value !== undefined)
  const hasStaleProjectsError = computed(() => isError.value && hasProjectsSnapshot.value)
  const showTenantAttribution = computed(() => authStore.isSuperAdmin)
  const tenantOptions = computed(() => {
    if (!showTenantAttribution.value) return []
    const tenants = new Map<string, string>()
    for (const project of projects.value) tenants.set(project.tenant_id, project.tenant_name)
    return [...tenants.entries()]
      .map(([id, name]) => ({ id, name }))
      .sort((left, right) => left.name.localeCompare(right.name) || left.id.localeCompare(right.id))
  })
  const scenarioLabels = computed(() => {
    return new Map((scenariosData.value || []).map((scenario) => [scenario.key, scenario.label]))
  })

  const activeSegment = ref('all')
  const searchQuery = ref('')
  const selectedTenantId = ref('all')
  const sortBy = ref('created_desc')
  const openDropdownId = ref<string | null>(null)
  const focusedIndex = ref(-1)
  const listElement = ref<HTMLElement | null>(null)
  const renameTarget = ref<ProjectSummary | null>(null)
  const renameNewTitle = ref('')
  const renameSaving = ref(false)
  const deleteTarget = ref<ProjectSummary | null>(null)
  const deleteSaving = ref(false)

  const getSegmentCount = (val: string) => {
    let list = projects.value
    if (showTenantAttribution.value && selectedTenantId.value !== 'all') {
      list = list.filter((project) => project.tenant_id === selectedTenantId.value)
    }
    if (val === 'all') return 0
    return list.filter((project) => project.status === val).length
  }

  const getScenarioLabel = (project: ProjectSummary): string => {
    return scenarioLabels.value.get(project.scenario_key) || project.scenario_key || '默认自治探索'
  }

  const retryingRefresh = ref(false)
  const retryRefresh = async () => {
    if (retryingRefresh.value) return
    retryingRefresh.value = true
    const previousIds = runtimeProjectIds.value.join(',')
    try {
      await refetch()
      // ID 变化会自动获取新 query；只合并其在途请求，不在它已完成后再取一次。
      await nextTick()
      if (
        runtimeProjectIds.value.length &&
        (previousIds === runtimeProjectIds.value.join(',') || runtimeFetching.value)
      ) await refetchRuntime({ cancelRefetch: false })
    } finally {
      retryingRefresh.value = false
    }
  }
  const getProjectStatus = (project: ProjectSummary) => resolveProjectStatus(project.status)

  const filteredProjects = computed(() => {
    let list = [...projects.value]

    if (showTenantAttribution.value && selectedTenantId.value !== 'all') {
      list = list.filter((project) => project.tenant_id === selectedTenantId.value)
    }

    if (activeSegment.value !== 'all') {
      list = list.filter((project) => project.status === activeSegment.value)
    }

    if (searchQuery.value.trim()) {
      list = list.filter((project) =>
        projectMatchesSearch(
          project,
          searchQuery.value,
          getScenarioLabel(project),
          showTenantAttribution.value,
        ),
      )
    }

    list.sort((a, b) => {
      if (sortBy.value === 'created_desc') {
        return b.created_at.localeCompare(a.created_at)
      }
      if (sortBy.value === 'created_asc') {
        return a.created_at.localeCompare(b.created_at)
      }
      if (sortBy.value === 'facts_desc') {
        return b.fact_count - a.fact_count
      }
      if (sortBy.value === 'intents_desc') {
        const actA = a.working_intent_count + a.unclaimed_intent_count
        const actB = b.working_intent_count + b.unclaimed_intent_count
        return actB - actA
      }
      return 0
    })

    return list
  })

  watch(filteredProjects, () => {
    focusedIndex.value = -1
  })

  const navigateToWorkspace = (id: string) => {
    router.push(`/projects/${id}`)
  }

  const { pendingProjectId, actionError, perform } = useProjectLifecycle(
    (id) => runtimeById.value.get(id),
    () => runtimeError.value,
    () => queryClient.invalidateQueries({ queryKey: ['projects'] }),
    (id) => projects.value.find((project) => project.id === id),
  )
  const finishTarget = ref<ProjectSummary | null>(null)
  const reauthorizationTarget = ref<ReauthorizationConfirmation | null>(null)
  const reauthorizationConfirmed = ref(false)
  const canPerform = (id: string, action: ProjectLifecycleAction) =>
    !pendingProjectId.value &&
    projectActionAllowed(
      runtimeById.value.get(id),
      action,
      runtimeError.value,
      projects.value.find((project) => project.id === id),
    )
  const changeStatus = async (id: string, action: DirectProjectLifecycleAction) => {
    openDropdownId.value = null
    if (await perform(id, action)) finishTarget.value = null
  }
  const confirmFinish = (project: ProjectSummary) => {
    if (!canPerform(project.id, 'finish')) return
    openDropdownId.value = null
    finishTarget.value = project
  }
  const saveFinish = () => {
    if (finishTarget.value) void changeStatus(finishTarget.value.id, 'finish')
  }
  const confirmReauthorization = (project: ProjectSummary) => {
    if (
      !canPerform(project.id, 'reauthorize') ||
      !Number.isInteger(project.authorization_epoch) ||
      !project.authorization_scope_digest
    )
      return
    openDropdownId.value = null
    reauthorizationConfirmed.value = false
    reauthorizationTarget.value = {
      project: { ...project },
      payload: {
        confirmed: true,
        expected_authorization_epoch: project.authorization_epoch!,
        expected_scope_digest: project.authorization_scope_digest,
      },
    }
  }
  const saveReauthorization = async () => {
    const confirmation = reauthorizationTarget.value
    if (!confirmation || !reauthorizationConfirmed.value) return
    const current = projects.value.find((item) => item.id === confirmation.project.id)
    if (
      !current ||
      current.authorization_epoch !== confirmation.payload.expected_authorization_epoch ||
      current.authorization_scope_digest !== confirmation.payload.expected_scope_digest
    ) {
      actionError.value = '项目授权版本或范围已变化，请关闭对话框并重新确认。'
      return
    }
    if (await perform(current.id, 'reauthorize', confirmation.payload)) {
      reauthorizationTarget.value = null
      reauthorizationConfirmed.value = false
      uiStore.showToast('授权已更新，项目仍处于暂停状态', 'success')
    }
  }

  const openRenameDialog = (project: ProjectSummary) => {
    openDropdownId.value = null
    renameTarget.value = project
    renameNewTitle.value = project.title
  }

  const saveRename = async () => {
    if (!renameTarget.value || !renameNewTitle.value.trim() || renameSaving.value) return
    const id = renameTarget.value.id
    const title = renameNewTitle.value.trim()
    renameSaving.value = true

    try {
      await api('PUT', `/projects/${id}/title`, { title })
      renameTarget.value = null
      uiStore.showToast('项目已成功重命名', 'success')
      queryClient.invalidateQueries({ queryKey: ['projects'] })
    } catch (err: any) {
      uiStore.showToast(err.message || '重命名失败', 'danger')
    } finally {
      renameSaving.value = false
    }
  }

  const confirmDeleteProject = (project: ProjectSummary) => {
    openDropdownId.value = null
    deleteTarget.value = project
  }

  const saveDelete = async () => {
    if (!deleteTarget.value || deleteSaving.value) return
    const id = deleteTarget.value.id
    deleteSaving.value = true

    try {
      await api('DELETE', `/projects/${id}`)
      deleteTarget.value = null
      uiStore.showToast('项目已被成功彻底删除', 'success')
      queryClient.invalidateQueries({ queryKey: ['projects'] })
    } catch (err: any) {
      uiStore.showToast(err.message || '删除项目失败', 'danger')
    } finally {
      deleteSaving.value = false
    }
  }

  const exportProjectYaml = async (id: string, title: string) => {
    openDropdownId.value = null
    try {
      const text = await fetchText(`/projects/${id}/export?format=yaml`)

      const blob = new Blob([text], { type: 'text/yaml' })
      const link = document.createElement('a')
      link.href = URL.createObjectURL(blob)
      link.download = `${title || id}_export.yaml`
      link.click()
      URL.revokeObjectURL(link.href)

      uiStore.showToast('项目导出配置下载成功', 'success')
    } catch (e: any) {
      uiStore.showToast(`导出失败: ${e.message}`, 'danger')
    }
  }

  const moveFocus = (direction: number) => {
    const nextIdx = focusedIndex.value + direction
    if (nextIdx >= 0 && nextIdx < filteredProjects.value.length) {
      focusedIndex.value = nextIdx
      const rows = listElement.value?.querySelectorAll('.project-row-main')
      if (rows && rows[nextIdx]) {
        ;(rows[nextIdx] as HTMLElement).focus()
      }
    }
  }

  return {
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
    getProjectStatus,
    runtimeById,
    getScenarioLabel,
    getSegmentCount,
    hasProjectsSnapshot,
    hasStaleProjectsError,
    isError,
    isLoading,
    retryRefresh,
    retryingRefresh,
    listElement,
    moveFocus,
    navigateToWorkspace,
    openDropdownId,
    openRenameDialog,
    refetch,
    renameNewTitle,
    renameSaving,
    renameTarget,
    saveDelete,
    saveRename,
    searchQuery,
    segments,
    showTenantAttribution,
    selectedTenantId,
    sortBy,
    tenantOptions,
  }
}
