"""项目对话的受理、单写者认领与既有成果工具；执行状态只取 TaskRun。"""

from __future__ import annotations

import json
import re
from base64 import b64decode
from hashlib import sha256
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from cairn.server.auth import _project_access_exists, RequestPrincipal
from cairn.server.db import get_conn
from cairn.server.models import CreateArtifactRequest, CreateTaskRunRequest
from .audit_artifacts import create_artifact_record
from . import agent_attachments as attachments
from .audit_task_runs import insert_task_run_record
from .ids import get_project_or_404
from .project_runtime_diagnostics import build_project_runtime
from .model_execution import model_availability, resolve_model_execution_binding
from .project_agent_configuration import default_model_id
from .time_utils import utcnow
from .verification_requests import _platform_secrets
from .vulnerabilities import get_vulnerability_detail, list_vulnerabilities, read_vulnerability_evidence
from .vulnerability_reports import render_vulnerability_report


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _conversation(conn, project_id, conversation_id, *, lock=False):
    row = conn.execute(
        "SELECT * FROM agent_conversations WHERE project_id = ? AND id = ?" + (" FOR UPDATE" if lock else ""),
        (project_id, conversation_id),
    ).fetchone()
    if row is None or row["deleted_at"] is not None:
        raise HTTPException(404, "agent_conversation_not_found")
    return row


def _public_conversation(conn, row):
    project = get_project_or_404(conn, row["project_id"])
    return {key: row[key] for key in ("id", "project_id", "created_at")} | model_availability(
        conn, tenant_id=project["tenant_id"], model_id=row["model_id"],
    )


def _model_binding(conn, conversation):
    project = get_project_or_404(conn, conversation["project_id"])
    return resolve_model_execution_binding(conn, tenant_id=project["tenant_id"], model_id=conversation["model_id"])


def list_conversations(conn, project_id):
    return [_public_conversation(conn, row) for row in conn.execute(
        "SELECT * FROM agent_conversations WHERE project_id = ? AND deleted_at IS NULL ORDER BY created_at, id", (project_id,),
    ).fetchall()]


def delete_conversation(conn, project_id, conversation_id):
    row = conn.execute("SELECT * FROM agent_conversations WHERE project_id = ? AND id = ? FOR UPDATE",
                       (project_id, conversation_id)).fetchone()
    if row is None:
        raise HTTPException(404, "agent_conversation_not_found")
    if row["deleted_at"] is not None:
        return
    # 与提交、认领共用会话行锁；独立报告和复测不属于聊天在途工作。
    if conn.execute(_MESSAGE_SELECT + "WHERE m.conversation_id = ? AND "
                    "(m.task_run_id IS NULL OR t.status IN ('queued', 'running')) LIMIT 1",
                    (conversation_id,)).fetchone():
        raise HTTPException(409, "会话仍有排队、执行或结束待确认的消息，暂不能删除。")
    conn.execute("UPDATE agent_conversations SET deleted_at = ? WHERE id = ?", (utcnow(), conversation_id))


def create_conversation(conn, project_id, principal):
    project = get_project_or_404(conn, project_id)
    if conn.execute("SELECT 1 FROM verification_requests WHERE project_id = ?", (project_id,)).fetchone():
        raise HTTPException(409, "复测内部项目不提供独立会话入口")
    model_id = default_model_id(conn, project["tenant_id"])
    resolve_model_execution_binding(conn, tenant_id=project["tenant_id"], model_id=model_id)
    identifier = "conv_" + uuid4().hex
    conn.execute(
        "INSERT INTO agent_conversations(id, project_id, model_id, created_by, session_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (identifier, project_id, model_id, principal.subject_id, "pi:" + str(uuid4()), utcnow()),
    )
    return _public_conversation(conn, _conversation(conn, project_id, identifier))


def _admit_public(value, secrets):
    if isinstance(value, dict):
        for key, item in value.items():
            _admit_public(key, secrets)
            _admit_public(item, secrets)
    elif isinstance(value, list):
        for item in value:
            _admit_public(item, secrets)
    elif isinstance(value, (str, bytes)):
        raw = value.encode("utf-8") if isinstance(value, str) else value
        if any(secret and secret in raw for secret in secrets):
            raise HTTPException(422, "agent_public_content_blocked")


def _binding_secrets(binding):
    return [value.encode("utf-8") for value in [binding.api_key, *(h.value for h in binding.custom_headers)] if value]


