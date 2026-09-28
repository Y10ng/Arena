"""结构化 Reason/Explore 的 Server 权威 Cell 绑定。"""

from __future__ import annotations

from hashlib import sha256
import json
import sqlite3
from typing import Any, Mapping

from fastapi import HTTPException
from pydantic import ValidationError

from cairn.assessment_contracts import (
    StructuredCoverageCellBinding,
    StructuredExploreBinding,
    StructuredIntentBinding,
    StructuredStrategyConstraint,
)

from .assessment_policy import parse_scenario_snapshot, policy_from_project_row
from .time_utils import parse_utc_timestamp, utcnow


ORDINARY_STRUCTURED_REASON_PHASES = {"mapping", "baseline"}


def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    canonical = {key: value for key, value in payload.items() if key != "digest"}
    return sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _with_digest(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "digest": _canonical_digest(payload)}


def _validate_digest(payload: Mapping[str, Any], detail: str) -> None:
    if payload.get("digest") != _canonical_digest(payload):
        raise HTTPException(422, detail)


def _assessment_phase(project: Mapping[str, Any]) -> str:
    phase = parse_scenario_snapshot(_row_value(project, "assessment_progress_json")).get(
        "phase"
    )
    return str(phase or "")


def ordinary_structured_reason_enabled(project: Mapping[str, Any]) -> bool:
    policy = policy_from_project_row(project)
    return policy.phased and _assessment_phase(project) in ORDINARY_STRUCTURED_REASON_PHASES


def _strategy_constraint(
    slot: Mapping[str, Any],
    *,
    cooldown_enabled: bool,
    now: str,
) -> StructuredStrategyConstraint:
    prior_strategy = str(_row_value(slot, "blocker_strategy_key", "") or "").strip()
    cooldown_until = parse_utc_timestamp(
        _row_value(slot, "blocker_cooldown_until")
    )
    current = parse_utc_timestamp(now)
    if (
        cooldown_enabled
        and prior_strategy
        and cooldown_until is not None
        and current is not None
        and cooldown_until > current
    ):
        return StructuredStrategyConstraint(
            mode="must_differ",
            excluded_strategy_key=prior_strategy,
        )
    return StructuredStrategyConstraint()


def _cell_binding(
    slot: Mapping[str, Any],
    *,
    cooldown_enabled: bool,
    now: str,
) -> StructuredCoverageCellBinding:
    try:
        return StructuredCoverageCellBinding(
            slot_key=str(_row_value(slot, "slot_key") or "").strip(),
            surface_kind=str(
                _row_value(slot, "surface_kind")
                or _row_value(slot, "kind")
                or ""
            ).strip(),
            surface_key=str(_row_value(slot, "surface_key") or "").strip(),
            test_family=str(_row_value(slot, "test_family") or "").strip(),
            applicability_basis=str(
                _row_value(slot, "applicability_basis") or ""
            ).strip(),
            completion_criteria=str(
                _row_value(slot, "completion_criteria") or ""
            ).strip(),
            coverage_requirement=str(
                _row_value(slot, "coverage_requirement") or ""
            ).strip(),
            tier=str(_row_value(slot, "tier") or "extended").strip(),
            strategy=_strategy_constraint(
                slot,
                cooldown_enabled=cooldown_enabled,
                now=now,
            ),
        )
    except ValidationError as exc:
        raise HTTPException(422, "structured_intent_binding_invalid") from exc


