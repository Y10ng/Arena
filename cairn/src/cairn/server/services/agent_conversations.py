"""会话受理、单写者认领与既有成果工具；执行状态只取 TaskRun。"""

from __future__ import annotations

import json
import logging
import re
from base64 import b64decode
from hashlib import sha256
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
import psycopg

from cairn.server.auth import _project_access_exists, RequestPrincipal
from cairn.server.db import get_conn
from . import agent_attachments as attachments
from . import conversation_files, agent_verifications
from .audit_task_runs import authenticated_execution_owner
from .ids import get_project_or_404
from .project_runtime_diagnostics import build_project_runtime
from .model_execution import model_availability, resolve_model_execution_binding
from .project_agent_configuration import default_model_id
from .time_utils import utcnow
from .verification_requests import _platform_secrets
from .vulnerabilities import get_vulnerability_detail, list_vulnerabilities, read_vulnerability_evidence
from .vulnerability_reports import render_vulnerability_report_material
from .agent_material_sources import SOURCE_METHODS, describe_source, public_sources, verified_source

LOG = logging.getLogger(__name__)


# 交互准入与参数名称共用一个来源；claim 只公开模型工具，不公开可信桥方法。
INTERACTION_TOOL_PARAMS = {
    "list_authorized_projects": {"limit", "offset"},
    "query_project_progress": {"project_id"},
    "list_research_sessions": {"limit", "offset"},
    "read_research_context": {"source", "task_run_id", "item_id", "side", "limit", "offset", "chunk_size"},
    "read_research_session": {"task_run_id", "limit", "offset"},
    "list_conversation_attachments": {"limit", "offset"},
    "read_conversation_attachment": {"attachment_id", "offset", "chunk_size"},
    "publish_custom_markdown": {"title", "content", "source_references", "source_notes", "limitations"},
    "list_vulnerabilities": {"limit", "offset"},
    "read_vulnerability": {"vulnerability_id", "observation_id"},
    "export_vulnerability_report": {"vulnerability_id", "observation_id"},
    "list_report_templates": set(),
    "request_project_report": {"template_key"},
    "query_agent_work": {"action_id"},
    "start_conversation_research": {"project_id", "vulnerability_id", "observation_id"},
    "list_conversation_research": {"project_id", "vulnerability_id", "observation_id", "limit", "offset"},
    "read_conversation_research": {"conversation_id", "task_run_id", "flow_id", "side", "limit", "cursor", "offset", "chunk_size", "source", "artifact_id"},
    "register_conversation_material": {"filename", "source_path", "content_base64"},
    **agent_verifications.TOOL_PARAMS,
}
# 仅既有只读取材与导出接受显式项目；报告请求仍沿固定消息上下文，历史动作按真实来源读取。
_EXPLICIT_PROJECT_TOOLS = {"query_project_progress", "list_research_sessions", "read_research_context",
                           "read_research_session", "list_vulnerabilities", "read_vulnerability", "export_vulnerability_report"}
for _name in _EXPLICIT_PROJECT_TOOLS:
    INTERACTION_TOOL_PARAMS[_name].add("project_id")
