"""覆盖 Cell 服务：pentest 与 default-copy 的攻击面覆盖状态机。

仅 coverage_gate 场景读写；default/logic-vuln 不触碰本表。default-copy 新版协议
还会在同一事务中追加权威 Surface Manifest 观察。
"""

from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timedelta, timezone

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL, validate_goal_driven_experiment
from cairn.server.models import CoverageSlot

from .assessment_policy import (
    DEFAULT_COPY_BLOCKER_COOLDOWN_SECONDS,
    policy_from_project_row,
)
from .common import *
from .surface_manifest import normalize_surface_key, register_surface_manifest_entry
from .time_utils import parse_utc_timestamp, utcnow


def structured_slot_key(surface_kind: str, surface_key: str, test_family: str) -> str:
    """由规范化 Cell 身份生成稳定键，避免标题或描述变化产生重复槽位。"""
    identity = f"default-copy-v1\n{surface_kind}\n{surface_key}\n{test_family}"
    return f"cell:{sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def blocker_fingerprint(
    blocker_code: str,
    limitation_reason: str,
    limitation_details: str,
) -> str:
    """规范化目标阻断事实，避免大小写或空白差异绕过 cooldown。"""
    parts = (
        blocker_code.strip().lower(),
        " ".join(limitation_reason.strip().lower().split()),
        " ".join(limitation_details.strip().lower().split()),
    )
    identity = "default-copy-blocker-v1\n" + "\n".join(parts)
    return f"blocker:{sha256(identity.encode('utf-8')).hexdigest()}"


def _project_policy(conn: sqlite3.Connection, project_id: str):
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return policy_from_project_row(row)


_RUNTIME_FAILURE_BLOCKER_CODES = {
    "config_mismatch",
    "container_failed",
    "dns_resolution_failed",
    "heartbeat_lost",
    "parse_error",
    "repair_failed",
    "server_write_failed",
    "worker_command_failed",
    "worker_oom",
    "worker_timeout",
}


def _has_current_run_artifact(
    conn: sqlite3.Connection,
    project_id: str,
    task_run_id: str | None,
    evidence: list[EvidenceRef],
    *,
    artifact_id: str | None = None,
) -> bool:
    """确认引用的 Artifact 属于当前业务运行，不能复用历史运行凑证据门。"""
    if not task_run_id:
        return False
    artifact_ids = {ref.artifact_id for ref in evidence}
    if artifact_id:
        artifact_ids.add(artifact_id)
    if not artifact_ids:
        return False
    placeholders = ",".join("?" for _ in artifact_ids)
    row = conn.execute(
        f"""
        SELECT 1 FROM artifacts
        WHERE project_id = ? AND task_run_id = ?
          AND id IN ({placeholders})
        LIMIT 1
        """,
        (project_id, task_run_id, *sorted(artifact_ids)),
    ).fetchone()
    return row is not None


