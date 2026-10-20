"""项目与目标接口，维护 fact graph 的根节点、运行状态和 reason 租约。"""

from fastapi import APIRouter, Depends

from cairn.server.auth import (
    RequestPrincipal,
    protect_cookie_request,
    require_dispatcher_protocol,
    require_tenant_project,
    require_user,
    require_user_tenant_project,
)
from cairn.server.db import get_conn
from cairn.server.models import (
    AssessmentReportClaimRequest,
    AssessmentReportClaimView,
    AssessmentReportFinalizationView,
    AssessmentReportFinishReceiptView,
    AssessmentReportFinishReplayRequest,
    AssessmentReportFinishRequest,
    AssessmentReportGenerationRequest,
    AssessmentReportPacket,
    AssessmentReportSnapshotRequest,
    CompleteRequest,
    DepthReviewRequest,
    DepthReviewResponse,
    CreateTargetRequest,
    CreateProjectRequest,
    HeartbeatRequest,
    Intent,
    Library,
    ProjectDetail,
    ProjectManagementOptions,
    ProjectMeta,
    ProjectSummary,
    ReauthorizeProjectRequest,
    ReopenRequest,
    ReopenResponse,
    ReasonClaimRequest,
    Target,
    UpdateProjectTitleRequest,
    UpdateProjectStatusRequest,
    UpdateProjectWorkersRequest,
    UpdateTargetRequest,
)
from cairn.server.services import (
    bind_assessment_report_input_snapshot,
    build_assessment_report_packet,
    claim_assessment_report_finalization,
    claim_project_reason_record,
    complete_project_record,
    create_target_record,
    delete_project_record,
    finish_assessment_report_finalization,
    get_assessment_report_finalization,
    get_project_detail,
    heartbeat_project_reason_record,
    list_project_summaries,
    list_selectable_current_libraries,
    list_target_records,
    list_tenant_workers,
    release_project_reason_record,
    replay_assessment_report_finalization,
    reopen_project_record,
    request_assessment_report_generation,
    update_project_status_record,
    project_lifecycle_action,
    reauthorize_project_record,
    update_project_title_record,
    update_project_workers_record,
    update_target_record,
)

router = APIRouter(tags=["projects"])


# Project 端点只做 HTTP 编排，状态迁移和 graph 不变量集中在 service 层校验。
@router.get("/projects", response_model=list[ProjectSummary])
def list_projects(principal: RequestPrincipal = Depends(require_dispatcher_protocol)):
    tenant_id = None if principal.kind == "machine" or principal.is_super_admin else principal.tenant_id
    return list_project_summaries(tenant_id, include_verification=principal.kind == "machine")


