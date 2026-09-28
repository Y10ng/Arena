"""知识库文件系统服务。"""

from __future__ import annotations

from .common import *

from cairn.paths import resolve_source_relative_path


def runtime_knowledge_base_path() -> str:
    configured = os.environ.get(KNOWLEDGE_BASE_ENV_VAR, "").strip()
    if configured:
        return str(Path(configured).expanduser().resolve())
    return str(
        resolve_source_relative_path(
            DEFAULT_RUNTIME_KNOWLEDGE_BASE_PATH,
            anchor=Path(__file__),
        )
    )


def knowledge_root() -> Path:
    root = Path(runtime_knowledge_base_path()).expanduser()
    return root.resolve()


def ensure_knowledge_root() -> Path:
    root = knowledge_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def available_knowledge_set_keys() -> set[str]:
    root = ensure_knowledge_root()
    keys = {KNOWLEDGE_BASE_KEY}
    for child in root.iterdir():
        if child.is_dir() and KNOWLEDGE_KEY_RE.fullmatch(child.name):
            keys.add(child.name)
    return keys


def validate_knowledge_key(key: str, *, allow_builtin: bool = True) -> str:
    text = key.strip() if isinstance(key, str) else ""
    if allow_builtin and text == KNOWLEDGE_BASE_KEY:
        return text
    if text == KNOWLEDGE_BASE_KEY:
        raise HTTPException(422, f"{KNOWLEDGE_BASE_KEY} is reserved")
    if not text or not KNOWLEDGE_KEY_RE.fullmatch(text):
        raise HTTPException(
            422,
            "Knowledge set key must contain only lowercase letters, numbers, underscores, or hyphens",
        )
    return text


def resolve_knowledge_set_path(key: str, *, require_exists: bool = True) -> Path:
    key = validate_knowledge_key(key)
    root = ensure_knowledge_root()
    path = root if key == KNOWLEDGE_BASE_KEY else (root / key).resolve()
    # 所有知识集都必须落在 root 内，即使 key 已校验也保留路径层防护。
    if not _path_is_within(path, root):
        raise HTTPException(400, "Knowledge set path escapes the knowledge root")
    if require_exists and (not path.exists() or not path.is_dir()):
        raise HTTPException(404, f"Knowledge set {key} not found")
    return path


def resolve_knowledge_file_path(
    key: str,
    name: str,
    *,
    require_exists: bool = False,
) -> tuple[Path, str]:
    set_path = resolve_knowledge_set_path(key)
    relative_name = validate_knowledge_file_name(name)
    if key == KNOWLEDGE_BASE_KEY and "/" in relative_name:
        # 根知识库只暴露顶层文件，避免和子知识集目录的管理边界混在一起。
        raise HTTPException(422, f"{KNOWLEDGE_BASE_KEY} files must live at the knowledge root")
    file_path = (set_path / relative_name).resolve()
    if not _path_is_within(file_path, set_path):
        raise HTTPException(400, "Knowledge file path escapes the knowledge set")
    if require_exists and (not file_path.exists() or not file_path.is_file()):
        raise HTTPException(404, f"Knowledge file {relative_name} not found")
    return file_path, relative_name


def validate_knowledge_file_name(name: str) -> str:
    text = name.strip().replace("\\", "/") if isinstance(name, str) else ""
    if not text or text.startswith("/") or text.endswith("/"):
        raise HTTPException(422, "Knowledge file name must be a relative file path")
    parts = [part for part in text.split("/") if part]
    if "/".join(parts) != text or any(part in {".", ".."} for part in parts):
        # 允许子目录，但禁止空段、当前目录和父目录段。
        raise HTTPException(422, "Knowledge file name must not contain empty, dot, or parent segments")
    for part in parts:
        if not KNOWLEDGE_FILE_SEGMENT_RE.fullmatch(part):
            raise HTTPException(
                422,
                "Knowledge file path segments must contain only letters, numbers, dots, underscores, or hyphens",
            )
    suffix = Path(parts[-1]).suffix.lower()
    if suffix not in KNOWLEDGE_FILE_EXTENSIONS:
        raise HTTPException(
            422,
            "Knowledge file extension must be one of: " + ", ".join(sorted(KNOWLEDGE_FILE_EXTENSIONS)),
        )
    return "/".join(parts)


def knowledge_set_exists(key: str) -> bool:
    key = validate_knowledge_key(key)
    return key in available_knowledge_set_keys()


def list_knowledge_sets(conn: sqlite3.Connection) -> list[KnowledgeSet]:
    root = ensure_knowledge_root()
    referenced = _knowledge_references(conn)
    keys = available_knowledge_set_keys()
    return [
        _knowledge_set_model(key, root if key == KNOWLEDGE_BASE_KEY else root / key, referenced)
        for key in sorted(keys, key=lambda value: (value != KNOWLEDGE_BASE_KEY, value))
    ]


def create_knowledge_set(conn: sqlite3.Connection, key: str) -> KnowledgeSet:
    key = validate_knowledge_key(key, allow_builtin=False)
    root = ensure_knowledge_root()
    path = (root / key).resolve()
    if not _path_is_within(path, root):
        raise HTTPException(400, "Knowledge set path escapes the knowledge root")
    if path.exists() and not path.is_dir():
        raise HTTPException(409, f"Knowledge set path {key} already exists as a file")
    path.mkdir(parents=True, exist_ok=True)
    return _knowledge_set_model(key, path, _knowledge_references(conn))


