"""pi 执行命令与临时输入文件。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re


EXECUTION_CHECKPOINT_PROTOCOL = "cairn-execution-event-v1"
EXECUTION_CHECKPOINT_SPOOL_ROOT = "/tmp/cairn-prompts"
EXECUTION_CHECKPOINT_MAX_EVENTS = 64
EXECUTION_CHECKPOINT_MAX_EVENT_BYTES = 16 * 1024
EXECUTION_CHECKPOINT_MAX_SPOOL_BYTES = 512 * 1024
CANDIDATE_SPOOL_PROTOCOL = "cairn-candidate-spool-v1"
CANDIDATE_FRAME_PROTOCOL = "cairn-candidate-frame-v1"
CANDIDATE_SPOOL_ROOT = "/tmp/cairn-prompts"
CANDIDATE_MAX_CANDIDATES = 16
CANDIDATE_MAX_BYTES = 2 * 1024 * 1024
CANDIDATE_MAX_MANIFEST_BYTES = 4 * 1024
CANDIDATE_MAX_SPOOL_BYTES = 34 * 1024 * 1024
CANDIDATE_MAX_OUTCOMES_PER_CANDIDATE = 64
CANDIDATE_MAX_ARTIFACTS = 16
CANDIDATE_MAX_FORBIDDEN_LITERALS = 32
CANDIDATE_MAX_FORBIDDEN_LITERAL_BYTES = 4 * 1024
REPORT_READONLY_INPUT_ROOT = "/opt/cairn/readonly-inputs"
_CHECKPOINT_SPOOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}\.jsonl$")
_CANDIDATE_CAPTURE_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_READONLY_INPUT_DIRECTORY_RE = re.compile(r"^[a-f0-9]{32}$")


@dataclass(frozen=True, slots=True)
class WorkerInputFile:
    """Worker 启动前写入容器、命令结束后清理的临时输入文件。"""

    path: str
    content: str
    owner: str = "kali"
    mode: int = 0o600
    allowed_root: str = "/tmp/cairn-prompts"
    readonly_directory: str | None = None

    def __post_init__(self) -> None:
        descriptor_error = (
            "invalid readonly worker input descriptor"
            if self.readonly_directory is not None
            else "invalid worker input descriptor"
        )
        if (
            not isinstance(self.path, str)
            or not self.path
            or not isinstance(self.content, str)
            or not isinstance(self.owner, str)
            or not self.owner
            or type(self.mode) is not int
            or not isinstance(self.allowed_root, str)
            or not self.allowed_root
            or (
                self.readonly_directory is not None
                and not isinstance(self.readonly_directory, str)
            )
        ):
            raise ValueError(descriptor_error)
        target = PurePosixPath(self.path)
        allowed_root = PurePosixPath(self.allowed_root)
        if (
            not target.is_absolute()
            or not allowed_root.is_absolute()
            or target.root != "/"
            or allowed_root.root != "/"
            or str(target) != self.path
            or str(allowed_root) != self.allowed_root
            or any(part in {"", ".", ".."} for part in target.parts[1:])
            or any(part in {"", ".", ".."} for part in allowed_root.parts[1:])
            or target == allowed_root
            or not target.is_relative_to(allowed_root)
            or "\0" in self.path
            or "\0" in self.allowed_root
        ):
            raise ValueError(descriptor_error)
        if self.readonly_directory is None:
            return
        directory = PurePosixPath(self.readonly_directory)
        root = PurePosixPath(REPORT_READONLY_INPUT_ROOT)
        if (
            not target.is_absolute()
            or not directory.is_absolute()
            or str(target) != self.path
            or str(directory) != self.readonly_directory
            or any(part in {"", ".", ".."} for part in target.parts[1:])
            or any(part in {"", ".", ".."} for part in directory.parts[1:])
            or directory.parent != root
            or not _READONLY_INPUT_DIRECTORY_RE.fullmatch(directory.name)
            or target.parent != directory
            or target.name in {"", ".", ".."}
            or self.owner != "root"
            or self.mode != 0o444
            or self.allowed_root != REPORT_READONLY_INPUT_ROOT
        ):
            raise ValueError("invalid readonly worker input descriptor")


@dataclass(frozen=True, slots=True)
class WorkerCandidateSpool:
    """一次物理 Pi 执行的 exact candidate binary spool 描述符。"""

    path: str
    capture_id: str
    protocol: str = CANDIDATE_SPOOL_PROTOCOL
    max_candidates: int = CANDIDATE_MAX_CANDIDATES
    max_candidate_bytes: int = CANDIDATE_MAX_BYTES
    max_manifest_bytes: int = CANDIDATE_MAX_MANIFEST_BYTES
    max_spool_bytes: int = CANDIDATE_MAX_SPOOL_BYTES
    binding_sha256: str | None = None
    result_schema_sha256: str | None = None

    def __post_init__(self) -> None:
        target = PurePosixPath(self.path)
        expected_name = f"pi-candidates-{self.capture_id}.bin"
        if (
            self.protocol != CANDIDATE_SPOOL_PROTOCOL
            or not _CANDIDATE_CAPTURE_ID_RE.fullmatch(self.capture_id)
            or not target.is_absolute()
            or target.parent != PurePosixPath(CANDIDATE_SPOOL_ROOT)
            or target.name != expected_name
        ):
            raise ValueError("invalid candidate spool descriptor")
        limits = (
            (self.max_candidates, CANDIDATE_MAX_CANDIDATES),
            (self.max_candidate_bytes, CANDIDATE_MAX_BYTES),
            (self.max_manifest_bytes, CANDIDATE_MAX_MANIFEST_BYTES),
            (self.max_spool_bytes, CANDIDATE_MAX_SPOOL_BYTES),
        )
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            or value > maximum
            for value, maximum in limits
        ):
            raise ValueError("candidate spool limits exceed dispatcher hard limits")
        digests = (self.binding_sha256, self.result_schema_sha256)
        if any(digest is not None for digest in digests) and any(
            digest is None or not re.fullmatch(r"[a-f0-9]{64}", digest)
            for digest in digests
        ):
            raise ValueError("candidate spool authority digests must be paired SHA256 values")


@dataclass(frozen=True, slots=True)
class WorkerCheckpointSpool:
    """一次 Worker 进程的本地 checkpoint spool 描述符。"""

    path: str
    protocol: str = EXECUTION_CHECKPOINT_PROTOCOL
    max_events: int = EXECUTION_CHECKPOINT_MAX_EVENTS
    max_event_bytes: int = EXECUTION_CHECKPOINT_MAX_EVENT_BYTES
    max_spool_bytes: int = EXECUTION_CHECKPOINT_MAX_SPOOL_BYTES

    def __post_init__(self) -> None:
        target = PurePosixPath(self.path)
        root = PurePosixPath(EXECUTION_CHECKPOINT_SPOOL_ROOT)
        if (
            self.protocol != EXECUTION_CHECKPOINT_PROTOCOL
            or not target.is_absolute()
            or target.parent != root
            or not _CHECKPOINT_SPOOL_NAME_RE.fullmatch(target.name)
        ):
            raise ValueError("invalid execution checkpoint spool descriptor")
        limits = (
            (self.max_events, EXECUTION_CHECKPOINT_MAX_EVENTS),
            (self.max_event_bytes, EXECUTION_CHECKPOINT_MAX_EVENT_BYTES),
            (self.max_spool_bytes, EXECUTION_CHECKPOINT_MAX_SPOOL_BYTES),
        )
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            or value > maximum
            for value, maximum in limits
        ):
            raise ValueError("execution checkpoint spool limits exceed dispatcher hard limits")


@dataclass(slots=True)
class DriverResult:
    """执行命令、可复用 session 及需要临时物化的输入文件。"""

    argv: list[str]
    session: str | None = None
    input_files: tuple[WorkerInputFile, ...] = ()
    checkpoint_spool: WorkerCheckpointSpool | None = None
    candidate_spool: WorkerCandidateSpool | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.argv, list)
            or not self.argv
            or any(not isinstance(item, str) or not item for item in self.argv)
        ):
            raise ValueError("driver result requires non-empty string argv")
