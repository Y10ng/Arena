import { flushPromises, mount } from '@vue/test-utils'
import { computed, defineComponent, h, reactive, ref, unref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, type ArtifactPreview } from '@/api/client'
import type { ProjectDetail } from '@/api/types'
import type { RunArtifact, TaskRun, TaskRunCommandEvent, TaskRunEventGroup } from './runTypes'
import { useRunsPage } from './useRunsPage'

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  loadProject: vi.fn(),
  refetchRuns: vi.fn(),
  router: { push: vi.fn(), replace: vi.fn() },
  routeStore: null as unknown as { query: Record<string, unknown> },
  workspaceStore: null as unknown as {
    currentProjectId: string | null
    projectDetail: ProjectDetail | null
  },
  uiStore: null as unknown as { globalRefreshKey: number },
}))

const runsData = ref<TaskRun[]>([])
const runsStatus = ref<'pending' | 'success' | 'error'>('success')

vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  api: mocks.api,
}))

vi.mock('@/stores/workspace', () => ({
  useWorkspaceStore: () => ({
    ...mocks.workspaceStore,
    get currentProjectId() {
      return mocks.workspaceStore.currentProjectId
    },
    get projectDetail() {
      return mocks.workspaceStore.projectDetail
    },
    loadProject: mocks.loadProject,
  }),
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => mocks.uiStore,
}))

vi.mock('vue-router', () => ({
  useRoute: () => mocks.routeStore,
  useRouter: () => mocks.router,
}))

vi.mock('@tanstack/vue-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown }) =>
    (unref(queryKey) as unknown[])[0] === 'project-runtime'
      ? { data: ref(undefined), isError: ref(false), error: ref(null) }
      : {
          data: runsData,
          error: ref<unknown>(null),
          isError: computed(() => runsStatus.value === 'error'),
          isSuccess: computed(() => runsStatus.value === 'success'),
          isFetching: ref(false),
          isLoading: ref(false),
          refetch: mocks.refetchRuns,
        },
}))

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason?: unknown) => void
}