def _artifacts(conn, row):
    result = []
    for identifier in json.loads(row["artifact_ids_json"]):
        artifact = conn.execute("SELECT id, metadata_json FROM artifacts WHERE project_id = ? AND id = ?", (row["project_id"], identifier)).fetchone()
        if artifact:
            metadata = json.loads(artifact["metadata_json"])
            result.append({"id": identifier, "title": metadata["title"],
                           "download_url": f"/projects/{row['project_id']}/agent-conversations/{row['conversation_id']}/artifacts/{identifier}"})
    return result


def _message(conn, row):
    status = row["run_status"] or "queued"
    error = row["error_code"]
    if row["task_run_id"] is None:
        configuration = _public_conversation(conn, _conversation(conn, row["project_id"], row["conversation_id"]))
        error = configuration["unavailable_reason"]
    if status == "running" and row["heartbeat_at"]:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["heartbeat_at"].replace("Z", "+00:00"))).total_seconds()
        if age > 120:
            error = "execution_unconfirmed"
    return {"id": row["id"], "client_message_id": row["client_message_id"], "text": row["text"],
            "created_at": row["created_at"], "task_run_id": row["task_run_id"], "status": status,
            "answer": row["answer"], "error_code": error, "artifacts": _artifacts(conn, row),
            "attachments": attachments.list_attachments(conn, row["project_id"], row["conversation_id"], message_id=row["id"]),
            "actions": [_action_reference(action) for action in conn.execute(
                "SELECT * FROM agent_actions WHERE message_id = ? ORDER BY id", (row["id"],)).fetchall()],
            "revision": row["revision"]}


_MESSAGE_SELECT = "SELECT m.*, t.status AS run_status FROM agent_messages m LEFT JOIN task_runs t ON t.project_id = m.project_id AND t.id = m.task_run_id "


def messages(conn, project_id, conversation_id, *, after=0, revision=None):
    _conversation(conn, project_id, conversation_id)
    field, cursor = ("m.id", after) if revision is None else ("m.revision", revision)
    rows = conn.execute(_MESSAGE_SELECT + f"WHERE m.project_id = ? AND m.conversation_id = ? AND {field} > ? ORDER BY m.id",
                        (project_id, conversation_id, cursor)).fetchall()
    return [_message(conn, row) for row in rows]


def submit_message(conn, project_id, conversation_id, client_message_id, text, principal, attachment_ids=None):
    attachment_ids = attachment_ids or []
    attachments.validate_attachment_ids(attachment_ids)
    conversation = _conversation(conn, project_id, conversation_id, lock=True)
    prior = conn.execute(_MESSAGE_SELECT + "WHERE m.conversation_id = ? AND m.client_message_id = ?", (conversation_id, client_message_id)).fetchone()
    if prior:
        prior_ids = {item["id"] for item in attachments.list_attachments(conn, project_id, conversation_id, message_id=prior["id"])}
        if prior["text"] != text or prior["submitted_by"] != principal.subject_id or prior_ids != set(attachment_ids):
            raise HTTPException(409, "agent_message_id_conflict")
        return _message(conn, prior)
    binding = _model_binding(conn, conversation)
    _admit_public(text, _binding_secrets(binding))
    row = conn.execute(
        "INSERT INTO agent_messages(conversation_id, project_id, client_message_id, text, created_at, submitted_by, submitted_tenant_id) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (conversation_id, project_id, client_message_id, text, utcnow(), principal.subject_id, principal.tenant_id),
    ).fetchone()
    attachments.bind_attachments(conn, project_id, conversation_id, row["id"], attachment_ids, principal)
    return _message(conn, conn.execute(_MESSAGE_SELECT + "WHERE m.id = ?", (row["id"],)).fetchone())


def pending_conversations(conn):
    return [dict(row) for row in conn.execute(
        "SELECT c.project_id, c.id AS conversation_id, p.execution_generation FROM agent_conversations c "
        "JOIN projects p ON p.id = c.project_id "
        "WHERE c.deleted_at IS NULL AND EXISTS (SELECT 1 FROM agent_messages m WHERE m.conversation_id = c.id AND m.task_run_id IS NULL) "
        "AND NOT EXISTS (SELECT 1 FROM agent_messages m JOIN task_runs t ON t.project_id = m.project_id AND t.id = m.task_run_id "
        "WHERE m.conversation_id = c.id AND t.status IN ('queued', 'running')) ORDER BY c.created_at, c.id LIMIT 100",
    ).fetchall()]


