"""default-copy 评估报告数据包与 durable finalization。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
import sqlite3
import stat
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL
from cairn.server.artifact_paths import UnsafeArtifactPath, resolve_artifact_path
from cairn.server.db import artifact_root, get_conn
from cairn.server.models import (
    AssessmentReportClaimView,
    AssessmentReportFinalizationView,
    AssessmentReportFinishPayload,
    AssessmentReportFinishReceiptView,
    AssessmentReportFinishReplayRequest,
    AssessmentReportFinishRequest,
    AssessmentReportPacket,
    AssessmentReportTaskRunResult,
    CreateTaskRunRequest,
)

from .assessment_policy import policy_from_project_row, require_execution_contract, row_value
from .assessment_convergence import assessment_execution_view, assessment_has_running_work, stop_assessment
from .assessment_quality_snapshot import build_assessment_quality_snapshot
from .ids import get_project_or_404
from .report_policy import (
    freeze_report_execution_plan,
    parse_report_execution_plan,
    report_policy_from_project_row,
)
from .report_templates import require_report_template_profile
from .time_utils import timestamp_delta_seconds, utcnow
from .vulnerability_chains import build_vulnerability_chain_candidates

_REPORT_VERSION = 1
_REPORT_FINISH_PROTOCOL = "cairn-assessment-report-finish-v1"
_REPORT_ARTIFACT_SUFFIX = "vulnerability-report.md"
_REPORT_SNAPSHOT_SUFFIX = "report-input-snapshot.json"
_MAX_RETRY_SECONDS = 3600


def _json_value(value: object, fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return fallback
    return parsed


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _rows(
    conn: sqlite3.Connection,
    query: str,
    params: tuple[object, ...],
    *,
    json_columns: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for row in conn.execute(query, params).fetchall():
        item = _row_dict(row)
        for column in json_columns:
            item[column.removesuffix("_json")] = _json_value(
                item.pop(column, None),
                [] if column == "evidence_json" else {},
            )
        values.append(item)
    return values


def _finalization_view(row: sqlite3.Row) -> AssessmentReportFinalizationView:
    values = _row_dict(row)
    # digest 是内部 fencing material，不进入通用读取模型。
    values.pop("claim_sha256", None)
    return AssessmentReportFinalizationView(**values)


def _report_task_run(conn: sqlite3.Connection, finalization: sqlite3.Row) -> sqlite3.Row | None:
    rows = conn.execute(
        "SELECT * FROM task_runs WHERE project_id = ? AND task_type = 'report'",
        (finalization["project_id"],),
    ).fetchall()
    for row in rows:
        context = _json_value(row["runtime_context_json"], {})
        claim = context.get("report_claim") if isinstance(context, dict) else None
        if (
            isinstance(claim, dict)
            and claim.get("generation_token") == finalization["generation_token"]
            and claim.get("attempt_count") == finalization["attempt_count"]
        ):
            return row
    return None


def _require_report_execution_generation(project, task_run) -> None:
    if task_run is None:
        raise HTTPException(409, "assessment_report_claim_mismatch")
    if task_run["execution_generation"] != project["execution_generation"] or task_run["pause_requested"]:
        raise HTTPException(409, "execution_generation_mismatch")


def _finish_report_task_run(
    conn: sqlite3.Connection, row: sqlite3.Row, *, status: str, now: str, error: str = "",
) -> None:
    # 报告收尾不推进 assessment phase，且不能改写已经终结的 TaskRun。
    if row["status"] not in {"queued", "running"}:
        return
    from .runtime_context import standard_task_run_error_type

    context = _json_value(row["runtime_context_json"], {})
    error_type = "worker_timeout" if status == "timed_out" else standard_task_run_error_type(error or None)
    context.update(status=status, ended_at=now, error_type=error_type, error_message=error or None)
    conn.execute(
        """UPDATE task_runs SET status = ?, ended_at = ?, error_type = ?, error_message = ?,
            runtime_context_json = ? WHERE project_id = ? AND id = ?""",
        (status, now, error_type, error or None, json.dumps(context), row["project_id"], row["id"]),
    )


def _validate_frozen_execution(row: sqlite3.Row) -> None:
    plan_json = row["report_execution_plan_json"]
    prompt_markdown = row["report_prompt_markdown"]
    if not isinstance(plan_json, str) or not isinstance(prompt_markdown, str):
        raise HTTPException(409, "assessment_report_execution_plan_missing")
    try:
        plan = parse_report_execution_plan(plan_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(409, "assessment_report_execution_plan_invalid") from exc
    if sha256(plan_json.encode("utf-8")).hexdigest() != row["report_execution_plan_sha256"]:
        raise HTTPException(409, "assessment_report_execution_plan_digest_mismatch")
    prompt_digest = sha256(prompt_markdown.encode("utf-8")).hexdigest()
    if prompt_digest != row["report_prompt_digest_sha256"]:
        raise HTTPException(409, "assessment_report_prompt_digest_mismatch")
    if plan.report_prompt_digest_sha256 != prompt_digest:
        raise HTTPException(409, "assessment_report_execution_plan_prompt_mismatch")


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _finish_request_material(
    project_id: str,
    dispatcher_id: str,
    body: AssessmentReportFinishPayload,
    *,
    claim_sha256: str,
) -> dict[str, Any]:
    return {
        "protocol": _REPORT_FINISH_PROTOCOL,
        "project_id": project_id,
        "authenticated_dispatcher_id": dispatcher_id,
        "worker": body.worker,
        "claim_sha256": claim_sha256,
        "generation_token": body.generation_token,
        "task_run_id": body.task_run_id,
        "status": body.status,
        "artifact_id": body.artifact_id,
        "artifact_sha256": body.artifact_sha256,
        "input_snapshot_artifact_id": body.input_snapshot_artifact_id,
        "input_snapshot_sha256": body.input_snapshot_sha256,
        "task_run_result": body.task_run_result.model_dump(mode="json"),
    }


def _artifact_content_bytes(row: sqlite3.Row, *, error_code: str) -> bytes:
    try:
        path = resolve_artifact_path(artifact_root(), dict(row))
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, UnsafeArtifactPath) as exc:
        raise HTTPException(409, error_code) from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size != row["size"]:
            raise HTTPException(409, error_code)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        content = b"".join(chunks)
    except OSError as exc:
        raise HTTPException(409, error_code) from exc
    finally:
        os.close(descriptor)
    if len(content) != row["size"] or sha256(content).hexdigest() != row["sha256"]:
        raise HTTPException(409, error_code)
    return content


def _artifact_row(
    conn: sqlite3.Connection,
    project_id: str,
    artifact_id: str,
    *,
    error_code: str,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM artifacts WHERE project_id = ? AND id = ?",
        (project_id, artifact_id),
    ).fetchone()
    if row is None:
        raise HTTPException(409, error_code)
    return row


def _expected_report_metadata(
    finalization: sqlite3.Row,
    *,
    snapshot_artifact_id: str,
    snapshot_sha256: str,
) -> dict[str, Any]:
    plan = parse_report_execution_plan(finalization["report_execution_plan_json"])
    return {
        "source": "report_task",
        "format": "markdown",
        "report_generation_token": finalization["generation_token"],
        "report_template_key": finalization["template_key"],
        "report_template_version": finalization["template_version"],
        "report_template_digest_sha256": finalization["template_digest_sha256"],
        "report_policy_version": finalization["report_policy_version"],
        "report_execution_plan_sha256": finalization[
            "report_execution_plan_sha256"
        ],
        "report_prompt_digest_sha256": finalization[
            "report_prompt_digest_sha256"
        ],
        "input_snapshot_artifact_id": snapshot_artifact_id,
        "input_snapshot_sha256": snapshot_sha256,
        "completeness_rule_version": plan.completeness_rule_version,
    }


def _validate_report_artifact(
    conn: sqlite3.Connection,
    finalization: sqlite3.Row,
    task_run: sqlite3.Row,
    *,
    artifact_id: str,
    artifact_sha256: str,
    snapshot_artifact_id: str,
    snapshot_sha256: str,
) -> sqlite3.Row:
    error_code = "assessment_report_artifact_invalid"
    artifact = _artifact_row(
        conn,
        finalization["project_id"],
        artifact_id,
        error_code=error_code,
    )
    expected_uri = (
        f"artifact://projects/{finalization['project_id']}/runs/{task_run['id']}/"
        f"{artifact_id}-{_REPORT_ARTIFACT_SUFFIX}"
    )
    metadata = _json_value(artifact["metadata_json"], {})
    expected_metadata = _expected_report_metadata(
        finalization,
        snapshot_artifact_id=snapshot_artifact_id,
        snapshot_sha256=snapshot_sha256,
    )
    if (
        artifact["task_run_id"] != task_run["id"]
        or artifact["kind"] != "report"
        or artifact["category"] != "deliverable"
        or artifact["uri"] != expected_uri
        or artifact["sha256"] != artifact_sha256
        or not isinstance(metadata, dict)
        or set(metadata) != {*expected_metadata, "runtime_context"}
        or not isinstance(metadata.get("runtime_context"), dict)
        or any(
            metadata.get(key) != value
            for key, value in expected_metadata.items()
        )
    ):
        raise HTTPException(409, error_code)
    content = _artifact_content_bytes(artifact, error_code=error_code)
    try:
        markdown = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(409, error_code) from exc
    if not markdown.strip():
        raise HTTPException(409, error_code)
    return artifact


def _snapshot_origin_task_run(
    conn: sqlite3.Connection,
    task_run: sqlite3.Row,
) -> sqlite3.Row:
    """沿 Server 派生的 delivery_retry 链定位冻结快照所属的原始 TaskRun。"""
    error_code = "assessment_report_snapshot_artifact_invalid"
    current = task_run
    seen: set[str] = set()
    for _ in range(16):
        current_id = str(current["id"])
        if current_id in seen:
            raise HTTPException(409, error_code)
        seen.add(current_id)
        context = _json_value(current["runtime_context_json"], {})
        if not isinstance(context, dict):
            raise HTTPException(409, error_code)
        delivery_retry = context.get("delivery_retry")
        if delivery_retry is None:
            return current
        if not isinstance(delivery_retry, dict) or set(delivery_retry) != {
            "source_task_run_id"
        }:
            raise HTTPException(409, error_code)
        source_task_run_id = delivery_retry.get("source_task_run_id")
        # 重试来源由 Server 写入；读取时仍按 canonical identifier 重新验证持久化边界。
        if (
            not isinstance(source_task_run_id, str)
            or not source_task_run_id
            or source_task_run_id != source_task_run_id.strip()
            or "\x00" in source_task_run_id
            or len(source_task_run_id) > 128
        ):
            raise HTTPException(409, error_code)
        source = conn.execute(
            "SELECT * FROM task_runs WHERE project_id = ? AND id = ?",
            (task_run["project_id"], source_task_run_id),
        ).fetchone()
        source_context = (
            _json_value(source["runtime_context_json"], {})
            if source is not None
            else None
        )
        current_claim = context.get("report_claim")
        source_claim = (
            source_context.get("report_claim")
            if isinstance(source_context, dict)
            else None
        )
        if (
            source is None
            or source["task_type"] != "report"
            or source["worker_name"] != task_run["worker_name"]
            or source["execution_generation"] != task_run["execution_generation"]
            or not isinstance(current_claim, dict)
            or not isinstance(source_claim, dict)
            or source_claim.get("generation_token")
            != current_claim.get("generation_token")
        ):
            raise HTTPException(409, error_code)
        current = source
    raise HTTPException(409, error_code)


def _validate_snapshot_artifact(
    conn: sqlite3.Connection,
    finalization: sqlite3.Row,
    task_run: sqlite3.Row,
    *,
    artifact_id: str,
    digest_sha256: str,
) -> sqlite3.Row:
    error_code = "assessment_report_snapshot_artifact_invalid"
    snapshot_task_run = _snapshot_origin_task_run(conn, task_run)
    artifact = _artifact_row(
        conn,
        finalization["project_id"],
        artifact_id,
        error_code=error_code,
    )
    plan = parse_report_execution_plan(finalization["report_execution_plan_json"])
    expected_uri = (
        f"artifact://projects/{finalization['project_id']}/runs/"
        f"{snapshot_task_run['id']}/{artifact_id}-{_REPORT_SNAPSHOT_SUFFIX}"
    )
    metadata = _json_value(artifact["metadata_json"], {})
    expected_metadata = {
        "source": "report_task",
        "format": "json",
        "report_generation_token": finalization["generation_token"],
        "report_template_key": finalization["template_key"],
        "report_template_version": finalization["template_version"],
        "report_template_digest_sha256": finalization["template_digest_sha256"],
        "report_execution_plan_sha256": finalization[
            "report_execution_plan_sha256"
        ],
        "report_prompt_digest_sha256": finalization[
            "report_prompt_digest_sha256"
        ],
        "report_policy_version": finalization["report_policy_version"],
        "packet_version": plan.packet_version,
        "snapshot_sha256": digest_sha256,
    }
    if (
        artifact["task_run_id"] != snapshot_task_run["id"]
        or artifact["kind"] != "report"
        or artifact["category"] != "deliverable"
        or artifact["uri"] != expected_uri
        or artifact["sha256"] != digest_sha256
        or not isinstance(metadata, dict)
        or set(metadata) != {*expected_metadata, "runtime_context"}
        or not isinstance(metadata.get("runtime_context"), dict)
        or any(metadata.get(key) != value for key, value in expected_metadata.items())
    ):
        raise HTTPException(409, error_code)
    content = _artifact_content_bytes(artifact, error_code=error_code)
    try:
        snapshot = json.loads(content.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(409, error_code) from exc
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("version") != 1
        or not isinstance(snapshot.get("graph_yaml"), str)
        or not snapshot["graph_yaml"].strip()
        or not isinstance(snapshot.get("report_packet"), dict)
        or snapshot["report_packet"].get("version") != plan.packet_version
        or not isinstance(snapshot["report_packet"].get("project"), dict)
        or snapshot["report_packet"]["project"].get("id")
        != finalization["project_id"]
    ):
        raise HTTPException(409, error_code)
    return artifact


def _finish_receipt_view(row: sqlite3.Row) -> AssessmentReportFinishReceiptView:
    try:
        payload = json.loads(row["response_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(500, "assessment_report_finish_receipt_invalid") from exc
    return AssessmentReportFinishReceiptView.model_validate(payload)


def ensure_assessment_report_finalization(
    conn: sqlite3.Connection,
    project_id: str,
) -> AssessmentReportFinalizationView:
    """为一次完成后的评估创建或重置 durable 报告交付状态。"""
    project = get_project_or_404(conn, project_id)
    if not report_policy_from_project_row(project).supported:
        raise HTTPException(409, "assessment_report_not_supported")
    now = utcnow()
    generation_token = uuid4().hex
    conn.execute(
        """
        INSERT INTO assessment_report_finalizations (
            project_id, version, status, attempt_count, last_error,
            generation_started_at, generation_token, created_at, updated_at
        ) VALUES (?, ?, 'pending', 0, '', ?, ?, ?, ?)
        ON CONFLICT(project_id) DO UPDATE SET
            version = excluded.version,
            status = 'pending',
            attempt_count = 0,
            claimed_by = NULL,
            claim_token = NULL,
            claim_sha256 = NULL,
            lease_expires_at = NULL,
            next_attempt_at = NULL,
            artifact_id = NULL,
            last_error = '',
            generation_started_at = excluded.generation_started_at,
            generation_token = excluded.generation_token,
            updated_at = excluded.updated_at,
            completed_at = NULL
        """,
        (project_id, _REPORT_VERSION, now, generation_token, now, now),
    )
    row = conn.execute(
        "SELECT * FROM assessment_report_finalizations WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return _finalization_view(row)


def _lock_report_project(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    """串行化同一项目的报告请求与生命周期变更。"""
    conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
    return get_project_or_404(conn, project_id)


def _stopped_project_has_active_assessment_runs(
    conn: sqlite3.Connection,
    project_id: str,
) -> bool:
    return (
        conn.execute(
            """
            SELECT 1 FROM task_runs
            WHERE project_id = ?
              AND task_type != 'report'
              AND status IN ('queued', 'running')
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        is not None
    )


