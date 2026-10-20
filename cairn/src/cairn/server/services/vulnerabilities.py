"""漏洞管理服务：所有场景通用的漏洞 CRUD + 自动确认 + pentest verified 升级。

worker 输出 fact 带 vulnerability 字段时由 conclude 端点自动确认入库；
pentest slot verified 时升级该 fact 的漏洞为 confirmed；支持人工独立创建。
"""

from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

from cairn.evidence_contracts import EvidenceItem, evidence_refs
from cairn.server.auth import RequestPrincipal, verification_project_access_clause
from cairn.server.models import (
    CreateVulnerabilityRequest,
    UpdateVulnerabilityRequest,
    Vulnerability,
    VulnerabilityDetail,
    VulnerabilityEvidenceArtifact,
    VulnerabilityIdentityInput,
    Fact,
    VulnerabilityListItem,
    VulnerabilityObservation,
    VulnerabilityProvenance,
    VulnerabilityProvenanceRelation,
    VulnerabilitySourceFact,
)

from .artifacts import artifact_to_model, fact_evidence_to_model
from .common import *
from .ids import lock_verification_project_write, next_vulnerability_id
from .projects import intent_to_model, task_run_to_model
from .time_utils import utcnow
from .vulnerability_identity import (
    canonicalize_vulnerability_identity,
    vulnerability_fingerprint,
)
from .vulnerability_verifications import (
    list_vulnerability_verifications,
    schedule_observation_verifier,
)


def _evidence_items_from_row(row: sqlite3.Row) -> list[EvidenceItem]:
    return [EvidenceItem.model_validate(item) for item in json.loads(row["evidence_items_json"])]


def read_vulnerability_evidence(
    conn: sqlite3.Connection,
    project_id: str,
    vuln_id: str,
    observation_id: str | None = None,
) -> dict[str, Any]:
    """一次只选择一个来源；缺失报文不从其他发现或 canonical 回填。"""
    vulnerability = get_vulnerability_or_404(conn, project_id, vuln_id)
    params = [project_id, vuln_id]
    selected = ""
    if observation_id is not None:
        selected = " AND id = ?"
        params.append(observation_id)
    observation = conn.execute(
        "SELECT * FROM vulnerability_observations "
        "WHERE project_id = ? AND vulnerability_id = ?" + selected
        + " ORDER BY observed_at DESC, id DESC LIMIT 1",
        params,
    ).fetchone()
    if observation_id is not None and observation is None:
        raise HTTPException(404, "vulnerability_observation_not_found")
    row = observation if observation is not None else vulnerability
    items = _evidence_items_from_row(row)
    source = items[0].source if items else (
        "capture" if observation is not None else (
            "historical" if vulnerability["source_fact_id"] or vulnerability["discovered_by_run_id"] else "manual"
        )
    )
    return {
        "observation_id": observation["id"] if observation is not None else None,
        "source": source,
        "evidence_items": [item.model_dump(exclude_none=True) for item in items],
    }


