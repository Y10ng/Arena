"""执行事件能力发现、机器追加和 Tenant 安全查询路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

from cairn.server.auth import RequestPrincipal, require_dispatcher, require_tenant_project
from cairn.server.schemas.execution_events import (
    ExecutionEventCapabilities,
    ExecutionEventEnvelope,
    ExecutionEventMetadata,
    ExecutionEventReceipt,
)
from cairn.server.services.execution_events import (
    append_execution_event,
    execution_event_capabilities,
    list_execution_events,
)

router = APIRouter(tags=["execution-events"])

_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}


def _set_private_headers(response: Response) -> None:
    for name, value in _PRIVATE_HEADERS.items():
        response.headers[name] = value


@router.get(
    "/execution-events/capabilities",
    response_model=ExecutionEventCapabilities,
)
def get_execution_event_capabilities(
    response: Response,
    _principal: RequestPrincipal = Depends(require_dispatcher),
):
    _set_private_headers(response)
    return execution_event_capabilities()


@router.post(
    "/projects/{project_id}/task-runs/{task_run_id}/execution-events",
    response_model=ExecutionEventReceipt,
    status_code=status.HTTP_201_CREATED,
)
def create_execution_event(
    project_id: str,
    task_run_id: str,
    body: ExecutionEventEnvelope,
    response: Response,
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    _set_private_headers(response)
    receipt = append_execution_event(
        project_id,
        task_run_id,
        body,
        principal=principal,
    )
    response.status_code = (
        status.HTTP_201_CREATED
        if receipt.disposition == "received"
        else status.HTTP_200_OK
    )
    return receipt


@router.get(
    "/projects/{project_id}/task-runs/{task_run_id}/execution-events",
    response_model=list[ExecutionEventMetadata],
)
def get_execution_events(
    project_id: str,
    task_run_id: str,
    response: Response,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _principal: RequestPrincipal = Depends(require_tenant_project),
):
    _set_private_headers(response)
    return list_execution_events(
        project_id,
        task_run_id,
        limit=limit,
        offset=offset,
    )