const deferred = <T>(): Deferred<T> => {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const run = (id: string, projectId = 'project-a', status = 'running'): TaskRun => ({
  id,
  project_id: projectId,
  intent_id: null,
  task_type: 'explore',
  worker_name: 'claude-code',
  worker_type: 'claude_code',
  status,
  session_id: null,
  prompt_group: null,
  prompt_version: null,
  execution_profile: null,
  started_at: '2026-08-20T10:00:00.000Z',
  ended_at: status === 'running' ? null : '2026-08-20T10:01:00.000Z',
  exit_code: null,
  error_type: null,
  error_message: null,
  assessment_attempt_id: null,
  runtime_context: {},
})

const artifact = (
  id: string,
  stream: 'stdout' | 'stderr' | 'diagnostic' = 'stdout',
): RunArtifact => ({
  id,
  project_id: 'project-a',
  task_run_id: 'run-a',
  kind: 'raw_output',
  kind_deprecated: false,
  deprecation_message: null,
  category: 'raw_output',
  uri: `artifact://runs/explore_execute-${stream}.log`,
  sha256: `${id}-sha256`,
  size: 128,
  created_at: '2026-08-20T10:00:00.000Z',
  metadata: { phase: 'explore_execute', stream, returncode: 0 },
})

const preview = (artifactId: string, content: string): ArtifactPreview => ({
  artifact_id: artifactId,
  project_id: 'project-a',
  task_run_id: 'run-a',
  kind: 'raw_output',
  kind_deprecated: false,
  deprecation_message: null,
  content,
  size: content.length,
  bytes_read: content.length,
  truncated: false,
  sha256: `${artifactId}-sha256`,
})

const runIdFromPath = (path: string): string => {
  const query = new URLSearchParams(path.split('?')[1] || '')
  return query.get('task_run_id') || ''
}

const commandGroups = (
  id: string,
  runId = 'run-a',
  projectId = 'project-a',
): TaskRunEventGroup<TaskRunCommandEvent>[] => [
  {
    task_run_id: runId,
    events: [
      {
        id,
        task_run_id: runId,
        project_id: projectId,
        ts: '2026-08-20T10:00:00.000Z',
        phase: 'explore_execute',
        command_kind: 'shell',
        kind: 'command_finished',
        status: 'success',
        exit_code: 0,
        returncode: 0,
        duration_seconds: 1,
        timed_out: false,
        cancelled: false,
        oom_killed: false,
        oom_evidence_source: null,
      },
    ],
  },
]

const emptyDetailResponse = (path: string, artifacts: RunArtifact[] = []) => {
  if (path.includes('/command-events') || path.includes('/hook-events')) {
    return [{ task_run_id: runIdFromPath(path), events: [] }]
  }
  if (path.startsWith('/reports?')) return { items: [], total: 0, page: 1, page_size: 100 }
  if (path.includes('/artifacts?')) return artifacts
  throw new Error(`未处理的 API 路径：${path}`)
}

let page: ReturnType<typeof useRunsPage>
let wrapper: ReturnType<typeof mount> | null = null
let routeState: { query: Record<string, unknown> }
let workspaceState: { currentProjectId: string | null; projectDetail: ProjectDetail | null }
let uiState: { globalRefreshKey: number }

const mountComposable = () => {
  const Host = defineComponent({
    setup() {
      page = useRunsPage()
      return () => h('div')
    },
  })
  wrapper = mount(Host)
}

beforeEach(() => {
  vi.useRealTimers()
  routeState = reactive({ query: { runId: 'run-a' } })
  workspaceState = reactive({ currentProjectId: 'project-a', projectDetail: null })
  uiState = reactive({ globalRefreshKey: 0 })
  mocks.routeStore = routeState
  mocks.workspaceStore = workspaceState
  mocks.uiStore = uiState
  mocks.api.mockReset()
  mocks.refetchRuns.mockReset().mockResolvedValue(undefined)
  mocks.router.push.mockReset()
  mocks.router.replace.mockReset()
  runsData.value = [run('run-a'), run('run-b')]
  runsStatus.value = 'success'
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.useRealTimers()
})

describe('useRunsPage chat exclusion', () => {
  it('uses the same non-chat collection for filtering, selection and refresh without changing API data', async () => {
    routeState.query = {}
    runsData.value = [
      { ...run('chat'), task_type: 'agent_chat' },
      { ...run('report'), task_type: 'report' },
      { ...run('verification'), task_type: 'external_verify' },
      run('exploration'),
    ]
    mocks.router.replace.mockImplementation(({ query }) => {
      routeState.query = query
    })
    mocks.api.mockImplementation((_method: string, path: string) =>
      Promise.resolve(emptyDetailResponse(path)),
    )
    mountComposable()
    await flushPromises()

    expect(page.filteredRuns.value.map((item) => item.id)).toEqual([
      'report',
      'verification',
      'exploration',
    ])
    expect(page.selectedRun.value).toBeNull()
    page.searchQuery.value = 'agent_chat'
    expect(page.filteredRuns.value).toEqual([])
    page.searchQuery.value = ''
    page.statusFilter.value = 'running'
    expect(page.filteredRuns.value).toHaveLength(3)
    page.selectRun('chat')
    expect(page.selectedRunId.value).toBeNull()
    expect(
      mocks.api.mock.calls.every(
        ([, path]) => path.includes('/hook-events') || path.startsWith('/reports?'),
      ),
    ).toBe(true)

    for (const id of ['report', 'verification']) {
      page.selectRun(id)
      await flushPromises()
      expect(page.selectedRun.value?.id).toBe(id)
      expect(mocks.api).toHaveBeenCalledWith(
        'GET',
        `/projects/project-a/artifacts?task_run_id=${id}`,
      )
    }
    mocks.refetchRuns.mockImplementation(() => {
      runsData.value = [...runsData.value, { ...run('new-chat'), task_type: 'agent_chat' }]
      return Promise.resolve()
    })
    uiState.globalRefreshKey += 1
    await flushPromises()
    expect(page.selectedRun.value?.id).toBe('verification')
    expect(page.filteredRuns.value.map((item) => item.id)).toEqual([
      'report',
      'verification',
      'exploration',
    ])
    expect(runsData.value.filter((item) => item.task_type === 'agent_chat')).toHaveLength(2)
    expect(
      mocks.api.mock.calls.every(([, path]) => !String(path).includes('task_run_id=chat')),
    ).toBe(true)
  })

  it('clears an old chat link only after successful type confirmation and never substitutes another run', async () => {
    routeState.query = { runId: 'chat', keep: 'value' }
    runsData.value = []
    runsStatus.value = 'pending'
    mocks.router.replace.mockImplementation(({ query }) => {
      routeState.query = query
    })
    mountComposable()
    await flushPromises()
    expect(page.selectedRun.value).toBeNull()
    expect(mocks.router.replace).not.toHaveBeenCalled()

    runsStatus.value = 'error'
    await flushPromises()
    expect(routeState.query.runId).toBe('chat')
    expect(page.chatRunNotice.value).toBe(false)
    expect(page.runsInitialError.value).toBe(true)
    runsData.value = [{ ...run('chat'), task_type: 'agent_chat' }, run('other')]
    await flushPromises()
    expect(mocks.router.replace).not.toHaveBeenCalled()
    expect(page.chatRunNotice.value).toBe(false)

    runsStatus.value = 'success'
    await flushPromises()
    expect(routeState.query).toEqual({ keep: 'value' })
    expect(page.chatRunNotice.value).toBe(true)
    expect(page.selectedRunId.value).toBeNull()
    expect(page.selectedRun.value).toBeNull()
    expect(
      mocks.api.mock.calls.every(
        ([, path]) => path.includes('/hook-events') || path.startsWith('/reports?'),
      ),
    ).toBe(true)
    uiState.globalRefreshKey += 1
    await flushPromises()
    expect(page.selectedRun.value).toBeNull()
    expect(page.chatRunNotice.value).toBe(true)
    page.openProjectAgent()
    expect(mocks.router.push).toHaveBeenCalledWith({
      path: '/agent',
      query: { project: 'project-a' },
    })

    routeState.query.runId = 'unknown'
    await flushPromises()
    expect(routeState.query.runId).toBe('unknown')
    expect(page.chatRunNotice.value).toBe(false)
    expect(page.selectedRun.value).toBeNull()
  })
})

describe('useRunsPage request ownership', () => {
  it('loads only retained detail requests and keeps selection through global refresh', async () => {
    mocks.api.mockImplementation((_method: string, path: string) =>
      Promise.resolve(emptyDetailResponse(path)),
    )
    mountComposable()
    await flushPromises()

    const detailPaths = [
      '/projects/project-a/task-runs/command-events?task_run_id=run-a',
      '/projects/project-a/artifacts?task_run_id=run-a',
    ]
    expect(
      mocks.api.mock.calls.map(([, path]) => path).filter((path) => !path.includes('/hook-events')),
    ).toEqual(detailPaths)
    expect(page.activeDetailTab.value).toBe('terminal')
    expect(page.detailTabs.map((tab) => tab.value)).toEqual(['terminal', 'commands', 'artifacts'])

    page.activeDetailTab.value = 'commands'
    uiState.globalRefreshKey += 1
    await flushPromises()

    expect(mocks.refetchRuns).toHaveBeenCalledOnce()
    expect(
      mocks.api.mock.calls.map(([, path]) => path).filter((path) => !path.includes('/hook-events')),
    ).toEqual([...detailPaths, ...detailPaths])
    expect(page.selectedRunId.value).toBe('run-a')
    expect(page.activeDetailTab.value).toBe('commands')
  })

  it('does not let a late run A response overwrite run B', async () => {
    const runACommands = deferred<TaskRunEventGroup<TaskRunCommandEvent>[]>()
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path.includes('/command-events?task_run_id=run-a')) return runACommands.promise
      if (path.includes('/command-events?task_run_id=run-b')) {
        return Promise.resolve(commandGroups('command-b', 'run-b'))
      }
      return Promise.resolve(emptyDetailResponse(path))
    })

    mountComposable()
    await flushPromises()
    routeState.query.runId = 'run-b'
    await flushPromises()

    expect(page.selectedRun.value?.id).toBe('run-b')
    expect(page.commandEvents.value.map((event) => event.id)).toEqual(['command-b'])

    runACommands.resolve(commandGroups('command-a'))
    await flushPromises()

    expect(page.commandEvents.value.map((event) => event.id)).toEqual(['command-b'])
  })

  it('does not let a project A response overwrite the same run ID in project B', async () => {
    const projectACommands = deferred<TaskRunEventGroup<TaskRunCommandEvent>[]>()
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path.includes('/projects/project-a/task-runs/command-events')) {
        return projectACommands.promise
      }
      if (path.includes('/projects/project-b/task-runs/command-events')) {
        return Promise.resolve(commandGroups('project-b-command', 'run-a', 'project-b'))
      }
      return Promise.resolve(emptyDetailResponse(path))
    })

    mountComposable()
    await flushPromises()
    workspaceState.currentProjectId = 'project-b'
    runsData.value = [run('run-a', 'project-b')]
    await flushPromises()

    expect(page.commandEvents.value.map((event) => event.id)).toEqual(['project-b-command'])

    projectACommands.resolve(commandGroups('project-a-command'))
    await flushPromises()

    expect(page.commandEvents.value.map((event) => event.id)).toEqual(['project-b-command'])
  })

  it('does not commit a preview after another artifact is selected', async () => {
    const previewA = deferred<ArtifactPreview>()
    const outputs = [artifact('output-a'), artifact('output-b', 'diagnostic')]
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path.includes('/output-a/preview-tail')) return previewA.promise
      if (path.includes('/output-b/preview-tail')) {
        return Promise.resolve(preview('output-b', 'diagnostic B'))
      }
      return Promise.resolve(emptyDetailResponse(path, outputs))
    })

    mountComposable()
    await flushPromises()
    expect(page.stdoutArtifactId.value).toBe('output-a')
    expect(page.stdoutContent.value).toBe('')

    page.selectTerminalOutput('output-b')
    await flushPromises()
    expect(page.stdoutArtifactId.value).toBe('output-b')
    expect(page.stdoutContent.value).toBe('diagnostic B')
    expect(page.stdoutDownloadUrl.value).toContain('/artifacts/output-b/content')

    previewA.resolve(preview('output-a', 'stale A'))
    await flushPromises()
    expect(page.stdoutArtifactId.value).toBe('output-b')
    expect(page.stdoutContent.value).toBe('diagnostic B')
  })

  it('clears selection when the runId query is removed', async () => {
    mocks.api.mockImplementation((_method: string, path: string) =>
      Promise.resolve(emptyDetailResponse(path)),
    )
    mountComposable()
    await flushPromises()

    routeState.query.runId = undefined
    await flushPromises()

    expect(page.selectedRunId.value).toBeNull()
    expect(page.selectedRun.value).toBeNull()
    expect(page.commandEvents.value).toEqual([])
  })

  it('R1 commits assets and logs before commands settle, and retries only failed commands', async () => {
    vi.useFakeTimers()
    const pendingCommands = deferred<TaskRunEventGroup<TaskRunCommandEvent>[]>()
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path.includes('/command-events')) return pendingCommands.promise
      if (path.includes('/preview-tail'))
        return Promise.resolve(preview('output-a', 'readable log'))
      return Promise.resolve(emptyDetailResponse(path, [artifact('output-a')]))
    })
    mountComposable()
    await flushPromises()
    expect(page.isLoadingCommands.value).toBe(true)
    expect(page.commandsLoaded.value).toBe(false)
    expect(page.artifactsLoaded.value).toBe(true)
    expect(page.stdoutContent.value).toBe('readable log')

    pendingCommands.reject(new ApiError('private server body', 403, {}))
    await flushPromises()
    expect(page.commandsError.value).toContain('HTTP 403')
    expect(page.commandsError.value).not.toContain('private server body')
    await vi.advanceTimersByTimeAsync(8000)
    expect(
      mocks.api.mock.calls.filter(([, path]) => path.includes('/command-events')),
    ).toHaveLength(1)
    expect(page.stdoutContent.value).toBe('readable log')

    mocks.api.mockClear().mockResolvedValue(commandGroups('retried-event'))
    page.refetchCommands()
    page.refetchCommands()
    await flushPromises()
    expect(mocks.api).toHaveBeenCalledExactlyOnceWith(
      'GET',
      '/projects/project-a/task-runs/command-events?task_run_id=run-a',
    )
    expect(page.commandsError.value).toBe('')
    expect(page.commandEvents.value.map((event) => event.id)).toEqual(['retried-event'])
  })

  it('R1 isolates initial and stale asset failures from readable commands', async () => {
    let assetFailure = true
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path.includes('/command-events')) return Promise.resolve(commandGroups('command-a'))
      if (path.includes('/preview-tail'))
        return Promise.resolve(preview('output-a', 'saved output'))
      if (assetFailure) return Promise.reject(new ApiError('missing', 404, {}))
      return Promise.resolve([artifact('output-a')])
    })
    mountComposable()
    await flushPromises()
    expect(page.commandsLoaded.value).toBe(true)
    expect(page.commandEvents.value).toHaveLength(1)
    expect(page.artifactsLoaded.value).toBe(false)
    expect(page.artifactsError.value).toContain('HTTP 404')
    assetFailure = false
    page.refetchArtifacts()
    await flushPromises()
    expect(page.stdoutContent.value).toBe('saved output')
    assetFailure = true
    page.refetchArtifacts()
    await flushPromises()
    expect(page.artifactsLoaded.value).toBe(true)
    expect(page.artifacts.value).toHaveLength(1)
    expect(page.artifactsError.value).toContain('HTTP 404')
    expect(page.commandEvents.value).toHaveLength(1)
    expect(page.stdoutContent.value).toBe('saved output')
  })

  it('R1 preserves preview failures, deduplicates reads and resumes transient failures on the existing interval', async () => {
    vi.useFakeTimers()
    const refresh = deferred<ArtifactPreview>()
    let previewCalls = 0
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path.includes('/preview-tail')) {
        previewCalls += 1
        return previewCalls === 1
          ? Promise.resolve({ ...preview('output-a', 'saved output'), truncated: true })
          : refresh.promise
      }
      return Promise.resolve(emptyDetailResponse(path, [artifact('output-a')]))
    })
    mountComposable()
    await flushPromises()
    page.autoScrollTerminal.value = false
    const screen = document.createElement('div')
    screen.scrollTop = 120
    page.terminalScreen.value = screen
    page.refetchTerminal()
    page.refetchTerminal()
    await vi.advanceTimersByTimeAsync(8000)
    expect(previewCalls).toBe(2)
    refresh.reject(new ApiError('unavailable', 503, {}))
    await flushPromises()
    expect(page.stdoutContent.value).toBe('saved output')
    expect(page.terminalPreviewLoaded.value).toBe(true)
    expect(page.terminalPreviewError.value).toContain('HTTP 503')
    expect(page.previewTruncationMessage.value).not.toBe('')
    expect(page.stdoutDownloadUrl.value).toContain('/output-a/content')
    expect(screen.scrollTop).toBe(120)
    await vi.advanceTimersByTimeAsync(4000)
    expect(previewCalls).toBe(3)
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path.includes('/preview-tail')) return Promise.resolve(preview('output-a', 'recovered'))
      return Promise.resolve(emptyDetailResponse(path, [artifact('output-a')]))
    })
    await vi.advanceTimersByTimeAsync(4000)
    expect(page.terminalPreviewError.value).toBe('')
    expect(page.stdoutContent.value).toBe('recovered')

    mocks.api.mockClear().mockResolvedValue(preview('output-a', ''))
    page.refetchTerminal()
    await flushPromises()
    expect(mocks.api).toHaveBeenCalledExactlyOnceWith(
      'GET',
      '/projects/project-a/artifacts/output-a/preview-tail',
    )
    expect(page.terminalPreviewError.value).toBe('')
    expect(page.terminalPreviewLoaded.value).toBe(true)
    expect(page.stdoutContent.value).toBe('')

    mocks.api.mockRejectedValue(new ApiError('missing', 404, {}))
    page.selectTerminalOutput('output-b')
    await flushPromises()
    expect(page.terminalPreviewLoaded.value).toBe(false)
    expect(page.previewTruncationMessage.value).toBe('')
    expect(page.stdoutDownloadUrl.value).toContain('/output-b/content')
    expect(page.terminalPreviewError.value).toContain('HTTP 404')
    mocks.api.mockClear()
    await vi.advanceTimersByTimeAsync(8000)
    expect(mocks.api.mock.calls.some(([, path]) => path.includes('/preview-tail'))).toBe(false)
  })

  it('discards pending preview responses after unmount', async () => {
    const pendingPreview = deferred<ArtifactPreview>()
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path.includes('/preview-tail')) return pendingPreview.promise
      return Promise.resolve(emptyDetailResponse(path, [artifact('output-a')]))
    })
    mountComposable()
    await flushPromises()

    wrapper?.unmount()
    wrapper = null
    pendingPreview.resolve(preview('output-a', 'late output'))
    await flushPromises()

    expect(page.stdoutContent.value).toBe('')
  })
})

