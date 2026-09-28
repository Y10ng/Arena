"""严格报告 finish 的本地 durable outbox，不持久化明文 claim token。"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from threading import RLock
from typing import Iterator, Literal, TypeAlias
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.server.models import AssessmentReportTaskRunResult
from cairn.server.schemas.common import TaskRunErrorType


OUTBOX_PROTOCOL = "cairn-assessment-report-finish-outbox-v1"
EXECUTION_PROTOCOL = "cairn-assessment-report-execution-outbox-v1"
OUTPUT_PROTOCOL = "cairn-assessment-report-output-outbox-v1"
FINISH_PROTOCOL = "cairn-assessment-report-finish-v1"
MAX_FINISH_ENTRY_BYTES = 64 * 1024
MAX_REPORT_MARKDOWN_BYTES = 2 * 1024 * 1024
# JSON control character 最多扩展为六字节，并额外预留 envelope 空间。
MAX_ENTRY_BYTES = 6 * MAX_REPORT_MARKDOWN_BYTES + MAX_FINISH_ENTRY_BYTES
REPORT_FINISH_FALLBACK_CODES = frozenset(
    {
        "assessment_report_artifact_invalid",
        "assessment_report_snapshot_artifact_invalid",
    }
)


def canonical_json(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def finish_request_material(
    *,
    project_id: str,
    authenticated_dispatcher_id: str,
    worker: str,
    claim_sha256: str,
    generation_token: str,
    task_run_id: str,
    status: str,
    artifact_id: str | None,
    artifact_sha256: str | None,
    input_snapshot_artifact_id: str | None,
    input_snapshot_sha256: str | None,
    task_run_result: dict[str, object],
) -> dict[str, object]:
    return {
        "protocol": FINISH_PROTOCOL,
        "project_id": project_id,
        "authenticated_dispatcher_id": authenticated_dispatcher_id,
        "worker": worker,
        "claim_sha256": claim_sha256,
        "generation_token": generation_token,
        "task_run_id": task_run_id,
        "status": status,
        "artifact_id": artifact_id,
        "artifact_sha256": artifact_sha256,
        "input_snapshot_artifact_id": input_snapshot_artifact_id,
        "input_snapshot_sha256": input_snapshot_sha256,
        "task_run_result": task_run_result,
    }


def finish_request_sha256(**kwargs: object) -> str:
    material = finish_request_material(**kwargs)  # type: ignore[arg-type]
    return sha256(canonical_json(material).encode("utf-8")).hexdigest()


class _ReportJournalIdentity(BaseModel):
    """报告执行各阶段共享的不可变 Server identity。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    project_id: str = Field(min_length=1, max_length=128)
    authenticated_dispatcher_id: str = Field(min_length=1, max_length=128)
    worker: str = Field(min_length=1, max_length=128)
    claim_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    generation_token: str = Field(min_length=1, max_length=256)
    task_run_id: str = Field(min_length=1, max_length=128)
    input_snapshot_artifact_id: str = Field(min_length=1, max_length=128)
    input_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator(
        "project_id",
        "authenticated_dispatcher_id",
        "worker",
        "generation_token",
        "task_run_id",
        "input_snapshot_artifact_id",
    )
    @classmethod
    def validate_identity_identifier(cls, value: str) -> str:
        if value != value.strip() or "\0" in value:
            raise ValueError("identifier must be canonical")
        return value


def _journal_identity_material(entry: _ReportJournalIdentity) -> dict[str, object]:
    return {
        "project_id": entry.project_id,
        "authenticated_dispatcher_id": entry.authenticated_dispatcher_id,
        "worker": entry.worker,
        "claim_sha256": entry.claim_sha256,
        "generation_token": entry.generation_token,
        "task_run_id": entry.task_run_id,
        "input_snapshot_artifact_id": entry.input_snapshot_artifact_id,
        "input_snapshot_sha256": entry.input_snapshot_sha256,
    }


def _journal_sha256(material: dict[str, object]) -> str:
    return sha256(canonical_json(material).encode("utf-8")).hexdigest()


