import { api } from './client'
import type { TrafficFlowDetail, TrafficReadable, TrafficScope, TrafficSide, TrafficState } from './types'

export function getTrafficReadable(projectId: string, flowId: string, side: TrafficSide, signal?: AbortSignal, mode: 'auto' | 'manual' = 'manual') {
  return api<TrafficReadable>('GET', `/projects/${encodeURIComponent(projectId)}/traffic/${encodeURIComponent(flowId)}/readable/${side}?mode=${mode}`, undefined, signal)
}

export interface TrafficAssociation {
  observation_id: string | null
  source: 'observation' | 'record'
  vulnerability_id: string
  status: string
  task_run_id: string | null
}

export interface TrafficSummary {
  id: string
  project_id: string
  project_title: string
  capture_scope_id: string
  task_run_id: string
  revision: number
  received_at: string
  updated_at: string
  started_at: number
  finished_at: number | null
  method: string
  host: string
  connection_host?: string
  address_issue?: string | null
  url: string
  path: string
  display_host: string
  display_target: string
  status_code: number | null
  state: TrafficState
  source_state: TrafficState
  error_code: string | null
  sync: TrafficFlowDetail['sync']
  associations: {
    observation_count: number
    vulnerability_count: number
    items: TrafficAssociation[]
    has_more: boolean
  }
}

export interface TrafficPage {
  items: TrafficSummary[]
  page: number
  limit: number
  total: number
  total_pages: number
}

export interface TrafficFilters {
  project_id?: string
  task_run_id?: string
  flow_id?: string
  vulnerability_id?: string
  observation_id?: string
  include_record_evidence?: boolean
  method?: string
  url?: string
  state?: TrafficState
  status_family?: string
  status_code?: number
  error_code?: string
  associated?: boolean
  page?: number
  limit?: number
}

export function listTraffic(filters: TrafficFilters, fixedProjectId?: string, signal?: AbortSignal) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== '' && !(fixedProjectId && key === 'project_id')) query.set(key, String(value))
  }
  query.set('limit', String(Math.min(200, Math.max(1, filters.limit ?? 50))))
  const path = fixedProjectId ? `/projects/${encodeURIComponent(fixedProjectId)}/traffic/summary` : '/traffic'
  return api<TrafficPage>('GET', `${path}?${query}`, undefined, signal)
}

export interface TrafficScopeStatus extends TrafficScope {
  status: {
    state: string
    code: string | null
    failure_code?: string | null
    pending: number | null
    pending_bytes: number | null
    control_stopped?: boolean | null
    test_stopped?: boolean | null
    collector_stopped?: boolean | null
    upload_complete?: boolean | null
  } | null
  status_updated_at: string | null
}

export function getTrafficScopes(projectId: string, signal?: AbortSignal, taskRunId?: string) {
  const query = new URLSearchParams({ limit: '100' })
  if (taskRunId !== undefined) query.set('task_run_id', taskRunId)
  return api<{ items: TrafficScopeStatus[]; has_more: boolean; next_cursor: string | null }>(
    'GET', `/projects/${encodeURIComponent(projectId)}/traffic/scopes/status?${query}`, undefined, signal,
  )
}
