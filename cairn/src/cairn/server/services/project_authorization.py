"""项目授权 epoch、非敏感范围摘要与显式重新授权。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any, Mapping

from fastapi import HTTPException

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL
from cairn.project_authorization_contract import (
    AUTHORIZATION_PROTOCOL,
    AUTHORIZATION_V2_PROTOCOL,
    authorization_scope_digest,
    authorization_scope_digest_v3,
    authorization_window_seconds,
    build_scope_descriptor,
    build_scope_descriptor_v3,
    canonical_json,
    canonical_object,
    target_scope_v3,
)
from cairn.server.db import get_conn
from cairn.server.models import ProjectMeta, ReauthorizeProjectRequest

from .assessment_policy import (
    parse_scenario_snapshot,
    policy_from_project_row,
    row_value,
)
from .coverage_scope import target_scope_snapshot
from .ids import get_project_or_404
from .time_utils import parse_utc_timestamp, utcnow


@dataclass(frozen=True)
class AuthorizationScopeProjection:
    scope_digest: str
    descriptor: dict[str, Any]
    window_seconds: int | None


def _goal_driven_target_scope(conn, project_id: str) -> tuple[str, list[str]]:
    targets = [
        dict(row)
        for row in conn.execute(
            "SELECT id, type, value, status FROM targets "
            "WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
    ]
    return target_scope_v3(targets)


def authorization_scope_projection(
    conn,
    project: Mapping[str, Any],
) -> AuthorizationScopeProjection:
    """新 scope 使用 v3 的可执行集合语义；v1 保持既有冻结摘要。"""
    policy = policy_from_project_row(project)
    if policy.protocol == GOAL_DRIVEN_PROTOCOL:
        target_digest, target_ids = _goal_driven_target_scope(
            conn,
            str(project["id"]),
        )
        descriptor = build_scope_descriptor_v3(
            project_id=str(project["id"]),
            tenant_id=str(row_value(project, "tenant_id") or ""),
            target_digest=target_digest,
            target_ids=target_ids,
        )
        return AuthorizationScopeProjection(
            scope_digest=authorization_scope_digest_v3(descriptor),
            descriptor=descriptor,
            window_seconds=None,
        )

    snapshot = parse_scenario_snapshot(row_value(project, "scenario_snapshot_json"))
    target_digest, target_ids = target_scope_snapshot(conn, str(project["id"]))
    window_seconds = authorization_window_seconds(snapshot)
    descriptor = build_scope_descriptor(
        project_id=str(project["id"]),
        tenant_id=str(row_value(project, "tenant_id") or ""),
        scenario_key=str(row_value(project, "scenario_key") or ""),
        scenario_snapshot=snapshot,
        target_digest=target_digest,
        target_ids=target_ids,
        assessment_protocol=policy.protocol,
        assessment_contract_version=policy.contract_version,
        explore_budget=policy.explore_budget,
        verification_reserve=policy.verification_reserve,
        window_seconds=window_seconds,
    )
    return AuthorizationScopeProjection(
        scope_digest=authorization_scope_digest(descriptor),
        descriptor=descriptor,
        window_seconds=window_seconds,
    )


def _deadline_from(now: str, window_seconds: int) -> str:
    current = parse_utc_timestamp(now)
    if current is None:
        raise ValueError("now must be an absolute UTC timestamp")
    return (current.astimezone(timezone.utc) + timedelta(seconds=window_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _insert_authorization_grant_v1(
    conn,
    *,
    project: Mapping[str, Any],
    epoch: int,
    source: str,
    actor_type: str,
    actor_id: str,
    projection: AuthorizationScopeProjection,
    previous_deadline: str | None,
    new_deadline: str,
    execution_generation: int,
    authorized_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO project_authorization_grants (
            project_id, authorization_epoch, protocol, source,
            actor_type, actor_id, scope_digest, scope_descriptor_json,
            window_seconds, previous_deadline, new_deadline,
            execution_generation, authorized_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project["id"],
            epoch,
            AUTHORIZATION_PROTOCOL,
            source,
            actor_type,
            actor_id,
            projection.scope_digest,
            canonical_json(projection.descriptor),
            projection.window_seconds,
            previous_deadline,
            new_deadline,
            execution_generation,
            authorized_at,
        ),
    )


def _insert_authorization_grant_v2(
    conn,
    *,
    project: Mapping[str, Any],
    epoch: int,
    source: str,
    actor_type: str,
    actor_id: str,
    projection: AuthorizationScopeProjection,
    authorization_expires_at: str | None,
    execution_generation: int,
    authorized_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO project_authorization_grants_v2 (
            project_id, authorization_epoch, protocol, source,
            actor_type, actor_id, scope_digest, scope_descriptor_json,
            authorization_expires_at, execution_generation, authorized_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project["id"],
            epoch,
            AUTHORIZATION_V2_PROTOCOL,
            source,
            actor_type,
            actor_id,
            projection.scope_digest,
            canonical_json(projection.descriptor),
            authorization_expires_at,
            execution_generation,
            authorized_at,
        ),
    )


def initialize_project_authorization(
    conn,
    project_id: str,
    *,
    actor_id: str,
    authorized_at: str,
) -> None:
    """seed Target 写入后冻结首个授权范围，不混入预算或执行截止。"""
    project = get_project_or_404(conn, project_id)
    policy = policy_from_project_row(project)
    if int(row_value(project, "authorization_epoch") or 0) != 0:
        return
    if not policy.goal_driven:
        if not policy.phased or not policy.explore_deadline:
            return
        projection = authorization_scope_projection(conn, project)
        updated = conn.execute(
            "UPDATE projects SET authorization_epoch = 1, "
            "authorization_scope_digest = ?, authorization_window_seconds = ? "
            "WHERE id = ? AND authorization_epoch = 0",
            (projection.scope_digest, projection.window_seconds, project_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("Project authorization changed during creation")
        _insert_authorization_grant_v1(
            conn,
            project=project,
            epoch=1,
            source="project_creation",
            actor_type="user",
            actor_id=actor_id,
            projection=projection,
            previous_deadline=None,
            new_deadline=policy.explore_deadline,
            execution_generation=int(row_value(project, "execution_generation") or 0),
            authorized_at=authorized_at,
        )
        return

    projection = authorization_scope_projection(conn, project)
    updated = conn.execute(
        "UPDATE projects SET authorization_epoch = 1, "
        "authorization_scope_digest = ?, authorization_window_seconds = NULL "
        "WHERE id = ? AND authorization_epoch = 0",
        (projection.scope_digest, project_id),
    )
    if updated.rowcount != 1:
        raise RuntimeError("Project authorization changed during creation")
    _insert_authorization_grant_v2(
        conn,
        project=project,
        epoch=1,
        source="project_creation",
        actor_type="user",
        actor_id=actor_id,
        projection=projection,
        authorization_expires_at=row_value(project, "authorization_expires_at"),
        execution_generation=int(row_value(project, "execution_generation") or 0),
        authorized_at=authorized_at,
    )


def project_authorization_block_reason(
    conn,
    project: Mapping[str, Any],
    *,
    now: str | None = None,
) -> str | None:
    """准入、调度快照和诊断共用授权判断；只读，不转换历史 grant。"""
    epoch = int(row_value(project, "authorization_epoch") or 0)
    current = parse_utc_timestamp(now or utcnow())
    if epoch < 1 or current is None:
        return "project_reauthorization_required"
    policy = policy_from_project_row(project)
    raw_expiry = row_value(
        project, "authorization_expires_at" if policy.goal_driven else "explore_deadline"
    )
    if not (policy.goal_driven and raw_expiry is None):
        expires = parse_utc_timestamp(raw_expiry) if isinstance(raw_expiry, str) else None
        if expires is None or current >= expires:
            return "project_authorization_expired"

    projection = authorization_scope_projection(conn, project)
    authorized_digest = str(row_value(project, "authorization_scope_digest") or "")
    if authorized_digest == projection.scope_digest:
        return None
    if policy.goal_driven:
        grant = conn.execute(
            "SELECT scope_descriptor_json FROM project_authorization_grants_v2 "
            "WHERE project_id = ? AND authorization_epoch = ? AND scope_digest = ?",
            (project["id"], epoch, authorized_digest),
        ).fetchone()
        # v2 未保存完整目标快照，不能由当前状态推测原范围后自动授权。
        if grant and canonical_object(grant["scope_descriptor_json"]).get("version") == 2:
            return "project_authorization_scope_upgrade_required"
    return "project_authorization_scope_changed"


def project_authorization_is_current(
    conn,
    project: Mapping[str, Any],
    *,
    now: str | None = None,
) -> bool:
    return project_authorization_block_reason(conn, project, now=now) is None


def require_project_authorization_current(
    conn,
    project: Mapping[str, Any],
    *,
    now: str | None = None,
) -> None:
    """新执行必须仍处于同一真实授权范围和期限内。"""
    if not project_authorization_is_current(conn, project, now=now):
        raise HTTPException(409, "project_reauthorization_required")


def project_reauthorization_reason(
    conn,
    project: Mapping[str, Any],
    *,
    now: str | None = None,
) -> str | None:
    """返回禁止重新授权的稳定原因；None 表示需要且允许显式确认。"""
    from .project_records import project_pause_draining

    policy = policy_from_project_row(project)
    if not (policy.goal_driven or policy.phased):
        return "assessment_contract_unsupported"
    if project["status"] != "paused":
        return "project_status_invalid"
    if policy.phased:
        progress = parse_scenario_snapshot(row_value(project, "assessment_progress_json"))
        if progress.get("phase") in {"finalizing", "completed"}:
            return "assessment_new_project_required"
    if conn.execute(
        "SELECT 1 FROM assessment_report_finalizations WHERE project_id = ? LIMIT 1",
        (project["id"],),
    ).fetchone() is not None:
        return "assessment_report_finalization_in_progress"
    if project_pause_draining(conn, project):
        return "project_pause_draining"
    if project_authorization_is_current(conn, project, now=now):
        return "project_authorization_current"
    return None


def reauthorize_project_record(
    project_id: str,
    body: ReauthorizeProjectRequest,
    *,
    actor_id: str,
    now: str | None = None,
) -> ProjectMeta:
    authorized_at = now or utcnow()
    with get_conn() as conn:
        # 与 Target 修改、Pause/Resume 和执行 admission 串行，防止确认后范围漂移。
        conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
        project = get_project_or_404(conn, project_id)
        epoch = int(row_value(project, "authorization_epoch") or 0)
        if body.expected_authorization_epoch != epoch:
            raise HTTPException(409, "authorization_epoch_mismatch")
        projection = authorization_scope_projection(conn, project)
        if body.expected_scope_digest != projection.scope_digest:
            raise HTTPException(409, "authorization_scope_mismatch")
        reason = project_reauthorization_reason(conn, project, now=authorized_at)
        if reason is not None:
            raise HTTPException(409, reason)

        new_epoch = epoch + 1
        old_generation = int(row_value(project, "execution_generation") or 0)
        new_generation = old_generation + 1
        policy = policy_from_project_row(project)
        if policy.goal_driven:
            expires_at = (
                body.authorization_expires_at
                if "authorization_expires_at" in body.model_fields_set
                else row_value(project, "authorization_expires_at")
            )
            expiry = None
            if expires_at is not None:
                if not isinstance(expires_at, str):
                    raise HTTPException(422, "authorization_expiry_invalid")
                expiry = parse_utc_timestamp(expires_at)
                if expiry is None:
                    raise HTTPException(422, "authorization_expiry_invalid")
            current = parse_utc_timestamp(authorized_at)
            if expiry is not None and current is not None and expiry <= current:
                raise HTTPException(422, "authorization_expiry_not_future")
            updated = conn.execute(
                """
                UPDATE projects
                SET authorization_epoch = ?, authorization_scope_digest = ?,
                    authorization_window_seconds = NULL,
                    authorization_expires_at = ?, execution_generation = ?
                WHERE id = ? AND status = 'paused' AND authorization_epoch = ?
                  AND execution_generation = ?
                """,
                (
                    new_epoch,
                    projection.scope_digest,
                    expires_at,
                    new_generation,
                    project_id,
                    epoch,
                    old_generation,
                ),
            )
            if updated.rowcount != 1:
                raise HTTPException(409, "project_authorization_conflict")
            _insert_authorization_grant_v2(
                conn,
                project=project,
                epoch=new_epoch,
                source="explicit_reauthorization",
                actor_type="user",
                actor_id=actor_id,
                projection=projection,
                authorization_expires_at=expires_at,
                execution_generation=new_generation,
                authorized_at=authorized_at,
            )
        else:
            previous_deadline = row_value(project, "explore_deadline")
            assert projection.window_seconds is not None
            new_deadline = _deadline_from(authorized_at, projection.window_seconds)
            updated = conn.execute(
                """
                UPDATE projects
                SET authorization_epoch = ?, authorization_scope_digest = ?,
                    authorization_window_seconds = ?, explore_deadline = ?,
                    execution_generation = ?
                WHERE id = ? AND status = 'paused' AND authorization_epoch = ?
                  AND execution_generation = ?
                """,
                (
                    new_epoch,
                    projection.scope_digest,
                    projection.window_seconds,
                    new_deadline,
                    new_generation,
                    project_id,
                    epoch,
                    old_generation,
                ),
            )
            if updated.rowcount != 1:
                raise HTTPException(409, "project_authorization_conflict")
            _insert_authorization_grant_v1(
                conn,
                project=project,
                epoch=new_epoch,
                source="explicit_reauthorization",
                actor_type="user",
                actor_id=actor_id,
                projection=projection,
                previous_deadline=previous_deadline,
                new_deadline=new_deadline,
                execution_generation=new_generation,
                authorized_at=authorized_at,
            )
        from .projects import project_meta_from_row

        return project_meta_from_row(conn, get_project_or_404(conn, project_id))
