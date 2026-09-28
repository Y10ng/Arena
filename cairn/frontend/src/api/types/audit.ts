export type TaskRunResultEventKind =
  'validation_result' | 'protocol_write' | 'network_flow' | 'decision_result'

export interface TaskRunResultEvent {
  id: string
  task_run_id: string
  project_id: string
  ts: string
  kind: TaskRunResultEventKind
  payload: Record<string, unknown>
}

export interface TaskRunResultEventGroup {
  task_run_id: string
  events: TaskRunResultEvent[]
}

export interface FactEvidence {
  project_id: string
  fact_id: string
  artifact_id: string
  pointer: string
  verifier_run_id: string | null
  artifact_exists: boolean
  verifier_run_exists: boolean | null
  evidence_valid: boolean
  created_at: string
}
