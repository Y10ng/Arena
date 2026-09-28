export interface MonitoringTaskRun {
  id: string;
  project_id: string;
  project_title: string;
  project_status: string;
  intent_id?: string | null;
  task_type: string;
  worker_name: string;
  worker_type: string;
  status: string;
  session_id?: string | null;
  queued_at?: string | null;
  queue_wait_seconds?: number | null;
  started_at?: string | null;
  ended_at?: string | null;
  duration_seconds?: number | null;
  error_type?: string | null;
  error_message?: string | null;
  artifact_count: number;
  artifact_root?: string | null;
  heartbeat_source: 'intent' | 'reason' | 'none';
  heartbeat_status: 'active' | 'missing' | 'stale' | 'not_tracked';
  heartbeat_at?: string | null;
  heartbeat_age_seconds?: number | null;
  heartbeat_timeout_seconds?: number | null;
  heartbeat_worker?: string | null;
}

export interface MonitoringWorkerSummary {
  worker_name: string;
  worker_type: string;
  health: 'idle' | 'running' | 'healthy' | 'unhealthy';
  configured: boolean;
  task_types: string[];
  priority?: number | null;
  max_running?: number | null;
  pool_running_count?: number | null;
  running_count: number;
  queued_count: number;
  succeeded_count: number;
  failed_count: number;
  timed_out_count: number;
  cancelled_count: number;
  consecutive_failure_count: number;
  last_status?: string | null;
  last_error_type?: string | null;
  last_started_at?: string | null;
  last_ended_at?: string | null;
  latest_healthcheck_status?: string | null;
  latest_healthcheck_at?: string | null;
  latest_healthcheck_phase?: string | null;
  latest_healthcheck_duration_seconds?: number | null;
  latest_healthcheck_command_kind?: string | null;
  supports_resume?: boolean | null;
  supports_json_mode?: boolean | null;
  max_context?: number | null;
}

export interface DispatcherWorkerCooldown {
  worker_name: string;
  worker_type?: string | null;
  error_type: string;
  retry_after_seconds: number;
  retry_until_at?: string | null;
}

export interface DispatcherWorkerRejection {
  project_id: string;
  task_type: string;
  worker_name: string;
  worker_type?: string | null;
  error_type: string;
  retry_after_seconds: number;
  retry_until_at?: string | null;
}

export interface DispatcherWorkerPoolItem {
  worker_name: string;
  worker_type: string;
  task_types: string[];
  priority: number;
  max_running: number;
  running_count: number;
  worker_id?: string | null;
  engine?: string | null;
  model_id?: string;
  concurrency?: number | null;
  enabled?: boolean;
  supports_resume?: boolean | null;
  supports_json_mode?: boolean | null;
  max_context?: number | null;
  env_keys?: string[];
}

export interface MonitoringProjectConcurrency {
  project_id: string;
  project_title: string;
  project_status: string;
  running_task_count?: number;
  queued_task_count?: number;
  active_reason_worker?: string | null;
}

export interface MonitoringDispatcherSummary {
  project_count: number;
  active_project_count: number;
  running_project_count: number;
  running_task_count: number;
  queued_task_count: number;
  last_dispatcher_id?: string | null;
  last_status?: string | null;
  effective_status?: string | null;
  started_at?: string | null;
  config_hash?: string | null;
  host?: string | null;
  config_path?: string | null;
  config_mtime?: number | null;
  config_sha256?: string | null;
  runtime?: Record<string, any>;
  container?: Record<string, any>;
  server_reachable_path?: string | null;
  last_loop_started_at?: string | null;
  last_loop_finished_at?: string | null;
  last_loop_duration_ms?: number | null;
  last_error_message?: string | null;
  updated_at?: string | null;
  heartbeat_age_seconds?: number | null;
  heartbeat_stale: boolean;
  worker_cooldowns: DispatcherWorkerCooldown[];
  worker_rejections: DispatcherWorkerRejection[];
  worker_pool: DispatcherWorkerPoolItem[];
  projects?: MonitoringProjectConcurrency[];
  container_images?: string[];
  cap_baseline?: string[];
  declared_inventory?: Record<string, any>;
}

export interface TenantMonitoringTaskRun {
  id: string;
  project_id: string;
  project_title: string;
  task_type: string;
  status: string;
  queued_at?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  duration_seconds?: number | null;
  heartbeat_status: string;
  worker_name: string;
}

