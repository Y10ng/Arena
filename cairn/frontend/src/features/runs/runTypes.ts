export type TaskRunStatus =
  'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'timed_out' | string

export interface TaskRunRuntimeContext {
  queued_at?: string | null
  [key: string]: unknown
}

export interface TaskRun {
  id: string
  project_id: string
  intent_id: string | null
  task_type: string
  worker_name: string
  worker_type: string
  status: TaskRunStatus
  session_id: string | null
  prompt_group: string | null
  prompt_version: string | null
  execution_profile: string | null
  started_at: string | null
  ended_at: string | null
  exit_code: number | null
  error_type: string | null
  error_message: string | null
  assessment_attempt_id: string | null
  runtime_context: TaskRunRuntimeContext
}

export interface AgentEvent {
  id: string
  task_run_id: string
  project_id: string
  ts: string
  kind: string
  payload: Record<string, unknown>
}

export interface TaskRunCommandEvent {
  id: string
  task_run_id: string
  project_id: string
  ts: string
  kind: string
  phase: string
  command_kind: string
  status: string
  exit_code: number | null
  returncode: number | null
  timed_out: boolean
  cancelled: boolean
  oom_killed: boolean
  oom_evidence_source: string | null
  duration_seconds: number | null
}

export interface TaskRunPromptEvent {
  id: string
  task_run_id: string
  project_id: string
  ts: string
  kind: string
  phase: string
  prompt_group: string
  prompt_name: string
  prompt_hash: string
  prompt_bytes: number | null
  prompt_chars: number | null
  replacement_keys: string[]
  worker_name: string
  worker_type: string
}

export interface TaskRunHookEvent {
  id: string
  task_run_id: string
  project_id: string
  ts: string
  kind: string
  session_id: string | null
  hook_event_name: string
  tool_name: string
  operation_id: string | null
  status: string
  recorded_at: string | null
  phase: string
  cwd: string | null
  tool_input: Record<string, unknown>
  tool_response: Record<string, unknown>
  command: string | null
  command_kind: string | null
  file_path: string | null
  file_path_digest: string | null
  query: string | null
  url: string | null
  exit_code: number | null
  duration_ms: number | null
  artifact_id: string | null
  risk_level: string
  summary: string | null
}

export interface TaskRunEventGroup<TEvent> {
  task_run_id: string
  events: TEvent[]
}

export interface RunArtifact {
  id: string
  project_id: string
  task_run_id: string | null
  kind: string
  kind_deprecated: boolean
  deprecation_message: string | null
  category: string
  uri: string
  sha256: string
  size: number
  created_at: string
  metadata: Record<string, unknown>
}
