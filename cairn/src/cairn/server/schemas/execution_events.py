"""执行事件账本的严格生产者协议与安全读模型。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EXECUTION_EVENT_PROTOCOL = "cairn-execution-event-v1"
EXECUTION_EVENT_RECEIPT_PROTOCOL = "cairn-execution-event-receipt-v1"
EXECUTION_EVENT_KIND = "checkpoint"
EXECUTION_EVENT_CATEGORIES = (
    "progress",
    "observation",
    "hypothesis",
    "limitation",
    "handoff",
)
EXECUTION_EVENT_MAX_EVENTS = 64
EXECUTION_EVENT_MAX_EVENT_BYTES = 16 * 1024
EXECUTION_EVENT_MAX_SPOOL_BYTES = 512 * 1024
EXECUTION_EVENT_MAX_SUMMARY_CHARS = 1000
EXECUTION_EVENT_MAX_DETAILS_BYTES = 16 * 1024
EXECUTION_EVENT_MAX_DETAILS_DEPTH = 6
EXECUTION_EVENT_MAX_DETAILS_NODES = 256
EXECUTION_EVENT_MAX_COLLECTION_ITEMS = 64
EXECUTION_EVENT_MAX_DETAIL_STRING_CHARS = 4000
EXECUTION_EVENT_MAX_DETAIL_KEY_CHARS = 128

ExecutionEventCategory = Literal[
    "progress",
    "observation",
    "hypothesis",
    "limitation",
    "handoff",
]
ExecutionEventGenerationDisposition = Literal[
    "current_at_receipt",
    "historical_at_receipt",
]
ExecutionEventReceiptDisposition = Literal["received", "replayed"]

_RESERVED_DETAIL_KEYS = {
    "artifact",
    "artifacts",
    "artifact_id",
    "artifact_ids",
    "artifact_handle",
    "artifact_handles",
    "artifact_path",
    "artifact_paths",
    "evidence_ref",
    "evidence_refs",
    "path",
    "paths",
    "project_id",
    "task_run_id",
    "owner",
    "owner_subject",
    "verified_dispatcher_id",
    "dispatcher_id",
    "generation",
    "execution_generation",
    "generation_disposition",
    "producer_event_key",
    "payload",
    "payload_json",
    "payload_digest",
    "payload_digest_sha256",
    "digest",
    "received_at",
    "created_at",
    "updated_at",
    "receipt",
    "receipt_disposition",
    "promotion",
    "promotion_status",
    "business_acceptance",
}
_RESERVED_DETAIL_KEYS_COMPACT = {
    key.replace("_", "") for key in _RESERVED_DETAIL_KEYS
}
_SAFE_PRODUCER_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_DETAIL_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_PHASE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ARTIFACT_DETAIL_KEY_RE = re.compile(
    r"artifact(?:s|id|ids|handle|handles|path|paths|ref|refs)?$"
)


def _validate_detail_key(key: str) -> None:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")
    compact = normalized.replace("_", "")
    tokens = set(normalized.split("_"))
    if (
        key != key.strip()
        or len(key) > EXECUTION_EVENT_MAX_DETAIL_KEY_CHARS
        or not _SAFE_DETAIL_KEY_RE.fullmatch(key)
        or normalized in _RESERVED_DETAIL_KEYS
        or compact in _RESERVED_DETAIL_KEYS_COMPACT
        or _ARTIFACT_DETAIL_KEY_RE.search(compact)
        or compact in {"evidenceref", "evidencerefs"}
        or compact.endswith(("path", "paths"))
        or tokens.intersection({"artifact", "artifacts", "path", "paths"})
        or ("evidence" in tokens and ("ref" in tokens or "refs" in tokens))
    ):
        raise ValueError("details contains a reserved or unsafe key")


def _validate_details_value(value: Any, *, depth: int, nodes: list[int]) -> None:
    nodes[0] += 1
    if nodes[0] > EXECUTION_EVENT_MAX_DETAILS_NODES:
        raise ValueError("details contains too many values")
    if depth > EXECUTION_EVENT_MAX_DETAILS_DEPTH:
        raise ValueError("details exceeds maximum nesting depth")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("details contains a non-finite number")
        return
    if isinstance(value, str):
        if "\0" in value:
            raise ValueError("details strings must not contain NUL")
        if len(value) > EXECUTION_EVENT_MAX_DETAIL_STRING_CHARS:
            raise ValueError("details string exceeds maximum length")
        return
    if isinstance(value, list):
        if len(value) > EXECUTION_EVENT_MAX_COLLECTION_ITEMS:
            raise ValueError("details list contains too many items")
        for item in value:
            _validate_details_value(item, depth=depth + 1, nodes=nodes)
        return
    if isinstance(value, dict):
        if len(value) > EXECUTION_EVENT_MAX_COLLECTION_ITEMS:
            raise ValueError("details object contains too many fields")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("details object keys must be strings")
            _validate_detail_key(key)
            _validate_details_value(item, depth=depth + 1, nodes=nodes)
        return
    raise ValueError("details must contain JSON values only")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class ExecutionEventCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    category: ExecutionEventCategory
    summary: str = Field(min_length=1, max_length=EXECUTION_EVENT_MAX_SUMMARY_CHARS)
    details: dict[str, Any]

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        if not value.strip() or "\0" in value:
            raise ValueError("summary must not be blank or contain NUL")
        return value

    @field_validator("details")
    @classmethod
    def validate_details(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_details_value(value, depth=1, nodes=[0])
        if len(_canonical_json_bytes(value)) > EXECUTION_EVENT_MAX_DETAILS_BYTES:
            raise ValueError("details exceeds maximum encoded size")
        return value


class ExecutionEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocol: Literal["cairn-execution-event-v1"]
    producer_event_key: str = Field(min_length=1, max_length=128)
    kind: Literal["checkpoint"]
    phase: str = Field(min_length=1, max_length=64)
    recorded_at: str
    checkpoint: ExecutionEventCheckpoint

    @field_validator("producer_event_key")
    @classmethod
    def validate_producer_event_key(cls, value: str) -> str:
        if not _SAFE_PRODUCER_KEY_RE.fullmatch(value):
            raise ValueError("producer_event_key must be a safe identifier")
        return value

    @field_validator("phase")
    @classmethod
    def validate_phase(cls, value: str) -> str:
        if value != value.strip() or not _SAFE_PHASE_RE.fullmatch(value):
            raise ValueError("phase must be a safe execution phase")
        return value

    @field_validator("recorded_at")
    @classmethod
    def validate_recorded_at(cls, value: str) -> str:
        if not value.endswith("Z") or value != value.strip() or "\0" in value:
            raise ValueError("recorded_at must be a UTC timestamp")
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise ValueError("recorded_at must be a valid UTC timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError("recorded_at must be a UTC timestamp")
        utc_value = parsed.astimezone(timezone.utc)
        timespec = "milliseconds" if utc_value.microsecond % 1000 == 0 else "microseconds"
        return utc_value.isoformat(timespec=timespec).replace("+00:00", "Z")

    @model_validator(mode="after")
    def validate_encoded_size(self) -> ExecutionEventEnvelope:
        if len(_canonical_json_bytes(self.model_dump(mode="json"))) > EXECUTION_EVENT_MAX_EVENT_BYTES:
            raise ValueError("execution event exceeds maximum encoded size")
        return self


class ExecutionEventCapabilities(BaseModel):
    enabled: bool
    protocol: Literal["cairn-execution-event-v1"] = EXECUTION_EVENT_PROTOCOL
    kinds: list[Literal["checkpoint"]] = Field(default_factory=lambda: [EXECUTION_EVENT_KIND])
    categories: list[ExecutionEventCategory] = Field(
        default_factory=lambda: list(EXECUTION_EVENT_CATEGORIES)
    )
    max_events: int = EXECUTION_EVENT_MAX_EVENTS
    max_event_bytes: int = EXECUTION_EVENT_MAX_EVENT_BYTES
    max_spool_bytes: int = EXECUTION_EVENT_MAX_SPOOL_BYTES
    max_summary_chars: int = EXECUTION_EVENT_MAX_SUMMARY_CHARS
    max_details_bytes: int = EXECUTION_EVENT_MAX_DETAILS_BYTES
    max_details_depth: int = EXECUTION_EVENT_MAX_DETAILS_DEPTH


class ExecutionEventMetadata(BaseModel):
    event_id: str
    task_run_id: str
    protocol: Literal["cairn-execution-event-v1"]
    kind: Literal["checkpoint"]
    category: ExecutionEventCategory
    phase: str
    producer_recorded_at: str
    received_at: str
    execution_generation: int
    generation_disposition: ExecutionEventGenerationDisposition


class ExecutionEventReceipt(BaseModel):
    protocol: Literal["cairn-execution-event-receipt-v1"] = EXECUTION_EVENT_RECEIPT_PROTOCOL
    event_id: str
    producer_event_key: str
    payload_digest_sha256: str
    disposition: ExecutionEventReceiptDisposition
    generation_disposition: ExecutionEventGenerationDisposition
    received_at: str


__all__ = [name for name in globals() if not name.startswith("_")]
