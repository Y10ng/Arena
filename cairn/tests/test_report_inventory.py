"""历史报告 Artifact inventory 的分类、范围、筛选和审计测试。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3

import pytest
from fastapi import HTTPException

from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import RequestPrincipal, protect_cookie_request
from cairn.server.models import CreateArtifactRequest
from cairn.server.routers import reports as reports_router
from cairn.server.services import audit_artifacts
from cairn.server.services.report_inventory import list_report_inventory

_DIGEST = "a" * 64


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db.run_migrations(conn, "sqlite")
    for tenant_id in ("tenant-a", "tenant-b"):
        conn.execute(
            "INSERT INTO tenants (id, kind, name, created_at) VALUES (?, 'personal', ?, ?)",
            (tenant_id, tenant_id, "2026-08-29T00:00:00Z"),
        )
    return conn


def _seed_project(
    conn: sqlite3.Connection,
    project_id: str,
    tenant_id: str,
    title: str,
) -> None:
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, scenario_key, tenant_id
        ) VALUES (?, ?, 'completed', '2026-08-29T00:00:00Z', 'default-copy-v2', ?)
        """,
        (project_id, title, tenant_id),
    )


def _metadata(
    *,
    generation_token: str,
    template_key: str = "standard-pentest",
    template_version: int = 1,
) -> dict[str, object]:
    return {
        "source": "report_task",
        "format": "markdown",
        "report_generation_token": generation_token,
        "report_template_key": template_key,
        "report_template_version": template_version,
        "report_template_digest_sha256": _DIGEST,
        "report_policy_version": 2,
        "report_execution_plan_sha256": "b" * 64,
        "report_prompt_digest_sha256": "c" * 64,
        "input_snapshot_artifact_id": f"snapshot-{generation_token}",
        "input_snapshot_sha256": "d" * 64,
        "completeness_rule_version": 3,
    }


def _seed_artifact(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    project_id: str,
    created_at: str,
    metadata: object,
    uri: str | None = None,
    kind: str = "report",
    category: str = "deliverable",
) -> None:
    metadata_json = metadata if isinstance(metadata, str) else json.dumps(metadata)
    task_run_id = f"run-{artifact_id}"
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, task_type, worker_name, worker_type, status,
            started_at, ended_at
        ) VALUES (?, ?, 'report', 'report-worker', 'claudecode', 'succeeded', ?, ?)
        """,
        (task_run_id, project_id, created_at, created_at),
    )
    conn.execute(
        """
        INSERT INTO artifacts (
            id, project_id, task_run_id, kind, category, uri, sha256, size,
            created_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 123, ?, ?)
        """,
        (
            artifact_id,
            project_id,
            task_run_id,
            kind,
            category,
            uri
            or f"artifact://projects/{project_id}/runs/{task_run_id}/{artifact_id}-vulnerability-report.md",
            "e" * 64,
            created_at,
            metadata_json,
        ),
    )


def test_inventory_accepts_real_artifact_id_prefixed_report_filename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    conn = _make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(audit_artifacts, "get_conn", fake_get_conn)
    monkeypatch.setattr(audit_artifacts, "artifact_root", lambda: tmp_path)
    try:
        _seed_project(conn, "project-a", "tenant-a", "Alpha Assessment")
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                started_at, ended_at
            ) VALUES (
                'run-real', 'project-a', 'report', 'report-worker', 'claudecode',
                'succeeded', '2026-08-29T01:00:00Z', '2026-08-29T01:00:00Z'
            )
            """
        )
        artifact = audit_artifacts.create_artifact_record(
            "project-a",
            CreateArtifactRequest(
                task_run_id="run-real",
                kind="report",
                category="deliverable",
                content="# Report",
                filename="vulnerability-report.md",
                metadata=_metadata(generation_token="generation-real"),
            ),
        )

        assert artifact.uri.endswith(f"/{artifact.id}-vulnerability-report.md")
        inventory = list_report_inventory(
            conn,
            tenant_id="tenant-a",
            q=artifact.id,
            page=1,
            page_size=20,
        )
        filename_inventory = list_report_inventory(
            conn,
            tenant_id="tenant-a",
            q="vulnerability-report.md",
            page=1,
            page_size=20,
        )

        assert [item.artifact_id for item in inventory.items] == [artifact.id]
        assert [item.artifact_id for item in filename_inventory.items] == [artifact.id]
    finally:
        conn.close()


