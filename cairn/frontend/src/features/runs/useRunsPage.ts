import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { api, ApiError, type ArtifactPreview } from '@/api/client'
import type { Intent, ReportInventoryItem, ReportInventoryPage } from '@/api/types'
import {
  buildWorkTimeline,
  isActiveRun,
  pairToolCalls,
  resultsForRun,
  type WorkNode,
} from './runTimelineModel'
import { bytes, formatRelativeTime, statusText } from '@/lib/format'
import { useUIStore } from '@/stores/ui'
import { useWorkspaceStore } from '@/stores/workspace'
import {
  buildTerminalOutputs,
  isConcludeRecovery,
  runDurationText,
  runReferenceTime,
  selectRetainedTerminalOutput,
  type TerminalOutput,
} from './runTerminalModel'
import type {
  RunArtifact,
  TaskRun,
  TaskRunCommandEvent,
  TaskRunEventGroup,
  TaskRunHookEvent,
} from './runTypes'

const detailTabs = [
  { label: '终端视图', value: 'terminal' },
  { label: 'Shell命令', value: 'commands' },
  { label: '产物证据', value: 'artifacts' },
]

// 读取反馈只展示稳定状态，不把服务端异常正文或运行材料带入提示。
const readErrorMessage = (error: unknown): string => {
  if (error instanceof ApiError) return `HTTP ${error.status}`
  return '网络或响应读取异常'
}

const stopsAutomaticRead = (error: unknown): boolean =>
  error instanceof ApiError &&
  error.status >= 400 &&
  error.status < 500 &&
  error.status !== 408 &&
  error.status !== 429

const eventsForRun = <TEvent>(groups: TaskRunEventGroup<TEvent>[], runId: string): TEvent[] => {
  return groups.find((group) => group.task_run_id === runId)?.events || []
}

