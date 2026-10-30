export interface InfrastructureWorker {
  worker_id?: string | null
  worker_name: string
  engine?: string | null
  worker_type: string
  model_id?: string
  task_types: string[]
  priority: number
  concurrency?: number | null
  max_running: number
  enabled?: boolean
  running_count: number
  supports_resume?: boolean | null
  supports_json_mode?: boolean | null
  max_context?: number | null
  env_keys?: string[]
  source: 'dispatcher_report'
}

export interface InfrastructureImage {
  image: string
  source: 'dispatcher_report'
}

export interface InfrastructureCapability {
  name: string
  source: 'declared_inventory' | 'dispatcher_report' | 'unavailable'
}