describe('useRunsPage business context', () => {
  it('searches local Intent and Fact context without per-row requests', async () => {
    routeState.query = {}
    workspaceState.projectDetail = {
      project: { id: 'project-a' } as ProjectDetail['project'],
      intents: [
        {
          id: 'intent-a',
          project_id: 'project-a',
          from: ['goal'],
          to: 'fact-a',
          description: '验证管理端会话是否可被低权限用户复用',
          creator: 'reason',
          priority: 2,
          assessment_role: 'verifier',
          created_at: '2026-08-20T09:59:00.000Z',
          concluded_at: '2026-08-20T10:01:00.000Z',
          conclusion: 'completed',
        },
        {
          id: 'intent-b',
          project_id: 'project-a',
          from: ['goal'],
          description: '枚举公开 API 入口',
          creator: 'reason',
          priority: 0,
          assessment_role: 'discovery',
          created_at: '2026-08-20T09:58:00.000Z',
          concluded_at: '2026-08-20T10:02:00.000Z',
          conclusion: 'abandoned',
          conclusion_reason: 'authorization_expired',
        },
      ],
      facts: [
        {
          id: 'fact-a',
          project_id: 'project-a',
          description: '未发现跨角色会话复用，证据已接受',
          task_run_id: 'run-a',
          creator: 'explore',
          created_at: '2026-08-20T10:01:00.000Z',
        },
      ],
      hints: [],
      targets: [],
      resources: [],
      library_bindings: [],
    }
    runsData.value = [
      { ...run('run-a', 'project-a', 'succeeded'), intent_id: 'intent-a' },
      { ...run('run-b', 'project-a', 'succeeded'), intent_id: 'intent-b' },
    ]

    mountComposable()
    await flushPromises()

    expect(page.runBusinessTitle(runsData.value[0])).toContain('验证管理端会话')
    expect(page.runRoleLabel(runsData.value[0])).toBe('独立复核')
    expect(page.runPriorityLabel(runsData.value[0])).toBe('优先')
    expect(page.runResultText(runsData.value[0])).toContain('证据已接受')
    expect(page.workTimeline.value).toHaveLength(2)
    expect(page.linkedIntentOutcomeText(runsData.value[1])).toBe(
      '工作已停止：authorization_expired',
    )

    const siblingRun = {
      ...run('run-sibling', 'project-a', 'succeeded'),
      intent_id: 'intent-a',
    }
    expect(page.runResultText(siblingRun)).toBe('执行结束，结果未确认')

    const searches: Array<[string, string]> = [
      ['管理端会话', 'run-a'],
      ['独立复核', 'run-a'],
      ['优先级 2', 'run-a'],
      ['证据已接受', 'run-a'],
      ['authorization_expired', 'run-b'],
      ['发现与验证', 'run-b'],
    ]
    for (const [query, expectedRunId] of searches) {
      page.searchQuery.value = query
      expect(page.filteredRuns.value.map((item) => item.id)).toEqual([expectedRunId])
    }
    expect(
      mocks.api.mock.calls.every(
        ([, path]) => path.includes('/hook-events') || path.startsWith('/reports?'),
      ),
    ).toBe(true)

    const intent = workspaceState.projectDetail.intents[0]!
    intent.conclusion = 'failed'
    intent.conclusion_reason = '证据不足'
    expect(page.linkedIntentOutcomeText(runsData.value[0])).toBe('工作未完成：证据不足')
    expect(page.runResultText(runsData.value[0])).toContain('证据已接受')
    intent.conclusion = 'abandoned'
    expect(page.workTimeline.value.some((work) => work.status === '已停止')).toBe(true)
    intent.conclusion = 'completed'
    intent.to = null
    expect(page.runResultText(runsData.value[0])).toBe('执行结束，结果未确认')
    expect(page.runResultText(run('no-intent', 'project-a', 'succeeded'))).toBe(
      '执行结束，结果未确认',
    )
    for (const status of ['failed', 'timed_out', 'cancelled']) {
      expect(
        page.runResultText({
          ...run('failed-run', 'project-a', status),
          error_message: '原始故障原因',
        }),
      ).toBe('本次执行未完成，结果未确认')
    }
  })
})

