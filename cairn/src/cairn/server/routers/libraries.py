"""资料库当前集合管理接口；已使用资料由项目／计划授权读取。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from cairn.server.auth import require_super_admin
from cairn.server.db import get_conn
from cairn.server.schemas.libraries import Library, LibraryCreate, LibraryFilesWrite, SelectableLibraryManifest
from cairn.server.services.libraries import (
    archive_library, create_library, get_library, list_libraries, replace_library_files,
)

router = APIRouter(tags=["libraries"], dependencies=[Depends(require_super_admin)])


@router.get("/libraries", response_model=list[Library])
def get_libraries():
    with get_conn() as conn:
        return list_libraries(conn)


@router.post("/libraries", response_model=Library, status_code=status.HTTP_201_CREATED)
def post_library(body: LibraryCreate):
    with get_conn() as conn:
        return create_library(conn, body.key, body.name)


@router.get("/libraries/{key}", response_model=Library)
def get_library_record(key: str):
    with get_conn() as conn:
        return get_library(conn, key)


@router.delete("/libraries/{key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_library(key: str):
    with get_conn() as conn:
        archive_library(conn, key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/libraries/{key}/files", response_model=SelectableLibraryManifest)
def put_library_files(key: str, body: LibraryFilesWrite):
    with get_conn() as conn:
        return replace_library_files(conn, key, body.files)
