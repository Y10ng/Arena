"""零 Surface 项目的证据化范围处置与目标快照校验。"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from fastapi import HTTPException

from cairn.project_authorization_contract import target_scope_digest
from cairn.server.models import CoverageScopeDispositionInput

from .assessment_policy import policy_from_project_row
from .common import dump_json
from .projects import (
    validate_artifacts_exist,
    validate_execution_generation,
    validate_facts_exist,
    validate_task_run_exists,
)
from .targets import normalize_target_value
from .time_utils import utcnow


COVERAGE_SCOPE_DISPOSITION_VERSION = 1


def target_scope_snapshot(conn, project_id: str) -> tuple[str, list[str]]:
    """对全部 Target 身份与状态做稳定快照，使范围变更自动使旧处置失效。"""
    rows = conn.execute(
        """
        SELECT id, type, value, status
        FROM targets
        WHERE project_id = ?
        ORDER BY id
        """,
        (project_id,),
    ).fetchall()
    snapshot = [
        {
            "id": str(row["id"]),
            "type": str(row["type"]),
            "value": normalize_target_value(
                str(row["type"]),
                str(row["value"]),
            ),
            "status": str(row["status"]),
        }
        for row in rows
    ]
    return target_scope_digest(snapshot)


def _evidence_payload(body: CoverageScopeDispositionInput) -> list[dict[str, Any]]:
    return [ref.model_dump(exclude_none=True) for ref in body.evidence_refs]


def register_coverage_scope_disposition(
    conn,
    project_id: str,
    body: CoverageScopeDispositionInput,
    *,
    source_intent_id: str,
    source_fact_id: str,
    source_task_run_id: str,
    allow_stopping_settlement: bool = False,
) -> str:
    """仅接受 Bootstrap 当前运行证据支持的零 Surface 范围结论。"""
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        raise HTTPException(404, "Project not found")
    if not policy_from_project_row(project).minimum_coverage_enabled:
        raise HTTPException(409, "minimum_coverage_not_enabled")

    task_run = validate_task_run_exists(conn, project_id, source_task_run_id)
    validate_execution_generation(
        conn,
        project_id,
        source_task_run_id,
        allow_stopping_settlement=allow_stopping_settlement,
    )
    if task_run["task_type"] != "bootstrap":
        raise HTTPException(422, "coverage_scope_disposition_bootstrap_required")
    if task_run["intent_id"] != source_intent_id:
        raise HTTPException(422, "coverage_scope_disposition_intent_mismatch")
    validate_facts_exist(conn, project_id, [source_fact_id])
    intent = conn.execute(
        "SELECT 1 FROM intents WHERE id = ? AND project_id = ?",
        (source_intent_id, project_id),
    ).fetchone()
    if intent is None:
        raise HTTPException(404, f"Intent {source_intent_id} not found")

    surface_count = conn.execute(
        "SELECT COUNT(DISTINCT surface_id) AS c FROM surface_manifest_entries WHERE project_id = ?",
        (project_id,),
    ).fetchone()["c"]
    if int(surface_count or 0) != 0:
        raise HTTPException(409, "coverage_scope_disposition_requires_zero_surfaces")
    if not body.evidence_refs:
        raise HTTPException(422, "coverage_scope_disposition_evidence_required")
    validate_artifacts_exist(conn, project_id, body.evidence_refs)
    artifact_ids = sorted({ref.artifact_id for ref in body.evidence_refs})
    placeholders = ",".join("?" for _ in artifact_ids)
    current_count = conn.execute(
        f"""
        SELECT COUNT(DISTINCT id) AS c
        FROM artifacts
        WHERE project_id = ? AND task_run_id = ?
          AND id IN ({placeholders})
        """,
        (project_id, source_task_run_id, *artifact_ids),
    ).fetchone()["c"]
    if int(current_count or 0) != len(artifact_ids):
        raise HTTPException(422, "coverage_scope_disposition_current_run_evidence_required")

    target_digest, active_target_ids = target_scope_snapshot(conn, project_id)
    submitted_target_ids = sorted(body.target_ids)
    if submitted_target_ids != active_target_ids:
        raise HTTPException(422, "coverage_scope_disposition_target_snapshot_mismatch")
    if body.disposition_kind == "no_in_scope_targets" and active_target_ids:
        raise HTTPException(422, "coverage_scope_disposition_active_targets_present")
    if body.disposition_kind == "no_reachable_surfaces" and not active_target_ids:
        raise HTTPException(422, "coverage_scope_disposition_active_targets_required")

    evidence = _evidence_payload(body)
    observation_identity = "\n".join(
        (
            "default-copy-coverage-scope-disposition-v1",
            body.disposition_kind,
            target_digest,
            dump_json(submitted_target_ids),
            body.reason,
            dump_json(evidence),
            source_intent_id,
            source_fact_id,
            source_task_run_id,
        )
    )
    observation_digest = sha256(observation_identity.encode("utf-8")).hexdigest()
    disposition_id = f"csd:{observation_digest[:24]}"
    conn.execute(
        """
        INSERT INTO coverage_scope_dispositions (
            disposition_id, project_id, disposition_kind,
            target_snapshot_digest, target_ids_json, reason, evidence_json,
            source_intent_id, source_fact_id, source_task_run_id,
            observation_fingerprint, disposition_version, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'observed', ?)
        ON CONFLICT(project_id, observation_fingerprint) DO NOTHING
        """,
        (
            disposition_id,
            project_id,
            body.disposition_kind,
            target_digest,
            dump_json(submitted_target_ids),
            body.reason,
            dump_json(evidence),
            source_intent_id,
            source_fact_id,
            source_task_run_id,
            f"coverage-scope-disposition:{observation_digest}",
            COVERAGE_SCOPE_DISPOSITION_VERSION,
            utcnow(),
        ),
    )
    return disposition_id


def has_current_coverage_scope_disposition(conn, project_id: str) -> bool:
    """只承认与当前完整 Target 快照一致的已观察处置。"""
    target_digest, _active_target_ids = target_scope_snapshot(conn, project_id)
    row = conn.execute(
        """
        SELECT 1
        FROM coverage_scope_dispositions
        WHERE project_id = ? AND target_snapshot_digest = ?
          AND disposition_version = ? AND status = 'observed'
        LIMIT 1
        """,
        (project_id, target_digest, COVERAGE_SCOPE_DISPOSITION_VERSION),
    ).fetchone()
    return row is not None
