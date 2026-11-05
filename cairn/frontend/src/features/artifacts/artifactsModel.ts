import { ApiError, type ArtifactKind, type ProjectArtifact } from '@/api/client'
import { fileNameFromUri } from '@/lib/fileLanguage'

export const artifactFilename = (artifact: Pick<ProjectArtifact, 'uri'>): string =>
  fileNameFromUri(artifact.uri) || '未命名原件'
const textKinds = new Set<ArtifactKind>([
  'raw_output',
  'log',
  'report',
  'patch',
  'file',
  'har',
  'hook_log',
])
export const artifactIsTextPreviewable = (artifact: Pick<ProjectArtifact, 'kind'>): boolean =>
  textKinds.has(artifact.kind)

const errors: Record<string, string> = {
  workspace_not_created: '研究目录尚未创建。文件读取不会启动项目或创建容器。',
  workspace_deleted: '所属研究容器已删除，实时目录不可用；已保存原件仍可通过原入口阅读。',
  workspace_owner_unknown: '无法核验研究目录归属，已停止读取。',
  workspace_unreachable: '研究目录暂时不可达，请稍后重试。',
  workspace_topology_unsupported: '当前部署拓扑不支持只读目录访问。',
  workspace_layer_unsupported: '此历史现场没有可读取的工作目录挂载，不支持读取容器旧层。',
  workspace_path_invalid: '文件相对路径无效。',
  workspace_path_private: '该路径属于私有运行资料，不允许读取。',
  workspace_path_blocked: '链接或特殊文件不允许读取。',
  workspace_path_not_found: '文件或目录已不存在，请刷新目录。',
  workspace_permission_denied: '没有读取该目录或文件的权限。',
  workspace_identity_changed: '研究目录身份已变化，旧选择已失效；请刷新重新读取。',
  workspace_identity_required: '缺少研究目录身份，请刷新重新读取。',
  workspace_cursor_invalid: '续查已失效，请刷新后重试。',
  workspace_search_changed: '搜索目录已变化，请刷新后重试。',
  workspace_query_required: '请输入文件名或路径。',
  workspace_filename_unsupported: '目录包含当前不支持的文件名，无法安全列出。',
  workspace_inline_unsupported: '该文件不支持安全图片预览，请下载后查看。',
}
export function workspaceErrorCode(error: unknown): string {
  if (!(error instanceof ApiError) || !error.data || typeof error.data !== 'object') return ''
  const detail = (error.data as { detail?: unknown }).detail
  return typeof detail === 'string' ? detail : ''
}
export function workspaceErrorMessage(error: unknown): string {
  const known = errors[workspaceErrorCode(error)]
  if (known) return known
  if (error instanceof ApiError && error.status === 403) return '没有读取该项目文件的权限。'
  if (error instanceof ApiError && error.status === 404) return '项目或文件不存在，或已删除。'
  // 不把服务端异常、宿主路径或控制面信息拼入页面。
  return '文件读取失败，请稍后重试。'
}
export const workspaceNodeKey = (
  projectId: string,
  workspaceId: string,
  kind: string,
  path: string,
) => JSON.stringify([projectId, workspaceId, kind, path])
