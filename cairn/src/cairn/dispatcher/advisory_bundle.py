"""为 Worker 物化只读 advisory bundle。"""

from __future__ import annotations

from dataclasses import dataclass
import errno
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path, PurePosixPath
from re import fullmatch
import shutil
import stat
import tempfile
from typing import Iterable

from cairn.advisory_contract import selection_sha256
from cairn.dispatcher.prompting import resolved_knowledge_base_path
from cairn.dispatcher.protocol.client import CairnClient
from cairn.server.models import LibraryFile, ProjectLibraryBinding, ProjectResource

ADVISORY_CONTAINER_PATH = "/cairn/advisory"
ADVISORY_ROOT_ENV_VAR = "CAIRN_ADVISORY_ROOT"
ADVISORY_DOCKER_SOURCE_ENV_VAR = "CAIRN_ADVISORY_DOCKER_SOURCE"
DEFAULT_ADVISORY_ROOT = "/tmp/cairn-advisory"
MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True, slots=True)
class AdvisoryBundle:
    """已完成校验且可只读挂载的 advisory bundle。"""

    source_path: str
    local_path: Path
    digest: str
    legacy_key: str | None
    resource_count: int
    manifest: dict[str, object]


@dataclass(frozen=True, slots=True)
class _LibraryRevisionFiles:
    binding: ProjectLibraryBinding
    files: tuple[tuple[LibraryFile, bytes], ...]


def stage_advisory_bundle(
    client: CairnClient,
    project_id: str,
    legacy_key: str | None,
) -> AdvisoryBundle | None:
    """优先物化冻结资料库，并合并项目资料；旧项目才回退场景知识集。"""
    project = client.get_project(project_id)
    # 旧服务无法表达显式空选择；不能把缺失字段当作旧项目而静默挂载场景资料。
    if "library_selection_explicit" not in project.model_fields_set:
        raise RuntimeError("server does not provide advisory selection state")
    bindings = sorted(project.library_bindings, key=lambda item: item.library_key)
    if len({binding.library_key for binding in bindings}) != len(bindings):
        raise RuntimeError("duplicate advisory library binding")
    library_revisions = [
        _read_library_revision(client, project_id, binding) for binding in bindings
    ]

    normalized_legacy_key = (
        None if bindings or project.library_selection_explicit else _normalized_legacy_key(legacy_key)
    )
    legacy_files = _legacy_files(normalized_legacy_key)
    resources = sorted(client.list_project_resources(project_id), key=lambda item: item.path)
    resource_files = resources
    for resource in resources:
        _safe_relative_path(resource.path, source="project resource")
    if not library_revisions and not legacy_files and not resource_files:
        return None

    manifest = {
        "version": 3,
        "project_id": project_id,
        "libraries": [
            {
                "key": revision.binding.library_key,
                "name": revision.binding.library_name,
                "version": revision.binding.version,
                "digest_sha256": revision.binding.digest_sha256,
                # 历史整库保持 v3 manifest 原字节；子集显式记录其身份。
                **(
                    {"paths": sorted(revision.binding.paths),
                     "selection_sha256": revision.binding.selection_sha256}
                    if revision.binding.paths is not None else {}
                ),
                "files": [
                    {
                        "path": metadata.path,
                        "bundle_path": (
                            f"library/{revision.binding.library_key}/{metadata.path}"
                        ),
                        "size_bytes": metadata.size_bytes,
                        "sha256": metadata.sha256,
                    }
                    for metadata, _content in revision.files
                ],
            }
            for revision in library_revisions
        ],
        "legacy_key": normalized_legacy_key,
        "legacy_files": [item[1] for item in legacy_files],
        "project_resources": [
            {
                "id": resource.id,
                "kind": resource.kind,
                "path": resource.path,
                "size_bytes": resource.size_bytes,
                "sha256": resource.sha256,
            }
            for resource in resource_files
        ],
    }
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = sha256(manifest_bytes).hexdigest()
    local_root = _advisory_root()
    project_segment = sha256(project_id.encode("utf-8")).hexdigest()[:32]
    project_root = local_root / project_segment
    final_path = project_root / digest
    if not os.path.lexists(final_path):
        _materialize_bundle(
            client, project_id,
            library_revisions,
            legacy_files,
            resource_files,
            manifest_bytes,
            project_root,
            final_path,
        )
    _verify_materialized_bundle(final_path, manifest, manifest_bytes)

    source_path = _docker_source_path(local_root, final_path)
    return AdvisoryBundle(
        source_path=str(source_path),
        local_path=final_path,
        digest=digest,
        legacy_key=normalized_legacy_key,
        resource_count=(
            sum(len(revision.files) for revision in library_revisions)
            + len(legacy_files)
            + len(resource_files)
        ),
        manifest=manifest,
    )