def issue_structured_intent_binding(
    conn: sqlite3.Connection,
    project: Mapping[str, Any],
    *,
    issued_at: str | None = None,
) -> dict[str, Any] | None:
    """在 Reason admission 的项目锁内冻结当前 eligible Cell 集合。"""
    if not ordinary_structured_reason_enabled(project):
        return None
    now = issued_at or utcnow()
    rows = conn.execute(
        """
        SELECT cell.*
        FROM coverage_slots AS cell
        WHERE cell.project_id = ?
          AND cell.status IN ('pending', 'exploring')
          AND (
              cell.assigned_intent_id IS NULL OR NOT EXISTS (
                  SELECT 1
                  FROM intents AS assigned
                  WHERE assigned.project_id = cell.project_id
                    AND assigned.id = cell.assigned_intent_id
                    AND assigned.to_fact_id IS NULL
                    AND assigned.concluded_at IS NULL
              )
          )
        ORDER BY CASE WHEN cell.tier = 'core' THEN 0 ELSE 1 END,
                 cell.created_at,
                 cell.slot_key
        """,
        (project["id"],),
    ).fetchall()
    policy = policy_from_project_row(project)
    cells = [
        _cell_binding(
            row,
            cooldown_enabled=policy.blocker_cooldown_enabled,
            now=now,
        )
        for row in rows
    ]
    payload = {
        "version": 1,
        "assessment_phase": _assessment_phase(project),
        "issued_at": now,
        "eligible_cells": [
            cell.model_dump(mode="json", exclude_none=True) for cell in cells
        ],
    }
    binding = StructuredIntentBinding.model_validate(_with_digest(payload))
    return binding.model_dump(mode="json", exclude_none=True)


def parse_structured_intent_binding(value: object) -> StructuredIntentBinding:
    if not isinstance(value, dict):
        raise HTTPException(422, "structured_intent_binding_required")
    try:
        _validate_digest(value, "structured_intent_binding_invalid")
        return StructuredIntentBinding.model_validate(value)
    except HTTPException:
        raise
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(422, "structured_intent_binding_invalid") from exc


def validate_structured_intent_submission(
    conn: sqlite3.Connection,
    project: Mapping[str, Any],
    run_context: Mapping[str, Any],
    *,
    slot_key: str | None,
    strategy_key: str,
) -> StructuredCoverageCellBinding:
    """校验 Reason 输出属于冻结集合；当前状态仍由 assignment 事务最终裁决。"""
    binding = parse_structured_intent_binding(
        run_context.get("structured_intent_binding")
    )
    if binding.assessment_phase != _assessment_phase(project):
        raise HTTPException(409, "structured_intent_binding_stale")
    normalized_slot = str(slot_key or "").strip()
    normalized_strategy = str(strategy_key or "").strip().lower()
    if not normalized_slot or not normalized_strategy:
        raise HTTPException(422, "structured_intent_binding_required")
    matches = [cell for cell in binding.eligible_cells if cell.slot_key == normalized_slot]
    if len(matches) != 1:
        raise HTTPException(422, "structured_intent_binding_invalid")
    cell = matches[0]
    if (
        cell.strategy.mode == "must_differ"
        and normalized_strategy == cell.strategy.excluded_strategy_key
    ):
        raise HTTPException(409, "coverage_cell_blocker_cooldown_active")
    current = conn.execute(
        "SELECT * FROM coverage_slots WHERE project_id = ? AND slot_key = ?",
        (project["id"], normalized_slot),
    ).fetchone()
    if current is None:
        raise HTTPException(409, "structured_intent_binding_stale")
    current_cell = _cell_binding(
        current,
        cooldown_enabled=policy_from_project_row(project).blocker_cooldown_enabled,
        now=utcnow(),
    )
    stable_fields = (
        "slot_key",
        "surface_kind",
        "surface_key",
        "test_family",
        "applicability_basis",
        "completion_criteria",
        "coverage_requirement",
        "tier",
    )
    if any(getattr(current_cell, key) != getattr(cell, key) for key in stable_fields):
        raise HTTPException(409, "structured_intent_binding_stale")
    return cell


def _source_binding_digest(
    conn: sqlite3.Connection,
    project_id: str,
    intent: Mapping[str, Any],
) -> str | None:
    task_run_id = str(_row_value(intent, "task_run_id") or "").strip()
    if not task_run_id:
        return None
    row = conn.execute(
        "SELECT runtime_context_json FROM task_runs WHERE project_id = ? AND id = ?",
        (project_id, task_run_id),
    ).fetchone()
    if row is None:
        return None
    context = parse_scenario_snapshot(row["runtime_context_json"])
    value = context.get("structured_intent_binding")
    if not isinstance(value, dict):
        return None
    try:
        binding = parse_structured_intent_binding(value)
    except HTTPException:
        return None
    return binding.digest