export interface TenantMonitoringSummary {
  generated_at: string;
  long_running_threshold_seconds: number;
  project_count: number;
  active_project_count: number;
  dispatcher: {
    status?: string | null;
    stale: boolean;
    updated_at?: string | null;
  };
  queued_tasks: TenantMonitoringTaskRun[];
  running_tasks: TenantMonitoringTaskRun[];
  long_running_tasks: TenantMonitoringTaskRun[];
  recent_failures: TenantMonitoringTaskRun[];
  vulnerabilities?: {
    total: number;
    by_severity: Record<string, number>;
    by_status: Record<string, number>;
    per_project: { project_id: string; title: string; count: number }[];
  };
}

export interface MonitoringPromptEffectItem {
  prompt_group?: string;
  prompt_name?: string;
  prompt_hash?: string;
  task_count?: number;
  succeeded_count?: number;
  failed_count?: number;
  parse_error_count?: number;
  latest_task_run_id?: string | null;
  latest_project_id?: string | null;
  latest_project_title?: string | null;
  latest_task_type?: string | null;
  latest_worker_name?: string | null;
  latest_worker_type?: string | null;
  latest_task_status?: string | null;
  latest_rendered_at?: string | null;
}

export interface MonitoringTrendBucket {
  bucket_start: string;
  bucket_end: string;
  started_count?: number;
  finished_count?: number;
  succeeded_count?: number;
  failed_count?: number;
  failure_rate?: number;
}

export interface MonitoringSummary {
  generated_at: string;
  long_running_threshold_seconds: number;
  dispatcher: MonitoringDispatcherSummary;
  queued_tasks: MonitoringTaskRun[];
  running_tasks: MonitoringTaskRun[];
  long_running_tasks: MonitoringTaskRun[];
  workers: MonitoringWorkerSummary[];
  artifacts: {
    total_count: number;
    total_size: number;
    raw_output_count: number;
    raw_output_size: number;
    largest_raw_output_artifact_id?: string | null;
    largest_raw_output_project_id?: string | null;
    largest_raw_output_task_run_id?: string | null;
    largest_raw_output_size: number;
    evidence_count: number;
    deliverable_count: number;
    declared_count: number;
    declared_by_kind?: Record<string, number>;
    latest_artifact_id?: string | null;
    latest_created_at?: string | null;
    latest_uri?: string | null;
    write_failed_task_count: number;
    recent_write_failures: any[];
  };
  result_audit: {
    recent_event_limit?: number;
    event_count: number;
    latest_event_at?: string | null;
    validation: {
      total_count: number;
      valid_count: number;
      invalid_count: number;
      rejected_count: number;
      unknown_count?: number;
    };
    protocol_write: {
      total_count: number;
      success_count: number;
      failed_count: number;
      skipped_count: number;
      unknown_count?: number;
    };
    decision: {
      total_count: number;
      complete_count: number;
      fact_count: number;
      intents_count: number;
      noop_count: number;
      low_confidence_count: number;
      blocked_count: number;
      target_dependency_count: number;
      created_intent_count: number;
      unknown_count?: number;
      concluded_created_intent_count?: number;
      goal_path_created_intent_count?: number;
      open_created_intent_count?: number;
      missing_created_intent_count?: number;
    };
  };
  prompt_effect: {
    recent_event_limit?: number;
    version_count: number;
    total_task_count: number;
    latest_rendered_at?: string | null;
    items: MonitoringPromptEffectItem[];
  };
  trends: {
    bucket_seconds?: number;
    bucket_count?: number;
    window_start?: string | null;
    window_end?: string | null;
    buckets: MonitoringTrendBucket[];
  };
  agent_events: {
    recent_event_limit: number;
    event_count: number;
    latest_event_at?: string | null;
    by_kind: { kind: string; count: number }[];
    command_finished: {
      total_count: number;
      success_count: number;
      failed_count: number;
    };
    hook_operation: {
      total_count: number;
      failed_count: number;
      denied_count: number;
      high_risk_count: number;
      knowledge_hit_count: number;
      knowledge_hits: any[];
      recent_failures: any[];
      recent_high_risk: any[];
    };
    running_commands: any[];
    recent_command_failures: any[];
  };
  recent_failures: MonitoringTaskRun[];
  error_counts: { error_type: string; count: number; latest_task_run?: MonitoringTaskRun | null }[];
  vulnerabilities?: {
    total: number;
    by_severity: Record<string, number>;
    by_status: Record<string, number>;
    per_project: { project_id: string; title: string; count: number }[];
  };
}
