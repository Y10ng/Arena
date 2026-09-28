"""Artifact 与 evidence 转换服务。"""

from __future__ import annotations

from .common import *

from .payload_values import artifact_metadata_payload, payload_text

def artifact_to_model(row: sqlite3.Row) -> Artifact:
    kind = row["kind"]
    kind_deprecated = kind in FROZEN_ARTIFACT_KINDS
    # 冻结类型仍可读取，API 响应标记 deprecated 供前端展示兼容提示。
    return Artifact(
        id=row["id"],
        project_id=row["project_id"],
        task_run_id=row["task_run_id"],
        kind=kind,
        kind_deprecated=kind_deprecated,
        deprecation_message=FROZEN_ARTIFACT_KIND_MESSAGE if kind_deprecated else None,
        category=row["category"],
        uri=row["uri"],
        sha256=row["sha256"],
        size=row["size"],
        created_at=row["created_at"],
        metadata=artifact_metadata_payload(_loads_json(row["metadata_json"])),
    )


def fact_evidence_to_model(row: sqlite3.Row) -> FactEvidence:
    artifact_exists = _optional_row_bool(row, "artifact_exists", default=True)
    verifier_run_exists = _optional_row_bool(row, "verifier_run_exists", default=None)
    # 证据有效性是派生状态：artifact 或 verifier 缺失即标记无效，但不删除历史关系。
    return FactEvidence(
        project_id=row["project_id"],
        fact_id=row["fact_id"],
        artifact_id=row["artifact_id"],
        pointer=payload_text(row["pointer"]) or "",
        verifier_run_id=row["verifier_run_id"],
        artifact_exists=artifact_exists is not False,
        verifier_run_exists=verifier_run_exists,
        evidence_valid=artifact_exists is not False and verifier_run_exists is not False,
        created_at=row["created_at"],
    )


def _optional_row_bool(row: sqlite3.Row, key: str, *, default: bool | None) -> bool | None:
    if key not in row.keys():
        return default
    value = row[key]
    if value is None:
        return None
    return bool(value)