_TRUSTED_BRIDGE_PARAMS = {
    "_read_conversation_attachment_bytes": {"attachment_id"},
    "_read_conversation_research_bytes": {"conversation_id", "task_run_id", "artifact_id", "source"},
    "_admit_research_session_page": {"project_id", "task_run_id", "page"},
    "_validate_workspace_claim": set(),
    "_resolve_conversation_research": set(),
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _conversation(conn, conversation_id, *, lock=False, include_deleted=False):
    row = conn.execute(
        "SELECT * FROM agent_conversations WHERE id = ?" + (" FOR UPDATE" if lock else ""),
        (conversation_id,),
    ).fetchone()
    if row is None or (row["deleted_at"] is not None and not include_deleted):
        raise HTTPException(404, "agent_conversation_not_found")
    return row


def require_owner(conn, conversation_id, principal, *, lock=False, include_deleted=False):
    row = _conversation(conn, conversation_id, lock=lock, include_deleted=include_deleted)
    if principal.kind != "user" or (row["tenant_id"], row["created_by"]) != (principal.tenant_id, principal.subject_id):
        raise HTTPException(404, "agent_conversation_not_found")
    _creator_access(conn, row)
    return row


def _public_conversation(conn, row):
    result = {key: row[key] for key in ("id", "project_id", "created_at")} | model_availability(
        conn, tenant_id=row["tenant_id"], model_id=row["model_id"],
    )
    if row["unavailable_reason"]:
        result.update(available=False, unavailable_reason=row["unavailable_reason"])
    return result


def _model_binding(conn, conversation):
    return resolve_model_execution_binding(conn, tenant_id=conversation["tenant_id"], model_id=conversation["model_id"])


def conversation_secrets(conn, conversation_id, task_run_id=None):
    from .verification_requests import _execution_secrets
    conversation = _conversation(conn, conversation_id, include_deleted=True)
    if task_run_id is None:
        return _binding_secrets(_model_binding(conn, conversation))
    run = conn.execute("SELECT * FROM task_runs WHERE conversation_id = ? AND id = ?", (conversation_id, task_run_id)).fetchone()
    if run is None:
        raise HTTPException(409, "agent_execution_source_unavailable")
    try:
        return _execution_secrets(conn, conversation["tenant_id"], [run])
    except HTTPException as exc:
        if exc.detail not in {"verification_execution_secrets_unavailable", "verification_execution_headers_unavailable"}:
            raise
        raise HTTPException(409, "agent_execution_source_unavailable") from exc


def list_conversations(conn, principal):
    return [_public_conversation(conn, row) for row in conn.execute(
        "SELECT * FROM agent_conversations WHERE tenant_id = ? AND created_by = ? AND deleted_at IS NULL ORDER BY created_at, id",
        (principal.tenant_id, principal.subject_id),
    ).fetchall()]


def delete_conversation(conn, conversation_id, principal):
    conversation = require_owner(conn, conversation_id, principal, lock=True, include_deleted=True)
    # 与提交、认领共用会话行锁；独立报告和复测不属于聊天在途工作。
    if conn.execute(_MESSAGE_SELECT + "WHERE m.conversation_id = ? AND "
                    "(m.task_run_id IS NULL OR t.status IN ('queued', 'running')) LIMIT 1",
                    (conversation_id,)).fetchone():
        raise HTTPException(409, "会话仍有排队、执行或结束待确认的消息，暂不能删除。")
    from .conversation_cleanup import request_cleanup, public_status
    return {"conversation_id": conversation_id, "cleanup": public_status(request_cleanup(conn, conversation))}


def stop_turn(conn, conversation_id, message_id, principal):
    require_owner(conn, conversation_id, principal, lock=True)
    row = conn.execute(_MESSAGE_SELECT + "WHERE m.conversation_id = ? AND m.id = ?", (conversation_id, message_id)).fetchone()
    if row is None:
        raise HTTPException(404, "agent_turn_not_found")
    if row["run_status"] == "running":
        conn.execute("UPDATE agent_messages SET cancel_requested_at = COALESCE(cancel_requested_at, ?), "
                     "revision = nextval('agent_message_revision') WHERE id = ?", (utcnow(), message_id))
    elif row["task_run_id"] is None:
        raise HTTPException(409, "agent_turn_not_started")
    return _message(conn, conn.execute(_MESSAGE_SELECT + "WHERE m.id = ?", (message_id,)).fetchone())


def _context_access(conn, project_id, principal):
    if project_id is not None and not _project_access_exists(conn, project_id, principal):
        raise HTTPException(404, "Project not found")


def create_conversation(conn, principal, project_id=None):
    if principal.kind != "user" or not principal.tenant_id:
        raise HTTPException(403, "User tenant is required")
    _context_access(conn, project_id, principal)
    model_id = default_model_id(conn, principal.tenant_id)
    resolve_model_execution_binding(conn, tenant_id=principal.tenant_id, model_id=model_id)
    identifier = "conv_" + uuid4().hex
    conn.execute(
        "INSERT INTO agent_conversations(id, tenant_id, project_id, model_id, created_by, session_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (identifier, principal.tenant_id, project_id, model_id, principal.subject_id, "pi:" + str(uuid4()), utcnow()),
    )
    return _public_conversation(conn, _conversation(conn, identifier))


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
        artifact = conn.execute("SELECT id, metadata_json FROM artifacts WHERE conversation_id = ? AND id = ?", (row["conversation_id"], identifier)).fetchone()
        if artifact:
            metadata = json.loads(artifact["metadata_json"])
            result.append({"id": identifier, "title": metadata["title"], "sources": public_sources(metadata),
                           **({"source_project_id": metadata["source_project_id"]} if "source_project_id" in metadata else {}),
                           "download_url": f"/agent-conversations/{row['conversation_id']}/artifacts/{identifier}"})
    return result


def _project_consumption(conn, task_run_ids, *, conversation_id):
    if not task_run_ids:
        return {}, False
    from .consumption import message_consumption
    conn.execute('SAVEPOINT chat_consumption_projection')
    try:
        # 计量查询仅为辅助展示；限时并回滚到 savepoint，不能中止聊天写事务。
        conn.execute("SET LOCAL statement_timeout = '2s'")
        result = message_consumption(conn, task_run_ids, conversation_id=conversation_id)
    except psycopg.Error as exc:
        conn.execute('ROLLBACK TO SAVEPOINT chat_consumption_projection')
        conn.execute('RELEASE SAVEPOINT chat_consumption_projection')
        LOG.warning('chat consumption projection unavailable error=%s', type(exc).__name__)
        return {}, True
    conn.execute('ROLLBACK TO SAVEPOINT chat_consumption_projection')
    conn.execute('RELEASE SAVEPOINT chat_consumption_projection')
    return result, False


def _message(conn, row, consumption=None, *, consumption_error=False):
    if consumption is None:
        ids = [row['task_run_id']] if row['task_run_id'] else []
        consumption, consumption_error = _project_consumption(conn, ids, conversation_id=row['conversation_id'])
    measured = consumption.get(row['task_run_id'], {})
    observed = {key: value for key, value in measured.items() if key != 'unobserved_bindings'}
    status = row["run_status"] or "queued"
    error = row["error_code"]
    if row["task_run_id"] is None:
        configuration = _public_conversation(conn, _conversation(conn, row["conversation_id"]))
        error = configuration["unavailable_reason"]
    if status == "running" and row["heartbeat_at"]:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["heartbeat_at"].replace("Z", "+00:00"))).total_seconds()
        if age > 120:
            error = "execution_unconfirmed"
    return {"id": row["id"], "client_message_id": row["client_message_id"], "text": row["text"],
            "created_at": row["created_at"], "project_id": row["project_id"], "task_run_id": row["task_run_id"], "status": status,
            "answer": row["answer"], "error_code": error, "cancel_requested_at": row["cancel_requested_at"], "artifacts": _artifacts(conn, row),
            "attachments": attachments.list_attachments(conn, row["conversation_id"], message_id=row["id"]),
            "actions": [_action_reference(action) for action in conn.execute(
                "SELECT * FROM agent_actions WHERE message_id = ? ORDER BY id", (row["id"],)).fetchall()],
            "revision": row["revision"],
            "usage": observed.get('usage', {field: None for field in (
                'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens',
            )}), 'usage_missing_responses': observed.get('partial_responses'),
            'unobserved_consumption_bindings': None if consumption_error else measured.get('unobserved_bindings', 0),
            'consumption_error': consumption_error, 'consumption': observed or None}


