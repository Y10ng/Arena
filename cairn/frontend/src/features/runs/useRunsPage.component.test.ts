import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, reactive, ref, unref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ArtifactPreview } from '@/api/client'
import type { ProjectDetail } from '@/api/types'
import type { RunArtifact, TaskRun, TaskRunCommandEvent, TaskRunEventGroup } from './runTypes'
import { useRunsPage } from './useRunsPage'

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
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

vi.mock('@/api/client', () => ({
  api: mocks.api,
}))

vi.mock('@/stores/workspace', () => ({
  useWorkspaceStore: () => mocks.workspaceStore,
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
          isError: ref(false),
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
  if (path.includes('/command-events')) {
    return [{ task_run_id: runIdFromPath(path), events: [] }]
  }
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
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.useRealTimers()
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
    expect(mocks.api.mock.calls.map(([, path]) => path)).toEqual(detailPaths)
    expect(page.activeDetailTab.value).toBe('terminal')
    expect(page.detailTabs.map((tab) => tab.value)).toEqual(['terminal', 'commands', 'artifacts'])

    page.activeDetailTab.value = 'commands'
    uiState.globalRefreshKey += 1
    await flushPromises()

    expect(mocks.refetchRuns).toHaveBeenCalledOnce()
    expect(mocks.api.mock.calls.map(([, path]) => path)).toEqual([...detailPaths, ...detailPaths])
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

  it('surfaces detail failures and retries through the guarded loader', async () => {
    let shouldFail = true
    mocks.api.mockImplementation((_method: string, path: string) => {
      if (path.includes('/command-events')) {
        if (shouldFail) return Promise.reject(new Error('network unavailable'))
        return Promise.resolve(commandGroups('retried-event'))
      }
      return Promise.resolve(emptyDetailResponse(path))
    })

    mountComposable()
    await flushPromises()
    expect(page.runDetailsError.value).toContain('network unavailable')

    shouldFail = false
    page.refetchTerminal()
    await flushPromises()
    expect(page.runDetailsError.value).toBe('')
    expect(page.commandEvents.value.map((event) => event.id)).toEqual(['retried-event'])
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
    expect(page.runCardDiagnostic(runsData.value[0])).toBe('')
    expect(page.runCardDiagnostic(runsData.value[1])).toBe('')
    expect(page.linkedIntentOutcomeText(runsData.value[1])).toBe(
      '工作已停止：authorization_expired',
    )

    const siblingRun = {
      ...run('run-sibling', 'project-a', 'succeeded'),
      intent_id: 'intent-a',
    }
    expect(page.runResultText(siblingRun)).toBe('执行成功；已接受结果属于关联运行')
    expect(page.runCardDiagnostic(siblingRun)).toBe('')

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
    expect(mocks.api).not.toHaveBeenCalled()

    const intent = workspaceState.projectDetail.intents[0]!
    intent.conclusion = 'failed'
    intent.conclusion_reason = '证据不足'
    expect(page.runCardDiagnostic(runsData.value[0])).toBe('')
    expect(page.linkedIntentOutcomeText(runsData.value[0])).toBe('结果未接受：证据不足')
    expect(page.runResultText(runsData.value[0])).toContain('证据已接受')

    intent.conclusion = 'abandoned'
    intent.conclusion_reason = 'authorization_expired'
    expect(page.runCardDiagnostic(runsData.value[0])).toBe('')
    expect(page.linkedIntentOutcomeText(runsData.value[0])).toBe(
      '工作已停止：authorization_expired',
    )

    intent.conclusion = null
    intent.conclusion_reason = null
    expect(page.runCardDiagnostic(runsData.value[0])).toBe('')
    expect(page.linkedIntentOutcomeText(runsData.value[0])).toBe('')

    intent.conclusion = 'completed'
    intent.to = null
    expect(page.runCardDiagnostic(runsData.value[0])).toBe('')
    expect(page.runResultText(runsData.value[0])).toBe('执行成功，未返回可展示的接受结果')
    expect(page.runCardDiagnostic(run('no-intent', 'project-a', 'succeeded'))).toBe('')
    expect(page.runResultText(run('no-intent', 'project-a', 'succeeded'))).toBe('执行成功')

    for (const status of ['failed', 'timed_out', 'cancelled']) {
      expect(
        page.runCardDiagnostic({
          ...run('failed-run', 'project-a', status),
          error_message: '原始故障原因',
        }),
      ).toBe('原始故障原因')
    }
    expect(page.runCardDiagnostic(run('queued-run', 'project-a', 'queued'))).toBe('排队中')
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
    expect(page.stdoutContent.value).toBe('output-1')
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
    page.refetchTerminal()
    expect(page.isLoadingRunDetails.value).toBe(true)
    expect(page.stdoutContent.value).toBe('output-3')
    await flushPromises()
    expect(page.runDetailsError.value).toContain('network unavailable')
    await vi.advanceTimersByTimeAsync(4000)
    await flushPromises()
    expect(page.runDetailsError.value).toContain('network unavailable')
    expect(page.isLoadingRunDetails.value).toBe(false)

    shouldFail = false
    page.refetchTerminal()
    await flushPromises()
    expect(page.runDetailsError.value).toBe('')
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