def _creator_access(conn, conversation):
    user = conn.execute("SELECT u.*, m.tenant_id FROM users u LEFT JOIN tenant_memberships m ON m.user_id = u.id AND m.role = 'owner' WHERE u.id = ?", (conversation["created_by"],)).fetchone()
    if user is None or user["disabled"]:
        raise HTTPException(403, "agent_conversation_owner_unavailable")
    principal = RequestPrincipal("user", user["id"], tenant_id=user["tenant_id"], role=user["role"])
    if not _project_access_exists(conn, conversation["project_id"], principal):
        raise HTTPException(404, "Project not found")


def claim_turn(conn, project_id, conversation_id, task_run, principal):
    conversation = _conversation(conn, project_id, conversation_id, lock=True)
    _creator_access(conn, conversation)
    if conn.execute(_MESSAGE_SELECT + "WHERE m.conversation_id = ? AND t.status IN ('queued', 'running') LIMIT 1", (conversation_id,)).fetchone():
        return None
    row = conn.execute("SELECT * FROM agent_messages WHERE conversation_id = ? AND task_run_id IS NULL ORDER BY id LIMIT 1", (conversation_id,)).fetchone()
    if row is None:
        return None
    previous = conn.execute("SELECT task_run_id FROM agent_messages WHERE conversation_id = ? AND task_run_id IS NOT NULL ORDER BY id DESC LIMIT 1", (conversation_id,)).fetchone()
    binding = _model_binding(conn, conversation)
    if task_run.task_type != "agent_chat" or task_run.worker_name != "project-agent" or task_run.worker_type != "pi":
        raise HTTPException(409, "agent_execution_binding_mismatch")
    task_run = task_run.model_copy(update={"status": "running", "session_id": conversation["session_id"], "intent_id": None,
        "runtime_context": {"execution_binding": binding.execution_provenance(), "model": binding.model_name,
                            "container_name": (task_run.runtime_context or {}).get("container_name"),
                            "agent_conversation_id": conversation_id, "agent_message_id": row["id"]}})
    run = insert_task_run_record(conn, project_id, task_run, principal=principal)
    conn.execute("UPDATE agent_messages SET task_run_id = ?, heartbeat_at = ?, revision = nextval('agent_message_revision') WHERE id = ?", (run.id, utcnow(), row["id"]))
    return {"message_id": row["id"], "conversation_id": conversation_id, "project_id": project_id,
            "task_run_id": run.id, "session_id": conversation["session_id"], "resume": previous is not None,
            "source_task_run_id": previous["task_run_id"] if previous else None,
            "prompt": row["text"], "attachments": attachments.list_attachments(conn, project_id, conversation_id, message_id=row["id"]),
            "model_id": binding.model_id, "execution_generation": run.execution_generation,
            "execution_binding": binding.model_dump(mode="json")}


def _owned_turn(conn, project_id, conversation_id, message_id, task_run_id, principal, *, terminal=False):
    conversation = _conversation(conn, project_id, conversation_id, lock=True)
    _creator_access(conn, conversation)
    row = conn.execute(_MESSAGE_SELECT + "WHERE m.project_id = ? AND m.conversation_id = ? AND m.id = ? AND m.task_run_id = ?",
                       (project_id, conversation_id, message_id, task_run_id)).fetchone()
    if row is None:
        raise HTTPException(404, "agent_turn_not_found")
    run = conn.execute("SELECT * FROM task_runs WHERE project_id = ? AND id = ?", (project_id, task_run_id)).fetchone()
    context = json.loads(run["runtime_context_json"])
    if context.get("dispatcher_id") != principal.subject_id:
        raise HTTPException(403, "execution_owner_mismatch")
    if not terminal and row["run_status"] != "running":
        raise HTTPException(409, "agent_turn_not_running")
    return row, run, context


def heartbeat(conn, project_id, conversation_id, message_id, task_run_id, principal):
    row, _, _ = _owned_turn(conn, project_id, conversation_id, message_id, task_run_id, principal, terminal=True)
    if row["run_status"] == "running":
        conn.execute("UPDATE agent_messages SET heartbeat_at = ? WHERE id = ?", (utcnow(), message_id))
    return {"cancelled": row["run_status"] != "running"}


