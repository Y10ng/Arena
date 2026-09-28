import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MonitoringSummary, MonitoringTaskRun } from '@/api/types'
import Monitoring from './Monitoring.vue'

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  router: { push: vi.fn() },
  refetch: vi.fn(),
  queryOptions: null as null | Record<string, unknown>,
  data: null as unknown,
  error: null as unknown,
  isLoading: null as unknown,
  isError: null as unknown,
  isFetching: null as unknown,
}))

vi.mock('@/api/client', () => ({ api: mocks.api }))
vi.mock('vue-router', () => ({ useRouter: () => mocks.router }))
vi.mock('@tanstack/vue-query', () => ({
  useQuery: (options: Record<string, unknown>) => {
    mocks.queryOptions = options
    return {
      data: mocks.data,
      error: mocks.error,
      isLoading: mocks.isLoading,
      isError: mocks.isError,
      isFetching: mocks.isFetching,
      refetch: mocks.refetch,
    }
  },
}))

const task = (
  projectId: string,
  title: string,
  overrides: Partial<MonitoringTaskRun> = {},
): MonitoringTaskRun => ({
  id: 'shared-run',
  project_id: projectId,
  project_title: title,
  project_status: 'active',
  task_type: 'explore',
  worker_name: 'claude-code',
  worker_type: 'claude_code',
  status: 'running',
  started_at: '2026-08-20T10:00:00Z',
  duration_seconds: 15,
  queue_wait_seconds: 0,
  artifact_count: 2,
  heartbeat_source: 'intent',
  heartbeat_status: 'active',
  ...overrides,
})

const summary = (): MonitoringSummary => ({
  generated_at: '2026-08-20T10:00:00Z',
  long_running_threshold_seconds: 300,
  dispatcher: {
    project_count: 2,
    active_project_count: 2,
    running_project_count: 2,
    running_task_count: 2,
    queued_task_count: 0,
    last_status: 'running',
    effective_status: 'stale',
    started_at: '2026-08-20T09:00:00Z',
    config_hash: '1234567890abcdef',
    last_loop_duration_ms: 42,
    last_error_message: 'dispatcher warning',
    heartbeat_age_seconds: 90,
    heartbeat_stale: true,
    worker_cooldowns: [],
    worker_rejections: [],
    worker_pool: [],
  },
  running_tasks: [task('project-a', 'Project A'), task('project-b', 'Project B')],
  long_running_tasks: [task('project-a', 'Project A')],
  queued_tasks: [],
  workers: [
    {
      worker_name: 'claude-code',
      worker_type: 'claude_code',
      health: 'healthy',
      configured: true,
      task_types: ['explore'],
      running_count: 1,
      queued_count: 0,
      succeeded_count: 8,
      failed_count: 1,
      timed_out_count: 0,
      cancelled_count: 0,
      consecutive_failure_count: 0,
      max_running: 2,
      last_started_at: '2026-08-20T10:00:00Z',
    },
    {
      worker_name: 'retired-worker',
      worker_type: 'claude_code',
      health: 'idle',
      configured: false,
      task_types: [],
      running_count: 0,
      queued_count: 0,
      succeeded_count: 2,
      failed_count: 0,
      timed_out_count: 0,
      cancelled_count: 0,
      consecutive_failure_count: 0,
    },
  ],
  artifacts: {
    total_count: 9,
    total_size: 2048,
    raw_output_count: 2,
    raw_output_size: 1024,
    largest_raw_output_size: 512,
    evidence_count: 4,
    deliverable_count: 3,
    declared_count: 2,
    write_failed_task_count: 1,
    recent_write_failures: [],
  },
  result_audit: {
    event_count: 0,
    validation: { total_count: 0, valid_count: 0, invalid_count: 0, rejected_count: 0 },
    protocol_write: { total_count: 0, success_count: 0, failed_count: 0, skipped_count: 0 },
    decision: {
      total_count: 0,
      complete_count: 0,
      fact_count: 0,
      intents_count: 0,
      noop_count: 0,
      low_confidence_count: 0,
      blocked_count: 0,
      target_dependency_count: 0,
      created_intent_count: 0,
    },
  },
  prompt_effect: { version_count: 0, total_task_count: 0, items: [] },
  trends: { buckets: [] },
  agent_events: {
    recent_event_limit: 200,
    event_count: 10,
    by_kind: [],
    command_finished: { total_count: 4, success_count: 3, failed_count: 1 },
    hook_operation: {
      total_count: 6,
      failed_count: 1,
      denied_count: 1,
      high_risk_count: 2,
      knowledge_hit_count: 3,
      knowledge_hits: [],
      recent_failures: [],
      recent_high_risk: [],
    },
    running_commands: [],
    recent_command_failures: [],
  },
  recent_failures: [],
  error_counts: [
    { error_type: 'worker_failed', count: 2, latest_task_run: task('project-a', 'Project A') },
  ],
})