_MESSAGE_SELECT = """SELECT m.*, t.status AS run_status
FROM agent_messages m LEFT JOIN task_runs t
    ON t.conversation_id = m.conversation_id AND t.id = m.task_run_id """


def messages(conn, conversation_id, *, after=0, revision=None):
    _conversation(conn, conversation_id)
    field, cursor = ("m.id", after) if revision is None else ("m.revision", revision)
    rows = conn.execute(_MESSAGE_SELECT + f"WHERE m.conversation_id = ? AND {field} > ? ORDER BY m.id",
                        (conversation_id, cursor)).fetchall()
    consumption, consumption_error = _project_consumption(
        conn, [row['task_run_id'] for row in rows if row['task_run_id']], conversation_id=conversation_id)
    return [_message(conn, row, consumption, consumption_error=consumption_error and row['task_run_id'] is not None)
            for row in rows]


def submit_message(conn, conversation_id, client_message_id, text, principal, attachment_ids=None, *, project_id=None):
    attachment_ids = attachment_ids or []
    attachments.validate_attachment_ids(attachment_ids)
    conversation = require_owner(conn, conversation_id, principal, lock=True)
    if conversation["unavailable_reason"]:
        raise HTTPException(409, conversation["unavailable_reason"])
    _context_access(conn, project_id, principal)
    prior = conn.execute(_MESSAGE_SELECT + "WHERE m.conversation_id = ? AND m.client_message_id = ?", (conversation_id, client_message_id)).fetchone()
    if prior:
        prior_ids = {item["id"] for item in attachments.list_attachments(conn, conversation_id, message_id=prior["id"])}
        if prior["text"] != text or prior["submitted_by"] != principal.subject_id or prior["project_id"] != project_id or prior_ids != set(attachment_ids):
            raise HTTPException(409, "agent_message_id_conflict")
        return _message(conn, prior)
    binding = _model_binding(conn, conversation)
    _admit_public(text, _binding_secrets(binding))
    row = conn.execute(
        "INSERT INTO agent_messages(conversation_id, project_id, client_message_id, text, created_at, submitted_by, submitted_tenant_id) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (conversation_id, project_id, client_message_id, text, utcnow(), principal.subject_id, principal.tenant_id),
    ).fetchone()
    attachments.bind_attachments(conn, conversation_id, row["id"], attachment_ids, principal)
    return _message(conn, conn.execute(_MESSAGE_SELECT + "WHERE m.id = ?", (row["id"],)).fetchone())


def pending_conversations(conn, *, after=""):
    return [dict(row) for row in conn.execute(
        "SELECT c.id AS conversation_id, c.session_machine_id FROM agent_conversations c "
        "WHERE c.id > ? AND c.deleted_at IS NULL AND c.unavailable_reason IS NULL "
        "AND EXISTS (SELECT 1 FROM agent_messages m WHERE m.conversation_id = c.id AND m.task_run_id IS NULL) "
        "AND NOT EXISTS (SELECT 1 FROM agent_messages m JOIN task_runs t ON t.conversation_id = m.conversation_id AND t.id = m.task_run_id "
        "WHERE m.conversation_id = c.id AND t.status IN ('queued', 'running')) ORDER BY c.id LIMIT 100", (after,),
    ).fetchall()]


def _creator_access(conn, conversation):
    user = conn.execute("SELECT u.* FROM users u JOIN tenant_memberships m ON m.user_id = u.id "
                        "WHERE u.id = ? AND m.tenant_id = ? AND m.role = 'owner'",
                        (conversation["created_by"], conversation["tenant_id"])).fetchone()
    if user is None or user["disabled"] or user["must_change_password"]:
        raise HTTPException(403, "agent_conversation_owner_unavailable")


