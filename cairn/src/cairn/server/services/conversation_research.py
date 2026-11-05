"""会话内研究准入与留存材料读取；来源引用不成为执行 owner。"""

import base64
import binascii
from hashlib import sha256
import json
from pathlib import PurePosixPath

from fastapi import HTTPException

from cairn.server.schemas.traffic import TrafficOwner
from .time_utils import utcnow


def admit(conn, row, params, actor):
    from .agent_conversations import _context_access
    from .ids import get_project_or_404
    from .project_authorization import project_authorization_block_reason, authorization_scope_projection
    from .vulnerabilities import get_vulnerability_detail

    project_id = params.get("project_id") or row["project_id"]
    if not project_id:
        raise HTTPException(422, "agent_project_context_required")
    _context_access(conn, project_id, actor)
    project = get_project_or_404(conn, project_id)
    reason = project_authorization_block_reason(conn, project)
    if reason:
        raise HTTPException(409, {"code": reason, "message": "现有目标授权已失效或范围改变；请先明确补充授权，不会恢复来源项目。"})
    source = {"project_id": project_id}
    if params.get("vulnerability_id"):
        detail = get_vulnerability_detail(conn, project_id, params["vulnerability_id"], params.get("observation_id"))
        source.update(vulnerability_id=params["vulnerability_id"], observation_id=detail.observation_id)
    elif params.get("observation_id"):
        raise HTTPException(422, "agent_research_vulnerability_required")
    run = conn.execute("SELECT * FROM task_runs WHERE conversation_id = ? AND id = ? FOR UPDATE",
                       (row["conversation_id"], row["task_run_id"])).fetchone()
    context = json.loads(run["runtime_context_json"])
    prior = context.get("conversation_research")
    if prior and prior["source"] != source:
        raise HTTPException(409, {"code": "conversation_research_scope_conflict", "message": "本轮已有研究范围；扩大目标或身份前请先结束本轮并取得新授权。"})
    if not prior:
        context["conversation_research"] = {"source": source, "authorization_epoch": project["authorization_epoch"],
                                            "scope_digest": project["authorization_scope_digest"],
                                            "scope_descriptor": authorization_scope_projection(conn, project).descriptor,
                                            "admitted_at": utcnow()}
        conn.execute("UPDATE task_runs SET runtime_context_json = ? WHERE conversation_id = ? AND id = ?",
                     (json.dumps(context), row["conversation_id"], row["task_run_id"]))
    return {"conversation_id": row["conversation_id"], "task_run_id": row["task_run_id"], "source": source,
            "state": "admitted", "scope": context["conversation_research"]["scope_descriptor"]}


def resolve(conn, row, actor):
    from .agent_conversations import _context_access
    from .ids import get_project_or_404
    from .project_authorization import project_authorization_block_reason
    from .test_egress import public_project_egress, _decrypt

    run = conn.execute("SELECT runtime_context_json FROM task_runs WHERE conversation_id = ? AND id = ?",
                       (row["conversation_id"], row["task_run_id"])).fetchone()
    research = json.loads(run["runtime_context_json"]).get("conversation_research")
    if not research:
        raise HTTPException(409, "conversation_research_not_admitted")
    project_id = research["source"]["project_id"]
    _context_access(conn, project_id, actor)
    project = get_project_or_404(conn, project_id)
    if (project_authorization_block_reason(conn, project) or project["authorization_epoch"] != research["authorization_epoch"]
            or project["authorization_scope_digest"] != research["scope_digest"]):
        raise HTTPException(409, "conversation_research_authorization_changed")
    egress = public_project_egress(project).model_dump(exclude={"auth_configured"})
    egress.update(_decrypt(project["test_egress_auth"], tenant_id=project["tenant_id"], owner_kind="project", owner_id=project_id))
    # 仅可信 Dispatcher 准备路径读取代理凭据；不经公开工具结果返回模型。
    return {"test_egress": egress, "source": research["source"]}