def _read_library_revision(
    client: CairnClient,
    project_id: str,
    binding: ProjectLibraryBinding,
) -> _LibraryRevisionFiles:
    key = _normalized_library_key(binding.library_key)
    if not {"paths", "selection_sha256"}.issubset(binding.model_fields_set):
        raise RuntimeError(f"server does not provide advisory selection identity: {key}")
    selected_paths = binding.paths
    if selected_paths is not None and (
        not selected_paths or len(selected_paths) != len(set(selected_paths))
    ):
        raise RuntimeError(f"invalid advisory library selection: {key}")
    metadata_files = sorted(
        client.list_project_library_files(project_id, key, binding.version),
        key=lambda item: item.path,
    )
    paths = [_safe_relative_path(item.path, source="library file").as_posix() for item in metadata_files]
    if len(paths) != len(set(paths)) or len(paths) != binding.file_count:
        raise RuntimeError(f"library selection file count mismatch: {key} v{binding.version}")
    if selected_paths is not None and set(paths) != set(selected_paths):
        raise RuntimeError(f"library selection paths mismatch: {key} v{binding.version}")
    actual_selection_digest = selection_sha256(
        key, binding.version, binding.digest_sha256,
        [{"path": item.path, "size_bytes": item.size_bytes, "sha256": item.sha256} for item in metadata_files],
    )
    if actual_selection_digest != binding.selection_sha256:
        raise RuntimeError(f"library selection sha256 mismatch: {key} v{binding.version}")

    files: list[tuple[LibraryFile, bytes]] = []
    digest_files: dict[str, bytes] = {}
    for metadata, path in zip(metadata_files, paths):
        content = client.read_project_library_file(project_id, key, binding.version, path)
        _verify_file(
            source=f"library {key} v{binding.version} file {path}",
            size_bytes=metadata.size_bytes,
            digest=metadata.sha256,
            content=content,
        )
        digest_files[path] = content
        files.append((metadata, content))
    # 子集不等于整版，只有整库选择才能与原版内容摘要比较。
    if selected_paths is None:
        actual_revision_digest = _library_digest(digest_files)
        if actual_revision_digest != binding.digest_sha256:
            raise RuntimeError(
                f"library revision sha256 mismatch: {key} v{binding.version} "
                f"expected={binding.digest_sha256} actual={actual_revision_digest}"
            )
    return _LibraryRevisionFiles(binding=binding, files=tuple(files))


