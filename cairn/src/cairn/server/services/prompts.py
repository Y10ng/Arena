"""Prompt 模板组服务。"""

from __future__ import annotations

import tempfile

from .common import *

def prompts_root() -> Path:
    return ensure_prompts_root()


def available_prompt_groups() -> set[str]:
    return dispatcher_available_prompt_groups()


def visible_exploration_prompt_groups() -> set[str]:
    return {
        info.key
        for info in list_prompt_group_infos()
        if info.visibility == PROMPT_VISIBILITY_VISIBLE
        and PROMPT_CAPABILITY_EXPLORATION in info.capabilities
    }


def validate_prompt_group_key(key: str) -> str:
    try:
        return validate_prompt_group_key_value(key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def validate_prompt_file_name(name: str) -> str:
    try:
        return validate_prompt_file_name_value(name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def list_prompt_groups(conn: sqlite3.Connection) -> list[PromptGroup]:
    references = _prompt_group_references(conn)
    return [
        _prompt_group_model(info, references)
        for info in list_prompt_group_infos()
    ]


def create_prompt_group(conn: sqlite3.Connection, key: str, clone_from: str | None = None) -> PromptGroup:
    key = validate_prompt_group_key(key)
    if key in RETIRED_PROMPT_GROUP_KEYS:
        raise HTTPException(422, f"Prompt group {key} has been retired")
    default_source = key if prompt_group_exists(key, builtin_only=True) else DEFAULT_PROMPT_GROUP
    source = validate_prompt_group_key(clone_from or default_source)
    if not prompt_group_exists(source):
        raise HTTPException(404, f"Prompt group {source} not found")

    # 受管模板组写到 CAIRN_PROMPTS_ROOT；内置模板始终只读，可通过克隆生成可编辑副本。
    path = _managed_prompt_group_path_http(key, require_exists=False)
    if path.exists():
        if path.is_dir():
            raise HTTPException(409, f"Prompt group {key} already exists")
        raise HTTPException(409, f"Prompt group path {key} already exists as a file")

    source_contents: dict[str, str] = {}
    source_builtin_only = source == key and prompt_group_exists(source, builtin_only=True)
    for name in sorted(PROMPT_ALLOWED_FILES):
        try:
            source_contents[name] = read_prompt_resource(
                source,
                name,
                builtin_only=source_builtin_only,
            )
        except FileNotFoundError:
            continue

    try:
        validate_prompt_group_contents(source_contents, prompt_group=key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _publish_managed_prompt_group(path, source_contents, replace=False)
    return _prompt_group_model_by_key(conn, key)


def delete_prompt_group(conn: sqlite3.Connection, key: str) -> None:
    key = validate_prompt_group_key(key)
    path = _managed_prompt_group_path_http(key, require_exists=False)
    if not path.exists():
        if prompt_group_exists(key, builtin_only=True):
            raise HTTPException(409, f"Built-in prompt group {key} cannot be deleted")
        return
    if not path.is_dir():
        raise HTTPException(409, f"Prompt group path {key} is not a directory")
    if prompt_group_exists(key, builtin_only=True):
        raise HTTPException(409, f"Managed override {key} must be restored instead of deleted")
    referenced_by = sorted(_prompt_group_references(conn).get(key, []))
    if referenced_by:
        raise HTTPException(
            409,
            f"Cannot delete prompt group {key}; referenced by scenarios: " + ", ".join(referenced_by),
        )
    shutil.rmtree(path)


def repair_prompt_group(
    conn: sqlite3.Connection,
    key: str,
    clone_from: str | None = None,
) -> PromptGroup:
    key = validate_prompt_group_key(key)
    path = _managed_prompt_group_path_http(key, require_exists=False)
    if not path.is_dir():
        if prompt_group_exists(key, builtin_only=True):
            raise HTTPException(409, f"Built-in prompt group {key} does not need repair")
        raise HTTPException(404, f"Prompt group {key} not found")

    is_override = prompt_group_exists(key, builtin_only=True)
    if is_override:
        source = key
        source_builtin_only = True
    else:
        if not clone_from:
            raise HTTPException(422, "Custom prompt group repair requires clone_from")
        source = validate_prompt_group_key(clone_from)
        if not prompt_group_exists(source):
            raise HTTPException(404, f"Prompt group {source} not found")
        source_builtin_only = False

    candidate: dict[str, str] = {}
    for name in PROMPT_ALLOWED_FILES:
        file_path = path / name
        if not file_path.is_file():
            continue
        try:
            candidate[name] = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # 无法解码的文件视为损坏，由下方来源内容替换。
            continue
    required = required_prompt_tokens_for_group(key)
    for name, tokens in required.items():
        content = candidate.get(name, "")
        if content and all(token in content for token in tokens):
            continue
        try:
            candidate[name] = read_prompt_resource(
                source,
                name,
                builtin_only=source_builtin_only,
            )
        except FileNotFoundError as exc:
            raise HTTPException(422, f"No repair source for Prompt file {name}") from exc

    # 修复时补齐来源中存在的可选文件，但不覆盖仍然有效的自定义内容。
    for name in sorted(PROMPT_ALLOWED_FILES - set(required)):
        if name in candidate:
            continue
        try:
            candidate[name] = read_prompt_resource(
                source,
                name,
                builtin_only=source_builtin_only,
            )
        except FileNotFoundError:
            continue

    try:
        validate_prompt_group_contents(candidate, prompt_group=key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _publish_managed_prompt_group(path, candidate, replace=True)
    return _prompt_group_model_by_key(conn, key)


def restore_prompt_group(conn: sqlite3.Connection, key: str) -> PromptGroup:
    key = validate_prompt_group_key(key)
    if not prompt_group_exists(key, builtin_only=True):
        raise HTTPException(409, f"Custom prompt group {key} has no built-in version to restore")
    path = _managed_prompt_group_path_http(key, require_exists=False)
    if not path.exists():
        raise HTTPException(409, f"Built-in prompt group {key} is already active")
    if not path.is_dir():
        raise HTTPException(409, f"Prompt group path {key} is not a directory")
    # 同名受管目录只是内置资源的覆盖层；移除后场景引用仍指向同一个 key。
    shutil.rmtree(path)
    return _prompt_group_model_by_key(conn, key)


def list_prompt_files(key: str) -> list[PromptFile]:
    key = validate_prompt_group_key(key)
    if not prompt_group_exists(key):
        raise HTTPException(404, f"Prompt group {key} not found")

    files: list[PromptFile] = []
    for name in sorted(PROMPT_ALLOWED_FILES):
        try:
            content = read_prompt_resource(key, name)
        except FileNotFoundError:
            continue
        files.append(_prompt_file_model(key, name, content=content))
    return files


def read_prompt_file(key: str, name: str) -> PromptFileContent:
    key = validate_prompt_group_key(key)
    name = validate_prompt_file_name(name)
    if not prompt_group_exists(key):
        raise HTTPException(404, f"Prompt group {key} not found")
    try:
        content = read_prompt_resource(key, name)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"Prompt file {name} not found") from exc
    except UnicodeDecodeError as exc:
        raise HTTPException(422, f"Prompt file {name} is not valid UTF-8") from exc
    file_model = _prompt_file_model(key, name, content=content)
    return PromptFileContent(**file_model.model_dump(), content=content)


def write_prompt_file(key: str, name: str, content: str) -> PromptFileContent:
    key = validate_prompt_group_key(key)
    name = validate_prompt_file_name(name)
    path = _managed_prompt_file_path_http(key, name, require_exists=False)
    if path is None:
        if prompt_group_exists(key, builtin_only=True):
            raise HTTPException(409, f"Built-in prompt group {key} must be cloned before editing")
        raise HTTPException(404, f"Prompt group {key} not found")
    group_path = path.parent
    if not group_path.exists() or not group_path.is_dir():
        if prompt_group_exists(key, builtin_only=True):
            raise HTTPException(409, f"Built-in prompt group {key} must be cloned before editing")
        raise HTTPException(404, f"Prompt group {key} not found")
    if path.exists() and not path.is_file():
        raise HTTPException(409, f"Prompt file path {name} is not a file")

    candidate = _managed_prompt_group_contents(key)
    candidate[name] = content
    try:
        # 保存前用候选内容整体校验必需文件/占位符，避免单文件更新破坏运行模板。
        validate_prompt_group_contents(candidate, prompt_group=key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    path.write_text(content, encoding="utf-8")
    return read_prompt_file(key, name)


def validate_prompt_group(key: str) -> PromptGroupValidationResult:
    key = validate_prompt_group_key(key)
    if not prompt_group_exists(key):
        raise HTTPException(404, f"Prompt group {key} not found")
    return _prompt_validation_result(key)


def _publish_managed_prompt_group(
    path: Path,
    contents: dict[str, str],
    *,
    replace: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{path.name}.staged-", dir=path.parent))
    backup: Path | None = None
    try:
        for name, content in contents.items():
            (staged / name).write_text(content, encoding="utf-8")
        if replace:
            backup = Path(tempfile.mkdtemp(prefix=f".{path.name}.backup-", dir=path.parent))
            backup.rmdir()
            path.rename(backup)
        staged.rename(path)
        if backup is not None:
            shutil.rmtree(backup)
    except Exception:
        if backup is not None and backup.exists() and not path.exists():
            backup.rename(path)
        raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)
        if backup is not None and backup.exists():
            shutil.rmtree(backup)


def _managed_prompt_group_path_http(key: str, *, require_exists: bool = False) -> Path:
    try:
        return managed_prompt_group_path(key, require_exists=require_exists)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"Prompt group {key} not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _managed_prompt_file_path_http(key: str, name: str, *, require_exists: bool = False) -> Path | None:
    try:
        return managed_prompt_file_path(key, name, require_exists=require_exists)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"Prompt file {name} not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _validate_managed_prompt_group_or_422(key: str) -> None:
    try:
        validate_prompt_group_contents(_managed_prompt_group_contents(key), prompt_group=key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _managed_prompt_group_contents(key: str) -> dict[str, str]:
    group_path = _managed_prompt_group_path_http(key, require_exists=True)
    contents: dict[str, str] = {}
    for name in PROMPT_ALLOWED_FILES:
        path = group_path / name
        if not path.is_file():
            continue
        try:
            contents[name] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(422, f"Prompt file {name} is not valid UTF-8") from exc
    return contents


def _prompt_group_references(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute(
        "SELECT key, prompt_group FROM scenarios ORDER BY key"
    ).fetchall()
    references: dict[str, set[str]] = {}
    for row in rows:
        scenario_key = str(row["key"] or "").strip()
        if not scenario_key:
            continue
        prompt_group = str(row["prompt_group"] or "").strip()
        if prompt_group:
            references.setdefault(prompt_group, set()).add(scenario_key)
    return {
        prompt_group: sorted(scenario_keys)
        for prompt_group, scenario_keys in references.items()
    }


def _prompt_group_model_by_key(conn: sqlite3.Connection, key: str) -> PromptGroup:
    for group in list_prompt_groups(conn):
        if group.key == key:
            return group
    raise HTTPException(404, f"Prompt group {key} not found")


def _prompt_group_model(info, references: dict[str, list[str]]) -> PromptGroup:
    validation = _prompt_validation_result(info.key)
    return PromptGroup(
        key=info.key,
        path=info.path,
        builtin=info.builtin,
        managed=info.managed,
        file_count=info.file_count,
        referenced_by=sorted(references.get(info.key, [])),
        overrides_builtin=info.overrides_builtin,
        capabilities=list(info.capabilities),
        visibility=info.visibility,
        valid=validation.valid,
        warnings=validation.warnings,
    )


def _prompt_validation_result(key: str) -> PromptGroupValidationResult:
    missing_files: list[str] = []
    missing_tokens: dict[str, list[str]] = {}
    warnings: list[str] = []
    required = required_prompt_tokens_for_group(key)
    for name, tokens in required.items():
        try:
            content = read_prompt_resource(key, name)
        except FileNotFoundError:
            missing_files.append(name)
            continue
        except UnicodeDecodeError:
            warnings.append(f"{name} is not valid UTF-8")
            continue
        missing = [token for token in tokens if token not in content]
        if missing:
            missing_tokens[name] = missing
    return PromptGroupValidationResult(
        valid=not missing_files and not missing_tokens and not warnings,
        missing_files=sorted(missing_files),
        missing_tokens=missing_tokens,
        warnings=warnings,
    )


def _prompt_file_model(key: str, name: str, *, content: str) -> PromptFile:
    path = None
    try:
        managed_path = managed_prompt_file_path(key, name, require_exists=False)
    except ValueError:
        managed_path = None
    if managed_path is not None and managed_path.is_file():
        path = managed_path
    try:
        contents = _managed_prompt_group_contents(key) if managed_prompt_group_path(key).is_dir() else {}
        contents[name] = content
        required = required_prompt_tokens_for_contents(contents, prompt_group=key)
    except HTTPException:
        required = required_prompt_tokens_for_group(key)
    required_tokens = list(required.get(name, ()))
    missing_tokens = [token for token in required_tokens if token not in content]
    stat = path.stat() if path is not None else None
    return PromptFile(
        name=name,
        path=name,
        size=len(content.encode("utf-8")),
        updated_at=(
            datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if stat is not None
            else None
        ),
        required_tokens=required_tokens,
        missing_tokens=missing_tokens,
    )
