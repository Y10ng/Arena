"""项目 router 使用的 target 服务。"""

from __future__ import annotations

from fastapi import HTTPException

from cairn.server.db import get_conn
from cairn.server.models import CreateTargetRequest, Target, UpdateTargetRequest

from .ids import get_project_or_404, next_target_id
from .projects import validate_facts_exist, validate_task_runs_exist
from .targets import (
    build_targets,
    get_target_or_404,
    normalize_target_value,
    target_to_model,
    validate_target_not_duplicate,
)
from .time_utils import utcnow


def list_target_records(project_id: str) -> list[Target]:
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return build_targets(conn, project_id)


def create_target_record(project_id: str, body: CreateTargetRequest) -> Target:
    with get_conn() as conn:
        # 与重新授权共用项目写锁，避免确认摘要后并发扩大执行范围。
        conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
        project = get_project_or_404(conn, project_id)
        if project["status"] == "active" and body.status in {"seed", "approved"}:
            raise HTTPException(409, "project_authorization_scope_change_requires_pause")
        # target 的来源 fact/run 必须属于同一 project，避免跨项目证据串线。
        if body.source_fact_id is not None:
            validate_facts_exist(conn, project_id, [body.source_fact_id])
        if body.discovered_by_run_id is not None:
            validate_task_runs_exist(conn, project_id, [body.discovered_by_run_id])

        normalized_value = normalize_target_value(body.type, body.value)
        validate_target_not_duplicate(conn, project_id, body.type, normalized_value)
        target_id = next_target_id(conn, project_id)
        now = utcnow()
        conn.execute(
            """
            INSERT INTO targets (id, project_id, value, type, status, source_fact_id, discovered_by_run_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_id,
                project_id,
                normalized_value,
                body.type,
                body.status,
                body.source_fact_id,
                body.discovered_by_run_id,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM targets WHERE id = ? AND project_id = ?",
            (target_id, project_id),
        ).fetchone()
        assert row is not None
        return target_to_model(row)


def update_target_record(project_id: str, target_id: str, body: UpdateTargetRequest) -> Target:
    with get_conn() as conn:
        conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
        project = get_project_or_404(conn, project_id)
        target = get_target_or_404(conn, project_id, target_id)
        executable = {"seed", "approved"}
        if (
            project["status"] == "active"
            and target["status"] not in executable
            and body.status in executable
        ):
            raise HTTPException(409, "project_authorization_scope_change_requires_pause")
        conn.execute(
            "UPDATE targets SET status = ? WHERE id = ? AND project_id = ?",
            (body.status, target_id, project_id),
        )
        updated = conn.execute(
            "SELECT * FROM targets WHERE id = ? AND project_id = ?",
            (target_id, project_id),
        ).fetchone()
        assert updated is not None
        return target_to_model(updated)
