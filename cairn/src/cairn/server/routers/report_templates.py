"""版本化报告模板目录与管理接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from cairn.server.auth import RequestPrincipal, protect_cookie_request, require_super_admin
from cairn.server.db import get_conn
from cairn.server.schemas.report_templates import (
    ReportTemplate,
    ReportTemplateCatalogEntry,
    ReportTemplateCreate,
    ReportTemplateVersion,
    ReportTemplateVersionCreate,
    ReportTemplateVersionDetail,
)
from cairn.server.services.report_templates import (
    archive_report_template,
    create_report_template,
    create_report_template_version,
    get_report_template_version,
    list_report_template_catalog,
    list_report_template_versions,
    list_report_templates,
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
        return list_report_templates(conn, include_archived=True)


@router.post(
    "/report-templates",
    response_model=ReportTemplate,
    status_code=status.HTTP_201_CREATED,
)
def post_report_template(body: ReportTemplateCreate):
    with get_conn() as conn:
        return create_report_template(conn, body.key, body.name, body.description)


@router.delete("/report-templates/{key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_report_template(key: str):
    with get_conn() as conn:
        archive_report_template(conn, key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/report-templates/{key}/versions",
    response_model=list[ReportTemplateVersion],
)
def get_report_template_versions(key: str):
    with get_conn() as conn:
        return list_report_template_versions(conn, key)


@router.post(
    "/report-templates/{key}/versions",
    response_model=ReportTemplateVersionDetail,
    status_code=status.HTTP_201_CREATED,
)
def post_report_template_version(
    key: str,
    body: ReportTemplateVersionCreate,
    principal: RequestPrincipal = Depends(require_super_admin),
):
    with get_conn() as conn:
        return create_report_template_version(
            conn,
            key,
            body.markdown_content,
            message=body.message,
            created_by=principal.subject_id,
            compatibility_profiles=body.compatibility_profiles,
        )


@router.get(
    "/report-templates/{key}/versions/{version}",
    response_model=ReportTemplateVersionDetail,
)
def get_report_template_version_record(key: str, version: int):
    with get_conn() as conn:
        return get_report_template_version(conn, key, version)
