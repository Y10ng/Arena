"""AI Agent 注入 Payload 记录 schema。"""

from __future__ import annotations

from .common import *

InjectionPayloadResponseStatus = Literal[
    "pending",
    "returned",
    "empty",
    "blocked",
    "timeout",
    "error",
    "not_sent",
    "interrupted",
]

InjectionPayloadTerminalStatus = Literal[
    "returned",
    "empty",
    "blocked",
    "timeout",
    "error",
    "not_sent",
    "interrupted",
]


class InjectionPayloadOperation(BaseModel):
    operation: Literal["begin", "finish"]
    capture_id: str
    task_run_id: str
    phase: str | None = None
    injection_type: str | None = None
    payload_text: str | None = None
    original_format: str | None = None
    response_status: InjectionPayloadTerminalStatus | None = None
    response_text: str | None = None
    response_format: str | None = None

    @field_validator("capture_id")
    @classmethod
    def validate_capture_id(cls, value: str) -> str:
        text = value.strip()
        if not text or len(text) > 128 or not fullmatch(r"[A-Za-z0-9._:-]+", text):
            raise ValueError("must be 1-128 safe identifier characters")
        return text

    @field_validator("task_run_id")
    @classmethod
    def validate_task_run_id(cls, value: str) -> str:
        text = value.strip()
        if not text or len(text) > 128:
            raise ValueError("must be 1-128 characters")
        return text

    @field_validator("phase", "injection_type", "original_format", "response_format")
    @classmethod
    def validate_descriptive_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        text = value.strip()
        limits = {
            "phase": 64,
            "injection_type": 200,
            "original_format": 1000,
            "response_format": 1000,
        }
        if not text:
            raise ValueError("must not be empty")
        if len(text) > limits[info.field_name]:
            raise ValueError(f"must be at most {limits[info.field_name]} characters")
        return text

    @model_validator(mode="after")
    def validate_operation_fields(self):
        if self.operation == "begin":
            required = (
                self.phase,
                self.injection_type,
                self.payload_text,
                self.original_format,
            )
            if any(value is None for value in required):
                raise ValueError(
                    "begin requires phase, injection_type, payload_text, and original_format"
                )
            if self.response_status is not None:
                raise ValueError("begin must not include response_status")
        else:
            if self.response_status is None:
                raise ValueError("finish requires response_status")
            if any(
                value is not None
                for value in (
                    self.phase,
                    self.injection_type,
                    self.payload_text,
                    self.original_format,
                )
            ):
                raise ValueError("finish must not redefine begin fields")
        return self


class BatchInjectionPayloadOperationsRequest(BaseModel):
    operations: list[InjectionPayloadOperation] = Field(min_length=1, max_length=200)


class InjectionPayloadRecord(BaseModel):
    id: str
    project_id: str
    task_run_id: str
    phase: str
    capture_id: str
    injection_type: str
    payload_text: str
    original_format: str
    response_status: InjectionPayloadResponseStatus
    response_text: str | None = None
    response_format: str | None = None
    created_at: str
    updated_at: str


class InjectionPayloadListItem(BaseModel):
    id: str
    project_id: str
    task_run_id: str
    phase: str
    capture_id: str
    injection_type: str
    payload_preview: str
    original_format: str
    response_status: InjectionPayloadResponseStatus
    response_preview: str | None = None
    response_format: str | None = None
    created_at: str
    updated_at: str


class InjectionPayloadListResponse(BaseModel):
    items: list[InjectionPayloadListItem]
    total: int
    limit: int
    offset: int


class InjectionPayloadCategorySummary(BaseModel):
    injection_type: str
    count: int


class InjectionPayloadOperationResult(BaseModel):
    capture_id: str
    record_id: str
    response_status: InjectionPayloadResponseStatus


class BatchInjectionPayloadOperationsResponse(BaseModel):
    results: list[InjectionPayloadOperationResult]