def _materialize_bundle(
    client: CairnClient,
    project_id: str,
    library_revisions: list[_LibraryRevisionFiles],
    legacy_files: list[tuple[bytes, dict[str, object]]],
    resource_files: list[ProjectResource],
    manifest_bytes: bytes,
    project_root: Path,
    final_path: Path,
) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".staging-", dir=project_root))
    try:
        for revision in library_revisions:
            key = _normalized_library_key(revision.binding.library_key)
            for metadata, content in revision.files:
                destination = temporary / "library" / key / _safe_relative_path(
                    metadata.path,
                    source="library file",
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
        for content, metadata in legacy_files:
            destination = temporary / str(metadata["bundle_path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        for resource in resource_files:
            destination = temporary / "project" / _safe_relative_path(
                resource.path,
                source="project resource",
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output:
                client.download_project_resource(project_id, resource, output)
        # 先固定权限再原子发布，避免并发读者看到尚未设为只读的完整目录。
        (temporary / MANIFEST_NAME).write_bytes(manifest_bytes)
        _make_read_only(temporary)
        try:
            temporary.rename(final_path)
        except OSError as exc:
            # 不同系统对非空目标目录分别返回 EEXIST 或 ENOTEMPTY；由调用方校验胜出的缓存。
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
    finally:
        if temporary.exists():
            # 并发落败时只清理由本次创建的临时目录，先恢复目录写权限以支持非 root 运行。
            for directory, _subdirectories, _files in os.walk(temporary, followlinks=False):
                os.chmod(directory, 0o700, follow_symlinks=False)
            shutil.rmtree(temporary)


def _bundle_expected_files(
    manifest: dict[str, object], manifest_bytes: bytes,
) -> dict[Path, tuple[int, str]]:
    """原件缓存与工作副本共用同一份来源清单和路径校验。"""
    expected = {Path(MANIFEST_NAME): (len(manifest_bytes), sha256(manifest_bytes).hexdigest())}

    def add(path: str, metadata: dict) -> None:
        relative = _safe_relative_path(path, source="advisory bundle")
        if relative in expected:
            raise RuntimeError(f"duplicate advisory bundle path: {relative}")
        expected[relative] = (metadata["size_bytes"], metadata["sha256"])

    for library in manifest["libraries"]:
        for item in library["files"]:
            add(item["bundle_path"], item)
    for item in manifest["legacy_files"]:
        add(item["bundle_path"], item)
    for item in manifest["project_resources"]:
        add(f"project/{item['path']}", item)
    return expected


def _verify_materialized_bundle(
    root: Path, manifest: dict[str, object], manifest_bytes: bytes,
) -> None:
    # 缓存和工作副本都可能已被修改；复用前核对完整集合与原始字节，不覆盖修复。
    expected = _bundle_expected_files(manifest, manifest_bytes)
    directories = {parent for path in expected for parent in path.parents}
    seen_files: set[Path] = set()
    seen_directories: set[Path] = set()
    device = root.lstat().st_dev
    pending = [Path(".")]
    while pending:
        relative = pending.pop()
        directory = root / relative
        directory_stat = directory.lstat()
        mode = directory_stat.st_mode
        if not stat.S_ISDIR(mode) or mode & 0o222 or directory_stat.st_dev != device:
            raise RuntimeError(f"invalid advisory bundle directory: {relative}")
        seen_directories.add(relative)
        with os.scandir(directory) as entries:
            for entry in entries:
                child = relative / entry.name
                entry_stat = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(entry_stat.st_mode):
                    if child not in directories:
                        raise RuntimeError(f"unexpected advisory bundle directory: {child}")
                    pending.append(child)
                elif stat.S_ISREG(entry_stat.st_mode):
                    if child not in expected or entry_stat.st_mode & 0o222:
                        raise RuntimeError(f"invalid advisory bundle file: {child}")
                    size, expected_digest = expected[child]
                    # 打开后再次检查类型和权限，避免替换成链接或特殊文件。
                    fd = os.open(directory / entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as source:
                        opened_stat = os.fstat(source.fileno())
                        if (not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_mode & 0o222
                                or opened_stat.st_nlink != 1 or opened_stat.st_dev != device):
                            raise RuntimeError(f"invalid advisory bundle file: {child}")
                        if opened_stat.st_size != size:
                            raise RuntimeError(f"advisory bundle file size mismatch: {child}")
                        actual_digest = file_digest(source, "sha256").hexdigest()
                    if actual_digest != expected_digest:
                        raise RuntimeError(f"advisory bundle file sha256 mismatch: {child}")
                    seen_files.add(child)
                else:
                    raise RuntimeError(f"invalid advisory bundle entry: {child}")
    if seen_files != set(expected) or seen_directories != directories:
        raise RuntimeError("advisory bundle is missing expected files or directories")


def _legacy_files(legacy_key: str | None) -> list[tuple[bytes, dict[str, object]]]:
    if legacy_key is None:
        return []
    root = Path(resolved_knowledge_base_path())
    selected = root if legacy_key == "knowledge_base" else root / legacy_key
    if not selected.is_dir():
        raise RuntimeError(f"configured legacy knowledge set not found: {legacy_key}")

    candidates: Iterable[Path]
    if legacy_key == "knowledge_base":
        # 根知识集只包含根目录文件，不能把其他命名知识集作为子目录一并暴露。
        candidates = (path for path in selected.iterdir() if path.is_file())
    else:
        candidates = (path for path in selected.rglob("*") if path.is_file())

    files: list[tuple[bytes, dict[str, object]]] = []
    selected_resolved = selected.resolve()
    for source in sorted(candidates, key=lambda path: path.as_posix()):
        # Legacy 管理目录也不得借助 symlink 把集合外文件带入 Tenant 容器。
        if source.is_symlink():
            raise RuntimeError(f"legacy knowledge symlink is not allowed: {source.name}")
        source_resolved = source.resolve()
        try:
            source_resolved.relative_to(selected_resolved)
        except ValueError as exc:
            raise RuntimeError("legacy knowledge file escapes selected set") from exc
        relative = _safe_relative_path(
            source.relative_to(selected).as_posix(),
            source="legacy knowledge file",
        ).as_posix()
        content = source_resolved.read_bytes()
        files.append(
            (
                content,
                {
                    "path": relative,
                    "bundle_path": f"legacy/{legacy_key}/{relative}",
                    "size_bytes": len(content),
                    "sha256": sha256(content).hexdigest(),
                },
            )
        )
    return files


def _verify_file(
    *,
    source: str,
    size_bytes: int,
    digest: str,
    content: bytes,
) -> None:
    if len(content) != size_bytes:
        raise RuntimeError(
            f"{source} size mismatch: expected={size_bytes} actual={len(content)}"
        )
    actual_digest = sha256(content).hexdigest()
    if actual_digest != digest:
        raise RuntimeError(
            f"{source} sha256 mismatch: expected={digest} actual={actual_digest}"
        )


def _library_digest(files: dict[str, bytes]) -> str:
    digest = sha256()
    for path, content in sorted(files.items()):
        path_bytes = path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _safe_relative_path(value: str, *, source: str) -> Path:
    if "\\" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise RuntimeError(f"invalid {source} path: {value}")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(f"invalid {source} path: {value}")
    return Path(*path.parts)


def _normalized_library_key(value: str) -> str:
    text = value.strip()
    if len(text) > 128 or fullmatch(r"[a-z0-9_-]+", text) is None:
        raise RuntimeError("invalid advisory library key")
    return text


def _normalized_legacy_key(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > 128 or fullmatch(r"[a-z0-9_-]+", text) is None:
        raise RuntimeError("invalid legacy knowledge key")
    return text


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _advisory_root() -> Path:
    configured = os.environ.get(ADVISORY_ROOT_ENV_VAR, "").strip()
    root = Path(configured or DEFAULT_ADVISORY_ROOT).expanduser()
    return root if root.is_absolute() else root.resolve()


def _docker_source_path(local_root: Path, final_path: Path) -> Path:
    configured = os.environ.get(ADVISORY_DOCKER_SOURCE_ENV_VAR, "").strip()
    if not configured:
        return final_path
    docker_root = Path(configured).expanduser()
    if not docker_root.is_absolute():
        docker_root = docker_root.resolve()
    return docker_root / final_path.relative_to(local_root)


__all__ = [
    "ADVISORY_CONTAINER_PATH",
    "AdvisoryBundle",
    "stage_advisory_bundle",
]
