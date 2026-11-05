"""已授权项目的实时研究目录；不改变容器或业务生命周期。"""
from typing import Literal
import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse

from cairn.server.auth import require_tenant_project
from cairn.server.services.project_workspace import download_workspace, list_workspace, preview_workspace, search_workspace

router = APIRouter(tags=["workspace"], dependencies=[Depends(require_tenant_project)])
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


@router.get("/projects/{project_id}/workspace/entries")
def entries(
    project_id: str, response: Response, path: str = Query("", max_length=4096),
    workspace_id: str | None = Query(None, min_length=64, max_length=64),
    cursor: str | None = Query(None, max_length=16384),
):
    response.headers.update(_HEADERS)
    return list_workspace(project_id, path, workspace_id=workspace_id, cursor=cursor)


@router.get("/projects/{project_id}/workspace/search")
def search(
    project_id: str, response: Response, q: str = Query(..., min_length=1, max_length=512),
    workspace_id: str = Query(..., min_length=64, max_length=64),
    cursor: str | None = Query(None, max_length=131072),
):
    response.headers.update(_HEADERS)
    return search_workspace(project_id, q, workspace_id=workspace_id, cursor=cursor)


@router.get("/projects/{project_id}/workspace/preview")
def preview(
    project_id: str, response: Response, path: str = Query(..., min_length=1, max_length=4096),
    workspace_id: str = Query(..., min_length=64, max_length=64), window: Literal["head", "tail"] = "head",
):
    response.headers.update(_HEADERS)
    return preview_workspace(project_id, path, workspace_id=workspace_id, window=window)


class _WorkspaceResponse(StreamingResponse):
    def __init__(self, download, *, inline: bool):
        self.download = download
        disposition = "inline" if inline else "attachment"
        super().__init__(download.chunks(), media_type=download.media_type, headers={
            **_HEADERS,
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(download.filename, safe='')}",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        })

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # 响应取消也必须关闭已校验的 FD，不能仅依赖迭代器被完整消费。
            os.close(self.download.fd)


@router.get("/projects/{project_id}/workspace/content")
def content(
    project_id: str, path: str = Query(..., min_length=1, max_length=4096),
    workspace_id: str = Query(..., min_length=64, max_length=64), inline: bool = False,
):
    download = download_workspace(project_id, path, workspace_id=workspace_id, inline=inline)
    return _WorkspaceResponse(download, inline=inline)
