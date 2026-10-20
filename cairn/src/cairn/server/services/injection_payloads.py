"""AI Agent 注入 Payload 的持久化、查询与幂等合并。"""

from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL
from cairn.server.models import (
    BatchInjectionPayloadOperationsRequest,
    BatchInjectionPayloadOperationsResponse,
    InjectionPayloadCategorySummary,
    InjectionPayloadListItem,
    InjectionPayloadListResponse,
    InjectionPayloadOperation,
    InjectionPayloadOperationResult,
    InjectionPayloadRecord,
)

from .ids import get_project_or_404, lock_verification_project_write, next_injection_payload_id
from .time_utils import utcnow

INJECTION_PAYLOAD_MAX_TEXT_BYTES = 1024 * 1024
INJECTION_PAYLOAD_MAX_BATCH_BYTES = 10 * 1024 * 1024
INJECTION_PAYLOAD_MAX_PROJECT_RECORDS = 20_000
INJECTION_PAYLOAD_PREVIEW_CHARS = 240


def _positive_env(name: str, fallback: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return fallback
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return parsed


def _max_text_bytes() -> int:
    return _positive_env(
        "CAIRN_INJECTION_PAYLOAD_MAX_TEXT_BYTES", INJECTION_PAYLOAD_MAX_TEXT_BYTES
    )


def _max_batch_bytes() -> int:
    return _positive_env(
        "CAIRN_INJECTION_PAYLOAD_MAX_BATCH_BYTES", INJECTION_PAYLOAD_MAX_BATCH_BYTES
    )


def _max_project_records() -> int:
    return _positive_env(
        "CAIRN_INJECTION_PAYLOAD_MAX_PROJECT_RECORDS",
        INJECTION_PAYLOAD_MAX_PROJECT_RECORDS,
    )


def _text_bytes(value: str | None) -> int:
    return len((value or "").encode("utf-8"))


def _validate_text_limits(operation: InjectionPayloadOperation) -> None:
    limit = _max_text_bytes()
    for field_name in ("payload_text", "response_text"):
        value = getattr(operation, field_name)
        if value is not None and _text_bytes(value) > limit:
            raise HTTPException(413, f"{field_name} exceeds configured byte limit")


def _validate_batch_size(body: BatchInjectionPayloadOperationsRequest) -> None:
    total = 0
    for operation in body.operations:
        _validate_text_limits(operation)
        for field_name in (
            "capture_id",
            "task_run_id",
            "phase",
            "injection_type",
            "payload_text",
            "original_format",
            "response_status",
            "response_text",
            "response_format",
        ):
            total += _text_bytes(getattr(operation, field_name))
    if total > _max_batch_bytes():
        raise HTTPException(413, "Payload operation batch exceeds configured byte limit")


def injection_payload_from_row(row: Any) -> InjectionPayloadRecord:
    return InjectionPayloadRecord(
        id=row["id"],
        project_id=row["project_id"],
        task_run_id=row["task_run_id"],
        phase=row["phase"],
        capture_id=row["capture_id"],
        injection_type=row["injection_type"],
        payload_text=row["payload_text"],
        original_format=row["original_format"],
        response_status=row["response_status"],
        response_text=row["response_text"],
        response_format=row["response_format"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _get_by_capture_id(conn, project_id: str, capture_id: str):
    return conn.execute(
        "SELECT * FROM injection_payload_records WHERE project_id = ? AND capture_id = ?",
        (project_id, capture_id),
    ).fetchone()


def _require_task_run(conn, project_id: str, task_run_id: str) -> None:
    # 对 TaskRun 做无值变更以获取事务写锁，避免终态转换与延迟 Payload 写入交错。
    conn.execute(
        "UPDATE task_runs SET status = status WHERE id = ? AND project_id = ?",
        (task_run_id, project_id),
    )
    row = conn.execute(
        "SELECT tr.status, p.assessment_protocol FROM task_runs tr "
        "JOIN projects p ON p.id = tr.project_id "
        "WHERE tr.id = ? AND tr.project_id = ?",
        (task_run_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Task run not found")
    if row["assessment_protocol"] != GOAL_DRIVEN_PROTOCOL:
        raise HTTPException(409, "Task run does not support injection Payload capture")
    if row["status"] in {"succeeded", "failed", "cancelled", "timed_out"}:
        raise HTTPException(409, "Terminal task run does not accept injection Payload operations")


def _begin_operation(conn, project_id: str, operation: InjectionPayloadOperation):
    assert operation.phase is not None
    assert operation.injection_type is not None
    assert operation.payload_text is not None
    assert operation.original_format is not None
    existing = _get_by_capture_id(conn, project_id, operation.capture_id)
    if existing is not None:
        immutable_values = (
            ("task_run_id", operation.task_run_id),
            ("phase", operation.phase),
            ("injection_type", operation.injection_type),
            ("payload_text", operation.payload_text),
            ("original_format", operation.original_format),
        )
        if any(existing[field] != value for field, value in immutable_values):
            raise HTTPException(409, "Capture ID already exists with different begin data")
        return existing

    count_row = conn.execute(
        "SELECT COUNT(*) AS value FROM injection_payload_records WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if count_row is not None and int(count_row["value"]) >= _max_project_records():
        raise HTTPException(409, "Project injection Payload record limit reached")

    record_id = next_injection_payload_id(conn, project_id)
    now = utcnow()
    conn.execute(
        """
        INSERT INTO injection_payload_records (
            id, project_id, task_run_id, phase, capture_id, injection_type,
            payload_text, original_format, response_status, response_text,
            response_format, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?)
        ON CONFLICT (project_id, capture_id) DO NOTHING
        """,
        (
            record_id,
            project_id,
            operation.task_run_id,
            operation.phase,
            operation.capture_id,
            operation.injection_type,
            operation.payload_text,
            operation.original_format,
            now,
            now,
        ),
    )
    stored = _get_by_capture_id(conn, project_id, operation.capture_id)
    assert stored is not None
    immutable_values = (
        ("task_run_id", operation.task_run_id),
        ("phase", operation.phase),
        ("injection_type", operation.injection_type),
        ("payload_text", operation.payload_text),
        ("original_format", operation.original_format),
    )
    if any(stored[field] != value for field, value in immutable_values):
        raise HTTPException(409, "Capture ID already exists with different begin data")
    return stored


def _finish_operation(conn, project_id: str, operation: InjectionPayloadOperation):
    assert operation.response_status is not None
    existing = _get_by_capture_id(conn, project_id, operation.capture_id)
    if existing is None:
        raise HTTPException(409, "Capture must begin before it can finish")
    if existing["task_run_id"] != operation.task_run_id:
        raise HTTPException(409, "Capture belongs to a different task run")

    requested = (
        operation.response_status,
        operation.response_text,
        operation.response_format,
    )
    current = (
        existing["response_status"],
        existing["response_text"],
        existing["response_format"],
    )
    if existing["response_status"] != "pending":
        if current != requested:
            raise HTTPException(409, "Capture already has a different terminal response")
        return existing

    cursor = conn.execute(
        """
        UPDATE injection_payload_records
        SET response_status = ?, response_text = ?, response_format = ?, updated_at = ?
        WHERE project_id = ? AND capture_id = ? AND response_status = 'pending'
        """,
        (
            operation.response_status,
            operation.response_text,
            operation.response_format,
            utcnow(),
            project_id,
            operation.capture_id,
        ),
    )
    stored = _get_by_capture_id(conn, project_id, operation.capture_id)
    assert stored is not None
    if not cursor.rowcount:
        stored_response = (
            stored["response_status"],
            stored["response_text"],
            stored["response_format"],
        )
        if stored_response != requested:
            raise HTTPException(409, "Capture already has a different terminal response")
    return stored


def apply_injection_payload_operations(
    conn,
    project_id: str,
    body: BatchInjectionPayloadOperationsRequest,
) -> BatchInjectionPayloadOperationsResponse:
    lock_verification_project_write(conn, project_id)
    get_project_or_404(conn, project_id)
    _validate_batch_size(body)
    results: list[InjectionPayloadOperationResult] = []
    for operation in body.operations:
        _require_task_run(conn, project_id, operation.task_run_id)
        if operation.operation == "begin":
            row = _begin_operation(conn, project_id, operation)
        else:
            row = _finish_operation(conn, project_id, operation)
        assert row is not None
        results.append(
            InjectionPayloadOperationResult(
                capture_id=row["capture_id"],
                record_id=row["id"],
                response_status=row["response_status"],
            )
        )
    return BatchInjectionPayloadOperationsResponse(results=results)


def get_injection_payload_record(conn, project_id: str, record_id: str) -> InjectionPayloadRecord:
    row = conn.execute(
        "SELECT * FROM injection_payload_records WHERE project_id = ? AND id = ?",
        (project_id, record_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Injection Payload record not found")
    return injection_payload_from_row(row)


def list_injection_payload_records(
    conn,
    project_id: str,
    *,
    injection_type: str | None = None,
    response_status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> InjectionPayloadListResponse:
    get_project_or_404(conn, project_id)
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if injection_type:
        clauses.append("injection_type = ?")
        params.append(injection_type)
    if response_status:
        clauses.append("response_status = ?")
        params.append(response_status)
    if search:
        clauses.append(
            "(injection_type LIKE ? OR payload_text LIKE ? OR response_text LIKE ? OR original_format LIKE ?)"
        )
        needle = f"%{search}%"
        params.extend([needle, needle, needle, needle])
    where = " AND ".join(clauses)
    count_row = conn.execute(
        f"SELECT COUNT(*) AS value FROM injection_payload_records WHERE {where}",
        tuple(params),
    ).fetchone()
    rows = conn.execute(
        f"""
        SELECT * FROM injection_payload_records
        WHERE {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    items = [
        InjectionPayloadListItem(
            id=row["id"],
            project_id=row["project_id"],
            task_run_id=row["task_run_id"],
            phase=row["phase"],
            capture_id=row["capture_id"],
            injection_type=row["injection_type"],
            payload_preview=row["payload_text"][:INJECTION_PAYLOAD_PREVIEW_CHARS],
            original_format=row["original_format"],
            response_status=row["response_status"],
            response_preview=(
                row["response_text"][:INJECTION_PAYLOAD_PREVIEW_CHARS]
                if row["response_text"] is not None
                else None
            ),
            response_format=row["response_format"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]
    return InjectionPayloadListResponse(
        items=items,
        total=int(count_row["value"]) if count_row is not None else 0,
        limit=limit,
        offset=offset,
    )


def list_injection_payload_categories(
    conn, project_id: str
) -> list[InjectionPayloadCategorySummary]:
    get_project_or_404(conn, project_id)
    rows = conn.execute(
        """
        SELECT injection_type, COUNT(*) AS value
        FROM injection_payload_records
        WHERE project_id = ?
        GROUP BY injection_type
        ORDER BY value DESC, injection_type ASC
        """,
        (project_id,),
    ).fetchall()
    return [
        InjectionPayloadCategorySummary(
            injection_type=row["injection_type"], count=int(row["value"])
        )
        for row in rows
    ]


def interrupt_pending_injection_payloads(conn, project_id: str, task_run_id: str) -> int:
    now = utcnow()
    cursor = conn.execute(
        """
        UPDATE injection_payload_records
        SET response_status = 'interrupted', updated_at = ?
        WHERE project_id = ? AND task_run_id = ? AND response_status = 'pending'
        """,
        (now, project_id, task_run_id),
    )
    return int(cursor.rowcount or 0)