def coverage_slot_from_row(row: sqlite3.Row) -> CoverageSlot:
    return CoverageSlot(
        project_id=row["project_id"],
        slot_key=row["slot_key"],
        kind=row["kind"],
        description=row["description"],
        status=row["status"],
        surface_key=row["surface_key"] if "surface_key" in row.keys() else None,
        surface_id=row["surface_id"] if "surface_id" in row.keys() else None,
        surface_kind=(row["surface_kind"] or "") if "surface_kind" in row.keys() else "",
        test_family=(row["test_family"] or "") if "test_family" in row.keys() else "",
        coverage_requirement=(
            (row["coverage_requirement"] or "")
            if "coverage_requirement" in row.keys()
            else ""
        ),
        coverage_rule_id=(
            (row["coverage_rule_id"] or "")
            if "coverage_rule_id" in row.keys()
            else ""
        ),
        applicability_basis=(
            (row["applicability_basis"] or "")
            if "applicability_basis" in row.keys()
            else ""
        ),
        completion_criteria=(
            (row["completion_criteria"] or "")
            if "completion_criteria" in row.keys()
            else ""
        ),
        result=(row["result"] or "") if "result" in row.keys() else "",
        skip_reason=(row["skip_reason"] or "") if "skip_reason" in row.keys() else "",
        blocker_fingerprint=(
            row["blocker_fingerprint"]
            if "blocker_fingerprint" in row.keys()
            else None
        ),
        blocker_strategy_key=(
            (row["blocker_strategy_key"] or "")
            if "blocker_strategy_key" in row.keys()
            else ""
        ),
        blocker_cooldown_until=(
            row["blocker_cooldown_until"]
            if "blocker_cooldown_until" in row.keys()
            else None
        ),
        source_intent_id=row["source_intent_id"],
        assigned_intent_id=row["assigned_intent_id"],
        resulting_fact_id=row["resulting_fact_id"],
        verified_run_id=row["verified_run_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        tier=row["tier"] if "tier" in row.keys() else "extended",
    )


def list_coverage_slots(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    statuses: tuple[str, ...] | None = None,
    tier: str | None = None,
) -> list[CoverageSlot]:
    # statuses 为 None 返回全部；否则按状态过滤。tier 为 None 返回所有层。
    clauses = ["project_id = ?"]
    params: list = [project_id]
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)
    if tier is not None:
        clauses.append("tier = ?")
        params.append(tier)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT * FROM coverage_slots WHERE {where} ORDER BY created_at, slot_key",
        params,
    ).fetchall()
    return [coverage_slot_from_row(row) for row in rows]


