"""default-copy 的确定性 Surface Manifest 与追加式发现记录。"""

from __future__ import annotations

from hashlib import sha256
import json
import sqlite3
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException

from cairn.server.models import EvidenceRef, SurfaceManifestEntry

from .common import dump_json
from .time_utils import utcnow


SURFACE_MANIFEST_VERSION = 1


def normalize_surface_key(surface_kind: str, surface_key: str) -> str:
    """保守规范化攻击面；只处理协议、主机、端口和 HTTP method。"""
    kind = surface_kind.strip().lower()
    text = " ".join(surface_key.strip().split())
    if not text:
        raise HTTPException(422, "surface_key must not be empty")

    if kind == "endpoint":
        parts = text.split(" ", 1)
        if len(parts) == 2 and parts[0].isalpha():
            method, target = parts[0].upper(), parts[1].strip()
            if "://" in target:
                try:
                    parsed = urlsplit(target)
                    host_value = parsed.hostname
                    port = parsed.port
                except ValueError as exc:
                    raise HTTPException(
                        422,
                        "surface_key contains an invalid endpoint URL",
                    ) from exc
                if parsed.scheme and host_value:
                    scheme = parsed.scheme.lower()
                    host = host_value.lower().rstrip(".")
                    authority_host = f"[{host}]" if ":" in host else host
                    default_port = (scheme == "http" and port == 80) or (
                        scheme == "https" and port == 443
                    )
                    authority = (
                        authority_host
                        if port is None or default_port
                        else f"{authority_host}:{port}"
                    )
                    path = parsed.path or "/"
                    target = urlunsplit((scheme, authority, path, parsed.query, ""))
            return f"{method} {target}"

    if kind in {"host", "service"}:
        return text.lower().rstrip(".")
    return text


def surface_manifest_id(surface_kind: str, surface_key: str) -> str:
    """规范化 Surface 身份并生成稳定 ID。"""
    kind = surface_kind.strip().lower()
    normalized_key = normalize_surface_key(kind, surface_key)
    identity = f"default-copy-surface-v1\n{kind}\n{normalized_key}"
    return f"surface:{sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def _plain_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(exclude_none=True))
    raise HTTPException(422, "surface_manifest_input_invalid")


def _normalized_evidence(values: Sequence[object]) -> list[dict[str, Any]]:
    evidence = [_plain_mapping(value) for value in values]
    return sorted(
        evidence,
        key=lambda item: (
            str(item.get("artifact_id") or ""),
            str(item.get("pointer") or ""),
            str(item.get("verifier_run_id") or ""),
        ),
    )


def register_surface_manifest_entry(
    conn,
    project_id: str,
    *,
    surface_kind: str,
    surface_key: str,
    description: str = "",
    traits: Mapping[str, Any] | None = None,
    evidence: Sequence[object] = (),
    source_intent_id: str | None = None,
    source_fact_id: str | None = None,
    source_task_run_id: str | None = None,
) -> str:
    """追加语义唯一的 Surface 观察，并保留 Fact/Run/Evidence 来源。"""
    normalized_kind = surface_kind.strip().lower()
    normalized_key = normalize_surface_key(normalized_kind, surface_key)
    surface_id = surface_manifest_id(normalized_kind, normalized_key)
    normalized_description = " ".join(description.split())
    normalized_traits = dict(traits or {})
    normalized_evidence = _normalized_evidence(evidence)
    observation_identity = "\n".join(
        (
            "default-copy-surface-observation-v1",
            surface_id,
            dump_json(normalized_traits),
            normalized_description,
            dump_json(normalized_evidence),
            source_intent_id or "",
            source_fact_id or "",
            source_task_run_id or "",
        )
    )
    observation_digest = sha256(observation_identity.encode("utf-8")).hexdigest()
    observation_fingerprint = f"surface-observation:{observation_digest}"
    entry_id = f"sme:{observation_digest[:24]}"
    conn.execute(
        """
        INSERT INTO surface_manifest_entries (
            entry_id, project_id, surface_id, surface_kind, surface_key,
            description, traits_json, evidence_json, source_intent_id,
            source_fact_id, source_task_run_id, observation_fingerprint,
            manifest_version, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'observed', ?)
        ON CONFLICT(project_id, observation_fingerprint) DO NOTHING
        """,
        (
            entry_id,
            project_id,
            surface_id,
            normalized_kind,
            normalized_key,
            normalized_description,
            dump_json(normalized_traits),
            dump_json(normalized_evidence),
            source_intent_id,
            source_fact_id,
            source_task_run_id,
            observation_fingerprint,
            SURFACE_MANIFEST_VERSION,
            utcnow(),
        ),
    )
    return surface_id


