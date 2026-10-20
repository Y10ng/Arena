import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'

vi.mock('@/api/client', () => ({ api: vi.fn() }))
afterEach(() => vi.clearAllMocks())
import type { ProjectDetail } from '@/api/types'
import { resolveProjectStatus } from './workspaceStatus'
import { runtimeFixture, runtimeV2Fixture } from './projectRuntimeFixtures'
import {
  getProjectRuntimes,
  projectActionAllowed,
  runtimeRequestError,
  runtimeSnapshotExpired,
} from './projectRuntime'

const project = (status: ProjectDetail['project']['status'] = 'active') => ({ id: 'p1', status })

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

  it('offers ordinary actions from project metadata despite absent, expired or incompatible queries', () => {
    for (const runtime of [
      undefined,
      runtimeFixture({ observed_at: '2020-01-01T00:00:00Z' }),
      runtimeFixture({ schema_version: 99 as 1 }),
    ]) {
      expect(projectActionAllowed(runtime, 'pause', true, project())).toBe(true)
      expect(projectActionAllowed(runtime, 'pause', false, project('paused'))).toBe(false)
      expect(projectActionAllowed(runtime, 'resume', true, project('paused'))).toBe(true)
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
    expect(runtimeSnapshotExpired(runtime)).toBe(true)
    expect(projectActionAllowed(runtime, 'reauthorize', false, project('paused'))).toBe(false)
    expect(resolveProjectStatus('paused').label).toBe('已暂停')
    const future = runtimeFixture({ observed_at: new Date(Date.now() + 60_000).toISOString() })
    expect(runtimeSnapshotExpired(future)).toBe(true)
    expect(projectActionAllowed(future, 'pause', true, project())).toBe(true)
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
    expect(runtimeRequestError(new Error('project_authorization_expired'))).toContain('项目授权已过期')
  })
})
