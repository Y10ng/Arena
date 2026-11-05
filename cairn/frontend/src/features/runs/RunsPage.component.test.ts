import { flushPromises, mount } from '@vue/test-utils'
import { computed, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import RunsPage from './RunsPage.vue'
import type { TaskRun, TaskRunCommandEvent } from './runTypes'
import { buildWorkTimeline } from './runTimelineModel'
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
  { label: '运行日志', value: 'terminal' },
  { label: '命令审计', value: 'commands' },
  { label: '关联资产', value: 'artifacts' },
]

function createRunsPageState() {
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
  const workTimeline = computed(() =>
    buildWorkTimeline(
      filteredRuns.value,
      [],
      [{ id: 'f', task_run_id: 'run-a', description: '未发现跨角色会话复用，证据已接受' } as Fact],
      new Set(filteredRuns.value.map((run) => run.id)),
    ),
  )
  const expandedTools = ref(new Set<string>())
  return {
    workTimeline,
    workTitle: vi.fn(() => '验证管理端会话隔离'),
    workActivity: vi.fn(() => []),
    toolCalls: vi.fn(() => []),
    hookReads: ref({}),
    hookReadError: vi.fn(() => ''),
    hookObservationIncomplete: vi.fn(() => false),
    expandedTools,
    toggleTools: vi.fn((work) => {
      expandedTools.value.has(work.key)
        ? expandedTools.value.delete(work.key)
        : expandedTools.value.add(work.key)
    }),
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
    expect(wrapper.find('.run-details-panel').exists()).toBe(false)
    expect(wrapper.findAll('.work-detail')).toHaveLength(1)
    expect(mocks.useQuery.mock.calls[0]![0].enabled.value).toBe(false)
    wrapper.unmount()
  })

  it('queries a terminal run beyond the first 100 traffic scopes and polls until synchronization and stopping are confirmed', async () => {
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
    mocks.useQuery.mockReturnValue({ data, isError: ref(false) })
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
    expect(wrapper.get('[aria-label="本次运行流量状态"]').text()).toBe('同步完成')
    expect(options.refetchInterval(query)).toBe(false)
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

  it('opens linked records in the reader and preserves filters, log position and focus when returning to the index', async () => {
    const state = createRunsPageState()
    state.autoScrollTerminal.value = false
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()
    const terminal = wrapper.get('.terminal-screen').element
    terminal.scrollTop = 120
    expect(wrapper.get('.runs-index').isVisible()).toBe(false)
    expect(wrapper.get('.run-details-panel').isVisible()).toBe(true)

    await wrapper.get('.run-back').trigger('click')
    await flushPromises()
    expect(wrapper.get('.runs-index').isVisible()).toBe(true)
    expect(wrapper.get('.run-details-panel').isVisible()).toBe(false)
    expect(document.activeElement).toBe(wrapper.get('.work-detail').element)
    await wrapper.get('input[aria-label="搜索运行元数据"]').setValue('metadata query')
    await wrapper.get('.work-detail').trigger('click')
    await flushPromises()
    expect(state.selectRun).toHaveBeenCalledWith('run-a')
    expect(document.activeElement).toBe(wrapper.get('.detail-header h2').element)
    expect(wrapper.get('.terminal-screen').element).toBe(terminal)
    expect(terminal.scrollTop).toBe(120)
    expect(state.autoScrollTerminal.value).toBe(false)
    expect(state.searchQuery.value).toBe('metadata query')
    expect(wrapper.find('.runs-detail-tabs .ui-tabs__actions a[download]').exists()).toBe(true)

    state.activeDetailTab.value = 'commands'
    await flushPromises()
    expect(wrapper.get('.terminal-screen').element).toBe(terminal)
    expect(wrapper.get('.terminal-screen').isVisible()).toBe(false)
    state.activeDetailTab.value = 'terminal'
    await flushPromises()
    expect(wrapper.get('.terminal-screen').element).toBe(terminal)
    expect(terminal.scrollTop).toBe(120)

    await wrapper.get('.run-back').trigger('click')
    state.autoScrollTerminal.value = true
    Object.defineProperty(terminal, 'scrollHeight', { configurable: true, value: 800 })
    await wrapper.get('.work-detail').trigger('click')
    await flushPromises()
    expect(terminal.scrollTop).toBe(800)

    await wrapper.get('.run-back').trigger('click')
    state.selectedRunId.value = 'linked-run'
    state.selectedRun.value = { ...state.selectedRun.value, id: 'linked-run' }
    await flushPromises()
    expect(wrapper.get('.run-details-panel').isVisible()).toBe(true)
    expect(wrapper.get('.run-reader-id').text()).toContain('linked-run')

    state.selectedRun.value = null as never
    await flushPromises()
    expect(wrapper.get('.runs-index').isVisible()).toBe(true)
    wrapper.unmount()
  })

  it('相同轮询不报新进展，新增节点不抢滚动，详情返回保留展开与位置', async () => {
    const state = createRunsPageState()
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await wrapper.get('.run-back').trigger('click')
    const viewport = wrapper.get('.runs-timeline-scroll').element
    viewport.scrollTop = 120
    await wrapper.get('.work-node button[aria-expanded]').trigger('click')
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
    await original.get('.work-detail').trigger('click')
    await wrapper.get('.run-back').trigger('click')
    expect(viewport.scrollTop).toBe(220)
    expect(original.get('button[aria-expanded]').attributes('aria-expanded')).toBe('true')
    expect(document.activeElement).toBe(original.get('.work-detail').element)
    wrapper.unmount()
  })

  it('keeps long results and errors intact in separate on-demand disclosure', async () => {
    const state = createRunsPageState()
    const result = '完整业务结果\n'.repeat(100)
    const diagnostic = 'provider_error 原始诊断\n'.repeat(100)
    state.runResultText.mockReturnValue(result)
    state.selectedRun.value = {
      ...state.selectedRun.value,
      status: 'failed',
      error_type: 'provider_error',
      error_message: diagnostic,
    } as never
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()
    expect(wrapper.get('.run-business-summary details').attributes('open')).toBeUndefined()
    expect(wrapper.get('.error-banner').attributes('open')).toBeUndefined()
    expect(wrapper.get('.run-result-content').element.textContent).toBe(result)
    expect(wrapper.get('.error-msg').element.textContent).toBe(diagnostic)
    expect(wrapper.get('.error-banner summary').text()).toContain('provider_error')
    wrapper.unmount()
  })

  it('以连续工作节点展示接受结果和来源，工具过程可按需展开', async () => {
    const state = createRunsPageState()
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    const node = wrapper.get('.work-node')
    expect(node.get('.work-heading').text()).toContain('验证管理端会话隔离')
    expect(node.get('.work-results').text()).toContain('证据已接受')
    expect(node.get('.work-result-source button').attributes('title')).toBe('run-a')
    expect(node.find('.row-diagnostic').exists()).toBe(false)
    const tools = node.get('button[aria-expanded]')
    expect(tools.attributes('aria-expanded')).toBe('false')
    await tools.trigger('click')
    expect(tools.attributes('aria-expanded')).toBe('true')
    expect(node.find('.work-tools').exists()).toBe(true)
    expect(wrapper.get('.run-business-summary').text()).toContain(
      '未发现跨角色会话复用，证据已接受',
    )
    expect(
      [...document.body.querySelectorAll('[role="tab"]')].map((tab) => tab.textContent?.trim()),
    ).toEqual(['运行日志', '命令审计', '关联资产'])
    wrapper.unmount()
  })

  it('shows a linked Intent outcome once in detail without copying it onto the run card', async () => {
    const state = createRunsPageState()
    state.linkedIntentOutcomeText.mockReturnValue('结果未接受：证据不足')
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    expect(wrapper.get('.work-attempts').text()).toContain('成功')
    expect(wrapper.find('.row-diagnostic').exists()).toBe(false)
    expect(wrapper.get('.intent-outcome-banner').text()).toContain('关联 Intent 最终结论')
    expect(wrapper.get('.intent-outcome-banner').text()).toContain('结果未接受：证据不足')
    expect(wrapper.find('[aria-label="运行错误"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps long diagnostics in a compact row with access to the unchanged detail original', async () => {
    const state = createRunsPageState()
    const diagnostic = '结果未知：等待确认\n' + '诊断原文 '.repeat(100)
    state.selectedRun.value = {
      ...state.selectedRun.value,
      status: 'failed',
      error_type: 'unknown_result',
      error_message: diagnostic,
    } as never
    state.filteredRuns.value[0]!.status = 'failed'
    state.runCardDiagnostic.mockReturnValue(diagnostic)
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    expect(wrapper.get('.work-node').text()).not.toContain(diagnostic)
    expect(wrapper.get('.work-attempts').text()).toContain('失败')
    expect(wrapper.get('.work-status').text()).toContain('未完成')
    await wrapper.get('.work-detail').trigger('click')
    expect(state.selectRun).toHaveBeenCalledWith('run-a')
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
    state.runCardDiagnostic.mockReturnValue('provider temporarily unavailable')
    state.linkedIntentOutcomeText.mockReturnValue('结果未接受：provider_error')
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    expect(wrapper.get('.work-node').text()).not.toContain('provider temporarily unavailable')
    expect(wrapper.get('[aria-label="运行错误"]').text()).toContain(
      'provider temporarily unavailable',
    )
    expect(wrapper.get('[aria-label="关联 Intent 最终结论"]').text()).toContain(
      '结果未接受：provider_error',
    )
    wrapper.unmount()
  })

  it('renders semantic detail tabs and keeps terminal selection separate', async () => {
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    expect(document.body.querySelectorAll('[role="tab"]')).toHaveLength(3)
    expect(tabByName('运行日志').getAttribute('aria-selected')).toBe('true')
    expect(document.body.textContent).toContain('completed output')

    const diagnosticButton = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('诊断'),
    )
    if (!diagnosticButton) throw new Error('找不到诊断终端输出选项')
    diagnosticButton.click()
    await flushPromises()

    expect(mocks.selectTerminalOutput).toHaveBeenCalledWith('output-b')
    expect(tabByName('运行日志').getAttribute('aria-selected')).toBe('true')
    wrapper.unmount()
  })

  it('uses manual keyboard activation for detail tabs', async () => {
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    const terminalTab = tabByName('运行日志')
    terminalTab.focus()
    terminalTab.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await flushPromises()

    expect(document.activeElement).toBe(tabByName('命令审计'))
    expect(tabByName('运行日志').getAttribute('aria-selected')).toBe('true')

    document.activeElement?.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    )
    await flushPromises()

    expect(tabByName('命令审计').getAttribute('aria-selected')).toBe('true')
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

    selectTab('命令审计')
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

    selectTab('命令审计')
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

    const title = wrapper.get('.work-heading')
    const tabs = wrapper.get('.runs-detail-tabs').element
    state.isFetchingRuns.value = true
    await flushPromises()

    expect(title.find('[role="status"]').exists()).toBe(false)
    expect(wrapper.find('.run-detail-state').exists()).toBe(false)
    expect(tabs.previousElementSibling?.classList.contains('run-business-summary')).toBe(true)
    expect(wrapper.get('.terminal-content').text()).toContain('completed output')

    state.isLoadingRunDetails.value = true
    await flushPromises()
    expect(wrapper.get('.run-detail-loading [role="status"]').attributes('aria-label')).toBe(
      '正在载入运行详情',
    )
    expect(wrapper.find('.run-detail-state').exists()).toBe(false)
    expect(tabs.previousElementSibling?.classList.contains('run-business-summary')).toBe(true)
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
    expect(wrapper.text()).toContain('本轮运行中没有写入')
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
    expect(wrapper.get('.work-node').text()).toContain('run-a')
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
