"""资料库当前文件与项目／计划实际使用的不可变资料。"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
from re import fullmatch
import sqlite3
from tempfile import NamedTemporaryFile

from fastapi import HTTPException

from cairn.advisory_contract import selection_sha256
from cairn.server.schemas.libraries import (
    Library,
    LibraryBinding,
    LibraryFile,
    LibraryFileContent,
    LibraryFileWrite,
    LibraryRevisionRef,
    LibrarySelection,
    LibraryVersion,
    SelectableLibraryManifest,
)


ADVISORY_LIBRARY_ROOT_ENV_VAR = "CAIRN_ADVISORY_LIBRARY_ROOT"
DEFAULT_ADVISORY_LIBRARY_ROOT = "datas/cairn/advisory-library-content"
ROOT_LIBRARY_KEY = "knowledge_base"
SAFETY_FILE_PATH = "执行安全约束.md"


def safety_file_path(key: str) -> str | None:
    return SAFETY_FILE_PATH if key == ROOT_LIBRARY_KEY else None


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def library_content_root() -> Path:
    configured = os.environ.get(ADVISORY_LIBRARY_ROOT_ENV_VAR, "").strip()
    return Path(configured or DEFAULT_ADVISORY_LIBRARY_ROOT).expanduser().resolve()


def library_storage_path(storage_key: str) -> Path:
    if not fullmatch(r"[0-9a-f]{64}", storage_key):
        raise RuntimeError("Invalid advisory library storage key")
    return library_content_root() / storage_key[:2] / storage_key[2:]


def _store_content(content: bytes) -> str:
    storage_key = sha256(content).hexdigest()
    destination = library_storage_path(storage_key)
    if destination.exists():
        existing = destination.read_bytes()
        if existing != content:
            raise RuntimeError(f"Advisory library storage collision: {storage_key}")
        return storage_key
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return storage_key


def validate_library_key(value: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not fullmatch(r"[a-z0-9_-]{1,128}", text):
        raise HTTPException(
            422,
            "Library key must contain only lowercase letters, numbers, underscores, or hyphens",
        )
    return text


def validate_library_path(value: str) -> str:
    text = value.replace("\\", "/") if isinstance(value, str) else ""
    if not text or text.startswith("/") or text.endswith("/") or len(text) > 512:
        raise HTTPException(422, "Library file path must be a relative path of at most 512 characters")
    parts = text.split("/")
    if any(not part or part in {".", ".."} or len(part) > 255 for part in parts):
        raise HTTPException(422, "Library file path contains an invalid segment")
    if any(any(ord(char) < 32 or ord(char) == 127 for char in part) for part in parts):
        raise HTTPException(422, "Library file path must not contain control characters")
    return "/".join(parts)


def list_libraries(conn: sqlite3.Connection, *, include_archived: bool = False) -> list[Library]:
    where = "" if include_archived else "WHERE library.archived_at IS NULL"
    rows = conn.execute(
        f"""
        SELECT library.key, library.name, library.created_at, library.archived_at,
               (SELECT COUNT(*) FROM advisory_library_current_files AS file
                WHERE file.library_key = library.key) AS file_count
        FROM advisory_libraries AS library
        {where}
        ORDER BY library.key
        """
    ).fetchall()
    return [_library_model(row) for row in rows]


def get_library(conn: sqlite3.Connection, key: str, *, include_archived: bool = False) -> Library:
    libraries = [item for item in list_libraries(conn, include_archived=include_archived) if item.key == key]
    if not libraries:
        raise HTTPException(404, f"Library {key} not found")
    return libraries[0]


def create_library(conn: sqlite3.Connection, key: str, name: str | None = None) -> Library:
    key = validate_library_key(key)
    row = conn.execute("SELECT key FROM advisory_libraries WHERE key = ?", (key,)).fetchone()
    if row is not None:
        raise HTTPException(409, f"Library {key} already exists")
    conn.execute(
        "INSERT INTO advisory_libraries (key, name, created_at) VALUES (?, ?, ?)",
        (key, name or key, _utcnow()),
    )
    return get_library(conn, key)


def archive_library(conn: sqlite3.Connection, key: str) -> None:
    if key == ROOT_LIBRARY_KEY:
        raise HTTPException(409, "根目录不可归档或删除")
    conn.execute("UPDATE advisory_libraries SET key = key WHERE key = ?", (key,))
    row = conn.execute("SELECT archived_at FROM advisory_libraries WHERE key = ?", (key,)).fetchone()
    if row is None:
        return
    binding = conn.execute(
        "SELECT project_id FROM project_advisory_library_bindings WHERE library_key = ? LIMIT 1",
        (key,),
    ).fetchone()
    if binding is not None:
        raise HTTPException(409, f"Library {key} is bound to project {binding['project_id']}")
    schedule_binding = conn.execute(
        "SELECT schedule_id FROM project_schedule_libraries WHERE library_key = ? LIMIT 1",
        (key,),
    ).fetchone()
    if schedule_binding is not None:
        raise HTTPException(409, f"Library {key} is bound to schedule {schedule_binding['schedule_id']}")
    if row["archived_at"] is None:
        conn.execute("UPDATE advisory_libraries SET archived_at = ? WHERE key = ?", (_utcnow(), key))


def list_selectable_current_libraries(conn: sqlite3.Connection) -> list[Library]:
    return list_libraries(conn)


def get_selectable_library_manifest(conn, key: str) -> SelectableLibraryManifest:
    """当前集合与不可变使用快照独立；读取清单不产生快照。"""
    normalized = validate_library_key(key)
    rows = conn.execute(
        """SELECT library.key, library.name, file.path, file.size_bytes, file.sha256
           FROM advisory_libraries AS library
           LEFT JOIN advisory_library_current_files AS file ON file.library_key = library.key
           WHERE library.key = ? AND library.archived_at IS NULL ORDER BY file.path""",
        (normalized,),
    ).fetchall()
    if not rows:
        raise HTTPException(404, f"Selectable library {normalized} not found")
    files = [LibraryFile(path=row["path"], size_bytes=row["size_bytes"], sha256=row["sha256"])
             for row in rows if row["path"] is not None]
    return SelectableLibraryManifest(
        library_key=normalized, library_name=rows[0]["name"],
        file_count=len(files), files=files, safety_path=safety_file_path(normalized),
    )


def resolve_library_selections(conn, selections: list[LibrarySelection], *, tenant_id: str, user_id: str,
                               existing: list[LibraryRevisionRef] | None = None) -> list[LibraryRevisionRef]:
    if not tenant_id or not user_id:
        raise HTTPException(403, "Library selection requires a tenant and user")
    previous = {item.library_key: item for item in existing or []}
    resolved = []
    # 普通管理写入与提交都锁定库，核对摘要与保存所选字节之间不能穿插编辑。
    for key in sorted({item.library_key for item in selections}):
        _lock_library_for_binding(conn, key)
    for selection in selections:
        key = selection.library_key
        if selection.source == "saved":
            prior = previous.get(key)
            if prior is None:
                raise HTTPException(403, "只能保留当前计划自己的已保存资料")
            prior_files = _selected_revision_files(conn, prior)
            prior_paths = {item.path for item in prior_files}
            paths = prior_paths if selection.paths is None else set(selection.paths)
            if not paths.issubset(prior_paths):
                raise HTTPException(403, "增加资料请明确更新为当前资料")
            files = [item.model_dump() for item in prior_files if item.path in paths]
            resolved.append(prior.model_copy(update={
                "paths": prior.paths if selection.paths is None else sorted(paths),
                "selection_sha256": selection_sha256(key, prior.version, prior.digest_sha256, files),
            }))
            continue
        manifest = get_selectable_library_manifest(conn, key)
        available = {item.path: item.sha256 for item in manifest.files}
        paths = set(available) if selection.paths is None else set(selection.paths)
        actual = {path: available[path] for path in paths if path in available}
        if not paths or set(actual) != paths or actual != selection.expected_files:
            raise HTTPException(409, {
                "code": "library_selection_conflict", "library_key": key,
                "message": "所选资料已变化，请更新资料后重新提交；其他输入已保留。",
            })
        contents = [LibraryFileWrite(path=path, content=read_library_file(conn, key, path).content)
                    for path in sorted(paths)]
        snapshot = _create_library_snapshot(conn, key, contents, created_by=user_id)
        files = [item.model_dump() for item in list_library_files(conn, key, snapshot.version)]
        resolved.append(LibraryRevisionRef(
            library_key=key, version=snapshot.version, digest_sha256=snapshot.digest_sha256,
            paths=None if selection.paths is None else sorted(paths),
            selection_sha256=selection_sha256(key, snapshot.version, snapshot.digest_sha256, files),
        ))
    return resolved


def bind_library_revision(
    conn: sqlite3.Connection,
    project_id: str,
    revision: LibraryRevisionRef,
) -> LibraryBinding:
    project_id = project_id.strip()
    if not project_id:
        raise HTTPException(422, "Library binding project_id is required")
    _lock_library_for_binding(conn, revision.library_key)
    _check_library_revision(conn, revision)
    _selected_revision_files(conn, revision)
    created_at = _utcnow()
    conn.execute(
        """INSERT INTO project_advisory_library_bindings
           (project_id, library_key, version, created_at, paths_json, selection_sha256)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (project_id, revision.library_key, revision.version, created_at,
         json.dumps(revision.paths, ensure_ascii=False) if revision.paths is not None else None,
         revision.selection_sha256),
    )
    return LibraryBinding(
        project_id=project_id, library_key=revision.library_key, version=revision.version,
        created_at=created_at, paths=revision.paths, selection_sha256=revision.selection_sha256,
    )


