"""版本化 advisory library 管理接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from cairn.server.auth import require_super_admin
from cairn.server.db import get_conn
from cairn.server.schemas.libraries import (
    Library,
    LibraryCreate,
    LibraryFile,
    LibraryFileContent,
    LibraryVersion,
    LibraryVersionCreate,
)
from cairn.server.services.libraries import (
    archive_library,
    create_library,
    create_library_version,
    get_library,
    get_library_version,
    list_libraries,
    list_library_files,
    list_library_versions,
    read_library_file,
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


@router.get("/libraries/{key}/versions", response_model=list[LibraryVersion])
def get_library_versions(key: str):
    with get_conn() as conn:
        return list_library_versions(conn, key)


@router.post(
    "/libraries/{key}/versions",
    response_model=LibraryVersion,
    status_code=status.HTTP_201_CREATED,
)
def post_library_version(key: str, body: LibraryVersionCreate):
    with get_conn() as conn:
        return create_library_version(conn, key, body.files, message=body.message)


@router.get("/libraries/{key}/versions/{version}", response_model=LibraryVersion)
def get_library_version_record(key: str, version: int):
    with get_conn() as conn:
        return get_library_version(conn, key, version)


@router.get("/libraries/{key}/versions/{version}/files", response_model=list[LibraryFile])
def get_library_version_files(key: str, version: int):
    with get_conn() as conn:
        return list_library_files(conn, key, version)


@router.get(
    "/libraries/{key}/versions/{version}/files/{path:path}",
    response_model=LibraryFileContent,
)
def get_library_version_file(key: str, version: int, path: str):
    with get_conn() as conn:
        return read_library_file(conn, key, path, version)
