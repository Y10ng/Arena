"""CandidateStore 的本地 durable outbox 与物理执行 fence。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import struct
import threading
from typing import Iterator, Mapping
import uuid

from cairn.dispatcher.workers.base import (
    CANDIDATE_MAX_BYTES,
    CANDIDATE_MAX_MANIFEST_BYTES,
    CANDIDATE_SPOOL_PROTOCOL,
    WorkerCandidateSpool,
)


_OUTBOX_MAGIC = b"CRNCOB01"
_FRAME_MAGIC = b"CRNCND01"
_OUTBOX_PROTOCOL = "cairn-candidate-capture-outbox-v1"
_FENCE_PROTOCOL = "cairn-candidate-execution-fence-v1"
TERMINATION_ATTEMPT_LIMIT = 3
_CAPTURE_SUFFIX = ".capture.bin"
_FENCE_SUFFIX = ".fence.json"
_SHA256_LENGTH = 64
_MAX_OUTBOX_METADATA_BYTES = 16 * 1024
_MAX_CAPTURE_FILE_BYTES = (
    16
    + _MAX_OUTBOX_METADATA_BYTES
    + 16
    + CANDIDATE_MAX_MANIFEST_BYTES
    + CANDIDATE_MAX_BYTES
    + 32
    + 32
)
_MAX_FENCE_FILE_BYTES = 64 * 1024
_CONTROL_PATH_RE = re.compile(r"^/tmp/cairn-exec-[0-9a-f]{32}\.json$")
_TEMPORARY_LINK_RE = re.compile(
    r"^\.(?P<target>[a-f0-9]{64}\.(?:capture\.bin|fence\.json))\."
    r"(?P<nonce>[a-f0-9]{32})\.tmp$"
)


class CandidateCaptureOutboxError(RuntimeError):
    """Candidate outbox 无法安全读写。"""


class CandidateCaptureOutboxConflict(CandidateCaptureOutboxError):
    """同一 delivery key 已绑定到不同 exact request。"""


def _canonical_json(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise CandidateCaptureOutboxError(
            "durable candidate JSON is not canonical"
        ) from exc


def _parse_object(raw: bytes) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CandidateCaptureOutboxError("duplicate JSON key in durable candidate state")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")
            ),
        )
    except (
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
        RecursionError,
    ) as exc:
        raise CandidateCaptureOutboxError("invalid durable candidate JSON") from exc
    if not isinstance(value, dict):
        raise CandidateCaptureOutboxError("durable candidate JSON must be an object")
    return value


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _delivery_key(
    producer_candidate_key: str,
    record_kind: str,
    outcome_sequence: int | None,
) -> str:
    if record_kind == "candidate" and outcome_sequence is None:
        return producer_candidate_key
    if (
        record_kind == "outcome"
        and isinstance(outcome_sequence, int)
        and not isinstance(outcome_sequence, bool)
        and outcome_sequence >= 1
    ):
        return f"{producer_candidate_key}:outcome:{outcome_sequence}"
    raise CandidateCaptureOutboxError("invalid candidate delivery identity")


@dataclass(frozen=True, slots=True)
class PendingCandidateCapture:
    """一条可按原始 manifest/raw 请求重放的 durable frame。"""

    project_id: str
    task_run_id: str
    phase: str
    session: str
    capture_id: str
    producer_candidate_key: str
    record_kind: str
    outcome_sequence: int | None
    manifest_bytes: bytes
    raw: bytes
    frame_bytes: bytes
    request_sha256: str
    raw_sha256: str | None
    frame_index: int = 0

    @property
    def delivery_key(self) -> str:
        return _delivery_key(
            self.producer_candidate_key,
            self.record_kind,
            self.outcome_sequence,
        )

    def __post_init__(self) -> None:
        text_values = (
            self.project_id,
            self.task_run_id,
            self.phase,
            self.session,
            self.capture_id,
            self.producer_candidate_key,
        )
        if any(not isinstance(value, str) or not value for value in text_values):
            raise CandidateCaptureOutboxError("candidate durable identity is incomplete")
        if (
            not isinstance(self.frame_index, int)
            or isinstance(self.frame_index, bool)
            or self.frame_index < 0
        ):
            raise CandidateCaptureOutboxError("invalid candidate frame index")
        if not _is_sha256(self.request_sha256):
            raise CandidateCaptureOutboxError("invalid candidate request digest")
        if self.raw_sha256 is not None and not _is_sha256(self.raw_sha256):
            raise CandidateCaptureOutboxError("invalid candidate raw digest")
        if sha256(self.manifest_bytes + self.raw).hexdigest() != self.request_sha256:
            raise CandidateCaptureOutboxError("candidate request digest mismatch")
        if self.record_kind == "candidate":
            if self.outcome_sequence is not None or self.raw_sha256 != sha256(self.raw).hexdigest():
                raise CandidateCaptureOutboxError("invalid candidate raw identity")
        elif self.record_kind == "outcome":
            if (
                not isinstance(self.outcome_sequence, int)
                or isinstance(self.outcome_sequence, bool)
                or self.outcome_sequence < 1
                or self.raw
                or self.raw_sha256 is not None
            ):
                raise CandidateCaptureOutboxError("invalid candidate outcome identity")
        else:
            raise CandidateCaptureOutboxError("invalid candidate record kind")
        expected_prefix = (
            _FRAME_MAGIC
            + struct.pack(">II", len(self.manifest_bytes), len(self.raw))
            + self.manifest_bytes
            + self.raw
        )
        if (
            self.frame_bytes != expected_prefix + sha256(expected_prefix).digest()
            or _canonical_json(_parse_object(self.manifest_bytes)) != self.manifest_bytes
        ):
            raise CandidateCaptureOutboxError("candidate frame bytes are inconsistent")


@dataclass(frozen=True, slots=True)
class CandidateExecutionFence:
    """模型启动前持久化的 metadata-only 物理执行标识。"""

    protocol: str
    owner_id: str
    project_id: str
    task_run_id: str
    container_name: str
    phase: str
    session: str
    spool_protocol: str
    spool_path: str
    capture_id: str
    max_candidates: int
    max_candidate_bytes: int
    max_manifest_bytes: int
    max_spool_bytes: int
    binding_sha256: str
    result_schema_sha256: str
    created_at: str
    docker_daemon_id: str | None = None
    start_attempted: bool | None = None
    container_id: str | None = None
    exec_id: str | None = None
    control_path: str | None = None
    terminal_confirmed: bool = False
    termination_attempted: bool = False
    termination_attempts: int = 0
    terminal_proof_version: int = 0
    recovery_status: str | None = None
    recovery_reason: str | None = None
    container_spool_recovered: bool | None = None
    recovery_exit_code: int | None = None

    @classmethod
    def create(
        cls,
        *,
        owner_id: str,
        project_id: str,
        task_run_id: str,
        container_name: str,
        phase: str,
        session: str,
        spool_path: str,
        capture_id: str,
        max_candidates: int,
        max_candidate_bytes: int,
        max_manifest_bytes: int,
        max_spool_bytes: int,
        binding_sha256: str,
        result_schema_sha256: str,
        docker_daemon_id: str,
    ) -> CandidateExecutionFence:
        return cls(
            protocol=_FENCE_PROTOCOL,
            owner_id=owner_id,
            project_id=project_id,
            task_run_id=task_run_id,
            container_name=container_name,
            phase=phase,
            session=session,
            spool_protocol=CANDIDATE_SPOOL_PROTOCOL,
            spool_path=spool_path,
            capture_id=capture_id,
            max_candidates=max_candidates,
            max_candidate_bytes=max_candidate_bytes,
            max_manifest_bytes=max_manifest_bytes,
            max_spool_bytes=max_spool_bytes,
            binding_sha256=binding_sha256,
            result_schema_sha256=result_schema_sha256,
            created_at=datetime.now(UTC).isoformat(),
            docker_daemon_id=docker_daemon_id,
            start_attempted=False,
        )

    def bind_execution(
        self,
        *,
        container_id: str,
        exec_id: str,
        control_path: str,
    ) -> CandidateExecutionFence:
        """生成只增加精确 Docker 执行身份与终态凭据路径的 fence。"""
        if self.execution_bound:
            if (
                self.container_id == container_id
                and self.exec_id == exec_id
                and self.control_path == control_path
            ):
                return self
            raise CandidateCaptureOutboxConflict("candidate fence execution identity conflict")
        if any(
            value is not None
            for value in (self.container_id, self.exec_id, self.control_path)
        ):
            raise CandidateCaptureOutboxConflict(
                "candidate fence execution identity is incomplete"
            )
        return CandidateExecutionFence(
            **{
                **asdict(self),
                "container_id": container_id,
                "exec_id": exec_id,
                "control_path": control_path,
            }
        )

    def mark_start_attempted(self) -> CandidateExecutionFence:
        """在调用 Docker exec_start 前 durable 记录启动尝试边界。"""
        if not self.execution_bound or self.terminal_confirmed:
            raise CandidateCaptureOutboxConflict(
                "candidate fence cannot mark start attempt in current state"
            )
        if self.start_attempted is True:
            return self
        return CandidateExecutionFence(
            **{
                **asdict(self),
                "start_attempted": True,
            }
        )

    @property
    def termination_attempt_count(self) -> int:
        # 历史一次性标记表示可能已发出一次信号；不能把缺少计数解释成零次。
        return max(self.termination_attempts, int(self.termination_attempted))

    def mark_termination_attempted(self) -> CandidateExecutionFence:
        """在物理身份预检通过后，先持久占用一次有界干预额度。"""
        if (
            not self.execution_bound
            or self.start_attempted is not True
            or self.terminal_confirmed
            or self.termination_attempt_count >= TERMINATION_ATTEMPT_LIMIT
        ):
            raise CandidateCaptureOutboxConflict("candidate fence cannot attempt termination")
        return CandidateExecutionFence(**{
            **asdict(self),
            "termination_attempted": True,
            "termination_attempts": self.termination_attempt_count + 1,
        })

    def confirm_terminal(self) -> CandidateExecutionFence:
        """只在新后代排空证明或精确容器停机核验后保存终态。"""
        if not self.execution_bound:
            raise CandidateCaptureOutboxConflict(
                "candidate fence cannot confirm an unbound execution"
            )
        if self.terminal_confirmed and self.terminal_proof_version == 2:
            return self
        return CandidateExecutionFence(
            **{
                **asdict(self),
                "terminal_confirmed": True,
                "terminal_proof_version": 2,
            }
        )

    def mark_recovered(
        self,
        *,
        status: str,
        reason: str,
        container_spool_recovered: bool,
        exit_code: int | None = None,
    ) -> CandidateExecutionFence:
        """在原执行终止且候选已取回或确实不可取回后保存持久化证明。"""
        if self.recovery_status is not None:
            if (
                self.recovery_status == status
                and self.recovery_reason == reason
                and self.container_spool_recovered is container_spool_recovered
                and self.recovery_exit_code == exit_code
            ):
                return self
            raise CandidateCaptureOutboxConflict("candidate recovery resolution conflict")
        return CandidateExecutionFence(
            **{
                **asdict(self),
                "recovery_status": status,
                "recovery_reason": reason,
                "container_spool_recovered": container_spool_recovered,
                "recovery_exit_code": exit_code,
            }
        )

    @property
    def execution_bound(self) -> bool:
        return (
            self.container_id is not None
            and self.exec_id is not None
            and self.control_path is not None
        )

    def __post_init__(self) -> None:
        text_values = (
            self.owner_id,
            self.project_id,
            self.task_run_id,
            self.container_name,
            self.phase,
            self.session,
            self.spool_path,
            self.capture_id,
            self.created_at,
        )
        limits = (
            self.max_candidates,
            self.max_candidate_bytes,
            self.max_manifest_bytes,
            self.max_spool_bytes,
        )
        if (
            self.protocol != _FENCE_PROTOCOL
            or self.spool_protocol != CANDIDATE_SPOOL_PROTOCOL
            or any(not isinstance(value, str) or not value for value in text_values)
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
                for value in limits
            )
            or not isinstance(self.terminal_confirmed, bool)
            or not isinstance(self.termination_attempted, bool)
            or not _is_nonnegative_integer(self.termination_attempts)
            or self.termination_attempts > TERMINATION_ATTEMPT_LIMIT
            or (self.termination_attempts > 0 and not self.termination_attempted)
            or self.terminal_proof_version not in {0, 2}
            or type(self.terminal_proof_version) is not int
            or (self.terminal_proof_version == 2 and not self.terminal_confirmed)
            or (self.termination_attempted and (not self.execution_bound or self.start_attempted is not True))
            or (
                self.recovery_status is not None
                and (
                    self.recovery_status not in {"stopped", "gone"}
                    or not isinstance(self.recovery_reason, str)
                    or not 0 < len(self.recovery_reason) <= 256
                    or not isinstance(self.container_spool_recovered, bool)
                    or (
                        self.recovery_exit_code is not None
                        and (
                            type(self.recovery_exit_code) is not int
                            or self.recovery_exit_code < 0
                            or self.recovery_status != "stopped"
                        )
                    )
                    or (
                        self.recovery_status == "gone"
                        and (not self.execution_bound or self.container_spool_recovered)
                    )
                    or (
                        self.recovery_status == "stopped"
                        and self.start_attempted is not False
                        and not self.terminal_confirmed
                    )
                    or (
                        self.recovery_status == "stopped"
                        and self.start_attempted is False
                        and self.container_spool_recovered
                    )
                )
            )
            or (
                self.recovery_status is None
                and (
                    self.recovery_reason is not None
                    or self.container_spool_recovered is not None
                    or self.recovery_exit_code is not None
                )
            )
            or (
                self.start_attempted is not None
                and not isinstance(self.start_attempted, bool)
            )
            or (
                self.docker_daemon_id is not None
                and (
                    not isinstance(self.docker_daemon_id, str)
                    or not self.docker_daemon_id
                    or self.docker_daemon_id != self.docker_daemon_id.strip()
                    or len(self.docker_daemon_id) > 256
                )
            )
            or len(
                {
                    value is None
                    for value in (
                        self.container_id,
                        self.exec_id,
                        self.control_path,
                    )
                }
            )
            != 1
            or (self.terminal_confirmed and not self.execution_bound)
            or (self.start_attempted is True and not self.execution_bound)
            or (self.terminal_confirmed and self.start_attempted is False)
            or (
                self.execution_bound
                and (
                    not isinstance(self.container_id, str)
                    or not self.container_id
                    or not isinstance(self.exec_id, str)
                    or not self.exec_id
                    or not isinstance(self.control_path, str)
                    or _CONTROL_PATH_RE.fullmatch(self.control_path) is None
                )
            )
            or not _is_sha256(self.binding_sha256)
            or not _is_sha256(self.result_schema_sha256)
        ):
            raise CandidateCaptureOutboxError("invalid candidate execution fence")

    def candidate_spool(self) -> WorkerCandidateSpool:
        """从 durable fence 恢复摄取所需的完整 spool authority。"""
        return WorkerCandidateSpool(
            path=self.spool_path,
            capture_id=self.capture_id,
            protocol=self.spool_protocol,
            max_candidates=self.max_candidates,
            max_candidate_bytes=self.max_candidate_bytes,
            max_manifest_bytes=self.max_manifest_bytes,
            max_spool_bytes=self.max_spool_bytes,
            binding_sha256=self.binding_sha256,
            result_schema_sha256=self.result_schema_sha256,
        )


class CandidateCaptureOutbox:
    """用 no-replace、fsync 与 compare-delete 保存 exact candidate frames。"""

    def __init__(self, root: str | Path, *, owner_id: str) -> None:
        self.root = Path(root)
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("candidate outbox requires a string owner_id")
        self.owner_id = owner_id
        self._thread_lock = threading.RLock()
        self._ensure_root()
        self._lock_path = self.root / ".lock"
        self._ensure_lock_file()
        with self._locked():
            self._reconcile_temporary_links()

    def persist(self, entry: PendingCandidateCapture) -> None:
        path = self._capture_path(entry.delivery_key)
        encoded = self._encode_entry(entry)
        with self._locked():
            self._persist_no_replace(path, encoded, conflict_message="candidate delivery conflict")

    def entries(self) -> list[PendingCandidateCapture]:
        with self._locked():
            paths = sorted(self.root.glob(f"*{_CAPTURE_SUFFIX}"))
            entries = [self._decode_entry(self._read_private_file(path)) for path in paths]
            return sorted(
                entries,
                key=lambda entry: (entry.capture_id, entry.frame_index),
            )

    def remove(self, entry: PendingCandidateCapture) -> bool:
        path = self._capture_path(entry.delivery_key)
        expected = self._encode_entry(entry)
        with self._locked():
            if not path.exists():
                return False
            current = self._read_private_file(path)
            if current != expected:
                raise CandidateCaptureOutboxConflict("candidate changed before removal")
            path.unlink()
            self._fsync_root()
            return True

    def persist_fence(self, fence: CandidateExecutionFence) -> None:
        if fence.owner_id != self.owner_id:
            raise CandidateCaptureOutboxError("candidate fence owner mismatch")
        path = self._fence_path(fence.capture_id)
        encoded = self._encode_fence(fence)
        with self._locked():
            self._persist_no_replace(path, encoded, conflict_message="candidate fence conflict")

    def fences(self) -> list[CandidateExecutionFence]:
        with self._locked():
            paths = sorted(self.root.glob(f"*{_FENCE_SUFFIX}"))
            fences: dict[str, CandidateExecutionFence] = {}
            for path in paths:
                fence = self._decode_fence(self._read_private_file(path))
                current = fences.get(fence.capture_id)
                if current is not None and current != fence:
                    raise CandidateCaptureOutboxError(
                        "conflicting candidate fence durable state"
                    )
                fences[fence.capture_id] = fence
            return [fences[capture_id] for capture_id in sorted(fences)]

    def replace_fence(
        self,
        expected: CandidateExecutionFence,
        replacement: CandidateExecutionFence,
    ) -> None:
        """以 exact compare-and-replace 原子推进 fence。"""
        if expected.owner_id != self.owner_id or replacement.owner_id != self.owner_id:
            raise CandidateCaptureOutboxError("candidate fence owner mismatch")
        if expected.capture_id != replacement.capture_id:
            raise CandidateCaptureOutboxConflict("candidate fence identity changed")
        allowed = False
        if (
            not expected.execution_bound
            and replacement.execution_bound
            and replacement.container_id is not None
            and replacement.exec_id is not None
            and replacement.control_path is not None
            and not replacement.terminal_confirmed
        ):
            allowed = replacement == expected.bind_execution(
                container_id=replacement.container_id,
                exec_id=replacement.exec_id,
                control_path=replacement.control_path,
            )
        elif (
            expected.execution_bound
            and not expected.terminal_confirmed
            and expected.start_attempted is not True
            and replacement.start_attempted is True
            and not replacement.terminal_confirmed
        ):
            allowed = replacement == expected.mark_start_attempted()
        elif (
            expected.execution_bound
            and not expected.terminal_confirmed
            and replacement.termination_attempt_count > expected.termination_attempt_count
        ):
            allowed = replacement == expected.mark_termination_attempted()
        elif (
            expected.execution_bound
            and expected.terminal_proof_version == 0
            and replacement.terminal_confirmed
        ):
            allowed = replacement == expected.confirm_terminal()
        elif (
            expected.recovery_status is None
            and replacement.recovery_status is not None
            and replacement.recovery_reason is not None
            and replacement.container_spool_recovered is not None
        ):
            allowed = replacement == expected.mark_recovered(
                status=replacement.recovery_status,
                reason=replacement.recovery_reason,
                container_spool_recovered=replacement.container_spool_recovered,
                exit_code=replacement.recovery_exit_code,
            )
        elif replacement == expected:
            allowed = True
        if not allowed:
            raise CandidateCaptureOutboxConflict(
                "candidate fence replacement is not a valid forward transition"
            )
        path = self._fence_path(expected.capture_id)
        expected_bytes = self._encode_fence(expected)
        replacement_bytes = self._encode_fence(replacement)
        with self._locked():
            current = self._read_private_file(path) if path.exists() else None
            if current == replacement_bytes:
                self._fsync_root()
                return
            if current != expected_bytes:
                raise CandidateCaptureOutboxConflict("candidate fence changed before replacement")
            self._replace_private_file(path, replacement_bytes)

    def remove_fence(self, fence: CandidateExecutionFence) -> bool:
        path = self._fence_path(fence.capture_id)
        release_path = self._fence_release_path(fence.capture_id)
        expected = self._encode_fence(fence)
        with self._locked():
            try:
                current = self._read_private_file(path) if path.exists() else None
                release = (
                    self._read_private_file(release_path)
                    if release_path.exists()
                    else None
                )
                if current is None and release is None:
                    return False
                if current not in {None, expected} or release not in {None, expected}:
                    raise CandidateCaptureOutboxConflict(
                        "candidate fence changed before removal"
                    )
                if release is None:
                    # 先 durable 保存 exact release witness，保证 unlink 未同步时仍有 blocker。
                    self._persist_no_replace(
                        release_path,
                        expected,
                        conflict_message="candidate fence release conflict",
                    )
                if path.exists():
                    path.unlink()
                # 即使上次已 unlink，也必须重试目录同步后才能清除 witness。
                self._fsync_root()
                release_path.unlink()
                try:
                    self._fsync_root()
                except OSError:
                    # witness 删除未同步时恢复它，保留下一轮清理所需的执行身份。
                    self._persist_no_replace(
                        release_path,
                        expected,
                        conflict_message="candidate fence release conflict",
                    )
                    raise
                return True
            except CandidateCaptureOutboxError:
                raise
            except OSError as exc:
                raise CandidateCaptureOutboxError(
                    "cannot remove candidate fence durable state"
                ) from exc

    def _capture_path(self, delivery_key: str) -> Path:
        digest = sha256(f"{self.owner_id}\0{delivery_key}".encode()).hexdigest()
        return self.root / f"{digest}{_CAPTURE_SUFFIX}"

    def _fence_path(self, capture_id: str) -> Path:
        digest = sha256(f"{self.owner_id}\0{capture_id}".encode()).hexdigest()
        return self.root / f"{digest}{_FENCE_SUFFIX}"

    def _fence_release_path(self, capture_id: str) -> Path:
        digest = sha256(
            f"{self.owner_id}\0{capture_id}\0release".encode()
        ).hexdigest()
        return self.root / f"{digest}{_FENCE_SUFFIX}"

    def _encode_entry(self, entry: PendingCandidateCapture) -> bytes:
        metadata = {
            "protocol": _OUTBOX_PROTOCOL,
            "owner_id": self.owner_id,
            "project_id": entry.project_id,
            "task_run_id": entry.task_run_id,
            "phase": entry.phase,
            "session": entry.session,
            "capture_id": entry.capture_id,
            "producer_candidate_key": entry.producer_candidate_key,
            "record_kind": entry.record_kind,
            "outcome_sequence": entry.outcome_sequence,
            "request_sha256": entry.request_sha256,
            "raw_sha256": entry.raw_sha256,
            "frame_index": entry.frame_index,
            "manifest_size_bytes": len(entry.manifest_bytes),
            "raw_size_bytes": len(entry.raw),
            "frame_size_bytes": len(entry.frame_bytes),
            "frame_sha256": sha256(entry.frame_bytes).hexdigest(),
        }
        metadata_bytes = _canonical_json(metadata)
        body = _OUTBOX_MAGIC + struct.pack(">II", len(metadata_bytes), len(entry.frame_bytes))
        body += metadata_bytes + entry.frame_bytes
        return body + sha256(body).digest()

    def _decode_entry(self, encoded: bytes) -> PendingCandidateCapture:
        if len(encoded) < 16 + 32 or encoded[:8] != _OUTBOX_MAGIC:
            raise CandidateCaptureOutboxError("invalid candidate outbox entry")
        metadata_length, frame_length = struct.unpack(">II", encoded[8:16])
        expected_size = 16 + metadata_length + frame_length + 32
        if (
            metadata_length <= 0
            or metadata_length > _MAX_OUTBOX_METADATA_BYTES
            or frame_length <= 0
            or len(encoded) != expected_size
            or sha256(encoded[:-32]).digest() != encoded[-32:]
        ):
            raise CandidateCaptureOutboxError("candidate outbox checksum mismatch")
        metadata_bytes = encoded[16 : 16 + metadata_length]
        metadata = _parse_object(metadata_bytes)
        if _canonical_json(metadata) != metadata_bytes:
            raise CandidateCaptureOutboxError("candidate outbox metadata is not canonical")
        frame = encoded[16 + metadata_length : -32]
        if len(frame) < 16 + 32 or frame[:8] != _FRAME_MAGIC:
            raise CandidateCaptureOutboxError("invalid candidate frame in outbox")
        manifest_length, raw_length = struct.unpack(">II", frame[8:16])
        if len(frame) != 16 + manifest_length + raw_length + 32:
            raise CandidateCaptureOutboxError("candidate frame length mismatch")
        manifest_bytes = frame[16 : 16 + manifest_length]
        raw = frame[16 + manifest_length : -32]
        required = {
            "protocol",
            "owner_id",
            "project_id",
            "task_run_id",
            "phase",
            "session",
            "capture_id",
            "producer_candidate_key",
            "record_kind",
            "outcome_sequence",
            "request_sha256",
            "raw_sha256",
            "frame_index",
            "manifest_size_bytes",
            "raw_size_bytes",
            "frame_size_bytes",
            "frame_sha256",
        }
        if set(metadata) != required or metadata["protocol"] != _OUTBOX_PROTOCOL:
            raise CandidateCaptureOutboxError("invalid candidate outbox metadata")
        if metadata["owner_id"] != self.owner_id:
            raise CandidateCaptureOutboxError("candidate outbox owner mismatch")
        text_fields = (
            "project_id",
            "task_run_id",
            "phase",
            "session",
            "capture_id",
            "producer_candidate_key",
            "record_kind",
            "request_sha256",
            "frame_sha256",
        )
        integer_fields = (
            "frame_index",
            "manifest_size_bytes",
            "raw_size_bytes",
            "frame_size_bytes",
        )
        if (
            any(
                not isinstance(metadata[field], str) or not metadata[field]
                for field in text_fields
            )
            or any(not _is_nonnegative_integer(metadata[field]) for field in integer_fields)
            or (
                metadata["outcome_sequence"] is not None
                and (
                    not isinstance(metadata["outcome_sequence"], int)
                    or isinstance(metadata["outcome_sequence"], bool)
                )
            )
            or (
                metadata["raw_sha256"] is not None
                and not isinstance(metadata["raw_sha256"], str)
            )
            or metadata["manifest_size_bytes"] != len(manifest_bytes)
            or metadata["raw_size_bytes"] != len(raw)
            or metadata["frame_size_bytes"] != len(frame)
            or metadata["frame_sha256"] != sha256(frame).hexdigest()
        ):
            raise CandidateCaptureOutboxError("candidate outbox metadata mismatch")
        try:
            return PendingCandidateCapture(
                project_id=metadata["project_id"],
                task_run_id=metadata["task_run_id"],
                phase=metadata["phase"],
                session=metadata["session"],
                capture_id=metadata["capture_id"],
                producer_candidate_key=metadata["producer_candidate_key"],
                record_kind=metadata["record_kind"],
                outcome_sequence=metadata["outcome_sequence"],  # type: ignore[arg-type]
                manifest_bytes=manifest_bytes,
                raw=raw,
                frame_bytes=frame,
                request_sha256=metadata["request_sha256"],
                raw_sha256=metadata["raw_sha256"],  # type: ignore[arg-type]
                frame_index=metadata["frame_index"],  # type: ignore[arg-type]
            )
        except (CandidateCaptureOutboxError, TypeError, ValueError) as exc:
            raise CandidateCaptureOutboxError("invalid candidate outbox entry fields") from exc

    def _encode_fence(self, fence: CandidateExecutionFence) -> bytes:
        payload = asdict(fence)
        if fence.docker_daemon_id is None:
            # 旧 fence 保持原始 canonical bytes；缺少 daemon 身份时只能保守判为 unknown。
            payload.pop("docker_daemon_id")
        if fence.start_attempted is None:
            # 历史 bound fence 未记录启动边界，恢复时不能把缺失字段解释为未启动。
            payload.pop("start_attempted")
        if not fence.termination_attempted:
            # 旧 fence 的 exact canonical bytes 保持不变。
            payload.pop("termination_attempted")
        if fence.termination_attempts == 0:
            payload.pop("termination_attempts")
        if fence.terminal_proof_version == 0:
            payload.pop("terminal_proof_version")
        if fence.recovery_status is None:
            # 旧 fence 的原始 canonical bytes 必须维持稳定，供 exact compare-and-replace 使用。
            payload.pop("recovery_status")
            payload.pop("recovery_reason")
            payload.pop("container_spool_recovered")
            payload.pop("recovery_exit_code")
        payload_bytes = _canonical_json(payload)
        return _canonical_json(
            {
                "payload": payload,
                "payload_sha256": sha256(payload_bytes).hexdigest(),
            }
        )

    def _decode_fence(self, encoded: bytes) -> CandidateExecutionFence:
        envelope = _parse_object(encoded)
        if _canonical_json(envelope) != encoded or set(envelope) != {"payload", "payload_sha256"}:
            raise CandidateCaptureOutboxError("invalid candidate fence envelope")
        payload = envelope["payload"]
        if not isinstance(payload, dict):
            raise CandidateCaptureOutboxError("invalid candidate fence payload")
        payload_bytes = _canonical_json(payload)
        if envelope["payload_sha256"] != sha256(payload_bytes).hexdigest():
            raise CandidateCaptureOutboxError("candidate fence checksum mismatch")
        try:
            fence = CandidateExecutionFence(**payload)  # type: ignore[arg-type]
        except (CandidateCaptureOutboxError, TypeError, ValueError) as exc:
            raise CandidateCaptureOutboxError("invalid candidate fence fields") from exc
        if fence.owner_id != self.owner_id:
            raise CandidateCaptureOutboxError("candidate fence owner mismatch")
        return fence

    def _ensure_root(self) -> None:
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            status = os.lstat(self.root)
            if (
                not stat.S_ISDIR(status.st_mode)
                or status.st_uid != os.getuid()
                or stat.S_IMODE(status.st_mode) != 0o700
            ):
                raise CandidateCaptureOutboxError(
                    "candidate outbox root must be owner-only"
                )
            # 每次初始化都同步父目录，让首次同步失败后的重试仍能证明根目录 durable。
            self._fsync_directory(self.root.parent)
        except CandidateCaptureOutboxError:
            raise
        except OSError as exc:
            raise CandidateCaptureOutboxError("cannot create candidate outbox") from exc

    def _ensure_lock_file(self) -> None:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self._lock_path, flags, 0o600)
        except OSError as exc:
            raise CandidateCaptureOutboxError("cannot open candidate outbox lock") from exc
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != os.getuid()
                or stat.S_IMODE(status.st_mode) != 0o600
            ):
                raise CandidateCaptureOutboxError("candidate outbox lock must be owner-only")
        finally:
            os.close(descriptor)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            flags = os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self._lock_path, flags)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _reconcile_temporary_links(self) -> None:
        """恢复 hard-link 安装窗口，并删除尚未发布的完整临时文件。"""
        changed = False
        for temporary in sorted(self.root.glob(".*.tmp")):
            match = _TEMPORARY_LINK_RE.fullmatch(temporary.name)
            if match is None:
                raise CandidateCaptureOutboxError(
                    "unknown candidate durable temporary state"
                )
            temporary_status = os.lstat(temporary)
            if (
                not stat.S_ISREG(temporary_status.st_mode)
                or temporary_status.st_uid != os.getuid()
                or stat.S_IMODE(temporary_status.st_mode) != 0o600
                or temporary_status.st_nlink not in {1, 2}
            ):
                raise CandidateCaptureOutboxError(
                    "candidate durable temporary state must be owner-only"
                )
            target = self.root / match.group("target")
            if target.exists():
                target_status = os.lstat(target)
                if (
                    not stat.S_ISREG(target_status.st_mode)
                    or target_status.st_uid != os.getuid()
                    or stat.S_IMODE(target_status.st_mode) != 0o600
                    or temporary_status.st_nlink != 2
                    or target_status.st_nlink != 2
                    or temporary_status.st_dev != target_status.st_dev
                    or temporary_status.st_ino != target_status.st_ino
                ):
                    raise CandidateCaptureOutboxError(
                        "candidate durable temporary link is inconsistent"
                    )
            elif temporary_status.st_nlink != 1:
                raise CandidateCaptureOutboxError(
                    "candidate durable temporary link is orphaned"
                )
            temporary.unlink()
            changed = True
        if changed:
            self._fsync_root()

    def _persist_no_replace(self, path: Path, encoded: bytes, *, conflict_message: str) -> None:
        if path.exists():
            if self._read_private_file(path) == encoded:
                self._fsync_root()
                return
            raise CandidateCaptureOutboxConflict(conflict_message)
        temporary = self.root / f".{path.name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise CandidateCaptureOutboxError("candidate durable write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                if self._read_private_file(path) != encoded:
                    raise CandidateCaptureOutboxConflict(conflict_message)
            # 先移除临时硬链接再同步目录，确保重启后目标文件仍满足 link-count=1。
            temporary.unlink()
            self._fsync_root()
        except CandidateCaptureOutboxError:
            raise
        except OSError as exc:
            raise CandidateCaptureOutboxError("cannot persist candidate durable state") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _replace_private_file(self, path: Path, encoded: bytes) -> None:
        temporary = self.root / f".{path.name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise CandidateCaptureOutboxError(
                        "candidate durable write made no progress"
                    )
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, path)
            self._fsync_root()
        except CandidateCaptureOutboxError:
            raise
        except OSError as exc:
            raise CandidateCaptureOutboxError(
                "cannot replace candidate durable state"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _read_private_file(self, path: Path) -> bytes:
        if path.name.endswith(_CAPTURE_SUFFIX):
            maximum_bytes = _MAX_CAPTURE_FILE_BYTES
        elif path.name.endswith(_FENCE_SUFFIX):
            maximum_bytes = _MAX_FENCE_FILE_BYTES
        else:
            raise CandidateCaptureOutboxError("unknown candidate durable state kind")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise CandidateCaptureOutboxError("cannot open candidate durable state") from exc
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != os.getuid()
                or stat.S_IMODE(status.st_mode) != 0o600
                or status.st_nlink != 1
            ):
                raise CandidateCaptureOutboxError("candidate durable state must be owner-only")
            if status.st_size > maximum_bytes:
                raise CandidateCaptureOutboxError("candidate durable state exceeds its byte limit")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum_bytes:
                    raise CandidateCaptureOutboxError(
                        "candidate durable state exceeds its byte limit"
                    )
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _fsync_root(self) -> None:
        self._fsync_directory(self.root)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
