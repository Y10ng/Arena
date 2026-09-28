"""按漏洞分组的项目证据 ZIP 导出服务。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from hashlib import sha256
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import Any
import zipfile

from fastapi import HTTPException

from cairn.server.db import get_conn
from cairn.server.models import Artifact

from .audit_artifacts import ArtifactContentFile, artifact_content_file
from .ids import get_project_or_404

_ARCHIVE_COPY_BYTES = 1024 * 1024
_ARCHIVE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_ARCHIVE_TITLE_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f\x7f]+")
_ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_IMAGE_EXTENSIONS = {
    ".ai",
    ".apng",
    ".avif",
    ".bmp",
    ".bpg",
    ".cur",
    ".dds",
    ".dib",
    ".djvu",
    ".emf",
    ".eps",
    ".exr",
    ".fits",
    ".flif",
    ".gif",
    ".hdr",
    ".heic",
    ".heif",
    ".icns",
    ".ico",
    ".jfif",
    ".jif",
    ".jpe",
    ".jpeg",
    ".jpg",
    ".jxl",
    ".mng",
    ".pbm",
    ".pcx",
    ".pgm",
    ".pict",
    ".png",
    ".pnm",
    ".ppm",
    ".psd",
    ".qoi",
    ".ras",
    ".svg",
    ".svgz",
    ".tga",
    ".tif",
    ".tiff",
    ".wbmp",
    ".webp",
    ".wmf",
    ".xbm",
    ".xcf",
    ".xpm",
}
_JAVASCRIPT_EXTENSIONS = {".cjs", ".es", ".es6", ".js", ".jse", ".jsx", ".mjs"}
_HTML_EXTENSIONS = {".hta", ".htm", ".html", ".mht", ".mhtml", ".shtml", ".stm", ".xhtml"}
_JAVASCRIPT_MEDIA_TYPES = {
    "application/ecmascript",
    "application/javascript",
    "application/x-ecmascript",
    "application/x-javascript",
    "text/ecmascript",
    "text/javascript",
}
_HTML_MEDIA_TYPES = {"application/xhtml+xml", "text/html"}


class VulnerabilityEvidenceArchiveUnavailableError(RuntimeError):
    """某个明确关联的漏洞证据无法被安全、完整地加入 ZIP。"""

    def __init__(self, artifact_id: str) -> None:
        super().__init__(f"Artifact {artifact_id} is unavailable for vulnerability evidence archive")
        self.artifact_id = artifact_id


@dataclass(slots=True)
class ProjectVulnerabilityEvidenceArchive:
    disk_path: Path
    filename: str
    temporary_directory: Path

    def cleanup(self) -> None:
        """幂等删除响应完成后不再需要的临时 ZIP。"""
        shutil.rmtree(self.temporary_directory, ignore_errors=True)


@dataclass(frozen=True, slots=True)
class _EvidenceRelation:
    source: str
    pointer: str = ""
    verifier_run_id: str | None = None


@dataclass(slots=True)
class _ArtifactEvidence:
    artifact: Artifact
    relations: list[_EvidenceRelation] = field(default_factory=list)


@dataclass(slots=True)
class _VulnerabilityEvidence:
    vulnerability: dict[str, Any]
    observations: list[dict[str, Any]]
    verifications: list[dict[str, Any]]
    facts: dict[str, dict[str, Any]]
    artifacts: list[_ArtifactEvidence]


def create_project_vulnerability_evidence_archive(
    project_id: str,
) -> ProjectVulnerabilityEvidenceArchive:
    vulnerabilities = _load_project_vulnerability_evidence(project_id)
    temporary_directory = Path(tempfile.mkdtemp(prefix="cairn-vulnerability-evidence-"))
    try:
        archive_path = temporary_directory / "vulnerability-evidence.zip"
        # 先以 0600 创建目标，避免 ZIP 构建期间被同机其他用户读取。
        archive_fd = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(archive_fd)

        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            _write_text_member(archive, "README.txt", _archive_readme(project_id, vulnerabilities))
            for evidence in vulnerabilities:
                _write_vulnerability(archive, project_id, evidence)
    except BaseException:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise

    safe_project_id = _safe_archive_component(project_id, fallback="project")
    return ProjectVulnerabilityEvidenceArchive(
        disk_path=archive_path,
        filename=f"cairn-{safe_project_id}-vulnerability-evidence.zip",
        temporary_directory=temporary_directory,
    )


def _load_project_vulnerability_evidence(project_id: str) -> list[_VulnerabilityEvidence]:
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        vulnerability_rows = conn.execute(
            """
            SELECT * FROM vulnerabilities
            WHERE project_id = ?
            ORDER BY created_at, id
            """,
            (project_id,),
        ).fetchall()
        observation_rows = conn.execute(
            """
            SELECT * FROM vulnerability_observations
            WHERE project_id = ?
            ORDER BY observed_at, id
            """,
            (project_id,),
        ).fetchall()
        verification_rows = conn.execute(
            """
            SELECT * FROM vulnerability_verifications
            WHERE project_id = ?
            ORDER BY created_at, id
            """,
            (project_id,),
        ).fetchall()

        vulnerabilities = [dict(row) for row in vulnerability_rows]
        observations_by_vulnerability: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in observation_rows:
            observation = dict(row)
            observations_by_vulnerability[str(observation["vulnerability_id"])].append(observation)
        verifications_by_vulnerability: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in verification_rows:
            verification = dict(row)
            verification["evidence_refs"] = _parse_evidence_refs(verification.get("evidence_json"))
            verifications_by_vulnerability[str(verification["vulnerability_id"])].append(verification)

        fact_ids = {
            str(fact_id)
            for vulnerability in vulnerabilities
            for fact_id in (
                vulnerability.get("source_fact_id"),
                *(
                    observation.get("source_fact_id")
                    for observation in observations_by_vulnerability.get(str(vulnerability["id"]), [])
                ),
                *(
                    verification.get("fact_id")
                    for verification in verifications_by_vulnerability.get(str(vulnerability["id"]), [])
                ),
            )
            if fact_id
        }
        facts = _load_rows_by_id(conn, "facts", project_id, fact_ids)
        fact_evidence = _load_fact_evidence(conn, project_id, fact_ids)

        relations_by_vulnerability: dict[str, dict[str, list[_EvidenceRelation]]] = {}
        all_artifact_ids: set[str] = set()
        for vulnerability in vulnerabilities:
            vulnerability_id = str(vulnerability["id"])
            relations: dict[str, list[_EvidenceRelation]] = defaultdict(list)
            _add_relation(
                relations,
                vulnerability.get("packet_artifact_id"),
                _EvidenceRelation("canonical packet"),
            )
            _add_fact_relations(
                relations,
                vulnerability.get("source_fact_id"),
                fact_evidence,
                "canonical fact evidence",
            )
            for observation in observations_by_vulnerability.get(vulnerability_id, []):
                observation_id = str(observation["id"])
                _add_relation(
                    relations,
                    observation.get("packet_artifact_id"),
                    _EvidenceRelation(f"observation {observation_id} packet"),
                )
                _add_fact_relations(
                    relations,
                    observation.get("source_fact_id"),
                    fact_evidence,
                    f"observation {observation_id} fact evidence",
                )
            for verification in verifications_by_vulnerability.get(vulnerability_id, []):
                verification_id = str(verification["id"])
                for ref in verification["evidence_refs"]:
                    _add_relation(
                        relations,
                        ref["artifact_id"],
                        _EvidenceRelation(
                            f"verification {verification_id} evidence",
                            pointer=ref.get("pointer", ""),
                            verifier_run_id=ref.get("verifier_run_id"),
                        ),
                    )
                _add_fact_relations(
                    relations,
                    verification.get("fact_id"),
                    fact_evidence,
                    f"verification {verification_id} fact evidence",
                )
            relations_by_vulnerability[vulnerability_id] = relations
            all_artifact_ids.update(relations)

        artifacts = _load_artifacts(conn, project_id, all_artifact_ids)

    missing_artifact_ids = all_artifact_ids - artifacts.keys()
    if missing_artifact_ids:
        raise VulnerabilityEvidenceArchiveUnavailableError(sorted(missing_artifact_ids)[0])

    result: list[_VulnerabilityEvidence] = []
    for vulnerability in vulnerabilities:
        vulnerability_id = str(vulnerability["id"])
        artifact_evidence = [
            _ArtifactEvidence(artifact=artifacts[artifact_id], relations=relations)
            for artifact_id, relations in relations_by_vulnerability[vulnerability_id].items()
        ]
        artifact_evidence.sort(key=lambda item: (item.artifact.created_at, item.artifact.id))
        result.append(
            _VulnerabilityEvidence(
                vulnerability=vulnerability,
                observations=observations_by_vulnerability.get(vulnerability_id, []),
                verifications=verifications_by_vulnerability.get(vulnerability_id, []),
                facts=facts,
                artifacts=artifact_evidence,
            )
        )
    return result


def _load_rows_by_id(conn, table: str, project_id: str, row_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not row_ids:
        return {}
    placeholders = ",".join("?" for _ in row_ids)
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE project_id = ? AND id IN ({placeholders})",
        (project_id, *sorted(row_ids)),
    ).fetchall()
    return {str(row["id"]): dict(row) for row in rows}


def _load_fact_evidence(
    conn,
    project_id: str,
    fact_ids: set[str],
) -> dict[str, list[dict[str, Any]]]:
    if not fact_ids:
        return {}
    placeholders = ",".join("?" for _ in fact_ids)
    rows = conn.execute(
        f"""
        SELECT * FROM fact_evidence
        WHERE project_id = ? AND fact_id IN ({placeholders})
        ORDER BY created_at, artifact_id
        """,
        (project_id, *sorted(fact_ids)),
    ).fetchall()
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        result[str(row["fact_id"])].append(dict(row))
    return result


def _load_artifacts(conn, project_id: str, artifact_ids: set[str]) -> dict[str, Artifact]:
    if not artifact_ids:
        return {}
    placeholders = ",".join("?" for _ in artifact_ids)
    rows = conn.execute(
        f"""
        SELECT * FROM artifacts
        WHERE project_id = ? AND id IN ({placeholders})
        """,
        (project_id, *sorted(artifact_ids)),
    ).fetchall()
    return {
        str(row["id"]): Artifact(
            id=row["id"],
            project_id=row["project_id"],
            task_run_id=row["task_run_id"],
            kind=row["kind"],
            category=row["category"],
            uri=row["uri"],
            sha256=row["sha256"],
            size=row["size"],
            created_at=row["created_at"],
        )
        for row in rows
    }


def _parse_evidence_refs(value: object) -> list[dict[str, str | None]]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    refs: list[dict[str, str | None]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        if not artifact_id:
            continue
        refs.append(
            {
                "artifact_id": artifact_id,
                "pointer": str(item.get("pointer") or "").strip(),
                "verifier_run_id": str(item.get("verifier_run_id") or "").strip() or None,
            }
        )
    return refs


def _add_fact_relations(
    relations: dict[str, list[_EvidenceRelation]],
    fact_id: object,
    fact_evidence: dict[str, list[dict[str, Any]]],
    source: str,
) -> None:
    if not fact_id:
        return
    for evidence in fact_evidence.get(str(fact_id), []):
        _add_relation(
            relations,
            evidence.get("artifact_id"),
            _EvidenceRelation(
                source,
                pointer=str(evidence.get("pointer") or ""),
                verifier_run_id=(
                    str(evidence.get("verifier_run_id") or "").strip() or None
                ),
            ),
        )


def _add_relation(
    relations: dict[str, list[_EvidenceRelation]],
    artifact_id: object,
    relation: _EvidenceRelation,
) -> None:
    normalized_artifact_id = str(artifact_id or "").strip()
    if not normalized_artifact_id:
        return
    if relation not in relations[normalized_artifact_id]:
        relations[normalized_artifact_id].append(relation)


def _write_vulnerability(
    archive: zipfile.ZipFile,
    project_id: str,
    evidence: _VulnerabilityEvidence,
) -> None:
    vulnerability = evidence.vulnerability
    title = _safe_vulnerability_title(str(vulnerability.get("title") or "漏洞"))
    vulnerability_id = _safe_archive_component(str(vulnerability["id"]), fallback="vulnerability")
    directory = f"vulnerabilities/{title}--{vulnerability_id}"

    _write_text_member(
        archive,
        f"{directory}/vulnerability.md",
        _vulnerability_markdown(evidence),
    )
    _write_text_member(
        archive,
        f"{directory}/observations.md",
        _observations_markdown(evidence),
    )
    _write_text_member(
        archive,
        f"{directory}/verifications.md",
        _verifications_markdown(evidence),
    )
    included: list[tuple[_ArtifactEvidence, str]] = []
    excluded: list[tuple[_ArtifactEvidence, str]] = []
    for artifact_evidence in evidence.artifacts:
        artifact = artifact_evidence.artifact
        filename = _artifact_filename(artifact.uri, artifact.id)
        exclusion = _artifact_exclusion_reason(artifact, filename)
        if exclusion:
            excluded.append((artifact_evidence, exclusion))
            continue
        try:
            content = artifact_content_file(project_id, artifact.id)
        except HTTPException as exc:
            raise VulnerabilityEvidenceArchiveUnavailableError(artifact.id) from exc
        exclusion = _media_type_exclusion_reason(content.media_type)
        if exclusion:
            excluded.append((artifact_evidence, exclusion))
            continue
        safe_artifact_id = _safe_archive_component(artifact.id, fallback="artifact")
        safe_filename = _safe_archive_component(
            content.filename,
            fallback=f"{safe_artifact_id}-file",
        )
        member_path = f"{directory}/artifacts/{safe_artifact_id}/{safe_filename}"
        _write_artifact(archive, artifact_evidence, content, member_path)
        included.append((artifact_evidence, member_path))

    _write_text_member(
        archive,
        f"{directory}/evidence-index.md",
        _evidence_index_markdown(included, excluded),
    )


def _write_artifact(
    archive: zipfile.ZipFile,
    artifact_evidence: _ArtifactEvidence,
    content: ArtifactContentFile,
    member_path: str,
) -> None:
    artifact = artifact_evidence.artifact
    if not content.disk_path.is_file():
        raise VulnerabilityEvidenceArchiveUnavailableError(artifact.id)
    member = _zip_member(member_path)
    digest = sha256()
    size = 0
    try:
        with content.disk_path.open("rb") as source, archive.open(member, "w", force_zip64=True) as target:
            while chunk := source.read(_ARCHIVE_COPY_BYTES):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as exc:
        raise VulnerabilityEvidenceArchiveUnavailableError(artifact.id) from exc
    if size != artifact.size or digest.hexdigest() != artifact.sha256:
        raise VulnerabilityEvidenceArchiveUnavailableError(artifact.id)


def _write_text_member(archive: zipfile.ZipFile, member_path: str, content: str) -> None:
    archive.writestr(_zip_member(member_path), content.replace("\r\n", "\n").encode("utf-8"))


def _zip_member(member_path: str) -> zipfile.ZipInfo:
    member = zipfile.ZipInfo(member_path, date_time=_ARCHIVE_TIMESTAMP)
    member.compress_type = zipfile.ZIP_DEFLATED
    member.external_attr = 0o100644 << 16
    return member


def _archive_readme(project_id: str, vulnerabilities: list[_VulnerabilityEvidence]) -> str:
    return (
        "Arena 漏洞证据归档\n"
        f"项目 ID: {project_id}\n"
        f"漏洞数量: {len(vulnerabilities)}\n\n"
        "本归档只包含通过漏洞 packet、Observation、Fact Evidence 或 Verification "
        "明确关联的证据。\n"
        "所有图片、JavaScript 和 HTML 文件均已排除；排除原因记录在每个漏洞的 "
        "evidence-index.md。\n"
        "即使没有可导出的 Artifact，每个漏洞仍保留数据库中的复现步骤、请求、响应、"
        "Observation 与 Verification 文字记录。\n"
    )


def _vulnerability_markdown(evidence: _VulnerabilityEvidence) -> str:
    vulnerability = evidence.vulnerability
    fact = evidence.facts.get(str(vulnerability.get("source_fact_id") or ""))
    lines = [
        f"# {vulnerability.get('title') or '未命名漏洞'}",
        "",
        f"- ID: `{vulnerability.get('id')}`",
        f"- Severity: `{vulnerability.get('severity')}`",
        f"- Status: `{vulnerability.get('status')}`",
        f"- Type: {_display(vulnerability.get('vuln_type'))}",
        f"- Target: {_display(vulnerability.get('target'))}",
        f"- Confidence: {_display(vulnerability.get('confidence'))}",
        f"- Fingerprint: {_display(vulnerability.get('fingerprint'))}",
        f"- Source Fact: {_code_or_empty(vulnerability.get('source_fact_id'))}",
        f"- Packet Artifact: {_code_or_empty(vulnerability.get('packet_artifact_id'))}",
        f"- Discovery Run: {_code_or_empty(vulnerability.get('discovered_by_run_id'))}",
        f"- Discovery Worker: {_display(vulnerability.get('discovered_by_worker'))}",
        f"- Slot: {_display(vulnerability.get('slot_key'))}",
        f"- Created At: {_display(vulnerability.get('created_at'))}",
        f"- Updated At: {_display(vulnerability.get('updated_at'))}",
        "",
        "## Identity",
        "",
        _json_block(vulnerability.get("identity_json"), vulnerability.get("identity")),
        "",
        "## Source Fact",
        "",
        _display(fact.get("description") if fact else None),
        "",
        "## Remediation",
        "",
        _display(vulnerability.get("remediation")),
        "",
        "## Reproduction Steps",
        "",
        _display(vulnerability.get("repro_steps")),
        "",
        "## Raw Request",
        "",
        _fenced_block(vulnerability.get("repro_request")),
        "",
        "## Raw Response",
        "",
        _fenced_block(vulnerability.get("repro_response")),
        "",
    ]
    return "\n".join(lines)


def _observations_markdown(evidence: _VulnerabilityEvidence) -> str:
    lines = ["# Observations", ""]
    if not evidence.observations:
        return "\n".join((*lines, "当前漏洞没有 Observation 记录。", ""))
    for index, observation in enumerate(evidence.observations, start=1):
        fact = evidence.facts.get(str(observation.get("source_fact_id") or ""))
        lines.extend(
            [
                f"## {index}. {observation.get('id')}",
                "",
                f"- Observed At: {_display(observation.get('observed_at'))}",
                f"- Severity: `{observation.get('severity')}`",
                f"- Confidence: {_display(observation.get('confidence'))}",
                f"- Worker: {_display(observation.get('worker'))}",
                f"- Task Run: {_code_or_empty(observation.get('task_run_id'))}",
                f"- Slot: {_display(observation.get('slot_key'))}",
                f"- Source Fact: {_code_or_empty(observation.get('source_fact_id'))}",
                f"- Packet Artifact: {_code_or_empty(observation.get('packet_artifact_id'))}",
                "",
                "### Identity",
                "",
                _json_block(observation.get("identity_json"), observation.get("identity")),
                "",
                "### Source Fact",
                "",
                _display(fact.get("description") if fact else None),
                "",
                "### Raw Request",
                "",
                _fenced_block(observation.get("repro_request")),
                "",
                "### Raw Response",
                "",
                _fenced_block(observation.get("repro_response")),
                "",
            ]
        )
    return "\n".join(lines)


def _verifications_markdown(evidence: _VulnerabilityEvidence) -> str:
    lines = ["# Verifications", ""]
    if not evidence.verifications:
        return "\n".join((*lines, "当前漏洞没有 Verification 记录。", ""))
    for index, verification in enumerate(evidence.verifications, start=1):
        fact = evidence.facts.get(str(verification.get("fact_id") or ""))
        lines.extend(
            [
                f"## {index}. {verification.get('id')}",
                "",
                f"- Outcome: `{verification.get('outcome')}`",
                f"- Independence: `{verification.get('independence_level')}`",
                f"- Observation: {_code_or_empty(verification.get('observation_id'))}",
                f"- Intent: {_code_or_empty(verification.get('intent_id'))}",
                f"- Discovery Run: {_code_or_empty(verification.get('discovery_task_run_id'))}",
                f"- Discovery Worker: {_display(verification.get('discovery_worker'))}",
                f"- Verifier Run: {_code_or_empty(verification.get('verifier_task_run_id'))}",
                f"- Verifier Worker: {_display(verification.get('verifier_worker'))}",
                f"- Verification Fact: {_code_or_empty(verification.get('fact_id'))}",
                f"- Created At: {_display(verification.get('created_at'))}",
                "",
                "### Verification Fact",
                "",
                _display(fact.get("description") if fact else None),
                "",
                "### Notes",
                "",
                _display(verification.get("notes")),
                "",
                "### Evidence References",
                "",
            ]
        )
        refs = verification["evidence_refs"]
        if refs:
            for ref in refs:
                detail = f"`{ref['artifact_id']}`"
                if ref.get("pointer"):
                    detail += f" — pointer: `{ref['pointer']}`"
                if ref.get("verifier_run_id"):
                    detail += f" — verifier run: `{ref['verifier_run_id']}`"
                lines.append(f"- {detail}")
        else:
            lines.append("- 无有效 Artifact 引用")
        lines.append("")
    return "\n".join(lines)


def _evidence_index_markdown(
    included: list[tuple[_ArtifactEvidence, str]],
    excluded: list[tuple[_ArtifactEvidence, str]],
) -> str:
    lines = ["# Evidence Index", "", "## Included Artifacts", ""]
    if not included:
        lines.append("没有符合格式规则的 Artifact；请查看同目录中的文字证据。")
    for item, member_path in included:
        artifact = item.artifact
        lines.extend(
            [
                f"### {artifact.id}",
                "",
                f"- ZIP Path: `{member_path}`",
                f"- Kind: `{artifact.kind}`",
                f"- Category: `{artifact.category}`",
                f"- Filename: `{_artifact_filename(artifact.uri, artifact.id)}`",
                f"- Size: `{artifact.size}` bytes",
                f"- SHA-256: `{artifact.sha256}`",
                "- Relations:",
                *_relation_lines(item.relations),
                "",
            ]
        )
    lines.extend(["## Excluded Linked Artifacts", ""])
    if not excluded:
        lines.append("没有因格式规则被排除的关联 Artifact。")
    for item, reason in excluded:
        artifact = item.artifact
        lines.extend(
            [
                f"### {artifact.id}",
                "",
                f"- Filename: `{_artifact_filename(artifact.uri, artifact.id)}`",
                f"- Kind: `{artifact.kind}`",
                f"- Exclusion: {reason}",
                "- Relations:",
                *_relation_lines(item.relations),
                "",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _relation_lines(relations: list[_EvidenceRelation]) -> list[str]:
    lines: list[str] = []
    for relation in relations:
        detail = relation.source
        if relation.pointer:
            detail += f" — pointer: `{relation.pointer}`"
        if relation.verifier_run_id:
            detail += f" — verifier run: `{relation.verifier_run_id}`"
        lines.append(f"  - {detail}")
    return lines


def _artifact_exclusion_reason(artifact: Artifact, filename: str) -> str | None:
    if artifact.kind == "screenshot":
        return "kind=screenshot（图片证据已排除）"
    extension = PurePosixPath(filename).suffix.lower()
    if extension in _IMAGE_EXTENSIONS:
        return f"图片扩展名 {extension} 已排除"
    if extension in _JAVASCRIPT_EXTENSIONS:
        return f"JavaScript 扩展名 {extension} 已排除"
    if extension in _HTML_EXTENSIONS:
        return f"HTML 扩展名 {extension} 已排除"
    guessed_media_type, _ = mimetypes.guess_type(filename)
    return _media_type_exclusion_reason(guessed_media_type)


def _media_type_exclusion_reason(media_type: str | None) -> str | None:
    normalized = str(media_type or "").split(";", 1)[0].strip().lower()
    if normalized.startswith("image/"):
        return f"图片 MIME {normalized} 已排除"
    if normalized in _JAVASCRIPT_MEDIA_TYPES:
        return f"JavaScript MIME {normalized} 已排除"
    if normalized in _HTML_MEDIA_TYPES:
        return f"HTML MIME {normalized} 已排除"
    return None


def _artifact_filename(uri: str, fallback: str) -> str:
    filename = PurePosixPath(str(uri or "").rstrip("/")).name
    return filename or fallback


def _safe_archive_component(value: str, *, fallback: str) -> str:
    cleaned = _ARCHIVE_COMPONENT_RE.sub("_", Path(value).name).strip("._")
    return cleaned or fallback


def _safe_vulnerability_title(value: str) -> str:
    cleaned = _ARCHIVE_TITLE_RE.sub("_", value)
    cleaned = re.sub(r"\.{2,}", "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return cleaned[:80].rstrip(" ._") or "漏洞"


def _display(value: object) -> str:
    text = str(value or "").strip()
    return text or "未记录"


def _code_or_empty(value: object) -> str:
    text = str(value or "").strip()
    return f"`{text}`" if text else "未记录"


def _json_block(raw_value: object, parsed_value: object = None) -> str:
    value = parsed_value
    if value is None:
        try:
            value = json.loads(str(raw_value or "{}"))
        except (TypeError, ValueError):
            value = {}
    return _fenced_block(json.dumps(value or {}, ensure_ascii=False, indent=2, sort_keys=True), "json")


def _fenced_block(value: object, language: str = "text") -> str:
    text = str(value or "").strip()
    if not text:
        return "未记录"
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


# 旧内部名称保留别名，避免第三方导入在内容契约变更时立即失效。
ArtifactArchiveUnavailableError = VulnerabilityEvidenceArchiveUnavailableError
ProjectArtifactArchive = ProjectVulnerabilityEvidenceArchive
create_project_artifact_archive = create_project_vulnerability_evidence_archive
