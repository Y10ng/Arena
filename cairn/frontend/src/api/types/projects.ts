import type { ProjectGroup } from './groups'
import type { TenantWorker } from './workers'

export interface ProjectReason {
  worker?: string | null
  worker_name?: string | null
  trigger?: string | null
  started_at?: string | null
  last_heartbeat_at?: string | null
}

export interface ProjectWorkerBinding {
  id: string
  display_name: string
  revision_at_bind: number
  task_types: string[]
  max_running: number
  priority: number
  enabled: boolean
  currently_available: boolean
  engine?: string | null
  unavailable_reason?: string | null
}

export interface UpdateProjectWorkersRequest {
  tenant_worker_ids: string[]
}

export interface ReportCapability {
  supported: boolean
  policy_version: number
  template_profile: string | null
  reason: string | null
}

export interface ProjectSummary {
  id: string
  title: string
  status: 'active' | 'paused' | 'stopped' | 'completed'
  created_at: string
  scenario_key: string
  assessment_protocol: string
  vulnerability_fingerprint_version?: number | null
  report_capability: ReportCapability
  coverage_stop_reason?: string | null
  tenant_workers: ProjectWorkerBinding[]
  preferred_workers?: string[]
  tenant_id: string
  tenant_name: string
  tenant_owner_login?: string | null
  group_id?: string | null
  group_name?: string | null
  reason?: ProjectReason | null
  fact_count: number
  intent_count: number
  working_intent_count: number
  unclaimed_intent_count: number
  hint_count: number
  execution_generation?: number
  authorization_epoch?: number
  readonly authorization_block_reason?: string | null
  authorization_scope_digest?: string | null
  authorization_window_seconds?: number | null
  authorization_expires_at?: string | null
  explore_deadline?: string | null
  explore_attempts_started?: number
  verification_attempts_started?: number
}

export type ProjectLifecycleAction = 'pause' | 'resume' | 'reauthorize' | 'finish'
export type ProjectRuntimeAction = ProjectLifecycleAction | 'reopen'

export interface ReauthorizeProjectRequest {
  confirmed: true
  expected_authorization_epoch: number
  expected_scope_digest: string
  authorization_expires_at?: string | null
}

export interface ProjectRuntimeBase {
  project_id: string
  observed_at: string
  execution_generation: number
  project_status: ProjectSummary['status']
  state: string
  code: string
  message: string
  dispatcher: {
    status: string
    freshness: 'fresh' | 'stale' | 'unknown'
    updated_at: string | null
    last_loop_finished_at?: string | null
  }
  scheduling: { code: string; observed_at: string | null; retry_at: string | null } | null
  active_runs: Array<{
    id: string
    task_type: string
    status: string
    worker_name: string
    execution_stage: string
    started_at: string | null
  }>
  last_error: {
    code: string
    message: string
    task_run_id: string | null
    submission?: {
      code: string
      issue?: { keyword: string; path: string } | null
      submission_attempts?: number | null
      repair_rounds?: number | null
      max_repair_rounds?: number | null
    } | null
  } | null
  last_activity: { kind: string; recorded_at: string; task_run_id: string | null } | null
  actions: Record<ProjectRuntimeAction, { allowed: boolean; reason: string | null }>
}

export interface ProjectRuntimeV1 extends ProjectRuntimeBase {
  schema_version: 1
}

export interface ProjectRuntimeAxis {
  state: string
  code: string | null
}

export type ProjectRuntimeAcceptanceState =
  | 'accepted'
  | 'partially_accepted'
  | 'not_evaluated'
  | 'rejected'

export type ProjectRuntimeAcceptanceEvidence =
  | 'conclusion_receipt'
  | 'completion_receipt'
  | 'intent_submission_prefix'
  | 'depth_review_receipt'
  | 'report_finish_receipt'

export interface ProjectRuntimeAcceptanceAxis extends ProjectRuntimeAxis {
  state: ProjectRuntimeAcceptanceState
  evidence: ProjectRuntimeAcceptanceEvidence | null
}

export interface ProjectRuntimeV2 extends ProjectRuntimeBase {
  schema_version: 2
  axes: {
    task_run_id: string | null
    execution: ProjectRuntimeAxis
    delivery: ProjectRuntimeAxis
    acceptance: ProjectRuntimeAcceptanceAxis
    process: ProjectRuntimeAxis & { termination_confirmed: boolean }
    resources: ProjectRuntimeAxis & { cleanup_confirmed: boolean }
  }
  checkpoint_staging: {
    state: 'none' | 'staged'
    received_count: number
    current_generation_count: number
    historical_generation_count: number
    last_received_at: string | null
    last_task_run_id: string | null
    business_acceptance: 'not_evaluated'
  }
  recovery: {
    route: 'none' | 'wait' | 'automatic_retry' | 'resume' | 'reauthorize' | 'manual'
    code: string | null
  }
}