def _require_report_request_eligibility(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
) -> None:
    from .project_records import project_pause_unconfirmed

    status = str(project["status"])
    if project_pause_unconfirmed(conn, str(project["id"])):
        raise HTTPException(409, "project_pause_draining")
    if assessment_has_running_work(conn, str(project["id"])):
        raise HTTPException(409, "assessment_report_tasks_not_quiescent")
    if status == "completed":
        return
    if status == "paused":
        raise HTTPException(409, "assessment_report_project_paused")
    if status == "active":
        raise HTTPException(409, "assessment_report_project_active")
    if status != "stopped":
        raise HTTPException(409, "assessment_report_project_status_invalid")
    project_id = str(project["id"])
    if _stopped_project_has_active_assessment_runs(conn, project_id):
        raise HTTPException(409, "assessment_report_stopped_tasks_not_quiescent")


def _require_report_claim_eligibility(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
) -> None:
    require_execution_contract(project)
    from .project_records import project_pause_unconfirmed

    status = str(project["status"])
    if project_pause_unconfirmed(conn, str(project["id"])):
        raise HTTPException(409, "project_pause_draining")
    if assessment_has_running_work(conn, str(project["id"])):
        raise HTTPException(409, "assessment_report_tasks_not_quiescent")
    if status == "completed":
        return
    if status == "paused":
        raise HTTPException(409, "assessment_report_project_paused")
    if status == "active":
        raise HTTPException(409, "assessment_report_project_active")
    if status != "stopped":
        raise HTTPException(409, "assessment_report_project_status_invalid")
    if _stopped_project_has_active_assessment_runs(conn, str(project["id"])):
        raise HTTPException(409, "assessment_report_stopped_tasks_not_quiescent")


