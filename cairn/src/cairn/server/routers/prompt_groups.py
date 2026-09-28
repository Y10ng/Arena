"""Prompt 模板组管理接口，允许在内置模板之外维护受管模板。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from cairn.server.db import get_conn
from cairn.server.auth import require_super_admin
from cairn.server.models import (
    PromptFile,
    PromptFileContent,
    PromptFileWrite,
    PromptGroup,
    PromptGroupCreate,
    PromptGroupRepair,
    PromptGroupValidationResult,
)
from cairn.server.services import (
    create_prompt_group,
    delete_prompt_group,
    list_prompt_files,
    list_prompt_groups,
    read_prompt_file,
    repair_prompt_group,
    restore_prompt_group,
    validate_prompt_group,
    write_prompt_file,
)

router = APIRouter(tags=["prompt-groups"], dependencies=[Depends(require_super_admin)])


@router.get("/prompt-groups", response_model=list[PromptGroup])
def get_prompt_groups():
    with get_conn() as conn:
        return list_prompt_groups(conn)


@router.post("/prompt-groups", response_model=PromptGroup, status_code=status.HTTP_201_CREATED)
def post_prompt_group(body: PromptGroupCreate):
    with get_conn() as conn:
        # 新组可以从内置或受管组克隆，创建后仍由 validate 接口检查必需占位符。
        return create_prompt_group(conn, body.key, body.clone_from)


@router.delete("/prompt-groups/{key}", status_code=status.HTTP_204_NO_CONTENT)
def remove_prompt_group(key: str):
    with get_conn() as conn:
        delete_prompt_group(conn, key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/prompt-groups/{key}/files", response_model=list[PromptFile])
def get_prompt_files(key: str):
    return list_prompt_files(key)


@router.get("/prompt-groups/{key}/files/{name}", response_model=PromptFileContent)
def get_prompt_file(key: str, name: str):
    return read_prompt_file(key, name)


@router.put("/prompt-groups/{key}/files/{name}", response_model=PromptFileContent)
def put_prompt_file(key: str, name: str, body: PromptFileWrite):
    return write_prompt_file(key, name, body.content)


@router.post("/prompt-groups/{key}/validate", response_model=PromptGroupValidationResult)
def post_prompt_group_validate(key: str):
    return validate_prompt_group(key)


@router.post("/prompt-groups/{key}/repair", response_model=PromptGroup)
def post_prompt_group_repair(key: str, body: PromptGroupRepair):
    with get_conn() as conn:
        return repair_prompt_group(conn, key, body.clone_from)


@router.post("/prompt-groups/{key}/restore", response_model=PromptGroup)
def post_prompt_group_restore(key: str):
    with get_conn() as conn:
        return restore_prompt_group(conn, key)
