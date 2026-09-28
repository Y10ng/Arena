export type ModelProtocol =
  | 'anthropic-messages'
  | 'openai-chat-completions'
  | 'openai-responses'

export type ModelAuthHeader = 'authorization-bearer' | 'x-api-key'

export interface CustomModelHeader {
  name: string
  value: string
}

export interface TenantModelConfig {
  display_name: string
  protocol: ModelProtocol
  base_url: string
  auth_header: ModelAuthHeader
  custom_headers: CustomModelHeader[]
  models_endpoint: string | null
  inference_endpoint: string | null
  model_name: string
  enabled: boolean
}

export interface TenantModel extends TenantModelConfig {
  id: string
  revision: number
  credential_configured: boolean
  credential_version: number | null
  credential_key_version: string | null
  created_at: string
  updated_at: string
}

export interface CreateTenantModelRequest extends TenantModelConfig {
  api_key?: string
}

export interface UpdateTenantModelRequest extends TenantModelConfig {
  expected_revision: number
  api_key?: string
}

export interface DraftTenantModelRequest extends TenantModelConfig {
  model_id?: string
  api_key?: string
  timeout_seconds?: number
}

export interface DiscoverTenantModelRequest extends DraftTenantModelRequest {}

export interface TestTenantModelRequest extends DraftTenantModelRequest {
  prompt: string
}

export interface ModelDiscoveryResult {
  model_names: string[]
  http_status: number
  latency_ms: number
  endpoint: string
}

export interface ModelTestResult {
  success: boolean
  http_status: number
  latency_ms: number
  endpoint: string
  response_body: string
}