export type ProjectRuntime = ProjectRuntimeV1 | ProjectRuntimeV2

export interface ProjectManagementOptions {
  groups: ProjectGroup[]
  tenant_workers: TenantWorker[]
}

export interface AssessmentEvidenceRef {
  artifact_id?: string | null
  artifact_handle?: string | null
  pointer: string
  verifier_run_id?: string | null
}

export type ExperimentAssessmentOutcome =
  | 'tested_no_finding'
  | 'finding'
  | 'temporarily_blocked'
  | 'permanently_unreachable'
  | 'out_of_scope'
  | 'not_applicable'
  | 'observation'

export interface ExperimentAssessmentRecord {
  version: 1
  kind: 'experiment'
  hypothesis: string
  outcome?: ExperimentAssessmentOutcome | null
  preconditions: string[]
  baseline: string
  variants: string[]
  controls: string[]
  observations: string[]
  impact: string
  limitations: string[]
  blocker_code?: string
  limitation_reason?: string
  limitation_details?: string
  attempted_strategies?: string[]
  evidence_refs: AssessmentEvidenceRef[]
}

export interface Fact {
  id: string
  project_id: string
  description: string
  assessment_record?: ExperimentAssessmentRecord | null
  task_run_id?: string | null
  creator: string
  created_at: string
}

export interface IntentWorkerHistoryEntry {
  worker?: string | null
  reason?: string | null
  cleared_at?: string | null
}

export interface Intent {
  id: string
  project_id: string
  from: string[]
  to?: string | null
  description: string
  creator: string
  priority?: 0 | 1 | 2
  assessment_role?: 'discovery' | 'verifier'
  target_observation_id?: string | null
  depends_on?: string | null
  task_run_id?: string | null
  worker?: string | null
  last_heartbeat_at?: string | null
  created_at: string
  concluded_at?: string | null
  conclusion?: string | null
  conclusion_reason?: string | null
  worker_history?: IntentWorkerHistoryEntry[]
}

export interface Hint {
  id: string
  project_id?: string
  content: string
  creator: string
  fact_id?: string | null
  intent_id?: string | null
  created_at: string
}

export interface CreateProjectHintRequest {
  content: string
  fact_id?: string
  intent_id?: string
}

export type TargetType = 'ip' | 'cidr' | 'domain' | 'url'
export type TargetStatus = 'seed' | 'discovered' | 'approved' | 'rejected'

export interface Target {
  id: string
  project_id?: string
  value: string
  type: TargetType
  status: TargetStatus
  source_fact_id?: string | null
  discovered_by_run_id?: string | null
  created_at: string
}

export interface CreateTargetRequest {
  value: string
  type: TargetType
  status?: TargetStatus
  source_fact_id?: string | null
  discovered_by_run_id?: string | null
}

export interface UpdateTargetRequest {
  status: TargetStatus
}

export interface CoverageQualitySnapshot {
  total_count: number
  qualified_count: number
  unqualified_count: number
  unresolved_count: number
  finding_count: number
  tested_no_finding_count: number
  permanent_limitation_count: number
  administrative_skip_count: number
}

export interface AssessmentQualitySnapshot {
  version: number
  status: 'observing' | 'ready'
  required: CoverageQualitySnapshot
  optional: CoverageQualitySnapshot
  manifest_surface_count: number
  missing_required_surface_count: number
  charged_attempt_count: number
  runtime_execution_count: number
  runtime_retry_count: number
  failed_attempt_count: number
  active_blocker_count: number
  active_cooldown_count: number
  finding_count: number
  observation_count: number
  confirmed_finding_count: number
  independently_verified_finding_count: number
  pending_verifier_count: number
  chain_candidate_count: number
  chain_verification_ready_count: number
  confirmed_chain_count: number
  stop_reason?: string | null
  residual_risks: string[]
}

export interface EvidenceRef {
  artifact_id: string
  pointer?: string
  verifier_run_id?: string | null
}

export interface SurfaceManifestEntry {
  entry_id: string
  project_id: string
  surface_id: string
  surface_kind: string
  surface_key: string
  description: string
  traits: Record<string, unknown>
  evidence: EvidenceRef[]
  source_intent_id?: string | null
  source_fact_id?: string | null
  source_task_run_id?: string | null
  observation_fingerprint: string
  manifest_version: number
  status: string
  created_at: string
}

