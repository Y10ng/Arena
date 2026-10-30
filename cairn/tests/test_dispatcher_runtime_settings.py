from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
import yaml

from cairn.server.schemas.dispatcher_runtime_settings import DispatcherRuntimeSettingsUpdate
from cairn.server.services.dispatcher_config_store import DispatcherConfigStore
from cairn.server.services.dispatcher_runtime_settings import read_runtime_settings, write_runtime_settings


ROOT = Path(__file__).resolve().parents[2]


class ReadOnlyRuntimeRows:
    """只提供此接口的两条 SELECT，任何项目维护或写库都会失败。"""

    def __init__(self):
        self.heartbeat = None
        self.leases = {"intent_timeout": 60, "reason_timeout": 60}

    def execute(self, query):
        if "FROM dispatcher_status" in query and query.lstrip().startswith("SELECT"):
            self.result = self.heartbeat
        elif query == "SELECT intent_timeout, reason_timeout FROM settings WHERE id = 1":
            self.result = self.leases
        else:
            pytest.fail(f"运行设置不应调用其他数据库操作: {query}")
        return self

    def fetchone(self):
        return self.result


def configured_copy(tmp_path, monkeypatch):
    path = tmp_path / "dispatch.yaml"
    config = yaml.safe_load((ROOT / "deploy/dispatch.yaml.example").read_text())
    config["common_env"] = {"SYNTHETIC_KEY": "synthetic-secret-do-not-return"}
    config["runtime"]["execution_checkpoints"] = "stage"
    config["container"]["dns"] = ["192.0.2.53"]
    config["tasks"]["report"]["max_output_tokens"] = 8192
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    monkeypatch.setenv("CAIRN_DISPATCH_CONFIG", str(path))
    monkeypatch.setenv("CAIRN_DISPATCH_YAML_ALLOWED_ROOTS", str(tmp_path))
    return path, config, ReadOnlyRuntimeRows()


def heartbeat(path, revision, **overrides):
    return {
        "server_reachable_path": str(path), "config_path": str(path),
        "updated_at": datetime.now(timezone.utc).isoformat(), "status": "idle",
        "runtime_json": json.dumps({"hard_cap": 16}),
        "config_hash": revision, "config_sha256": revision, "error_message": None,
        **overrides,
    }


def test_narrow_patch_preserves_full_configuration_and_reports_loading(tmp_path, monkeypatch):
    path, original, conn = configured_copy(tmp_path, monkeypatch)
    initial = read_runtime_settings(conn)
    assert initial.load_status.state == "unknown"
    assert "synthetic-secret" not in initial.model_dump_json()
    assert "execution_profiles" not in initial.model_dump()
    conn.heartbeat = heartbeat(path, initial.revision)
    assert read_runtime_settings(conn).load_status.state == "loaded"
    saved = write_runtime_settings(conn, DispatcherRuntimeSettingsUpdate.model_validate({
        "revision": initial.revision,
        "runtime": {"max_workers": 10, "max_project_workers": 5},
        "tasks": {"bootstrap": {"timeout": 720}, "report": {"timeout": 800}},
    }))
    expected = deepcopy(original)
    expected["runtime"].update(max_workers=10, max_project_workers=5)
    expected["tasks"]["bootstrap"]["timeout"] = 720
    expected["tasks"]["report"]["timeout"] = 800
    assert yaml.safe_load(path.read_text()) == expected
    assert saved.load_status.state == "pending"
    assert saved.load_status.hard_cap == 16
    assert "synthetic-secret" not in saved.model_dump_json()
    conn.heartbeat = heartbeat(path, saved.revision)
    assert read_runtime_settings(conn).load_status.state == "loaded"


def test_rejected_patch_does_not_replace_configuration(tmp_path, monkeypatch):
    path, _original, conn = configured_copy(tmp_path, monkeypatch)
    initial = read_runtime_settings(conn)
    conn.heartbeat = heartbeat(path, initial.revision)
    original_bytes = path.read_bytes()
    for runtime, reason in [
        ({"max_project_workers": 9}, "max_project_workers"),
        ({"max_workers": 17}, "max_workers"),
        ({"interval": 60}, "interval"),
    ]:
        with pytest.raises(HTTPException) as caught:
            write_runtime_settings(conn, DispatcherRuntimeSettingsUpdate(
                revision=initial.revision, runtime=runtime,
            ))
        assert caught.value.status_code == 422
        assert reason in caught.value.detail
        assert "synthetic-secret" not in caught.value.detail
        assert path.read_bytes() == original_bytes
    with pytest.raises(HTTPException) as caught:
        write_runtime_settings(conn, DispatcherRuntimeSettingsUpdate(
            revision="stale", runtime={"interval": 4},
        ))
    assert caught.value.status_code == 409
    assert path.read_bytes() == original_bytes
    for invalid in [
        {"runtime": {"max_workers": 0}},
        {"runtime": {"max_workers": True}},
        {"runtime": {"max_workers": None}},
        {"runtime": None}, {"tasks": {"explore": None}},
        {"tasks": {"explore": {"timeout": "synthetic-secret"}}},
        {"execution_profiles": []}, {"runtime": {"prompt_group": "other"}},
    ]:
        with pytest.raises(ValidationError):
            DispatcherRuntimeSettingsUpdate.model_validate({"revision": initial.revision, **invalid})
        assert path.read_bytes() == original_bytes


