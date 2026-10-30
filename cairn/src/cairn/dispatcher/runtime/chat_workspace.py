"""会话材料工作区；命令只在无凭据、无网络的临时工具容器执行。"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import threading

import requests


WORKSPACE = "/home/kali/workspace"
MAX_MARKDOWN_BYTES = 1024 * 1024


class ChatWorkspace:
    def __init__(self, manager, project_id: str, conversation_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", conversation_id) or conversation_id in {".", ".."}:
            raise ValueError("chat_workspace_identity_invalid")
        self.manager = manager
        self.project_id = project_id
        self.directory = manager.chat_directory(project_id) / "workspace" / conversation_id
        if self.directory.is_symlink():
            raise ValueError("chat_workspace_path_invalid")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(self.directory, 1000, 1000)
        self.cwd = f"{WORKSPACE}/{conversation_id}"
        self._lock = threading.Lock()
        self._container = None
        self._closed = False

    def path(self, value: str) -> Path:
        if not isinstance(value, str) or not value or "\0" in value:
            raise ValueError("chat_workspace_path_invalid")
        path = PurePosixPath(value)
        if path.is_absolute():
            try:
                path = path.relative_to(self.cwd)
            except ValueError:
                raise ValueError("chat_workspace_path_invalid") from None
        if ".." in path.parts or not path.parts:
            raise ValueError("chat_workspace_path_invalid")
        result = self.directory
        for part in path.parts:
            result = result / part
            if result.is_symlink():
                raise ValueError("chat_workspace_path_invalid")
        return result

    def write(self, value: str, content: bytes) -> str:
        path = self.path(value)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for parent in [path.parent, *path.parent.parents]:
            if parent == self.directory:
                break
            os.chown(parent, 1000, 1000)
        # 不覆盖用户已处理的文件；相同原件重取保持幂等。
        if path.exists():
            if path.is_file() and path.stat().st_size == len(content) and path.read_bytes() == content:
                return f"{self.cwd}/{path.relative_to(self.directory)}"
            raise ValueError("chat_workspace_file_exists")
        with path.open("xb") as output:
            output.write(content)
        os.chmod(path, 0o600)
        os.chown(path, 1000, 1000)
        return f"{self.cwd}/{path.relative_to(self.directory)}"

    def read(self, value: str) -> str:
        path = self.path(value)
        if not path.is_file() or path.stat().st_size > MAX_MARKDOWN_BYTES:
            raise ValueError("chat_workspace_file_invalid")
        return path.read_bytes().decode("utf-8")

    def execute(self, params: dict) -> dict:
        if set(params) != {"command", "cwd", "timeout_seconds"}:
            raise ValueError("chat_workspace_command_invalid")
        command, timeout = params["command"], params["timeout_seconds"]
        if (not isinstance(command, str) or "\0" in command or type(timeout) not in (int, float)
                or not 0 < timeout <= 120 or params["cwd"] != self.cwd):
            raise ValueError("chat_workspace_command_invalid")
        parent = self.manager._require_container(self.manager.chat_container_name(self.project_id), project_id=self.project_id)
        container = None
        try:
            with self._lock:
                if self._closed:
                    raise ValueError("chat_workspace_closed")
                # 不继承控制容器的环境、PID空间、session、socket、研究挂载或网络。
                container = self.manager._client.containers.create(
                    parent.attrs["Config"]["Image"], ["/bin/bash", "--noprofile", "--norc", "-c", command],
                    user="1000:1000", init=True, network_mode="none", cap_drop=["ALL"],
                    security_opt=["no-new-privileges:true"], mem_limit="512m", pids_limit=64,
                    working_dir=self.cwd, environment={"HOME": "/tmp", "TMPDIR": "/tmp"},
                    volumes={str(self.directory): {"bind": self.cwd, "mode": "rw"}},
                    labels={"cairn.chat.workspace": self.project_id},
                )
                self._container = container
                container.start()
            try:
                status = container.wait(timeout=timeout)
            except requests.exceptions.Timeout:
                return {"error": {"code": "workspace_timeout", "message": "工作区命令超过本次时间限制。"}}
            output = bytearray()
            for chunk in container.logs(stream=True):
                output.extend(chunk)
                if len(output) > 512 * 1024:
                    return {"error": {"code": "workspace_output_too_large", "message": "命令输出过大，请将结果保存到工作区文件后按范围读取。"}}
            return {"exit_code": status["StatusCode"], "output": output.decode("utf-8", errors="replace")}
        finally:
            with self._lock:
                if container is not None and self._container is container:
                    container.remove(force=True)
                    self._container = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._container is not None:
                self._container.remove(force=True)
                self._container = None
