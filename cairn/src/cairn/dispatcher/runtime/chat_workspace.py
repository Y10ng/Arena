"""会话工作区；离线材料与获准研究共用文件，目标命令仅交给测试后端。"""
from __future__ import annotations

import base64
import http.client
import json
import logging
import socket
import os
from pathlib import Path, PurePosixPath
import re
import threading

import requests
from docker.errors import DockerException, NotFound


LOG = logging.getLogger(__name__)
WORKSPACE = "/home/kali/workspace"
MAX_MARKDOWN_BYTES = 1024 * 1024


class ChatWorkspace:
    def __init__(self, manager, conversation_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", conversation_id) or conversation_id in {".", ".."}:
            raise ValueError("chat_workspace_identity_invalid")
        self.manager = manager
        self.conversation_id = conversation_id
        self.directory = manager.chat_directory(conversation_id) / "workspace" / conversation_id
        if self.directory.is_symlink():
            raise ValueError("chat_workspace_path_invalid")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(self.directory, 1000, 1000)
        self.cwd = f"{WORKSPACE}/{conversation_id}"
        self._lock = threading.Lock()
        self._container = None
        self._closed = False
        self.research_execution = None

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
        return self.read_bytes(value).decode("utf-8")

    def read_bytes(self, value: str) -> bytes:
        path = self.path(value)
        if not path.is_file() or path.stat().st_size > MAX_MARKDOWN_BYTES:
            raise ValueError("chat_workspace_file_invalid")
        with path.open("rb") as source:
            content = source.read(MAX_MARKDOWN_BYTES + 1)
        if len(content) > MAX_MARKDOWN_BYTES:
            raise ValueError("chat_workspace_file_invalid")
        return content

    def execute(self, params: dict) -> dict:
        if set(params) != {"command", "cwd", "timeout_seconds"}:
            raise ValueError("chat_workspace_command_invalid")
        command, timeout = params["command"], params["timeout_seconds"]
        if (not isinstance(command, str) or "\0" in command or type(timeout) not in (int, float)
                or not 0 < timeout <= 120 or params["cwd"] != self.cwd):
            raise ValueError("chat_workspace_command_invalid")
        if self._closed:
            raise ValueError("chat_workspace_closed")
        if self.research_execution is not None:
            return self._execute_research(command, timeout)
        parent = self.manager._require_container(self.manager.chat_container_name(self.conversation_id), conversation_id=self.conversation_id)
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
                    labels={"cairn.chat.workspace": self.conversation_id, "cairn.conversation_id": self.conversation_id},
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
                    self._remove_command_container()

    def _execute_research(self, command, timeout):
        execution = self.research_execution
        if execution.stopping.is_set():
            raise ValueError("chat_research_stopping")
        state = execution.runtime.manifest["scopes"][execution.scope_id]
        connection = http.client.HTTPConnection("localhost", timeout=timeout + 10)
        transport = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        transport.settimeout(timeout + 10)
        output = bytearray()
        try:
            directory_fd = os.open(execution.runtime.execution / execution.scope_id, os.O_RDONLY | os.O_DIRECTORY)
            try:
                # 会话材料路径可能超过 AF_UNIX 限长，目录 FD 仍指向原私有 socket，不另建短路径副本。
                transport.connect(f"/proc/self/fd/{directory_fd}/execute.sock")
            finally:
                os.close(directory_fd)
            connection.sock = transport
            connection.request("POST", "/execute", body=json.dumps({
                "scope_id": execution.scope_id, "task_run_id": state["task_run_id"], "command": command,
                "cwd": self.cwd, "timeout_seconds": timeout, "env": {}, "stdin_base64": "",
            }), headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("chat_research_execution_rejected")
            while line := response.readline(256 * 1024):
                event = json.loads(line)
                if event.get("error"):
                    raise ValueError("chat_research_execution_failed")
                for side in ("stdout", "stderr"):
                    if side in event:
                        output.extend(base64.b64decode(event[side], validate=True))
                if len(output) > 512 * 1024:
                    return {"error": {"code": "workspace_output_too_large", "message": "输出过大，请保存到当前工作区后按范围读取。"}}
                if type(event.get("exit_code")) is int:
                    return {"exit_code": event["exit_code"], "output": output.decode("utf-8", errors="replace"),
                            "scope_id": execution.scope_id}
            raise ValueError("chat_research_result_missing")
        finally:
            connection.close()
            transport.close()

    def _remove_command_container(self):
        identifier = self._container.id
        try:
            self._container.remove(force=True)
        except NotFound:
            pass
        try:
            self.manager._client.containers.get(identifier)
        except NotFound:
            self._container = None
        else:
            raise RuntimeError("chat_workspace_termination_unconfirmed")

    @property
    def execution_completed(self):
        with self._lock:
            return self._closed and self._container is None and (
                self.research_execution is None or self.research_execution.execution_completed)

    def cancel(self, reason):
        # 离线材料命令也可能在途；不能等私有桥超时才停止，更不能阻塞控制进程的取消。
        threading.Thread(target=self.close, name="chat-workspace-stop", daemon=True).start()

    def close(self) -> None:
        try:
            with self._lock:
                self._closed = True
                if self._container is not None:
                    self._remove_command_container()
        except (DockerException, requests.RequestException, OSError, RuntimeError):
            # 保留资源引用和未确认事实，由轮次收口拒绝报告终态。
            LOG.warning("chat workspace termination unconfirmed")
        if self.research_execution is not None:
            self.research_execution.stop("chat_turn_finished")
