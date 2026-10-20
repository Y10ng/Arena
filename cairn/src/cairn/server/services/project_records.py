"""项目 router 使用的项目记录服务。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import shutil
from typing import Literal

from pydantic import Field

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL
from cairn.server.db import PostgresConnection, artifact_root, get_conn, schedule_transaction
from cairn.server.models import (
    CreateProjectRequest,
    Fact,
    Hint,
    ProjectDetail,
    ProjectLibraryBinding,
    ProjectMeta,
    ProjectSummary,
    Target,
    UpdateProjectStatusRequest,
    UpdateProjectTitleRequest,
    UpdateProjectWorkersRequest,
)

from .assessment_quality_snapshot import build_assessment_quality_snapshot
from .assessment_policy import (
    parse_scenario_snapshot,
    policy_for_scenario,
    policy_from_project_row,
    row_value,
    scenario_snapshot,
)
from .common import *
from .ids import get_project_or_404, next_hint_id, next_project_id, next_target_id
from .libraries import (
    bind_library_revision,
    get_library,
    get_library_version,
    list_library_bindings,
    resolve_current_library_revisions,
)
from .projects import (
    build_intents,
    check_project_active,
    clear_open_intent_worker,
    clear_open_intent_workers_for_project,
    clear_project_reason,
    expire_reason_leases,
    expire_workers,
    project_meta_from_row,
    project_reason_from_row,
)
from .projects import _parse_preferred_workers
from .project_resources import (
    consume_project_resource_batch,
    create_initial_project_resource_records,
    delete_project_resource_directory,
    list_project_resource_records,
)
from .scenarios import get_scenario_or_404, resolve_scenario_key
from .tenant_workers import project_worker_bindings, validate_tenant_worker_selection
from .targets import (
    build_targets,
    normalize_target_value,
    validate_target_not_duplicate,
)
from .time_utils import parse_utc_timestamp, utcnow


@dataclass(frozen=True)
class _AuthorizationMeta:
    scope_digest: str | None
    scope_descriptor: dict | None
    window_seconds: int | None


def _authorization_meta(conn, project) -> _AuthorizationMeta:
    from .project_authorization import authorization_scope_projection, project_authorization_scope_descriptor

    descriptor = project_authorization_scope_descriptor(conn, project)
    policy = policy_from_project_row(project)
    if not policy.goal_driven:
        return _AuthorizationMeta(
            scope_digest=row_value(project, "authorization_scope_digest"),
            scope_descriptor=descriptor,
            window_seconds=row_value(project, "authorization_window_seconds"),
        )
    projection = authorization_scope_projection(conn, project)
    return _AuthorizationMeta(
        scope_digest=projection.scope_digest,
        scope_descriptor=descriptor,
        window_seconds=projection.window_seconds,
    )


def list_project_summaries(tenant_id: str | None = None, *, include_verification: bool = True) -> list[ProjectSummary]:
    """列出显式 Tenant 的项目；省略 context 保留内部调用与直接 service 测试。"""
    from .project_authorization import project_authorization_block_reason

    with get_conn() as conn:
        # 读取列表前先过期失联的 worker/reason 租约，保证摘要计数接近实时状态。
        expire_workers(conn)
        expire_reason_leases(conn)
        from .verification_requests import expire_pending_verification_requests
        expire_pending_verification_requests(conn)
        clauses = [] if tenant_id is None else ["p.tenant_id = ?"]
        clauses.append("NOT EXISTS (SELECT 1 FROM verification_requests vr WHERE vr.project_id = p.id AND vr.purge_state IS NOT NULL)")
        if not include_verification:
            clauses.append("NOT EXISTS (SELECT 1 FROM verification_requests vr WHERE vr.project_id = p.id)")
        tenant_clause = "WHERE " + " AND ".join(clauses) if clauses else ""
        params = () if tenant_id is None else (tenant_id,)
        rows = conn.execute(f"""
            SELECT p.*,
                (SELECT t.name FROM tenants t WHERE t.id = p.tenant_id) AS tenant_name,
                (SELECT u.login
                 FROM tenant_memberships tm
                 JOIN users u ON u.id = tm.user_id
                 WHERE tm.tenant_id = p.tenant_id AND tm.role = 'owner'
                 ORDER BY tm.created_at, u.id
                 LIMIT 1) AS tenant_owner_login,
                (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id) AS intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NOT NULL) AS working_intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NULL) AS unclaimed_intent_count,
                (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count
            FROM projects p
            {tenant_clause}
            ORDER BY p.created_at
        """, params).fetchall()
        return [
            ProjectSummary(
                id=row["id"],
                title=row["title"],
                status=row["status"],
                execution_generation=int(row_value(row, "execution_generation") or 0),
                authorization_epoch=int(row_value(row, "authorization_epoch") or 0),
                authorization_scope_digest=(authorization := _authorization_meta(conn, row)).scope_digest,
                authorization_scope_descriptor=authorization.scope_descriptor,
                authorization_window_seconds=authorization.window_seconds,
                authorization_block_reason=project_authorization_block_reason(conn, row),
                created_at=row["created_at"],
                scenario_key=row["scenario_key"],
                scenario_snapshot=parse_scenario_snapshot(
                    row_value(row, "scenario_snapshot_json")
                ),
                assessment_protocol=str(row_value(row, "assessment_protocol") or ""),
                assessment_progress=parse_scenario_snapshot(row_value(row, "assessment_progress_json")) or None,
                assessment_execution=(meta := project_meta_from_row(conn, row)).assessment_execution,
                schedule_id=meta.schedule_id,
                verification_request_id=meta.verification_request_id,
                verification_attempts_started=int(row_value(row, "verification_attempts_started") or 0),
                explore_deadline=row_value(row, "explore_deadline"),
                authorization_expires_at=row_value(
                    row, "authorization_expires_at"
                ),
                explore_attempts_started=int(
                    row_value(row, "explore_attempts_started") or 0
                ),
                coverage_checkpoint_cell_count=int(
                    row_value(row, "coverage_checkpoint_cell_count") or 0
                ),
                coverage_checkpoint_required_count=int(
                    row_value(row, "coverage_checkpoint_required_count") or 0
                ),
                coverage_checkpoint_optional_count=int(
                    row_value(row, "coverage_checkpoint_optional_count") or 0
                ),
                coverage_checkpoint_required_terminal_count=int(
                    row_value(row, "coverage_checkpoint_required_terminal_count") or 0
                ),
                coverage_stop_reason=row_value(row, "coverage_stop_reason"),
                tenant_workers=project_worker_bindings(conn, row["id"]),
                preferred_workers=_parse_preferred_workers(row["preferred_workers_json"]),
                tenant_id=row["tenant_id"],
                tenant_name=row["tenant_name"],
                tenant_owner_login=row["tenant_owner_login"],
                reason=project_reason_from_row(row),
                fact_count=row["fact_count"],
                intent_count=row["intent_count"],
                working_intent_count=row["working_intent_count"],
                unclaimed_intent_count=row["unclaimed_intent_count"],
                hint_count=row["hint_count"],
            )
            for row in rows
        ]


class _VerificationProjectRequest(CreateProjectRequest):
    """内部复测固定场景与专用 Worker，独立于普通项目创建入口。"""

    scenario: Literal["goal-driven-v1"] = "goal-driven-v1"
    tenant_worker_ids: list[str] = Field(min_length=1, max_length=1)
    goal: str = "根据来源背景与所提供资料验证漏洞主张，保留实际观察、证据与限制，遵守来源已明确的范围和排除项，不开展无关探索。"

def create_verification_project_record(
    conn,
    *,
    title: str,
    origin: str,
    tenant_id: str,
    tenant_worker_id: str,
    creator: str,
    actor_type: str,
    execution_deadline: str | None,
    authorization_expires_at: str | None,
) -> ProjectDetail:
    body = _VerificationProjectRequest(
        title=title, origin=origin, tenant_worker_ids=[tenant_worker_id],
        explore_deadline=execution_deadline, authorization_expires_at=authorization_expires_at,
    )
    if not tenant_id:
        raise ValueError("tenant_id is required")
    return _create_project_record(
        conn, body, tenant_id=tenant_id, creator=creator, initial_status="pending",
        library_revisions=None, require_all_roles=False, actor_type=actor_type, worker_purpose="verification",
    )


def create_project_record(
    body: CreateProjectRequest,
    conn: sqlite3.Connection | None = None,
    *,
    tenant_id: str,
    creator: str = "system",
    initial_status: str = "active",
    library_revisions: list | None = None,
    require_all_roles: bool = True,
    actor_type: str = "user",
) -> ProjectDetail:
    """普通项目与计划使用同租户的普通用途 Worker。"""
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if initial_status not in {"active", "pending"}:
        raise ValueError("invalid initial project status")
    if conn is not None:
        return _create_project_record(conn, body, tenant_id=tenant_id, creator=creator, initial_status=initial_status, library_revisions=library_revisions, require_all_roles=require_all_roles, actor_type=actor_type)
    with get_conn() as owned_conn:
        return _create_project_record(owned_conn, body, tenant_id=tenant_id, creator=creator, initial_status=initial_status, library_revisions=library_revisions, require_all_roles=require_all_roles, actor_type=actor_type)


def _create_project_record(
    conn: sqlite3.Connection,
    body: CreateProjectRequest,
    *,
    tenant_id: str,
    creator: str,
    initial_status: str,
    library_revisions: list | None,
    require_all_roles: bool,
    actor_type: str,
    worker_purpose: str = "general",
) -> ProjectDetail:
    now = utcnow()
    scenario_key = resolve_scenario_key(conn, body.scenario)
    scenario = get_scenario_or_404(conn, scenario_key)
    snapshot = scenario_snapshot(scenario)
    current = parse_utc_timestamp(now)
    for field_name, value in (
        ("explore_deadline", body.explore_deadline),
        ("authorization_expires_at", body.authorization_expires_at),
    ):
        parsed = parse_utc_timestamp(value) if value is not None else None
        if parsed is not None and current is not None and parsed <= current:
            raise HTTPException(422, f"{field_name}_not_future")
    policy = policy_for_scenario(
        scenario,
        created_at=now,
        explore_deadline=body.explore_deadline,
    )
    selected_workers = validate_tenant_worker_selection(
        conn,
        tenant_id=tenant_id,
        worker_ids=body.tenant_worker_ids,
        require_all_roles=require_all_roles,
        purpose=worker_purpose,
    )
    for library_key in body.library_keys:
        get_library(conn, library_key)
    if library_revisions is None:
        library_revisions = resolve_current_library_revisions(conn, body.library_keys)
    pid = next_project_id(conn)

    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, scenario_key,
            scenario_snapshot_json, assessment_protocol,
            vulnerability_fingerprint_version, explore_deadline,
            authorization_expires_at, preferred_workers_json, tenant_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pid,
            body.title,
            initial_status,
            now,
            scenario_key,
            dump_json(snapshot),
            policy.protocol,
            policy.fingerprint_version,
            policy.explore_deadline,
            body.authorization_expires_at,
            "[]",
            tenant_id,
        ),
    )
    if policy.phased:
        conn.execute(
            "UPDATE projects SET assessment_progress_json = ? WHERE id = ?",
            (dump_json({"version": 1, "phase": "mapping", "phase_started_at": now, "depth_review_fact_id": None, "accepted_input_digest": None}), pid),
        )
    for position, worker in enumerate(selected_workers):
        conn.execute(
            """
            INSERT INTO project_tenant_workers
                (project_id, tenant_id, tenant_worker_id, tenant_worker_revision, position, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (pid, tenant_id, worker["id"], worker["revision"], position, now),
        )
    for revision in library_revisions:
        bind_library_revision(conn, pid, revision)
    conn.execute(
        "INSERT INTO facts (id, project_id, description, task_run_id, creator, created_at) VALUES (?, ?, ?, NULL, 'system', ?)",
        ("origin", pid, body.origin, now),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description, task_run_id, creator, created_at) VALUES (?, ?, ?, NULL, 'system', ?)",
        ("goal", pid, body.goal, now),
    )

    # origin/goal 是每个项目的固定根 fact；hints 与 seed targets 是可选的人工输入。
    hints = _insert_project_hints(conn, pid, body, now, creator=creator)
    targets = _insert_project_seed_targets(conn, pid, body, now)
    if policy.goal_driven:
        from .project_authorization import initialize_project_authorization

        initialize_project_authorization(
            conn,
            pid,
            actor_id=creator,
            actor_type=actor_type,
            authorized_at=now,
            origin_fact_id="origin" if body.targets is None else None,
        )
    if body.resource_batch_id is not None:
        resources = consume_project_resource_batch(
            conn,
            pid,
            body.resource_batch_id,
            tenant_id=tenant_id,
            user_id=creator,
            created_by=creator,
            require_stopped=False,
        )
    else:
        resources = create_initial_project_resource_records(
            conn,
            pid,
            body.resources or [],
            created_by=creator,
        )
    return ProjectDetail(
        project=ProjectMeta.model_validate({
            **project_meta_from_row(
                conn,
                conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone(),
            ).model_dump(),
            "tenant_workers": project_worker_bindings(conn, pid),
        }),
        facts=[
            Fact(id="origin", description=body.origin, creator="system", created_at=now),
            Fact(id="goal", description=body.goal, creator="system", created_at=now),
        ],
        intents=[],
        hints=hints,
        targets=targets,
        resources=resources,
        library_bindings=_project_library_bindings(conn, pid),
    )


