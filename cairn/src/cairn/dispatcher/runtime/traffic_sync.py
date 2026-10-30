"""持续同步私有采集 spool；只恢复材料上传，永不重新发起目标请求。"""

from __future__ import annotations

import base64
from collections import deque
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
import time

from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.server.schemas.traffic import (
    TRAFFIC_BATCH_BODY_BYTES, TRAFFIC_BATCH_BYTES, TRAFFIC_BATCH_FLOWS, TRAFFIC_CHUNK_BYTES,
)


def _save_receipt(path: Path, receipt: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        json.dump(receipt, output, separators=(",", ":"))
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class TrafficSync:
    def __init__(
        self, client: CairnClient, *, capture: Path, receipts: Path,
        interval: float = 0.5, max_attempts: int = 5,
    ) -> None:
        self.client = client
        self.capture = capture
        self.receipts = receipts
        self.interval = interval
        self.max_attempts = max_attempts
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._registered: set[str] = set()
        self._failures: dict[str, dict] = {}
        self._status_lock = threading.Lock()
        self._status = {"state": "stopped", "code": None, "running": False, "last_success": None,
                        "pending": None, "pending_bytes": None, "errors": {}, "scopes": {}}
        self._pending_bytes = 0
        self._seal_hashes: dict[tuple, tuple] = {}
        self._hash_progress = 0
        self._scopes: dict[str, dict] = {}
        self._scope_queue: deque[str] = deque()
        self._reconcile: set[str] = set()
        self._bindings_scan = None
        self._bindings_complete = False
        self._next_bindings_scan = 0.0
        self._persisted_status = None
        self.reconcile_interval = 5.0

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("traffic_sync_already_started")
        self.receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
        status_path = self.receipts / "status.json"
        if status_path.exists():
            saved = json.loads(status_path.read_text())
            self._failures = saved.get("failures", {})
            for key in ("last_success",):
                if key in saved:
                    self._status[key] = saved[key]
            # 重启后的旧扫描不能证明当前 spool 已排空；scopes 等本轮重新遍历再发布。
        self._publish_status(state="starting", running=True)
        self._thread = threading.Thread(target=self._run, name="traffic-sync", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))
        with self._status_lock:
            if self._thread is not None and self._thread.is_alive():
                # 在途 HTTP 仍可能成功；不能把 join 超时称为上传已停止或删除 spool。
                self._status.update(state="stopping", running=True)
        return self.status()

    def status(self) -> dict:
        with self._status_lock:
            return json.loads(json.dumps(self._status))

    def request_reconcile(self, scope_id: str) -> None:
        """collector 已停止后要求重新核对；调用方不等待上传或文件 I/O。"""
        with self._status_lock:
            self._reconcile.add(scope_id)
            scope = self._status["scopes"].get(scope_id)
            if scope is not None:
                scope.update(upload_complete=False, pending=None, pending_bytes=None, inventory_complete=False)
            self._status.update(pending=None, pending_bytes=None)

    def _publish_status(self, **changes) -> None:
        with self._status_lock:
            self._status.update(changes)
            for scope_id in self._reconcile:
                scope = self._status["scopes"].get(scope_id)
                if scope is not None:
                    scope.update(upload_complete=False, pending=None, pending_bytes=None, inventory_complete=False)
                self._status.update(pending=None, pending_bytes=None)
            if self._stop.is_set() and self._status["running"] and self._status["state"] != "blocked":
                self._status["state"] = "stopping"
            self._status["errors"] = {
                key: {"code": value["code"], "attempts": value["attempts"], "blocked": value["blocked"]}
                for key, value in self._failures.items()
            }
            snapshot = json.loads(json.dumps({**self._status, "failures": self._failures}))
        # 文件 I/O 不持有状态锁，stop 的有界 join 不会再等待磁盘 fsync。
        if snapshot != self._persisted_status:
            _save_receipt(self.receipts / "status.json", snapshot)
            self._persisted_status = snapshot

    def _failure(self, key: str, result: ApiResult) -> None:
        attempts = self._failures.get(key, {}).get("attempts", 0) + 1
        retryable = result.status_code in {0, 408, 429} or result.status_code >= 500
        with self._status_lock:
            self._failures[key] = {
                "attempts": attempts,
                "blocked": not retryable or attempts >= self.max_attempts,
                "retry_at": time.time() + min(30, 2 ** min(attempts - 1, 5)),
                "code": result.error_code or f"http_{result.status_code}",
            }
        self._publish_status()

    def _ready(self, key: str) -> bool:
        failure = self._failures.get(key)
        return not self._stop.is_set() and (
            not failure or (not failure["blocked"] and time.time() >= failure["retry_at"])
        )

    def _accept(self, key: str, result: ApiResult) -> bool:
        if not result.ok or not isinstance(result.data, dict) or result.error_code:
            if result.ok and not result.error_code:
                result = ApiResult(status_code=422, error_code="invalid_receipt")
            self._failure(key, result)
            return False
        # 预算属于实际操作，metadata 成功不能清除同一 flow 的 body/seal 失败。
        with self._status_lock:
            self._failures.pop(key, None)
            self._status["last_success"] = time.time()
        # 成功统计按有界轮转发布；每条材料回执仍在确认后独立 fsync。
        return True

    def _sync_flow(self, binding: dict, path: Path) -> bool:
        record, receipt, digest = self._load_flow(binding, path)
        flow_id = path.parent.name
        receipt_path = self.receipts / f"{binding['id']}-{flow_id}.json"
        key = f"{binding['id']}/{flow_id}"
        metadata_key = f"{key}/metadata"
        if receipt.get("metadata_sha256") != digest:
            unsent_bytes = sum(
                max(0, record[side]["body_bytes"] - receipt["bodies"].get(side, {}).get(
                    "accepted_bytes", receipt["bodies"].get(side, {}).get("size", 0),
                )) for side in ("request", "response") if record[side] is not None
            )
            if not self._ready(metadata_key):
                self._pending_bytes += unsent_bytes
                return False
            result = self.client.sync_traffic_flow(binding["project_id"], binding["id"], record)
            if not self._accept(metadata_key, result):
                self._pending_bytes += unsent_bytes
                return False
            # 回执目录丢失时从服务端持久前缀恢复；有 pending 范围时仍按原范围重试。
            for side, remote in result.data["sync"]["bodies"].items():
                if side not in receipt["bodies"] and remote["status"] != "absent":
                    receipt["bodies"][side] = {
                        "accepted_bytes": remote["accepted_bytes"],
                        **({"revision": remote["revision"], "artifact_id": remote["artifact_id"],
                            "sha256": remote["sha256"], "size": remote["size"]} if remote["sealed"] else {}),
                    }
            receipt.update(metadata_sha256=digest, revision=record["revision"])
            _save_receipt(receipt_path, receipt)
        complete = record["state"] != "receiving"
        for side in ("request", "response"):
            message = record[side]
            if message is None:
                continue
            saved = receipt["bodies"].setdefault(side, {"accepted_bytes": 0})
            if saved.get("artifact_id") and saved.get("revision") == message["body_revision"]:
                continue
            accepted = saved.get("accepted_bytes", 0)
            size = message["body_bytes"]
            if accepted > size:
                raise ValueError("traffic_spool_prefix_regressed")
            body_key = f"{key}/{side}/body"
            if accepted < size:
                if not self._ready(body_key):
                    self._pending_bytes += size - accepted
                    complete = False
                    continue
                pending = saved.get("pending")
                length = pending["size"] if pending else min(size - accepted, TRAFFIC_CHUNK_BYTES)
                # 每轮每侧至多一个范围，避免长正文饿死其它 flow 或延迟取消。
                with (path.parent / f"{side}.body").open("rb") as source:
                    source.seek(accepted)
                    chunk = source.read(length)
                if len(chunk) != length:
                    raise OSError("traffic_body_short_read")
                chunk_digest = sha256(chunk).hexdigest()
                expected = {"offset": accepted, "size": length, "sha256": chunk_digest}
                if pending is not None and pending != expected:
                    raise ValueError("traffic_pending_chunk_changed")
                saved["pending"] = expected
                # 先保存重试身份，避免丢回执后因文件继续增长而重传不同长度。
                _save_receipt(receipt_path, receipt)
                if self._stop.is_set():
                    return False
                result = self.client.sync_traffic_body(
                    binding["project_id"], binding["id"], flow_id, side,
                    **expected, content=chunk,
                )
                if result.ok and not result.error_code and (
                    not isinstance(result.data, dict)
                    or any(result.data.get(name) != value for name, value in {
                        **expected, "project_id": binding["project_id"], "flow_id": flow_id,
                        "side": side, "next_offset": accepted + length,
                    }.items())
                ):
                    result = ApiResult(status_code=422, error_code="invalid_body_receipt")
                if not self._accept(body_key, result):
                    self._pending_bytes += size - accepted
                    complete = False
                    continue
                accepted += length
                saved["accepted_bytes"] = accepted
                saved.pop("pending", None)
                _save_receipt(receipt_path, receipt)
            self._pending_bytes += size - accepted
            if accepted < size:
                complete = False
                continue
            if not message["body_sealed"]:
                complete = False
                continue
            seal_key = f"{key}/{side}/seal"
            if not self._ready(seal_key):
                complete = False
                continue
            seal = saved.get("seal")
            if seal is None:
                hash_key = (binding["id"], flow_id, side, message["body_revision"], size)
                body_digest, offset = self._seal_hashes.get(hash_key, (sha256(), 0))
                length = min(size - offset, TRAFFIC_CHUNK_BYTES)
                with (path.parent / f"{side}.body").open("rb") as source:
                    source.seek(offset)
                    chunk = source.read(length)
                if len(chunk) != length:
                    raise OSError("traffic_body_short_read")
                body_digest.update(chunk)
                self._hash_progress += length
                offset += length
                if offset < size:
                    # 封存摘要也按范围轮转；重启可重算，不把未完成 hash 当成持久回执。
                    self._seal_hashes[hash_key] = (body_digest, offset)
                    complete = False
                    continue
                self._seal_hashes.pop(hash_key, None)
                seal = {"revision": message["body_revision"], "size": size, "sha256": body_digest.hexdigest()}
                saved["seal"] = seal
                _save_receipt(receipt_path, receipt)
            if self._stop.is_set():
                return False
            result = self.client.seal_traffic_body(binding["project_id"], binding["id"], flow_id, side, **seal)
            if result.ok and not result.error_code and (
                not isinstance(result.data, dict) or not result.data.get("artifact_id")
                or any(result.data.get(name) != value for name, value in {
                    **seal, "project_id": binding["project_id"], "flow_id": flow_id, "side": side,
                }.items())
            ):
                result = ApiResult(status_code=422, error_code="invalid_seal_receipt")
            if not self._accept(seal_key, result):
                complete = False
                continue
            saved.update(**seal, artifact_id=result.data["artifact_id"])
            saved.pop("seal", None)
            _save_receipt(receipt_path, receipt)
        return complete

    def _load_flow(self, binding: dict, path: Path) -> tuple[dict, dict, str]:
        raw = path.read_bytes()
        record = json.loads(raw)
        if record["id"] != path.parent.name or record["scope"] != binding:
            raise ValueError("traffic_spool_binding_mismatch")
        receipt_path = self.receipts / f"{binding['id']}-{record['id']}.json"
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {"bodies": {}}
        return record, receipt, sha256(raw).hexdigest()

    @staticmethod
    def _remaining(record: dict, receipt: dict) -> tuple[bool, int]:
        complete = record["state"] != "receiving"
        pending_bytes = 0
        for side in ("request", "response"):
            message = record[side]
            if message is None:
                continue
            saved = receipt["bodies"].get(side, {})
            pending_bytes += max(0, message["body_bytes"] - saved.get("accepted_bytes", saved.get("size", 0)))
            complete = complete and bool(message["body_sealed"] and saved.get("artifact_id")
                                         and saved.get("revision") == message["body_revision"])
        return complete, pending_bytes

    def _discover_scopes(self) -> bool:
        if self._bindings_scan is None:
            if time.monotonic() < self._next_bindings_scan:
                return False
            self._bindings_scan = os.scandir(self.capture)
        for _ in range(64):
            entry = next(self._bindings_scan, None)
            if entry is None:
                self._bindings_scan.close()
                self._bindings_scan = None
                self._bindings_complete = True
                self._next_bindings_scan = time.monotonic() + self.reconcile_interval
                break
            if not entry.name.endswith(".json"):
                continue
            scope_id = Path(entry.name).stem
            if scope_id not in self._scopes:
                self._scopes[scope_id] = {"path": Path(entry.path), "binding": None, "entries": {},
                                          "queue": deque(), "scan": None, "inventory_complete": False,
                                          "scan_started_at": None, "scanned_at": None, "next_scan": 0.0,
                                          "pending": 0, "pending_bytes": 0, "unknown_bytes": 0}
                self._scope_queue.append(scope_id)
        return True

    @staticmethod
    def _set_progress(scope: dict, entry: dict, complete: bool, pending_bytes: int | None) -> None:
        # 计数随增量项更新，发布状态不再每轮遍历所有已确认 flow。
        scope["pending"] += int(not complete) - int(not entry["complete"])
        scope["unknown_bytes"] += int(pending_bytes is None) - int(entry["pending_bytes"] is None)
        scope["pending_bytes"] += (pending_bytes or 0) - (entry["pending_bytes"] or 0)
        entry.update(complete=complete, pending_bytes=pending_bytes)

    def _scan_scope(self, scope_id: str, scope: dict) -> bool:
        with self._status_lock:
            requested = scope_id in self._reconcile
            self._reconcile.discard(scope_id)
        if requested:
            if scope["scan"] is not None:
                scope["scan"].close()
            scope.update(scan=None, inventory_complete=False, next_scan=0.0)
        if scope["scan"] is None:
            if time.monotonic() < scope["next_scan"]:
                return False
            scope["scan_started_at"] = time.time()
            scope["inventory_complete"] = False
            root = self.capture / scope_id / "flows"
            scope["scan"] = os.scandir(root) if root.exists() else iter(())
        # 目录遍历也分片，不先排序或读取整个大 scope 才服务其它 scope。
        for _ in range(128):
            directory = next(scope["scan"], None)
            if directory is None:
                scan = scope["scan"]
                if hasattr(scan, "close"):
                    scan.close()
                scope.update(scan=None, inventory_complete=True, scanned_at=time.time(),
                             next_scan=time.monotonic() + self.reconcile_interval)
                break
            path = Path(directory.path) / "flow.json"
            if not path.is_file():
                continue
            info = path.stat()
            signature = (info.st_mtime_ns, info.st_size, info.st_ino)
            entry = scope["entries"].get(directory.name)
            if entry is not None and entry["signature"] == signature:
                continue
            if entry is None:
                entry = {"path": path, "queued": False, "complete": True, "pending_bytes": 0}
                scope["entries"][directory.name] = entry
            entry["signature"] = signature
            complete, pending_bytes = False, None
            try:
                record, receipt, digest = self._load_flow(scope["binding"], path)
                complete, pending_bytes = self._remaining(record, receipt)
                complete = complete and receipt.get("metadata_sha256") == digest
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._failure(f"{scope_id}/{directory.name}/spool", ApiResult(status_code=422, error_code=type(exc).__name__))
            self._set_progress(scope, entry, complete, pending_bytes)
            if not entry["complete"] and not entry["queued"]:
                scope["queue"].append(directory.name)
                entry["queued"] = True
        return True

    def _batch(self, binding: dict, candidates: list[tuple[dict, dict, str, dict]]) -> None:
        result = self.client.sync_traffic_batch(binding["project_id"], binding["id"], [item[3] for item in candidates])
        remote = result.data.get("items") if result.ok and isinstance(result.data, dict) else None
        valid = isinstance(remote, list) and len(remote) == len(candidates) and all(
            isinstance(reply, dict) and reply.get("flow_id") == record["id"]
            for reply, (record, _, _, _) in zip(remote, candidates)
        )
        for index, (record, receipt, digest, item) in enumerate(candidates):
            key = f"{binding['id']}/{record['id']}/metadata"
            accepted = (ApiResult(status_code=remote[index].get("status_code", 422),
                                  data=remote[index].get("data"), error_code=remote[index].get("error_code"))
                        if valid else ApiResult(status_code=result.status_code if not result.ok else 422,
                                                error_code=result.error_code or "invalid_batch_receipt"))
            data = accepted.data
            if accepted.ok and (not isinstance(data, dict) or data.get("id") != record["id"]
                                or data.get("revision") != record["revision"] or not isinstance(data.get("sync"), dict)):
                accepted = ApiResult(status_code=422, error_code="invalid_batch_receipt")
            if accepted.ok:
                bodies = data["sync"].get("bodies")
                if not isinstance(bodies, dict) or set(bodies) != {"request", "response"}:
                    accepted = ApiResult(status_code=422, error_code="invalid_batch_receipt")
                else:
                    for side, message in ((side, record[side]) for side in ("request", "response")):
                        saved = bodies[side]
                        if not isinstance(saved, dict) or (message is None and saved.get("status") != "absent") or (
                            message is not None and (saved.get("status") not in {"available", "pending", "receiving"}
                            or type(saved.get("accepted_bytes")) is not int or saved["accepted_bytes"] < 0
                            or saved["accepted_bytes"] > message["body_bytes"] or type(saved.get("sealed")) is not bool
                            or (saved["sealed"] and not all(name in saved for name in ("revision", "artifact_id", "sha256", "size"))))
                        ):
                            accepted = ApiResult(status_code=422, error_code="invalid_batch_receipt")
                            break
            if accepted.ok:
                for side, part in item["bodies"].items():
                    saved = bodies.get(side, {})
                    if not saved.get("artifact_id") or not saved.get("sealed") or any(
                        saved.get(name) != part[name] for name in ("revision", "size", "sha256")
                    ):
                        accepted = ApiResult(status_code=422, error_code="invalid_batch_receipt")
                        break
            if not self._accept(key, accepted):
                continue
            for side, saved in data["sync"]["bodies"].items():
                if saved["status"] == "absent" or "pending" in receipt["bodies"].get(side, {}):
                    continue
                receipt["bodies"][side] = {"accepted_bytes": saved["accepted_bytes"], **(
                    {name: saved[name] for name in ("revision", "artifact_id", "sha256", "size")} if saved["sealed"] else {})}
            receipt.update(metadata_sha256=digest, revision=record["revision"])
            _save_receipt(self.receipts / f"{binding['id']}-{record['id']}.json", receipt)

    def _service_scope(self, scope_id: str, scope: dict) -> None:
        binding = scope["binding"]
        selected = []
        candidates = []
        body_budget = TRAFFIC_BATCH_BODY_BYTES
        singles = []
        for _ in range(min(TRAFFIC_BATCH_FLOWS, len(scope["queue"]))):
            flow_id = scope["queue"].popleft()
            selected.append(flow_id)
            entry = scope["entries"][flow_id]
            key = f"{scope_id}/{flow_id}"
            if not self._ready(f"{key}/spool"):
                continue
            try:
                record, receipt, digest = self._load_flow(binding, entry["path"])
                if receipt.get("metadata_sha256") == digest:
                    singles.append(entry)
                    continue
                if not self._ready(f"{key}/metadata"):
                    continue
                item = {"record": record, "bodies": {}}
                messages = [(side, record[side]) for side in ("request", "response") if record[side] is not None]
                size = sum(message["body_bytes"] for _, message in messages)
                if size <= body_budget and all(message["body_sealed"] for _, message in messages) and not any(
                    saved.get("pending") for saved in receipt["bodies"].values()
                ):
                    for side, message in messages:
                        with (entry["path"].parent / f"{side}.body").open("rb") as source:
                            raw = source.read(message["body_bytes"] + 1)
                        if len(raw) != message["body_bytes"]:
                            raise OSError("traffic_body_size_mismatch")
                        item["bodies"][side] = {"revision": message["body_revision"], "size": len(raw),
                                               "sha256": sha256(raw).hexdigest(),
                                               "content_base64": base64.b64encode(raw).decode("ascii")}
                batch_items = [candidate[3] for candidate in candidates] + [item]
                if len(json.dumps({"items": batch_items}).encode()) > TRAFFIC_BATCH_BYTES:
                    singles.append(entry)
                    continue
                candidates.append((record, receipt, digest, item))
                if item["bodies"]:
                    body_budget -= size
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._failure(f"{key}/spool", ApiResult(status_code=422, error_code=type(exc).__name__))
        if candidates and not self._stop.is_set():
            self._batch(binding, candidates)
        # 大正文仍用已有有界 chunk 协议，一次轮转至多服务一个 flow 的两侧。
        for entry in singles[:1]:
            if self._stop.is_set():
                break
            try:
                self._pending_bytes = 0
                self._sync_flow(binding, entry["path"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._failure(f"{scope_id}/{entry['path'].parent.name}/spool",
                              ApiResult(status_code=422, error_code=type(exc).__name__))
        if singles:
            serviced = singles[0]["path"].parent.name
            selected.remove(serviced)
            selected.append(serviced)
        for flow_id in selected:
            entry = scope["entries"][flow_id]
            try:
                record, receipt, digest = self._load_flow(binding, entry["path"])
                complete, pending_bytes = self._remaining(record, receipt)
                complete = complete and receipt.get("metadata_sha256") == digest
            except (OSError, ValueError, KeyError, TypeError):
                complete, pending_bytes = False, None
            self._set_progress(scope, entry, complete, pending_bytes)
            entry["queued"] = not entry["complete"]
            if entry["queued"]:
                scope["queue"].append(flow_id)

    def _publish_inventory(self) -> None:
        scopes = {}
        with self._status_lock:
            requested = set(self._reconcile)
        for scope_id, scope in self._scopes.items():
            failures = [value for key, value in self._failures.items() if key.startswith(f"{scope_id}/")]
            known = scope["inventory_complete"] and scope_id not in requested
            pending = scope["pending"] if known else None
            pending_bytes = scope["pending_bytes"] if known and not scope["unknown_bytes"] else None
            scopes[scope_id] = {
                "pending": pending, "pending_bytes": pending_bytes,
                "upload_complete": known and pending == 0 and not failures,
                "inventory_complete": known,
                "state": "blocked" if any(item["blocked"] for item in failures) else "retrying" if failures else "watching",
                "code": failures[0]["code"] if failures else None,
                "scan_started_at": scope["scan_started_at"], "scanned_at": scope["scanned_at"],
            }
        self._publish_status(
            state="blocked" if any(item["blocked"] for item in self._failures.values()) else "retrying" if self._failures else "watching",
            pending=sum(s["pending"] for s in scopes.values()) if self._bindings_complete and all(s["pending"] is not None for s in scopes.values()) else None,
            pending_bytes=sum(s["pending_bytes"] for s in scopes.values()) if self._bindings_complete and all(s["pending_bytes"] is not None for s in scopes.values()) else None,
            scopes=scopes,
        )

    def _step(self) -> bool:
        progressed = self._discover_scopes()
        if not self._scope_queue:
            return progressed
        scope_id = self._scope_queue.popleft()
        self._scope_queue.append(scope_id)
        scope = self._scopes[scope_id]
        scope_key = f"{scope_id}/scope"
        if not self._ready(scope_key):
            return progressed
        before = (self._status["last_success"], self._hash_progress)
        try:
            if scope["binding"] is None:
                binding = json.loads(scope["path"].read_text())
                if binding["id"] != scope_id:
                    raise ValueError("traffic_scope_path_mismatch")
                scope["binding"] = binding
            # 先建立本地库存，注册失败也不能把未知积压写成零。
            progressed = self._scan_scope(scope_id, scope) or progressed
            if scope_id not in self._registered:
                binding = scope["binding"]
                if not self._accept(scope_key, self.client.register_traffic_scope(binding["project_id"], binding)):
                    return progressed
                self._registered.add(scope_id)
            self._service_scope(scope_id, scope)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._failure(scope_key, ApiResult(status_code=422, error_code=type(exc).__name__))
        return progressed or (self._status["last_success"], self._hash_progress) != before

    def _run(self) -> None:
        idle = 0
        try:
            while not self._stop.is_set():
                progressed = self._step()
                self._publish_inventory()
                idle = 0 if progressed else idle + 1
                if idle >= max(1, len(self._scope_queue)):
                    self._stop.wait(self.interval)
                    idle = 0
        except OSError:
            # 状态盘不可写不能伪报成功，原件和已确认回执仍保留。
            with self._status_lock:
                self._status.update(state="blocked", code="status_persist_failed",
                                    errors={"status": {"code": "status_persist_failed"}})
        finally:
            for scope in self._scopes.values():
                if scope["scan"] is not None and hasattr(scope["scan"], "close"):
                    scope["scan"].close()
            if self._bindings_scan is not None:
                self._bindings_scan.close()
            try:
                self._publish_status(state="blocked" if self._status["state"] == "blocked" else "stopped", running=False)
            except OSError:
                with self._status_lock:
                    self._status.update(state="blocked", running=False, code="status_persist_failed",
                                        errors={"status": {"code": "status_persist_failed"}})
