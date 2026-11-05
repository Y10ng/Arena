"""鉴权 HTTP 输入、可补读 SSE 和 Dispatcher 私有轮次接口。"""

from __future__ import annotations

import json
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cairn.server.auth import (
    RequestPrincipal, SESSION_COOKIE, require_dispatcher,
    protect_cookie_request, resolve_session,
)
from cairn.server.db import get_conn
from cairn.server.services import agent_conversations as service
from cairn.server.services import project_agent_configuration as configuration
from cairn.server.services import agent_attachments as attachments
from cairn.server.services.project_resources import project_resource_limits
from cairn.server.services.conversation_files import artifact_file

router = APIRouter(tags=["agent-conversations"])
BASE = "/agent-conversations"
TURN = BASE + "/{conversation_id}/turns/{message_id}"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateConversation(Input):
    project_id: str | None = Field(default=None, min_length=1, max_length=200)


class AgentConfiguration(Input):
    model_id: str | None = Field(min_length=1, max_length=128)


class CreateMessage(Input):
    project_id: str | None = Field(default=None, min_length=1, max_length=200)
    client_message_id: UUID
    text: str = Field(min_length=1, max_length=32000)
    attachment_ids: list[str] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def nonempty(cls, value):
        if not value.strip() or "\0" in value:
            raise ValueError("消息不能为空或包含 NUL")
        return value


class Claim(Input):
    request_id: UUID


class Turn(Input):
    task_run_id: str = Field(min_length=1, max_length=200)


class Tool(Turn):
    tool_call_id: str = Field(min_length=1, max_length=512)
    method: str
    params: dict = Field(default_factory=dict)


class Usage(Input):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)


class Finish(Turn):
    session_initialized: bool | None = None
    status: Literal["succeeded", "failed", "cancelled"]
    text: str | None = Field(default=None, max_length=1024 * 1024)
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,100}$")
    usage: dict[str, int | None] | None = None
    usage_missing_responses: int | None = Field(default=None, ge=0, strict=True)

    @field_validator("usage")
    @classmethod
    def valid_usage(cls, value):
        return Usage.model_validate(value).model_dump() if value is not None else None


def user_tenant(principal=Depends(protect_cookie_request)):
    if principal.kind != "user" or not principal.tenant_id:
        raise HTTPException(403, "User tenant is required")
    return principal


@router.get("/project-agent/configuration")
def get_configuration(principal=Depends(user_tenant)):
    with get_conn() as conn:
        return configuration.get_configuration(conn, principal.tenant_id)


@router.put("/project-agent/configuration")
def put_configuration(body: AgentConfiguration, principal=Depends(user_tenant)):
    with get_conn() as conn:
        return configuration.update_configuration(conn, body.model_id, principal)


@router.get(BASE)
def list_conversations(_principal=Depends(user_tenant)):
    with get_conn() as conn:
        return service.list_conversations(conn, _principal)


@router.post(BASE, status_code=201)
def create_conversation(body: CreateConversation, principal=Depends(user_tenant)):
    with get_conn() as conn:
        return service.create_conversation(conn, principal, body.project_id)


@router.get(BASE + "/cleanup-requests")
def list_cleanup_requests(principal=Depends(user_tenant)):
    from cairn.server.services.conversation_cleanup import list_requests
    with get_conn() as conn:
        return list_requests(conn, principal)


@router.delete(BASE + "/{conversation_id}", status_code=202)
def delete_conversation(conversation_id: str, _principal=Depends(user_tenant)):
    with get_conn() as conn:
        return service.delete_conversation(conn, conversation_id, _principal)


@router.post(BASE + "/{conversation_id}/messages/{message_id}/stop", status_code=202)
def stop_turn(conversation_id: str, message_id: int, principal=Depends(user_tenant)):
    with get_conn() as conn:
        return service.stop_turn(conn, conversation_id, message_id, principal)


