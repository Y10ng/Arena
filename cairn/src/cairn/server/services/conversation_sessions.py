"""部署时显式迁移本机旧聊天 SDK；不启动服务、不迁移 Schema、不自动扫描或跨机恢复。"""
from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

from cairn.dispatcher.runtime.conversation_sessions import session_evidence
from cairn.server.db import register_after_commit, register_after_rollback


def _directory(path: Path) -> None:
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("chat_session_destination_invalid")
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chown(directory, 1000, 1000)
        descriptor = os.open(directory.parent, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _persist(path: Path, content: bytes) -> None:
    # 失败留下可核对的目标，不覆盖原件；重试只接受逐字节相同的文件。
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("chat_session_destination_invalid")
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise ValueError("chat_session_destination_conflict")
        return
    _directory(path.parent)
    with path.open("xb") as output:
        output.write(content)
        output.flush()
        os.fchown(output.fileno(), 1000, 1000)
        os.fsync(output.fileno())
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def migrate_local_conversation_session(conn, manager, conversation_id: str, *, machine_id: str) -> dict:
    """由部署调用方在托管事务内调用；失败不解除局部阻断，返回值不含业务正文。"""
    conversation = conn.execute("SELECT * FROM agent_conversations WHERE id = ? FOR UPDATE", (conversation_id,)).fetchone()
    if conversation is None or conversation["session_machine_id"] != machine_id:
        raise ValueError("chat_session_machine_mismatch")
    if conn.execute("SELECT 1 FROM task_runs WHERE conversation_id = ? AND status IN ('queued', 'running')", (conversation_id,)).fetchone():
        raise ValueError("chat_session_execution_active")
    project_id, session = conversation["project_id"], conversation["session_id"]
    if not project_id or manager.traffic_execution is None:
        raise ValueError("chat_session_source_unverifiable")
    old_root = manager.traffic_execution.root.with_name(manager.traffic_execution.root.name + "-chat") / project_id
    target = manager.chat_directory(conversation_id)
    # 只接受原 Dispatcher 的原聊天挂载；不以目录存在推断执行位置。
    container = manager._client.containers.get(manager.container_name(project_id) + "-chat")
    container.reload()
    labels = container.attrs.get("Config", {}).get("Labels") or {}
    if (labels.get("cairn.pi.instance_id") != manager.instance_id or labels.get("cairn.engine") != "pi"
            or labels.get("cairn.project_id") != project_id or labels.get("cairn.scenario_key") != "project-chat"
            or container.attrs.get("State", {}).get("Status") not in {"exited", "created"}):
        raise ValueError("chat_session_source_container_unverified")
    mounts = {item["Destination"]: item["Source"] for item in container.attrs.get("Mounts", [])}
    if any(mounts.get(destination) != str(old_root / child) for child, destination in (
            ("control", "/home/kali/.cairn"), ("workspace", "/home/kali/workspace"))):
        raise ValueError("chat_session_source_mount_mismatch")
    # 两侧使用 runner 的同一排他锁；不删除遗留锁，也不停止任何执行。
    source_root = old_root / "control" / "pi"
    manifest, raw, initialized = session_evidence(source_root, session, project_id=project_id)
    writer = conn.execute("SELECT * FROM task_runs WHERE conversation_id = ? AND id = ? AND session_id = ?",
                          (conversation_id, manifest.get("last_task_run_id"), session)).fetchone()
    if writer is None or json.loads(writer["runtime_context_json"]).get("dispatcher_id") != machine_id:
        raise ValueError("chat_session_writer_unverified")
    identifier = session[3:]
    target_root = target / "control" / "pi"
    for directory in (old_root, old_root / "control", source_root, target, target / "control", target_root):
        if directory.is_symlink():
            raise ValueError("chat_session_path_invalid")
    _directory(target_root)
    locks = []

    def release():
        while locks:
            path, handle = locks.pop()
            handle.close()
            path.unlink()

    # 文件锁持续到数据库提交或回滚；非托管事务在写文件前拒绝。
    register_after_commit(conn, release)
    register_after_rollback(conn, release)
    try:
        for root in (source_root, target_root):
            path = root / (identifier + ".lock")
            handle = path.open("x")
            locks.append((path, handle))
            handle.write(json.dumps({"migration": True, "conversation_id": conversation_id, "session": session}))
            handle.flush()
        # 锁后再核对原始字节，不能使用先前验证后发生变化的摘要。
        if ((source_root / identifier / "session.jsonl").read_bytes() != raw
                or json.loads((source_root / identifier / "manifest.json").read_bytes()) != manifest):
            raise ValueError("chat_session_source_changed")
        lines = raw.splitlines(keepends=True)
        header = json.loads(lines[0])
        header["cairn"] = {"conversation_id": conversation_id, "session": session}
        migrated = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode() + b"\n" + b"".join(lines[1:])
        updated = {key: value for key, value in manifest.items() if key != "project_id"}
        binding_digest = sha256(json.dumps({"conversation_id": conversation_id}, separators=(",", ":")).encode()).hexdigest()
        updated.update(conversation_id=conversation_id, initialized=initialized, sha256=sha256(migrated).hexdigest(), binding_sha256=binding_digest)
        if "current_binding_sha256" in updated:
            updated["current_binding_sha256"] = binding_digest
        destination = target_root / identifier
        _persist(destination / "session.jsonl", migrated)
        _persist(destination / "manifest.json", json.dumps(updated, ensure_ascii=False).encode())
        workspace = old_root / "workspace" / conversation_id
        if any(path.is_symlink() for path in (workspace, *workspace.parents)):
            raise ValueError("chat_workspace_source_invalid")
        if workspace.exists():
            for source in (workspace, *workspace.rglob("*")):
                if source.is_symlink() or not (source.is_dir() or source.is_file()):
                    raise ValueError("chat_workspace_source_invalid")
                destination = target / "workspace" / conversation_id / source.relative_to(workspace)
                if source.is_file():
                    _persist(destination, source.read_bytes())
                else:
                    _directory(destination)
        conn.execute("UPDATE agent_conversations SET session_initialized = ?, unavailable_reason = NULL WHERE id = ?",
                     (initialized, conversation_id))
        return {"conversation_id": conversation_id, "session_initialized": initialized,
                "sdk_session_preserved": True, "entry_bytes_preserved": True}
    except BaseException:
        release()
        raise