def claim_turn(conn, conversation_id, request_id, principal):
    owner = authenticated_execution_owner(principal)
    conversation = _conversation(conn, conversation_id, lock=True)
    prior = conn.execute(_MESSAGE_SELECT + "WHERE m.conversation_id = ? AND m.claim_request_id = ?",
                         (conversation_id, request_id)).fetchone()
    if prior:
        row, run, context = _owned_turn(conn, conversation_id, prior["id"], prior["task_run_id"], principal, terminal=True)
        # 响应丢失时仍返回原轮次；已收口的请求绝不再认领另一条消息。
        receipt = {"conversation_id": conversation_id, "message_id": row["id"], "task_run_id": run["id"],
                   "request_id": request_id, "status": run["status"]}
        if run["status"] != "running":
            return receipt
        try:
            _creator_access(conn, conversation)
            _message_actor(conn, row)
            return _claim_response(conn, conversation, row, run, context)
        except HTTPException as exc:
            code = exc.detail.get("code") if isinstance(exc.detail, dict) else exc.detail
            if code not in {"agent_conversation_owner_unavailable", "agent_action_actor_unavailable",
                            "agent_execution_source_unavailable", "model_unconfigured", "model_disabled",
                            "model_protocol_unsupported", "model_incomplete", "credential_missing",
                            "model_endpoint_invalid", "credential_unavailable"}:
                raise
            # 只返回已受理身份供原机器失败收口，不再返回已撤权正文或不可验证的凭据。
            return {**receipt, "claim_error": code}
    if conversation["unavailable_reason"]:
        raise HTTPException(409, conversation["unavailable_reason"])
    if conversation["session_machine_id"] not in (None, owner):
        raise HTTPException(409, "agent_session_machine_mismatch")
    _creator_access(conn, conversation)
    if conn.execute(_MESSAGE_SELECT + "WHERE m.conversation_id = ? AND t.status IN ('queued', 'running') LIMIT 1", (conversation_id,)).fetchone():
        return None
    if conversation["session_initialized"] is None:
        raise HTTPException(409, "session_verification_required")
    row = conn.execute("SELECT * FROM agent_messages WHERE conversation_id = ? AND task_run_id IS NULL ORDER BY id LIMIT 1", (conversation_id,)).fetchone()
    if row is None:
        return None
    _message_actor(conn, row)
    binding = _model_binding(conn, conversation)
    previous = conn.execute("SELECT task_run_id FROM agent_messages WHERE conversation_id = ? AND task_run_id IS NOT NULL ORDER BY id DESC LIMIT 1", (conversation_id,)).fetchone()
    identifier, now = "tr_" + uuid4().hex, utcnow()
    context = {"execution_binding": binding.execution_provenance(), "model": binding.model_name,
               "consumption_source": "receipts-v1",
               "dispatcher_id": owner, "conversation_id": conversation_id, "agent_message_id": row["id"],
               "session_id": conversation["session_id"], "status": "running", "started_at": now,
               "resume": conversation["session_initialized"],
               "source_task_run_id": previous["task_run_id"] if previous and conversation["session_initialized"] else None,
               "artifact_root": f"artifact://conversations/{conversation_id}/runs/{identifier}"}
    # 聊天直接写同一 TaskRun 表，不读取研究 generation、项目状态或研究认领。
    conn.execute("INSERT INTO task_runs(id, conversation_id, task_type, worker_name, worker_type, status, session_id, started_at, runtime_context_json) "
                 "VALUES (?, ?, 'agent_chat', 'project-agent', 'pi', 'running', ?, ?, ?)",
                 (identifier, conversation_id, conversation["session_id"], now, _json(context)))
    conn.execute("UPDATE agent_messages SET task_run_id = ?, claim_request_id = ?, heartbeat_at = ?, revision = nextval('agent_message_revision') WHERE id = ?",
                 (identifier, request_id, now, row["id"]))
    conn.execute("UPDATE agent_conversations SET session_machine_id = ?, session_initialized = NULL WHERE id = ?", (owner, conversation_id))
    row = conn.execute("SELECT * FROM agent_messages WHERE id = ?", (row["id"],)).fetchone()
    run = conn.execute("SELECT * FROM task_runs WHERE conversation_id = ? AND id = ?", (conversation_id, identifier)).fetchone()
    return _claim_response(conn, conversation, row, run, context)


def _claim_response(conn, conversation, row, run, context):
    binding = _model_binding(conn, conversation)
    if binding.execution_provenance() != context["execution_binding"]:
        raise HTTPException(409, "agent_execution_source_unavailable")
    return {"message_id": row["id"], "conversation_id": conversation["id"], "project_id": row["project_id"],
            "request_id": row["claim_request_id"], "status": run["status"],
            "task_run_id": run["id"], "session_id": run["session_id"], "resume": context["resume"],
            "session_machine_id": context["dispatcher_id"], "source_task_run_id": context["source_task_run_id"],
            "prompt": row["text"], "attachments": attachments.list_attachments(conn, conversation["id"], message_id=row["id"]),
            "model_id": binding.model_id, "execution_binding": binding.model_dump(mode="json"),
            "allowed_tools": list(INTERACTION_TOOL_PARAMS)}


def _owned_turn(conn, conversation_id, message_id, task_run_id, principal, *, terminal=False):
    conversation = _conversation(conn, conversation_id, lock=True)
    # 机器收口不依赖用户当前权限；工具调用仍单独检查消息操作人。
    if not terminal:
        _creator_access(conn, conversation)
    row = conn.execute(_MESSAGE_SELECT + "WHERE m.conversation_id = ? AND m.id = ? AND m.task_run_id = ?",
                       (conversation_id, message_id, task_run_id)).fetchone()
    if row is None:
        raise HTTPException(404, "agent_turn_not_found")
    run = conn.execute("SELECT * FROM task_runs WHERE conversation_id = ? AND id = ?", (conversation_id, task_run_id)).fetchone()
    context = json.loads(run["runtime_context_json"])
    if principal.kind != "machine" or context.get("dispatcher_id") != principal.subject_id:
        raise HTTPException(403, "execution_owner_mismatch")
    if not terminal and (row["run_status"] != "running" or row["cancel_requested_at"] is not None):
        raise HTTPException(409, "agent_turn_not_running")
    return row, run, context


def heartbeat(conn, conversation_id, message_id, task_run_id, principal):
    row, _, _ = _owned_turn(conn, conversation_id, message_id, task_run_id, principal, terminal=True)
    if row["run_status"] == "running":
        conn.execute("UPDATE agent_messages SET heartbeat_at = ? WHERE id = ?", (utcnow(), message_id))
    return {"cancelled": row["run_status"] != "running" or row["cancel_requested_at"] is not None}


