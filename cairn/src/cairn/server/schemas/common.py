"""Server 对外协议模型，集中定义 API 请求/响应、审计事件和监控读模型。"""

from __future__ import annotations

from base64 import b64decode
from binascii import Error as BinasciiError
from hashlib import sha256
from re import fullmatch
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, field_validator, model_validator

# Worker/Task 的枚举既用于 API 校验，也被 dispatcher client 复用，新增值需同步两端处理逻辑。
# report 使用冻结的场景报告策略生成持久化成果，不依赖运行时探索 Prompt 组。
TaskType = Literal["bootstrap", "reason", "explore", "verify", "conclude", "report", "external_verify"]


WorkerType = Literal["claudecode", "codex", "pi", "mock"]


CompletedAction = Literal["remove", "stop"]


ExecutionProfile = Literal["shared_container", "shared_local"]


ProjectStatus = Literal["pending", "active", "paused", "stopped", "completed"]


TaskRunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "timed_out"]


# 标准错误类型用于 dispatcher、审计和监控聚合对齐，未知错误进入 unknown。
TaskRunErrorType = Literal[
    "provider_auth",
    "provider_rate_limit",
    "provider_error",
    "session_error",
    "transport_error",
    "protocol_error",
    "contract_error",
    "request_contract",
    "result_schema",
    "submission_missing",
    "submission_invalid",
    "delivery_unavailable",
    "stale_business_state",
    "receipt_conflict",
    "business_acceptance",
    "candidate_integrity",
    "artifact_error",
    "parse_error",
    "repair_failed",
    "worker_timeout",
    "conclude_timeout",
    "worker_unhealthy",
    "readiness_timeout",
    "readiness_transport",
    "readiness_oom",
    "readiness_invalid",
    "readiness_process",
    "healthcheck_timeout",
    "api_unreachable",
    "api_incompatible",
    "config_mismatch",
    "worker_rejected",
    "worker_command_failed",
    "worker_oom",
    "heartbeat_lost",
    "server_write_failed",
    "report_packet_read_failed",
    "tool_missing",
    "network_blocked",
    "target_unreachable",
    "auth_failed",
    "policy_denied",
    "evidence_insufficient",
    "artifact_write_failed",
    "container_failed",
    "container_runtime_mismatch",
    "cancelled",
    "unknown",
]


STANDARD_TASK_RUN_ERROR_TYPES: set[str] = set(get_args(TaskRunErrorType))


ArtifactKind = Literal["log", "file", "report", "screenshot", "patch", "pcap", "har", "raw_output", "hook_log"]


CreateArtifactKind = Literal["log", "file", "report", "screenshot", "patch", "raw_output", "hook_log"]


ArtifactCategory = Literal["evidence", "deliverable"]


FROZEN_ARTIFACT_KINDS = {"pcap", "har"}


FROZEN_ARTIFACT_KIND_MESSAGE = "冻结兼容：当前阶段不再新建或采集此类产物，仅保留历史读取。"


TargetType = Literal["ip", "cidr", "domain", "url"]


TargetStatus = Literal["seed", "discovered", "approved", "rejected"]


# network_flow 是历史兼容读模型，当前主线只保留摘要解析和展示能力。
NETWORK_FLOW_STATUSES = {"observed", "allowed", "blocked", "failed", "unknown"}


NETWORK_FLOW_NETWORK_PLANES = {"none", "control", "target"}


NETWORK_FLOW_PROTOCOLS = {
    "dns",
    "ftp",
    "http",
    "https",
    "icmp",
    "imap",
    "pop3",
    "smtp",
    "ssh",
    "tcp",
    "tls",
    "udp",
    "ws",
    "wss",
}


NETWORK_FLOW_IDS = {
    "target_scope_execution_preflight",
    "target_scope_policy_probe",
    "target_scope_policy_enforcement",
    "target_side_flow_metadata",
    "target_side_packet_flow",
    "target_side_web_flow",
}


AgentEventKind = Literal[
    "prompt_rendered",
    "worker_started",
    "worker_stdout",
    "worker_stderr",
    "tool_call",
    "command_started",
    "command_finished",
    "artifact_created",
    "network_flow",
    "runtime_context_updated",
    "hook_operation",
    "candidate_fact",
    "validation_result",
    "verifier_result",
    "decision_result",
    "protocol_write",
]


# pi 事件保持 SDK 原名；Claude 条目仅用于历史审计读取，不代表执行支持。
HOOK_EVENT_NAMES = {
    "tool_execution_start",
    "tool_execution_end",
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "SessionEnd",
}


HOOK_TOOL_NAMES = {
    "read",
    "bash",
    "edit",
    "write",
    "grep",
    "find",
    "ls",
    "read_evidence",
    "register_artifact",
    "submit_result",
    "Bash",
    "Read",
    "Glob",
    "Grep",
    "Edit",
    "MultiEdit",
    "Write",
    "WebFetch",
    "WebSearch",
    "Task",
}


HOOK_OPERATION_STATUSES = {
    "started",
    "submitted",
    "planned",
    "allowed",
    "denied",
    "succeeded",
    "failed",
    "stopped",
    "ended",
    "unknown",
}


HOOK_RISK_LEVELS = {"low", "medium", "high", "unknown"}


IntentWorkerHistoryReason = Literal["released", "project_stopped", "project_completed", "heartbeat_timeout", "abandoned"]


__all__ = [name for name in globals() if not name.startswith("__")]
