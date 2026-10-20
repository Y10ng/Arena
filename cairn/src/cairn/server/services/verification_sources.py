"""只读取所选漏洞的原文和直接证据，不遍历 provenance 或运行资料。"""

from __future__ import annotations

import json
from pathlib import PurePosixPath
import re

from fastapi import HTTPException

from cairn.evidence_contracts import EvidenceItem, evidence_refs
from cairn.server.auth import RequestPrincipal, _project_access_exists

from .assessment_policy import policy_from_project_row
from .audit_artifacts import _artifact_disk_path_from_row
from .project_resources import (
    _copy_project_resource_records,
    _insert_prepared_project_resources,
    _project_resource_disk_path,
    validate_project_resource_path,
    validate_resource_content,
)
from .time_utils import utcnow
from .vulnerabilities import read_vulnerability_evidence

SOURCE_REPORT_PATH = "_verification/platform-vulnerability.json"
SOURCE_MANIFEST_PATH = "_verification/source-manifest.json"
SOURCE_ARTIFACT_ROOT = "_verification/historical-evidence"
SOURCE_RESOURCE_ROOT = "_verification/source-resources"


def artifact_is_verification_material(artifact) -> bool:
    metadata = json.loads(artifact["metadata_json"] or "{}")
    # stream 由 Dispatcher 登记运行流时设置，目标命令的输出文件没有这个身份。
    if metadata.get("stream") in {"stdout", "stderr", "diagnostic"}:
        return False
    return not artifact_is_internal_file(artifact)


def artifact_is_internal_file(artifact) -> bool:
    metadata = json.loads(artifact["metadata_json"] or "{}")
    # source_path 来自 Runner 对原件 realpath 的捕获，不用可改名的 uri 或 Worker kind 判断用途。
    for key in ("source_path", "registered_path"):
        source = metadata.get(key)
        if not source:
            continue
        path = PurePosixPath(source)
        if any(
            root == path or root in path.parents for root in (
                PurePosixPath("/home/kali/.pi"),
                PurePosixPath("/home/kali/workspace/.cairn/pi"),
                PurePosixPath("/tmp/cairn-prompts"),
                PurePosixPath("/opt/cairn/pi-agent"),
            )
        ) or path == PurePosixPath("/opt/cairn/AGENTS.md"):
            return True
    return False


def require_platform_source_user(principal: RequestPrincipal) -> None:
    # 必须在查询项目、漏洞或磁盘原件之前拒绝 integration。
    if principal.kind != "user" or not principal.tenant_id:
        raise HTTPException(403, "平台漏洞来源仅允许当前租户的浏览器用户使用")


def source_project(conn, project_id: str, principal: RequestPrincipal, *, for_creation: bool = False):
    require_platform_source_user(principal)
    if not _project_access_exists(conn, project_id, principal):
        raise HTTPException(404, "来源项目不存在或无权访问")
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    # 管理员可以跨租户查看，取材新建仍只能使用当前认证租户。
    if project is None or (for_creation and project["tenant_id"] != principal.tenant_id):
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


def _historical_resource_path(resource) -> str:
    try:
        return validate_project_resource_path(f"{SOURCE_RESOURCE_ROOT}/{resource['id']}/{resource['path']}")
    except ValueError as exc:
        raise HTTPException(409, "来源项目资料无法作为复测输入路径") from exc


