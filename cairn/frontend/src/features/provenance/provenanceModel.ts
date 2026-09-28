import type { TaskRunResultEvent } from '@/api/types'
import type {
  RunArtifact,
  TaskRunCommandEvent,
  TaskRunHookEvent,
  TaskRunPromptEvent,
} from '@/features/runs/runTypes'

export type ProvenanceTimelineKind = 'hook' | 'command' | 'prompt' | 'result' | 'artifact'

export interface ProvenanceTimelineItem {
  id: string
  kind: ProvenanceTimelineKind
  ts: string
  title: string
  status: string
  summary: string
  payload: Record<string, unknown>
  incomplete: boolean
}

const timestamp = (value: string): number => {
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : Number.MAX_SAFE_INTEGER
}

const text = (value: unknown): string => {
  if (typeof value === 'string') return value
  if (value === null || value === undefined) return ''
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

const hookSummary = (event: TaskRunHookEvent): string =>
  event.command || event.file_path || event.query || event.url || event.summary || event.status

export const buildProvenanceTimeline = ({
  hooks = [],
  commands = [],
  prompts = [],
  results = [],
  artifacts = [],
}: {
  hooks?: readonly TaskRunHookEvent[]
  commands?: readonly TaskRunCommandEvent[]
  prompts?: readonly TaskRunPromptEvent[]
  results?: readonly TaskRunResultEvent[]
  artifacts?: readonly RunArtifact[]
}): ProvenanceTimelineItem[] => {
  const items: ProvenanceTimelineItem[] = []
  const hooksByOperation = new Map<string, TaskRunHookEvent[]>()

  hooks.forEach((event) => {
    const key = event.operation_id || event.id
    const group = hooksByOperation.get(key) || []
    group.push(event)
    hooksByOperation.set(key, group)
  })

  hooksByOperation.forEach((group, operationId) => {
    const ordered = [...group].sort(
      (left, right) =>
        timestamp(left.recorded_at || left.ts) - timestamp(right.recorded_at || right.ts),
    )
    const first = ordered[0]
    const last = ordered[ordered.length - 1]
    const hasPre = ordered.some((event) => event.hook_event_name === 'PreToolUse')
    const hasPost = ordered.some((event) =>
      ['PostToolUse', 'PostToolUseFailure'].includes(event.hook_event_name),
    )
    items.push({
      id: `hook:${operationId}`,
      kind: 'hook',
      ts: first.recorded_at || first.ts,
      title: first.tool_name || 'Hook operation',
      status: last.status || first.status,
      summary: hookSummary(first),
      payload: {
        operation_id: operationId,
        events: ordered,
        tool_input: first.tool_input,
        tool_response: last.tool_response,
      },
      incomplete: Boolean(first.operation_id) && (!hasPre || !hasPost),
    })
  })

  commands.forEach((event) => {
    items.push({
      id: `command:${event.id}`,
      kind: 'command',
      ts: event.ts,
      title: `${event.phase} · ${event.command_kind}`,
      status: event.kind === 'command_started' ? 'started' : event.status,
      summary:
        event.kind === 'command_finished'
          ? `Exit ${event.exit_code ?? event.returncode ?? '—'}${
              event.duration_seconds === null ? '' : ` · ${event.duration_seconds}s`
            }`
          : '命令已启动',
      payload: { ...event },
      incomplete: false,
    })
  })

  prompts.forEach((event) => {
    items.push({
      id: `prompt:${event.id}`,
      kind: 'prompt',
      ts: event.ts,
      title: `${event.phase} · ${event.prompt_group}/${event.prompt_name}`,
      status: 'rendered',
      summary: `${event.prompt_chars ?? 0} 字符 · ${event.worker_name}`,
      payload: { ...event },
      incomplete: false,
    })
  })

  results.forEach((event) => {
    items.push({
      id: `result:${event.id}`,
      kind: 'result',
      ts: event.ts,
      title: event.kind,
      status: text(event.payload.status ?? event.payload.outcome) || 'recorded',
      summary: text(
        event.payload.rationale ?? event.payload.error_message ?? event.payload.operation,
      ),
      payload: event.payload,
      incomplete: false,
    })
  })

  artifacts.forEach((artifact) => {
    items.push({
      id: `artifact:${artifact.id}`,
      kind: 'artifact',
      ts: artifact.created_at,
      title: artifact.kind,
      status: 'created',
      summary: artifact.uri,
      payload: { ...artifact },
      incomplete: false,
    })
  })

  return items.sort(
    (left, right) => timestamp(left.ts) - timestamp(right.ts) || left.id.localeCompare(right.id),
  )
}
