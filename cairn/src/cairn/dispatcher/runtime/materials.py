"""Docker 宿主运维材料读取；不启动容器，不执行容器命令，不连接业务数据库。"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile

from docker import utils


_ROOTS = ("/home/kali", "/home/kali/workspace", "/tmp", "/var/tmp")
_PRIVATE_PARTS = frozenset({
    ".cairn", ".pi", "cairn-prompts", "prompts", "control", "private", "private.json",
    ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".netrc", ".npmrc",
    "capture", "ca", "egress", "cairn-exec", "cairn-egress", "cairn-traffic", "cairn-ca",
    "gate.json", "execute.sock", "execution.sock",
})
_DIR_MODE = 1 << 31  # Docker stat 使用 Go os.FileMode，不是 POSIX st_mode。
_SYMLINK_MODE = 1 << 27
_LIST_BYTES = 8 * 1024 * 1024
_LIST_ENTRIES = 200
_CHUNK_BYTES = 64 * 1024


class MaterialsError(ValueError):
    """仅携带固定错误码，避免 Docker 异常中的私有配置进入运维输出。"""


class _ArchiveReader:
    def __init__(self, chunks, limit):
        self.chunks = iter(chunks)
        self.buffer = bytearray()
        self.received = 0
        self.limit = limit

    def read(self, size):
        while len(self.buffer) < size:
            if self.received >= self.limit:
                raise MaterialsError("materials_archive_limit")
            chunk = next(self.chunks, b"")
            if not chunk:
                break
            self.received += len(chunk)
            if self.received > self.limit:
                raise MaterialsError("materials_archive_limit")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _material_path(value: str) -> PurePosixPath:
    if (not value.startswith("/") or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/")[1:])):
        raise MaterialsError("materials_path_invalid")
    path = PurePosixPath(value)
    if not any(_within(value, root) for root in _ROOTS) or _PRIVATE_PARTS.intersection(path.parts):
        raise MaterialsError("materials_path_private_or_unsupported")
    if ".config" in path.parts:
        position = path.parts.index(".config") + 1
        if position >= len(path.parts) or path.parts[position] not in {"chromium", "google-chrome", "ms-playwright"}:
            raise MaterialsError("materials_path_private_or_unsupported")
    return path


class ProjectMaterials:
    def __init__(self, client):
        self.client = client

    def _owned(self, project_id, container_id):
        if not project_id or not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise MaterialsError("materials_full_container_id_required")
        container = self.client.containers.get(container_id)
        container.reload()
        if container.id != container_id or container.attrs.get("Id") != container_id:
            raise MaterialsError("materials_container_identity_mismatch")
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        if labels.get("cairn.project_id") == project_id and "io.cairn.traffic.project" not in labels:
            if (labels.get("cairn.engine") != "pi"
                    or not re.fullmatch(r"[a-f0-9]{64}", labels.get("cairn.pi.instance_id", ""))
                    or not labels.get("cairn.runtime_signature") or not labels.get("cairn.scenario_key")
                    or labels.get("cairn.conversation_id") is not None):
                raise MaterialsError("materials_container_owner_invalid")
            role = labels.get("cairn.container_role")
            if role == "report" and labels.get("cairn.report_task_run_id"):
                kind, task_run = "report", labels["cairn.report_task_run_id"]
            elif role is None:
                kind, task_run = "research", None
            else:
                raise MaterialsError("materials_container_owner_invalid")
            owner = labels["cairn.pi.instance_id"]
        elif labels.get("io.cairn.traffic.project") == project_id and "cairn.project_id" not in labels:
            required = ("owner", "dispatcher", "resource", "scope", "task-run", "generation")
            if labels.get("io.cairn.traffic.role") != "executor" or any(
                not labels.get(f"io.cairn.traffic.{key}") for key in required
            ):
                raise MaterialsError("materials_container_owner_invalid")
            kind, task_run = "executor", labels["io.cairn.traffic.task-run"]
            owner = labels["io.cairn.traffic.owner"]
        else:
            raise MaterialsError("materials_project_mismatch")
        return container, {
            "container_id": container_id, "project_id": project_id, "kind": kind,
            "task_run_id": task_run, "owner_id": owner,
            "state": container.attrs.get("State", {}).get("Status", "unknown"),
        }

    def containers(self, project_id: str) -> list[dict]:
        found = {}
        for label in ("cairn.project_id", "io.cairn.traffic.project"):
            for container in self.client.containers.list(all=True, filters={"label": f"{label}={project_id}"}):
                try:
                    _, info = self._owned(project_id, container.id)
                except MaterialsError:
                    continue
                found[container.id] = info
        return [found[key] for key in sorted(found)]

    def _scene(self, project_id, container_id):
        container, info = self._owned(project_id, container_id)
        if info["state"] not in {"paused", "exited", "created", "dead"}:
            raise MaterialsError("materials_quiescent_container_required")
        return container, info

    def _stat(self, container_id, path):
        api = self.client.api
        # HEAD 不下载父目录 tar；linkTarget 能发现 Docker 随后可能跟随的 symlink。
        with api.head(api._url("/containers/{0}/archive", container_id),
                      params={"path": str(path)}, timeout=api.timeout) as response:
            api._raise_for_status(response)
            encoded = response.headers.get("x-docker-container-path-stat")
            if not encoded:
                raise MaterialsError("materials_stat_unavailable")
            value = utils.decode_json_header(encoded)
        if (not isinstance(value, dict) or type(value.get("mode")) is not int
                or type(value.get("size")) is not int or value["size"] < 0):
            raise MaterialsError("materials_stat_invalid")
        if value.get("linkTarget") or value["mode"] & _SYMLINK_MODE:
            raise MaterialsError("materials_symlink_forbidden")
        return value

    def _checked_stat(self, container_id, path):
        for parent in reversed(path.parents):
            value = self._stat(container_id, parent)
            if not value["mode"] & _DIR_MODE:
                raise MaterialsError("materials_parent_not_directory")
        return self._stat(container_id, path)

    def _mount(self, container, info, path, *, directory=False):
        mounts = container.attrs.get("Mounts", [])
        private = []
        for mount in mounts:
            destination = mount.get("Destination", "")
            if _PRIVATE_PARTS.intersection(PurePosixPath(destination).parts):
                private.append(mount)
                if _within(str(path), destination) or (directory and _within(destination, str(path))):
                    raise MaterialsError("materials_private_mount_forbidden")
        candidates = [mount for mount in mounts if _within(str(path), mount.get("Destination", ""))]
        selected = max(candidates, key=lambda item: len(item["Destination"]), default=None)
        if selected is not None:
            if selected.get("Type") not in {"bind", "volume"} or not selected.get("Source", "").startswith("/"):
                raise MaterialsError("materials_mount_unsupported")
            source = os.path.realpath(selected["Source"])
            for mount in private:
                if not mount.get("Source", "").startswith("/"):
                    raise MaterialsError("materials_private_mount_identity_unavailable")
                private_source = os.path.realpath(mount["Source"])
                if (_within(source, private_source) or _within(private_source, source)
                        or os.path.samefile(source, private_source)):
                    raise MaterialsError("materials_private_mount_alias")
        elif info["kind"] in {"research", "report"}:
            # 独立挂载使 layer 无法硬链接到控制凭据；Docker stat 本身不能证明 layer inode 的 nlink。
            destinations = {mount.get("Destination") for mount in private if mount.get("Type") in {"bind", "volume"}}
            if not {"/home/kali/.cairn", "/tmp/cairn-prompts"}.issubset(destinations):
                raise MaterialsError("materials_private_mount_isolation_required")
        return selected

    @contextmanager
    def _bind_file(self, mount, path):
        if mount is None:
            yield None
            return
        source = Path(mount["Source"])
        relative = path.relative_to(mount["Destination"])
        parts = (*source.parts[1:], *relative.parts)
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                if index < len(parts) - 1:
                    flags |= os.O_DIRECTORY
                following = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = following
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                raise MaterialsError("materials_regular_unlinked_file_required")
            yield fd
            if os.fstat(fd).st_nlink != 1:
                raise MaterialsError("materials_hardlink_forbidden")
        finally:
            os.close(fd)

    def list(self, project_id: str, container_id: str, path: str | None = None) -> dict:
        container, info = self._scene(project_id, container_id)
        if path is None:
            # 默认只列已知材料入口，不递归下载 HOME、TMPDIR 或系统根。
            return {**info, "roots": list(_ROOTS), "entries": [], "truncated": False}
        target = _material_path(path)
        value = self._checked_stat(container_id, target)
        directory = bool(value["mode"] & _DIR_MODE)
        self._mount(container, info, target, directory=directory)
        if not directory:
            return {**info, "entries": [{"path": str(target), "kind": "file", "size": value["size"]}], "truncated": False}
        if str(target) in {"/home/kali", "/tmp", "/var/tmp"}:
            raise MaterialsError("materials_select_material_subdirectory")
        entries = []
        truncated, reason = False, None
        stream, _ = container.get_archive(str(target), chunk_size=_CHUNK_BYTES)
        try:
            with tarfile.open(fileobj=_ArchiveReader(stream, _LIST_BYTES), mode="r|") as archive:
                for member in archive:
                    name = PurePosixPath(member.name)
                    if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != target.name:
                        raise MaterialsError("materials_archive_path_invalid")
                    actual = target.parent / name
                    try:
                        _material_path(str(actual))
                    except MaterialsError:
                        truncated, reason = True, "private_subtree"
                        break
                    if len(entries) >= _LIST_ENTRIES or member.size > _LIST_BYTES:
                        truncated, reason = True, "listing_limit"
                        break
                    entries.append({"path": str(actual), "kind": "directory" if member.isdir() else "file" if member.isreg() else "blocked_special", "size": member.size})
        except MaterialsError as exc:
            if str(exc) != "materials_archive_limit":
                raise
            truncated, reason = True, "listing_limit"
        finally:
            stream.close()
        return {**info, "entries": entries, "truncated": truncated, "limit_reason": reason}

    def save(self, project_id: str, container_id: str, path: str, output: Path) -> dict:
        container, info = self._scene(project_id, container_id)
        target = _material_path(path)
        value = self._checked_stat(container_id, target)
        if value["mode"] & 0xFFFF0000:
            raise MaterialsError("materials_regular_file_required")
        mount = self._mount(container, info, target)
        output = Path(output)
        if os.path.lexists(output):
            raise MaterialsError("materials_output_exists")
        temporary = None
        stream = None
        try:
            with self._bind_file(mount, target):
                stream, archive_stat = container.get_archive(str(target), chunk_size=_CHUNK_BYTES)
                if (archive_stat != value):
                    raise MaterialsError("materials_file_changed")
                with tarfile.open(fileobj=_ArchiveReader(stream, value["size"] + 1024 * 1024), mode="r|") as archive:
                    member = archive.next()
                    if (member is None or member.name != target.name or not member.isreg()
                            or member.linkname or member.sparse or member.size != value["size"]):
                        raise MaterialsError("materials_archive_regular_file_required")
                    fd, temporary = tempfile.mkstemp(prefix=".cairn-material-", dir=output.parent)
                    with os.fdopen(fd, "wb") as destination, archive.extractfile(member) as source:
                        copied = 0
                        while chunk := source.read(_CHUNK_BYTES):
                            destination.write(chunk)
                            copied += len(chunk)
                        if copied != member.size or archive.next() is not None:
                            raise MaterialsError("materials_archive_single_file_required")
                        destination.flush()
                        os.fsync(destination.fileno())
                if self._checked_stat(container_id, target) != value:
                    raise MaterialsError("materials_file_changed")
                self._scene(project_id, container_id)
            # link 是同目录原子 no-clobber 发布，不用会覆盖已有文件的 rename/replace。
            os.link(temporary, output, follow_symlinks=False)
        finally:
            if stream is not None:
                stream.close()
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return {**info, "path": str(target), "output": str(output), "size": value["size"],
                "hardlink_check": "host_nlink_and_tar" if mount is not None else "tar_only"}
