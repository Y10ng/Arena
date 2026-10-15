import { api } from './client'
import type { CreateProjectRequest, ProjectLibraryBinding, ProjectResourceKind } from './types'

export type ScheduleMode = 'once' | 'daily' | 'weekly'
export type ScheduleAction = 'cancel' | 'start_now' | 'pause' | 'resume' | 'disable'
export interface ScheduleRules {
  mode: ScheduleMode
  timezone: string
  start_at: string
  local_time: string | null
  weekdays: number[]
  end_date: string | null
  max_runs: number | null
}
export interface CreateScheduleRequest extends ScheduleRules {
  request_id: string
  project: CreateProjectRequest
}
export interface UpdateScheduleRequest extends ScheduleRules {
  version: number
  project?: CreateScheduleRequest['project']
  resource_ids?: string[]
  refresh_library_keys?: string[]
  explore_deadline?: string | null
}
export interface ScheduleOccurrence {
  id: string
  schedule_id: string
  occurrence_key: string
  scheduled_at: string
  processed_at: string
  project_id: string | null
  config_version: number
  status: 'started' | 'skipped' | 'missed' | 'blocked'
  reason: string | null
}
export interface ProjectSchedule extends ScheduleRules {
  id: string
  tenant_id: string
  creator_id: string
  status: 'active' | 'paused' | 'cancelled' | 'disabled' | 'completed' | 'blocked'
  version: number
  next_run_at: string | null
  project_id: string | null
  last_project_id: string | null
  generated_count: number
  block_reason: string | null
  explore_deadline?: string | null
  authorization_expires_at?: string | null
  created_at: string
  updated_at: string
  project: CreateScheduleRequest['project'] | null
  resources: Array<{
    id: string
    path: string
    kind: ProjectResourceKind
    size_bytes: number
    digest_sha256: string
    source_name: string
  }>
  library_bindings: ProjectLibraryBinding[]
  last_result: ScheduleOccurrence | null
}
export interface SchedulePreview {
  times: Array<{ at: string; local: string; note: string | null }>
  boundary: string | null
}
export type CreationOperation = 'project' | 'schedule'
export interface CreationRequestReference {
  request_id: string
  operation: CreationOperation
}
export interface CreationRequestResult extends CreationRequestReference {
  project_id: string | null
  schedule_id: string | null
  status: 'committed' | 'unknown'
}
const schedulePath = (id: string) => `/project-schedules/${encodeURIComponent(id)}`
export const listProjectSchedules = () => api<ProjectSchedule[]>('GET', '/project-schedules')
export const getProjectSchedule = (id: string) => api<ProjectSchedule>('GET', schedulePath(id))
export const createProjectSchedule = (body: CreateScheduleRequest) =>
  api<ProjectSchedule>('POST', '/project-schedules', body)
export const updateProjectSchedule = (id: string, body: UpdateScheduleRequest) =>
  api<ProjectSchedule>('PUT', schedulePath(id), body)
export const previewProjectSchedule = (body: ScheduleRules & { project?: CreateProjectRequest }) =>
  api<SchedulePreview>('POST', '/project-schedules/preview', body)
export const actOnProjectSchedule = (id: string, version: number, action: ScheduleAction) =>
  api<ProjectSchedule>('POST', `${schedulePath(id)}/actions`, { version, action })
export const getProjectScheduleHistory = (id: string, offset = 0, limit = 20) =>
  api<{ items: ScheduleOccurrence[]; total: number }>(
    'GET',
    `${schedulePath(id)}/history?offset=${offset}&limit=${limit}`,
  )
export const getCreationRequest = (reference: CreationRequestReference) =>
  api<CreationRequestResult>(
    'GET',
    `/creation-requests/${encodeURIComponent(reference.request_id)}?operation=${reference.operation}`,
  )

export function createCreationRequestId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

// 页面刷新仅恢复请求查询入口；正文、文件和完整 payload 只留在组件内存。
const storageKey = 'arena.creation-request'
interface CreationRequestCacheResult {
  references: CreationRequestReference[]
  warning: string
}
type CacheChange = { remember: CreationRequestReference } | { forget: string }
function accessCreationRequestCache(change?: CacheChange): CreationRequestCacheResult {
  let references: CreationRequestReference[] = []
  let warning = ''
  // 只隔离可选缓存，API 结果和内存中的原请求不进入此异常边界。
  try {
    const storage = sessionStorage
    const raw = storage.getItem(storageKey)
    if (raw !== null) {
      let value: unknown
      try {
        value = JSON.parse(raw)
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error
      }
      if (Array.isArray(value) && value.every((entry) =>
        entry && typeof entry.request_id === 'string' &&
        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(entry.request_id) &&
        (entry.operation === 'project' || entry.operation === 'schedule') &&
        Object.keys(entry).every((key) => key === 'request_id' || key === 'operation'),
      )) {
        references = value.map((entry) => ({ request_id: entry.request_id, operation: entry.operation }))
      } else {
        storage.removeItem(storageKey)
        warning = '请求引用缓存已损坏并清除，无法从该缓存恢复跨刷新查询入口。'
      }
    }
    if (change) {
      references = 'remember' in change
        ? [...references.filter((entry) => entry.request_id !== change.remember.request_id),
            { request_id: change.remember.request_id, operation: change.remember.operation }]
        : references.filter((entry) => entry.request_id !== change.forget)
      if (references.length) storage.setItem(storageKey, JSON.stringify(references))
      else storage.removeItem(storageKey)
    }
  } catch (error) {
    const detail = error instanceof Error ? error.message : '浏览器缓存访问失败'
    warning = !change
      ? `请求引用缓存不可用，无法读取跨刷新查询入口；刷新后不保留正文或原请求载荷，不能自动重发。${detail}`
      : 'forget' in change
        ? `请求引用缓存清理未完成，刷新后可能仍显示旧入口；已确认的请求结果不受影响。${detail}`
        : `请求引用缓存不可用，跨刷新查询入口未保存；本次页面内仍可查询或用原请求恢复。${detail}`
  }
  return { references, warning }
}
export const rememberCreationRequest = (reference: CreationRequestReference) =>
  accessCreationRequestCache({ remember: reference })
export const readCreationRequests = () => accessCreationRequestCache()
export const forgetCreationRequest = (requestId: string) =>
  accessCreationRequestCache({ forget: requestId })

export const scheduleStatusLabels: Record<ProjectSchedule['status'], string> = {
  active: '等待计划时刻',
  paused: '已暂停',
  cancelled: '已取消预约',
  disabled: '已停用',
  completed: '已完成',
  blocked: '需要处理',
}
export const occurrenceStatusLabels: Record<ScheduleOccurrence['status'], string> = {
  started: '已进入调度',
  skipped: '已跳过',
  missed: '已错过',
  blocked: '需要处理',
}
export const weekdayLabels = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
export function scheduleRuleLabel(schedule: ScheduleRules) {
  if (schedule.mode === 'once') return '单次定时'
  return `${schedule.mode === 'daily' ? '每日' : schedule.weekdays.map((day) => weekdayLabels[day]).join('、')} ${schedule.local_time}`
}
export function scheduleLocalTime(at: string | null, timezone: string) {
  return at ? new Date(at).toLocaleString('zh-CN', { timeZone: timezone, hour12: false }) : '无'
}
