import { flushPromises, mount } from '@vue/test-utils'
import { computed, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import RunsPage from './RunsPage.vue'
import type { TaskRunCommandEvent } from './runTypes'

const mocks = vi.hoisted(() => ({
  useRunsPage: vi.fn(),
  selectTerminalOutput: vi.fn(),
}))

vi.mock('./useRunsPage', () => ({
  useRunsPage: mocks.useRunsPage,
}))

const detailTabs = [
  { label: '终端视图', value: 'terminal' },
  { label: 'Shell命令', value: 'commands' },
  { label: '产物证据', value: 'artifacts' },
]

function createRunsPageState() {
  const activeDetailTab = ref('terminal')
  const selectedRun = ref({
    id: 'run-a',
    session_id: 'session-a',
    execution_profile: 'default',
    task_type: 'explore',
    status: 'succeeded',
    worker_name: 'claude-code',
    worker_type: 'claude_code',
    error_type: null,
    error_message: null,
  })

  return {
    activeDetailTab,
    artifacts: ref([]),
    autoScrollTerminal: ref(true),
    captureTruncationMessage: ref(''),
    commandEvents: ref<TaskRunCommandEvent[]>([]),
    detailTabs,
    filteredRuns: ref([
      {
        id: 'run-a',
        task_type: 'explore',
        worker_name: 'claude-code',
        status: 'succeeded',
      },
    ]),
    filtersActive: computed(() => false),
    formatRelativeTime: vi.fn(() => '刚刚'),
    formatTimeShort: vi.fn(() => '12:00:00'),
    isFetchingRuns: ref(false),
    isLoadingRunDetails: ref(false),
    isLoadingRuns: ref(false),
    isLoadingTerminalPreview: ref(false),
    linkedIntentOutcomeText: vi.fn(() => ''),
    previewArtifact: vi.fn(),
    previewTruncationMessage: ref(''),
    refetchRuns: vi.fn(),
    refetchTerminal: vi.fn(),
    runBusinessTitle: vi.fn(() => '验证管理端会话隔离'),
    runCardDiagnostic: vi.fn(() => ''),
    runDetailsError: ref(''),
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
  mocks.selectTerminalOutput.mockReset()
  mocks.useRunsPage.mockReset().mockReturnValue(createRunsPageState())
  document.body.innerHTML = ''
})

describe('RunsPage details', () => {
  it('opens a run and returns to the narrow list without stacking two high panels', async () => {
    const state = createRunsPageState()
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()
    expect(wrapper.classes()).not.toContain('runs-container--reading')
    await wrapper.get('.run-row').trigger('click')
    expect(state.selectRun).toHaveBeenCalledWith('run-a')
    expect(wrapper.classes()).toContain('runs-container--reading')
    await wrapper.get('.run-back').trigger('click')
    expect(wrapper.classes()).not.toContain('runs-container--reading')
    expect(wrapper.find('.terminal-toolbar').exists()).toBe(false)
    expect(wrapper.find('.runs-detail-tabs .ui-tabs__actions a[download]').exists()).toBe(true)
    await wrapper.get('.run-row').trigger('click')
    state.selectedRun.value = null as never
    await flushPromises()
    expect(wrapper.classes()).not.toContain('runs-container--reading')
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

  it('keeps a normal success badge without result text or result tooltips on the card', async () => {
    const state = createRunsPageState()
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    const card = wrapper.get('.run-row')
    expect(card.text().match(/成功/g)).toHaveLength(1)
    expect(card.text()).toContain('验证管理端会话隔离')
    expect(card.text()).toContain('claude-code')
    expect(card.text()).toContain('1分30秒')
    expect(card.find('.row-diagnostic').exists()).toBe(false)
    expect(card.text()).not.toContain('证据已接受')
    expect(
      card.findAll('[title]').some((item) => item.attributes('title')?.includes('证据已接受')),
    ).toBe(false)
    expect(wrapper.get('.run-business-summary').text()).toContain(
      '未发现跨角色会话复用，证据已接受',
    )
    expect(
      [...document.body.querySelectorAll('[role="tab"]')].map((tab) => tab.textContent?.trim()),
    ).toEqual(['终端视图', 'Shell命令', '产物证据'])
    wrapper.unmount()
  })

  it('shows a linked Intent outcome once in detail without copying it onto the run card', async () => {
    const state = createRunsPageState()
    state.linkedIntentOutcomeText.mockReturnValue('结果未接受：证据不足')
    mocks.useRunsPage.mockReturnValue(state)
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    expect(wrapper.get('.run-row').text()).toContain('成功')
    expect(wrapper.find('.row-diagnostic').exists()).toBe(false)
    expect(wrapper.get('.intent-outcome-banner').text()).toContain('关联 Intent 最终结论')
    expect(wrapper.get('.intent-outcome-banner').text()).toContain('结果未接受：证据不足')
    expect(wrapper.find('[aria-label="运行错误"]').exists()).toBe(false)
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

    expect(wrapper.get('.row-diagnostic').text()).toBe('provider temporarily unavailable')
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
    expect(tabByName('终端视图').getAttribute('aria-selected')).toBe('true')
    expect(document.body.textContent).toContain('completed output')

    const diagnosticButton = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('诊断'),
    )
    if (!diagnosticButton) throw new Error('找不到诊断终端输出选项')
    diagnosticButton.click()
    await flushPromises()

    expect(mocks.selectTerminalOutput).toHaveBeenCalledWith('output-b')
    expect(tabByName('终端视图').getAttribute('aria-selected')).toBe('true')
    wrapper.unmount()
  })

  it('uses manual keyboard activation for detail tabs', async () => {
    const wrapper = mount(RunsPage, { attachTo: document.body })
    await flushPromises()

    const terminalTab = tabByName('终端视图')
    terminalTab.focus()
    terminalTab.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await flushPromises()

    expect(document.activeElement).toBe(tabByName('Shell命令'))
    expect(tabByName('终端视图').getAttribute('aria-selected')).toBe('true')

    document.activeElement?.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    )
    await flushPromises()

    expect(tabByName('Shell命令').getAttribute('aria-selected')).toBe('true')
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

    selectTab('Shell命令')
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

    selectTab('Shell命令')
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

    const title = wrapper.get('.sidebar-title-row')
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

  it('renders terminal loading, error, and empty states explicitly', async () => {
    const state = createRunsPageState()
    state.stdoutContent.value = ''
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
      { id: 'run-a', task_type: 'explore', worker_name: 'claude-code', status: 'succeeded' },
    ]
    state.runsInitialError.value = false
    state.runsStaleError.value = true
    await flushPromises()
    expect(document.body.textContent).toContain('刷新失败，保留上次运行列表')
    expect(document.body.textContent).toContain('#run-a')
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
