"""管理员使用的资料库当前文本读写接口。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Response, status

from cairn.server.auth import require_super_admin
from cairn.server.db import get_conn
from cairn.server.models import (
    KnowledgeFile,
    KnowledgeFileContent,
    KnowledgeFileWrite,
    KnowledgeSet,
    KnowledgeSetCreate,
)
from cairn.server.services.libraries import (
    archive_library,
    create_library,
    get_library,
    safety_file_path,
    list_libraries,
    list_library_files,
    read_library_file,
    remove_library_file,
    replace_library_file,
)

router = APIRouter(tags=["knowledge"], dependencies=[Depends(require_super_admin)])


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
        builtin=library.builtin,
        safety_path=safety_file_path(library.key),
        file_count=library.file_count,
        referenced_by=_references(conn, library.key),
    )


@router.get("/knowledge", response_model=list[KnowledgeSet])
def get_knowledge_sets():
    with get_conn() as conn:
        return [_knowledge_set(conn, item) for item in list_libraries(conn)]


@router.post("/knowledge", response_model=KnowledgeSet, status_code=status.HTTP_201_CREATED)
def post_knowledge_set(body: KnowledgeSetCreate):
    with get_conn() as conn:
        create_library(conn, body.key, body.key)
        return _knowledge_set(conn, get_library(conn, body.key))


@router.delete("/knowledge/{key}", status_code=status.HTTP_204_NO_CONTENT)
def remove_knowledge_set(key: str):
    with get_conn() as conn:
        archive_library(conn, key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/knowledge/{key}/files", response_model=list[KnowledgeFile])
def get_knowledge_files(key: str):
    with get_conn() as conn:
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
        remove_library_file(conn, key, name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
