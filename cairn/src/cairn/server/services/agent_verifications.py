"""会话工具的复测薄适配；受理与消息引用共用事务，状态只读 VerificationRequest。"""

from typing import Annotated, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cairn.server.schemas.verification_requests import (
    CreateVerificationRequest, FileVerificationReport, InlineVerificationReport,
    PlatformVulnerabilityVerificationReport,
)
from . import agent_attachments as attachments, verification_requests as verification
from .payload_values import dump_json
from .project_resources import _list_resource_batch_members


class ConversationAttachmentReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["conversation_attachment"]
    attachment_id: str = Field(min_length=1)


class AgentVerificationRequest(CreateVerificationRequest):
    report: Annotated[
        InlineVerificationReport | FileVerificationReport | PlatformVulnerabilityVerificationReport | ConversationAttachmentReport,
        Field(discriminator="type"),
    ]
    attachment_ids: list[str] = Field(default_factory=list, max_length=100)


TOOL_PARAMS = {
    "create_verification_request": set(AgentVerificationRequest.model_fields) - {"schema_version", "request_id"},
    "list_verification_requests": {"limit", "offset", "external_id", "mode", "execution_status", "outcome",
                                   "source_project_id", "source_vulnerability_id", "source_number"},
    "read_verification_request": {"request_id", "creation_tool_call_id"},
    "cancel_verification_request": {"request_id"},
}


def _client_request_id(row, tool_call_id):
    # SDK 重传保持 tool call 身份；另一次明确实验不按参数相似度合并。
    return uuid5(NAMESPACE_URL, f"cairn:verification:{row['conversation_id']}:{row['id']}:{tool_call_id}")


def _create_body(params, request_id):
    try:
        body = AgentVerificationRequest.model_validate({**params, "schema_version": 1, "request_id": request_id})
    except ValidationError as exc:
        raise HTTPException(422, "复测参数不符合要求，请核对模式、标题、来源与本次目标。") from exc
    attachments.validate_attachment_ids(body.attachment_ids)
    if body.resource_batch_id and (body.attachment_ids or body.report.type == "conversation_attachment"):
        raise HTTPException(422, "会话附件与已有资源批次不能同时指定。")
    return body


def validate_params(method, params):
    if method == "create_verification_request":
        _create_body(params, uuid5(NAMESPACE_URL, "cairn:verification:validation"))
        return
    if method == "read_verification_request":
        if len(params) != 1 or not any(key in params for key in ("request_id", "creation_tool_call_id")):
            raise HTTPException(422, "请指定复测 request_id，或用原 creation_tool_call_id 查询本条消息的未知受理。")
    if method == "cancel_verification_request" and "request_id" not in params:
        raise HTTPException(422, "取消复测必须指定 request_id。")
    for key, value in params.items():
        if key in {"limit", "offset"}:
            if type(value) is not int or not (0 if key == "offset" else 1) <= value <= (2**31 - 1 if key == "offset" else 100):
                raise HTTPException(422, "agent_tool_parameters_invalid")
        elif not isinstance(value, str) or not 1 <= len(value) <= 512:
            raise HTTPException(422, "agent_tool_parameters_invalid")


def call(conn, row, tool_call_id, method, params, actor):
    if method == "list_verification_requests":
        data = verification.list_verification_requests(actor, **{"limit": 20, "offset": 0, **params}, conn=conn)
        offset, limit = params.get("offset", 0), params.get("limit", 20)
        return data | {"next_offset": offset + limit if offset + limit < data["total"] else None}
    if method in {"read_verification_request", "cancel_verification_request"}:
        request_id = params.get("request_id")
        if request_id is None:
            # 未知受理只查可信消息＋原 tool call 对应身份，不触发创建或补偿实验。
            prior = conn.execute(
                "SELECT id FROM verification_requests WHERE tenant_id = ? AND caller_type = ? AND caller_id = ? AND client_request_id = ?",
                (actor.tenant_id, actor.kind, actor.subject_id, str(_client_request_id(row, params["creation_tool_call_id"]))),
            ).fetchone()
            if prior is None:
                return {"accepted": False, "creation_tool_call_id": params["creation_tool_call_id"]}
            request_id = prior["id"]
        handler = verification.get_verification_request if method == "read_verification_request" else verification.cancel_verification_request
        return {"request": handler(request_id, actor, conn=conn), "url": f"/verifications/{request_id}"}

    identity = {"request_sha256": verification._digest(params)}
    prior = conn.execute("SELECT * FROM agent_actions WHERE message_id = ? AND tool_call_id = ?", (row["id"], tool_call_id)).fetchone()
    if prior:
        if prior["method"] != method or prior["params_json"] != dump_json(identity):
            raise HTTPException(409, "同一次复测动作的参数已改变，请明确发起新请求。")
        detail = verification.get_verification_request(prior["verification_request_id"], actor, conn=conn)
        action_id, replay = prior["id"], True
    else:
        body = _create_body(params, _client_request_id(row, tool_call_id))
        payload = body.model_dump(mode="json", exclude={"attachment_ids"})
        if body.test_egress is not None:
            payload["test_egress"] = body.test_egress
        identifiers = list(body.attachment_ids)
        if body.report.type == "conversation_attachment" and body.report.attachment_id not in identifiers:
            identifiers.append(body.report.attachment_id)
        if identifiers:
            batch_id = attachments.copy_attachments_to_batch(conn, row, identifiers, actor)
            payload["resource_batch_id"] = batch_id
            if body.report.type == "conversation_attachment":
                attachment = attachments.attachment_row(conn, row["conversation_id"], body.report.attachment_id, sent_only=True)
                # 批次服务按唯一文件名校验；attachment_id 从不作为 member ID 使用。
                member = next(item for item in _list_resource_batch_members(conn, batch_id) if item["path"] == attachment["filename"])
                payload["report"] = {"type": "file", "batch_member_id": member["id"]}
        detail, replay = verification.create_verification_request(CreateVerificationRequest.model_validate(payload), actor, conn=conn)
        action_id = "act_" + uuid4().hex
        conn.execute(
            "INSERT INTO agent_actions(id, message_id, tool_call_id, method, params_json, kind, verification_request_id, "
            "source_vulnerability_id, source_observation_id) VALUES (?, ?, ?, ?, ?, 'verification', ?, ?, ?)",
            (action_id, row["id"], tool_call_id, method, dump_json(identity), detail["id"],
             body.report.vulnerability_id if body.report.type == "platform_vulnerability" else None,
             body.report.observation_id if body.report.type == "platform_vulnerability" else None),
        )
        conn.execute("UPDATE agent_messages SET revision = nextval('agent_message_revision') WHERE id = ?", (row["id"],))
    return {"action_id": action_id, "request_id": detail["id"], "execution_status": detail["execution_status"],
            "url": f"/verifications/{detail['id']}", "replayed": replay}
