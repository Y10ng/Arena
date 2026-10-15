import { api, apiRaw } from './client'
import type { EvidenceRef, ProjectResourceBatch, ProjectResourceBatchMember, ProjectResourceKind } from './types'
import type { Vulnerability } from './client'

export type VerificationMode = 'reproduce' | 'retest'
export type VerificationStatus = 'queued' | 'running' | 'cancelling' | 'completed' | 'failed' | 'cancelled'
export type VerificationOutcome = 'confirmed' | 'not_reproduced' | 'environment_changed' | 'insufficient_evidence'
export type VerificationReportInput =
  | { type: 'markdown' | 'text'; content: string }
  | { type: 'file'; batch_member_id: string }
  | { type: 'platform_vulnerability'; project_id: string; vulnerability_id: string }
export interface VerificationInputSource {
  type: 'platform_vulnerability'
  project_id: string
  vulnerability_id: string
  input_mode: 'current_vulnerability' | 'copied_snapshot'
}
export interface VerificationSourcePreview {
  project_id: string
  vulnerability_id: string
  project_title: string
  vulnerability: Pick<Vulnerability, 'id' | 'title' | 'severity' | 'status' | 'target' | 'remediation' | 'repro_request' | 'repro_response' | 'repro_steps'> & Record<string, unknown>
  facts: { id: string; description: string; [key: string]: unknown }[]
  observations: { id: string; [key: string]: unknown }[]
  verifications: { id: string; outcome: VerificationOutcome; notes: string; [key: string]: unknown }[]
  fact_evidence: { fact_id: string; artifact_id: string; pointer: string; [key: string]: unknown }[]
  artifacts: { artifact_id: string; filename?: string; kind?: string; size_bytes?: number; sha256?: string; available: boolean; [key: string]: unknown }[]
  missing_materials: { code: string; source_type: string; source_id: string; artifact_id?: string; message: string }[]
}
export interface CreateVerificationRequest {
  schema_version: 1
  request_id: string
  external_id: string | null
  mode: VerificationMode
  title: string
  origin: string
  report: VerificationReportInput
  resource_batch_id?: string
  previous_request_id?: string
  authorization_expires_at: string | null
  execution_deadline?: string | null
}
export interface VerificationResult {
  outcome: VerificationOutcome
  summary: string
  tested_scope: string
  notes: string
  limitations: string[]
  reason_code: string | null
  evidence_refs: EvidenceRef[]
}
export interface VerificationError { code: string; message: string }
export interface VerificationSummary {
  schema_version: 1
  id: string
  request_id: string
  external_id: string | null
  title: string
  mode: VerificationMode
  source: { type: 'user' | 'integration'; id: string; name: string }
  input_source: VerificationInputSource | null
  execution_status: VerificationStatus
  result: VerificationResult | null
  error: VerificationError | null
  created_at: string
  updated_at: string
}
export interface VerificationMaterial {
  id: string
  path: string
  size_bytes: number
  sha256: string
  derived_from_resource_id: string | null
}
export interface VerificationArtifact {
  id: string
  filename: string
  kind: string
  size: number
  sha256: string
  task_run_id: string | null
}
export interface VerificationRun {
  id: string
  worker_name: string
  status: string
  started_at: string | null
  ended_at: string | null
  error_type: string | null
  error_message: string | null
}
export interface VerificationDetail extends VerificationSummary {
  project_id: string
  accepted_at: string
  terminal_at: string | null
  origin: string
  report: { type: 'markdown' | 'text' | 'file' | 'platform_vulnerability'; resource_id: string; manifest_resource_id: string | null }
  previous_request_id: string | null
  group_id: string | null
  worker_template_id: string | null
  tenant_workers: { id: string; display_name: string; revision: number | null }[]
  execution_binding: { worker_id: string; display_name: string; worker_revision: number; model_id: string; model_name: string; model_revision: number } | null
  authorization_expires_at: string | null
  execution_deadline: string | null
  materials: VerificationMaterial[]
  artifacts: VerificationArtifact[]
  runs: VerificationRun[]
  history: VerificationSummary[]
  cleanup: { status: string; error: VerificationError | null }
  execution_message: string | null
}
export interface VerificationFilters {
  external_id?: string
  mode?: VerificationMode
  execution_status?: VerificationStatus
  outcome?: VerificationOutcome
  limit: number
  offset: number
}
export interface VerificationBatchMember extends ProjectResourceBatchMember {
  source_resource_id?: string | null
}
export interface VerificationBatch extends Omit<ProjectResourceBatch, 'members'> {
  members: VerificationBatchMember[]
}
const requestsPath = '/api/v1/verification-requests'
const batchesPath = '/api/v1/verification-resource-batches'
const requestPath = (id: string) => `${requestsPath}/${encodeURIComponent(id)}`
const batchPath = (id: string) => `${batchesPath}/${encodeURIComponent(id)}`
export const listVerificationRequests = (filters: VerificationFilters, signal?: AbortSignal) => {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== '') params.set(key, String(value))
  }
  return api<{ items: VerificationSummary[]; total: number }>('GET', `${requestsPath}?${params}`, undefined, signal)
}
export const getVerificationRequest = (id: string, signal?: AbortSignal) =>
  api<VerificationDetail>('GET', requestPath(id), undefined, signal)
export const createVerificationRequest = (body: CreateVerificationRequest) =>
  api<VerificationDetail>('POST', requestsPath, body)
export const cancelVerificationRequest = (id: string) =>
  api<VerificationDetail>('POST', `${requestPath(id)}/cancel`, {})