@router.get("/library-catalog", response_model=list[Library])
def get_library_catalog(_principal: RequestPrincipal = Depends(require_user)):
    with get_conn() as conn:
        return list_selectable_current_libraries(conn)


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def create_project(
    body: CreateProjectRequest,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    from cairn.server.services.creation_requests import create_project_request

    return create_project_request(body, tenant_id=principal.tenant_id, user_id=principal.subject_id)


@router.get(
    "/projects/{project_id}",
    response_model=ProjectDetail,
    dependencies=[Depends(require_tenant_project)],
)
def get_project(project_id: str):
    return get_project_detail(project_id)


def _project_tenant_id(project_id: str) -> str:
    """从目标项目解析 Tenant，禁止使用操作者或客户端提供的 Tenant。"""
    with get_conn() as conn:
        row = conn.execute("SELECT tenant_id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        from fastapi import HTTPException

        raise HTTPException(404, "Project not found")
    return str(row["tenant_id"])


@router.get(
    "/projects/{project_id}/management-options",
    response_model=ProjectManagementOptions,
    dependencies=[Depends(require_user_tenant_project)],
)
def get_project_management_options(project_id: str):
    tenant_id = _project_tenant_id(project_id)
    return ProjectManagementOptions(
        tenant_workers=list_tenant_workers(tenant_id=tenant_id),
    )


@router.get("/projects/{project_id}/targets", response_model=list[Target], dependencies=[Depends(require_tenant_project)])
def list_targets(project_id: str):
    return list_target_records(project_id)


@router.post("/projects/{project_id}/targets", response_model=Target, status_code=201, dependencies=[Depends(require_user_tenant_project)])
def create_target(project_id: str, body: CreateTargetRequest):
    return create_target_record(project_id, body)


@router.put("/projects/{project_id}/targets/{target_id}", response_model=Target, dependencies=[Depends(require_user_tenant_project)])
def update_target(project_id: str, target_id: str, body: UpdateTargetRequest):
    return update_target_record(project_id, target_id, body)


@router.delete("/projects/{project_id}", status_code=204, dependencies=[Depends(require_user_tenant_project)])
def delete_project(project_id: str):
    delete_project_record(project_id)


@router.put("/projects/{project_id}/title", response_model=ProjectMeta, dependencies=[Depends(require_user_tenant_project)])
def update_project_title(project_id: str, body: UpdateProjectTitleRequest):
    return update_project_title_record(project_id, body)


@router.post("/projects/{project_id}/pause", response_model=ProjectMeta, dependencies=[Depends(require_user_tenant_project)])
def pause_project(project_id: str):
    return project_lifecycle_action(project_id, "pause")


@router.post("/projects/{project_id}/resume", response_model=ProjectMeta, dependencies=[Depends(require_user_tenant_project)])
def resume_project(project_id: str):
    return project_lifecycle_action(project_id, "resume")


@router.post("/projects/{project_id}/reauthorize", response_model=ProjectMeta)
def reauthorize_project(
    project_id: str,
    body: ReauthorizeProjectRequest,
    principal: RequestPrincipal = Depends(require_user_tenant_project),
):
    return reauthorize_project_record(
        project_id,
        body,
        actor_id=principal.subject_id,
    )


@router.post("/projects/{project_id}/finish", response_model=ProjectMeta, dependencies=[Depends(require_user_tenant_project)])
def finish_project(project_id: str):
    return project_lifecycle_action(project_id, "finish")


@router.put("/projects/{project_id}/status", response_model=ProjectMeta, deprecated=True, dependencies=[Depends(require_user_tenant_project)])
def update_project_status(project_id: str, body: UpdateProjectStatusRequest):
    return update_project_status_record(project_id, body)


@router.put("/projects/{project_id}/workers", response_model=ProjectMeta)
def update_project_workers(
    project_id: str,
    body: UpdateProjectWorkersRequest,
    _principal: RequestPrincipal = Depends(require_user_tenant_project),
):
    return update_project_workers_record(
        project_id,
        body,
        tenant_id=_project_tenant_id(project_id),
    )


@router.post("/projects/{project_id}/reason/claim", response_model=ProjectMeta, dependencies=[Depends(require_tenant_project)])
def claim_project_reason(project_id: str, body: ReasonClaimRequest):
    # reason 是项目级租约，和 intent worker 租约分离，防止决策阶段并发写入 graph。
    return claim_project_reason_record(project_id, body)


@router.post("/projects/{project_id}/reason/heartbeat", response_model=ProjectMeta, dependencies=[Depends(require_tenant_project)])
def heartbeat_project_reason(project_id: str, body: HeartbeatRequest):
    return heartbeat_project_reason_record(project_id, body)


@router.post("/projects/{project_id}/reason/depth-review", response_model=DepthReviewResponse, dependencies=[Depends(require_tenant_project)])
def submit_depth_review(project_id: str, body: DepthReviewRequest):
    from cairn.server.services.project_reason import accept_depth_review

    return accept_depth_review(project_id, body)


@router.post("/projects/{project_id}/reason/release", response_model=ProjectMeta, dependencies=[Depends(require_tenant_project)])
def release_project_reason(project_id: str, body: HeartbeatRequest):
    return release_project_reason_record(project_id, body)


@router.get(
    "/projects/{project_id}/assessment-report-packet",
    response_model=AssessmentReportPacket,
    dependencies=[Depends(require_tenant_project)],
)
def get_assessment_report_packet(project_id: str):
    with get_conn() as conn:
        return build_assessment_report_packet(conn, project_id)


@router.get(
    "/projects/{project_id}/assessment-report-finalization",
    response_model=AssessmentReportFinalizationView,
    dependencies=[Depends(require_tenant_project)],
)
def get_assessment_report_finalization_route(project_id: str):
    with get_conn() as conn:
        return get_assessment_report_finalization(conn, project_id)


@router.post(
    "/projects/{project_id}/assessment-report-finalization/request",
    response_model=AssessmentReportFinalizationView,
    status_code=202,
)
def request_assessment_report_generation_route(
    project_id: str,
    body: AssessmentReportGenerationRequest,
    principal: RequestPrincipal = Depends(require_user_tenant_project),
):
    with get_conn() as conn:
        return request_assessment_report_generation(
            conn,
            project_id,
            template_key=body.template_key,
            requested_by=principal.subject_id,
        )


@router.post(
    "/projects/{project_id}/assessment-report-finalization/claim",
    response_model=AssessmentReportClaimView | AssessmentReportFinishReceiptView,
)
def claim_assessment_report_finalization_route(
    project_id: str,
    body: AssessmentReportClaimRequest,
    principal: RequestPrincipal = Depends(require_tenant_project),
):
    return claim_assessment_report_finalization(
        project_id,
        body.worker,
        lease_seconds=body.lease_seconds,
        principal=principal,
    )


@router.post(
    "/projects/{project_id}/assessment-report-finalization/snapshot",
    response_model=AssessmentReportFinalizationView,
)
def bind_assessment_report_input_snapshot_route(
    project_id: str,
    body: AssessmentReportSnapshotRequest,
    principal: RequestPrincipal = Depends(require_tenant_project),
):
    return bind_assessment_report_input_snapshot(
        project_id,
        body.worker,
        claim_token=body.claim_token,
        artifact_id=body.artifact_id,
        digest_sha256=body.digest_sha256,
        principal=principal,
    )


@router.post(
    "/projects/{project_id}/assessment-report-finalization/finish",
    response_model=AssessmentReportFinishReceiptView,
)
def finish_assessment_report_finalization_route(
    project_id: str,
    body: AssessmentReportFinishRequest,
    principal: RequestPrincipal = Depends(require_tenant_project),
):
    return finish_assessment_report_finalization(
        project_id,
        body,
        principal=principal,
    )


@router.post(
    "/projects/{project_id}/assessment-report-finalization/finish-replay",
    response_model=AssessmentReportFinishReceiptView,
)
def replay_assessment_report_finalization_route(
    project_id: str,
    body: AssessmentReportFinishReplayRequest,
    principal: RequestPrincipal = Depends(require_tenant_project),
):
    return replay_assessment_report_finalization(
        project_id,
        body,
        principal=principal,
    )


@router.post("/projects/{project_id}/complete", response_model=Intent, dependencies=[Depends(require_tenant_project)])
def complete_project(project_id: str, body: CompleteRequest):
    # 完成项目走固定 goal fact，不通过普通 create_intent 暴露 goal 写入细节。
    return complete_project_record(project_id, body)


@router.post("/projects/{project_id}/reopen", response_model=ReopenResponse)
def reopen_project(
    project_id: str,
    body: ReopenRequest,
    principal: RequestPrincipal = Depends(require_user_tenant_project),
):
    # 浏览器只提交反馈内容，creator 由认证主体派生，不能作为客户端授权或归属输入。
    return reopen_project_record(project_id, body, creator=principal.subject_id)
