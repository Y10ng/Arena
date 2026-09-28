"""旧 `/knowledge` 协议的版本化 library 兼容接口。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, status

from cairn.server.auth import require_super_admin
from cairn.server.db import get_conn
from cairn.server.models import (
    KnowledgeFile,
    KnowledgeFileContent,
    KnowledgeFileWrite,
    KnowledgeSet,
    KnowledgeSetCreate,
)
from cairn.server.services.knowledge import knowledge_root
from cairn.server.services.libraries import (
    archive_library,
    create_library,
    create_library_version,
    get_library,
    import_legacy_knowledge,
    list_libraries,
    list_library_files,
    read_library_file,
    remove_library_file,
    replace_library_file,
)

router = APIRouter(tags=["knowledge"], dependencies=[Depends(require_super_admin)])


def _import_legacy(conn) -> None:
    # 迁移后的首次访问执行幂等导入，保留既有部署的文件系统知识内容。
    import_legacy_knowledge(conn, knowledge_root())


def _references(conn, key: str) -> list[str]:
    rows = conn.execute(
        """SELECT project_id FROM project_advisory_library_bindings
           WHERE library_key = ? ORDER BY project_id""",
        (key,),
    ).fetchall()
    return [row["project_id"] for row in rows]


def _knowledge_set(conn, library) -> KnowledgeSet:
    return KnowledgeSet(
        key=library.key,
        path=f"library://{library.key}",
        builtin=library.key == "knowledge_base",
        file_count=library.file_count,
        referenced_by=_references(conn, library.key),
    )


@router.get("/knowledge", response_model=list[KnowledgeSet])
def get_knowledge_sets():
    with get_conn() as conn:
        _import_legacy(conn)
        return [_knowledge_set(conn, item) for item in list_libraries(conn)]


@router.post("/knowledge", response_model=KnowledgeSet, status_code=status.HTTP_201_CREATED)
def post_knowledge_set(body: KnowledgeSetCreate):
    with get_conn() as conn:
        create_library(conn, body.key, body.key)
        create_library_version(conn, body.key, [])
        return _knowledge_set(conn, get_library(conn, body.key))


@router.delete("/knowledge/{key}", status_code=status.HTTP_204_NO_CONTENT)
def remove_knowledge_set(key: str):
    with get_conn() as conn:
        if key == "knowledge_base":
            raise HTTPException(422, "knowledge_base is reserved")
        archive_library(conn, key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/knowledge/{key}/files", response_model=list[KnowledgeFile])
def get_knowledge_files(key: str):
    with get_conn() as conn:
        _import_legacy(conn)
        library = get_library(conn, key)
        if library.latest_version is None:
            return []
        return [
            KnowledgeFile(
                name=Path(item.path).name,
                path=item.path,
                size=item.size_bytes,
                updated_at=None,
                warnings=[],
            )
            for item in list_library_files(conn, key)
        ]


@router.get("/knowledge/{key}/files/{name:path}", response_model=KnowledgeFileContent)
def get_knowledge_file(key: str, name: str):
    with get_conn() as conn:
        _import_legacy(conn)
        item = read_library_file(conn, key, name)
        return KnowledgeFileContent(
            name=Path(item.path).name,
            path=item.path,
            size=item.size_bytes,
            updated_at=None,
            warnings=[],
            content=item.content,
        )


@router.put("/knowledge/{key}/files/{name:path}", response_model=KnowledgeFileContent)
def put_knowledge_file(key: str, name: str, body: KnowledgeFileWrite):
    with get_conn() as conn:
        _import_legacy(conn)
        item = replace_library_file(conn, key, name, body.content)
        return KnowledgeFileContent(
            name=Path(item.path).name,
            path=item.path,
            size=item.size_bytes,
            updated_at=None,
            warnings=[],
            content=item.content,
        )


@router.delete("/knowledge/{key}/files/{name:path}", status_code=status.HTTP_204_NO_CONTENT)
def remove_knowledge_file(key: str, name: str):
    with get_conn() as conn:
        _import_legacy(conn)
        remove_library_file(conn, key, name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