def register_discovered_surfaces(
    conn,
    project_id: str,
    surfaces: Sequence[object],
    *,
    tier: str,
    source_intent_id: str | None = None,
    source_fact_id: str | None = None,
    source_task_run_id: str | None = None,
    fallback_evidence: Sequence[object] = (),
    allow_stopping_settlement: bool = False,
) -> list[str]:
    """先登记权威 Surface，再由显式适用性生成兼容 Coverage Cells。"""
    from cairn.server.models import DiscoveredSurfaceInput

    from .assessment_policy import policy_from_project_row
    from .coverage_slots import upsert_coverage_slots
    from .projects import (
        validate_artifacts_exist,
        validate_execution_generation,
        validate_facts_exist,
        validate_task_run_exists,
    )

    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        raise HTTPException(404, "Project not found")
    policy = policy_from_project_row(project)
    if not policy.surface_manifest_enabled:
        raise HTTPException(409, "surface_manifest_not_enabled")
    if source_intent_id is not None:
        intent = conn.execute(
            "SELECT 1 FROM intents WHERE id = ? AND project_id = ?",
            (source_intent_id, project_id),
        ).fetchone()
        if intent is None:
            raise HTTPException(404, f"Intent {source_intent_id} not found")
    if source_fact_id is not None:
        validate_facts_exist(conn, project_id, [source_fact_id])
    if source_task_run_id is not None:
        validate_task_run_exists(conn, project_id, source_task_run_id)
        validate_execution_generation(
            conn,
            project_id,
            source_task_run_id,
            allow_stopping_settlement=allow_stopping_settlement,
        )

    fallback_refs = [
        EvidenceRef.model_validate(_plain_mapping(value)) for value in fallback_evidence
    ]
    validate_artifacts_exist(conn, project_id, fallback_refs)

    surface_ids: list[str] = []
    cells: list[dict[str, Any]] = []
    for value in surfaces:
        surface = DiscoveredSurfaceInput.model_validate(
            _plain_mapping(value)
        ).model_dump(exclude_none=True)
        kind = str(surface.get("surface_kind") or "").strip().lower()
        key = str(surface.get("surface_key") or "").strip()
        normalized_key = normalize_surface_key(kind, key)
        evidence = surface.get("evidence_refs") or fallback_refs
        evidence_refs = [
            EvidenceRef.model_validate(_plain_mapping(value)) for value in evidence
        ]
        validate_artifacts_exist(conn, project_id, evidence_refs)
        surface_id = register_surface_manifest_entry(
            conn,
            project_id,
            surface_kind=kind,
            surface_key=normalized_key,
            description=str(surface.get("description") or ""),
            traits=_plain_mapping(surface.get("traits") or {}),
            evidence=evidence_refs,
            source_intent_id=source_intent_id,
            source_fact_id=source_fact_id,
            source_task_run_id=source_task_run_id,
        )
        surface_ids.append(surface_id)
        if policy.minimum_coverage_enabled:
            from .minimum_coverage import derive_coverage_cells

            derived = derive_coverage_cells(
                surface_id=surface_id,
                surface_kind=kind,
                surface_key=normalized_key,
                description=str(surface.get("description") or ""),
                traits=_plain_mapping(surface.get("traits") or {}),
                applicable_tests=surface.get("applicable_tests") or [],
                rule_version=policy.minimum_coverage_version,
            )
            cells.extend(
                {
                    "kind": cell.surface_kind,
                    "surface_id": cell.surface_id,
                    "surface_kind": cell.surface_kind,
                    "surface_key": cell.surface_key,
                    "description": cell.description,
                    "test_family": cell.test_family,
                    "coverage_requirement": cell.coverage_requirement,
                    "coverage_rule_id": cell.coverage_rule_id,
                    "applicability_basis": cell.applicability_basis,
                    "completion_criteria": cell.completion_criteria,
                }
                for cell in derived
            )
            continue
        for test_value in surface.get("applicable_tests") or []:
            test = _plain_mapping(test_value)
            cells.append(
                {
                    "kind": kind,
                    "surface_kind": kind,
                    "surface_key": normalized_key,
                    "description": str(test.get("description") or surface.get("description") or ""),
                    "test_family": str(test.get("test_family") or ""),
                    "applicability_basis": str(test.get("applicability_basis") or ""),
                    "completion_criteria": str(test.get("completion_criteria") or ""),
                }
            )
    if cells:
        upsert_coverage_slots(
            conn,
            project_id,
            cells,
            source_intent_id=source_intent_id,
            tier=tier,
            register_manifest_observation=False,
        )
    return surface_ids


def surface_manifest_entry_from_row(row) -> SurfaceManifestEntry:
    """将数据库 JSON 字段转换为稳定的 Manifest 观察模型。"""
    return SurfaceManifestEntry(
        entry_id=row["entry_id"],
        project_id=row["project_id"],
        surface_id=row["surface_id"],
        surface_kind=row["surface_kind"],
        surface_key=row["surface_key"],
        description=row["description"],
        traits=json.loads(row["traits_json"] or "{}"),
        evidence=json.loads(row["evidence_json"] or "[]"),
        source_intent_id=row["source_intent_id"],
        source_fact_id=row["source_fact_id"],
        source_task_run_id=row["source_task_run_id"],
        observation_fingerprint=row["observation_fingerprint"],
        manifest_version=int(row["manifest_version"]),
        status=row["status"],
        created_at=row["created_at"],
    )


def list_surface_manifest_entries(conn, project_id: str) -> list[SurfaceManifestEntry]:
    """按 Surface 与观察时间稳定列出项目的追加式 Manifest。"""
    rows = conn.execute(
        """
        SELECT *
        FROM surface_manifest_entries
        WHERE project_id = ?
        ORDER BY surface_id, created_at, entry_id
        """,
        (project_id,),
    ).fetchall()
    return [surface_manifest_entry_from_row(row) for row in rows]


def surface_manifest_digest(conn: sqlite3.Connection, project_id: str) -> str:
    """按稳定 Surface ID 计算顺序无关的 Manifest 摘要。"""
    rows = conn.execute(
        """
        SELECT DISTINCT surface_id
        FROM surface_manifest_entries
        WHERE project_id = ?
        ORDER BY surface_id
        """,
        (project_id,),
    ).fetchall()
    identity = "default-copy-surface-manifest-v1\n" + "\n".join(
        str(row["surface_id"]) for row in rows
    )
    return f"manifest:{sha256(identity.encode('utf-8')).hexdigest()}"
