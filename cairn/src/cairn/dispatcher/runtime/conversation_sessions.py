"""只读取持久 SDK 证据；缺文件、锁或摘要不符均为未知，不推断从未初始化。"""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from uuid import UUID


def session_evidence(root: Path, session: str, *, conversation_id: str | None = None,
                     project_id: str | None = None, cwd: str = "/home/kali/workspace") -> tuple[dict, bytes, bool]:
    if (conversation_id is None) == (project_id is None) or not session.startswith("pi:"):
        raise ValueError("chat_session_identity_invalid")
    identifier = session[3:]
    if str(UUID(identifier)) != identifier:
        raise ValueError("chat_session_identity_invalid")
    directory = root / identifier
    if (root / (identifier + ".lock")).exists():
        raise ValueError("chat_session_locked")
    for path in (*root.parents, root, directory, directory / "manifest.json", directory / "session.jsonl"):
        if path.is_symlink():
            raise ValueError("chat_session_path_invalid")
    binding_digest = sha256(json.dumps({"conversation_id": conversation_id} if conversation_id is not None else {}, separators=(",", ":")).encode()).hexdigest()
    manifest = json.loads((directory / "manifest.json").read_bytes())
    raw = (directory / "session.jsonl").read_bytes()
    if (not isinstance(manifest, dict) or manifest.get("protocol") != "cairn-pi-session-v1" or manifest.get("session") != session
            or manifest.get("project_id") != project_id or manifest.get("conversation_id") != conversation_id
            or manifest.get("cwd") != cwd or manifest.get("phase_family") != "chat"
            or manifest.get("binding_sha256") != binding_digest
            or manifest.get("current_binding_sha256", binding_digest) != binding_digest
            or manifest.get("sha256") != sha256(raw).hexdigest() or not raw.endswith(b"\n")):
        raise ValueError("chat_session_ownership_invalid")
    entries = [json.loads(line) for line in raw.splitlines()]
    header = entries[0]
    owner = {"conversation_id": conversation_id} if conversation_id is not None else {"project_id": project_id}
    if (not isinstance(header, dict) or header.get("type") != "session" or header.get("version") != 3
            or header.get("id") != manifest.get("sdk_session_id") or not header.get("id")
            or header.get("cwd") != cwd or header.get("cairn") != {**owner, "session": session}):
        raise ValueError("chat_session_header_invalid")
    seen = set()
    initialized = False
    for entry in entries[1:]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or entry["id"] in seen
                or not isinstance(entry.get("timestamp"), str) or "parentId" not in entry
                or entry["parentId"] is not None and (not isinstance(entry["parentId"], str) or entry["parentId"] not in seen)
                or entry.get("type") not in {"message", "thinking_level_change", "model_change", "compaction", "branch_summary", "custom", "custom_message", "label", "session_info"}):
            raise ValueError("chat_session_chain_invalid")
        seen.add(entry["id"])
        if entry["type"] == "message":
            if not isinstance(entry.get("message"), dict) or entry["message"].get("role") not in {"user", "assistant", "toolResult", "bashExecution", "custom"}:
                raise ValueError("chat_session_message_invalid")
            initialized |= entry["message"]["role"] == "assistant"
    if conversation_id is not None and manifest.get("initialized") is not initialized:
        raise ValueError("chat_session_initialization_invalid")
    return manifest, raw, initialized


def conversation_initialized(manager, conversation_id: str, session: str) -> bool | None:
    try:
        return session_evidence(manager.chat_directory(conversation_id) / "control" / "pi", session,
                                conversation_id=conversation_id)[2]
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None
