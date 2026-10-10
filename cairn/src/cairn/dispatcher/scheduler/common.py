"""Dispatcher 主调度循环，协调项目状态、worker 选择、容器和任务生命周期。"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Literal

import requests

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.models import BindingCircuitKey, ReasonCheckpoint, RunningTask
from cairn.dispatcher.prompting import knowledge_base_runtime_context
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager, ContainerRuntimeOptions
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.runtime.readiness import READINESS_ERROR_TYPES, run_readiness_check
from cairn.dispatcher.runtime.startup_healthcheck import format_failure_summary, run_startup_healthchecks
from cairn.dispatcher.scheduler.worker_select import choose_worker
from cairn.dispatcher.tasks.bootstrap import run_bootstrap_task
from cairn.dispatcher.tasks.explore import run_explore_task
from cairn.dispatcher.tasks.reason import run_reason_task
from cairn.dispatcher.tasks.report import run_post_completion_report
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import Intent, ProjectDetail, ProjectSummary, Scenario, Target
from cairn.server.schemas.common import STANDARD_TASK_RUN_ERROR_TYPES

LOG = logging.getLogger(__name__)
WORKER_COOLDOWN_RETRY_AFTER_SECONDS = 5
REJECTED_RETRY_AFTER_SECONDS = 5
WORKER_BACKOFF_MAX_SECONDS = 60
WORKER_HEALTH_BACKOFF_MAX_SECONDS = 300
RUNTIME_PROBE_TIMEOUT_SECONDS = 5
RUNTIME_PROBE_VALUE_LIMIT = 160
PROJECT_WORKSPACE_PATH = "/home/kali/workspace"
RUNTIME_TOOL_VERSION_PROBES = {
    "git": ["git", "--version"],
    "curl": ["curl", "--version"],
    "nmap": ["nmap", "--version"],
}
BOOTSTRAP_INTENT_DESCRIPTION = "bootstrap"
BOOTSTRAP_INTENT_CREATOR = "dispatcher.bootstrap"
WORKER_COOLDOWN_ERROR_TYPES = {
    *READINESS_ERROR_TYPES,
    "provider_auth",
    "provider_rate_limit",
    "provider_error",
    "transport_error",
    "worker_unhealthy",
    "healthcheck_timeout",
    "api_unreachable",
    "api_incompatible",
    "config_mismatch",
    "worker_timeout",
    "worker_command_failed",
}
REJECTION_RETRY_ERROR_TYPES = {
    "worker_rejected",
    "artifact_write_failed",
    "tool_missing",
    "network_blocked",
    "target_unreachable",
    "auth_failed",
    "evidence_insufficient",
    "worker_oom",
}
STANDARD_ERROR_TYPES = STANDARD_TASK_RUN_ERROR_TYPES
DEFAULT_WORKER_FEATURES = {
    "pi": {
        "supports_resume": True,
        "supports_json_mode": True,
        "max_context": None,
    },
}
ERROR_MESSAGES = {
    "readiness_timeout": "Worker 离线就绪探针超时",
    "readiness_transport": "Worker 离线就绪探针无法执行",
    "readiness_oom": "Worker 离线就绪探针内存超限退出",
    "readiness_invalid": "Worker 离线就绪探针响应无效",
    "readiness_process": "Worker 离线就绪探针进程失败",
    "provider_auth": "模型服务认证失败",
    "provider_rate_limit": "模型服务限流，SDK 有界重试已结束",
    "provider_error": "模型服务请求失败",
    "session_error": "Pi 会话状态或归属无效",
    "transport_error": "Worker 结果通道失败",
    "protocol_error": "Worker 结果协议无效",
    "contract_error": "Worker 结果不符合阶段契约",
    "request_contract": "Worker 请求契约无效",
    "result_schema": "Worker 结果结构无效或修复次数耗尽",
    "business_acceptance": "Worker 结果未通过业务验收",
    "candidate_integrity": "Worker candidate 捕获完整性校验失败",
    "artifact_error": "Worker Artifact 或证据引用无效",
    "parse_error": "Worker 输出无法解析",
    "repair_failed": "Worker 输出修复失败",
    "worker_timeout": "Worker 主执行超时",
    "conclude_timeout": "Worker 收尾执行超时",
    "worker_unhealthy": "Worker 健康检查失败",
    "healthcheck_timeout": "Worker 健康检查超时",
    "api_unreachable": "Worker API 不可达",
    "api_incompatible": "Worker API 不兼容",
    "config_mismatch": "Worker API 配置不匹配",
    "worker_rejected": "Worker 拒绝任务",
    "worker_command_failed": "Worker 命令执行失败",
    "worker_oom": "Worker 内存超限退出",
    "heartbeat_lost": "任务心跳丢失",
    "server_write_failed": "Dispatcher 写回 Server 失败",
    "submission_missing": "Worker 未提交终态结果",
    "submission_invalid": "Worker 终态提交无效",
    "delivery_unavailable": "原执行交付上下文不可用",
    "stale_business_state": "执行结果对应的业务状态已过期",
    "receipt_conflict": "幂等收据摘要冲突",
    "tool_missing": "执行环境缺少必要工具",
    "network_blocked": "网络访问被阻断",
    "target_unreachable": "目标不可达",
    "auth_failed": "认证失败",
    "policy_denied": "策略拒绝执行",
    "evidence_insufficient": "证据不足",
    "artifact_write_failed": "Artifact 写入失败",
    "container_failed": "容器运行失败",
    "container_runtime_mismatch": "运行环境与保留的项目容器不一致；工作区已保留，请恢复原容器配置后再继续",
    "cancelled": "任务已取消",
    "unknown": "未分类任务失败",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_from_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _runtime_probe_value(value: object) -> str:
    text = " ".join(str(value).split())
    if not text:
        return "unknown"
    text = text.strip()
    if not text:
        return "unknown"
    if len(text) <= RUNTIME_PROBE_VALUE_LIMIT:
        return text
    return text[: RUNTIME_PROBE_VALUE_LIMIT - 3].rstrip() + "..."


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class DeliveryWorkerRoute:
    """Server 裁定的执行模式；首次执行与原交付续跑不能在后续重新猜测。"""

    workers: list[object]
    mode: Literal["fresh", "delivery_retry", "continuation"]
    container_id: str | None = None


@dataclass(slots=True)
class WorkerSelection:
    """一次 worker 选择的结果和所有被排除候选的原因，用于 runtime_context 审计。"""

    worker: WorkerConfig | None
    candidate_order: list[dict[str, object]]
    blocked_busy: list[dict[str, object]]
    blocked_cooldown: list[dict[str, object]]
    blocked_rejected: list[dict[str, object]]
    blocked_task_type: list[dict[str, object]]
    blocked_disabled: list[dict[str, object]]
    blocked_not_preferred: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class PendingTaskRunFinish:
    """Server 暂时不可写时缓存的 task_run 收尾更新。"""

    project_id: str
    task_run_id: str
    status: str
    session_id: str | None
    exit_code: int | None
    error_type: str | None
    error_message: str | None
    task_type: str | None
    worker_name: str | None
    worker_type: str | None
    container_runtime: ContainerRuntimeOptions | None
    execution_lifecycle: dict[str, object] | None = None
    execution_container_ids: list[str] | None = None
    worker_failure: dict[str, object] | None = None


@dataclass(slots=True)
class DispatcherBootstrap:
    """Dispatcher 通过本地 YAML 文件引导，运行时热加载同一配置文件。"""

    config_path: Path
    hard_cap: int = 64


def _target_scope_runtime_context(targets: list[Target]) -> dict[str, object]:
    # dispatcher 在任务开始时冻结 target scope 快照，worker 后续只能按该快照执行。
    scope: dict[str, object] = {
        "active": [],
        "discovered": [],
        "rejected": [],
        "policy": _target_scope_snapshot_context(),
    }
    for target in targets:
        item = {
            "id": target.id,
            "value": target.value,
            "type": target.type,
            "status": target.status,
            "source_fact_id": target.source_fact_id,
            "discovered_by_run_id": target.discovered_by_run_id,
            "created_at": target.created_at,
        }
        if target.status in ("seed", "approved"):
            active = scope["active"]
            assert isinstance(active, list)
            active.append(item)
        elif target.status == "discovered":
            discovered = scope["discovered"]
            assert isinstance(discovered, list)
            discovered.append(item)
        elif target.status == "rejected":
            rejected = scope["rejected"]
            assert isinstance(rejected, list)
            rejected.append(item)
    return scope


def _target_scope_snapshot_context() -> dict[str, object]:
    return {
        "version": "target_scope_policy_v1",
        "source": "server_targets_snapshot",
        "network_plane": "target",
        "active_statuses": ["seed", "approved"],
        "review_statuses": ["discovered"],
        "blocked_statuses": ["rejected"],
    }

__all__ = [name for name in globals() if not name.startswith("__")]
