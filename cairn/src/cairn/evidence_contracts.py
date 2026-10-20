"""发现与复测共用的有序证据关系；原件正文仍由 Artifact 保存。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cairn.assessment_contracts import AssessmentEvidenceRef


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    pointer: str = ""
    verifier_run_id: str | None = None

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("pointer", "verifier_run_id")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text and value != "":
            raise ValueError("must not be blank")
        return text


Ref = TypeVar("Ref", EvidenceRef, AssessmentEvidenceRef)
EvidenceSource = Literal["capture", "manual", "historical"]

# 同一份槽位定义同时约束 Python 写入和 Worker 实际使用的 JSON Schema。
_CONTENT_SLOTS = {
    "http": ("request", "response", "request_text", "response_text"),
    "artifact": ("artifact",),
    "text": ("artifact", "text"),
}
_INLINE_SLOTS = {"request_text", "response_text", "text"}


def _evidence_item_schema(schema: dict[str, Any], model: type[BaseModel]) -> None:
    slots = set().union(*_CONTENT_SLOTS.values()) & model.model_fields.keys()
    schema["oneOf"] = [
        {
            "properties": {
                "kind": {"const": kind},
                **{slot: {"type": "null"} for slot in sorted(slots - set(allowed))},
            },
            "anyOf": [
                {"required": [slot], "properties": {slot: {"not": {"type": "null"}}}}
                for slot in allowed if slot in slots
            ],
        }
        for kind, allowed in _CONTENT_SLOTS.items()
    ]
    inline = slots & _INLINE_SLOTS
    if inline:
        schema["allOf"] = [{
            "if": {"required": ["source"], "properties": {"source": {"const": "capture"}}},
            "then": {"properties": {slot: {"type": "null"} for slot in sorted(inline)}},
        }]


class _EvidenceItem(BaseModel, Generic[Ref]):
    model_config = ConfigDict(extra="forbid", strict=True, json_schema_extra=_evidence_item_schema)

    id: str = Field(min_length=1)
    kind: Literal["http", "artifact", "text"]
    role: Literal["primary", "supporting"] = "primary"
    title: str = ""
    source: EvidenceSource
    request: Ref | None = None
    response: Ref | None = None
    artifact: Ref | None = None

    @model_validator(mode="after")
    def validate_content_slots(self) -> _EvidenceItem[Ref]:
        slots = set().union(*_CONTENT_SLOTS.values()) & type(self).model_fields.keys()
        present = {slot for slot in slots if getattr(self, slot) is not None}
        if not present or not present <= set(_CONTENT_SLOTS[self.kind]):
            raise ValueError("evidence kind requires content in its own slots only")
        if self.source == "capture" and present & _INLINE_SLOTS:
            raise ValueError("capture evidence must reference artifacts, not inline text")
        return self


class EvidenceItem(_EvidenceItem[EvidenceRef]):
    # 历史原文不冒充采集原件，也不能因校验或序列化而裁剪空白。
    request_text: str | None = None
    response_text: str | None = None
    text: str | None = None

class WorkerEvidenceItem(_EvidenceItem[AssessmentEvidenceRef]):
    """Worker 只提交已登记原件的引用，不生成或内联复现报文。"""


VerificationText = Annotated[str, Field(min_length=1, pattern=r"\S")]


class VerificationKeyEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reason: VerificationText
    evidence_item_ids: list[VerificationText]


class VerificationTestStep(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: VerificationText
    observation: VerificationText
    evidence_item_ids: list[VerificationText]


def validate_verification_evidence_links(
    items: Iterable[_EvidenceItem],
    key_evidence: Iterable[VerificationKeyEvidence],
    test_steps: Iterable[VerificationTestStep],
) -> None:
    """条目身份不随 handle 解析变化；理由和步骤只能引用同一结果里的条目。"""
    ids = [item.id for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError("本轮证据条目 ID 不能重复")
    known = set(ids)
    for section in (*key_evidence, *test_steps):
        if not set(section.evidence_item_ids) <= known:
            raise ValueError("判断依据与测试步骤只能引用本结果中的证据条目")


def evidence_refs(items: Iterable[_EvidenceItem[Ref]]) -> list[Ref]:
    """按条目及请求、响应、附件顺序生成唯一底层引用投影。"""
    result: list[Ref] = []
    seen: set[tuple[str | None, str | None, str, str | None]] = set()
    for item in items:
        for ref in (item.request, item.response, item.artifact):
            if ref is None:
                continue
            key = (
                ref.artifact_id,
                getattr(ref, "artifact_handle", None),
                ref.pointer,
                ref.verifier_run_id,
            )
            if key not in seen:
                seen.add(key)
                result.append(ref)
    return result
