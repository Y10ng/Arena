import type { TenantWorker, TenantWorkerTaskType } from '@/api/types'

export interface TenantWorkerDraft {
  display_name: string
  model_id: string
  task_types: TenantWorkerTaskType[]
  max_running: number
  priority: number
  enabled: boolean
}

export const TENANT_WORKER_MAX_RUNNING = { min: 1, max: 64 } as const
export const TENANT_WORKER_PRIORITY = { min: -10_000, max: 10_000 } as const

const isIntegerInRange = (value: unknown, min: number, max: number) =>
  typeof value === 'number' && Number.isFinite(value) && Number.isInteger(value) && value >= min && value <= max

export const tenantWorkerDraftIsValid = (draft: TenantWorkerDraft) =>
  Boolean(draft.display_name.trim() && draft.model_id && draft.task_types.length) &&
  isIntegerInRange(draft.max_running, TENANT_WORKER_MAX_RUNNING.min, TENANT_WORKER_MAX_RUNNING.max) &&
  isIntegerInRange(draft.priority, TENANT_WORKER_PRIORITY.min, TENANT_WORKER_PRIORITY.max)

export const defaultTenantWorkerDraft = (modelId = ''): TenantWorkerDraft => ({
  display_name: '',
  model_id: modelId,
  task_types: ['reason', 'bootstrap', 'explore'],
  max_running: 1,
  priority: 100,
  enabled: true,
})

export const tenantWorkerDraft = (worker: TenantWorker): TenantWorkerDraft => ({
  display_name: worker.display_name,
  model_id: worker.model_id,
  task_types: [...worker.task_types],
  max_running: worker.max_running,
  priority: worker.priority,
  enabled: worker.enabled,
})

export const selectAfterWorkerListChange = (
  workers: TenantWorker[],
  selectedId: string,
): string => {
  if (workers.some(worker => worker.id === selectedId)) return selectedId
  return workers[0]?.id || ''
}

const unavailableLabels: Record<string, string> = {
  engine_unsupported: '历史引擎不受支持 · 请新建 pi Worker',
  worker_disabled: '已禁用',
  model_disabled: 'Model 已禁用',
  model_protocol_unsupported: 'Model 协议不受支持',
  model_incomplete: 'Model 未完成',
  credential_missing: '缺少 Key',
}

export const tenantWorkerUnavailableLabel = (reason: string | null) =>
  unavailableLabels[reason || ''] || '不可用'

export const tenantWorkerStatusLabel = (worker: TenantWorker) => {
  if (!worker.available) return tenantWorkerUnavailableLabel(worker.unavailable_reason)
  return worker.executor_available ? '可用' : '配置就绪 · Dispatcher 不可用'
}
