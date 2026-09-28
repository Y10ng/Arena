"""验证 Pi binary spool，并保留可原样重放的 exact candidate frames。"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
import struct
import time

from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.tasks.candidate_capture_outbox import (
    CandidateCaptureOutbox,
    CandidateCaptureOutboxError,
    PendingCandidateCapture,
)
from cairn.dispatcher.tasks.common_base import LOG
from cairn.dispatcher.worker_protocol.submission import (
    ARTIFACT_KINDS,
    CANDIDATE_MAX_OUTCOMES_PER_CANDIDATE,
    CANDIDATE_OUTCOME_CODES,
    SUBMISSION_ISSUE_KEYWORDS,
    submission_schema_paths,
)
from cairn.dispatcher.workers.base import CANDIDATE_FRAME_PROTOCOL, WorkerCandidateSpool


_FRAME_MAGIC = b"CRNCND01"
_FRAME_HEADER_BYTES = 16
_FRAME_CHECKSUM_BYTES = 32
_MAX_ARTIFACTS = 16
_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_PRODUCER_COORDINATE = 1_000_000
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SAFE_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PRODUCER_KEY_RE = re.compile(
    r"^(?P<capture>[0-9a-f-]{36}):(?P<turn>[1-9][0-9]*):(?P<index>0|[1-9][0-9]*)$"
)
_CANDIDATE_KEYS = {
    "protocol",
    "record_kind",
    "capture_id",
    "producer_candidate_key",
    "parent_producer_candidate_key",
    "assistant_turn",
    "content_index",
    "project_id",
    "task_run_id",
    "phase",
    "session",
    "binding_sha256",
    "result_schema_sha256",
    "artifacts",
    "raw_sha256",
    "raw_size_bytes",
}
_OUTCOME_KEYS = {
    "protocol",
    "record_kind",
    "capture_id",
    "producer_candidate_key",
    "outcome_sequence",
    "code",
    "path",
    "keyword",
    "count",
    "artifact_status",
    "binding_status",
}
_ARTIFACT_KEYS = {"handle", "kind", "sha256", "size_bytes"}
_ARTIFACT_STATUSES = {"not_evaluated", "validated", "rejected"}
_BINDING_STATUSES = {"not_evaluated", "not_required", "injected", "rejected"}


class CandidateIntakeError(RuntimeError):
    """Candidate spool 未通过完整性或执行 authority 校验。"""


def _canonical_json(value: dict[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise CandidateIntakeError("candidate manifest is not canonical JSON") from exc


def _parse_manifest(manifest_bytes: bytes) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CandidateIntakeError("candidate manifest contains duplicate keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise CandidateIntakeError("candidate manifest contains a non-JSON number")

    try:
        decoded = manifest_bytes.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        if isinstance(exc, CandidateIntakeError):
            raise
        raise CandidateIntakeError("candidate manifest must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CandidateIntakeError("candidate manifest must be a JSON object")
    if _canonical_json(value) != manifest_bytes:
        raise CandidateIntakeError("candidate manifest must use canonical JSON")
    return value


def _integer(value: object, *, minimum: int = 0) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= _MAX_SAFE_INTEGER
    )


def _required_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _validate_artifacts(value: object) -> None:
    if not isinstance(value, list) or len(value) > _MAX_ARTIFACTS:
        raise CandidateIntakeError("candidate artifact identity list is invalid")
    handles: list[str] = []
    for artifact in value:
        if not isinstance(artifact, dict) or set(artifact) != _ARTIFACT_KEYS:
            raise CandidateIntakeError("candidate artifact identity is invalid")
        handle = artifact["handle"]
        kind = artifact["kind"]
        if (
            not isinstance(handle, str)
            or not handle.startswith("local-")
            or _SAFE_IDENTITY_RE.fullmatch(handle) is None
            or not isinstance(kind, str)
            or kind not in ARTIFACT_KINDS
            or not isinstance(artifact["sha256"], str)
            or _SHA256_RE.fullmatch(artifact["sha256"]) is None
            or not _integer(artifact["size_bytes"])
        ):
            raise CandidateIntakeError("candidate artifact identity is invalid")
        handles.append(handle)
    if len(set(handles)) != len(handles) or handles != sorted(handles):
        raise CandidateIntakeError("candidate artifact handles must be unique and sorted")


def _validate_candidate_manifest(
    manifest: dict[str, object],
    raw: bytes,
    descriptor: WorkerCandidateSpool,
    *,
    project_id: str,
    task_run_id: str,
    phase: str,
    session: str,
    candidate_keys: set[str],
    previous_candidate_key: str | None,
    previous_candidate_coordinate: tuple[int, int] | None,
) -> None:
    if set(manifest) != _CANDIDATE_KEYS:
        raise CandidateIntakeError("candidate manifest fields are invalid")
    if (
        manifest["protocol"] != CANDIDATE_FRAME_PROTOCOL
        or manifest["record_kind"] != "candidate"
        or manifest["capture_id"] != descriptor.capture_id
    ):
        raise CandidateIntakeError("candidate frame identity is invalid")
    if (
        manifest["project_id"] != project_id
        or manifest["task_run_id"] != task_run_id
        or manifest["phase"] != phase
        or manifest["session"] != session
    ):
        raise CandidateIntakeError("candidate execution authority mismatch")
    if descriptor.binding_sha256 is None or descriptor.result_schema_sha256 is None:
        raise CandidateIntakeError("candidate binding authority is unavailable")
    if manifest["binding_sha256"] != descriptor.binding_sha256:
        raise CandidateIntakeError("candidate binding digest mismatch")
    if manifest["result_schema_sha256"] != descriptor.result_schema_sha256:
        raise CandidateIntakeError("candidate result schema digest mismatch")
    turn = manifest["assistant_turn"]
    index = manifest["content_index"]
    producer_key = manifest["producer_candidate_key"]
    if (
        not _integer(turn, minimum=1)
        or turn > _MAX_PRODUCER_COORDINATE
        or not _integer(index)
        or index > _MAX_PRODUCER_COORDINATE
        or not isinstance(producer_key, str)
        or producer_key in candidate_keys
        or (
            previous_candidate_coordinate is not None
            and (turn, index) <= previous_candidate_coordinate
        )
    ):
        raise CandidateIntakeError("candidate producer identity is invalid")
    key_match = _PRODUCER_KEY_RE.fullmatch(producer_key)
    if (
        key_match is None
        or key_match.group("capture") != descriptor.capture_id
        or int(key_match.group("turn")) != turn
        or int(key_match.group("index")) != index
    ):
        raise CandidateIntakeError("candidate producer identity is invalid")
    if manifest["parent_producer_candidate_key"] != previous_candidate_key:
        raise CandidateIntakeError("candidate lineage is invalid")
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CandidateIntakeError("candidate raw bytes must be valid UTF-8") from exc
    if (
        not _integer(manifest["raw_size_bytes"])
        or manifest["raw_size_bytes"] != len(raw)
        or manifest["raw_sha256"] != sha256(raw).hexdigest()
    ):
        raise CandidateIntakeError("candidate raw identity mismatch")
    _validate_artifacts(manifest["artifacts"])


def _validate_outcome_manifest(
    manifest: dict[str, object],
    raw: bytes,
    descriptor: WorkerCandidateSpool,
    *,
    phase: str,
    candidate_keys: set[str],
    outcome_sequences: dict[str, int],
) -> None:
    if set(manifest) != _OUTCOME_KEYS:
        raise CandidateIntakeError("candidate outcome fields are invalid")
    producer_key = manifest["producer_candidate_key"]
    sequence = manifest["outcome_sequence"]
    if (
        manifest["protocol"] != CANDIDATE_FRAME_PROTOCOL
        or manifest["record_kind"] != "outcome"
        or manifest["capture_id"] != descriptor.capture_id
        or raw
        or not isinstance(producer_key, str)
        or producer_key not in candidate_keys
        or not _integer(sequence, minimum=1)
        or sequence > CANDIDATE_MAX_OUTCOMES_PER_CANDIDATE
        or sequence != outcome_sequences.get(producer_key, 0) + 1
    ):
        raise CandidateIntakeError("candidate outcome identity is invalid")

    code = manifest["code"]
    path = manifest["path"]
    keyword = manifest["keyword"]
    count = manifest["count"]
    artifact_status = manifest["artifact_status"]
    binding_status = manifest["binding_status"]
    valid = (
        isinstance(code, str)
        and code in CANDIDATE_OUTCOME_CODES
        and (count is None or type(count) is int and count == 1)
        and ((path is None) == (keyword is None))
        and isinstance(artifact_status, str)
        and artifact_status in _ARTIFACT_STATUSES
        and isinstance(binding_status, str)
        and binding_status in _BINDING_STATUSES
    )
    if path is not None:
        valid = valid and (
            code == "submission_invalid"
            and isinstance(path, str)
            and path in submission_schema_paths(phase)
            and isinstance(keyword, str)
            and keyword in SUBMISSION_ISSUE_KEYWORDS
            and count == 1
            and manifest["artifact_status"] == "not_evaluated"
            and manifest["binding_status"] == "not_evaluated"
        )
        if isinstance(keyword, str) and keyword in {"strict_json", "input_size"}:
            valid = valid and path == "$"
    if code in {"locally_validated", "submission_invalid"}:
        valid = valid and count == 1
    if not valid:
        raise CandidateIntakeError("candidate outcome diagnostic is invalid")


def decode_candidate_spool(
    spool: bytes,
    descriptor: WorkerCandidateSpool,
    *,
    project_id: str,
    task_run_id: str,
    phase: str,
    session: str,
) -> list[PendingCandidateCapture]:
    """完整验证一个 spool；任一 frame 失败时不返回部分结果。"""
    if not isinstance(spool, bytes) or len(spool) > descriptor.max_spool_bytes:
        raise CandidateIntakeError("candidate spool exceeds its byte limit")
    offset = 0
    candidate_count = 0
    candidate_keys: set[str] = set()
    previous_candidate_key: str | None = None
    previous_candidate_coordinate: tuple[int, int] | None = None
    candidate_authority: dict[str, tuple[str, str, str, str]] = {}
    outcome_sequences: dict[str, int] = {}
    result: list[PendingCandidateCapture] = []
    while offset < len(spool):
        start = offset
        if len(spool) - offset < _FRAME_HEADER_BYTES + _FRAME_CHECKSUM_BYTES:
            raise CandidateIntakeError("candidate spool contains a truncated frame")
        if spool[offset : offset + 8] != _FRAME_MAGIC:
            raise CandidateIntakeError("candidate spool frame magic is invalid")
        manifest_length, raw_length = struct.unpack(">II", spool[offset + 8 : offset + 16])
        if manifest_length <= 0 or manifest_length > descriptor.max_manifest_bytes:
            raise CandidateIntakeError("candidate manifest exceeds its byte limit")
        if raw_length > descriptor.max_candidate_bytes:
            raise CandidateIntakeError("candidate raw bytes exceed their byte limit")
        end = offset + _FRAME_HEADER_BYTES + manifest_length + raw_length + _FRAME_CHECKSUM_BYTES
        if end > len(spool):
            raise CandidateIntakeError("candidate spool contains a truncated frame")
        frame_bytes = spool[start:end]
        if sha256(frame_bytes[:-32]).digest() != frame_bytes[-32:]:
            raise CandidateIntakeError("candidate frame checksum mismatch")
        manifest_start = offset + _FRAME_HEADER_BYTES
        raw_start = manifest_start + manifest_length
        manifest_bytes = spool[manifest_start:raw_start]
        raw = spool[raw_start : raw_start + raw_length]
        manifest = _parse_manifest(manifest_bytes)
        record_kind = manifest.get("record_kind")
        if record_kind == "candidate":
            candidate_count += 1
            if candidate_count > descriptor.max_candidates:
                raise CandidateIntakeError("candidate spool exceeds its candidate limit")
            _validate_candidate_manifest(
                manifest,
                raw,
                descriptor,
                project_id=project_id,
                task_run_id=task_run_id,
                phase=phase,
                session=session,
                candidate_keys=candidate_keys,
                previous_candidate_key=previous_candidate_key,
                previous_candidate_coordinate=previous_candidate_coordinate,
            )
            producer_key = manifest["producer_candidate_key"]
            turn = manifest["assistant_turn"]
            index = manifest["content_index"]
            assert isinstance(producer_key, str)
            assert isinstance(turn, int) and not isinstance(turn, bool)
            assert isinstance(index, int) and not isinstance(index, bool)
            candidate_keys.add(producer_key)
            previous_candidate_key = producer_key
            previous_candidate_coordinate = (turn, index)
            candidate_authority[producer_key] = (
                project_id,
                task_run_id,
                phase,
                session,
            )
            outcome_sequence = None
            raw_sha256: str | None = sha256(raw).hexdigest()
        elif record_kind == "outcome":
            _validate_outcome_manifest(
                manifest,
                raw,
                descriptor,
                phase=phase,
                candidate_keys=candidate_keys,
                outcome_sequences=outcome_sequences,
            )
            producer_key = manifest["producer_candidate_key"]
            outcome_sequence = manifest["outcome_sequence"]
            assert isinstance(producer_key, str)
            assert isinstance(outcome_sequence, int)
            outcome_sequences[producer_key] = outcome_sequence
            project_id, task_run_id, phase, session = candidate_authority[producer_key]
            raw_sha256 = None
        else:
            raise CandidateIntakeError("candidate frame kind is invalid")
        result.append(
            PendingCandidateCapture(
                project_id=project_id,
                task_run_id=task_run_id,
                phase=phase,
                session=session,
                capture_id=descriptor.capture_id,
                producer_candidate_key=producer_key,
                record_kind=record_kind,
                outcome_sequence=outcome_sequence,
                manifest_bytes=manifest_bytes,
                raw=raw,
                frame_bytes=frame_bytes,
                request_sha256=sha256(manifest_bytes + raw).hexdigest(),
                raw_sha256=raw_sha256,
                frame_index=len(result),
            )
        )
        offset = end
    return result


_CANDIDATE_DELIVERY_ATTEMPTS = 3
_CANDIDATE_DELIVERY_RETRY_BASE_SECONDS = 0.5
_RECEIPT_KEYS = {
    "protocol",
    "record_kind",
    "candidate_id",
    "producer_candidate_key",
    "outcome_sequence",
    "request_sha256",
    "raw_sha256",
    "disposition",
    "generation_disposition",
    "received_at",
}


@dataclass(slots=True)
class CandidateIntakeSummary:
    """不含 candidate 正文的 durable intake 与 delivery 摘要。"""

    spool_bytes: int = 0
    frames_seen: int = 0
    host_durable: bool = False
    received: int = 0
    replayed: int = 0
    pending: int = 0
    integrity_error: str | None = None
    blocked_project_ids: set[str] = field(default_factory=set)

    @property
    def blocks_business_acceptance(self) -> bool:
        return self.integrity_error is not None

    @property
    def global_integrity_error(self) -> str | None:
        """只有无法确认归属的 outbox 损坏才阻断全部调度。"""
        if self.integrity_error == "candidate_outbox_invalid":
            return self.integrity_error
        return None


def _read_candidate_spool(
    container_manager: ContainerManager,
    container_name: str,
    descriptor: WorkerCandidateSpool,
) -> bytes:
    with container_manager.materialize_file(
        container_name,
        descriptor.path,
    ) as materialized:
        if materialized.size > descriptor.max_spool_bytes:
            raise CandidateIntakeError("candidate spool exceeds its byte limit")
        with Path(materialized.path).open("rb") as source:
            content = source.read(descriptor.max_spool_bytes + 1)
    if len(content) > descriptor.max_spool_bytes:
        raise CandidateIntakeError("candidate spool exceeds its byte limit")
    return content


def _receipt_shape_valid(data: object) -> bool:
    if not isinstance(data, dict) or set(data) != _RECEIPT_KEYS:
        return False
    record_kind = data.get("record_kind")
    disposition = data.get("disposition")
    generation_disposition = data.get("generation_disposition")
    return (
        data.get("protocol") == "cairn-candidate-capture-receipt-v1"
        and isinstance(record_kind, str)
        and record_kind in {"candidate", "outcome"}
        and _required_text(data.get("candidate_id"))
        and _required_text(data.get("producer_candidate_key"))
        and (
            data.get("outcome_sequence") is None
            or _integer(data.get("outcome_sequence"), minimum=1)
        )
        and isinstance(data.get("request_sha256"), str)
        and _SHA256_RE.fullmatch(data["request_sha256"]) is not None
        and (
            data.get("raw_sha256") is None
            or (
                isinstance(data.get("raw_sha256"), str)
                and _SHA256_RE.fullmatch(data["raw_sha256"]) is not None
            )
        )
        and isinstance(disposition, str)
        and disposition in {"received", "replayed"}
        and isinstance(generation_disposition, str)
        and generation_disposition
        in {"current_at_receipt", "historical_at_receipt"}
        and _required_text(data.get("received_at"))
    )


def _receipt_matches(
    response: ApiResult,
    entry: PendingCandidateCapture,
) -> tuple[bool, bool]:
    """返回 (strict_match, structural_receipt_with_conflicting_identity)。"""
    data = response.data
    if response.status_code not in {200, 201} or not _receipt_shape_valid(data):
        return False, False
    assert isinstance(data, dict)
    expected_disposition = "received" if response.status_code == 201 else "replayed"
    matches = (
        data["record_kind"] == entry.record_kind
        and data["producer_candidate_key"] == entry.producer_candidate_key
        and data["outcome_sequence"] == entry.outcome_sequence
        and data["request_sha256"] == entry.request_sha256
        and data["raw_sha256"] == entry.raw_sha256
        and data["disposition"] == expected_disposition
    )
    return matches, not matches


def _capture_once(
    client: CairnClient,
    entry: PendingCandidateCapture,
) -> ApiResult:
    # CairnClient 已把 requests transport failure 归一为 status_code=0；编程错误必须传播。
    return client.capture_candidate(
        entry.project_id,
        entry.task_run_id,
        entry.manifest_bytes,
        entry.raw,
    )


def _deliver_entry(
    client: CairnClient,
    entry: PendingCandidateCapture,
) -> tuple[str, ApiResult]:
    for attempt in range(1, _CANDIDATE_DELIVERY_ATTEMPTS + 1):
        response = _capture_once(client, entry)
        matches, conflicts = _receipt_matches(response, entry)
        if matches:
            return "accepted", response
        if conflicts:
            return "conflict", response
        transient = (
            response.status_code == 0
            or response.status_code in {200, 201, 408, 429}
            or 500 <= response.status_code < 600
        )
        if not transient:
            return "rejected", response
        if attempt < _CANDIDATE_DELIVERY_ATTEMPTS:
            delay = _CANDIDATE_DELIVERY_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            LOG.warning(
                "candidate delivery pending project=%s task_run=%s capture=%s frame=%s status=%s retry_in=%.1fs attempt=%s/%s",
                entry.project_id,
                entry.task_run_id,
                entry.capture_id,
                entry.frame_index,
                response.status_code,
                delay,
                attempt + 1,
                _CANDIDATE_DELIVERY_ATTEMPTS,
            )
            time.sleep(delay)
    return "pending", response


def deliver_candidate_entries(
    client: CairnClient,
    outbox: CandidateCaptureOutbox,
    entries: list[PendingCandidateCapture],
) -> CandidateIntakeSummary:
    """按 capture/frame 顺序重放；前序未知时不越过其 dependent outcomes。"""
    summary = CandidateIntakeSummary(host_durable=True, frames_seen=len(entries))
    blocked_captures: set[str] = set()
    for entry in sorted(entries, key=lambda item: (item.capture_id, item.frame_index)):
        if entry.capture_id in blocked_captures:
            summary.pending += 1
            continue
        disposition, response = _deliver_entry(client, entry)
        if disposition == "accepted":
            try:
                removed = outbox.remove(entry)
            except CandidateCaptureOutboxError:
                removed = False
            if not removed:
                summary.pending += 1
                summary.integrity_error = (
                    summary.integrity_error or "candidate_outbox_invalid"
                )
                summary.blocked_project_ids.add(entry.project_id)
                blocked_captures.add(entry.capture_id)
                continue
            if response.status_code == 201:
                summary.received += 1
            else:
                summary.replayed += 1
            continue
        summary.pending += 1
        summary.blocked_project_ids.add(entry.project_id)
        blocked_captures.add(entry.capture_id)
        if disposition == "conflict":
            summary.integrity_error = summary.integrity_error or "candidate_receipt_conflict"
        elif disposition == "rejected":
            summary.integrity_error = summary.integrity_error or "candidate_capture_rejected"
    return summary


def replay_candidate_outbox(
    client: CairnClient,
    outbox: CandidateCaptureOutbox,
    *,
    max_entries: int | None = None,
    excluded_project_ids: set[str] | None = None,
    excluded_task_runs: set[tuple[str, str]] | None = None,
) -> CandidateIntakeSummary:
    """只重放有界数量的 durable exact requests；绝不启动模型。"""
    if max_entries is not None and (
        not isinstance(max_entries, int)
        or isinstance(max_entries, bool)
        or max_entries <= 0
    ):
        raise ValueError("max_entries must be a positive integer")
    try:
        entries = outbox.entries()
    except CandidateCaptureOutboxError:
        return CandidateIntakeSummary(
            integrity_error="candidate_outbox_invalid",
        )
    if excluded_project_ids:
        # 活跃 future 仍拥有同一 outbox frame 的交付权，恢复线程不能并发 compare-delete。
        entries = [
            entry
            for entry in entries
            if entry.project_id not in excluded_project_ids
        ]
    if excluded_task_runs:
        entries = [
            entry for entry in entries
            if (entry.project_id, entry.task_run_id) not in excluded_task_runs
        ]
    if max_entries is not None:
        entries = entries[:max_entries]
    return deliver_candidate_entries(client, outbox, entries)


def intake_worker_candidates(
    client: CairnClient,
    container_manager: ContainerManager,
    container_name: str,
    descriptor: WorkerCandidateSpool,
    outbox: CandidateCaptureOutbox,
    *,
    project_id: str,
    task_run_id: str,
    phase: str,
    session: str,
) -> CandidateIntakeSummary:
    """进程退出后先完整验证并 durable persist，再尝试 Server capture。"""
    summary = CandidateIntakeSummary()
    try:
        content = _read_candidate_spool(container_manager, container_name, descriptor)
    except CandidateIntakeError as exc:
        summary.integrity_error = "candidate_spool_invalid"
        LOG.warning(
            "candidate intake rejected project=%s task_run=%s phase=%s error=%s",
            project_id,
            task_run_id,
            phase,
            type(exc).__name__,
        )
        return summary
    except (OSError, RuntimeError) as exc:
        summary.integrity_error = "candidate_spool_read_failed"
        LOG.warning(
            "candidate intake read failed project=%s task_run=%s phase=%s error=%s",
            project_id,
            task_run_id,
            phase,
            type(exc).__name__,
        )
        return summary

    summary.spool_bytes = len(content)
    try:
        entries = decode_candidate_spool(
            content,
            descriptor,
            project_id=project_id,
            task_run_id=task_run_id,
            phase=phase,
            session=session,
        )
    except CandidateIntakeError as exc:
        summary.integrity_error = "candidate_spool_invalid"
        LOG.warning(
            "candidate intake rejected project=%s task_run=%s phase=%s error=%s",
            project_id,
            task_run_id,
            phase,
            type(exc).__name__,
        )
        return summary
    summary.frames_seen = len(entries)

    try:
        # 完整 spool 校验成功后才开始写；所有 frame 必须先跨过宿主 fsync 阈值。
        for entry in entries:
            outbox.persist(entry)
    except CandidateCaptureOutboxError as exc:
        summary.integrity_error = "candidate_outbox_persist_failed"
        LOG.warning(
            "candidate durable persist failed project=%s task_run=%s phase=%s error=%s",
            project_id,
            task_run_id,
            phase,
            type(exc).__name__,
        )
        return summary

    summary.host_durable = True
    delivered = deliver_candidate_entries(client, outbox, entries)
    summary.received = delivered.received
    summary.replayed = delivered.replayed
    summary.pending = delivered.pending
    summary.integrity_error = delivered.integrity_error
    summary.blocked_project_ids = delivered.blocked_project_ids
    return summary
