"""报告 Artifact inventory API 的专用响应 schema。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReportInventoryItem(BaseModel):
    artifact_id: str
    project_id: str
    project_title: str
    task_run_id: str
    artifact_uri: str
    artifact_sha256: str
    artifact_size: int
    created_at: str
    template_key: str
    template_version: int | None = None
    packet_version: int | None = None
    output_contract_version: int | None = None
    report_data_schema_version: int | None = None
    template_digest_sha256: str
    report_policy_version: int
    report_execution_plan_sha256: str
    report_prompt_digest_sha256: str
    input_snapshot_artifact_id: str
    input_snapshot_sha256: str
    completeness_rule_version: int


class ReportInventoryPage(BaseModel):
    items: list[ReportInventoryItem] = Field(default_factory=list)
    total: int = 0
    page: int
    page_size: int


__all__ = [name for name in globals() if not name.startswith("_")]
