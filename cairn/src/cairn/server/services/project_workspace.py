"""项目研究目录的只读入口；只使用已登记容器的现有本机 bind。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import ctypes
import errno
from hashlib import sha256
import heapq
import json
import os
from pathlib import PurePosixPath
import re
import stat
import struct
from time import monotonic

import docker
from docker.errors import DockerException, NotFound
from fastapi import HTTPException

from cairn.dispatcher.runtime.materials import MaterialsError, ProjectMaterials
from cairn.server.db import get_conn
from .ids import get_project_or_404

WORKSPACE_PATH = "/home/kali/workspace"
PAGE_SIZE = 100
SEARCH_STEPS = 1000
SEARCH_SECONDS = 0.25
# Linux 目录 cookie 可直接续读，避免大目录每次续查都从头枚举；不保存服务端扫描状态。
_getdents = ctypes.CDLL(None, use_errno=True).getdents64
_getdents.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
_getdents.restype = ctypes.c_ssize_t
PREVIEW_BYTES = 64 * 1024
IMAGE_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
# 旧研究 session 的实际位置；不能因此隐藏同级 artifacts/registered 原件。
_PRIVATE_ROOTS = (".cairn/pi",)


def _fail(code: str, status: int = 409):
    raise HTTPException(status, "workspace_" + code)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _parts(path: str) -> tuple[str, ...]:
    if (path.startswith("/") or "\\" in path or "\x00" in path
            or (path and any(part in {"", ".", ".."} for part in path.split("/")))):
        _fail("path_invalid", 400)
    return tuple(path.split("/")) if path else ()


def _modified(value) -> str:
    return datetime.fromtimestamp(value.st_mtime, timezone.utc).isoformat()


def _record(project_id: str):
    with get_conn() as conn:
        project = get_project_or_404(conn, project_id)
        activations = conn.execute(
            "SELECT dispatcher_id, container_id, docker_daemon_id FROM project_container_activations WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        history = conn.execute(
            "SELECT 1 FROM task_runs WHERE project_id = ? AND task_type IN ('bootstrap', 'reason', 'explore') LIMIT 1",
            (project_id,),
        ).fetchone()
        return dict(project), [dict(row) for row in activations], history is not None


@contextmanager
def _filesystem_errors(*, root: bool = False):
    try:
        yield
    except PermissionError:
        _fail("permission_denied", 403)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.ENXIO}:
            _fail("path_blocked", 403)
        if exc.errno == errno.ENOENT:
            _fail("unreachable" if root else "path_not_found", 409 if root else 404)
        _fail("unreachable", 503)


def _open_root(source: str) -> int:
    parts = PurePosixPath(source).parts
    if not source.startswith("/") or ".." in parts or len(parts) < 2:
        _fail("owner_unknown")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


@dataclass
class Workspace:
    project_id: str
    identity: str
    fd: int
    device: int
    blocked_paths: tuple[str, ...]

    def check_path(self, path: str) -> tuple[str, ...]:
        parts = _parts(path)
        if any(_within(path, root) for root in (*_PRIVATE_ROOTS, *self.blocked_paths)):
            _fail("path_private", 403)
        return parts

    def open(self, path: str, *, directory: bool = False) -> int:
        parts = self.check_path(path)
        fd = os.dup(self.fd)
        try:
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                if index < len(parts) - 1 or directory:
                    flags |= os.O_DIRECTORY
                following = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = following
                if os.fstat(fd).st_dev != self.device:
                    _fail("path_blocked", 403)
            value = os.fstat(fd)
            if directory:
                if not stat.S_ISDIR(value.st_mode):
                    _fail("path_blocked", 403)
            else:
                _regular(value, self.device)
            return fd
        except BaseException:
            os.close(fd)
            raise


def _regular(value, device: int):
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or value.st_dev != device:
        _fail("path_blocked", 403)


@contextmanager
def _workspace(project_id: str, expected: str | None):
    project, activations, history = _record(project_id)
    if not activations:
        _fail("owner_unknown" if history else "not_created")
    if len(activations) != 1:
        # 多 owner 留存根不能靠最近一次更新时间猜选。
        _fail("owner_unknown")
    activation = activations[0]
    identity = activation["container_id"]
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity) or not activation["docker_daemon_id"]:
        _fail("owner_unknown")
    client = None
    fd = None
    try:
        client = docker.from_env(timeout=5)
        if client.api.base_url != "http+docker://localhost":
            _fail("topology_unsupported")
        if client.info().get("ID") != activation["docker_daemon_id"]:
            _fail("topology_unsupported")
        container, info = ProjectMaterials(client)._owned(project_id, identity)
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        if info["kind"] != "research" or labels.get("cairn.scenario_key") != project["scenario_key"]:
            _fail("owner_unknown")
        mounts = container.attrs.get("Mounts", [])
        roots = [mount for mount in mounts if mount.get("Destination") == WORKSPACE_PATH]
        if not roots:
            _fail("layer_unsupported")
        if len(roots) != 1 or roots[0].get("Type") != "bind":
            _fail("topology_unsupported")
        source = roots[0].get("Source", "")
        if not source.startswith("/"):
            _fail("owner_unknown")
        blocked = []
        for mount in mounts:
            destination = mount.get("Destination", "")
            if destination == WORKSPACE_PATH:
                continue
            if _within(destination, WORKSPACE_PATH):
                blocked.append(destination[len(WORKSPACE_PATH) + 1:])
            other = mount.get("Source", "")
            if other.startswith("/"):
                actual, private = os.path.realpath(source), os.path.realpath(other)
                if _within(actual, private) or _within(private, actual):
                    _fail("path_private", 403)
        with _filesystem_errors(root=True):
            fd = _open_root(source)
            value = os.fstat(fd)
        # 身份包括真实根 inode；同名容器/目录被替换后，旧选择不能悄悄读取新文件。
        opaque = sha256(json.dumps([
            project_id, activation["dispatcher_id"], activation["docker_daemon_id"], identity,
            info["owner_id"], source, value.st_dev, value.st_ino,
        ], ensure_ascii=True).encode()).hexdigest()
        if expected is not None and expected != opaque:
            _fail("identity_changed")
        yield Workspace(project_id, opaque, fd, value.st_dev, tuple(blocked))
    except NotFound:
        _fail("deleted", 410)
    except MaterialsError:
        _fail("owner_unknown")
    except DockerException:
        _fail("unreachable", 503)
    finally:
        if fd is not None:
            os.close(fd)
        if client is not None:
            client.close()


def _cursor(value: str | None, workspace: Workspace, path: str):
    if not value:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
        if (not isinstance(data, list) or len(data) != 4 or data[:2] != [workspace.identity, path]
                or type(data[2]) is not int or data[2] not in {0, 1} or not isinstance(data[3], str)):
            raise ValueError
        return data[2], data[3]
    except (ValueError, UnicodeError):
        _fail("cursor_invalid", 400)


def _entry(workspace: Workspace, path: str, name: str, fd: int):
    child = f"{path}/{name}" if path else name
    if any(_within(child, private) for private in (*_PRIVATE_ROOTS, *workspace.blocked_paths)):
        return None
    try:
        name.encode("utf-8")
    except UnicodeError:
        _fail("filename_unsupported")
    try:
        value = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        # 实时目录允许并发删除；不伪造消失节点的属性。
        return None
    directory = stat.S_ISDIR(value.st_mode)
    blocked = (value.st_dev != workspace.device or (not directory and (
        not stat.S_ISREG(value.st_mode) or value.st_nlink != 1)))
    return value, {"name": name, "path": child,
                   "kind": "blocked" if blocked else "directory" if directory else "file",
                   "size": value.st_size if stat.S_ISREG(value.st_mode) else None,
                   "modified_at": _modified(value),
                   "blocked_reason": "link_or_special_file" if blocked else None}


def list_workspace(project_id: str, path: str = "", *, workspace_id: str | None = None, cursor: str | None = None):
    _parts(path)
    if (path or cursor) and not workspace_id:
        _fail("identity_required", 400)
    with _workspace(project_id, workspace_id) as workspace, _filesystem_errors():
        after = _cursor(cursor, workspace, path)
        fd = workspace.open(path, directory=True)
        try:
            def entries():
                with os.scandir(fd) as listing:
                    for entry in listing:
                        item = _entry(workspace, path, entry.name, fd)
                        if item is None:
                            continue
                        value, metadata = item
                        key = (0 if stat.S_ISDIR(value.st_mode) else 1, entry.name)
                        if after is None or key > after:
                            yield key, metadata
            page = heapq.nsmallest(PAGE_SIZE + 1, entries(), key=lambda item: item[0])
        finally:
            os.close(fd)
        more = len(page) > PAGE_SIZE
        page = page[:PAGE_SIZE]
        next_cursor = base64.urlsafe_b64encode(json.dumps([
            workspace.identity, path, *page[-1][0],
        ], ensure_ascii=True).encode()).decode() if more else None
        return {"project_id": project_id, "workspace_id": workspace.identity, "root_path": WORKSPACE_PATH,
                "path": path, "entries": [item for _, item in page], "next_cursor": next_cursor,
                "has_more": more, "live": True}


def _search_names(fd: int, offset: int):
    os.lseek(fd, offset, os.SEEK_SET)
    buffer = ctypes.create_string_buffer(32768)
    while True:
        count = _getdents(fd, buffer, len(buffer))
        if count < 0:
            raise OSError(ctypes.get_errno(), "directory read failed")
        if not count:
            return
        position = 0
        while position < count:
            # linux_dirent64 的头固定为 inode、下一条 cookie、记录长度、类型。
            _, following, length, _ = struct.unpack_from("=QqHB", buffer, position)
            name = os.fsdecode(buffer.raw[position + 19:position + length].split(b"\x00", 1)[0])
            yield name, following
            position += length


def _search_cursor(cursor: str | None, workspace: Workspace, query: str):
    if not cursor:
        return "", [[0, os.fstat(workspace.fd).st_ino]]
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        if (not isinstance(data, list) or len(data) != 4 or data[:2] != [workspace.identity, query]
                or not isinstance(data[2], str) or not isinstance(data[3], list)):
            raise ValueError
        path, frames = data[2:]
        parts = workspace.check_path(path)
        if len(frames) != len(parts) + 1 or any(
            not isinstance(frame, list) or len(frame) != 2
            or any(type(value) is not int or value < 0 or value > 2**(63 + index) - 1
                   for index, value in enumerate(frame))
            for frame in frames
        ):
            raise ValueError
        return path, frames
    except (ValueError, UnicodeError):
        _fail("cursor_invalid", 400)


def search_workspace(project_id: str, q: str, *, workspace_id: str, cursor: str | None = None):
    query = q.strip().casefold()
    if not query:
        _fail("query_required", 400)
    with _workspace(project_id, workspace_id) as workspace, _filesystem_errors():
        path, frames = _search_cursor(cursor, workspace, query)
        results = []
        steps = 0
        deadline = monotonic() + SEARCH_SECONDS
        # 按目录流顺序深度优先；同一目录不变时顺序确定，游标不要求全量排序或索引。
        while frames and steps < SEARCH_STEPS and len(results) < PAGE_SIZE:
            if steps and monotonic() >= deadline:
                break
            fd = workspace.open(path, directory=True)
            try:
                if os.fstat(fd).st_ino != frames[-1][1]:
                    _fail("search_changed")
                steps += 1
                for name, following in _search_names(fd, frames[-1][0]):
                    frames[-1][0] = following
                    steps += 1
                    item = None if name in {".", ".."} else _entry(workspace, path, name, fd)
                    if item is not None:
                        value, metadata = item
                        if metadata["kind"] == "directory":
                            path = metadata["path"]
                            frames.append([0, value.st_ino])
                            break
                        if metadata["kind"] == "file" and query in metadata["path"].casefold():
                            results.append(metadata)
                    if steps >= SEARCH_STEPS or len(results) >= PAGE_SIZE or monotonic() >= deadline:
                        break
                else:
                    frames.pop()
                    path = path.rpartition("/")[0]
            finally:
                os.close(fd)
        next_cursor = base64.urlsafe_b64encode(json.dumps([
            workspace.identity, query, path, frames,
        ], ensure_ascii=True).encode()).decode() if frames else None
        return {"project_id": project_id, "workspace_id": workspace.identity, "root_path": WORKSPACE_PATH,
                "query": q.strip(), "entries": results, "next_cursor": next_cursor,
                "has_more": bool(frames), "live": True}


def _image_type(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def preview_workspace(project_id: str, path: str, *, workspace_id: str, window: str = "head"):
    with _workspace(project_id, workspace_id) as workspace, _filesystem_errors():
        fd = workspace.open(path)
        try:
            value = os.fstat(fd)
            prefix = os.pread(fd, min(4096, PREVIEW_BYTES), 0)
            image = _image_type(prefix)
            offset = max(0, value.st_size - PREVIEW_BYTES) if window == "tail" else 0
            raw = os.pread(fd, PREVIEW_BYTES, offset)
            _regular(os.fstat(fd), workspace.device)
            binary = bool(image) or b"\x00" in raw or any(byte < 9 or 13 < byte < 32 for byte in raw)
            # UTF-8 窗口边缘可能截断码点；显式标记替代解码，不把它冒充原始字节。
            content = None if binary else raw.decode("utf-8", errors="replace")
            preview_kind = "image" if image and value.st_size <= IMAGE_BYTES else "binary" if binary else "text"
            return {"project_id": project_id, "workspace_id": workspace.identity, "path": path,
                    "size": value.st_size, "modified_at": _modified(value), "preview_kind": preview_kind,
                    "media_type": image or ("application/octet-stream" if binary else "text/plain"),
                    "content": content, "bytes_read": len(raw), "truncated": value.st_size > len(raw),
                    "encoding_lossy": content is not None and "�" in content,
                    "window": window, "live": True}
        finally:
            os.close(fd)


@dataclass
class WorkspaceDownload:
    fd: int
    size: int
    device: int
    filename: str
    media_type: str

    def chunks(self):
        # 固定本次观测的长度，不无限追随持续追加的日志；仍不是原子快照。
        remaining = self.size
        while remaining:
            _regular(os.fstat(self.fd), self.device)
            chunk = os.read(self.fd, min(CHUNK_BYTES, remaining))
            _regular(os.fstat(self.fd), self.device)
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def download_workspace(project_id: str, path: str, *, workspace_id: str, inline: bool = False):
    with _workspace(project_id, workspace_id) as workspace, _filesystem_errors():
        fd = workspace.open(path)
        try:
            value = os.fstat(fd)
            image = _image_type(os.pread(fd, 16, 0))
            if inline and (not image or value.st_size > IMAGE_BYTES):
                _fail("inline_unsupported", 415)
            return WorkspaceDownload(fd, value.st_size, workspace.device, PurePosixPath(path).name,
                                     image if inline else "application/octet-stream")
        except BaseException:
            os.close(fd)
            raise