export interface CoverageCell {
  project_id: string
  slot_key: string
  kind: string
  description: string
  status: string
  surface_key?: string | null
  surface_id?: string | null
  surface_kind: string
  test_family: string
  coverage_requirement: string
  coverage_rule_id: string
  applicability_basis: string
  completion_criteria: string
  result: string
  skip_reason: string
  blocker_fingerprint?: string | null
  blocker_strategy_key: string
  blocker_cooldown_until?: string | null
  tier: string
  source_intent_id?: string | null
  assigned_intent_id?: string | null
  resulting_fact_id?: string | null
  verified_run_id?: string | null
  created_at: string
  updated_at: string
}

export interface AssessmentReportGenerationRequest {
  template_key: string
  template_version: number
}

export interface AssessmentReportFinalization {
  project_id: string
  version: number
  status: 'pending' | 'running' | 'ready' | 'failed'
  attempt_count: number
  claimed_by: string | null
  lease_expires_at: string | null
  next_attempt_at: string | null
  artifact_id: string | null
  last_error: string
  generation_started_at: string
  generation_token: string
  claim_token: string | null
  requested_at: string | null
  requested_by: string | null
  template_key: string | null
  template_version: number | null
  template_digest_sha256: string | null
  input_snapshot_artifact_id?: string | null
  input_snapshot_sha256?: string | null
  report_policy_version?: number | null
  report_execution_plan_sha256?: string | null
  report_prompt_digest_sha256?: string | null
  created_at: string
  updated_at: string
  completed_at: string | null
}

export type ProjectResourceKind = 'note' | 'script' | 'file'
export type ProjectResourceBatchState = 'open' | 'consumed'
export type ProjectResourceBatchMemberSource = 'file' | 'zip'

export interface ProjectResourceLimits {
  max_active_count: number
  max_file_bytes: number
  max_active_total_bytes: number
  max_zip_upload_bytes: number
  max_zip_entries: number
  max_compression_ratio: number
  max_open_batches_per_user: number
  max_staged_bytes_per_user: number
  max_path_bytes: number
  max_path_segment_bytes: number
}

export interface ProjectResourceConfig {
  kinds: ProjectResourceKind[]
  limits: ProjectResourceLimits
  reserved_names: string[]
  batch_ttl_seconds: number
  zip_policy: {
    allowed_compression_methods: string[]
    nested_archives: 'opaque'
    preserve_relative_directories: boolean
  }
}

export interface ProjectResourceBatchMember {
  id: string
  kind: ProjectResourceKind
  path: string
  size_bytes: number
  sha256: string
  source: ProjectResourceBatchMemberSource
}

export interface ProjectResourceBatch {
  id: string
  state: ProjectResourceBatchState
  created_at: string
  expires_at: string
  resource_count: number
  total_bytes: number
  members: ProjectResourceBatchMember[]
}

export interface UpdateProjectResourceBatchMemberRequest {
  path?: string
  kind?: ProjectResourceKind
}

export interface ProjectResourceImportResponse {
  resources: ProjectResource[]
}

export interface ProjectResource {
  id: string
  project_id: string
  kind: ProjectResourceKind
  scope: 'project'
  path: string
  size_bytes: number
  sha256: string
  active: boolean
  created_by: string
  created_at: string
  deactivated_at?: string | null
}

export interface CreateProjectResourceRequest {
  kind: ProjectResourceKind
  path: string
  content: string
}

export interface ProjectLibraryBinding {
  library_key: string
  library_name: string
  version: number
  digest_sha256: string
  file_count: number
}

export interface CreateProjectRequest {
  title: string
  origin: string
  goal: string
  scenario: string
  tenant_worker_ids: string[]
  group_id: string
  explore_deadline?: string
  authorization_expires_at: string | null
  hints?: Array<{ content: string }>
  targets?: Array<{ value: string; type: TargetType }>
  resources?: CreateProjectResourceRequest[]
  resource_batch_id?: string
  library_keys?: string[]
}

export interface ProjectDetail {
  project: {
    id: string
    title: string
    status: 'active' | 'paused' | 'stopped' | 'completed'
    created_at: string
    scenario_key: string
    tenant_workers: ProjectWorkerBinding[]
    preferred_workers?: string[]
    tenant_id: string
    tenant_name: string
    tenant_owner_login?: string | null
    group_id?: string | null
    group_name?: string | null
    reason?: ProjectReason | null
    assessment_protocol: string
    report_capability: ReportCapability
    coverage_stop_reason?: string | null
    assessment_quality?: AssessmentQualitySnapshot | null
    execution_generation?: number
    authorization_epoch?: number
    readonly authorization_block_reason?: string | null
    authorization_scope_digest?: string | null
    authorization_window_seconds?: number | null
    authorization_expires_at?: string | null
    explore_deadline?: string | null
    explore_attempts_started?: number
    verification_attempts_started?: number
  }
  facts: Fact[]
  intents: Intent[]
  hints: Hint[]
  targets: Target[]
  resources: ProjectResource[]
  library_bindings: ProjectLibraryBinding[]
}
