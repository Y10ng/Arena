"""计划管理只接受用户会话，到期消费仅接受 Dispatcher 机器身份。"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from cairn.server.auth import RequestPrincipal, protect_cookie_request, require_dispatcher
from cairn.server.schemas.project_schedules import CreateScheduleRequest, ScheduleActionRequest, ScheduleTiming, UpdateScheduleRequest
from cairn.server.schemas.projects import CreateProjectRequest
from cairn.server.services.creation_requests import query_creation_request
from cairn.server.services.project_schedules import (
    consume_due_schedules, create_schedule, get_schedule, list_schedules,
    preview_schedule, schedule_action, schedule_history, update_schedule,
)

router = APIRouter(tags=["project-schedules"])


class PreviewScheduleRequest(ScheduleTiming):
    project: CreateProjectRequest | None = None


@router.get("/creation-requests/{request_id}")
def creation_result(request_id: UUID, operation: Literal["project", "schedule"], principal: RequestPrincipal = Depends(protect_cookie_request)):
    return query_creation_request(tenant_id=principal.tenant_id, user_id=principal.subject_id, operation=operation, request_id=str(request_id))


@router.post("/project-schedules/preview")
def preview(body: PreviewScheduleRequest, _principal: RequestPrincipal = Depends(protect_cookie_request)):
    return preview_schedule(body, project=body.project)


@router.get("/project-schedules")
def list_all(principal: RequestPrincipal = Depends(protect_cookie_request)):
    return list_schedules(principal)


@router.post("/project-schedules", status_code=201)
def save(body: CreateScheduleRequest, principal: RequestPrincipal = Depends(protect_cookie_request)):
    return create_schedule(body, principal)


@router.get("/project-schedules/{schedule_id}")
def detail(schedule_id: str, principal: RequestPrincipal = Depends(protect_cookie_request)):
    return get_schedule(schedule_id, principal)


@router.get("/project-schedules/{schedule_id}/history")
def history(schedule_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), principal: RequestPrincipal = Depends(protect_cookie_request)):
    return schedule_history(schedule_id, principal, offset=offset, limit=limit)


@router.put("/project-schedules/{schedule_id}")
def edit(schedule_id: str, body: UpdateScheduleRequest, principal: RequestPrincipal = Depends(protect_cookie_request)):
    return update_schedule(schedule_id, body, principal)


@router.post("/project-schedules/{schedule_id}/actions")
def action(schedule_id: str, body: ScheduleActionRequest, principal: RequestPrincipal = Depends(protect_cookie_request)):
    return schedule_action(schedule_id, body, principal)


@router.post("/dispatcher/project-schedules/consume", dependencies=[Depends(require_dispatcher)])
def consume():
    return consume_due_schedules()
