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
    VulnerabilityListItem,
    VulnerabilityObservation,
    VulnerabilityReadable,
    VulnerabilitySourceFact,
)

from .common import *
from .ids import lock_verification_project_write, next_vulnerability_id
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


def vulnerability_delete_block_reason(conn, project_id: str) -> str | None:
    """业务删除依赖项目写入资格，不等待全项目物理清理或其他可恢复工作。"""
    from .ids import get_project_or_404

    project = get_project_or_404(conn, project_id)
    if conn.execute("SELECT 1 FROM verification_requests WHERE project_id = ?", (project_id,)).fetchone():
        return "独立复测请使用删除复测记录"
    if project["status"] == "active":
        if project["coverage_stop_reason"] == "user_stopped":
            return "项目正在结束，待在途结果收口后可删除"
        return "请先暂停或结束项目"
    if project["status"] not in {"paused", "stopped", "completed"}:
        return "仅已暂停、已结束或已完成的项目允许删除漏洞"
    return None


def _with_delete_eligibility(conn, vulnerability: Vulnerability) -> Vulnerability:
    reason = vulnerability_delete_block_reason(conn, vulnerability.project_id)
    return vulnerability.model_copy(update={"can_delete": reason is None, "delete_block_reason": reason})


def delete_vulnerability(conn, project_id: str, vuln_id: str) -> None:
    """仅删除漏洞业务行；项目锁与 conclude/派发/项目状态更新互斥，不触碰共享原件。"""
    from .ids import get_project_or_404
    from .project_records import require_unmanaged_project

    conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
    get_project_or_404(conn, project_id)
    require_unmanaged_project(conn, project_id)
    _get_vulnerability_for_update(conn, project_id, vuln_id)
    reason = vulnerability_delete_block_reason(conn, project_id)
    if reason is not None:
        raise HTTPException(409, reason)
    # 只结束目标 verifier 的逻辑义务；物理执行和清理证明保留原状，其他可恢复工作不动。
    now = utcnow()
    conn.execute(
        """UPDATE assessment_attempts SET status = 'failed', ended_at = ?, outcome = '目标漏洞已删除'
           WHERE project_id = ? AND status = 'active' AND intent_id IN (
               SELECT id FROM intents WHERE project_id = ? AND assessment_role = 'verifier'
                 AND target_observation_id IN (
                     SELECT id FROM vulnerability_observations WHERE project_id = ? AND vulnerability_id = ?
                 )
           )""",
        (now, project_id, project_id, project_id, vuln_id),
    )
    # 沿用 Intent abandoned/conclusion_reason 终止语义，保留历史身份，不制造验证结论。
    conn.execute(
        """UPDATE intents SET concluded_at = ?, conclusion = 'abandoned',
                  conclusion_reason = '目标漏洞已删除', worker = NULL, last_heartbeat_at = NULL
           WHERE project_id = ? AND assessment_role = 'verifier'
             AND concluded_at IS NULL AND to_fact_id IS NULL
             AND target_observation_id IN (
                 SELECT id FROM vulnerability_observations WHERE project_id = ? AND vulnerability_id = ?
             )""",
        (now, project_id, project_id, vuln_id),
    )
    conn.execute("DELETE FROM vulnerabilities WHERE project_id = ? AND id = ?", (project_id, vuln_id))


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
        reproduction_steps=json.loads(row["reproduction_steps_json"]),
        remediation=row["remediation"],
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
    *,
    include_display_images: bool = False,
) -> VulnerabilityDetail:
    """默认主证据来自最新真实 Observation；其他历史材料保留独立入口。"""
    row = get_vulnerability_or_404(conn, project_id, vuln_id)
    from .vulnerability_dispositions import list_vulnerability_dispositions

    detail = VulnerabilityDetail(
        **_with_delete_eligibility(conn, vulnerability_from_row(row)).model_dump(),
        record_version=row["record_version"],
        dispositions=list_vulnerability_dispositions(conn, project_id, vuln_id),
    )
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
            "SELECT id, description, task_run_id, assessment_record_json FROM facts WHERE id = ? AND project_id = ?",
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
                preconditions=json.loads(fact_row["assessment_record_json"] or "{}").get("preconditions", []),
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

    from .vulnerability_reports import build_vulnerability_readable
    from .http_evidence_render import list_display_images

    # 图片与流量取材仅用于显式阅读／导出，首屏不访问证据文件。
    if include_display_images:
        detail.display_images = list_display_images(
            conn, project_id, selected["evidence_items"], task_run_id=run_id,
        )
    detail.readable = VulnerabilityReadable.model_validate(build_vulnerability_readable(conn, detail))
    return detail