class PendingReportExecution(_ReportJournalIdentity):
    """模型执行前写入的 fence；缺少后续阶段时禁止自动重跑模型。"""

    protocol: Literal["cairn-assessment-report-execution-outbox-v1"] = (
        EXECUTION_PROTOCOL
    )
    stage: Literal["execution_pending"] = "execution_pending"
    entry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_entry_identity(self) -> "PendingReportExecution":
        expected = _journal_sha256(
            {
                "protocol": self.protocol,
                "stage": self.stage,
                **_journal_identity_material(self),
            }
        )
        if self.entry_sha256 != expected:
            raise ValueError("entry_sha256 does not match report execution")
        return self


class ReportOutputMetadata(BaseModel):
    """已冻结 Markdown Artifact 请求允许持久化的完整 metadata。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    source: Literal["report_task"] = "report_task"
    format: Literal["markdown"] = "markdown"
    report_generation_token: str | None = Field(default=None, max_length=256)
    report_template_key: str | None = Field(default=None, max_length=256)
    report_template_version: int | None = None
    report_template_digest_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    report_policy_version: int | None = None
    report_execution_plan_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    report_prompt_digest_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    input_snapshot_artifact_id: str | None = Field(
        default=None,
        max_length=128,
    )
    input_snapshot_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    completeness_rule_version: int | None = None

    @field_validator(
        "report_generation_token",
        "report_template_key",
        "input_snapshot_artifact_id",
    )
    @classmethod
    def validate_metadata_identifier(cls, value: str | None) -> str | None:
        if value is not None and (
            not value or value != value.strip() or "\0" in value
        ):
            raise ValueError("metadata identifier must be canonical")
        return value


class PendingReportOutput(_ReportJournalIdentity):
    """已验证并渲染的精确 Markdown；恢复仅重放该 Artifact 请求。"""

    protocol: Literal["cairn-assessment-report-output-outbox-v1"] = (
        OUTPUT_PROTOCOL
    )
    stage: Literal["validated_output"] = "validated_output"
    entry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["report"] = "report"
    category: Literal["deliverable"] = "deliverable"
    filename: Literal["vulnerability-report.md"] = "vulnerability-report.md"
    content: str = Field(min_length=1)
    metadata: ReportOutputMetadata

    @model_validator(mode="after")
    def validate_output_identity(self) -> "PendingReportOutput":
        try:
            content_bytes = self.content.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise ValueError("report output is not valid UTF-8") from None
        if len(content_bytes) > MAX_REPORT_MARKDOWN_BYTES:
            raise ValueError("report output exceeds durable size limit")
        if sha256(content_bytes).hexdigest() != self.artifact_sha256:
            raise ValueError("artifact_sha256 does not match report output")
        if (
            self.metadata.report_generation_token != self.generation_token
            or self.metadata.input_snapshot_artifact_id
            != self.input_snapshot_artifact_id
            or self.metadata.input_snapshot_sha256
            != self.input_snapshot_sha256
        ):
            raise ValueError("report output metadata changed immutable identity")
        expected = _journal_sha256(
            {
                "protocol": self.protocol,
                "stage": self.stage,
                **_journal_identity_material(self),
                "artifact_sha256": self.artifact_sha256,
                "kind": self.kind,
                "category": self.category,
                "filename": self.filename,
                "content": self.content,
                "metadata": self.metadata.model_dump(mode="json"),
            }
        )
        if self.entry_sha256 != expected:
            raise ValueError("entry_sha256 does not match report output")
        return self

    def replay(self, client: CairnClient) -> ApiResult:
        return client.create_artifact(
            self.project_id,
            task_run_id=self.task_run_id,
            kind=self.kind,
            content=self.content,
            filename=self.filename,
            metadata=self.metadata.model_dump(mode="json"),
            category=self.category,
        )


def pending_report_execution(
    *,
    project_id: str,
    authenticated_dispatcher_id: str,
    worker: str,
    claim_sha256: str,
    generation_token: str,
    task_run_id: str,
    input_snapshot_artifact_id: str,
    input_snapshot_sha256: str,
) -> PendingReportExecution:
    values = {
        "project_id": project_id,
        "authenticated_dispatcher_id": authenticated_dispatcher_id,
        "worker": worker,
        "claim_sha256": claim_sha256,
        "generation_token": generation_token,
        "task_run_id": task_run_id,
        "input_snapshot_artifact_id": input_snapshot_artifact_id,
        "input_snapshot_sha256": input_snapshot_sha256,
    }
    return PendingReportExecution(
        **values,
        entry_sha256=_journal_sha256(
            {
                "protocol": EXECUTION_PROTOCOL,
                "stage": "execution_pending",
                **values,
            }
        ),
    )


def pending_report_output(
    execution: PendingReportExecution,
    *,
    content: str,
    metadata: dict[str, object],
) -> PendingReportOutput:
    parsed_metadata = ReportOutputMetadata.model_validate(metadata)
    artifact_sha256 = sha256(content.encode("utf-8", errors="strict")).hexdigest()
    values = {
        **_journal_identity_material(execution),
        "artifact_sha256": artifact_sha256,
        "kind": "report",
        "category": "deliverable",
        "filename": "vulnerability-report.md",
        "content": content,
        "metadata": parsed_metadata.model_dump(mode="json"),
    }
    return PendingReportOutput(
        **values,
        entry_sha256=_journal_sha256(
            {
                "protocol": OUTPUT_PROTOCOL,
                "stage": "validated_output",
                **values,
            }
        ),
    )


class PendingReportFinish(BaseModel):
    """可跨 Dispatcher 重启重放、但不含 claim token 的精确请求。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    protocol: Literal["cairn-assessment-report-finish-outbox-v1"] = OUTBOX_PROTOCOL
    project_id: str = Field(min_length=1, max_length=128)
    authenticated_dispatcher_id: str = Field(min_length=1, max_length=128)
    worker: str = Field(min_length=1, max_length=128)
    claim_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    generation_token: str = Field(min_length=1, max_length=256)
    task_run_id: str = Field(min_length=1, max_length=128)
    status: Literal["ready", "failed"]
    artifact_id: str | None = Field(default=None, min_length=1, max_length=128)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    input_snapshot_artifact_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    input_snapshot_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    task_run_result: AssessmentReportTaskRunResult

    @field_validator(
        "project_id",
        "authenticated_dispatcher_id",
        "worker",
        "generation_token",
        "task_run_id",
        "artifact_id",
        "input_snapshot_artifact_id",
    )
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        if value is not None and (value != value.strip() or "\0" in value):
            raise ValueError("identifier must be canonical")
        return value

    @model_validator(mode="after")
    def validate_request_identity(self) -> "PendingReportFinish":
        expected = finish_request_sha256(
            project_id=self.project_id,
            authenticated_dispatcher_id=self.authenticated_dispatcher_id,
            worker=self.worker,
            claim_sha256=self.claim_sha256,
            generation_token=self.generation_token,
            task_run_id=self.task_run_id,
            status=self.status,
            artifact_id=self.artifact_id,
            artifact_sha256=self.artifact_sha256,
            input_snapshot_artifact_id=self.input_snapshot_artifact_id,
            input_snapshot_sha256=self.input_snapshot_sha256,
            task_run_result=self.task_run_result.model_dump(mode="json"),
        )
        if expected != self.request_sha256:
            raise ValueError("request_sha256 does not match finish request")
        if (self.artifact_id is None) != (self.artifact_sha256 is None):
            raise ValueError("artifact identity must be complete")
        if (self.input_snapshot_artifact_id is None) != (
            self.input_snapshot_sha256 is None
        ):
            raise ValueError("snapshot identity must be complete")
        if self.status == "ready":
            if self.artifact_id is None or self.input_snapshot_artifact_id is None:
                raise ValueError("ready finish requires artifact identities")
            if self.task_run_result.status != "succeeded":
                raise ValueError("ready finish requires succeeded task result")
        elif self.artifact_id is not None or self.task_run_result.status == "succeeded":
            raise ValueError("failed finish contains successful facts")
        return self

    def replay(self, client: CairnClient) -> ApiResult:
        return client.replay_assessment_report_finalization(
            self.project_id,
            self.worker,
            claim_sha256=self.claim_sha256,
            request_sha256=self.request_sha256,
            generation_token=self.generation_token,
            task_run_id=self.task_run_id,
            status=self.status,
            artifact_id=self.artifact_id,
            artifact_sha256=self.artifact_sha256,
            input_snapshot_artifact_id=self.input_snapshot_artifact_id,
            input_snapshot_sha256=self.input_snapshot_sha256,
            task_run_result=self.task_run_result.model_dump(mode="json"),
        )


