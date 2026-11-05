import { api } from './client'

export type WorkspaceWindow = 'head' | 'tail'
export interface WorkspaceEntry {
  name: string
  path: string
  kind: 'directory' | 'file' | 'blocked'
  size: number | null
  modified_at: string
  blocked_reason?: string | null
}
export interface WorkspaceEntries {
  project_id: string
  workspace_id: string
  root_path: '/home/kali/workspace'
  path: string
  entries: WorkspaceEntry[]
  next_cursor: string | null
  has_more: boolean
  live: true
}
export interface WorkspaceSearch extends Omit<WorkspaceEntries, 'path'> {
  query: string
}
export interface WorkspacePreview {
  project_id: string
  workspace_id: string
  path: string
  size: number
  modified_at: string
  media_type: string
  preview_kind: 'text' | 'image' | 'binary'
  content: string | null
  bytes_read: number
  truncated: boolean
  window: WorkspaceWindow
  live: true
  encoding_lossy?: boolean
}
const base = (projectId: string) => `/projects/${encodeURIComponent(projectId)}/workspace`
export function listWorkspaceEntries(
  projectId: string,
  path = '',
  workspaceId?: string,
  cursor?: string,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({ path })
  if (workspaceId) query.set('workspace_id', workspaceId)
  if (cursor) query.set('cursor', cursor)
  return api<WorkspaceEntries>('GET', `${base(projectId)}/entries?${query}`, undefined, signal)
}
export function searchWorkspaceFiles(
  projectId: string,
  workspaceId: string,
  query: string,
  cursor?: string,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({ workspace_id: workspaceId, q: query })
  if (cursor) params.set('cursor', cursor)
  return api<WorkspaceSearch>('GET', `${base(projectId)}/search?${params}`, undefined, signal)
}
export function fetchWorkspacePreview(
  projectId: string,
  workspaceId: string,
  path: string,
  window: WorkspaceWindow,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({ workspace_id: workspaceId, path, window })
  return api<WorkspacePreview>('GET', `${base(projectId)}/preview?${query}`, undefined, signal)
}
// 下载交给浏览器流式接收，不将完整文件收集为内存 Blob。
export function workspaceContentUrl(
  projectId: string,
  workspaceId: string,
  path: string,
  inline = false,
) {
  const query = new URLSearchParams({ workspace_id: workspaceId, path })
  if (inline) query.set('inline', 'true')
  return `${base(projectId)}/content?${query}`
}
