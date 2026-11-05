"""Dispatcher 专用采集入站与项目授权读取；HTTP 内容始终作为数据返回。"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from starlette.concurrency import run_in_threadpool

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from cairn.server.auth import RequestPrincipal, protect_cookie_request, require_dispatcher, require_dispatcher_protocol, require_tenant_project
from cairn.server.schemas.traffic import (
    TRAFFIC_BATCH_BYTES, TRAFFIC_CHUNK_BYTES, TrafficBodySeal, TrafficReadMode, TrafficScope, TrafficScopeStatus, TrafficSummaryPage, TrafficSummaryQuery,
)
from cairn.server.services.flow_evidence import read_flow_preview, read_flow_projection
from cairn.server.services.traffic_query import list_traffic_summaries
from cairn.server.services.traffic import (
    PREVIEW_BYTES,
    accept_traffic_batch,
    accept_traffic_body,
    accept_traffic_record,
    list_traffic_records,
    read_traffic_body,
    read_traffic_record,
    register_traffic_scope,
    seal_traffic_body,
    list_traffic_scope_status,
    update_traffic_scope_status,
)

router = APIRouter(tags=["traffic"])
project_router = APIRouter(prefix="/projects/{project_id}/traffic")
SAFE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


@project_router.post("/scopes", dependencies=[Depends(require_tenant_project)])
def register_scope(
    project_id: str, body: TrafficScope,
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    return register_traffic_scope(project_id, body, principal)


@project_router.get("/scopes/status", dependencies=[Depends(require_tenant_project)])
def get_scope_status(
    project_id: str, response: Response, cursor: str = "", limit: int = Query(default=100, ge=1, le=200),
    task_run_id: str | None = None,
):
    response.headers.update(SAFE_HEADERS)
    return list_traffic_scope_status(project_id, cursor=cursor, limit=limit, task_run_id=task_run_id)


@project_router.put("/scopes/{scope_id}/status", dependencies=[Depends(require_tenant_project)])
def put_scope_status(
    project_id: str, scope_id: str, body: TrafficScopeStatus, response: Response,
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    response.headers.update(SAFE_HEADERS)
    return update_traffic_scope_status(project_id, scope_id, body, principal)


@project_router.post("/scopes/{scope_id}/batch", dependencies=[Depends(require_dispatcher), Depends(require_tenant_project)])
async def post_batch(
    project_id: str, scope_id: str, request: Request, response: Response,
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > TRAFFIC_BATCH_BYTES:
            raise HTTPException(413, "traffic_batch_too_large")
        raw.extend(chunk)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(422, "traffic_batch_invalid") from None
    response.headers.update(SAFE_HEADERS)
    return await run_in_threadpool(accept_traffic_batch, project_id, scope_id, body, principal=principal)


@project_router.put("/scopes/{scope_id}/flows/{flow_id}", dependencies=[Depends(require_dispatcher), Depends(require_tenant_project)])
def put_flow(
    project_id: str, scope_id: str, flow_id: str, body: dict, response: Response,
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    response.headers.update(SAFE_HEADERS)
    return accept_traffic_record(project_id, scope_id, flow_id, body, principal=principal)


@project_router.put("/scopes/{scope_id}/flows/{flow_id}/bodies/{side}", dependencies=[Depends(require_dispatcher), Depends(require_tenant_project)])
async def put_body(
    project_id: str, scope_id: str, flow_id: str, side: Literal["request", "response"],
    request: Request, response: Response,
    offset: int = Query(ge=0), size: int = Query(gt=0, le=TRAFFIC_CHUNK_BYTES),
    sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    response.headers.update(SAFE_HEADERS)
    return await accept_traffic_body(
        project_id, scope_id, flow_id, side, offset, size, sha256, request.stream(), principal=principal,
    )


@project_router.post("/scopes/{scope_id}/flows/{flow_id}/bodies/{side}/seal", dependencies=[Depends(require_dispatcher), Depends(require_tenant_project)])
def seal_body(
    project_id: str, scope_id: str, flow_id: str, side: Literal["request", "response"],
    body: TrafficBodySeal, response: Response, principal: RequestPrincipal = Depends(require_dispatcher),
):
    response.headers.update(SAFE_HEADERS)
    return seal_traffic_body(project_id, scope_id, flow_id, side, body, principal=principal)


@project_router.get("", dependencies=[Depends(require_tenant_project)])
def list_flows(
    project_id: str, response: Response,
    limit: int = Query(default=50, ge=1, le=200), cursor: int = Query(default=0, ge=0),
    after_update: int | None = Query(default=None, ge=0),
    scope_id: str | None = None, task_run_id: str | None = None,
    method: str | None = None, host: str | None = None,
    state: Literal["receiving", "complete", "partial", "failed"] | None = None,
    status_code: int | None = Query(default=None, ge=100, le=999),
):
    if after_update is not None and (cursor or any(value is not None for value in (method, host, state, status_code))):
        # 增量必须包括移出筛选结果的更新；展示侧再应用可变的元数据筛选。
        raise HTTPException(422, "traffic_update_cursor_filter_conflict")
    response.headers.update(SAFE_HEADERS)
    return list_traffic_records(
        project_id, limit=limit, cursor=cursor, after_update=after_update, scope_id=scope_id,
        task_run_id=task_run_id, method=method, host=host, state=state, status_code=status_code,
    )


@router.get("/traffic", response_model=TrafficSummaryPage)
def list_all_flow_summaries(
    response: Response, query: Annotated[TrafficSummaryQuery, Query()],
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    response.headers.update(SAFE_HEADERS)
    return list_traffic_summaries(query, principal)


@project_router.get("/summary", response_model=TrafficSummaryPage)
def list_project_flow_summaries(
    project_id: str, response: Response, query: Annotated[TrafficSummaryQuery, Query()],
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    response.headers.update(SAFE_HEADERS)
    return list_traffic_summaries(query, principal, project_id=project_id)


@project_router.post("/http-evidence/prepare", dependencies=[Depends(require_dispatcher), Depends(require_tenant_project)])
def prepare_http_image(project_id: str, body: dict, response: Response):
    from cairn.server.db import get_conn
    from cairn.server.services.http_evidence_render import resolve_render_input
    if (set(body) - {"task_run_id", "source", "historical"} or not isinstance(body.get("task_run_id"), str)
            or not isinstance(body.get("source"), dict) or type(body.get("historical", False)) is not bool):
        raise HTTPException(422, "http_image_request_invalid")
    response.headers.update(SAFE_HEADERS)
    with get_conn() as conn:
        return resolve_render_input(conn, project_id, body["task_run_id"], body["source"], historical=body.get("historical", False))


@project_router.post("/http-evidence/publish", dependencies=[Depends(require_dispatcher), Depends(require_tenant_project)])
async def publish_http_image(project_id: str, request: Request, response: Response):
    import base64
    from cairn.server.db import get_conn
    from cairn.server.services.http_evidence_render import MAX_PNG_BYTES, publish_rendered_image, resolve_render_input
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_PNG_BYTES * 2:
            raise HTTPException(413, "http_image_request_too_large")
        raw.extend(chunk)
    try:
        body = json.loads(raw)
        if (not isinstance(body, dict) or set(body) - {"task_run_id", "source", "historical", "page", "page_count", "truncated", "content_base64", "expected_source"}
                or not isinstance(body.get("task_run_id"), str) or not isinstance(body.get("source"), dict)
                or type(body.get("historical", False)) is not bool):
            raise ValueError()
        content = base64.b64decode(body["content_base64"], validate=True)
    except (ValueError, TypeError, KeyError):
        raise HTTPException(422, "http_image_request_invalid") from None
    def resolve_source():
        with get_conn() as conn:
            return resolve_render_input(conn, project_id, body["task_run_id"], body["source"], historical=body.get("historical", False))
    source = await run_in_threadpool(resolve_source)
    if body.get("expected_source") != source["source"]:
        raise HTTPException(409, "http_image_source_changed")
    artifact = await publish_rendered_image(project_id, body["task_run_id"], source,
        page=body.get("page"), page_count=body.get("page_count"), truncated=body.get("truncated"), content=content)
    response.headers.update(SAFE_HEADERS)
    return artifact


@project_router.get("/{flow_id}", dependencies=[Depends(require_tenant_project)])
def get_flow(project_id: str, flow_id: str, response: Response):
    response.headers.update(SAFE_HEADERS)
    return read_traffic_record(project_id, flow_id)


@project_router.get("/{flow_id}/projection/{side}", dependencies=[Depends(require_tenant_project)])
def get_projection(
    project_id: str, flow_id: str, side: Literal["request", "response"], response: Response,
    offset: int = Query(default=0, ge=0), limit: int = Query(default=PREVIEW_BYTES, ge=1, le=PREVIEW_BYTES),
):
    response.headers.update(SAFE_HEADERS)
    return read_flow_projection(project_id, flow_id, side, offset, limit)


@project_router.get("/{flow_id}/readable/{side}", dependencies=[Depends(require_tenant_project)])
def get_readable(
    project_id: str, flow_id: str, side: Literal["request", "response"], response: Response,
    mode: TrafficReadMode = "manual",
):
    from cairn.server.services.flow_evidence import read_flow_readable

    response.headers.update(SAFE_HEADERS)
    return read_flow_readable(project_id, flow_id, side, mode=mode)


@project_router.get("/{flow_id}/preview/{side}", dependencies=[Depends(require_tenant_project)])
def get_preview(
    project_id: str, flow_id: str, side: Literal["request", "response"], response: Response,
    offset: int = Query(default=0, ge=0), limit: int = Query(default=PREVIEW_BYTES, ge=1, le=PREVIEW_BYTES),
):
    response.headers.update(SAFE_HEADERS)
    return read_flow_preview(project_id, flow_id, side, offset, limit)


@project_router.get("/{flow_id}/bodies/{side}", dependencies=[Depends(require_tenant_project)])
def get_body(
    project_id: str, flow_id: str, side: Literal["request", "response"], response: Response,
    preview: bool = False, offset: int = Query(default=0, ge=0),
    limit: int = Query(default=PREVIEW_BYTES, ge=1, le=PREVIEW_BYTES),
):
    content = read_traffic_body(project_id, flow_id, side, preview=preview, offset=offset, limit=limit)
    if preview:
        response.headers.update(SAFE_HEADERS)
        return content
    return FileResponse(
        content.disk_path, media_type="application/octet-stream", filename=content.filename,
        content_disposition_type="attachment", headers=SAFE_HEADERS,
    )


def conversation_traffic_owner(conversation_id: str, principal=Depends(require_dispatcher_protocol)):
    from cairn.server.db import get_conn
    from cairn.server.schemas.traffic import TrafficOwner
    from cairn.server.services.agent_conversations import _conversation, require_owner

    with get_conn() as conn:
        if principal.kind == "machine":
            row = _conversation(conn, conversation_id, include_deleted=True)
            if row["session_machine_id"] != principal.subject_id:
                raise HTTPException(404, "agent_conversation_not_found")
        else:
            require_owner(conn, conversation_id, principal, include_deleted=True)
    return TrafficOwner(conversation_id=conversation_id)


conversation_router = APIRouter(prefix="/agent-conversations/{conversation_id}/traffic")


@conversation_router.post("/scopes")
def register_conversation_scope(body: TrafficScope, owner=Depends(conversation_traffic_owner), principal=Depends(require_dispatcher)):
    return register_traffic_scope(owner, body, principal)


@conversation_router.get("/scopes/status")
def conversation_scope_status(response: Response, owner=Depends(conversation_traffic_owner), cursor: str = "",
                              limit: int = Query(default=100, ge=1, le=200), task_run_id: str | None = None):
    response.headers.update(SAFE_HEADERS)
    return list_traffic_scope_status(owner, cursor=cursor, limit=limit, task_run_id=task_run_id)


@conversation_router.put("/scopes/{scope_id}/status")
def conversation_put_status(scope_id: str, body: TrafficScopeStatus, response: Response,
                            owner=Depends(conversation_traffic_owner), principal=Depends(require_dispatcher)):
    return put_scope_status(owner, scope_id, body, response, principal)


@conversation_router.post("/scopes/{scope_id}/batch")
async def conversation_batch(scope_id: str, request: Request, response: Response,
                             owner=Depends(conversation_traffic_owner), principal=Depends(require_dispatcher)):
    return await post_batch(owner, scope_id, request, response, principal)


@conversation_router.put("/scopes/{scope_id}/flows/{flow_id}")
def conversation_put_flow(scope_id: str, flow_id: str, body: dict, response: Response,
                          owner=Depends(conversation_traffic_owner), principal=Depends(require_dispatcher)):
    return put_flow(owner, scope_id, flow_id, body, response, principal)


@conversation_router.put("/scopes/{scope_id}/flows/{flow_id}/bodies/{side}")
async def conversation_put_body(scope_id: str, flow_id: str, side: Literal["request", "response"], request: Request,
                                response: Response, offset: int = Query(ge=0),
                                size: int = Query(gt=0, le=TRAFFIC_CHUNK_BYTES), sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
                                owner=Depends(conversation_traffic_owner), principal=Depends(require_dispatcher)):
    return await put_body(owner, scope_id, flow_id, side, request, response, offset, size, sha256, principal)


@conversation_router.post("/scopes/{scope_id}/flows/{flow_id}/bodies/{side}/seal")
def conversation_seal_body(scope_id: str, flow_id: str, side: Literal["request", "response"], body: TrafficBodySeal,
                           response: Response, owner=Depends(conversation_traffic_owner), principal=Depends(require_dispatcher)):
    return seal_body(owner, scope_id, flow_id, side, body, response, principal)


@conversation_router.get("")
def conversation_flows(response: Response, owner=Depends(conversation_traffic_owner),
                       limit: int = Query(default=50, ge=1, le=200), cursor: int = Query(default=0, ge=0),
                       task_run_id: str | None = None):
    response.headers.update(SAFE_HEADERS)
    return list_traffic_records(owner, limit=limit, cursor=cursor, task_run_id=task_run_id)


@conversation_router.get("/{flow_id}")
def conversation_flow(flow_id: str, response: Response, owner=Depends(conversation_traffic_owner)):
    return get_flow(owner, flow_id, response)


@conversation_router.get("/{flow_id}/bodies/{side}")
def conversation_body(flow_id: str, side: Literal["request", "response"], response: Response,
                      owner=Depends(conversation_traffic_owner), preview: bool = False,
                      offset: int = Query(default=0, ge=0), limit: int = Query(default=PREVIEW_BYTES, ge=1, le=PREVIEW_BYTES)):
    return get_body(owner, flow_id, side, response, preview, offset, limit)


router.include_router(project_router)
router.include_router(conversation_router)