def finish_turn(conn, conversation_id, message_id, body, principal):
    row, run, context = _owned_turn(conn, conversation_id, message_id, body.task_run_id, principal, terminal=True)
    text, error = body.text, body.error_code
    if body.status == "succeeded" and (not text or error):
        raise HTTPException(422, "agent_answer_required")
    if row["run_status"] != "running":
        metered = context.get('consumption_source') == 'receipts-v1'
        if (row["run_status"], row["answer"], row["error_code"], None if metered else context.get("usage"),
                None if metered else context.get("usage_missing_responses"), context.get("session_initialized")) != (
                body.status, text, error, None if metered else body.usage,
                None if metered else body.usage_missing_responses, body.session_initialized):
            raise HTTPException(409, "agent_finish_conflict")
        return _message(conn, row)
    if text:
        _admit_public(text, conversation_secrets(conn, conversation_id, body.task_run_id))
    # 来源不可核验只阻断正文；原机器可提交空正文失败及原 usage，不吞掉其他冲突。
    now = utcnow()
    initialized = body.session_initialized
    if context.get("resume") and initialized is False:
        raise HTTPException(409, "agent_session_state_conflict")
    context.update({'status': body.status, 'ended_at': now, 'session_initialized': initialized})
    if context.get('consumption_source') != 'receipts-v1':
        # 旧执行原件保持兼容；新执行不再维护第二套消息累计来源。
        context.update({'usage': body.usage, 'usage_missing_responses': body.usage_missing_responses})
    conn.execute("UPDATE task_runs SET status = ?, ended_at = ?, error_type = ?, runtime_context_json = ? WHERE conversation_id = ? AND id = ?",
                 (body.status, now, "unknown" if error else None, _json(context), conversation_id, run["id"]))
    conn.execute("UPDATE agent_conversations SET session_initialized = ?, unavailable_reason = ? WHERE id = ?",
                 (initialized, "session_verification_required" if initialized is None else None, conversation_id))
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
    return actor


