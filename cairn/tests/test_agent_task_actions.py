"""仅在一次性 PostgreSQL 验证真实业务事务，不调用模型或研究调度。"""

import asyncio
import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import HTTPException

from cairn.server import db
from cairn.server.auth import RequestPrincipal, create_user_with_personal_tenant
from cairn.server.schemas.models import CreateTenantModelRequest
from cairn.server.schemas.projects import CreateIntentRequest, CreateProjectRequest
from cairn.server.services.agent_task_actions import (
    request_task_action,
    task_action_work,
    validate_task_action_params,
)
from cairn.server.services.project_records import create_project_record, project_lifecycle_action
from cairn.server.services.time_utils import utcnow
from cairn.server.services.tenant_models import create_tenant_model
from test_postgres_bootstrap import postgres_instance, empty_postgres_url

pytestmark = pytest.mark.skipif(
    os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1",
    reason="仅使用显式启用的一次性 PostgreSQL",
)


@pytest.fixture
def action_context(empty_postgres_url, tmp_path, monkeypatch):
    from cairn.server.services import model_credentials

    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({"active_version": "test", "keys": {"test": base64.b64encode(b"t" * 32).decode()}}))
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_ROOT", str(tmp_path / "project-resources"))
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    uid, tenant = create_user_with_personal_tenant("task-action-user", "local-test-password")
    actor = RequestPrincipal("user", uid, tenant_id=tenant, role="user")
    model = create_tenant_model(CreateTenantModelRequest(
        display_name="隔离模型", protocol="openai-chat-completions", base_url="http://127.0.0.1:1", model_name="controlled", api_key="synthetic-action-test-key",
    ), principal=actor)
    with db.get_conn() as conn:
        conn.execute("UPDATE tenant_workers SET model_id = ? WHERE tenant_id = ?", (model.id, tenant))
        project = create_project_record(CreateProjectRequest(
            title="来源项目", origin="仅用于隔离测试的合成资料", goal="保存隔离结果", authorization_expires_at=None,
        ), conn=conn, tenant_id=tenant, creator=uid)
        pid = project.project.id
        cid = "conv_" + uuid4().hex
        conn.execute(
            "INSERT INTO agent_conversations(id,project_id,model_id,created_by,session_id,created_at) VALUES (?,?,?,?,?,?)",
            (cid, pid, model.id, uid, "pi:" + str(uuid4()), "2026-10-29T00:00:00Z"),
        )
        row = conn.execute(
            "INSERT INTO agent_messages(conversation_id,project_id,client_message_id,text,created_at,submitted_by,submitted_tenant_id) "
            "VALUES (?,?,?,?,?,?,?) RETURNING *",
            (cid, pid, str(uuid4()), "受控工具动作", "2026-10-29T00:00:00Z", uid, tenant),
        ).fetchone()
    return actor, row


def _request(context, call_id, method, params):
    actor, row = context
    with db.get_conn() as conn:
        return dict(request_task_action(conn, row, call_id, method, params, actor))


def _counts(conn):
    return {table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            for table in ("projects", "intents", "task_runs", "agent_actions", "creation_requests")}


