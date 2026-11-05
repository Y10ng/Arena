"""以隔离数据库和原件验证计划事务，不启动 Worker、模型或真实目标。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import asyncio
import base64
import json
import os
import time
from uuid import uuid4


from fastapi import HTTPException
import pytest

from cairn.project_authorization_contract import (
    authorization_scope_digest_v4, build_scope_descriptor_v3, build_scope_descriptor_v4,
    origin_scope_digest, scope_source_from_descriptor, target_scope_v3,
)
from cairn.server import db
from cairn.server.schemas.libraries import LibraryFileWrite, LibrarySelection
from cairn.server.schemas.project_schedules import CreateScheduleRequest, ScheduleActionRequest, ScheduleTiming, UpdateScheduleRequest
from cairn.server.schemas.projects import CreateProjectRequest, UpdateProjectStatusRequest
from cairn.server.services import model_credentials
from cairn.server.services.creation_requests import create_project_request, query_creation_request
from cairn.server.services.libraries import archive_library, create_library, replace_library_files
from cairn.server.services.project_authorization import authorization_scope_projection, project_authorization_block_reason
from cairn.server.services.project_records import delete_project_record, get_project_detail, project_lifecycle_action, update_project_status_record
from cairn.server.services.project_resources import create_project_resource_batch, upload_project_resource_batch_file, project_resource_root, read_project_resource_content, cleanup_expired_project_resource_batches
from cairn.server.services.project_schedules import create_schedule, consume_due_schedules, get_schedule, list_schedules, schedule_action, schedule_history, update_schedule
from cairn.server.services.schedule_times import next_times, timestamp
from cairn.server.services import tenant_workers as worker_service
from test_project_creation_workers import _principal, _worker
from test_postgres_bootstrap import postgres_instance, empty_postgres_url

NOW = datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=1)


@pytest.fixture
def schedule_env(request, tmp_path, monkeypatch):
    if os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1":
        pytest.skip("需显式启用本地一次性 PostgreSQL，绝不使用业务库")
    db._reset_for_tests()
    url = request.getfixturevalue("empty_postgres_url")
    monkeypatch.setenv("CAIRN_KNOWLEDGE_ROOT", str(tmp_path / "knowledge"))
    monkeypatch.setenv("CAIRN_ADVISORY_LIBRARY_ROOT", str(tmp_path / "libraries"))
    db.configure(database_url=url, artifact_path=tmp_path / "artifacts")
    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({"active_version": "test-v1", "keys": {"test-v1": base64.b64encode(b"p" * 32).decode()}}))
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_ROOT", str(tmp_path / "resources"))
    monkeypatch.setattr(worker_service, "dispatcher_executor_available", lambda conn: True)
    principal = _principal("schedule-user")
    _worker(principal, "计划模型")
    project = CreateProjectRequest(title="隔离计划", origin="背景 https://background.example.test 不等于授权", goal="验证计划事务", scenario="goal-driven-v1", authorization_expires_at=None, library_selections=[], targets=[{"type": "domain", "value": "authorized.example.test"}])
    yield principal, project
    db._reset_for_tests()


def _upload(batch_id, principal, path, content):
    async def stream():
        yield content
    return asyncio.run(upload_project_resource_batch_file(batch_id, stream(), path=path, kind=None, tenant_id=principal.tenant_id, user_id=principal.subject_id))


def _selection(principal, key, paths=None):
    from cairn.server.services.libraries import get_selectable_library_manifest

    with db.get_conn() as conn:
        manifest = get_selectable_library_manifest(conn, key)
    return LibrarySelection(library_key=key, source="current", paths=paths,
        expected_files={file.path: file.sha256 for file in manifest.files if paths is None or file.path in paths})


def _body(project, mode="once", **values):
    return CreateScheduleRequest(request_id=uuid4(), mode=mode, timezone="UTC", start_at=START.isoformat(), local_time="10:00" if mode != "once" else None, project=project, **values)


def _action(schedule, action):
    return ScheduleActionRequest(version=schedule["version"], action=action)


def test_once_persistence_idempotency_cancel_race_and_explicit_scope(schedule_env):
    principal, project = schedule_env
    body = _body(project)
    schedule = create_schedule(body, principal, now=NOW)
    project_id = schedule["project_id"]
    assert get_project_detail(project_id).project.status == "pending"
    assert [target.value for target in get_project_detail(project_id).targets] == ["authorized.example.test"]
    assert consume_due_schedules(now=NOW)["processed"] == 0
    # 模拟响应丢失后相同请求与多消费者，必须只返回同一项目和单次逻辑键。
    assert create_schedule(body, principal, now=START + timedelta(days=1))["id"] == schedule["id"]
    for action in ("resume", "finish"):
        with pytest.raises(HTTPException):
            project_lifecycle_action(project_id, action)
    with pytest.raises(HTTPException):
        update_project_status_record(project_id, UpdateProjectStatusRequest(status="active"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel = pool.submit(schedule_action, schedule["id"], _action(schedule, "cancel"), principal, now=START)
        consume = pool.submit(consume_due_schedules, now=START)
        consume.result()
        try:
            cancel.result()
        except HTTPException as exc:
            assert exc.status_code == 409
    saved = get_schedule(schedule["id"], principal)
    if saved["status"] == "cancelled":
        assert get_project_detail(project_id).project.status == "pending"
        saved = schedule_action(saved["id"], _action(saved, "start_now"), principal, now=START)
    assert saved["status"] == "completed"
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: consume_due_schedules(now=START), range(2)))
    assert schedule_history(saved["id"], principal)["total"] == 1
    assert get_project_detail(project_id).project.status == "active"


def test_selectable_manifest_is_current_metadata_only_for_authenticated_users(schedule_env, monkeypatch):
    from fastapi.testclient import TestClient
    from cairn.server.app import app
    from cairn.server.services import libraries as library_service

    principal, _ = schedule_env
    with db.get_conn() as conn:
        create_library(conn, "catalog-active", "可选资料")
        replace_library_files(conn, "catalog-active", [
            LibraryFileWrite(path="nested/a.md", content="version one"),
            LibraryFileWrite(path="b.txt", content="second"),
        ])
        create_library(conn, "catalog-unpublished", "新建空库")
        create_library(conn, "catalog-empty", "空库")
        replace_library_files(conn, "catalog-empty", [])
    original_read = library_service.read_library_file
    monkeypatch.setattr(library_service, "read_library_file", lambda *args, **kwargs: pytest.fail("清单不应读取正文"))
    http = TestClient(app)
    assert http.get("/library-catalog/catalog-active/manifest").status_code == 401
    assert http.post("/auth/login", json={"login": "schedule-user", "password": "safe password"}).status_code == 200
    response = http.get("/library-catalog/catalog-active/manifest")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    first_manifest = response.json()
    assert first_manifest == {
        "library_key": "catalog-active", "library_name": "可选资料", "safety_path": None,
        "file_count": 2,
        "files": [
            {"path": "b.txt", "size_bytes": 6, "sha256": sha256(b"second").hexdigest()},
            {"path": "nested/a.md", "size_bytes": 11, "sha256": sha256(b"version one").hexdigest()},
        ],
    }
    assert http.get("/libraries").status_code == 403
    assert http.put("/libraries/catalog-active/files", json={"files": []}).status_code == 403
    assert http.get("/library-catalog/catalog-active/manifest?version=1").status_code == 422
    assert http.get("/library-catalog/catalog-unpublished/manifest").json()["files"] == []
    empty = http.get("/library-catalog/catalog-empty/manifest")
    assert empty.status_code == 200 and empty.json()["files"] == [] and empty.json()["file_count"] == 0
    with monkeypatch.context() as patch:
        patch.setattr(library_service, "read_library_file", original_read)
        with db.get_conn() as conn:
            replace_library_files(conn, "catalog-active", [
                LibraryFileWrite(path="new.md", content="version two"),
            ])
    current = http.get("/library-catalog/catalog-active/manifest").json()
    assert not {"version", "digest_sha256", "selection_proof"}.intersection(current)
    assert [file["path"] for file in current["files"]] == ["new.md"]
    assert [file["path"] for file in first_manifest["files"]] == ["b.txt", "nested/a.md"]
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM advisory_library_versions").fetchone()["count"] == 0
    with db.get_conn() as conn:
        archive_library(conn, "catalog-active")
    assert http.get("/library-catalog/catalog-active/manifest").status_code == 404
    http.close()


def test_once_view_reports_its_pending_project_resources_and_frozen_libraries(schedule_env):
    principal, project = schedule_env
    batch = create_project_resource_batch(tenant_id=principal.tenant_id, user_id=principal.subject_id)
    _upload(batch.id, principal, "inputs/brief.txt", b"original file")
    with db.get_conn() as conn:
        create_library(conn, "once-notes", "单次资料")
        replace_library_files(
            conn, "once-notes", [LibraryFileWrite(path="notes/readme.md", content="v1")],
        )
    project = project.model_copy(update={"resource_batch_id": batch.id, "library_selections": [_selection(principal, "once-notes")]})
    schedule = create_schedule(_body(project), principal, now=NOW)
    project_id = schedule["project_id"]
    assert schedule["project"] is None
    assert schedule["once_project_materials_available"] is True
    assert [(item["path"], item["size_bytes"], item["digest_sha256"]) for item in schedule["resources"]] == [
        ("inputs/brief.txt", len(b"original file"), sha256(b"original file").hexdigest())
    ]
    assert schedule["resources"][0]["source_name"] is None
    assert [(item["library_key"], item["version"], item["file_count"], item["digest_sha256"]) for item in schedule["library_bindings"]] == [
        ("once-notes", 1, 1, get_project_detail(project_id).library_bindings[0].digest_sha256)
    ]
    with db.get_conn() as conn:
        replace_library_files(
            conn, "once-notes", [LibraryFileWrite(path="changed.txt", content="v2")],
        )
    assert get_schedule(schedule["id"], principal)["library_bindings"] == schedule["library_bindings"]
    assert list_schedules(principal)[0]["resources"] == schedule["resources"]
    other = _principal("another-schedule-user")
    with pytest.raises(HTTPException) as exc:
        get_schedule(schedule["id"], other)
    assert exc.value.status_code == 404
    delete_project_record(project_id)
    deleted = get_schedule(schedule["id"], principal)
    assert deleted["project_id"] is None
    assert deleted["once_project_materials_available"] is False
    assert deleted["resources"] == [] and deleted["library_bindings"] == []


def test_once_retime_cancel_delete_and_restart_recovery(schedule_env):
    principal, project = schedule_env
    schedule = create_schedule(_body(project), principal, now=NOW)
    new_start = START + timedelta(hours=2)
    schedule = update_schedule(schedule["id"], UpdateScheduleRequest(mode="once", timezone="UTC", start_at=new_start.isoformat(), version=schedule["version"]), principal, now=NOW)
    assert consume_due_schedules(now=START)["processed"] == 0
    # 配置重读即重启恢复，消费者不依赖浏览器、线程计时器或进程内领取状态。
    config = db._config
    db._reset_for_tests()
    db.configure(database_url=config.database_url, artifact_path=config.artifact_path)
    assert consume_due_schedules(now=new_start + timedelta(minutes=2))["processed"] == 1
    saved = get_schedule(schedule["id"], principal)
    assert saved["last_result"]["reason"] == "延迟 120 秒启动"
    another = create_schedule(_body(project), principal, now=NOW)
    delete_project_record(another["project_id"])
    assert get_schedule(another["id"], principal)["status"] == "cancelled"
    assert consume_due_schedules(now=new_start + timedelta(days=1))["processed"] == 0


def test_recurring_raw_resources_fixed_revision_overlap_history_and_end(schedule_env, tmp_path, monkeypatch):
    principal, project = schedule_env
    batch = create_project_resource_batch(tenant_id=principal.tenant_id, user_id=principal.subject_id)
    content = b"schedule original bytes\n\x00 retained"
    _upload(batch.id, principal, "notes/input.bin", content)
    with db.get_conn() as conn:
        create_library(conn, "schedule-notes", "计划资料")
        replace_library_files(conn, "schedule-notes", [LibraryFileWrite(path="README.md", content="fixed v1\n")])
    project = project.model_copy(update={"resource_batch_id": batch.id, "library_selections": [_selection(principal, "schedule-notes")]})
    body = _body(project, "daily", max_runs=2)
    schedule = create_schedule(body, principal, now=NOW)
    assert len(schedule["resources"]) == 1 and schedule["project_id"] is None
    assert not (project_resource_root() / ".batches" / batch.id).exists()
    assert create_schedule(body, principal, now=NOW)["id"] == schedule["id"]
    with db.get_conn() as conn:
        conn.execute("UPDATE project_resource_batches SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (batch.id,))
        replace_library_files(conn, "schedule-notes", [LibraryFileWrite(path="README.md", content="latest v2\n")])
    cleanup_expired_project_resource_batches()
    assert consume_due_schedules(now=START)["errors"] == 0
    first = get_schedule(schedule["id"], principal)["last_project_id"]
    assert read_project_resource_content(first, get_project_detail(first).resources[0].id).content == content
    assert get_project_detail(first).library_bindings[0].version == 1
    consume_due_schedules(now=START + timedelta(days=1))
    assert get_schedule(schedule["id"], principal)["generated_count"] == 1
    assert schedule_history(schedule["id"], principal)["items"][0]["status"] == "skipped"
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET status='completed' WHERE id=?", (first,))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: consume_due_schedules(now=START + timedelta(days=2)), range(2)))
    saved = get_schedule(schedule["id"], principal)
    assert saved["status"] == "completed" and saved["generated_count"] == 2
    second = saved["last_project_id"]
    assert second != first
    assert read_project_resource_content(second, get_project_detail(second).resources[0].id).content == content
    assert get_project_detail(second).library_bindings[0].version == 1
    with db.get_conn() as conn:
        with pytest.raises(HTTPException) as exc:
            archive_library(conn, "schedule-notes")
        assert exc.value.status_code == 409
    assert schedule_history(schedule["id"], principal)["total"] == 3
    # 使用真实 Server 读接口和既有 Dispatcher 客户端物化只读 bundle，不运行 Worker。
    from pathlib import Path
    from fastapi.testclient import TestClient
    from cairn.server.app import app
    from cairn.server.auth import create_machine_principal
    from cairn.dispatcher.protocol.client import CairnClient
    from cairn.dispatcher.advisory_bundle import stage_advisory_bundle

    http = TestClient(app)
    assert http.post("/dispatcher/project-schedules/consume").status_code == 401
    _, token = create_machine_principal("schedule-validation", "isolated-test-token")
    http.headers["Authorization"] = "Bearer " + token
    monkeypatch.setenv("CAIRN_ADVISORY_ROOT", str(tmp_path / "advisory"))
    monkeypatch.delenv("CAIRN_ADVISORY_DOCKER_SOURCE", raising=False)
    client = CairnClient("http://testserver", token=token)
    monkeypatch.setattr(client, "_session", lambda: http)
    bundle = stage_advisory_bundle(client, second, None)
    assert bundle is not None
    root = Path(bundle.source_path)
    assert (root / "project/notes/input.bin").read_bytes() == content
    assert (root / "library/schedule-notes/README.md").read_bytes() == b"fixed v1\n"
    assert (root / "project/notes/input.bin").stat().st_mode & 0o222 == 0
    assert client.consume_project_schedules() == {"processed": 0, "errors": 0}
    http.close()


def test_recurring_edit_pause_resume_missed_range_and_disable(schedule_env):
    principal, project = schedule_env
    saved = create_schedule(_body(project, "daily"), principal, now=NOW)
    saved = schedule_action(saved["id"], _action(saved, "pause"), principal, now=NOW)
    assert consume_due_schedules(now=START + timedelta(days=2))["processed"] == 0
    saved = schedule_action(saved["id"], _action(saved, "resume"), principal, now=START + timedelta(days=2, seconds=1))
    assert timestamp(saved["next_run_at"]) == START + timedelta(days=3)
    saved = update_schedule(saved["id"], UpdateScheduleRequest(mode="weekly", timezone="UTC", start_at=START.isoformat(), local_time="11:00", weekdays=[0, 2], project=project.model_copy(update={"goal": "编辑只影响未来轮次"}), version=saved["version"]), principal, now=START + timedelta(days=2))
    with pytest.raises(HTTPException) as exc:
        schedule_action(saved["id"], ScheduleActionRequest(version=saved["version"] - 1, action="pause"), principal, now=NOW)
    assert exc.value.status_code == 409
    consume_due_schedules(now=START + timedelta(days=100, hours=1))
    history = schedule_history(saved["id"], principal)
    assert history["total"] <= 2 and any(item["status"] == "missed" for item in history["items"])
    saved = get_schedule(saved["id"], principal)
    saved = schedule_action(saved["id"], _action(saved, "disable"), principal, now=START + timedelta(days=101))
    assert saved["status"] == "disabled" and saved["next_run_at"] is None


def test_creation_unknown_replay_conflict_and_empty_scope_blocks(schedule_env):
    principal, project = schedule_env
    request = project.model_copy(update={"request_id": uuid4()})
    first = create_project_request(request, tenant_id=principal.tenant_id, user_id=principal.subject_id)
    assert create_project_request(request, tenant_id=principal.tenant_id, user_id=principal.subject_id).project.id == first.project.id
    result = query_creation_request(tenant_id=principal.tenant_id, user_id=principal.subject_id, operation="project", request_id=str(request.request_id))
    assert result["status"] == "committed" and result["project_id"] == first.project.id
    with pytest.raises(HTTPException) as exc:
        create_project_request(request.model_copy(update={"title": "同键改输入"}), tenant_id=principal.tenant_id, user_id=principal.subject_id)
    assert exc.value.status_code == 409
    empty = create_schedule(_body(project.model_copy(update={"targets": []})), principal, now=NOW)
    assert get_project_detail(empty["project_id"]).targets == []
    consume_due_schedules(now=START)
    saved = get_schedule(empty["id"], principal)
    assert saved["status"] == "blocked" and "空授权范围" in saved["block_reason"]
    assert get_project_detail(empty["project_id"]).project.status == "pending"
    assert get_project_detail(empty["project_id"]).project.authorization_scope_descriptor["version"] == 3
    empty_cycle = create_schedule(_body(project.model_copy(update={"targets": []}), "daily"), principal, now=NOW)
    consume_due_schedules(now=START)
    assert "空授权范围" in get_schedule(empty_cycle["id"], principal)["block_reason"]
    descriptor = first.project.authorization_scope_descriptor
    assert scope_source_from_descriptor(descriptor) == "targets"
    digest, ids = target_scope_v3([target.model_dump() for target in first.targets])
    assert descriptor == build_scope_descriptor_v3(project_id=first.project.id, tenant_id=principal.tenant_id, target_digest=digest, target_ids=ids)
    with db.get_conn() as conn:
        grant = dict(conn.execute("SELECT * FROM project_authorization_grants_v2 WHERE project_id=?", (first.project.id,)).fetchone())
        conn.execute("UPDATE facts SET description=? WHERE project_id=? AND id='origin'", ("新背景 https://not-authorized.example.test", first.project.id))
        conn.execute("UPDATE targets SET status='approved' WHERE project_id=?", (first.project.id,))
        row = conn.execute("SELECT * FROM projects WHERE id=?", (first.project.id,)).fetchone()
        assert project_authorization_block_reason(conn, row) is None
        assert dict(conn.execute("SELECT * FROM project_authorization_grants_v2 WHERE project_id=?", (first.project.id,)).fetchone()) == grant
    cycle = create_schedule(_body(project, "daily"), principal, now=NOW)
    update = UpdateScheduleRequest(mode="daily", timezone="UTC", start_at=START.isoformat(), local_time="10:00", version=cycle["version"], project=project.model_copy(update={"targets": None}))
    with pytest.raises(HTTPException) as exc:
        update_schedule(cycle["id"], update, principal, now=NOW)
    assert exc.value.status_code == 422
    assert get_schedule(cycle["id"], principal)["version"] == cycle["version"]
    updated = update.model_copy(update={"project": project.model_copy(update={"goal": "编辑保留旧范围", "origin": "背景 https://not-authorized.example.test"})})
    cycle = update_schedule(cycle["id"], updated, principal, now=NOW)
    assert cycle["project"]["targets"] == project.model_dump(mode="json")["targets"]
    consume_due_schedules(now=START)
    legacy = get_project_detail(get_schedule(cycle["id"], principal)["last_project_id"])
    assert [target.value for target in legacy.targets] == ["authorized.example.test"]
    assert legacy.project.authorization_scope_descriptor["version"] == 3
    assert get_project_detail(first.project.id).project.authorization_scope_descriptor == descriptor


@pytest.mark.parametrize("explicit_null", [False, True])
def test_origin_scope_original_fact_digest_and_no_regex_seed(schedule_env, explicit_null):
    principal, project = schedule_env
    origin = "  授权研究 https://authorized.example.test 与内部业务资料。\n参考 https://reference.example.test；排除 https://excluded.example.test。\n原始模板记号 {goal}。\n  "
    payload = project.model_dump(exclude={"targets"})
    payload.update(origin=origin, goal="  保留 Goal 原件\n", request_id=uuid4())
    if explicit_null:
        payload["targets"] = None
    request = CreateProjectRequest.model_validate(payload)
    detail = create_project_request(request, tenant_id=principal.tenant_id, user_id=principal.subject_id)
    assert detail.targets == []
    assert detail.facts[0].description == origin
    assert detail.facts[1].description == payload["goal"]
    descriptor = detail.project.authorization_scope_descriptor
    expected = build_scope_descriptor_v4(project_id=detail.project.id, tenant_id=principal.tenant_id, origin_fact_id="origin", origin=origin)
    assert descriptor == expected
    assert scope_source_from_descriptor(descriptor) == "origin"
    assert descriptor["origin_sha256"] == origin_scope_digest(origin) == sha256(origin.encode("utf-8")).hexdigest()
    assert detail.project.authorization_scope_digest == authorization_scope_digest_v4(descriptor)
    with db.get_conn() as conn:
        grant = dict(conn.execute("SELECT * FROM project_authorization_grants_v2 WHERE project_id=?", (detail.project.id,)).fetchone())
        assert json.loads(grant["scope_descriptor_json"]) == descriptor
        assert "description" not in descriptor and "target_ids" not in descriptor
        row = conn.execute("SELECT * FROM projects WHERE id=?", (detail.project.id,)).fetchone()
        assert project_authorization_block_reason(conn, row) is None
        conn.execute("UPDATE facts SET description=? WHERE project_id=? AND id='origin'", (origin + "新补正文", detail.project.id))
    changed = get_project_detail(detail.project.id)
    assert changed.project.authorization_scope_descriptor == descriptor
    assert changed.project.authorization_block_reason == "project_authorization_scope_changed"
    with db.get_conn() as conn:
        saved_grant = dict(conn.execute("SELECT * FROM project_authorization_grants_v2 WHERE project_id=?", (detail.project.id,)).fetchone())
        assert saved_grant == grant
        row = conn.execute("SELECT * FROM projects WHERE id=?", (detail.project.id,)).fetchone()
        assert authorization_scope_projection(conn, row).descriptor["origin_sha256"] == sha256((origin + "新补正文").encode("utf-8")).hexdigest()


def test_origin_offline_once_and_recurring_freeze_input_without_scope_drift(schedule_env):
    principal, project = schedule_env
    origin = "  只研究随附的业务资料，不进行外部访问。\n  "
    payload = project.model_dump(exclude={"targets"})
    payload["origin"] = origin
    project = CreateProjectRequest.model_validate(payload)
    once = create_schedule(_body(project), principal, now=NOW)
    pending = get_project_detail(once["project_id"])
    assert pending.project.authorization_scope_descriptor["version"] == 4
    batch = create_project_resource_batch(tenant_id=principal.tenant_id, user_id=principal.subject_id)
    content = b"reference only https://reference.example.test\n"
    _upload(batch.id, principal, "reference.txt", content)
    recurring = create_schedule(_body(project.model_copy(update={"resource_batch_id": batch.id}), "daily", max_runs=2), principal, now=NOW)
    assert recurring["project"]["targets"] is None
    assert recurring["project"]["origin"] == origin
    recurring = update_schedule(recurring["id"], UpdateScheduleRequest(mode="daily", timezone="UTC", start_at=START.isoformat(), local_time="10:00", max_runs=2, version=recurring["version"], project=project.model_copy(update={"goal": "只调整未来 Goal"})), principal, now=NOW)
    assert recurring["project"]["targets"] is None and recurring["project"]["origin"] == origin
    assert consume_due_schedules(now=START) == {"processed": 2, "errors": 0}
    assert get_schedule(once["id"], principal)["status"] == "completed"
    active_once = get_project_detail(once["project_id"])
    assert active_once.project.status == "active" and active_once.targets == []
    assert active_once.project.authorization_scope_descriptor == pending.project.authorization_scope_descriptor
    first_id = get_schedule(recurring["id"], principal)["last_project_id"]
    first = get_project_detail(first_id)
    assert first.targets == [] and first.facts[0].description == origin
    assert read_project_resource_content(first_id, first.resources[0].id).content == content
    first_descriptor = first.project.authorization_scope_descriptor
    with db.get_conn() as conn:
        conn.execute("INSERT INTO facts(id,project_id,description,creator,created_at) VALUES('new-reference',?,?, 'system',?)", (first_id, "后续资料 https://later.example.test", START.isoformat()))
        conn.execute("INSERT INTO targets(id,project_id,value,type,status,source_fact_id,created_at) VALUES('target-later',?,?,'domain','discovered','new-reference',?)", (first_id, "later.example.test", START.isoformat()))
        conn.execute("UPDATE projects SET status='completed' WHERE id=?", (first_id,))
    assert consume_due_schedules(now=START + timedelta(days=1)) == {"processed": 1, "errors": 0}
    saved = get_schedule(recurring["id"], principal)
    assert saved["status"] == "completed" and saved["generated_count"] == 2
    second = get_project_detail(saved["last_project_id"])
    assert second.project.id != first_id and second.targets == []
    assert second.facts[0].description == origin and len(second.facts) == 2
    assert second.project.authorization_scope_descriptor == build_scope_descriptor_v4(project_id=second.project.id, tenant_id=principal.tenant_id, origin_fact_id="origin", origin=origin)
    assert second.project.authorization_scope_descriptor["origin_sha256"] == first_descriptor["origin_sha256"]
    assert get_project_detail(first_id).project.authorization_scope_descriptor == first_descriptor
    assert saved["project"]["origin"] == origin and saved["project"]["targets"] is None
    assert read_project_resource_content(second.project.id, second.resources[0].id).content == content
    from cairn.dispatcher.prompting import authorization_scope_replacements
    from cairn.dispatcher.tasks.bootstrap_context import bootstrap_prompt_replacements

    # 从真实事务触发后的 detail 贯通共用执行片段和 Bootstrap builder，不另造项目替身。
    for triggered in (active_once, second):
        scope = authorization_scope_replacements(triggered)
        bootstrap = bootstrap_prompt_replacements(triggered)
        assert scope["origin"] == bootstrap["origin"] == origin
        assert scope["authorization_scope"] == bootstrap["authorization_scope"]
        assert json.loads(scope["authorization_scope"])["scope_descriptor"] == triggered.project.authorization_scope_descriptor


def test_resource_corruption_rolls_back_cycle_without_losing_original(schedule_env):
    principal, project = schedule_env
    batch = create_project_resource_batch(tenant_id=principal.tenant_id, user_id=principal.subject_id)
    _upload(batch.id, principal, "note.txt", b"original")
    project = project.model_copy(update={"resource_batch_id": batch.id})
    saved = create_schedule(_body(project, "daily"), principal, now=NOW)
    original = project_resource_root() / ".schedules" / saved["id"] / saved["resources"][0]["id"]
    original.write_bytes(b"corrupt")
    consume_due_schedules(now=START + timedelta(days=1))
    saved = get_schedule(saved["id"], principal)
    assert saved["status"] == "blocked" and saved["generated_count"] == 0
    assert "历史轮次不补跑" in saved["block_reason"]
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] == 0
    assert original.exists()
    assert not [path for path in project_resource_root().iterdir() if not path.name.startswith(".")]


def test_stop_boundary_update_and_frozen_once_configuration(schedule_env):
    principal, project = schedule_env
    stopping = START + timedelta(minutes=1, seconds=23)
    authorization = (START + timedelta(days=2, seconds=37)).isoformat()
    saved = create_schedule(_body(project.model_copy(update={"explore_deadline": stopping.isoformat(), "authorization_expires_at": authorization})), principal, now=NOW)
    assert saved["explore_deadline"] == stopping.isoformat()
    assert saved["authorization_expires_at"] == authorization
    # 只改开始时省略截止，保留原始精度；超过旧截止的改期必须整体回滚。
    saved = update_schedule(saved["id"], UpdateScheduleRequest(mode="once", timezone="UTC", start_at=(START + timedelta(seconds=10)).isoformat(), version=saved["version"]), principal, now=NOW)
    assert saved["explore_deadline"] == stopping.isoformat()
    assert saved["authorization_expires_at"] == authorization
    with pytest.raises(HTTPException) as exc:
        update_schedule(saved["id"], UpdateScheduleRequest(mode="once", timezone="UTC", start_at=stopping.isoformat(), version=saved["version"]), principal, now=NOW)
    assert exc.value.status_code == 422
    assert get_schedule(saved["id"], principal)["version"] == saved["version"]
    consume_due_schedules(now=stopping + timedelta(seconds=1))
    assert get_schedule(saved["id"], principal)["status"] == "blocked"
    saved = get_schedule(saved["id"], principal)
    with db.get_conn() as conn:
        conn.execute("UPDATE scenarios SET prompt_group='missing-new-template' WHERE key='goal-driven-v1'")
    # 单次实际执行使用原冻结引用；解除停止边界不续期授权，也不提前激活。
    saved = update_schedule(saved["id"], UpdateScheduleRequest(mode="once", timezone="UTC", start_at=(START + timedelta(days=1)).isoformat(), explore_deadline=None, version=saved["version"]), principal, now=stopping + timedelta(seconds=1))
    assert saved["explore_deadline"] is None
    assert saved["authorization_expires_at"] == authorization
    assert get_project_detail(saved["project_id"]).project.status == "pending"
    consume_due_schedules(now=START + timedelta(days=1))
    saved = get_schedule(saved["id"], principal)
    assert saved["status"] == "completed" and "先前受阻" in saved["last_result"]["reason"]


def test_edit_resource_rollback_and_explicit_revision_refresh(schedule_env, monkeypatch):
    principal, project = schedule_env
    with db.get_conn() as conn:
        create_library(conn, "edit-notes")
        replace_library_files(conn, "edit-notes", [LibraryFileWrite(path="note.md", content="v1")])
    batch = create_project_resource_batch(tenant_id=principal.tenant_id, user_id=principal.subject_id)
    _upload(batch.id, principal, "first.txt", b"kept")
    original_selection = _selection(principal, "edit-notes")
    saved = create_schedule(_body(project.model_copy(update={"resource_batch_id": batch.id, "library_selections": [original_selection]}), "daily"), principal, now=NOW)
    original = project_resource_root() / ".schedules" / saved["id"] / saved["resources"][0]["id"]
    second_batch = create_project_resource_batch(tenant_id=principal.tenant_id, user_id=principal.subject_id)
    _upload(second_batch.id, principal, "second.txt", b"new bytes")
    saved_selection = LibrarySelection(library_key="edit-notes", source="saved", paths=None)
    future = project.model_copy(update={"resource_batch_id": second_batch.id, "library_selections": [saved_selection]})
    update = UpdateScheduleRequest(mode="daily", timezone="UTC", start_at=START.isoformat(), local_time="10:00", version=saved["version"], project=future, resource_ids=[])
    import cairn.server.services.project_schedules as schedule_service
    def fail(*args, **kwargs):
        raise HTTPException(422, "模拟复制后的外层拒绝")
    with monkeypatch.context() as patch:
        patch.setattr(schedule_service, "_future_state", fail)
        with pytest.raises(HTTPException):
            update_schedule(saved["id"], update, principal, now=NOW)
    assert original.read_bytes() == b"kept"
    assert get_schedule(saved["id"], principal)["resources"] == saved["resources"]
    from cairn.server.services.project_resources import get_project_resource_batch
    assert get_project_resource_batch(second_batch.id, tenant_id=principal.tenant_id, user_id=principal.subject_id).state == "open"
    with db.get_conn() as conn:
        replace_library_files(conn, "edit-notes", [LibraryFileWrite(path="note.md", content="v2")])
    saved = update_schedule(saved["id"], update, principal, now=NOW)
    assert not original.exists() and saved["library_bindings"][0]["version"] == 1
    assert [item["path"] for item in saved["resources"]] == ["second.txt"]
    refreshed_project = future.model_copy(update={"resource_batch_id": None, "library_selections": [_selection(principal, "edit-notes")]})
    refreshed = update.model_copy(update={"version": saved["version"], "project": refreshed_project, "resource_ids": None})
    saved = update_schedule(saved["id"], refreshed, principal, now=NOW)
    assert saved["library_bindings"][0]["version"] == 2


def test_cycle_authorization_stop_is_not_normal_completion(schedule_env):
    principal, project = schedule_env
    project = project.model_copy(update={"authorization_expires_at": (START + timedelta(hours=1)).isoformat()})
    saved = create_schedule(_body(project, "daily"), principal, now=NOW)
    consume_due_schedules(now=START)
    saved = get_schedule(saved["id"], principal)
    assert saved["generated_count"] == 1 and saved["status"] == "blocked"
    assert "后续有效" in saved["block_reason"]
    missed = create_schedule(_body(project, "daily"), principal, now=NOW)
    consume_due_schedules(now=START + timedelta(minutes=2))
    missed = get_schedule(missed["id"], principal)
    assert missed["generated_count"] == 0 and missed["status"] == "blocked"
    assert missed["last_result"]["status"] == "missed"
    ended = create_schedule(_body(project.model_copy(update={"authorization_expires_at": None}), "daily", end_date="2030-01-01"), principal, now=NOW)
    consume_due_schedules(now=START + timedelta(days=1))
    ended = get_schedule(ended["id"], principal)
    assert ended["status"] == "completed" and ended["generated_count"] == 0
    assert ended["last_result"]["status"] == "missed"


def test_timezone_preview_dst_and_end_boundary():
    gap = ScheduleTiming(mode="daily", timezone="America/New_York", start_at="2030-03-09T00:00:00-05:00", local_time="02:30", end_date="2030-03-12")
    values = next_times(gap, after=timestamp("2030-03-09T00:00:00Z"))
    assert [item["local"][:10] for item in values] == ["2030-03-09", "2030-03-11", "2030-03-12"]
    assert "不存在" in values[1]["note"]
    fold = ScheduleTiming(mode="weekly", timezone="America/New_York", start_at="2030-11-03T00:00:00-04:00", local_time="01:30", weekdays=[6])
    assert next_times(fold, after=timestamp(fold.start_at))[0]["at"] == "2030-11-03T05:30:00Z"
    with pytest.raises(ValueError):
        ScheduleTiming(mode="once", timezone="America/New_York", start_at="2030-03-10T02:30:00-05:00")


@pytest.mark.parametrize("mode", ["immediate", "once", "daily", "weekly"])
def test_fixed_workers_configuration_edits_preserve_creation_identity(schedule_env, mode):
    principal, project = schedule_env
    workers = worker_service.list_tenant_workers(tenant_id=principal.tenant_id)
    worker_ids = [worker.id for worker in workers]
    assert [worker.task_types for worker in workers] == [["bootstrap"], ["reason"], ["explore"]]
    body = project.model_copy(update={"request_id": uuid4()}) if mode == "immediate" else _body(
        project, mode, **({"weekdays": [1]} if mode == "weekly" else {}),
    )

    def create():
        if mode == "immediate":
            return create_project_request(body, tenant_id=principal.tenant_id, user_id=principal.subject_id)
        return create_schedule(body, principal, now=NOW)

    saved = create()
    initial_project_id = saved.project.id if mode == "immediate" else saved["project_id"]
    with db.get_conn() as conn:
        before_requests = [dict(row) for row in conn.execute("SELECT * FROM creation_requests").fetchall()]
        before_bindings = [dict(row) for row in conn.execute("SELECT * FROM project_tenant_workers ORDER BY project_id, position").fetchall()]
        if mode in {"daily", "weekly"}:
            stored = conn.execute("SELECT project_json FROM project_schedules WHERE id=?", (saved["id"],)).fetchone()
            assert "tenant_worker_ids" not in json.loads(stored["project_json"])

    from cairn.server.schemas.tenant_workers import UpdateTenantWorkerRequest

    def configure(worker, *, enabled):
        return worker_service.update_tenant_worker(
            worker.id,
            UpdateTenantWorkerRequest(
                model_id=worker.model_id,
                max_running=worker.max_running + 1, priority=worker.priority,
                enabled=enabled, expected_revision=worker.revision,
            ), principal=principal,
        )

    # 已成功请求的重发不重新校验配置，也不重写摘要或已有绑定来源。
    disabled = configure(workers[0], enabled=False)
    replay = create()
    assert (replay.project.id if mode == "immediate" else replay["id"]) == (saved.project.id if mode == "immediate" else saved["id"])
    with db.get_conn() as conn:
        assert [dict(row) for row in conn.execute("SELECT * FROM creation_requests").fetchall()] == before_requests
        assert [dict(row) for row in conn.execute("SELECT * FROM project_tenant_workers ORDER BY project_id, position").fetchall()] == before_bindings
    current_worker = configure(disabled, enabled=True)
    if mode == "immediate":
        assert [worker.id for worker in replay.project.tenant_workers] == worker_ids
        return

    changes = {"project": project, "local_time": "10:00"} if mode != "once" else {}
    if mode == "weekly":
        changes["weekdays"] = [1]
    saved = update_schedule(
        saved["id"], UpdateScheduleRequest(
            mode=mode, timezone="UTC", start_at=START.isoformat(),
            version=saved["version"], **changes,
        ), principal, now=NOW,
    )
    if mode == "once":
        assert get_project_detail(initial_project_id).project.status == "pending"
    assert consume_due_schedules(now=START)["errors"] == 0
    saved = get_schedule(saved["id"], principal)
    assert saved["generated_count"] == 1
    generated = get_project_detail(saved["last_project_id"])
    assert generated.project.status == "active"
    assert [worker.id for worker in generated.project.tenant_workers] == worker_ids
    with db.get_conn() as conn:
        binding = conn.execute(
            "SELECT tenant_worker_revision FROM project_tenant_workers WHERE project_id=? AND tenant_worker_id=?",
            (generated.project.id, current_worker.id),
        ).fetchone()
    # 单次保留保存时来源，周期实例绑定触发时的当前配置；两者均不被旧 revision 阻塞。
    assert binding["tenant_worker_revision"] == (workers[0].revision if mode == "once" else current_worker.revision)


@pytest.mark.parametrize("values", [
    {"source": "current", "paths": ["a.md"]},
    {"source": "current", "paths": [], "expected_files": {"a.md": "a" * 64}},
    {"source": "current", "paths": ["a/b", "a\\b"], "expected_files": {"a/b": "a" * 64}},
    {"source": "current", "paths": ["a.md"], "expected_files": {"a.md": "invalid"}},
    {"source": "saved", "paths": ["a.md"], "expected_files": {"a.md": "a" * 64}},
    {"source": "saved", "paths": ["a.md"], "version": 1, "proof": "old-proof"},
])
def test_selection_contract_rejects_missing_digests_and_historical_identity(values):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LibrarySelection(library_key="guide", **values)


def test_schedule_current_selection_conflict_preserves_saved_binding(schedule_env):
    principal, project = schedule_env
    with db.get_conn() as conn:
        create_library(conn, "conflict-guide", "资料")
        replace_library_files(conn, "conflict-guide", [
            LibraryFileWrite(path="a.md", content="a1"),
            LibraryFileWrite(path="b.md", content="b1"),
        ])
    selected = _selection(principal, "conflict-guide", ["a.md", "b.md"])
    saved = create_schedule(_body(project.model_copy(update={"library_selections": [selected]}), "daily"), principal, now=NOW)
    with db.get_conn() as conn:
        replace_library_files(conn, "conflict-guide", [LibraryFileWrite(path="a.md", content="a2")])
    update = UpdateScheduleRequest(mode="daily", timezone="UTC", start_at=START.isoformat(),
        local_time="10:00", version=saved["version"],
        project=project.model_copy(update={"library_selections": [selected]}))
    with pytest.raises(HTTPException) as conflict:
        update_schedule(saved["id"], update, principal, now=NOW)
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "library_selection_conflict"
    unchanged = get_schedule(saved["id"], principal)
    assert unchanged["library_bindings"] == saved["library_bindings"]
    assert unchanged["version"] == saved["version"]
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM advisory_library_versions WHERE library_key=?", ("conflict-guide",)).fetchone()["count"] == 1


def test_schedule_update_omission_preserves_frozen_and_legacy_without_database(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from pydantic import ValidationError
    from cairn.server.schemas.libraries import LibraryRevisionRef
    from cairn.server.schemas.test_egress import TestEgressView
    from cairn.server.services import project_schedules as schedules

    base = {"title": "计划", "origin": "origin", "goal": "goal", "authorization_expires_at": None}
    timing = {"mode": "daily", "timezone": "UTC", "start_at": START.isoformat(), "local_time": "10:00", "version": 1}
    with pytest.raises(ValidationError):
        CreateProjectRequest(**base)
    with pytest.raises(ValidationError):
        UpdateScheduleRequest(**timing, project=base, library_selection_explicit=True)
    omitted = UpdateScheduleRequest(**timing, project=base)
    assert "library_selections" not in omitted.project.model_fields_set

    row = {"id": "s1", "tenant_id": "tenant", "mode": "daily", "status": "active", "version": 1,
           "project_json": json.dumps({"test_egress": {}}), "test_egress_auth": None,
           "generated_count": 0, "library_selection_explicit": 0}
    state = {"stored": CreateProjectRequest(**base, library_selections=[]), "bindings": [], "writes": [], "replaced": []}
    principal = SimpleNamespace(kind="user", tenant_id="tenant", subject_id="user", is_super_admin=False)

    class Connection:
        def execute(self, sql, args):
            state["writes"].append((sql, args))

    @contextmanager
    def transaction():
        yield Connection()

    monkeypatch.setattr(schedules, "schedule_transaction", transaction)
    monkeypatch.setattr(schedules, "_lock_schedule", lambda *args: row)
    monkeypatch.setattr(schedules, "_project_input", lambda *args: state["stored"])
    monkeypatch.setattr(schedules, "list_schedule_library_revisions", lambda *args: state["bindings"])
    monkeypatch.setattr(schedules, "_validate_input", lambda *args: None)
    monkeypatch.setattr(schedules, "stored_settings", lambda *args, **kwargs: (TestEgressView(), None))
    monkeypatch.setattr(schedules, "_replace_library_bindings", lambda conn, sid, choices, **kwargs: state["replaced"].append(choices) or [])
    monkeypatch.setattr(schedules, "update_schedule_resources", lambda *args, **kwargs: None)
    monkeypatch.setattr(schedules, "_future_state", lambda *args: (START.isoformat(), "active", None))
    monkeypatch.setattr(schedules, "get_schedule", lambda *args: {})

    def run(body):
        state["writes"].clear()
        state["replaced"].clear()
        schedules.update_schedule("s1", body, principal, now=NOW)
        explicit = next(args[0] for sql, args in state["writes"] if "SET library_selection_explicit" in sql)
        project_json = next(args[8] for sql, args in state["writes"] if "project_json=?,status=" in sql)
        return explicit, json.loads(project_json)["library_selections"]

    assert run(omitted) == (0, [])
    assert state["replaced"] == []
    explicit_empty = UpdateScheduleRequest(**timing, project=base | {"library_selections": []}, library_selection_explicit=True)
    assert run(explicit_empty) == (1, [])
    assert state["replaced"] == [[]]

    binding = LibraryRevisionRef(library_key="guide", version=1, digest_sha256="d" * 64,
                                 paths=["a.md"], selection_sha256="e" * 64)
    state["stored"] = CreateProjectRequest(**base, library_selections=[{"library_key": "guide", "source": "saved", "paths": ["a.md"]}])
    state["bindings"] = [binding]
    assert run(omitted) == (0, [{"library_key": "guide", "source": "saved", "paths": ["a.md"], "expected_files": None}])
    assert state["replaced"] == []
    removed = UpdateScheduleRequest(**timing, project=base | {"library_selections": []})
    assert run(removed) == (1, [])
    assert state["replaced"] == [[]]
    state["bindings"] = []
    with pytest.raises(HTTPException) as mismatch:
        run(omitted)
    assert mismatch.value.status_code == 409


def test_creation_receipt_replay_keeps_original_selection_fingerprint(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from cairn.server.services import creation_requests

    request_id = uuid4()
    values = {"title": "receipt", "origin": "origin", "goal": "goal", "authorization_expires_at": None,
              "request_id": request_id, "library_selections": [
                  {"library_key": "guide", "source": "current", "paths": ["a.md"], "expected_files": {"a.md": "a" * 64}}]}
    body = CreateProjectRequest(**values)
    payload = body.model_dump(mode="json", exclude={"request_id", "test_egress"})
    assert "proof" not in payload["library_selections"][0]
    digest = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    class Connection:
        def execute(self, sql, args):
            if sql.startswith("INSERT INTO creation_requests"):
                return SimpleNamespace(rowcount=0)
            return SimpleNamespace(fetchone=lambda: {"request_digest": digest, "project_id": "p1", "schedule_id": None})

    @contextmanager
    def transaction():
        yield Connection()

    monkeypatch.setattr(creation_requests, "schedule_transaction", transaction)
    monkeypatch.setattr(creation_requests, "get_project_detail", lambda project_id, tenant_id: project_id)
    monkeypatch.setattr(creation_requests, "create_project_record", lambda *args, **kwargs: pytest.fail("replay must not create"))
    assert creation_requests.create_project_request(body, tenant_id="t", user_id="u") == "p1"
    monkeypatch.delenv(model_credentials.KEYRING_FILE_ENV, raising=False)
    assert creation_requests.create_project_request(CreateProjectRequest(**values), tenant_id="t", user_id="u") == "p1"
    values["library_selections"][0]["expected_files"] = {"a.md": "b" * 64}
    with pytest.raises(HTTPException) as conflict:
        creation_requests.create_project_request(CreateProjectRequest(**values), tenant_id="t", user_id="u")
    assert conflict.value.status_code == 409


def test_frozen_subset_schedule_and_replay_postgres(schedule_env, monkeypatch, tmp_path):
    from pathlib import Path
    from fastapi.testclient import TestClient
    from cairn.dispatcher.advisory_bundle import stage_advisory_bundle
    from cairn.dispatcher.protocol.client import CairnClient
    from cairn.server.app import app
    from cairn.server.auth import create_machine_principal
    from cairn.server.services.project_schedules import get_schedule_library_manifest

    principal, project = schedule_env
    with db.get_conn() as conn:
        create_library(conn, "subset-guide", "冻结资料")
        replace_library_files(conn, "subset-guide", [
            LibraryFileWrite(path="a.md", content="a1"),
            LibraryFileWrite(path="b.md", content="b1"),
            LibraryFileWrite(path="c.md", content="c1"),
        ])
    browsed_at = time.time()
    original = _selection(principal, "subset-guide", ["a.md", "b.md"])
    request = project.model_copy(update={"library_selections": [original], "request_id": uuid4()})
    # 浏览超过 30 分钟但所选正文未变时，当前选择仍可保存。
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: browsed_at + 1801)
        created = create_project_request(request, tenant_id=principal.tenant_id, user_id=principal.subject_id)
    binding = created.library_bindings[0]
    assert created.library_selection_explicit is True
    assert (binding.version, binding.paths, binding.file_count) == (1, ["a.md", "b.md"], 2)
    with db.get_conn() as conn:
        assert [row["path"] for row in conn.execute(
            "SELECT path FROM advisory_library_files WHERE library_key=? AND version=? ORDER BY path",
            ("subset-guide", binding.version),
        ).fetchall()] == ["a.md", "b.md"]

    with TestClient(app) as http:
        assert http.post("/auth/login", json={"login": "schedule-user", "password": "safe password"}).status_code == 200
        prefix = f"/projects/{created.project.id}/advisory-libraries/subset-guide/versions/1/files"
        assert [item["path"] for item in http.get(prefix).json()] == ["a.md", "b.md"]
        assert http.get(prefix + "/a.md").content == b"a1"
        assert http.get(prefix + "/c.md").status_code == 404

    cycle_input = project.model_copy(update={"library_selections": [original]})
    cycle = create_schedule(_body(cycle_input, "daily"), principal, now=NOW)
    once = create_schedule(_body(cycle_input), principal, now=NOW)
    with db.get_conn() as conn:
        replace_library_files(conn, "subset-guide", [LibraryFileWrite(path="new.md", content="v2")])
    manifest = get_schedule_library_manifest(cycle["id"], "subset-guide", principal)
    assert not {"version", "digest_sha256", "selection_proof"}.intersection(manifest.model_dump())
    assert [item.path for item in manifest.files] == ["a.md", "b.md"]
    assert get_schedule(once["id"], principal)["library_bindings"][0]["paths"] == ["a.md", "b.md"]
    outsider = _principal("subset-other")
    with pytest.raises(HTTPException) as hidden:
        get_schedule_library_manifest(cycle["id"], "subset-guide", outsider)
    assert hidden.value.status_code == 404

    narrow = LibrarySelection(library_key="subset-guide", source="saved", paths=["a.md"])
    update = UpdateScheduleRequest(mode="daily", timezone="UTC", start_at=START.isoformat(),
                                    local_time="10:00", version=cycle["version"],
                                    project=project.model_copy(update={"library_selections": [narrow]}))
    with monkeypatch.context() as patch:
        patch.delenv(model_credentials.KEYRING_FILE_ENV)
        assert create_project_request(request, tenant_id=principal.tenant_id, user_id=principal.subject_id).project.id == created.project.id
        cycle = update_schedule(cycle["id"], update, principal, now=NOW)
        assert cycle["library_bindings"][0]["paths"] == ["a.md"]
        wider = update.model_copy(update={"version": cycle["version"], "project": project.model_copy(update={
            "library_selections": [LibrarySelection(library_key="subset-guide", source="saved", paths=["a.md", "b.md"])],
        })})
        with pytest.raises(HTTPException) as denied:
            update_schedule(cycle["id"], wider, principal, now=NOW)
        assert denied.value.status_code == 403
    assert consume_due_schedules(now=START)["errors"] == 0
    generated = get_project_detail(get_schedule(cycle["id"], principal)["last_project_id"])
    assert generated.library_selection_explicit is True
    assert generated.library_bindings[0].paths == ["a.md"]
    assert generated.library_bindings[0].selection_sha256 == cycle["library_bindings"][0]["selection_sha256"]

    # 经真实 Server 路由和 Dispatcher 客户端下载，不启动 Worker 或访问目标。
    monkeypatch.setenv("CAIRN_ADVISORY_ROOT", str(tmp_path / "advisory"))
    monkeypatch.delenv("CAIRN_ADVISORY_DOCKER_SOURCE", raising=False)
    _, token = create_machine_principal("subset-validation", "isolated-test-token")
    with TestClient(app) as http:
        http.headers["Authorization"] = "Bearer " + token
        client = CairnClient("http://testserver", token=token)
        monkeypatch.setattr(client, "_session", lambda: http)
        bundle = stage_advisory_bundle(client, generated.project.id, None)
    assert bundle is not None
    root = Path(bundle.source_path)
    assert (root / "library/subset-guide/a.md").read_bytes() == b"a1"
    assert not (root / "library/subset-guide/b.md").exists()
    assert not (root / "library/subset-guide/c.md").exists()
    assert not (root / "library/subset-guide/new.md").exists()
    assert bundle.manifest["libraries"][0]["paths"] == ["a.md"]
    assert bundle.manifest["libraries"][0]["selection_sha256"] == generated.library_bindings[0].selection_sha256
    assert bundle.digest != generated.library_bindings[0].selection_sha256


def test_v82_migrates_current_files_saved_choices_and_preserves_receipts_postgres(empty_postgres_url, tmp_path, monkeypatch):
    from cairn.advisory_contract import selection_sha256
    from cairn.server import migrations
    from cairn.server.services import libraries

    monkeypatch.setenv("CAIRN_KNOWLEDGE_ROOT", str(tmp_path / "knowledge"))
    monkeypatch.setenv("CAIRN_ADVISORY_LIBRARY_ROOT", str(tmp_path / "libraries"))
    # 在本 fixture 新建的空库中先运行原有 v80，再插入旧格式数据。
    with monkeypatch.context() as patch:
        patch.setattr(db, "POSTGRES_MIGRATIONS", tuple(m for m in migrations.POSTGRES_MIGRATIONS if m.version <= 80))
        patch.setattr(libraries, "initialize_root_library", lambda conn: None)
        db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    now = "2026-01-01T00:00:00Z"
    def legacy_version(conn, key, version, contents):
        # 历史 fixture 直接建立旧表，不用当前文件接口伪装旧发布行为。
        digest = libraries._library_digest(contents)
        conn.execute(
            "INSERT INTO advisory_library_versions(library_key,version,digest_sha256,source,created_at) VALUES (?,?,?,'api',?)",
            (key, version, digest, now),
        )
        for path, content in contents.items():
            conn.execute(
                "INSERT INTO advisory_library_files(library_key,version,path,storage_key,size_bytes,sha256) VALUES (?,?,?,?,?,?)",
                (key, version, path, libraries._store_content(content), len(content), sha256(content).hexdigest()),
            )
        return digest

    with db.get_conn() as conn:
        for key in ("old-guide", "old-empty"):
            conn.execute("INSERT INTO advisory_libraries(key,name,created_at) VALUES (?,?,?)", (key, key, now))
        old_digest = legacy_version(conn, "old-guide", 1, {"old.md": b"old"})
        legacy_version(conn, "old-guide", 2, {"new.md": b"\xef\xbb\xbfnew\r\n"})
        empty_digest = legacy_version(conn, "old-empty", 1, {})
        conn.execute(
            "INSERT INTO projects(id,title,status,created_at,tenant_id) VALUES (?,?,?,?,?)",
            ("old-project", "旧项目", "paused", now, "tenant_legacy"),
        )
        for key in ("old-guide", "old-empty"):
            conn.execute(
                "INSERT INTO project_advisory_library_bindings(project_id,library_key,version,created_at) VALUES (?,?,1,?)",
                ("old-project", key, now),
            )
        for schedule_id, project_json in (
            ("old-bound", {"title": "旧计划", "library_keys": ["old-guide", "old-empty"], "refresh_library_keys": ["old-guide"]}),
            ("old-default", {"title": "旧缺省", "library_keys": []}),
        ):
            conn.execute(
                "INSERT INTO project_schedules(id,tenant_id,creator_id,mode,status,timezone,start_at,project_json,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (schedule_id, "tenant_legacy", "legacy-user", "daily", "active", "UTC", now,
                 json.dumps(project_json, ensure_ascii=False), now, now),
            )
        for key, digest in (("old-guide", old_digest), ("old-empty", empty_digest)):
            conn.execute(
                "INSERT INTO project_schedule_libraries(schedule_id,library_key,version,digest_sha256,created_at) VALUES (?,?,1,?,?)",
                ("old-bound", key, digest, now),
            )
        conn.execute(
            "INSERT INTO creation_requests(tenant_id,user_id,operation,request_id,request_digest,project_id,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("tenant_legacy", "legacy-user", "project", "old-request", "f" * 64, "old-project", now),
        )
        receipt_before = conn.execute("SELECT * FROM creation_requests WHERE request_id=?", ("old-request",)).fetchone()
    with monkeypatch.context() as patch:
        patch.setattr(db, "POSTGRES_MIGRATIONS", tuple(m for m in migrations.POSTGRES_MIGRATIONS if m.version <= 81))
        with db.get_conn() as conn:
            db.run_migrations(conn, "postgresql")
    with db.get_conn() as conn:
        assert conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()["version"] == 81
        expected = {
            "old-guide": selection_sha256("old-guide", 1, old_digest, [
                {"path": "old.md", "size_bytes": 3, "sha256": sha256(b"old").hexdigest()},
            ]),
            "old-empty": selection_sha256("old-empty", 1, empty_digest, []),
        }
        for table in ("project_advisory_library_bindings", "project_schedule_libraries"):
            bindings = conn.execute(f"SELECT library_key,version,paths_json,selection_sha256 FROM {table} ORDER BY library_key").fetchall()
            assert [(row["library_key"], row["version"], row["paths_json"], row["selection_sha256"]) for row in bindings] == [
                (key, 1, None, expected[key]) for key in sorted(expected)
            ]
        schedules = {row["id"]: row for row in conn.execute(
            "SELECT id,project_json,library_selection_explicit FROM project_schedules ORDER BY id"
        ).fetchall()}
        saved = json.loads(schedules["old-bound"]["project_json"])
        assert saved == {"title": "旧计划", "library_selections": [
            {"library_key": key, "version": 1, "paths": None} for key in sorted(expected)
        ]}
        assert json.loads(schedules["old-default"]["project_json"])["library_selections"] == []
        assert all(row["library_selection_explicit"] == 0 for row in schedules.values())
        assert conn.execute("SELECT library_selection_explicit FROM projects WHERE id='old-project'").fetchone()["library_selection_explicit"] == 0
        assert conn.execute("SELECT * FROM creation_requests WHERE request_id=?", ("old-request",)).fetchone() == receipt_before
        for table in ("project_advisory_library_bindings", "project_schedule_libraries"):
            conn.execute(f"UPDATE {table} SET paths_json=? WHERE library_key='old-guide'", (json.dumps(["old.md"]),))
        bindings_before = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY library_key").fetchall()
            for table in ("project_advisory_library_bindings", "project_schedule_libraries")
        }
        # v82 必须以绑定为准，旧浏览 proof 和周期配置中的版本不再是资料身份。
        saved["library_selections"][0]["proof"] = "historical-proof"
        conn.execute("UPDATE project_schedules SET project_json=? WHERE id='old-bound'", (json.dumps(saved),))
    with db.get_conn() as conn:
        db.run_migrations(conn, "postgresql", initialize_runtime=True)
    with db.get_conn() as conn:
        assert conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()["version"] == 82
        converted = json.loads(conn.execute("SELECT project_json FROM project_schedules WHERE id='old-bound'").fetchone()["project_json"])
        assert converted == {"title": "旧计划", "library_selections": [
            {"library_key": key, "source": "saved", "paths": ["old.md"] if key == "old-guide" else None, "expected_files": None}
            for key in sorted(expected)
        ]}
        assert libraries.read_library_file(conn, "old-guide", "new.md").content == "﻿new\r\n"
        assert libraries.read_library_file(conn, "old-guide", "old.md", 1).content == "old"
        assert libraries.get_selectable_library_manifest(conn, "old-empty").files == []
        for table, before in bindings_before.items():
            assert conn.execute(f"SELECT * FROM {table} ORDER BY library_key").fetchall() == before
        assert conn.execute("SELECT * FROM creation_requests WHERE request_id=?", ("old-request",)).fetchone() == receipt_before
    receipt = query_creation_request(tenant_id="tenant_legacy", user_id="legacy-user", operation="project", request_id="old-request")
    assert receipt["status"] == "committed" and receipt["project_id"] == "old-project"


def test_historical_selection_migration_uses_frozen_bindings_without_database():
    from types import SimpleNamespace
    from cairn.advisory_contract import selection_sha256
    from cairn.server import migrations

    updates = []
    scripts = []
    class Connection:
        def executescript(self, sql):
            scripts.append(sql)

        def execute(self, sql, args=()):
            if sql.startswith("SELECT b.project_id"):
                rows = [{"project_id": "p-old", "library_key": "empty", "version": 1, "digest_sha256": "e" * 64}]
            elif sql.startswith("SELECT b.schedule_id"):
                rows = [{"schedule_id": "s-old", "library_key": "guide", "version": 1, "digest_sha256": "d" * 64}]
            elif sql.startswith("SELECT path,size_bytes"):
                rows = [] if args[0] == "empty" else [{"path": "old.md", "size_bytes": 3, "sha256": "f" * 64}]
            elif sql.startswith("SELECT id, project_json"):
                rows = [{"id": "s-old", "project_json": json.dumps({"library_keys": ["guide"], "title": "old"})},
                        {"id": "s-empty", "project_json": json.dumps({"library_keys": [], "title": "empty"})}]
            elif sql.startswith("SELECT library_key,version FROM project_schedule_libraries"):
                rows = [{"library_key": "guide", "version": 1}] if args[0] == "s-old" else []
            elif sql.startswith("UPDATE "):
                updates.append((sql, args))
                rows = []
            else:
                pytest.fail(f"unexpected migration query: {sql}")
            return SimpleNamespace(fetchall=lambda: rows)

    migrations._apply_postgres_project_library_selection(Connection())
    assert len(scripts) == 2
    expected_empty = selection_sha256("empty", 1, "e" * 64, [])
    assert any(args == (expected_empty, "p-old", "empty") for _, args in updates)
    rebuilt = {args[1]: json.loads(args[0]) for sql, args in updates if sql.startswith("UPDATE project_schedules SET project_json")}
    assert rebuilt["s-old"]["library_selections"] == [{"library_key": "guide", "version": 1, "paths": None}]
    assert rebuilt["s-empty"]["library_selections"] == []
    assert "library_keys" not in rebuilt["s-old"]
