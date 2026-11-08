import type { Fact, Intent, ReportInventoryItem } from '@/api/types'
import type { TaskRun, TaskRunHookEvent } from './runTypes'

export const isActiveRun = (run: TaskRun) => ['queued', 'running'].includes(run.status)

export interface WorkResult {
  key: string
  runId: string
  label: string
  text: string
  artifactId?: string
}

export const resultsForRun = (
  run: TaskRun,
  facts: readonly Fact[],
  intents: readonly Intent[],
  reports: readonly ReportInventoryItem[] = [],
): WorkResult[] => {
  const intent = intents.find((item) => item.id === run.intent_id)
  const results: WorkResult[] = facts
    .filter((fact) => fact.task_run_id === run.id && (!run.intent_id || intent?.to === fact.id))
    .map((fact) => ({
      key: `fact:${fact.id}`,
      runId: run.id,
      label: '已接受事实',
      text: fact.description,
    }))
  // Intent.task_run_id 是创建来源，不是执行该工作的 Run；这里只表达已创建的工作。
  for (const created of intents.filter((item) => item.task_run_id === run.id)) {
    results.push({
      key: `intent:${created.id}`,
      runId: run.id,
      label: created.to === 'goal' ? '已接受完成决策' : '已创建工作',
      text: created.description,
    })
  }
  for (const report of reports.filter((item) => item.task_run_id === run.id)) {
    results.push({
      key: `report:${report.artifact_id}`,
      runId: run.id,
      label: '已接受报告',
      text: '查看报告原件',
      artifactId: report.artifact_id,
    })
  }
  return results
}

const validTime = (value: unknown): string | null =>
  typeof value === 'string' && Number.isFinite(Date.parse(value)) ? value : null

export const firstRunTime = (run: TaskRun): string | null => {
  const times = [validTime(run.runtime_context.queued_at), validTime(run.started_at)].filter(
    (time): time is string => time !== null,
  )
  return times.sort((a, b) => Date.parse(a) - Date.parse(b))[0] || null
}

export interface WorkNode {
  key: string
  intent: Intent | null
  runs: TaskRun[]
  matchedRuns: TaskRun[]
  startedAt: string | null
  results: WorkResult[]
  status: string
}

export const buildWorkTimeline = (
  runs: readonly TaskRun[],
  intents: readonly Intent[],
  facts: readonly Fact[],
  matchingIds: ReadonlySet<string>,
  reports: readonly ReportInventoryItem[] = [],
): WorkNode[] => {
  const groups = new Map<string, WorkNode>()
  const intentById = new Map(intents.map((intent) => [intent.id, intent]))
  for (const run of runs) {
    const key = run.intent_id ? `intent:${run.intent_id}` : `run:${run.id}`
    let work = groups.get(key)
    if (!work) {
      work = {
        key,
        intent: intentById.get(run.intent_id || '') || null,
        runs: [],
        matchedRuns: [],
        startedAt: null,
        results: [],
        status: '',
      }
      groups.set(key, work)
    }
    work.runs.push(run)
    if (matchingIds.has(run.id)) work.matchedRuns.push(run)
    const time = firstRunTime(run)
    if (time && (!work.startedAt || Date.parse(time) < Date.parse(work.startedAt)))
      work.startedAt = time
    work.results.push(...resultsForRun(run, facts, intents, reports))
  }
  for (const work of groups.values()) {
    work.runs.sort(
      (a, b) => (Date.parse(firstRunTime(b) || '') || 0) - (Date.parse(firstRunTime(a) || '') || 0),
    )
    work.matchedRuns = work.runs.filter((run) => matchingIds.has(run.id))
    // 卡片与 selectWork 使用同一个默认 Run，筛选失败时不能被另一次成功覆盖。
    work.status = (work.matchedRuns[0] || work.runs[0]!).status
  }
  return [...groups.values()]
    .filter((work) => work.matchedRuns.length)
    .sort(
      (a, b) =>
        (Date.parse(b.startedAt || '') || 0) - (Date.parse(a.startedAt || '') || 0) ||
        a.key.localeCompare(b.key),
    )
}

