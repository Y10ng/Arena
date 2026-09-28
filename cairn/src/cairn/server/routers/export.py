"""项目导出接口，将 fact graph 和审计链路整理为 YAML、timeline 或 audit 视图。"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from datetime import datetime
from json import loads
import yaml

from cairn.server.auth import require_tenant_project
from cairn.server.db import get_conn
from cairn.server.services import expire_reason_leases, expire_workers, get_project_or_404

router = APIRouter(tags=["export"], dependencies=[Depends(require_tenant_project)])


def format_export_timestamp(value: str | None) -> str | None:
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _load_project_data(conn, project_id: str):
    # 导出前也刷新租约状态，避免把已超时的 worker 误标为仍在工作。
    expire_workers(conn, project_id)
    expire_reason_leases(conn, project_id)
    proj = get_project_or_404(conn, project_id)

    facts = conn.execute(
        "SELECT id, description, assessment_record_json FROM facts WHERE project_id = ?", (project_id,)
    ).fetchall()
    hints = conn.execute(
        "SELECT content, creator, fact_id, intent_id, created_at FROM hints WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    targets = conn.execute(
        """
        SELECT id, value, type, status, source_fact_id, discovered_by_run_id, created_at
        FROM targets
        WHERE project_id = ?
        ORDER BY created_at, id
        """,
        (project_id,),
    ).fetchall()
    intents = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    charge_compensations = conn.execute(
        """
        SELECT *
        FROM assessment_charge_compensations
        WHERE project_id = ?
        ORDER BY created_at, assessment_attempt_id
        """,
        (project_id,),
    ).fetchall()
    authorization_grants = conn.execute(
        """
        SELECT *
        FROM project_authorization_grants
        WHERE project_id = ?
        ORDER BY authorization_epoch
        """,
        (project_id,),
    ).fetchall()

    sources_by_intent = {}
    for i in intents:
        rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY source_order, fact_id",
            (i["id"], project_id),
        ).fetchall()
        sources_by_intent[i["id"]] = [r["fact_id"] for r in rows]

    return (
        proj,
        facts,
        hints,
        targets,
        intents,
        sources_by_intent,
        charge_compensations,
        authorization_grants,
    )


def _export_yaml(conn, project_id: str) -> str:
    (
        proj,
        facts,
        hints,
        targets,
        intents,
        sources_by_intent,
        charge_compensations,
        authorization_grants,
    ) = _load_project_data(conn, project_id)

    origin_desc = ""
    goal_desc = ""
    for f in facts:
        if f["id"] == "origin":
            origin_desc = f["description"]
        elif f["id"] == "goal":
            goal_desc = f["description"]

    data: dict = {
        "project": {
            "title": proj["title"],
            "origin": origin_desc,
            "goal": goal_desc,
        }
    }

    # YAML 导出偏向给人阅读，保留中文和原始顺序，不强制补空字段。
    if hints:
        data["hints"] = [
            {
                "content": h["content"],
                "creator": h["creator"],
                "fact_id": h["fact_id"],
                "intent_id": h["intent_id"],
                "created_at": format_export_timestamp(h["created_at"]),
            }
            for h in hints
        ]

    if targets:
        data["targets"] = [
            {
                "id": target["id"],
                "value": target["value"],
                "type": target["type"],
                "status": target["status"],
                "source_fact_id": target["source_fact_id"],
                "discovered_by_run_id": target["discovered_by_run_id"],
                "created_at": format_export_timestamp(target["created_at"]),
            }
            for target in targets
        ]

    data["facts"] = []
    for fact in facts:
        entry = {"id": fact["id"], "description": fact["description"]}
        record = _loads_json_object(fact["assessment_record_json"])
        if record:
            # 完整实验留在按需读取的图谱文件中，不要求每轮展开全部历史。
            entry["assessment_record"] = record
        data["facts"].append(entry)

    intent_list = []
    for i in intents:
        entry: dict = {
            "from": sources_by_intent.get(i["id"], []),
            "to": i["to_fact_id"],
            "description": i["description"],
            "creator": i["creator"],
            "worker": i["worker"],
            "created_at": format_export_timestamp(i["created_at"]),
            "concluded_at": format_export_timestamp(i["concluded_at"]),
        }
        # conclusion 让导出可区分 completed/abandoned/failed，供图谱按收敛方式渲染。
        conclusion = i["conclusion"] if "conclusion" in i.keys() else None
        if conclusion:
            entry["conclusion"] = conclusion
        depends_on = i["depends_on"] if "depends_on" in i.keys() else None
        if depends_on:
            entry["depends_on"] = depends_on
        intent_list.append(entry)

    if intent_list:
        data["intents"] = intent_list

    if charge_compensations:
        data["assessment_charge_compensations"] = [
            {
                "assessment_attempt_id": row["assessment_attempt_id"],
                "migration_version": row["migration_version"],
                "predicate_version": row["predicate_version"],
                "reason_code": row["reason_code"],
                "original_charged": row["original_charged"],
                "charge_delta": row["charge_delta"],
                "project_counter_before": row["project_counter_before"],
                "project_counter_after": row["project_counter_after"],
                "source_reason_task_run_id": row["source_reason_task_run_id"],
                "task_run_ids": _loads_json_list(row["task_run_ids_json"]),
                "proof_sha256": row["proof_sha256"],
                "created_at": format_export_timestamp(row["created_at"]),
            }
            for row in charge_compensations
        ]

    if authorization_grants:
        data["project_authorization_grants"] = [
            {
                "authorization_epoch": row["authorization_epoch"],
                "protocol": row["protocol"],
                "source": row["source"],
                "actor_type": row["actor_type"],
                "actor_id": row["actor_id"],
                "scope_digest": row["scope_digest"],
                "scope_descriptor": _loads_json_object(
                    row["scope_descriptor_json"]
                ),
                "window_seconds": row["window_seconds"],
                "previous_deadline": row["previous_deadline"],
                "new_deadline": row["new_deadline"],
                "execution_generation": row["execution_generation"],
                "authorized_at": format_export_timestamp(row["authorized_at"]),
            }
            for row in authorization_grants
        ]

    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _export_timeline(conn, project_id: str) -> str:
    (
        proj,
        facts,
        hints,
        _targets,
        intents,
        sources_by_intent,
        charge_compensations,
        authorization_grants,
    ) = _load_project_data(conn, project_id)

    facts_by_id = {f["id"]: f["description"] for f in facts}

    events: list[tuple[str, int, str]] = []  # (timestamp, order, text)，order 用于同时间事件稳定排序。
    order = 0

    origin_desc = facts_by_id.get("origin", "")
    goal_desc = facts_by_id.get("goal", "")
    ts = format_export_timestamp(proj["created_at"]) or ""
    block = f"[{ts}] PROJECT CREATED\n  origin: {origin_desc}\n  goal: {goal_desc}"
    events.append((proj["created_at"] or "", order, block))
    order += 1

    for h in hints:
        ts = format_export_timestamp(h["created_at"]) or ""
        block = f"[{ts}] HINT by {h['creator']}\n  {h['content']}"
        events.append((h["created_at"] or "", order, block))
        order += 1

    for i in intents:
        src = sources_by_intent.get(i["id"], [])
        from_str = ", ".join(src)

        ts = format_export_timestamp(i["created_at"]) or ""
        meta = f"  from: {from_str}"
        if i["worker"] and not i["concluded_at"]:
            meta += f"\n  worker: {i['worker']} (in progress)"
        block = f"[{ts}] INTENT DECLARED {i['id']} by {i['creator']}\n{meta}\n  {i['description']}"
        events.append((i["created_at"] or "", order, block))
        order += 1

        if not i["concluded_at"]:
            continue

        ts = format_export_timestamp(i["concluded_at"]) or ""
        actor = i["worker"] or i["creator"]
        conclusion = i["conclusion"] if "conclusion" in i.keys() else None

        # abandoned/failed：无结论 fact，单独记一条收尾事件，避免在图上"没有下文"。
        if not i["to_fact_id"]:
            label = {"abandoned": "INTENT ABANDONED", "failed": "INTENT FAILED"}.get(
                conclusion or "", "INTENT CLOSED"
            )
            block = f"[{ts}] {label} {i['id']} by {actor}\n  from: {from_str}\n  {i['description']}"
            events.append((i["concluded_at"] or "", order, block))
            order += 1
            continue

        if i["to_fact_id"] == "goal":
            block = f"[{ts}] PROJECT COMPLETED by {actor}\n  via: {i['id']} from {from_str}"
        else:
            fact_desc = facts_by_id.get(i["to_fact_id"], "")
            block = f"[{ts}] INTENT CONCLUDED {i['id']} by {actor}\n  from: {from_str}\n  produced: {i['to_fact_id']}\n  {fact_desc}"

        events.append((i["concluded_at"] or "", order, block))
        order += 1

    for compensation in charge_compensations:
        ts = format_export_timestamp(compensation["created_at"]) or ""
        task_run_ids = ", ".join(
            _loads_json_list(compensation["task_run_ids_json"])
        )
        block = (
            f"[{ts}] ASSESSMENT CHARGE COMPENSATED "
            f"{compensation['assessment_attempt_id']}\n"
            f"  reason: {compensation['reason_code']}\n"
            f"  delta: {compensation['charge_delta']} "
            f"({compensation['project_counter_before']} -> "
            f"{compensation['project_counter_after']})\n"
            f"  task_runs: {task_run_ids}\n"
            f"  proof_sha256: {compensation['proof_sha256']}"
        )
        events.append((compensation["created_at"] or "", order, block))
        order += 1

    authorization_labels = {
        "legacy_backfill": "PROJECT AUTHORIZATION BACKFILLED",
        "project_creation": "PROJECT AUTHORIZED",
        "explicit_reauthorization": "PROJECT REAUTHORIZED",
    }
    for grant in authorization_grants:
        ts = format_export_timestamp(grant["authorized_at"]) or ""
        label = authorization_labels.get(
            grant["source"], "PROJECT AUTHORIZATION RECORDED"
        )
        block = (
            f"[{ts}] {label} epoch {grant['authorization_epoch']}\n"
            f"  actor: {grant['actor_type']}:{grant['actor_id']}\n"
            f"  scope_digest: {grant['scope_digest']}\n"
            f"  window_seconds: {grant['window_seconds']}\n"
            f"  deadline: {grant['new_deadline']}\n"
            f"  execution_generation: {grant['execution_generation']}"
        )
        events.append((grant["authorized_at"] or "", order, block))
        order += 1

    events.sort(key=lambda e: (e[0], e[1]))

    return "\n\n".join(e[2] for e in events) + "\n"


def _export_audit(conn, project_id: str, fact_id: str | None) -> str:
    if not fact_id:
        raise HTTPException(400, "fact_id is required for audit export")

    expire_workers(conn, project_id)
    expire_reason_leases(conn, project_id)
    proj = get_project_or_404(conn, project_id)
    fact = conn.execute(
        "SELECT * FROM facts WHERE project_id = ? AND id = ?",
        (project_id, fact_id),
    ).fetchone()
    if fact is None:
        raise HTTPException(404, f"Fact {fact_id} not found")

    intents = conn.execute(
        """
        SELECT * FROM intents
        WHERE project_id = ? AND to_fact_id = ?
        ORDER BY COALESCE(concluded_at, created_at), id
        """,
        (project_id, fact_id),
    ).fetchall()
    sources_by_intent = _sources_by_intent(conn, project_id, [intent["id"] for intent in intents])

    evidence_rows = conn.execute(
        """
        SELECT
            fe.project_id,
            fe.fact_id,
            fe.artifact_id,
            fe.pointer,
            fe.verifier_run_id,
            fe.created_at AS evidence_created_at,
            a.task_run_id AS artifact_task_run_id,
            a.kind AS artifact_kind,
            a.category AS artifact_category,
            a.uri AS artifact_uri,
            a.sha256 AS artifact_sha256,
            a.size AS artifact_size,
            a.created_at AS artifact_created_at,
            a.metadata_json AS artifact_metadata_json
        FROM fact_evidence fe
        LEFT JOIN artifacts a
          ON a.project_id = fe.project_id
         AND a.id = fe.artifact_id
        WHERE fe.project_id = ? AND fe.fact_id = ?
        ORDER BY fe.created_at, fe.artifact_id, fe.pointer
        """,
        (project_id, fact_id),
    ).fetchall()

    # audit 视图只聚焦与目标 fact 直接相关的 run/event，避免导出整项目噪声。
    related_task_run_ids = _related_audit_task_run_ids(fact, intents, evidence_rows)
    task_runs = _task_runs_by_id(conn, project_id, related_task_run_ids)
    audit_events = _audit_events_for_task_runs(conn, project_id, list(task_runs))

    data = {
        "project": {
            "id": proj["id"],
            "title": proj["title"],
            "status": proj["status"],
            "created_at": proj["created_at"],
        },
        "fact": _fact_summary(fact),
        "conclusion_intents": [
            _intent_summary(intent, sources_by_intent.get(intent["id"], []))
            for intent in intents
        ],
        "evidence": [_evidence_summary(row) for row in evidence_rows],
        "task_runs": [task_runs[task_run_id] for task_run_id in sorted(task_runs)],
        "audit_events": audit_events,
    }
    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _sources_by_intent(conn, project_id: str, intent_ids: list[str]) -> dict[str, list[str]]:
    if not intent_ids:
        return {}
    placeholders = ", ".join("?" for _ in intent_ids)
    rows = conn.execute(
        f"""
        SELECT intent_id, fact_id
        FROM intent_sources
        WHERE project_id = ? AND intent_id IN ({placeholders})
        ORDER BY intent_id, source_order, fact_id
        """,
        (project_id, *intent_ids),
    ).fetchall()
    sources: dict[str, list[str]] = {intent_id: [] for intent_id in intent_ids}
    for row in rows:
        sources.setdefault(row["intent_id"], []).append(row["fact_id"])
    return sources


def _related_audit_task_run_ids(fact, intents, evidence_rows) -> set[str]:
    task_run_ids: set[str] = set()
    if fact["task_run_id"]:
        task_run_ids.add(fact["task_run_id"])
    for intent in intents:
        if intent["task_run_id"]:
            task_run_ids.add(intent["task_run_id"])
    for row in evidence_rows:
        if row["artifact_task_run_id"]:
            task_run_ids.add(row["artifact_task_run_id"])
        if row["verifier_run_id"]:
            task_run_ids.add(row["verifier_run_id"])
    return task_run_ids


def _task_runs_by_id(conn, project_id: str, task_run_ids: set[str]) -> dict[str, dict]:
    if not task_run_ids:
        return {}
    ordered_ids = sorted(task_run_ids)
    placeholders = ", ".join("?" for _ in ordered_ids)
    rows = conn.execute(
        f"""
        SELECT * FROM task_runs
        WHERE project_id = ? AND id IN ({placeholders})
        ORDER BY COALESCE(started_at, id), id
        """,
        (project_id, *ordered_ids),
    ).fetchall()
    return {row["id"]: _task_run_summary(row) for row in rows}


def _audit_events_for_task_runs(conn, project_id: str, task_run_ids: list[str]) -> list[dict]:
    if not task_run_ids:
        return []
    included_kinds = (
        "validation_result",
        "decision_result",
        "protocol_write",
        "artifact_created",
        "hook_operation",
    )
    task_placeholders = ", ".join("?" for _ in task_run_ids)
    kind_placeholders = ", ".join("?" for _ in included_kinds)
    rows = conn.execute(
        f"""
        SELECT * FROM agent_events
        WHERE project_id = ?
          AND task_run_id IN ({task_placeholders})
          AND kind IN ({kind_placeholders})
        ORDER BY ts, id
        """,
        (project_id, *task_run_ids, *included_kinds),
    ).fetchall()
    return [_agent_event_summary(row) for row in rows]


def _fact_summary(row) -> dict:
    result = {
        "id": row["id"],
        "description": row["description"],
        "task_run_id": row["task_run_id"],
        "creator": row["creator"],
        "created_at": row["created_at"],
    }
    record = _loads_json_object(row["assessment_record_json"])
    if record:
        result["assessment_record"] = record
    return result


def _intent_summary(row, from_ids: list[str]) -> dict:
    return {
        "id": row["id"],
        "from": from_ids,
        "to": row["to_fact_id"],
        "description": row["description"],
        "creator": row["creator"],
        "task_run_id": row["task_run_id"],
        "worker": row["worker"],
        "created_at": row["created_at"],
        "concluded_at": row["concluded_at"],
    }


def _evidence_summary(row) -> dict:
    artifact = None
    if row["artifact_kind"] is not None:
        artifact = {
            "id": row["artifact_id"],
            "task_run_id": row["artifact_task_run_id"],
            "kind": row["artifact_kind"],
            "category": row["artifact_category"],
            "uri": row["artifact_uri"],
            "sha256": row["artifact_sha256"],
            "size": row["artifact_size"],
            "created_at": row["artifact_created_at"],
            "metadata": _loads_json_object(row["artifact_metadata_json"]),
        }
    return {
        "fact_id": row["fact_id"],
        "artifact_id": row["artifact_id"],
        "pointer": row["pointer"] or "",
        "verifier_run_id": row["verifier_run_id"],
        "created_at": row["evidence_created_at"],
        "artifact": artifact,
    }


def _task_run_summary(row) -> dict:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "intent_id": row["intent_id"],
        "task_type": row["task_type"],
        "worker_name": row["worker_name"],
        "worker_type": row["worker_type"],
        "status": row["status"],
        "session_id": row["session_id"],
        "prompt_group": row["prompt_group"],
        "prompt_version": row["prompt_version"],
        "execution_profile": row["execution_profile"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "exit_code": row["exit_code"],
        "error_type": row["error_type"],
        "error_message": row["error_message"],
        "runtime_context": _loads_json_object(row["runtime_context_json"]),
    }


def _agent_event_summary(row) -> dict:
    return {
        "id": row["id"],
        "task_run_id": row["task_run_id"],
        "ts": row["ts"],
        "kind": row["kind"],
        "payload": _loads_json_object(row["payload_json"]),
    }


def _loads_json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        loaded = loads(value)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _loads_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        loaded = loads(value)
    except ValueError:
        return []
    if not isinstance(loaded, list) or any(not isinstance(item, str) for item in loaded):
        return []
    return loaded


@router.get("/projects/{project_id}/export")
def export_project(project_id: str, format: str = "yaml", fact_id: str | None = None):
    if format not in ("yaml", "timeline", "audit"):
        raise HTTPException(400, "Supported formats: yaml, timeline, audit")

    with get_conn() as conn:
        if format == "audit":
            text = _export_audit(conn, project_id, fact_id)
        elif format == "timeline":
            text = _export_timeline(conn, project_id)
        else:
            text = _export_yaml(conn, project_id)

        return Response(content=text, media_type="text/plain")
