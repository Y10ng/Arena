import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { api, type ArtifactPreview } from '@/api/client'
import type { Intent } from '@/api/types'
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
import type { RunArtifact, TaskRun, TaskRunCommandEvent, TaskRunEventGroup } from './runTypes'

const detailTabs = [
  { label: '终端视图', value: 'terminal' },
  { label: 'Shell命令', value: 'commands' },
  { label: '产物证据', value: 'artifacts' },
]

const errorMessage = (error: unknown): string => {
  if (error instanceof Error && error.message) return error.message
  return String(error || '未知错误')
}

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
  const isLoadingRunDetails = ref(false)
  const runDetailsError = ref('')
  const isLoadingTerminalPreview = ref(false)
  const terminalPreviewError = ref('')
  let detailRequestGeneration = 0
  let detailRequestInFlight: { projectId: string; runId: string; generation: number } | null = null
  let terminalPreviewRequestGeneration = 0

  const currentProjectId = computed(() => workspaceStore.currentProjectId)
  const projectDetail = computed(() => {
    const detail = workspaceStore.projectDetail
    return detail?.project.id === currentProjectId.value ? detail : null
  })
  const intentById = computed(
    () => new Map((projectDetail.value?.intents || []).map((intent) => [intent.id, intent])),
  )
  const factById = computed(
    () => new Map((projectDetail.value?.facts || []).map((fact) => [fact.id, fact])),
  )

  const {
    data: runsData,
    error: runsError,
    isError: isRunsError,
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

  const runs = computed(() => runsData.value || [])
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
      return intent.conclusion_reason ? `结果未接受：${intent.conclusion_reason}` : '结果未接受'
    }
    return ''
  }

  const runCardDiagnostic = (run: TaskRun): string => {
    if (run.error_message || run.error_type) return run.error_message || run.error_type || ''
    if (run.status === 'succeeded') return ''
    return statusText(run.status)
  }

  const linkedIntentOutcomeText = (run: TaskRun): string =>
    intentDiagnosticText(runIntent(run))

  const acceptedFactForRun = (run: TaskRun) => {
    const intent = runIntent(run)
    const fact = intent?.to ? factById.value.get(intent.to) : null
    return fact?.task_run_id === run.id ? fact : null
  }

  const runResultText = (run: TaskRun): string => {
    const intent = runIntent(run)
    const fact = acceptedFactForRun(run)
    if (fact?.description) return fact.description
    if (run.status === 'failed' || run.status === 'timed_out' || run.status === 'cancelled') {
      return run.error_message || run.error_type || statusText(run.status)
    }
    if (run.status === 'succeeded') {
      if (intent?.conclusion === 'completed') {
        return intent.to
          ? '执行成功；已接受结果属于关联运行'
          : '执行成功，未返回可展示的接受结果'
      }
      return intent ? '执行成功，等待结果接受' : '执行成功'
    }
    if (run.status === 'running') return '正在执行'
    if (run.status === 'queued') return '等待执行'
    return statusText(run.status)
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
    selectedRunId.value = id
    void router.replace({ query: { ...route.query, runId: id } })
  }

  const scrollToBottom = () => {
    if (terminalScreen.value) {
      terminalScreen.value.scrollTop = terminalScreen.value.scrollHeight
    }
  }

  const invalidateTerminalPreview = () => {
    terminalPreviewRequestGeneration += 1
    terminalPreview.value = null
    terminalPreviewSource.value = null
    isLoadingTerminalPreview.value = false
    terminalPreviewError.value = ''
  }

  const clearRunDetails = () => {
    detailRequestGeneration += 1
    detailRequestInFlight = null
    terminalPreviewRequestGeneration += 1
    commandEvents.value = []
    artifacts.value = []
    terminalOutputs.value = []
    stdoutArtifactId.value = null
    terminalPreview.value = null
    terminalPreviewSource.value = null
    terminalRecovered.value = false
    isLoadingRunDetails.value = false
    runDetailsError.value = ''
    isLoadingTerminalPreview.value = false
    terminalPreviewError.value = ''
  }

  const loadTerminalOutput = async (
    artifactId: string,
    options: { projectId?: string; runId?: string; preservePreview?: boolean } = {},
  ) => {
    const projectId = options.projectId || currentProjectId.value
    const runId = options.runId || selectedRun.value?.id
    if (!projectId || !runId) return

    const requestGeneration = ++terminalPreviewRequestGeneration
    const preservePreview =
      options.preservePreview === true &&
      terminalPreviewSource.value?.projectId === projectId &&
      terminalPreviewSource.value?.runId === runId &&
      terminalPreviewSource.value?.artifactId === artifactId

    stdoutArtifactId.value = artifactId
    terminalPreviewError.value = ''
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
      if (autoScrollTerminal.value) await nextTick(scrollToBottom)
    } catch (error: unknown) {
      if (
        requestGeneration === terminalPreviewRequestGeneration &&
        currentProjectId.value === projectId &&
        selectedRun.value?.id === runId &&
        stdoutArtifactId.value === artifactId
      ) {
        terminalPreview.value = null
        terminalPreviewSource.value = null
        terminalPreviewError.value = `载入终端日志失败：${errorMessage(error)}`
      }
    } finally {
      if (requestGeneration === terminalPreviewRequestGeneration) {
        isLoadingTerminalPreview.value = false
      }
    }
  }

  const selectTerminalOutput = (artifactId: string) => {
    void loadTerminalOutput(artifactId)
  }

  const loadRunDetails = async (
    runId: string,
    options: { skipIfInFlight?: boolean; background?: boolean } = {},
  ) => {
    const projectId = currentProjectId.value
    if (!projectId || selectedRun.value?.id !== runId) return
    if (
      options.skipIfInFlight &&
      detailRequestInFlight?.projectId === projectId &&
      detailRequestInFlight.runId === runId
    ) {
      return
    }

    const requestGeneration = ++detailRequestGeneration
    detailRequestInFlight = { projectId, runId, generation: requestGeneration }
    if (!options.background) isLoadingRunDetails.value = true

    try {
      const encodedProjectId = encodeURIComponent(projectId)
      const encodedRunId = encodeURIComponent(runId)
      const [commandGroups, runArtifacts] = await Promise.all([
        api<TaskRunEventGroup<TaskRunCommandEvent>[]>(
          'GET',
          `/projects/${encodedProjectId}/task-runs/command-events?task_run_id=${encodedRunId}`,
        ),
        api<RunArtifact[]>(
          'GET',
          `/projects/${encodedProjectId}/artifacts?task_run_id=${encodedRunId}`,
        ),
      ])

      if (
        requestGeneration !== detailRequestGeneration ||
        currentProjectId.value !== projectId ||
        selectedRun.value?.id !== runId
      ) {
        return
      }

      commandEvents.value = eventsForRun(commandGroups, runId)
      artifacts.value = runArtifacts
      runDetailsError.value = ''

      const outputs = buildTerminalOutputs(runArtifacts)
      const retainedOutput = selectRetainedTerminalOutput(outputs, stdoutArtifactId.value)
      const previousArtifactId = stdoutArtifactId.value
      terminalOutputs.value = outputs
      terminalRecovered.value = isConcludeRecovery(outputs)

      if (retainedOutput) {
        await loadTerminalOutput(retainedOutput.id, {
          projectId,
          runId,
          preservePreview: retainedOutput.id === previousArtifactId,
        })
      } else {
        stdoutArtifactId.value = null
        invalidateTerminalPreview()
      }
    } catch (error: unknown) {
      if (
        requestGeneration === detailRequestGeneration &&
        currentProjectId.value === projectId &&
        selectedRun.value?.id === runId
      ) {
        runDetailsError.value = `载入运行详情失败：${errorMessage(error)}`
      }
    } finally {
      if (detailRequestInFlight?.generation === requestGeneration) {
        detailRequestInFlight = null
      }
      if (requestGeneration === detailRequestGeneration) {
        isLoadingRunDetails.value = false
      }
    }
  }

  const refetchTerminal = () => {
    if (selectedRun.value) {
      void loadRunDetails(selectedRun.value.id)
    }
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
    () => route.query.runId,
    (newId) => {
      selectedRunId.value = typeof newId === 'string' && newId ? newId : null
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

  let pollTimer: number | null = null
  onMounted(() => {
    pollTimer = window.setInterval(() => {
      if (currentProjectId.value) {
        void refetchRuns()
      }
      if (selectedRun.value && ['running', 'queued'].includes(selectedRun.value.status)) {
        void loadRunDetails(selectedRun.value.id, { skipIfInFlight: true, background: true })
      }
    }, 4000)
  })

  onUnmounted(() => {
    if (pollTimer !== null) window.clearInterval(pollTimer)
    detailRequestGeneration += 1
    detailRequestInFlight = null
    terminalPreviewRequestGeneration += 1
  })

  watch(
    () => uiStore.globalRefreshKey,
    () => {
      void refetchRuns()
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
    previewArtifact,
    previewTruncationMessage,
    refetchRuns,
    refetchTerminal,
    runBusinessTitle,
    runCardDiagnostic,
    runDetailsError,
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
