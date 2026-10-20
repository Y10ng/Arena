"""严格识别并分页返回可展示的历史评估报告 Artifact。"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from cairn.server.schemas.reports import ReportInventoryItem, ReportInventoryPage
from cairn.server.services.report_policy import (
    REPORT_DATA_SCHEMA_VERSION,
    REPORT_OUTPUT_CONTRACT_VERSION,
    REPORT_PACKET_VERSION,
)

_REPORT_FILENAME_SUFFIX = "vulnerability-report.md"
_REPORT_PREFIXED_FILENAME_SUFFIX = f"-{_REPORT_FILENAME_SUFFIX}"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _report_artifact_filename(uri: object) -> str | None:
    """识别历史无前缀和生产环境 Artifact ID 前缀的最终报告文件。"""
    text = _non_empty_text(uri)
    if text is None:
        return None
    filename = text.rsplit("/", 1)[-1]
    if filename == _REPORT_FILENAME_SUFFIX:
        return filename
    prefix, separator, suffix = filename.partition(_REPORT_PREFIXED_FILENAME_SUFFIX)
    if prefix and separator and not suffix:
        return filename
    return None


def _non_empty_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _digest(value: object) -> str | None:
    text = _non_empty_text(value)
    if text is None or _SHA256_RE.fullmatch(text) is None:
        return None
    return text


def _report_provenance(metadata_json: object) -> dict[str, Any] | None:
    """只接受 Dispatcher 最终报告写入的完整、类型正确的 provenance。"""
    try:
        metadata = json.loads(str(metadata_json))
    except (TypeError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    if metadata.get("source") != "report_task" or metadata.get("format") != "markdown":
        return None

    generation_token = _non_empty_text(metadata.get("report_generation_token"))
    template_key = _non_empty_text(metadata.get("report_template_key"))
    template_version = _positive_integer(metadata.get("report_template_version"))
    protocol_versions = {
        "packet_version": _positive_integer(metadata.get("packet_version")),
        "output_contract_version": _positive_integer(metadata.get("output_contract_version")),
        "report_data_schema_version": _positive_integer(metadata.get("report_data_schema_version")),
    }
    if "report_template_version" in metadata:
        # 历史成品沿用原有修订身份；不能把损坏版本或新合同伪装成旧报告。
        if template_version is None or any(key in metadata for key in protocol_versions):
            return None
    elif protocol_versions != {
        "packet_version": REPORT_PACKET_VERSION,
        "output_contract_version": REPORT_OUTPUT_CONTRACT_VERSION,
        "report_data_schema_version": REPORT_DATA_SCHEMA_VERSION,
    }:
        return None
    template_digest = _digest(metadata.get("report_template_digest_sha256"))
    policy_version = _positive_integer(metadata.get("report_policy_version"))
    execution_digest = _digest(metadata.get("report_execution_plan_sha256"))
    prompt_digest = _digest(metadata.get("report_prompt_digest_sha256"))
    snapshot_artifact_id = _non_empty_text(metadata.get("input_snapshot_artifact_id"))
    snapshot_digest = _digest(metadata.get("input_snapshot_sha256"))
    completeness_version = _positive_integer(metadata.get("completeness_rule_version"))
    if any(
        value is None
        for value in (
            generation_token,
            template_key,
            template_digest,
            policy_version,
            execution_digest,
            prompt_digest,
            snapshot_artifact_id,
            snapshot_digest,
            completeness_version,
        )
    ):
        return None

    # generation token 只参与分类，绝不进入返回模型。
    return {
        "template_key": template_key,
        "template_version": template_version,
        **protocol_versions,
        "template_digest_sha256": template_digest,
        "report_policy_version": policy_version,
        "report_execution_plan_sha256": execution_digest,
        "report_prompt_digest_sha256": prompt_digest,
        "input_snapshot_artifact_id": snapshot_artifact_id,
        "input_snapshot_sha256": snapshot_digest,
        "completeness_rule_version": completeness_version,
    }


def list_report_inventory(
    conn: sqlite3.Connection,
    *,
    tenant_id: str | None,
    q: str | None = None,
    project_id: str | None = None,
    template_key: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> ReportInventoryPage:
    """从 Artifact 历史构建 inventory，不依赖每项目唯一的 finalization 行。"""
    clauses = ["artifact.kind = 'report'", "artifact.category = 'deliverable'"]
    params: list[object] = []
    if tenant_id is not None:
        clauses.append("project.tenant_id = ?")
        params.append(tenant_id)
    if project_id is not None:
        clauses.append("artifact.project_id = ?")
        params.append(project_id)
    if date_from is not None:
        clauses.append("artifact.created_at >= ?")
        params.append(date_from)
    if date_to is not None:
        clauses.append("artifact.created_at <= ?")
        params.append(date_to)

    rows = conn.execute(
        f"""
        SELECT artifact.id AS artifact_id,
               artifact.project_id,
               project.title AS project_title,
               artifact.task_run_id,
               artifact.uri AS artifact_uri,
               artifact.sha256 AS artifact_sha256,
               artifact.size AS artifact_size,
               artifact.created_at,
               artifact.metadata_json
        FROM artifacts artifact
        JOIN projects project ON project.id = artifact.project_id
        WHERE {' AND '.join(clauses)}
        ORDER BY artifact.created_at DESC, artifact.id DESC
        """,
        params,
    ).fetchall()

    normalized_q = q.strip().casefold() if q is not None else ""
    items: list[ReportInventoryItem] = []
    for row in rows:
        report_filename = _report_artifact_filename(row["artifact_uri"])
        if report_filename is None:
            continue
        task_run_id = _non_empty_text(row["task_run_id"])
        artifact_sha256 = _digest(row["artifact_sha256"])
        if task_run_id is None or artifact_sha256 is None:
            continue
        provenance = _report_provenance(row["metadata_json"])
        if provenance is None:
            continue
        if template_key is not None and provenance["template_key"] != template_key:
            continue
        if normalized_q and normalized_q not in " ".join(
            (
                str(row["artifact_id"]),
                str(row["project_id"]),
                str(row["project_title"]),
                str(row["artifact_uri"]),
                report_filename,
                str(provenance["template_key"]),
            )
        ).casefold():
            continue
        items.append(
            ReportInventoryItem(
                artifact_id=row["artifact_id"],
                project_id=row["project_id"],
                project_title=row["project_title"],
                task_run_id=task_run_id,
                artifact_uri=row["artifact_uri"],
                artifact_sha256=artifact_sha256,
                artifact_size=row["artifact_size"],
                created_at=row["created_at"],
                **provenance,
            )
        )

    total = len(items)
    start = (page - 1) * page_size
    return ReportInventoryPage(
        items=items[start : start + page_size],
        total=total,
        page=page,
        page_size=page_size,
    )


__all__ = ["list_report_inventory"]
