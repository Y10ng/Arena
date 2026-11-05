"""项目运行诊断的单项与批量只读入口。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Literal

from pydantic import BaseModel, Field

from cairn.server.services.project_execution_control import confirm_execution_control

from cairn.server.auth import RequestPrincipal, protect_cookie_request, require_dispatcher, require_user_tenant_project
from cairn.server.db import get_conn
from cairn.server.schemas.project_runtime import ProjectRuntimeBatch, ProjectRuntimeResponse
from cairn.server.services.project_runtime_diagnostics import build_project_runtime, build_project_runtimes
from cairn.server.services.project_container_pause import (
    abort_container_activation,
    adopt_container_pause,
    claim_container_pause,
    confirm_container_pause,
    container_activation,
    finish_container_activation,
    register_container_activation,
)

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("/pause-timeouts/expire")
def expire_pause_timeouts(_principal: RequestPrincipal = Depends(require_dispatcher)):
    from cairn.server.services.project_records import expire_paused_projects

    return expire_paused_projects()


class ExecutionControlConfirmationRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    state: Literal["paused", "running", "stopped", "failed", "lost"]
    resources: list[dict[str, str]] = Field(default_factory=list, max_length=1024)
    error: str | None = Field(default=None, max_length=128)


@router.post("/{project_id}/execution-control/confirm")
def confirm_execution(
    project_id: str, body: ExecutionControlConfirmationRequest,
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    return confirm_execution_control(
        project_id, principal.subject_id, request_id=body.request_id,
        state=body.state, resources=body.resources, error=body.error,
    )


class ContainerActivationRequest(BaseModel):
    generation: int
    docker_daemon_id: str


class ContainerActivationFinishRequest(BaseModel):
    activation_token: str
    container_id: str = Field(min_length=1)


class ContainerActivationAbortRequest(BaseModel):
    activation_token: str = Field(min_length=1)
    docker_daemon_id: str = Field(min_length=1)


class ContainerPauseRequest(BaseModel):
    generation: int
    docker_daemon_id: str


class ContainerPauseAdoptionRequest(BaseModel):
    generation: int
    container_id: str | None
    docker_daemon_id: str


class ContainerPauseConfirmationRequest(BaseModel):
    generation: int
    stop_token: str
    container_id: str | None
    docker_daemon_id: str


@router.get("/runtime", response_model=ProjectRuntimeBatch)
def get_project_runtime_batch(
    project_ids: str = Query(..., min_length=1, max_length=16000),
    schema_version: int = Query(1, ge=1, le=2),
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    if principal.kind != "user":
        raise HTTPException(403, "User authentication required")
    ids = list(dict.fromkeys(item.strip() for item in project_ids.split(",") if item.strip()))
    if not ids or len(ids) > 100:
        raise HTTPException(422, "Expected between 1 and 100 project IDs")
    placeholders = ",".join("?" for _ in ids)
    params = list(ids)
    query = f"SELECT * FROM projects WHERE id IN ({placeholders})"
    if not principal.is_super_admin:
        query += " AND tenant_id = ?"
        params.append(principal.tenant_id)
    with get_conn() as conn:
        rows = {row["id"]: row for row in conn.execute(query, params).fetchall()}
        # 全部授权后才投影；不通过部分结果暴露其他 Tenant 的项目是否存在。
        if len(rows) != len(ids):
            raise HTTPException(404, "Project not found")
        return ProjectRuntimeBatch(
            items=build_project_runtimes(
                conn,
                [rows[item] for item in ids],
                schema_version=schema_version,
            )
        )


@router.get("/{project_id}/runtime", response_model=ProjectRuntimeResponse)
def get_project_runtime(
    project_id: str,
    schema_version: int = Query(1, ge=1, le=2),
    _principal: RequestPrincipal = Depends(require_user_tenant_project),
):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Project not found")
        return build_project_runtime(conn, row, schema_version=schema_version)


@router.get("/{project_id}/container-activation")
def get_container_activation(
    project_id: str,
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    return {"activation": container_activation(project_id, principal.subject_id)}


@router.post("/{project_id}/container-activation")
def register_activation(
    project_id: str, body: ContainerActivationRequest,
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    return register_container_activation(project_id, principal.subject_id, body.generation, body.docker_daemon_id)


@router.post("/{project_id}/container-activation/finish")
def finish_activation(
    project_id: str, body: ContainerActivationFinishRequest,
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    return finish_container_activation(project_id, principal.subject_id, body.activation_token, body.container_id)


@router.post("/{project_id}/container-activation/abort")
def abort_activation(
    project_id: str, body: ContainerActivationAbortRequest,
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    return abort_container_activation(project_id, principal.subject_id, body.activation_token, body.docker_daemon_id)


@router.post("/{project_id}/container-pause/adopt")
def adopt_pause(
    project_id: str, body: ContainerPauseAdoptionRequest,
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    return adopt_container_pause(project_id, principal.subject_id, body.generation, body.container_id, body.docker_daemon_id)


@router.post("/{project_id}/container-pause/claim")
def claim_pause(
    project_id: str, body: ContainerPauseRequest,
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    return claim_container_pause(project_id, principal.subject_id, body.generation, body.docker_daemon_id)


@router.post("/{project_id}/container-pause/confirm")
def confirm_pause(
    project_id: str, body: ContainerPauseConfirmationRequest,
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    return confirm_container_pause(
        project_id, principal.subject_id, body.generation, body.stop_token, body.container_id, body.docker_daemon_id,
    )
