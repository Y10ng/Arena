"""消费回执只由原执行机器交付，不接收客户端费率或租户归属。"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from cairn.server.auth import require_dispatcher
from cairn.server.db import get_conn
from cairn.server.services import consumption

router = APIRouter(prefix='/model-consumption', tags=['model-consumption'])


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Binding(Input):
    task_run_id: str = Field(min_length=1, max_length=200)
    project_id: str | None = Field(default=None, max_length=200)
    conversation_id: str | None = Field(default=None, max_length=200)


class Usage(Input):
    input_tokens: int | None = Field(default=None, ge=0, le=2**63-1, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, le=2**63-1, strict=True)
    cache_read_tokens: int | None = Field(default=None, ge=0, le=2**63-1, strict=True)
    cache_write_tokens: int | None = Field(default=None, ge=0, le=2**63-1, strict=True)


class Response(Input):
    response_seq: int = Field(ge=0, le=2**31-1, strict=True)
    revision: int = Field(ge=0, le=2**31-1, strict=True)
    occurred_at: AwareDatetime | None = None
    time_basis: Literal['response','received','observed'] = 'response'
    measurement: Literal['raw','sdk_normalized','started','interrupted']
    usage: Usage = Field(default_factory=Usage)
    missing_responses: int = Field(default=0, ge=0, le=2**31-1, strict=True)

    @field_validator('occurred_at', mode='before')
    @classmethod
    def timestamp_string(cls, value):
        # 原 JSON 用于精确 ACK，禁止被 Pydantic 转成日期的数字原样进入 PostgreSQL。
        if value is not None:
            if not isinstance(value, str):
                raise ValueError('occurred_at must be an ISO timestamp with timezone')
            datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value


@router.put('/{identifier}')
def register(identifier: UUID, body: Binding, principal=Depends(require_dispatcher)):
    with get_conn() as conn:
        consumption.register_binding(conn,str(identifier),principal=principal,**body.model_dump())
    return {'accepted':True, 'id':str(identifier), **body.model_dump()}


async def raw_receipt(request: Request):
    return await request.json()


@router.post('/{identifier}/responses')
def receive(identifier: UUID, body: Response, event=Depends(raw_receipt), principal=Depends(require_dispatcher)):
    # 读取原 JSON 不阻塞事件循环；同步事务由 FastAPI 在线程池执行。
    with get_conn() as conn:
        consumption.accept_response(conn,str(identifier),event,principal)
    return {'accepted':True, 'id':str(identifier), 'response_seq':body.response_seq,
            'revision':body.revision, 'sha256':consumption.receipt_digest(event)}
