"""项目状态表达控制意图；本模块只保存精确执行归属和物理控制回执。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from fastapi import HTTPException

from cairn.server.db import get_conn

from .assessment_policy import parse_scenario_snapshot, row_value
from .ids import get_project_or_404
from .time_utils import parse_utc_timestamp, utcnow


_RESEARCH_TYPES = {"bootstrap", "reason", "explore"}
_CONFIRMED = {"pause": "paused", "resume": "running", "finish": "stopped"}
_SCENE_LOSS_REASONS = {
    "run_generation_mismatch", "run_connection_missing",
    "activation_missing", "activation_stopped", "activation_identity_mismatch",
    "activation_owner_mismatch", "activation_generation_mismatch", "docker_daemon_mismatch",
    "physical_scene_unverified",
}


def execution_control(project) -> dict:
    return parse_scenario_snapshot(row_value(project, "execution_control_json"))


def is_resume_retry(project) -> bool:
    control = execution_control(project)
    return (
        project["status"] == "active"
        and control.get("action") == "resume"
        and control.get("state") in {"failed", "lost"}
    )


def _write(conn, project_id: str, control: dict) -> None:
    conn.execute("UPDATE projects SET execution_control_json = ? WHERE id = ?", (
        json.dumps(control, ensure_ascii=False, separators=(",", ":")), project_id,
    ))


def research_execution_owners(conn, project) -> tuple[set[str], bool]:
    owners: set[str] = set()
    unknown = False
    for run in conn.execute("SELECT * FROM task_runs WHERE project_id = ?", (project["id"],)):
        if run["task_type"] not in _RESEARCH_TYPES:
            continue
        context = parse_scenario_snapshot(run["runtime_context_json"])
        lifecycle = context.get("execution_lifecycle", {})
        if (
            run["status"] not in {"queued", "running"}
            and isinstance(lifecycle, dict)
            and lifecycle.get("generation") == run["execution_generation"]
            and lifecycle.get("termination_confirmed") is True
            and lifecycle.get("cleanup_confirmed") is True
        ):
            continue
        owner = context.get("dispatcher_id")
        if isinstance(owner, str) and owner:
            owners.add(owner)
        else:
            unknown = True
    # readiness 也可能已启动项目控制容器，不能仅凭没有 TaskRun 推定没有物理现场。
    for activation in conn.execute(
        "SELECT * FROM project_container_activations WHERE project_id = ? AND state != 'stopped'",
        (project["id"],),
    ):
        if activation["state"] == "inventory":
            unknown = True
        else:
            owners.add(activation["dispatcher_id"])
    for scope in conn.execute(
        "SELECT s.* FROM http_capture_scopes s JOIN task_runs r ON r.project_id = s.project_id AND r.id = s.task_run_id "
        "WHERE s.project_id = ? AND r.task_type IN ('bootstrap', 'reason', 'explore')",
        (project["id"],),
    ):
        if parse_scenario_snapshot(scope["status_json"]).get("test_stopped") is not True:
            owners.add(scope["dispatcher_id"])
    return owners, unknown


def request_execution_control(conn, project, action: str, *, now: str | None = None) -> dict:
    old = execution_control(project)
    owners, unknown = research_execution_owners(conn, project)
    if action in {"resume", "finish"}:
        owners.update(old.get("dispatcher_ids", []))
    now = now or utcnow()
    control = {
        "request_id": uuid4().hex,
        "action": action,
        "state": "requested",
        "requested_at": now,
        "confirmed_at": None,
        "dispatcher_ids": sorted(owners),
        "dispatcher_id": next(iter(owners)) if len(owners) == 1 else None,
        "resources": old.get("resources", []),
        "resources_request_id": old.get("resources_request_id") or old.get("request_id"),
        "resources_observed_at": old.get("resources_observed_at") or old.get("confirmed_at"),
        "receipts": {},
        "error": "project_execution_owner_unknown" if unknown else None,
        "owner_unknown": unknown,
        "paused_at": old.get("paused_at"),
        "paused_seconds": old.get("paused_seconds", 0),
    }
    if old.get("paused_at_source"):
        control["paused_at_source"] = old["paused_at_source"]
    if action == "pause" and not old.get("paused_at"):
        control["paused_at"] = now
        control.pop("paused_at_source", None)
    if not owners and not unknown:
        # 没有执行或容器时只改变派发，不宣称存在可恢复进程。
        control.update(
            state=_CONFIRMED[action], confirmed_at=now, resources=[],
            resources_request_id=control["request_id"], resources_observed_at=now,
        )
        if action == "resume":
            _finish_pause_clock(conn, project, control, now)
            _renew_leases(conn, project["id"])
        elif action == "finish":
            _finish_pause_clock(conn, project, control, now)
            _close_research_claims(conn, project["id"], now)
    _write(conn, project["id"], control)
    return control


def _finish_pause_clock(conn, project, control: dict, now: str) -> None:
    start = parse_utc_timestamp(control.get("paused_at"))
    end = parse_utc_timestamp(now)
    if start is None or end is None:
        return
    seconds = max(0.0, (end - start).total_seconds())
    control["paused_seconds"] = float(control.get("paused_seconds", 0)) + seconds
    control["paused_at"] = None
    control.pop("paused_at_source", None)
    deadline = parse_utc_timestamp(project["explore_deadline"])
    if deadline is not None:
        conn.execute("UPDATE projects SET explore_deadline = ? WHERE id = ?", (
            (deadline + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"), project["id"],
        ))


def _renew_leases(conn, project_id: str) -> None:
    from .projects import lease_clock

    boot_id, now_ns = lease_clock()
    now = utcnow()
    conn.execute(
        "UPDATE projects SET reason_last_heartbeat_at = ?, reason_heartbeat_boot_id = ?, "
        "reason_heartbeat_boottime_ns = ? WHERE id = ? AND reason_worker IS NOT NULL",
        (now, boot_id, now_ns, project_id),
    )
    conn.execute(
        "UPDATE intents SET last_heartbeat_at = ?, heartbeat_boot_id = ?, heartbeat_boottime_ns = ? "
        "WHERE project_id = ? AND worker IS NOT NULL AND concluded_at IS NULL",
        (now, boot_id, now_ns, project_id),
    )


def _close_research_claims(conn, project_id: str, now: str) -> None:
    from .projects import clear_open_intent_workers_for_project, clear_project_reason

    # 物理停止后才释放研究租约；报告、聊天的独立 claim 不在这里处理。
    clear_open_intent_workers_for_project(conn, project_id, "project_stopped", cleared_at=now)
    clear_project_reason(conn, project_id)
    project = get_project_or_404(conn, project_id)
    conn.execute(
        "UPDATE intents SET concluded_at = ?, conclusion = 'abandoned', conclusion_reason = ? "
        "WHERE project_id = ? AND concluded_at IS NULL AND to_fact_id IS NULL",
        (now, project["coverage_stop_reason"] or "user_stopped", project_id),
    )


def _validate_physical_resources(conn, project_id: str, dispatcher_id: str, state: str, resources: list[dict]) -> None:
    known: set[str] = set()
    required: set[str] = set()
    unidentified_execution = False
    for run in conn.execute(
        "SELECT * FROM task_runs WHERE project_id = ? AND task_type IN ('bootstrap', 'reason', 'explore')",
        (project_id,),
    ):
        context = parse_scenario_snapshot(run["runtime_context_json"])
        if context.get("dispatcher_id") != dispatcher_id:
            continue
        identities = {value for value in context.get("execution_container_ids", []) if isinstance(value, str) and value not in {"", "unknown"}}
        delivery = context.get("execution_delivery")
        for value in (context.get("container_id"), delivery.get("container_id") if isinstance(delivery, dict) else None):
            if isinstance(value, str) and value not in {"", "unknown"}:
                identities.add(value)
        known.update(identities)
        lifecycle = context.get("execution_lifecycle", {})
        closed = isinstance(lifecycle, dict) and lifecycle.get("generation") == run["execution_generation"] and lifecycle.get("termination_confirmed") is True and lifecycle.get("cleanup_confirmed") is True and run["status"] not in {"queued", "running"}
        if not closed:
            required.update(identities)
            unidentified_execution |= not identities and (not isinstance(lifecycle, dict) or lifecycle.get("process_started") is not False)
    activations = conn.execute(
        "SELECT container_id FROM project_container_activations WHERE project_id = ? AND dispatcher_id = ? AND state != 'stopped'",
        (project_id, dispatcher_id),
    ).fetchall()
    activation_ids = {row["container_id"] for row in activations if row["container_id"]}
    known.update(activation_ids)
    required.update(activation_ids)
    scopes = {row["id"]: row for row in conn.execute(
        "SELECT s.* FROM http_capture_scopes s JOIN task_runs r ON r.project_id = s.project_id AND r.id = s.task_run_id "
        "WHERE s.project_id = ? AND s.dispatcher_id = ? AND r.task_type IN ('bootstrap', 'reason', 'explore')",
        (project_id, dispatcher_id),
    )}
    required_scopes = {scope_id for scope_id, scope in scopes.items() if parse_scenario_snapshot(scope["status_json"]).get("test_stopped") is not True}
    confirmed: set[str] = set()
    confirmed_scopes: set[str] = set()
    for resource in resources:
        identity = resource.get("container_id")
        physical = resource.get("state")
        # 暂停也可确认已登记但尚未启动的惰性资源，必须保留实际 Docker 状态。
        # 恢复不接受这些状态；原 owner 放行启动并核验 running 后才确认恢复。
        inert = state == "paused" and (
            resource.get("kind") == "research" and physical in {"created", "exited"}
            or resource.get("kind") == "test" and physical == "created"
        )
        if not identity or (physical != state and not inert):
            raise HTTPException(422, "execution_control_resource_invalid")
        if resource.get("kind") == "research":
            if identity not in known:
                raise HTTPException(422, "execution_control_resource_invalid")
        elif resource.get("kind") == "test":
            scope = scopes.get(resource.get("scope_id"))
            if scope is None or scope["container_id"] != identity or scope["task_run_id"] != resource.get("task_run_id"):
                raise HTTPException(422, "execution_control_resource_invalid")
            confirmed_scopes.add(scope["id"])
        else:
            raise HTTPException(422, "execution_control_resource_invalid")
        confirmed.add(identity)
    # 认证 owner 不能用空列表或部分容器冒充整个研究域的物理成功。
    # 缺失的执行身份需由原 owner 先登记；不能从任意上报的容器猜测归属。
    if any(not row["container_id"] for row in activations) or (unidentified_execution and not activation_ids) or required - confirmed or required_scopes - confirmed_scopes:
        raise HTTPException(409, "execution_control_unconfirmed")


def confirm_execution_control(
    project_id: str, dispatcher_id: str, *, request_id: str, state: str,
    resources: list[dict], error: str | None = None,
    scene_loss_reason: str | None = None, task_run_id: str | None = None,
) -> dict:
    with get_conn() as conn:
        conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
        project = get_project_or_404(conn, project_id)
        control = execution_control(project)
        if control.get("request_id") != request_id:
            raise HTTPException(409, "execution_control_request_stale")
        if dispatcher_id not in control.get("dispatcher_ids", []):
            raise HTTPException(403, "execution_owner_mismatch")
        expected = _CONFIRMED[control["action"]]
        if state not in {expected, "failed", "lost"}:
            raise HTTPException(422, "execution_control_confirmation_invalid")
        # 诊断不改变物理回执；只持久化固定原因与原 owner 在本项目中的研究 Run。
        diagnostic = {}
        if state == "lost" and scene_loss_reason in _SCENE_LOSS_REASONS:
            diagnostic["scene_loss_reason"] = scene_loss_reason
            if task_run_id is not None:
                run = conn.execute(
                    "SELECT id, task_type, runtime_context_json FROM task_runs WHERE project_id = ? AND id = ?",
                    (project_id, task_run_id),
                ).fetchone()
                if run is None or run["task_type"] not in _RESEARCH_TYPES or parse_scenario_snapshot(
                    run["runtime_context_json"]
                ).get("dispatcher_id") != dispatcher_id:
                    raise HTTPException(422, "execution_control_task_run_invalid")
                diagnostic["task_run_id"] = run["id"]
        allowed = {"container_id", "container_name", "kind", "task_run_id", "exec_id", "scope_id", "state"}
        for resource in resources:
            if not isinstance(resource, dict) or set(resource) - allowed or not all(
                isinstance(value, str) for value in resource.values()
            ):
                raise HTTPException(422, "execution_control_resource_invalid")
            if resource.get("task_run_id"):
                run = conn.execute(
                    "SELECT task_type, runtime_context_json FROM task_runs WHERE project_id = ? AND id = ?",
                    (project_id, resource["task_run_id"]),
                ).fetchone()
                if run is None or run["task_type"] not in _RESEARCH_TYPES or parse_scenario_snapshot(
                    run["runtime_context_json"]
                ).get("dispatcher_id") != dispatcher_id:
                    raise HTTPException(422, "execution_control_resource_invalid")
        # 回执只能确认本次请求；资源身份由认证 owner 上报，不能修改执行代次和任务业务状态。
        prior = control.get("receipts", {}).get(dispatcher_id)
        if prior and prior.get("state") == "lost" and state != "lost":
            raise HTTPException(409, "execution_scene_lost")
        if prior and prior.get("state") == expected:
            if state != expected or prior.get("resources") != resources:
                raise HTTPException(409, "execution_control_confirmation_conflict")
            return control
        if state == expected:
            _validate_physical_resources(conn, project_id, dispatcher_id, state, resources)
            if state == "stopped":
                conn.execute(
                    "UPDATE project_container_activations SET state = 'stopped', updated_at = ? "
                    "WHERE project_id = ? AND dispatcher_id = ? AND state != 'inventory'",
                    (utcnow(), project_id, dispatcher_id),
                )
        # 公开投影只保存固定错误码，不能把 owner 原始异常或私有控制凭据写入项目材料。
        safe_errors = {
            "project_execution_owner_unknown", "execution_control_failed", "execution_control_unconfirmed",
            "execution_scene_lost", "docker_daemon_unavailable", "docker_daemon_changed",
            "container_identity_mismatch", "container_ownership_mismatch", "execution_control_invalid",
        }
        if state in {"failed", "lost"}:
            error = error if error in safe_errors else (
                "execution_scene_lost" if state == "lost" else "execution_control_failed"
            )
        else:
            error = None
        now = utcnow()
        control.setdefault("receipts", {})[dispatcher_id] = {
            "state": state, "resources": resources, "error": error, "confirmed_at": now,
            **diagnostic,
        }
        receipts = control["receipts"]
        # 当次逐 owner 回执留在 receipts；失败或部分回执不能抹掉上次完整资源观测。
        if any(receipt["state"] in {"failed", "lost"} for receipt in receipts.values()):
            control["state"] = "lost" if any(receipt["state"] == "lost" for receipt in receipts.values()) else "failed"
            control["error"] = next(
                receipt["error"] for receipt in receipts.values() if receipt["state"] in {"failed", "lost"}
            )
        elif not control.get("owner_unknown") and all(
            receipts.get(owner, {}).get("state") == expected for owner in control["dispatcher_ids"]
        ):
            control.update(
                state=expected, confirmed_at=now, error=None,
                resources=[item for receipt in receipts.values() for item in receipt["resources"]],
                resources_request_id=request_id, resources_observed_at=now,
            )
            if control["action"] == "resume":
                _finish_pause_clock(conn, project, control, now)
                _renew_leases(conn, project_id)
            elif control["action"] == "finish":
                _finish_pause_clock(conn, project, control, now)
                _close_research_claims(conn, project_id, now)
        else:
            control.update(
                state="requested", confirmed_at=None,
                error="project_execution_owner_unknown" if control.get("owner_unknown") else None,
            )
        _write(conn, project_id, control)
        return control


def execution_dispatch_blocked(project) -> bool:
    control = execution_control(project)
    return bool(control.get("action") and control.get("state") != "running")


def effective_explore_deadline(project, deadline: str | None) -> str | None:
    """执行预算扣暂停；授权期限从独立字段读取，绝不平移。"""
    start = parse_utc_timestamp(execution_control(project).get("paused_at"))
    end = parse_utc_timestamp(deadline)
    if start is None or end is None:
        return deadline
    elapsed = max(timedelta(), datetime.now(timezone.utc) - start)
    return (end + elapsed).strftime("%Y-%m-%dT%H:%M:%SZ")
