import type {
  CreateProjectResourceRequest,
  ProjectResourceConfig,
  ProjectResourceKind,
} from '@/api/types'

const SCRIPT_EXTENSIONS = new Set(['bash', 'js', 'mjs', 'ps1', 'py', 'rb', 'sh', 'ts', 'zsh'])
const SCRIPT_NAMES = new Set(['makefile'])
const NOTE_EXTENSIONS = new Set([
  'csv',
  'htm',
  'html',
  'json',
  'log',
  'md',
  'rst',
  'text',
  'toml',
  'tsv',
  'txt',
  'xml',
  'yaml',
  'yml',
])

export interface ProjectResourceDraft extends CreateProjectResourceRequest {
  id: number
  sizeBytes: number
}

export const inferProjectResourceKind = (path: string): ProjectResourceKind => {
  const name = path.split('/').pop()?.toLowerCase() || ''
  if (SCRIPT_NAMES.has(name)) return 'script'
  const extension = name.includes('.') ? name.split('.').pop() || '' : ''
  if (SCRIPT_EXTENSIONS.has(extension)) return 'script'
  if (NOTE_EXTENSIONS.has(extension)) return 'note'
  return 'file'
}

export const projectResourceKindLabel = (kind: ProjectResourceKind) => {
  if (kind === 'script') return '脚本'
  if (kind === 'note') return '笔记'
  return '文件'
}

export const projectResourceKindOptions = (config?: ProjectResourceConfig | null) => {
  const kinds = config?.kinds || (['note', 'script', 'file'] satisfies ProjectResourceKind[])
  return kinds.map((kind) => ({ value: kind, label: projectResourceKindLabel(kind) }))
}

export const formatProjectResourceSize = (sizeBytes: number) => {
  if (sizeBytes < 1024) return `${sizeBytes} B`
  if (sizeBytes < 1024 * 1024) return `${(sizeBytes / 1024).toFixed(1)} KiB`
  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MiB`
}

export const projectResourceFilePath = (file: File) => file.webkitRelativePath || file.name

// 知识库文本上传仍需要严格 UTF-8 解码；Project Resource 的新主流程不会调用此函数。
export const readUtf8File = async (file: File): Promise<string> => {
  const bytes = await file.arrayBuffer()
  try {
    return new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes)
  } catch {
    throw new Error(`${file.name} 不是有效的 UTF-8 文本文件`)
  }
}

export const projectResourcePathError = (
  value: string,
  config?: ProjectResourceConfig | null,
): string => {
  const path = value.trim()
  if (!path) return '资源路径不能为空'
  if (path.startsWith('/') || path.includes('\\')) return '资源路径必须是 POSIX 相对路径'
  const segments = path.split('/')
  if (segments.some((segment) => !segment || segment === '.' || segment === '..')) {
    return '资源路径不能包含空段、. 或 ..'
  }
  if (segments.some((segment) => /[\x00-\x1f\x7f]/.test(segment))) {
    return '资源路径不能包含控制字符'
  }
  const segmentLimit = config?.limits.max_path_segment_bytes ?? 255
  if (segments.some((segment) => new TextEncoder().encode(segment).byteLength > segmentLimit)) {
    return `资源路径单段不能超过 ${segmentLimit} 字节`
  }
  const pathLimit = config?.limits.max_path_bytes ?? 1024
  if (new TextEncoder().encode(path).byteLength > pathLimit) {
    return `资源路径不能超过 ${pathLimit} 字节`
  }
  const reservedNames = new Set(
    (config?.reserved_names || ['manifest.json']).map((name) => name.toLowerCase()),
  )
  const reserved = segments.find((segment) => reservedNames.has(segment.toLowerCase()))
  if (reserved) return `${reserved} 是保留名称，不能出现在资源路径中`
  return ''
}

export const projectResourceFileError = (
  file: File,
  path: string,
  config: ProjectResourceConfig,
): string => {
  const pathError = projectResourcePathError(path, config)
  if (pathError) return `${path || file.name}：${pathError}`
  if (file.size > config.limits.max_file_bytes) {
    return `${path} 超过单文件 ${formatProjectResourceSize(config.limits.max_file_bytes)} 限制`
  }
  return ''
}

// 旧 JSON 文本 API 仍可由兼容调用方使用；新上传流程不再读取或解码 File 内容。
export const projectResourcePayload = (
  drafts: ProjectResourceDraft[],
): CreateProjectResourceRequest[] =>
  drafts.map((draft) => ({
    kind: draft.kind,
    path: draft.path.trim(),
    content: draft.content,
  }))

export const canMutateProjectResources = (
  projectStatus: string,
  monitoringReady: boolean,
  activeTaskStatuses: string[],
) =>
  ['paused', 'stopped'].includes(projectStatus) &&
  monitoringReady &&
  !activeTaskStatuses.some((status) => status === 'queued' || status === 'running')