export interface ToolCall {
  key: string
  runId: string
  name: string
  startedAt: string | null
  endedAt: string | null
  status: string
  active: boolean
  durationMs: number | null
  phase: string
  sessionId: string | null
  operationId: string | null
  artifactId: string | null
  command: string | null
  filePath: string | null
  query: string | null
  url: string | null
  summary: string | null
  exitCode: number | null
  toolInput: Record<string, unknown> | null
  toolResponse: Record<string, unknown> | null
}

export const pairToolCalls = (run: TaskRun, events: readonly TaskRunHookEvent[]): ToolCall[] => {
  const pairs = new Map<string, { start?: TaskRunHookEvent; end?: TaskRunHookEvent }>()
  const sources = new Map<string, string>()
  for (const event of events) {
    if (event.task_run_id !== run.id || event.project_id !== run.project_id) continue
    const sourceKey = JSON.stringify([event.phase, event.session_id])
    if (event.artifact_id) sources.set(sourceKey, event.artifact_id)
    const start = ['tool_execution_start', 'PreToolUse'].includes(event.hook_event_name)
    const end = ['tool_execution_end', 'PostToolUse', 'PostToolUseFailure'].includes(
      event.hook_event_name,
    )
    if (!start && !end) continue
    // 缺调用身份时保留独立观测，不按工具名或相邻顺序猜配对。
    const key = JSON.stringify([
      run.id,
      event.phase,
      event.session_id,
      event.operation_id || `event:${event.id}`,
    ])
    const pair = pairs.get(key) || {}
    if (start && !pair.start) pair.start = event
    if (end) pair.end = event
    pairs.set(key, pair)
  }
  return [...pairs.entries()]
    .map(([key, pair]) => {
      const event = pair.end || pair.start!
      const startedAt = validTime(pair.start?.recorded_at)
      const endedAt = validTime(pair.end?.recorded_at)
      const active = Boolean(pair.start && !pair.end && isActiveRun(run))
      const duration = startedAt && endedAt ? Date.parse(endedAt) - Date.parse(startedAt) : null
      const recordedDuration = pair.end?.duration_ms ?? pair.start?.duration_ms ?? null
      // 输入优先取开始事件，响应只取结束事件；空对象不代表来源记录了正文。
      const startInput = pair.start?.tool_input
      const endInput = pair.end?.tool_input
      const endResponse = pair.end?.tool_response
      const toolInput =
        startInput && Object.keys(startInput).length
          ? startInput
          : endInput && Object.keys(endInput).length
            ? endInput
            : null
      const toolResponse = endResponse && Object.keys(endResponse).length ? endResponse : null
      return {
        key,
        runId: run.id,
        name: event.tool_name,
        startedAt,
        endedAt,
        status: pair.end
          ? event.status === 'succeeded'
            ? '已完成'
            : event.status === 'failed'
              ? '调用失败'
              : '结束结果未确认'
          : active
            ? '执行中'
            : '结束状态未记录',
        active,
        durationMs:
          recordedDuration !== null && recordedDuration >= 0
            ? recordedDuration
            : duration !== null && duration >= 0
              ? duration
              : null,
        phase: event.phase,
        sessionId: event.session_id,
        operationId: event.operation_id,
        artifactId:
          event.artifact_id || sources.get(JSON.stringify([event.phase, event.session_id])) || null,
        command: pair.start?.command || pair.end?.command || null,
        filePath: pair.start?.file_path || pair.end?.file_path || null,
        query: pair.start?.query || pair.end?.query || null,
        url: pair.start?.url || pair.end?.url || null,
        summary: pair.end?.summary || pair.start?.summary || null,
        exitCode: pair.end?.exit_code ?? pair.start?.exit_code ?? null,
        toolInput,
        toolResponse,
      }
    })
    .sort(
      (a, b) =>
        (Date.parse(a.startedAt || a.endedAt || '') || 0) -
        (Date.parse(b.startedAt || b.endedAt || '') || 0),
    )
}
