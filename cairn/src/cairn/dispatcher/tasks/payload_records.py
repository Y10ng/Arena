"""AI Agent Payload 记录器的容器准备与 Dispatcher 收集。"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.containers import ContainerManager

LOG = logging.getLogger(__name__)

PAYLOAD_RECORDER_ROOT = "/home/kali/workspace/.cairn"
PAYLOAD_RECORDER_BIN_ROOT = f"{PAYLOAD_RECORDER_ROOT}/bin"
PAYLOAD_RECORDER_PATH = f"{PAYLOAD_RECORDER_BIN_ROOT}/cairn-payload"
PAYLOAD_LOG_ROOT = "/tmp/cairn-payloads"
PAYLOAD_OPERATION_BATCH_COUNT = 200
PAYLOAD_OPERATION_BATCH_BYTES = 8 * 1024 * 1024
PAYLOAD_TERMINAL_STATUSES = {
    "returned",
    "empty",
    "blocked",
    "timeout",
    "error",
    "not_sent",
    "interrupted",
}
PAYLOAD_CAPTURE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

PAYLOAD_RECORDER_SCRIPT = r'''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import sys

ALLOWED_STATUSES = {
    "returned", "empty", "blocked", "timeout", "error", "not_sent", "interrupted"
}


def read_text(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


def append_operation(operation: dict[str, object]) -> None:
    log_path = os.environ.get("CAIRN_PAYLOAD_LOG", "").strip()
    if not log_path:
        raise RuntimeError("CAIRN_PAYLOAD_LOG is not configured")
    target = pathlib.Path(log_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(operation, ensure_ascii=False, separators=(",", ":")) + "\n"
    descriptor = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, line.encode("utf-8"))
    finally:
        os.close(descriptor)


def begin(args: argparse.Namespace) -> int:
    capture_id = "pc_" + secrets.token_hex(16)
    append_operation(
        {
            "version": 1,
            "operation": "begin",
            "capture_id": capture_id,
            "injection_type": read_text(args.injection_type_file),
            "payload_text": read_text(args.payload_file),
            "original_format": read_text(args.original_format_file),
        }
    )
    print(capture_id)
    return 0


def finish(args: argparse.Namespace) -> int:
    if args.status not in ALLOWED_STATUSES:
        raise ValueError(f"unsupported response status: {args.status}")
    operation: dict[str, object] = {
        "version": 1,
        "operation": "finish",
        "capture_id": args.capture_id,
        "response_status": args.status,
    }
    if args.response_file:
        operation["response_text"] = read_text(args.response_file)
    if args.response_format_file:
        operation["response_format"] = read_text(args.response_format_file)
    append_operation(operation)
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="cairn-payload")
    commands = root.add_subparsers(dest="command", required=True)

    begin_parser = commands.add_parser("begin")
    begin_parser.add_argument("--injection-type-file", required=True)
    begin_parser.add_argument("--payload-file", required=True)
    begin_parser.add_argument("--original-format-file", required=True)
    begin_parser.set_defaults(handler=begin)

    finish_parser = commands.add_parser("finish")
    finish_parser.add_argument("--capture-id", required=True)
    finish_parser.add_argument("--status", required=True)
    finish_parser.add_argument("--response-file")
    finish_parser.add_argument("--response-format-file")
    finish_parser.set_defaults(handler=finish)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(f"cairn-payload: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


@dataclass(frozen=True, slots=True)
class PayloadOperationLog:
    path: str
    phase: str


def prepare_payload_recorder(
    container_manager: ContainerManager,
    container_name: str,
    project_id: str | None,
    task_run_id: str | None,
    *,
    phase: str,
    enabled: bool,
) -> PayloadOperationLog | None:
    if not enabled or project_id is None or task_run_id is None:
        # 固定脚本没有 CAIRN_PAYLOAD_LOG 时无法写入；不要删除并发任务正在使用的入口。
        return None
    log_path = (
        f"{PAYLOAD_LOG_ROOT}/{_safe_segment(project_id)}/{_safe_segment(task_run_id)}/"
        f"{_safe_segment(phase)}-{uuid.uuid4().hex[:12]}.jsonl"
    )
    try:
        # 脚本只写本次进程的 JSONL，不接触 Server 凭据或数据库连接。
        container_manager.write_text_file(
            container_name,
            PAYLOAD_RECORDER_PATH,
            PAYLOAD_RECORDER_SCRIPT,
            owner="kali",
            mode=0o755,
        )
        # 预创建空日志，让“本阶段没有实际 Payload”成为正常空结果而不是读取失败。
        container_manager.write_text_file(
            container_name,
            log_path,
            "",
            owner="kali",
            mode=0o600,
        )
    except Exception as exc:
        try:
            _remove_payload_log(container_manager, container_name, log_path)
        except Exception:
            pass
        LOG.warning(
            "payload recorder setup failed project=%s task_run=%s phase=%s error=%s",
            project_id,
            task_run_id,
            phase,
            exc,
        )
        return None
    return PayloadOperationLog(path=log_path, phase=phase)


def payload_recorder_env(
    log: PayloadOperationLog,
    *,
    current_path: str | None,
) -> dict[str, str]:
    base_path = current_path or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    return {
        "CAIRN_PAYLOAD_LOG": log.path,
        "PATH": f"{PAYLOAD_RECORDER_BIN_ROOT}:{base_path}",
    }


def record_payload_operations(
    client: CairnClient | None,
    container_manager: ContainerManager,
    container_name: str,
    project_id: str | None,
    task_run_id: str | None,
    payload_log: PayloadOperationLog | None,
) -> None:
    if client is None or project_id is None or task_run_id is None or payload_log is None:
        return
    try:
        with container_manager.materialize_file(container_name, payload_log.path) as materialized:
            if materialized.size == 0:
                _remove_payload_log(container_manager, container_name, payload_log.path)
                return
            # 上传前先完整扫描，避免尾部坏行导致前半批次已写入、后半批次才失败。
            operation_count = _validate_operation_log(
                materialized.path,
                task_run_id=task_run_id,
                phase=payload_log.phase,
            )
            uploaded_count = 0
            for batch in _operation_batches(
                materialized.path,
                task_run_id=task_run_id,
                phase=payload_log.phase,
            ):
                response = client.apply_injection_payload_operations(project_id, batch)
                if not response.ok:
                    LOG.warning(
                        "payload operation upload failed project=%s task_run=%s phase=%s status=%s count=%s",
                        project_id,
                        task_run_id,
                        payload_log.phase,
                        response.status_code,
                        len(batch),
                    )
                    return
                uploaded_count += len(batch)
            if uploaded_count != operation_count:
                raise RuntimeError("payload operation upload count mismatch")
            _remove_payload_log(container_manager, container_name, payload_log.path)
            LOG.info(
                "payload operations recorded project=%s task_run=%s phase=%s count=%s",
                project_id,
                task_run_id,
                payload_log.phase,
                operation_count,
            )
    except Exception as exc:
        # 失败时保留容器源文件，日志只记录定位信息，不回显 Payload 或响应。
        LOG.warning(
            "payload operation collect failed project=%s task_run=%s phase=%s path=%s error=%s",
            project_id,
            task_run_id,
            payload_log.phase,
            payload_log.path,
            exc,
        )


def _validate_operation_log(
    path: Path,
    *,
    task_run_id: str,
    phase: str,
) -> int:
    count = 0
    with path.open("rb") as content:
        for line_number, raw_line in enumerate(content, start=1):
            if not raw_line.strip():
                continue
            _parse_operation(
                raw_line,
                line_number=line_number,
                task_run_id=task_run_id,
                phase=phase,
            )
            count += 1
    return count


def _operation_batches(
    path: Path,
    *,
    task_run_id: str,
    phase: str,
) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    batch_bytes = 0
    with path.open("rb") as content:
        for line_number, raw_line in enumerate(content, start=1):
            if not raw_line.strip():
                continue
            operation = _parse_operation(
                raw_line,
                line_number=line_number,
                task_run_id=task_run_id,
                phase=phase,
            )
            operation_bytes = len(
                json.dumps(operation, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            if batch and (
                len(batch) >= PAYLOAD_OPERATION_BATCH_COUNT
                or batch_bytes + operation_bytes > PAYLOAD_OPERATION_BATCH_BYTES
            ):
                yield batch
                batch = []
                batch_bytes = 0
            batch.append(operation)
            batch_bytes += operation_bytes
    if batch:
        yield batch


def _parse_operation(
    raw_line: bytes,
    *,
    line_number: int,
    task_run_id: str,
    phase: str,
) -> dict[str, Any]:
    try:
        raw = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"invalid payload JSONL at line {line_number}") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError(f"unsupported payload operation at line {line_number}")
    operation_name = raw.get("operation")
    capture_id = raw.get("capture_id")
    if (
        operation_name not in {"begin", "finish"}
        or not isinstance(capture_id, str)
        or PAYLOAD_CAPTURE_ID_PATTERN.fullmatch(capture_id) is None
    ):
        raise ValueError(f"invalid payload operation at line {line_number}")

    if operation_name == "begin":
        operation = {
            "operation": "begin",
            "capture_id": capture_id,
            "task_run_id": task_run_id,
            "phase": phase,
            "injection_type": raw.get("injection_type"),
            "payload_text": raw.get("payload_text"),
            "original_format": raw.get("original_format"),
        }
        if not all(
            isinstance(operation[field], str)
            for field in ("injection_type", "payload_text", "original_format")
        ):
            raise ValueError(f"invalid payload begin operation at line {line_number}")
        return operation

    response_status = raw.get("response_status")
    if response_status not in PAYLOAD_TERMINAL_STATUSES:
        raise ValueError(f"invalid payload finish operation at line {line_number}")
    operation = {
        "operation": "finish",
        "capture_id": capture_id,
        "task_run_id": task_run_id,
        "response_status": response_status,
    }
    for field in ("response_text", "response_format"):
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"invalid payload finish operation at line {line_number}")
        if value is not None:
            operation[field] = value
    return operation


def _remove_payload_log(
    container_manager: ContainerManager,
    container_name: str,
    path: str,
) -> None:
    container_manager.remove_file(
        container_name,
        path,
        allowed_root=PAYLOAD_LOG_ROOT,
    )


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "unknown"
