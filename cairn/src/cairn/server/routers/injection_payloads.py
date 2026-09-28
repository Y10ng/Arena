"""AI Agent 注入 Payload 的项目级写入、查询与导出接口。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse

from cairn.server.auth import require_dispatcher, require_tenant_project
from cairn.server.db import get_conn
from cairn.server.models import (
    BatchInjectionPayloadOperationsRequest,
    BatchInjectionPayloadOperationsResponse,
    InjectionPayloadCategorySummary,
    InjectionPayloadListResponse,
    InjectionPayloadRecord,
    InjectionPayloadResponseStatus,
)
from cairn.server.services.injection_payloads import (
    apply_injection_payload_operations,
    get_injection_payload_record,
    injection_payload_from_row,
    list_injection_payload_categories,
    list_injection_payload_records,
)

router = APIRouter(
    tags=["injection-payloads"], dependencies=[Depends(require_tenant_project)]
)

_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}


def _set_private_headers(response: Response) -> None:
    for name, value in _PRIVATE_HEADERS.items():
        response.headers[name] = value


@router.post(
    "/projects/{project_id}/injection-payloads/operations",
    response_model=BatchInjectionPayloadOperationsResponse,
)
def apply_injection_payload_operations_route(
    project_id: str,
    body: BatchInjectionPayloadOperationsRequest,
    response: Response,
    _principal=Depends(require_dispatcher),
):
    _set_private_headers(response)
    with get_conn() as conn:
        return apply_injection_payload_operations(conn, project_id, body)


@router.get(
    "/projects/{project_id}/injection-payloads",
    response_model=InjectionPayloadListResponse,
)
def list_injection_payload_records_route(
    project_id: str,
    response: Response,
    injection_type: str | None = Query(default=None, max_length=200),
    response_status: InjectionPayloadResponseStatus | None = Query(default=None),
    search: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    _set_private_headers(response)
    with get_conn() as conn:
        return list_injection_payload_records(
            conn,
            project_id,
            injection_type=injection_type,
            response_status=response_status,
            search=search,
            limit=limit,
            offset=offset,
        )


@router.get(
    "/projects/{project_id}/injection-payloads/categories",
    response_model=list[InjectionPayloadCategorySummary],
)
def list_injection_payload_categories_route(project_id: str, response: Response):
    _set_private_headers(response)
    with get_conn() as conn:
        return list_injection_payload_categories(conn, project_id)


@router.get("/projects/{project_id}/injection-payloads/export.jsonl")
def export_injection_payload_records_route(project_id: str):
    # 导出期间保持数据库连接打开，逐条输出，避免把项目全部 Payload 聚合到内存。
    def iter_jsonl():
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM injection_payload_records
                WHERE project_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (project_id,),
            )
            for row in rows:
                yield (
                    json.dumps(
                        injection_payload_from_row(row).model_dump(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")

    return StreamingResponse(
        iter_jsonl(),
        media_type="application/x-ndjson",
        headers={
            **_PRIVATE_HEADERS,
            "Content-Disposition": (
                f'attachment; filename="{project_id}-injection-payloads.jsonl"'
            ),
        },
    )


@router.get(
    "/projects/{project_id}/injection-payloads/{record_id}",
    response_model=InjectionPayloadRecord,
)
def get_injection_payload_record_route(
    project_id: str, record_id: str, response: Response
):
    _set_private_headers(response)
    with get_conn() as conn:
        return get_injection_payload_record(conn, project_id, record_id)
