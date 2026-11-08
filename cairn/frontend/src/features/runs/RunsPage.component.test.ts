import { flushPromises, mount } from '@vue/test-utils'
import { computed, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import RunsPage from './RunsPage.vue'
import type { TaskRun, TaskRunCommandEvent } from './runTypes'
import { buildWorkTimeline, type ToolCall } from './runTimelineModel'
import type { Fact } from '@/api/types'

const mocks = vi.hoisted(() => ({
  useRunsPage: vi.fn(),
  useQuery: vi.fn(),
  selectTerminalOutput: vi.fn(),
  api: vi.fn(),
}))

vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  api: mocks.api,
}))
vi.mock('@tanstack/vue-query', () => ({ useQuery: mocks.useQuery }))

vi.mock('./useRunsPage', () => ({
  useRunsPage: mocks.useRunsPage,
}))

const detailTabs = [
  { label: '输出', value: 'terminal' },
  { label: '工具', value: 'tools' },
  { label: '命令', value: 'commands' },
  { label: '文件', value: 'artifacts' },
]

function createRunsPageState() {
  const activeWorkView = ref('execution')
  const activeDetailTab = ref('terminal')
  const selectedRun = ref({
    id: 'run-a',
    project_id: 'project-a',
    session_id: 'session-a',
    execution_profile: 'default',
    task_type: 'explore',
    status: 'succeeded',
    worker_name: 'claude-code',
    worker_type: 'claude_code',
    error_type: null,
    error_message: null,
  })

  const filteredRuns = ref([
    {
      ...selectedRun.value,
      runtime_context: {},
      started_at: '2026-08-20T10:00:00.000Z',
    } as TaskRun,
  ])
  const acceptedResult = ref('未发现跨角色会话复用，证据已接受')
  const workTimeline = computed(() =>
    buildWorkTimeline(
      filteredRuns.value,
      [],
      [{ id: 'f', task_run_id: 'run-a', description: acceptedResult.value } as Fact],
      new Set(filteredRuns.value.map((run) => run.id)),
    ),
  )
  const selectedWork = computed(
    () =>
      workTimeline.value.find((work) =>
        work.runs.some((run) => run.id === selectedRun.value?.id),
      ) || null,
  )
  return {
    activeWorkView,
    workTimeline,
    selectedWork,
    selectWork: vi.fn(),
    workTitle: vi.fn(() => '验证管理端会话隔离'),
    toolCalls: vi.fn(() => [] as ToolCall[]),
    hookReads: ref({}),
    hookReadError: vi.fn(() => ''),
    hookObservationIncomplete: vi.fn(() => false),
    refetchHooks: vi.fn(),
    refreshTimeline: vi.fn(),
    reportsLoaded: ref(false),
    resultReadState: ref(''),
    reportsError: ref(''),
    loadReports: vi.fn(),
    activeDetailTab,
    artifacts: ref([]),
    autoScrollTerminal: ref(true),
    captureTruncationMessage: ref(''),
    chatRunNotice: ref(false),
    commandEvents: ref<TaskRunCommandEvent[]>([]),
    detailTabs,
    filteredRuns,
    filtersActive: computed(() => false),
    formatRelativeTime: vi.fn(() => '刚刚'),
    formatTimeShort: vi.fn(() => '12:00:00'),
    isFetchingRuns: ref(false),
    isLoadingRunDetails: ref(false),
    isLoadingRuns: ref(false),
    isLoadingTerminalPreview: ref(false),
    linkedIntentOutcomeText: vi.fn(() => ''),
    openProjectAgent: vi.fn(),
    previewArtifact: vi.fn(),
    previewTruncationMessage: ref(''),
    refetchRuns: vi.fn(),
    refetchTerminal: vi.fn(),
    runBusinessTitle: vi.fn(() => '验证管理端会话隔离'),
    runCardDiagnostic: vi.fn(() => ''),
    commandsLoaded: ref(true),
    commandsError: ref(''),
    isLoadingCommands: ref(false),
    artifactsLoaded: ref(true),
    artifactsError: ref(''),
    isLoadingArtifacts: ref(false),
    refetchCommands: vi.fn(),
    refetchArtifacts: vi.fn(),
    terminalPreviewLoaded: ref(true),
    runDurationText: vi.fn(() => '1分30秒'),
    runIntent: vi.fn(() => ({ priority: 2 })),
    runPriorityLabel: vi.fn(() => '优先'),
    runReferenceTime: vi.fn(() => '2026-08-20T10:00:00.000Z'),
    runResultText: vi.fn(() => '未发现跨角色会话复用，证据已接受'),
    runRoleLabel: vi.fn(() => '独立复核'),
    runsError: ref<unknown>(null),
    runsInitialError: ref(false),
    runsStaleError: ref(false),
    searchQuery: ref(''),
    acceptedResult,
    selectedRun,
    selectedRunId: ref('run-a'),
    selectRun: vi.fn(),
    selectTerminalOutput: mocks.selectTerminalOutput,
    statusFilter: ref('all'),
    statusText: vi.fn((status: string) => status),
    stdoutArtifactId: ref('output-a'),
    stdoutContent: ref('completed output'),
    stdoutDownloadLabel: ref('下载日志'),
    stdoutDownloadTitle: ref('下载服务端已保存的完整日志产物'),
    stdoutDownloadUrl: ref('/projects/project-a/artifacts/output-a/content'),
    terminalOutputs: ref([
      {
        id: 'output-a',
        label: 'Explore 执行 · 标准输出',
        phase: 'explore_execute',
        stream: 'stdout',
        successful: true,
        timedOut: false,
      },
      {
        id: 'output-b',
        label: 'Explore 收尾 · 诊断',
        phase: 'explore_conclude',
        stream: 'diagnostic',
        successful: true,
        timedOut: false,
      },
    ]),
    terminalPreviewError: ref(''),
    terminalRecovered: ref(false),
    terminalScreen: ref<HTMLElement | null>(null),
  }
}