const verificationSourcePath = (projectId: string, vulnerabilityId: string) =>
  `/api/v1/verification-sources/projects/${encodeURIComponent(projectId)}/vulnerabilities/${encodeURIComponent(vulnerabilityId)}`
export const getVerificationSourcePreview = (projectId: string, vulnerabilityId: string, signal?: AbortSignal) =>
  api<VerificationSourcePreview>('GET', `${verificationSourcePath(projectId, vulnerabilityId)}/preview`, undefined, signal)
export const getVerificationSourceHistory = (projectId: string, vulnerabilityId: string, limit: number, offset: number, signal?: AbortSignal) =>
  api<{ items: VerificationSummary[]; total: number }>('GET', `${verificationSourcePath(projectId, vulnerabilityId)}/history?${new URLSearchParams({ limit: String(limit), offset: String(offset) })}`, undefined, signal)
export const verificationReportUrl = (id: string) => `${requestPath(id)}/report`
export const verificationMaterialUrl = (id: string, resourceId: string) =>
  `${requestPath(id)}/materials/${encodeURIComponent(resourceId)}`
export const verificationArtifactUrl = (id: string, artifactId: string) =>
  `${requestPath(id)}/artifacts/${encodeURIComponent(artifactId)}`
export const createVerificationBatch = (sourceRequestId?: string) =>
  api<VerificationBatch>('POST', batchesPath, sourceRequestId ? { source_request_id: sourceRequestId } : {})
export const getVerificationBatch = (id: string) => api<VerificationBatch>('GET', batchPath(id))
export const uploadVerificationFile = (id: string, file: File, path: string, kind: ProjectResourceKind) =>
  apiRaw<VerificationBatchMember>('POST', `${batchPath(id)}/files?${new URLSearchParams({ path, kind })}`, file)
export const deleteVerificationBatchMember = (id: string, memberId: string) =>
  api<void>('DELETE', `${batchPath(id)}/members/${encodeURIComponent(memberId)}`)
export const cancelVerificationBatch = (id: string) => api<void>('DELETE', batchPath(id))

export interface VerificationReadiness {
  configured: boolean
  ready: boolean
  worker_name: string | null
  model_name: string | null
  model_display_name: string | null
  enabled: boolean | null
  reason: string | null
  message: string
}
export interface VerificationConfigurationInput {
  model_id: string
  display_name: string
  engine: 'pi'
  max_running: number
  priority: number
  enabled: boolean
}
export interface VerificationConfiguration {
  tenant_id: string
  tenant_worker_id: string | null
  worker_name: string | null
  display_name: string | null
  engine: 'pi' | null
  model_id: string | null
  model_name: string | null
  model_display_name: string | null
  max_running: number | null
  priority: number | null
  enabled: boolean | null
  revision: number | null
  ready: boolean
  reason: string | null
  message: string
  updated_at: string | null
}
export interface VerificationConfigurationOptions {
  models: { id: string; display_name: string; model_name: string; protocol: string; enabled: boolean; credential_configured: boolean }[]
  engines: { id: 'pi'; name: string }[]
  defaults: Omit<VerificationConfigurationInput, 'model_id'>
}
export interface VerificationTenant { id: string; name: string; kind: string }
const tenantQuery = (tenantId: string) => `?${new URLSearchParams({ tenant_id: tenantId })}`
export const getVerificationReadiness = () => api<VerificationReadiness>('GET', '/api/v1/verification-readiness')
export const listVerificationTenants = () => api<VerificationTenant[]>('GET', '/admin/tenants')
export const getVerificationConfiguration = (tenantId: string) =>
  api<VerificationConfiguration>('GET', `/api/v1/verification-configuration${tenantQuery(tenantId)}`)
export const getVerificationConfigurationOptions = (tenantId: string) =>
  api<VerificationConfigurationOptions>('GET', `/api/v1/verification-configuration/options${tenantQuery(tenantId)}`)
export const saveVerificationConfiguration = (tenantId: string, body: VerificationConfigurationInput) =>
  api<VerificationConfiguration>('PUT', `/api/v1/verification-configuration${tenantQuery(tenantId)}`, body)

export interface VerificationIntegration {
  id: string
  name: string
  enabled: boolean
  created_at: string
}
export interface VerificationIntegrationInput { name: string }
export interface VerificationIntegrationSecret {
  integration: VerificationIntegration
  token: string
}
const integrationsPath = '/api/v1/verification-integrations'
const integrationPath = (id: string) => `${integrationsPath}/${encodeURIComponent(id)}`
export const listVerificationIntegrations = (tenantId: string) =>
  api<VerificationIntegration[]>('GET', `${integrationsPath}${tenantQuery(tenantId)}`)
export const createVerificationIntegration = (tenantId: string, body: VerificationIntegrationInput) =>
  api<VerificationIntegrationSecret>('POST', `${integrationsPath}${tenantQuery(tenantId)}`, body)
export const updateVerificationIntegration = (tenantId: string, id: string, body: { name: string; enabled: boolean }) =>
  api<VerificationIntegration>('PATCH', `${integrationPath(id)}${tenantQuery(tenantId)}`, body)
export const disableVerificationIntegration = (tenantId: string, id: string) =>
  api<VerificationIntegration>('POST', `${integrationPath(id)}/disable${tenantQuery(tenantId)}`, {})
export const rotateVerificationIntegrationToken = (tenantId: string, id: string) =>
  api<VerificationIntegrationSecret>('POST', `${integrationPath(id)}/rotate-token${tenantQuery(tenantId)}`, {})
