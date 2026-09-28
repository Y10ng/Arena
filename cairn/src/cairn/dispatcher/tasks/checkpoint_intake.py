"""Worker checkpoint spool 的有界校验与 Server staging 摄取。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import time
from typing import Any

from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.workers.base import (
    EXECUTION_CHECKPOINT_MAX_EVENT_BYTES,
    EXECUTION_CHECKPOINT_MAX_EVENTS,
    EXECUTION_CHECKPOINT_MAX_SPOOL_BYTES,
    EXECUTION_CHECKPOINT_PROTOCOL,
    WorkerCheckpointSpool,
)

from .common_base import LOG
from .write_outcomes import classify_write_response

_CHECKPOINT_CATEGORIES = {
    "progress",
    "observation",
    "hypothesis",
    "limitation",
    "handoff",
}
_CHECKPOINT_MAX_SUMMARY_CHARS = 1000
_CHECKPOINT_MAX_DETAILS_DEPTH = 6
_CHECKPOINT_MAX_DETAILS_NODES = 256
_CHECKPOINT_MAX_COLLECTION_ITEMS = 64
_CHECKPOINT_MAX_DETAIL_STRING_CHARS = 4000
_CHECKPOINT_MAX_DETAIL_KEY_CHARS = 128
_CHECKPOINT_WRITE_ATTEMPTS = 3
_CHECKPOINT_WRITE_RETRY_BASE_SECONDS = 0.5
_SAFE_EVENT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_DETAIL_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_RESERVED_DETAIL_KEYS = {
    "artifact",
    "artifacts",
    "artifact_id",
    "artifact_ids",
    "artifact_handle",
    "artifact_handles",
    "artifact_path",
    "artifact_paths",
    "evidence_ref",
    "evidence_refs",
    "path",
    "paths",
    "project_id",
    "task_run_id",
    "owner",
    "owner_subject",
    "verified_dispatcher_id",
    "dispatcher_id",
    "generation",
    "execution_generation",
    "generation_disposition",
    "producer_event_key",
    "payload",
    "payload_json",
    "payload_digest",
    "payload_digest_sha256",
    "digest",
    "received_at",
    "created_at",
    "updated_at",
    "receipt",
    "receipt_disposition",
    "promotion",
    "promotion_status",
    "business_acceptance",
}
_RESERVED_DETAIL_KEYS_COMPACT = {
    key.replace("_", "") for key in _RESERVED_DETAIL_KEYS
}
_ARTIFACT_DETAIL_KEY_RE = re.compile(
    r"artifact(?:s|id|ids|handle|handles|path|paths|ref|refs)?$"
)


@dataclass(slots=True)
class CheckpointIntakeSummary:
    """不含 checkpoint 正文的摄取摘要。"""

    spool_bytes: int = 0
    events_seen: int = 0
    valid_events: int = 0
    received: int = 0
    replayed: int = 0
    receipt_conflicts: int = 0
    transient_failures: int = 0
    permanent_failures: int = 0
    invalid_events: int = 0
    error_code: str | None = None


class _CheckpointValidationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _validation_error(code: str) -> _CheckpointValidationError:
    return _CheckpointValidationError(code)


def _read_spool_bytes(
    container_manager: ContainerManager,
    container_name: str,
    descriptor: WorkerCheckpointSpool,
) -> bytes:
    materialize = getattr(container_manager, "materialize_file", None)
    if callable(materialize):
        with materialize(container_name, descriptor.path) as materialized:
            if materialized.size > descriptor.max_spool_bytes:
                raise _validation_error("spool_size_limit")
            with Path(materialized.path).open("rb") as source:
                content = source.read(descriptor.max_spool_bytes + 1)
    else:
        # 测试替身可只实现 bytes API；真实 ContainerManager 走上面的有界宿主文件路径。
        content = container_manager.read_file(container_name, descriptor.path)
    if len(content) > descriptor.max_spool_bytes:
        raise _validation_error("spool_size_limit")
    return content


def _json_without_duplicates(text: str) -> object:
    def reject_constant(_value: str) -> object:
        raise _validation_error("event_non_finite_number")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        output: dict[str, object] = {}
        for key, value in pairs:
            if key in output:
                raise _validation_error("event_duplicate_field")
            output[key] = value
        return output

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except _CheckpointValidationError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _validation_error("event_json_invalid") from exc


def _normalized_detail_key(key: str) -> str:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")


def _validate_detail_key(key: str) -> None:
    normalized = _normalized_detail_key(key)
    compact = normalized.replace("_", "")
    tokens = set(normalized.split("_"))
    if (
        key != key.strip()
        or len(key) > _CHECKPOINT_MAX_DETAIL_KEY_CHARS
        or not _SAFE_DETAIL_KEY_RE.fullmatch(key)
        or normalized in _RESERVED_DETAIL_KEYS
        or compact in _RESERVED_DETAIL_KEYS_COMPACT
        or _ARTIFACT_DETAIL_KEY_RE.search(compact)
        or compact in {"evidenceref", "evidencerefs"}
        or compact.endswith(("path", "paths"))
        or tokens.intersection({"artifact", "artifacts", "path", "paths"})
        or ("evidence" in tokens and ("ref" in tokens or "refs" in tokens))
    ):
        raise _validation_error("details_key_invalid")


def _validate_details_value(value: object, *, depth: int, nodes: list[int]) -> None:
    nodes[0] += 1
    if nodes[0] > _CHECKPOINT_MAX_DETAILS_NODES:
        raise _validation_error("details_node_limit")
    if depth > _CHECKPOINT_MAX_DETAILS_DEPTH:
        raise _validation_error("details_depth_limit")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _validation_error("event_non_finite_number")
        return
    if isinstance(value, str):
        if len(value) > _CHECKPOINT_MAX_DETAIL_STRING_CHARS or "\0" in value:
            raise _validation_error("details_string_limit")
        return
    if isinstance(value, list):
        if len(value) > _CHECKPOINT_MAX_COLLECTION_ITEMS:
            raise _validation_error("details_collection_limit")
        for item in value:
            _validate_details_value(item, depth=depth + 1, nodes=nodes)
        return
    if isinstance(value, dict):
        if len(value) > _CHECKPOINT_MAX_COLLECTION_ITEMS:
            raise _validation_error("details_collection_limit")
        for key, item in value.items():
            if not isinstance(key, str):
                raise _validation_error("details_key_invalid")
            _validate_detail_key(key)
            _validate_details_value(item, depth=depth + 1, nodes=nodes)
        return
    raise _validation_error("details_value_invalid")


def _validate_recorded_at(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z") or "\0" in value:
        raise _validation_error("recorded_at_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise _validation_error("recorded_at_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise _validation_error("recorded_at_invalid")
    utc_value = parsed.astimezone(timezone.utc)
    timespec = "milliseconds" if utc_value.microsecond % 1000 == 0 else "microseconds"
    return utc_value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _validate_event(
    value: object,
    descriptor: WorkerCheckpointSpool,
    *,
    phase: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "protocol",
        "producer_event_key",
        "kind",
        "phase",
        "recorded_at",
        "checkpoint",
    }:
        raise _validation_error("event_shape_invalid")
    if value.get("protocol") != descriptor.protocol or value.get("protocol") != EXECUTION_CHECKPOINT_PROTOCOL:
        raise _validation_error("event_protocol_invalid")
    if value.get("kind") != "checkpoint":
        raise _validation_error("event_kind_invalid")
    if value.get("phase") != phase:
        raise _validation_error("event_phase_mismatch")
    producer_key = value.get("producer_event_key")
    if not isinstance(producer_key, str) or not _SAFE_EVENT_KEY_RE.fullmatch(producer_key):
        raise _validation_error("producer_event_key_invalid")
    value["recorded_at"] = _validate_recorded_at(value.get("recorded_at"))

    checkpoint = value.get("checkpoint")
    if not isinstance(checkpoint, dict) or set(checkpoint) != {"category", "summary", "details"}:
        raise _validation_error("checkpoint_shape_invalid")
    if checkpoint.get("category") not in _CHECKPOINT_CATEGORIES:
        raise _validation_error("checkpoint_category_invalid")
    summary = checkpoint.get("summary")
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or "\0" in summary
        or len(summary) > _CHECKPOINT_MAX_SUMMARY_CHARS
    ):
        raise _validation_error("checkpoint_summary_invalid")
    details = checkpoint.get("details")
    if not isinstance(details, dict):
        raise _validation_error("checkpoint_details_invalid")
    _validate_details_value(details, depth=1, nodes=[0])
    return value


def _canonical_event_digest(event: dict[str, object]) -> str:
    encoded = json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _receipt_matches_event(response: ApiResult, event: dict[str, object]) -> bool:
    data = response.data
    expected_disposition = {201: "received", 200: "replayed"}.get(response.status_code)
    if expected_disposition is None or not isinstance(data, dict):
        return False
    expected_keys = {
        "protocol",
        "event_id",
        "producer_event_key",
        "payload_digest_sha256",
        "disposition",
        "generation_disposition",
        "received_at",
    }
    digest = data.get("payload_digest_sha256")
    return (
        set(data) == expected_keys
        and data.get("protocol") == "cairn-execution-event-receipt-v1"
        and isinstance(data.get("event_id"), str)
        and bool(data["event_id"])
        and data.get("producer_event_key") == event.get("producer_event_key")
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and digest == _canonical_event_digest(event)
        and data.get("disposition") == expected_disposition
        and data.get("generation_disposition")
        in {"current_at_receipt", "historical_at_receipt"}
        and isinstance(data.get("received_at"), str)
        and bool(data["received_at"])
    )


def _post_event_with_retry(
    client: CairnClient,
    project_id: str,
    task_run_id: str,
    event: dict[str, object],
) -> ApiResult:
    attempt = 1
    while True:
        try:
            response = client.create_execution_event(project_id, task_run_id, event)
        except Exception:
            # 响应解析或 transport 异常意味着收据未知，只允许重放当前 event。
            response = ApiResult(status_code=0)
        receipt_missing = response.ok and not _receipt_matches_event(response, event)
        retry = classify_write_response(response).retry_same_write or receipt_missing
        if not retry or attempt >= _CHECKPOINT_WRITE_ATTEMPTS:
            return response
        delay = _CHECKPOINT_WRITE_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
        LOG.warning(
            "checkpoint delivery transient failure project=%s task_run=%s status=%s retry_in=%.1fs attempt=%s/%s",
            project_id,
            task_run_id,
            response.status_code,
            delay,
            attempt + 1,
            _CHECKPOINT_WRITE_ATTEMPTS,
        )
        time.sleep(delay)
        attempt += 1


def _record_delivery(
    summary: CheckpointIntakeSummary,
    response: ApiResult,
    event: dict[str, object],
) -> None:
    if response.status_code in {200, 201}:
        if not _receipt_matches_event(response, event):
            summary.transient_failures += 1
            summary.error_code = summary.error_code or "execution_event_receipt_invalid"
        elif response.status_code == 201:
            summary.received += 1
        else:
            summary.replayed += 1
        return
    disposition = classify_write_response(response)
    if disposition.disposition == "receipt_conflict":
        summary.receipt_conflicts += 1
    elif disposition.retry_same_write:
        summary.transient_failures += 1
    else:
        summary.permanent_failures += 1


def intake_worker_checkpoints(
    client: CairnClient,
    container_manager: ContainerManager,
    container_name: str,
    descriptor: WorkerCheckpointSpool,
    *,
    project_id: str,
    task_run_id: str,
    phase: str,
) -> CheckpointIntakeSummary:
    """摄取一次已退出进程的 spool；任何失败都只形成安全摘要。"""
    summary = CheckpointIntakeSummary()
    try:
        content = _read_spool_bytes(container_manager, container_name, descriptor)
        summary.spool_bytes = len(content)
    except _CheckpointValidationError as exc:
        summary.error_code = exc.code
        LOG.warning(
            "checkpoint intake rejected project=%s task_run=%s phase=%s code=%s",
            project_id,
            task_run_id,
            phase,
            exc.code,
        )
        return summary
    except Exception as exc:
        summary.error_code = "spool_read_failed"
        LOG.warning(
            "checkpoint intake read failed project=%s task_run=%s phase=%s error=%s",
            project_id,
            task_run_id,
            phase,
            type(exc).__name__,
        )
        return summary

    if not content:
        return summary
    records = content.split(b"\n")
    complete_records = records[:-1]
    if records[-1]:
        summary.invalid_events += 1
        summary.error_code = "spool_framing_invalid"
    event_count = len(complete_records) + (1 if records[-1] else 0)
    summary.events_seen = event_count
    if event_count > descriptor.max_events or event_count > EXECUTION_CHECKPOINT_MAX_EVENTS:
        summary.error_code = "spool_event_limit"
        summary.invalid_events = event_count
        LOG.warning(
            "checkpoint intake rejected project=%s task_run=%s phase=%s code=%s events=%s",
            project_id,
            task_run_id,
            phase,
            summary.error_code,
            event_count,
        )
        return summary

    for index, raw_line in enumerate(complete_records, start=1):
        if not raw_line or len(raw_line) > descriptor.max_event_bytes or len(raw_line) > EXECUTION_CHECKPOINT_MAX_EVENT_BYTES:
            summary.invalid_events += 1
            summary.error_code = summary.error_code or "event_size_invalid"
            continue
        try:
            text = raw_line.decode("utf-8", errors="strict")
            event = _validate_event(_json_without_duplicates(text), descriptor, phase=phase)
        except UnicodeDecodeError:
            summary.invalid_events += 1
            summary.error_code = summary.error_code or "event_utf8_invalid"
            continue
        except _CheckpointValidationError as exc:
            summary.invalid_events += 1
            summary.error_code = summary.error_code or exc.code
            LOG.info(
                "checkpoint event ignored project=%s task_run=%s phase=%s line=%s code=%s",
                project_id,
                task_run_id,
                phase,
                index,
                exc.code,
            )
            continue

        summary.valid_events += 1
        try:
            response = _post_event_with_retry(client, project_id, task_run_id, event)
        except Exception as exc:
            # client transport 正常返回 status=0；此分支只防御测试替身或未知 client 异常。
            summary.transient_failures += 1
            LOG.warning(
                "checkpoint delivery crashed project=%s task_run=%s phase=%s line=%s error=%s",
                project_id,
                task_run_id,
                phase,
                index,
                type(exc).__name__,
            )
            continue
        _record_delivery(summary, response, event)

    LOG.info(
        "checkpoint intake complete project=%s task_run=%s phase=%s events=%s valid=%s received=%s replayed=%s conflicts=%s transient=%s permanent=%s invalid=%s code=%s",
        project_id,
        task_run_id,
        phase,
        summary.events_seen,
        summary.valid_events,
        summary.received,
        summary.replayed,
        summary.receipt_conflicts,
        summary.transient_failures,
        summary.permanent_failures,
        summary.invalid_events,
        summary.error_code,
    )
    return summary
