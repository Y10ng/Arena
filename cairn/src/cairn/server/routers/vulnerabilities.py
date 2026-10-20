"""漏洞管理接口：所有场景通用的漏洞 CRUD + fact 关联查询。

worker 输出 fact 带 vulnerability 字段时由 conclude 端点自动确认入库（见 routers/intents.py）；
本 router 提供人工独立创建、列表、详情、状态流转、fact→漏洞查询。
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from cairn.server.auth import (
    RequestPrincipal,
    protect_cookie_request,
    require_tenant_project,
    require_user_tenant_project,
    write_security_audit,
)
from cairn.server.db import get_conn
from cairn.server.models import (
    CreateVulnerabilityRequest,
    UpdateVulnerabilityRequest,
    Vulnerability,
    VulnerabilityChainCandidate,
    DuplicateVulnerabilityCandidatesResponse,
    VulnerabilityDetail,
    VulnerabilityListItem,
    VulnerabilityProvenance,
    VulnerabilityVerifierTarget,
)
from cairn.server.services import (
    build_duplicate_vulnerability_candidates,
    build_vulnerability_chain_candidates,
    create_vulnerability,
    get_vulnerability_detail,
    get_vulnerability_provenance,
    get_observation_verifier_target,
    get_vulnerability_for_fact,
    list_all_vulnerabilities,
    list_vulnerabilities,
    update_vulnerability,
)

router = APIRouter(tags=["vulnerabilities"])


@router.get(
    "/vulnerabilities",
    response_model=list[VulnerabilityListItem],
)
def get_all_vulnerabilities_route(
    severity: str | None = Query(default=None),
    status: str | None = Query(default=None),
    current_tenant_only: bool = Query(default=False),
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    # 普通成果查看保留超管全局视角；复测候选只取当前创建租户，不接受客户端指定归属。
    tenant_id = None if principal.is_super_admin and not current_tenant_only else principal.tenant_id
    with get_conn() as conn:
        results = list_all_vulnerabilities(
            conn,
            tenant_id=tenant_id,
            severity=severity,
            status=status,
            principal=principal,
        )
        if principal.is_super_admin:
            # 跨 Tenant 读取仍需保留与显式管理接口一致的审计轨迹。
            write_security_audit(
                conn,
                actor_type="user",
                actor_id=principal.subject_id,
                tenant_id=principal.tenant_id,
                action="admin.vulnerabilities.read",
                resource_type="admin_read_model",
                resource_id=None,
                outcome="success",
            )
        return results


@router.get(
    "/projects/{project_id}/vulnerabilities",
    response_model=list[Vulnerability],
    dependencies=[Depends(require_tenant_project)],
)
def get_vulnerabilities_route(
    project_id: str,
    severity: str | None = Query(default=None),
    status: str | None = Query(default=None),
):
    with get_conn() as conn:
        return list_vulnerabilities(conn, project_id, severity=severity, status=status)


@router.get(
    "/projects/{project_id}/vulnerability-chain-candidates",
    response_model=list[VulnerabilityChainCandidate],
    dependencies=[Depends(require_tenant_project)],
)
def get_vulnerability_chain_candidates_route(project_id: str):
    with get_conn() as conn:
        return build_vulnerability_chain_candidates(conn, project_id)


@router.get(
    "/projects/{project_id}/vulnerabilities/{vuln_id}/duplicate-candidates",
    response_model=DuplicateVulnerabilityCandidatesResponse,
    dependencies=[Depends(require_tenant_project)],
)
def get_duplicate_vulnerability_candidates_route(project_id: str, vuln_id: str):
    with get_conn() as conn:
        return build_duplicate_vulnerability_candidates(conn, project_id, vuln_id)


@router.post(
    "/projects/{project_id}/vulnerabilities",
    response_model=Vulnerability,
    status_code=201,
    dependencies=[Depends(require_user_tenant_project)],
)
def create_vulnerability_route(project_id: str, body: CreateVulnerabilityRequest):
    # 人工入口不能冒充某次自动发现；自动来源由 conclude 事务绑定真实 Fact/TaskRun。
    if body.source_fact_id is not None:
        raise HTTPException(422, "automatic_vulnerability_requires_conclude")
    with get_conn() as conn:
        return create_vulnerability(conn, project_id, body)


@router.get(
    "/projects/{project_id}/vulnerability-observations/{observation_id}/verifier-target",
    response_model=VulnerabilityVerifierTarget,
    dependencies=[Depends(require_tenant_project)],
)
def get_verifier_target_route(project_id: str, observation_id: str):
    with get_conn() as conn:
        return get_observation_verifier_target(conn, project_id, observation_id)


@router.get(
    "/projects/{project_id}/vulnerabilities/{vuln_id}",
    response_model=VulnerabilityDetail,
    dependencies=[Depends(require_tenant_project)],
)
def get_vulnerability_route(project_id: str, vuln_id: str, observation_id: str | None = None):
    # 主证据和补充资料始终来自服务端选定的同一次发现。
    with get_conn() as conn:
        return get_vulnerability_detail(conn, project_id, vuln_id, observation_id)


@router.get(
    "/projects/{project_id}/vulnerabilities/{vuln_id}/provenance",
    response_model=VulnerabilityProvenance,
    dependencies=[Depends(require_tenant_project)],
)
def get_vulnerability_provenance_route(project_id: str, vuln_id: str):
    with get_conn() as conn:
        return get_vulnerability_provenance(conn, project_id, vuln_id)


@router.patch(
    "/projects/{project_id}/vulnerabilities/{vuln_id}",
    response_model=Vulnerability,
    dependencies=[Depends(require_tenant_project)],
)
def update_vulnerability_route(project_id: str, vuln_id: str, body: UpdateVulnerabilityRequest):
    with get_conn() as conn:
        return update_vulnerability(conn, project_id, vuln_id, body)


@router.get(
    "/projects/{project_id}/facts/{fact_id}/vulnerability",
    response_model=Vulnerability | None,
    dependencies=[Depends(require_tenant_project)],
)
def get_fact_vulnerability_route(project_id: str, fact_id: str):
    # 前端 fact 详情用：查该 fact 关联的漏洞（1:1）。
    with get_conn() as conn:
        return get_vulnerability_for_fact(conn, project_id, fact_id)