def vulnerability_read_scope(
    *, tenant_id: str | None = None, principal: RequestPrincipal | None = None,
) -> tuple[str, tuple]:
    """列表、统计与首页预览共享 Tenant／复测 caller 及隐藏记录边界。"""
    clauses: list[str] = []
    params: tuple = ()
    if principal is not None:
        access_clause, access_params = verification_project_access_clause(principal)
        clauses.append(access_clause)
        params += access_params
    if tenant_id is not None:
        clauses.append("p.tenant_id = ?")
        params += (tenant_id,)
    return " AND ".join(clauses) if clauses else "1 = 1", params


def list_all_vulnerabilities(
    conn: sqlite3.Connection,
    *,
    tenant_id: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    principal: RequestPrincipal | None = None,
) -> list[VulnerabilityListItem]:
    """跨项目成果列表：聚合所有项目的漏洞并附带项目标题，供主菜单"成果列表"展示。"""
    scope, scope_params = vulnerability_read_scope(tenant_id=tenant_id, principal=principal)
    clauses = [scope]
    params = list(scope_params)
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
    delete_reasons = {project_id: vulnerability_delete_block_reason(conn, project_id)
                      for project_id in {row["project_id"] for row in rows}}
    for row in rows:
        base = vulnerability_from_row(row).model_dump()
        reason = delete_reasons[row["project_id"]]
        base.update(can_delete=reason is None, delete_block_reason=reason)
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
    reason = vulnerability_delete_block_reason(conn, project_id)
    return [vulnerability_from_row(row).model_copy(update={
        "can_delete": reason is None, "delete_block_reason": reason,
    }) for row in rows]


def validate_flow_evidence_sources(conn, project_id: str, items: list[EvidenceItem], *, task_run_id: str | None) -> bool:
    """真实执行绑定决定当前或历史来源；模型标签不能赋予跨项目引用权限。"""
    from .flow_evidence import resolve_flow_evidence

    current = False
    run = None
    for item in items:
        if item.flow_id is None:
            continue
        if item.source == "manual":
            # 人工补充只建立同项目引用；取材完整性留到实际阅读／受理时核验。
            if task_run_id is not None:
                raise HTTPException(422, "flow_evidence_source_mismatch")
            if conn.execute("SELECT 1 FROM http_flows WHERE project_id = ? AND id = ?", (project_id, item.flow_id)).fetchone() is None:
                raise HTTPException(422, "flow_not_found_in_project")
            continue
        snapshot = resolve_flow_evidence(conn, project_id, item.flow_id)
        scope = snapshot["scope"]
        same_run = task_run_id is not None and scope["task_run_id"] == task_run_id
        if item.source == "capture":
            if not same_run:
                raise HTTPException(422, "flow_evidence_current_run_required")
            if run is None:
                run = conn.execute(
                    "SELECT execution_generation FROM task_runs WHERE project_id = ? AND id = ?",
                    (project_id, task_run_id),
                ).fetchone()
            if run is None or scope["generation"] != run["execution_generation"]:
                raise HTTPException(422, "flow_evidence_generation_mismatch")
            # 缺响应的失败请求可以保留，但不能独自证明本次已经复现。
            current = current or snapshot["record"]["response"] is not None
        elif item.source != "historical" or same_run:
            raise HTTPException(422, "flow_evidence_source_mismatch")
    return current


def has_current_evidence(conn, project_id: str, task_run_id: str | None, items: list[EvidenceItem]) -> bool:
    """本轮复现至少有本轮可读流量响应或本轮原件，历史输入不充当新实验。"""
    current_flow = validate_flow_evidence_sources(conn, project_id, items, task_run_id=task_run_id)
    for ref in evidence_refs(item for item in items if item.source != "historical"):
        row = conn.execute(
            "SELECT task_run_id FROM artifacts WHERE project_id = ? AND id = ?",
            (project_id, ref.artifact_id),
        ).fetchone()
        if row is not None and task_run_id is not None and row["task_run_id"] == task_run_id:
            return True
    return current_flow


