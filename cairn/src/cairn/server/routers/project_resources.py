"""项目资源、临时批次和辅助资料库接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from cairn.server.auth import (
    RequestPrincipal,
    protect_cookie_request,
    require_tenant_project,
    require_user,
    require_user_tenant_project,
)
from cairn.server.db import get_conn
from cairn.server.models import (
    CreateProjectResourceRequest,
    LibraryFile,
    ProjectResource,
    ProjectResourceBatch,
    ProjectResourceBatchMember,
    ProjectResourceConfig,
    ProjectResourceImportRequest,
    ProjectResourceImportResponse,
    UpdateProjectResourceBatchMemberRequest,
)
from cairn.server.services import (
    commit_project_resource_batch,
    create_project_resource_batch,
    create_project_resource_record,
    deactivate_project_resource_record,
    delete_project_resource_batch,
    delete_project_resource_batch_member,
    get_bound_library_revision,
    get_project_resource_batch,
    import_project_resource_batch_zip,
    list_library_revision_files,
    list_project_resource_records,
    project_resource_config,
    read_library_revision_content,
    read_project_resource_content,
    update_project_resource_batch_member,
    upload_project_resource_batch_file,
    validate_project_resource_path,
)

router = APIRouter(tags=["project-resources"])


def _principal_scope(principal: RequestPrincipal) -> tuple[str, str]:
    if principal.kind != "user" or principal.tenant_id is None:
        raise HTTPException(403, "User tenant context required")
    return principal.tenant_id, principal.subject_id


@router.get("/project-resource-config", response_model=ProjectResourceConfig)
def get_resource_config(_principal: RequestPrincipal = Depends(require_user)):
    return project_resource_config()


@router.post("/project-resource-batches", response_model=ProjectResourceBatch, status_code=201)
def create_resource_batch(principal: RequestPrincipal = Depends(protect_cookie_request)):
    tenant_id, user_id = _principal_scope(principal)
    return create_project_resource_batch(tenant_id=tenant_id, user_id=user_id)


@router.get("/project-resource-batches/{batch_id}", response_model=ProjectResourceBatch)
def read_resource_batch(
    batch_id: str,
    principal: RequestPrincipal = Depends(require_user),
):
    tenant_id, user_id = _principal_scope(principal)
    return get_project_resource_batch(batch_id, tenant_id=tenant_id, user_id=user_id)


@router.post(
    "/project-resource-batches/{batch_id}/files",
    response_model=ProjectResourceBatchMember,
    status_code=201,
)
async def upload_resource_batch_file(
    batch_id: str,
    request: Request,
    path: str = Query(min_length=1),
    kind: str | None = Query(default=None),
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    if kind is not None and kind not in {"note", "script", "file"}:
        raise HTTPException(422, "kind must be one of note, script, file")
    try:
        path = validate_project_resource_path(path)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    tenant_id, user_id = _principal_scope(principal)
    return await upload_project_resource_batch_file(
        batch_id,
        request.stream(),
        path=path,
        kind=kind,
        tenant_id=tenant_id,
        user_id=user_id,
    )


@router.post("/project-resource-batches/{batch_id}/zip", response_model=ProjectResourceBatch)
async def upload_resource_batch_zip(
    batch_id: str,
    request: Request,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    tenant_id, user_id = _principal_scope(principal)
    return await import_project_resource_batch_zip(
        batch_id,
        request.stream(),
        tenant_id=tenant_id,
        user_id=user_id,
    )


@router.patch(
    "/project-resource-batches/{batch_id}/members/{member_id}",
    response_model=ProjectResourceBatchMember,
)
def patch_resource_batch_member(
    batch_id: str,
    member_id: str,
    body: UpdateProjectResourceBatchMemberRequest,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    tenant_id, user_id = _principal_scope(principal)
    return update_project_resource_batch_member(
        batch_id,
        member_id,
        body,
        tenant_id=tenant_id,
        user_id=user_id,
    )


@router.delete("/project-resource-batches/{batch_id}/members/{member_id}", status_code=204)
def remove_resource_batch_member(
    batch_id: str,
    member_id: str,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    tenant_id, user_id = _principal_scope(principal)
    delete_project_resource_batch_member(
        batch_id,
        member_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )


@router.delete("/project-resource-batches/{batch_id}", status_code=204)
def remove_resource_batch(
    batch_id: str,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    tenant_id, user_id = _principal_scope(principal)
    delete_project_resource_batch(batch_id, tenant_id=tenant_id, user_id=user_id)


@router.post(
    "/projects/{project_id}/resource-imports",
    response_model=ProjectResourceImportResponse,
)
def import_resources_into_project(
    project_id: str,
    body: ProjectResourceImportRequest,
    principal: RequestPrincipal = Depends(require_user_tenant_project),
):
    tenant_id, user_id = _principal_scope(principal)
    resources = commit_project_resource_batch(
        project_id,
        body.resource_batch_id,
        tenant_id=tenant_id,
        user_id=user_id,
        created_by=user_id,
        allow_cross_tenant=principal.is_super_admin,
    )
    return ProjectResourceImportResponse(resources=resources)


@router.get(
    "/projects/{project_id}/resources",
    response_model=list[ProjectResource],
    dependencies=[Depends(require_tenant_project)],
)
def list_project_resources(project_id: str):
    return list_project_resource_records(project_id)


@router.get(
    "/projects/{project_id}/resources/{resource_id}",
    dependencies=[Depends(require_tenant_project)],
)
def read_project_resource(project_id: str, resource_id: str):
    resource = read_project_resource_content(project_id, resource_id)
    return Response(
        content=resource.content,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get(
    "/projects/{project_id}/advisory-libraries/{key}/versions/{version}/files",
    response_model=list[LibraryFile],
    dependencies=[Depends(require_tenant_project)],
)
def list_project_advisory_library_files(project_id: str, key: str, version: int):
    with get_conn() as conn:
        revision = get_bound_library_revision(conn, project_id, key, version)
        return list_library_revision_files(conn, revision)


@router.get(
    "/projects/{project_id}/advisory-libraries/{key}/versions/{version}/files/{path:path}",
    dependencies=[Depends(require_tenant_project)],
)
def read_project_advisory_library_file(project_id: str, key: str, version: int, path: str):
    with get_conn() as conn:
        revision = get_bound_library_revision(conn, project_id, key, version)
        content = read_library_revision_content(conn, revision, path)
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/projects/{project_id}/resources", response_model=ProjectResource, status_code=201)
def create_project_resource(
    project_id: str,
    body: CreateProjectResourceRequest,
    principal: RequestPrincipal = Depends(require_user_tenant_project),
):
    return create_project_resource_record(project_id, body, created_by=principal.subject_id)


@router.delete("/projects/{project_id}/resources/{resource_id}", status_code=204)
def deactivate_project_resource(
    project_id: str,
    resource_id: str,
    principal: RequestPrincipal = Depends(require_user_tenant_project),
):
    del principal
    deactivate_project_resource_record(project_id, resource_id)
