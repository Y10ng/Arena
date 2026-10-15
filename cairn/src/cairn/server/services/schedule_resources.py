"""计划持有原始资料，每轮生成独立项目副本。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from cairn.server.db import register_after_commit
from cairn.server.models import ProjectResource

from .ids import get_project_or_404
from .project_resources import (
    _batch_member_disk_path,
    _copy_project_resource_records,
    _list_resource_batch_members,
    _lock_owned_open_batch,
    _mark_resource_batch_consumed,
    _safe_storage_segment,
    check_resource_collection,
    copy_verified_resource_content,
    delete_resource_copy,
    project_resource_root,
)
from .time_utils import utcnow


def _get_schedule(conn: Any, schedule_id: str) -> Any:
    row = conn.execute(
        "SELECT * FROM project_schedules WHERE id = ?", (schedule_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Project schedule not found")
    return row


def _lock_schedule(conn: Any, schedule_id: str) -> Any:
    # 与计划编辑及触发共用行锁，确保最终资料集合不会在复制期间变化。
    conn.execute("UPDATE project_schedules SET id = id WHERE id = ?", (schedule_id,))
    return _get_schedule(conn, schedule_id)


def _list_schedule_resource_rows(conn: Any, schedule_id: str) -> list[Any]:
    return conn.execute(
        "SELECT * FROM schedule_resources WHERE schedule_id = ? ORDER BY created_at, id",
        (schedule_id,),
    ).fetchall()


def list_schedule_resource_records(conn: Any, schedule_id: str) -> list[dict[str, Any]]:
    _get_schedule(conn, schedule_id)
    return [
        {
            "id": row["id"],
            "path": row["path"],
            "kind": row["kind"],
            "size_bytes": int(row["size_bytes"]),
            "digest_sha256": row["digest_sha256"],
            "source_name": row["source_name"],
        }
        for row in _list_schedule_resource_rows(conn, schedule_id)
    ]


def update_schedule_resources(
    conn: Any,
    schedule_id: str,
    *,
    resource_ids: list[str] | None = None,
    batch_id: str | None = None,
    tenant_id: str,
    user_id: str,
    created_by: str,
) -> list[dict[str, Any]]:
    """None 保留全部、空列表移除全部；新批次追加到最终集合。"""
    schedule = _lock_schedule(conn, schedule_id)
    if schedule["tenant_id"] != tenant_id:
        raise HTTPException(404, "Project schedule not found")
    existing = _list_schedule_resource_rows(conn, schedule_id)
    by_id = {row["id"]: row for row in existing}
    if resource_ids is None:
        retained_ids = set(by_id)
    else:
        if len(set(resource_ids)) != len(resource_ids):
            raise HTTPException(422, "Duplicate schedule resource ID")
        if any(resource_id not in by_id for resource_id in resource_ids):
            raise HTTPException(404, "Schedule resource not found")
        retained_ids = set(resource_ids)
    retained = [row for row in existing if row["id"] in retained_ids]
    removed = [row for row in existing if row["id"] not in retained_ids]
    members: list[Any] = []
    if batch_id is not None:
        _lock_owned_open_batch(conn, batch_id, tenant_id=tenant_id, user_id=user_id)
        members = _list_resource_batch_members(conn, batch_id)
    check_resource_collection(
        [(row["kind"], row["path"], int(row["size_bytes"])) for row in retained + members]
    )

    created_paths: list[Path] = []
    try:
        # 先解除 DB 归属以支持同路径替换；原件仍保留到整个事务成功提交。
        for row in removed:
            conn.execute(
                "DELETE FROM schedule_resources WHERE id = ? AND schedule_id = ?",
                (row["id"], schedule_id),
            )
        for member in members:
            resource_id = f"sr_{uuid4().hex}"
            target = _schedule_resource_disk_path(schedule_id, resource_id, create_parent=True)
            copy_verified_resource_content(
                conn,
                _batch_member_disk_path(batch_id, member["id"]),
                target,
                path=member["path"],
                size_bytes=int(member["size_bytes"]),
                digest_sha256=member["sha256"],
            )
            created_paths.append(target)
            conn.execute(
                """
                INSERT INTO schedule_resources
                    (id, schedule_id, path, kind, size_bytes, digest_sha256,
                     source_name, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resource_id, schedule_id, member["path"], member["kind"],
                    int(member["size_bytes"]), member["sha256"], member["path"], created_by, utcnow(),
                ),
            )
        if batch_id is not None:
            _mark_resource_batch_consumed(conn, batch_id, schedule_id=schedule_id)
        for row in removed:
            target = _schedule_resource_disk_path(schedule_id, row["id"])
            register_after_commit(conn, lambda target=target: delete_resource_copy(target))
        return list_schedule_resource_records(conn, schedule_id)
    except Exception:
        for target in reversed(created_paths):
            delete_resource_copy(target)
        raise


def copy_schedule_resources_to_project(
    conn: Any,
    schedule_id: str,
    project_id: str,
    *,
    created_by: str,
) -> list[ProjectResource]:
    schedule = _lock_schedule(conn, schedule_id)
    project = get_project_or_404(conn, project_id)
    if project["tenant_id"] != schedule["tenant_id"]:
        raise HTTPException(404, "Project schedule not found")
    rows = _list_schedule_resource_rows(conn, schedule_id)
    return _copy_project_resource_records(
        conn,
        project_id,
        [
            (
                row["kind"], row["path"], int(row["size_bytes"]), row["digest_sha256"],
                _schedule_resource_disk_path(schedule_id, row["id"]),
            )
            for row in rows
        ],
        created_by=created_by,
    )


def _schedule_resource_directory(schedule_id: str) -> Path:
    safe_schedule_id = _safe_storage_segment(schedule_id, "schedule_id")
    root = project_resource_root()
    directory = root / ".schedules" / safe_schedule_id
    try:
        directory.resolve().relative_to(root)
    except ValueError as exc:
        raise HTTPException(404, "Schedule resource storage path is invalid") from exc
    return directory


def _schedule_resource_disk_path(
    schedule_id: str,
    resource_id: str,
    *,
    create_parent: bool = False,
) -> Path:
    safe_resource_id = _safe_storage_segment(resource_id, "resource_id")
    directory = _schedule_resource_directory(schedule_id)
    disk_path = directory / safe_resource_id
    try:
        disk_path.resolve().relative_to(directory.resolve())
    except ValueError as exc:
        raise HTTPException(404, "Schedule resource storage path is invalid") from exc
    if create_parent:
        directory.mkdir(parents=True, exist_ok=True)
    return disk_path


__all__ = [name for name in globals() if not name.startswith("_")]
