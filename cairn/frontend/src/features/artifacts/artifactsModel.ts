import type { ArtifactCategory, ArtifactKind, ProjectArtifact } from '@/api/client'
import { fileNameFromUri } from '@/lib/fileLanguage'

export type ArtifactViewMode = 'deliverable' | 'diagnostic'
export type ArtifactKindFilter = 'all' | ArtifactKind
export type ArtifactPreviewWindow = 'tail' | 'head'

export interface ArtifactKindOption {
  value: ArtifactKindFilter
  label: string
}

const ARTIFACT_KIND_LABELS = {
  screenshot: '图片截图',
  report: '报告 Report',
  patch: '补丁 Patch',
  file: '数据文件',
  raw_output: '标准输出 Log',
  log: '诊断日志',
  hook_log: 'Hook 日志',
  har: 'HTTP Archive',
  pcap: '网络抓包 PCAP',
} as const satisfies Record<ArtifactKind, string>

const ARTIFACT_KIND_ORDER = [
  'screenshot',
  'report',
  'patch',
  'file',
  'raw_output',
  'log',
  'hook_log',
  'har',
  'pcap',
] as const satisfies readonly ArtifactKind[]

const DEFAULT_KINDS = {
  deliverable: ['screenshot', 'report', 'patch', 'file'],
  diagnostic: ['raw_output', 'log', 'hook_log'],
} as const satisfies Record<ArtifactViewMode, readonly ArtifactKind[]>

const TEXT_PREVIEWABLE_KINDS = new Set<ArtifactKind>([
  'raw_output',
  'log',
  'report',
  'patch',
  'file',
  'har',
  'hook_log',
])

export const artifactViewCategory = (viewMode: ArtifactViewMode): ArtifactCategory =>
  viewMode === 'deliverable' ? 'deliverable' : 'evidence'

export const viewModeForArtifact = (
  artifact: Pick<ProjectArtifact, 'category'>,
): ArtifactViewMode => (artifact.category === 'deliverable' ? 'deliverable' : 'diagnostic')

export const artifactFilename = (artifact: Pick<ProjectArtifact, 'uri'>): string =>
  fileNameFromUri(artifact.uri) || artifact.uri || '未命名产物'

export const artifactKindLabel = (kind: ArtifactKind): string => ARTIFACT_KIND_LABELS[kind]

export const artifactIsTextPreviewable = (artifact: Pick<ProjectArtifact, 'kind'>): boolean =>
  TEXT_PREVIEWABLE_KINDS.has(artifact.kind)

export const artifactIsScreenshot = (artifact: Pick<ProjectArtifact, 'kind'>): boolean =>
  artifact.kind === 'screenshot'

export const filterAndSortArtifacts = (
  artifacts: readonly ProjectArtifact[],
  viewMode: ArtifactViewMode,
  kindFilter: ArtifactKindFilter,
  filenameSearch = '',
): ProjectArtifact[] => {
  const category = artifactViewCategory(viewMode)
  const normalizedSearch = filenameSearch.trim().toLowerCase()
  return artifacts
    .map((artifact, index) => ({ artifact, index }))
    .filter(
      ({ artifact }) =>
        artifact.category === category &&
        (kindFilter === 'all' || artifact.kind === kindFilter) &&
        artifactFilename(artifact).toLowerCase().includes(normalizedSearch),
    )
    .sort(
      (left, right) =>
        right.artifact.created_at.localeCompare(left.artifact.created_at) ||
        left.index - right.index,
    )
    .map(({ artifact }) => artifact)
}

export const kindOptionsForArtifacts = (
  artifacts: readonly ProjectArtifact[],
  viewMode: ArtifactViewMode,
): ArtifactKindOption[] => {
  const category = artifactViewCategory(viewMode)
  const visibleKinds = new Set(
    artifacts.filter((artifact) => artifact.category === category).map((artifact) => artifact.kind),
  )
  const allowedKinds = new Set<ArtifactKind>([...DEFAULT_KINDS[viewMode], ...visibleKinds])

  return [
    { value: 'all', label: '全部类别' },
    ...ARTIFACT_KIND_ORDER.filter((kind) => allowedKinds.has(kind)).map((kind) => ({
      value: kind,
      label: ARTIFACT_KIND_LABELS[kind],
    })),
  ]
}
