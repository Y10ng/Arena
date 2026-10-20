"""项目绑定必须穿过真实 HTTP 和 Dispatcher 解码后仍可选择。"""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.scheduler.worker_runtime import WorkerRuntimeMixin
from cairn.server import db
from cairn.server.auth import protect_cookie_request, require_dispatcher_protocol
from cairn.server.routers import projects
from cairn.server.schemas.projects import ProjectWorkerBinding
from test_project_creation_workers import configured_database, _principal, _worker


@pytest.mark.parametrize("engine", ["pi", "legacy-engine"])
def test_project_http_binding_reaches_real_worker_selection(monkeypatch, engine):
    principal = _principal("wire-owner")
    worker = _worker(principal, "Wire Worker")
    app = FastAPI()
    app.include_router(projects.router)
    app.dependency_overrides[protect_cookie_request] = lambda: principal
    app.dependency_overrides[require_dispatcher_protocol] = lambda: principal
    with TestClient(app) as http:
        created = http.post("/projects", json={
            "title": "Synthetic wire project",
            "origin": "https://example.test",
            "goal": "Synthetic authorization fixture",
            "scenario": "default",
            "tenant_worker_ids": [worker.id],
        })
        assert created.status_code == 201, created.text
        project_id = created.json()["project"]["id"]
        with db.get_conn() as conn:
            conn.execute("UPDATE tenant_workers SET engine = ? WHERE id = ?", (engine, worker.id))
        client = CairnClient("http://testserver", token="synthetic-test-only")
        monkeypatch.setattr(client, "_session", lambda: http)
        detail = client.get_project(project_id)
        binding = detail.project.tenant_workers[0]
        assert binding.engine == engine
        runtime = WorkerRuntimeMixin()
        runtime._worker_counts = lambda: {}
        runtime.worker_cooldown_until = {}
        runtime.worker_rejected_until = {}
        selection = runtime._select_worker(
            project_id, "bootstrap", SimpleNamespace(key="default"), detail.project.tenant_workers,
        )
        if engine == "pi":
            assert selection.worker is not None
            assert selection.worker.id == worker.id
        else:
            assert selection.worker is None
            assert selection.blocked_rejected[0]["reason"] == "engine_unsupported"
            assert binding.unavailable_reason == "engine_unsupported"
        client.close()


def test_missing_binding_engine_is_not_assumed_pi():
    binding = ProjectWorkerBinding.model_validate({
        "id": "historical-worker", "display_name": "Historical",
        "revision_at_bind": 1, "task_types": ["bootstrap"], "currently_available": True,
    })
    runtime = WorkerRuntimeMixin()
    runtime._worker_counts = lambda: {}
    runtime.worker_cooldown_until = {}
    runtime.worker_rejected_until = {}
    selection = runtime._select_worker("synthetic", "bootstrap", SimpleNamespace(key="default"), [binding])
    assert selection.worker is None
    assert selection.blocked_rejected[0]["reason"] == "engine_unsupported"
