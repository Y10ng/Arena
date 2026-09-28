"""项目组接口：以认证主体的 Tenant 作为唯一访问依据。"""

from fastapi import APIRouter, Depends

from cairn.server.auth import RequestPrincipal, protect_cookie_request
from cairn.server.models import (
    CreateProjectGroupRequest,
    ProjectGroup,
    UpdateProjectGroupRequest,
)
from cairn.server.services import (
    create_project_group,
    delete_project_group,
    list_project_groups,
    update_project_group,
)

router = APIRouter(tags=["project-groups"])


@router.get("/project-groups", response_model=list[ProjectGroup])
def get_project_groups(principal: RequestPrincipal = Depends(protect_cookie_request)):
    return list_project_groups(tenant_id=principal.tenant_id)


@router.post("/project-groups", response_model=ProjectGroup, status_code=201)
def post_project_group(
    body: CreateProjectGroupRequest,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    return create_project_group(
        body,
        tenant_id=principal.tenant_id,
        actor_id=principal.subject_id,
    )


@router.put("/project-groups/{group_id}", response_model=ProjectGroup)
def put_project_group(
    group_id: str,
    body: UpdateProjectGroupRequest,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    return update_project_group(group_id, body, tenant_id=principal.tenant_id)


@router.delete("/project-groups/{group_id}", status_code=204)
def delete_project_group_route(
    group_id: str,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    delete_project_group(group_id, tenant_id=principal.tenant_id)
