"""覆盖槽位接口：pentest 场景攻击面待覆盖槽位的登记与查询。

仅在 pentest 场景使用；default/logic-vuln 场景不调用。
"""

from fastapi import APIRouter, Depends

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL
from cairn.server.auth import require_tenant_project
from cairn.server.db import get_conn
from cairn.server.models import (
    CoverageSlot,
    ProjectMeta,
    RegisterSurfaceManifestRequest,
    SurfaceManifestEntry,
    UpsertCoverageSlotsRequest,
)
from cairn.server.services import (
    advance_goal_driven_convergence,
    advance_structured_convergence,
    check_project_active,
    list_coverage_slots,
    list_surface_manifest_entries,
    project_meta_from_row,
    register_discovered_surfaces,
    upsert_coverage_slots,
)

router = APIRouter(tags=["coverage-slots"], dependencies=[Depends(require_tenant_project)])


@router.get("/projects/{project_id}/coverage-slots", response_model=list[CoverageSlot])
def list_project_coverage_slots(project_id: str):
    with get_conn() as conn:
        return list_coverage_slots(conn, project_id)


@router.get(
    "/projects/{project_id}/surface-manifest",
    response_model=list[SurfaceManifestEntry],
)
def list_project_surface_manifest(project_id: str):
    with get_conn() as conn:
        return list_surface_manifest_entries(conn, project_id)


@router.post(
    "/projects/{project_id}/surface-manifest",
    response_model=list[SurfaceManifestEntry],
    status_code=201,
)
def register_project_surface_manifest(
    project_id: str,
    body: RegisterSurfaceManifestRequest,
):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        register_discovered_surfaces(
            conn,
            project_id,
            body.surfaces,
            tier=body.tier,
            source_intent_id=body.source_intent_id,
            source_fact_id=body.source_fact_id,
            source_task_run_id=body.source_task_run_id,
            fallback_evidence=body.evidence,
        )
        return list_surface_manifest_entries(conn, project_id)


@router.post(
    "/projects/{project_id}/coverage-convergence",
    response_model=ProjectMeta,
)
def advance_project_coverage_convergence(project_id: str):
    with get_conn() as conn:
        project = check_project_active(conn, project_id)
        if project["assessment_protocol"] == GOAL_DRIVEN_PROTOCOL:
            advance_goal_driven_convergence(conn, project_id)
        else:
            advance_structured_convergence(conn, project_id)
        project = conn.execute(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        return project_meta_from_row(conn, project)


@router.post(
    "/projects/{project_id}/coverage-slots",
    response_model=list[CoverageSlot],
    status_code=201,
)
def upsert_project_coverage_slots(project_id: str, body: UpsertCoverageSlotsRequest):
    with get_conn() as conn:
        # 槽位登记要求项目仍 active；dispatcher 在 bootstrap 成功路径写入。
        check_project_active(conn, project_id)
        slots = [
            {
                "kind": s.kind,
                "key": s.key,
                "description": s.description,
                "surface_key": s.surface_key,
                "surface_kind": s.surface_kind,
                "test_family": s.test_family,
                "applicability_basis": s.applicability_basis,
                "completion_criteria": s.completion_criteria,
            }
            for s in body.slots
        ]
        upsert_coverage_slots(conn, project_id, slots, source_intent_id=body.source_intent_id, tier=body.tier)
        return list_coverage_slots(conn, project_id)
