import { duration } from '@/lib/format'
import type { TaskRun } from './runTypes'

export interface RunArtifactLike {
  id: string
  kind?: string | null
  uri?: string | null
  size?: number | null
  created_at?: string | null
  metadata?: Record<string, unknown> | null
}

export type TerminalOutputStream = 'stdout' | 'stderr' | 'diagnostic'

export interface TerminalOutput {
  artifact: RunArtifactLike
  id: string
  phase: string
  stream: TerminalOutputStream
  label: string
  successful: boolean
  timedOut: boolean
  captureTruncated: boolean
  originalSize: number | null
  storedSize: number | null
}

const terminalStatuses = new Set(['succeeded', 'failed', 'cancelled', 'timed_out'])

const phaseLabels: Record<string, string> = {
  bootstrap: 'Bootstrap 执行',
  bootstrap_conclude: 'Bootstrap 收尾',
  reason_execute: 'Reason 执行',
  explore_execute: 'Explore 执行',
  explore_conclude: 'Explore 收尾',
  verifier_execute: 'Verifier 执行',
  verifier_conclude: 'Verifier 收尾',
  report_execute: 'Report 执行',
}

const streamLabels: Record<TerminalOutputStream, string> = {
  stdout: '标准输出',
  stderr: '标准错误',
  diagnostic: '诊断',
}

const metadataValue = (artifact: RunArtifactLike, key: string): unknown => artifact.metadata?.[key]

const numericMetadata = (artifact: RunArtifactLike, key: string): number | null => {
  const value = metadataValue(artifact, key)
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

const streamForArtifact = (artifact: RunArtifactLike): TerminalOutputStream | null => {
  const stream = metadataValue(artifact, 'stream')
  if (stream === 'stdout' || stream === 'stderr' || stream === 'diagnostic') return stream
  if (artifact.kind !== 'raw_output') return null

  const uri = artifact.uri?.toLowerCase() || ''
  if (uri.includes('stdout')) return 'stdout'
  if (uri.includes('stderr')) return 'stderr'
  if (uri.includes('diagnostic')) return 'diagnostic'
  return null
}

const phaseForArtifact = (artifact: RunArtifactLike): string => {
  const phase = metadataValue(artifact, 'phase')
  return typeof phase === 'string' && phase ? phase : 'output'
}

const isSuccessfulArtifact = (artifact: RunArtifactLike): boolean =>
  metadataValue(artifact, 'returncode') === 0 &&
  metadataValue(artifact, 'timed_out') !== true &&
  metadataValue(artifact, 'cancelled') !== true

const validTimestamp = (value: string | null | undefined): number | null => {
  if (!value) return null
  const timestamp = new Date(value).getTime()
  return Number.isNaN(timestamp) ? null : timestamp
}

export const buildTerminalOutputs = (artifacts: RunArtifactLike[]): TerminalOutput[] =>
  artifacts.flatMap((artifact) => {
    const stream = streamForArtifact(artifact)
    if (!stream) return []

    const phase = phaseForArtifact(artifact)
    const phaseLabel = phaseLabels[phase] || phase.replaceAll('_', ' ')
    return [
      {
        artifact,
        id: artifact.id,
        phase,
        stream,
        label: `${phaseLabel} · ${streamLabels[stream]}`,
        successful: isSuccessfulArtifact(artifact),
        timedOut: metadataValue(artifact, 'timed_out') === true,
        captureTruncated: metadataValue(artifact, 'truncated') === true,
        originalSize: numericMetadata(artifact, 'original_size'),
        storedSize:
          typeof artifact.size === 'number' && Number.isFinite(artifact.size) && artifact.size >= 0
            ? artifact.size
            : null,
      },
    ]
  })

export const selectDefaultTerminalOutput = (outputs: TerminalOutput[]): TerminalOutput | null => {
  const stdout = outputs.filter((output) => output.stream === 'stdout')
  const successfulStdout = stdout.filter((output) => output.successful)
  const diagnostics = outputs.filter((output) => output.stream === 'diagnostic')
  const stderr = outputs.filter((output) => output.stream === 'stderr')
  return successfulStdout.at(-1) || stdout.at(-1) || diagnostics.at(-1) || stderr.at(-1) || null
}

export const selectRetainedTerminalOutput = (
  outputs: TerminalOutput[],
  currentArtifactId: string | null,
): TerminalOutput | null => {
  return (
    outputs.find((output) => output.id === currentArtifactId) ||
    selectDefaultTerminalOutput(outputs)
  )
}

export const isConcludeRecovery = (outputs: TerminalOutput[]): boolean => {
  const stdout = outputs.filter((output) => output.stream === 'stdout')
  const hasTimedOutPrimary = stdout.some(
    (output) => !output.phase.endsWith('_conclude') && output.timedOut,
  )
  const hasSuccessfulConclude = stdout.some(
    (output) => output.phase.endsWith('_conclude') && output.successful,
  )
  return hasTimedOutPrimary && hasSuccessfulConclude
}

export const runQueuedAt = (run: TaskRun): string | null => {
  const queuedAt = run.runtime_context?.queued_at
  return typeof queuedAt === 'string' && queuedAt ? queuedAt : null
}

export const runReferenceTime = (run: TaskRun): string | null => {
  return run.started_at || runQueuedAt(run) || run.ended_at || null
}

export const runDurationSeconds = (run: TaskRun): number | null => {
  const startedAt = validTimestamp(run.started_at)
  const endedAt = validTimestamp(run.ended_at)
  if (startedAt === null || endedAt === null) return null
  return Math.max(0, (endedAt - startedAt) / 1000)
}

export const runDurationText = (run: TaskRun): string => {
  if (run.status === 'queued') return '排队中'
  if (run.status === 'running') return '执行中'

  const seconds = runDurationSeconds(run)
  if (seconds !== null) return duration(seconds)
  if (terminalStatuses.has(run.status)) return '已结束'
  return run.started_at ? '执行中' : '排队中'
}
