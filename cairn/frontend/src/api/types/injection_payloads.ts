export const injectionPayloadResponseStatuses = [
  'pending',
  'returned',
  'empty',
  'blocked',
  'timeout',
  'error',
  'not_sent',
  'interrupted',
] as const

export type InjectionPayloadResponseStatus = (typeof injectionPayloadResponseStatuses)[number]

export interface InjectionPayloadListItem {
  id: string
  project_id: string
  task_run_id: string
  phase: string
  capture_id: string
  injection_type: string
  payload_preview: string
  original_format: string
  response_status: InjectionPayloadResponseStatus
  response_preview: string | null
  response_format: string | null
  created_at: string
  updated_at: string
}

export interface InjectionPayloadRecord {
  id: string
  project_id: string
  task_run_id: string
  phase: string
  capture_id: string
  injection_type: string
  payload_text: string
  original_format: string
  response_status: InjectionPayloadResponseStatus
  response_text: string | null
  response_format: string | null
  created_at: string
  updated_at: string
}

export interface InjectionPayloadListResponse {
  items: InjectionPayloadListItem[]
  total: number
  limit: number
  offset: number
}

export interface InjectionPayloadCategorySummary {
  injection_type: string
  count: number
}

export interface InjectionPayloadListFilters {
  injectionType?: string
  responseStatus?: InjectionPayloadResponseStatus
  search?: string
  limit?: number
  offset?: number
}