ReportJournalEntry: TypeAlias = (
    PendingReportExecution | PendingReportOutput | PendingReportFinish
)


def validate_report_artifact_receipt(
    response: ApiResult,
    entry: PendingReportOutput,
) -> ApiResult:
    """2xx 只有返回 canonical Artifact identity 与 exact digest 才算 ACK。"""
    if not response.ok:
        return response
    data = response.data
    artifact_id = data.get("id") if isinstance(data, dict) else None
    if (
        isinstance(artifact_id, str)
        and artifact_id
        and len(artifact_id) <= 128
        and artifact_id == artifact_id.strip()
        and "\0" not in artifact_id
        and data.get("sha256") == entry.artifact_sha256
    ):
        return response
    return ApiResult(
        status_code=0,
        data={"detail": "assessment_report_artifact_receipt_invalid"},
    )


def recover_report_artifact_id(
    response: ApiResult,
    entry: PendingReportOutput,
) -> str | None:
    """从权威 Artifact 列表识别同一 TaskRun 已提交的 exact Markdown。"""
    if not response.ok or not isinstance(response.data, list):
        return None
    expected_metadata = entry.metadata.model_dump(mode="json")
    expected_size = len(entry.content.encode("utf-8"))
    matches: list[str] = []
    for item in response.data:
        if not isinstance(item, dict):
            continue
        artifact_id = item.get("id")
        metadata = item.get("metadata")
        if (
            not isinstance(artifact_id, str)
            or not artifact_id
            or len(artifact_id) > 128
            or artifact_id != artifact_id.strip()
            or "\0" in artifact_id
            or not isinstance(metadata, dict)
        ):
            continue
        expected_uri = (
            f"artifact://projects/{entry.project_id}/runs/{entry.task_run_id}/"
            f"{artifact_id}-{entry.filename}"
        )
        if (
            item.get("project_id") == entry.project_id
            and item.get("task_run_id") == entry.task_run_id
            and item.get("kind") == entry.kind
            and item.get("category") == entry.category
            and item.get("uri") == expected_uri
            and item.get("sha256") == entry.artifact_sha256
            and item.get("size") == expected_size
            and set(metadata) == {*expected_metadata, "runtime_context"}
            and isinstance(metadata.get("runtime_context"), dict)
            and all(
                metadata.get(key) == value
                for key, value in expected_metadata.items()
            )
        ):
            matches.append(artifact_id)
    return matches[-1] if matches else None