def list_research(conn, row, params, actor):
    from .agent_conversations import _context_access

    project_id = params.get("project_id") or row["project_id"]
    if not project_id:
        raise HTTPException(422, "agent_project_context_required")
    _context_access(conn, project_id, actor)
    source = "COALESCE(r.runtime_context_json::jsonb -> 'conversation_research' -> 'source', jsonb_build_object('project_id', m.project_id))"
    clauses = ["c.tenant_id = ?", "c.created_by = ?", f"{source} ->> 'project_id' = ?",
               "(r.runtime_context_json::jsonb -> 'conversation_research' IS NOT NULL OR EXISTS "
               "(SELECT 1 FROM artifacts a WHERE a.conversation_id = c.id AND a.task_run_id = r.id "
               "AND a.metadata_json::jsonb ->> 'agent_method' IN "
               "('register_conversation_material', 'publish_custom_markdown', 'export_vulnerability_report')))"]
    values = [actor.tenant_id, actor.subject_id, project_id]
    for name in ("vulnerability_id", "observation_id"):
        if name in params:
            clauses.append(f"{source} ->> '{name}' = ?")
            values.append(params[name])
    offset, limit = params.get("offset", 0), params.get("limit", 20)
    rows = conn.execute(
        f"SELECT r.id, r.conversation_id, r.status, r.started_at, r.ended_at, {source} AS source, c.deleted_at "
        "FROM task_runs r JOIN agent_conversations c ON c.id = r.conversation_id "
        "JOIN agent_messages m ON m.conversation_id = c.id AND m.task_run_id = r.id WHERE "
        + " AND ".join(clauses) + " ORDER BY r.id LIMIT ? OFFSET ?", (*values, limit + 1, offset),
    ).fetchall()
    return {"project_id": project_id, "read_at": utcnow(), "items": [
        {"task_run_id": item["id"], "conversation_id": item["conversation_id"], "status": item["status"],
         "started_at": item["started_at"], "ended_at": item["ended_at"], "conversation_deleted": item["deleted_at"] is not None,
         "source": item["source"]}
        for item in rows[:limit]], "next_offset": offset + limit if len(rows) > limit else None,
        "scope": "本人会话中按来源登记的研究执行；含已删除会话的留存材料，不读取聊天或重新执行。"}