describe('useRunsPage timeline final reads', () => {
  it('补读终态撞上在途的工具、命令、资产与日志请求，报告跨页不漏接受结果', async () => {
    const pendingHooks = deferred<unknown>()
    const pendingCommands = deferred<unknown>()
    const pendingAssets = deferred<unknown>()
    const pendingPreview = deferred<ArtifactPreview>()
    const pendingReports = deferred<unknown>()
    runsData.value = [{ ...run('run-a'), task_type: 'report' }]
    let hooks = 0,
      commands = 0,
      assets = 0,
      previews = 0,
      reports = 0
    mocks.loadProject.mockClear().mockResolvedValue(undefined)
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path.includes('/hook-events'))
        return ++hooks === 1
          ? pendingHooks.promise
          : Promise.resolve([{ task_run_id: 'run-a', events: [{ id: 'final-hook' }] }])
      if (path.includes('/command-events'))
        return ++commands === 1
          ? pendingCommands.promise
          : Promise.resolve(commandGroups('final-command'))
      if (path.includes('/artifacts?'))
        return ++assets === 2 ? pendingAssets.promise : Promise.resolve([artifact('output-a')])
      if (path.includes('/preview-tail'))
        return ++previews === 1
          ? pendingPreview.promise
          : Promise.resolve(preview('output-a', 'final output'))
      if (path.startsWith('/reports?')) {
        reports += 1
        if (reports === 1) return pendingReports.promise
        return Promise.resolve({
          items: path.includes('page=2')
            ? [{ artifact_id: 'accepted-report', task_run_id: 'run-a' }]
            : [],
          total: 101,
          page_size: 100,
        })
      }
      throw new Error(path)
    })
    mountComposable()
    await flushPromises()
    page.refetchArtifacts()
    runsData.value = [{ ...runsData.value[0]!, status: 'failed' }]
    await flushPromises()
    expect(mocks.loadProject).toHaveBeenCalledWith('project-a')
    pendingHooks.resolve([{ task_run_id: 'run-a', events: [] }])
    pendingCommands.resolve(commandGroups('stale-command'))
    pendingAssets.resolve([])
    pendingPreview.resolve(preview('output-a', 'stale output'))
    pendingReports.resolve({ items: [], total: 0, page_size: 100 })
    await flushPromises()
    expect(hooks).toBe(2)
    expect(commands).toBe(2)
    expect(assets).toBe(3)
    expect(page.hookReads.value['run-a']?.events[0]?.id).toBe('final-hook')
    expect(page.commandEvents.value[0]?.id).toBe('final-command')
    expect(page.stdoutContent.value).toBe('final output')
    expect(page.workTimeline.value[0]?.results[0]?.artifactId).toBe('accepted-report')
    expect(page.workTimeline.value[0]?.status).toBe('未完成 · 已有部分产出')
  })

  it('工具刷新失败保留同身份旧内容，切项目隔离迟到响应', async () => {
    routeState.query = {}
    runsData.value = [run('run-a')]
    let response: Promise<unknown> = Promise.resolve([
      { task_run_id: 'run-a', events: [{ id: 'saved' }] },
    ])
    mocks.api.mockImplementation(() => response)
    mountComposable()
    await flushPromises()
    response = Promise.reject(new ApiError('private body', 503, {}))
    page.refetchHooks('run-a')
    await flushPromises()
    expect(page.hookReadError('run-a')).toContain('刷新失败，显示上次观测')
    expect(page.hookReads.value['run-a']?.events[0]?.id).toBe('saved')
    const late = deferred<unknown>()
    response = late.promise
    page.refetchHooks('run-a')
    workspaceState.currentProjectId = 'project-b'
    response = Promise.resolve([{ task_run_id: 'run-a', events: [{ id: 'new-project' }] }])
    runsData.value = [run('run-a', 'project-b')]
    await flushPromises()
    late.resolve([{ task_run_id: 'run-a', events: [{ id: 'old-project' }] }])
    await flushPromises()
    expect(page.hookReads.value['run-a']?.events[0]?.id).toBe('new-project')
  })
})

