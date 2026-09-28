"""审计接口，记录 task run、agent event、artifact 与 fact evidence 的链路。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from cairn.server.auth import RequestPrincipal, require_tenant_project
from cairn.server.models import (
    AgentEvent,
    Artifact,
    ArtifactPreview,
    CreateAgentEventRequest,
    CreateArtifactRequest,
    CreateRawArtifactRequest,
    CreateTaskRunRequest,
    FactEvidence,
    TaskRunCommandEvents,
    TaskRunHookEvents,
    TaskRunPromptEvents,
    TaskRunResultEvents,
    TaskRun,
    UpdateTaskRunRequest,
)
from cairn.server.services import (
    VulnerabilityEvidenceArchiveUnavailableError,
    artifact_content_file,
    create_agent_event_record,
    create_project_vulnerability_evidence_archive,
    create_artifact_record,
    create_raw_artifact_record,
    create_task_run_record,
    list_artifact_records,
    list_fact_evidence_records,
    list_task_run_command_event_records,
    list_task_run_event_records,
    list_task_run_hook_event_records,
    list_task_run_prompt_event_records,
    list_task_run_records,
    list_task_run_result_event_records,
    read_artifact_preview_record,
    update_task_run_record,
)

router = APIRouter(tags=["audit"], dependencies=[Depends(require_tenant_project)])


class _CleanupFileResponse(FileResponse):
    """无论下载成功、Range 拒绝还是客户端断开，都回收临时文件。"""

    def __init__(self, *args, cleanup, **kwargs) -> None:
        self._cleanup = cleanup
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._cleanup()


# task_run 是 dispatcher 审计的主线，后续 event/artifact/evidence 都挂在它或项目 fact 上。
@router.post(
    "/projects/{project_id}/task-runs",
    response_model=TaskRun,
    status_code=201,
)
def create_task_run(
    project_id: str, body: CreateTaskRunRequest,
    principal: RequestPrincipal = Depends(require_tenant_project),
):
    return create_task_run_record(project_id, body, principal=principal)


@router.get("/projects/{project_id}/task-runs", response_model=list[TaskRun])
def list_task_runs(project_id: str):
    return list_task_run_records(project_id)


@router.patch(
    "/projects/{project_id}/task-runs/{task_run_id}",
    response_model=TaskRun,
)
def update_task_run(
    project_id: str, task_run_id: str, body: UpdateTaskRunRequest,
    principal: RequestPrincipal = Depends(require_tenant_project),
):
    return update_task_run_record(project_id, task_run_id, body, principal=principal)


@router.post(
    "/projects/{project_id}/agent-events",
    response_model=AgentEvent,
    status_code=201,
)
def create_agent_event(
    project_id: str, body: CreateAgentEventRequest,
    principal: RequestPrincipal = Depends(require_tenant_project),
):
    context = body.payload.get("runtime_context")
    if (
        body.kind == "runtime_context_updated"
        and isinstance(context, dict)
        and "execution_lifecycle" in context
        and principal.kind != "machine"
    ):
        # 审计旁路也不能让用户伪造调度器的进程/资源确认。
        raise HTTPException(403, "execution_lifecycle_machine_required")
    return create_agent_event_record(project_id, body)


@router.get(
    "/projects/{project_id}/task-runs/{task_run_id}/agent-events",
    response_model=list[AgentEvent],
)
def list_task_run_events(project_id: str, task_run_id: str):
    return list_task_run_event_records(project_id, task_run_id)


@router.get(
    "/projects/{project_id}/task-runs/result-events",
    response_model=list[TaskRunResultEvents],
)
def list_task_run_result_events(
    project_id: str,
    task_run_id: Annotated[list[str] | None, Query()] = None,
):
    # 聚合事件按 task_run_id 分组返回，避免前端为多 run 审计页重复请求每个子端点。
    return list_task_run_result_event_records(project_id, task_run_id)


@router.get(
    "/projects/{project_id}/task-runs/command-events",
    response_model=list[TaskRunCommandEvents],
)
def list_task_run_command_events(
    project_id: str,
    task_run_id: Annotated[list[str] | None, Query()] = None,
):
    return list_task_run_command_event_records(project_id, task_run_id)


@router.get(
    "/projects/{project_id}/task-runs/prompt-events",
    response_model=list[TaskRunPromptEvents],
)
def list_task_run_prompt_events(
    project_id: str,
    task_run_id: Annotated[list[str] | None, Query()] = None,
):
    return list_task_run_prompt_event_records(project_id, task_run_id)


@router.get(
    "/projects/{project_id}/task-runs/hook-events",
    response_model=list[TaskRunHookEvents],
)
def list_task_run_hook_events(
    project_id: str,
    task_run_id: Annotated[list[str] | None, Query()] = None,
):
    return list_task_run_hook_event_records(project_id, task_run_id)


@router.post(
    "/projects/{project_id}/artifacts",
    response_model=Artifact,
    status_code=201,
)
def create_artifact(project_id: str, body: CreateArtifactRequest):
    return create_artifact_record(project_id, body)


@router.post(
    "/projects/{project_id}/artifacts/raw",
    response_model=Artifact,
    status_code=201,
)
async def create_raw_artifact(
    project_id: str,
    request: Request,
    body: Annotated[CreateRawArtifactRequest, Query()],
):
    # raw body 不经 JSON/base64 聚合，直接把 ASGI 分块交给 artifact service。
    return await create_raw_artifact_record(project_id, body, request.stream())


@router.get("/projects/{project_id}/artifacts", response_model=list[Artifact])
def list_artifacts(project_id: str, task_run_id: str | None = None):
    return list_artifact_records(project_id, task_run_id)


@router.get(
    "/projects/{project_id}/artifacts/archive",
    response_class=FileResponse,
)
def download_artifact_archive(project_id: str):
    try:
        archive = create_project_vulnerability_evidence_archive(project_id)
    except VulnerabilityEvidenceArchiveUnavailableError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _CleanupFileResponse(
        archive.disk_path,
        cleanup=archive.cleanup,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
        media_type="application/zip",
        filename=archive.filename,
        content_disposition_type="attachment",
    )


@router.get(
    "/projects/{project_id}/artifacts/{artifact_id}/content",
    response_class=FileResponse,
)
def read_artifact_content(project_id: str, artifact_id: str):
    content = artifact_content_file(project_id, artifact_id)
    # artifact 下载由 service 层完成路径校验；这里额外设置 nosniff 降低浏览器误判执行风险。
    return FileResponse(
        content.disk_path,
        headers={"X-Content-Type-Options": "nosniff"},
        media_type=content.media_type,
        filename=content.filename,
        content_disposition_type=content.content_disposition_type,
    )


@router.get(
    "/projects/{project_id}/artifacts/{artifact_id}/preview",
    response_model=ArtifactPreview,
)
def read_artifact_preview(project_id: str, artifact_id: str):
    return read_artifact_preview_record(project_id, artifact_id, tail=False)


@router.get(
    "/projects/{project_id}/artifacts/{artifact_id}/preview-tail",
    response_model=ArtifactPreview,
)
def read_artifact_preview_tail(project_id: str, artifact_id: str):
    return read_artifact_preview_record(project_id, artifact_id, tail=True)


@router.get(
    "/projects/{project_id}/facts/{fact_id}/evidence",
    response_model=list[FactEvidence],
)
def list_fact_evidence(project_id: str, fact_id: str):
    return list_fact_evidence_records(project_id, fact_id)