def vulnerability_from_row(row: sqlite3.Row) -> Vulnerability:
    identity: dict[str, Any] = {}
    if "identity_json" in row.keys() and row["identity_json"]:
        try:
            parsed = json.loads(row["identity_json"])
            if isinstance(parsed, dict):
                identity = parsed
        except (TypeError, ValueError):
            identity = {}
    return Vulnerability(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        severity=row["severity"],
        vuln_type=row["vuln_type"] or "",
        target=row["target"] or "",
        confidence=row["confidence"],
        status=row["status"],
        source_fact_id=row["source_fact_id"],
        evidence_items=_evidence_items_from_row(row),
        remediation=row["remediation"] or "",
        # repro_steps 为加性列，旧库迁移前查询兜底空串。
        repro_steps=row["repro_steps"] if "repro_steps" in row.keys() else "",
        discovered_by_run_id=row["discovered_by_run_id"],
        discovered_by_worker=row["discovered_by_worker"],
        slot_key=row["slot_key"],
        fingerprint=(
            row["fingerprint"] if "fingerprint" in row.keys() else None
        ),
        identity=identity,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_vulnerability_or_404(conn: sqlite3.Connection, project_id: str, vuln_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM vulnerabilities WHERE id = ? AND project_id = ?",
        (vuln_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Vulnerability not found")
    return row


def _observation_from_row(row: sqlite3.Row) -> VulnerabilityObservation:
    identity: dict[str, Any] = {}
    try:
        parsed = json.loads(row["identity_json"] or "{}")
        if isinstance(parsed, dict):
            identity = parsed
    except (TypeError, ValueError):
        pass
    return VulnerabilityObservation(
        id=row["id"],
        source_fact_id=row["source_fact_id"],
        task_run_id=row["task_run_id"],
        worker=row["worker"],
        slot_key=row["slot_key"],
        evidence_items=_evidence_items_from_row(row),
        identity=identity,
        severity=row["severity"],
        confidence=row["confidence"],
        observed_at=row["observed_at"],
    )


def _artifact_filename(uri: str) -> str:
    """从 artifact uri 提取展示用文件名（uri 形如 artifact://.../<filename>）。"""
    if not uri:
        return ""
    return uri.rsplit("/", 1)[-1] if "/" in uri else uri


def _evidence_artifact_from_row(row: sqlite3.Row, *, source: str, pointer: str = "") -> VulnerabilityEvidenceArtifact:
    return VulnerabilityEvidenceArtifact(
        id=row["id"],
        kind=row["kind"],
        filename=_artifact_filename(row["uri"]),
        pointer=pointer,
        source=source,
    )


def get_vulnerability_detail(
    conn: sqlite3.Connection,
    project_id: str,
    vuln_id: str,
    observation_id: str | None = None,
) -> VulnerabilityDetail:
    """默认主证据来自最新真实 Observation；其他历史材料保留独立入口。"""
    row = get_vulnerability_or_404(conn, project_id, vuln_id)
    detail = VulnerabilityDetail(**vulnerability_from_row(row).model_dump())
    detail.record_evidence_items = detail.evidence_items
    selected = read_vulnerability_evidence(conn, project_id, vuln_id, observation_id)
    detail.observation_id = selected["observation_id"]
    detail.source = selected["source"]
    detail.evidence_items = [EvidenceItem.model_validate(item) for item in selected["evidence_items"]]

    selected_observation = (
        conn.execute(
            "SELECT * FROM vulnerability_observations WHERE project_id = ? AND id = ?",
            (project_id, detail.observation_id),
        ).fetchone()
        if detail.observation_id is not None else None
    )
    source_fact_id = (selected_observation if selected_observation is not None else row)["source_fact_id"]
    if source_fact_id:
        fact_row = conn.execute(
            "SELECT id, description, task_run_id FROM facts WHERE id = ? AND project_id = ?",
            (source_fact_id, project_id),
        ).fetchone()
        if fact_row is not None:
            evidence_rows = conn.execute(
                """
                SELECT a.id, a.kind, a.uri, fe.pointer
                FROM fact_evidence fe
                JOIN artifacts a ON a.id = fe.artifact_id AND a.project_id = fe.project_id
                WHERE fe.fact_id = ? AND fe.project_id = ?
                ORDER BY fe.created_at, a.id
                """,
                (source_fact_id, project_id),
            ).fetchall()
            evidence = [
                _evidence_artifact_from_row(er, source="fact_evidence", pointer=er["pointer"] or "")
                for er in evidence_rows
            ]
            detail.source_fact = VulnerabilitySourceFact(
                id=fact_row["id"],
                description=fact_row["description"],
                task_run_id=fact_row["task_run_id"],
                evidence=evidence,
            )

    observation_rows = conn.execute(
        """
        SELECT * FROM vulnerability_observations
        WHERE project_id = ? AND vulnerability_id = ?
        ORDER BY observed_at, id
        """,
        (project_id, vuln_id),
    ).fetchall()
    detail.observations = [
        _observation_from_row(observation) for observation in observation_rows
    ]
    detail.verifications = list_vulnerability_verifications(
        conn,
        project_id,
        vuln_id,
    )

    run_id = (
        selected_observation["task_run_id"] if selected_observation is not None
        else row["discovered_by_run_id"]
    )
    if run_id:
        # 产出 run 的 stdout/diagnostic 日志：完整 worker 交互，含复现命令与响应
        run_artifact_rows = conn.execute(
            """
            SELECT id, kind, uri FROM artifacts
            WHERE task_run_id = ? AND project_id = ?
              AND (kind = 'raw_output' OR uri LIKE ? OR uri LIKE ?)
            ORDER BY created_at, id
            """,
            (run_id, project_id, "%stdout%", "%diagnostic%"),
        ).fetchall()
        detail.run_artifacts = [
            _evidence_artifact_from_row(ar, source="discovered_run")
            for ar in run_artifact_rows
        ]

    return detail


def get_vulnerability_provenance(
    conn: sqlite3.Connection,
    project_id: str,
    vuln_id: str,
) -> VulnerabilityProvenance:
    """返回项目内 canonical 漏洞及其可追溯的发现、验证与证据关系。"""
    vulnerability = vulnerability_from_row(
        get_vulnerability_or_404(conn, project_id, vuln_id)
    )
    observation_rows = conn.execute(
        """
        SELECT * FROM vulnerability_observations
        WHERE project_id = ? AND vulnerability_id = ?
        ORDER BY observed_at, id
        """,
        (project_id, vuln_id),
    ).fetchall()
    observations = [_observation_from_row(row) for row in observation_rows]
    verifications = list_vulnerability_verifications(conn, project_id, vuln_id)

    fact_ids = {
        value
        for value in (
            vulnerability.source_fact_id,
            *(item.source_fact_id for item in observations),
            *(item.fact_id for item in verifications),
        )
        if value
    }
    run_ids = {
        value
        for value in (
            vulnerability.discovered_by_run_id,
            *(item.task_run_id for item in observations),
            *(item.discovery_task_run_id for item in verifications),
            *(item.verifier_task_run_id for item in verifications),
            *(
                ref.verifier_run_id
                for item in verifications
                for ref in item.evidence
            ),
        )
        if value
    }
    intent_ids = {item.intent_id for item in verifications}
    observation_ids = {item.id for item in observations}
    discovery_refs = evidence_refs([
        *vulnerability.evidence_items,
        *(item for observation in observations for item in observation.evidence_items),
    ])
    artifact_ids = {
        ref.artifact_id
        for ref in [*discovery_refs, *(ref for item in verifications for ref in item.evidence)]
    }
    run_ids.update(ref.verifier_run_id for ref in discovery_refs if ref.verifier_run_id)

    fact_rows: list[sqlite3.Row] = []
    run_rows: list[sqlite3.Row] = []
    intent_rows: list[sqlite3.Row] = []
    evidence_rows: list[sqlite3.Row] = []
    while True:
        before = (frozenset(fact_ids), frozenset(run_ids), frozenset(intent_ids))

        if fact_ids:
            placeholders = ", ".join("?" for _ in fact_ids)
            fact_rows = conn.execute(
                f"""
                SELECT * FROM facts
                WHERE project_id = ? AND id IN ({placeholders})
                ORDER BY created_at, id
                """,
                (project_id, *sorted(fact_ids)),
            ).fetchall()
        else:
            fact_rows = []
        run_ids.update(row["task_run_id"] for row in fact_rows if row["task_run_id"])

        if run_ids:
            placeholders = ", ".join("?" for _ in run_ids)
            run_rows = conn.execute(
                f"""
                SELECT * FROM task_runs
                WHERE project_id = ? AND id IN ({placeholders})
                ORDER BY COALESCE(started_at, ended_at, ''), id
                """,
                (project_id, *sorted(run_ids)),
            ).fetchall()
        else:
            run_rows = []
        intent_ids.update(row["intent_id"] for row in run_rows if row["intent_id"])

        intent_clauses: list[str] = []
        intent_params: list[str] = [project_id]
        if intent_ids:
            placeholders = ", ".join("?" for _ in intent_ids)
            intent_clauses.append(f"id IN ({placeholders})")
            intent_params.extend(sorted(intent_ids))
        if fact_ids:
            placeholders = ", ".join("?" for _ in fact_ids)
            intent_clauses.append(f"to_fact_id IN ({placeholders})")
            intent_params.extend(sorted(fact_ids))
        if observation_ids:
            placeholders = ", ".join("?" for _ in observation_ids)
            intent_clauses.append(f"target_observation_id IN ({placeholders})")
            intent_params.extend(sorted(observation_ids))
        if intent_clauses:
            intent_rows = conn.execute(
                f"""
                SELECT * FROM intents
                WHERE project_id = ? AND ({' OR '.join(intent_clauses)})
                ORDER BY created_at, id
                """,
                intent_params,
            ).fetchall()
        else:
            intent_rows = []
        intent_ids.update(row["id"] for row in intent_rows)
        fact_ids.update(row["to_fact_id"] for row in intent_rows if row["to_fact_id"])
        run_ids.update(row["task_run_id"] for row in intent_rows if row["task_run_id"])
        if intent_ids:
            placeholders = ", ".join("?" for _ in intent_ids)
            source_rows = conn.execute(
                f"""
                SELECT fact_id FROM intent_sources
                WHERE project_id = ? AND intent_id IN ({placeholders})
                ORDER BY intent_id, source_order, fact_id
                """,
                (project_id, *sorted(intent_ids)),
            ).fetchall()
            fact_ids.update(row["fact_id"] for row in source_rows)

        if fact_ids:
            placeholders = ", ".join("?" for _ in fact_ids)
            evidence_rows = conn.execute(
                f"""
                SELECT fe.*,
                       CASE WHEN a.id IS NULL THEN 0 ELSE 1 END AS artifact_exists,
                       CASE
                           WHEN fe.verifier_run_id IS NULL THEN NULL
                           WHEN tr.id IS NULL THEN 0
                           ELSE 1
                       END AS verifier_run_exists
                FROM fact_evidence fe
                LEFT JOIN artifacts a
                  ON a.project_id = fe.project_id AND a.id = fe.artifact_id
                LEFT JOIN task_runs tr
                  ON tr.project_id = fe.project_id AND tr.id = fe.verifier_run_id
                WHERE fe.project_id = ? AND fe.fact_id IN ({placeholders})
                ORDER BY fe.created_at, fe.fact_id, fe.artifact_id, fe.pointer
                """,
                (project_id, *sorted(fact_ids)),
            ).fetchall()
        else:
            evidence_rows = []
        artifact_ids.update(row["artifact_id"] for row in evidence_rows)
        run_ids.update(
            row["verifier_run_id"] for row in evidence_rows if row["verifier_run_id"]
        )

        after = (frozenset(fact_ids), frozenset(run_ids), frozenset(intent_ids))
        if after == before:
            break

    facts = [
        Fact(
            id=row["id"],
            description=row["description"],
            task_run_id=row["task_run_id"],
            creator=row["creator"],
            created_at=row["created_at"],
        )
        for row in fact_rows
    ]
    task_runs = [task_run_to_model(row) for row in run_rows]
    intents = [intent_to_model(conn, row, project_id) for row in intent_rows]
    evidence = [fact_evidence_to_model(row) for row in evidence_rows]

    artifact_clauses: list[str] = []
    artifact_params: list[str] = [project_id]
    if artifact_ids:
        placeholders = ", ".join("?" for _ in artifact_ids)
        artifact_clauses.append(f"id IN ({placeholders})")
        artifact_params.extend(sorted(artifact_ids))
    if run_ids:
        placeholders = ", ".join("?" for _ in run_ids)
        artifact_clauses.append(f"task_run_id IN ({placeholders})")
        artifact_params.extend(sorted(run_ids))
    artifact_rows = (
        conn.execute(
            f"""
            SELECT * FROM artifacts
            WHERE project_id = ? AND ({' OR '.join(artifact_clauses)})
            ORDER BY created_at, id
            """,
            artifact_params,
        ).fetchall()
        if artifact_clauses
        else []
    )
    artifacts = [artifact_to_model(row) for row in artifact_rows]

    relations: list[VulnerabilityProvenanceRelation] = []
    relation_keys: set[tuple[str, str, str, str]] = set()

    def add_relation(
        source_id: str,
        target_id: str | None,
        relation: str,
        source: str,
    ) -> None:
        if not target_id:
            return
        key = (source_id, target_id, relation, source)
        if key in relation_keys:
            return
        relation_keys.add(key)
        relations.append(
            VulnerabilityProvenanceRelation(
                source_id=source_id,
                target_id=target_id,
                relation=relation,
                source=source,
            )
        )

    add_relation(
        vuln_id,
        vulnerability.source_fact_id,
        "sourced_from_fact",
        "vulnerabilities.source_fact_id",
    )
    add_relation(
        vuln_id,
        vulnerability.discovered_by_run_id,
        "discovered_by_run",
        "vulnerabilities.discovered_by_run_id",
    )
    for ref in evidence_refs(vulnerability.evidence_items):
        add_relation(vuln_id, ref.artifact_id, "supported_by_artifact", "vulnerabilities.evidence_items_json")
    for observation in observations:
        add_relation(
            vuln_id,
            observation.id,
            "has_observation",
            "vulnerability_observations.vulnerability_id",
        )
        add_relation(
            observation.id,
            observation.source_fact_id,
            "sourced_from_fact",
            "vulnerability_observations.source_fact_id",
        )
        add_relation(
            observation.id,
            observation.task_run_id,
            "discovered_by_run",
            "vulnerability_observations.task_run_id",
        )
        for ref in evidence_refs(observation.evidence_items):
            add_relation(observation.id, ref.artifact_id, "supported_by_artifact", "vulnerability_observations.evidence_items_json")
    for verification in verifications:
        add_relation(
            verification.id,
            vuln_id,
            "verifies_vulnerability",
            "vulnerability_verifications.vulnerability_id",
        )
        add_relation(
            verification.id,
            verification.observation_id,
            "verifies_observation",
            "vulnerability_verifications.observation_id",
        )
        add_relation(
            verification.id,
            verification.intent_id,
            "requested_by_intent",
            "vulnerability_verifications.intent_id",
        )
        add_relation(
            verification.id,
            verification.fact_id,
            "concluded_as_fact",
            "vulnerability_verifications.fact_id",
        )
        add_relation(
            verification.id,
            verification.discovery_task_run_id,
            "compares_discovery_run",
            "vulnerability_verifications.discovery_task_run_id",
        )
        add_relation(
            verification.id,
            verification.verifier_task_run_id,
            "performed_by_run",
            "vulnerability_verifications.verifier_task_run_id",
        )
        for ref in verification.evidence:
            add_relation(
                verification.id,
                ref.artifact_id,
                "supported_by_artifact",
                "vulnerability_verifications.evidence_json",
            )
            add_relation(
                verification.id,
                ref.verifier_run_id,
                "evidence_verified_by_run",
                "vulnerability_verifications.evidence_json",
            )
    for fact in facts:
        add_relation(fact.id, fact.task_run_id, "produced_by_run", "facts.task_run_id")
    for intent in intents:
        for source_fact_id in intent.from_:
            add_relation(
                intent.id,
                source_fact_id,
                "uses_source_fact",
                "intent_sources.fact_id",
            )
        add_relation(intent.id, intent.to, "concluded_as_fact", "intents.to_fact_id")
        add_relation(
            intent.id,
            intent.task_run_id,
            "executed_by_run",
            "intents.task_run_id",
        )
        add_relation(
            intent.id,
            intent.target_observation_id,
            "targets_observation",
            "intents.target_observation_id",
        )
    for run in task_runs:
        add_relation(run.id, run.intent_id, "executes_intent", "task_runs.intent_id")
    for artifact in artifacts:
        add_relation(
            artifact.id,
            artifact.task_run_id,
            "produced_by_run",
            "artifacts.task_run_id",
        )
    for item in evidence:
        add_relation(
            item.fact_id,
            item.artifact_id,
            "supported_by_artifact",
            "fact_evidence.artifact_id",
        )
        add_relation(
            item.fact_id,
            item.verifier_run_id,
            "evidence_verified_by_run",
            "fact_evidence.verifier_run_id",
        )

    loaded_fact_ids = {item.id for item in facts}
    loaded_intent_ids = {item.id for item in intents}
    loaded_run_ids = {item.id for item in task_runs}
    loaded_artifact_ids = {item.id for item in artifacts}
    warnings: list[str] = []

    def warn_missing(kind: str, owner: str, target_id: str | None, loaded: set[str]) -> None:
        if target_id and target_id not in loaded:
            warnings.append(f"{owner} references missing {kind} {target_id}.")

    if not observations:
        warnings.append(f"Vulnerability {vuln_id} has no discovery observations.")
    warn_missing("fact", f"Vulnerability {vuln_id}", vulnerability.source_fact_id, loaded_fact_ids)
    warn_missing("task run", f"Vulnerability {vuln_id}", vulnerability.discovered_by_run_id, loaded_run_ids)
    for ref in evidence_refs(vulnerability.evidence_items):
        warn_missing("artifact", f"Vulnerability {vuln_id}", ref.artifact_id, loaded_artifact_ids)
    for observation in observations:
        owner = f"Observation {observation.id}"
        warn_missing("fact", owner, observation.source_fact_id, loaded_fact_ids)
        warn_missing("task run", owner, observation.task_run_id, loaded_run_ids)
        for ref in evidence_refs(observation.evidence_items):
            warn_missing("artifact", owner, ref.artifact_id, loaded_artifact_ids)
    for verification in verifications:
        owner = f"Verification {verification.id}"
        warn_missing("observation", owner, verification.observation_id, observation_ids)
        warn_missing("intent", owner, verification.intent_id, loaded_intent_ids)
        warn_missing("fact", owner, verification.fact_id, loaded_fact_ids)
        warn_missing("task run", owner, verification.discovery_task_run_id, loaded_run_ids)
        warn_missing("task run", owner, verification.verifier_task_run_id, loaded_run_ids)
        for ref in verification.evidence:
            warn_missing("artifact", owner, ref.artifact_id, loaded_artifact_ids)
            warn_missing("task run", owner, ref.verifier_run_id, loaded_run_ids)
    for item in evidence:
        if not item.artifact_exists:
            warnings.append(
                f"Fact evidence {item.fact_id} references missing artifact {item.artifact_id}."
            )
        if item.verifier_run_exists is False:
            warnings.append(
                f"Fact evidence {item.fact_id} references missing task run {item.verifier_run_id}."
            )

    return VulnerabilityProvenance(
        vulnerability=vulnerability,
        observations=observations,
        verifications=verifications,
        facts=facts,
        intents=intents,
        task_runs=task_runs,
        artifacts=artifacts,
        evidence=evidence,
        relations=relations,
        completeness_warnings=list(dict.fromkeys(warnings)),
    )


def list_all_vulnerabilities(
    conn: sqlite3.Connection,
    *,
    tenant_id: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    principal: RequestPrincipal | None = None,
) -> list[VulnerabilityListItem]:
    """跨项目成果列表：聚合所有项目的漏洞并附带项目标题，供主菜单"成果列表"展示。"""
    clauses: list[str] = []
    params: list = []
    if principal is not None:
        access_clause, access_params = verification_project_access_clause(principal)
        clauses.append(access_clause)
        params.extend(access_params)
    if tenant_id is not None:
        clauses.append("p.tenant_id = ?")
        params.append(tenant_id)
    if severity:
        clauses.append("v.severity = ?")
        params.append(severity)
    if status:
        clauses.append("v.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT v.*, p.title AS project_title
        FROM vulnerabilities v
        JOIN projects p ON p.id = v.project_id
        {where}
        ORDER BY v.created_at DESC, v.id DESC
        """,
        params,
    ).fetchall()
    items: list[VulnerabilityListItem] = []
    for row in rows:
        base = vulnerability_from_row(row).model_dump()
        base["project_title"] = row["project_title"] or ""
        items.append(VulnerabilityListItem(**base))
    return items


def list_vulnerabilities(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    severity: str | None = None,
    status: str | None = None,
) -> list[Vulnerability]:
    clauses = ["project_id = ?"]
    params: list = [project_id]
    if severity:
        clauses.append("severity = ?")
        params.append(severity)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT * FROM vulnerabilities WHERE {where} ORDER BY created_at DESC, id DESC",
        params,
    ).fetchall()
    return [vulnerability_from_row(row) for row in rows]


def _validate_evidence_items(
    conn: sqlite3.Connection,
    project_id: str,
    items: list[EvidenceItem],
    *,
    discovery: bool = False,
    task_run_id: str | None = None,
    preserved_items: list[EvidenceItem] | None = None,
) -> None:
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "evidence_item_id_must_be_unique")
    if discovery and any(
        item.source == "historical"
        or any(value is not None for value in (item.request_text, item.response_text, item.text))
        for item in items
    ):
        raise HTTPException(422, "new_discovery_requires_artifact_evidence")
    # 原样保留的既有条目不是新输入；原件已缺失时仍须保留其真实历史引用。
    refs = evidence_refs(item for item in items if item not in (preserved_items or []))
    for ref in refs:
        row = conn.execute(
            "SELECT task_run_id FROM artifacts WHERE project_id = ? AND id = ?",
            (project_id, ref.artifact_id),
        ).fetchone()
        if row is None:
            raise HTTPException(422, f"Artifact {ref.artifact_id} not found in project")
        if discovery and (task_run_id is None or row["task_run_id"] != task_run_id):
            raise HTTPException(422, "discovery_evidence_current_run_required")


def _insert_discovery_observation(
    conn: sqlite3.Connection,
    project_id: str,
    vulnerability_id: str,
    body: CreateVulnerabilityRequest,
    *,
    identity_json: str,
    task_run_id: str | None,
    worker: str | None,
    observed_at: str,
) -> str:
    observation_id = f"vo_{uuid4().hex}"
    conn.execute(
        """
        INSERT INTO vulnerability_observations (
            id, project_id, vulnerability_id, source_fact_id,
            task_run_id, worker, slot_key, identity_json,
            severity, confidence, evidence_items_json, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id, project_id, vulnerability_id, body.source_fact_id,
            task_run_id, worker, body.slot_key, identity_json,
            body.severity, body.confidence,
            dump_json([item.model_dump(exclude_none=True) for item in body.evidence_items]),
            observed_at,
        ),
    )
    return observation_id


def create_vulnerability(
    conn: sqlite3.Connection,
    project_id: str,
    body: CreateVulnerabilityRequest,
    *,
    discovered_by_run_id: str | None = None,
    discovered_by_worker: str | None = None,
) -> Vulnerability:
    # 自动确认时 source_fact_id/slot_key 由 body 带入；人工创建两者为 None。
    # source_fact_id 存在则校验 fact 属于本项目（防悬空引用）。
    if body.source_fact_id is not None:
        fact_row = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?",
            (body.source_fact_id, project_id),
        ).fetchone()
        if fact_row is None:
            raise HTTPException(422, f"Fact {body.source_fact_id} not found in project")
    discovery = body.source_fact_id is not None
    _validate_evidence_items(
        conn, project_id, body.evidence_items,
        discovery=discovery, task_run_id=discovered_by_run_id,
    )
    if not discovery and any(item.source != "manual" for item in body.evidence_items):
        raise HTTPException(422, "manual_vulnerability_requires_manual_evidence")
    vid = next_vulnerability_id(conn, project_id)
    now = utcnow()
    # 自动确认（有 source_fact_id）默认 status=new，由 pentest verified 路径升级 confirmed；
    # 人工独立创建也默认 new，由人工后续流转。
    conn.execute(
        """
        INSERT INTO vulnerabilities
            (id, project_id, title, severity, vuln_type, target, confidence, status,
             source_fact_id, remediation, evidence_items_json,
             discovered_by_run_id, discovered_by_worker, slot_key, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            vid, project_id, body.title, body.severity, body.vuln_type, body.target,
            body.confidence, body.source_fact_id, body.remediation,
            dump_json([] if discovery else [item.model_dump(exclude_none=True) for item in body.evidence_items]),
            discovered_by_run_id, discovered_by_worker, body.slot_key, now, now,
        ),
    )
    if discovery:
        _insert_discovery_observation(
            conn, project_id, vid, body, identity_json="{}",
            task_run_id=discovered_by_run_id, worker=discovered_by_worker, observed_at=now,
        )
    row = get_vulnerability_or_404(conn, project_id, vid)
    return vulnerability_from_row(row)


_SEVERITY_RANK = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


def _get_vulnerability_for_update(conn, project_id: str, vulnerability_id: str):
    """PostgreSQL 锁定 canonical 行；SQLite 由当前写事务提供串行化。"""
    suffix = "" if isinstance(conn, sqlite3.Connection) else " FOR UPDATE"
    row = conn.execute(
        """
        SELECT * FROM vulnerabilities
        WHERE id = ? AND project_id = ?
        """ + suffix,
        (vulnerability_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Vulnerability not found")
    return row


def register_discovered_vulnerability(
    conn: sqlite3.Connection,
    project_id: str,
    body: CreateVulnerabilityRequest,
    identity: VulnerabilityIdentityInput,
    *,
    fingerprint_version: int,
    discovered_by_run_id: str | None = None,
    discovered_by_worker: str | None = None,
) -> Vulnerability:
    """按语义指纹合并 canonical 漏洞，并为每次发现追加 observation。"""
    if body.source_fact_id is None:
        raise HTTPException(422, "Structured vulnerability requires source_fact_id")
    fact_row = conn.execute(
        "SELECT 1 FROM facts WHERE id = ? AND project_id = ?",
        (body.source_fact_id, project_id),
    ).fetchone()
    if fact_row is None:
        raise HTTPException(
            422,
            f"Fact {body.source_fact_id} not found in project",
        )
    _validate_evidence_items(
        conn, project_id, body.evidence_items,
        discovery=True, task_run_id=discovered_by_run_id,
    )

    canonical_identity = canonicalize_vulnerability_identity(
        identity,
        version=fingerprint_version,
    )
    fingerprint = vulnerability_fingerprint(canonical_identity)
    identity_json = dump_json(canonical_identity)
    now = utcnow()
    candidate_id = next_vulnerability_id(conn, project_id)
    inserted = conn.execute(
        """
        INSERT INTO vulnerabilities (
            id, project_id, title, severity, vuln_type, target,
            confidence, status, source_fact_id, remediation,
            discovered_by_run_id, discovered_by_worker, slot_key,
            fingerprint, identity_json, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT (project_id, fingerprint)
            WHERE fingerprint IS NOT NULL
        DO NOTHING
        RETURNING id
        """,
        (
            candidate_id,
            project_id,
            body.title,
            body.severity,
            body.vuln_type,
            body.target,
            body.confidence,
            body.source_fact_id,
            body.remediation,
            discovered_by_run_id,
            discovered_by_worker,
            body.slot_key,
            fingerprint,
            identity_json,
            now,
            now,
        ),
    ).fetchone()
    vulnerability_id = (
        inserted["id"]
        if inserted is not None
        else conn.execute(
            """
            SELECT id FROM vulnerabilities
            WHERE project_id = ? AND fingerprint = ?
            """,
            (project_id, fingerprint),
        ).fetchone()["id"]
    )

    current = _get_vulnerability_for_update(
        conn,
        project_id,
        vulnerability_id,
    )
    severity = current["severity"]
    if _SEVERITY_RANK[body.severity] > _SEVERITY_RANK[severity]:
        severity = body.severity
    confidence = max(float(current["confidence"]), body.confidence)
    conn.execute(
        """
        UPDATE vulnerabilities
        SET severity = ?,
            confidence = ?,
            vuln_type = CASE WHEN vuln_type = '' THEN ? ELSE vuln_type END,
            target = CASE WHEN target = '' THEN ? ELSE target END,
            remediation = CASE
                WHEN remediation = '' THEN ? ELSE remediation END,
            updated_at = ?
        WHERE id = ? AND project_id = ?
        """,
        (
            severity,
            confidence,
            body.vuln_type,
            body.target,
            body.remediation,
            now,
            vulnerability_id,
            project_id,
        ),
    )
    observation_id = _insert_discovery_observation(
        conn, project_id, vulnerability_id, body,
        identity_json=identity_json,
        task_run_id=discovered_by_run_id, worker=discovered_by_worker, observed_at=now,
    )
    # verifier Intent 与 observation 同事务创建，避免发现已提交但验证工作丢失。
    schedule_observation_verifier(conn, project_id, observation_id)
    return vulnerability_from_row(
        get_vulnerability_or_404(conn, project_id, vulnerability_id)
    )


def update_vulnerability(
    conn: sqlite3.Connection,
    project_id: str,
    vuln_id: str,
    body: UpdateVulnerabilityRequest,
) -> Vulnerability:
    lock_verification_project_write(conn, project_id)
    current = get_vulnerability_or_404(conn, project_id, vuln_id)
    sets: list[str] = []
    params: list = []
    for field in ("status", "severity", "remediation"):
        value = getattr(body, field)
        if value is not None:
            sets.append(f"{field} = ?")
            params.append(value)
    if body.confidence is not None:
        sets.append("confidence = ?")
        params.append(body.confidence)
    if body.evidence_items is not None:
        current_items = _evidence_items_from_row(current)
        historical = [item for item in current_items if item.source == "historical"]
        submitted_history = [item for item in body.evidence_items if item.source == "historical"]
        # 人工替换不能改写或删除迁移原文，也不能将人工材料标成自动采集。
        if historical != submitted_history:
            raise HTTPException(422, "historical_evidence_is_immutable")
        if any(item.source == "capture" for item in body.evidence_items):
            raise HTTPException(422, "manual_vulnerability_requires_manual_evidence")
        _validate_evidence_items(conn, project_id, body.evidence_items, preserved_items=current_items)
        sets.append("evidence_items_json = ?")
        params.append(dump_json([item.model_dump(exclude_none=True) for item in body.evidence_items]))
    # 报告阶段回写的复现步骤（markdown 编号步骤）；None=不改动。
    if body.repro_steps is not None:
        sets.append("repro_steps = ?")
        params.append(body.repro_steps)
    if not sets:
        row = get_vulnerability_or_404(conn, project_id, vuln_id)
        return vulnerability_from_row(row)
    sets.append("updated_at = ?")
    params.append(utcnow())
    params.extend([vuln_id, project_id])
    conn.execute(
        f"UPDATE vulnerabilities SET {', '.join(sets)} WHERE id = ? AND project_id = ?",
        params,
    )
    row = get_vulnerability_or_404(conn, project_id, vuln_id)
    return vulnerability_from_row(row)


def get_vulnerability_for_fact(
    conn: sqlite3.Connection,
    project_id: str,
    fact_id: str,
) -> Vulnerability | None:
    # 合并后的后续发现也通过实际 Observation 映射回同一个漏洞身份。
    row = conn.execute(
        "SELECT v.* FROM vulnerabilities v WHERE v.project_id = ? AND "
        "(v.source_fact_id = ? OR EXISTS (SELECT 1 FROM vulnerability_observations o "
        "WHERE o.project_id = v.project_id AND o.vulnerability_id = v.id AND o.source_fact_id = ?)) "
        "ORDER BY v.created_at, v.id LIMIT 1",
        (project_id, fact_id, fact_id),
    ).fetchone()
    return vulnerability_from_row(row) if row is not None else None


def confirm_vulnerability_for_slot(
    conn: sqlite3.Connection,
    project_id: str,
    slot_key: str,
) -> int:
    # pentest slot verified 时：把该 slot 关联的漏洞（通过 slot_key 匹配）升级 confirmed。
    # slot_key 在漏洞创建时由 conclude 写入（fact 带 vulnerability 且 intent 有 slot_key）。
    cur = conn.execute(
        """
        UPDATE vulnerabilities
        SET confidence = 1.0, status = 'confirmed', updated_at = ?
        WHERE project_id = ? AND slot_key = ? AND status != 'dismissed'
        """,
        (utcnow(), project_id, slot_key),
    )
    return int(cur.rowcount)


def vulnerabilities_summary(
    conn: sqlite3.Connection,
    *,
    tenant_id: str | None = None,
    principal: RequestPrincipal | None = None,
) -> dict[str, object]:
    """按显式 Tenant 聚合漏洞；省略 Tenant 仅供超级管理员全局视图。"""
    tenant_join = "JOIN projects p ON p.id = v.project_id" if tenant_id is not None or principal is not None else ""
    clauses = ["p.tenant_id = ?"] if tenant_id is not None else []
    params = (tenant_id,) if tenant_id is not None else ()
    if principal is not None:
        access_clause, access_params = verification_project_access_clause(principal)
        clauses.append(access_clause)
        params += access_params
    tenant_where = "WHERE " + " AND ".join(clauses) if clauses else ""
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM vulnerabilities v {tenant_join} {tenant_where}",
        params,
    ).fetchone()["c"]
    by_severity: dict[str, int] = {}
    for row in conn.execute(
        f"""
        SELECT v.severity, COUNT(*) AS c
        FROM vulnerabilities v {tenant_join} {tenant_where}
        GROUP BY v.severity
        """,
        params,
    ).fetchall():
        by_severity[row["severity"]] = int(row["c"])
    by_status: dict[str, int] = {}
    for row in conn.execute(
        f"""
        SELECT v.status, COUNT(*) AS c
        FROM vulnerabilities v {tenant_join} {tenant_where}
        GROUP BY v.status
        """,
        params,
    ).fetchall():
        by_status[row["status"]] = int(row["c"])
    # 项目标题、ID 与计数必须使用相同的 Tenant／复测 caller 过滤。
    project_where = tenant_where
    per_project: list[dict[str, object]] = []
    for row in conn.execute(
        f"""
        SELECT v.project_id, p.title, COUNT(*) AS c
        FROM vulnerabilities v JOIN projects p ON v.project_id = p.id
        {project_where}
        GROUP BY v.project_id, p.title ORDER BY c DESC LIMIT 10
        """,
        params,
    ).fetchall():
        per_project.append({"project_id": row["project_id"], "title": row["title"], "count": int(row["c"])})
    return {
        "total": int(total),
        "by_severity": by_severity,
        "by_status": by_status,
        "per_project": per_project,
    }
