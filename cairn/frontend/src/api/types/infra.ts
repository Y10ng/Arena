export interface DispatcherYamlDocument {
  path: string
  content: string
  revision: string
}

export interface DispatcherConfigDocument {
  path: string
  content: string
  config: Record<string, any>
  revision: string
}

export interface DispatcherProvider {
  id: string
  base_url: string
  provider_api: 'anthropic-messages' | 'openai-responses' | 'openai-chat-completions'
  credential_ref: string
  credential_configured: boolean
  credential_env_override: boolean
  credential_warning?: string | null
}

export type DispatcherProviderUpdate = Pick<DispatcherProvider, 'id' | 'base_url' | 'provider_api'>

export interface DispatcherModel {
  id: string
  provider_id: string
  upstream_model: string
  context_window?: number | null
}

export interface DispatcherWorkerConfig {
  id?: string
  name: string
  // 读取历史配置时保留原始 engine；写入类型在下方单独约束。
  engine?: string
  type?: string
  model_id?: string
  task_types: Array<'bootstrap' | 'reason' | 'explore'>
  concurrency?: number
  max_running?: number
  priority: number
  enabled?: boolean
  env?: Record<string, string>
}

export interface DispatcherManagedWorkerUpdate {
  id: string
  name: string
  engine: 'pi'
  model_id: string
  task_types: Array<'bootstrap' | 'reason' | 'explore'>
  concurrency: number
  priority: number
  enabled: boolean
}

export interface DispatcherControlCollection<T> {
  revision: string
  items: T[]
}

export interface DispatcherCredentialStatus {
  provider_id: string
  credential_ref: string
  configured: boolean
  revision: string
}

export type ConnectivityTargetType = 'provider' | 'model' | 'worker'
export type ConnectivityStatus = 'queued' | 'running' | 'completed'
export type ConnectivityCategory = 'dns' | 'connect' | 'tls' | 'auth' | 'not_found' | 'rate_limited' | 'model' | 'protocol' | 'timeout' | 'ok'

export interface DispatcherConnectivityAccepted {
  test_id: string
  status: 'queued'
  poll_url: string
}

export interface DispatcherConnectivityTest {
  id: string
  target_type: ConnectivityTargetType
  target_id: string
  provider_id?: string | null
  model_id?: string | null
  scope: 'messages_api' | 'transport_endpoint_only'
  status: ConnectivityStatus
  category?: ConnectivityCategory | null
  detail: string
  http_status?: number | null
  latency_ms?: number | null
  config_revision: string
  credential_fingerprint?: string | null
  stale: boolean
  created_at: string
  started_at?: string | null
  finished_at?: string | null
}

export interface ProjectWorkerOption {
  worker_name: string
  worker_type: string
  model_id?: string
  task_types: string[]
  enabled?: boolean
}

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
