"""项目组服务测试：Tenant 隔离、创建者归因与项目分组。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import (
    CreateProjectGroupRequest,
    MoveProjectToGroupRequest,
    UpdateProjectGroupRequest,
)
from cairn.server.services import project_groups as groups_service
from cairn.server.services import project_records as records_service


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn)
    # 轻量内存库不运行版本化迁移；补齐项目归属与只读归因所需的最小结构。
    conn.execute("ALTER TABLE projects ADD COLUMN tenant_id TEXT")
    conn.execute("ALTER TABLE project_groups ADD COLUMN tenant_id TEXT")
    conn.executescript(
        """
        CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE users (id TEXT PRIMARY KEY, login TEXT NOT NULL);
        CREATE TABLE tenant_memberships (
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO tenants (id, name) VALUES
            ('tenant_a', 'Tenant A'),
            ('tenant_b', 'Tenant B');
        """
    )
    return conn


@contextmanager
def patched_conn(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection):
    monkeypatch.setattr(groups_service, "get_conn", lambda: _yield(conn))
    yield


@contextmanager
def patched_records_conn(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection):
    # project_records 直接引用了 db.get_conn，需单独 patch 它的引用。
    monkeypatch.setattr(records_service, "get_conn", lambda: _yield(conn))
    yield


def _insert_project(conn: sqlite3.Connection, pid: str, tenant_id: str) -> None:
    conn.execute(
        "INSERT INTO projects (id, title, status, created_at, scenario_key, tenant_id) "
        "VALUES (?, ?, 'active', '2026-01-01T00:00:00Z', 'default', ?)",
        (pid, pid, tenant_id),
    )


@contextmanager
def _yield(conn: sqlite3.Connection):
    yield conn


def test_tenant_is_required_by_service_contract() -> None:
    body = CreateProjectGroupRequest(name="组")
    with pytest.raises(TypeError):
        groups_service.create_project_group(body)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        groups_service.list_project_groups()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="tenant_id is required"):
        groups_service.list_project_groups(tenant_id="")


def test_create_ignores_spoofed_owner_and_lists_by_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = make_conn()
    try:
        with patched_conn(monkeypatch, conn):
            group = groups_service.create_project_group(
                CreateProjectGroupRequest(name="Tenant A", owner="forged-owner"),
                tenant_id="tenant_a",
                actor_id="usr_a",
            )
            row = conn.execute(
                "SELECT owner, tenant_id FROM project_groups WHERE id = ?", (group.id,)
            ).fetchone()
            assert dict(row) == {"owner": "usr_a", "tenant_id": "tenant_a"}
            assert [item.id for item in groups_service.list_project_groups(tenant_id="tenant_a")] == [group.id]
            assert groups_service.list_project_groups(tenant_id="tenant_b") == []
    finally:
        conn.close()


def test_cross_tenant_update_and_delete_return_404(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = make_conn()
    try:
        with patched_conn(monkeypatch, conn):
            group = groups_service.create_project_group(
                CreateProjectGroupRequest(name="Tenant A"),
                tenant_id="tenant_a",
                actor_id="usr_a",
            )
            with pytest.raises(HTTPException) as update_exc:
                groups_service.update_project_group(
                    group.id,
                    UpdateProjectGroupRequest(name="hidden"),
                    tenant_id="tenant_b",
                )
            assert update_exc.value.status_code == 404

            with pytest.raises(HTTPException) as delete_exc:
                groups_service.delete_project_group(group.id, tenant_id="tenant_b")
            assert delete_exc.value.status_code == 404
    finally:
        conn.close()


def test_move_project_to_group_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：项目只能移入同一 Tenant 的分组，也可移出分组。"""
    conn = make_conn()
    try:
        with patched_conn(monkeypatch, conn), patched_records_conn(monkeypatch, conn):
            group = groups_service.create_project_group(
                CreateProjectGroupRequest(name="G1"),
                tenant_id="tenant_a",
                actor_id="usr_a",
            )
            _insert_project(conn, "proj_001", "tenant_a")
            meta = records_service.update_project_group_record(
                "proj_001",
                MoveProjectToGroupRequest(group_id=group.id),
                tenant_id="tenant_a",
            )
            assert meta.group_id == group.id

            meta = records_service.update_project_group_record(
                "proj_001",
                MoveProjectToGroupRequest(group_id=None),
                tenant_id="tenant_a",
            )
            assert meta.group_id is None
    finally:
        conn.close()


def test_delete_group_keeps_project_and_clears_group_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：删除项目组仅将存量项目恢复为未分组，不删除项目。"""
    conn = make_conn()
    try:
        with patched_conn(monkeypatch, conn):
            group = groups_service.create_project_group(
                CreateProjectGroupRequest(name="Disposable"),
                tenant_id="tenant_a",
                actor_id="usr_a",
            )
            _insert_project(conn, "proj_legacy", "tenant_a")
            conn.execute(
                "UPDATE projects SET group_id = ? WHERE id = ?",
                (group.id, "proj_legacy"),
            )

            groups_service.delete_project_group(group.id, tenant_id="tenant_a")

            row = conn.execute(
                "SELECT group_id FROM projects WHERE id = ?",
                ("proj_legacy",),
            ).fetchone()
            assert row is not None
            assert row["group_id"] is None
    finally:
        conn.close()


def test_move_project_rejects_cross_tenant_group(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = make_conn()
    try:
        with patched_conn(monkeypatch, conn), patched_records_conn(monkeypatch, conn):
            group = groups_service.create_project_group(
                CreateProjectGroupRequest(name="Tenant B"),
                tenant_id="tenant_b",
                actor_id="usr_b",
            )
            _insert_project(conn, "proj_001", "tenant_a")
            with pytest.raises(HTTPException) as exc:
                records_service.update_project_group_record(
                    "proj_001",
                    MoveProjectToGroupRequest(group_id=group.id),
                    tenant_id="tenant_a",
                )
            assert exc.value.status_code == 404
    finally:
        conn.close()


def test_move_and_rename_request_schemas_do_not_collide() -> None:
    # 守卫：重命名分组与移动项目使用不同 schema，避免字段再次互相覆盖。
    assert set(UpdateProjectGroupRequest.model_fields) == {"name"}
    assert set(MoveProjectToGroupRequest.model_fields) == {"group_id"}
