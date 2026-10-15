"""复测调用者入口与 Dispatcher 专用执行接口；身份不扩散到普通项目协议。"""

from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from cairn.server.auth import RequestPrincipal, require_dispatcher, require_super_admin, require_verification_caller
from cairn.server.schemas.resources import UpdateProjectResourceBatchMemberRequest
from cairn.server.schemas.verification_requests import (
    CreateVerificationIntegration, CreateVerificationRequest, CreateVerificationResourceBatch,
    UpdateVerificationConfiguration, UpdateVerificationIntegration, VerificationClaimRequest, VerificationExecutionBinding,
    VerificationFailureSubmission, VerificationResultSubmission,
)
from cairn.server.services import project_resources as resources
from cairn.server.services import verification_configuration as configuration
from cairn.server.services import verification_integrations as integrations
from cairn.server.services import verification_requests as service

router = APIRouter(prefix="/api/v1", tags=["verification-requests"])
NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


@router.post("/verification-requests")
def create_request(body: CreateVerificationRequest, response: Response,
                   principal: RequestPrincipal = Depends(require_verification_caller)):
    detail, replay = service.create_verification_request(body, principal)
    response.status_code = 200 if replay else 202
    response.headers["Location"] = f"/api/v1/verification-requests/{detail['id']}"
    response.headers["Cache-Control"] = "no-store"
    return detail


@router.get("/verification-requests")
def list_requests(limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                  external_id: str | None = None, mode: str | None = None,
                  execution_status: str | None = None, outcome: str | None = None,
                  source_project_id: str | None = None, source_vulnerability_id: str | None = None,
                  principal: RequestPrincipal = Depends(require_verification_caller)):
    return service.list_verification_requests(principal, limit=limit, offset=offset, external_id=external_id,
                                             mode=mode, execution_status=execution_status, outcome=outcome,
                                             source_project_id=source_project_id, source_vulnerability_id=source_vulnerability_id)


@router.get("/verification-sources/projects/{project_id}/vulnerabilities/{vulnerability_id}/preview")
def source_preview(project_id: str, vulnerability_id: str, response: Response,
                   principal: RequestPrincipal = Depends(require_verification_caller)):
    response.headers["Cache-Control"] = "no-store"
    return service.preview_platform_vulnerability(project_id, vulnerability_id, principal)


@router.get("/verification-sources/projects/{project_id}/vulnerabilities/{vulnerability_id}/history")
def source_history(project_id: str, vulnerability_id: str, response: Response,
                   limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                   principal: RequestPrincipal = Depends(require_verification_caller)):
    response.headers["Cache-Control"] = "no-store"
    return service.platform_vulnerability_history(project_id, vulnerability_id, principal, limit=limit, offset=offset)


@router.get("/verification-requests/{request_id}")
def read_request(request_id: str, principal: RequestPrincipal = Depends(require_verification_caller)):
    return service.get_verification_request(request_id, principal)


@router.post("/verification-requests/{request_id}/cancel")
def cancel_request(request_id: str, principal: RequestPrincipal = Depends(require_verification_caller)):
    return service.cancel_verification_request(request_id, principal)


@router.get("/verification-requests/{request_id}/report")
def read_report(request_id: str, principal: RequestPrincipal = Depends(require_verification_caller)):
    headers = NO_STORE | {"Content-Disposition": f"attachment; filename*=UTF-8''{quote('复测报告-' + request_id + '.md', safe='')}"}
    return Response(service.verification_report(request_id, principal), media_type="text/markdown; charset=utf-8", headers=headers)


@router.get("/verification-requests/{request_id}/materials/{resource_id}")
def read_material(request_id: str, resource_id: str, principal: RequestPrincipal = Depends(require_verification_caller)):
    material = service.verification_material(request_id, resource_id, principal)
    filename = PurePosixPath(material.path).name
    headers = NO_STORE | {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}"}
    return Response(material.content, media_type="application/octet-stream", headers=headers)


@router.get("/verification-requests/{request_id}/artifacts/{artifact_id}")
def read_artifact(request_id: str, artifact_id: str, principal: RequestPrincipal = Depends(require_verification_caller)):
    artifact = service.verification_artifact(request_id, artifact_id, principal)
    return FileResponse(artifact.disk_path, media_type=artifact.media_type, filename=artifact.filename,
                        content_disposition_type=artifact.content_disposition_type, headers=NO_STORE)


@router.get("/verification-resource-config")
def resource_config(principal: RequestPrincipal = Depends(require_verification_caller)):
    service.caller_scope(principal)
    return resources.project_resource_config()


@router.post("/verification-resource-batches", status_code=201)
def create_batch(body: CreateVerificationResourceBatch,
                 principal: RequestPrincipal = Depends(require_verification_caller)):
    return service.create_verification_batch(body.source_request_id, principal)


@router.get("/verification-resource-batches/{batch_id}")
def read_batch(batch_id: str, principal: RequestPrincipal = Depends(require_verification_caller)):
    return resources.get_project_resource_batch(batch_id, **service.caller_scope(principal))


@router.post("/verification-resource-batches/{batch_id}/files", status_code=201)
async def upload_file(batch_id: str, request: Request, path: str = Query(min_length=1), kind: str | None = None,
                      principal: RequestPrincipal = Depends(require_verification_caller)):
    if kind is not None and kind not in {"note", "script", "file"}:
        raise HTTPException(422, "kind 必须为 note、script 或 file")
    try:
        path = resources.validate_project_resource_path(path)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await resources.upload_project_resource_batch_file(batch_id, request.stream(), path=path, kind=kind,
                                                              **service.caller_scope(principal))


