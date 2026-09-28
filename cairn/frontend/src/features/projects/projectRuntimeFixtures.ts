import type { ProjectRuntimeV1, ProjectRuntimeV2 } from '@/api/types'

// 合成诊断覆盖真实协议字段，避免测试绕过运行状态解析器。
export const runtimeFixture = (
  overrides: Partial<ProjectRuntimeV1> = {},
): ProjectRuntimeV1 => ({
  schema_version: 1,
  project_id: 'p1',
  observed_at: new Date().toISOString(),
  execution_generation: 1,
  project_status: 'active',
  state: 'blocked',
  code: 'worker_unavailable',
  message: '当前没有可用的 Worker，请检查项目绑定与模型配置。',
  dispatcher: { status: 'ok', freshness: 'fresh', updated_at: new Date().toISOString() },
  scheduling: { code: 'worker_unavailable', observed_at: new Date().toISOString(), retry_at: null },
  active_runs: [],
  last_error: null,
  last_activity: null,
  actions: {
    pause: { allowed: true, reason: null },
    resume: { allowed: false, reason: 'project_not_paused' },
    reauthorize: { allowed: false, reason: 'project_status_invalid' },
    reopen: { allowed: false, reason: 'project_status_invalid' },
    finish: { allowed: true, reason: null },
  },
  ...overrides,
})

export const runtimeV2Fixture = (
  overrides: Partial<ProjectRuntimeV2> = {},
): ProjectRuntimeV2 => {
  const {
    schema_version: _schemaVersion,
    axes,
    checkpoint_staging: checkpointStaging,
    recovery,
    ...baseOverrides
  } = overrides
  return {
    ...runtimeFixture(baseOverrides),
    schema_version: 2,
    axes: axes ?? {
      task_run_id: null,
      execution: { state: 'blocked', code: 'worker_unavailable' },
      delivery: { state: 'none', code: null },
      acceptance: { state: 'not_evaluated', code: null, evidence: null },
      process: { state: 'unobserved', code: null, termination_confirmed: false },
      resources: { state: 'unconfirmed', code: null, cleanup_confirmed: false },
    },
    checkpoint_staging: checkpointStaging ?? {
      state: 'none',
      received_count: 0,
      current_generation_count: 0,
      historical_generation_count: 0,
      last_received_at: null,
      last_task_run_id: null,
      business_acceptance: 'not_evaluated',
    },
    recovery: recovery ?? { route: 'none', code: null },
  }
}
