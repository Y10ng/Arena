export type TenantWorkerTaskType = 'reason' | 'bootstrap' | 'explore'

export interface TenantWorker {
  id: string
  display_name: string
  model_id: string
  model_display_name: string
  model_name: string
  // 历史引擎按真实值展示，不代表可执行。
  engine: string
  task_types: TenantWorkerTaskType[]
  max_running: number
  priority: number
  enabled: boolean
  available: boolean
  unavailable_reason: string | null
  executor_available: boolean
  revision: number
  created_at: string
  updated_at: string
}

export interface CreateTenantWorkerRequest {
  engine?: 'pi'
  display_name: string
  model_id: string
  task_types: TenantWorkerTaskType[]
  max_running: number
  priority: number
  enabled: boolean
}

export interface UpdateTenantWorkerRequest extends CreateTenantWorkerRequest {
  expected_revision: number
}
