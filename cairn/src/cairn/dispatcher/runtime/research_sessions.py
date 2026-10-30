"""只读研究 session 已落盘的完整行；不取得写锁、不恢复或启动研究容器。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat

MAX_RECORD_BYTES = 256 * 1024
MAX_PAGE_BYTES = 512 * 1024


def _now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _session_file(manager, reference):
    identifier = reference["session_id"][3:].lower()
    project_id = reference["project_id"]
    if reference["storage"] == "legacy_container":
        # 旧容器只用 Docker archive 读取快照；容器缺失时不 ensure_running。
        with manager.materialize_file(manager.container_name(project_id), f"/home/kali/workspace/.cairn/pi/{identifier}/session.jsonl") as materialized:
            with materialized.path.open("rb") as stream:
                yield stream
        return
    runtime = manager.traffic_execution
    if runtime is None:
        raise FileNotFoundError("research_runtime_not_configured")
    # 逐层以 dirfd 打开受控根，避免读取活动目录时链接替换跨到其他项目。
    directory = os.open(Path(runtime.root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (project_id, "control", "pi", identifier):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open("session.jsonl", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("session_path_not_regular")
            yield stream
    finally:
        os.close(directory)


def read_session_page(manager, reference):
    session_id = reference["session_id"]
    unavailable = {"session_id": session_id, "availability": "unavailable"}
    if manager is None:
        return {**unavailable, "reason": "research_reader_unavailable"}
    if not re.fullmatch(r"pi:[0-9a-fA-F-]{36}", session_id) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", reference["project_id"]):
        return {**unavailable, "reason": "session_identity_invalid"}
    try:
        with _session_file(manager, reference) as stream:
            stat = os.fstat(stream.fileno())
            observed = stat.st_size
            header = stream.readline(MAX_RECORD_BYTES + 1)
            parsed = json.loads(header)
            if (not header.endswith(b"\n") or not isinstance(parsed, dict) or parsed.get("type") != "session"
                    or parsed.get("cairn") != {"project_id": reference["project_id"], "session": session_id}):
                return {**unavailable, "reason": "session_ownership_unverifiable"}
            stream.seek(0)
            line, used, entries, missing, incomplete = 0, 0, [], [], False
            start, limit = reference["offset"], reference["limit"]
            while stream.tell() < observed:
                position = stream.tell()
                raw = stream.readline(min(MAX_RECORD_BYTES + 1, observed - position))
                if len(raw) > MAX_RECORD_BYTES and raw.endswith(b"\n"):
                    if line >= start:
                        missing.append({"line": line, "reason": "record_exceeds_read_limit"})
                elif not raw.endswith(b"\n"):
                    if len(raw) > MAX_RECORD_BYTES:
                        while stream.tell() < observed and not raw.endswith(b"\n"):
                            raw = stream.readline(min(MAX_RECORD_BYTES, observed - stream.tell()))
                        if not raw.endswith(b"\n"):
                            incomplete = True
                            break
                        if line >= start:
                            missing.append({"line": line, "reason": "record_exceeds_read_limit"})
                    else:
                        incomplete = True
                        break
                elif line >= start:
                    if used + len(raw) > MAX_PAGE_BYTES:
                        stream.seek(position)
                        break
                    try:
                        text = raw.decode("utf-8")
                        json.loads(text)
                    except (UnicodeError, ValueError):
                        missing.append({"line": line, "reason": "record_unreadable"})
                    else:
                        entries.append({"line": line, "raw": text})
                        used += len(raw)
                line += 1
                if line >= start + limit:
                    break
            return {"session_id": session_id, "availability": "available", "read_at": _now(),
                    "entries": entries, "missing": missing, "incomplete_tail": incomplete,
                    "next_offset": line if incomplete or stream.tell() < observed else None,
                    "observed_bytes": observed, "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()}
    except FileNotFoundError:
        return {**unavailable, "reason": "session_not_persisted_or_removed"}
    except (OSError, RuntimeError, ValueError, TypeError):
        return {**unavailable, "reason": "session_unreadable_or_ownership_unverifiable"}
