"""审计 agent event 服务。"""

from __future__ import annotations

from .common import *
from .event_payloads import (
    agent_event_to_model,
    command_event_to_model,
    hook_event_to_model,
    prompt_event_to_model,
    result_event_to_model,
)
from .ids import get_project_or_404, next_agent_event_id
from .projects import validate_task_run_exists
from .runtime_context import task_run_runtime_context
from .time_utils import utcnow
from .payload_values import dump_json

from cairn.server.db import get_conn
from cairn.server.models import (
    AgentEvent,
    CreateAgentEventRequest,
    TaskRunCommandEvents,
    TaskRunHookEvents,
    TaskRunPromptEvents,
    TaskRunResultEvents,
)

RESULT_AUDIT_EVENT_KINDS = ("validation_result", "decision_result", "protocol_write", "network_flow")
COMMAND_EVENT_KINDS = ("command_started", "command_finished")
PROMPT_EVENT_KINDS = ("prompt_rendered",)
HOOK_EVENT_KINDS = ("hook_operation",)


def create_agent_event_record(project_id: str, body: CreateAgentEventRequest) -> AgentEvent:
    from .audit_task_runs import SERVER_RESULT_CONTEXT_FIELDS

    context = body.payload.get("runtime_context")
    if body.kind == "runtime_context_updated" and isinstance(context, dict) and SERVER_RESULT_CONTEXT_FIELDS.intersection(context):
        raise HTTPException(422, "assessment_result_metadata_is_server_owned")
    with get_conn() as conn:
        event_id = insert_agent_event(conn, project_id, body.task_run_id, body.kind, body.payload)
        row = conn.execute(
            "SELECT * FROM agent_events WHERE id = ? AND project_id = ?",
            (event_id, project_id),
        ).fetchone()
        return agent_event_to_model(row)


def list_task_run_event_records(project_id: str, task_run_id: str) -> list[AgentEvent]:
    with get_conn() as conn:
        validate_task_run_exists(conn, project_id, task_run_id)
        rows = conn.execute(
            """
            SELECT * FROM agent_events
            WHERE project_id = ? AND task_run_id = ?
            ORDER BY ts, id
            """,
            (project_id, task_run_id),
        ).fetchall()
        return [agent_event_to_model(row) for row in rows]


def list_task_run_result_event_records(
    project_id: str,
    task_run_id: list[str] | None = None,
) -> list[TaskRunResultEvents]:
    return _list_task_run_grouped_events(
        project_id,
        task_run_id,
        event_kinds=RESULT_AUDIT_EVENT_KINDS,
        model_fn=result_event_to_model,
        response_cls=TaskRunResultEvents,
    )


def list_task_run_command_event_records(
    project_id: str,
    task_run_id: list[str] | None = None,
) -> list[TaskRunCommandEvents]:
    return _list_task_run_grouped_events(
        project_id,
        task_run_id,
        event_kinds=COMMAND_EVENT_KINDS,
        model_fn=command_event_to_model,
        response_cls=TaskRunCommandEvents,
    )


def list_task_run_prompt_event_records(
    project_id: str,
    task_run_id: list[str] | None = None,
) -> list[TaskRunPromptEvents]:
    return _list_task_run_grouped_events(
        project_id,
        task_run_id,
        event_kinds=PROMPT_EVENT_KINDS,
        model_fn=prompt_event_to_model,
        response_cls=TaskRunPromptEvents,
    )


def list_task_run_hook_event_records(
    project_id: str,
    task_run_id: list[str] | None = None,
) -> list[TaskRunHookEvents]:
    return _list_task_run_grouped_events(
        project_id,
        task_run_id,
        event_kinds=HOOK_EVENT_KINDS,
        model_fn=hook_event_to_model,
        response_cls=TaskRunHookEvents,
    )


def insert_agent_event(
    conn,
    project_id: str,
    task_run_id: str,
    kind: str,
    payload: dict,
    *,
    task_row=None,
) -> str:
    if task_row is None:
        task_row = validate_task_run_exists(conn, project_id, task_run_id)
    # runtime_context 在写入事件时补齐，审计导出能解释同一 run 下每个事件发生时的运行环境。
    event_payload = dict(payload)
    event_payload["runtime_context"] = task_run_runtime_context(task_row)
    event_id = next_agent_event_id(conn, project_id)
    now = utcnow()
    conn.execute(
        """
        INSERT INTO agent_events (id, task_run_id, project_id, ts, kind, payload_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            task_run_id,
            project_id,
            now,
            kind,
            dump_json(event_payload),
        ),
    )
    return event_id


def _list_task_run_grouped_events(
    project_id: str,
    task_run_id: list[str] | None,
    *,
    event_kinds: tuple[str, ...],
    model_fn,
    response_cls,
):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        task_run_ids = _validated_task_run_ids(conn, project_id, task_run_id)
        if not task_run_ids:
            return []

        # 动态 IN 参数只来自已验证 task_run_id/kind，仍使用占位符避免拼接值进入 SQL。
        task_placeholders = ", ".join("?" for _ in task_run_ids)
        kind_placeholders = ", ".join("?" for _ in event_kinds)
        rows = conn.execute(
            f"""
            SELECT * FROM agent_events
            WHERE project_id = ?
              AND task_run_id IN ({task_placeholders})
              AND kind IN ({kind_placeholders})
            ORDER BY ts, id
            """,
            (project_id, *task_run_ids, *event_kinds),
        ).fetchall()
        events_by_run = {task_run_id: [] for task_run_id in task_run_ids}
        for row in rows:
            events_by_run[row["task_run_id"]].append(model_fn(row))
        return [
            response_cls(task_run_id=task_run_id, events=events_by_run[task_run_id])
            for task_run_id in task_run_ids
        ]


def _validated_task_run_ids(conn, project_id: str, task_run_id: list[str] | None) -> list[str]:
    task_run_ids: list[str] = []
    seen: set[str] = set()
    # 按请求顺序去重并校验，响应分组顺序与调用方传入顺序保持一致。
    for value in task_run_id or []:
        candidate = value.strip()
        if not candidate or candidate in seen:
            continue
        validate_task_run_exists(conn, project_id, candidate)
        task_run_ids.append(candidate)
        seen.add(candidate)
    return task_run_ids