@router.get(BASE + "/{conversation_id}/cleanup")
def cleanup_status(conversation_id: str, principal=Depends(user_tenant)):
    from cairn.server.services.conversation_cleanup import public_status
    with get_conn() as conn:
        row = service.require_owner(conn, conversation_id, principal, include_deleted=True)
        value = json.loads(row["cleanup_request_json"]) if row["cleanup_request_json"] else None
        return {"conversation_id": conversation_id, "cleanup": public_status(value)}


class CleanupTarget(Input):
    kind: Literal["container", "network"]
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    conversation_id: str = Field(min_length=1, max_length=200)
    machine_id: str = Field(min_length=1, max_length=200)
    instance_id: str = Field(min_length=1, max_length=200)
    daemon_id: str = Field(min_length=1, max_length=200)
    resource_key: str = Field(min_length=1, max_length=200)
    labels: dict[str, str]


class CleanupUpdate(Input):
    request_id: str = Field(min_length=1, max_length=200)
    action: Literal["bind", "completed", "failed"]
    targets: list[CleanupTarget] | None = Field(default=None, max_length=1000)
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,100}$")


@router.get("/agent-conversation-cleanups/pending")
def pending_cleanups(principal=Depends(require_dispatcher)):
    from cairn.server.services import conversation_cleanup
    with get_conn() as conn:
        return conversation_cleanup.pending(conn, principal)


@router.post(BASE + "/{conversation_id}/cleanup")
def update_cleanup(conversation_id: str, body: CleanupUpdate, principal=Depends(require_dispatcher)):
    from cairn.server.services import conversation_cleanup
    with get_conn() as conn:
        return conversation_cleanup.update(conn, conversation_id, body.request_id, body.action,
                                           [target.model_dump() for target in body.targets] if body.targets is not None else None,
                                           body.error_code, principal)


@router.get(BASE + "/{conversation_id}/attachment-limits")
def attachment_limits(conversation_id: str, _principal=Depends(user_tenant)):
    with get_conn() as conn:
        service.require_owner(conn, conversation_id, _principal)
    limits = project_resource_limits()
    return {"max_file_bytes": limits.max_file_bytes, "max_count": limits.max_active_count,
            "max_total_bytes": limits.max_active_total_bytes}


@router.get(BASE + "/{conversation_id}/attachments")
def list_attachments(conversation_id: str, _principal=Depends(user_tenant)):
    with get_conn() as conn:
        service.require_owner(conn, conversation_id, _principal)
        return attachments.list_attachments(conn, conversation_id)


@router.post(BASE + "/{conversation_id}/attachments", status_code=201)
async def upload_attachment(conversation_id: str, request: Request,
                            filename: str = Query(min_length=1, max_length=1024),
                            principal=Depends(user_tenant)):
    return await attachments.upload_attachment(conversation_id, filename, request.stream(), principal)


@router.delete(BASE + "/{conversation_id}/attachments/{attachment_id}", status_code=204)
def remove_attachment(conversation_id: str, attachment_id: str, principal=Depends(user_tenant)):
    with get_conn() as conn:
        attachments.remove_attachment(conn, conversation_id, attachment_id, principal)
    return Response(status_code=204)


