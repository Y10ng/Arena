"""普通用户的 Tenant Worker 管理接口。"""

from fastapi import APIRouter, Depends

from cairn.server.auth import RequestPrincipal, protect_cookie_request, require_dispatcher
from cairn.server.schemas.tenant_workers import (
    TenantWorker,
    TenantWorkerExecutionBinding,
    UpdateTenantWorkerRequest,
)
from cairn.server.services.tenant_workers import (
    get_tenant_worker,
    list_tenant_workers,
    resolve_project_worker_execution_binding,
    update_tenant_worker,
)

router = APIRouter(prefix="/tenant-workers", tags=["tenant-workers"])
dispatcher_router = APIRouter(prefix="/dispatcher", tags=["dispatcher"])


@router.get("", response_model=list[TenantWorker])
def get_workers(principal: RequestPrincipal = Depends(protect_cookie_request)):
    return list_tenant_workers(tenant_id=_tenant_id(principal))


@router.get("/{worker_id}", response_model=TenantWorker)
def get_worker(
    worker_id: str,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    return get_tenant_worker(worker_id, tenant_id=_tenant_id(principal))


@router.put("/{worker_id}", response_model=TenantWorker)
def put_worker(
    worker_id: str,
    body: UpdateTenantWorkerRequest,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    return update_tenant_worker(worker_id, body, principal=principal)


@dispatcher_router.post(
    "/projects/{project_id}/tenant-workers/{worker_id}/execution-bindings",
    response_model=TenantWorkerExecutionBinding,
)
def post_execution_binding(
    project_id: str,
    worker_id: str,
    task_type: str,
    _principal: RequestPrincipal = Depends(require_dispatcher),
):
    return resolve_project_worker_execution_binding(
        project_id,
        worker_id,
        task_type=task_type,
    )


def _tenant_id(principal: RequestPrincipal) -> str:
    if principal.kind != "user" or not principal.tenant_id:
        from fastapi import HTTPException

        raise HTTPException(403, "User tenant is required")
    return principal.tenant_id