def ready_finish_for_output(
    entry: PendingReportOutput,
    *,
    artifact_id: str,
) -> PendingReportFinish:
    """把已 ACK 的 exact Artifact 原子推进为现有 token-free finish 请求。"""
    task_run_result = AssessmentReportTaskRunResult(
        status="succeeded",
        exit_code=0,
    )
    values = {
        "project_id": entry.project_id,
        "authenticated_dispatcher_id": entry.authenticated_dispatcher_id,
        "worker": entry.worker,
        "claim_sha256": entry.claim_sha256,
        "generation_token": entry.generation_token,
        "task_run_id": entry.task_run_id,
        "status": "ready",
        "artifact_id": artifact_id,
        "artifact_sha256": entry.artifact_sha256,
        "input_snapshot_artifact_id": entry.input_snapshot_artifact_id,
        "input_snapshot_sha256": entry.input_snapshot_sha256,
        "task_run_result": task_run_result.model_dump(mode="json"),
    }
    return PendingReportFinish(
        **values,
        request_sha256=finish_request_sha256(**values),
    )


def failed_finish_for_recovery(
    *,
    project_id: str,
    authenticated_dispatcher_id: str,
    worker: str,
    claim_sha256: str,
    generation_token: str,
    task_run_id: str,
    exit_code: int | None,
    error_type: TaskRunErrorType,
    error_message: str,
) -> PendingReportFinish:
    """构造不依赖易失 claim token 的确定性失败收据请求。"""
    task_run_result = AssessmentReportTaskRunResult(
        status="failed",
        exit_code=exit_code,
        error_type=error_type,
        error_message=error_message,
    )
    values = {
        "project_id": project_id,
        "authenticated_dispatcher_id": authenticated_dispatcher_id,
        "worker": worker,
        "claim_sha256": claim_sha256,
        "generation_token": generation_token,
        "task_run_id": task_run_id,
        "status": "failed",
        "artifact_id": None,
        "artifact_sha256": None,
        # 失败收尾无需重验输入 Artifact；Server 仍会校验 claim 与 TaskRun identity。
        "input_snapshot_artifact_id": None,
        "input_snapshot_sha256": None,
        "task_run_result": task_run_result.model_dump(mode="json"),
    }
    return PendingReportFinish(
        **values,
        request_sha256=finish_request_sha256(**values),
    )