def _project_context(conn, project, missing: list[dict], *, resource_allowed) -> dict:
    context = {
        "project_id": project["id"], "title": project["title"],
        "origin": None, "goal": None, "targets": [], "resource_refs": [],
        "authorization": _fields(project, "authorization_epoch authorization_expires_at authorization_scope_digest"),
        "notice": "来源项目的历史上下文，不构成本轮授权；未记录的范围或凭据不推断补齐。",
    }
    for fact_id in ("origin", "goal"):
        row = conn.execute("SELECT * FROM facts WHERE project_id = ? AND id = ?", (project["id"], fact_id)).fetchone()
        if row is None:
            missing.append(_missing("project_context_fact_missing", "fact", fact_id, "来源项目未记录该上下文原文。"))
        else:
            context[fact_id] = _fields(row, "id description creator created_at")
    context["targets"] = [dict(row) for row in conn.execute(
        "SELECT id, value, type, status, source_fact_id, discovered_by_run_id, created_at "
        "FROM targets WHERE project_id = ? ORDER BY created_at, id", (project["id"],),
    ).fetchall()]
    # 只引用实际保存的 grant，不用当前 targets 重建或伪造历史授权。
    table = "project_authorization_grants_v2" if policy_from_project_row(project).goal_driven else "project_authorization_grants"
    grant = conn.execute(
        f"SELECT authorization_epoch, scope_descriptor_json, authorized_at FROM {table} "
        "WHERE project_id = ? AND authorization_epoch = ?",
        (project["id"], project["authorization_epoch"]),
    ).fetchone()
    context["authorization"]["grant"] = dict(grant) if grant is not None else None
    descriptions = [context[key]["description"] for key in ("origin", "goal") if context[key] is not None]
    for resource in conn.execute(
        "SELECT * FROM project_resources "
        "WHERE project_id = ? ORDER BY path, id", (project["id"],),
    ).fetchall():
        # 资料必须在原文中明确引用；不因同处一个目录而扩大输入闭包。
        paths = (resource["path"], f"/home/kali/workspace/{resource['path']}")
        if not any(re.search(r"(?<![\w./-])" + re.escape(path) + r"(?![\w./-])", text) for path in paths for text in descriptions):
            continue
        item = {
            **_fields(resource, "id project_id path kind size_bytes sha256 active"),
            "content_path": f"/projects/{project['id']}/resources/{resource['id']}",
            "material_role": "historical_input", "available": False,
        }
        context["resource_refs"].append(item)
        try:
            path = _historical_resource_path(resource)
            validate_resource_content(_project_resource_disk_path(project["id"], resource["id"]), path=resource["path"],
                                      size_bytes=resource["size_bytes"], digest_sha256=resource["sha256"])
        except HTTPException as exc:
            if exc.status_code not in {404, 409}:
                raise
            missing.append(_missing("project_context_resource_unreadable", "project_resource", resource["id"], "明确关联的项目资料原件不可读取、路径不可用或完整性校验失败。"))
            continue
        if not resource_allowed(conn, resource):
            missing.append(_missing("project_context_resource_not_input", "project_resource", resource["id"], "明确关联的项目资料未通过内部输入边界；原件保留，不复制或改写。"))
            continue
        item.update(available=True, input_path=path)
    return context


