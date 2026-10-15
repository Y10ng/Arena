import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'

vi.mock('@/api/client', () => ({ api: vi.fn() }))
afterEach(() => vi.clearAllMocks())
import type { ProjectDetail, ProjectSummary } from '@/api/types'
import {
  resolveRuntimeStatus,
  resolveProjectDetailStatus,
  resolveProjectSummaryStatus,
} from './workspaceStatus'
import { runtimeFixture, runtimeV2Fixture } from './projectRuntimeFixtures'
import { getProjectRuntimes, projectActionAllowed, runtimeRequestError } from './projectRuntime'

const detail = (
  project: Partial<ProjectDetail['project']> = {},
  intents: ProjectDetail['intents'] = [],
): ProjectDetail => ({
  project: {
    id: 'p1',
    title: 'P1',
    status: 'active',
    created_at: '2026-01-01T00:00:00Z',
    scenario_key: 'default-copy-v2',
    tenant_workers: [],
    tenant_id: 'tenant-1',
    tenant_name: 'Tenant 1',
    assessment_protocol: 'default-copy-v1',
    ...project,
    report_capability: project.report_capability ?? {
      supported: true,
      policy_version: 1,
      template_profile: 'assessment-report-packet-v1',
      reason: null,
    },
  },
  facts: [],
  intents,
  hints: [],
  targets: [],
  resources: [],
  library_bindings: [],
})

const summary = (values: Partial<ProjectSummary> = {}): ProjectSummary => ({
  id: 'p1',
  title: 'P1',
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
  scenario_key: 'default-copy-v2',
  assessment_protocol: 'default-copy-v1',
  tenant_workers: [],
  tenant_id: 'tenant-1',
  tenant_name: 'Tenant 1',
  fact_count: 0,
  intent_count: 0,
  working_intent_count: 0,
  unclaimed_intent_count: 0,
  hint_count: 0,
  ...values,
  report_capability: values.report_capability ?? {
    supported: true,
    policy_version: 1,
    template_profile: 'assessment-report-packet-v1',
    reason: null,
  },
})

const intent = (values: Partial<ProjectDetail['intents'][number]> = {}) => ({
  id: 'i1',
  project_id: 'p1',
  from: ['origin'],
  description: '探索',
  creator: 'reason',
  created_at: '2026-01-01T00:00:00Z',
  ...values,
})

