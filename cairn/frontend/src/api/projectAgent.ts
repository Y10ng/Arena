import { api, apiRaw } from './client'
import type { VerificationResult } from './verificationRequests'

export interface AgentConversation {
  id: string
  project_id: string
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

export interface AgentMessage {
  id: number
  client_message_id: string
  text: string
  created_at: string
  task_run_id: string | null
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  answer: string | null
  error_code: string | null
  artifacts: { id: string; title: string; download_url: string }[]
  actions: AgentAction[]
  attachments: AgentAttachment[]
}

const conversationsPath = (projectId: string) =>
  `/projects/${encodeURIComponent(projectId)}/agent-conversations`
const conversationPath = (projectId: string, conversationId: string) =>
  `${conversationsPath(projectId)}/${encodeURIComponent(conversationId)}`

export const listAgentConversations = (projectId: string, signal?: AbortSignal) =>
  api<AgentConversation[]>('GET', conversationsPath(projectId), undefined, signal)

export const createAgentConversation = (projectId: string) =>
  api<AgentConversation>('POST', conversationsPath(projectId), {})

export const listAgentMessages = (projectId: string, conversationId: string, signal?: AbortSignal) =>
  api<AgentMessage[]>('GET', `${conversationPath(projectId, conversationId)}/messages?after=0`, undefined, signal)

export const listAgentWork = (projectId: string, conversationId: string, signal?: AbortSignal) =>
  api<AgentWork[]>('GET', `${conversationPath(projectId, conversationId)}/actions`, undefined, signal)

export const deleteAgentConversation = (projectId: string, conversationId: string) =>
  api<void>('DELETE', conversationPath(projectId, conversationId))

export const listAgentAttachments = (projectId: string, conversationId: string, signal?: AbortSignal) =>
  api<AgentAttachment[]>('GET', `${conversationPath(projectId, conversationId)}/attachments`, undefined, signal)

export const getAgentAttachmentLimits = (projectId: string, conversationId: string, signal?: AbortSignal) =>
  api<AgentAttachmentLimits>('GET', `${conversationPath(projectId, conversationId)}/attachment-limits`, undefined, signal)

export const uploadAgentAttachment = (projectId: string, conversationId: string, file: File) =>
  apiRaw<AgentAttachment>('POST', `${conversationPath(projectId, conversationId)}/attachments?filename=${encodeURIComponent(file.name)}`, file)

export const deleteAgentAttachment = (projectId: string, conversationId: string, id: string) =>
  api<void>('DELETE', `${conversationPath(projectId, conversationId)}/attachments/${encodeURIComponent(id)}`)

export const sendAgentMessage = (projectId: string, conversationId: string, body: { client_message_id: string; text: string; attachment_ids: string[] }) =>
  api<AgentMessage>('POST', `${conversationPath(projectId, conversationId)}/messages`, body)

export const agentEventsUrl = (projectId: string, conversationId: string, after: string) =>
  `${conversationPath(projectId, conversationId)}/events?after=${encodeURIComponent(after)}`

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