def read_platform_vulnerability_source(conn, project_id: str, vulnerability_id: str,
                                      principal: RequestPrincipal, *, artifact_allowed, resource_allowed,
                                      observation_id: str | None = None, for_creation: bool = False) -> tuple[dict, list]:
    project = source_project(conn, project_id, principal, for_creation=for_creation)
    vulnerability = conn.execute(
        "SELECT * FROM vulnerabilities WHERE project_id = ? AND id = ?", (project_id, vulnerability_id),
    ).fetchone()
    if vulnerability is None:
        raise HTTPException(404, "来源漏洞不存在")
    selected = read_vulnerability_evidence(conn, project_id, vulnerability_id, observation_id)
    snapshot = {
        "schema_version": 2,
        "project_id": project_id, "vulnerability_id": vulnerability_id, "project_title": project["title"],
        "captured_at": utcnow(),
        "material_role": "historical_input",
        "notice": "以下为受理时的历史主张与历史证据，不代表本轮观察，也不扩大本轮 Origin 授权。原文字段未经摘要或改写。",
        "vulnerability": _fields(vulnerability, "id title severity vuln_type target confidence status remediation repro_steps fingerprint identity_json created_at updated_at"),
        "evidence": selected,
        "facts": [], "observations": [], "verifications": [], "fact_evidence": [],
        "history_path": f"/projects/{project_id}/vulnerabilities/{vulnerability_id}/provenance",
        "artifacts": [], "missing_materials": [],
    }
    missing = snapshot["missing_materials"]
    snapshot["project_context"] = _project_context(conn, project, missing, resource_allowed=resource_allowed)
    observations = conn.execute(
        "SELECT * FROM vulnerability_observations WHERE project_id = ? AND vulnerability_id = ? ORDER BY observed_at, id",
        (project_id, vulnerability_id),
    ).fetchall()
    snapshot["observations"] = [
        {**_fields(row, "id vulnerability_id source_fact_id task_run_id worker slot_key identity_json severity confidence observed_at"),
         "detail_path": f"/projects/{project_id}/vulnerabilities/{vulnerability_id}?observation_id={row['id']}"}
        for row in observations
    ]
    verifications = conn.execute(
        "SELECT * FROM vulnerability_verifications WHERE project_id = ? AND vulnerability_id = ? ORDER BY created_at, id",
        (project_id, vulnerability_id),
    ).fetchall()
    snapshot["verifications"] = [
        {**_fields(row, "id vulnerability_id observation_id intent_id fact_id verifier_task_run_id verifier_worker discovery_task_run_id discovery_worker independence_level outcome created_at"),
         "detail_path": snapshot["history_path"]}
        for row in verifications
    ]
    selected_observation = next((row for row in observations if row["id"] == selected["observation_id"]), None)
    fact_id = selected_observation["source_fact_id"] if selected_observation is not None else vulnerability["source_fact_id"]
    references: dict[str, list[dict]] = {}

    def reference(artifact_id, source_type, source_id, pointer=""):
        if artifact_id:
            value = {"source_type": source_type, "source_id": source_id, "pointer": pointer}
            if value not in references.setdefault(artifact_id, []):
                references[artifact_id].append(value)

    source_type = "observation" if selected_observation is not None else "vulnerability"
    source_id = selected["observation_id"] or vulnerability_id
    for ref in evidence_refs(EvidenceItem.model_validate(item) for item in selected["evidence_items"]):
        reference(ref.artifact_id, source_type, source_id, ref.pointer)
    if fact_id:
        fact = conn.execute("SELECT * FROM facts WHERE project_id = ? AND id = ?", (project_id, fact_id)).fetchone()
        if fact is None:
            missing.append(_missing("source_fact_missing", "fact", fact_id, "选中发现对应的来源 Fact 已不存在。"))
        else:
            snapshot["facts"].append(_fields(fact, "id description task_run_id creator created_at assessment_record_json"))
        evidence = conn.execute(
            "SELECT * FROM fact_evidence WHERE project_id = ? AND fact_id = ? ORDER BY created_at, artifact_id, pointer",
            (project_id, fact_id),
        ).fetchall()
        for row in evidence:
            snapshot["fact_evidence"].append(_fields(row, "fact_id artifact_id pointer verifier_run_id created_at"))
            reference(row["artifact_id"], "fact", fact_id, row["pointer"])
    for key in ("origin", "goal"):
        context_fact = snapshot["project_context"][key]
        if context_fact is None:
            continue
        context_fact["evidence_refs"] = [dict(row) for row in conn.execute(
            "SELECT artifact_id, pointer, verifier_run_id FROM fact_evidence "
            "WHERE project_id = ? AND fact_id = ? ORDER BY created_at, artifact_id, pointer", (project_id, key),
        ).fetchall()]
        for ref in context_fact["evidence_refs"]:
            reference(ref["artifact_id"], "project_context", key, ref["pointer"])
    if not observations:
        missing.append(_missing("observations_not_recorded", "vulnerability", vulnerability_id, "来源未记录发现 Observation，不能据此宣称已有完整发现链。"))
    if not fact_id:
        missing.append(_missing("source_fact_not_recorded", "vulnerability", vulnerability_id, "选中来源未关联 Fact；本次仅保留实际存在的漏洞原文。"))
    if not references:
        missing.append(_missing("direct_evidence_not_recorded", "vulnerability", vulnerability_id, "选中来源未直接引用证据文件，历史证据并不齐全。"))
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
        # 历史直接引用仍须排除 Harness 运行流；合法目标日志与普通文件按相同来源边界检查。
        if not artifact_is_verification_material(artifact):
            missing.append(_missing("source_artifact_not_input", "artifact", artifact_id, "该直接引用指向 Harness 内部运行流、会话、指引或配置，不作为复测输入复制。", artifact_id=artifact_id))
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
    for resource in snapshot["project_context"]["resource_refs"]:
        if not resource["available"]:
            continue
        resources.extend(_copy_project_resource_records(conn, project_id, [(
            resource["kind"], resource["input_path"], int(resource["size_bytes"]), resource["sha256"],
            _project_resource_disk_path(resource["project_id"], resource["id"]),
        )], created_by=created_by))
    report_bytes = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    manifest = {
        "schema_version": 2, "material_role": "historical_input",
        "source": {"project_id": snapshot["project_id"], "vulnerability_id": snapshot["vulnerability_id"],
                   "observation_id": snapshot["evidence"]["observation_id"]},
        "captured_at": snapshot["captured_at"], "snapshot_path": SOURCE_REPORT_PATH,
        "evidence": {"snapshot_path": SOURCE_REPORT_PATH, "pointer": "/evidence",
                     "source": snapshot["evidence"]["source"],
                     "item_ids": [item["id"] for item in snapshot["evidence"]["evidence_items"]]},
        "project_context": {"snapshot_path": SOURCE_REPORT_PATH, "pointer": "/project_context"},
        "artifacts": snapshot["artifacts"], "resources": snapshot["project_context"]["resource_refs"],
        "missing_materials": snapshot["missing_materials"],
        "notice": snapshot["notice"],
    }
    created = _insert_prepared_project_resources(conn, project_id, [
        ("note", SOURCE_REPORT_PATH, report_bytes),
        ("note", SOURCE_MANIFEST_PATH, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")),
    ], created_by=created_by)
    return created[0], [*resources, *created]