def test_inventory_preserves_historical_versions_and_strictly_classifies_artifacts() -> None:
    conn = _make_conn()
    try:
        _seed_project(conn, "project-a", "tenant-a", "Alpha Assessment")
        _seed_artifact(
            conn,
            artifact_id="report-old",
            project_id="project-a",
            created_at="2026-08-29T01:00:00Z",
            metadata=_metadata(generation_token="generation-old", template_version=1),
            uri="artifact://projects/project-a/runs/run-report-old/vulnerability-report.md",
        )
        _seed_artifact(
            conn,
            artifact_id="report-new",
            project_id="project-a",
            created_at="2026-08-29T02:00:00Z",
            metadata=_metadata(generation_token="generation-new", template_version=2),
        )
        invalid_cases = {
            "missing-provenance": {
                key: value
                for key, value in _metadata(generation_token="missing").items()
                if key != "input_snapshot_sha256"
            },
            "bad-digest": {
                **_metadata(generation_token="bad-digest"),
                "report_prompt_digest_sha256": "not-a-digest",
            },
            "wrong-source": {
                **_metadata(generation_token="wrong-source"),
                "source": "manual",
            },
        }
        for artifact_id, metadata in invalid_cases.items():
            _seed_artifact(
                conn,
                artifact_id=artifact_id,
                project_id="project-a",
                created_at="2026-08-29T03:00:00Z",
                metadata=metadata,
            )
        _seed_artifact(
            conn,
            artifact_id="wrong-name",
            project_id="project-a",
            created_at="2026-08-29T03:00:00Z",
            metadata=_metadata(generation_token="wrong-name"),
            uri="artifact://projects/project-a/runs/run-wrong-name/notes.md",
        )
        _seed_artifact(
            conn,
            artifact_id="input-snapshot",
            project_id="project-a",
            created_at="2026-08-29T03:00:00Z",
            metadata=_metadata(generation_token="input-snapshot"),
            uri="artifact://projects/project-a/runs/run-input-snapshot/input-snapshot-report-input-snapshot.json",
        )
        _seed_artifact(
            conn,
            artifact_id="wrong-category",
            project_id="project-a",
            created_at="2026-08-29T03:00:00Z",
            metadata=_metadata(generation_token="wrong-category"),
            category="evidence",
        )

        inventory = list_report_inventory(
            conn,
            tenant_id="tenant-a",
            page=1,
            page_size=20,
        )

        assert inventory.total == 2
        assert [item.artifact_id for item in inventory.items] == [
            "report-new",
            "report-old",
        ]
        assert [item.template_version for item in inventory.items] == [2, 1]
        payload = inventory.items[0].model_dump()
        assert payload["task_run_id"] == "run-report-new"
        assert payload["input_snapshot_artifact_id"] == "snapshot-generation-new"
        assert "generation_token" not in payload
        assert "metadata" not in payload
        assert "metadata_json" not in payload
    finally:
        conn.close()