def finish_turn(conn, project_id, conversation_id, message_id, body, principal):
    row, run, context = _owned_turn(conn, project_id, conversation_id, message_id, body.task_run_id, principal, terminal=True)
    text, error = body.text, body.error_code
    if body.status == "succeeded" and (not text or error):
        raise HTTPException(422, "agent_answer_required")
    if text:
        _admit_public(text, _platform_secrets(conn, project_id, body.task_run_id))
    if row["run_status"] != "running":
        if (row["run_status"], row["answer"], row["error_code"]) != (body.status, text, error):
            raise HTTPException(409, "agent_finish_conflict")
        return _message(conn, row)
    now = utcnow()
    context.update({"status": body.status, "ended_at": now, "usage": body.usage})
    conn.execute("UPDATE task_runs SET status = ?, ended_at = ?, error_type = ?, runtime_context_json = ? WHERE project_id = ? AND id = ?",
                 (body.status, now, "unknown" if error else None, _json(context), project_id, run["id"]))
    conn.execute("UPDATE agent_messages SET answer = ?, error_code = ?, revision = nextval('agent_message_revision') WHERE id = ?", (text, error, message_id))
    return _message(conn, conn.execute(_MESSAGE_SELECT + "WHERE m.id = ?", (message_id,)).fetchone())


def _message_actor(conn, row):
    user = conn.execute(
        "SELECT u.id, u.role, u.disabled, u.must_change_password FROM users u "
        "JOIN tenant_memberships m ON m.user_id = u.id AND m.tenant_id = ? "
        "WHERE u.id = ? AND m.role = 'owner'", (row["submitted_tenant_id"], row["submitted_by"]),
    ).fetchone()
    if user is None or user["disabled"] or user["must_change_password"]:
        raise HTTPException(403, {"code": "agent_action_actor_unavailable", "message": "本条消息的操作人身份不可用，请登录后重新发送。"})
    actor = RequestPrincipal("user", user["id"], tenant_id=row["submitted_tenant_id"], role=user["role"])
    if not _project_access_exists(conn, row["project_id"], actor):
        raise HTTPException(403, {"code": "agent_action_project_unavailable", "message": "消息操作人已无权访问来源项目。"})
    return actor


def _action_reference(row):
    title = {"verification": f"复测 {row['source_vulnerability_id']}", "report": "项目综合报告",
             "project": "新建项目", "intent": "追加研究任务"}[row["kind"]]
    return {"id": row["id"], "kind": row["kind"], "title": title,
            "source_vulnerability_id": row["source_vulnerability_id"], "source_observation_id": row["source_observation_id"]}


def _action_work(conn, row, project_id, principal):
    from . import verification_requests as verification
    from .assessment_reports import get_assessment_report_finalization
    from .agent_task_actions import task_action_work

    if row["kind"] in {"project", "intent"}:
        return task_action_work(conn, row, principal)
    work = _action_reference(row) | {"status": "unavailable", "error": None, "url": "", "download_url": None,
                                    "result": None, "next_attempt_at": None}
    try:
        if row["kind"] == "verification":
            request = verification._owned_request(conn, row["verification_request_id"], principal)
            detail = verification._summary(conn, request, principal)
            work.update(status=detail["execution_status"], url=f"/verifications/{request['id']}",
                        error=detail["error"]["message"] if detail["error"] else None,
                        result={key: detail["result"][key] for key in
                                ("outcome", "summary", "tested_scope", "limitations", "reason_code")} if detail["result"] else None)
            if detail["result"]:
                work["download_url"] = f"/api/v1/verification-requests/{request['id']}/report"
        else:
            if not _project_access_exists(conn, project_id, principal):
                raise HTTPException(404, "Project not found")
            current = get_assessment_report_finalization(conn, project_id)
            token = row["generation_token"]
            work.update(generation_token=token, accepted_generation_token=current.accepted_generation_token,
                        url=f"/projects/{project_id}")
            if current.generation_token == token:
                work.update(status={"pending": "queued", "ready": "succeeded"}.get(current.status, current.status),
                            error=current.last_error or None)
                # next_attempt_at 由原 finalization 按重试资格保存／清除；退避到期也仍需等待原调度器 claim。
                if current.status == "failed" and current.next_attempt_at is not None:
                    work.update(status="queued", next_attempt_at=current.next_attempt_at)
            else:
                work.update(status="superseded", error="已有新一轮报告请求，本轮不再执行。")
            # 新一代失败可能保留旧 accepted 文件；只把完全匹配的成果交给本次动作。
            if current.accepted_generation_token == token and current.accepted_artifact_id:
                work.update(status="succeeded", error=None,
                            download_url=f"/projects/{project_id}/artifacts/{current.accepted_artifact_id}/content",
                            url=f"/reports?reportProjectId={project_id}&reportId={current.accepted_artifact_id}")
            elif work["status"] == "succeeded":
                work.update(status="unavailable", error="本轮报告尚无已接受成果。")
    except HTTPException as exc:
        if exc.status_code not in {403, 404, 410}:
            raise
        work.update(status="unavailable", error="业务记录已不可访问，请检查权限或记录是否已删除。", url="")
    return work


