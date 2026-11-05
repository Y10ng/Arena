"""项目 Agent 的只读研究索引与按范围读取；不触发研究租约或环境维护。"""

from __future__ import annotations

import json
import re

from fastapi import HTTPException

from .audit_artifacts import _artifact_disk_path_from_row
from .ids import get_project_or_404
from .time_utils import utcnow
from .verification_requests import PUBLIC_SECRET_PATTERN, _artifact_deliverable, _platform_secrets
from .verification_sources import artifact_is_verification_material

SESSION_PAGE_METHOD = "_admit_research_session_page"
# 认证报文、凭据字段和私有控制路径不能借研究材料进入聊天上下文。
_AUTH_CHANNEL = re.compile(
    r'(?i)(?:["\']?(?:authorization|proxy-authorization|cookie|set-cookie|password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret)["\']?\s*[:=]\s*["\']?[^\s"\',;}]+|'
    r'/tmp/cairn-prompts/|/home/kali/\.cairn/(?:auth|credentials)|/run/secrets/)'
)


def validate_research_params(method, params):
    if method == SESSION_PAGE_METHOD:
        page = params.get("page")
        if (set(params) != {"task_run_id", "page"} or not isinstance(params["task_run_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", params["task_run_id"]) or not isinstance(page, dict)):
            raise HTTPException(422, "agent_tool_parameters_invalid")
        if not isinstance(page.get("session_id"), str):
            raise HTTPException(422, "agent_tool_parameters_invalid")
        if page.get("availability") == "unavailable":
            if (set(page) != {"session_id", "availability", "reason"}
                    or page["reason"] not in {"research_reader_unavailable", "session_identity_invalid",
                        "session_ownership_unverifiable", "session_not_persisted_or_removed", "session_unreadable_or_ownership_unverifiable"}):
                raise HTTPException(422, "agent_tool_parameters_invalid")
            return
        if (set(page) != {"session_id", "availability", "read_at", "entries", "missing", "incomplete_tail", "next_offset", "observed_bytes", "modified_at"}
                or page["availability"] != "available" or type(page["incomplete_tail"]) is not bool
                or type(page["observed_bytes"]) is not int or page["observed_bytes"] < 0
                or (page["next_offset"] is not None and (type(page["next_offset"]) is not int or page["next_offset"] < 0))):
            raise HTTPException(422, "agent_tool_parameters_invalid")
        for name in ("read_at", "modified_at"):
            if not isinstance(page[name], str) or len(page[name]) > 64:
                raise HTTPException(422, "agent_tool_parameters_invalid")
        for name in ("entries", "missing"):
            if not isinstance(page[name], list) or len(page[name]) > 100:
                raise HTTPException(422, "agent_tool_parameters_invalid")
        for item in page["entries"]:
            if (not isinstance(item, dict) or set(item) != {"line", "raw"} or type(item["line"]) is not int
                    or item["line"] < 0 or not isinstance(item["raw"], str) or len(item["raw"].encode("utf-8")) > 256 * 1024):
                raise HTTPException(422, "agent_tool_parameters_invalid")
        for item in page["missing"]:
            if (not isinstance(item, dict) or set(item) != {"line", "reason"} or type(item["line"]) is not int
                    or item["line"] < 0 or item["reason"] not in {"record_exceeds_read_limit", "record_unreadable"}):
                raise HTTPException(422, "agent_tool_parameters_invalid")
        return
    if method == "read_research_context" and (not isinstance(params.get("source"), str)
            or params["source"] not in {"events", "facts", "intents", "evidence", "http_flows"}):
        raise HTTPException(422, "agent_tool_parameters_invalid")
    if method == "read_research_context" and "side" in params and (not isinstance(params["side"], str) or params["side"] not in {"request", "response"}):
        raise HTTPException(422, "agent_tool_parameters_invalid")
    if method == "read_research_session" and "task_run_id" not in params:
        raise HTTPException(422, "agent_tool_parameters_invalid")


def _fields(row, names):
    return {name: row[name] for name in names.split()}


def _unavailable(source, reason, **extra):
    return {"source": source, "read_at": utcnow(), "availability": "unavailable", "reason": reason, **extra}


def _research_run(conn, project_id, task_run_id):
    row = conn.execute(
        "SELECT * FROM task_runs WHERE project_id = ? AND id = ? AND task_type <> 'agent_chat'",
        (project_id, task_run_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "research_execution_not_found", "message": "当前项目没有此研究执行。"})
    return row


def _protected(raw, secrets):
    values = [raw]
    try:
        values.append(json.loads(raw))
    except ValueError:
        pass
    # JSONL 的引号转义不能绕过原始凭据检查；只拒绝输出，不重写原始记录。
    while values:
        value = values.pop()
        if isinstance(value, dict):
            values.extend(value.keys())
            values.extend(value.values())
        elif isinstance(value, list):
            values.extend(value)
        elif isinstance(value, str):
            if (PUBLIC_SECRET_PATTERN.search(value) or _AUTH_CHANNEL.search(value)
                    or any(secret and secret in value.encode("utf-8") for secret in secrets)):
                return True
    return False


def _source_secrets(conn, project_id, task_run_id):
    return _platform_secrets(conn, project_id, task_run_id) if task_run_id else []


def list_research_sessions(conn, project_id, params):
    get_project_or_404(conn, project_id)
    offset, limit = params.get("offset", 0), params.get("limit", 20)
    rows = conn.execute(
        "SELECT r.*, GREATEST((SELECT MAX(e.ts) FROM agent_events e WHERE e.project_id = r.project_id "
        "AND e.task_run_id = r.id), r.ended_at, r.started_at, r.runtime_context_json::json ->> 'queued_at') AS updated_at "
        "FROM task_runs r WHERE r.project_id = ? AND r.task_type <> 'agent_chat' "
        "ORDER BY COALESCE(r.started_at, r.ended_at), LENGTH(r.id), r.id LIMIT ? OFFSET ?",
        (project_id, limit + 1, offset),
    ).fetchall()
    return {"project_id": project_id, "source": "task_runs", "read_at": utcnow(),
            "items": [{**_fields(row, "id task_type worker_name worker_type status session_id intent_id started_at ended_at updated_at"),
                       "session_source": "pi_session" if row["session_id"] else None,
                       "session_availability": "not_read" if row["session_id"] else "not_recorded"}
                      for row in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
            "materials": ["events", "facts", "intents", "evidence", "http_flows"],
            "scope": "已持久登记的研究执行；状态是读取时的数据库记录，不证明进程或模型实时健康。"}


def _collection(conn, project_id, params):
    source = params["source"]
    table, fields, order = {
        "events": ("agent_events", "id task_run_id ts kind", "ts, LENGTH(id), id"),
        "facts": ("facts", "id task_run_id creator created_at", "created_at, LENGTH(id), id"),
        "intents": ("intents", "id task_run_id worker created_at concluded_at conclusion to_fact_id depends_on", "created_at, LENGTH(id), id"),
        "evidence": ("artifacts", "id task_run_id kind category size sha256 created_at", "created_at, LENGTH(id), id"),
    }[source]
    where, values = "project_id = ?", [project_id]
    if params.get("task_run_id"):
        _research_run(conn, project_id, params["task_run_id"])
        where += " AND task_run_id = ?"
        values.append(params["task_run_id"])
    else:
        where += " AND (task_run_id IS NULL OR task_run_id NOT IN (SELECT id FROM task_runs WHERE project_id = ? AND task_type = 'agent_chat'))"
        values.append(project_id)
    if params.get("item_id"):
        row = conn.execute(f"SELECT * FROM {table} WHERE {where} AND id = ?", (*values, params["item_id"])).fetchone()
        if row is None:
            raise HTTPException(404, {"code": "research_material_not_found", "message": "当前研究范围内没有此材料。"})
        return row
    offset, limit = params.get("offset", 0), params.get("limit", 20)
    rows = conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                        (*values, limit + 1, offset)).fetchall()
    items = [_fields(row, fields) for row in rows[:limit]]
    if source == "evidence":
        for item, row in zip(items, rows):
            item["availability"] = "not_read" if artifact_is_verification_material(row) else "protected"
    return {"project_id": project_id, "source": source, "read_at": utcnow(), "items": items,
            "next_offset": offset + limit if len(rows) > limit else None}


def _read_http_flows(conn, project_id, params):
    from .flow_evidence import _artifact, flow_metadata_allowed, read_frozen_flow_projection, resolve_flow_evidence

    where, values = "f.project_id = ? AND r.task_type <> 'agent_chat'", [project_id]
    if params.get("task_run_id"):
        _research_run(conn, project_id, params["task_run_id"])
        where += " AND s.task_run_id = ?"
        values.append(params["task_run_id"])
    if params.get("item_id"):
        where += " AND f.id = ?"
        values.append(params["item_id"])
    offset, limit = params.get("offset", 0), params.get("limit", 20)
    rows = conn.execute(
        "SELECT f.id, f.state, f.revision, f.received_at, f.updated_at, s.task_run_id FROM http_flows f "
        "JOIN http_capture_scopes s ON s.project_id=f.project_id AND s.id=f.capture_scope_id "
        "JOIN task_runs r ON r.project_id=s.project_id AND r.id=s.task_run_id WHERE " + where
        + " ORDER BY f.created_cursor LIMIT ? OFFSET ?", (*values, 1 if params.get("item_id") else limit + 1, 0 if params.get("item_id") else offset),
    ).fetchall()
    if not params.get("item_id"):
        return {"project_id": project_id, "source": "http_flows", "read_at": utcnow(), "items": [dict(row) for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}
    if not rows:
        raise HTTPException(404, {"code": "research_material_not_found", "message": "当前研究范围内没有此材料。"})
    side = params.get("side", "request")
    source = {"kind": "http_flows", "item_id": rows[0]["id"], "task_run_id": rows[0]["task_run_id"], "side": side}
    try:
        snapshot = resolve_flow_evidence(conn, project_id, rows[0]["id"])
        projection = snapshot["projections"][side]
        if projection is None:
            return _unavailable(source, "traffic_side_absent")
        body = _artifact(conn, project_id, projection["body_artifact_id"])
        if not flow_metadata_allowed(conn, snapshot, public=True) or not _artifact_deliverable(conn, body):
            return _unavailable(source, "protected_or_unverifiable_evidence")
        if projection["body_display"] == "utf8":
            with _artifact_disk_path_from_row(body).open("r", encoding="utf-8") as stream:
                overlap = ""
                while chunk := stream.read(16384):
                    if _AUTH_CHANNEL.search(overlap + chunk):
                        return _unavailable(source, "protected_authentication_material")
                    overlap = chunk[-1024:]
        page = read_frozen_flow_projection(conn, snapshot, side, offset, params.get("chunk_size", 12000))
    except HTTPException as error:
        if error.status_code not in {404, 409}:
            raise
        return _unavailable(source, "traffic_not_sealed_or_material_missing")
    except (OSError, UnicodeError):
        return _unavailable(source, "traffic_material_unreadable")
    return {"source": source, "read_at": utcnow(), "availability": "available", "content": page["text"],
            "offset": page["offset"], "next_offset": page["next_offset"] if page["has_more"] else None,
            "offset_unit": "utf8_bytes", "representation": page["representation"], "revision": page["revision"],
            "sha256": page["sha256"], "body_display": page["body_display"]}


def read_research_context(conn, project_id, params):
    get_project_or_404(conn, project_id)
    if params["source"] == "http_flows":
        return _read_http_flows(conn, project_id, params)
    row = _collection(conn, project_id, params)
    if not params.get("item_id"):
        return row
    source = {"kind": params["source"], "item_id": row["id"], "task_run_id": row["task_run_id"]}
    start, size = params.get("offset", 0), params.get("chunk_size", 12000)
    if params["source"] == "evidence":
        # 复用现有证据输出边界：内部文件、认证材料及不可核验的来源不交给模型。
        if not _artifact_deliverable(conn, row):
            return _unavailable(source, "protected_or_unverifiable_evidence")
        try:
            with _artifact_disk_path_from_row(row).open("r", encoding="utf-8") as stream:
                overlap = ""
                while chunk := stream.read(16384):
                    if _AUTH_CHANNEL.search(overlap + chunk):
                        return _unavailable(source, "protected_authentication_material")
                    overlap = chunk[-1024:]
                stream.seek(0)
                remaining = start
                while remaining:
                    skipped = stream.read(min(remaining, 16384))
                    if not skipped:
                        break
                    remaining -= len(skipped)
                text = stream.read(size + 1)
        except (OSError, UnicodeError, HTTPException):
            return _unavailable(source, "missing_or_non_utf8_evidence")
        return {"source": source, "read_at": utcnow(), "availability": "available", "content": text[:size],
                "offset": start, "next_offset": start + size if len(text) > size else None,
                "offset_unit": "unicode_characters", "sha256": row["sha256"]}
    if params["source"] == "events":
        value = {**_fields(row, "id task_run_id ts kind"), "payload": json.loads(row["payload_json"])}
    elif params["source"] == "facts":
        value = _fields(row, "id description task_run_id creator created_at")
        value["evidence"] = [dict(item) for item in conn.execute(
            "SELECT artifact_id, verifier_run_id, created_at FROM fact_evidence WHERE project_id = ? AND fact_id = ? ORDER BY created_at, artifact_id",
            (project_id, row["id"]),
        ).fetchall()]
    else:
        value = _fields(row, "id description task_run_id creator worker created_at concluded_at conclusion to_fact_id depends_on last_heartbeat_at")
        value["source_fact_ids"] = [item["fact_id"] for item in conn.execute(
            "SELECT fact_id FROM intent_sources WHERE project_id = ? AND intent_id = ? ORDER BY source_order, fact_id",
            (project_id, row["id"]),
        ).fetchall()]
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    try:
        secrets = _source_secrets(conn, project_id, row["task_run_id"])
    except HTTPException:
        return _unavailable(source, "source_credentials_unverifiable")
    if _protected(raw, secrets):
        return _unavailable(source, "protected_authentication_material")
    return {"source": source, "read_at": utcnow(), "availability": "available", "content": raw[start:start + size],
            "offset": start, "next_offset": start + size if len(raw) > start + size else None,
            "offset_unit": "unicode_characters", "format": "json"}


def research_session_reference(conn, project_id, params):
    run = _research_run(conn, project_id, params["task_run_id"])
    if not run["session_id"] or not re.fullmatch(r"pi:[0-9a-fA-F-]{36}", run["session_id"]):
        return _unavailable({"task_run_id": run["id"]}, "session_not_recorded")
    project = get_project_or_404(conn, project_id)
    snapshot = json.loads(project["scenario_snapshot_json"] or "{}")
    persisted = snapshot.get("features", {}).get("http_traffic_execution_version") == 1
    return {"project_id": project_id, "task_run_id": run["id"], "session_id": run["session_id"],
            "source": "pi_session", "read_at": utcnow(), "status": run["status"],
            "storage": "persistent_control" if persisted else "legacy_container",
            "offset": params.get("offset", 0), "limit": params.get("limit", 10)}


def admit_research_session_page(conn, project_id, params):
    run = _research_run(conn, project_id, params["task_run_id"])
    page = params["page"]
    if page.get("session_id") != run["session_id"]:
        raise HTTPException(422, "agent_tool_parameters_invalid")
    source = {"kind": "pi_session", "task_run_id": run["id"], "session_id": run["session_id"]}
    if page.get("availability") == "unavailable":
        return _unavailable(source, page["reason"])
    try:
        # 同一 session 的多个执行可能使用过不同凭据，检查全部绑定而非只查最后一次。
        secrets = []
        for item in conn.execute("SELECT id FROM task_runs WHERE project_id = ? AND session_id = ?", (project_id, run["session_id"])).fetchall():
            secrets.extend(_source_secrets(conn, project_id, item["id"]))
    except HTTPException:
        return _unavailable(source, "source_credentials_unverifiable")
    entries, missing = [], list(page["missing"])
    for item in page["entries"]:
        raw = item["raw"]
        if _protected(raw, secrets):
            missing.append({"line": item["line"], "reason": "protected_authentication_material"})
        else:
            entries.append(item)
    return {"source": source, "read_at": page["read_at"], "availability": "partial" if missing or page["incomplete_tail"] else "available",
            "entries": entries, "missing": missing, "next_offset": page["next_offset"], "offset_unit": "jsonl_lines",
            "observed_bytes": page["observed_bytes"], "modified_at": page["modified_at"],
            "incomplete_tail": page["incomplete_tail"],
            "scope": "读取时已落盘的完整 JSONL 行；运行中尚未落盘的内容和认证材料不可读，未修改原件。",
            "source_verification": "unverified",
            "source_limitation": "Server 无法在发布时直接复核相同 session 页；只能作为未核验来源声明，不能填入 source_references。"}
