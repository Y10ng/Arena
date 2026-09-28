"""`cairn-assessment-v2` Worker 结果 envelope 定义与校验。"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

ASSESSMENT_PROTOCOL_ID = "cairn-assessment-v2"

PHASE_RESULT_KINDS = {
    "bootstrap_execute": "bootstrap_result",
    "bootstrap_conclude": "bootstrap_conclude_result",
    "explore_execute": "explore_result",
    "explore_conclude": "explore_conclude_result",
    "verifier_execute": "verifier_result",
    "verifier_conclude": "verifier_conclude_result",
    "reason_execute": "reason_result",
    "report_execute": "report_result",
}


class WorkerProtocolError(ValueError):
    """Worker 结果不满足版本化 wire protocol。"""


class AcceptedResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocol: Literal[ASSESSMENT_PROTOCOL_ID]
    accepted: Literal[True]
    kind: str = Field(min_length=1)
    data: dict[str, Any]
    reason: str | None = Field(default=None, min_length=1)


class RejectedResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocol: Literal[ASSESSMENT_PROTOCOL_ID]
    accepted: Literal[False]
    reason: str = Field(min_length=1)


ResultEnvelope = Annotated[
    AcceptedResultEnvelope | RejectedResultEnvelope,
    Field(discriminator="accepted"),
]
_RESULT_ENVELOPE_ADAPTER = TypeAdapter(ResultEnvelope)


def validate_result_envelope(
    payload: dict[str, Any],
    *,
    allowed_kinds: set[str],
) -> tuple[bool, dict[str, Any] | None]:
    """严格校验版本、顶层字段和 phase kind，拒绝旧协议与额外字段。"""
    try:
        envelope = _RESULT_ENVELOPE_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise WorkerProtocolError(_format_validation_error(exc)) from exc

    if isinstance(envelope, RejectedResultEnvelope):
        return False, None
    if envelope.kind not in allowed_kinds:
        raise WorkerProtocolError(f"unexpected result kind: {envelope.kind}")
    # 业务 validator 会在原 payload 上做受控 canonicalization，不能返回 Pydantic 的拷贝。
    data = payload.get("data")
    assert isinstance(data, dict)
    return True, data


def canonical_result_example(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    """生成 Prompt、Mock 与测试可共享的 canonical success envelope。"""
    return {
        "protocol": ASSESSMENT_PROTOCOL_ID,
        "accepted": True,
        "kind": kind,
        "data": data,
    }


def protocol_json_schema() -> dict[str, Any]:
    """返回完整 Worker envelope schema，供协议冻结和审计使用。"""
    return _RESULT_ENVELOPE_ADAPTER.json_schema()


def protocol_transport_json_schema() -> dict[str, Any]:
    """返回 Claude Code StructuredOutput 使用的无 union transport schema。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "protocol": {
                "type": "string",
                "enum": [ASSESSMENT_PROTOCOL_ID],
            },
            "accepted": {"type": "boolean"},
            "kind": {"type": "string"},
            "data": {"type": "object"},
            "reason": {"type": "string"},
        },
        # accepted/rejected 的条件关系继续由统一 envelope validator 严格校验。
        "required": ["protocol", "accepted"],
    }


def protocol_schema_digest() -> str:
    """返回稳定 schema digest，供后续项目 protocol bundle 冻结。"""
    schema = protocol_json_schema()
    encoded = json.dumps(
        schema,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _format_validation_error(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False, include_context=False)
    if not errors:
        return "invalid worker result envelope"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "$"
    message = str(first.get("msg") or "invalid value")
    return f"invalid worker result envelope at {location}: {message}"
