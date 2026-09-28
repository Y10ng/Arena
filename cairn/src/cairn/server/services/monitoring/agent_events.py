"""Agent event、hook 和 command 聚合。"""

from __future__ import annotations

from .common import *

def _agent_event_summary(conn, *, full: bool = True) -> MonitoringAgentEventSummary:
    now = utcnow()
    # agent event 只取近期窗口；full=false 时保留计数，跳过重型明细列表。
    rows = conn.execute(
        """
        SELECT ae.id, ae.task_run_id, ae.project_id, ae.ts, ae.kind, ae.payload_json,
               tr.task_type, tr.worker_name, tr.worker_type,
               p.title AS project_title
        FROM agent_events ae
        JOIN task_runs tr ON tr.project_id = ae.project_id AND tr.id = ae.task_run_id
        JOIN projects p ON p.id = ae.project_id
        ORDER BY ae.ts DESC, ae.id DESC
        LIMIT ?
        """,
        (AGENT_EVENT_LIMIT,),
    ).fetchall()
    by_kind: dict[str, int] = {}
    command_finished = {
        "success_count": 0,
        "failed_count": 0,
        "unknown_count": 0,
    }
    command_total = 0
    network_flow = {
        "observed_count": 0,
        "allowed_count": 0,
        "blocked_count": 0,
        "failed_count": 0,
        "unknown_count": 0,
    }
    network_flow_total = 0
    network_flow_latest_at = None
    network_flow_by_plane: dict[str, int] = {}
    network_flow_by_protocol: dict[str, int] = {}
    network_flow_by_flow_id: dict[str, int] = {}
    hook_total = 0
    hook_failed_count = 0
    hook_denied_count = 0
    hook_high_risk_count = 0
    hook_latest_at = None
    hook_by_tool: dict[str, int] = {}
    hook_by_event: dict[str, int] = {}
    hook_recent_failures: list[MonitoringHookFailure] = []
    hook_recent_high_risk: list[MonitoringHookFailure] = []
    knowledge_hit_count = 0
    knowledge_hits_by_path: dict[str, MonitoringKnowledgeHit] = {}

    for row in rows:
        kind = row["kind"]
        by_kind[kind] = by_kind.get(kind, 0) + 1
        payload = event_payload(row["payload_json"])
        if kind == "network_flow":
            if not full:
                continue
            network_flow_total += 1
            if network_flow_latest_at is None:
                network_flow_latest_at = row["ts"]
            _accumulate_network_flow_summary(
                payload,
                counts=network_flow,
                by_plane=network_flow_by_plane,
                by_protocol=network_flow_by_protocol,
                by_flow_id=network_flow_by_flow_id,
            )
            continue
        if kind == "hook_operation":
            hook_total += 1
            if hook_latest_at is None:
                hook_latest_at = row["ts"]
            hook_event = hook_event_to_model(row)
            tool_name = hook_event.tool_name
            hook_by_tool[tool_name] = hook_by_tool.get(tool_name, 0) + 1
            event_name = hook_event.hook_event_name
            hook_by_event[event_name] = hook_by_event.get(event_name, 0) + 1
            status = hook_event.status
            risk_level = hook_event.risk_level
            failure = None
            if status == "failed":
                hook_failed_count += 1
                if full and len(hook_recent_failures) < HOOK_OPERATION_ITEM_LIMIT:
                    failure = failure or _monitoring_hook_failure(row, hook_event)
                    hook_recent_failures.append(failure)
            if status == "denied":
                hook_denied_count += 1
                if full and len(hook_recent_failures) < HOOK_OPERATION_ITEM_LIMIT:
                    failure = failure or _monitoring_hook_failure(row, hook_event)
                    hook_recent_failures.append(failure)
            if risk_level == "high":
                hook_high_risk_count += 1
                if full and len(hook_recent_high_risk) < HOOK_OPERATION_ITEM_LIMIT:
                    failure = failure or _monitoring_hook_failure(row, hook_event)
                    hook_recent_high_risk.append(failure)
            knowledge_path = _knowledge_hit_path(hook_event)
            if knowledge_path is not None:
                # 知识库命中只基于 Read hook 推断，是审计提示而非访问控制日志。
                knowledge_hit_count += 1
                if full:
                    previous = knowledge_hits_by_path.get(knowledge_path)
                    knowledge_hits_by_path[knowledge_path] = MonitoringKnowledgeHit(
                        file_path=knowledge_path,
                        count=(previous.count if previous is not None else 0) + 1,
                        latest_task_run_id=previous.latest_task_run_id if previous is not None else row["task_run_id"],
                        latest_project_id=previous.latest_project_id if previous is not None else row["project_id"],
                        latest_project_title=previous.latest_project_title if previous is not None else row["project_title"],
                        latest_task_type=previous.latest_task_type if previous is not None else row["task_type"],
                        latest_worker_name=previous.latest_worker_name if previous is not None else row["worker_name"],
                        latest_worker_type=previous.latest_worker_type if previous is not None else row["worker_type"],
                        latest_accessed_at=previous.latest_accessed_at if previous is not None else row["ts"],
                    )
            continue
        if kind != "command_finished":
            continue
        if not full:
            continue
        command_total += 1
        status = command_finished_status(payload)
        if status in {"success", "failed"}:
            command_finished[f"{status}_count"] += 1
        else:
            command_finished["unknown_count"] += 1

    return MonitoringAgentEventSummary(
        recent_event_limit=AGENT_EVENT_LIMIT,
        event_count=len(rows),
        latest_event_at=rows[0]["ts"] if rows else None,
        by_kind=[
            MonitoringAgentEventKindSummary(kind=kind, count=count)
            for kind, count in sorted(by_kind.items(), key=lambda item: (-item[1], item[0]))
        ],
        command_finished=MonitoringCommandEventSummary(
            total_count=command_total,
            **command_finished,
        ),
        network_flow=MonitoringNetworkFlowSummary(
            total_count=network_flow_total,
            latest_event_at=network_flow_latest_at,
            by_network_plane=dict(sorted(network_flow_by_plane.items())),
            by_protocol=dict(sorted(network_flow_by_protocol.items())),
            by_flow_id=dict(sorted(network_flow_by_flow_id.items())),
            **network_flow,
        ),
        hook_operation=MonitoringHookOperationSummary(
            total_count=hook_total,
            failed_count=hook_failed_count,
            denied_count=hook_denied_count,
            high_risk_count=hook_high_risk_count,
            latest_event_at=hook_latest_at,
            by_tool=dict(sorted(hook_by_tool.items())),
            by_event=dict(sorted(hook_by_event.items())),
            knowledge_hit_count=knowledge_hit_count,
            knowledge_hits=sorted(
                knowledge_hits_by_path.values(),
                key=lambda item: (-item.count, item.file_path),
            )[:KNOWLEDGE_HIT_ITEM_LIMIT]
            if full
            else [],
            recent_failures=hook_recent_failures,
            recent_high_risk=hook_recent_high_risk,
        ),
        running_commands=_running_commands(conn, now) if full else [],
        recent_command_failures=_recent_command_failures(conn) if full else [],
    )