@router.get(BASE + "/{conversation_id}/attachments/{attachment_id}/content")
def download_attachment(conversation_id: str, attachment_id: str, _principal=Depends(user_tenant)):
    with get_conn() as conn:
        service.require_owner(conn, conversation_id, _principal)
        row = attachments.attachment_row(conn, conversation_id, attachment_id)
        path = attachments.attachment_file(conn, row)
    return FileResponse(path, filename=row["filename"], media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get(BASE + "/{conversation_id}/messages")
def get_messages(conversation_id: str, after: int = Query(0, ge=0), _principal=Depends(user_tenant)):
    with get_conn() as conn:
        service.require_owner(conn, conversation_id, _principal)
        return service.messages(conn, conversation_id, after=after)


@router.get(BASE + "/{conversation_id}/actions")
def get_actions(conversation_id: str, principal=Depends(user_tenant)):
    with get_conn() as conn:
        return service.action_works(conn, conversation_id, principal)


@router.post(BASE + "/{conversation_id}/messages")
def post_message(conversation_id: str, body: CreateMessage, _principal=Depends(user_tenant)):
    with get_conn() as conn:
        return service.submit_message(conn, conversation_id, str(body.client_message_id), body.text, _principal, body.attachment_ids, project_id=body.project_id)


@router.get(BASE + "/{conversation_id}/events")
def events(conversation_id: str, request: Request, after: int = Query(0, ge=0),
           last_event_id: str | None = Header(None), _principal=Depends(user_tenant)):
    if last_event_id is not None:
        if not last_event_id.isdecimal():
            raise HTTPException(422, "agent_event_cursor_invalid")
        after = max(after, int(last_event_id))
    with get_conn() as conn:
        service.require_owner(conn, conversation_id, _principal)
    token = request.cookies.get(SESSION_COOKIE)

    def stream():
        cursor = after
        # 每次连接有界退出，由 EventSource 按已确认游标续接；不重放用户请求。
        for _ in range(60):
            resolved = resolve_session(token)
            if resolved is None:
                yield 'event: error\ndata: {"status":401,"detail":"登录已失效，请重新登录。"}\n\n'
                return
            with get_conn() as conn:
                try:
                    service.require_owner(conn, conversation_id, resolved[0])
                    items = service.messages(conn, conversation_id, revision=cursor)
                except HTTPException as exc:
                    if exc.status_code not in {403, 404}:
                        raise
                    yield 'event: error\ndata: {"status":404,"detail":"会话已删除或不可访问。"}\n\n'
                    return
            if items:
                cursor = max(item["revision"] for item in items)
                yield f"id: {cursor}\nevent: messages\ndata: {json.dumps(items, ensure_ascii=False)}\n\n"
            else:
                yield ": keepalive\n\n"
            time.sleep(1)
        yield 'event: close\ndata: {"reason":"rotate"}\n\n'

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get(BASE + "/{conversation_id}/artifacts/{artifact_id}")
def download(conversation_id: str, artifact_id: str, _principal=Depends(user_tenant)):
    with get_conn() as conn:
        row = service.conversation_artifact(conn, conversation_id, artifact_id, _principal)
        path = artifact_file(conn, row)
        title = json.loads(row["metadata_json"]).get("title", artifact_id)
    return FileResponse(path, filename=title, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/agent-turns/pending")
def pending(after: str = Query("", max_length=200), _principal=Depends(require_dispatcher)):
    with get_conn() as conn:
        return service.pending_conversations(conn, after=after)


@router.post(BASE + "/{conversation_id}/claim")
def claim(conversation_id: str, body: Claim, principal: RequestPrincipal = Depends(require_dispatcher)):
    with get_conn() as conn:
        result = service.claim_turn(conn, conversation_id, str(body.request_id), principal)
    return result if result is not None else Response(status_code=204)


@router.post(TURN + "/heartbeat")
def heartbeat(conversation_id: str, message_id: int, body: Turn, principal=Depends(require_dispatcher)):
    with get_conn() as conn:
        return service.heartbeat(conn, conversation_id, message_id, body.task_run_id, principal)


@router.post(TURN + "/tools")
def tool(conversation_id: str, message_id: int, body: Tool, principal=Depends(require_dispatcher)):
    return service.call_tool(conversation_id, message_id, body.task_run_id, body.method, body.params, principal,
                             tool_call_id=body.tool_call_id)


@router.post(TURN + "/finish")
def finish(conversation_id: str, message_id: int, body: Finish, principal=Depends(require_dispatcher)):
    with get_conn() as conn:
        return service.finish_turn(conn, conversation_id, message_id, body, principal)
