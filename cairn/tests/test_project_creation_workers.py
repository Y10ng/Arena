"""普通项目由服务端绑定同 Tenant 的固定三类 Worker。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import RequestPrincipal, create_user_with_personal_tenant
from cairn.server.models import (
    CreateProjectRequest,
    CreateProjectResourceRequest,
)
from cairn.server.routers.projects import (
    list_projects,
)
from cairn.server.schemas.models import CreateTenantModelRequest
from cairn.server.schemas.tenant_workers import UpdateTenantWorkerRequest
from cairn.server.services import model_credentials
from cairn.server.services.project_records import (
    create_project_record,
    delete_project_record,
    get_project_detail,
)
from cairn.server.services.project_resources import (
    create_project_resource_batch,
    upload_project_resource_batch_file,
)
from cairn.server.services.scenarios import list_scenarios, replace_scenarios
from cairn.server.services.tenant_models import create_tenant_model
from cairn.server.services.tenant_workers import list_tenant_workers, update_tenant_worker
from cairn.server.services import tenant_workers as tenant_workers_service
from test_postgres_bootstrap import postgres_instance, empty_postgres_url


@pytest.fixture(autouse=True)
def configured_database(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1":
        pytest.skip("需显式启用本地一次性 PostgreSQL，绝不使用业务库")
    db._reset_for_tests()
    url = request.getfixturevalue("empty_postgres_url")
    db.configure(database_url=url, artifact_path=tmp_path / "artifacts")
    keyring = tmp_path / "keyring.json"
    keyring.write_text(
        json.dumps(
            {
                "active_version": "key-v1",
                "keys": {"key-v1": base64.b64encode(b"p" * 32).decode()},
            }
        ),
        encoding="utf-8",
    )
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_ROOT", str(tmp_path / "project-resources"))
    monkeypatch.setattr(
        tenant_workers_service,
        "dispatcher_executor_available",
        lambda conn: True,
    )
    yield
    db._reset_for_tests()


def _principal(login: str, role: str = "user") -> RequestPrincipal:
    user_id, tenant_id = create_user_with_personal_tenant(
        login,
        "safe password",
        role=role,
    )
    return RequestPrincipal(
        kind="user",
        subject_id=user_id,
        tenant_id=tenant_id,
        role=role,
    )


def _worker(principal: RequestPrincipal, name: str):
    model = create_tenant_model(
        CreateTenantModelRequest(
            display_name=f"{name} Model",
            protocol="anthropic-messages",
            base_url="https://api.example.test",
            model_name="claude-test",
            api_key="project-secret",
        ),
        principal=principal,
    )
    return [
        update_tenant_worker(
            worker.id,
            UpdateTenantWorkerRequest(
                model_id=model.id,
                max_running=worker.max_running, priority=worker.priority,
                enabled=worker.enabled, expected_revision=worker.revision,
            ),
            principal=principal,
        )
        for worker in list_tenant_workers(tenant_id=principal.tenant_id)
    ]


def test_admin_foreign_project_preserves_target_tenant_bindings_and_attribution() -> None:
    admin = _principal("global-admin", role="super_admin")
    owner = _principal("foreign-owner")
    admin_worker = _worker(admin, "Admin Worker")
    owner_worker = _worker(owner, "Owner Worker")
    detail = create_project_record(
        CreateProjectRequest(
            title="Foreign project",
            origin="https://foreign.example.test",
            goal="Audit foreign tenant",
            scenario="goal-driven-v1",
            authorization_expires_at=None,
        ),
        tenant_id=owner.tenant_id,
        creator=owner.subject_id,
    )
    admin_detail = create_project_record(
        CreateProjectRequest(
            title="Admin project",
            origin="https://admin.example.test",
            goal="Audit admin tenant",
            scenario="goal-driven-v1",
            authorization_expires_at=None,
        ),
        tenant_id=admin.tenant_id,
        creator=admin.subject_id,
    )

    assert detail.project.scenario_snapshot["key"] == "goal-driven-v1"
    assert detail.project.scenario_snapshot["prompt_group"] == "goal-driven-v1"
    assert (
        detail.project.scenario_snapshot["features"]["assessment_protocol"]
        == "goal-driven-v1"
    )
    assert not {"report_policy_version", "report_prompt_group"} & detail.project.scenario_snapshot["features"].keys()
    assert detail.project.vulnerability_fingerprint_version == 1
    assert detail.project.assessment_protocol == "goal-driven-v1"
    assert "explore_budget" not in detail.project.model_dump()
    assert detail.project.assessment_progress is None
    assert "verification_reserve" not in detail.project.assessment_execution
    assert [item.id for item in list_projects(owner)] == [detail.project.id]
    summaries = list_projects(admin)
    assert {item.id for item in summaries} == {detail.project.id, admin_detail.project.id}
    summary = next(item for item in summaries if item.id == detail.project.id)
    assert summary.tenant_id == owner.tenant_id
    assert summary.tenant_name == "foreign-owner"
    assert summary.tenant_owner_login == "foreign-owner"
    assert "group_id" not in summary.model_dump()
    assert "group_name" not in summary.model_dump()

    assert {worker.id for worker in detail.project.tenant_workers} == {worker.id for worker in owner_worker}
    assert {worker.id for worker in admin_detail.project.tenant_workers} == {worker.id for worker in admin_worker}
    assert not {worker.id for worker in owner_worker} & {worker.id for worker in admin_worker}


def test_project_creation_requires_durable_worker_binding() -> None:
    principal = _principal("project-owner")
    workers = _worker(principal, "Configured")
    project = create_project_record(
        CreateProjectRequest(
            title="Bound project", origin="https://example.com", goal="Audit the target",
            scenario="goal-driven-v1", authorization_expires_at=None,
        ),
        tenant_id=principal.tenant_id,
    )
    assert project.project.status == "active"
    assert [item.id for item in project.project.tenant_workers] == [item.id for item in workers]
    assert project.project.preferred_workers == []
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT tenant_id, tenant_worker_id, tenant_worker_revision, position "
            "FROM project_tenant_workers WHERE project_id = ? ORDER BY position",
            (project.project.id,),
        ).fetchall()
        assert [dict(row) for row in rows] == [
            {"tenant_id": principal.tenant_id, "tenant_worker_id": worker.id,
             "tenant_worker_revision": worker.revision, "position": position}
            for position, worker in enumerate(workers)
        ]
        assert conn.execute(
            "SELECT preferred_workers_json FROM projects WHERE id = ?",
            (project.project.id,),
        ).fetchone()["preferred_workers_json"] == "[]"


def test_project_creation_includes_resources_in_detail_and_deletion_cleans_files(
    tmp_path: Path,
) -> None:
    principal = _principal("resource-owner")
    worker = _worker(principal, "Resource Worker")
    content = "  username=tester\npassword=allowed-secret\n"

    project = create_project_record(
        CreateProjectRequest(
            title="Resource project",
            origin="https://example.com",
            goal="Audit the target",
            scenario="goal-driven-v1",
            authorization_expires_at=None,
            resources=[
                CreateProjectResourceRequest(
                    kind="script",
                    path="scripts/login.py",
                    content=content,
                )
            ],
        ),
        tenant_id=principal.tenant_id,
        creator=principal.subject_id,
    )

    assert project.project.scenario_snapshot["knowledge"] is None
    assert len(project.resources) == 1
    resource = project.resources[0]
    assert resource.path == "scripts/login.py"
    assert resource.created_by == principal.subject_id
    stored_path = tmp_path / "project-resources" / project.project.id / resource.id
    assert stored_path.read_text(encoding="utf-8") == content

    detail = get_project_detail(project.project.id, tenant_id=principal.tenant_id)
    assert detail.resources == project.resources

    delete_project_record(project.project.id)
    assert not stored_path.parent.exists()


async def _resource_bytes(content: bytes):
    yield content


def test_project_creation_consumes_binary_resource_batch(tmp_path: Path) -> None:
    principal = _principal("batch-resource-owner")
    worker = _worker(principal, "Batch Resource Worker")
    batch = create_project_resource_batch(
        tenant_id=principal.tenant_id,
        user_id=principal.subject_id,
    )
    content = b"\x00\xffnew-project-resource"
    asyncio.run(
        upload_project_resource_batch_file(
            batch.id,
            _resource_bytes(content),
            path="payloads/input.bin",
            kind="file",
            tenant_id=principal.tenant_id,
            user_id=principal.subject_id,
        )
    )

    project = create_project_record(
        CreateProjectRequest(
            title="Batch resource project",
            origin="https://example.com",
            goal="Audit the target",
            scenario="goal-driven-v1",
            authorization_expires_at=None,
            resource_batch_id=batch.id,
        ),
        tenant_id=principal.tenant_id,
        creator=principal.subject_id,
    )

    assert [(resource.kind, resource.path) for resource in project.resources] == [
        ("file", "payloads/input.bin")
    ]
    stored = tmp_path / "project-resources" / project.project.id / project.resources[0].id
    assert stored.read_bytes() == content
    assert not (tmp_path / "project-resources" / ".batches" / batch.id).exists()
    with db.get_conn() as conn:
        batch_row = conn.execute(
            "SELECT state, consumed_project_id FROM project_resource_batches WHERE id = ?",
            (batch.id,),
        ).fetchone()
    assert dict(batch_row) == {
        "state": "consumed",
        "consumed_project_id": project.project.id,
    }


@pytest.mark.parametrize(("field", "value"), [
    ("explore_budget", 40), ("explore_budget", None),
    ("exploration_limit", "balanced"), ("verification_reserve", 8),
])
def test_create_project_rejects_removed_budget_fields_with_422(monkeypatch, field, value) -> None:
    from cairn.server.auth import protect_cookie_request

    principal = RequestPrincipal(kind="user", subject_id="fixture-user", tenant_id="fixture-tenant", role="user")
    monkeypatch.setitem(app.dependency_overrides, protect_cookie_request, lambda: principal)
    response = TestClient(app).post("/projects", json={
        "title": "Removed budget request", "origin": "授权本地测试", "goal": "验证创建边界",
        "authorization_expires_at": None, field: value,
    })
    assert response.status_code == 422
    assert any(
        error["loc"] == ["body", field] and error["type"] == "extra_forbidden"
        for error in response.json()["detail"]
    )
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"] == 0


def test_project_creation_requires_explicit_authorization_expiry_choice() -> None:
    with pytest.raises(ValidationError) as exc:
        CreateProjectRequest(
            title="Missing authorization choice",
            origin="https://example.com",
            goal="Audit the target",
            scenario="goal-driven-v1",
        )

    assert exc.value.errors()[0]["loc"] == ("authorization_expires_at",)
    assert exc.value.errors()[0]["type"] == "missing"


def test_project_creation_rejects_expired_authorization() -> None:
    principal = _principal("expired-authorization-owner")
    worker = _worker(principal, "Expired Authorization Worker")

    with pytest.raises(HTTPException) as exc:
        create_project_record(
            CreateProjectRequest(
                title="Expired authorization project",
                origin="https://example.com",
                goal="Audit the target",
                scenario="goal-driven-v1",
                authorization_expires_at="2000-01-01T00:00:00Z",
            ),
            tenant_id=principal.tenant_id,
            creator=principal.subject_id,
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == "authorization_expires_at_not_future"


@pytest.mark.parametrize("deadline", [None, "2099-01-01T00:00:00Z"])
def test_goal_driven_project_freezes_scenario_without_budget_and_keeps_optional_deadline(deadline) -> None:
    principal = _principal("snapshot-owner")
    worker = _worker(principal, "Snapshot Worker")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE scenarios SET features_json = ? WHERE key = 'goal-driven-v1'",
            (json.dumps({"sandbox": True, "exploration_limit": "thorough", "verification_reserve": 8}),),
        )

    project = create_project_record(
        CreateProjectRequest(
            title="Snapshot project",
            origin="已有接口说明和两个测试角色，关注订单审批相关功能",
            goal="检查普通用户之间的数据隔离及审批流程",
            scenario="goal-driven-v1",
            **({"explore_deadline": deadline} if deadline is not None else {}),
            authorization_expires_at=None,
        ),
        tenant_id=principal.tenant_id,
    )

    assert project.targets == []
    frozen = project.project.scenario_snapshot
    assert frozen["key"] == "goal-driven-v1"
    assert frozen["prompt_group"] == "goal-driven-v1"
    assert frozen["features"]["assessment_protocol"] == "goal-driven-v1"
    assert not {"report_policy_version", "report_prompt_group"} & frozen["features"].keys()
    assert frozen["features"]["execution_contract_version"] == 1
    assert frozen["features"]["execution_engine"] == "pi"
    assert frozen["features"]["assessment_contract_version"] == 2
    assert frozen["features"]["outcome_contract_version"] == 2
    assert frozen["features"]["verifier_contract_version"] == 1
    assert "blocker_cooldown_version" not in frozen["features"]
    assert "surface_manifest_version" not in frozen["features"]
    assert "minimum_coverage_version" not in frozen["features"]
    assert "quality_gate_version" not in frozen["features"]
    assert project.project.assessment_protocol == "goal-driven-v1"
    assert "explore_budget" not in project.project.model_dump()
    assert "verification_reserve" not in frozen["features"]
    assert "exploration_limit" not in frozen["features"]
    assert not {"budget", "verification_reserve", "remaining_total", "remaining_discovery"} & project.project.assessment_execution.keys()
    assert project.project.explore_deadline == deadline
    assert project.project.assessment_progress is None

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT scenario_snapshot_json, assessment_protocol, explore_budget, "
            "explore_deadline FROM projects WHERE id = ?",
            (project.project.id,),
        ).fetchone()
        assert json.loads(row["scenario_snapshot_json"]) == frozen
        assert row["assessment_protocol"] == "goal-driven-v1"
        assert row["explore_budget"] is None
        assert row["explore_deadline"] == deadline
        conn.execute(
            "UPDATE scenarios SET label = ?, prompt_group = ?, features_json = ? WHERE key = ?",
            (
                "Changed after creation",
                "default",
                json.dumps({"sandbox": True}),
                "goal-driven-v1",
            ),
        )

    reloaded = get_project_detail(project.project.id, tenant_id=principal.tenant_id)
    assert reloaded.project.scenario_snapshot == frozen
    assert reloaded.project.scenario_snapshot["key"] == "goal-driven-v1"
    assert reloaded.project.scenario_snapshot["prompt_group"] == "goal-driven-v1"
    assert not {"report_policy_version", "report_prompt_group"} & reloaded.project.scenario_snapshot["features"].keys()
    assert reloaded.project.assessment_protocol == "goal-driven-v1"
    assert "explore_budget" not in reloaded.project.model_dump()
    assert reloaded.project.explore_deadline == deadline
    assert reloaded.project.assessment_quality is None


def test_project_creation_does_not_borrow_other_tenant_configuration() -> None:
    admin = _principal("project-admin", role="super_admin")
    other = _principal("project-other")
    _worker(other, "Other Worker")
    with pytest.raises(HTTPException) as unavailable:
        create_project_record(
            CreateProjectRequest(title="No global fallback", origin="https://example.com",
                                 goal="Audit the target", authorization_expires_at=None),
            tenant_id=admin.tenant_id,
        )
    assert unavailable.value.status_code == 422
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM projects").fetchone()["count"] == 0


def test_project_creation_allows_configured_worker_without_dispatcher_heartbeat() -> None:
    principal = _principal("project-offline-dispatcher")
    worker = _worker(principal, "Configured")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tenant_workers_service, "dispatcher_executor_available", lambda conn: False)
    try:
        project = create_project_record(
            CreateProjectRequest(
                title="Configured while offline",
                origin="https://example.com",
                goal="Audit the target",
                scenario="goal-driven-v1",
                authorization_expires_at=None,
            ),
            tenant_id=principal.tenant_id,
        )
    finally:
        monkey.undo()

    assert [item.id for item in project.project.tenant_workers] == [item.id for item in worker]
    with db.get_conn() as conn:
        binding = conn.execute(
            "SELECT tenant_worker_id FROM project_tenant_workers WHERE project_id = ?",
            (project.project.id,),
        ).fetchone()
    assert binding["tenant_worker_id"] in {item.id for item in worker}


def test_incomplete_worker_fails_before_project_insert() -> None:
    principal = _principal("project-incomplete")
    model = create_tenant_model(
        CreateTenantModelRequest(
            display_name="Incomplete Project Model",
            protocol="anthropic-messages",
            base_url="https://api.example.test",
            model_name="claude-test",
        ),
        principal=principal,
    )
    worker = list_tenant_workers(tenant_id=principal.tenant_id)[0]
    update_tenant_worker(
        worker.id, UpdateTenantWorkerRequest(
            model_id=model.id,
            max_running=worker.max_running, priority=worker.priority, enabled=worker.enabled,
            expected_revision=worker.revision,
        ), principal=principal,
    )

    with pytest.raises(HTTPException) as unavailable:
        create_project_record(
            CreateProjectRequest(
                title="Unavailable",
                origin="https://example.com",
                goal="Audit the target",
                scenario="goal-driven-v1",
                authorization_expires_at=None,
            ),
            tenant_id=principal.tenant_id,
        )
    assert unavailable.value.status_code == 422
    assert "credential_missing" in unavailable.value.detail
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM projects").fetchone()["count"] == 0


def test_new_database_omits_scenario_admission_columns_but_keeps_connectivity_table() -> None:
    with db.get_conn() as conn:
        columns = {row["column_name"] for row in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'scenarios'").fetchall()}
        assert "required_task_types_json" not in columns
        assert "worker_preflight_json" not in columns
        assert conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = 'dispatcher_connectivity_tests'"
        ).fetchone() is not None


def test_existing_extra_scenario_columns_are_ignored_without_being_dropped() -> None:
    with db.get_conn() as conn:
        conn.execute(
            "ALTER TABLE scenarios ADD COLUMN required_task_types_json "
            "TEXT NOT NULL DEFAULT '[\"bootstrap\",\"reason\",\"explore\"]'"
        )
        conn.execute("ALTER TABLE scenarios ADD COLUMN worker_preflight_json TEXT NOT NULL DEFAULT '{}'")
        conn.execute(
            "UPDATE scenarios SET required_task_types_json = '[\"reason\"]', "
            "worker_preflight_json = '{\"min_candidates\":3}' WHERE key = 'goal-driven-v1'"
        )
        scenarios = list_scenarios(conn)
        updated = [
            scenario.model_copy(update={"label": "更新后的默认探索"})
            if scenario.key == "goal-driven-v1"
            else scenario
            for scenario in scenarios
        ]
        replace_scenarios(conn, updated)

        row = conn.execute(
            "SELECT label, required_task_types_json, worker_preflight_json "
            "FROM scenarios WHERE key = 'goal-driven-v1'"
        ).fetchone()
        assert row["label"] == "更新后的默认探索"
        assert row["required_task_types_json"] == '["reason"]'
        assert row["worker_preflight_json"] == '{"min_candidates":3}'
        columns = {item["column_name"] for item in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'scenarios'").fetchall()}
        assert {"required_task_types_json", "worker_preflight_json"} <= columns


def test_internal_verification_project_keeps_single_dedicated_worker() -> None:
    from cairn.server.schemas.verification_requests import UpdateVerificationConfiguration
    from cairn.server.services.project_records import _VerificationProjectRequest, create_verification_project_record
    from cairn.server.services.verification_configuration import update_verification_configuration

    principal = _principal("verification-only")
    model = create_tenant_model(
        CreateTenantModelRequest(
            display_name="Verification Model", protocol="anthropic-messages",
            base_url="https://api.example.test", model_name="fixture-model", api_key="fixture-key",
        ), principal=principal,
    )
    configuration = update_verification_configuration(
        UpdateVerificationConfiguration(model_id=model.id), principal,
    )
    worker_id = configuration["tenant_worker_id"]
    with pytest.raises(ValidationError):
        _VerificationProjectRequest(title="Invalid", origin="授权复测", authorization_expires_at=None,
                                    tenant_worker_ids=[worker_id, worker_id])
    # 普通三类仍无模型；内部复测不能依赖普通配置，也不能被替换为三条普通绑定。
    with db.get_conn() as conn:
        detail = create_verification_project_record(
            conn, title="Dedicated verification", origin="仅验证来源主张",
            tenant_id=principal.tenant_id, tenant_worker_id=worker_id, creator=principal.subject_id,
            actor_type="user", execution_deadline=None, authorization_expires_at=None,
        )
        bindings = conn.execute(
            "SELECT tenant_worker_id FROM project_tenant_workers WHERE project_id=?", (detail.project.id,),
        ).fetchall()
    assert detail.project.status == "pending"
    assert [binding["tenant_worker_id"] for binding in bindings] == [worker_id]


def test_legacy_scenario_admission_api_is_not_exposed() -> None:
    app.openapi_schema = None
    paths = app.openapi()["paths"]
    assert "/scenarios/{scenario_key}/availability" not in paths
