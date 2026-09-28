"""超级管理员固定管理协议与 Tenant 安全监控 schema。"""

from __future__ import annotations

from .common import BaseModel, Field
from .monitoring_summary import MonitoringSummary, VulnerabilitiesSummary


class AdminUserCreateRequest(BaseModel):
    login: str


class AdminUserStatusRequest(BaseModel):
    disabled: bool
    expected_updated_at: str


class AdminUserPasswordResetRequest(BaseModel):
    password: str = Field(repr=False, json_schema_extra={"writeOnly": True})
    expected_updated_at: str


class AdminUserView(BaseModel):
    id: str
    login: str
    role: str
    disabled: bool
    must_change_password: bool
    tenant_id: str | None = None
    active_session_count: int = 0
    created_at: str
    updated_at: str


class AdminTenantView(BaseModel):
    id: str
    kind: str
    name: str
    member_count: int = 0
    project_count: int = 0
    created_at: str


class AdminProjectView(BaseModel):
    id: str
    tenant_id: str | None = None
    title: str
    status: str
    scenario_key: str | None = None
    group_id: str | None = None
    created_at: str


class AdminVulnerabilityView(BaseModel):
    id: str
    project_id: str
    tenant_id: str | None = None
    project_title: str
    title: str
    severity: str
    vuln_type: str
    target: str
    confidence: float
    status: str
    created_at: str
    updated_at: str


class AdminSecurityAuditEventView(BaseModel):
    id: str
    actor_type: str
    actor_id: str | None = None
    tenant_id: str | None = None
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    outcome: str
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: str


class TenantDispatcherLiveness(BaseModel):
    status: str | None = None
    stale: bool = False
    updated_at: str | None = None


class TenantTaskRunView(BaseModel):
    id: str
    project_id: str
    project_title: str
    task_type: str
    worker_name: str
    status: str
    queued_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: int | None = None
    heartbeat_status: str = "not_tracked"


class TenantMonitoringSummary(BaseModel):
    generated_at: str
    long_running_threshold_seconds: int
    project_count: int = 0
    active_project_count: int = 0
    dispatcher: TenantDispatcherLiveness = Field(default_factory=TenantDispatcherLiveness)
    queued_tasks: list[TenantTaskRunView] = Field(default_factory=list)
    running_tasks: list[TenantTaskRunView] = Field(default_factory=list)
    long_running_tasks: list[TenantTaskRunView] = Field(default_factory=list)
    recent_failures: list[TenantTaskRunView] = Field(default_factory=list)
    vulnerabilities: VulnerabilitiesSummary = Field(default_factory=VulnerabilitiesSummary)


class AdminMonitoringView(MonitoringSummary):
    """固定名称标识全局监控只能从 admin surface 返回。"""
