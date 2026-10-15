import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import {
  api,
  createProjectGroup,
  deleteProjectGroup,
  fetchText,
  getProjectManagementOptions,
  listProjectGroups,
  moveProjectToGroup,
  updateProjectGroup,
} from '@/api/client'
import type {
  ProjectGroup,
  ProjectSummary,
  ReauthorizeProjectRequest,
  Scenario,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useUIStore } from '@/stores/ui'
import { formatRelativeTime, statusText } from '@/lib/format'
import { resolveProjectSummaryStatus } from './workspaceStatus'
import {
  getProjectRuntimes,
  projectRuntimeBatchKey,
  useRuntimeClock,
  runtimeSnapshotExpired,
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
  { label: '待启动', value: 'pending' },
  { label: '活跃探索', value: 'active' },
  { label: '暂停中 / 已暂停', value: 'paused' },
  { label: '已结束', value: 'stopped' },
  { label: '已完成', value: 'completed' },
]

export const projectMatchesSearch = (
  project: ProjectSummary,
  query: string,
  runtimeSubtitle: string,
  scenarioLabel: string,
  includeTenantAttribution = false,
): boolean => {
  const normalized = query.trim().toLowerCase()
  if (!normalized) return true
  const values = [
    project.title,
    project.id,
    runtimeSubtitle,
    scenarioLabel,
    project.group_name || '',
  ]
  if (includeTenantAttribution) {
    values.push(project.tenant_id, project.tenant_name, project.tenant_owner_login || '')
  }
  return values.some((value) => value.toLowerCase().includes(normalized))
}

