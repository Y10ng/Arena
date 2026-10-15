"""只读取所选漏洞的原文和直接证据，不遍历 provenance 或运行资料。"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from fastapi import HTTPException

from cairn.server.auth import RequestPrincipal

from .audit_artifacts import _artifact_disk_path_from_row
from .project_resources import (
    _copy_project_resource_records,
    _insert_prepared_project_resources,
    validate_project_resource_path,
    validate_resource_content,
)
from .time_utils import utcnow

SOURCE_REPORT_PATH = "_verification/platform-vulnerability.json"
SOURCE_MANIFEST_PATH = "_verification/source-manifest.json"
SOURCE_ARTIFACT_ROOT = "_verification/historical-evidence"
SOURCE_ARTIFACT_KINDS = {"file", "report", "screenshot", "patch", "pcap", "har"}


def artifact_is_verification_material(artifact) -> bool:
    if artifact["kind"] not in SOURCE_ARTIFACT_KINDS:
        return False
    metadata = json.loads(artifact["metadata_json"] or "{}")
    if metadata.get("stream") in {"stdout", "stderr", "diagnostic"}:
        return False
    filename = PurePosixPath(artifact["uri"]).name.removeprefix(f"{artifact['id']}-")
    paths = [filename]
    if metadata.get("source_path"):
        paths.append(metadata["source_path"])
    for path in paths:
        name = PurePosixPath(path).name.casefold()
        # 按原始来源和明确文件身份排除内部材料，不把漏洞主题词当作文件类别。
        if ".pi" in PurePosixPath(path).parts or name in {"agents.md", "agent.md", "skill.md", "env", ".env"} or name.startswith(".env."):
            return False
        if PurePosixPath(name).suffix == ".log":
            return False
        if PurePosixPath(name).stem in {
            "session", "sessions", "prompt", "system-prompt", "auth", "config", "settings",
            "model-config", "credential", "credentials", "stdout", "stderr", "diagnostic",
        } and PurePosixPath(name).suffix in {"", ".json", ".jsonl", ".md", ".txt", ".yaml", ".yml", ".toml"}:
            return False
    return True


def require_platform_source_user(principal: RequestPrincipal) -> None:
    # 必须在查询项目、漏洞或磁盘原件之前拒绝 integration。
    if principal.kind != "user" or not principal.tenant_id:
        raise HTTPException(403, "平台漏洞来源仅允许当前租户的浏览器用户使用")


def source_project(conn, project_id: str, principal: RequestPrincipal):
    require_platform_source_user(principal)
    project = conn.execute(
        "SELECT id, title, tenant_id FROM projects WHERE id = ? AND tenant_id = ?",
        (project_id, principal.tenant_id),
    ).fetchone()
    if project is None:
        raise HTTPException(404, "来源项目不存在或不属于当前租户")
    return project


def _fields(row, names: str) -> dict:
    return {name: row[name] for name in names.split()}


def _missing(code: str, source_type: str, source_id: str, message: str, *, artifact_id=None) -> dict:
    item = {"code": code, "source_type": source_type, "source_id": source_id, "message": message}
    if artifact_id is not None:
        item["artifact_id"] = artifact_id
    return item


def _historical_artifact_path(artifact) -> str:
    filename = PurePosixPath(artifact["uri"]).name
    path = f"{SOURCE_ARTIFACT_ROOT}/{artifact['id']}/{filename}"
    try:
        return validate_project_resource_path(path)
    except ValueError as exc:
        raise HTTPException(409, "来源证据文件名无法作为复测输入路径") from exc


def read_platform_vulnerability_source(conn, project_id: str, vulnerability_id: str,
                                      principal: RequestPrincipal, *, artifact_allowed) -> tuple[dict, list]:
    project = source_project(conn, project_id, principal)
    vulnerability = conn.execute(
        "SELECT * FROM vulnerabilities WHERE project_id = ? AND id = ?", (project_id, vulnerability_id),
    ).fetchone()
    if vulnerability is None:
        raise HTTPException(404, "来源漏洞不存在")
    snapshot = {
        "schema_version": 1,
        "project_id": project_id, "vulnerability_id": vulnerability_id, "project_title": project["title"],
        "captured_at": utcnow(),
        "material_role": "historical_input",
        "notice": "以下为受理时的历史主张与历史证据，不代表本轮观察，也不扩大本轮 Origin 授权。原文字段未经摘要或改写。",
        "vulnerability": _fields(vulnerability, "id title severity vuln_type target confidence status source_fact_id packet_artifact_id remediation repro_request repro_response repro_steps discovered_by_run_id discovered_by_worker slot_key fingerprint identity_json created_at updated_at"),
        "facts": [], "observations": [], "verifications": [], "fact_evidence": [],
        "artifacts": [], "missing_materials": [],
    }
    missing = snapshot["missing_materials"]
    observations = conn.execute(
        "SELECT * FROM vulnerability_observations WHERE project_id = ? AND vulnerability_id = ? ORDER BY observed_at, id",
        (project_id, vulnerability_id),
    ).fetchall()
    snapshot["observations"] = [
        _fields(row, "id vulnerability_id source_fact_id task_run_id worker slot_key packet_artifact_id identity_json severity confidence repro_request repro_response observed_at")
        for row in observations
    ]
    verifications = conn.execute(
        "SELECT * FROM vulnerability_verifications WHERE project_id = ? AND vulnerability_id = ? ORDER BY created_at, id",
        (project_id, vulnerability_id),
    ).fetchall()
    snapshot["verifications"] = [
        _fields(row, "id vulnerability_id observation_id intent_id fact_id verifier_task_run_id verifier_worker discovery_task_run_id discovery_worker independence_level outcome evidence_json notes created_at")
        for row in verifications
    ]
    fact_ids = {value for value in (
        vulnerability["source_fact_id"], *(row["source_fact_id"] for row in observations),
        *(row["fact_id"] for row in verifications),
    ) if value}
    references: dict[str, list[dict]] = {}

    def reference(artifact_id, source_type, source_id, pointer=""):
        if artifact_id:
            references.setdefault(artifact_id, []).append(
                {"source_type": source_type, "source_id": source_id, "pointer": pointer}
            )

    reference(vulnerability["packet_artifact_id"], "vulnerability", vulnerability_id)
    for row in observations:
        reference(row["packet_artifact_id"], "observation", row["id"])
    for row in verifications:
        try:
            evidence = json.loads(row["evidence_json"])
        except (TypeError, ValueError):
            evidence = None
        if not isinstance(evidence, list) or any(not isinstance(ref, dict) or not isinstance(ref.get("artifact_id"), str) or not ref["artifact_id"] for ref in evidence):
            missing.append(_missing("invalid_verification_evidence", "verification", row["id"], "既有复核证据引用无法读取；已保留原始 evidence_json。"))
            continue
        for ref in evidence:
            reference(ref["artifact_id"], "verification", row["id"], ref.get("pointer", ""))
    for fact_id in sorted(fact_ids):
        fact = conn.execute("SELECT * FROM facts WHERE project_id = ? AND id = ?", (project_id, fact_id)).fetchone()
        if fact is None:
            missing.append(_missing("source_fact_missing", "fact", fact_id, "直接关联的来源 Fact 已不存在。"))
            continue
        snapshot["facts"].append(_fields(fact, "id description task_run_id creator created_at assessment_record_json"))
        evidence = conn.execute(
            "SELECT * FROM fact_evidence WHERE project_id = ? AND fact_id = ? ORDER BY created_at, artifact_id, pointer",
            (project_id, fact_id),
        ).fetchall()
        for row in evidence:
            snapshot["fact_evidence"].append(_fields(row, "fact_id artifact_id pointer verifier_run_id created_at"))
            reference(row["artifact_id"], "fact", fact_id, row["pointer"])
    if not observations:
        missing.append(_missing("observations_not_recorded", "vulnerability", vulnerability_id, "来源未记录发现 Observation，不能据此宣称已有完整发现链。"))
    if not fact_ids:
        missing.append(_missing("source_fact_not_recorded", "vulnerability", vulnerability_id, "来源未关联 Fact；本次仅保留实际存在的漏洞原文。"))
    if not references:
        missing.append(_missing("direct_evidence_not_recorded", "vulnerability", vulnerability_id, "来源未直接引用证据文件，历史证据并不齐全。"))
    artifacts = []
    for artifact_id, refs in sorted(references.items()):
        artifact = conn.execute("SELECT * FROM artifacts WHERE project_id = ? AND id = ?", (project_id, artifact_id)).fetchone()
        item = {"artifact_id": artifact_id, "references": refs, "available": False}
        snapshot["artifacts"].append(item)
        if artifact is None:
            missing.append(_missing("source_artifact_missing", "artifact", artifact_id, "直接引用的历史证据记录已不存在。", artifact_id=artifact_id))
            continue
        filename = PurePosixPath(artifact["uri"]).name
        item.update(filename=filename, kind=artifact["kind"], size_bytes=artifact["size"], sha256=artifact["sha256"])
        # 直接引用也不授权复制 session、日志、Prompt、模型配置或凭据。
        if not artifact_is_verification_material(artifact):
            missing.append(_missing("source_artifact_not_input", "artifact", artifact_id, "该直接引用指向日志、会话、指引或内部配置，不作为复测输入复制。", artifact_id=artifact_id))
            continue
        try:
            path = _historical_artifact_path(artifact)
            validate_resource_content(_artifact_disk_path_from_row(artifact), path=artifact_id,
                                      size_bytes=artifact["size"], digest_sha256=artifact["sha256"])
        except HTTPException as exc:
            if exc.status_code not in {404, 409}:
                raise
            missing.append(_missing("source_artifact_unreadable", "artifact", artifact_id, "历史证据原件不可读取、路径不可用或完整性校验失败。", artifact_id=artifact_id))
            continue
        if not artifact_allowed(conn, artifact):
            missing.append(_missing("source_artifact_not_deliverable", "artifact", artifact_id, "历史证据包含不允许携带的内部资料，或无法核实原执行的凭据边界，不能复制；原件未改写，资料并不齐全。", artifact_id=artifact_id))
            continue
        item.update(available=True, input_path=path)
        artifacts.append(artifact)
    return snapshot, artifacts


def save_platform_vulnerability_source(conn, project_id: str, snapshot: dict, artifacts: list, *, created_by: str):
    resources = []
    for artifact in artifacts:
        # 复制时再次校验原字节；竞态删除或变化直接拒绝受理，不留下不完整的成功档案。
        resources.extend(_copy_project_resource_records(conn, project_id, [(
            "file", _historical_artifact_path(artifact), int(artifact["size"]), artifact["sha256"],
            _artifact_disk_path_from_row(artifact),
        )], created_by=created_by))
    report_bytes = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    manifest = {
        "schema_version": 1, "material_role": "historical_input",
        "source": {"project_id": snapshot["project_id"], "vulnerability_id": snapshot["vulnerability_id"]},
        "captured_at": snapshot["captured_at"], "snapshot_path": SOURCE_REPORT_PATH,
        "artifacts": snapshot["artifacts"], "missing_materials": snapshot["missing_materials"],
        "notice": snapshot["notice"],
    }
    created = _insert_prepared_project_resources(conn, project_id, [
        ("note", SOURCE_REPORT_PATH, report_bytes),
        ("note", SOURCE_MANIFEST_PATH, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")),
    ], created_by=created_by)
    return created[0], [*resources, *created]
