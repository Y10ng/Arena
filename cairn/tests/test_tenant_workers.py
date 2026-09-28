"""Tenant Worker service 的隔离、Model 绑定与 revision 测试。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi import HTTPException
import pytest
from pydantic import ValidationError

from cairn.server import db
from cairn.server.auth import RequestPrincipal, create_user_with_personal_tenant
from cairn.server.schemas.models import CreateTenantModelRequest
from cairn.server.schemas.tenant_workers import CreateTenantWorkerRequest, UpdateTenantWorkerRequest
from cairn.server.services import model_credentials
from cairn.server.services.tenant_models import create_tenant_model, update_tenant_model
from cairn.server.schemas.models import UpdateTenantModelRequest
from cairn.server.services.time_utils import utcnow
from cairn.server.services.tenant_workers import (
    create_tenant_worker,
    delete_tenant_worker,
    get_tenant_worker,
    list_tenant_workers,
    resolve_project_worker_execution_binding,
    update_tenant_worker,
    validate_tenant_worker_selection,
)


@pytest.fixture(autouse=True)
def configured_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db._reset_for_tests()
    db.configure(tmp_path / "tenant-workers.db")
    keyring = tmp_path / "keyring.json"
    keyring.write_text(
        json.dumps({"active_version": "key-v1", "keys": {"key-v1": base64.b64encode(b"w" * 32).decode()}}),
        encoding="utf-8",
    )
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    yield
    db._reset_for_tests()


def _principal(login: str, role: str = "user") -> RequestPrincipal:
    user_id, tenant_id = create_user_with_personal_tenant(login, "safe password", role=role)
    return RequestPrincipal(kind="user", subject_id=user_id, tenant_id=tenant_id, role=role)


def _model(
    principal: RequestPrincipal,
    protocol: str = "anthropic-messages",
    *,
    api_key: str | None = None,
):
    return create_tenant_model(
        CreateTenantModelRequest(
            display_name=f"{protocol.title()} Test Model",
            protocol=protocol,
            base_url="https://api.example.test",
            model_name="model-test",
            api_key=api_key,
        ),
        principal=principal,
    )


def test_worker_crud_is_tenant_scoped_and_engine_is_fixed() -> None:
    tenant_a = _principal("worker-a")
    tenant_b = _principal("worker-b")
    model = _model(tenant_a)
    created = create_tenant_worker(
        CreateTenantWorkerRequest(display_name="Primary", model_id=model.id),
        principal=tenant_a,
    )

    assert created.engine == "pi"
    assert created.unavailable_reason == "credential_missing"
    assert [item.id for item in list_tenant_workers(tenant_id=tenant_a.tenant_id)] == [created.id]
    assert list_tenant_workers(tenant_id=tenant_b.tenant_id) == []
    with pytest.raises(HTTPException) as hidden:
        get_tenant_worker(created.id, tenant_id=tenant_b.tenant_id)
    assert hidden.value.status_code == 404


@pytest.mark.parametrize("protocol", ["anthropic-messages", "openai-chat-completions", "openai-responses"])
def test_all_platform_model_protocols_can_be_bound_but_cross_tenant_models_cannot(protocol: str) -> None:
    tenant_a = _principal("binding-a")
    tenant_b = _principal("binding-b")
    model = _model(tenant_a, protocol, api_key="binding-test-key")
    foreign = _model(tenant_b)

    worker = create_tenant_worker(
        CreateTenantWorkerRequest(display_name="Unified pi", model_id=model.id), principal=tenant_a,
    )
    assert worker.engine == "pi"
    assert worker.available is True
    with pytest.raises(HTTPException) as hidden:
        create_tenant_worker(CreateTenantWorkerRequest(display_name="Foreign", model_id=foreign.id), principal=tenant_a)
    assert hidden.value.status_code == 404


def test_existing_pi_worker_remains_available_when_model_changes_to_supported_protocol() -> None:
    principal = _principal("binding-protocol-change")
    model = _model(principal, api_key="protocol-secret")
    worker = create_tenant_worker(
        CreateTenantWorkerRequest(display_name="Stable binding", model_id=model.id),
        principal=principal,
    )

    updated_model = update_tenant_model(
        model.id,
        UpdateTenantModelRequest(
            display_name=model.display_name,
            protocol="openai-responses",
            base_url=model.base_url,
            auth_header=model.auth_header,
            custom_headers=model.custom_headers,
            models_endpoint=model.models_endpoint,
            inference_endpoint=model.inference_endpoint,
            model_name=model.model_name,
            enabled=model.enabled,
            expected_revision=model.revision,
        ),
        principal=principal,
    )
    refreshed = get_tenant_worker(worker.id, tenant_id=principal.tenant_id)

    assert refreshed.model_id == updated_model.id == model.id
    assert refreshed.engine == "pi"
    assert refreshed.available is True
    assert refreshed.unavailable_reason is None


def test_revision_conflict_and_safe_audit() -> None:
    principal = _principal("worker-revision")
    model = _model(principal, api_key="worker-known-secret")
    worker = create_tenant_worker(CreateTenantWorkerRequest(display_name="Worker", model_id=model.id), principal=principal)
    updated = update_tenant_worker(
        worker.id,
        UpdateTenantWorkerRequest(
            display_name="Worker 2",
            model_id=model.id,
            task_types=["bootstrap", "explore"],
            max_running=2,
            priority=50,
            enabled=True,
            expected_revision=worker.revision,
        ),
        principal=principal,
    )
    assert updated.revision == 2

    with pytest.raises(HTTPException) as conflict:
        delete_tenant_worker(worker.id, expected_revision=worker.revision, principal=principal)
    assert conflict.value.status_code == 409
    with db.get_conn() as conn:
        metadata = [row["metadata_json"] for row in conn.execute("SELECT metadata_json FROM security_audit_events").fetchall()]
    assert "worker-known-secret" not in json.dumps(metadata)

    delete_tenant_worker(worker.id, expected_revision=updated.revision, principal=principal)


def test_project_execution_binding_is_scoped_and_secret_is_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = _principal("worker-execution")
    model = create_tenant_model(
        CreateTenantModelRequest(
            display_name="Anthropic Messages Test Model",
            protocol="anthropic-messages",
            base_url="https://api.example.test",
            auth_header="x-api-key",
            custom_headers=[{"name": "X-Tenant", "value": "tenant-a"}],
            inference_endpoint="/gateway/v1/messages",
            model_name="model-test",
            api_key="execution-secret",
        ),
        principal=principal,
    )
    monkeypatch.setattr(
        "cairn.server.services.tenant_workers.dispatcher_executor_available",
        lambda conn: True,
    )
    worker = create_tenant_worker(
        CreateTenantWorkerRequest(display_name="Execution", model_id=model.id),
        principal=principal,
    )
    assert worker.available is True
    assert worker.executor_available is True
    assert worker.model_display_name == "Anthropic Messages Test Model"
    assert worker.model_name == "model-test"
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, scenario_key, preferred_workers_json, tenant_id) "
            "VALUES ('proj_exec', 'Execution', 'active', '2026-08-13T00:00:00Z', 'default', '[]', ?)",
            (principal.tenant_id,),
        )
        conn.execute(
            "INSERT INTO project_tenant_workers "
            "(project_id, tenant_id, tenant_worker_id, tenant_worker_revision, position, created_at) "
            "VALUES ('proj_exec', ?, ?, ?, 0, '2026-08-13T00:00:00Z')",
            (principal.tenant_id, worker.id, worker.revision),
        )

    renamed_model = update_tenant_model(
        model.id,
        UpdateTenantModelRequest(
            display_name="Renamed Execution Model",
            protocol=model.protocol,
            base_url=model.base_url,
            auth_header=model.auth_header,
            custom_headers=model.custom_headers,
            models_endpoint=model.models_endpoint,
            inference_endpoint=model.inference_endpoint,
            model_name=model.model_name,
            enabled=model.enabled,
            expected_revision=model.revision,
        ),
        principal=principal,
    )
    refreshed_worker = get_tenant_worker(worker.id, tenant_id=principal.tenant_id)
    assert refreshed_worker.model_display_name == renamed_model.display_name
    assert refreshed_worker.model_name == "model-test"

    binding = resolve_project_worker_execution_binding(
        "proj_exec",
        worker.id,
        task_type="reason",
    )
    assert binding.worker_id == worker.id
    assert binding.model_revision == renamed_model.revision
    assert binding.model_name == "model-test"
    assert binding.auth_header == "x-api-key"
    assert [header.model_dump() for header in binding.custom_headers] == [
        {"name": "X-Tenant", "value": "tenant-a"}
    ]
    assert binding.engine == "pi"
    assert binding.protocol == "anthropic-messages"
    assert binding.base_url == "https://api.example.test"
    assert binding.inference_endpoint == "https://api.example.test/gateway/v1/messages"
    assert "messages_endpoint" not in binding.model_dump()
    assert binding.worker_revision == worker.revision
    assert binding.credential_version_id
    assert binding.credential_version == 1
    assert binding.credential_fingerprint
    assert binding.task_types == worker.task_types
    assert binding.max_running == worker.max_running
    assert binding.priority == worker.priority
    assert binding.api_key == "execution-secret"
    assert "execution-secret" not in repr(binding)

    custom_endpoint_model = update_tenant_model(
        model.id,
        UpdateTenantModelRequest(
            display_name=renamed_model.display_name,
            protocol=renamed_model.protocol,
            base_url=renamed_model.base_url,
            auth_header=renamed_model.auth_header,
            custom_headers=renamed_model.custom_headers,
            models_endpoint=renamed_model.models_endpoint,
            inference_endpoint="/custom/generate",
            model_name=renamed_model.model_name,
            enabled=renamed_model.enabled,
            expected_revision=renamed_model.revision,
        ),
        principal=principal,
    )
    assert custom_endpoint_model.revision == renamed_model.revision + 1
    custom_binding = resolve_project_worker_execution_binding("proj_exec", worker.id, task_type="reason")
    assert custom_binding.inference_endpoint == "https://api.example.test/custom/generate"
    assert custom_binding.base_url == model.base_url
    assert custom_binding.model_revision == custom_endpoint_model.revision

    with pytest.raises(HTTPException) as unbound:
        resolve_project_worker_execution_binding(
            "proj_missing",
            worker.id,
            task_type="reason",
        )
    assert unbound.value.status_code == 404
    with db.get_conn() as conn:
        serialized = json.dumps([dict(row) for row in conn.execute("SELECT * FROM task_runs").fetchall()])
        serialized += json.dumps([dict(row) for row in conn.execute("SELECT * FROM security_audit_events").fetchall()])
    assert "execution-secret" not in serialized


def test_completed_and_stopped_projects_allow_report_binding_but_reject_reason() -> None:
    principal = _principal("worker-completed-report")
    model = _model(principal, api_key="completed-report-secret")
    worker = create_tenant_worker(
        CreateTenantWorkerRequest(display_name="Report", model_id=model.id),
        principal=principal,
    )
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, scenario_key, preferred_workers_json, tenant_id) "
            "VALUES ('proj_report', 'Report', 'completed', '2026-08-19T00:00:00Z', 'default', '[]', ?)",
            (principal.tenant_id,),
        )
        conn.execute(
            "INSERT INTO project_tenant_workers "
            "(project_id, tenant_id, tenant_worker_id, tenant_worker_revision, position, created_at) "
            "VALUES ('proj_report', ?, ?, ?, 0, '2026-08-19T00:00:00Z')",
            (principal.tenant_id, worker.id, worker.revision),
        )
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, scenario_key, preferred_workers_json, tenant_id) "
            "VALUES ('proj_stopped_report', 'Stopped Report', 'stopped', '2026-08-19T00:00:00Z', 'default', '[]', ?)",
            (principal.tenant_id,),
        )
        conn.execute(
            "INSERT INTO project_tenant_workers "
            "(project_id, tenant_id, tenant_worker_id, tenant_worker_revision, position, created_at) "
            "VALUES ('proj_stopped_report', ?, ?, ?, 0, '2026-08-19T00:00:00Z')",
            (principal.tenant_id, worker.id, worker.revision),
        )

    binding = resolve_project_worker_execution_binding(
        "proj_report",
        worker.id,
        task_type="report",
    )
    assert binding.worker_id == worker.id
    assert "completed-report-secret" not in repr(binding)

    stopped_binding = resolve_project_worker_execution_binding(
        "proj_stopped_report",
        worker.id,
        task_type="report",
    )
    assert stopped_binding.worker_id == worker.id

    with pytest.raises(HTTPException) as rejected:
        resolve_project_worker_execution_binding(
            "proj_report",
            worker.id,
            task_type="reason",
        )
    assert rejected.value.status_code == 409
    assert rejected.value.detail == "Project is not active"


def _bind_project(principal: RequestPrincipal, worker, project_id: str = "proj_binding") -> str:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, scenario_key, preferred_workers_json, tenant_id) "
            "VALUES (?, 'Binding', 'active', '2026-09-16T00:00:00Z', 'default', '[]', ?)",
            (project_id, principal.tenant_id),
        )
        conn.execute(
            "INSERT INTO project_tenant_workers "
            "(project_id, tenant_id, tenant_worker_id, tenant_worker_revision, position, created_at) "
            "VALUES (?, ?, ?, ?, 0, '2026-09-16T00:00:00Z')",
            (project_id, principal.tenant_id, worker.id, worker.revision),
        )
    return project_id


@pytest.mark.parametrize("engine", ["claudecode", "codex", "mock", "future-engine"])
def test_legacy_engines_remain_readable_but_cannot_be_selected_updated_or_executed(
    engine: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = _principal("legacy-worker")
    model = _model(principal, api_key="legacy-test-key")
    worker = create_tenant_worker(
        CreateTenantWorkerRequest(display_name="Historical", model_id=model.id), principal=principal,
    )
    project_id = _bind_project(principal, worker)
    with db.get_conn() as conn:
        conn.execute("UPDATE tenant_workers SET engine = ? WHERE id = ?", (engine, worker.id))

    def unexpected_decryption(*args, **kwargs):
        pytest.fail("旧引擎不可进入 credential 解密阶段")

    monkeypatch.setattr("cairn.server.services.tenant_workers.decrypt_active_credential", unexpected_decryption)
    historical = get_tenant_worker(worker.id, tenant_id=principal.tenant_id)
    assert historical.engine == engine
    assert historical.available is False
    assert historical.unavailable_reason == "engine_unsupported"
    assert list_tenant_workers(tenant_id=principal.tenant_id)[0].engine == engine
    with db.get_conn() as conn, pytest.raises(HTTPException, match="engine_unsupported"):
        validate_tenant_worker_selection(conn, tenant_id=principal.tenant_id, worker_ids=[worker.id])
    with pytest.raises(HTTPException, match="engine_unsupported") as rejected:
        resolve_project_worker_execution_binding(project_id, worker.id, task_type="reason")
    assert rejected.value.status_code == 422
    with pytest.raises(HTTPException, match="engine_unsupported"):
        update_tenant_worker(
            worker.id,
            UpdateTenantWorkerRequest(
                display_name="Must not convert", model_id=model.id, expected_revision=worker.revision,
            ),
            principal=principal,
        )
    unchanged = get_tenant_worker(worker.id, tenant_id=principal.tenant_id)
    assert unchanged.engine == engine
    assert unchanged.revision == worker.revision


@pytest.mark.parametrize("engine", ["claudecode", "codex", "mock", "future-engine"])
def test_new_worker_requests_reject_non_pi_engines(engine: str) -> None:
    with pytest.raises(ValidationError):
        CreateTenantWorkerRequest(display_name="Invalid", model_id="model-id", engine=engine)


@pytest.mark.parametrize(
    ("protocol", "default_endpoint"),
    [
        ("anthropic-messages", "messages"),
        ("openai-chat-completions", "chat/completions"),
        ("openai-responses", "responses"),
    ],
)
@pytest.mark.parametrize("auth_header", ["authorization-bearer", "x-api-key"])
@pytest.mark.parametrize("override", [None, "/custom/infer?api-version=test", "https://gateway.example.test/generate?route=test"])
def test_execution_binding_preserves_protocol_endpoint_headers_and_scheduling(
    protocol: str, default_endpoint: str, auth_header: str, override: str | None,
) -> None:
    principal = _principal("binding-contract")
    model = create_tenant_model(
        CreateTenantModelRequest(
            display_name="Protocol fixture", protocol=protocol,
            base_url="https://api.example.test/proxy/v1", inference_endpoint=override,
            model_name="model-fixture", auth_header=auth_header,
            custom_headers=[{"name": "X-Route", "value": " preserve-spacing "}],
            api_key="binding-fixture-key",
        ),
        principal=principal,
    )
    worker = create_tenant_worker(
        CreateTenantWorkerRequest(
            display_name="Protocol worker", model_id=model.id,
            task_types=["reason", "explore"], max_running=7, priority=-12,
        ),
        principal=principal,
    )
    project_id = _bind_project(principal, worker)
    binding = resolve_project_worker_execution_binding(project_id, worker.id, task_type="reason")
    expected_endpoint = (
        f"{model.base_url}/{default_endpoint}" if override is None
        else override if override.startswith("https://")
        else f"{model.base_url}{override}"
    )
    assert binding.engine == "pi"
    assert binding.protocol == protocol
    assert binding.base_url == model.base_url
    assert binding.inference_endpoint == expected_endpoint
    assert binding.model_name == model.model_name
    assert binding.model_revision == model.revision
    assert binding.worker_revision == worker.revision
    assert binding.auth_header == auth_header
    assert binding.custom_headers == model.custom_headers
    assert binding.credential_version == model.credential_version
    assert binding.credential_version_id and binding.credential_fingerprint
    assert binding.task_types == ["reason", "explore"]
    assert binding.max_running == 7
    assert binding.priority == -12
    assert binding.api_key == "binding-fixture-key"
    assert "binding-fixture-key" not in repr(binding)
    assert "messages_endpoint" not in binding.model_dump()
    with pytest.raises(HTTPException, match="does not support"):
        resolve_project_worker_execution_binding(project_id, worker.id, task_type="bootstrap")
    update_tenant_worker(
        worker.id,
        UpdateTenantWorkerRequest(
            display_name=worker.display_name, model_id=model.id, expected_revision=worker.revision,
        ),
        principal=principal,
    )
    with pytest.raises(HTTPException, match="revision is stale"):
        resolve_project_worker_execution_binding(project_id, worker.id, task_type="reason")


def test_worker_runtime_status_uses_dispatcher_heartbeat_not_static_pool() -> None:
    principal = _principal("worker-heartbeat")
    model = _model(principal, api_key="heartbeat-secret")
    worker = create_tenant_worker(
        CreateTenantWorkerRequest(display_name="Heartbeat", model_id=model.id),
        principal=principal,
    )
    assert worker.executor_available is False

    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO dispatcher_status
                (id, status, worker_pool_json, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            ("dispatcher-tenant", "idle", "[]", utcnow()),
        )

    refreshed = get_tenant_worker(worker.id, tenant_id=principal.tenant_id)
    assert refreshed.available is True
    assert refreshed.executor_available is True


def test_worker_availability_tracks_model_configuration() -> None:
    principal = _principal("worker-state")
    model = create_tenant_model(
        CreateTenantModelRequest(
            display_name="Incomplete Worker Model",
            protocol="anthropic-messages",
            base_url="https://api.example.test",
        ),
        principal=principal,
    )
    worker = create_tenant_worker(CreateTenantWorkerRequest(display_name="State", model_id=model.id), principal=principal)
    assert worker.unavailable_reason == "model_incomplete"

    model = update_tenant_model(
        model.id,
        UpdateTenantModelRequest(
            display_name=model.display_name,
            protocol="anthropic-messages",
            base_url=model.base_url,
            model_name="claude-test",
            enabled=True,
            expected_revision=model.revision,
            api_key="secret",
        ),
        principal=principal,
    )
    refreshed = get_tenant_worker(worker.id, tenant_id=principal.tenant_id)
    assert refreshed.available is True
    assert refreshed.unavailable_reason is None
    assert refreshed.executor_available is False