export const useProjectsPage = () => {
  const router = useRouter()
  const route = useRoute()
  const authStore = useAuthStore()
  const uiStore = useUIStore()
  const queryClient = useQueryClient()

  const {
    data: projectsData,
    isLoading,
    isFetching,
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
  const { data: runtimeBatch, isError: runtimeError } = useQuery({
    queryKey: computed(() => [...projectRuntimeBatchKey, runtimeProjectIds.value]),
    queryFn: ({ signal }) => getProjectRuntimes(runtimeProjectIds.value, signal),
    enabled: computed(() => runtimeProjectIds.value.length > 0),
    refetchInterval: 8000,
    retry: false,
  })
  const runtimeNow = useRuntimeClock()
  const runtimeById = computed(
    () =>
      new Map((runtimeBatch.value?.items || []).map((runtime) => [runtime.project_id, runtime])),
  )

  const { data: scenariosData } = useQuery<Scenario[]>({
    queryKey: ['scenarios'],
    queryFn: () => api<Scenario[]>('GET', '/scenarios'),
    refetchInterval: 10000,
  })

  // 项目组由当前认证会话的 Tenant 隔离。
  const { data: groupsData, refetch: refetchGroups } = useQuery<ProjectGroup[]>({
    queryKey: ['project-groups'],
    queryFn: listProjectGroups,
    refetchInterval: 10000,
  })
  const groups = computed(() => groupsData.value || [])

  // 组过滤器：'all' = 全部，'ungrouped' = 未分组，其余为具体 group_id。
  // 由 URL query（?group=xxx）驱动——左侧导航栏点击分组时写入 query，此处单向同步。
  const selectedGroupId = ref<string>('all')
  watch(
    () => route.query.group,
    (groupQuery) => {
      const value = Array.isArray(groupQuery) ? groupQuery[0] : groupQuery
      // 无 query 视为全部；未知值原样保留（组尚未加载完成时可能短暂不存在）
      selectedGroupId.value = value ? String(value) : 'all'
    },
    { immediate: true },
  )

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
    // 计数基于当前分组过滤后的项目集，选中分组时只统计该组内的状态分布。
    let list = projects.value
    if (showTenantAttribution.value && selectedTenantId.value !== 'all') {
      list = list.filter((project) => project.tenant_id === selectedTenantId.value)
    }
    if (selectedGroupId.value === 'ungrouped') {
      list = list.filter((project) => !project.group_id)
    } else if (selectedGroupId.value !== 'all') {
      list = list.filter((project) => project.group_id === selectedGroupId.value)
    }
    if (val === 'all') return 0
    return list.filter((project) => project.status === val).length
  }

  const getScenarioLabel = (project: ProjectSummary): string => {
    return scenarioLabels.value.get(project.scenario_key) || project.scenario_key || '默认自治探索'
  }

  const projectRuntimeById = computed(
    () =>
      new Map(
        projects.value.map((project) => [
          project.id,
          resolveProjectSummaryStatus({
            project,
            runtime: runtimeById.value.get(project.id),
            runtimeUnavailable:
              runtimeError.value ||
              Boolean(
                runtimeById.value.get(project.id) &&
                runtimeSnapshotExpired(
                  runtimeById.value.get(project.id)!,
                  Math.max(runtimeNow.value, Date.now()),
                ),
              ),
          }),
        ]),
      ),
  )

  const getProjectRuntime = (project: ProjectSummary) => projectRuntimeById.value.get(project.id)!

  const getProjectSubtitle = (project: ProjectSummary): string => {
    const runtime = getProjectRuntime(project)
    return runtime.detail ? `${runtime.label} · ${runtime.detail}` : runtime.label
  }

  const filteredProjects = computed(() => {
    let list = [...projects.value]

    if (showTenantAttribution.value && selectedTenantId.value !== 'all') {
      list = list.filter((project) => project.tenant_id === selectedTenantId.value)
    }

    // 组过滤：选定具体组只看该组；ungrouped 只看未分组；all 不限。
    if (selectedGroupId.value === 'ungrouped') {
      list = list.filter((project) => !project.group_id)
    } else if (selectedGroupId.value !== 'all') {
      list = list.filter((project) => project.group_id === selectedGroupId.value)
    }

    if (activeSegment.value !== 'all') {
      list = list.filter((project) => project.status === activeSegment.value)
    }

    if (searchQuery.value.trim()) {
      list = list.filter((project) =>
        projectMatchesSearch(
          project,
          searchQuery.value,
          getProjectSubtitle(project),
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

  const getStatusClass = (status: string) => {
    if (status === 'active') return 'status-active'
    if (status === 'stopped') return 'status-stopped'
    if (status === 'completed') return 'status-completed'
    return 'status-neutral'
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

  // 移动项目到分组：把已有项目（含未分组）加入或换到指定分组。
  const moveTarget = ref<ProjectSummary | null>(null)
  const moveTargetGroupId = ref<string>('')
  const moveGroups = ref<ProjectGroup[]>([])
  const moveSaving = ref(false)

  const openMoveDialog = async (project: ProjectSummary) => {
    openDropdownId.value = null
    try {
      const options = await getProjectManagementOptions(project.id)
      moveGroups.value = options.groups
      moveTarget.value = project
      // 现有项目的候选组必须来自目标项目 Tenant。
      moveTargetGroupId.value = project.group_id || ''
    } catch (err: any) {
      uiStore.showToast(err.message || '加载项目分组失败', 'danger')
    }
  }

  const saveMove = async () => {
    if (!moveTarget.value || moveSaving.value) return
    const id = moveTarget.value.id
    const target = moveTargetGroupId.value || null
    moveSaving.value = true

    try {
      await moveProjectToGroup(id, target)
      moveTarget.value = null
      uiStore.showToast('项目已移动到新分组', 'success')
      queryClient.invalidateQueries({ queryKey: ['projects'] })
    } catch (err: any) {
      uiStore.showToast(err.message || '移动分组失败', 'danger')
    } finally {
      moveSaving.value = false
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

  // 项目组 CRUD 的归属由服务端认证主体决定。
  const getGroupName = (project: ProjectSummary): string => {
    if (!project.group_id) return '未分组'
    return project.group_name || project.group_id
  }

  // 侧栏分组计数：基于未过滤的全量项目集，'all' 为总数，'ungrouped' 为未分组数。
  const getGroupCount = (groupId: string): number => {
    const list = projects.value
    if (groupId === 'all') return list.length
    if (groupId === 'ungrouped') return list.filter((p) => !p.group_id).length
    return list.filter((p) => p.group_id === groupId).length
  }

  const createGroup = async (name: string) => {
    const trimmed = name.trim()
    if (!trimmed) return
    try {
      await createProjectGroup({ name: trimmed })
      uiStore.showToast('项目组已创建', 'success')
      await refetchGroups()
    } catch (err: any) {
      uiStore.showToast(err.message || '创建项目组失败', 'danger')
    }
  }

  const renameGroup = async (groupId: string, name: string) => {
    const trimmed = name.trim()
    if (!trimmed) return
    try {
      await updateProjectGroup(groupId, { name: trimmed })
      uiStore.showToast('项目组已重命名', 'success')
      await refetchGroups()
    } catch (err: any) {
      uiStore.showToast(err.message || '重命名项目组失败', 'danger')
    }
  }

  const removeGroup = async (groupId: string) => {
    try {
      await deleteProjectGroup(groupId)
      if (selectedGroupId.value === groupId) selectedGroupId.value = 'all'
      uiStore.showToast('项目组已删除，组内项目已转为未分组', 'info')
      await refetchGroups()
      queryClient.invalidateQueries({ queryKey: ['projects'] })
    } catch (err: any) {
      uiStore.showToast(err.message || '删除项目组失败', 'danger')
    }
  }

  watch(
    () => uiStore.globalRefreshKey,
    () => {
      refetch()
    },
  )

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
    createGroup,
    deleteSaving,
    deleteTarget,
    exportProjectYaml,
    filteredProjects,
    focusedIndex,
    formatRelativeTime,
    getGroupName,
    getGroupCount,
    getProjectRuntime,
    runtimeById,
    getProjectSubtitle,
    getScenarioLabel,
    getSegmentCount,
    getStatusClass,
    groups,
    hasProjectsSnapshot,
    hasStaleProjectsError,
    isError,
    isFetching,
    isLoading,
    listElement,
    moveFocus,
    moveGroups,
    moveSaving,
    moveTarget,
    moveTargetGroupId,
    navigateToWorkspace,
    openDropdownId,
    openMoveDialog,
    openRenameDialog,
    refetch,
    removeGroup,
    renameGroup,
    renameNewTitle,
    renameSaving,
    renameTarget,
    saveDelete,
    saveMove,
    saveRename,
    searchQuery,
    segments,
    showTenantAttribution,
    selectedGroupId,
    selectedTenantId,
    sortBy,
    tenantOptions,
    statusText,
  }
}
