import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'

vi.mock('@/api/client', () => ({ api: vi.fn() }))
afterEach(() => vi.clearAllMocks())
import type { ProjectDetail, ProjectExecutionControl } from '@/api/types'
import {
  resolveExecutionControl,
  resolveProjectStatus,
  resolveRuntimeObservation,
} from './workspaceStatus'
import { runtimeFixture, runtimeV2Fixture } from './projectRuntimeFixtures'
import {
  executionControlConfirmed,
  getProjectRuntimes,
  projectActionAllowed,
  runtimeRequestError,
  runtimeSnapshotExpired,
} from './projectRuntime'

const project = (status: ProjectDetail['project']['status'] = 'active') => ({ id: 'p1', status })
const receipt = (overrides: Partial<ProjectExecutionControl> = {}): ProjectExecutionControl => ({
  action: 'pause',
  state: 'paused',
  requested_at: '2026-11-02T00:00:00Z',
  confirmed_at: '2026-11-02T00:00:01Z',
  resources: [{ container_id: 'exact-container', state: 'paused' }],
  ...overrides,
})

describe('execution control request and physical receipt boundaries', () => {
  it.each([
    ['pause', 'paused', 'paused', '物理暂停已确认'],
    ['resume', 'running', 'active', '原地恢复已确认'],
    ['finish', 'stopped', 'stopped', '所属执行终止已确认'],
  ] as const)('recognizes only paired %s receipts', (action, state, status, text) => {
    const control = receipt({ action, state })
    expect(executionControlConfirmed(control, state)).toBe(true)
    expect(
      resolveExecutionControl({ ...project(status), execution_control: control }),
    ).toMatchObject({
      confirmed: true,
      stateText: text,
    })
  })

  it.each([
    { state: 'requested' },
    { action: 'resume' },
    { requested_at: undefined },
    { confirmed_at: null },
    { confirmed_at: 'invalid' },
    { confirmed_at: '2026-11-01T23:59:59Z' },
  ] as Partial<ProjectExecutionControl>[])(
    'does not infer physical success from %j',
    (overrides) => {
      const control = receipt(overrides)
      expect(executionControlConfirmed(control, 'paused')).toBe(false)
      expect(
        resolveExecutionControl({ ...project('paused'), execution_control: control })?.confirmed,
      ).toBe(false)
      expect(
        projectActionAllowed(undefined, 'resume', false, {
          ...project('paused'),
          execution_control: control,
        }),
      ).toBe(false)
      expect(
        projectActionAllowed(undefined, 'finish', true, {
          ...project('paused'),
          execution_control: control,
        }),
      ).toBe(true)
    },
  )

  it('keeps acceptance separate from metadata and retains read failures beside the receipt', () => {
    const subject = { ...project('paused'), execution_control: receipt({ state: 'requested' }) }
    const result = resolveRuntimeObservation(
      subject,
      undefined,
      { failed: true, fetching: false, dataUpdatedAt: 0 },
      Date.now(),
    )
    expect(result.control?.label).toBe('暂停已受理')
    expect(result.notice).toContain('物理暂停待确认')
    expect(result.notice).toContain('观测读取失败')
  })

  it.each(['failed', 'lost'] as const)(
    'shows %s without exposing unknown private errors',
    (state) => {
      const subject = {
        ...project('paused'),
        execution_control: receipt({ state, error: '/secret/token=canary' }),
      }
      const view = resolveExecutionControl(subject)!
      expect(view.confirmed).toBe(false)
      expect(view.tone).toBe('danger')
      expect(view.notice).not.toContain('canary')
      expect(projectActionAllowed(undefined, 'resume', false, subject)).toBe(false)
      expect(projectActionAllowed(undefined, 'finish', false, subject)).toBe(true)
    },
  )

  it('does not claim an original process was resumed when there are no owned resources', () => {
    const view = resolveExecutionControl({
      ...project(),
      execution_control: receipt({ action: 'resume', state: 'running', resources: [] }),
    })!
    expect(view.stateText).toContain('当前无所属执行现场')
    expect(view.stateText).not.toContain('原地恢复已确认')
    expect(resolveExecutionControl({ ...project(), execution_control: {} })).toBeUndefined()
  })

  it('does not override a physical receipt with an old stop/drain projection', () => {
    const subject = { ...project('paused'), execution_control: receipt() }
    const runtime = runtimeFixture({
      project_status: 'paused',
      state: 'pausing',
      code: 'project_pause_draining',
    })
    const view = resolveRuntimeObservation(
      subject,
      runtime,
      { failed: false, fetching: false, dataUpdatedAt: Date.now() },
      Date.now(),
    )
    expect(view.notice).toBe('')
    expect(view.control?.stateText).toBe('物理暂停已确认')
  })
})

