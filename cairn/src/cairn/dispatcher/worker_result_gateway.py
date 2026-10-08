"""Worker 结果进入业务任务前的统一解码与契约校验入口。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Callable
from uuid import UUID

from pydantic import ValidationError

from cairn.dispatcher.contracts import PhaseContractError
from cairn.dispatcher.worker_protocol.submission import (
    CANDIDATE_OUTCOME_CODES,
    RESULT_KINDS,
    RESULT_PROTOCOL,
    SUBMISSION_FAILURE_CODES,
    sanitize_submission_diagnostics,
    validate_phase_data,
)

from cairn.dispatcher.worker_protocol import WorkerProtocolError

ValidationResult = tuple[str, object | None]
ResultValidator = Callable[[dict[str, object]], ValidationResult]


@dataclass(frozen=True, slots=True)
class WorkerResultFailure:
    failure_class: str
    code: str
    message: str
    fingerprint: str
    retryable: bool = False
    origin: str = "dispatcher_validation"
    phase: str = ""
    path: str | None = None
    exit_code: int | None = None
    submission_diagnostics: dict[str, object] | None = None

    def details(self) -> dict[str, object]:
        details: dict[str, object] = {
            "origin": self.origin, "code": self.code, "phase": self.phase,
            "error_type": worker_failure_error_type(self), "message": self.message,
        }
        if self.path is not None:
            details["path"] = self.path
        if self.exit_code is not None:
            details["exit_code"] = self.exit_code
        if self.submission_diagnostics:
            details.update(sanitize_submission_diagnostics(self.submission_diagnostics, self.phase))
        return details


class WorkerRequestError(ValueError):
    """Python 请求生产者拒绝无效输入，不等在线健康检查后再执行。"""

    def __init__(
        self,
        path: str,
        *,
        code: str = "invalid_request",
        message: str | None = None,
        phase: str = "reason_execute",
    ):
        self.failure = WorkerResultFailure(
            failure_class="request_contract",
            code=code,
            message=message or f"Worker 请求契约无效：{path}",
            fingerprint="",
            origin="dispatcher_request",
            phase=phase,
            path=path,
        )
        super().__init__(self.failure.message)


@dataclass(frozen=True, slots=True)
class WorkerResultOutcome:
    payload: dict[str, object] | None = None
    kind: str | None = None
    validated: object | None = None
    failure: WorkerResultFailure | None = None

    @property
    def description(self) -> str | None:
        return self.validated if isinstance(self.validated, str) else None

    @property
    def succeeded(self) -> bool:
        return self.failure is None


def worker_failure_error_type(failure: WorkerResultFailure) -> str:
    """把 Gateway failure class 映射为任务层稳定错误类型。"""
    return {
        "contract": "contract_error",
        "request_contract": "request_contract",
        "result_schema": "result_schema",
        "submission_missing": "submission_missing",
        "submission_invalid": "submission_invalid",
        "artifact": "artifact_error",
        "permission": "policy_denied",
        "business_acceptance": "business_acceptance",
        "stale_business_state": "stale_business_state",
        "server_write": "server_write_failed",
        "process": "worker_command_failed",
        "candidate_integrity": "candidate_integrity",
        "oom": "worker_oom",
        "healthcheck_timeout": "healthcheck_timeout",
        "transport": "transport_error",
        "provider_auth": "provider_auth",
        "provider_rate_limit": "provider_rate_limit",
        "provider": "provider_error",
        "session": "session_error",
        "cancelled": "cancelled",
        "timeout": "worker_timeout",
    }.get(failure.failure_class, "protocol_error")


_PI_ERROR_CLASSES = {
    **{
        code: "candidate_integrity"
        for code in CANDIDATE_OUTCOME_CODES
        if code.startswith("candidate_")
    },
    "provider_auth": "provider_auth", "provider_rate_limit": "provider_rate_limit",
    "provider_error": "provider", "unsupported_model_config": "request_contract",
    "invalid_request": "request_contract", "invalid_result_schema": "request_contract",
    "result_schema": "result_schema", "submission_conflict": "result_schema",
    "submission_missing": "submission_missing", "submission_invalid": "submission_invalid",
    "submission_injected_too_large": "request_contract",
    "artifact_unknown": "artifact", "artifact_changed": "artifact",
    "artifact_invalid": "artifact", "artifact_unreadable": "artifact",
    "artifact_permission": "permission", "tool_permission": "permission",
    "session_locked": "session", "session_exists": "session",
    "session_ownership": "session", "session_corrupt": "session",
    "cancelled": "cancelled", "deadline_exceeded": "timeout", "runner_error": "transport",
}
# 只允许请求协议声明的字段路径；不从任意 message 提取值或路径。
_REQUEST_PATHS = frozenset({
    "$", "protocol", "project_id", "task_run_id", "phase", "prompt", "session",
    "resume", "conclude_only", "timeout_seconds", "max_result_repairs", "result_schema",
    "model", "model.protocol", "model.auth_header", "model.model_name",
    "model.inference_endpoint", "model.base_url", "model.api_key", "model.custom_headers",
    "binding", "binding.slot_key", "binding.surface_key", "binding.test_family",
    "binding.verification_observation_id", "binding.review_input_digest",
    "binding.max_intents", "binding.allowed_fact_ids", "binding.allowed_intent_ids",
    "binding.structured_intent_binding", "cwd", "session_root",
})
_PI_FIELDS = {
    "protocol", "engine", "project_id", "task_run_id", "phase", "session",
    "status", "result", "submission_id", "usage", "error",
}


def decode_pi_result(
    stdout: str,
    *,
    phase: str,
    validator: ResultValidator,
    project_id: str | None = None,
    task_run_id: str | None = None,
    session: str | None = None,
    assessment_protocol: str | None = None,
) -> WorkerResultOutcome:
    """只接收一个程序生成的终态信封；进程取消/失败还必须由调用方拦截。"""
    def invalid(code: str, message: str, failure_class: str = "protocol") -> WorkerResultOutcome:
        return WorkerResultOutcome(failure=_failure(
            failure_class=failure_class, code=code, phase=phase, message=message, payload=None,
        ))

    if not isinstance(stdout, str) or len(stdout.encode("utf-8", errors="replace")) > 2 * 1024 * 1024:
        return invalid("result_size_invalid", "pi result exceeds the transport limit")
    try:
        envelope = json.loads(stdout, parse_constant=_reject_json_constant, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, TypeError, RecursionError):
        return invalid("invalid_result_json", "pi stdout must contain exactly one complete JSON object")
    if not isinstance(envelope, dict) or set(envelope) != _PI_FIELDS:
        return invalid("result_envelope_invalid", "pi result envelope fields do not match the execution contract")
    if envelope["protocol"] != RESULT_PROTOCOL or envelope["engine"] != "pi" or phase not in RESULT_KINDS:
        return invalid("result_version_unsupported", "unsupported pi result version, engine or phase")
    if envelope["phase"] != phase:
        return invalid("result_identity_mismatch", "pi result phase does not match the invocation")
    for field, expected in (("project_id", project_id), ("task_run_id", task_run_id), ("session", session)):
        value = envelope[field]
        if not isinstance(value, str) or not value.strip() or (expected is not None and value != expected):
            return invalid("result_identity_mismatch", "pi result identity does not match the invocation")
    try:
        value = envelope["session"]
        if not value.startswith("pi:") or str(UUID(value[3:])) != value[3:]:
            raise ValueError("invalid session")
    except (ValueError, AttributeError):
        return invalid("result_session_invalid", "pi result session must be a canonical pi UUID")
    status = envelope["status"]
    if status not in ("succeeded", "failed", "cancelled"):
        return invalid("result_status_invalid", "unknown pi terminal status")
    usage = envelope["usage"]
    if usage is not None and not isinstance(usage, dict):
        return invalid("result_usage_invalid", "pi usage must be an object or null")
    if status != "succeeded":
        error = envelope["error"]
        if envelope["result"] is not None or envelope["submission_id"] is not None or not isinstance(error, dict):
            return invalid("result_terminal_conflict", "failed pi envelope cannot contain an accepted result")
        code = error.get("code")
        if not isinstance(code, str) or code not in _PI_ERROR_CLASSES:
            return invalid("runner_error", "pi runner returned an unrecognized failure", "transport")
        if status == "cancelled" and code not in ("cancelled", "deadline_exceeded"):
            return invalid("result_terminal_conflict", "pi cancellation status conflicts with its error code")
        failure_class = _PI_ERROR_CLASSES[code]
        path = error.get("path")
        path = path if code == "invalid_request" and isinstance(path, str) and path in _REQUEST_PATHS else None
        origin = (
            "pi_submission" if code in SUBMISSION_FAILURE_CODES
            else "pi_request" if failure_class == "request_contract"
            else "pi_sdk" if failure_class in {"provider_auth", "provider_rate_limit", "provider"}
            else "pi_submission" if failure_class == "result_schema"
            else "pi_session" if failure_class == "session"
            else "pi_runner"
        )
        # 外部异常正文不进入诊断模型，固定 code 与已声明 path 足以定位失败。
        return WorkerResultOutcome(failure=_failure(
            failure_class=failure_class, code=code, phase=phase,
            message=f"Pi 执行失败：{code}" + (f"（{path}）" if path else ""),
            payload=None, origin=origin, path=path,
            submission_diagnostics=sanitize_submission_diagnostics(error, phase),
        ))
    result = envelope["result"]
    if envelope["error"] is not None or not isinstance(result, dict):
        return invalid("result_terminal_conflict", "successful pi envelope requires a result and no error")
    if not isinstance(envelope["submission_id"], str) or not re.fullmatch(r"[0-9a-f]{64}", envelope["submission_id"]):
        return invalid("result_submission_invalid", "pi submission identifier must be a SHA256 digest")
    if result.get("accepted") is True:
        if result.get("kind") != RESULT_KINDS[phase]:
            return invalid("result_kind_invalid", "pi result kind does not match the invocation")
        data = result.get("data")
        if not isinstance(data, dict):
            return invalid("result_data_invalid", "pi result data must be an object")
        try:
            validate_phase_data(
                phase,
                data,
                assessment_protocol=assessment_protocol,
            )
        except ValidationError:
            # extra 字段名与 mapping key 也可能是业务秘密，不发布 Pydantic 原始 loc。
            return invalid("phase_shape_invalid", "Worker 结果不符合阶段数据结构：phase_shape_invalid", "result_schema")
    return validate_worker_payload(result, phase=phase, validator=validator)


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def validate_worker_payload(
    payload: dict[str, object],
    *,
    phase: str,
    validator: ResultValidator,
) -> WorkerResultOutcome:
    """结构化结果直接进入既有语义校验，不再序列化后重复解码。"""
    try:
        kind, validated = validator(payload)
    except WorkerProtocolError as exc:
        return WorkerResultOutcome(
            payload=payload,
            failure=_failure(
                failure_class="protocol",
                code="result_envelope_invalid",
                phase=phase,
                message="Worker 业务信封不符合阶段契约：result_envelope_invalid",
                payload=payload,
            ),
        )
    except (TypeError, ValueError) as exc:
        return WorkerResultOutcome(
            payload=payload,
            failure=_failure(
                failure_class="business_acceptance",
                code="phase_contract_invalid",
                phase=phase,
                # 只发布权威规则显式声明的原因，不解释任意 ValueError/Pydantic 文本。
                message="Worker 结果未通过阶段业务验收：" + (
                    exc.public_reason
                    if isinstance(exc, PhaseContractError)
                    else "phase_contract_invalid"
                ),
                payload=payload,
            ),
        )
    return WorkerResultOutcome(
        payload=payload,
        kind=kind,
        validated=validated,
    )


def _failure(
    *,
    failure_class: str,
    code: str,
    phase: str,
    message: str,
    payload: dict[str, object] | None,
    origin: str = "dispatcher_validation",
    path: str | None = None,
    submission_diagnostics: dict[str, object] | None = None,
) -> WorkerResultFailure:
    normalized = {
        "failure_class": failure_class,
        "code": code,
        "phase": phase,
        "message": message,
        "payload_hash": _payload_hash(payload),
    }
    fingerprint = sha256(
        json.dumps(normalized, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return WorkerResultFailure(
        failure_class=failure_class,
        code=code,
        message=message,
        fingerprint=fingerprint,
        origin=origin,
        phase=phase,
        path=path,
        submission_diagnostics=submission_diagnostics,
    )


def _payload_hash(payload: dict[str, object] | None) -> str:
    if payload is None:
        return ""
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(serialized.encode("utf-8")).hexdigest()