@router.post("/verification-resource-batches/{batch_id}/zip")
async def upload_zip(batch_id: str, request: Request, principal: RequestPrincipal = Depends(require_verification_caller)):
    return await resources.import_project_resource_batch_zip(batch_id, request.stream(), **service.caller_scope(principal))


@router.patch("/verification-resource-batches/{batch_id}/members/{member_id}")
def patch_member(batch_id: str, member_id: str, body: UpdateProjectResourceBatchMemberRequest,
                 principal: RequestPrincipal = Depends(require_verification_caller)):
    return resources.update_project_resource_batch_member(batch_id, member_id, body, **service.caller_scope(principal))


@router.delete("/verification-resource-batches/{batch_id}/members/{member_id}", status_code=204)
def delete_member(batch_id: str, member_id: str, principal: RequestPrincipal = Depends(require_verification_caller)):
    resources.delete_project_resource_batch_member(batch_id, member_id, **service.caller_scope(principal))


@router.delete("/verification-resource-batches/{batch_id}", status_code=204)
def delete_batch(batch_id: str, principal: RequestPrincipal = Depends(require_verification_caller)):
    resources.delete_project_resource_batch(batch_id, **service.caller_scope(principal))


@router.get("/verification-readiness")
def readiness(response: Response, principal: RequestPrincipal = Depends(require_verification_caller)):
    response.headers["Cache-Control"] = "no-store"
    return configuration.verification_readiness(principal)


@router.get("/verification-configuration")
def read_configuration(tenant_id: str = Query(min_length=1),
                       principal: RequestPrincipal = Depends(require_super_admin)):
    return configuration.get_verification_configuration(principal, tenant_id=tenant_id)


@router.put("/verification-configuration")
def update_configuration(body: UpdateVerificationConfiguration, tenant_id: str = Query(min_length=1),
                         principal: RequestPrincipal = Depends(require_super_admin)):
    return configuration.update_verification_configuration(body, principal, tenant_id=tenant_id)


@router.get("/verification-configuration/options")
def configuration_options(tenant_id: str = Query(min_length=1),
                          principal: RequestPrincipal = Depends(require_super_admin)):
    return configuration.verification_configuration_options(principal, tenant_id=tenant_id)


@router.get("/verification-integrations")
def list_integrations(tenant_id: str = Query(min_length=1),
                      principal: RequestPrincipal = Depends(require_super_admin)):
    return integrations.list_integrations(principal, tenant_id=tenant_id)


@router.post("/verification-integrations", status_code=201)
def create_integration(body: CreateVerificationIntegration, response: Response, tenant_id: str = Query(min_length=1),
                       principal: RequestPrincipal = Depends(require_super_admin)):
    response.headers["Cache-Control"] = "no-store"
    return integrations.create_integration(body, principal, tenant_id=tenant_id)


@router.patch("/verification-integrations/{integration_id}")
def update_integration(integration_id: str, body: UpdateVerificationIntegration, tenant_id: str = Query(min_length=1),
                       principal: RequestPrincipal = Depends(require_super_admin)):
    return integrations.update_integration(integration_id, body, principal, tenant_id=tenant_id)


@router.post("/verification-integrations/{integration_id}/disable")
def disable_integration(integration_id: str, tenant_id: str = Query(min_length=1),
                        principal: RequestPrincipal = Depends(require_super_admin)):
    return integrations.disable_integration(integration_id, principal, tenant_id=tenant_id)


@router.post("/verification-integrations/{integration_id}/rotate-token")
def rotate_token(integration_id: str, response: Response, tenant_id: str = Query(min_length=1),
                 principal: RequestPrincipal = Depends(require_super_admin)):
    response.headers["Cache-Control"] = "no-store"
    return integrations.rotate_integration_token(integration_id, principal, tenant_id=tenant_id)


@router.get("/verification-requests/{request_id}/input")
def execution_input(request_id: str, principal: RequestPrincipal = Depends(require_dispatcher)):
    return service.verification_input(request_id, principal)


@router.post("/verification-requests/{request_id}/claim")
def execution_claim(request_id: str, body: VerificationClaimRequest, response: Response,
                    principal: RequestPrincipal = Depends(require_dispatcher)):
    response.headers["Cache-Control"] = "no-store"
    return service.claim_verification_request(request_id, body, principal)


@router.post("/verification-requests/{request_id}/heartbeat")
def execution_heartbeat(request_id: str, body: VerificationExecutionBinding, principal: RequestPrincipal = Depends(require_dispatcher)):
    return service.heartbeat_verification_request(request_id, body, principal)


@router.post("/verification-requests/{request_id}/result")
def execution_result(request_id: str, body: VerificationResultSubmission, principal: RequestPrincipal = Depends(require_dispatcher)):
    return service.submit_verification_result(request_id, body, principal)


@router.post("/verification-requests/{request_id}/failure")
def execution_failure(request_id: str, body: VerificationFailureSubmission, principal: RequestPrincipal = Depends(require_dispatcher)):
    return service.submit_verification_failure(request_id, body, principal)


@router.post("/verification-requests/{request_id}/cancel-ack")
def execution_cancel_ack(request_id: str, body: VerificationExecutionBinding, principal: RequestPrincipal = Depends(require_dispatcher)):
    return service.acknowledge_verification_cancel(request_id, body, principal)
