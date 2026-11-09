import { api } from './client'

export type UpdatePhase =
  | 'idle'
  | 'checking'
  | 'available'
  | 'up_to_date'
  | 'preparing'
  | 'ready'
  | 'applying'
  | 'done'
  | 'failed'
  | 'interrupted'

export interface UpdateChannelDoc {
  format: string
  version: string
  notes_url?: string
  app?: { asset: string; sha256: string; size?: number }
  worker?: { reuse_version?: boolean }
  images?: { traffic_gateway?: { image: string; digest: string } }
}

export interface UpdateStatusResponse {
  version: string
  install_type: 'git' | 'zip' | 'unsupported'
  state: {
    phase: UpdatePhase
    error?: string | null
    prepare_progress?: string | null
    channel_version?: string | null
    channel?: UpdateChannelDoc | null
    apply_log_path?: string | null
    previous_gateway_ref?: string | null
    updated_at?: string | null
  }
  channel_configured: boolean
  gateway_image: string | null
  running_task_count: number
  queued_task_count: number
  running_project_count: number
}

export interface CheckUpdateResponse {
  phase: UpdatePhase
  current_version: string
  channel_version: string | null
  update_available: boolean
}

export const getSystemUpdateStatus = () => api<UpdateStatusResponse>('GET', '/system/update/status')
export const checkSystemUpdate = () => api<CheckUpdateResponse>('POST', '/system/update/check', {})
export const prepareSystemUpdate = () =>
  api<{ phase: UpdatePhase }>('POST', '/system/update/prepare', {})
export const cancelSystemUpdate = () =>
  api<{ phase: UpdatePhase }>('POST', '/system/update/cancel', {})
export const applySystemUpdate = () =>
  api<{ updater_pid: number }>('POST', '/system/update/apply', {})
