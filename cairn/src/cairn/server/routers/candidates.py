"""CandidateStore 能力发现、机器捕获和 Tenant 授权原文读取路由。"""

from __future__ import annotations

import base64
import binascii
import json
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from cairn.server.auth import (
    RequestPrincipal,
    require_dispatcher,
    require_user_tenant_project,
)
from cairn.server.schemas.candidates import (
    CANDIDATE_MANIFEST_HEADER,
    CANDIDATE_MAX_MANIFEST_BYTES,
    CandidateCapabilities,
    CandidateFrameManifest,
    CandidateMetadata,
    CandidateOutcomeManifest,
    CandidateReceipt,
    canonical_json_bytes,
)
from cairn.server.services.candidate_store import (
    candidate_capabilities,
    capture_candidate,
    capture_candidate_outcome,
    list_candidates,
    read_candidate_bytes,
    read_candidate_raw,
)

router = APIRouter(tags=["candidates"])

_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_ENCODED_MANIFEST_LENGTH = (CANDIDATE_MAX_MANIFEST_BYTES * 4 + 2) // 3


def _set_private_headers(response: Response) -> None:
    for name, value in _PRIVATE_HEADERS.items():
        response.headers[name] = value


def _decode_manifest_header(
    encoded: str,
) -> tuple[CandidateFrameManifest | CandidateOutcomeManifest, bytes]:
    if len(encoded) > _MAX_ENCODED_MANIFEST_LENGTH:
        raise HTTPException(413, "candidate_manifest_too_large")
    if not encoded or not _BASE64URL_RE.fullmatch(encoded):
        raise HTTPException(422, "candidate_manifest_encoding_invalid")
    padding = "=" * (-len(encoded) % 4)
    try:
        manifest_bytes = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError):
        raise HTTPException(422, "candidate_manifest_encoding_invalid") from None
    canonical_encoding = (
        base64.urlsafe_b64encode(manifest_bytes).rstrip(b"=").decode("ascii")
    )
    if canonical_encoding != encoded:
        raise HTTPException(422, "candidate_manifest_encoding_invalid")
    if not manifest_bytes or len(manifest_bytes) > CANDIDATE_MAX_MANIFEST_BYTES:
        raise HTTPException(413, "candidate_manifest_too_large")
    try:
        manifest_json = manifest_bytes.decode("utf-8", errors="strict")
        loaded = json.loads(manifest_json)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise HTTPException(422, "candidate_manifest_invalid") from None
    if not isinstance(loaded, dict):
        raise HTTPException(422, "candidate_manifest_invalid")
    # Header 必须已经是规范 JSON；这里不允许解析后重新序列化来改变权威字节。
    try:
        canonical_manifest = canonical_json_bytes(loaded)
    except (UnicodeEncodeError, ValueError, RecursionError):
        raise HTTPException(422, "candidate_manifest_invalid") from None
    if canonical_manifest != manifest_bytes:
        raise HTTPException(422, "candidate_manifest_not_canonical")
    try:
        if loaded.get("record_kind") == "candidate":
            manifest = CandidateFrameManifest.model_validate(loaded)
        elif loaded.get("record_kind") == "outcome":
            manifest = CandidateOutcomeManifest.model_validate(loaded)
        else:
            raise HTTPException(422, "candidate_record_kind_invalid")
    except (ValidationError, RecursionError):
        raise HTTPException(422, "candidate_manifest_invalid") from None
    return manifest, manifest_bytes


@router.get(
    "/candidates/capabilities",
    response_model=CandidateCapabilities,
)
def get_candidate_capabilities(
    response: Response,
    _principal: RequestPrincipal = Depends(require_dispatcher),
):
    _set_private_headers(response)
    return candidate_capabilities()


@router.post(
    "/projects/{project_id}/task-runs/{task_run_id}/candidates",
    response_model=CandidateReceipt,
    status_code=status.HTTP_201_CREATED,
)
async def create_candidate_record(
    project_id: str,
    task_run_id: str,
    request: Request,
    response: Response,
    manifest_header: str = Header(alias=CANDIDATE_MANIFEST_HEADER),
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    _set_private_headers(response)
    if request.headers.get("content-type", "").casefold() != "application/octet-stream":
        raise HTTPException(415, "candidate_content_type_invalid")
    if len(request.headers.getlist(CANDIDATE_MANIFEST_HEADER)) != 1:
        raise HTTPException(422, "candidate_manifest_header_ambiguous")
    manifest, manifest_bytes = _decode_manifest_header(manifest_header)
    raw = await read_candidate_raw(request.stream())
    if isinstance(manifest, CandidateFrameManifest):
        receipt = await run_in_threadpool(
            capture_candidate,
            project_id,
            task_run_id,
            manifest,
            manifest_bytes,
            raw,
            principal=principal,
        )
    else:
        receipt = await run_in_threadpool(
            capture_candidate_outcome,
            project_id,
            task_run_id,
            manifest,
            manifest_bytes,
            raw,
            principal=principal,
        )
    response.status_code = (
        status.HTTP_201_CREATED
        if receipt.disposition == "received"
        else status.HTTP_200_OK
    )
    return receipt


@router.get(
    "/projects/{project_id}/task-runs/{task_run_id}/candidates",
    response_model=list[CandidateMetadata],
)
def get_candidates(
    project_id: str,
    task_run_id: str,
    response: Response,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _principal: RequestPrincipal = Depends(require_user_tenant_project),
):
    _set_private_headers(response)
    return list_candidates(
        project_id,
        task_run_id,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/projects/{project_id}/task-runs/{task_run_id}/candidates/{candidate_id}/raw",
    response_class=Response,
)
def get_candidate_raw(
    project_id: str,
    task_run_id: str,
    candidate_id: str,
    _principal: RequestPrincipal = Depends(require_user_tenant_project),
):
    raw = read_candidate_bytes(project_id, task_run_id, candidate_id)
    return Response(
        content=raw,
        media_type="application/octet-stream",
        headers={
            **_PRIVATE_HEADERS,
            "Content-Disposition": f'attachment; filename="{candidate_id}.bin"',
        },
    )