def test_inventory_scopes_filters_and_paginates_with_stable_artifact_tiebreaker() -> None:
    conn = _make_conn()
    try:
        _seed_project(conn, "project-a", "tenant-a", "Alpha Assessment")
        _seed_project(conn, "project-b", "tenant-b", "Beta Assessment")
        for artifact_id, template_key, created_at in (
            ("report-a1", "standard-pentest", "2026-08-29T01:00:00Z"),
            ("report-a2", "executive", "2026-08-29T02:00:00Z"),
            ("report-a3", "executive", "2026-08-29T02:00:00Z"),
        ):
            _seed_artifact(
                conn,
                artifact_id=artifact_id,
                project_id="project-a",
                created_at=created_at,
                metadata=_metadata(
                    generation_token=artifact_id,
                    template_key=template_key,
                ),
            )
        _seed_artifact(
            conn,
            artifact_id="report-b1",
            project_id="project-b",
            created_at="2026-08-29T03:00:00Z",
            metadata=_metadata(generation_token="report-b1", template_key="executive"),
        )

        first = list_report_inventory(
            conn,
            tenant_id="tenant-a",
            q="alpha",
            project_id="project-a",
            template_key="executive",
            date_from="2026-08-29T02:00:00Z",
            date_to="2026-08-29T02:00:00Z",
            page=1,
            page_size=1,
        )
        second = list_report_inventory(
            conn,
            tenant_id="tenant-a",
            q="alpha",
            project_id="project-a",
            template_key="executive",
            date_from="2026-08-29T02:00:00Z",
            date_to="2026-08-29T02:00:00Z",
            page=2,
            page_size=1,
        )
        global_inventory = list_report_inventory(
            conn,
            tenant_id=None,
            template_key="executive",
            page=1,
            page_size=20,
        )

        assert first.total == 2
        assert first.page == 1 and first.page_size == 1
        assert [item.artifact_id for item in first.items] == ["report-a3"]
        assert [item.artifact_id for item in second.items] == ["report-a2"]
        assert [item.artifact_id for item in global_inventory.items] == [
            "report-b1",
            "report-a3",
            "report-a2",
        ]
    finally:
        conn.close()


def _call_route(
    principal: RequestPrincipal,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    return reports_router.get_reports_route(
        q=None,
        project_id=None,
        template_key=None,
        date_from=date_from,
        date_to=date_to,
        page=1,
        page_size=20,
        principal=principal,
    )


def test_reports_route_scopes_users_and_audits_super_admin_global_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(reports_router, "get_conn", fake_get_conn)
    try:
        _seed_project(conn, "project-a", "tenant-a", "Alpha")
        _seed_project(conn, "project-b", "tenant-b", "Beta")
        for project_id in ("project-a", "project-b"):
            _seed_artifact(
                conn,
                artifact_id=f"report-{project_id}",
                project_id=project_id,
                created_at="2026-08-29T01:00:00Z",
                metadata=_metadata(generation_token=project_id),
            )

        user_page = _call_route(
            RequestPrincipal("user", "user-a", "tenant-a", "user")
        )
        admin_page = _call_route(
            RequestPrincipal("user", "admin", "tenant-a", "super_admin")
        )

        assert [item.project_id for item in user_page.items] == ["project-a"]
        assert {item.project_id for item in admin_page.items} == {
            "project-a",
            "project-b",
        }
        audit = conn.execute(
            """
            SELECT actor_type, actor_id, tenant_id, action, resource_type,
                   resource_id, outcome
            FROM security_audit_events
            WHERE action = 'admin.reports.read'
            """
        ).fetchone()
        assert dict(audit) == {
            "actor_type": "user",
            "actor_id": "admin",
            "tenant_id": "tenant-a",
            "action": "admin.reports.read",
            "resource_type": "admin_read_model",
            "resource_id": None,
            "outcome": "success",
        }
        with pytest.raises(HTTPException) as missing_tenant:
            _call_route(RequestPrincipal("user", "orphan", None, "user"))
        assert missing_tenant.value.status_code == 403
        with pytest.raises(HTTPException) as reversed_dates:
            _call_route(
                RequestPrincipal("user", "user-a", "tenant-a", "user"),
                date_from=datetime(2026, 8, 30, tzinfo=timezone.utc),
                date_to=datetime(2026, 8, 29, tzinfo=timezone.utc),
            )
        assert reversed_dates.value.status_code == 422
    finally:
        conn.close()


def test_reports_route_is_registered_with_cookie_authentication_and_bounds() -> None:
    # OpenAPI 验证应用挂载；路由本身的依赖检查不依赖 FastAPI 的延迟挂载实现。
    assert "get" in app.openapi()["paths"]["/reports"]
    route = next(
        route
        for route in reports_router.router.routes
        if getattr(route, "path", None) == "/reports"
        and "GET" in getattr(route, "methods", set())
    )

    assert protect_cookie_request in {
        dependency.call for dependency in route.dependant.dependencies
    }
    query_fields = {
        parameter.name: parameter.field_info
        for parameter in route.dependant.query_params
    }
    assert query_fields["q"].metadata
    assert query_fields["page_size"].metadata