describe('authoritative project runtime presentation', () => {
  it('deduplicates and chunks runtime batches at the server limit without truncating', async () => {
    vi.mocked(api).mockImplementation(async (_method, path) => {
      const ids = new URL(path, 'https://example.test').searchParams.get('project_ids')!.split(',')
      return { items: ids.map((project_id) => runtimeFixture({ project_id })) } as never
    })
    const ids = Array.from({ length: 120 }, (_, index) => `project-${index}`)
    const result = await getProjectRuntimes([...ids, ...ids.slice(0, 20)])
    expect(api).toHaveBeenCalledTimes(2)
    for (const [, path] of vi.mocked(api).mock.calls) {
      expect(new URL(path, 'https://example.test').searchParams.get('schema_version')).toBe('2')
    }
    expect(result.items.map((runtime) => runtime.project_id)).toEqual(ids)
  })

  it('shows a blocker with no TaskRun instead of inferring scheduling from open intents', () => {
    const runtime = runtimeFixture()
    const result = resolveProjectDetailStatus({ detail: detail({}, [intent()]), runtime })
    expect(result).toMatchObject({
      code: 'blocked',
      label: '配置阻塞',
      detail: runtime.message,
      workerNames: [],
    })
    expect(
      resolveProjectSummaryStatus({ project: summary({ unclaimed_intent_count: 1 }), runtime }),
    ).toEqual(result)
  })

  it.each([
    'project_authorization_scope_upgrade_required',
    'project_authorization_scope_changed',
    'project_authorization_expired',
    'project_reauthorization_required',
  ])('shows %s as an authorization blocker rather than Worker waiting', (code) => {
    const runtime = runtimeFixture({
      state: 'blocked',
      code,
      message: '新任务尚未开始，请在当前范围上显式确认授权。',
    })
    expect(resolveRuntimeStatus({ runtime })).toMatchObject({
      label: '授权阻断',
      detail: runtime.message,
      workerNames: [],
    })
    expect(runtimeRequestError({ status: 409, data: { detail: code } })).toContain('授权')
  })

  it('uses the shared v1 fields from a negotiated v2 snapshot', () => {
    const runtime = runtimeV2Fixture({
      project_status: 'paused',
      state: 'paused',
      actions: {
        pause: { allowed: false, reason: null },
        resume: { allowed: true, reason: null },
        reauthorize: { allowed: false, reason: 'project_authorization_current' },
        reopen: { allowed: false, reason: 'project_status_invalid' },
        finish: { allowed: true, reason: null },
      },
    })
    expect(resolveRuntimeStatus({ runtime })).toMatchObject({ code: 'paused', label: '已暂停' })
    expect(projectActionAllowed(runtime, 'resume', false, detail({ status: 'paused' }).project)).toBe(true)
  })

  it('never falls back to intents or a claimed reason when diagnostics are unavailable', () => {
    const result = resolveProjectDetailStatus({
      detail: detail({ reason: { worker: 'old-worker' } }, [intent({ worker: 'old-worker' })]),
      runtimeUnavailable: true,
    })
    expect(result).toMatchObject({ code: 'unknown', label: '状态暂未更新', workerNames: [] })
    expect(result.detail).toContain('状态查询失败')
  })

  it('ignores snapshots belonging to another project', () => {
    expect(
      resolveProjectDetailStatus({
        detail: detail(),
        runtime: runtimeFixture({ project_id: 'other' }),
      }).code,
    ).toBe('unknown')
  })

  it.each(['stopped', 'paused', 'completed'] as const)('keeps %s lifecycle distinct', (state) => {
    const result = resolveRuntimeStatus({
      runtime: runtimeFixture({ project_status: state, state }),
    })
    expect(result.label).toBe({ stopped: '已结束', paused: '已暂停', completed: '已完成' }[state])
  })

  it('distinguishes finish draining from physically stopped execution', () => {
    const runtime = runtimeFixture({
      project_status: 'stopped',
      state: 'stopping',
      code: 'project_finish_draining',
      message: '已要求结束评估，仍在等待原执行退出和资源清理确认',
    })
    expect(resolveRuntimeStatus({ runtime })).toMatchObject({
      code: 'stopping',
      label: '正在结束',
      tone: 'warning',
      detail: runtime.message,
    })
  })

  it('separates stale dispatcher observations from failed state queries', () => {
    const runtime = runtimeFixture({
      dispatcher: { status: 'ok', freshness: 'stale', updated_at: null },
    })
    expect(resolveRuntimeStatus({ runtime })).toMatchObject({ code: 'blocked', degraded: true })
    expect(
      resolveRuntimeStatus({ runtime: runtimeFixture(), runtimeUnavailable: true }).label,
    ).toBe('状态暂未更新')
    expect(resolveRuntimeStatus({ runtime: runtimeFixture({ state: 'paused', dispatcher: runtime.dispatcher }) }))
      .toMatchObject({ code: 'paused', label: '已暂停', degraded: false })
  })

  it('requires process milestone evidence rather than inferring model activity from running', () => {
    const runtime = runtimeFixture({
      state: 'running',
      active_runs: [
        {
          id: 'run1',
          task_type: 'explore',
          status: 'running',
          worker_name: 'pi-worker',
          execution_stage: 'assigned',
          started_at: null,
        },
      ],
    })
    expect(resolveRuntimeStatus({ runtime }).label).toBe('执行已领取')
    runtime.active_runs[0]!.execution_stage = 'running'
    expect(resolveRuntimeStatus({ runtime }).label).toBe('进程已启动')
  })

  it.each([
    ['capacity_full', '等待容量'],
    ['worker_backoff', '退避中'],
  ])('explains scheduling %s', (code, label) => {
    expect(
      resolveRuntimeStatus({ runtime: runtimeFixture({ state: 'waiting', code }) }).label,
    ).toBe(label)
  })

  it('offers ordinary actions from project metadata despite absent, expired or incompatible state queries', () => {
    for (const runtime of [
      undefined,
      runtimeFixture({ observed_at: '2020-01-01T00:00:00Z' }),
      runtimeFixture({ schema_version: 99 as 1 }),
    ]) {
      expect(projectActionAllowed(runtime, 'pause', true, detail().project)).toBe(true)
      expect(
        projectActionAllowed(runtime, 'pause', false, detail({ status: 'paused' }).project),
      ).toBe(false)
      expect(
        projectActionAllowed(runtime, 'resume', true, detail({ status: 'paused' }).project),
      ).toBe(true)
      expect(projectActionAllowed(runtime, 'finish', true, detail().project)).toBe(true)
    }
    expect(
      projectActionAllowed(
        runtimeFixture({
          actions: {
            pause: { allowed: false, reason: 'assessment_report_finalization_in_progress' },
            resume: { allowed: false, reason: 'project_not_paused' },
            reauthorize: { allowed: false, reason: 'project_status_invalid' },
            reopen: { allowed: false, reason: 'project_status_invalid' },
            finish: { allowed: false, reason: 'assessment_report_finalization_in_progress' },
          },
        }),
        'pause',
        false,
        detail().project,
      ),
    ).toBe(true)
    expect(projectActionAllowed(runtimeFixture(), 'pause')).toBe(false)
  })

  it('lets the server reject an unsafe resume instead of trusting inconsistent observations', () => {
    const runtime = runtimeFixture({
      project_status: 'paused',
      state: 'pausing',
      actions: {
        pause: { allowed: false, reason: null },
        resume: { allowed: true, reason: null },
        reauthorize: { allowed: false, reason: 'project_authorization_current' },
        reopen: { allowed: false, reason: 'project_status_invalid' },
        finish: { allowed: true, reason: null },
      },
    })
    expect(projectActionAllowed(runtime, 'resume', false, detail({ status: 'paused' }).project)).toBe(true)
    runtime.project_status = 'stopped'
    expect(projectActionAllowed(runtime, 'resume', false, detail({ status: 'stopped' }).project)).toBe(false)
    expect(projectActionAllowed(runtime, 'finish', true, detail({ status: 'stopped' }).project)).toBe(false)
    expect(resolveRuntimeStatus({ runtime: runtimeFixture({ state: 'future-state' }) }).label).toBe(
      '尚未观测',
    )
  })

  it('does not use incomplete state structures for reauthorization', () => {
    const unknownSchema = runtimeFixture({ schema_version: 99 as 1 })
    const missingActions = runtimeFixture({ actions: undefined as never })
    const missingRuns = runtimeFixture({ active_runs: undefined as never })
    for (const runtime of [unknownSchema, missingActions, missingRuns]) {
      expect(projectActionAllowed(runtime, 'reauthorize', false, detail({ status: 'paused' }).project)).toBe(false)
    }
    expect(resolveRuntimeStatus({ runtime: unknownSchema }).code).toBe('unknown')
    expect(resolveRuntimeStatus({ runtime: missingRuns }).code).toBe('unknown')
  })

  it('allows a normal action despite a future-dated state response', () => {
    const runtime = runtimeFixture({ observed_at: new Date(Date.now() + 60_000).toISOString() })
    expect(projectActionAllowed(runtime, 'pause', true, detail().project)).toBe(true)
  })

  it('maps stable error codes to Chinese without displaying raw exception content', () => {
    expect(runtimeRequestError(new Error('project_pause_draining'))).toContain('旧执行仍在取消')
    expect(runtimeRequestError(new Error('project_pause_draining'))).toContain(
      '关联码：project_pause_draining',
    )
    expect(
      runtimeRequestError({ data: { detail: { code: 'project_pause_unsupported' } } }),
    ).toContain('升级')
    expect(runtimeRequestError(new Error('/secret/token=canary'))).not.toContain('canary')
    expect(runtimeRequestError({ status: 403 })).toContain('权限')
    for (const code of [
      'explore_deadline_exceeded',
      'project_deadline_exceeded',
      'assessment_deadline_exceeded',
    ]) {
      const message = runtimeRequestError({ status: 409, data: { detail: code } })
      expect(message).toContain('已到执行停止时间')
      expect(message).toContain('重新授权不会延长此时间')
      expect(message).toContain(`关联码：${code}`)
      expect(message).not.toContain('授权时间窗已到期')
    }
    expect(runtimeRequestError(new Error('project_authorization_expired'))).toContain(
      '项目授权已过期',
    )
  })
})