export const useRunsPage = () => {
  const route = useRoute()
  const router = useRouter()
  const workspaceStore = useWorkspaceStore()
  const uiStore = useUIStore()

  const searchQuery = ref('')
  const statusFilter = ref('all')
  const activeDetailTab = ref('terminal')
  const autoScrollTerminal = ref(true)
  const selectedRunId = ref<string | null>(null)
  const chatRunNotice = ref(false)
  const commandEvents = ref<TaskRunCommandEvent[]>([])
  const artifacts = ref<RunArtifact[]>([])
  const stdoutArtifactId = ref<string | null>(null)
  const terminalOutputs = ref<TerminalOutput[]>([])
  const terminalPreview = ref<ArtifactPreview | null>(null)
  const terminalPreviewSource = ref<{
    projectId: string
    runId: string
    artifactId: string
  } | null>(null)
  const terminalRecovered = ref(false)
  const terminalScreen = ref<HTMLElement | null>(null)
  const isLoadingCommands = ref(false)
  const commandsLoaded = ref(false)
  const commandsFailure = shallowRef<unknown>(null)
  const commandsError = computed(() =>
    commandsFailure.value ? `读取命令审计失败：${readErrorMessage(commandsFailure.value)}` : '',
  )
  const isLoadingArtifacts = ref(false)
  const artifactsLoaded = ref(false)
  const artifactsFailure = shallowRef<unknown>(null)
  const artifactsError = computed(() =>
    artifactsFailure.value ? `读取关联资产失败：${readErrorMessage(artifactsFailure.value)}` : '',
  )
  const isLoadingRunDetails = computed(
    () =>
      (isLoadingCommands.value && !commandsLoaded.value) ||
      (isLoadingArtifacts.value && !artifactsLoaded.value),
  )
  const isLoadingTerminalPreview = ref(false)
  const terminalPreviewFailure = shallowRef<unknown>(null)
  const terminalPreviewError = computed(() =>
    terminalPreviewFailure.value
      ? `载入终端日志失败：${readErrorMessage(terminalPreviewFailure.value)}`
      : '',
  )
  const terminalPreviewLoaded = computed(() => terminalPreview.value !== null)
  let detailRequestGeneration = 0
  let terminalPreviewRequestGeneration = 0
  let terminalPreviewRequest: { projectId: string; runId: string; artifactId: string } | null = null

  const currentProjectId = computed(() => workspaceStore.currentProjectId)
  const projectDetail = computed(() => {
    const detail = workspaceStore.projectDetail
    return detail?.project.id === currentProjectId.value ? detail : null
  })
  const resultReadState = computed(() =>
    workspaceStore.error
      ? projectDetail.value
        ? '工作与接受结果刷新失败，显示上次读取内容。'
        : '工作与接受结果读取失败，当前结果未知。'
      : !projectDetail.value
        ? '工作与接受结果尚未读到。'
        : '',
  )
  const intentById = computed(
    () => new Map((projectDetail.value?.intents || []).map((intent) => [intent.id, intent])),
  )
  const reports = ref<ReportInventoryItem[]>([])
  const reportsLoaded = ref(false)
  const reportsFailure = shallowRef<unknown>(null)
  const reportsError = computed(() =>
    reportsFailure.value ? `已接受报告读取失败：${readErrorMessage(reportsFailure.value)}` : '',
  )
  let reportsRequest: object | null = null
  let reportsReadAgain = false

  interface HookRead {
    events: TaskRunHookEvent[]
    loaded: boolean
    loading: boolean
    failure: unknown
    readAgain: boolean
  }
  const hookReads = ref<Record<string, HookRead>>({})
  const expandedTools = ref(new Set<string>())
  let timelineGeneration = 0

  const {
    data: runsData,
    error: runsError,
    isError: isRunsError,
    isSuccess: isRunsSuccess,
    isLoading: isLoadingRuns,
    refetch: refetchRuns,
  } = useQuery<TaskRun[]>({
    queryKey: ['workspaceRuns', currentProjectId],
    queryFn: () => {
      const projectId = currentProjectId.value
      if (!projectId) return Promise.resolve([])
      return api<TaskRun[]>('GET', `/projects/${encodeURIComponent(projectId)}/task-runs`)
    },
    enabled: computed(() => Boolean(currentProjectId.value)),
  })

  const runs = computed(() =>
    (runsData.value || []).filter(
      (run) => run.project_id === currentProjectId.value && run.task_type !== 'agent_chat',
    ),
  )
  const runsInitialError = computed(() => isRunsError.value && runs.value.length === 0)
  const runsStaleError = computed(() => isRunsError.value && runs.value.length > 0)

  const taskPurposeLabels: Record<string, string> = {
    bootstrap: '建立项目起点',
    reason: '规划下一步工作',
    explore: '执行探索任务',
    conclude: '收敛探索结果',
    verifier: '独立复核结果',
    verify: '独立复核结果',
    report: '生成评估报告',
  }
  const taskRoleLabels: Record<string, string> = {
    bootstrap: '项目初始化',
    reason: '调度推理',
    explore: '探索执行',
    conclude: '结果收敛',
    verifier: '独立复核',
    verify: '独立复核',
    report: '报告生成',
  }
  const priorityLabels: Record<number, string> = {
    0: '补充',
    1: '普通',
    2: '优先',
  }

  const runIntent = (run: TaskRun): Intent | null =>
    run.intent_id ? intentById.value.get(run.intent_id) || null : null

  const runBusinessTitle = (run: TaskRun): string =>
    runIntent(run)?.description || taskPurposeLabels[run.task_type] || run.task_type

  const runRoleLabel = (run: TaskRun): string => {
    const role = runIntent(run)?.assessment_role
    if (role === 'verifier') return '独立复核'
    if (role === 'discovery') return '发现与验证'
    return taskRoleLabels[run.task_type] || '执行任务'
  }

  const runPriorityLabel = (run: TaskRun): string => {
    const priority = runIntent(run)?.priority
    return priority === undefined ? '' : priorityLabels[priority] || `优先级 ${priority}`
  }

  const intentDiagnosticText = (intent: Intent | null): string => {
    if (intent?.conclusion === 'abandoned') {
      return intent.conclusion_reason ? `工作已停止：${intent.conclusion_reason}` : '工作已停止'
    }
    if (intent?.conclusion === 'failed') {
      return intent.conclusion_reason ? `工作未完成：${intent.conclusion_reason}` : '工作未完成'
    }
    return ''
  }

  const linkedIntentOutcomeText = (run: TaskRun): string => intentDiagnosticText(runIntent(run))

  const runResults = (run: TaskRun) =>
    resultsForRun(
      run,
      projectDetail.value?.facts || [],
      projectDetail.value?.intents || [],
      reports.value,
    )
  const runResultText = (run: TaskRun): string => {
    const results = runResults(run)
    if (results.length) return results.map((result) => result.text).join('\n\n')
    if (run.status === 'running') return '正在执行'
    if (run.status === 'queued') return '等待执行'
    return run.status === 'succeeded' ? '执行结束，结果未确认' : '本次执行未完成，结果未确认'
  }

  const filteredRuns = computed(() => {
    let list = [...runs.value]

    if (statusFilter.value !== 'all') {
      list = list.filter((run) => run.status === statusFilter.value)
    }

    if (searchQuery.value.trim()) {
      const query = searchQuery.value.trim().toLowerCase()
      list = list.filter((run) => {
        const intent = runIntent(run)
        const searchable = [
          run.id,
          run.worker_name,
          run.task_type,
          runBusinessTitle(run),
          runRoleLabel(run),
          runPriorityLabel(run),
          runResultText(run),
          intent?.assessment_role,
          intent?.priority === undefined
            ? ''
            : `priority:${intent.priority} 优先级 ${intent.priority}`,
          intent?.conclusion,
          intent?.conclusion_reason,
        ]
        return searchable.some((value) =>
          String(value || '')
            .toLowerCase()
            .includes(query),
        )
      })
    }

    list.sort((left, right) => {
      return (runReferenceTime(right) || '').localeCompare(runReferenceTime(left) || '')
    })

    return list
  })

  const workTimeline = computed(() =>
    buildWorkTimeline(
      runs.value,
      projectDetail.value?.intents || [],
      projectDetail.value?.facts || [],
      new Set(filteredRuns.value.map((run) => run.id)),
      reports.value,
    ),
  )
  const workTitle = (work: WorkNode) => work.intent?.description || runBusinessTitle(work.runs[0]!)
  const toolCalls = (run: TaskRun) => pairToolCalls(run, hookReads.value[run.id]?.events || [])
  const workActivity = (work: WorkNode) => {
    const calls = work.runs.filter(isActiveRun).flatMap(toolCalls)
    const active = calls.filter((call) => call.active)
    if (active.length) return active
    const latest = calls
      .filter((call) => call.endedAt)
      .sort((a, b) => Date.parse(b.endedAt!) - Date.parse(a.endedAt!))[0]
    return latest ? [latest] : []
  }
  const hookReadError = (runId: string) => {
    const read = hookReads.value[runId]
    return read?.failure
      ? `${read.loaded ? '工具过程刷新失败，显示上次观测' : '工具过程读取失败'}：${readErrorMessage(read.failure)}`
      : ''
  }
  const hookObservationIncomplete = (runId: string) =>
    hookReads.value[runId]?.events.some(
      (event) => event.hook_event_name === 'observation_incomplete',
    ) || false

  const loadHooks = async (runId: string, background = false, finalRead = false) => {
    const projectId = currentProjectId.value
    if (!projectId) return
    const generation = timelineGeneration
    hookReads.value[runId] ||= {
      events: [],
      loaded: false,
      loading: false,
      failure: null,
      readAgain: false,
    }
    const read = hookReads.value[runId]!
    if (read.loading) {
      if (finalRead) read.readAgain = true
      return
    }
    if (background && stopsAutomaticRead(read.failure)) return
    read.loading = true
    const ownsRead = () => generation === timelineGeneration && currentProjectId.value === projectId
    try {
      const groups = await api<TaskRunEventGroup<TaskRunHookEvent>[]>(
        'GET',
        `/projects/${encodeURIComponent(projectId)}/task-runs/hook-events?task_run_id=${encodeURIComponent(runId)}`,
      )
      if (!ownsRead()) return
      read.events = eventsForRun(groups, runId)
      read.loaded = true
      read.failure = null
    } catch (error: unknown) {
      if (ownsRead()) read.failure = error
    } finally {
      if (ownsRead()) {
        read.loading = false
        if (read.readAgain) {
          read.readAgain = false
          void loadHooks(runId, true)
        }
      }
    }
  }
  const toggleTools = (work: WorkNode) => {
    if (expandedTools.value.has(work.key)) expandedTools.value.delete(work.key)
    else {
      expandedTools.value.add(work.key)
      for (const run of work.runs) void loadHooks(run.id)
    }
  }
  const refetchHooks = (runId: string) => {
    void loadHooks(runId)
  }
  const loadReports = async (finalRead = false) => {
    const projectId = currentProjectId.value
    if (!projectId || !runs.value.some((run) => run.task_type === 'report')) return
    if (reportsRequest) {
      if (finalRead) reportsReadAgain = true
      return
    }
    const request = {}
    reportsRequest = request
    const items: ReportInventoryItem[] = []
    try {
      let page = 1
      while (true) {
        const result = await api<ReportInventoryPage>(
          'GET',
          `/reports?project_id=${encodeURIComponent(projectId)}&page=${page}&page_size=100`,
        )
        if (reportsRequest !== request || currentProjectId.value !== projectId) return
        items.push(...result.items)
        if (page * result.page_size >= result.total) break
        page += 1
      }
      reports.value = items
      reportsLoaded.value = true
      reportsFailure.value = null
    } catch (error: unknown) {
      if (reportsRequest === request) reportsFailure.value = error
    } finally {
      if (reportsRequest === request) {
        reportsRequest = null
        if (reportsReadAgain) {
          reportsReadAgain = false
          void loadReports()
        }
      }
    }
  }
  const refreshTimeline = () => {
    void refetchRuns()
    if (currentProjectId.value) void workspaceStore.loadProject(currentProjectId.value)
    void loadReports()
    for (const run of runs.value) {
      if (isActiveRun(run) || hookReads.value[run.id]) void loadHooks(run.id)
    }
  }

  const filtersActive = computed(
    () => statusFilter.value !== 'all' || Boolean(searchQuery.value.trim()),
  )

  const selectedRun = computed(() => {
    if (!selectedRunId.value) return null
    return runs.value.find((run) => run.id === selectedRunId.value) || null
  })
  const selectedTerminalOutput = computed(() => {
    return terminalOutputs.value.find((output) => output.id === stdoutArtifactId.value) || null
  })
  const stdoutContent = computed(() => terminalPreview.value?.content || '')
  const stdoutDownloadUrl = computed(() => {
    const projectId = currentProjectId.value
    const artifactId = stdoutArtifactId.value
    if (!projectId || !artifactId) return ''
    return `/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifactId)}/content`
  })
  const stdoutDownloadLabel = computed(() =>
    selectedTerminalOutput.value?.captureTruncated ? '下载已保存日志' : '下载日志',
  )
  const stdoutDownloadTitle = computed(() =>
    selectedTerminalOutput.value?.captureTruncated
      ? '下载服务端已保存的首尾日志片段；采集时丢弃的中间内容无法恢复'
      : '下载服务端已保存的完整日志产物',
  )
  const previewTruncationMessage = computed(() => {
    const preview = terminalPreview.value
    if (!preview?.truncated) return ''
    return `当前展示日志尾部预览（${bytes(preview.bytes_read)} / ${bytes(preview.size)}）；下载可获取服务端已保存的完整产物。`
  })
  const captureTruncationMessage = computed(() => {
    const output = selectedTerminalOutput.value
    if (!output?.captureTruncated) return ''
    const originalSize = output.originalSize === null ? '未知大小' : bytes(output.originalSize)
    return `此输出在采集时已截断，原始输出约为 ${originalSize}；当前产物仅保留首尾片段，下载无法恢复丢弃的中间内容。`
  })

  const selectRun = (id: string) => {
    if (!runs.value.some((run) => run.id === id)) return
    chatRunNotice.value = false
    selectedRunId.value = id
    void router.replace({ query: { ...route.query, runId: id } })
  }

  const openProjectAgent = () => {
    const projectId = currentProjectId.value
    if (projectId) void router.push({ path: '/agent', query: { project: projectId } })
  }

  const scrollToBottom = () => {
    if (terminalScreen.value) {
      terminalScreen.value.scrollTop = terminalScreen.value.scrollHeight
    }
  }

  const invalidateTerminalPreview = () => {
    terminalPreviewRequestGeneration += 1
    terminalPreviewRequest = null
    terminalPreview.value = null
    terminalPreviewSource.value = null
    isLoadingTerminalPreview.value = false
    terminalPreviewFailure.value = null
  }

  const clearRunDetails = () => {
    detailRequestGeneration += 1
    commandEvents.value = []
    commandsLoaded.value = false
    commandsFailure.value = null
    isLoadingCommands.value = false
    artifacts.value = []
    artifactsLoaded.value = false
    artifactsFailure.value = null
    isLoadingArtifacts.value = false
    terminalOutputs.value = []
    stdoutArtifactId.value = null
    terminalRecovered.value = false
    invalidateTerminalPreview()
  }

  const loadTerminalOutput = async (
    artifactId: string,
    options: { projectId?: string; runId?: string; background?: boolean } = {},
  ) => {
    const projectId = options.projectId || currentProjectId.value
    const runId = options.runId || selectedRun.value?.id
    if (!projectId || !runId) return
    if (
      terminalPreviewRequest?.projectId === projectId &&
      terminalPreviewRequest.runId === runId &&
      terminalPreviewRequest.artifactId === artifactId
    )
      return
    if (
      options.background &&
      stdoutArtifactId.value === artifactId &&
      stopsAutomaticRead(terminalPreviewFailure.value)
    )
      return

    const requestGeneration = ++terminalPreviewRequestGeneration
    const activeAtRead = selectedRun.value ? isActiveRun(selectedRun.value) : false
    const preservePreview =
      terminalPreviewSource.value?.projectId === projectId &&
      terminalPreviewSource.value?.runId === runId &&
      terminalPreviewSource.value?.artifactId === artifactId

    if (stdoutArtifactId.value !== artifactId) terminalPreviewFailure.value = null
    stdoutArtifactId.value = artifactId
    terminalPreviewRequest = { projectId, runId, artifactId }
    isLoadingTerminalPreview.value = true
    if (!preservePreview) {
      terminalPreview.value = null
      terminalPreviewSource.value = null
    }

    try {
      const preview = await api<ArtifactPreview>(
        'GET',
        `/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifactId)}/preview-tail`,
      )
      if (
        requestGeneration !== terminalPreviewRequestGeneration ||
        currentProjectId.value !== projectId ||
        selectedRun.value?.id !== runId ||
        stdoutArtifactId.value !== artifactId
      ) {
        return
      }
      terminalPreview.value = preview
      terminalPreviewSource.value = { projectId, runId, artifactId }
      terminalPreviewFailure.value = null
      if (autoScrollTerminal.value) await nextTick(scrollToBottom)
    } catch (error: unknown) {
      if (
        requestGeneration === terminalPreviewRequestGeneration &&
        currentProjectId.value === projectId &&
        selectedRun.value?.id === runId &&
        stdoutArtifactId.value === artifactId
      ) {
        terminalPreviewFailure.value = error
      }
    } finally {
      if (requestGeneration === terminalPreviewRequestGeneration) {
        terminalPreviewRequest = null
        isLoadingTerminalPreview.value = false
        if (activeAtRead && selectedRun.value && !isActiveRun(selectedRun.value)) {
          void loadTerminalOutput(artifactId, { projectId, runId, background: true })
        }
      }
    }
  }

  const selectTerminalOutput = (artifactId: string) => {
    void loadTerminalOutput(artifactId)
  }

  const ownsDetails = (generation: number, projectId: string, runId: string) =>
    generation === detailRequestGeneration &&
    currentProjectId.value === projectId &&
    selectedRun.value?.id === runId

  const loadCommands = async (runId: string, background = false) => {
    const projectId = currentProjectId.value
    if (!projectId || selectedRun.value?.id !== runId || isLoadingCommands.value) return
    if (background && stopsAutomaticRead(commandsFailure.value)) return
    const generation = detailRequestGeneration
    const activeAtRead = isActiveRun(selectedRun.value)
    isLoadingCommands.value = true
    try {
      const groups = await api<TaskRunEventGroup<TaskRunCommandEvent>[]>(
        'GET',
        `/projects/${encodeURIComponent(projectId)}/task-runs/command-events?task_run_id=${encodeURIComponent(runId)}`,
      )
      if (!ownsDetails(generation, projectId, runId)) return
      commandEvents.value = eventsForRun(groups, runId)
      commandsLoaded.value = true
      commandsFailure.value = null
    } catch (error: unknown) {
      if (ownsDetails(generation, projectId, runId)) {
        commandsFailure.value = error
      }
    } finally {
      if (ownsDetails(generation, projectId, runId)) {
        isLoadingCommands.value = false
        if (activeAtRead && selectedRun.value && !isActiveRun(selectedRun.value))
          void loadCommands(runId, true)
      }
    }
  }

  const loadArtifacts = async (runId: string, background = false) => {
    const projectId = currentProjectId.value
    if (!projectId || selectedRun.value?.id !== runId || isLoadingArtifacts.value) return
    if (background && stopsAutomaticRead(artifactsFailure.value)) return
    const generation = detailRequestGeneration
    const activeAtRead = isActiveRun(selectedRun.value)
    isLoadingArtifacts.value = true
    try {
      const runArtifacts = await api<RunArtifact[]>(
        'GET',
        `/projects/${encodeURIComponent(projectId)}/artifacts?task_run_id=${encodeURIComponent(runId)}`,
      )
      if (!ownsDetails(generation, projectId, runId)) return
      artifacts.value = runArtifacts
      artifactsLoaded.value = true
      artifactsFailure.value = null

      const outputs = buildTerminalOutputs(runArtifacts)
      const retainedOutput = selectRetainedTerminalOutput(outputs, stdoutArtifactId.value)
      terminalOutputs.value = outputs
      terminalRecovered.value = isConcludeRecovery(outputs)
      if (retainedOutput) {
        if (retainedOutput.id !== stdoutArtifactId.value) {
          void loadTerminalOutput(retainedOutput.id, { projectId, runId, background })
        }
      } else {
        stdoutArtifactId.value = null
        invalidateTerminalPreview()
      }
    } catch (error: unknown) {
      if (ownsDetails(generation, projectId, runId)) {
        artifactsFailure.value = error
      }
    } finally {
      if (ownsDetails(generation, projectId, runId)) {
        isLoadingArtifacts.value = false
        if (activeAtRead && selectedRun.value && !isActiveRun(selectedRun.value))
          void loadArtifacts(runId, true)
      }
    }
  }

  const loadRunDetails = (runId: string, background = false) => {
    // 各来源独立提交、独立去重；瞬时故障沿用刷新节奏，明确拒绝留给手动重读。
    void loadCommands(runId, background)
    void loadArtifacts(runId, background)
    if (stdoutArtifactId.value) void loadTerminalOutput(stdoutArtifactId.value, { background })
  }
  const refetchCommands = () => {
    if (selectedRun.value) void loadCommands(selectedRun.value.id)
  }
  const refetchArtifacts = () => {
    if (selectedRun.value) void loadArtifacts(selectedRun.value.id)
  }
  const refetchTerminal = () => {
    if (stdoutArtifactId.value) void loadTerminalOutput(stdoutArtifactId.value)
    else refetchArtifacts()
  }

  const previewArtifact = (artifactId: string) => {
    const projectId = currentProjectId.value
    if (!projectId) return
    void router.push(
      `/projects/${encodeURIComponent(projectId)}/artifacts?artifactId=${encodeURIComponent(artifactId)}`,
    )
  }

  const formatTimeShort = (value: string): string => {
    if (!value) return ''
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return ''
    return date.toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
  }

  watch(
    [currentProjectId, () => route.query.runId, runsData, isRunsSuccess],
    ([projectId, newId, data, success], previous) => {
      if (projectId !== previous?.[0] || newId) chatRunNotice.value = false
      selectedRunId.value = typeof newId === 'string' && newId ? newId : null
      // 只根据成功加载的实际类型处理旧聊天链接，失败或查无记录不猜测归属。
      if (!success || !data?.some((run) => run.id === newId && run.task_type === 'agent_chat')) {
        return
      }
      selectedRunId.value = null
      chatRunNotice.value = true
      const query = { ...route.query }
      delete query.runId
      void router.replace({ query })
    },
    { immediate: true },
  )

  watch(
    [currentProjectId, () => selectedRun.value?.id || null],
    ([projectId, runId], previous) => {
      const [previousProjectId, previousRunId] = previous || [null, null]
      if (projectId !== previousProjectId || runId !== previousRunId) {
        clearRunDetails()
      }
      if (projectId && runId) {
        void loadRunDetails(runId)
      }
    },
    { immediate: true },
  )

  watch(
    currentProjectId,
    () => {
      timelineGeneration += 1
      hookReads.value = {}
      expandedTools.value = new Set()
      reportsRequest = null
      reportsReadAgain = false
      reports.value = []
      reportsLoaded.value = false
      reportsFailure.value = null
    },
    { flush: 'sync' },
  )

  watch(
    runs,
    (current, previous) => {
      const before = new Map(
        (previous || [])
          .filter((run) => run.project_id === currentProjectId.value)
          .map((run) => [run.id, run]),
      )
      let terminalChanged = false
      for (const run of current) {
        const old = before.get(run.id)
        if (old && isActiveRun(old) && !isActiveRun(run)) {
          terminalChanged = true
          void loadHooks(run.id, true, true)
          if (selectedRun.value?.id === run.id) loadRunDetails(run.id, true)
        } else if (isActiveRun(run) && !old) void loadHooks(run.id, true)
      }
      if (terminalChanged && currentProjectId.value)
        void workspaceStore.loadProject(currentProjectId.value)
      if (
        terminalChanged ||
        current.some((run) => run.task_type === 'report' && !before.has(run.id))
      )
        void loadReports(terminalChanged)
    },
    { immediate: true },
  )

  let pollTimer: number | null = null
  onMounted(() => {
    pollTimer = window.setInterval(() => {
      if (currentProjectId.value) {
        void refetchRuns()
      }
      for (const run of runs.value) {
        if (isActiveRun(run)) void loadHooks(run.id, true)
      }
      if (selectedRun.value && isActiveRun(selectedRun.value)) {
        loadRunDetails(selectedRun.value.id, true)
      }
    }, 4000)
  })

  onUnmounted(() => {
    if (pollTimer !== null) window.clearInterval(pollTimer)
    detailRequestGeneration += 1
    terminalPreviewRequest = null
    terminalPreviewRequestGeneration += 1
    timelineGeneration += 1
    reportsRequest = null
  })

  watch(
    () => uiStore.globalRefreshKey,
    () => {
      refreshTimeline()
      if (selectedRun.value) {
        void loadRunDetails(selectedRun.value.id)
      }
    },
  )

  return {
    activeDetailTab,
    artifacts,
    autoScrollTerminal,
    captureTruncationMessage,
    chatRunNotice,
    commandEvents,
    detailTabs,
    filteredRuns,
    filtersActive,
    formatRelativeTime,
    formatTimeShort,
    isLoadingRunDetails,
    isLoadingRuns,
    isLoadingTerminalPreview,
    linkedIntentOutcomeText,
    openProjectAgent,
    previewArtifact,
    previewTruncationMessage,
    refetchRuns,
    refetchTerminal,
    runBusinessTitle,
    workTimeline,
    workTitle,
    workActivity,
    toolCalls,
    hookReads,
    hookReadError,
    hookObservationIncomplete,
    expandedTools,
    toggleTools,
    refetchHooks,
    refreshTimeline,
    reportsLoaded,
    resultReadState,
    reportsError,
    loadReports,
    commandsLoaded,
    commandsError,
    isLoadingCommands,
    artifactsLoaded,
    artifactsError,
    isLoadingArtifacts,
    refetchCommands,
    refetchArtifacts,
    terminalPreviewLoaded,
    runDurationText,
    runIntent,
    runPriorityLabel,
    runReferenceTime,
    runResultText,
    runRoleLabel,
    runsError,
    runsInitialError,
    runsStaleError,
    searchQuery,
    selectedRun,
    selectedRunId,
    selectRun,
    selectTerminalOutput,
    statusFilter,
    statusText,
    stdoutArtifactId,
    stdoutContent,
    stdoutDownloadLabel,
    stdoutDownloadTitle,
    stdoutDownloadUrl,
    terminalOutputs,
    terminalPreviewError,
    terminalRecovered,
    terminalScreen,
  }
}