def action_works(conn, project_id, conversation_id, principal):
    _conversation(conn, project_id, conversation_id)
    rows = conn.execute(
        "SELECT a.* FROM agent_actions a JOIN agent_messages m ON m.id = a.message_id "
        "WHERE m.project_id = ? AND m.conversation_id = ? ORDER BY m.id, a.id", (project_id, conversation_id),
    ).fetchall()
    return [_action_work(conn, row, project_id, principal) for row in rows]


def _request_action(conn, row, tool_call_id, method, params, actor):
    from cairn.server.schemas.verification_requests import CreateVerificationRequest
    from cairn.server.report_template_defaults_v60 import DEFAULT_REPORT_TEMPLATE_KEY_V60
    from .assessment_reports import request_assessment_report_generation
    from .verification_requests import create_verification_request

    prior = conn.execute("SELECT * FROM agent_actions WHERE message_id = ? AND tool_call_id = ?", (row["id"], tool_call_id)).fetchone()
    if prior:
        if prior["method"] != method or json.loads(prior["params_json"]) != params:
            raise HTTPException(409, {"code": "agent_action_conflict", "message": "同一次动作的参数已改变，请明确发起新请求。"})
        action = prior
    else:
        identifier = "act_" + uuid4().hex
        request_id, generation, vulnerability_id, observation_id = None, None, None, None
        if method == "request_vulnerability_retest":
            vulnerability_id = params["vulnerability_id"]
            observation_id = read_vulnerability_evidence(conn, row["project_id"], vulnerability_id, params.get("observation_id"))["observation_id"]
            detail, _ = create_verification_request(CreateVerificationRequest(
                schema_version=1, request_id=uuid4(), mode="retest", title=f"复测 {vulnerability_id}",
                target_url=params.get("target_url"),
                resource_batch_id=attachments.copy_attachments_to_batch(conn, row, params.get("attachment_ids", []), actor),
                report={"type": "platform_vulnerability", "project_id": row["project_id"],
                        "vulnerability_id": vulnerability_id, "observation_id": observation_id},
            ), actor, conn=conn)
            request_id = detail["id"]
            kind = "verification"
        else:
            finalization = request_assessment_report_generation(
                conn, row["project_id"], template_key=DEFAULT_REPORT_TEMPLATE_KEY_V60, requested_by=actor.subject_id,
            )
            generation, kind = finalization.generation_token, "report"
        conn.execute(
            "INSERT INTO agent_actions(id, message_id, tool_call_id, method, params_json, kind, verification_request_id, "
            "generation_token, source_vulnerability_id, source_observation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (identifier, row["id"], tool_call_id, method, _json(params), kind, request_id, generation, vulnerability_id, observation_id),
        )
        conn.execute("UPDATE agent_messages SET revision = nextval('agent_message_revision') WHERE id = ?", (row["id"],))
        action = conn.execute("SELECT * FROM agent_actions WHERE id = ?", (identifier,)).fetchone()
    data = {"action_id": action["id"], "work": _action_work(conn, action, row["project_id"], actor)}
    if action["kind"] == "verification":
        request = conn.execute("SELECT project_id FROM verification_requests WHERE id = ?", (action["verification_request_id"],)).fetchone()
        data.update(verification_request_id=action["verification_request_id"], project_id=request["project_id"],
                    source_project_id=row["project_id"], source_vulnerability_id=action["source_vulnerability_id"],
                    source_observation_id=action["source_observation_id"])
    else:
        data.update(project_id=row["project_id"], generation_token=action["generation_token"])
    return data