def request_assessment_report_generation(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    template_key: str,
    template_version: int,
    requested_by: str,
) -> AssessmentReportFinalizationView:
    """冻结模板并创建一次显式报告生成请求，历史报告 Artifact 保持不变。"""
    project = _lock_report_project(conn, project_id)
    require_execution_contract(project)
    report_policy = report_policy_from_project_row(project)
    if not report_policy.supported:
        raise HTTPException(409, "assessment_report_not_supported")
    _require_report_request_eligibility(conn, project)

    current = conn.execute(
        "SELECT * FROM assessment_report_finalizations WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    now = utcnow()
    if current is not None and str(current["status"]) == "running":
        lease_expires_at = current["lease_expires_at"]
        if lease_expires_at is None or str(lease_expires_at) > now:
            raise HTTPException(409, "assessment_report_generation_running")
    if current is not None and str(current["status"]) == "pending":
        if (
            current["template_key"] == template_key
            and int(current["template_version"] or 0) == template_version
            and current["requested_at"] is not None
        ):
            # 已接受请求完全依赖冻结快照；幂等重试不重新读取实时模板或 Prompt。
            return _finalization_view(current).model_copy(update={"claim_token": None})
        raise HTTPException(409, "assessment_report_generation_pending")

    template = require_report_template_profile(
        conn,
        template_key,
        template_version,
        report_policy.template_profile,
    )
    try:
        execution_plan, report_prompt_markdown = freeze_report_execution_plan(report_policy)
    except (FileNotFoundError, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(409, "assessment_report_configuration_invalid") from exc
    execution_plan_json = execution_plan.canonical_json()
    execution_plan_digest = execution_plan.digest_sha256()

    generation_token = uuid4().hex
    conn.execute(
        """
        INSERT INTO assessment_report_finalizations (
            project_id, version, status, attempt_count, last_error,
            generation_started_at, generation_token,
            requested_at, requested_by, template_key, template_version,
            template_digest_sha256, template_markdown,
            report_policy_version, report_execution_plan_json,
            report_execution_plan_sha256, report_prompt_digest_sha256,
            report_prompt_markdown, created_at, updated_at
        ) VALUES (?, ?, 'pending', 0, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id) DO UPDATE SET
            version = excluded.version,
            status = 'pending',
            attempt_count = 0,
            claimed_by = NULL,
            claim_token = NULL,
            claim_sha256 = NULL,
            lease_expires_at = NULL,
            next_attempt_at = NULL,
            artifact_id = NULL,
            last_error = '',
            generation_started_at = excluded.generation_started_at,
            generation_token = excluded.generation_token,
            requested_at = excluded.requested_at,
            requested_by = excluded.requested_by,
            template_key = excluded.template_key,
            template_version = excluded.template_version,
            template_digest_sha256 = excluded.template_digest_sha256,
            template_markdown = excluded.template_markdown,
            report_policy_version = excluded.report_policy_version,
            report_execution_plan_json = excluded.report_execution_plan_json,
            report_execution_plan_sha256 = excluded.report_execution_plan_sha256,
            report_prompt_digest_sha256 = excluded.report_prompt_digest_sha256,
            report_prompt_markdown = excluded.report_prompt_markdown,
            input_snapshot_artifact_id = NULL,
            input_snapshot_sha256 = NULL,
            updated_at = excluded.updated_at,
            completed_at = NULL
        """,
        (
            project_id,
            _REPORT_VERSION,
            now,
            generation_token,
            now,
            requested_by,
            template.template_key,
            template.version,
            template.digest_sha256,
            template.markdown_content,
            report_policy.version,
            execution_plan_json,
            execution_plan_digest,
            execution_plan.report_prompt_digest_sha256,
            report_prompt_markdown,
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM assessment_report_finalizations WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return _finalization_view(row).model_copy(update={"claim_token": None})


def get_assessment_report_finalization(
    conn: sqlite3.Connection,
    project_id: str,
) -> AssessmentReportFinalizationView:
    from .assessment_attempts import delivery_retry_routes

    project = get_project_or_404(conn, project_id)
    row = conn.execute(
        "SELECT * FROM assessment_report_finalizations WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "assessment_report_finalization_not_found")
    routes = delivery_retry_routes(conn, project)
    route = next((value for value in routes.values() if value["task_type"] == "report"), None)
    # 常规状态读取不暴露当前 lease 的 fencing token，路由只消费持久资格的派生值。
    return _finalization_view(row).model_copy(update={"claim_token": None, "delivery_retry_route": route})


def _timestamp_after(now: str, seconds: int) -> str:
    current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current.astimezone(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _report_execution_physically_resolved(run: sqlite3.Row) -> bool:
    """仅接受与 TaskRun 代次一致的终止及清理正向证明。"""
    context = _json_value(run["runtime_context_json"], {})
    lifecycle = context.get("execution_lifecycle") if isinstance(context, dict) else None
    return bool(
        isinstance(lifecycle, dict)
        and type(lifecycle.get("generation")) is int
        and lifecycle["generation"] == run["execution_generation"]
        and lifecycle.get("termination_confirmed") is True
        and lifecycle.get("cleanup_confirmed") is True
    )


def _recover_expired_report_artifact(
    conn: sqlite3.Connection,
    current: sqlite3.Row,
    old_run: sqlite3.Row,
) -> AssessmentReportFinishReceiptView | None:
    snapshot_artifact_id = current["input_snapshot_artifact_id"]
    snapshot_sha256 = current["input_snapshot_sha256"]
    if not snapshot_artifact_id or not snapshot_sha256:
        return None
    context = _json_value(old_run["runtime_context_json"], {})
    claim = context.get("report_claim") if isinstance(context, dict) else None
    dispatcher_id = context.get("dispatcher_id") if isinstance(context, dict) else None
    if (
        not isinstance(claim, dict)
        or not isinstance(claim.get("claim_sha256"), str)
        or not isinstance(dispatcher_id, str)
        or not dispatcher_id
    ):
        return None
    try:
        _validate_snapshot_artifact(
            conn,
            current,
            old_run,
            artifact_id=str(snapshot_artifact_id),
            digest_sha256=str(snapshot_sha256),
        )
    except HTTPException:
        return None
    candidates = conn.execute(
        """
        SELECT * FROM artifacts
        WHERE project_id = ? AND task_run_id = ? AND kind = 'report'
          AND category = 'deliverable'
        ORDER BY created_at DESC, id DESC
        """,
        (current["project_id"], old_run["id"]),
    ).fetchall()
    artifact = None
    for candidate in candidates:
        try:
            _validate_report_artifact(
                conn,
                current,
                old_run,
                artifact_id=str(candidate["id"]),
                artifact_sha256=str(candidate["sha256"]),
                snapshot_artifact_id=str(snapshot_artifact_id),
                snapshot_sha256=str(snapshot_sha256),
            )
        except HTTPException:
            continue
        artifact = candidate
        break
    if artifact is None:
        return None
    body = AssessmentReportFinishRequest(
        protocol=_REPORT_FINISH_PROTOCOL,
        worker=str(old_run["worker_name"]),
        claim_token="recovered-expired-claim",
        generation_token=str(current["generation_token"]),
        task_run_id=str(old_run["id"]),
        status="ready",
        artifact_id=str(artifact["id"]),
        artifact_sha256=str(artifact["sha256"]),
        input_snapshot_artifact_id=str(snapshot_artifact_id),
        input_snapshot_sha256=str(snapshot_sha256),
        task_run_result=AssessmentReportTaskRunResult(
            status="succeeded",
            exit_code=0,
        ),
    )
    request_material = _finish_request_material(
        str(current["project_id"]),
        dispatcher_id,
        body,
        claim_sha256=str(claim["claim_sha256"]),
    )
    request_json = _canonical_json(request_material)
    return _persist_report_finish(
        conn,
        current,
        old_run,
        body,
        request_material=request_material,
        request_sha256=sha256(request_json.encode("utf-8")).hexdigest(),
        now=utcnow(),
        disposition="recovered",
    )


def claim_assessment_report_finalization(
    project_id: str,
    worker: str,
    *,
    lease_seconds: int = 300,
    principal,
) -> AssessmentReportClaimView | AssessmentReportFinishReceiptView:
    """原子 claim 已显式请求的报告；过期 running lease 可由重启后的 Dispatcher 接管。"""
    from .audit_task_runs import authenticated_execution_owner

    authenticated_execution_owner(principal)
    now = utcnow()
    lease_expires_at = _timestamp_after(now, lease_seconds)
    claim_token = uuid4().hex
    claim_sha256 = sha256(claim_token.encode("utf-8")).hexdigest()
    with get_conn() as conn:
        project = _lock_report_project(conn, project_id)
        _require_report_claim_eligibility(conn, project)
        current = conn.execute(
            "SELECT * FROM assessment_report_finalizations WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if current is None:
            raise HTTPException(404, "assessment_report_finalization_not_found")
        from .assessment_attempts import (
            DELIVERY_RETRY_ERROR_TYPES,
            execution_retry_eligibility,
        )

        expired = (
            current["status"] == "running"
            and current["lease_expires_at"] is not None
            and current["lease_expires_at"] <= now
        )
        old_run = _report_task_run(conn, current) if expired else None
        if expired and (
            old_run is None or not _report_execution_physically_resolved(old_run)
        ):
            # lease 只证明逻辑超时；物理执行未知时不能终结旧 run 或并行重派模型。
            raise HTTPException(409, "assessment_report_execution_unresolved")
        try:
            # 过期 Artifact 恢复也会读取冻结计划；必须先把损坏归为永久失败，不能冒泡成 500。
            _validate_frozen_execution(current)
        except HTTPException as exc:
            detail = str(exc.detail)
            if expired and old_run is not None:
                _finish_report_task_run(
                    conn,
                    old_run,
                    status="timed_out",
                    now=current["lease_expires_at"],
                    error="report_claim_expired",
                )
            conn.execute(
                """
                UPDATE assessment_report_finalizations
                SET status = 'failed', claimed_by = NULL, claim_token = NULL,
                    claim_sha256 = NULL, lease_expires_at = NULL,
                    next_attempt_at = NULL, last_error = ?, updated_at = ?
                WHERE project_id = ?
                """,
                (detail, now, project_id),
            )
            # 永久冻结配置错误必须先落库；随后抛出的稳定错误不能回滚该状态。
            conn.commit()
            raise
        if expired and old_run is not None:
            recovered = _recover_expired_report_artifact(conn, current, old_run)
            if recovered is not None:
                return recovered
            _finish_report_task_run(
                conn,
                old_run,
                status="timed_out",
                now=current["lease_expires_at"],
                error="report_claim_expired",
            )
        eligibility = execution_retry_eligibility(
            conn,
            project,
            "report",
            report_generation_token=current["generation_token"],
            now=now,
        )
        if not eligibility["allowed"]:
            if expired:
                reason = str(eligibility["reason"])
                # 过期执行已经终结，finalization 也必须离开 running；永久拒绝不能留下假 lease。
                conn.execute(
                    """
                    UPDATE assessment_report_finalizations
                    SET status = 'failed', claimed_by = NULL, claim_token = NULL,
                        claim_sha256 = NULL, lease_expires_at = NULL,
                        next_attempt_at = ?, last_error = ?, updated_at = ?
                    WHERE project_id = ?
                    """,
                    (eligibility.get("retry_at"), reason, now, project_id),
                )
                conn.commit()
            raise HTTPException(409, eligibility["reason"])
        preserved_snapshot_id = None
        preserved_snapshot_sha256 = None
        delivery_source = _report_task_run(conn, current)
        if (
            delivery_source is not None
            and delivery_source["error_type"] in DELIVERY_RETRY_ERROR_TYPES
            and current["input_snapshot_artifact_id"] is not None
            and current["input_snapshot_sha256"] is not None
        ):
            _validate_snapshot_artifact(
                conn,
                current,
                delivery_source,
                artifact_id=str(current["input_snapshot_artifact_id"]),
                digest_sha256=str(current["input_snapshot_sha256"]),
            )
            preserved_snapshot_id = current["input_snapshot_artifact_id"]
            preserved_snapshot_sha256 = current["input_snapshot_sha256"]
        cursor = conn.execute(
            """
            UPDATE assessment_report_finalizations
            SET status = 'running',
                attempt_count = attempt_count + 1,
                claimed_by = ?,
                claim_token = NULL,
                claim_sha256 = ?,
                lease_expires_at = ?,
                next_attempt_at = NULL,
                input_snapshot_artifact_id = ?,
                input_snapshot_sha256 = ?,
                last_error = '',
                updated_at = ?
            WHERE project_id = ?
              AND requested_at IS NOT NULL
              AND template_key IS NOT NULL
              AND template_version IS NOT NULL
              AND template_digest_sha256 IS NOT NULL
              AND template_markdown IS NOT NULL
              AND report_policy_version IS NOT NULL
              AND report_execution_plan_json IS NOT NULL
              AND report_execution_plan_sha256 IS NOT NULL
              AND report_prompt_digest_sha256 IS NOT NULL
              AND report_prompt_markdown IS NOT NULL
              AND status != 'ready'
              AND (status != 'running' OR lease_expires_at IS NULL OR lease_expires_at <= ?)
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            """,
            (
                worker,
                claim_sha256,
                lease_expires_at,
                preserved_snapshot_id,
                preserved_snapshot_sha256,
                now,
                project_id,
                now,
                now,
            ),
        )
        if cursor.rowcount != 1:
            row = conn.execute(
                "SELECT status FROM assessment_report_finalizations WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "assessment_report_finalization_not_found")
            raise HTTPException(409, f"assessment_report_not_claimable:{row['status']}")
        row = conn.execute(
            "SELECT * FROM assessment_report_finalizations WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        from .audit_task_runs import insert_task_run_record

        # 接管过期尝试时同步封口旧 run，避免重启后遗留永久 running 的报告审计。
        for old_run in conn.execute(
            "SELECT * FROM task_runs WHERE project_id = ? AND task_type = 'report' AND status IN ('queued', 'running')",
            (project_id,),
        ).fetchall():
            _finish_report_task_run(conn, old_run, status="timed_out", now=now, error="report_claim_expired")
        plan = parse_report_execution_plan(row["report_execution_plan_json"])
        task_run = insert_task_run_record(
            conn,
            project_id,
            CreateTaskRunRequest(
                task_type="report",
                worker_name=worker,
                worker_type="pi",
                prompt_group=plan.report_prompt_group,
                runtime_context={
                    "report_claim": {
                        "generation_token": row["generation_token"],
                        "attempt_count": row["attempt_count"],
                        "claim_sha256": claim_sha256,
                    },
                    "report_generation": {
                        "generation_token": row["generation_token"],
                        "template_key": row["template_key"],
                        "template_version": row["template_version"],
                        "template_digest_sha256": row["template_digest_sha256"],
                        "report_execution_plan_sha256": row["report_execution_plan_sha256"],
                        "report_prompt_digest_sha256": row["report_prompt_digest_sha256"],
                    },
                },
            ),
            principal=principal,
        )
        response_values = _row_dict(row)
        response_values.pop("claim_sha256", None)
        # 明文 token 只存在于本次响应内，数据库与 TaskRun 均只保存 digest。
        response_values["claim_token"] = claim_token
        return AssessmentReportClaimView(
            **response_values,
            task_run_id=task_run.id,
            task_run_context=task_run.runtime_context,
        )


def bind_assessment_report_input_snapshot(
    project_id: str,
    worker: str,
    *,
    claim_token: str,
    artifact_id: str,
    digest_sha256: str,
    principal,
) -> AssessmentReportFinalizationView:
    """把不可变输入快照绑定到当前 claim，防止旧 Worker 覆盖新代际。"""
    from .audit_task_runs import authenticated_execution_owner

    dispatcher_id = authenticated_execution_owner(principal)
    claim_sha256 = sha256(claim_token.encode("utf-8")).hexdigest()
    now = utcnow()
    with get_conn() as conn:
        project = _lock_report_project(conn, project_id)
        _require_report_claim_eligibility(conn, project)
        finalization = conn.execute(
            """
            SELECT *
            FROM assessment_report_finalizations
            WHERE project_id = ? AND status = 'running'
              AND claimed_by = ? AND claim_sha256 = ?
              AND (lease_expires_at IS NULL OR lease_expires_at > ?)
            """,
            (project_id, worker, claim_sha256, now),
        ).fetchone()
        if finalization is None:
            raise HTTPException(409, "assessment_report_claim_mismatch")
        task_run = _report_task_run(conn, finalization)
        _require_report_execution_generation(project, task_run)
        assert task_run is not None
        task_context = _json_value(task_run["runtime_context_json"], {})
        if (
            task_run["worker_name"] != worker
            or not isinstance(task_context, dict)
            or task_context.get("dispatcher_id") != dispatcher_id
        ):
            raise HTTPException(403, "execution_owner_mismatch")
        existing_identity = (
            finalization["input_snapshot_artifact_id"],
            finalization["input_snapshot_sha256"],
        )
        requested_identity = (artifact_id, digest_sha256)
        if existing_identity != (None, None):
            if existing_identity != requested_identity:
                raise HTTPException(
                    409,
                    "assessment_report_snapshot_receipt_conflict",
                )
            return _finalization_view(finalization).model_copy(
                update={"claim_token": None}
            )
        _validate_snapshot_artifact(
            conn,
            finalization,
            task_run,
            artifact_id=artifact_id,
            digest_sha256=digest_sha256,
        )
        cursor = conn.execute(
            """
            UPDATE assessment_report_finalizations
            SET input_snapshot_artifact_id = ?, input_snapshot_sha256 = ?, updated_at = ?
            WHERE project_id = ? AND status = 'running'
              AND claimed_by = ? AND claim_sha256 = ?
              AND (lease_expires_at IS NULL OR lease_expires_at > ?)
            """,
            (
                artifact_id,
                digest_sha256,
                now,
                project_id,
                worker,
                claim_sha256,
                now,
            ),
        )
        if cursor.rowcount != 1:
            raise HTTPException(409, "assessment_report_claim_mismatch")
        row = conn.execute(
            "SELECT * FROM assessment_report_finalizations WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return _finalization_view(row).model_copy(update={"claim_token": None})


def _terminalize_report_task_run(
    conn: sqlite3.Connection,
    task_run: sqlite3.Row,
    result: AssessmentReportTaskRunResult,
    *,
    now: str,
) -> None:
    from .runtime_context import sanitize_task_run_runtime_context

    existing_error = task_run["error_message"] or ""
    if task_run["status"] in {"succeeded", "failed", "cancelled", "timed_out"}:
        if (
            task_run["status"] != result.status
            or task_run["exit_code"] != result.exit_code
            or task_run["error_type"] != result.error_type
            or existing_error != result.error_message
        ):
            raise HTTPException(409, "assessment_report_task_run_terminal_conflict")
        return
    if task_run["status"] not in {"queued", "running"}:
        raise HTTPException(409, "assessment_report_task_run_state_invalid")
    context = _json_value(task_run["runtime_context_json"], {})
    if not isinstance(context, dict):
        raise HTTPException(409, "assessment_report_task_run_context_invalid")
    started_at = task_run["started_at"] or now
    context.update(
        status=result.status,
        started_at=started_at,
        ended_at=now,
        queue_wait_seconds=timestamp_delta_seconds(
            context.get("queued_at"),
            started_at,
        ),
        exit_code=result.exit_code,
        error_type=result.error_type,
        error_message=result.error_message or None,
    )
    if result.worker_failure is not None:
        context["worker_failure"] = result.worker_failure.model_dump(
            mode="json",
            exclude_none=True,
        )
    else:
        context.pop("worker_failure", None)
    context = sanitize_task_run_runtime_context(context)
    conn.execute(
        """
        UPDATE task_runs
        SET status = ?, started_at = ?, ended_at = ?, exit_code = ?, error_type = ?,
            error_message = ?, runtime_context_json = ?
        WHERE project_id = ? AND id = ?
        """,
        (
            result.status,
            started_at,
            now,
            result.exit_code,
            result.error_type,
            result.error_message or None,
            _canonical_json(context),
            task_run["project_id"],
            task_run["id"],
        ),
    )


def _persist_report_finish(
    conn: sqlite3.Connection,
    current: sqlite3.Row,
    task_run: sqlite3.Row,
    body: AssessmentReportFinishPayload,
    *,
    request_material: dict[str, Any],
    request_sha256: str,
    now: str,
    disposition: str = "accepted",
) -> AssessmentReportFinishReceiptView:
    if body.input_snapshot_artifact_id is not None:
        assert body.input_snapshot_sha256 is not None
        _validate_snapshot_artifact(
            conn,
            current,
            task_run,
            artifact_id=body.input_snapshot_artifact_id,
            digest_sha256=body.input_snapshot_sha256,
        )
    if body.status == "ready":
        assert body.artifact_id is not None
        assert body.artifact_sha256 is not None
        assert body.input_snapshot_artifact_id is not None
        assert body.input_snapshot_sha256 is not None
        _validate_report_artifact(
            conn,
            current,
            task_run,
            artifact_id=body.artifact_id,
            artifact_sha256=body.artifact_sha256,
            snapshot_artifact_id=body.input_snapshot_artifact_id,
            snapshot_sha256=body.input_snapshot_sha256,
        )
    _terminalize_report_task_run(
        conn,
        task_run,
        body.task_run_result,
        now=now,
    )
    if body.status == "ready":
        next_attempt_at = None
        artifact_id = body.artifact_id
        last_error = ""
        completed_at = now
    else:
        retry_seconds = min(
            _MAX_RETRY_SECONDS,
            30 * (2 ** max(0, int(current["attempt_count"] or 1) - 1)),
        )
        next_attempt_at = _timestamp_after(now, retry_seconds)
        artifact_id = None
        last_error = body.task_run_result.error_message
        completed_at = None
    cursor = conn.execute(
        """
        UPDATE assessment_report_finalizations
        SET status = ?, claimed_by = NULL, claim_token = NULL,
            claim_sha256 = NULL, lease_expires_at = NULL,
            next_attempt_at = ?, artifact_id = ?,
            last_error = ?, updated_at = ?, completed_at = ?
        WHERE project_id = ? AND status = 'running'
          AND generation_token = ? AND attempt_count = ?
          AND claimed_by = ? AND claim_sha256 = ?
        """,
        (
            body.status,
            next_attempt_at,
            artifact_id,
            last_error,
            now,
            completed_at,
            current["project_id"],
            body.generation_token,
            current["attempt_count"],
            current["claimed_by"],
            current["claim_sha256"],
        ),
    )
    if cursor.rowcount != 1:
        raise HTTPException(409, "assessment_report_claim_mismatch")
    finalization = conn.execute(
        "SELECT * FROM assessment_report_finalizations WHERE project_id = ?",
        (current["project_id"],),
    ).fetchone()
    response_values = _row_dict(finalization)
    response_values.pop("claim_sha256", None)
    # receipt 描述被接受的 immutable 请求；finalization 可保留此前绑定的快照供后续重试。
    response_values["artifact_id"] = body.artifact_id
    response_values["input_snapshot_artifact_id"] = body.input_snapshot_artifact_id
    response_values["input_snapshot_sha256"] = body.input_snapshot_sha256
    response = AssessmentReportFinishReceiptView(
        **response_values,
        protocol=_REPORT_FINISH_PROTOCOL,
        task_run_id=body.task_run_id,
        request_sha256=request_sha256,
        finish_disposition=disposition,
        accepted_at=now,
    )
    response_json = _canonical_json(response.model_dump(mode="json"))
    conn.execute(
        """
        INSERT INTO assessment_report_finish_receipts (
            project_id, task_run_id, protocol, generation_token,
            request_sha256, request_json, response_json,
            artifact_id, artifact_sha256,
            input_snapshot_artifact_id, input_snapshot_sha256, accepted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            current["project_id"],
            body.task_run_id,
            _REPORT_FINISH_PROTOCOL,
            body.generation_token,
            request_sha256,
            _canonical_json(request_material),
            response_json,
            body.artifact_id,
            body.artifact_sha256,
            body.input_snapshot_artifact_id,
            body.input_snapshot_sha256,
            now,
        ),
    )
    return response


def _finish_assessment_report_finalization(
    project_id: str,
    body: AssessmentReportFinishPayload,
    *,
    dispatcher_id: str,
    claim_sha256: str,
    allow_expired_lease: bool,
) -> AssessmentReportFinishReceiptView:
    request_material = _finish_request_material(
        project_id,
        dispatcher_id,
        body,
        claim_sha256=claim_sha256,
    )
    request_json = _canonical_json(request_material)
    request_sha256 = sha256(request_json.encode("utf-8")).hexdigest()
    now = utcnow()
    with get_conn() as conn:
        project = _lock_report_project(conn, project_id)
        receipt = conn.execute(
            """
            SELECT * FROM assessment_report_finish_receipts
            WHERE project_id = ? AND task_run_id = ?
            """,
            (project_id, body.task_run_id),
        ).fetchone()
        if receipt is not None:
            if receipt["request_sha256"] != request_sha256:
                raise HTTPException(
                    409,
                    "assessment_report_finish_receipt_conflict",
                )
            return _finish_receipt_view(receipt)
        _require_report_claim_eligibility(conn, project)
        project_status = str(project["status"])
        if project_status == "active":
            raise HTTPException(409, "assessment_report_project_active")
        if project_status not in {"completed", "stopped"}:
            raise HTTPException(409, "assessment_report_project_status_invalid")
        current = conn.execute(
            "SELECT * FROM assessment_report_finalizations WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if current is None:
            raise HTTPException(404, "assessment_report_finalization_not_found")
        task_run = conn.execute(
            "SELECT * FROM task_runs WHERE project_id = ? AND id = ?",
            (project_id, body.task_run_id),
        ).fetchone()
        _require_report_execution_generation(project, task_run)
        assert task_run is not None
        context = _json_value(task_run["runtime_context_json"], {})
        claim = context.get("report_claim") if isinstance(context, dict) else None
        dispatcher_owner = context.get("dispatcher_id") if isinstance(context, dict) else None
        if dispatcher_owner != dispatcher_id:
            raise HTTPException(403, "execution_owner_mismatch")
        current_claim_sha256 = current["claim_sha256"]
        lease_expired = (
            current["lease_expires_at"] is not None
            and str(current["lease_expires_at"]) <= now
        )
        if (
            task_run["task_type"] != "report"
            or task_run["worker_name"] != body.worker
            or not isinstance(claim, dict)
            or claim.get("claim_sha256") != claim_sha256
            or current_claim_sha256 != claim_sha256
            or claim.get("generation_token") != body.generation_token
            or claim.get("attempt_count") != current["attempt_count"]
            or current["generation_token"] != body.generation_token
            or current["status"] != "running"
            or current["claimed_by"] != body.worker
            or (lease_expired and not allow_expired_lease)
        ):
            raise HTTPException(409, "assessment_report_claim_mismatch")
        bound_snapshot = (
            current["input_snapshot_artifact_id"],
            current["input_snapshot_sha256"],
        )
        requested_snapshot = (
            body.input_snapshot_artifact_id,
            body.input_snapshot_sha256,
        )
        if (
            body.status == "ready" or requested_snapshot != (None, None)
        ) and bound_snapshot != requested_snapshot:
            raise HTTPException(
                409,
                "assessment_report_snapshot_receipt_conflict",
            )
        return _persist_report_finish(
            conn,
            current,
            task_run,
            body,
            request_material=request_material,
            request_sha256=request_sha256,
            now=now,
        )


def finish_assessment_report_finalization(
    project_id: str,
    body: AssessmentReportFinishRequest,
    *,
    principal,
) -> AssessmentReportFinishReceiptView:
    """按精确 Artifact 与终态事实原子完成报告，并保存不可变收据。"""
    from .audit_task_runs import authenticated_execution_owner

    dispatcher_id = authenticated_execution_owner(principal)
    claim_sha256 = sha256(body.claim_token.encode("utf-8")).hexdigest()
    return _finish_assessment_report_finalization(
        project_id,
        body,
        dispatcher_id=dispatcher_id,
        claim_sha256=claim_sha256,
        allow_expired_lease=False,
    )


def replay_assessment_report_finalization(
    project_id: str,
    body: AssessmentReportFinishReplayRequest,
    *,
    principal,
) -> AssessmentReportFinishReceiptView:
    """重放不含明文 claim token 的本地 durable finish outbox。"""
    from .audit_task_runs import authenticated_execution_owner

    dispatcher_id = authenticated_execution_owner(principal)
    request_material = _finish_request_material(
        project_id,
        dispatcher_id,
        body,
        claim_sha256=body.claim_sha256,
    )
    actual_request_sha256 = sha256(
        _canonical_json(request_material).encode("utf-8")
    ).hexdigest()
    if actual_request_sha256 != body.request_sha256:
        raise HTTPException(409, "assessment_report_finish_request_digest_mismatch")
    return _finish_assessment_report_finalization(
        project_id,
        body,
        dispatcher_id=dispatcher_id,
        claim_sha256=body.claim_sha256,
        allow_expired_lease=True,
    )


_GOAL_DRIVEN_TESTED_OUTCOMES = {
    "finding",
    "tested_no_finding",
}
_GOAL_DRIVEN_BLOCKED_OUTCOMES = {
    "permanently_unreachable",
    "temporarily_blocked",
}


def _goal_driven_record_reason(
    record: dict[str, Any],
    intent: dict[str, Any],
) -> str:
    values: list[str] = []
    for key in ("limitation_reason", "limitation_details", "blocker_code"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    limitations = record.get("limitations")
    if isinstance(limitations, list):
        values.extend(str(value).strip() for value in limitations if str(value).strip())
    conclusion_reason = str(intent.get("conclusion_reason") or "").strip()
    if conclusion_reason:
        values.append(conclusion_reason)
    return "; ".join(dict.fromkeys(values))


def _goal_driven_report_coverage(
    conn: sqlite3.Connection,
    project_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """从单一 ExperimentRecord 与 Intent 结论生成新协议的工作交付摘要。"""
    facts = {
        item["id"]: item
        for item in _rows(
            conn,
            "SELECT * FROM facts WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
            json_columns=("assessment_record_json",),
        )
    }
    coverage: dict[str, list[dict[str, Any]]] = {
        "tested": [],
        "blocked": [],
        "untested": [],
    }
    intents = _rows(
        conn,
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at, id",
        (project_id,),
    )
    for intent in intents:
        if (
            intent.get("assessment_role") == "verifier"
            or intent.get("creator") == "dispatcher.bootstrap"
            or intent.get("to_fact_id") == "goal"
        ):
            # Bootstrap、独立复核和完成边不属于普通 Explore 工作范围。
            continue
        fact_id = intent.get("to_fact_id")
        fact = facts.get(fact_id) if isinstance(fact_id, str) else None
        record = fact.get("assessment_record") if isinstance(fact, dict) else None
        experiment = (
            record
            if isinstance(record, dict) and record.get("kind") == "experiment"
            else {}
        )
        outcome = experiment.get("outcome")
        if outcome in _GOAL_DRIVEN_TESTED_OUTCOMES:
            disposition = "tested"
        elif outcome in _GOAL_DRIVEN_BLOCKED_OUTCOMES:
            disposition = "blocked"
        else:
            # 越界、不适用和没有接受记录的工作均不能写成已测。
            disposition = "untested"
        result = (
            outcome
            if isinstance(outcome, str) and outcome
            else intent.get("conclusion") or "not_executed"
        )
        coverage[disposition].append(
            {
                "intent_id": intent.get("id"),
                "description": intent.get("description"),
                "priority": intent.get("priority", 1),
                "result": result,
                "reason": _goal_driven_record_reason(experiment, intent),
                "fact_id": fact_id,
                "hypothesis": experiment.get("hypothesis"),
                "observations": experiment.get("observations", []),
                "limitations": experiment.get("limitations", []),
                "attempted_strategies": experiment.get("attempted_strategies", []),
                "evidence_refs": experiment.get("evidence_refs", []),
            }
        )
    return coverage


def build_assessment_report_packet(
    conn: sqlite3.Connection,
    project_id: str,
) -> AssessmentReportPacket:
    """一次性聚合报告所需权威数据，避免 Dispatcher 逐端点拼接丢失 Observation。"""
    project_row = _lock_report_project(conn, project_id)
    if assessment_has_running_work(conn, project_id):
        raise HTTPException(409, "assessment_report_tasks_not_quiescent")
    if project_row["status"] == "stopped" and policy_from_project_row(project_row).phased:
        stop_assessment(conn, project_id, row_value(project_row, "coverage_stop_reason") or "user_stopped")
        project_row = get_project_or_404(conn, project_id)
    project = _row_dict(project_row)
    project["assessment_progress"] = _json_value(project.pop("assessment_progress_json", None), {})
    observation_statuses = []
    for observation in conn.execute("SELECT id, vulnerability_id FROM vulnerability_observations WHERE project_id = ? ORDER BY id", (project_id,)):
        results = [dict(row) for row in conn.execute("SELECT id, outcome, verifier_task_run_id FROM vulnerability_verifications WHERE project_id = ? AND observation_id = ? ORDER BY created_at, id", (project_id, observation["id"]))]
        verification_intents = [dict(row) for row in conn.execute("SELECT id, conclusion, conclusion_reason, to_fact_id FROM intents WHERE project_id = ? AND assessment_role = 'verifier' AND target_observation_id = ? ORDER BY id", (project_id, observation["id"]))]
        outcomes = {row["outcome"] for row in results}
        if len(outcomes) > 1:
            status = "conflicting"
        elif outcomes:
            status = next(iter(outcomes))
        elif any(row["conclusion"] == "failed" for row in verification_intents):
            status = "run_failed"
        elif verification_intents and all(row["conclusion"] is not None for row in verification_intents):
            status = "not_run"
        else:
            status = "pending" if verification_intents else "not_scheduled"
        observation_statuses.append({**dict(observation), "verification_status": status, "verifications": results, "intents": verification_intents})
    assessment_policy = policy_from_project_row(project_row)
    cells = _rows(conn, "SELECT * FROM coverage_slots WHERE project_id = ? ORDER BY slot_key", (project_id,))
    if assessment_policy.protocol == GOAL_DRIVEN_PROTOCOL:
        coverage = _goal_driven_report_coverage(conn, project_id)
    else:
        coverage = {"tested": [], "blocked": [], "untested": []}
        for cell in cells:
            disposition = "tested" if cell["result"] in {"finding", "tested_no_finding"} else "blocked" if cell["result"] == "temporarily_blocked" else "untested"
            coverage[disposition].append({"slot_key": cell["slot_key"], "result": cell["result"], "reason": cell["skip_reason"], "fact_id": cell["resulting_fact_id"]})
    summary = {
        "execution": assessment_execution_view(conn, project_row),
        "project_status": project_row["status"],
        "stop_reason": row_value(project_row, "coverage_stop_reason"),
        "coverage": coverage,
        "selected_deep": _rows(conn, "SELECT id, slot_key, strategy_key, depth_review_fact_id, to_fact_id, conclusion, conclusion_reason FROM intents WHERE project_id = ? AND depth_review_fact_id IS NOT NULL ORDER BY id", (project_id,)),
        "calls_by_task_type": _rows(conn, "SELECT task_type, COUNT(*) AS execution_count FROM task_runs WHERE project_id = ? GROUP BY task_type ORDER BY task_type", (project_id,)),
    }
    project["scenario_snapshot"] = _json_value(
        project.pop("scenario_snapshot_json", None),
        {},
    )
    policy = asdict(assessment_policy)
    report_policy = asdict(report_policy_from_project_row(project_row))
    quality = build_assessment_quality_snapshot(conn, project_id)
    chains = [
        item.model_dump()
        for item in build_vulnerability_chain_candidates(conn, project_id)
    ]
    return AssessmentReportPacket(
        assessment_summary=summary,
        observation_statuses=observation_statuses,
        project=project,
        policy=policy,
        report_policy=report_policy,
        targets=_rows(
            conn,
            "SELECT * FROM targets WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
        ),
        facts=_rows(
            conn,
            "SELECT * FROM facts WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
            json_columns=("assessment_record_json",),
        ),
        intents=_rows(
            conn,
            "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
        ),
        intent_sources=_rows(
            conn,
            "SELECT * FROM intent_sources WHERE project_id = ? ORDER BY intent_id, source_order, fact_id",
            (project_id,),
        ),
        surface_manifest=_rows(
            conn,
            "SELECT * FROM surface_manifest_entries WHERE project_id = ? ORDER BY created_at, entry_id",
            (project_id,),
            json_columns=("identity_json", "traits_json", "evidence_json"),
        ),
        coverage_cells=_rows(
            conn,
            "SELECT * FROM coverage_slots WHERE project_id = ? ORDER BY slot_key",
            (project_id,),
        ),
        coverage_scope_dispositions=_rows(
            conn,
            "SELECT * FROM coverage_scope_dispositions WHERE project_id = ? ORDER BY created_at, disposition_id",
            (project_id,),
            json_columns=("target_ids_json", "evidence_json"),
        ),
        quality_snapshot=asdict(quality) if quality is not None else None,
        vulnerabilities=_rows(
            conn,
            "SELECT * FROM vulnerabilities WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
            json_columns=("identity_json",),
        ),
        observations=_rows(
            conn,
            "SELECT * FROM vulnerability_observations WHERE project_id = ? ORDER BY observed_at, id",
            (project_id,),
            json_columns=("identity_json",),
        ),
        verifications=_rows(
            conn,
            "SELECT * FROM vulnerability_verifications WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
            json_columns=("evidence_json",),
        ),
        evidence_links=_rows(
            conn,
            "SELECT * FROM fact_evidence WHERE project_id = ? ORDER BY fact_id, artifact_id, pointer",
            (project_id,),
        ),
        artifacts=_rows(
            conn,
            "SELECT * FROM artifacts WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
            json_columns=("metadata_json",),
        ),
        chain_candidates=chains,
        assessment_attempts=_rows(
            conn,
            "SELECT * FROM assessment_attempts WHERE project_id = ? ORDER BY started_at, id",
            (project_id,),
        ),
        task_runs=_rows(
            conn,
            "SELECT * FROM task_runs WHERE project_id = ? ORDER BY started_at, id",
            (project_id,),
            json_columns=("runtime_context_json",),
        ),
    )