let wrapper: ReturnType<typeof mount> | null = null

beforeEach(() => {
  mocks.data = ref<MonitoringSummary | undefined>(summary())
  mocks.error = ref<unknown>(null)
  mocks.isLoading = ref(false)
  mocks.isError = ref(false)
  mocks.isFetching = ref(false)
  mocks.refetch.mockReset()
  mocks.router.push.mockReset()
  mocks.api.mockReset()
  mocks.queryOptions = null
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('Monitoring', () => {
  it('preserves endpoint, polling, effective status, composite task identity, deep links, and summaries', async () => {
    wrapper = mount(Monitoring, { attachTo: document.body })
    await flushPromises()

    expect(mocks.queryOptions?.refetchInterval).toBe(8000)
    await (mocks.queryOptions?.queryFn as () => Promise<unknown>)()
    expect(mocks.api).toHaveBeenCalledWith('GET', '/admin/monitoring?profile=monitoring')
    expect(document.body.textContent).toContain('心跳过期')
    expect(document.body.textContent).toContain('Project A')
    expect(document.body.textContent).toContain('Project B')
    expect(document.body.textContent).toContain('长时间运行')
    expect(document.body.textContent).toContain('4 项')
    expect(document.body.textContent).toContain('3 项')
    expect(document.body.textContent).toContain('2 KB')
    expect(document.body.textContent).toContain('6')
    expect(document.body.textContent).toContain('累计标准错误')

    const runButtons = wrapper
      .findAll('button')
      .filter((button) => button.text().includes('#shared-r'))
    expect(runButtons).toHaveLength(2)
    await runButtons[1]!.trigger('click')
    expect(mocks.router.push).toHaveBeenCalledWith({
      name: 'WorkspaceRuns',
      params: { id: 'project-b' },
      query: { runId: 'shared-run' },
    })
  })

  it('renders starting and stopping dispatcher states with accurate tones', async () => {
    const data = mocks.data as ReturnType<typeof ref<MonitoringSummary | undefined>>
    const starting = summary()
    starting.dispatcher.effective_status = 'starting'
    data.value = starting
    wrapper = mount(Monitoring, { attachTo: document.body })
    await flushPromises()

    const dispatcherBadge = () =>
      [...document.body.querySelectorAll<HTMLElement>('.ui-badge')].find((badge) =>
        ['启动中', '停止中'].some((label) => badge.textContent?.includes(label)),
      )
    expect(document.body.textContent).toContain('启动中')
    expect(dispatcherBadge()?.classList).toContain('ui-badge--info')

    const stopping = summary()
    stopping.dispatcher.effective_status = 'stopping'
    data.value = stopping
    await flushPromises()
    expect(document.body.textContent).toContain('停止中')
    expect(dispatcherBadge()?.classList).toContain('ui-badge--warning')
  })

  it('hides historical workers by default and reveals them with the shared checkbox', async () => {
    wrapper = mount(Monitoring, { attachTo: document.body })
    await flushPromises()
    expect(document.body.textContent).not.toContain('retired-worker')

    await wrapper.get('input[type="checkbox"]').setValue(true)
    expect(document.body.textContent).toContain('retired-worker')
  })

  it('shows an initial error and preserves stale data on refresh failure', async () => {
    const data = mocks.data as ReturnType<typeof ref<MonitoringSummary | undefined>>
    const error = mocks.error as ReturnType<typeof ref<unknown>>
    const isError = mocks.isError as ReturnType<typeof ref<boolean>>
    data.value = undefined
    error.value = new Error('offline')
    isError.value = true
    wrapper = mount(Monitoring, { attachTo: document.body })
    await flushPromises()
    expect(document.body.textContent).toContain('监控数据加载失败')
    expect(document.body.textContent).toContain('offline')

    data.value = summary()
    await flushPromises()
    expect(document.body.textContent).toContain('实时刷新失败，保留上次监控快照')
    expect(document.body.textContent).toContain('Project A')
  })
})