def upsert_coverage_slots(
    conn: sqlite3.Connection,
    project_id: str,
    slots: list[dict[str, str | None]],
    *,
    source_intent_id: str | None = None,
    tier: str = "extended",
    register_manifest_observation: bool = True,
) -> int:
    # legacy 仍按 slot_key 去重；结构化协议改由攻击面 × 测试族决定唯一 Cell。
    policy = _project_policy(conn, project_id)
    structured = policy.structured
    if policy.minimum_coverage_enabled and register_manifest_observation:
        # v1 minimum Coverage 只接受 Manifest 注册流程的服务端派生结果。
        raise HTTPException(409, "minimum_coverage_manifest_authority_required")
    now = utcnow()
    inserted_or_updated = 0
    for slot in slots:
        kind = str(slot["kind"])
        key = str(slot.get("key") or "")
        description = str(slot.get("description", "") or "")
        if not structured:
            if not key.strip():
                raise HTTPException(422, "legacy Coverage Slot requires key")
            conn.execute(
                """
                INSERT INTO coverage_slots
                    (project_id, slot_key, kind, description, status, tier, source_intent_id,
                     assigned_intent_id, resulting_fact_id, verified_run_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, NULL, ?, ?)
                ON CONFLICT(project_id, slot_key) DO UPDATE SET
                    description = excluded.description,
                    source_intent_id = COALESCE(excluded.source_intent_id, coverage_slots.source_intent_id),
                    updated_at = excluded.updated_at
                """,
                (project_id, key, kind, description, tier, source_intent_id, now, now),
            )
            inserted_or_updated += 1
            continue

        surface_kind = str(slot.get("surface_kind") or kind).strip().lower()
        raw_surface_key = str(slot.get("surface_key") or "").strip()
        test_family = str(slot.get("test_family") or "").strip().lower()
        applicability_basis = str(slot.get("applicability_basis") or "").strip()
        completion_criteria = str(slot.get("completion_criteria") or "").strip()
        if surface_kind not in COVERAGE_SLOT_KINDS:
            raise HTTPException(422, "structured Coverage Cell has invalid surface_kind")
        if kind != surface_kind:
            raise HTTPException(422, "structured Coverage Cell kind must equal surface_kind")
        if test_family not in DEFAULT_COPY_TEST_FAMILIES:
            raise HTTPException(422, "structured Coverage Cell has invalid test_family")
        if not applicability_basis:
            raise HTTPException(422, "structured Coverage Cell requires applicability_basis")
        if not completion_criteria:
            raise HTTPException(422, "structured Coverage Cell requires completion_criteria")

        normalized_surface = normalize_surface_key(surface_kind, raw_surface_key)
        if policy.minimum_coverage_enabled:
            surface_id = str(slot.get("surface_id") or "").strip()
            coverage_requirement = str(
                slot.get("coverage_requirement") or ""
            ).strip().lower()
            coverage_rule_id = str(slot.get("coverage_rule_id") or "").strip()
            if not surface_id or coverage_requirement not in {"required", "optional"}:
                raise HTTPException(422, "minimum_coverage_cell_identity_invalid")
            if not coverage_rule_id:
                raise HTTPException(422, "minimum_coverage_rule_id_required")
            manifest = conn.execute(
                """
                SELECT 1 FROM surface_manifest_entries
                WHERE project_id = ? AND surface_id = ?
                  AND surface_kind = ? AND surface_key = ?
                LIMIT 1
                """,
                (project_id, surface_id, surface_kind, normalized_surface),
            ).fetchone()
            if manifest is None:
                raise HTTPException(409, "minimum_coverage_surface_not_in_manifest")
            cell_key = structured_slot_key(
                surface_kind,
                normalized_surface,
                test_family,
            )
            conn.execute(
                """
                INSERT INTO coverage_slots (
                    project_id, slot_key, kind, description, status, tier,
                    source_intent_id, assigned_intent_id, resulting_fact_id,
                    verified_run_id, created_at, updated_at, surface_key,
                    surface_id, surface_kind, test_family, coverage_requirement,
                    coverage_rule_id, applicability_basis, completion_criteria,
                    result, skip_reason
                ) VALUES (
                    ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, NULL, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, '', ''
                )
                ON CONFLICT(project_id, surface_id, test_family)
                    WHERE surface_id IS NOT NULL AND surface_id != '' AND test_family != ''
                DO UPDATE SET
                    description = excluded.description,
                    coverage_requirement = CASE
                        WHEN coverage_slots.coverage_requirement = 'required'
                          OR excluded.coverage_requirement = 'required'
                        THEN 'required' ELSE 'optional'
                    END,
                    coverage_rule_id = CASE
                        WHEN coverage_slots.coverage_requirement = 'required'
                        THEN coverage_slots.coverage_rule_id
                        ELSE excluded.coverage_rule_id
                    END,
                    applicability_basis = excluded.applicability_basis,
                    completion_criteria = excluded.completion_criteria,
                    tier = CASE
                        WHEN coverage_slots.tier = 'core' OR excluded.tier = 'core'
                        THEN 'core' ELSE 'extended'
                    END,
                    source_intent_id = COALESCE(
                        excluded.source_intent_id, coverage_slots.source_intent_id
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    project_id,
                    cell_key,
                    surface_kind,
                    description,
                    tier,
                    source_intent_id,
                    now,
                    now,
                    normalized_surface,
                    surface_id,
                    surface_kind,
                    test_family,
                    coverage_requirement,
                    coverage_rule_id,
                    applicability_basis,
                    completion_criteria,
                ),
            )
            inserted_or_updated += 1
            continue
        if policy.surface_manifest_enabled and register_manifest_observation:
            register_surface_manifest_entry(
                conn,
                project_id,
                surface_kind=surface_kind,
                surface_key=normalized_surface,
                description=description,
                traits={
                    "test_family": test_family,
                    "applicability_basis": applicability_basis,
                    "completion_criteria": completion_criteria,
                },
                source_intent_id=source_intent_id,
            )
        cell_key = structured_slot_key(surface_kind, normalized_surface, test_family)
        conn.execute(
            """
            INSERT INTO coverage_slots (
                project_id, slot_key, kind, description, status, tier, source_intent_id,
                assigned_intent_id, resulting_fact_id, verified_run_id, created_at, updated_at,
                surface_key, surface_kind, test_family, applicability_basis,
                completion_criteria, result, skip_reason
            ) VALUES (
                ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, NULL, ?, ?,
                ?, ?, ?, ?, ?, '', ''
            )
            ON CONFLICT(project_id, surface_kind, surface_key, test_family)
                WHERE surface_key IS NOT NULL AND surface_key != '' AND test_family != ''
            DO UPDATE SET
                description = excluded.description,
                applicability_basis = excluded.applicability_basis,
                completion_criteria = excluded.completion_criteria,
                tier = CASE
                    WHEN coverage_slots.tier = 'core' OR excluded.tier = 'core'
                    THEN 'core' ELSE 'extended'
                END,
                source_intent_id = COALESCE(
                    excluded.source_intent_id, coverage_slots.source_intent_id
                ),
                updated_at = excluded.updated_at
            """,
            (
                project_id,
                cell_key,
                surface_kind,
                description,
                tier,
                source_intent_id,
                now,
                now,
                normalized_surface,
                surface_kind,
                test_family,
                applicability_basis,
                completion_criteria,
            ),
        )
        inserted_or_updated += 1
    return inserted_or_updated


def assign_slot_intent(
    conn: sqlite3.Connection,
    project_id: str,
    slot_key: str,
    intent_id: str,
) -> None:
    policy = _project_policy(conn, project_id)
    if not policy.structured:
        # legacy pentest 保持宽松绑定，不改变既有场景对异常 slot_key 的容错。
        conn.execute(
            "UPDATE coverage_slots SET assigned_intent_id = ?, updated_at = ? WHERE project_id = ? AND slot_key = ?",
            (intent_id, utcnow(), project_id, slot_key),
        )
        return

    slot = conn.execute(
        "SELECT * FROM coverage_slots WHERE project_id = ? AND slot_key = ?",
        (project_id, slot_key),
    ).fetchone()
    if slot is None:
        raise HTTPException(422, "Coverage Cell not found")
    if slot["status"] not in ("pending", "exploring"):
        raise HTTPException(409, "Coverage Cell is already terminal")
    now = utcnow()
    if policy.blocker_cooldown_enabled:
        intent = conn.execute(
            "SELECT strategy_key FROM intents WHERE project_id = ? AND id = ?",
            (project_id, intent_id),
        ).fetchone()
        strategy_key = (
            (intent["strategy_key"] or "")
            if intent is not None and "strategy_key" in intent.keys()
            else ""
        )
        if not strategy_key:
            raise HTTPException(422, "coverage_cell_strategy_required")
        cooldown_until = (
            slot["blocker_cooldown_until"]
            if "blocker_cooldown_until" in slot.keys()
            else None
        )
        prior_strategy = (
            (slot["blocker_strategy_key"] or "")
            if "blocker_strategy_key" in slot.keys()
            else ""
        )
        cooldown_dt = parse_utc_timestamp(cooldown_until)
        now_dt = parse_utc_timestamp(now)
        if (
            cooldown_dt is not None
            and now_dt is not None
            and cooldown_dt > now_dt
            and strategy_key == prior_strategy
        ):
            raise HTTPException(409, "coverage_cell_blocker_cooldown_active")
    assigned = slot["assigned_intent_id"]
    if assigned and assigned != intent_id:
        prior = conn.execute(
            "SELECT concluded_at FROM intents WHERE project_id = ? AND id = ?",
            (project_id, assigned),
        ).fetchone()
        if prior is not None and prior["concluded_at"] is None:
            raise HTTPException(409, "Coverage Cell is assigned to another open intent")

    updated = conn.execute(
        """
        UPDATE coverage_slots
        SET assigned_intent_id = ?, status = 'exploring', updated_at = ?
        WHERE project_id = ? AND slot_key = ?
          AND status IN ('pending', 'exploring')
          AND (
              assigned_intent_id IS NULL OR assigned_intent_id = ? OR NOT EXISTS (
                  SELECT 1 FROM intents AS prior
                  WHERE prior.project_id = coverage_slots.project_id
                    AND prior.id = coverage_slots.assigned_intent_id
                    AND prior.to_fact_id IS NULL AND prior.concluded_at IS NULL
              )
          )
        """,
        (intent_id, now, project_id, slot_key, intent_id),
    )
    if updated.rowcount != 1:
        raise HTTPException(409, "coverage_cell_assignment_changed")


def reset_slot_for_abandoned_intent(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    *,
    updated_at: str,
) -> None:
    """释放 abandoned Intent 占用的非终态 Cell，使其可在后续轮次重新调度。"""
    if not _project_policy(conn, project_id).structured:
        return
    conn.execute(
        """
        UPDATE coverage_slots
        SET status = 'pending', assigned_intent_id = NULL, updated_at = ?
        WHERE project_id = ? AND assigned_intent_id = ?
          AND status IN ('pending', 'exploring')
        """,
        (updated_at, project_id, intent_id),
    )


def validate_structured_coverage_result(
    conn: sqlite3.Connection,
    project_id: str,
    intent_row: sqlite3.Row,
    coverage_result,
    vulnerability,
    evidence: list[EvidenceRef],
    *,
    task_run_id: str | None = None,
) -> sqlite3.Row | None:
    """在 Fact 写入前验证 Cell 身份、绑定和 outcome 证明要求。"""
    policy = _project_policy(conn, project_id)
    if not policy.structured:
        return None
    slot_key = intent_row["slot_key"] if "slot_key" in intent_row.keys() else None
    if not slot_key:
        if coverage_result is not None:
            raise HTTPException(422, "Coverage result requires a Cell-bound intent")
        return None
    if coverage_result is None:
        raise HTTPException(422, "default-copy-v1 Cell conclude requires coverage_result")

    slot = conn.execute(
        "SELECT * FROM coverage_slots WHERE project_id = ? AND slot_key = ?",
        (project_id, slot_key),
    ).fetchone()
    if slot is None:
        raise HTTPException(422, "Coverage Cell not found")
    selected_deep = policy.phased and bool(intent_row["depth_review_fact_id"])
    if not selected_deep:
        if slot["assigned_intent_id"] != intent_row["id"]:
            raise HTTPException(409, "Coverage Cell is not assigned to this intent")
        if slot["status"] not in ("pending", "exploring"):
            raise HTTPException(409, "Coverage Cell is already terminal")

    normalized_surface = normalize_surface_key(slot["surface_kind"], coverage_result.surface_key)
    if (
        coverage_result.slot_key != slot_key
        or normalized_surface != slot["surface_key"]
        or coverage_result.test_family != slot["test_family"]
    ):
        raise HTTPException(422, "coverage_result identity does not match the assigned Cell")

    evidence_keys = {
        (ref.artifact_id, ref.pointer, ref.verifier_run_id) for ref in evidence
    }
    for ref in coverage_result.evidence_refs:
        key = (ref.artifact_id, ref.pointer, ref.verifier_run_id)
        if key not in evidence_keys:
            raise HTTPException(422, "coverage_result evidence_refs must also appear in evidence")

    outcome = coverage_result.outcome
    if outcome != "finding" and vulnerability is not None:
        raise HTTPException(
            422,
            "non_finding_outcome_must_not_include_vulnerability",
        )
    if outcome == "tested_no_finding":
        if not coverage_result.baseline or not coverage_result.variants:
            raise HTTPException(422, "tested_no_finding requires baseline and variants")
        if policy.outcome_contract_version >= 2 and not _has_current_run_artifact(
            conn, project_id, task_run_id, coverage_result.evidence_refs
        ):
            raise HTTPException(422, "coverage_evidence_current_run_required")
    elif outcome == "finding":
        if vulnerability is None:
            raise HTTPException(422, "finding requires vulnerability")
        if vulnerability.identity is None:
            raise HTTPException(422, "finding requires vulnerability identity")
        if policy.outcome_contract_version >= 2:
            if not coverage_result.baseline or not coverage_result.variants:
                raise HTTPException(422, "finding_baseline_variants_required")
            complete_exchange = bool(
                vulnerability.repro_request and vulnerability.repro_response
            )
            packet_evidence = bool(vulnerability.packet_artifact_id) and _has_current_run_artifact(
                conn,
                project_id,
                task_run_id,
                [],
                artifact_id=vulnerability.packet_artifact_id,
            )
            if not complete_exchange and not packet_evidence:
                raise HTTPException(422, "finding_complete_reproduction_required")
            if not _has_current_run_artifact(
                conn, project_id, task_run_id, coverage_result.evidence_refs
            ):
                raise HTTPException(422, "coverage_evidence_current_run_required")
        else:
            has_reproduction = bool(
                vulnerability.packet_artifact_id
                or vulnerability.repro_request
                or vulnerability.repro_response
                or coverage_result.evidence_refs
            )
            if not has_reproduction:
                raise HTTPException(422, "finding requires reproduction evidence")
    elif outcome == "temporarily_blocked":
        if not coverage_result.limitation_reason:
            raise HTTPException(422, "temporarily_blocked requires limitation_reason")
        if policy.outcome_contract_version >= 2:
            blocker_code = coverage_result.blocker_code.strip().lower()
            if blocker_code in _RUNTIME_FAILURE_BLOCKER_CODES:
                raise HTTPException(422, "runtime_failure_is_not_target_blocker")
            if not blocker_code or not coverage_result.limitation_details:
                raise HTTPException(422, "target_blocker_details_required")
            if not coverage_result.attempted_strategies:
                raise HTTPException(422, "target_blocker_strategy_required")
        if policy.blocker_cooldown_enabled:
            intent_strategy = (
                (intent_row["strategy_key"] or "")
                if "strategy_key" in intent_row.keys()
                else ""
            )
            if not coverage_result.strategy_key:
                raise HTTPException(422, "blocker_strategy_key_required")
            if coverage_result.strategy_key != intent_strategy:
                raise HTTPException(422, "blocker_strategy_key_mismatch")
    elif outcome in {"permanently_unreachable", "out_of_scope", "not_applicable"}:
        if policy.outcome_contract_version >= 2:
            # 当前没有可绑定到单个 Cell 的 Server 权威否定记录，不能用自由文本跳过。
            raise HTTPException(
                422,
                "coverage_terminal_outcome_not_authoritatively_verifiable",
            )
        if not coverage_result.limitation_reason or not coverage_result.limitation_details:
            raise HTTPException(
                422, f"{outcome} requires limitation_reason and limitation_details"
            )
    return slot


def validate_assessment_experiment(conn, project_id: str, intent, body, slot) -> dict | None:
    """持久化前校验唯一实验记录与证据边界；不复制 coverage 正文。"""
    record = body.assessment_record
    policy = _project_policy(conn, project_id)
    goal_driven = policy.protocol == GOAL_DRIVEN_PROTOCOL
    if goal_driven:
        if body.coverage_result is not None:
            raise HTTPException(422, "goal_driven_coverage_result_forbidden")
        if record is None:
            raise HTTPException(422, "assessment_experiment_required")
        if slot is not None:
            raise HTTPException(422, "goal_driven_experiment_must_not_bind_cell")
    else:
        if not policy.phased:
            if record is not None:
                raise HTTPException(409, "assessment_contract_unsupported")
            return None
        if slot is None:
            if record is not None:
                raise HTTPException(422, "experiment_requires_coverage_cell")
            return None
        if record is None:
            raise HTTPException(422, "assessment_experiment_required")

    if goal_driven:
        try:
            validate_goal_driven_experiment(record)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    if any(
        ref.artifact_handle is not None or ref.artifact_id is None
        for ref in record.evidence_refs
    ):
        raise HTTPException(422, "assessment_evidence_handle_unresolved")
    refs = [
        EvidenceRef(
            artifact_id=ref.artifact_id,
            pointer=ref.pointer,
            verifier_run_id=ref.verifier_run_id,
        )
        for ref in record.evidence_refs
    ]
    supplied = {(ref.artifact_id, ref.pointer) for ref in body.evidence}
    if any((ref.artifact_id, ref.pointer) not in supplied for ref in refs):
        raise HTTPException(422, "experiment_evidence_must_appear_in_evidence")
    if not refs or not _has_current_run_artifact(
        conn, project_id, body.task_run_id, refs
    ):
        raise HTTPException(422, "experiment_current_run_evidence_required")

    outcome = record.outcome if goal_driven else body.coverage_result.outcome
    if outcome in {"finding", "tested_no_finding"} and not goal_driven:
        if not (record.baseline and record.variants and record.controls and record.observations):
            raise HTTPException(422, "experiment_comparison_and_observations_required")
        if (
            record.baseline != body.coverage_result.baseline
            or record.variants != body.coverage_result.variants
        ):
            raise HTTPException(422, "experiment_coverage_comparison_mismatch")
        if not goal_driven and slot["test_family"] in {
            "authorization",
            "business_logic",
        } and not record.preconditions:
            raise HTTPException(
                422, "experiment_identity_or_state_preconditions_required"
            )
        if (
            not goal_driven
            and slot["test_family"] == "authorization"
            and outcome == "tested_no_finding"
        ):
            manifests = conn.execute(
                """SELECT traits_json FROM surface_manifest_entries
                   WHERE project_id = ? AND surface_id = ?
                   ORDER BY created_at DESC, entry_id DESC""",
                (project_id, slot["surface_id"]),
            ).fetchall()
            available = False
            for row in manifests:
                traits = json.loads(row["traits_json"] or "{}")
                if "second_identity_available" in traits:
                    # 最新显式观察优先，历史 true 不能覆盖身份已失效的事实。
                    available = str(traits["second_identity_available"]).lower() in {
                        "true",
                        "yes",
                        "1",
                    }
                    break
            if not available:
                raise HTTPException(
                    422, "authorization_second_identity_missing_use_blocked"
                )
    if goal_driven:
        vulnerability = body.vulnerability
        if outcome != "finding" and vulnerability is not None:
            raise HTTPException(
                422, "non_finding_outcome_must_not_include_vulnerability"
            )
        if outcome == "finding":
            if vulnerability is None or vulnerability.identity is None:
                raise HTTPException(422, "finding_requires_vulnerability_identity")
            complete_exchange = bool(
                vulnerability.repro_request and vulnerability.repro_response
            )
            packet_evidence = bool(
                vulnerability.packet_artifact_id
            ) and _has_current_run_artifact(
                conn,
                project_id,
                body.task_run_id,
                [],
                artifact_id=vulnerability.packet_artifact_id,
            )
            if not complete_exchange and not packet_evidence:
                raise HTTPException(422, "finding_complete_reproduction_required")
        elif outcome == "temporarily_blocked" and (
            record.blocker_code.strip().lower() in _RUNTIME_FAILURE_BLOCKER_CODES
        ):
            raise HTTPException(422, "runtime_failure_is_not_target_blocker")
    elif outcome == "temporarily_blocked" and not record.limitations:
        raise HTTPException(422, "blocked_experiment_limitations_required")
    return record.model_dump(by_alias=True, exclude_none=True)


def apply_structured_coverage_result(
    conn: sqlite3.Connection,
    project_id: str,
    slot_key: str,
    *,
    intent_id: str,
    fact_id: str,
    coverage_result,
) -> None:
    """Conclude 事务内推进 Cell；临时受阻回到 pending，允许后续重新分配。"""
    outcome = coverage_result.outcome
    policy = _project_policy(conn, project_id)
    blocker_state: tuple[str | None, str, str | None] = (None, "", None)
    if outcome == "temporarily_blocked":
        status = "pending"
        assigned_intent_id = None
        if policy.blocker_cooldown_enabled:
            cooldown_until = (
                datetime.now(timezone.utc)
                + timedelta(seconds=DEFAULT_COPY_BLOCKER_COOLDOWN_SECONDS)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            blocker_state = (
                blocker_fingerprint(
                    coverage_result.blocker_code,
                    coverage_result.limitation_reason,
                    coverage_result.limitation_details,
                ),
                coverage_result.strategy_key,
                cooldown_until,
            )
    elif outcome in {"permanently_unreachable", "out_of_scope", "not_applicable"}:
        status = "skipped"
        assigned_intent_id = intent_id
    else:
        status = "explored"
        assigned_intent_id = intent_id
    skip_reason = (
        coverage_result.limitation_reason
        if outcome in {
            "temporarily_blocked",
            "permanently_unreachable",
            "out_of_scope",
            "not_applicable",
        }
        else ""
    )
    updated = conn.execute(
        """
        UPDATE coverage_slots
        SET status = ?, result = ?, skip_reason = ?, resulting_fact_id = ?,
            assigned_intent_id = ?, blocker_fingerprint = ?,
            blocker_strategy_key = ?, blocker_cooldown_until = ?, updated_at = ?
        WHERE project_id = ? AND slot_key = ?
          AND assigned_intent_id = ? AND status IN ('pending', 'exploring')
        """,
        (
            status,
            outcome,
            skip_reason,
            fact_id,
            assigned_intent_id,
            blocker_state[0],
            blocker_state[1],
            blocker_state[2],
            utcnow(),
            project_id,
            slot_key,
            intent_id,
        ),
    )
    if updated.rowcount != 1:
        raise HTTPException(409, "coverage_cell_assignment_changed")


def mark_slot_explored(
    conn: sqlite3.Connection,
    project_id: str,
    slot_key: str,
    *,
    intent_id: str | None = None,
    fact_id: str,
) -> None:
    # explore conclude 时回写：slot 置 explored，绑定产出 fact。失败/无漏洞也算覆盖。
    # 只在 slot 仍处于 pending/exploring 时推进，避免覆盖已 verified 的状态（Phase 3）。
    conn.execute(
        """
        UPDATE coverage_slots
        SET status = 'explored',
            resulting_fact_id = ?,
            assigned_intent_id = COALESCE(?, assigned_intent_id),
            updated_at = ?
        WHERE project_id = ? AND slot_key = ? AND status IN ('pending', 'exploring')
        """,
        (fact_id, intent_id, utcnow(), project_id, slot_key),
    )


def mark_slot_verified(
    conn: sqlite3.Connection,
    project_id: str,
    slot_key: str,
    *,
    intent_id: str | None = None,
    fact_id: str | None = None,
    verifier_run_id: str | None = None,
) -> None:
    # Phase 3 verifier：对已 explored 的 slot 再跑一轮复现验证，conclude 时置 verified。
    # 只推进 explored 状态，避免把 pending/exploring 误升为 verified。
    conn.execute(
        """
        UPDATE coverage_slots
        SET status = 'verified',
            resulting_fact_id = COALESCE(?, resulting_fact_id),
            assigned_intent_id = COALESCE(?, assigned_intent_id),
            verified_run_id = COALESCE(?, verified_run_id),
            updated_at = ?
        WHERE project_id = ? AND slot_key = ? AND status = 'explored'
        """,
        (fact_id, intent_id, verifier_run_id, utcnow(), project_id, slot_key),
    )


def count_open_coverage_slots(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    require_verified: bool = False,
    tier: str | None = None,
) -> int:
    # 完成门用：未收敛槽位数。Phase 1/2 查 pending+exploring；
    # Phase 3 verifier 启用时（require_verified=True）额外把 explored-unverified 算作未收敛。
    # Phase 4 tier 过滤：'core' 仅 core 层，'extended' 仅 extended 层，None 全部。
    if require_verified:
        statuses = ("pending", "exploring", "explored")
    else:
        statuses = ("pending", "exploring")
    placeholders = ",".join("?" for _ in statuses)
    clauses = [f"project_id = ?", f"status IN ({placeholders})"]
    params: list = [project_id, *statuses]
    if tier is not None:
        clauses.append("tier = ?")
        params.append(tier)
    where = " AND ".join(clauses)
    row = conn.execute(f"SELECT COUNT(*) AS c FROM coverage_slots WHERE {where}", params).fetchone()
    return int(row["c"]) if row else 0


def mark_slots_skipped(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    tier: str | None = None,
    reason: str = "coverage tail",
) -> int:
    # Phase 4 B/D 收尾：批量把 pending/exploring 的 slot 标 skipped，让 reason 下轮看 pending 空能 complete。
    # tier=None 在执行截止时收尾所有层；tier='extended' 只收尾过程发现（B 收益递减），core 不动。
    clauses = ["project_id = ?", "status IN ('pending', 'exploring')"]
    params: list = [project_id]
    if tier is not None:
        clauses.append("tier = ?")
        params.append(tier)
    where = " AND ".join(clauses)
    now = utcnow()
    cur = conn.execute(
        f"UPDATE coverage_slots SET status = 'skipped', updated_at = ? WHERE {where}",
        (now, *params),
    )
    return int(cur.rowcount)
