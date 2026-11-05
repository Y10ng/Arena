"""首页只读摘要；预览不包含输入、证据正文或执行配置。"""

from pydantic import BaseModel, Field

from .common import ProjectStatus
from .consumption import ConsumptionSummary


class OverviewProjectCounts(BaseModel):
    total: int
    by_status: dict[str, int]


class OverviewVulnerabilityCounts(BaseModel):
    total: int
    by_severity: dict[str, int]
    by_status: dict[str, int]


class OverviewProjectPreview(BaseModel):
    id: str
    title: str
    status: ProjectStatus
    created_at: str
    consumption: ConsumptionSummary | None = None


class OverviewVulnerabilityPreview(BaseModel):
    id: str
    project_id: str
    project_title: str
    title: str
    severity: str
    status: str
    created_at: str


class OverviewResponse(BaseModel):
    generated_at: str
    consumption: dict
    projects: OverviewProjectCounts
    vulnerabilities: OverviewVulnerabilityCounts
    project_previews: list[OverviewProjectPreview] = Field(max_length=6)
    vulnerability_previews: list[OverviewVulnerabilityPreview] = Field(max_length=5)
