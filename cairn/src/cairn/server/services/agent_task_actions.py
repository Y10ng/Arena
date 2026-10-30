"""聊天动作只保存业务引用，创建与 Intent 沿原服务在同一事务中提交。"""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException
from pydantic import ValidationError

from cairn.server.auth import _project_access_exists
from cairn.server.schemas.projects import CreateIntentRequest, CreateProjectRequest

from .creation_requests import begin_creation_request, complete_creation_request
from .intent_creation import create_intent_record
from .project_records import create_project_record


TASK_ACTION_METHODS = {"create_project", "append_project_intent"}


def validate_task_action_params(method, params):
    allowed = {
        "create_project": {"title", "origin", "goal", "authorization_expires_at", "scenario", "attachment_ids"},
        "append_project_intent": {"from", "description", "depends_on", "priority", "slot_key", "strategy_key"},
    }
    if method not in allowed or not isinstance(params, dict) or set(params) - allowed[method]:
        raise HTTPException(422, "agent_tool_parameters_invalid")
    if method == "create_project":
        from .agent_attachments import validate_attachment_ids

        validate_attachment_ids(params.get("attachment_ids", []))
        body = {key: value for key, value in params.items() if key != "attachment_ids"}
        model = CreateProjectRequest
    else:
        body, model = params, CreateIntentRequest
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        # 不将 Pydantic 的原始输入回显到工具错误中。
        raise HTTPException(422, "agent_tool_parameters_invalid") from exc


def request_task_action(conn, row, tool_call_id, method, params, actor):
    """调用方已核验执行归属与消息提交人；沿其事务保存真实业务和幂等收据。"""
    body = validate_task_action_params(method, params)
    if (actor.kind != "user" or not actor.tenant_id
            or actor.subject_id != row["submitted_by"] or actor.tenant_id != row["submitted_tenant_id"]):
        raise HTTPException(403, "agent_action_actor_unavailable")
    # 同一消息的并发重送在读取收据前串行化，不靠唯一键报错回滚成功业务。
    conn.execute("SELECT id FROM agent_messages WHERE id = ? FOR UPDATE", (row["id"],))
    prior = conn.execute(
        "SELECT * FROM agent_actions WHERE message_id = ? AND tool_call_id = ?", (row["id"], tool_call_id),
    ).fetchone()
    if prior is not None:
        if prior["method"] != method or json.loads(prior["params_json"]) != params:
            raise HTTPException(409, {"code": "agent_action_conflict", "message": "同一次动作的参数已改变，请明确发起新请求。"})
        return prior

    intent_id = None
    if method == "create_project":
        attachments = params.get("attachment_ids", [])
        if attachments:
            from .agent_attachments import copy_attachments_to_batch

            batch_id = copy_attachments_to_batch(conn, row, attachments, actor)
            body = body.model_copy(update={"resource_batch_id": batch_id})
        request_id = str(uuid5(NAMESPACE_URL, f"cairn:agent:create-project:{row['id']}:{tool_call_id}"))
        replay = begin_creation_request(
            conn, tenant_id=actor.tenant_id, user_id=actor.subject_id, operation="project", request_id=request_id,
            payload=body.model_dump(mode="json", exclude={"request_id", "test_egress"}),
        )
        if replay["project_id"] is not None:
            project_id = replay["project_id"]
        elif replay["schedule_id"] is not None:
            raise HTTPException(409, "创建请求结果类型不匹配")
        else:
            # pending 仅由计划激活；paused 不启动研究，并保留用户通过原 resume 显式启动的路径。
            project_id = create_project_record(
                body, conn=conn, tenant_id=actor.tenant_id, creator=actor.subject_id, initial_status="paused",
            ).project.id
            complete_creation_request(
                conn, tenant_id=actor.tenant_id, user_id=actor.subject_id, operation="project", request_id=request_id,
                project_id=project_id,
            )
        kind = "project"
    else:
        project_id = row["project_id"]
        intent = create_intent_record(conn, project_id, body, actor)
        intent_id, kind = intent.id, "intent"

    identifier = "act_" + uuid4().hex
    conn.execute(
        "INSERT INTO agent_actions(id, message_id, tool_call_id, method, params_json, kind, business_project_id, intent_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (identifier, row["id"], tool_call_id, method,
         json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":")), kind, project_id, intent_id),
    )
    conn.execute("UPDATE agent_messages SET revision = nextval('agent_message_revision') WHERE id = ?", (row["id"],))
    return conn.execute("SELECT * FROM agent_actions WHERE id = ?", (identifier,)).fetchone()


def task_action_work(conn, action, actor):
    """只读业务当前状态；不调用会回收租约的项目详情，不启动或重建任何任务。"""
    work = {
        "id": action["id"], "kind": action["kind"],
        "title": "新建项目" if action["kind"] == "project" else "追加项目 Intent",
        "source_vulnerability_id": None, "source_observation_id": None,
        "status": "unavailable", "error": "业务记录已不可访问，请检查权限或记录是否已删除。",
        "url": "", "download_url": None, "result": None, "next_attempt_at": None,
    }
    project_id = action["business_project_id"]
    if not _project_access_exists(conn, project_id, actor):
        return work
    project = conn.execute("SELECT status FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        return work
    result = {"project_id": project_id, "project_status": project["status"]}
    if action["kind"] == "intent":
        intent = conn.execute("SELECT * FROM intents WHERE project_id = ? AND id = ?", (project_id, action["intent_id"])).fetchone()
        if intent is None:
            return work
        result.update(intent_id=intent["id"], creator=intent["creator"], worker=intent["worker"],
                      to_fact_id=intent["to_fact_id"], concluded_at=intent["concluded_at"], conclusion=intent["conclusion"])
    # succeeded 表示动作已提交，不表示项目研究或 Intent 探索已完成。
    work.update(status="succeeded", error=None, url=f"/projects/{project_id}", result=result)
    return work