def validate_registration(params):
    filename, path, encoded = (params.get(key) for key in ("filename", "source_path", "content_base64"))
    if (not isinstance(filename, str) or not 1 <= len(filename) <= 200 or filename in {".", ".."}
            or any(char in filename for char in "/\\\r\n\0")
            or not isinstance(path, str) or not 1 <= len(path) <= 4096 or "\0" in path
            or not isinstance(encoded, str) or len(encoded) > 4 * ((1024 * 1024 + 2) // 3)):
        raise HTTPException(422, "agent_tool_parameters_invalid")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "agent_tool_parameters_invalid") from exc
    if len(content) > 1024 * 1024:
        raise HTTPException(422, "agent_tool_parameters_invalid")


def artifact_reference(artifact):
    metadata = json.loads(artifact["metadata_json"])
    return {"id": artifact["id"], "task_run_id": artifact["task_run_id"], "title": metadata["title"],
            "size": artifact["size"], "sha256": artifact["sha256"],
            "download_url": f"/agent-conversations/{artifact['conversation_id']}/artifacts/{artifact['id']}"}


def register_material(conn, row, params, tool_call_id, secrets):
    from .agent_conversations import _admit_public, _published_artifact
    from .conversation_files import create_artifact, artifact_file

    path = PurePosixPath(params["source_path"])
    root = PurePosixPath("/home/kali/workspace") / row["conversation_id"]
    if root not in path.parents or ".." in path.parts or path.name != params["filename"]:
        raise HTTPException(422, "agent_tool_parameters_invalid")
    content = base64.b64decode(params["content_base64"], validate=True)
    _admit_public([content, params["source_path"], params["filename"]], secrets)
    stored = _published_artifact(conn, row, tool_call_id, "register_conversation_material", params)
    if stored is None:
        stored = create_artifact(conn, row["conversation_id"], task_run_id=row["task_run_id"],
            kind="file", filename=params["filename"], content=content, metadata={
                "title": params["filename"], "source_path": params["source_path"],
                "agent_method": "register_conversation_material", "agent_tool_call_id": tool_call_id,
                "agent_message_id": row["id"],
                "agent_request_sha256": sha256(json.dumps(params, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
            })
        # 准出失败回滚登记与原件，不把私有 session 或凭据作为可下载成品。
        artifact_file(conn, stored)
        identifiers = json.loads(row["artifact_ids_json"])
        identifiers.append(stored["id"])
        conn.execute("UPDATE agent_messages SET artifact_ids_json = ?, revision = nextval('agent_message_revision') WHERE id = ?",
                     (json.dumps(identifiers), row["id"]))
    else:
        artifact_file(conn, stored)
    return {"conversation_id": row["conversation_id"], "task_run_id": row["task_run_id"],
            "artifact": artifact_reference(stored), "status": "registered"}


def read_artifact(conn, conversation_id, params, *, binary):
    from .conversation_files import artifact_file

    # 只索引明确发布的工作文件和既有会话成品；HTTP 原件仍沿原 flow 路径。
    where = ("conversation_id = ? AND metadata_json::jsonb ->> 'agent_method' IN "
             "('register_conversation_material', 'publish_custom_markdown', 'export_vulnerability_report')")
    values = [conversation_id]
    if params.get("task_run_id"):
        where += " AND task_run_id = ?"
        values.append(params["task_run_id"])
    if params.get("artifact_id"):
        stored = conn.execute("SELECT * FROM artifacts WHERE " + where + " AND id = ?",
                              (*values, params["artifact_id"])).fetchone()
        if stored is None:
            raise HTTPException(404, "conversation_material_not_found")
        content = artifact_file(conn, stored).read_bytes()
        data = {"conversation_id": conversation_id, "task_run_id": stored["task_run_id"],
                "artifact": artifact_reference(stored), "sha256": stored["sha256"]}
        if binary:
            if len(content) > 1024 * 1024:
                raise HTTPException(422, "conversation_material_transfer_too_large")
            return {**data, "content_base64": base64.b64encode(content).decode("ascii"), "representation": "original_bytes"}
        offset, size = params.get("offset", 0), params.get("chunk_size", 16384)
        chunk = content[offset:offset + size]
        return {**data, "content": chunk.decode("utf-8", errors="backslashreplace"),
                "representation": "original_utf8_backslashreplace", "offset_unit": "original_bytes", "offset": offset,
                "bytes_read": len(chunk), "has_more": offset + len(chunk) < len(content),
                "next_offset": offset + len(chunk) if offset + len(chunk) < len(content) else None}
    offset, limit = params.get("offset", 0), params.get("limit", 20)
    rows = conn.execute("SELECT * FROM artifacts WHERE " + where + " ORDER BY record_id LIMIT ? OFFSET ?",
                        (*values, limit + 1, offset)).fetchall()
    return {"conversation_id": conversation_id, "source": "artifacts",
            "items": [artifact_reference(item) for item in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None}


def read_material(conn, row, params, actor, *, binary=False):
    from .traffic import list_traffic_records, read_traffic_record, read_traffic_body
    from .agent_research_context import _protected
    from .agent_conversations import require_owner, conversation_secrets
    from cairn.server.traffic_schema import traffic_request_address

    conversation_id = params.setdefault("conversation_id", row["conversation_id"])
    require_owner(conn, conversation_id, actor, include_deleted=True)
    owner = TrafficOwner(conversation_id=conversation_id)
    flow_id = params.get("flow_id")
    if params.get("source") == "artifacts" or "artifact_id" in params:
        data = read_artifact(conn, conversation_id, params, binary=binary)
        runs = {item["task_run_id"] for item in data["items"]} if "items" in data else {data["task_run_id"]}
    elif not flow_id:
        page = list_traffic_records(owner, task_run_id=params.get("task_run_id"),
                                    limit=params.get("limit", 20), cursor=params.get("cursor", 0))
        # 索引不批量带出头部或正文，按真实 flow_id 再读取所需一侧。
        data = {"conversation_id": conversation_id, "items": [
            {"id": item["id"], "task_run_id": item["scope"]["task_run_id"], "state": item["state"],
             "method": item["request"]["method"], "location": traffic_request_address(item["request"]),
             "body_artifacts": {side: value.get("artifact_id") for side, value in item["sync"]["bodies"].items()}}
            for item in page["items"]], "has_more": page["has_more"], "next_cursor": page["next_cursor"]}
        runs = {item["task_run_id"] for item in data["items"]}
    else:
        record = read_traffic_record(owner, flow_id)
        task_run_id = record["scope"]["task_run_id"]
        if params.get("task_run_id", task_run_id) != task_run_id:
            raise HTTPException(404, "traffic_flow_not_found")
        side = params.get("side", "request")
        if side not in {"request", "response"}:
            raise HTTPException(422, "traffic_side_invalid")
        body = read_traffic_body(owner, flow_id, side, preview=True, offset=params.get("offset", 0),
                                 limit=params.get("chunk_size", 16384))
        data = {"conversation_id": conversation_id, "flow_id": flow_id, "task_run_id": task_run_id,
                "side": side, "headers": record["header_text"][side],
                "content": base64.b64decode(body["content_base64"]).decode("utf-8", errors="backslashreplace"),
                "representation": "captured_body_utf8_backslashreplace", "offset_unit": "captured_bytes",
                "revision": record["revision"],
                **{key: body[key] for key in ("artifact_id", "offset", "bytes_read", "next_offset", "has_more", "sealed", "body_complete", "sha256")},
                "artifact_url": f"/agent-conversations/{conversation_id}/artifacts/{body['artifact_id']}" if body["artifact_id"] else None}
        runs = {task_run_id}
    secrets = [secret for run in sorted(runs) for secret in conversation_secrets(conn, conversation_id, run)]
    if _protected(json.dumps(data, ensure_ascii=False), secrets):
        raise HTTPException(403, "agent_research_material_protected")
    return data