def test_create_project_paused_idempotent_read_only_query_and_explicit_resume(action_context):
    actor, row = action_context
    params = {"title": "只创建，不启动", "origin": "原始资料\r\n保留空格  ", "goal": "受控目标", "authorization_expires_at": None}
    with ThreadPoolExecutor(max_workers=2) as pool:
        actions = list(pool.map(lambda _: _request(action_context, "create-1", "create_project", params), range(2)))
    assert actions[0] == actions[1]
    action = actions[0]
    assert action["kind"] == "project" and action["business_project_id"] != row["project_id"]
    with db.get_conn() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (action["business_project_id"],)).fetchone()
        assert project["status"] == "paused" and project["tenant_id"] == actor.tenant_id
        origin = conn.execute("SELECT description FROM facts WHERE project_id = ? AND id = 'origin'", (project["id"],)).fetchone()
        assert origin["description"] == params["origin"]
        receipt = conn.execute("SELECT * FROM creation_requests WHERE project_id = ?", (project["id"],)).fetchone()
        assert receipt["user_id"] == actor.subject_id and receipt["tenant_id"] == actor.tenant_id
        grant = conn.execute("SELECT * FROM project_authorization_grants_v2 WHERE project_id = ?", (project["id"],)).fetchone()
        assert grant["actor_id"] == actor.subject_id
        assert _counts(conn) == {"projects": 2, "intents": 0, "task_runs": 0, "agent_actions": 1, "creation_requests": 1}
    with pytest.raises(HTTPException) as conflict:
        _request(action_context, "create-1", "create_project", {**params, "goal": "内容变化"})
    assert conflict.value.status_code == 409
    with db.get_conn() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        work = task_action_work(conn, action, actor)
        assert work["status"] == "succeeded" and work["result"]["project_status"] == "paused"
        assert work["url"] == f"/projects/{action['business_project_id']}"
        denied = task_action_work(conn, action, RequestPrincipal("user", "outsider", tenant_id="other", role="user"))
        assert denied["status"] == "unavailable" and denied["result"] is None
    # 只登记隔离库中的受控就绪状态，不启动 Dispatcher 或研究进程。
    with db.get_conn() as conn:
        conn.execute("INSERT INTO dispatcher_status(id,status,updated_at,runtime_json) VALUES ('controlled-dispatcher','idle',?,?)",
                     (utcnow(), json.dumps({"runtime_capabilities": ["project_pause_v1"]})))
    assert project_lifecycle_action(action["business_project_id"], "resume").status == "active"
    with db.get_conn() as conn:
        assert _counts(conn)["task_runs"] == 0
    # 删除业务结果后，原工具调用不能把它重新创建。
    with db.get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (action["business_project_id"],))
    assert _request(action_context, "create-1", "create_project", params)["id"] == action["id"]
    with db.get_conn() as conn:
        assert task_action_work(conn, action, actor)["status"] == "unavailable"
        assert _counts(conn)["projects"] == 1


def test_append_intent_original_rules_actor_and_replay(action_context):
    actor, row = action_context
    params = {"from": ["origin"], "description": "仅追加当前项目", "priority": 2}
    with ThreadPoolExecutor(max_workers=2) as pool:
        actions = list(pool.map(lambda _: _request(action_context, "intent-1", "append_project_intent", params), range(2)))
    assert actions[0] == actions[1]
    action = actions[0]
    assert action["kind"] == "intent" and action["business_project_id"] == row["project_id"]
    with db.get_conn() as conn:
        intent = conn.execute("SELECT * FROM intents WHERE project_id = ? AND id = ?", (row["project_id"], action["intent_id"])).fetchone()
        assert intent["creator"] == actor.subject_id and intent["worker"] is None and intent["task_run_id"] is None
        assert intent["priority"] == 2
        assert _counts(conn)["task_runs"] == 0
    for changed in ({"from": ["goal"]}, {"from": ["missing"]}, {"depends_on": "missing"}):
        with pytest.raises(HTTPException):
            _request(action_context, "invalid-" + str(uuid4()), "append_project_intent", {**params, **changed})
    with pytest.raises(HTTPException) as conflict:
        _request(action_context, "intent-1", "append_project_intent", {**params, "description": "改变动作"})
    assert conflict.value.status_code == 409
    for status in ("paused", "stopped", "completed", "pending"):
        with db.get_conn() as conn:
            conn.execute("UPDATE projects SET status = ? WHERE id = ?", (status, row["project_id"]))
        with pytest.raises(HTTPException) as rejected:
            _request(action_context, "new-" + status, "append_project_intent", params)
        assert rejected.value.status_code == 403
        # 原收据仅返回已创建对象；不静默恢复，也不重新创建。
        assert _request(action_context, "intent-1", "append_project_intent", params)["id"] == action["id"]
        with db.get_conn() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            work = task_action_work(conn, action, actor)
            assert work["result"]["project_status"] == status
            assert work["result"]["intent_id"] == action["intent_id"]
    with db.get_conn() as conn:
        assert _counts(conn) == {"projects": 1, "intents": 1, "task_runs": 0, "agent_actions": 1, "creation_requests": 0}


