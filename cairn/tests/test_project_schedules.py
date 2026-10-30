"""以隔离数据库和原件验证计划事务，不启动 Worker、模型或真实目标。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import asyncio
import base64
import json
import os
from uuid import uuid4


from fastapi import HTTPException
import pytest

from cairn.project_authorization_contract import (
    authorization_scope_digest_v4, build_scope_descriptor_v3, build_scope_descriptor_v4,
    origin_scope_digest, scope_source_from_descriptor, target_scope_v3,
)
from cairn.server import db
from cairn.server.schemas.libraries import LibraryFileWrite
from cairn.server.schemas.project_schedules import CreateScheduleRequest, ScheduleActionRequest, ScheduleTiming, UpdateScheduleRequest
from cairn.server.schemas.projects import CreateProjectRequest, UpdateProjectStatusRequest
from cairn.server.services import model_credentials
from cairn.server.services.creation_requests import create_project_request, query_creation_request
from cairn.server.services.libraries import archive_library, create_library, create_library_version
from cairn.server.services.project_authorization import authorization_scope_projection, project_authorization_block_reason
from cairn.server.services.project_records import delete_project_record, get_project_detail, project_lifecycle_action, update_project_status_record
from cairn.server.services.project_resources import create_project_resource_batch, upload_project_resource_batch_file, project_resource_root, read_project_resource_content, cleanup_expired_project_resource_batches
from cairn.server.services.project_schedules import create_schedule, consume_due_schedules, get_schedule, schedule_action, schedule_history, update_schedule
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
    db.configure(database_url=url, artifact_path=tmp_path / "artifacts")
    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({"active_version": "test-v1", "keys": {"test-v1": base64.b64encode(b"p" * 32).decode()}}))
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_ROOT", str(tmp_path / "resources"))
    monkeypatch.setenv("CAIRN_ADVISORY_LIBRARY_ROOT", str(tmp_path / "libraries"))
    monkeypatch.setattr(worker_service, "dispatcher_executor_available", lambda conn: True)
    principal = _principal("schedule-user")
    _worker(principal, "计划模型")
    project = CreateProjectRequest(title="隔离计划", origin="背景 https://background.example.test 不等于授权", goal="验证计划事务", scenario="goal-driven-v1", authorization_expires_at=None, targets=[{"type": "domain", "value": "authorized.example.test"}])
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
