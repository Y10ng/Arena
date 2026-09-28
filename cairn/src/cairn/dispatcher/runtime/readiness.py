"""Dispatcher 确定性离线 readiness 探针。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Literal, TypeAlias

from docker.errors import DockerException

from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.process import ProcessResult

ReadinessErrorType: TypeAlias = Literal[
    "readiness_timeout",
    "readiness_transport",
    "readiness_oom",
    "readiness_invalid",
    "readiness_process",
]
READINESS_ERROR_TYPES: frozenset[ReadinessErrorType] = frozenset(
    {
        "readiness_timeout",
        "readiness_transport",
        "readiness_oom",
        "readiness_invalid",
        "readiness_process",
    }
)
READINESS_COMMAND = ("cairn-agent", "--self-check")
READINESS_COMMUNICATE_GRACE_SECONDS = 10
_MAX_CAPABILITIES = 64
_MAX_CAPABILITY_CHARS = 128
_READINESS_RUNTIME_EXCEPTIONS = (DockerException, OSError, RuntimeError)


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    """不持有进程输出文件的 readiness 摘要。"""

    ok: bool
    error_type: ReadinessErrorType | None
    returncode: int
    duration_ms: int
    capabilities: frozenset[str]


def run_readiness_check(
    container_manager: ContainerManager,
    container_name: str,
    *,
    timeout_seconds: int,
) -> ReadinessResult:
    """只运行离线 self-check，并把全部执行细节压缩为固定分类。"""
    if timeout_seconds <= 0:
        raise ValueError("readiness timeout must be positive")

    started = time.perf_counter()
    process = None
    result: ProcessResult | None = None
    returncode = 1
    try:
        try:
            before_oom_count = container_manager.oom_kill_count(container_name)
            process = container_manager.build_exec_process(
                container_name,
                {},
                list(READINESS_COMMAND),
                timeout_seconds=timeout_seconds,
            )
            process.start()
            result = process.communicate(
                timeout=timeout_seconds + READINESS_COMMUNICATE_GRACE_SECONDS
            )
            returncode = result.returncode
        except _READINESS_RUNTIME_EXCEPTIONS:
            return _summary(
                started,
                returncode=returncode,
                error_type="readiness_transport",
            )

        assert result is not None
        try:
            _apply_oom_evidence(
                container_manager,
                container_name,
                result,
                before_count=before_oom_count,
            )
        except _READINESS_RUNTIME_EXCEPTIONS:
            return _summary(
                started,
                returncode=returncode,
                error_type="readiness_transport",
            )

        error_type = _process_error_type(result)
        if error_type is not None:
            return _summary(
                started,
                returncode=returncode,
                error_type=error_type,
            )

        try:
            stdout = result.read_stdout()
        except _READINESS_RUNTIME_EXCEPTIONS:
            return _summary(
                started,
                returncode=returncode,
                error_type="readiness_transport",
            )
        capabilities = _parse_self_check(stdout)
        if capabilities is None:
            return _summary(
                started,
                returncode=returncode,
                error_type="readiness_invalid",
            )
        return _summary(
            started,
            returncode=returncode,
            error_type=None,
            capabilities=capabilities,
        )
    finally:
        # readiness 不转交 ProcessResult；清理失败必须暴露，不能遗留包含探针输出的临时文件。
        if process is not None:
            process.cleanup_output()


def _apply_oom_evidence(
    container_manager: ContainerManager,
    container_name: str,
    result: ProcessResult,
    *,
    before_count: int | None,
) -> None:
    # 与业务进程一致：只有当前 exec 以 137 退出时才采纳容器级正向 OOM 证据。
    if result.returncode != 137:
        return
    oom_killed, source = container_manager.detect_oom_kill(
        container_name,
        before_count=before_count,
    )
    result.oom_killed = oom_killed
    result.oom_evidence_source = source


def _process_error_type(result: ProcessResult) -> ReadinessErrorType | None:
    if result.cancelled:
        return "readiness_process"
    if result.oom_killed:
        return "readiness_oom"
    if result.timed_out or result.returncode in (124, 137):
        return "readiness_timeout"
    if result.transport_error:
        return "readiness_transport"
    if result.returncode != 0:
        return "readiness_process"
    return None


def _parse_self_check(stdout: str) -> frozenset[str] | None:
    try:
        payload = json.loads(
            stdout,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (TypeError, ValueError, RecursionError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ok"
        or payload.get("engine") != "pi"
        or payload.get("offline") is not True
    ):
        return None
    raw_capabilities = payload.get("capabilities")
    if not isinstance(raw_capabilities, list) or len(raw_capabilities) > _MAX_CAPABILITIES:
        return None
    capabilities: list[str] = []
    for capability in raw_capabilities:
        if (
            not isinstance(capability, str)
            or not capability
            or capability != capability.strip()
            or len(capability) > _MAX_CAPABILITY_CHARS
            or any(character.isspace() or not character.isprintable() for character in capability)
        ):
            return None
        capabilities.append(capability)
    if len(set(capabilities)) != len(capabilities):
        return None
    return frozenset(capabilities)


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON object key")
        payload[key] = value
    return payload


def _summary(
    started: float,
    *,
    returncode: int,
    error_type: ReadinessErrorType | None,
    capabilities: frozenset[str] = frozenset(),
) -> ReadinessResult:
    return ReadinessResult(
        ok=error_type is None,
        error_type=error_type,
        returncode=returncode,
        duration_ms=int((time.perf_counter() - started) * 1000),
        capabilities=capabilities,
    )
