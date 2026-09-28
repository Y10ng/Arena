"""命令审计事件记录器。"""

from __future__ import annotations

from .common_base import *

def record_command_started(
    client: CairnClient | None,
    project_id: str | None,
    task_run_id: str | None,
    *,
    phase: str,
    worker: WorkerConfig,
    argv: list[str],
) -> None:
    # started 事件先记录 unknown 状态，finished 事件再补退出码和耗时。
    _record_command_event(
        client,
        project_id,
        task_run_id,
        kind="command_started",
        payload={
            "phase": phase,
            "worker_name": worker.name,
            "worker_type": worker.type,
            "command_kind": _command_kind(argv, phase),
            "returncode": None,
            "exit_code": None,
            "timed_out": False,
            "cancelled": False,
            "duration_seconds": 0.0,
            "status": "unknown",
        },
    )


def record_command_finished(
    client: CairnClient | None,
    project_id: str | None,
    task_run_id: str | None,
    *,
    phase: str,
    worker: WorkerConfig,
    argv: list[str],
    result: ProcessResult | None,
    duration_seconds: float,
    status: str | None = None,
) -> None:
    returncode = result.returncode if result is not None else None
    timed_out = bool(did_timeout(result)) if result is not None else False
    # 统一把 Docker/Python 侧取消、超时和 returncode 映射到监控可聚合字段。
    event_payload: dict[str, object] = {
        "phase": phase,
        "worker_name": worker.name,
        "worker_type": worker.type,
        "command_kind": _command_kind(argv, phase),
        "returncode": returncode,
        "exit_code": returncode,
        "timed_out": timed_out,
        "cancelled": bool(result.cancelled) if result is not None else False,
        "oom_killed": bool(result.oom_killed) if result is not None else False,
        "duration_seconds": round(max(duration_seconds, 0.0), 3),
        "status": _normalize_command_status(status or _command_status(result)),
    }
    if result is not None and result.cancel_reason:
        event_payload["cancel_reason"] = result.cancel_reason
    if result is not None and result.oom_evidence_source:
        event_payload["oom_evidence_source"] = result.oom_evidence_source
    _record_command_event(
        client,
        project_id,
        task_run_id,
        kind="command_finished",
        payload=event_payload,
    )


def _record_command_event(
    client: CairnClient | None,
    project_id: str | None,
    task_run_id: str | None,
    *,
    kind: str,
    payload: dict[str, object],
) -> None:
    if client is None or project_id is None or task_run_id is None:
        return
    try:
        response = client.create_agent_event(
            project_id,
            task_run_id=task_run_id,
            kind=kind,
            payload=payload,
        )
    except Exception as exc:
        # command event 是审计副产物，写入失败只告警，不能影响 worker 主流程。
        LOG.warning(
            "command event create raised project=%s task_run=%s kind=%s phase=%s error=%s",
            project_id,
            task_run_id,
            kind,
            payload.get("phase"),
            exc,
        )
        return
    if response.ok:
        return
    LOG.warning(
        "command event create failed project=%s task_run=%s kind=%s phase=%s status=%s body=%s",
        project_id,
        task_run_id,
        kind,
        payload.get("phase"),
        response.status_code,
        response.text,
    )


def _command_kind(argv: list[str], phase: str) -> str:
    # command_kind 用于聚合展示，不记录完整 argv，降低 token/API key 泄露风险。
    if phase.endswith("_healthcheck") or phase == "healthcheck":
        return "healthcheck"
    if phase.endswith("_conclude"):
        return "conclude"
    if phase.endswith("_execute") or phase == "bootstrap":
        return "execute"
    if not argv:
        return "unknown"
    executable = argv[0].rsplit("/", 1)[-1].strip()
    if not executable:
        return "unknown"
    return executable[:64]


def _command_status(result: ProcessResult | None) -> str:
    if result is None:
        return "unknown"
    if result.cancelled or did_timeout(result):
        return "failed"
    if result.returncode == 0:
        return "success"
    return "failed"


def _normalize_command_status(status: str) -> str:
    if status in {"success", "failed", "unknown"}:
        return status
    return "unknown"
