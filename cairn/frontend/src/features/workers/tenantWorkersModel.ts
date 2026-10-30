import type { TenantWorker, UpdateTenantWorkerRequest } from '@/api/types'

export const TENANT_WORKER_MAX_RUNNING = { min: 1, max: 64 } as const
export const TENANT_WORKER_PRIORITY = { min: -10_000, max: 10_000 } as const

const isIntegerInRange = (value: unknown, min: number, max: number) =>
  typeof value === 'number' && Number.isFinite(value) && Number.isInteger(value) && value >= min && value <= max

export const tenantWorkerDraftIsValid = (draft: UpdateTenantWorkerRequest) =>
  (draft.model_id === null || Boolean(draft.model_id)) &&
  isIntegerInRange(draft.max_running, TENANT_WORKER_MAX_RUNNING.min, TENANT_WORKER_MAX_RUNNING.max) &&
  isIntegerInRange(draft.priority, TENANT_WORKER_PRIORITY.min, TENANT_WORKER_PRIORITY.max)

export const tenantWorkerDraft = (worker: TenantWorker): UpdateTenantWorkerRequest => ({
  model_id: worker.model_id,
  max_running: worker.max_running,
  priority: worker.priority,
  enabled: worker.enabled,
  expected_revision: worker.revision,
})

const unavailableLabels: Record<string, string> = {
  engine_unsupported: '引擎不受支持',
  worker_disabled: '已禁用',
  model_unconfigured: '未配置模型',
  model_disabled: '模型已禁用',
  model_protocol_unsupported: '模型协议不受支持',
  model_incomplete: '模型配置未完成',
  credential_missing: '缺少模型凭据',
}

export const tenantWorkerUnavailableLabel = (reason: string | null) =>
  unavailableLabels[reason || ''] || '不可用'

export const tenantWorkerStatusLabel = (worker: TenantWorker) => {
  if (!worker.available) return tenantWorkerUnavailableLabel(worker.unavailable_reason)
  return worker.executor_available ? '配置就绪' : '配置就绪 · Dispatcher 不可用'
}
