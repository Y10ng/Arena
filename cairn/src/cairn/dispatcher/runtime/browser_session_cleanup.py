"""可由 python3 -c 执行的 stdlib-only 浏览器会话清理入口。

仅支持已核验的 Linux @playwright/cli 0.1.15 / core 1.62.0-alpha-2026-06-29。
probe 不运行 CLI，只读定位 namespace；父进程必须原样保留 context，并登记 exact name。
cleanup 只关闭 cairn-task-<32 位小写十六进制>，命名格式本身不构成任务归属证明。
父进程仍须限制整个命令时限；helper 的八秒预算不替代 OS/文件系统级超时。
不支持 CLI 的超长 socket 文件名截断形式，遇到该情况保守跳过，不猜测路径。
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import time


CLI_VERSION = "0.1.15"
CORE_VERSION = "1.62.0-alpha-2026-06-29"


def _namespace(path):
    return hashlib.sha1(str(path).encode()).hexdigest()[:16]


def _workspace(cwd):
    directory = Path(cwd)
    for _ in range(10):
        marker = directory / ".playwright"
        if marker.is_symlink():
            raise ValueError("linked_workspace")
        try:
            marker.stat()
            return str(directory)
        except FileNotFoundError:
            pass
        if directory.parent == directory:
            break
        directory = directory.parent
    return None


def probe():
    """返回 {status: ok, context} 或无敏感细节的 unsupported/skipped 原因。"""
    try:
        result = _probe()
        if len(json.dumps(result).encode()) >= 16384:
            return {"status": "skipped", "reason": "context_too_large"}
        return result
    except Exception:
        return {"status": "skipped", "reason": "invalid_context"}


def _probe():
    if sys.platform != "linux":
        return {"status": "unsupported", "reason": "unsupported_platform"}
    if (not os.path.isabs(os.environ.get("HOME", ""))
            or (os.environ.get("XDG_CACHE_HOME")
                and not os.path.isabs(os.environ["XDG_CACHE_HOME"]))
            or any(value for key, value in os.environ.items()
                   if key.startswith("PWTEST_") or key in {
                       "PLAYWRIGHT_CLI_VERSION_FOR_TEST", "NODE_OPTIONS", "NODE_PATH",
                   })):
        return {"status": "skipped", "reason": "unsafe_environment"}
    executable = shutil.which("playwright-cli")
    if not executable:
        return {"status": "unsupported", "reason": "cli_not_found"}
    executable = Path(executable).resolve(strict=True)
    package = executable.parent
    metadata = _read_json(package / "package.json", owned=False)
    core = package / "node_modules/playwright-core"
    core_metadata = _read_json(core / "package.json", owned=False)
    if (metadata.get("name") != "@playwright/cli"
            or metadata.get("version") != CLI_VERSION
            or metadata.get("bin") != {"playwright-cli": "playwright-cli.js"}
            or executable.name != "playwright-cli.js"
            or core_metadata.get("name") != "playwright-core"
            or core_metadata.get("version") != CORE_VERSION):
        return {"status": "unsupported", "reason": "unsupported_cli"}
    home = Path(os.environ["HOME"])
    cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    username = os.environ.get("USERNAME") or os.environ.get("USER") or "default"
    user_hash = hashlib.sha1(username.encode()).hexdigest()[:8]
    temp = os.environ.get("TMPDIR") or os.environ.get("TMP") or os.environ.get("TEMP") or "/tmp"
    node = shutil.which("node")
    if not node:
        return {"status": "unsupported", "reason": "node_not_found"}
    node = Path(node).resolve(strict=True)
    if not all(_plain_path(path) for path in (home, cache / "ms-playwright/daemon", temp, core)):
        return {"status": "skipped", "reason": "invalid_context"}
    context = {
        "schema": 1,
        "node_path": str(node),
        "installation_id": [_identity(path) for path in (
            executable, package / "package.json", core / "package.json",
            core / "lib/coreBundle.js", node,
        )],
        "socket_root": str(Path(temp) / ("pw-" + user_hash) / "cli"),
        "uid": os.getuid(),
        "home": str(home),
        "cache_root": str(cache / "ms-playwright/daemon"),
        "cli_path": str(executable),
        "cli_version": metadata["version"],
        "core_version": core_metadata["version"],
        "package_root": str(core),
        "cwd": os.getcwd(),
        "workspace_dir": _workspace(os.getcwd()),
        "namespace_id": _namespace(_workspace(os.getcwd()) or core),
    }
    return {"status": "ok", "context": context}


def _result(status, reason, counts=None):
    return {"status": status, "reason": reason, "counts": counts or {
        "matched": 0, "closed": 0, "absent": 0, "skipped": 0, "retry": 0,
    }}


def _same_context(expected):
    current = probe()
    if current["status"] != "ok" or not isinstance(expected, dict):
        return False
    actual = current["context"]
    local = {"cwd", "workspace_dir", "namespace_id"}
    if actual.keys() != expected.keys() or any(
        json.dumps(actual[key], sort_keys=True) != json.dumps(expected[key], sort_keys=True)
        for key in actual if key not in local
    ):
        return False
    return (Path(expected["cwd"]).is_dir()
            and _workspace(expected["cwd"]) == expected["workspace_dir"]
            and _namespace(expected["workspace_dir"] or expected["package_root"])
            == expected["namespace_id"])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _identity(path):
    if not _plain_path(path):
        raise ValueError("unsafe_path")
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("unsafe_file")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _read_json(path, limit=65536, owned=True):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe_path")
    # 逐级用 dir_fd + O_NOFOLLOW 打开，拒绝祖先链接和 FIFO；只读不创建。
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > limit
                    or info.st_nlink != 1 or (owned and info.st_uid != os.getuid())):
                raise ValueError("unsafe_file")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError("oversize_file")
        result = json.loads(data, object_pairs_hook=_unique_object)
        if not isinstance(result, dict):
            raise ValueError("invalid_object")
        return result
    finally:
        os.close(directory)


def _plain_path(path):
    path = Path(path)
    return path.is_absolute() and str(path) == str(path.resolve())


def _socket_path(context, namespace, name):
    root = context["socket_root"]
    if not root.isascii() or not _plain_path(root):
        raise ValueError("unsafe_socket_root")
    filename = namespace + "-" + name
    maximum = 103 - len(root) - 1 - len(".sock")
    if len(filename) > maximum:
        raise ValueError("unsupported_socket_length")
    return str(Path(root) / (filename + ".sock"))


def _session_cwd(config, record, name, context):
    if (not isinstance(config, dict)
            or set(config) - {"name", "version", "timestamp", "socketPath", "workspaceDir", "attached", "cli", "browser"}
            or config.get("name") != name
            or config.get("version") != CORE_VERSION
            or type(config.get("timestamp")) is not int
            or config["timestamp"] <= 0
            or not isinstance(config.get("browser"), dict)
            or not isinstance(config.get("cli"), dict)
            or set(config["cli"]) - {"persistent"}
            or config["cli"].get("persistent", False) is not False
            or config.get("attached", False) is not False):
        return None
    # 该 core 版本会省略 undefined：短期记录的 cli={}，attached 不出现。
    workspace = config.get("workspaceDir")
    if "workspaceDir" in config and (
        not isinstance(workspace, str) or not os.path.isabs(workspace)
        or _workspace(workspace) != workspace
    ):
        return None
    cwd = workspace or context["cwd"]
    if (not _plain_path(cwd) or not Path(cwd).is_dir()
            or _workspace(cwd) != workspace
            or record.parent.name != _namespace(workspace or context["package_root"])):
        return None
    socket_path = config.get("socketPath")
    if (socket_path != _socket_path(context, record.parent.name, name)
            or not _plain_path(socket_path)):
        return None
    if os.path.lexists(socket_path):
        info = os.lstat(socket_path)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != context["uid"]:
            return None
    return cwd


def _entries(path):
    entries = []
    with os.scandir(path) as iterator:
        for entry in iterator:
            if len(entries) >= 256:
                raise ValueError("registry_limit")
            entries.append(Path(entry.path))
    return entries


def _audit_registry(root):
    # 官方 close 也会 Registry.load 全量读取；先拒绝其他文件的伪名/错 hash，防止选中别的条目。
    if not _plain_path(root):
        raise ValueError("unsafe_registry")
    scanned = 0
    for namespace in _entries(root):
        if (not _plain_path(namespace) or not namespace.is_dir()
                or not re.fullmatch(r"[0-9a-f]{16}", namespace.name)):
            raise ValueError("unsafe_registry")
        for record in _entries(namespace):
            scanned += 1
            if scanned > 512:
                raise ValueError("registry_limit")
            if record.suffix != ".session":
                continue
            config = _read_json(record)
            if (config.get("name", record.stem) != record.stem
                    or (config.get("workspaceDir")
                        and (not isinstance(config["workspaceDir"], str)
                             or _namespace(config["workspaceDir"]) != namespace.name))):
                raise ValueError("ambiguous_registry")


def _socket_state(path):
    if not os.path.lexists(path):
        return "absent"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.15)
        try:
            connection.connect(path)
            return "live"
        except OSError:
            return "unknown"


def _connection_closed(connection):
    try:
        return connection.recv(1) == b""
    except (ConnectionResetError, BrokenPipeError):
        return True
    except OSError:
        return False


def _unchanged(record, config, cwd, name, context, allow_missing=False):
    try:
        if not _same_context(context) or _session_cwd(config, record, name, context) != cwd:
            return False
        _audit_registry(context["cache_root"])
        if allow_missing and not os.path.lexists(record):
            return True
        return _read_json(record) == config
    except Exception:
        return False


def _close(record, config, cwd, name, context, total_deadline):
    if time.monotonic() >= total_deadline:
        return "retry"
    socket_path = config["socketPath"]
    state = _socket_state(socket_path)
    if state == "absent":
        return "absent"
    if state != "live":
        return "retry"
    # 保留只读连接，避免把仅 unlink 路径、daemon 仍存活误判为关闭。
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.15)
        try:
            connection.connect(socket_path)
        except OSError:
            return "retry"
        for _ in range(2):
            if not _unchanged(record, config, cwd, name, context):
                return "skipped"
            remaining = total_deadline - time.monotonic()
            if remaining <= 0:
                return "retry"
            try:
                subprocess.run(
                    [context["cli_path"], "-s=" + name, "close"],
                    cwd=cwd, env=dict(os.environ, NO_UPDATE_NOTIFIER="1"),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=min(3, remaining), check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            deadline = min(total_deadline, time.monotonic() + 0.3)
            while time.monotonic() < deadline:
                if not _unchanged(record, config, cwd, name, context, allow_missing=True):
                    return "skipped"
                if _socket_state(socket_path) == "absent" and _connection_closed(connection):
                    return "closed"
                time.sleep(0.05)
    return "retry"


def cleanup(name, expected_context):
    """返回 status/reason/counts；counts 按目标记录计数，不是浏览器进程数。

    全局身份与原 probe cwd 的 namespace 必须不变；调用 cleanup 自身的 cwd 可以不同。
    多记录结果优先级为 retry > skipped > closed > absent，已完成部分保留在 counts 中。
    """
    try:
        return _cleanup(name, expected_context)
    except Exception:
        return _result("skipped", "invalid_context")


def _cleanup(name, expected_context):
    total_deadline = time.monotonic() + 8
    if not isinstance(name, str) or not re.fullmatch(r"cairn-task-[0-9a-f]{32}", name):
        return _result("skipped", "unmanaged_name")
    if not _same_context(expected_context):
        return _result("skipped", "context_mismatch")
    root = Path(expected_context["cache_root"])
    try:
        records = [namespace / (name + ".session") for namespace in _entries(root)
                   if os.path.lexists(namespace / (name + ".session"))]
    except FileNotFoundError:
        records = []
    except Exception:
        return _result("skipped", "unsafe_registry")
    if not records:
        return _result("absent", "no_matching_session")
    counts = {"matched": len(records), "closed": 0, "absent": 0, "skipped": 0, "retry": 0}
    try:
        _audit_registry(expected_context["cache_root"])
    except Exception:
        counts["skipped"] = len(records)
        return _result("skipped", "unsafe_registry", counts)
    for record in records:
        try:
            config = _read_json(record)
            cwd = _session_cwd(config, record, name, expected_context)
        except Exception:
            counts["skipped"] += 1
            continue
        if cwd is None:
            counts["skipped"] += 1
            continue
        counts[_close(record, config, cwd, name, expected_context, total_deadline)] += 1
    if counts["retry"]:
        return _result("retry", "close_unverified", counts)
    if counts["skipped"]:
        return _result("skipped", "unsafe_session", counts)
    if counts["closed"]:
        return _result("closed", "verified_closed", counts)
    return _result("absent", "no_live_session", counts)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv == ["probe"]:
            result = probe()
        elif len(argv) == 3 and argv[0] == "cleanup" and len(argv[2].encode()) <= 16384:
            result = cleanup(argv[1], json.loads(argv[2], object_pairs_hook=_unique_object))
        else:
            result = {"status": "skipped", "reason": "invalid_arguments"}
    except Exception:
        result = _result("skipped", "invalid_arguments")
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