function commandEvent(id: string, overrides: Partial<TaskRunCommandEvent>): TaskRunCommandEvent {
  return {
    id,
    task_run_id: 'run-a',
    project_id: 'project-a',
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
    ...overrides,
  }
}

function tabByName(name: string) {
  const tab = [...document.body.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(
    (candidate) => candidate.textContent?.includes(name),
  )
  if (!tab) throw new Error(`找不到运行详情页签：${name}`)
  return tab
}

function selectTab(name: string) {
  tabByName(name).dispatchEvent(
    new MouseEvent('mousedown', {
      bubbles: true,
      button: 0,
    }),
  )
}

beforeEach(() => {
  mocks.api.mockReset()
  mocks.useQuery.mockReset().mockReturnValue({ data: ref(undefined), isError: ref(false) })
  mocks.selectTerminalOutput.mockReset()
  mocks.useRunsPage.mockReset().mockReturnValue(createRunsPageState())
  document.body.innerHTML = ''
})

describe('RunsPage details', () => {
  it('offers the project Agent entry for an excluded chat link without showing another run detail', async () => {
    const state = createRunsPageState()
    state.chatRunNotice.value = true
    state.selectedRun.value = null as never
    state.selectedRunId.value = null as never
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage)

    const notice = wrapper.get('.runs-stale-error')
    expect(notice.text()).toContain('此记录属于 Agent 对话')
    expect(notice.get('button').text()).toBe('打开项目 Agent')
    await notice.get('button').trigger('click')
    expect(state.openProjectAgent).toHaveBeenCalledOnce()
    expect(wrapper.get('.run-details-empty').text()).toContain('选择一项工作')
    expect(wrapper.findAll('.work-select')).toHaveLength(1)
    expect(mocks.useQuery.mock.calls[0]![0].enabled.value).toBe(false)
    wrapper.unmount()
  })

  it('queries and polls run traffic, hiding only a healthy completed synchronization', async () => {
    const scopes = Array.from({ length: 101 }, (_, index) => ({
      id: `scope-${String(index).padStart(3, '0')}`,
      task_run_id: index === 100 ? 'run-a' : `other-run-${index}`,
      status: {
        state: 'stopped',
        failure_code: 'traffic_capture_stalled',
        control_stopped: true,
        test_stopped: true,
        collector_stopped: true,
        upload_complete: false,
      },
    }))
    expect(scopes.slice(0, 100).some((scope) => scope.task_run_id === 'run-a')).toBe(false)
    mocks.api.mockImplementation(async (_method, path) => {
      const url = new URL(path, 'http://synthetic.invalid')
      expect(url.pathname).toBe('/projects/project-a/traffic/scopes/status')
      const runId = url.searchParams.get('task_run_id')
      const matching =
        runId === null ? scopes : scopes.filter((scope) => scope.task_run_id === runId)
      return {
        items: matching.slice(0, Number(url.searchParams.get('limit'))),
        has_more: matching.length > 100,
        next_cursor: null,
      }
    })
    const data = ref<{ items: typeof scopes }>()
    const isError = ref(false)
    mocks.useQuery.mockReturnValue({ data, isError })
    const wrapper = mount(RunsPage)
    const options = mocks.useQuery.mock.calls[0]![0]
    const signal = new AbortController().signal
    data.value = await options.queryFn({ signal })
    await flushPromises()
    expect(mocks.api).toHaveBeenCalledWith(
      'GET',
      '/projects/project-a/traffic/scopes/status?limit=100&task_run_id=run-a',
      undefined,
      signal,
    )
    expect(data.value!.items).toHaveLength(1)
    const query = { state: { data: data.value, status: 'success' } }
    const status = data.value!.items[0]!.status
    expect(wrapper.get('[aria-label="本次运行流量状态"]').text()).toBe(
      '采集已停止，正在同步已有流量',
    )
    expect(options.refetchInterval(query)).toBe(3000)
    status.state = 'blocked'
    await flushPromises()
    expect(wrapper.get('[aria-label="本次运行流量状态"]').text()).toBe('同步失败')
    expect(options.refetchInterval(query)).toBe(3000)
    status.upload_complete = true
    await flushPromises()
    expect(wrapper.find('[aria-label="本次运行流量状态"]').exists()).toBe(false)
    expect(wrapper.find('.run-context-statuses').exists()).toBe(false)
    expect(options.refetchInterval(query)).toBe(false)
    isError.value = true
    await flushPromises()
    expect(wrapper.get('[aria-label="本次运行流量状态"]').text()).toBe(
      '同步完成 · 状态刷新失败，显示上次观测重试',
    )
    expect(wrapper.get('[aria-label="本次运行流量状态"]').classes()).toContain(
      'run-inline-state',
    )
    for (const key of ['control_stopped', 'test_stopped', 'collector_stopped'] as const) {
      status[key] = false
      expect(options.refetchInterval(query)).toBe(3000)
      status[key] = true
    }
    wrapper.unmount()
  })

  it('shows traffic query failures without a cached scope and retains stale observations with manual retry', async () => {
    const data = ref<{ items: { task_run_id: string; status: null }[] }>()
    const isError = ref(true)
    const refetch = vi.fn()
    mocks.useQuery.mockReturnValue({ data, isError, refetch })
    const wrapper = mount(RunsPage)
    const alert = wrapper.get('[aria-label="本次运行流量状态"]')
    expect(alert.classes()).toContain('run-inline-state')
    expect(alert.classes()).not.toContain('ui-alert')
    expect(alert.text()).toContain('流量状态查询失败，当前状态未知')
    expect(alert.text()).not.toContain('显示上次观测')
    await alert.get('button').trigger('click')
    expect(refetch).toHaveBeenCalledOnce()
    const options = mocks.useQuery.mock.calls[0]![0]
    expect(options.refetchInterval({ state: { status: 'error' } })).toBe(false)
    data.value = { items: [{ task_run_id: 'run-a', status: null }] }
    await flushPromises()
    expect(alert.text()).toContain('采集状态未上报 · 状态刷新失败，显示上次观测')
    isError.value = false
    await flushPromises()
    expect(alert.text()).toBe('采集状态未上报')
    wrapper.unmount()
  })

  it('keeps the work index and reader visible while result sources and detail tabs stay in place', async () => {
    const state = createRunsPageState()
    state.autoScrollTerminal.value = false
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()
    const terminal = wrapper.get('.terminal-screen').element
    terminal.scrollTop = 120
    expect(wrapper.get('.runs-index').isVisible()).toBe(true)
    expect(wrapper.get('.run-details-panel').isVisible()).toBe(true)
    expect(wrapper.find('.run-back').exists()).toBe(false)

    await wrapper.get('input[aria-label="搜索工作元数据或结果"]').setValue('metadata query')
    await wrapper.get('.work-select').trigger('click')
    expect(state.selectWork).toHaveBeenCalledWith(expect.objectContaining({ key: 'run:run-a' }))
    expect(state.searchQuery.value).toBe('metadata query')

    selectTab('结果')
    await flushPromises()
    expect(wrapper.get('.results-view').isVisible()).toBe(true)
    expect(wrapper.get('.execution-view').isVisible()).toBe(false)
    await wrapper.get('.work-result-source button').trigger('click')
    expect(state.selectRun).toHaveBeenCalledWith('run-a')
    selectTab('执行记录')
    await flushPromises()
    expect(wrapper.get('.terminal-screen').element).toBe(terminal)
    expect(terminal.scrollTop).toBe(120)
    expect(wrapper.find('.runs-detail-tabs .ui-tabs__actions a[download]').exists()).toBe(true)

    state.activeDetailTab.value = 'commands'
    await flushPromises()
    expect(wrapper.get('.terminal-screen').element).toBe(terminal)
    expect(wrapper.get('.terminal-screen').isVisible()).toBe(false)
    state.activeDetailTab.value = 'terminal'
    await flushPromises()
    expect(wrapper.get('.terminal-screen').element).toBe(terminal)
    expect(terminal.scrollTop).toBe(120)
    expect(state.autoScrollTerminal.value).toBe(false)
    wrapper.unmount()
  })

  it('相同轮询不报新进展，新增节点不抢工作索引滚动', async () => {
    const state = createRunsPageState()
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    const viewport = wrapper.get('.runs-timeline-scroll').element
    viewport.scrollTop = 120
    state.filteredRuns.value = state.filteredRuns.value.map((run) => ({ ...run }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('有新进展')
    const original = wrapper.get('[data-work-key="run:run-a"]')
    const initialCount = wrapper.findAll('.work-node').length
    Object.defineProperty(viewport, 'offsetHeight', { configurable: true, value: 1000 })
    vi.spyOn(viewport, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 1000, 900))
    vi.spyOn(original.element, 'getBoundingClientRect').mockImplementation(
      () =>
        new DOMRect(0, wrapper.findAll('.work-node').length === initialCount ? 150 : 240, 800, 300),
    )
    state.filteredRuns.value.unshift({
      ...state.filteredRuns.value[0]!,
      id: 'new-run',
      started_at: '2026-08-21T10:00:00Z',
    })
    await flushPromises()
    expect(wrapper.text()).toContain('有新进展')
    // 90% 下新增节点移动 90 个视觉像素，滚动补偿应为 100 个布局像素。
    expect(viewport.scrollTop).toBe(220)
    await original.get('.work-select').trigger('click')
    expect(state.selectWork).toHaveBeenCalledWith(expect.objectContaining({ key: 'run:run-a' }))
    expect(viewport.scrollTop).toBe(220)
    expect(wrapper.get('.run-details-panel').isVisible()).toBe(true)
    wrapper.unmount()
  })

  it('keeps complete accepted results and uses a persistent disclosure for full run errors', async () => {
    const state = createRunsPageState()
    const result = '完整业务结果\n'.repeat(100)
    const diagnostic = 'provider_error 原始诊断\n'.repeat(100)
    state.acceptedResult.value = result
    state.activeWorkView.value = 'results'
    state.selectedRun.value = {
      ...state.selectedRun.value,
      status: 'failed',
      error_type: 'provider_error',
      error_message: diagnostic,
    } as never
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    expect(wrapper.find('.work-results details').exists()).toBe(false)
    expect(wrapper.get('.work-result-content').element.textContent).toBe(result)
    expect(wrapper.find('.error-banner').exists()).toBe(false)
    const disclosure = wrapper.get<HTMLDetailsElement>('.run-error-disclosure')
    expect(disclosure.element.open).toBe(false)
    expect(disclosure.get('summary').text()).toBe('运行错误 · provider_error')

    state.activeWorkView.value = 'execution'
    await flushPromises()
    await disclosure.get('summary').trigger('click')
    expect(disclosure.element.open).toBe(true)
    expect(wrapper.get('.error-msg').isVisible()).toBe(true)
    expect(wrapper.get('.error-msg').element.textContent).toBe(diagnostic)

    state.selectedRun.value = {
      ...state.selectedRun.value,
      error_type: null,
    } as never
    await flushPromises()
    expect(wrapper.get('.run-error-disclosure').element).toBe(disclosure.element)
    expect(disclosure.element.open).toBe(true)
    expect(disclosure.get('summary').text()).toBe('运行错误 · 未知类型')
    expect(wrapper.get('.error-msg').element.textContent).toBe(diagnostic)
    expect(wrapper.find('.run-business-summary').exists()).toBe(false)
    wrapper.unmount()
  })

  it('左栏只选择工作，右栏汇集接受结果并提供两级稳定页签', async () => {
    const state = createRunsPageState()
    const fullTitle = '验证管理端会话隔离，并核对跨角色访问边界与较长工作描述'
    state.workTitle.mockReturnValue(fullTitle)
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    const node = wrapper.get('.work-node')
    expect(node.get('.work-title').text()).toBe(fullTitle)
    expect(node.get('.work-title').attributes('title')).toBe(fullTitle)
    expect(wrapper.get('.reader-heading h2').text()).toBe(fullTitle)
    expect(node.findAll('.work-select > *')).toHaveLength(2)
    expect(node.findAll('.work-meta > *')).toHaveLength(2)
    expect(node.get('.work-status').text()).toBe('成功')
    expect(node.get('.work-meta time').text()).toBe('刚刚')
    expect(node.find('.work-results').exists()).toBe(false)
    expect(node.find('.work-tools').exists()).toBe(false)
    expect(node.find('details').exists()).toBe(false)
    for (const label of ['证据已接受', '已有结果', '结果未确认', '已有部分产出']) {
      expect(node.text()).not.toContain(label)
    }
    expect(wrapper.get('.work-result-content').element.textContent).toBe(state.acceptedResult.value)
    expect(wrapper.get('.work-result-source button').attributes('title')).toBe('run-a')
    expect(
      [...document.body.querySelectorAll('[role="tab"]')].map((tab) => tab.textContent?.trim()),
    ).toEqual(['结果', '执行记录', '输出', '工具', '命令', '文件'])
    wrapper.unmount()
  })

  it('lists every real run without inventing retry links or overwriting an old failure', async () => {
    const state = createRunsPageState()
    const current = {
      ...state.filteredRuns.value[0]!,
      intent_id: 'shared-intent',
      status: 'succeeded',
    }
    const failed = {
      ...state.filteredRuns.value[0]!,
      id: 'run-old',
      intent_id: 'shared-intent',
      status: 'failed',
      started_at: '2026-08-19T10:00:00.000Z',
      error_type: 'provider_error',
      error_message: '旧 Run 的原始失败',
    }
    state.filteredRuns.value = [current, failed]
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    const selects = wrapper.findAllComponents({ name: 'UiSelect' })
    const options = selects[1]!.props('options') as Array<{ value: string; label: string }>
    expect(options.map((option) => option.value)).toEqual(['run-a', 'run-old'])
    expect(options[0]!.label).toContain('succeeded')
    expect(options[0]!.label).toContain('1分30秒')
    expect(options[1]!.label).toContain('failed')
    expect(wrapper.text()).not.toContain('自动重试')
    expect(wrapper.text()).not.toContain('恢复链')

    state.selectedRunId.value = 'run-old'
    state.selectedRun.value = failed as never
    await flushPromises()
    expect(wrapper.get('.run-selector').attributes('aria-label')).toBe('选择执行记录')
    const disclosure = wrapper.get<HTMLDetailsElement>('.run-error-disclosure')
    expect(disclosure.element.open).toBe(false)
    await disclosure.get('summary').trigger('click')
    expect(disclosure.element.open).toBe(true)
    expect(wrapper.get('.error-msg').element.textContent).toBe('旧 Run 的原始失败')
    expect(wrapper.text()).not.toContain('旧 Run 已恢复')
    wrapper.unmount()
  })

  it('expands authoritative tool fields and complete input and response bodies', async () => {
    const state = createRunsPageState()
    const command = `printf '${'完整命令'.repeat(100)}'`
    const input = { file_path: '/tmp/input', content: '输入正文'.repeat(100) }
    const response = { output: '响应正文'.repeat(100) }
    state.toolCalls.mockReturnValue([
      {
        key: 'call-a',
        runId: 'run-a',
        name: 'Bash',
        startedAt: '2026-08-20T10:00:00.000Z',
        endedAt: '2026-08-20T10:00:02.000Z',
        status: '已完成',
        active: false,
        durationMs: 2000,
        phase: 'execute',
        sessionId: 'session-a',
        operationId: 'operation-a',
        artifactId: 'artifact-a',
        command,
        filePath: '/tmp/input',
        query: '完整查询',
        url: 'https://example.invalid/path',
        summary: '工具摘要',
        exitCode: 0,
        toolInput: input,
        toolResponse: response,
      },
    ])
    state.hookReads.value = {
      'run-a': { events: [], loaded: true, loading: false, failure: null, readAgain: false },
    } as never
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    selectTab('工具')
    await flushPromises()
    await wrapper.get('.tool-call summary').trigger('click')

    expect(wrapper.get('.tool-call-fields').text()).toContain(command)
    expect(wrapper.get('.tool-call-fields').text()).toContain('/tmp/input')
    expect(wrapper.get('.tool-call-fields').text()).toContain('完整查询')
    expect(wrapper.get('.tool-call-fields').text()).toContain('https://example.invalid/path')
    expect(wrapper.get('.tool-call-fields').text()).toContain('退出码')
    const bodies = wrapper.findAll('.tool-body')
    expect(bodies).toHaveLength(2)
    expect(bodies[0]!.text()).toContain(JSON.stringify(input, null, 2))
    expect(bodies[1]!.text()).toContain(JSON.stringify(response, null, 2))
    expect(wrapper.find('.tool-body-missing').exists()).toBe(false)
    await wrapper.get('.tool-call-detail button').trigger('click')
    expect(state.previewArtifact).toHaveBeenCalledWith('artifact-a')
    wrapper.unmount()
  })

  it('states the pi body limitation instead of rendering empty objects', async () => {
    const state = createRunsPageState()
    state.toolCalls.mockReturnValue([
      {
        key: 'pi-call',
        runId: 'run-a',
        name: 'read',
        startedAt: null,
        endedAt: '2026-08-20T10:00:02.000Z',
        status: '已完成',
        active: false,
        durationMs: null,
        phase: 'execute',
        sessionId: 'session-a',
        operationId: 'pi-operation',
        artifactId: null,
        command: null,
        filePath: null,
        query: null,
        url: null,
        summary: null,
        exitCode: null,
        toolInput: null,
        toolResponse: null,
      },
    ])
    state.hookReads.value = {
      'run-a': { events: [], loaded: true, loading: false, failure: null, readAgain: false },
    } as never
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    selectTab('工具')
    await flushPromises()
    await wrapper.get('.tool-call summary').trigger('click')

    expect(wrapper.get('.tool-body-missing').text()).toBe('该来源未记录工具输入／响应正文')
    expect(wrapper.findAll('.tool-body')).toHaveLength(0)
    expect(wrapper.get('.tool-call-detail').text()).not.toContain('{}')
    expect(wrapper.get('.tool-time').text()).toBe('12:00:00')
    wrapper.unmount()
  })

  it('shows a linked Intent outcome once in the reader without copying it onto the work row', async () => {
    const state = createRunsPageState()
    state.linkedIntentOutcomeText.mockReturnValue('结果未接受：证据不足')
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    expect(wrapper.get('.work-node').text()).not.toContain('结果未接受：证据不足')
    expect(wrapper.find('.row-diagnostic').exists()).toBe(false)
    const outcome = wrapper.get('[aria-label="工作结论"]')
    expect(outcome.text()).toBe('结果未接受：证据不足')
    expect(outcome.text()).not.toContain('工作结论')
    expect(outcome.classes()).toContain('intent-outcome-state')
    expect(outcome.classes()).not.toContain('ui-alert')
    expect(outcome.find('svg').exists()).toBe(true)
    expect(wrapper.find('[aria-label="运行错误"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps long diagnostics out of the work index and intact in the selected run', async () => {
    const state = createRunsPageState()
    const diagnostic = '结果未知：等待确认\n' + '诊断原文 '.repeat(100)
    state.selectedRun.value = {
      ...state.selectedRun.value,
      status: 'failed',
      error_type: 'unknown_result',
      error_message: diagnostic,
    } as never
    state.filteredRuns.value[0]!.status = 'failed'
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    expect(wrapper.get('.work-node').text()).not.toContain(diagnostic)
    expect(wrapper.get('.work-status').text()).toBe('失败')
    await wrapper.get('.work-select').trigger('click')
    expect(state.selectWork).toHaveBeenCalledWith(expect.objectContaining({ key: 'run:run-a' }))
    await wrapper.get('.run-error-summary').trigger('click')
    expect(wrapper.get<HTMLDetailsElement>('.run-error-disclosure').element.open).toBe(true)
    expect(wrapper.get('.error-msg').element.textContent).toBe(diagnostic)
    wrapper.unmount()
  })

  it('keeps a TaskRun error separate from its linked Intent outcome', async () => {
    const state = createRunsPageState()
    state.selectedRun.value = {
      ...state.selectedRun.value,
      status: 'failed',
      error_type: 'provider_error',
      error_message: 'provider temporarily unavailable',
    } as never
    state.linkedIntentOutcomeText.mockReturnValue('结果未接受：provider_error')
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    expect(wrapper.get('.work-node').text()).not.toContain('provider temporarily unavailable')
    const disclosure = wrapper.get<HTMLDetailsElement>('[aria-label="运行错误"]')
    await disclosure.get('summary').trigger('click')
    expect(disclosure.element.open).toBe(true)
    expect(disclosure.get('.error-msg').element.textContent).toBe(
      'provider temporarily unavailable',
    )
    expect(disclosure.text()).not.toContain('结果未接受：provider_error')
    expect(wrapper.get('[aria-label="工作结论"]').text()).toBe('结果未接受：provider_error')
    wrapper.unmount()
  })

  it('renders semantic detail tabs and keeps terminal selection separate', async () => {
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    expect(document.body.querySelectorAll('[role="tab"]')).toHaveLength(6)
    expect(tabByName('执行记录').getAttribute('aria-selected')).toBe('true')
    expect(tabByName('输出').getAttribute('aria-selected')).toBe('true')
    expect(document.body.textContent).toContain('completed output')

    const diagnosticButton = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('诊断'),
    )
    if (!diagnosticButton) throw new Error('找不到诊断终端输出选项')
    diagnosticButton.click()
    await flushPromises()

    expect(mocks.selectTerminalOutput).toHaveBeenCalledWith('output-b')
    expect(tabByName('输出').getAttribute('aria-selected')).toBe('true')
    wrapper.unmount()
  })

  it('uses manual keyboard activation for detail tabs', async () => {
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    const terminalTab = tabByName('输出')
    terminalTab.focus()
    terminalTab.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await flushPromises()

    expect(document.activeElement).toBe(tabByName('工具'))
    expect(tabByName('输出').getAttribute('aria-selected')).toBe('true')

    document.activeElement?.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    )
    await flushPromises()

    expect(tabByName('工具').getAttribute('aria-selected')).toBe('true')
    wrapper.unmount()
  })

  it('renders flattened command diagnostics', async () => {
    const state = createRunsPageState()
    state.commandEvents.value = [
      {
        id: 'command-a',
        task_run_id: 'run-a',
        project_id: 'project-a',
        ts: '2026-08-20T10:00:00.000Z',
        phase: 'explore_execute',
        command_kind: 'shell',
        kind: 'command_finished',
        status: 'failed',
        exit_code: 137,
        returncode: 137,
        duration_seconds: 12.5,
        timed_out: true,
        cancelled: false,
        oom_killed: true,
        oom_evidence_source: 'cgroup',
      },
    ]
    mocks.useRunsPage.mockReturnValue(state)

    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    selectTab('命令')
    await flushPromises()
    expect(document.body.textContent).toContain('explore_execute')
    expect(document.body.textContent).toContain('shell')
    expect(document.body.textContent).toContain('Exit 137')
    expect(document.body.textContent).toContain('超时')
    expect(document.body.textContent).toContain('OOM')

    wrapper.unmount()
  })

  it('maps command outcomes to non-misleading badge tones', async () => {
    const state = createRunsPageState()
    state.commandEvents.value = [
      commandEvent('failed-zero', { status: 'failed', exit_code: 0, returncode: 0 }),
      commandEvent('timeout-zero', { status: 'timed_out', timed_out: true }),
      commandEvent('cancelled-zero', { status: 'cancelled', cancelled: true }),
      commandEvent('success-zero', { status: 'success' }),
    ]
    mocks.useRunsPage.mockReturnValue(state)

    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()
    const badgeByText = (text: string) =>
      [...document.body.querySelectorAll<HTMLElement>('.ui-badge')].find((badge) =>
        badge.textContent?.includes(text),
      )

    selectTab('命令')
    await flushPromises()
    expect(badgeByText('failed')?.classList).toContain('ui-badge--danger')
    expect(badgeByText('timed_out')?.classList).toContain('ui-badge--warning')
    expect(badgeByText('cancelled')?.classList).toContain('ui-badge--warning')
    expect(badgeByText('success')?.classList).toContain('ui-badge--success')

    wrapper.unmount()
  })

  it('shows distinct preview and producer truncation notices with a content download', async () => {
    const state = createRunsPageState()
    state.previewTruncationMessage.value = '当前展示日志尾部预览（64 KB / 128 KB）'
    state.captureTruncationMessage.value = '此输出在采集时已截断，下载无法恢复丢弃的中间内容。'
    state.stdoutDownloadLabel.value = '下载已保存日志'
    state.stdoutDownloadTitle.value = '下载服务端已保存的首尾日志片段'
    mocks.useRunsPage.mockReturnValue(state)

    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    expect(document.body.textContent).toContain('当前展示日志尾部预览')
    expect(document.body.textContent).toContain('此输出在采集时已截断')
    const download = document.body.querySelector<HTMLAnchorElement>('a[download]')
    expect(download?.getAttribute('href')).toBe('/projects/project-a/artifacts/output-a/content')
    expect(download?.textContent).toContain('下载已保存日志')
    expect(document.body.querySelector('.dialog-backdrop')).toBeNull()
    expect(document.body.textContent).not.toContain('查看输出')
    wrapper.unmount()
  })

  it('keeps poll feedback out of the title and detail flow', async () => {
    const state = createRunsPageState()
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    const title = wrapper.get('.reader-heading')
    const tabs = wrapper.get('.runs-detail-tabs').element
    state.isFetchingRuns.value = true
    await flushPromises()

    expect(title.find('[role="status"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="本次运行流量状态"]').exists()).toBe(false)
    expect(tabs.previousElementSibling?.classList.contains('run-context')).toBe(true)
    expect(wrapper.get('.terminal-content').text()).toContain('completed output')

    state.isLoadingRunDetails.value = true
    await flushPromises()
    expect(wrapper.get('.run-detail-loading [role="status"]').attributes('aria-label')).toBe(
      '正在载入运行详情',
    )
    expect(wrapper.find('[aria-label="本次运行流量状态"]').exists()).toBe(false)
    expect(tabs.previousElementSibling?.classList.contains('run-context')).toBe(true)
    wrapper.unmount()
  })

  it('R1 keeps stale log content, notices, download and scroll position visible while retrying only logs', async () => {
    const state = createRunsPageState()
    state.autoScrollTerminal.value = false
    state.previewTruncationMessage.value = '尾部预览限制'
    state.captureTruncationMessage.value = '采集截断限制'
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()
    const screen = wrapper.get('.terminal-screen').element
    screen.scrollTop = 120
    state.terminalPreviewError.value = '载入终端日志失败：HTTP 503'
    await flushPromises()
    const alert = wrapper.get('[aria-label="日志读取状态"]')
    expect(alert.text()).toContain('刷新失败，显示上次读取内容')
    expect(wrapper.get('.terminal-content').text()).toBe('completed output')
    expect(screen.scrollTop).toBe(120)
    expect(wrapper.get('a[download]').attributes('href')).toContain('/output-a/content')
    expect(wrapper.get('.terminal-preview-notice').text()).toBe('尾部预览限制')
    expect(wrapper.get('.terminal-capture-warning').text()).toBe('采集截断限制')
    await alert.get('button').trigger('click')
    expect(state.refetchTerminal).toHaveBeenCalledOnce()
    expect(state.refetchCommands).not.toHaveBeenCalled()
    expect(state.refetchArtifacts).not.toHaveBeenCalled()
    state.isLoadingTerminalPreview.value = true
    await flushPromises()
    expect(alert.get('button').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.terminal-content').text()).toBe('completed output')
    wrapper.unmount()
  })

  it('R1 distinguishes source loading and failure from successful empty collections', async () => {
    const state = createRunsPageState()
    state.commandsLoaded.value = false
    state.artifactsLoaded.value = false
    state.isLoadingCommands.value = true
    state.isLoadingArtifacts.value = true
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()
    expect(wrapper.find('[aria-label="正在读取命令审计"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="正在读取关联资产"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('本轮执行中没有调用')
    expect(wrapper.text()).not.toContain('本轮运行中没有写入')
    state.isLoadingCommands.value = false
    state.isLoadingArtifacts.value = false
    state.commandsError.value = '读取命令审计失败：HTTP 403'
    state.artifactsError.value = '读取关联资产失败：HTTP 404'
    await flushPromises()
    expect(wrapper.text()).not.toContain('本轮执行中没有调用')
    expect(wrapper.text()).not.toContain('本轮运行中没有写入')
    await wrapper.get('[aria-label="命令审计读取状态"] button').trigger('click')
    await wrapper.get('[aria-label="关联资产读取状态"] button').trigger('click')
    expect(state.refetchCommands).toHaveBeenCalledOnce()
    expect(state.refetchArtifacts).toHaveBeenCalledOnce()
    state.commandsLoaded.value = true
    state.artifactsLoaded.value = true
    state.commandsError.value = ''
    state.artifactsError.value = ''
    await flushPromises()
    expect(wrapper.text()).toContain('暂无 Shell 命令')
    expect(wrapper.text()).toContain('暂无文件')
    state.commandEvents.value = [commandEvent('retained-command', {})]
    state.commandsError.value = '读取命令审计失败：HTTP 503'
    await flushPromises()
    expect(wrapper.get('[aria-label="命令审计读取状态"]').text()).toContain('显示上次读取内容')
    expect(wrapper.get('.commands-view table').text()).toContain('shell')
    wrapper.unmount()
  })

  it('renders terminal loading, error, and empty states explicitly', async () => {
    const state = createRunsPageState()
    state.stdoutContent.value = ''
    state.terminalPreviewLoaded.value = false
    state.isLoadingTerminalPreview.value = true
    mocks.useRunsPage.mockReturnValue(state)

    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()
    expect(document.body.textContent).toContain('正在载入终端日志')

    state.isLoadingTerminalPreview.value = false
    state.terminalPreviewError.value = '载入终端日志失败：网络错误'
    await flushPromises()
    expect(document.body.textContent).toContain('载入终端日志失败：网络错误')

    state.terminalPreviewError.value = ''
    state.terminalPreviewLoaded.value = true
    await flushPromises()
    expect(wrapper.get('.terminal-screen').text()).toBe('日志内容为空')
    state.terminalPreviewLoaded.value = false
    state.stdoutArtifactId.value = ''
    await flushPromises()
    expect(document.body.textContent).toContain('暂无标准输出、标准错误或诊断日志产物')
    wrapper.unmount()
  })

  it('distinguishes initial list errors from stale rows kept after refresh failure', async () => {
    const state = createRunsPageState()
    state.filteredRuns.value = []
    state.selectedRun.value = null as never
    state.selectedRunId.value = null as never
    state.runsError.value = new Error('runs offline')
    state.runsInitialError.value = true
    mocks.useRunsPage.mockReturnValue(state)

    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()
    expect(document.body.textContent).toContain('运行列表加载失败')
    expect(document.body.textContent).toContain('runs offline')

    state.filteredRuns.value = [
      {
        id: 'run-a',
        project_id: 'project-a',
        runtime_context: {},
        task_type: 'explore',
        worker_name: 'claude-code',
        status: 'succeeded',
      } as TaskRun,
    ]
    state.runsInitialError.value = false
    state.runsStaleError.value = true
    await flushPromises()
    expect(document.body.textContent).toContain('刷新失败，保留上次运行列表')
    expect(wrapper.get('.work-node').text()).toContain('验证管理端会话隔离')
    wrapper.unmount()
  })

  it('uses timing helpers and exposes cancelled and timed-out filters', async () => {
    const state = createRunsPageState()
    mocks.useRunsPage.mockReturnValue(state)

    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    expect(document.body.textContent).toContain('1分30秒')
    expect(state.runDurationText).toHaveBeenCalled()
    const filterOptions = wrapper.findComponent({ name: 'UiSelect' }).props('options') as Array<{
      value: string
    }>
    expect(filterOptions.map((option) => option.value)).toEqual(
      expect.arrayContaining(['cancelled', 'timed_out']),
    )
    wrapper.unmount()
  })
})