describe('useRunsPage polling selection', () => {
  it('refreshes list and active detail twice without foreground loading, and retains failures until retry succeeds', async () => {
    vi.useFakeTimers()
    const firstPoll = deferred<TaskRunEventGroup<TaskRunCommandEvent>[]>()
    let detailVersion = 0
    let shouldFail = false
    mocks.refetchRuns.mockImplementation(() => {
      runsData.value = runsData.value.map((item) =>
        item.id === 'run-a' ? { ...item, worker_name: `worker-${detailVersion + 1}` } : item,
      )
      return Promise.resolve()
    })
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path.includes('/command-events?task_run_id=run-a')) {
        detailVersion += 1
        if (detailVersion === 2) return firstPoll.promise
        if (shouldFail) return Promise.reject(new Error('network unavailable'))
        return Promise.resolve(commandGroups(`command-${detailVersion}`))
      }
      if (path.includes('/preview-tail')) {
        return Promise.resolve(preview('output-a', `output-${detailVersion}`))
      }
      return Promise.resolve(emptyDetailResponse(path, [artifact('output-a')]))
    })

    mountComposable()
    await flushPromises()
    expect(page.stdoutContent.value).toBe('output-1')

    await vi.advanceTimersByTimeAsync(4000)
    expect(detailVersion).toBe(2)
    expect(page.isLoadingRunDetails.value).toBe(false)
    expect(page.stdoutContent.value).toBe('output-2')
    firstPoll.resolve(commandGroups('command-2'))
    await flushPromises()
    expect(page.stdoutContent.value).toBe('output-2')
    expect(page.selectedRun.value?.worker_name).toBe('worker-2')

    await vi.advanceTimersByTimeAsync(4000)
    await flushPromises()
    expect(page.commandEvents.value.map((event) => event.id)).toEqual(['command-3'])
    expect(page.stdoutContent.value).toBe('output-3')
    expect(page.selectedRun.value?.worker_name).toBe('worker-3')
    expect(mocks.refetchRuns).toHaveBeenCalledTimes(2)
    expect(page.isLoadingRunDetails.value).toBe(false)

    shouldFail = true
    page.refetchCommands()
    expect(page.isLoadingCommands.value).toBe(true)
    expect(page.stdoutContent.value).toBe('output-3')
    await flushPromises()
    expect(page.commandsError.value).toContain('网络或响应读取异常')
    await vi.advanceTimersByTimeAsync(4000)
    await flushPromises()
    expect(page.commandsError.value).toContain('网络或响应读取异常')
    expect(page.isLoadingRunDetails.value).toBe(false)

    shouldFail = false
    await vi.advanceTimersByTimeAsync(4000)
    await flushPromises()
    expect(page.commandsError.value).toBe('')
    expect(page.stdoutContent.value).toBe(`output-${detailVersion}`)
  })

  it('allows an initial detail request spanning poll intervals to commit', async () => {
    vi.useFakeTimers()
    const pendingCommands = deferred<TaskRunEventGroup<TaskRunCommandEvent>[]>()
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path.includes('/command-events?task_run_id=run-a')) return pendingCommands.promise
      return Promise.resolve(emptyDetailResponse(path))
    })

    mountComposable()
    await flushPromises()
    expect(page.isLoadingRunDetails.value).toBe(true)

    await vi.advanceTimersByTimeAsync(8000)
    await flushPromises()
    expect(
      mocks.api.mock.calls.filter(([, path]) =>
        String(path).includes('/command-events?task_run_id=run-a'),
      ),
    ).toHaveLength(1)

    pendingCommands.resolve(commandGroups('slow-event'))
    await flushPromises()

    expect(page.commandEvents.value.map((event) => event.id)).toEqual(['slow-event'])
    expect(page.isLoadingRunDetails.value).toBe(false)
  })

  it('retains a manual output across polling and falls back only after it disappears', async () => {
    vi.useFakeTimers()
    let outputs = [artifact('output-a'), artifact('output-b')]
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path.includes('/preview-tail')) {
        const artifactId = path.includes('output-a') ? 'output-a' : 'output-b'
        return Promise.resolve(preview(artifactId, artifactId))
      }
      return Promise.resolve(emptyDetailResponse(path, outputs))
    })

    mountComposable()
    await flushPromises()
    expect(page.stdoutArtifactId.value).toBe('output-b')

    page.selectTerminalOutput('output-a')
    await flushPromises()
    expect(page.stdoutArtifactId.value).toBe('output-a')

    await vi.advanceTimersByTimeAsync(4000)
    await flushPromises()
    expect(page.stdoutArtifactId.value).toBe('output-a')

    outputs = [artifact('output-b')]
    await vi.advanceTimersByTimeAsync(4000)
    await flushPromises()
    expect(page.stdoutArtifactId.value).toBe('output-b')
  })
})