describe('project metadata status and independent runtime action boundaries', () => {
  it.each([
    ['pending', '待启动', 'info'],
    ['active', '进行中', 'info'],
    ['paused', '已暂停', 'neutral'],
    ['stopped', '已结束', 'neutral'],
    ['completed', '已完成', 'success'],
  ])('maps %s directly to the shared short label', (status, label, tone) => {
    expect(resolveProjectStatus(status)).toEqual({ label, tone })
  })

  it('does not turn an unknown project status into a completed or active project', () => {
    expect(resolveProjectStatus('future-state')).toEqual({ label: '未知状态', tone: 'neutral' })
  })

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

  it.each([
    'project_authorization_scope_upgrade_required',
    'project_authorization_scope_changed',
    'project_authorization_expired',
    'project_reauthorization_required',
  ])('keeps %s available as an action error', (code) => {
    expect(runtimeRequestError({ status: 409, data: { detail: code } })).toContain('授权')
  })

  it('uses the negotiated v2 snapshot for runtime-dependent actions', () => {
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
    expect(projectActionAllowed(runtime, 'resume', false, project('paused'))).toBe(true)
    expect(projectActionAllowed(runtime, 'reauthorize', false, project('paused'))).toBe(false)
  })

  it('offers pause and finish from metadata but requires fresh Server admission for resume', () => {
    for (const runtime of [
      undefined,
      runtimeFixture({ observed_at: '2020-01-01T00:00:00Z' }),
      runtimeFixture({ schema_version: 99 as 1 }),
    ]) {
      expect(projectActionAllowed(runtime, 'pause', true, project())).toBe(true)
      expect(projectActionAllowed(runtime, 'pause', false, project('paused'))).toBe(false)
      expect(projectActionAllowed(runtime, 'resume', true, project('paused'))).toBe(false)
      expect(
        projectActionAllowed(runtime, 'resume', true, {
          ...project('paused'),
          execution_control: receipt(),
        }),
      ).toBe(false)
      expect(projectActionAllowed(runtime, 'finish', true, project())).toBe(true)
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
        project(),
      ),
    ).toBe(true)
    expect(projectActionAllowed(runtimeFixture(), 'pause')).toBe(false)
  })

  it.each([undefined, {}])('does not offer legacy resume without fresh Server admission (%j)', (control) => {
    const subject = { ...project('paused'), execution_control: control }
    const runtime = runtimeV2Fixture({
      project_status: 'paused',
      state: 'blocked',
      actions: {
        pause: { allowed: true, reason: null },
        resume: { allowed: false, reason: 'execution_scene_unavailable' },
        reauthorize: { allowed: false, reason: null },
        reopen: { allowed: false, reason: null },
        finish: { allowed: true, reason: null },
      },
    })
    expect(projectActionAllowed(runtime, 'resume', false, subject)).toBe(false)
    expect(projectActionAllowed(runtime, 'finish', false, subject)).toBe(true)
    expect(runtimeRequestError({ status: 409, data: { detail: 'execution_scene_unavailable' } }))
      .toContain('没有原执行的热暂停回执')
    runtime.actions.resume = { allowed: true, reason: null }
    expect(projectActionAllowed(runtime, 'resume', false, subject)).toBe(true)
    expect(projectActionAllowed(runtime, 'resume', true, subject)).toBe(false)
    runtime.observed_at = '2020-01-01T00:00:00Z'
    expect(projectActionAllowed(runtime, 'resume', false, subject)).toBe(true)
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
    expect(projectActionAllowed(runtime, 'resume', false, project('paused'))).toBe(true)
    runtime.project_status = 'stopped'
    expect(projectActionAllowed(runtime, 'resume', false, project('stopped'))).toBe(false)
    expect(projectActionAllowed(runtime, 'finish', true, project('stopped'))).toBe(false)
  })

  it('does not use incomplete state structures for reauthorization', () => {
    for (const runtime of [
      runtimeFixture({ schema_version: 99 as 1 }),
      runtimeFixture({ actions: undefined as never }),
      runtimeFixture({ active_runs: undefined as never }),
    ]) {
      expect(projectActionAllowed(runtime, 'reauthorize', false, project('paused'))).toBe(false)
    }
  })

  it('keeps the runtime TTL for actions without applying it to project labels', () => {
    const runtime = runtimeFixture({
      state: 'paused',
      project_status: 'paused',
      observed_at: new Date(Date.now() - 60_001).toISOString(),
      actions: {
        pause: { allowed: false, reason: null },
        resume: { allowed: true, reason: null },
        reauthorize: { allowed: true, reason: null },
        reopen: { allowed: false, reason: null },
        finish: { allowed: true, reason: null },
      },
    })
    const expired = runtimeSnapshotExpired(Date.now() - 60_001)
    expect(expired).toBe(true)
    expect(projectActionAllowed(runtime, 'reauthorize', expired, project('paused'))).toBe(false)
    expect(resolveProjectStatus('paused').label).toBe('已暂停')
    const future = runtimeFixture({ observed_at: new Date(Date.now() + 120_000).toISOString() })
    expect(runtimeSnapshotExpired(Date.now())).toBe(false)
    expect(runtimeSnapshotExpired(0)).toBe(true)
    expect(projectActionAllowed(future, 'pause', true, project())).toBe(true)
  })

  it('maps stable error codes to Chinese without displaying raw exception content', () => {
    expect(runtimeRequestError(new Error('project_pause_draining'))).toContain('物理冻结尚未确认')
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