def _action_reference(row):
    title = {"verification": f"复测 {row['source_vulnerability_id']}" if row['source_vulnerability_id'] else "独立复测", "report": "项目综合报告",
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


def action_works(conn, conversation_id, principal):
    require_owner(conn, conversation_id, principal)
    rows = conn.execute(
        "SELECT a.*, m.project_id AS source_project_id FROM agent_actions a JOIN agent_messages m ON m.id = a.message_id "
        "WHERE m.conversation_id = ? ORDER BY m.id, a.id", (conversation_id,),
    ).fetchall()
    return [_action_work(conn, row, row["source_project_id"], principal) for row in rows]


def _request_action(conn, row, tool_call_id, method, params, actor):
    from .assessment_reports import request_assessment_report_generation

    prior = conn.execute("SELECT * FROM agent_actions WHERE message_id = ? AND tool_call_id = ?", (row["id"], tool_call_id)).fetchone()
    if prior:
        if prior["method"] != method or json.loads(prior["params_json"]) != params:
            raise HTTPException(409, {"code": "agent_action_conflict", "message": "同一次动作的参数已改变，请明确发起新请求。"})
        action = prior
    else:
        identifier = "act_" + uuid4().hex
        finalization = request_assessment_report_generation(
            conn, row["project_id"], template_key=params["template_key"], requested_by=actor.subject_id,
        )
        conn.execute(
            "INSERT INTO agent_actions(id, message_id, tool_call_id, method, params_json, kind, generation_token) "
            "VALUES (?, ?, ?, ?, ?, 'report', ?)",
            (identifier, row["id"], tool_call_id, method, _json(params), finalization.generation_token),
        )
        conn.execute("UPDATE agent_messages SET revision = nextval('agent_message_revision') WHERE id = ?", (row["id"],))
        action = conn.execute("SELECT * FROM agent_actions WHERE id = ?", (identifier,)).fetchone()
    data = {"action_id": action["id"], "work": _action_work(conn, action, row["project_id"], actor)}
    data.update(project_id=row["project_id"], generation_token=action["generation_token"])
    return data


def _tool_params(method, params):
    from .agent_research_context import SESSION_PAGE_METHOD, validate_research_params

    allowed = INTERACTION_TOOL_PARAMS.get(method, _TRUSTED_BRIDGE_PARAMS.get(method))
    if allowed is None:
        raise HTTPException(403, "agent_tool_not_available")
    if not isinstance(params, dict) or set(params) - allowed:
        raise HTTPException(422, "agent_tool_parameters_invalid")
    if method in agent_verifications.TOOL_PARAMS:
        agent_verifications.validate_params(method, params)
        return
    if method == "register_conversation_material":
        from .conversation_research import validate_registration
        validate_registration(params)
        return
    if method in {"read_conversation_research", "_read_conversation_research_bytes"}:
        if (("source" in params and (not isinstance(params["source"], str) or params["source"] not in {"http", "artifacts"}))
                or ("artifact_id" in params and ("flow_id" in params or "side" in params or params.get("source") == "http"))
                or (params.get("source") == "artifacts" and ("flow_id" in params or "side" in params))
                or (method.startswith("_") and "artifact_id" not in params)):
            raise HTTPException(422, "agent_tool_parameters_invalid")
    if method == "publish_custom_markdown":
        if (not isinstance(params.get("title"), str) or not 1 <= len(params["title"]) <= 200
                or any(char in params["title"] for char in "/\\\r\n\0")
                or not isinstance(params.get("content"), str) or not params["content"].strip()
                or len(params["content"].encode("utf-8")) > 1024 * 1024):
            raise HTTPException(422, "agent_tool_parameters_invalid")
        for key in ("source_notes", "limitations"):
            value = params.get(key, [])
            if not isinstance(value, list) or len(value) > 100 or any(not isinstance(item, str) or len(item) > 4096 for item in value):
                raise HTTPException(422, "agent_tool_parameters_invalid")
        references = params.get("source_references", [])
        if not isinstance(references, list) or len(references) > 100:
            raise HTTPException(422, "agent_tool_parameters_invalid")
        for reference in references:
            if (not isinstance(reference, dict) or set(reference) != {"method", "params", "expected_version"}
                    or not isinstance(reference["method"], str) or reference["method"] not in SOURCE_METHODS
                    or not isinstance(reference["expected_version"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", reference["expected_version"])):
                raise HTTPException(422, "agent_tool_parameters_invalid")
            _tool_params(reference["method"], reference["params"])
            if reference["method"] in {"read_research_context", "read_vulnerability"} and not reference["params"].get("project_id"):
                raise HTTPException(422, "agent_tool_parameters_invalid")
        return
    if "project_id" in params and (not isinstance(params["project_id"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", params["project_id"])):
        raise HTTPException(422, "agent_tool_parameters_invalid")
    validate_research_params(method, {key: value for key, value in params.items() if key != "project_id"})
    if method == SESSION_PAGE_METHOD:
        return
    if method in {"read_conversation_attachment", "_read_conversation_attachment_bytes"} and "attachment_id" not in params:
        raise HTTPException(422, "agent_tool_parameters_invalid")
    for key, value in params.items():
        if key in {"limit", "offset", "chunk_size", "cursor"}:
            maximum = {"limit": 100, "offset": 2**31 - 1, "cursor": 2**63 - 1, "chunk_size": 16384}[key]
            if type(value) is not int or not (0 if key in {"offset", "cursor"} else 1) <= value <= maximum:
                raise HTTPException(422, "agent_tool_parameters_invalid")
        elif not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", value):
            raise HTTPException(422, "agent_tool_parameters_invalid")
    if method in {"read_vulnerability", "export_vulnerability_report"} and "vulnerability_id" not in params:
        raise HTTPException(422, "agent_tool_parameters_invalid")
    if method == "request_project_report" and "template_key" not in params:
        raise HTTPException(422, "agent_tool_parameters_invalid")
    if method == "query_agent_work" and "action_id" not in params:
        raise HTTPException(422, "agent_tool_parameters_invalid")


def call_tool(conversation_id, message_id, task_run_id, method, params, principal, *, tool_call_id):
    try:
        return _call_tool(conversation_id, message_id, task_run_id, method, params, principal, tool_call_id=tool_call_id)
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
                "agent_project_context_required": "请先从授权项目索引选择项目，再使用明确的 project_id 读取材料。",
                "agent_tool_parameters_invalid": "工具参数不符合当前业务要求。",
                "agent_tool_not_available": "当前交互入口未开放此工具。请使用已提供的材料、报告或独立复测工具。",
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
            _admit_public(detail, conversation_secrets(conn, conversation_id, task_run_id))
        raise HTTPException(exc.status_code, detail) from exc


def _read_material(conn, row, method, params, actor, secrets):
    """实际读取和发布复核共用授权、取材及准出路径，不递归调用工具或触发执行。"""
    params = dict(params)
    if method in {"read_research_context", "read_vulnerability"}:
        project_id = params.setdefault("project_id", row["project_id"])
        if project_id is None:
            raise HTTPException(422, "agent_project_context_required")
        _context_access(conn, project_id, actor)
    if method in {"read_conversation_attachment", "_read_conversation_attachment_bytes"}:
        data = attachments.read_attachment(conn, row, params, binary=method.startswith("_"))
        if "content_base64" in data:
            _admit_public(b64decode(data["content_base64"]), secrets)
    elif method == "read_research_context":
        from .agent_research_context import read_research_context
        data = read_research_context(conn, project_id, params)
    elif method in {"read_conversation_research", "_read_conversation_research_bytes"}:
        from .conversation_research import read_material
        data = read_material(conn, row, params, actor, binary=method.startswith("_"))
        if "content_base64" in data:
            _admit_public(b64decode(data["content_base64"]), secrets)
    else:
        detail = get_vulnerability_detail(conn, project_id, params["vulnerability_id"], params.get("observation_id"))
        secrets.extend(_finding_source_secrets(conn, project_id, detail.id, admission_secrets=secrets,
                                              observation_id=detail.observation_id, detail=True))
        if detail.observation_id:
            params["observation_id"] = detail.observation_id
        data = {"project_id": project_id, "vulnerability_id": detail.id, "number": detail.number, "observation_id": detail.observation_id,
                "title": detail.title, "status": detail.status, "severity": detail.severity,
                "readable": detail.readable.model_dump(mode="json") if detail.readable is not None else None,
                "preconditions": detail.readable.preconditions if detail.readable else [],
                "reproduction_steps": next(([step.model_dump(mode="json") for step in item.reproduction_steps] for item in detail.observations if item.id == detail.observation_id), []),
                "evidence_items": [item.model_dump(mode="json") for item in detail.evidence_items],
                "supplemental_evidence_items": [item.model_dump(mode="json") for item in detail.record_evidence_items] if detail.observation_id else [],
                "available_observations": [item.id for item in detail.observations]}
    _admit_public(data, secrets)
    description = describe_source(method, params, data)
    if description:
        data["source_reference"] = description["reference"]
        data["source_scope"] = description["scope"]
    return data, description


def _publish_custom_markdown(conn, row, tool_call_id, params, secrets, actor):
    verified = []
    for reference in params.get("source_references", []):
        try:
            _, description = _read_material(conn, row, reference["method"], reference["params"], actor, secrets)
        except HTTPException as exc:
            if exc.status_code not in {403, 404, 409}:
                raise
            raise HTTPException(exc.status_code, {"code": "agent_source_unavailable",
                "message": "所选来源已删除、不可读取或无法核验；请检查当前权限并重新读取后再交付。"}) from exc
        if description is None or description["reference"] != reference:
            raise HTTPException(409, {"code": "agent_source_changed",
                                     "message": "所选来源已变化或不能准出，请重新读取后再交付。"})
        verified.append(verified_source(description))
    return _publish_markdown(conn, row, tool_call_id, "publish_custom_markdown", params, secrets,
                             title=params["title"], content=params["content"], metadata={
                                 "custom_report": True, "verified_sources": verified,
                                 "source_notes": params.get("source_notes", []),
                                 "limitations": params.get("limitations", []), "published_at": utcnow(),
                             })


def _published_artifact(conn, row, tool_call_id, method, params):
    prior = conn.execute(
        "SELECT * FROM artifacts WHERE conversation_id = ? AND task_run_id = ? "
        "AND metadata_json::json->>'agent_tool_call_id' = ?",
        (row["conversation_id"], row["task_run_id"], tool_call_id),
    ).fetchone()
    if prior:
        stored_metadata = json.loads(prior["metadata_json"])
        digest = sha256(json.dumps(params, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if (stored_metadata.get("agent_request_sha256") != digest
                or stored_metadata.get("agent_method", "publish_custom_markdown") != method):
            raise HTTPException(409, "同一次交付的参数已改变，请明确发起新请求。")
    return prior


def _publish_markdown(conn, row, tool_call_id, method, params, secrets, *, title, content, metadata):
    _admit_public([params, title, content, metadata], secrets)
    digest = sha256(json.dumps(params, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    prior = _published_artifact(conn, row, tool_call_id, method, params)
    if prior:
        identifier = prior["id"]
    else:
        title = title if title.endswith(".md") else title + ".md"
        stored = conversation_files.create_artifact(conn, row["conversation_id"],
            task_run_id=row["task_run_id"], kind="report", filename=f"agent-{uuid4().hex}.md",
            content=content.encode("utf-8"), metadata={
                **metadata, "title": title, "agent_conversation_id": row["conversation_id"], "agent_message_id": row["id"],
                "agent_tool_call_id": tool_call_id, "agent_request_sha256": digest, "agent_method": method,
            })
        conversation_files.artifact_file(conn, stored)
        identifier = stored["id"]
        ids = json.loads(row["artifact_ids_json"])
        ids.append(identifier)
        conn.execute("UPDATE agent_messages SET artifact_ids_json = ?, revision = nextval('agent_message_revision') WHERE id = ?",
                     (_json(ids), row["id"]))
    current = conn.execute("SELECT * FROM agent_messages WHERE id = ?", (row["id"],)).fetchone()
    reference = next(item for item in _artifacts(conn, current) if item["id"] == identifier)
    return {"project_id": row["project_id"], "artifact": reference, "status": "succeeded"}


def _finding_source_secrets(conn, project_id, vulnerability_id, *, admission_secrets, observation_id=None, detail=False):
    """按本次正文的真实来源检查；不以默认上下文或无关 external_verify 代替产出运行。"""
    record = conn.execute("SELECT * FROM vulnerabilities WHERE project_id = ? AND id = ?",
                          (project_id, vulnerability_id)).fetchone()
    runs = {record["discovered_by_run_id"]}
    facts = {record["source_fact_id"]}
    materials = [dict(record)]
    if detail:
        if observation_id is not None:
            observation = conn.execute("SELECT * FROM vulnerability_observations WHERE project_id = ? AND id = ? AND vulnerability_id = ?",
                                       (project_id, observation_id, vulnerability_id)).fetchone()
            if observation is None:
                raise HTTPException(404, "vulnerability_observation_not_found")
            runs.add(observation["task_run_id"])
            facts.add(observation["source_fact_id"])
            materials.append(dict(observation))
        for verification in conn.execute(
                "SELECT verifier_task_run_id, notes FROM vulnerability_verifications WHERE project_id = ? AND vulnerability_id = ? "
                "AND observation_id IS NOT DISTINCT FROM ?", (project_id, vulnerability_id, observation_id)).fetchall():
            runs.add(verification["verifier_task_run_id"])
            materials.append(dict(verification))
    for fact_id in facts - {None}:
        fact = conn.execute("SELECT task_run_id, description FROM facts WHERE project_id = ? AND id = ?", (project_id, fact_id)).fetchone()
        if fact is not None:
            runs.add(fact["task_run_id"])
            if detail:
                materials.append(dict(fact))
    secrets = []
    for run in sorted(runs - {None}):
        secrets.extend(_platform_secrets(conn, project_id, run))
    # Markdown 转义可能改变秘密的字面字节；准出先检查真实原料，再检查最终输出。
    _admit_public(materials, [*admission_secrets, *secrets])
    return secrets


def _call_tool(conversation_id, message_id, task_run_id, method, params, principal, *, tool_call_id):
    with get_conn() as conn:
        row, _, _ = _owned_turn(conn, conversation_id, message_id, task_run_id, principal)
        actor = _message_actor(conn, row)
        # 完整编号仅解析为既有身份，仍由下方项目权限判断授权。
        if method in {"read_vulnerability", "export_vulnerability_report"} and isinstance(params, dict):
            from cairn.vulnerability_number import parse_vulnerability_number
            value = params.get("vulnerability_id")
            if isinstance(value, str) and "/" in value:
                try:
                    source_project, source_id = parse_vulnerability_number(value, params.get("project_id"))
                except ValueError as exc:
                    raise HTTPException(422, "agent_tool_parameters_invalid") from exc
                params = {**params, "project_id": source_project, "vulnerability_id": source_id}
        # 真实执行归属及消息操作人核对后才裁决准入，拒绝发生在任何业务副作用之前。
        _tool_params(method, params)
        if method in {"start_conversation_research", "list_conversation_research", "_resolve_conversation_research"}:
            from . import conversation_research
            if method == "start_conversation_research":
                data = conversation_research.admit(conn, row, params, actor)
            elif method == "_resolve_conversation_research":
                return conversation_research.resolve(conn, row, actor)
            else:
                data = conversation_research.list_research(conn, row, params, actor)
            _admit_public(data, conversation_secrets(conn, conversation_id, task_run_id))
            return data
        if method == "_validate_workspace_claim":
            run = conn.execute("SELECT runtime_context_json FROM task_runs WHERE conversation_id = ? AND id = ?",
                               (conversation_id, task_run_id)).fetchone()
            if json.loads(run["runtime_context_json"]).get("conversation_research"):
                from .conversation_research import resolve
                resolve(conn, row, actor)
            return {"active": True}
        project_id = params.get("project_id", row["project_id"])
        secrets = conversation_secrets(conn, conversation_id, task_run_id)
        if method in {"publish_custom_markdown", "export_vulnerability_report"}:
            prior = _published_artifact(conn, row, tool_call_id, method, params)
            if prior:
                # 已准出成品独立于当前来源；先核对请求相等，再返回原身份和核验信息。
                metadata = json.loads(prior["metadata_json"])
                data = {"project_id": metadata.get("source_project_id", row["project_id"]), "status": "succeeded",
                        "artifact": next(item for item in _artifacts(conn, row) if item["id"] == prior["id"])}
                if method == "export_vulnerability_report":
                    data.update(vulnerability_id=metadata["vulnerability_id"], observation_id=metadata["observation_id"])
                _admit_public(data, secrets)
                return data
        project_methods = {"query_project_progress", "list_research_sessions", "read_research_context",
                           "read_research_session", "_admit_research_session_page", "list_vulnerabilities",
                           "read_vulnerability", "export_vulnerability_report", "request_project_report"}
        if method in project_methods:
            if project_id is None:
                raise HTTPException(422, "agent_project_context_required")
            _context_access(conn, project_id, actor)
            if method in {"query_project_progress", "request_project_report"}:
                secrets.extend(_platform_secrets(conn, project_id))
        if method == "list_authorized_projects":
            from cairn.server.auth import verification_project_access_clause
            clause, values = verification_project_access_clause(actor)
            where = f"({clause})"
            values = list(values)
            if not actor.is_super_admin:
                where += " AND p.tenant_id = ?"
                values.append(actor.tenant_id)
            offset, limit = params.get("offset", 0), params.get("limit", 20)
            items = conn.execute(f"SELECT p.id, p.status FROM projects p WHERE {where} ORDER BY p.id LIMIT ? OFFSET ?",
                                 (*values, limit + 1, offset)).fetchall()
            data = {"items": [dict(item) for item in items[:limit]], "read_at": utcnow(),
                    "next_offset": offset + limit if len(items) > limit else None}
        elif method == "list_report_templates":
            from .report_templates import list_report_template_catalog
            data = {"items": [item.model_dump(mode="json") for item in list_report_template_catalog(conn)]}
        elif method == "list_conversation_attachments":
            items = attachments.list_attachments(conn, conversation_id, sent_only=True)
            offset, limit = params.get("offset", 0), params.get("limit", 20)
            data = {"items": items[offset:offset + limit], "total": len(items),
                    "next_offset": offset + limit if offset + limit < len(items) else None}
        elif method in SOURCE_METHODS:
            data, _ = _read_material(conn, row, method, params, actor, secrets)
        elif method in agent_verifications.TOOL_PARAMS:
            _admit_public(params, secrets)
            data = agent_verifications.call(conn, row, tool_call_id, method, params, actor)
        elif method == "register_conversation_material":
            from .conversation_research import register_material
            data = register_material(conn, row, params, tool_call_id, secrets)
        elif method == "publish_custom_markdown":
            data = _publish_custom_markdown(conn, row, tool_call_id, params, secrets, actor)
        elif method in {"request_project_report", "query_agent_work"}:
            if method == "query_agent_work":
                action = conn.execute(
                    "SELECT a.*, m.project_id AS source_project_id FROM agent_actions a JOIN agent_messages m ON m.id = a.message_id "
                    "WHERE a.id = ? AND m.conversation_id = ?",
                    (params["action_id"], conversation_id),
                ).fetchone()
                if action is None:
                    raise HTTPException(404, {"code": "agent_action_not_found", "message": "当前会话中没有此项工作。"})
                data = {"action_id": action["id"], "work": _action_work(conn, action, action["source_project_id"], actor)}
            else:
                data = _request_action(conn, row, tool_call_id, method, params, actor)
        elif method in {"list_research_sessions", "read_research_session", "_admit_research_session_page"}:
            from .agent_research_context import (
                admit_research_session_page, list_research_sessions, research_session_reference,
            )

            handler = {"list_research_sessions": list_research_sessions,
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
            for item in items[offset:offset + limit]:
                secrets.extend(_finding_source_secrets(conn, project_id, item.id, admission_secrets=secrets))
            data = {"items": [item.model_dump(mode="json") for item in items[offset:offset + limit]], "total": len(items),
                    "next_offset": offset + limit if offset + limit < len(items) else None}
        else:
            # 只先确定来源；完整详情由报告服务读取一次，不为导出再装载一遍证据正文。
            vulnerability_id = params["vulnerability_id"]
            observation_id = read_vulnerability_evidence(conn, project_id, vulnerability_id, params.get("observation_id"))["observation_id"]
            secrets.extend(_finding_source_secrets(conn, project_id, vulnerability_id, admission_secrets=secrets, observation_id=observation_id, detail=True))
            markdown, title, source = render_vulnerability_report_material(conn, project_id, vulnerability_id, observation_id=observation_id)
            # 成品和消息引用使用持有会话锁的同一事务，重传不重复发布。
            data = _publish_markdown(conn, row, tool_call_id, method, params, secrets,
                                     title=title, content=markdown,
                                     metadata={"source_project_id": project_id, "vulnerability_id": vulnerability_id,
                                               "observation_id": observation_id, "verified_sources": [source],
                                               "limitations": [source["limitations"]]})
            data.update(project_id=project_id, vulnerability_id=vulnerability_id, observation_id=observation_id)
        _admit_public(data, secrets)
        return data


def conversation_artifact(conn, conversation_id, artifact_id, principal):
    require_owner(conn, conversation_id, principal, include_deleted=True)
    row = conn.execute("SELECT * FROM artifacts WHERE conversation_id = ? AND id = ?", (conversation_id, artifact_id)).fetchone()
    if row is None:
        raise HTTPException(404, "agent_artifact_not_found")
    return row
