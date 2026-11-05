"""直接 HTTP 推理的持久回执；只重送计量，不重跑模型请求。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import tempfile
from threading import Lock
from typing import Any, Iterator
from uuid import uuid4

from cairn.server import db
from .consumption import FIELDS, model_snapshot, normalize_http_usage, save_response

logger = logging.getLogger(__name__)
_MEMORY_LIMIT = 128
_memory_receipts: dict[str, dict[str, Any]] = {}
_memory_lock = Lock()


def _spool_root() -> Path:
    root = db.artifact_root() / "model-consumption"
    root.mkdir(mode=0o700, exist_ok=True)
    _fsync_directory(root.parent)
    return root


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_receipt(directory: Path, receipt: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(receipt, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / "receipt.json")
        _fsync_directory(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _submit_receipt(receipt: dict[str, Any]) -> None:
    binding = receipt["binding"]
    # get_conn 每次打开独立连接；外层业务回滚不能撤销已经发生的消费。
    with db.get_conn() as conn:
        conn.execute("""INSERT INTO model_consumption_bindings
            (id,tenant_id,owner_id,source,model_json) VALUES (?,?,?,?,?)
            ON CONFLICT (id) DO NOTHING""", (
                binding["id"], binding["tenant_id"], binding["owner_id"],
                binding["source"], json.dumps(binding["model"]),
            ))
        save_response(conn, binding["id"], receipt["event"])


def _deliver(directory: Path | None, receipt: dict[str, Any]) -> bool:
    identifier = receipt["binding"]["id"]
    try:
        _submit_receipt(receipt)
    except Exception as exc:
        # 不记录 DB 异常原文，避免 SQL 参数或连接凭据进入日志。
        logger.warning("Direct consumption delivery failed: %s (%s)", identifier, type(exc).__name__)
        return False
    if directory is None:
        return True
    try:
        (directory / "receipt.json").unlink(missing_ok=True)
        # 崩溃可能留下尚未 replace 的临时文件；它们不是已确认的消费原件。
        for temporary in directory.glob(".receipt-*"):
            temporary.unlink()
        directory.rmdir()
        _fsync_directory(directory.parent)
    except OSError as exc:
        # 已提交但清理失败的回执可由原恢复周期幂等重送。
        logger.warning("Direct consumption receipt cleanup pending: %s (%s)", identifier, type(exc).__name__)
    return True


class DirectConsumption:
    def __init__(self, directory: Path | None, receipt: dict[str, Any]) -> None:
        self.directory = directory
        self.receipt = receipt
        self.durable_revision = -1

    def persist(self) -> None:
        if self.directory is None:
            return
        try:
            _write_receipt(self.directory, self.receipt)
            self.durable_revision = self.receipt["event"]["revision"]
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Direct consumption spool unavailable: %s (%s)",
                           self.receipt["binding"]["id"], type(exc).__name__)

    def observe_response(self, protocol: str, body: bytes) -> None:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            payload = None
        usage = normalize_http_usage(protocol, payload)
        self.receipt["event"] = {
            "response_seq": 0, "revision": 1,
            "occurred_at": datetime.now(timezone.utc).isoformat(), "time_basis": "received",
            "usage": usage, "measurement": "raw",
            "missing_responses": int(all(value is None for value in usage.values())),
        }
        # 原始正文仅在当前栈中解析，回执只允许模型快照和四分量计量。
        self.persist()


@contextmanager
def record_direct_consumption(*, tenant_id: str | None, owner_id: str, source: str,
                       model: dict[str, Any]) -> Iterator[DirectConsumption]:
    identifier = "direct_" + uuid4().hex
    receipt = {
        "binding": {"id": identifier, "tenant_id": tenant_id, "owner_id": owner_id,
                    "source": source, "model": model_snapshot(model)},
        "event": {"response_seq": 0, "revision": 0,
                  "occurred_at": datetime.now(timezone.utc).isoformat(), "time_basis": "observed",
                  "usage": dict.fromkeys(FIELDS), "measurement": "started", "missing_responses": 1},
    }
    descriptor = None
    directory = None
    try:
        try:
            root = _spool_root()
            candidate = root / identifier
            candidate.mkdir(mode=0o700)
            descriptor = os.open(candidate, os.O_RDONLY | os.O_DIRECTORY)
            # 锁覆盖实际 HTTP；恢复周期不能删除在途开始回执。
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            directory = candidate
            _fsync_directory(root)
        except OSError as exc:
            logger.warning("Direct consumption spool unavailable: %s (%s)", identifier, type(exc).__name__)
        recording = DirectConsumption(directory, receipt)
        recording.persist()
        try:
            yield recording
        finally:
            if receipt["event"]["revision"] == 0:
                receipt["event"]["revision"] = 1
                receipt["event"]["measurement"] = "interrupted"
            if recording.durable_revision != receipt["event"]["revision"]:
                recording.persist()
            if recording.durable_revision != receipt["event"]["revision"]:
                # 磁盘故障时只留本路径有界内存兜底，仍由现有恢复周期投递；崩溃不可恢复。
                with _memory_lock:
                    retained = len(_memory_receipts) < _MEMORY_LIMIT
                    if retained:
                        _memory_receipts[identifier] = receipt
                if retained:
                    logger.warning("Direct consumption receipt only in memory: %s", identifier)
                else:
                    logger.error("Direct consumption gap not fully retained: %s", identifier)
            # 已持久化回执交由现有恢复周期；账本连接或锁不能卡住业务结果。
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                logger.warning("Direct consumption spool close failed: %s (%s)", identifier, type(exc).__name__)


def replay_direct_consumption(*, limit: int = 100) -> int:
    with _memory_lock:
        pending = list(_memory_receipts.items())[:limit]
    delivered = 0
    for identifier, receipt in pending:
        if _deliver(None, receipt):
            with _memory_lock:
                if _memory_receipts.get(identifier) is receipt:
                    del _memory_receipts[identifier]
            delivered += 1
    try:
        root = _spool_root()
    except OSError as exc:
        logger.error("Direct consumption replay spool unavailable (%s)", type(exc).__name__)
        return delivered
    attempted = 0
    for directory in root.iterdir():
        if attempted >= limit:
            break
        if not directory.name.startswith("direct_") or not directory.is_dir():
            continue
        try:
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        except FileNotFoundError:
            continue
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            try:
                receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            attempted += 1
            delivered += int(_deliver(directory, receipt))
        finally:
            os.close(descriptor)
    return delivered


async def replay_direct_consumption_periodically() -> None:
    while True:
        try:
            await asyncio.to_thread(replay_direct_consumption)
        except (OSError, ValueError) as exc:
            logger.error("Direct consumption replay failed (%s)", type(exc).__name__)
        await asyncio.sleep(30)
