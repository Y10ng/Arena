import { api, apiRaw } from './client'
import type { VerificationResult } from './verificationRequests'
import type { ConsumptionSummary } from './types/consumption'

export interface AgentConversation {
  id: string
  project_id: string | null
  model_id: string | null
  model_display_name?: string | null
  model_name?: string | null
  available?: boolean
  unavailable_reason?: string | null
  created_at: string
}

export interface AgentConfiguration {
  model_id: string | null
  model_display_name: string | null
  model_name: string | null
  available: boolean
  unavailable_reason: string | null
}

export const getAgentConfiguration = () => api<AgentConfiguration>('GET', '/project-agent/configuration')
export const saveAgentConfiguration = (modelId: string) =>
  api<AgentConfiguration>('PUT', '/project-agent/configuration', { model_id: modelId })

export interface AgentAction {
  id: string
  kind: 'verification' | 'report' | 'project' | 'intent'
  title: string
  source_vulnerability_id?: string | null
  source_observation_id?: string | null
}

export interface AgentWork {
  id: string
  kind: 'verification' | 'report' | 'project' | 'intent'
  title: string
  status: string
  error: string | null
  url: string
  download_url: string | null
  result: Pick<VerificationResult, 'outcome' | 'summary' | 'tested_scope' | 'limitations' | 'reason_code'>
    | { project_id: string; project_status: string; intent_id?: string; conclusion?: string | null }
    | null
  next_attempt_at: string | null
  generation_token?: string
  accepted_generation_token?: string | null
}

export interface AgentAttachment {
  id: string
  filename: string
  size: number
  download_url: string
  message_id: number | null
}

export interface AgentAttachmentLimits {
  max_file_bytes: number
  max_count: number
  max_total_bytes: number
}

export interface AgentArtifact {
  id: string
  title: string
  download_url: string
  source_project_id?: string
  sources?: {
    verified: { label: string; scope: string; version: string; verified_at: string; limitations?: string; reference: Record<string, unknown>; material_version?: Record<string, unknown> }[]
    declared: string[]
    declared_read_at: string | null
    limitations: string[]
  }
}

export interface AgentMessage {
  id: number
  client_message_id: string
  project_id: string | null
  text: string
  created_at: string
  task_run_id: string | null
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  answer: string | null
  error_code: string | null
  artifacts: AgentArtifact[]
  actions: AgentAction[]
  attachments: AgentAttachment[]
  usage?: {
    input_tokens: number | null
    output_tokens: number | null
    cache_read_tokens: number | null
    cache_write_tokens: number | null
  } | null
  consumption?: ConsumptionSummary | null
  consumption_error?: boolean
  unobserved_consumption_bindings?: number | null
  usage_missing_responses?: number | null
  cancel_requested_at?: string | null
}

const conversationsPath = '/agent-conversations'
const conversationPath = (conversationId: string) =>
  `${conversationsPath}/${encodeURIComponent(conversationId)}`

export const listAgentConversations = (signal?: AbortSignal) =>
  api<AgentConversation[]>('GET', conversationsPath, undefined, signal)

export const createAgentConversation = (projectId: string | null) =>
  api<AgentConversation>('POST', conversationsPath, { project_id: projectId })

export const listAgentMessages = (conversationId: string, signal?: AbortSignal) =>
  api<AgentMessage[]>('GET', `${conversationPath(conversationId)}/messages?after=0`, undefined, signal)

export const listAgentWork = (conversationId: string, signal?: AbortSignal) =>
  api<AgentWork[]>('GET', `${conversationPath(conversationId)}/actions`, undefined, signal)

export interface AgentCleanup {
  request_id: string
  state: 'requested' | 'completed' | 'failed'
  attempts: number
  retry_at: number
  error_code: string | null
  finished_at: string | null
}
export interface AgentCleanupResult { conversation_id: string; cleanup: AgentCleanup | null }

export const deleteAgentConversation = (conversationId: string) =>
  api<AgentCleanupResult>('DELETE', conversationPath(conversationId))

export const listAgentCleanupRequests = (signal?: AbortSignal) =>
  api<AgentCleanupResult[]>('GET', `${conversationsPath}/cleanup-requests`, undefined, signal)

export const getAgentCleanup = (conversationId: string) =>
  api<AgentCleanupResult>('GET', `${conversationPath(conversationId)}/cleanup`)

export const stopAgentTurn = (conversationId: string, messageId: number) =>
  api<AgentMessage>('POST', `${conversationPath(conversationId)}/messages/${messageId}/stop`)

export const listAgentAttachments = (conversationId: string, signal?: AbortSignal) =>
  api<AgentAttachment[]>('GET', `${conversationPath(conversationId)}/attachments`, undefined, signal)

export const getAgentAttachmentLimits = (conversationId: string, signal?: AbortSignal) =>
  api<AgentAttachmentLimits>('GET', `${conversationPath(conversationId)}/attachment-limits`, undefined, signal)

export const uploadAgentAttachment = (conversationId: string, file: File) =>
  apiRaw<AgentAttachment>('POST', `${conversationPath(conversationId)}/attachments?filename=${encodeURIComponent(file.name)}`, file)

export const deleteAgentAttachment = (conversationId: string, id: string) =>
  api<void>('DELETE', `${conversationPath(conversationId)}/attachments/${encodeURIComponent(id)}`)

export interface AgentMessageInput {
  client_message_id: string
  text: string
  attachment_ids: string[]
  project_id: string | null
}
export const sendAgentMessage = (conversationId: string, body: AgentMessageInput) =>
  api<AgentMessage>('POST', `${conversationPath(conversationId)}/messages`, body)

export const agentEventsUrl = (conversationId: string, after: string) =>
  `${conversationPath(conversationId)}/events?after=${encodeURIComponent(after)}`

export function agentDownloadUrl(value: string): string | undefined {
  try {
    const url = new URL(value, window.location.origin)
    if (url.origin === window.location.origin && ['http:', 'https:'].includes(url.protocol)) {
      return url.href
    }
  } catch {
    // 无效成品地址只显示不可下载，不从模型正文猜测替代链接。
  }
  return undefined
}
