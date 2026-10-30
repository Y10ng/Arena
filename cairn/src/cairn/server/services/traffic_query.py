"""工作台只读摘要：授权在分页之前，排序与筛选身份不依赖项目局部游标。"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from hashlib import sha256
import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cairn.server.auth import RequestPrincipal, _project_access_exists, verification_project_access_clause
from cairn.server.db import get_conn
from cairn.server.schemas.traffic import TrafficSummaryQuery
from cairn.server.traffic_schema import traffic_request_address
from .traffic import _sync


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    direction: Literal["next", "previous"]
    received_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    project_id: str = Field(min_length=1, max_length=200)
    flow_id: str = Field(min_length=1, max_length=200)


def _encode_cursor(value: _Cursor) -> str:
    raw = json.dumps(value.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(token: str, identity: str) -> _Cursor:
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        cursor = _Cursor.model_validate_json(raw)
        datetime.strptime(cursor.received_at, "%Y-%m-%dT%H:%M:%SZ")
        # 只接受本接口的规范编码；重复键、尾部垃圾和错筛选均不能默默续页。
        if _encode_cursor(cursor) != token or cursor.identity != identity:
            raise ValueError("cursor mismatch")
        return cursor
    except (ValueError, ValidationError, binascii.Error):
        raise HTTPException(422, "traffic_summary_cursor_invalid") from None


def _identity(query: TrafficSummaryQuery, principal: RequestPrincipal, scope: str) -> str:
    value = {"filters": query.model_dump(exclude={"cursor"}), "scope": scope,
             "principal": [principal.kind, principal.subject_id, principal.tenant_id, principal.is_super_admin]}
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


_RELATION = (
    "EXISTS (SELECT 1 FROM vulnerability_observations o JOIN vulnerabilities v "
    "ON v.project_id = o.project_id AND v.id = o.vulnerability_id "
    "WHERE o.project_id = f.project_id AND o.evidence_items_json::jsonb "
    "@> jsonb_build_array(jsonb_build_object('flow_id', f.id)))"
)
_FROM = (
    "FROM http_flows f JOIN projects p ON p.id = f.project_id "
    "JOIN http_capture_scopes s ON s.project_id = f.project_id AND s.id = f.capture_scope_id "
)
_KEY = "(f.received_at, f.project_id, f.id)"


def _filters(query: TrafficSummaryQuery, principal: RequestPrincipal) -> tuple[str, list]:
    access, access_params = verification_project_access_clause(principal)
    clauses, params = [f"({access})"], list(access_params)
    if not principal.is_super_admin:
        clauses.append("p.tenant_id = ?")
        params.append(principal.tenant_id)
    for column, value in (("f.project_id", query.project_id), ("f.id", query.flow_id),
                          ("s.task_run_id", query.task_run_id), ("UPPER(f.method)", query.method),
                          ("f.state", query.state), ("f.status_code", query.status_code),
                          ("f.record_json::jsonb->'error'->>'code'", query.error_code)):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    # strpos 不把用户输入的 %/_ 当通配符；URL/path 保留大小写及原始转义语义。
    for column, value in (("LOWER(f.host)", query.host), ("f.request_url", query.url),
                          ("f.request_path", query.path)):
        if value is not None:
            clauses.append(f"strpos({column}, ?) > 0")
            params.append(value)
    if query.status_family is not None:
        lower = int(query.status_family[0]) * 100
        clauses.append("f.status_code >= ? AND f.status_code < ?")
        params.extend((lower, lower + 100))
    if query.associated is not None:
        clauses.append(_RELATION if query.associated else f"NOT ({_RELATION})")
    return " AND ".join(clauses), params


def _batch_metadata(conn, rows: list) -> tuple[dict, dict, dict]:
    if not rows:
        return {}, {}, {}
    placeholders = ",".join("(?, ?)" for _ in rows)
    params = tuple(value for row in rows for value in (row["project_id"], row["id"]))
    bodies, streams = {}, {}
    for table, target in (("http_flow_bodies", bodies), ("http_flow_body_streams", streams)):
        for item in conn.execute(
            f"SELECT * FROM {table} WHERE (project_id, flow_id) IN ({placeholders})", params,
        ).fetchall():
            target.setdefault((item["project_id"], item["flow_id"]), []).append(item)
    # 每页一次关联查询：同一 Observation 的多个 evidence_items 不重复计数，定位只返回五项。
    associations = conn.execute(
        f"WITH page(project_id, flow_id) AS (VALUES {placeholders}), matches AS ("
        "SELECT page.project_id, page.flow_id, o.id AS observation_id, o.vulnerability_id, "
        "v.status, o.task_run_id, row_number() OVER (PARTITION BY page.project_id, page.flow_id "
        "ORDER BY o.observed_at DESC, o.id DESC) AS position "
        "FROM page JOIN vulnerability_observations o ON o.project_id = page.project_id "
        "AND o.evidence_items_json::jsonb @> jsonb_build_array(jsonb_build_object('flow_id', page.flow_id)) "
        "JOIN vulnerabilities v ON v.project_id = o.project_id AND v.id = o.vulnerability_id) "
        "SELECT project_id, flow_id, count(*) AS observation_count, "
        "count(DISTINCT vulnerability_id) AS vulnerability_count, "
        "jsonb_agg(jsonb_build_object('observation_id', observation_id, 'vulnerability_id', vulnerability_id, "
        "'status', status, 'task_run_id', task_run_id) ORDER BY position) FILTER (WHERE position <= 5) AS items "
        "FROM matches GROUP BY project_id, flow_id", params,
    ).fetchall()
    return bodies, streams, {
        (row["project_id"], row["flow_id"]): {
            "observation_count": row["observation_count"], "vulnerability_count": row["vulnerability_count"],
            "items": row["items"], "has_more": row["observation_count"] > 5,
        } for row in associations
    }


def list_traffic_summaries(
    query: TrafficSummaryQuery, principal: RequestPrincipal, *, project_id: str | None = None,
) -> dict:
    if principal.kind != "user":
        raise HTTPException(403, "User authentication required")
    if project_id is not None:
        if query.project_id is not None and query.project_id != project_id:
            raise HTTPException(422, "traffic_summary_project_conflict")
        query = query.model_copy(update={"project_id": project_id})
    identity = _identity(query, principal, "global" if project_id is None else "project")
    cursor = _decode_cursor(query.cursor, identity) if query.cursor is not None else None
    where, params = _filters(query, principal)
    with get_conn() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        if query.project_id is not None and not _project_access_exists(conn, query.project_id, principal):
            raise HTTPException(404, "Project not found")
        page_where, page_params = where, list(params)
        previous = cursor is not None and cursor.direction == "previous"
        if cursor is not None:
            page_where += f" AND {_KEY} {'>' if previous else '<'} (?, ?, ?)"
            page_params.extend((cursor.received_at, cursor.project_id, cursor.flow_id))
        order = "ASC" if previous else "DESC"
        rows = conn.execute(
            "SELECT f.*, p.title AS project_title, s.task_run_id " + _FROM + "WHERE " + page_where
            + f" ORDER BY f.received_at {order}, f.project_id {order}, f.id {order} LIMIT ?",
            (*page_params, query.limit),
        ).fetchall()
        if previous:
            rows.reverse()
        bodies, streams, associations = _batch_metadata(conn, rows)
        items = []
        for row in rows:
            record = json.loads(row["record_json"])
            key = (row["project_id"], row["id"])
            items.append({
                **{name: row[name] for name in (
                    "id", "project_id", "project_title", "capture_scope_id", "task_run_id", "revision",
                    "received_at", "updated_at", "method", "host", "status_code", "state",
                )},
                "url": row["request_url"], "path": row["request_path"],
                "connection_host": record["request"]["host"],
                "address_issue": traffic_request_address(record["request"])["address_issue"],
                "started_at": record["started_at"], "finished_at": record["finished_at"],
                "source_state": record["state"], "error_code": record["error"]["code"] if record["error"] else None,
                "sync": _sync(record, bodies.get(key, []), streams.get(key, [])),
                "associations": associations.get(key, {
                    "observation_count": 0, "vulnerability_count": 0, "items": [], "has_more": False,
                }),
            })

        def page_cursor(direction: Literal["next", "previous"], boundary) -> str | None:
            if boundary is None:
                return None
            values = (boundary["received_at"], boundary["project_id"], boundary["id"])
            operator = "<" if direction == "next" else ">"
            exists = conn.execute(
                "SELECT 1 " + _FROM + "WHERE " + where + f" AND {_KEY} {operator} (?, ?, ?) LIMIT 1",
                (*params, *values),
            ).fetchone()
            if exists is None:
                return None
            return _encode_cursor(_Cursor(version=1, identity=identity, direction=direction,
                                           received_at=values[0], project_id=values[1], flow_id=values[2]))

        # 实时筛选可能让历史页变空，仍保留原边界供用户向前/后查找，而非跳回首页。
        empty_boundary = ({"received_at": cursor.received_at, "project_id": cursor.project_id,
                           "id": cursor.flow_id} if cursor is not None else None)
        next_cursor = page_cursor("next", rows[-1] if rows else empty_boundary)
        previous_cursor = page_cursor("previous", rows[0] if rows else empty_boundary)
        return {"items": items, "next_cursor": next_cursor, "previous_cursor": previous_cursor,
                "has_more": next_cursor is not None, "has_previous": previous_cursor is not None}