def _tool_params(method, params):
    from .agent_research_context import SESSION_PAGE_METHOD, validate_research_params

    from .agent_task_actions import TASK_ACTION_METHODS, validate_task_action_params

    if method in TASK_ACTION_METHODS:
        validate_task_action_params(method, params)
        return
    if method == "publish_custom_markdown":
        if (set(params) - {"title", "content", "sources", "read_at", "limitations"}
                or not isinstance(params.get("title"), str) or not 1 <= len(params["title"]) <= 200
                or any(char in params["title"] for char in "/\\\r\n\0")
                or not isinstance(params.get("content"), str) or not params["content"].strip()
                or len(params["content"].encode("utf-8")) > 1024 * 1024):
            raise HTTPException(422, "agent_tool_parameters_invalid")
        for key in ("sources", "limitations"):
            value = params.get(key, [])
            if not isinstance(value, list) or len(value) > 100 or any(not isinstance(item, str) or len(item) > 4096 for item in value):
                raise HTTPException(422, "agent_tool_parameters_invalid")
        if "read_at" in params:
            try:
                if datetime.fromisoformat(params["read_at"].replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError("timezone required")
            except (AttributeError, ValueError, TypeError) as exc:
                raise HTTPException(422, "agent_tool_parameters_invalid") from exc
        return
    validate_research_params(method, params)
    if method == SESSION_PAGE_METHOD:
        return
    allowed = {"query_project_progress": set(), "list_vulnerabilities": {"limit", "offset"},
               "list_research_sessions": {"limit", "offset"},
               "read_research_context": {"source", "task_run_id", "item_id", "side", "limit", "offset", "chunk_size"},
               "read_research_session": {"task_run_id", "limit", "offset"},
               "read_vulnerability": {"vulnerability_id", "observation_id"},
               "export_vulnerability_report": {"vulnerability_id", "observation_id", "template_key"},
               "request_vulnerability_retest": {"vulnerability_id", "observation_id", "attachment_ids", "target_url"},
               "list_conversation_attachments": {"limit", "offset"},
               "read_conversation_attachment": {"attachment_id", "offset", "chunk_size"},
               "_read_conversation_attachment_bytes": {"attachment_id"},
               "request_project_report": set(), "query_agent_work": {"action_id"}}
    if method not in allowed or set(params) - allowed[method]:
        raise HTTPException(422, "agent_tool_parameters_invalid")
    if method in {"read_conversation_attachment", "_read_conversation_attachment_bytes"} and "attachment_id" not in params:
        raise HTTPException(422, "agent_tool_parameters_invalid")
    for key, value in params.items():
        if key == "attachment_ids":
            attachments.validate_attachment_ids(value)
        elif key == "target_url":
            from cairn.server.schemas.verification_requests import CreateVerificationRequest

            try:
                if not isinstance(value, str) or len(value) > 4096:
                    raise ValueError("invalid target URL")
                CreateVerificationRequest.explicit_target_url(value)
            except ValueError as exc:
                raise HTTPException(422, "agent_tool_parameters_invalid") from exc
        elif key in {"limit", "offset", "chunk_size"}:
            maximum = {"limit": 100, "offset": 2**31 - 1, "chunk_size": 16384}[key]
            if type(value) is not int or not (0 if key == "offset" else 1) <= value <= maximum:
                raise HTTPException(422, "agent_tool_parameters_invalid")
        elif not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", value):
            raise HTTPException(422, "agent_tool_parameters_invalid")
    if method in {"read_vulnerability", "export_vulnerability_report", "request_vulnerability_retest"} and "vulnerability_id" not in params:
        raise HTTPException(422, "agent_tool_parameters_invalid")
    if method == "query_agent_work" and "action_id" not in params:
        raise HTTPException(422, "agent_tool_parameters_invalid")


def call_tool(project_id, conversation_id, message_id, task_run_id, method, params, principal, *, tool_call_id):
    try:
        return _call_tool(project_id, conversation_id, message_id, task_run_id, method, params, principal, tool_call_id=tool_call_id)
    except HTTPException as exc:
        # 只传播业务 HTTP 错误；执行异常、堆栈和响应原文不进入模型。
        if isinstance(exc.detail, dict):
            detail = exc.detail
        else:
            code = str(exc.detail)
            labels = {
                "assessment_report_tasks_not_quiescent": "项目研究工作尚未结束，暂不能生成综合报告。",
                "assessment_report_stopped_tasks_not_quiescent": "项目的研究执行尚未确认停止，请先查看运行记录。",
                "project_pause_draining": "项目正在暂停，执行尚未确认停止。",
                "assessment_report_project_active": "项目仍在研究中，本轮不支持阶段报告，也不会自动停止项目。",
                "assessment_report_project_paused": "综合报告仅支持已完成或已结束的项目。",
                "assessment_report_project_status_invalid": "当前项目状态不支持生成综合报告。",
                "assessment_report_not_supported": "当前项目不支持生成综合报告。",
                "assessment_report_generation_pending": "已有报告正在生成，请查询当前报告。",
                "assessment_report_cancellation_pending": "上一轮报告仍在停止，请稍后查询。",
                "assessment_report_configuration_invalid": "报告执行配置不可用，请检查报告 Worker 与模型配置。",
                "agent_tool_parameters_invalid": "工具参数不符合当前业务要求。",
            }
            if code in labels:
                detail = {"code": code, "message": labels[code]}
            elif re.search(r"[一-鿿]", code):
                detail = {"code": "agent_business_rejected", "message": code}
                if "专用 Worker" in code or "复测模型" in code:
                    detail["action_url"] = "/console/verification-configuration"
            else:
                detail = {"code": "agent_business_rejected", "message": f"业务请求未受理（HTTP {exc.status_code}），请查看对应业务页面。"}
        with get_conn() as conn:
            _admit_public(detail, _platform_secrets(conn, project_id, task_run_id))
        raise HTTPException(exc.status_code, detail) from exc


def _publish_custom_markdown(conn, row, tool_call_id, params, secrets):
    from cairn.server.db import register_after_rollback
    from .audit_artifacts import _artifact_disk_path_from_row
    from .verification_requests import _artifact_deliverable

    _admit_public(params, secrets)
    digest = sha256(json.dumps(params, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    prior = conn.execute(
        "SELECT * FROM artifacts WHERE project_id = ? AND task_run_id = ? "
        "AND metadata_json::json->>'agent_tool_call_id' = ?",
        (row["project_id"], row["task_run_id"], tool_call_id),
    ).fetchone()
    if prior:
        if json.loads(prior["metadata_json"]).get("agent_request_sha256") != digest:
            raise HTTPException(409, "同一次交付的参数已改变，请明确发起新请求。")
        identifier = prior["id"]
    else:
        title = params["title"] if params["title"].endswith(".md") else params["title"] + ".md"
        artifact = create_artifact_record(row["project_id"], CreateArtifactRequest(
            task_run_id=row["task_run_id"], kind="report", filename=f"agent-custom-{uuid4().hex}.md",
            content=params["content"], metadata={
                "title": title, "agent_conversation_id": row["conversation_id"], "agent_message_id": row["id"],
                "agent_tool_call_id": tool_call_id, "agent_request_sha256": digest,
                "custom_report": True, "sources": params.get("sources", []), "read_at": params.get("read_at"),
                "limitations": params.get("limitations", []), "published_at": utcnow(),
            },
        ), conn=conn)
        stored = conn.execute("SELECT * FROM artifacts WHERE project_id = ? AND id = ?", (row["project_id"], artifact.id)).fetchone()
        disk_path = _artifact_disk_path_from_row(stored)
        register_after_rollback(conn, lambda: disk_path.unlink(missing_ok=True))
        if not _artifact_deliverable(conn, stored):
            raise HTTPException(422, "agent_public_content_blocked")
        identifier = artifact.id
        ids = json.loads(row["artifact_ids_json"])
        ids.append(identifier)
        conn.execute("UPDATE agent_messages SET artifact_ids_json = ?, revision = nextval('agent_message_revision') WHERE id = ?",
                     (_json(ids), row["id"]))
    current = conn.execute("SELECT * FROM agent_messages WHERE id = ?", (row["id"],)).fetchone()
    reference = next(item for item in _artifacts(conn, current) if item["id"] == identifier)
    return {"project_id": row["project_id"], "artifact": reference, "status": "succeeded"}


def _call_tool(project_id, conversation_id, message_id, task_run_id, method, params, principal, *, tool_call_id):
    _tool_params(method, params)
    with get_conn() as conn:
        row, _, _ = _owned_turn(conn, project_id, conversation_id, message_id, task_run_id, principal)
        secrets = _platform_secrets(conn, project_id, task_run_id)
        if method in {"create_project", "append_project_intent"}:
            from .agent_task_actions import request_task_action

            actor = _message_actor(conn, row)
            action = request_task_action(conn, row, tool_call_id, method, params, actor)
            data = {"action_id": action["id"], "project_id": action["business_project_id"],
                    "intent_id": action["intent_id"], "work": _action_work(conn, action, project_id, actor)}
        elif method == "list_conversation_attachments":
            items = attachments.list_attachments(conn, project_id, conversation_id, sent_only=True)
            offset, limit = params.get("offset", 0), params.get("limit", 20)
            data = {"items": items[offset:offset + limit], "total": len(items),
                    "next_offset": offset + limit if offset + limit < len(items) else None}
        elif method in {"read_conversation_attachment", "_read_conversation_attachment_bytes"}:
            data = attachments.read_attachment(conn, row, params, binary=method.startswith("_"))
            if "content_base64" in data:
                _admit_public(b64decode(data["content_base64"]), secrets)
        elif method == "publish_custom_markdown":
            data = _publish_custom_markdown(conn, row, tool_call_id, params, secrets)
        elif method in {"request_vulnerability_retest", "request_project_report", "query_agent_work"}:
            actor = _message_actor(conn, row)
            if method == "query_agent_work":
                action = conn.execute(
                    "SELECT a.* FROM agent_actions a JOIN agent_messages m ON m.id = a.message_id "
                    "WHERE a.id = ? AND m.project_id = ? AND m.conversation_id = ?",
                    (params["action_id"], project_id, conversation_id),
                ).fetchone()
                if action is None:
                    raise HTTPException(404, {"code": "agent_action_not_found", "message": "当前会话中没有此项工作。"})
                data = {"action_id": action["id"], "work": _action_work(conn, action, project_id, actor)}
            else:
                data = _request_action(conn, row, tool_call_id, method, params, actor)
        elif method in {"list_research_sessions", "read_research_context", "read_research_session", "_admit_research_session_page"}:
            from .agent_research_context import (
                admit_research_session_page, list_research_sessions, read_research_context, research_session_reference,
            )

            handler = {"list_research_sessions": list_research_sessions, "read_research_context": read_research_context,
                       "read_research_session": research_session_reference, "_admit_research_session_page": admit_research_session_page}[method]
            data = handler(conn, project_id, params)
        elif method == "query_project_progress":
            project = get_project_or_404(conn, project_id)
            data = {"project_id": project_id, "status": project["status"], "queried_at": utcnow(),
                    "runtime": build_project_runtime(conn, project, schema_version=2).model_dump(mode="json"),
                    "saved_vulnerabilities": conn.execute("SELECT count(*) AS count FROM vulnerabilities WHERE project_id = ?", (project_id,)).fetchone()["count"]}
        elif method == "list_vulnerabilities":
            items = list_vulnerabilities(conn, project_id)
            offset, limit = params.get("offset", 0), params.get("limit", 20)
            data = {"items": [item.model_dump(mode="json") for item in items[offset:offset + limit]], "total": len(items),
                    "next_offset": offset + limit if offset + limit < len(items) else None}
        elif method == "read_vulnerability":
            detail = get_vulnerability_detail(conn, project_id, params["vulnerability_id"], params.get("observation_id"))
            data = {"project_id": project_id, "vulnerability_id": detail.id, "observation_id": detail.observation_id,
                    "title": detail.title, "status": detail.status, "severity": detail.severity,
                    "readable": detail.readable.model_dump(mode="json") if detail.readable is not None else None,
                    "available_observations": [item.id for item in detail.observations]}
        else:
            # 只先确定来源；完整详情由报告服务读取一次，不为导出再装载一遍证据正文。
            vulnerability_id = params["vulnerability_id"]
            observation_id = read_vulnerability_evidence(conn, project_id, vulnerability_id, params.get("observation_id"))["observation_id"]
            markdown, title = render_vulnerability_report(conn, project_id, vulnerability_id, observation_id=observation_id, template_key=params.get("template_key"))
            _admit_public([markdown, title], secrets)
            data = None
        if data is not None:
            _admit_public(data, secrets)
            return data
    # 成品由现有 Artifact 服务落盘；不在消息行存第二份漏洞正文。
    artifact = create_artifact_record(project_id, CreateArtifactRequest(
        task_run_id=task_run_id, kind="report", filename=f"agent-{uuid4().hex}.md", content=markdown,
        metadata={"title": title + ".md", "agent_conversation_id": conversation_id, "agent_message_id": message_id,
                  "vulnerability_id": vulnerability_id, "observation_id": observation_id},
    ))
    with get_conn() as conn:
        row, _, _ = _owned_turn(conn, project_id, conversation_id, message_id, task_run_id, principal)
        ids = json.loads(row["artifact_ids_json"])
        ids.append(artifact.id)
        conn.execute("UPDATE agent_messages SET artifact_ids_json = ?, revision = nextval('agent_message_revision') WHERE id = ?", (_json(ids), message_id))
        row = conn.execute("SELECT * FROM agent_messages WHERE id = ?", (message_id,)).fetchone()
        return {"project_id": project_id, "vulnerability_id": vulnerability_id, "observation_id": observation_id,
                "artifact": _artifacts(conn, row)[-1]}


def conversation_artifact(conn, project_id, conversation_id, artifact_id):
    # 整理聊天历史不撤销项目成果；下载仍由项目权限、来源及内容准出检查裁决。
    row = conn.execute("SELECT * FROM artifacts WHERE project_id = ? AND id = ?", (project_id, artifact_id)).fetchone()
    if row is None or json.loads(row["metadata_json"]).get("agent_conversation_id") != conversation_id:
        raise HTTPException(404, "agent_artifact_not_found")
    return row