def _validate_evidence_items(
    conn: sqlite3.Connection,
    project_id: str,
    items: list[EvidenceItem],
    *,
    discovery: bool = False,
    task_run_id: str | None = None,
    preserved_items: list[EvidenceItem] | None = None,
) -> None:
    manual_flows = [item.flow_id for item in items if item.source == "manual" and item.flow_id]
    if len(manual_flows) != len(set(manual_flows)):
        raise HTTPException(422, "manual_flow_reference_must_be_unique")
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "evidence_item_id_must_be_unique")
    if discovery and any(
        (item.source == "historical" and item.flow_id is None)
        or any(value is not None for value in (item.request_text, item.response_text, item.text))
        for item in items
    ):
        raise HTTPException(422, "new_discovery_requires_artifact_evidence")
    # 原样保留的既有条目不是新输入；原件已缺失时仍须保留其真实历史引用。
    submitted_items = [item for item in items if item not in (preserved_items or [])]
    validate_flow_evidence_sources(conn, project_id, submitted_items, task_run_id=task_run_id)
    from .http_evidence_render import is_optional_http_image

    refs = evidence_refs(submitted_items)
    for ref in refs:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE project_id = ? AND id = ?",
            (project_id, ref.artifact_id),
        ).fetchone()
        if row is None:
            raise HTTPException(422, f"Artifact {ref.artifact_id} not found in project")
        if is_optional_http_image(row):
            raise HTTPException(422, "derived_image_is_not_original_evidence")
        if discovery and (task_run_id is None or row["task_run_id"] != task_run_id):
            raise HTTPException(422, "discovery_evidence_current_run_required")

    from .vulnerability_reports import validate_evidence_excerpts

    validate_evidence_excerpts(conn, project_id, submitted_items)


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
            severity, confidence, evidence_items_json,
            reproduction_steps_json, remediation, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id, project_id, vulnerability_id, body.source_fact_id,
            task_run_id, worker, body.slot_key, identity_json,
            body.severity, body.confidence,
            dump_json([item.model_dump(exclude_none=True) for item in body.evidence_items]),
            dump_json([step.model_dump() for step in body.reproduction_steps]),
            body.remediation, observed_at,
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
    if not discovery and body.reproduction_steps:
        raise HTTPException(422, "reproduction_steps_require_discovery_observation")
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
            body.confidence, body.source_fact_id, "" if discovery else body.remediation,
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
            confidence, status, source_fact_id,
            discovered_by_run_id, discovered_by_worker, slot_key,
            fingerprint, identity_json, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?
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
            updated_at = ?
        WHERE id = ? AND project_id = ?
        """,
        (
            severity,
            confidence,
            body.vuln_type,
            body.target,
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
    *,
    principal: RequestPrincipal | None = None,
) -> Vulnerability:
    from .vulnerability_dispositions import prepare_vulnerability_disposition, insert_vulnerability_disposition

    if body.status is not None:
        # 与删除及报告冻结按相同顺序先锁项目，再锁漏洞，避免处置依据跨事务漂移。
        conn.execute("SELECT id FROM projects WHERE id = ? FOR UPDATE", (project_id,)).fetchone()
    lock_verification_project_write(conn, project_id)
    current = conn.execute(
        "SELECT * FROM vulnerabilities WHERE project_id = ? AND id = ? FOR UPDATE",
        (project_id, vuln_id),
    ).fetchone()
    if current is None:
        raise HTTPException(404, "Vulnerability not found")
    disposition = (prepare_vulnerability_disposition(conn, project_id, current, body, principal)
                   if body.status is not None else None)
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
    if disposition is not None:
        insert_vulnerability_disposition(conn, disposition, row["record_version"])
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


def vulnerability_counts(
    conn,
    *,
    tenant_id: str | None = None,
    principal: RequestPrincipal | None = None,
) -> dict[str, object]:
    """只聚合计数，不装配正文、删除资格或执行诊断。"""
    scope, params = vulnerability_read_scope(tenant_id=tenant_id, principal=principal)
    total = 0
    by_severity: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for row in conn.execute(
        f"""
        SELECT v.severity, v.status, COUNT(*) AS c
        FROM vulnerabilities v JOIN projects p ON p.id = v.project_id
        WHERE {scope}
        GROUP BY v.severity, v.status
        """,
        params,
    ).fetchall():
        count = int(row["c"])
        total += count
        by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + count
        by_status[row["status"]] = by_status.get(row["status"], 0) + count
    return {"total": total, "by_severity": by_severity, "by_status": by_status}


def vulnerabilities_summary(
    conn: sqlite3.Connection,
    *,
    tenant_id: str | None = None,
    principal: RequestPrincipal | None = None,
) -> dict[str, object]:
    """按显式 Tenant 聚合漏洞；省略 Tenant 仅供超级管理员全局视图。"""
    counts = vulnerability_counts(conn, tenant_id=tenant_id, principal=principal)
    scope, params = vulnerability_read_scope(tenant_id=tenant_id, principal=principal)
    # 项目标题、ID 与计数必须使用相同的 Tenant／复测 caller 过滤。
    project_where = f"WHERE {scope}"
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
    return {**counts, "per_project": per_project}