def test_action_and_business_rollback_together_and_router_reuses_transaction(action_context):
    actor, row = action_context
    with db.get_conn() as conn:
        before = _counts(conn)
    for method, params in (
        ("create_project", {"title": "回滚", "origin": "来源", "goal": "目标", "authorization_expires_at": None}),
        ("append_project_intent", {"from": ["origin"], "description": "回滚 Intent"}),
    ):
        with pytest.raises(RuntimeError, match="模拟响应提交前失败"):
            with db.get_conn() as conn:
                request_task_action(conn, row, "rollback-" + method, method, params, actor)
                raise RuntimeError("模拟响应提交前失败")
        with db.get_conn() as conn:
            assert _counts(conn) == before
    from cairn.server.routers.intents import create_intent

    ordinary = create_intent(row["project_id"], CreateIntentRequest(
        **{"from": ["origin"]}, description="原用户入口仍可追加", creator="not-the-user", worker="not-a-worker",
    ), actor)
    assert ordinary.creator == actor.subject_id and ordinary.worker is None
    with db.get_conn() as conn:
        assert _counts(conn)["intents"] == 1 and _counts(conn)["agent_actions"] == 0


def test_create_project_copies_bound_attachment_once_and_keeps_original(action_context):
    from cairn.server.services.agent_attachments import attachment_file, attachment_row, bind_attachments, upload_attachment

    actor, row = action_context
    content = b"# Synthetic input\r\nKeep original bytes.\r\n"

    async def stream():
        yield content

    attachment = asyncio.run(upload_attachment(row["project_id"], row["conversation_id"], "input.md", stream(), actor))
    params = {"title": "附件项目", "origin": "按上传原件", "goal": "不启动研究", "authorization_expires_at": None,
              "attachment_ids": [attachment["id"]]}
    with pytest.raises(HTTPException) as unsent:
        _request(action_context, "with-attachment", "create_project", params)
    assert unsent.value.status_code == 404
    with db.get_conn() as conn:
        bind_attachments(conn, row["project_id"], row["conversation_id"], row["id"], [attachment["id"]], actor)
    action = _request(action_context, "with-attachment", "create_project", params)
    assert _request(action_context, "with-attachment", "create_project", params)["id"] == action["id"]
    with db.get_conn() as conn:
        resource = conn.execute("SELECT * FROM project_resources WHERE project_id = ?", (action["business_project_id"],)).fetchone()
        assert resource["path"] == "input.md" and resource["created_by"] == actor.subject_id
        assert conn.execute("SELECT count(*) AS n FROM project_resource_batches").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM task_runs").fetchone()["n"] == 0
        original = attachment_row(conn, row["project_id"], row["conversation_id"], attachment["id"], sent_only=True)
        assert attachment_file(conn, original).read_bytes() == content
        assert conn.execute("SELECT count(*) AS n FROM project_resources WHERE project_id = ?", (row["project_id"],)).fetchone()["n"] == 0
    with pytest.raises(RuntimeError, match="附件事务回滚"):
        with db.get_conn() as conn:
            request_task_action(conn, row, "rollback-attachment", "create_project", params, actor)
            raise RuntimeError("附件事务回滚")
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM project_resource_batches").fetchone()["n"] == 1
        assert _counts(conn)["projects"] == 2
        original = attachment_row(conn, row["project_id"], row["conversation_id"], attachment["id"], sent_only=True)
        assert attachment_file(conn, original).read_bytes() == content


def test_task_action_parameters_cannot_supply_identity_or_execution(action_context):
    actor, row = action_context
    create = {"title": "项目", "origin": "来源", "goal": "目标", "authorization_expires_at": None}
    intent = {"from": ["origin"], "description": "新增线索"}
    for method, params, key, value in (
        ("create_project", create, "request_id", str(uuid4())),
        ("create_project", create, "resource_batch_id", "foreign-batch"),
        ("create_project", create, "attachment_ids", ["duplicate", "duplicate"]),
        ("append_project_intent", intent, "creator", "spoofed-user"),
        ("append_project_intent", intent, "project_id", "foreign-project"),
        ("append_project_intent", intent, "task_run_id", "fake-run"),
        ("append_project_intent", intent, "priority", True),
    ):
        with pytest.raises(HTTPException) as rejected:
            validate_task_action_params(method, {**params, key: value})
        assert rejected.value.status_code == 422
    with pytest.raises(HTTPException):
        validate_task_action_params("create_project", {key: value for key, value in create.items() if key != "authorization_expires_at"})
    with pytest.raises(HTTPException) as rejected:
        with db.get_conn() as conn:
            request_task_action(conn, row, "wrong-actor", "append_project_intent", intent,
                                RequestPrincipal("machine", actor.subject_id, tenant_id=actor.tenant_id))
    assert rejected.value.status_code == 403
    with db.get_conn() as conn:
        assert _counts(conn) == {"projects": 1, "intents": 0, "task_runs": 0, "agent_actions": 0, "creation_requests": 0}