def _knowledge_hit_path(hook_event) -> str | None:
    if hook_event.tool_name != "Read" or not hook_event.file_path:
        return None
    # hook 里的路径可能是相对路径或平台分隔符，统一成知识库挂载路径下的安全展示路径。
    path = hook_event.file_path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    knowledge_path = runtime_knowledge_base_path().strip().replace("\\", "/")
    prefix = f"{knowledge_path}/"
    if not path.startswith(prefix):
        return None
    suffix = path[len(prefix):]
    parts = [part for part in suffix.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    return f"{knowledge_path}/{'/'.join(parts)}"[:256]


def _accumulate_network_flow_summary(
    payload: dict,
    *,
    counts: dict[str, int],
    by_plane: dict[str, int],
    by_protocol: dict[str, int],
    by_flow_id: dict[str, int],
) -> None:
    status = network_flow_status(payload)
    counts[f"{status}_count"] += 1
    plane = network_flow_network_plane(payload.get("network_plane") or payload.get("plane"))
    by_plane[plane] = by_plane.get(plane, 0) + 1
    protocol = network_flow_protocol(
        payload.get("protocol")
        or payload.get("transport")
        or payload.get("scheme")
    )
    by_protocol[protocol] = by_protocol.get(protocol, 0) + 1
    if payload.get("flow_id") is not None:
        flow_id = network_flow_id(payload.get("flow_id"))
        by_flow_id[flow_id] = by_flow_id.get(flow_id, 0) + 1


def _recent_command_failures(conn) -> list[MonitoringCommandFailure]:
    rows = conn.execute(
        """
        SELECT ae.id AS event_id, ae.task_run_id, ae.project_id, ae.ts, ae.payload_json,
               tr.task_type, tr.worker_name, tr.worker_type,
               p.title AS project_title
        FROM agent_events ae
        JOIN task_runs tr ON tr.project_id = ae.project_id AND tr.id = ae.task_run_id
        JOIN projects p ON p.id = ae.project_id
        WHERE ae.kind = 'command_finished'
        ORDER BY ae.ts DESC, ae.id DESC
        LIMIT ?
        """,
        (AGENT_EVENT_LIMIT,),
    ).fetchall()
    failures: list[MonitoringCommandFailure] = []
    for row in rows:
        payload = event_payload(row["payload_json"])
        if command_finished_status(payload) != "failed":
            continue
        failures.append(
            MonitoringCommandFailure(
                event_id=row["event_id"],
                task_run_id=row["task_run_id"],
                project_id=row["project_id"],
                project_title=row["project_title"],
                task_type=row["task_type"],
                worker_name=row["worker_name"],
                worker_type=row["worker_type"],
                phase=payload_text(payload.get("phase")) or "unknown",
                command_kind=sanitized_command_kind(payload.get("command_kind")),
                exit_code=integer_payload_value(payload.get("exit_code")),
                returncode=integer_payload_value(payload.get("returncode")),
                timed_out=payload.get("timed_out") is True,
                cancelled=payload.get("cancelled") is True,
                duration_seconds=duration_payload_value(payload.get("duration_seconds")),
                finished_at=row["ts"],
            )
        )
        if len(failures) >= COMMAND_FAILURE_LIMIT:
            break
    return failures


def _monitoring_hook_failure(row, hook_event) -> MonitoringHookFailure:
    return MonitoringHookFailure(
        event_id=row["id"],
        task_run_id=row["task_run_id"],
        project_id=row["project_id"],
        project_title=row["project_title"],
        task_type=row["task_type"],
        worker_name=row["worker_name"],
        worker_type=row["worker_type"],
        hook_event_name=hook_event.hook_event_name,
        tool_name=hook_event.tool_name,
        status=hook_event.status,
        risk_level=hook_event.risk_level,
        summary=hook_event.summary,
        artifact_id=hook_event.artifact_id,
        occurred_at=row["ts"],
    )


def _running_commands(conn, now: str) -> list[MonitoringRunningCommand]:
    # running command 由 started/finished 事件配对推断，缺 finished 才认为仍在执行。
    rows = conn.execute(
        """
        SELECT ae.task_run_id, ae.project_id, ae.ts, ae.payload_json,
               tr.task_type, tr.worker_name, tr.worker_type,
               p.title AS project_title
        FROM agent_events ae
        JOIN task_runs tr ON tr.project_id = ae.project_id AND tr.id = ae.task_run_id
        JOIN projects p ON p.id = ae.project_id
        WHERE ae.kind = 'command_started' AND tr.status = 'running'
        ORDER BY ae.ts DESC, ae.id DESC
        LIMIT ?
        """,
        (AGENT_EVENT_LIMIT,),
    ).fetchall()
    if not rows:
        return []

    finished_rows = conn.execute(
        """
        SELECT ae.task_run_id, ae.project_id, ae.ts, ae.payload_json
        FROM agent_events ae
        JOIN task_runs tr ON tr.project_id = ae.project_id AND tr.id = ae.task_run_id
        WHERE ae.kind = 'command_finished' AND tr.status = 'running'
        ORDER BY ae.ts DESC, ae.id DESC
        LIMIT ?
        """,
        (AGENT_EVENT_LIMIT,),
    ).fetchall()
    latest_finished_by_phase: dict[tuple[str, str, str], str] = {}
    for row in finished_rows:
        payload = event_payload(row["payload_json"])
        phase = payload_text(payload.get("phase"))
        if phase is None:
            continue
        key = (row["project_id"], row["task_run_id"], phase)
        if key not in latest_finished_by_phase:
            latest_finished_by_phase[key] = row["ts"]

    commands: list[MonitoringRunningCommand] = []
    seen_tasks: set[tuple[str, str]] = set()
    for row in rows:
        task_key = (row["project_id"], row["task_run_id"])
        if task_key in seen_tasks:
            continue
        seen_tasks.add(task_key)
        payload = event_payload(row["payload_json"])
        phase = payload_text(payload.get("phase"))
        if phase is None:
            phase = "unknown"
        finished_at = latest_finished_by_phase.get((row["project_id"], row["task_run_id"], phase))
        if finished_at is not None and finished_at >= row["ts"]:
            continue
        command_kind = sanitized_command_kind(payload.get("command_kind"))
        age_seconds = conn.execute(
            "SELECT CAST(MAX(0, (julianday(?) - julianday(?)) * 86400) AS INTEGER) AS age_seconds",
            (now, row["ts"]),
        ).fetchone()["age_seconds"]
        commands.append(
            MonitoringRunningCommand(
                task_run_id=row["task_run_id"],
                project_id=row["project_id"],
                project_title=row["project_title"],
                task_type=row["task_type"],
                worker_name=row["worker_name"],
                worker_type=row["worker_type"],
                phase=phase,
                command_kind=command_kind,
                started_at=row["ts"],
                age_seconds=age_seconds,
            )
        )
    return sorted(
        commands,
        key=lambda command: (-(command.age_seconds or 0), command.project_id, command.task_run_id),
    )
