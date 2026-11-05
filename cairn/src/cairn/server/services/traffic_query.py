"""工作台只读摘要：授权在分页之前，排序与筛选身份不依赖项目局部游标。"""

from __future__ import annotations

import json

from fastapi import HTTPException

from cairn.server.auth import RequestPrincipal, _project_access_exists, verification_project_access_clause
from cairn.server.db import get_conn
from cairn.server.schemas.traffic import TrafficSummaryQuery
from cairn.server.traffic_schema import traffic_request_address
from .traffic import _sync


def _relation(*, vulnerability: bool = False, observation: bool = False, include_record: bool = False) -> str:
    observation_scope = (" AND o.vulnerability_id = ?" if vulnerability else "") + (" AND o.id = ?" if observation else "")
    observed = (
        "EXISTS (SELECT 1 FROM vulnerability_observations o JOIN vulnerabilities v "
        "ON v.project_id = o.project_id AND v.id = o.vulnerability_id "
        "WHERE o.project_id = f.project_id AND o.evidence_items_json::jsonb "
        "@> jsonb_build_array(jsonb_build_object('flow_id', f.id))" + observation_scope + ")"
    )
    if observation and not include_record:
        return observed
    recorded = (
        "EXISTS (SELECT 1 FROM vulnerabilities v WHERE v.project_id = f.project_id "
        "AND v.evidence_items_json::jsonb @> jsonb_build_array(jsonb_build_object('flow_id', f.id))"
        + (" AND v.id = ?" if vulnerability else "")
        + (" AND EXISTS (SELECT 1 FROM vulnerability_observations so WHERE so.project_id = v.project_id AND so.vulnerability_id = v.id AND so.id = ?)" if observation else "") + ")"
    )
    return f"({observed} OR {recorded})"


_RELATION = _relation()
_FROM = (
    "FROM http_flows f JOIN projects p ON p.id = f.project_id "
    "JOIN http_capture_scopes s ON s.project_id = f.project_id AND s.id = f.capture_scope_id "
)


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
    if query.vulnerability_id is not None or query.observation_id is not None:
        clauses.append(_relation(vulnerability=query.vulnerability_id is not None, observation=query.observation_id is not None, include_record=query.include_record_evidence))
        if query.vulnerability_id is not None:
            params.append(query.vulnerability_id)
        if query.observation_id is not None:
            params.append(query.observation_id)
        if query.vulnerability_id is not None and (query.observation_id is None or query.include_record_evidence):
            params.append(query.vulnerability_id)
        if query.include_record_evidence:
            params.append(query.observation_id)
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
    # 同一来源的多个引用只计一次；人工补充不增加 Observation 数。
    associations = conn.execute(
        f"WITH page(project_id, flow_id) AS (VALUES {placeholders}), relations AS ("
        "SELECT page.project_id, page.flow_id, o.id AS observation_id, o.vulnerability_id, "
        "v.status, o.task_run_id, 'observation' AS source, o.observed_at AS linked_at "
        "FROM page JOIN vulnerability_observations o ON o.project_id = page.project_id "
        "AND o.evidence_items_json::jsonb @> jsonb_build_array(jsonb_build_object('flow_id', page.flow_id)) "
        "JOIN vulnerabilities v ON v.project_id = o.project_id AND v.id = o.vulnerability_id "
        "UNION ALL SELECT page.project_id, page.flow_id, NULL, v.id, v.status, NULL, 'record', v.updated_at "
        "FROM page JOIN vulnerabilities v ON v.project_id = page.project_id "
        "AND v.evidence_items_json::jsonb @> jsonb_build_array(jsonb_build_object('flow_id', page.flow_id))), matches AS ("
        "SELECT *, row_number() OVER (PARTITION BY project_id, flow_id "
        "ORDER BY linked_at DESC, vulnerability_id, observation_id NULLS LAST) AS position FROM relations) "
        "SELECT project_id, flow_id, count(observation_id) AS observation_count, count(*) AS relation_count, "
        "count(DISTINCT vulnerability_id) AS vulnerability_count, "
        "jsonb_agg(jsonb_build_object('observation_id', observation_id, 'vulnerability_id', vulnerability_id, "
        "'status', status, 'task_run_id', task_run_id, 'source', source) ORDER BY position) FILTER (WHERE position <= 5) AS items "
        "FROM matches GROUP BY project_id, flow_id", params,
    ).fetchall()
    return bodies, streams, {
        (row["project_id"], row["flow_id"]): {
            "observation_count": row["observation_count"], "vulnerability_count": row["vulnerability_count"],
            "items": row["items"], "has_more": row["relation_count"] > 5,
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
    where, params = _filters(query, principal)
    with get_conn() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        if query.project_id is not None and not _project_access_exists(conn, query.project_id, principal):
            raise HTTPException(404, "Project not found")
        # COUNT 与当前页共用权限、筛选和数据库视图；关联用 EXISTS，不扩增 flow 行数。
        total = conn.execute("SELECT COUNT(*) AS total " + _FROM + "WHERE " + where, params).fetchone()["total"]
        total_pages = (total + query.limit - 1) // query.limit
        page = min(query.page, max(1, total_pages))
        rows = conn.execute(
            "SELECT f.*, p.title AS project_title, s.task_run_id " + _FROM + "WHERE " + where
            + " ORDER BY f.received_at DESC, f.project_id DESC, f.id DESC LIMIT ? OFFSET ?",
            (*params, query.limit, (page - 1) * query.limit),
        ).fetchall()
        bodies, streams, associations = _batch_metadata(conn, rows)
        items = []
        for row in rows:
            record = json.loads(row["record_json"])
            key = (row["project_id"], row["id"])
            address = traffic_request_address(record["request"])
            items.append({
                **{name: row[name] for name in (
                    "id", "project_id", "project_title", "capture_scope_id", "task_run_id", "revision",
                    "received_at", "updated_at", "method", "host", "status_code", "state",
                )},
                "url": row["request_url"], "path": row["request_path"],
                "connection_host": record["request"]["host"],
                **{name: address[name] for name in ("address_issue", "display_host", "display_target")},
                "started_at": record["started_at"], "finished_at": record["finished_at"],
                "source_state": record["state"], "error_code": record["error"]["code"] if record["error"] else None,
                "sync": _sync(record, bodies.get(key, []), streams.get(key, [])),
                "associations": associations.get(key, {
                    "observation_count": 0, "vulnerability_count": 0, "items": [], "has_more": False,
                }),
            })

        return {"items": items, "page": page, "limit": query.limit, "total": total, "total_pages": total_pages}
