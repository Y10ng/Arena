"""以隔离数据库和原件验证计划事务，不启动 Worker、模型或真实目标。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import asyncio
import base64
import json
import os
import sqlite3
from threading import Barrier
from uuid import uuid4

from psycopg.errors import UniqueViolation

from fastapi import HTTPException
import pytest

from cairn.project_authorization_contract import (
    authorization_scope_digest_v4, build_scope_descriptor_v3, build_scope_descriptor_v4,
    origin_scope_digest, scope_source_from_descriptor, target_scope_v3,
)
from cairn.server import db
from cairn.server.schemas.libraries import LibraryFileWrite
from cairn.server.schemas.project_schedules import CreateScheduleRequest, ScheduleActionRequest, ScheduleTiming, UpdateScheduleRequest
from cairn.server.schemas.projects import CreateProjectRequest, UpdateProjectStatusRequest, UpdateProjectWorkersRequest
from cairn.server.schemas.tenant_workers import CreateTenantWorkerRequest
from cairn.server.schemas.worker_templates import CreateWorkerTemplateRequest, UpdateWorkerTemplateRequest
from cairn.server.services import worker_templates as templates_service
from cairn.server.services import model_credentials
from cairn.server.services.creation_requests import create_project_request, query_creation_request
from cairn.server.services.libraries import archive_library, create_library, create_library_version
from cairn.server.services.project_authorization import authorization_scope_projection, project_authorization_block_reason
from cairn.server.services.project_records import delete_project_record, get_project_detail, project_lifecycle_action, update_project_status_record, update_project_workers_record
from cairn.server.services.project_resources import create_project_resource_batch, upload_project_resource_batch_file, project_resource_root, read_project_resource_content, cleanup_expired_project_resource_batches
from cairn.server.services.project_schedules import create_schedule, consume_due_schedules, get_schedule, schedule_action, schedule_history, update_schedule
from cairn.server.services.schedule_times import next_times, timestamp
from cairn.server.services import tenant_workers as worker_service
from test_project_library_bindings import _principal, _worker
from test_postgres_bootstrap import postgres_instance, empty_postgres_url

NOW = datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=1)


@pytest.fixture(params=["sqlite", "postgres"])
def schedule_env(request, tmp_path, monkeypatch):
    db._reset_for_tests()
    with monkeypatch.context() as patch:
        if request.param.endswith("-v48"):
            # 仅在隔离实例初始化期间截断注册表，恢复后再走真实 configure 升级。
            for registry in ("SQLITE_MIGRATIONS", "POSTGRES_MIGRATIONS"):
                patch.setattr(db, registry, tuple(item for item in getattr(db, registry) if item.version <= 48))
        if request.param.removesuffix("-v48") == "postgres":
            if os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1":
                pytest.skip("需显式启用本地一次性 PostgreSQL，绝不使用业务库")
            url = request.getfixturevalue("empty_postgres_url")
            db.configure(database_url=url, artifact_path=tmp_path / "artifacts")
        else:
            db.configure(tmp_path / "schedules.db")
    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({"active_version": "test-v1", "keys": {"test-v1": base64.b64encode(b"p" * 32).decode()}}))
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_ROOT", str(tmp_path / "resources"))
    monkeypatch.setenv("CAIRN_ADVISORY_LIBRARY_ROOT", str(tmp_path / "libraries"))
    monkeypatch.setattr(worker_service, "dispatcher_executor_available", lambda conn: True)
    principal = _principal("schedule-user")
    worker = _worker(principal)
    project = CreateProjectRequest(title="隔离计划", origin="背景 https://background.example.test 不等于授权", goal="验证计划事务", scenario="goal-driven-v1", tenant_worker_ids=[worker.id], authorization_expires_at=None, targets=[{"type": "domain", "value": "authorized.example.test"}])
    yield principal, project
    db._reset_for_tests()


def _upload(batch_id, principal, path, content):
    async def stream():
        yield content
    return asyncio.run(upload_project_resource_batch_file(batch_id, stream(), path=path, kind=None, tenant_id=principal.tenant_id, user_id=principal.subject_id))


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


def test_once_retime_cancel_delete_and_restart_recovery(schedule_env):
    principal, project = schedule_env
    schedule = create_schedule(_body(project), principal, now=NOW)
    new_start = START + timedelta(hours=2)
    schedule = update_schedule(schedule["id"], UpdateScheduleRequest(mode="once", timezone="UTC", start_at=new_start.isoformat(), version=schedule["version"]), principal, now=NOW)
    assert consume_due_schedules(now=START)["processed"] == 0
    # 配置重读即重启恢复，消费者不依赖浏览器、线程计时器或进程内领取状态。
    config = db._config
    db._reset_for_tests()
    if config.backend == "sqlite":
        db.configure(config.sqlite_path)
    else:
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
        create_library_version(conn, "schedule-notes", [LibraryFileWrite(path="README.md", content="fixed v1\n")], created_by=principal.subject_id)
    project = project.model_copy(update={"resource_batch_id": batch.id, "library_keys": ["schedule-notes"]})
    body = _body(project, "daily", max_runs=2)
    schedule = create_schedule(body, principal, now=NOW)
    assert len(schedule["resources"]) == 1 and schedule["project_id"] is None
    assert not (project_resource_root() / ".batches" / batch.id).exists()
    assert create_schedule(body, principal, now=NOW)["id"] == schedule["id"]
    with db.get_conn() as conn:
        conn.execute("UPDATE project_resource_batches SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (batch.id,))
        create_library_version(conn, "schedule-notes", [LibraryFileWrite(path="README.md", content="latest v2\n")], created_by=principal.subject_id)
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
        create_library_version(conn, "edit-notes", [LibraryFileWrite(path="note.md", content="v1")])
    batch = create_project_resource_batch(tenant_id=principal.tenant_id, user_id=principal.subject_id)
    _upload(batch.id, principal, "first.txt", b"kept")
    saved = create_schedule(_body(project.model_copy(update={"resource_batch_id": batch.id, "library_keys": ["edit-notes"]}), "daily"), principal, now=NOW)
    original = project_resource_root() / ".schedules" / saved["id"] / saved["resources"][0]["id"]
    second_batch = create_project_resource_batch(tenant_id=principal.tenant_id, user_id=principal.subject_id)
    _upload(second_batch.id, principal, "second.txt", b"new bytes")
    future = project.model_copy(update={"resource_batch_id": second_batch.id, "library_keys": ["edit-notes"]})
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
        create_library_version(conn, "edit-notes", [LibraryFileWrite(path="note.md", content="v2")])
    saved = update_schedule(saved["id"], update, principal, now=NOW)
    assert not original.exists() and saved["library_bindings"][0]["version"] == 1
    assert [item["path"] for item in saved["resources"]] == ["second.txt"]
    refreshed = update.model_copy(update={"version": saved["version"], "project": future.model_copy(update={"resource_batch_id": None}), "resource_ids": None, "refresh_library_keys": ["edit-notes"]})
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


def test_default_templates_tenant_concurrency_uniqueness_and_rollback(schedule_env, monkeypatch):
    principal, project = schedule_env
    foreign = _principal("default-foreign")
    foreign_worker = _worker(foreign)
    foreign_default = templates_service.create_worker_template(
        CreateWorkerTemplateRequest(name="其他 Tenant 默认", tenant_worker_ids=[foreign_worker.id], is_default=True),
        tenant_id=foreign.tenant_id, actor_id=foreign.subject_id,
    )
    barrier = Barrier(2)

    def create_default(index):
        barrier.wait(timeout=10)
        return templates_service.create_worker_template(
            CreateWorkerTemplateRequest(name=f"并发默认 {index}", tenant_worker_ids=project.tenant_worker_ids, is_default=True),
            tenant_id=principal.tenant_id, actor_id=principal.subject_id,
        )

    # 尚无默认行的首次并发保存也必须串行成功，不能只依靠唯一索引拒绝一方。
    with ThreadPoolExecutor(max_workers=2) as pool:
        templates = list(pool.map(create_default, range(2)))
    assert len(templates_service.list_worker_templates(tenant_id=principal.tenant_id)) == 2
    assert sum(item.is_default for item in templates_service.list_worker_templates(tenant_id=principal.tenant_id)) == 1

    def switch_default(template):
        barrier.wait(timeout=10)
        return templates_service.update_worker_template(
            template.id, UpdateWorkerTemplateRequest(is_default=True), tenant_id=principal.tenant_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(item.is_default for item in pool.map(switch_default, templates))
    saved = templates_service.list_worker_templates(tenant_id=principal.tenant_id)
    current = next(item for item in saved if item.is_default)
    other = next(item for item in saved if not item.is_default)
    assert sum(item.is_default for item in saved) == 1
    assert templates_service.list_worker_templates(tenant_id=foreign.tenant_id) == [foreign_default]
    with pytest.raises(HTTPException) as hidden:
        templates_service.update_worker_template(foreign_default.id, UpdateWorkerTemplateRequest(is_default=True), tenant_id=principal.tenant_id)
    assert hidden.value.status_code == 404

    # 绕开服务也不能保存同 Tenant 的两个默认；失败事务不能留下半次切换。
    with pytest.raises((sqlite3.IntegrityError, UniqueViolation)):
        with db.get_conn() as conn:
            conn.execute("UPDATE worker_templates SET is_default=1 WHERE id=?", (other.id,))
    before = [item.model_dump() for item in templates_service.list_worker_templates(tenant_id=principal.tenant_id)]
    replace = templates_service._replace_template_workers

    def fail_after_binding(*args, **kwargs):
        replace(*args, **kwargs)
        raise HTTPException(422, "模拟模板绑定写入后失败")

    with monkeypatch.context() as patch:
        patch.setattr(templates_service, "_replace_template_workers", fail_after_binding)
        with pytest.raises(HTTPException, match="模拟模板绑定写入后失败"):
            templates_service.create_worker_template(
                CreateWorkerTemplateRequest(name="必须回滚", tenant_worker_ids=project.tenant_worker_ids, is_default=True),
                tenant_id=principal.tenant_id, actor_id=principal.subject_id,
            )
        assert [item.model_dump() for item in templates_service.list_worker_templates(tenant_id=principal.tenant_id)] == before
        with pytest.raises(HTTPException, match="模拟模板绑定写入后失败"):
            templates_service.update_worker_template(
                other.id, UpdateWorkerTemplateRequest(name="不可留下的新名称", tenant_worker_ids=project.tenant_worker_ids, is_default=True),
                tenant_id=principal.tenant_id,
            )
    assert [item.model_dump() for item in templates_service.list_worker_templates(tenant_id=principal.tenant_id)] == before
    assert templates_service.list_worker_templates(tenant_id=foreign.tenant_id) == [foreign_default]
    assert not templates_service.update_worker_template(current.id, UpdateWorkerTemplateRequest(is_default=False), tenant_id=principal.tenant_id).is_default
    assert not any(item.is_default for item in templates_service.list_worker_templates(tenant_id=principal.tenant_id))
    templates_service.update_worker_template(other.id, UpdateWorkerTemplateRequest(is_default=True), tenant_id=principal.tenant_id)
    templates_service.delete_worker_template(other.id, tenant_id=principal.tenant_id)
    assert not any(item.is_default for item in templates_service.list_worker_templates(tenant_id=principal.tenant_id))


def test_creation_role_union_and_default_changes_preserve_old_bindings(schedule_env):
    principal, project = schedule_env
    with db.get_conn() as conn:
        model_id = conn.execute("SELECT model_id FROM tenant_workers WHERE id=?", (project.tenant_worker_ids[0],)).fetchone()["model_id"]
    roles = ("bootstrap", "reason", "explore")
    partial = {
        missing: worker_service.create_tenant_worker(
            CreateTenantWorkerRequest(display_name=f"缺少 {missing}", model_id=model_id, task_types=[role for role in roles if role != missing]),
            principal=principal,
        ) for missing in roles
    }
    same_roles = [partial["explore"]] + [
        worker_service.create_tenant_worker(
            CreateTenantWorkerRequest(display_name=f"重复角色 {index}", model_id=model_id, task_types=["bootstrap", "reason"]), principal=principal,
        ) for index in range(2)
    ]
    selections = [([worker.id], missing) for missing, worker in partial.items()]
    selections.append(([worker.id for worker in same_roles], "explore"))
    ordinary = templates_service.create_worker_template(
        CreateWorkerTemplateRequest(name="普通不完整组合", tenant_worker_ids=[partial["explore"].id]),
        tenant_id=principal.tenant_id, actor_id=principal.subject_id,
    )
    assert not ordinary.is_default
    ordinary = templates_service.update_worker_template(
        ordinary.id, UpdateWorkerTemplateRequest(tenant_worker_ids=[partial["reason"].id]), tenant_id=principal.tenant_id,
    )
    default = templates_service.create_worker_template(
        CreateWorkerTemplateRequest(name="单 Worker 三角色", tenant_worker_ids=project.tenant_worker_ids, is_default=True),
        tenant_id=principal.tenant_id, actor_id=principal.subject_id,
    )
    with pytest.raises(HTTPException) as rejected_default:
        templates_service.create_worker_template(
            CreateWorkerTemplateRequest(name="数量不能替代角色", tenant_worker_ids=[worker.id for worker in same_roles], is_default=True),
            tenant_id=principal.tenant_id, actor_id=principal.subject_id,
        )
    assert rejected_default.value.status_code == 422 and "explore" in rejected_default.value.detail
    with pytest.raises(HTTPException) as rejected_promotion:
        templates_service.update_worker_template(ordinary.id, UpdateWorkerTemplateRequest(is_default=True), tenant_id=principal.tenant_id)
    assert rejected_promotion.value.status_code == 422 and "reason" in rejected_promotion.value.detail
    assert next(item for item in templates_service.list_worker_templates(tenant_id=principal.tenant_id) if item.is_default).id == default.id

    def create(mode, worker_ids):
        request = project.model_copy(update={"tenant_worker_ids": worker_ids, "request_id": uuid4() if mode == "immediate" else None})
        if mode == "immediate":
            return create_project_request(request, tenant_id=principal.tenant_id, user_id=principal.subject_id)
        return create_schedule(_body(request, mode, **({"weekdays": [1]} if mode == "weekly" else {})), principal, now=NOW)

    for mode in ("immediate", "once", "daily", "weekly"):
        for worker_ids, missing in selections:
            with pytest.raises(HTTPException) as rejected:
                create(mode, worker_ids)
            assert rejected.value.status_code == 422 and missing in rejected.value.detail
    with db.get_conn() as conn:
        for table in ("projects", "project_schedules", "creation_requests", "project_tenant_workers"):
            assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0

    union = [partial["bootstrap"].id, partial["explore"].id]
    for worker_ids in (project.tenant_worker_ids, union):
        for mode in ("immediate", "once", "daily", "weekly"):
            result = create(mode, worker_ids)
            if mode == "immediate":
                assert [item.id for item in result.project.tenant_workers] == worker_ids
            elif mode == "once":
                assert [item.id for item in get_project_detail(result["project_id"]).project.tenant_workers] == worker_ids
            else:
                with db.get_conn() as conn:
                    stored = conn.execute("SELECT project_json FROM project_schedules WHERE id=?", (result["id"],)).fetchone()
                assert json.loads(stored["project_json"])["tenant_worker_ids"] == worker_ids
    historical = create("immediate", project.tenant_worker_ids)
    rebound = update_project_workers_record(historical.project.id, UpdateProjectWorkersRequest(tenant_worker_ids=[partial["reason"].id]), tenant_id=principal.tenant_id)
    assert [item.id for item in rebound.tenant_workers] == [partial["reason"].id]

    def stored_bindings():
        with db.get_conn() as conn:
            return (
                [dict(row) for row in conn.execute("SELECT * FROM project_tenant_workers ORDER BY project_id, position").fetchall()],
                [dict(row) for row in conn.execute("SELECT * FROM project_schedules ORDER BY id").fetchall()],
            )

    before = stored_bindings()
    replacement = templates_service.create_worker_template(
        CreateWorkerTemplateRequest(name="双 Worker 角色并集", tenant_worker_ids=union, is_default=True),
        tenant_id=principal.tenant_id, actor_id=principal.subject_id,
    )
    assert replacement.is_default and stored_bindings() == before
    templates_service.update_worker_template(replacement.id, UpdateWorkerTemplateRequest(tenant_worker_ids=project.tenant_worker_ids), tenant_id=principal.tenant_id)
    templates_service.update_worker_template(default.id, UpdateWorkerTemplateRequest(is_default=True), tenant_id=principal.tenant_id)
    assert stored_bindings() == before


@pytest.mark.parametrize("schedule_env", ["sqlite-v48", "postgres-v48"], indirect=True)
def test_default_false_v49_migrates_existing_templates_without_rebinding(schedule_env):
    principal, project = schedule_env
    with db.get_conn() as conn:
        assert conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()["version"] == 48
        for template_id in ("wtm_history_one", "wtm_history_two"):
            conn.execute(
                "INSERT INTO worker_templates(id,name,owner,worker_names_json,created_at,tenant_id) VALUES(?,?,?,?,?,?)",
                (template_id, "历史模板", principal.subject_id, '["historical-worker"]', NOW.isoformat(), principal.tenant_id),
            )
            conn.execute(
                "INSERT INTO worker_template_tenant_workers(template_id,tenant_id,tenant_worker_id,position,created_at) VALUES(?,?,?,0,?)",
                (template_id, principal.tenant_id, project.tenant_worker_ids[0], NOW.isoformat()),
            )
        before_templates = [dict(row) for row in conn.execute("SELECT * FROM worker_templates ORDER BY id").fetchall()]
        before_bindings = [dict(row) for row in conn.execute("SELECT * FROM worker_template_tenant_workers ORDER BY template_id, position").fetchall()]
        assert all("is_default" not in row for row in before_templates)
    config = db._config
    db._reset_for_tests()
    if config.backend == "sqlite":
        db.configure(config.sqlite_path)
    else:
        db.configure(database_url=config.database_url, artifact_path=config.artifact_path)
    with db.get_conn() as conn:
        assert [dict(row) for row in conn.execute("SELECT * FROM worker_templates ORDER BY id").fetchall()] == [dict(row, is_default=0) for row in before_templates]
        assert [dict(row) for row in conn.execute("SELECT * FROM worker_template_tenant_workers ORDER BY template_id, position").fetchall()] == before_bindings
        registry = db.SQLITE_MIGRATIONS if config.backend == "sqlite" else db.POSTGRES_MIGRATIONS
        migration = next(item for item in registry if item.version == 49)
        assert dict(conn.execute("SELECT name,checksum FROM schema_migrations WHERE version=49").fetchone()) == {"name": migration.name, "checksum": migration.checksum}
    assert all(not item.is_default for item in templates_service.list_worker_templates(tenant_id=principal.tenant_id))
    fresh = templates_service.create_worker_template(
        CreateWorkerTemplateRequest(name="升级后的普通模板", tenant_worker_ids=project.tenant_worker_ids),
        tenant_id=principal.tenant_id, actor_id=principal.subject_id,
    )
    assert not fresh.is_default