def can_fallback_report_finish(safe_code: str | None) -> bool:
    """仅显式 Artifact identity 拒绝可安全降级为 failed 终态。"""
    return safe_code in REPORT_FINISH_FALLBACK_CODES


def failed_finish_fallback(
    entry: PendingReportFinish,
    *,
    safe_code: str | None,
) -> PendingReportFinish:
    """把 allowlisted Artifact 拒绝转换为同一 claim 的 failed 终态。"""
    if entry.status != "ready":
        raise ValueError("only a ready finish can transition to failed")
    if not can_fallback_report_finish(safe_code):
        raise ValueError("report finish rejection cannot transition to failed")
    snapshot_invalid = (
        safe_code == "assessment_report_snapshot_artifact_invalid"
    )
    task_run_result = AssessmentReportTaskRunResult(
        status="failed",
        exit_code=None,
        error_type="artifact_error",
        error_message="artifact_error",
        worker_failure=None,
    )
    values = {
        "project_id": entry.project_id,
        "authenticated_dispatcher_id": entry.authenticated_dispatcher_id,
        "worker": entry.worker,
        "claim_sha256": entry.claim_sha256,
        "generation_token": entry.generation_token,
        "task_run_id": entry.task_run_id,
        "status": "failed",
        "artifact_id": None,
        "artifact_sha256": None,
        "input_snapshot_artifact_id": (
            None if snapshot_invalid else entry.input_snapshot_artifact_id
        ),
        "input_snapshot_sha256": (
            None if snapshot_invalid else entry.input_snapshot_sha256
        ),
        "task_run_result": task_run_result.model_dump(mode="json"),
    }
    return PendingReportFinish(
        **values,
        request_sha256=finish_request_sha256(**values),
    )


def validate_finish_receipt(
    response: ApiResult,
    entry: PendingReportFinish,
) -> ApiResult:
    """把错误 identity 的 2xx 转成 uncertain ACK，只允许重放同一 entry。"""
    if not response.ok:
        return response
    data = response.data
    valid = (
        isinstance(data, dict)
        and data.get("protocol") == FINISH_PROTOCOL
        and data.get("project_id") == entry.project_id
        and data.get("task_run_id") == entry.task_run_id
        and data.get("generation_token") == entry.generation_token
        and data.get("status") == entry.status
        and data.get("artifact_id") == entry.artifact_id
        and data.get("input_snapshot_artifact_id")
        == entry.input_snapshot_artifact_id
        and data.get("input_snapshot_sha256") == entry.input_snapshot_sha256
        and data.get("request_sha256") == entry.request_sha256
        and data.get("finish_disposition") in ("accepted", "recovered")
    )
    if valid:
        return response
    return ApiResult(
        status_code=0,
        data={"detail": "assessment_report_finish_receipt_invalid"},
    )


class ReportFinishOutboxConflict(RuntimeError):
    """同一 TaskRun 已存在不同或非法推进的 durable report identity。"""


def _entry_digest(entry: ReportJournalEntry) -> str:
    if isinstance(entry, PendingReportFinish):
        return entry.request_sha256
    return entry.entry_sha256


def _entry_identity(entry: ReportJournalEntry) -> tuple[str, str, str, str, str, str]:
    return (
        entry.project_id,
        entry.authenticated_dispatcher_id,
        entry.worker,
        entry.claim_sha256,
        entry.generation_token,
        entry.task_run_id,
    )