def _validate_selected_depth_intent(
    conn: sqlite3.Connection,
    project_id: str,
    intent: Mapping[str, Any],
) -> None:
    fact_id = str(_row_value(intent, "depth_review_fact_id") or "").strip()
    if not fact_id:
        return
    row = conn.execute(
        "SELECT assessment_record_json FROM facts WHERE project_id = ? AND id = ?",
        (project_id, fact_id),
    ).fetchone()
    record = parse_scenario_snapshot(row["assessment_record_json"] if row else None)
    selected = record.get("selected")
    if not isinstance(selected, list):
        raise HTTPException(409, "structured_intent_binding_stale")
    matches = [
        item
        for item in selected
        if isinstance(item, dict)
        and item.get("slot_key") == _row_value(intent, "slot_key")
        and item.get("strategy_key") == _row_value(intent, "strategy_key")
    ]
    if len(matches) != 1:
        raise HTTPException(409, "structured_intent_binding_stale")


def admit_structured_explore_binding(
    conn: sqlite3.Connection,
    project: Mapping[str, Any],
    intent: Mapping[str, Any],
    *,
    admitted_at: str | None = None,
) -> dict[str, Any] | None:
    """在任何 Attempt/计费/claim 写入前验证并冻结一个可执行 Cell。"""
    policy = policy_from_project_row(project)
    if not policy.structured:
        return None
    role = str(_row_value(intent, "assessment_role", "discovery") or "discovery")
    if role == "verifier":
        return None
    slot_key = str(_row_value(intent, "slot_key") or "").strip()
    strategy_key = str(_row_value(intent, "strategy_key") or "").strip().lower()
    if not slot_key or not strategy_key:
        raise HTTPException(422, "structured_intent_binding_required")
    slot = conn.execute(
        "SELECT * FROM coverage_slots WHERE project_id = ? AND slot_key = ?",
        (project["id"], slot_key),
    ).fetchone()
    if slot is None:
        raise HTTPException(422, "structured_intent_binding_invalid")
    deep = bool(str(_row_value(intent, "depth_review_fact_id") or "").strip())
    if deep:
        _validate_selected_depth_intent(conn, project["id"], intent)
    else:
        if slot["status"] not in {"pending", "exploring"}:
            raise HTTPException(409, "structured_intent_binding_stale")
        if slot["assigned_intent_id"] != intent["id"]:
            raise HTTPException(409, "structured_intent_binding_stale")
    now = admitted_at or utcnow()
    cell = _cell_binding(
        slot,
        cooldown_enabled=policy.blocker_cooldown_enabled,
        now=now,
    )
    if (
        cell.strategy.mode == "must_differ"
        and strategy_key == cell.strategy.excluded_strategy_key
    ):
        raise HTTPException(409, "coverage_cell_blocker_cooldown_active")
    payload = {
        "version": 1,
        "assessment_phase": _assessment_phase(project),
        "admitted_at": now,
        "intent_id": intent["id"],
        "strategy_key": strategy_key,
        "cell": cell.model_dump(mode="json", exclude_none=True),
    }
    source_digest = _source_binding_digest(conn, project["id"], intent)
    if source_digest is not None:
        payload["source_binding_digest"] = source_digest
    binding = StructuredExploreBinding.model_validate(_with_digest(payload))
    return binding.model_dump(mode="json", exclude_none=True)


def parse_structured_explore_binding(value: object) -> StructuredExploreBinding:
    if not isinstance(value, dict):
        raise HTTPException(422, "structured_explore_binding_required")
    try:
        _validate_digest(value, "structured_explore_binding_invalid")
        return StructuredExploreBinding.model_validate(value)
    except HTTPException:
        raise
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(422, "structured_explore_binding_invalid") from exc