def delete_knowledge_set(conn: sqlite3.Connection, key: str) -> None:
    key = validate_knowledge_key(key, allow_builtin=False)
    referenced_by = sorted(_knowledge_references(conn).get(key, []))
    if referenced_by:
        raise HTTPException(
            409,
            f"Cannot delete knowledge set {key}; referenced by scenarios: " + ", ".join(referenced_by),
        )
    path = resolve_knowledge_set_path(key, require_exists=False)
    if not path.exists():
        return
    if not path.is_dir():
        raise HTTPException(409, f"Knowledge set path {key} is not a directory")
    shutil.rmtree(path)


def list_knowledge_files(key: str) -> list[KnowledgeFile]:
    set_path = resolve_knowledge_set_path(key)
    child_set_paths = _knowledge_child_set_paths(set_path) if key == KNOWLEDGE_BASE_KEY else set()
    files: list[KnowledgeFile] = []
    for path in sorted(item for item in set_path.rglob("*") if item.is_file()):
        if any(_path_is_within(path, child_set_path) for child_set_path in child_set_paths):
            # 列根知识库时跳过子知识集内部文件，防止同一文件被两个知识集重复管理。
            continue
        try:
            relative = path.relative_to(set_path).as_posix()
        except ValueError:
            continue
        try:
            normalized = validate_knowledge_file_name(relative)
        except HTTPException:
            continue
        files.append(_knowledge_file_model(path, normalized))
    return files


def read_knowledge_file(key: str, name: str) -> KnowledgeFileContent:
    path, relative_name = resolve_knowledge_file_path(key, name, require_exists=True)
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, f"Knowledge file {relative_name} is not valid UTF-8") from exc
    file_model = _knowledge_file_model(path, relative_name, content=content)
    return KnowledgeFileContent(**file_model.model_dump(), content=content)


def write_knowledge_file(key: str, name: str, content: str) -> KnowledgeFileContent:
    path, relative_name = resolve_knowledge_file_path(key, name, require_exists=False)
    if path.exists() and not path.is_file():
        raise HTTPException(409, f"Knowledge file path {relative_name} is not a file")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return read_knowledge_file(key, relative_name)


def delete_knowledge_file(key: str, name: str) -> None:
    path, _relative_name = resolve_knowledge_file_path(key, name, require_exists=False)
    if not path.exists():
        return
    if not path.is_file():
        raise HTTPException(409, "Knowledge file path is not a file")
    path.unlink()
    _remove_empty_parent_dirs(path.parent, resolve_knowledge_set_path(key))


def _knowledge_references(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute("SELECT key, knowledge FROM scenarios ORDER BY key").fetchall()
    references: dict[str, list[str]] = {}
    for row in rows:
        knowledge = str(row["knowledge"] or "").strip()
        scenario_key = str(row["key"] or "").strip()
        if not knowledge or not scenario_key:
            continue
        references.setdefault(knowledge, []).append(scenario_key)
    return references


def _knowledge_set_model(
    key: str,
    path: Path,
    references: dict[str, list[str]],
) -> KnowledgeSet:
    return KnowledgeSet(
        key=key,
        path=str(path),
        builtin=key == KNOWLEDGE_BASE_KEY,
        file_count=_knowledge_file_count(path, skip_child_sets=key == KNOWLEDGE_BASE_KEY),
        referenced_by=sorted(references.get(key, [])),
    )


def _knowledge_file_count(path: Path, *, skip_child_sets: bool = False) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    child_set_paths = _knowledge_child_set_paths(path) if skip_child_sets else set()
    count = 0
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        if any(_path_is_within(item, child_set_path) for child_set_path in child_set_paths):
            continue
        try:
            validate_knowledge_file_name(item.relative_to(path).as_posix())
        except HTTPException:
            continue
        count += 1
    return count


def _knowledge_child_set_paths(path: Path) -> set[Path]:
    return {
        child.resolve()
        for child in path.iterdir()
        if child.is_dir() and KNOWLEDGE_KEY_RE.fullmatch(child.name)
    }


def _knowledge_file_model(
    path: Path,
    relative_name: str,
    *,
    content: str | None = None,
) -> KnowledgeFile:
    stat = path.stat()
    if content is None:
        warnings: list[str] = []
    else:
        warnings = _knowledge_file_warnings(relative_name, content)
    return KnowledgeFile(
        name=Path(relative_name).name,
        path=relative_name,
        size=stat.st_size,
        updated_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        warnings=warnings,
    )


def _knowledge_file_warnings(name: str, content: str) -> list[str]:
    if Path(name).suffix.lower() != ".md" or not content.startswith("---"):
        return []
    # Markdown front matter 只做轻量提示，不阻止保存；严格语义留给知识库维护者。
    end = content.find("\n---", 3)
    if end == -1:
        return ["YAML front matter start marker found without a closing marker"]
    front_matter = content[3:end].strip()
    if not front_matter:
        return []
    try:
        import yaml

        parsed = yaml.safe_load(front_matter)
    except Exception:
        return ["YAML front matter could not be parsed"]
    if parsed is not None and not isinstance(parsed, dict):
        return ["YAML front matter should be an object"]
    return []


def _remove_empty_parent_dirs(path: Path, stop_at: Path) -> None:
    stop_at = stop_at.resolve()
    current = path.resolve()
    while current != stop_at and _path_is_within(current, stop_at):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
