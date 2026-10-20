"""报告模板只读目录与当前内容管理接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from cairn.server.auth import RequestPrincipal, protect_cookie_request, require_super_admin
from cairn.server.db import get_conn
from cairn.server.schemas.report_templates import (
    ReportTemplate,
    ReportTemplateCatalogEntry,
    ReportTemplateCreate,
    ReportTemplateUpdate,
)
from cairn.server.services.report_templates import (
    create_report_template,
    delete_report_template as delete_current_report_template,
    get_report_template,
    list_report_template_catalog,
    list_report_templates,
    update_report_template,
)

catalog_router = APIRouter(tags=["report-templates"])
router = APIRouter(
    tags=["report-templates"], dependencies=[Depends(require_super_admin)]
)


@catalog_router.get(
    "/report-template-catalog", response_model=list[ReportTemplateCatalogEntry]
)
def get_report_template_catalog(
    _principal: RequestPrincipal = Depends(protect_cookie_request),
):
    with get_conn() as conn:
        return list_report_template_catalog(conn)


@router.get("/report-templates", response_model=list[ReportTemplate])
def get_report_templates():
    with get_conn() as conn:
        return list_report_templates(conn)


@router.get("/report-templates/{key}", response_model=ReportTemplate)
def get_report_template_record(key: str):
    with get_conn() as conn:
        return get_report_template(conn, key)


@router.post(
    "/report-templates",
    response_model=ReportTemplate,
    status_code=status.HTTP_201_CREATED,
)
def post_report_template(body: ReportTemplateCreate):
    with get_conn() as conn:
        return create_report_template(conn, **body.model_dump())


@router.put("/report-templates/{key}", response_model=ReportTemplate)
def put_report_template(key: str, body: ReportTemplateUpdate):
    with get_conn() as conn:
        return update_report_template(conn, key, **body.model_dump())


@router.delete("/report-templates/{key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_report_template(key: str):
    with get_conn() as conn:
        delete_current_report_template(conn, key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
