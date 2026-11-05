"""项目与目标接口，维护 fact graph 的根节点、运行状态和 reason 租约。"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from cairn.server.auth import (
    RequestPrincipal,
    protect_cookie_request,
    require_dispatcher_protocol,
    require_dispatcher,
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
    ProjectMeta,
    ProjectSummary,
    ReauthorizeProjectRequest,
    ReopenRequest,
    ReopenResponse,
    ReasonClaimRequest,
    Target,
    UpdateProjectTitleRequest,
    UpdateProjectStatusRequest,
    UpdateTargetRequest,
)
from cairn.server.services import (
    bind_assessment_report_input_snapshot,
    build_assessment_report_packet,
    prepare_assessment_report_packet,
    claim_assessment_report_finalization,
    claim_project_reason_record,
    complete_project_record,
    create_target_record,
    delete_project_record,
    finish_assessment_report_finalization,
    get_assessment_report_finish_receipt,
    get_assessment_report_finalization,
    get_project_detail,
    heartbeat_project_reason_record,
    list_project_summaries,
    list_selectable_current_libraries,
    list_target_records,
    release_project_reason_record,
    replay_assessment_report_finalization,
    reopen_project_record,
    request_assessment_report_generation,
    update_project_status_record,
    project_lifecycle_action,
    reauthorize_project_record,
    update_project_title_record,
    update_target_record,
)

from cairn.server.services.assessment_reports import (
    cancel_assessment_report_generation,
    list_assessment_report_finalizations,
)

from cairn.server.schemas.libraries import SelectableLibraryManifest
from cairn.server.schemas.test_egress import TestEgressInput, TestEgressView
from cairn.server.services.libraries import get_selectable_library_manifest
from cairn.server.services.test_egress import resolve_task_test_egress, save_project_egress

router = APIRouter(tags=["projects"])


@router.put("/projects/{project_id}/test-egress", response_model=TestEgressView,
            dependencies=[Depends(require_user_tenant_project)])
def update_test_egress(project_id: str, body: TestEgressInput):
    with get_conn() as conn:
        return save_project_egress(conn, project_id, body)


@router.post("/projects/{project_id}/task-runs/{task_run_id}/test-egress")
def task_test_egress(project_id: str, task_run_id: str, response: Response,
                     principal: RequestPrincipal = Depends(require_dispatcher)):
    response.headers["Cache-Control"] = "no-store"
    return resolve_task_test_egress(project_id, task_run_id, principal)


# Project 端点只做 HTTP 编排，状态迁移和 graph 不变量集中在 service 层校验。
@router.get("/projects", response_model=list[ProjectSummary])
def list_projects(principal: RequestPrincipal = Depends(require_dispatcher_protocol)):
    tenant_id = None if principal.kind == "machine" or principal.is_super_admin else principal.tenant_id
    return list_project_summaries(tenant_id, include_verification=principal.kind == "machine")


@router.get("/library-catalog", response_model=list[Library])
def get_library_catalog(_principal: RequestPrincipal = Depends(require_user)):
    with get_conn() as conn:
        return list_selectable_current_libraries(conn)


@router.get("/library-catalog/{key}/manifest", response_model=SelectableLibraryManifest)
def get_library_catalog_manifest(
    key: str, request: Request, response: Response,
    principal: RequestPrincipal = Depends(require_user),
):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    # 不接受版本参数；客户端不能误以为请求了历史版本却收到当前版本。
    if request.query_params:
        raise HTTPException(422, "Selectable library manifest accepts no query parameters")
    with get_conn() as conn:
        return get_selectable_library_manifest(conn, key)


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
def get_assessment_report_packet(project_id: str, generation_token: str | None = None):
    with get_conn() as conn:
        generation_token = prepare_assessment_report_packet(conn, project_id, generation_token=generation_token)
    with get_conn() as conn:
        # PostgreSQL 快照固定本次输入；后续聊天和导出不会延长项目写锁。
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return build_assessment_report_packet(conn, project_id, generation_token=generation_token)


@router.get(
    "/projects/{project_id}/assessment-report-finalization",
    response_model=AssessmentReportFinalizationView,
    dependencies=[Depends(require_tenant_project)],
)
def get_assessment_report_finalization_route(
    project_id: str, vulnerability_id: str | None = None, observation_id: str | None = None,
    generation_token: str | None = None,
):
    with get_conn() as conn:
        return get_assessment_report_finalization(
            conn, project_id, vulnerability_id=vulnerability_id,
            observation_id=observation_id, generation_token=generation_token,
        )


@router.get("/projects/{project_id}/assessment-report-finalizations", response_model=list[AssessmentReportFinalizationView],
            dependencies=[Depends(require_tenant_project)])
def list_assessment_report_finalizations_route(project_id: str):
    with get_conn() as conn:
        return list_assessment_report_finalizations(conn, project_id)


@router.post("/projects/{project_id}/assessment-report-finalization/cancel", response_model=AssessmentReportFinalizationView)
def cancel_assessment_report_generation_route(
    project_id: str, vulnerability_id: str | None = None, observation_id: str | None = None,
    principal: RequestPrincipal = Depends(require_user_tenant_project),
):
    with get_conn() as conn:
        return cancel_assessment_report_generation(conn, project_id, vulnerability_id=vulnerability_id, observation_id=observation_id)


@router.get("/projects/{project_id}/assessment-report-finalization/accepted", dependencies=[Depends(require_tenant_project)])
def get_accepted_readable_report_route(
    project_id: str, vulnerability_id: str, observation_id: str | None = None,
):
    from cairn.server.services.vulnerability_reports import read_accepted_vulnerability_report

    with get_conn() as conn:
        result = read_accepted_vulnerability_report(conn, project_id, vulnerability_id, observation_id)
        if result is None:
            raise HTTPException(404, "assessment_report_accepted_not_found")
        return result


@router.post(
    "/projects/{project_id}/assessment-report-finalization/request",
    response_model=AssessmentReportFinalizationView,
    status_code=202,
)
def request_assessment_report_generation_route(
    project_id: str,
    body: AssessmentReportGenerationRequest,
    request: Request,
    principal: RequestPrincipal = Depends(require_user_tenant_project),
):
    # 旧单漏洞请求必须明确拒绝，不能忽略范围后创建项目总报告。
    if {"vulnerability_id", "observation_id"}.intersection(request.query_params):
        raise HTTPException(422, "assessment_report_single_vulnerability_generation_removed")
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
        generation_token=body.generation_token,
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


@router.get(
    "/projects/{project_id}/assessment-report-finalization/finish-receipts/{task_run_id}",
    response_model=AssessmentReportFinishReceiptView,
)
def get_assessment_report_finish_receipt_route(
    project_id: str,
    task_run_id: str,
    principal: RequestPrincipal = Depends(require_tenant_project),
):
    return get_assessment_report_finish_receipt(project_id, task_run_id, principal=principal)


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
