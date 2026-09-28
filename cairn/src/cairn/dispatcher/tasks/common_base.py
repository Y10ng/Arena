"""任务执行公共工具，负责命令运行、审计事件、artifact 和协议写回。"""

from __future__ import annotations

import logging
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import PurePosixPath

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.contracts import DeclaredArtifact, normalize_declared_artifact_kind
from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.server.models import EvidenceRef

PROCESS_COMMUNICATE_GRACE_SECONDS = 15
LOG_PREVIEW_LIMIT = 1200
# verbose 模式下不截断 worker 输出，便于调试时看到完整 stdout/stderr（如 pi 的 404 JSONL）。
_preview_verbose = False


def set_preview_verbose(enabled: bool) -> None:
    global _preview_verbose
    _preview_verbose = enabled


def preview(text: str, limit: int = LOG_PREVIEW_LIMIT) -> str:
    if _preview_verbose:
        # 调试模式返回完整原文，不做空白压缩与截断。
        return text
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."
PROCESS_ARTIFACT_MAX_BYTES = 2_000_000
PROCESS_ARTIFACT_HEAD_BYTES = 1_000_000
PROCESS_ARTIFACT_TAIL_BYTES = 1_000_000
GRAPH_SNAPSHOT_ROOT = "/tmp/cairn-prompts"
LOG = logging.getLogger(__name__)
VALIDATION_REASON_LIMIT = 200
LANGUAGE_WARNING_PREVIEW_LIMIT = 160
LANGUAGE_WARNING_MAX_COUNT = 8
LANGUAGE_WARNING_MIN_LATIN_CHARS = 16
LANGUAGE_WARNING_MAX_CHINESE_SHARE = 0.35


@dataclass(slots=True)
class ConcludeWriteResult:
    status: str
    fact_id: str | None = None


@dataclass(slots=True)
class ProcessArtifactRecord:
    """一次进程输出/声明 artifact 的写入结果和可解析证据引用。"""

    refs: list[EvidenceRef]
    write_failed: bool = False
    collect_failed: bool = False
    artifact_ids_by_handle: dict[str, str] = field(default_factory=dict)


def did_oom(result: ProcessResult) -> bool:
    """只有正向 cgroup/Docker 证据才能覆盖 137 的超时兼容语义。"""
    return not result.cancelled and result.oom_killed


def did_timeout(result: ProcessResult) -> bool:
    # 容器内 timeout 常返回 124，Python 侧强杀常见 137；有 OOM 证据时优先归类 OOM。
    return not result.cancelled and not result.oom_killed and (
        result.timed_out or result.returncode in (124, 137)
    )


def cancel_reason(result: ProcessResult, cancellation: TaskCancellation | None = None) -> str | None:
    if result.cancelled:
        return result.cancel_reason or "cancelled"
    if cancellation is not None:
        return cancellation.reason
    return None


def communicate_timeout(timeout_seconds: int, grace_seconds: int = PROCESS_COMMUNICATE_GRACE_SECONDS) -> int:
    return timeout_seconds + grace_seconds


def _artifact_id_from_response(data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    artifact_id = data.get("id")
    if not isinstance(artifact_id, str) or not artifact_id:
        return None
    return artifact_id


def _string_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _integer_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _compact_validation_text(value: object, *, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


__all__ = [name for name in globals() if not name.startswith("__")]
