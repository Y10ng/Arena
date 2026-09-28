"""TaskRun 候选捕获协议、不可变收据和安全读模型。"""

from __future__ import annotations

from hashlib import sha256
import json
import math
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CANDIDATE_SPOOL_PROTOCOL = "cairn-candidate-spool-v1"
CANDIDATE_FRAME_PROTOCOL = "cairn-candidate-frame-v1"
CANDIDATE_RECEIPT_PROTOCOL = "cairn-candidate-capture-receipt-v1"
CANDIDATE_MANIFEST_HEADER = "X-Cairn-Candidate-Manifest"
CANDIDATE_MANIFEST_ENCODING = "base64url-utf8-canonical-json-no-padding"
CANDIDATE_SOURCE_FORMAT = "pi-toolcall-delta-utf8-v1"
CANDIDATE_MAX_CANDIDATES_PER_TASK_RUN = 16
CANDIDATE_MAX_RAW_BYTES = 2 * 1024 * 1024
CANDIDATE_MAX_MANIFEST_BYTES = 4 * 1024
CANDIDATE_MAX_SPOOL_BYTES = 34 * 1024 * 1024
CANDIDATE_MAX_OUTCOMES_PER_CANDIDATE = 64

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_PHASE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PRODUCER_KEY_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}:[1-9][0-9]*:[0-9]+$"
)
_SAFE_DIAGNOSTIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SESSION_RE = re.compile(
    r"^pi:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

CandidateGenerationDisposition = Literal[
    "current_at_receipt",
    "historical_at_receipt",
]
CandidateReceiptDisposition = Literal["received", "replayed"]
CandidateRecordKind = Literal["candidate", "outcome"]


def _ecmascript_number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("canonical JSON does not permit non-finite numbers")
    if value == 0:
        return "0"
    text = repr(value).lower()
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    if "e" not in text:
        return sign + (text[:-2] if text.endswith(".0") else text)

    mantissa, exponent_text = text.split("e", 1)
    exponent = int(exponent_text)
    digits = mantissa.replace(".", "")
    magnitude = abs(value)
    if 1e-6 <= magnitude < 1e21:
        decimal_position = mantissa.find(".")
        if decimal_position < 0:
            decimal_position = len(mantissa)
        decimal_position += exponent
        if decimal_position <= 0:
            rendered = "0." + ("0" * -decimal_position) + digits
        elif decimal_position >= len(digits):
            rendered = digits + ("0" * (decimal_position - len(digits)))
        else:
            rendered = digits[:decimal_position] + "." + digits[decimal_position:]
        return sign + rendered

    normalized_mantissa = mantissa[:-2] if mantissa.endswith(".0") else mantissa
    exponent_sign = "+" if exponent >= 0 else "-"
    return f"{sign}{normalized_mantissa}e{exponent_sign}{abs(exponent)}"


def _canonical_json_text(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if not -(2**53 - 1) <= value <= 2**53 - 1:
            raise ValueError("canonical JSON integer exceeds ECMAScript safe range")
        return str(value)
    if isinstance(value, float):
        return _ecmascript_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json_text(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("canonical JSON object keys must be strings")
        keys = sorted(
            value,
            key=lambda key: key.encode("utf-16-be", errors="surrogatepass"),
        )
        return "{" + ",".join(
            _canonical_json_text(key) + ":" + _canonical_json_text(value[key])
            for key in keys
        ) + "}"
    raise ValueError("canonical JSON contains an unsupported value")


def canonical_json_bytes(value: object) -> bytes:
    return _canonical_json_text(value).encode("utf-8", errors="strict")


def canonical_value_sha256(value: object) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def _validate_uuid(value: str, field: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUIDv4")
    return value


def _validate_hex64(value: str) -> str:
    if not _HEX64_RE.fullmatch(value):
        raise ValueError("digest must be lowercase SHA-256 hex")
    return value


class CandidateArtifactIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    handle: str = Field(min_length=1, max_length=128)
    kind: Literal["log", "file", "report", "screenshot", "patch", "raw_output"]
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0, le=9_007_199_254_740_991)

    @field_validator("handle")
    @classmethod
    def validate_handle(cls, value: str) -> str:
        if not value.startswith("local-") or not _SAFE_DIAGNOSTIC_RE.fullmatch(value):
            raise ValueError("artifact handle must be a safe local identity")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _validate_hex64(value)


class CandidateFrameManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocol: Literal["cairn-candidate-frame-v1"]
    record_kind: Literal["candidate"]
    capture_id: str = Field(min_length=36, max_length=36)
    producer_candidate_key: str = Field(min_length=1, max_length=128)
    parent_producer_candidate_key: str | None = Field(max_length=128)
    assistant_turn: int = Field(ge=1, le=1_000_000)
    content_index: int = Field(ge=0, le=1_000_000)
    project_id: str = Field(min_length=1, max_length=256)
    task_run_id: str = Field(min_length=1, max_length=256)
    phase: str = Field(min_length=1, max_length=64)
    session: str = Field(min_length=1, max_length=64)
    binding_sha256: str = Field(min_length=64, max_length=64)
    result_schema_sha256: str = Field(min_length=64, max_length=64)
    raw_sha256: str = Field(min_length=64, max_length=64)
    raw_size_bytes: int = Field(ge=0, le=CANDIDATE_MAX_RAW_BYTES)
    artifacts: list[CandidateArtifactIdentity] = Field(max_length=16)

    @field_validator("capture_id")
    @classmethod
    def validate_capture_id(cls, value: str) -> str:
        return _validate_uuid(value, "capture_id")

    @field_validator("producer_candidate_key")
    @classmethod
    def validate_producer_key(cls, value: str) -> str:
        if not _PRODUCER_KEY_RE.fullmatch(value):
            raise ValueError("producer_candidate_key must be a safe capture key")
        return value

    @field_validator("parent_producer_candidate_key")
    @classmethod
    def validate_parent_key(cls, value: str | None) -> str | None:
        if value is not None and not _PRODUCER_KEY_RE.fullmatch(value):
            raise ValueError("parent_producer_candidate_key must be a safe capture key")
        return value

    @field_validator("project_id", "task_run_id")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if value != value.strip() or "\0" in value:
            raise ValueError("identity must not contain surrounding whitespace or NUL")
        return value

    @field_validator("phase")
    @classmethod
    def validate_phase(cls, value: str) -> str:
        if not _PHASE_RE.fullmatch(value):
            raise ValueError("phase must be a safe execution phase")
        return value

    @field_validator("session")
    @classmethod
    def validate_session(cls, value: str) -> str:
        if not _SESSION_RE.fullmatch(value):
            raise ValueError("session must be a canonical pi session")
        _validate_uuid(value[3:], "session")
        return value

    @field_validator(
        "binding_sha256",
        "result_schema_sha256",
        "raw_sha256",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _validate_hex64(value)

    @model_validator(mode="after")
    def validate_capture_identity(self) -> CandidateFrameManifest:
        expected = f"{self.capture_id}:{self.assistant_turn}:{self.content_index}"
        if self.producer_candidate_key != expected:
            raise ValueError("producer_candidate_key does not match capture coordinates")
        if self.parent_producer_candidate_key == self.producer_candidate_key:
            raise ValueError("candidate cannot be its own parent")
        handles = [artifact.handle for artifact in self.artifacts]
        if handles != sorted(handles) or len(handles) != len(set(handles)):
            raise ValueError("artifacts must use unique handles in sorted order")
        return self


class CandidateOutcomeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocol: Literal["cairn-candidate-frame-v1"]
    record_kind: Literal["outcome"]
    capture_id: str = Field(min_length=36, max_length=36)
    producer_candidate_key: str = Field(min_length=1, max_length=128)
    outcome_sequence: int = Field(ge=1, le=CANDIDATE_MAX_OUTCOMES_PER_CANDIDATE)
    code: str = Field(min_length=1, max_length=128)
    path: str | None = Field(max_length=256)
    keyword: str | None = Field(max_length=128)
    count: int | None = Field(ge=0, le=1_000_000)
    artifact_status: Literal["not_evaluated", "validated", "rejected"]
    binding_status: Literal[
        "not_evaluated",
        "not_required",
        "injected",
        "rejected",
    ]

    @field_validator("capture_id")
    @classmethod
    def validate_capture_id(cls, value: str) -> str:
        return _validate_uuid(value, "capture_id")

    @field_validator("producer_candidate_key")
    @classmethod
    def validate_producer_key(cls, value: str) -> str:
        if not _PRODUCER_KEY_RE.fullmatch(value):
            raise ValueError("producer_candidate_key must be a safe capture key")
        return value

    @field_validator("code", "keyword")
    @classmethod
    def validate_safe_diagnostic(cls, value: str | None) -> str | None:
        if value is not None and not _SAFE_DIAGNOSTIC_RE.fullmatch(value):
            raise ValueError("diagnostic value must be a safe identifier")
        return value

    @field_validator("path")
    @classmethod
    def validate_safe_path(cls, value: str | None) -> str | None:
        if value is not None and (value != value.strip() or "\0" in value):
            raise ValueError("diagnostic path must not contain whitespace padding or NUL")
        return value


class CandidateCapabilities(BaseModel):
    required: Literal[True] = True
    spool_protocol: Literal["cairn-candidate-spool-v1"] = CANDIDATE_SPOOL_PROTOCOL
    frame_protocol: Literal["cairn-candidate-frame-v1"] = CANDIDATE_FRAME_PROTOCOL
    receipt_protocol: Literal["cairn-candidate-capture-receipt-v1"] = (
        CANDIDATE_RECEIPT_PROTOCOL
    )
    manifest_header: Literal["X-Cairn-Candidate-Manifest"] = CANDIDATE_MANIFEST_HEADER
    manifest_encoding: Literal["base64url-utf8-canonical-json-no-padding"] = (
        CANDIDATE_MANIFEST_ENCODING
    )
    content_type: Literal["application/octet-stream"] = "application/octet-stream"
    source_format: Literal["pi-toolcall-delta-utf8-v1"] = CANDIDATE_SOURCE_FORMAT
    max_candidates_per_task_run: int = CANDIDATE_MAX_CANDIDATES_PER_TASK_RUN
    max_raw_bytes: int = CANDIDATE_MAX_RAW_BYTES
    max_manifest_bytes: int = CANDIDATE_MAX_MANIFEST_BYTES
    max_spool_bytes: int = CANDIDATE_MAX_SPOOL_BYTES
    max_outcomes_per_candidate: int = CANDIDATE_MAX_OUTCOMES_PER_CANDIDATE
    max_artifacts_per_candidate: int = 16


class CandidateReceipt(BaseModel):
    protocol: Literal["cairn-candidate-capture-receipt-v1"] = CANDIDATE_RECEIPT_PROTOCOL
    record_kind: CandidateRecordKind
    candidate_id: str
    producer_candidate_key: str
    outcome_sequence: int | None = None
    request_sha256: str
    raw_sha256: str | None = None
    disposition: CandidateReceiptDisposition
    generation_disposition: CandidateGenerationDisposition
    received_at: str


class CandidateMetadata(BaseModel):
    candidate_id: str
    task_run_id: str
    protocol: Literal["cairn-candidate-frame-v1"]
    source_format: Literal["pi-toolcall-delta-utf8-v1"] = CANDIDATE_SOURCE_FORMAT
    phase: str
    assistant_turn: int
    content_index: int
    parent_candidate_id: str | None
    raw_size_bytes: int
    received_at: str
    execution_generation: int
    generation_disposition: CandidateGenerationDisposition
    latest_outcome_code: str | None = None
    latest_outcome_path: str | None = None
    latest_outcome_keyword: str | None = None
    latest_outcome_count: int | None = None
    artifact_status: Literal["not_evaluated", "validated", "rejected"] | None = None
    binding_status: Literal[
        "not_evaluated",
        "not_required",
        "injected",
        "rejected",
    ] | None = None


__all__ = [name for name in globals() if not name.startswith("_")]