def get_project_detail(project_id: str, *, tenant_id: str | None = None) -> ProjectDetail:
    with get_conn() as conn:
        expire_workers(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id, tenant_id=tenant_id)

        facts = conn.execute(
            "SELECT * FROM facts WHERE project_id = ?", (project_id,)
        ).fetchall()
        hints = conn.execute(
            "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()

        quality_snapshot = build_assessment_quality_snapshot(conn, project_id)
        return ProjectDetail(
            project=ProjectMeta.model_validate({
                **project_meta_from_row(conn, row).model_dump(),
                "tenant_workers": project_worker_bindings(conn, project_id),
                "assessment_quality": (
                    asdict(quality_snapshot) if quality_snapshot is not None else None
                ),
            }),
            facts=[Fact(**dict(f), assessment_record=parse_scenario_snapshot(row_value(f, "assessment_record_json")) or None) for f in facts],
            intents=build_intents(conn, project_id),
            hints=[Hint(**dict(h)) for h in hints],
            targets=build_targets(conn, project_id),
            resources=list_project_resource_records(project_id, conn=conn),
            library_bindings=_project_library_bindings(conn, project_id),
        )


def _project_library_bindings(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[ProjectLibraryBinding]:
    result: list[ProjectLibraryBinding] = []
    for binding in list_library_bindings(conn, project_id):
        library = get_library(conn, binding.library_key, include_archived=True)
        revision = get_library_version(conn, binding.library_key, binding.version)
        result.append(
            ProjectLibraryBinding(
                library_key=library.key,
                library_name=library.name,
                version=revision.version,
                digest_sha256=revision.digest_sha256,
                file_count=revision.file_count,
            )
        )
    return result


def require_unmanaged_project(conn, project_id: str) -> None:
    linked = conn.execute("SELECT id FROM verification_requests WHERE project_id = ?", (project_id,)).fetchone()
    if linked is not None:
        raise HTTPException(409, f"verification_request_managed:{linked['id']}")


def delete_project_record(project_id: str) -> None:
    with schedule_transaction() as conn:
        require_unmanaged_project(conn, project_id)
        # 与计划消费保持“计划→项目”锁序；历史关联保留，只撤销尚未触发的预约。
        schedule_sql = (
            "SELECT s.* FROM project_schedules s "
            "WHERE s.project_id = ? OR s.last_project_id = ? OR EXISTS ("
            "SELECT 1 FROM project_schedule_occurrences o "
            "WHERE o.schedule_id = s.id AND o.project_id = ?) ORDER BY s.id"
        )
        if isinstance(conn, PostgresConnection):
            schedule_sql += " FOR UPDATE"
        schedules = conn.execute(schedule_sql, (project_id, project_id, project_id)).fetchall()
        conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
        get_project_or_404(conn, project_id)
        now = utcnow()
        for schedule in schedules:
            if (
                schedule["mode"] == "once"
                and schedule["project_id"] == project_id
                and schedule["generated_count"] == 0
            ):
                conn.execute(
                    "UPDATE project_schedules SET status = 'cancelled', next_run_at = NULL, "
                    "block_reason = NULL, version = version + 1, updated_at = ? WHERE id = ?",
                    (now, schedule["id"]),
                )
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    # DB 行已级联删除；磁盘上的 artifact 文件不依赖 LLM 总结，需单独清理，避免删项目后残留。
    # 清理失败不阻断删除流程（DB 已删），best-effort 静默忽略。
    target = artifact_root() / "projects" / project_id
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    delete_project_resource_directory(project_id)


def update_project_title_record(project_id: str, body: UpdateProjectTitleRequest) -> ProjectMeta:
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE projects SET title = ? WHERE id = ?",
            (body.title, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(conn, updated)


def update_project_workers_record(
    project_id: str,
    body: UpdateProjectWorkersRequest,
    *,
    tenant_id: str,
) -> ProjectMeta:
    """显式重绑项目 Worker，并刷新绑定 revision 快照。"""
    with get_conn() as conn:
        row = get_project_or_404(conn, project_id, tenant_id=tenant_id)
        require_unmanaged_project(conn, project_id)
        selected_workers = validate_tenant_worker_selection(
            conn,
            tenant_id=tenant_id,
            worker_ids=body.tenant_worker_ids,
        )
        now = utcnow()
        conn.execute(
            "DELETE FROM project_tenant_workers WHERE project_id = ?",
            (project_id,),
        )
        for position, worker in enumerate(selected_workers):
            conn.execute(
                """
                INSERT INTO project_tenant_workers
                    (project_id, tenant_id, tenant_worker_id,
                     tenant_worker_revision, position, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (project_id, tenant_id, worker["id"], worker["revision"], position, now),
            )
        return ProjectMeta.model_validate({
            **project_meta_from_row(conn, row).model_dump(),
            "tenant_workers": project_worker_bindings(conn, project_id),
        })


def project_pause_unconfirmed(conn, project_id: str) -> bool:
    """结束评估也不能绕过已要求暂停的进程和资源终止确认。"""
    for run in conn.execute("SELECT * FROM task_runs WHERE project_id = ? AND pause_requested = 1", (project_id,)):
        lifecycle = parse_scenario_snapshot(run["runtime_context_json"]).get("execution_lifecycle", {})
        if run["status"] in {"queued", "running"} or not isinstance(lifecycle, dict) or not (
            type(lifecycle.get("generation")) is int
            and lifecycle["generation"] == run["execution_generation"]
            and lifecycle.get("termination_confirmed") is True
            and lifecycle.get("cleanup_confirmed") is True
        ):
            return True
    return False


def project_pause_draining(conn, project) -> bool:
    """仅使用持久化确认；租约过期和 TaskRun 终态均不证明进程已退出。"""
    # 历史快照可能缺少 pause_requested；物理归属未退出就仍需等待收尾。
    from .project_container_pause import project_container_pause_draining

    owners, unknown = project_execution_owners(conn, project)
    if owners or unknown or project_container_pause_draining(conn, project):
        return True
    return bool(project["reason_worker"] or conn.execute(
        "SELECT 1 FROM intents WHERE project_id = ? AND concluded_at IS NULL AND worker IS NOT NULL LIMIT 1",
        (project["id"],),
    ).fetchone())


def project_execution_owners(conn, project) -> tuple[set[str], bool]:
    """诊断与恢复共同使用实际 TaskRun owner；不以全局最新心跳推定归属。"""
    owners: set[str] = set()
    unknown = False
    for run in conn.execute("SELECT * FROM task_runs WHERE project_id = ?", (project["id"],)):
        context = parse_scenario_snapshot(run["runtime_context_json"])
        lifecycle = context.get("execution_lifecycle", {})
        confirmed = isinstance(lifecycle, dict) and type(lifecycle.get("generation")) is int and lifecycle["generation"] == run["execution_generation"] and lifecycle.get("termination_confirmed") is True and lifecycle.get("cleanup_confirmed") is True
        # 同代双确认且已终态即退出物理归属，不要求暂停前历史 owner 永久在线。
        if confirmed and run["status"] not in {"queued", "running"}:
            continue
        owner = context.get("dispatcher_id")
        if isinstance(owner, str) and owner:
            owners.add(owner)
        else:
            unknown = True
    return owners, unknown


def _project_pause_capability_reason(conn, project) -> str | None:
    from .infra import DISPATCHER_HEARTBEAT_STALE_SECONDS
    from .time_utils import parse_utc_timestamp

    owners, unknown = project_execution_owners(conn, project)
    if unknown:
        return "project_execution_owner_unknown"
    now = parse_utc_timestamp(utcnow())
    for owner in sorted(owners):
        row = conn.execute("SELECT * FROM dispatcher_status WHERE id = ?", (owner,)).fetchone()
        observed = parse_utc_timestamp(row["updated_at"]) if row else None
        if observed is None or not 0 <= (now - observed).total_seconds() <= DISPATCHER_HEARTBEAT_STALE_SECONDS:
            return "project_dispatcher_unavailable"
        capabilities = parse_scenario_snapshot(row["runtime_json"]).get("runtime_capabilities", [])
        if not isinstance(capabilities, list) or "project_pause_v1" not in capabilities:
            return "project_pause_unsupported"
    return None


def project_ready_dispatcher(conn, *, now: str | None = None):
    """物理清理完成后独立选择就绪实例；容量满是等待，不是历史归属锁。"""
    from .infra import DISPATCHER_HEARTBEAT_STALE_SECONDS
    from .time_utils import parse_utc_timestamp

    current = parse_utc_timestamp(now) if now is not None else datetime.now(timezone.utc)
    reason = "project_dispatcher_unavailable"
    for row in conn.execute("SELECT * FROM dispatcher_status ORDER BY updated_at DESC, id"):
        observed = parse_utc_timestamp(row["updated_at"])
        if row["status"] not in {"idle", "running"} or current is None or observed is None:
            continue
        if not 0 <= (current - observed).total_seconds() <= DISPATCHER_HEARTBEAT_STALE_SECONDS:
            continue
        capabilities = parse_scenario_snapshot(row["runtime_json"]).get("runtime_capabilities", [])
        if isinstance(capabilities, list) and "project_pause_v1" in capabilities:
            return row, None
        reason = "project_pause_unsupported"
    return None, reason


def project_lifecycle_actions(conn, project) -> dict[str, dict]:
    """供 HTTP 动作与只读诊断共用；不得过期租约或推进评估阶段。"""
    from .assessment_policy import policy_from_project_row, require_execution_contract

    from .project_authorization import (
        project_authorization_is_current,
        project_reauthorization_reason,
    )

    if conn.execute("SELECT 1 FROM verification_requests WHERE project_id = ?", (project["id"],)).fetchone():
        return {name: {"allowed": False, "reason": "verification_request_managed"}
                for name in ("pause", "resume", "reauthorize", "reopen", "finish")}
    status = project["status"]
    actions = {
        name: {"allowed": False, "reason": "project_status_invalid"}
        for name in ("pause", "resume", "reauthorize", "reopen", "finish")
    }
    if status == "pending":
        # pending 的激活只能由计划事务完成，普通生命周期动作不得绕过预约。
        for action in actions:
            actions[action]["reason"] = "project_pending_schedule_required"
        reason = project_reauthorization_reason(conn, project)
        actions["reauthorize"] = {"allowed": reason is None, "reason": reason}
        return actions
    if status == "completed":
        from .project_completion import project_reopen_reason

        for name in ("pause", "resume", "reauthorize", "finish"):
            actions[name]["reason"] = "Completed projects cannot change status"
        reopen_reason = project_reopen_reason(conn, project)
        actions["reopen"] = {
            "allowed": reopen_reason is None,
            "reason": reopen_reason,
        }
        return actions
    actions["finish"] = {"allowed": status in {"active", "paused", "stopped"}, "reason": None}
    try:
        require_execution_contract(project)
        contract_reason = None
    except HTTPException as exc:
        contract_reason = str(exc.detail)
    policy = policy_from_project_row(project)
    progress = parse_scenario_snapshot(row_value(project, "assessment_progress_json"))
    frozen = conn.execute("SELECT status FROM assessment_report_finalizations WHERE project_id = ?", (project["id"],)).fetchone()
    finalizing = progress.get("phase") == "finalizing"
    terminal_reason = contract_reason or ("assessment_new_project_required" if status == "stopped" or progress.get("phase") == "completed" else None)
    if frozen and (not finalizing or frozen["status"] == "ready") and terminal_reason is None:
        terminal_reason = "assessment_report_finalization_in_progress"
    # 暂停只收紧执行权限，不依赖心跳、诊断或已损坏的执行契约。
    actions["pause"] = {"allowed": status in {"active", "paused"}, "reason": None if status in {"active", "paused"} else "project_status_invalid"}
    reauthorization_reason = terminal_reason or project_reauthorization_reason(
        conn,
        project,
    )
    actions["reauthorize"] = {
        "allowed": reauthorization_reason is None,
        "reason": reauthorization_reason,
    }
    for action, expected, noop in (("resume", "paused", "active"),):
        reason = terminal_reason
        if reason is None and status == noop:
            actions[action] = {"allowed": True, "reason": None}
            continue
        if reason is None and status != expected:
            reason = "project_status_invalid"
        if reason is None and action == "resume":
            if project_pause_draining(conn, project):
                reason = "project_pause_draining"
            elif (
                (policy.phased or policy.goal_driven)
                and not finalizing
                and not project_authorization_is_current(conn, project)
            ):
                reason = "project_reauthorization_required"
        if reason is None:
            reason = _project_pause_capability_reason(conn, project)
        if reason is None:
            _dispatcher, reason = project_ready_dispatcher(conn)
        actions[action] = {"allowed": reason is None, "reason": reason}
    return actions


def project_lifecycle_action(project_id: str, action: str, *, legacy: bool = False) -> ProjectMeta:
    with get_conn() as conn:
        # 与领取、结果接受及报告冻结共用项目写锁。
        conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
        row = get_project_or_404(conn, project_id)
        require_unmanaged_project(conn, project_id)
        if legacy and action == "resume" and row["status"] == "active":
            return project_meta_from_row(conn, row)
        eligibility = project_lifecycle_actions(conn, row)[action]
        if not eligibility["allowed"]:
            raise HTTPException(409, eligibility["reason"])
        target_status = {"pause": "paused", "resume": "active", "finish": "stopped"}[action]
        if row["status"] == target_status:
            return project_meta_from_row(conn, row)
        if action == "pause":
            conn.execute("UPDATE projects SET status = 'paused', execution_generation = execution_generation + 1 WHERE id = ?", (project_id,))
            # 已终态但未确认清理的执行仍可能保有浏览器/容器，必须纳入 drain。
            draining_intents = set()
            draining_reason = False
            for run in conn.execute("SELECT * FROM task_runs WHERE project_id = ?", (project_id,)).fetchall():
                lifecycle = parse_scenario_snapshot(run["runtime_context_json"]).get("execution_lifecycle", {})
                confirmed = isinstance(lifecycle, dict) and type(lifecycle.get("generation")) is int and lifecycle["generation"] == run["execution_generation"] and lifecycle.get("termination_confirmed") is True and lifecycle.get("cleanup_confirmed") is True
                if run["status"] in {"queued", "running"} or not confirmed:
                    conn.execute("UPDATE task_runs SET pause_requested = 1, pause_interrupted = CASE WHEN status IN ('queued', 'running') THEN 1 ELSE pause_interrupted END WHERE project_id = ? AND id = ?", (project_id, run["id"]))
                    draining_intents.add(run["intent_id"])
                    draining_reason = draining_reason or run["task_type"] == "reason"
            # admission 已被同一项目锁封闭；没有未确认物理执行的领取可安全收口。
            # 历史执行必须全部真实清理，不能凭时间或 worker 名推断当前 claim 安全。
            for claim in conn.execute("SELECT id FROM intents WHERE project_id = ? AND concluded_at IS NULL AND worker IS NOT NULL", (project_id,)).fetchall():
                if claim["id"] not in draining_intents:
                    clear_open_intent_worker(conn, project_id, claim["id"], "released")
            if row["reason_worker"] and not draining_reason:
                clear_project_reason(conn, project_id)
        elif action == "finish" and policy_from_project_row(row).protocol == GOAL_DRIVEN_PROTOCOL:
            from .assessment_convergence import advance_goal_driven_convergence

            if row_value(row, "coverage_stop_reason") == "user_stopped":
                advance_goal_driven_convergence(
                    conn,
                    project_id,
                    reason="user_stopped",
                )
                return project_meta_from_row(
                    conn,
                    get_project_or_404(conn, project_id),
                )
            # paused 已在暂停事务中推进代次；结束时复用该 fence，避免旧运行落后两代而无法结算。
            conn.execute(
                """
                UPDATE projects
                SET status = 'active',
                    execution_generation = CASE
                        WHEN status = 'active' THEN execution_generation + 1
                        ELSE execution_generation
                    END,
                    coverage_stop_reason = 'user_stopped'
                WHERE id = ? AND status IN ('active', 'paused')
                """,
                (project_id,),
            )
            draining_intents = set()
            draining_reason = False
            for run in conn.execute("SELECT * FROM task_runs WHERE project_id = ?", (project_id,)).fetchall():
                lifecycle = parse_scenario_snapshot(run["runtime_context_json"]).get("execution_lifecycle", {})
                confirmed = isinstance(lifecycle, dict) and type(lifecycle.get("generation")) is int and lifecycle["generation"] == run["execution_generation"] and lifecycle.get("termination_confirmed") is True and lifecycle.get("cleanup_confirmed") is True
                if run["status"] in {"queued", "running"} or not confirmed:
                    conn.execute("UPDATE task_runs SET pause_requested = 1, pause_interrupted = CASE WHEN status IN ('queued', 'running') THEN 1 ELSE pause_interrupted END WHERE project_id = ? AND id = ?", (project_id, run["id"]))
                    draining_intents.add(run["intent_id"])
                    draining_reason = draining_reason or run["task_type"] == "reason"
            for claim in conn.execute("SELECT id FROM intents WHERE project_id = ? AND concluded_at IS NULL AND worker IS NOT NULL", (project_id,)).fetchall():
                if claim["id"] not in draining_intents:
                    clear_open_intent_worker(conn, project_id, claim["id"], "user_stopped")
            if row["reason_worker"] and not draining_reason:
                clear_project_reason(conn, project_id)
            # 先冻结新 admission 并请求物理停止；在途结果、候选收据与清理证明仍可结算。
            advance_goal_driven_convergence(
                conn,
                project_id,
                reason="user_stopped",
            )
        else:
            conn.execute("UPDATE projects SET status = ? WHERE id = ?", (target_status, project_id))
            if action == "finish":
                from .assessment_convergence import stop_assessment

                if policy_from_project_row(row).phased:
                    stop_assessment(conn, project_id, "user_stopped")
                else:
                    clear_open_intent_workers_for_project(conn, project_id, "project_stopped")
                clear_project_reason(conn, project_id)
        return project_meta_from_row(conn, get_project_or_404(conn, project_id))


def update_project_status_record(project_id: str, body: UpdateProjectStatusRequest) -> ProjectMeta:
    # 兼容旧 PUT：stopped 永远表示结束，不再承担暂停语义。
    return project_lifecycle_action(project_id, "finish" if body.status == "stopped" else "resume", legacy=True)


def _insert_project_hints(
    conn,
    project_id: str,
    body: CreateProjectRequest,
    now: str,
    *,
    creator: str,
) -> list[Hint]:
    hints: list[Hint] = []
    for hint in body.hints or []:
        hint_id = next_hint_id(conn, project_id)
        conn.execute(
            "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
            (hint_id, project_id, hint.content, creator, now),
        )
        hints.append(Hint(id=hint_id, content=hint.content, creator=creator, created_at=now))
    return hints


def _insert_project_seed_targets(conn, project_id: str, body: CreateProjectRequest, now: str) -> list[Target]:
    targets: list[Target] = []
    for target in body.targets or []:
        normalized_value = normalize_target_value(target.type, target.value)
        validate_target_not_duplicate(conn, project_id, target.type, normalized_value)
        target_id = next_target_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO targets (id, project_id, value, type, status, source_fact_id, discovered_by_run_id, created_at)
            VALUES (?, ?, ?, ?, 'seed', NULL, NULL, ?)
            """,
            (target_id, project_id, normalized_value, target.type, now),
        )
        targets.append(
            Target(
                id=target_id,
                value=normalized_value,
                type=target.type,
                status="seed",
                source_fact_id=None,
                discovered_by_run_id=None,
                created_at=now,
            )
        )
    # 缺省集合的范围由 Origin 原件持有；背景、参考和排除地址不自动成为 seed。
    return targets
