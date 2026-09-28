"""监控聚合服务共享依赖和常量。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from json import JSONDecodeError, dumps, loads
from math import ceil
from typing import Literal

from cairn.server.models import (
    DispatcherHeartbeatRequest,
    DispatcherHeartbeatStatus,
    DispatcherWorkerCooldown,
    DispatcherWorkerPoolItem,
    DispatcherWorkerRejection,
    MonitoringAgentEventKindSummary,
    MonitoringAgentEventSummary,
    MonitoringArtifactSummary,
    MonitoringCommandFailure,
    MonitoringCommandEventSummary,
    MonitoringDecisionResultSummary,
    MonitoringDispatcherSummary,
    MonitoringErrorAggregate,
    MonitoringHookFailure,
    MonitoringHookOperationSummary,
    MonitoringKnowledgeHit,
    MonitoringNetworkFlowSummary,
    MonitoringPromptEffectItem,
    MonitoringPromptEffectSummary,
    MonitoringProjectConcurrency,
    MonitoringProtocolWriteSummary,
    MonitoringResultAuditSummary,
    MonitoringRunningCommand,
    MonitoringSummary,
    MonitoringTaskRun,
    MonitoringTrendBucket,
    MonitoringTrendSummary,
    VulnerabilitiesSummary,
    MonitoringValidationResultSummary,
    MonitoringWorkerSummary,
    STANDARD_TASK_RUN_ERROR_TYPES,
)
from cairn.server.services.event_payloads import event_payload, hook_event_to_model
from cairn.server.services.knowledge import runtime_knowledge_base_path
from cairn.server.services.payload_values import (
    command_finished_status,
    duration_payload_value,
    integer_payload_value,
    network_flow_id,
    network_flow_network_plane,
    network_flow_protocol,
    network_flow_status,
    normalized_payload_status,
    payload_text,
    runtime_context_text,
    sanitized_command_kind,
    sanitized_prompt_hash,
    sanitized_prompt_label,
)
from cairn.server.services.projects import (
    expire_reason_leases,
    expire_workers,
    get_intent_timeout,
    get_reason_timeout,
)
from cairn.server.services.runtime_context import standard_task_run_error_type, task_run_runtime_context
from cairn.server.services.time_utils import timestamp_delta_seconds, utcnow

UNHEALTHY_ERROR_TYPES = {
    "worker_unhealthy",
    "healthcheck_timeout",
    "api_unreachable",
    "api_incompatible",
    "config_mismatch",
    "worker_timeout",
    "worker_command_failed",
    "worker_oom",
    "heartbeat_lost",
    "container_failed",
}
RESULT_AUDIT_EVENT_LIMIT = 100
DECISION_LOW_CONFIDENCE_THRESHOLD = 0.5
PROMPT_EFFECT_EVENT_LIMIT = 100
AGENT_EVENT_LIMIT = 200
COMMAND_FAILURE_LIMIT = 10
HOOK_OPERATION_ITEM_LIMIT = 10
KNOWLEDGE_HIT_ITEM_LIMIT = 10
TREND_BUCKET_SECONDS = 7200
TREND_BUCKET_COUNT = 12
WORKER_HEALTHCHECK_EVENT_LIMIT = 2000
WORKER_RECENT_CLOSED_TASK_LIMIT = 50
ERROR_COUNT_WINDOW_DAYS = 14
ERROR_COUNT_LIMIT = 500
SummaryProfile = Literal["full", "overview", "monitoring", "dispatcher"]

__all__ = [name for name in globals() if not name.startswith("__")]