class ReportFinishOutbox:
    """在 owner-only 目录中原子保存报告 execution/output/finish 单一阶段。"""

    _LOCK_NAME = ".lock"

    def __init__(self, root: Path):
        self.root = Path(os.path.abspath(root))
        self._lock = RLock()

    @staticmethod
    def _entry_name(project_id: str, task_run_id: str) -> str:
        digest = sha256(f"{project_id}\0{task_run_id}".encode("utf-8")).hexdigest()
        return f"{digest}.json"

    def _entry_path(self, project_id: str, task_run_id: str) -> Path:
        return self.root / self._entry_name(project_id, task_run_id)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _ensure_root(self) -> None:
        parent = self.root.parent
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            parent_created = False
        else:
            parent_created = True
        if parent_created:
            self._fsync_directory(parent.parent)
        parent_stat = os.lstat(parent)
        if (
            stat.S_ISLNK(parent_stat.st_mode)
            or not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
            or parent_stat.st_mode & 0o077
        ):
            raise OSError("report finish outbox parent is not owner-only")
        try:
            self.root.mkdir(mode=0o700)
        except FileExistsError:
            root_created = False
        else:
            root_created = True
        if root_created:
            self._fsync_directory(parent)
        root_stat = os.lstat(self.root)
        if (
            stat.S_ISLNK(root_stat.st_mode)
            or not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != os.geteuid()
            or root_stat.st_mode & 0o077
        ):
            raise OSError("report finish outbox is not owner-only")

    @contextmanager
    def _filesystem_guard(self, *, exclusive: bool) -> Iterator[None]:
        """以 owner-only flock 串行化 mutation，并保护一致的目录快照读取。"""
        with self._lock:
            self._ensure_root()
            lock_path = self.root / self._LOCK_NAME
            descriptor = os.open(
                lock_path,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                details = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(details.st_mode)
                    or details.st_uid != os.geteuid()
                    or stat.S_IMODE(details.st_mode) != 0o600
                ):
                    raise OSError(
                        "report finish outbox lock is not an owner file"
                    )
                operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(descriptor, operation)
                locked_details = os.fstat(descriptor)
                path_details = os.lstat(lock_path)
                if (
                    locked_details.st_dev != path_details.st_dev
                    or locked_details.st_ino != path_details.st_ino
                    or not stat.S_ISREG(path_details.st_mode)
                ):
                    raise OSError("report finish outbox lock changed")
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    @contextmanager
    def _mutation_guard(self) -> Iterator[None]:
        with self._filesystem_guard(exclusive=True):
            yield

    @staticmethod
    def _payload(entry: ReportJournalEntry) -> bytes:
        payload = canonical_json(entry.model_dump(mode="json")).encode("utf-8")
        limit = (
            MAX_FINISH_ENTRY_BYTES
            if isinstance(entry, PendingReportFinish)
            else MAX_ENTRY_BYTES
        )
        if len(payload) > limit:
            raise OSError("report finish outbox entry is too large")
        return payload

    def _write_temporary(self, destination: Path, payload: bytes) -> Path:
        temporary = self.root / f".{destination.name}.{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or stat.S_IMODE(details.st_mode) != 0o600
            ):
                raise OSError(
                    "report finish outbox temporary is not an owner file"
                )
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short report finish outbox write")
                view = view[written:]
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise
        os.close(descriptor)
        return temporary

    def _read_path(self, path: Path) -> ReportJournalEntry:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid():
                raise OSError("report finish outbox entry is not an owner file")
            if details.st_mode & 0o077 or details.st_size > MAX_ENTRY_BYTES:
                raise OSError("report finish outbox entry permissions or size invalid")
            content = b""
            while len(content) <= MAX_ENTRY_BYTES:
                chunk = os.read(
                    descriptor,
                    min(8192, MAX_ENTRY_BYTES + 1 - len(content)),
                )
                if not chunk:
                    break
                content += chunk
        finally:
            os.close(descriptor)
        if len(content) > MAX_ENTRY_BYTES:
            raise OSError("report finish outbox entry is too large")
        try:
            decoded = content.decode("utf-8", errors="strict")

            def reject_duplicate_keys(
                pairs: list[tuple[str, object]],
            ) -> dict[str, object]:
                value: dict[str, object] = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate JSON key")
                    value[key] = item
                return value

            parsed = json.loads(decoded, object_pairs_hook=reject_duplicate_keys)
            if not isinstance(parsed, dict):
                raise ValueError("entry must be an object")
            protocol = parsed.get("protocol")
            model: type[ReportJournalEntry]
            if protocol == EXECUTION_PROTOCOL:
                model = PendingReportExecution
            elif protocol == OUTPUT_PROTOCOL:
                model = PendingReportOutput
            elif protocol == OUTBOX_PROTOCOL:
                model = PendingReportFinish
            else:
                raise ValueError("unknown report journal protocol")
            entry = model.model_validate(parsed)
            if (
                isinstance(entry, PendingReportFinish)
                and len(content) > MAX_FINISH_ENTRY_BYTES
            ):
                raise ValueError("finish entry exceeds size limit")
            return entry
        except (UnicodeDecodeError, ValueError):
            # Pydantic 错误可能回显原始输入，统一收敛为不含内容的稳定错误。
            raise OSError("report finish outbox entry is invalid") from None

    def _persist_locked(
        self,
        entry: ReportJournalEntry,
        payload: bytes,
    ) -> None:
        destination = self._entry_path(entry.project_id, entry.task_run_id)
        try:
            current = self._read_path(destination)
        except FileNotFoundError:
            current = None
        if current is not None:
            if _entry_digest(current) == _entry_digest(entry):
                self._fsync_directory(self.root)
                return
            raise ReportFinishOutboxConflict(
                "task run already has a different report finish request"
            )
        temporary = self._write_temporary(destination, payload)
        try:
            try:
                # hard link 保留 no-replace 语义；flock 负责与 remove/replace 同步。
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                current = self._read_path(destination)
                if _entry_digest(current) != _entry_digest(entry):
                    raise ReportFinishOutboxConflict(
                        "task run already has a different report journal entry"
                    ) from None
        finally:
            temporary.unlink(missing_ok=True)
            self._fsync_directory(self.root)

    def persist(self, entry: ReportJournalEntry) -> None:
        payload = self._payload(entry)
        with self._mutation_guard():
            self._persist_locked(entry, payload)

    @staticmethod
    def _validate_replacement(
        expected: ReportJournalEntry,
        replacement: ReportJournalEntry,
    ) -> None:
        if _entry_identity(expected) != _entry_identity(replacement):
            raise ReportFinishOutboxConflict(
                "replacement changed immutable report journal identity"
            )
        if _entry_digest(expected) == _entry_digest(replacement):
            return
        allowed = False
        if isinstance(expected, PendingReportExecution):
            allowed = isinstance(replacement, PendingReportOutput) or (
                isinstance(replacement, PendingReportFinish)
                and replacement.status == "failed"
            )
        elif isinstance(expected, PendingReportOutput):
            allowed = (
                isinstance(replacement, PendingReportFinish)
                and replacement.status == "ready"
                and replacement.artifact_sha256 == expected.artifact_sha256
            )
        elif isinstance(expected, PendingReportFinish):
            allowed = (
                isinstance(replacement, PendingReportFinish)
                and expected.status == "ready"
                and replacement.status == "failed"
            )
        if not allowed:
            raise ReportFinishOutboxConflict(
                "report journal replacement is not an allowed transition"
            )
        expected_snapshot_id = expected.input_snapshot_artifact_id
        expected_snapshot_sha256 = expected.input_snapshot_sha256
        replacement_snapshot_id = replacement.input_snapshot_artifact_id
        replacement_snapshot_sha256 = replacement.input_snapshot_sha256
        snapshot_may_drop = (
            isinstance(replacement, PendingReportFinish)
            and replacement.status == "failed"
            and (
                isinstance(expected, PendingReportExecution)
                or (
                    isinstance(expected, PendingReportFinish)
                    and expected.status == "ready"
                )
            )
        )
        if snapshot_may_drop:
            valid_snapshot = (
                replacement_snapshot_id
                in {None, expected_snapshot_id}
                and replacement_snapshot_sha256
                in {None, expected_snapshot_sha256}
            )
        else:
            valid_snapshot = (
                replacement_snapshot_id == expected_snapshot_id
                and replacement_snapshot_sha256 == expected_snapshot_sha256
            )
        if not valid_snapshot:
            raise ReportFinishOutboxConflict(
                "report journal replacement changed snapshot identity"
            )

    def _replace_locked(
        self,
        expected: ReportJournalEntry,
        replacement: ReportJournalEntry,
        payload: bytes,
    ) -> None:
        destination = self._entry_path(
            expected.project_id,
            expected.task_run_id,
        )
        try:
            current = self._read_path(destination)
        except FileNotFoundError:
            raise ReportFinishOutboxConflict(
                "expected report finish request is missing"
            ) from None
        if _entry_digest(current) == _entry_digest(replacement):
            self._fsync_directory(self.root)
            return
        if _entry_digest(current) != _entry_digest(expected):
            raise ReportFinishOutboxConflict(
                "report journal entry changed before replacement"
            )
        temporary = self._write_temporary(destination, payload)
        try:
            os.replace(temporary, destination)
            self._fsync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)

    def replace(
        self,
        expected: ReportJournalEntry,
        replacement: ReportJournalEntry,
    ) -> None:
        """原子执行 exact compare-and-replace，只允许单向 journal 推进。"""
        self._validate_replacement(expected, replacement)
        payload = self._payload(replacement)
        with self._mutation_guard():
            self._replace_locked(expected, replacement, payload)

    def _remove_locked(self, entry: ReportJournalEntry) -> None:
        path = self._entry_path(entry.project_id, entry.task_run_id)
        try:
            current = self._read_path(path)
        except FileNotFoundError:
            return
        if _entry_digest(current) != _entry_digest(entry):
            raise ReportFinishOutboxConflict(
                "refusing to remove a different report journal entry"
            )
        path.unlink()
        self._fsync_directory(self.root)

    def remove(self, entry: ReportJournalEntry) -> None:
        if not self.root.exists():
            return
        with self._mutation_guard():
            self._remove_locked(entry)

    def _entries_locked(self) -> list[ReportJournalEntry]:
        values: list[ReportJournalEntry] = []
        for path in sorted(self.root.iterdir(), key=lambda item: item.name):
            if path.name.startswith("."):
                continue
            if not path.name.endswith(".json"):
                raise OSError("unexpected report finish outbox entry")
            entry = self._read_path(path)
            if path.name != self._entry_name(entry.project_id, entry.task_run_id):
                raise OSError("report finish outbox filename mismatch")
            values.append(entry)
        return values

    def entries(self) -> list[ReportJournalEntry]:
        # 空 outbox 也执行 storage preflight，避免模型运行后才发现目录不可持久化。
        with self._filesystem_guard(exclusive=False):
            return self._entries_locked()

    def _migrate_locked_to(self, destination: "ReportFinishOutbox") -> int:
        """source lock 已持有；逐项先 durable 写目标，再删除旧副本。"""
        migrated = 0
        with destination._mutation_guard():
            for entry in self._entries_locked():
                destination_path = destination._entry_path(
                    entry.project_id,
                    entry.task_run_id,
                )
                try:
                    current = destination._read_path(destination_path)
                except FileNotFoundError:
                    current = None
                if current is None:
                    destination._persist_locked(entry, destination._payload(entry))
                elif _entry_digest(current) == _entry_digest(entry):
                    pass
                else:
                    try:
                        destination._validate_replacement(current, entry)
                    except ReportFinishOutboxConflict:
                        try:
                            destination._validate_replacement(entry, current)
                        except ReportFinishOutboxConflict:
                            raise ReportFinishOutboxConflict(
                                "legacy namespace has a conflicting report journal entry"
                            ) from None
                    else:
                        destination._replace_locked(
                            current,
                            entry,
                            destination._payload(entry),
                        )
                self._remove_locked(entry)
                migrated += 1
        return migrated

    @contextmanager
    def cutover_to(
        self,
        destination: "ReportFinishOutbox",
    ) -> Iterator[int]:
        """迁移后持续独占 legacy lock，阻止旧 Dispatcher 写出遗漏 entry。"""
        if self.root == destination.root:
            yield 0
            return
        # canonicalization 是单向且幂等的，canonical root 不会反向迁移到 legacy root。
        with self._mutation_guard():
            migrated = self._migrate_locked_to(destination)
            yield migrated

    def has_project(self, project_id: str) -> bool:
        return any(entry.project_id == project_id for entry in self.entries())
