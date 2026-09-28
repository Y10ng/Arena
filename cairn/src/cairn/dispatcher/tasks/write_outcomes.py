"""Dispatcher 写请求的稳定分类与同请求重放策略。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cairn.dispatcher.protocol.client import ApiResult


WriteDispositionName = Literal[
    "accepted",
    "transient",
    "policy_denied",
    "request_contract",
    "business_acceptance",
    "stale_business_state",
    "receipt_conflict",
]


@dataclass(frozen=True, slots=True)
class WriteDisposition:
    """把 HTTP 结果拆成稳定语义，避免用一个 retryable bool 混淆重放与重跑。"""

    disposition: WriteDispositionName
    error_type: str | None
    retry_same_write: bool
    safe_code: str | None = None

    @property
    def accepted(self) -> bool:
        return self.disposition == "accepted"


_STALE_CODES = {
    "assessment_report_claim_mismatch",
    "execution_delivery_owner_mismatch",
    "execution_delivery_source_mismatch",
    "execution_generation_mismatch",
    "execution_retry_blocked",
    "intent_claim_mismatch",
    "lease_mismatch",
    "reason_claim_mismatch",
    "stale_business_state",
    "vulnerability_observation_not_found",
}
_RECEIPT_CONFLICT_CODES = {
    "assessment_report_finish_receipt_conflict",
    "assessment_report_snapshot_receipt_conflict",
    "assessment_report_task_run_terminal_conflict",
    "execution_event_receipt_conflict",
    "receipt_conflict",
    "submission_conflict",
}


def response_detail_code(response: ApiResult) -> str | None:
    """只提取受控短码；调用方不得把任意响应正文写进诊断或 UI。"""
    data = response.data
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("code")
    if not isinstance(detail, str):
        return None
    code = detail.strip().split(":", 1)[0]
    if not code or len(code) > 128:
        return None
    if not all(character.isalnum() or character in {"_", "-", "."} for character in code):
        return None
    return code


def classify_write_response(
    response: ApiResult,
    *,
    client_error: Literal["request_contract", "business_acceptance"] = "business_acceptance",
) -> WriteDisposition:
    """分类一次 Server 写响应；仅瞬态 transport/server 状态允许重放同一请求。"""
    status = response.status_code
    code = response_detail_code(response)
    if 200 <= status < 300:
        return WriteDisposition("accepted", None, False, code)
    if status == 0 or status in {408, 429} or 500 <= status < 600:
        return WriteDisposition("transient", "server_write_failed", True, code)
    if status in {401, 403}:
        return WriteDisposition("policy_denied", "policy_denied", False, code)
    if code in _RECEIPT_CONFLICT_CODES:
        return WriteDisposition("receipt_conflict", "receipt_conflict", False, code)
    if status in {404, 409, 412} and code in _STALE_CODES:
        return WriteDisposition("stale_business_state", "stale_business_state", False, code)
    if status in {400, 404, 405, 409, 412, 422}:
        return WriteDisposition(client_error, client_error, False, code)
    return WriteDisposition("business_acceptance", "business_acceptance", False, code)
