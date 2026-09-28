"""worker 模板接口：以认证主体的 Tenant 作为唯一访问依据。"""

from fastapi import APIRouter, Depends

from cairn.server.auth import RequestPrincipal, protect_cookie_request
from cairn.server.models import (
    CreateWorkerTemplateRequest,
    UpdateWorkerTemplateRequest,
    WorkerTemplate,
)
from cairn.server.services import (
    create_worker_template,
    delete_worker_template,
    list_worker_templates,
    update_worker_template,
)

router = APIRouter(tags=["worker-templates"])


@router.get("/worker-templates", response_model=list[WorkerTemplate])
def get_worker_templates(principal: RequestPrincipal = Depends(protect_cookie_request)):
    return list_worker_templates(tenant_id=principal.tenant_id)


@router.post("/worker-templates", response_model=WorkerTemplate, status_code=201)
def post_worker_template(
    body: CreateWorkerTemplateRequest,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    return create_worker_template(
        body,
        tenant_id=principal.tenant_id,
        actor_id=principal.subject_id,
    )


@router.put("/worker-templates/{template_id}", response_model=WorkerTemplate)
def put_worker_template(
    template_id: str,
    body: UpdateWorkerTemplateRequest,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    return update_worker_template(template_id, body, tenant_id=principal.tenant_id)


@router.delete("/worker-templates/{template_id}", status_code=204)
def delete_worker_template_route(
    template_id: str,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    delete_worker_template(template_id, tenant_id=principal.tenant_id)
