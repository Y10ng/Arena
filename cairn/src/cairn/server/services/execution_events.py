"""执行事件账本的能力开关、校验、幂等追加与安全查询。"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from cairn.server.auth import RequestPrincipal
from cairn.server.db import get_conn
from cairn.server.schemas.execution_events import (
    EXECUTION_EVENT_CATEGORIES,
    EXECUTION_EVENT_KIND,
    EXECUTION_EVENT_MAX_DETAILS_BYTES,
    EXECUTION_EVENT_MAX_DETAILS_DEPTH,
    EXECUTION_EVENT_MAX_EVENTS,
    EXECUTION_EVENT_MAX_EVENT_BYTES,
    EXECUTION_EVENT_MAX_SPOOL_BYTES,
    EXECUTION_EVENT_MAX_SUMMARY_CHARS,
    EXECUTION_EVENT_PROTOCOL,
    ExecutionEventCapabilities,
    ExecutionEventEnvelope,
    ExecutionEventGenerationDisposition,
    ExecutionEventMetadata,
    ExecutionEventReceipt,
    ExecutionEventReceiptDisposition,
)

from .audit_task_runs import authenticated_execution_owner
from .time_utils import utcnow

EXECUTION_EVENTS_ENABLED_ENV = "CAIRN_EXECUTION_EVENTS_ENABLED"
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def execution_events_enabled() -> bool:
    return os.environ.get(EXECUTION_EVENTS_ENABLED_ENV, "").strip().casefold() in _TRUE_ENV_VALUES


def execution_event_capabilities() -> ExecutionEventCapabilities:
    return ExecutionEventCapabilities(
        enabled=execution_events_enabled(),
        protocol=EXECUTION_EVENT_PROTOCOL,
        kinds=[EXECUTION_EVENT_KIND],
        categories=list(EXECUTION_EVENT_CATEGORIES),
        max_events=EXECUTION_EVENT_MAX_EVENTS,
        max_event_bytes=EXECUTION_EVENT_MAX_EVENT_BYTES,
        max_spool_bytes=EXECUTION_EVENT_MAX_SPOOL_BYTES,
        max_summary_chars=EXECUTION_EVENT_MAX_SUMMARY_CHARS,
        max_details_bytes=EXECUTION_EVENT_MAX_DETAILS_BYTES,
        max_details_depth=EXECUTION_EVENT_MAX_DETAILS_DEPTH,
    )


def canonical_execution_event_payload(body: ExecutionEventEnvelope) -> str:
    return json.dumps(
        body.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _event_metadata(row: Any) -> ExecutionEventMetadata:
    return ExecutionEventMetadata(
        event_id=row["id"],
        task_run_id=row["task_run_id"],
        protocol=row["protocol"],
        kind=row["event_kind"],
        category=row["category"],
        phase=row["phase"],
        producer_recorded_at=row["producer_recorded_at"],
        received_at=row["received_at"],
        execution_generation=row["execution_generation"],
        generation_disposition=row["generation_disposition"],
    )


def _event_receipt(
    row: Any,
    *,
    disposition: ExecutionEventReceiptDisposition,
) -> ExecutionEventReceipt:
    return ExecutionEventReceipt(
        event_id=row["id"],
        producer_event_key=row["producer_event_key"],
        payload_digest_sha256=row["payload_digest_sha256"],
        disposition=disposition,
        generation_disposition=row["generation_disposition"],
        received_at=row["received_at"],
    )


def _load_runtime_context(row: Any) -> dict[str, Any]:
    try:
        loaded = json.loads(row["runtime_context_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        loaded = {}
    return loaded if isinstance(loaded, dict) else {}


def _require_task_run_owner_and_phase(
    row: Any,
    *,
    principal: RequestPrincipal,
    requested_phase: str,
) -> str:
    context = _load_runtime_context(row)
    owner = context.get("dispatcher_id")
    if not isinstance(owner, str) or not owner:
        raise HTTPException(403, "execution_owner_mismatch")
    authenticated_execution_owner(principal, owner)

    # delivery 快照是在模型调用前冻结的权威 request phase，不能用 task_type 推断。
    delivery = context.get("execution_delivery")
    phase = delivery.get("phase") if isinstance(delivery, dict) else None
    if not isinstance(phase, str) or not phase:
        raise HTTPException(409, "execution_event_phase_unavailable")
    if requested_phase != phase:
        raise HTTPException(409, "execution_event_phase_mismatch")
    return owner


def _generation_disposition(
    run_generation: int,
    project_generation: int,
) -> ExecutionEventGenerationDisposition:
    if run_generation < 0:
        raise HTTPException(409, "execution_event_generation_invalid")
    if run_generation > project_generation:
        raise HTTPException(409, "execution_event_future_generation")
    if run_generation == project_generation:
        return "current_at_receipt"
    return "historical_at_receipt"


def append_execution_event(
    project_id: str,
    task_run_id: str,
    body: ExecutionEventEnvelope,
    *,
    principal: RequestPrincipal,
) -> ExecutionEventReceipt:
    if not execution_events_enabled():
        raise HTTPException(409, "execution_events_disabled")

    payload_json = canonical_execution_event_payload(body)
    payload_digest_sha256 = sha256(payload_json.encode("utf-8")).hexdigest()
    with get_conn() as conn:
        # 同一项目的收据判定串行化，避免并发唯一键竞争被误判为成功或 500。
        locked = conn.execute(
            "UPDATE projects SET id = id WHERE id = ?",
            (project_id,),
        )
        if locked.rowcount != 1:
            raise HTTPException(404, "Project not found")
        project = conn.execute(
            "SELECT execution_generation FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        run = conn.execute(
            "SELECT * FROM task_runs WHERE project_id = ? AND id = ?",
            (project_id, task_run_id),
        ).fetchone()
        if run is None:
            raise HTTPException(404, "Task run not found")

        verified_dispatcher_id = _require_task_run_owner_and_phase(
            run,
            principal=principal,
            requested_phase=body.phase,
        )
        run_generation = int(run["execution_generation"])
        project_generation = int(project["execution_generation"])
        generation_disposition = _generation_disposition(
            run_generation,
            project_generation,
        )

        existing = conn.execute(
            """
            SELECT * FROM execution_events
            WHERE project_id = ? AND task_run_id = ? AND producer_event_key = ?
            """,
            (project_id, task_run_id, body.producer_event_key),
        ).fetchone()
        if existing is not None:
            if existing["payload_digest_sha256"] != payload_digest_sha256:
                raise HTTPException(409, "execution_event_receipt_conflict")
            return _event_receipt(existing, disposition="replayed")

        # Server 也约束每个 TaskRun 的唯一事件数，不能只信任 runner/Dispatcher 的 spool 上限。
        event_count = conn.execute(
            """
            SELECT COUNT(*) AS count FROM execution_events
            WHERE project_id = ? AND task_run_id = ?
            """,
            (project_id, task_run_id),
        ).fetchone()["count"]
        if int(event_count) >= EXECUTION_EVENT_MAX_EVENTS:
            raise HTTPException(409, "execution_event_limit_reached")

        event_id = f"xev_{uuid4().hex}"
        received_at = utcnow()
        cursor = conn.execute(
            """
            INSERT INTO execution_events (
                id, project_id, task_run_id, protocol, producer_event_key,
                event_kind, phase, category, producer_recorded_at, received_at,
                verified_dispatcher_id, execution_generation,
                generation_disposition, payload_digest_sha256, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (project_id, task_run_id, producer_event_key) DO NOTHING
            """,
            (
                event_id,
                project_id,
                task_run_id,
                body.protocol,
                body.producer_event_key,
                body.kind,
                body.phase,
                body.checkpoint.category,
                body.recorded_at,
                received_at,
                verified_dispatcher_id,
                run_generation,
                generation_disposition,
                payload_digest_sha256,
                payload_json,
            ),
        )
        stored = conn.execute(
            """
            SELECT * FROM execution_events
            WHERE project_id = ? AND task_run_id = ? AND producer_event_key = ?
            """,
            (project_id, task_run_id, body.producer_event_key),
        ).fetchone()
        if stored is None:
            raise RuntimeError("execution event receipt was not persisted")
        if stored["payload_digest_sha256"] != payload_digest_sha256:
            raise HTTPException(409, "execution_event_receipt_conflict")
        disposition = "received" if cursor.rowcount == 1 else "replayed"
        return _event_receipt(stored, disposition=disposition)


def list_execution_events(
    project_id: str,
    task_run_id: str,
    *,
    limit: int,
    offset: int,
) -> list[ExecutionEventMetadata]:
    with get_conn() as conn:
        run = conn.execute(
            "SELECT 1 FROM task_runs WHERE project_id = ? AND id = ?",
            (project_id, task_run_id),
        ).fetchone()
        if run is None:
            raise HTTPException(404, "Task run not found")
        rows = conn.execute(
            """
            SELECT id, task_run_id, protocol, event_kind, category, phase,
                   producer_recorded_at, received_at, execution_generation,
                   generation_disposition
            FROM execution_events
            WHERE project_id = ? AND task_run_id = ?
            ORDER BY received_at ASC, id ASC
            LIMIT ? OFFSET ?
            """,
            (project_id, task_run_id, limit, offset),
        ).fetchall()
    return [_event_metadata(row) for row in rows]