def _lock_library_for_binding(conn: sqlite3.Connection, key: str) -> None:
    # 归档与新增绑定串行，避免归档检查后又提交新的计划引用。
    conn.execute("UPDATE advisory_libraries SET key = key WHERE key = ?", (key,))
    get_library(conn, key)


def _check_library_revision(conn: sqlite3.Connection, revision: LibraryRevisionRef) -> None:
    selected = get_library_version(conn, revision.library_key, revision.version)
    if selected.digest_sha256 != revision.digest_sha256:
        raise HTTPException(409, f"Library {revision.library_key} version {revision.version} digest mismatch")


def bind_schedule_library_revision(
    conn: sqlite3.Connection,
    schedule_id: str,
    revision: LibraryRevisionRef,
) -> LibraryRevisionRef:
    _lock_library_for_binding(conn, revision.library_key)
    _check_library_revision(conn, revision)
    _selected_revision_files(conn, revision)
    conn.execute(
        """INSERT INTO project_schedule_libraries
           (schedule_id, library_key, version, digest_sha256, created_at, paths_json, selection_sha256)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (schedule_id, revision.library_key, revision.version, revision.digest_sha256, _utcnow(),
         json.dumps(revision.paths, ensure_ascii=False) if revision.paths is not None else None,
         revision.selection_sha256),
    )
    return revision


def list_schedule_library_revisions(
    conn: sqlite3.Connection,
    schedule_id: str,
) -> list[LibraryRevisionRef]:
    rows = conn.execute(
        """SELECT library_key, version, digest_sha256, paths_json, selection_sha256
           FROM project_schedule_libraries WHERE schedule_id = ? ORDER BY library_key""",
        (schedule_id,),
    ).fetchall()
    revisions = [LibraryRevisionRef(
        library_key=row["library_key"], version=row["version"], digest_sha256=row["digest_sha256"],
        paths=_stored_paths(row["paths_json"]), selection_sha256=row["selection_sha256"],
    ) for row in rows]
    for revision in revisions:
        _check_library_revision(conn, revision)
        _selected_revision_files(conn, revision)
    return revisions


def validate_library_revision_content(
    conn: sqlite3.Connection,
    revision: LibraryRevisionRef,
) -> None:
    """触发前校验固定版本原件，不把全库读盘放进详情列表。"""
    _check_library_revision(conn, revision)
    digest = sha256()
    for file in _selected_revision_files(conn, revision):
        try:
            content = read_library_revision_content(conn, revision, file.path)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(
                409, f"Library {revision.library_key} version {revision.version} content integrity check failed"
            ) from exc
        _update_library_digest(digest, file.path, content)
    if revision.paths is None and digest.hexdigest() != revision.digest_sha256:
        raise HTTPException(409, f"Library {revision.library_key} version {revision.version} digest mismatch")


def list_library_bindings(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[LibraryBinding]:
    rows = conn.execute(
        """SELECT project_id, library_key, version, created_at, paths_json, selection_sha256
           FROM project_advisory_library_bindings
           WHERE project_id = ? ORDER BY library_key""",
        (project_id,),
    ).fetchall()
    return [LibraryBinding(project_id=row["project_id"], library_key=row["library_key"],
                           version=row["version"], created_at=row["created_at"],
                           paths=_stored_paths(row["paths_json"]), selection_sha256=row["selection_sha256"])
            for row in rows]


def get_bound_library_revision(
    conn: sqlite3.Connection,
    project_id: str,
    key: str,
    version: int,
) -> LibraryRevisionRef:
    """只解析项目已经冻结的 revision，避免 Dispatcher 越界读取其他资料。"""
    normalized_key = validate_library_key(key)
    row = conn.execute(
        """SELECT version, paths_json, selection_sha256 FROM project_advisory_library_bindings
           WHERE project_id = ? AND library_key = ? AND version = ?""",
        (project_id, normalized_key, version),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Project advisory library binding not found")
    selected = get_library_version(conn, normalized_key, version)
    return LibraryRevisionRef(
        library_key=normalized_key,
        version=selected.version,
        digest_sha256=selected.digest_sha256,
        paths=_stored_paths(row["paths_json"]), selection_sha256=row["selection_sha256"],
    )


def list_library_versions(conn: sqlite3.Connection, key: str) -> list[LibraryVersion]:
    get_library(conn, key, include_archived=True)
    rows = conn.execute(
        """
        SELECT version.*, COUNT(file.path) AS file_count
        FROM advisory_library_versions AS version
        LEFT JOIN advisory_library_files AS file
          ON file.library_key = version.library_key AND file.version = version.version
        WHERE version.library_key = ?
        GROUP BY version.library_key, version.version, version.digest_sha256, version.source,
                 version.message, version.created_by, version.created_at
        ORDER BY version.version DESC
        """,
        (key,),
    ).fetchall()
    return [_version_model(row) for row in rows]


def get_library_version(conn: sqlite3.Connection, key: str, version: int | None = None) -> LibraryVersion:
    versions = list_library_versions(conn, key)
    if version is None:
        if not versions:
            raise HTTPException(404, f"Library {key} has no versions")
        return versions[0]
    for item in versions:
        if item.version == version:
            return item
    raise HTTPException(404, f"Library {key} version {version} not found")


def _create_library_snapshot(
    conn: sqlite3.Connection,
    key: str,
    files: list[LibraryFileWrite],
    *,
    source: str = "api",
    message: str | None = None,
    created_by: str | None = None,
) -> LibraryVersion:
    get_library(conn, key)
    normalized: dict[str, bytes] = {}
    for item in files:
        path = validate_library_path(item.path)
        if path in normalized:
            raise HTTPException(422, f"Duplicate library file path: {path}")
        normalized[path] = item.content.encode("utf-8")
    digest = _library_digest(normalized)
    existing = conn.execute(
        """SELECT version FROM advisory_library_versions
           WHERE library_key = ? AND digest_sha256 = ? ORDER BY version DESC LIMIT 1""",
        (key, digest),
    ).fetchone()
    if existing is not None:
        return get_library_version(conn, key, int(existing["version"]))
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM advisory_library_versions WHERE library_key = ?",
        (key,),
    ).fetchone()
    version = int(row["next_version"])
    conn.execute(
        """INSERT INTO advisory_library_versions
           (library_key, version, digest_sha256, source, message, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (key, version, digest, source, message, created_by, _utcnow()),
    )
    for path, content in sorted(normalized.items()):
        content_sha256 = sha256(content).hexdigest()
        storage_key = _store_content(content)
        conn.execute(
            """INSERT INTO advisory_library_files
               (library_key, version, path, storage_key, size_bytes, sha256)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (key, version, path, storage_key, len(content), content_sha256),
        )
    return get_library_version(conn, key, version)


def list_library_files(conn: sqlite3.Connection, key: str, version: int | None = None) -> list[LibraryFile]:
    if version is None:
        return get_selectable_library_manifest(conn, key).files
    selected = get_library_version(conn, key, version)
    rows = conn.execute(
        """SELECT path, size_bytes, sha256 FROM advisory_library_files
           WHERE library_key = ? AND version = ? ORDER BY path""",
        (key, selected.version),
    ).fetchall()
    return [LibraryFile(path=row["path"], size_bytes=row["size_bytes"], sha256=row["sha256"]) for row in rows]


def read_library_file(
    conn: sqlite3.Connection,
    key: str,
    path: str,
    version: int | None = None,
) -> LibraryFileContent:
    normalized = validate_library_path(path)
    if version is None:
        get_library(conn, key)
        row = conn.execute(
            """SELECT path, content, size_bytes, sha256 FROM advisory_library_current_files
               WHERE library_key = ? AND path = ?""", (key, normalized),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Library file {normalized} not found")
        return LibraryFileContent(**dict(row))
    selected = get_library_version(conn, key, version)
    row = conn.execute(
        """SELECT path, storage_key, size_bytes, sha256 FROM advisory_library_files
           WHERE library_key = ? AND version = ? AND path = ?""",
        (key, selected.version, normalized),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"Library file {normalized} not found")
    storage_path = library_storage_path(row["storage_key"])
    try:
        content = storage_path.read_bytes()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Advisory library content is missing: {row['storage_key']}") from exc
    if len(content) != row["size_bytes"]:
        raise RuntimeError(f"Advisory library size mismatch: {normalized}")
    if sha256(content).hexdigest() != row["sha256"]:
        raise RuntimeError(f"Advisory library sha256 mismatch: {normalized}")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Stored library file {normalized} is not valid UTF-8") from exc
    return LibraryFileContent(path=normalized, content=text, size_bytes=row["size_bytes"], sha256=row["sha256"])


def _stored_paths(value: str | None) -> list[str] | None:
    if value is None:
        return None
    try:
        paths = json.loads(value)
        if not isinstance(paths, list):
            raise ValueError("paths must be an array")
        validated = LibrarySelection(library_key="stored", source="saved", paths=paths).paths
        if paths != validated:
            raise ValueError("noncanonical paths")
        return validated
    except (TypeError, ValueError) as exc:
        raise HTTPException(409, "Stored library selection is invalid") from exc


def _selected_revision_files(conn, revision: LibraryRevisionRef) -> list[LibraryFile]:
    all_files = sorted(list_library_files(conn, revision.library_key, revision.version), key=lambda item: item.path)
    paths = set(revision.paths) if revision.paths is not None else None
    selected = all_files if paths is None else [item for item in all_files if item.path in paths]
    if paths is not None and {item.path for item in selected} != paths:
        raise HTTPException(409, "Stored library selection contains missing paths")
    digest = selection_sha256(revision.library_key, revision.version, revision.digest_sha256,
                              [item.model_dump() for item in selected])
    if not revision.selection_sha256 or not hmac.compare_digest(digest, revision.selection_sha256):
        raise HTTPException(409, "Library selection digest mismatch")
    return selected


def list_library_revision_files(
    conn: sqlite3.Connection,
    revision: LibraryRevisionRef,
) -> list[LibraryFile]:
    return _selected_revision_files(conn, revision)


def read_library_revision_content(
    conn: sqlite3.Connection,
    revision: LibraryRevisionRef,
    path: str,
) -> bytes:
    normalized = validate_library_path(path)
    if normalized not in {item.path for item in _selected_revision_files(conn, revision)}:
        raise HTTPException(404, "Project advisory library file not found")
    item = read_library_file(conn, revision.library_key, normalized, revision.version)
    # Dispatcher 需要原始 UTF-8 字节，不能经过预览、脱敏或语义清洗。
    return item.content.encode("utf-8")


def replace_library_file(
    conn: sqlite3.Connection,
    key: str,
    path: str,
    content: str,
    *,
    created_by: str | None = None,
) -> LibraryFileContent:
    normalized = validate_library_path(path)
    _lock_library_for_binding(conn, key)
    data = content.encode("utf-8")
    conn.execute(
        """INSERT INTO advisory_library_current_files (library_key, path, content, size_bytes, sha256)
           VALUES (?, ?, ?, ?, ?) ON CONFLICT (library_key, path) DO UPDATE SET
           content = excluded.content, size_bytes = excluded.size_bytes, sha256 = excluded.sha256""",
        (key, normalized, content, len(data), sha256(data).hexdigest()),
    )
    return read_library_file(conn, key, normalized)


def remove_library_file(conn: sqlite3.Connection, key: str, path: str, *, created_by: str | None = None) -> None:
    normalized = validate_library_path(path)
    if normalized == safety_file_path(key):
        raise HTTPException(409, "系统文件不可删除，路径不可更改")
    _lock_library_for_binding(conn, key)
    conn.execute("DELETE FROM advisory_library_current_files WHERE library_key = ? AND path = ?", (key, normalized))


def replace_library_files(conn, key: str, files: list[LibraryFileWrite]) -> SelectableLibraryManifest:
    normalized = {validate_library_path(item.path): item.content for item in files}
    if len(normalized) != len(files):
        raise HTTPException(422, "资料路径重复")
    if key == ROOT_LIBRARY_KEY and SAFETY_FILE_PATH not in normalized:
        raise HTTPException(409, "系统文件不可删除，完整替换必须保留执行安全约束.md")
    _lock_library_for_binding(conn, key)
    for item in list_library_files(conn, key):
        if item.path not in normalized:
            remove_library_file(conn, key, item.path)
    for path, content in normalized.items():
        replace_library_file(conn, key, path, content)
    return get_selectable_library_manifest(conn, key)


def initialize_root_library(conn) -> None:
    """仅补缺失的固定正文，重复启动不覆盖管理员编辑。"""
    conn.execute(
        """INSERT INTO advisory_libraries (key, name, created_at) VALUES (?, ?, ?)
           ON CONFLICT (key) DO UPDATE SET archived_at = NULL""",
        (ROOT_LIBRARY_KEY, "根目录", _utcnow()),
    )
    content = (Path(__file__).parent.parent / "resources" / "execution-safety.md").read_text(encoding="utf-8")
    data = content.encode("utf-8")
    conn.execute(
        """INSERT INTO advisory_library_current_files (library_key, path, content, size_bytes, sha256)
           VALUES (?, ?, ?, ?, ?) ON CONFLICT (library_key, path) DO NOTHING""",
        (ROOT_LIBRARY_KEY, SAFETY_FILE_PATH, content, len(data), sha256(data).hexdigest()),
    )


def import_legacy_knowledge(conn: sqlite3.Connection, root: Path) -> list[Library]:
    root = root.expanduser().resolve()
    if not root.exists():
        return []
    keys = ["knowledge_base"] + sorted(
        child.name
        for child in root.iterdir()
        if child.is_dir()
        and not child.is_symlink()
        and fullmatch(r"[a-z0-9_-]{1,128}", child.name)
    )
    imported: list[Library] = []
    child_roots = {(root / key).resolve() for key in keys if key != "knowledge_base"}
    for key in keys:
        source_root = root if key == "knowledge_base" else (root / key).resolve()
        existing = conn.execute(
            "SELECT archived_at FROM advisory_libraries WHERE key = ?", (key,)
        ).fetchone()
        if existing is None:
            create_library(conn, key, key)
        else:
            # 数据库一旦拥有这个库，旧磁盘目录不再是权威来源（空库也一样）。
            continue
        files: list[LibraryFileWrite] = []
        for path in sorted(source_root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            if key == "knowledge_base" and any(resolved.is_relative_to(child) for child in child_roots):
                continue
            relative = path.relative_to(source_root).as_posix()
            try:
                normalized = validate_library_path(relative)
                content = path.read_bytes().decode("utf-8")
            except (HTTPException, UnicodeDecodeError):
                continue
            files.append(LibraryFileWrite(path=normalized, content=content))
        for item in files:
            replace_library_file(conn, key, item.path, item.content)
        imported.append(get_library(conn, key))
    return imported


def _update_library_digest(digest, path: str, content: bytes) -> None:
    path_bytes = path.encode("utf-8")
    digest.update(len(path_bytes).to_bytes(8, "big"))
    digest.update(path_bytes)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)


def _library_digest(files: dict[str, bytes]) -> str:
    digest = sha256()
    for path, content in sorted(files.items()):
        _update_library_digest(digest, path, content)
    return digest.hexdigest()


def _library_model(row) -> Library:
    return Library(
        key=row["key"],
        name=row["name"],
        builtin=row["key"] == ROOT_LIBRARY_KEY,
        file_count=row["file_count"],
        created_at=row["created_at"],
        archived_at=row["archived_at"],
    )


def _version_model(row) -> LibraryVersion:
    return LibraryVersion(
        library_key=row["library_key"],
        version=row["version"],
        digest_sha256=row["digest_sha256"],
        source=row["source"],
        message=row["message"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        file_count=row["file_count"],
    )


__all__ = [name for name in globals() if not name.startswith("_")]