def test_load_status_uses_matching_fresh_heartbeat_without_exposing_error(tmp_path, monkeypatch):
    path, _original, conn = configured_copy(tmp_path, monkeypatch)
    revision = read_runtime_settings(conn).revision
    conn.heartbeat = heartbeat(path, "old", error_message="config_invalid")
    assert read_runtime_settings(conn).load_status.state == "failed"
    conn.heartbeat["error_message"] = "synthetic-secret-do-not-return"
    result = read_runtime_settings(conn)
    assert result.load_status.state == "pending"
    assert "synthetic-secret" not in result.model_dump_json()
    conn.heartbeat = heartbeat(path, revision, updated_at=(
        datetime.now(timezone.utc) - timedelta(seconds=90)
    ).isoformat())
    result = read_runtime_settings(conn)
    assert result.load_status.state == "unknown"
    assert result.load_status.hard_cap is None
    conn.heartbeat = heartbeat(path, revision, server_reachable_path=str(path.parent / "other.yaml"))
    assert read_runtime_settings(conn).load_status.state == "unknown"
    conn.heartbeat = heartbeat(path, revision, status="stopped")
    assert read_runtime_settings(conn).load_status.state == "unknown"


def test_full_resource_validation_is_kept_and_errors_do_not_reflect_secrets(tmp_path, monkeypatch):
    path, original, conn = configured_copy(tmp_path, monkeypatch)
    original["execution_profiles"][-1]["pids_limit"] = "synthetic-secret-do-not-return"
    path.write_text(yaml.safe_dump(original))
    before = path.read_bytes()
    revision = DispatcherConfigStore(path).read()[2]
    with pytest.raises(HTTPException) as caught:
        write_runtime_settings(conn, DispatcherRuntimeSettingsUpdate(
            revision=revision, runtime={"max_workers": 10},
        ))
    assert caught.value.status_code == 422
    assert "execution_profiles.2.pids_limit" in caught.value.detail
    assert "synthetic-secret" not in caught.value.detail
    assert path.read_bytes() == before


def test_runtime_settings_http_projection_and_admin_boundary(tmp_path, monkeypatch):
    from cairn.server.app import request_validation_error
    from cairn.server.auth import RequestPrincipal, protect_cookie_request
    from cairn.server.routers import infra

    path, _original, conn = configured_copy(tmp_path, monkeypatch)

    @contextmanager
    def get_conn():
        yield conn

    monkeypatch.setattr(infra, "get_conn", get_conn)
    app = FastAPI()
    app.include_router(infra.router)
    app.add_exception_handler(RequestValidationError, request_validation_error)
    principal = RequestPrincipal(kind="user", subject_id="synthetic", role="user")
    app.dependency_overrides[protect_cookie_request] = lambda: principal
    client = TestClient(app)
    assert client.get("/dispatcher/runtime-settings").status_code == 403
    assert client.patch("/dispatcher/runtime-settings", json={}).status_code == 403
    principal = RequestPrincipal(kind="user", subject_id="synthetic", role="super_admin")
    response = client.get("/dispatcher/runtime-settings")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"revision", "runtime", "tasks", "load_status"}
    before = path.read_bytes()
    invalid = client.patch("/dispatcher/runtime-settings", json={
        "revision": payload["revision"],
        "runtime": {"max_workers": "synthetic-secret-do-not-return"},
    })
    assert invalid.status_code == 422
    assert "synthetic-secret" not in invalid.text
    assert "input" not in invalid.text
    assert path.read_bytes() == before
    saved = client.patch("/dispatcher/runtime-settings", json={
        "revision": payload["revision"], "runtime": {"max_workers": 10},
    })
    assert saved.status_code == 200
    assert saved.json()["runtime"]["max_workers"] == 10
    assert "synthetic-secret" not in saved.text
