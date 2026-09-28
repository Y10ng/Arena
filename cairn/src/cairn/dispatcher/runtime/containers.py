"""Docker 容器管理层，封装项目容器复用、运行时签名和文件读写。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from hashlib import sha256
from ipaddress import IPv6Address
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import threading
import time
from typing import Callable, Literal
import uuid
from urllib.parse import urlsplit, urlunsplit

import docker
from docker.errors import APIError, DockerException, NotFound
from docker.models.containers import Container
from requests import PreparedRequest
from requests.exceptions import RequestException

from cairn.dispatcher.config import ContainerConfig
from cairn.dispatcher.runtime.browser_sessions import BrowserSession, BrowserSessions
from cairn.dispatcher.runtime.container_archives import text_file_archive
from cairn.dispatcher.runtime.container_options import ContainerRuntimeOptions
from cairn.dispatcher.runtime.docker_stats import cpu_percent_from_stats, int_value
from cairn.dispatcher.runtime.exec_control import (
    EXEC_CLEANUP,
    EXEC_TERMINATOR,
    EXEC_VERIFY_DRAINED,
    EXEC_VERIFY_EXIT,
    EXEC_VERIFY_RUNNING,
)
from cairn.dispatcher.runtime.process import ManagedProcess
from cairn.dispatcher.runtime.temporary_files import runtime_temp_dir
from cairn.dispatcher.workers.base import REPORT_READONLY_INPUT_ROOT

LOG = logging.getLogger(__name__)
CONTAINER_ARCHIVE_SPOOL_BYTES = 4 * 1024 * 1024
CONTAINER_FILE_COPY_BYTES = 1024 * 1024
BROWSER_HELPER_TIMEOUT_SECONDS = 12
_EXEC_CONTROL_PATH_RE = re.compile(r"^/tmp/cairn-exec-[0-9a-f]{32}\.json$")
_EXEC_CONTROL_TIMEOUT_SECONDS = 2.0


def canonical_server_address(server: str) -> str:
    """规范化 Server 地址；凭据、query 和 fragment 不进入本地 namespace。"""
    try:
        parsed = urlsplit(server.strip())
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if scheme not in {"http", "https"} or not host:
            raise ValueError
        authority = (
            f"[{IPv6Address(host).compressed}]"
            if ":" in host
            else host.encode("idna").decode("ascii")
        )
        if port is not None and (scheme, port) not in {
            ("http", 80),
            ("https", 443),
        }:
            authority = f"{authority}:{port}"
        sanitized = urlunsplit((scheme, authority, parsed.path, "", ""))
        prepared = PreparedRequest()
        prepared.prepare_url(sanitized, None)
        if not isinstance(prepared.url, str):
            raise ValueError
        # namespace 与 requests 实际发送前的 dot-segment/percent normalization 保持一致。
        normalized_path = urlsplit(prepared.url).path.rstrip("/")
        return urlunsplit((scheme, authority, normalized_path, "", ""))
    except (RequestException, ValueError, UnicodeError):
        raise ValueError(
            "invalid dispatcher container ownership configuration"
        ) from None


def container_instance_id(dispatcher_id: str, server: str) -> str:
    """绑定稳定 Dispatcher 与 canonical Server 地址。"""
    dispatcher = dispatcher_id.strip()
    if not dispatcher:
        raise ValueError(
            "invalid dispatcher container ownership configuration"
        )
    address = canonical_server_address(server)
    identity = json.dumps(
        [dispatcher, address],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return sha256(identity.encode("utf-8")).hexdigest()


class ContainerOwnershipError(RuntimeError):
    """容器不在本pi实例命名空间，或没有匹配的归属标签。"""


class ContainerRuntimeMismatchError(RuntimeError):
    """运行边界变化时保留原工作区，阻止以删除重建替代恢复。"""


def _controlled_container_path(path: str, allowed_root: str) -> tuple[PurePosixPath, PurePosixPath]:
    """只接受受控根目录下的规范绝对路径，拒绝根目录本身和词法穿越。"""
    target = PurePosixPath(path)
    root = PurePosixPath(allowed_root)
    if (
        not target.is_absolute()
        or not root.is_absolute()
        or str(target) != path
        or str(root) != allowed_root
        or target == root
        or any(part in {"", ".", ".."} for part in target.parts[1:])
        or any(part in {"", ".", ".."} for part in root.parts[1:])
    ):
        raise ValueError("container path must be a canonical child of the allowed root")
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"container file is outside allowed root: {path}") from exc
    return target, root


def _readonly_input_directory(path: str) -> PurePosixPath:
    target, root = _controlled_container_path(path, REPORT_READONLY_INPUT_ROOT)
    if target.parent != root or not re.fullmatch(r"[a-f0-9]{32}", target.name):
        raise ValueError("invalid readonly input directory")
    return target


def _docker_exec_succeeded(result: object) -> bool:
    """Docker helper 只有返回 literal integer zero 才视为成功。"""
    exit_code = getattr(result, "exit_code", None)
    return type(exit_code) is int and exit_code == 0


@dataclass(frozen=True, slots=True)
class CandidateExecutionCheck:
    """一次 exact candidate 执行检查的可操作结论。"""

    status: Literal["running", "stopped", "gone", "unknown"]
    reason: str
    exit_code: int | None = None


@dataclass(slots=True)
class MaterializedContainerFile:
    """容器文件在宿主机上的临时副本，调用方负责及时清理。"""

    path: Path
    size: int

    def cleanup(self) -> None:
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> MaterializedContainerFile:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.cleanup()


class ContainerManager:
    """按项目管理长生命周期容器，并提供 exec/file/artifact 辅助能力。"""

    _PREFIX = "cairn-pi-dispatch-"
    _STARTUP_PREFIX = "cairn-pi-healthcheck-"
    _LABEL_PROJECT_ID = "cairn.project_id"
    _LABEL_SCENARIO_KEY = "cairn.scenario_key"
    _LABEL_RUNTIME_SIGNATURE = "cairn.runtime_signature"
    _LABEL_INSTANCE_ID = "cairn.pi.instance_id"
    _LABEL_ENGINE = "cairn.engine"

    def __init__(self, config: ContainerConfig, *, instance_id: str):
        if not re.fullmatch(r"[a-f0-9]{64}", instance_id):
            raise ValueError("container instance_id must be a SHA256 identity")
        self.instance_id = instance_id
        self._project_prefix = f"{self._PREFIX}{instance_id[:16]}-"
        self._startup_prefix = f"{self._STARTUP_PREFIX}{instance_id[:16]}-"
        self._config = config
        # Docker 控制请求必须有界；输出流由 ManagedProcess 单独关闭和核验终态。
        self._client = docker.from_env(timeout=5)
        self._ensure_running_locks: dict[str, threading.Lock] = {}
        self._ensure_running_locks_guard = threading.Lock()
        self._container_user_ids_cache: dict[tuple[str, str], tuple[int, int]] = {}
        self.browser_sessions = BrowserSessions()
        self._browser_session_policy = "legacy"
        self._browser_helper_slots = threading.BoundedSemaphore(4)

    def configure_browser_sessions(self, policy: str) -> None:
        if policy not in ("legacy", "task"):
            raise ValueError("unknown browser session policy")
        self._browser_session_policy = policy

    def docker_daemon_id(self) -> str:
        """读取当前 Docker daemon 身份，不能用 endpoint 可达性替代。"""
        try:
            info = self._client.info()
        except (DockerException, RequestException) as exc:
            raise RuntimeError("docker daemon identity is unavailable") from exc
        daemon_id = info.get("ID") if isinstance(info, dict) else None
        if not isinstance(daemon_id, str) or not daemon_id.strip():
            raise RuntimeError("docker daemon identity is unavailable")
        return daemon_id.strip()

    def prepare_browser_session(
        self,
        container_name: str,
        env: dict[str, str],
        *,
        project_id: str | None,
        task_run_id: str | None,
    ) -> tuple[dict[str, str], BrowserSession | None]:
        process_env = dict(env)
        if not project_id or not task_run_id:
            return process_env, None
        with self._ensure_running_lock(container_name):
            container = self._require_container(container_name)
            if container.attrs["Config"]["Labels"].get(self._LABEL_PROJECT_ID) != project_id:
                raise ContainerOwnershipError("browser task project does not match its container")
            policy = self.browser_sessions.policy_for_task(
                project_id, task_run_id, self._browser_session_policy,
            )
            if "PLAYWRIGHT_CLI_SESSION" in env:
                self.browser_sessions.keep_unmanaged(project_id, task_run_id)
                return process_env, None
            if policy != "task":
                return process_env, None
            inherited_env = container.attrs.get("Config", {}).get("Env") or []
            if any(item.partition("=")[0] == "PLAYWRIGHT_CLI_SESSION" for item in inherited_env):
                self.browser_sessions.keep_unmanaged(project_id, task_run_id)
                return process_env, None
            safe_env = {
                key: value for key, value in env.items()
                if key in {
                    "HOME", "XDG_CACHE_HOME", "PATH", "USER", "USERNAME",
                    "TMPDIR", "TMP", "TEMP",
                }
            }
            unsafe_environment = any(
                value for key, value in env.items()
                if key.startswith("PWTEST_") or key in {
                    "PLAYWRIGHT_CLI_VERSION_FOR_TEST", "NODE_OPTIONS", "NODE_PATH",
                }
            )
            existing = self.browser_sessions.bound_session(project_id, task_run_id)
            if existing is not None:
                if (
                    existing.container_id != container.id or unsafe_environment
                    or {k: v for k, v in existing.env if k != "PATH"}
                    != {k: v for k, v in safe_env.items() if k != "PATH"}
                ):
                    self.browser_sessions.keep_unmanaged(project_id, task_run_id)
                # 后续阶段仅复用绑定；CLI/context 在实际 cleanup 时重新核验。
                # 探测拥塞或超时不能把已经打开的页面切换成 default。
                process_env["PLAYWRIGHT_CLI_SESSION"] = existing.name
                return process_env, existing
            if unsafe_environment:
                self.browser_sessions.keep_unmanaged(project_id, task_run_id)
                return process_env, None
            probe = self._run_browser_helper(container.id, safe_env, ["probe"])
            if probe.get("status") != "ok" or not isinstance(probe.get("context"), dict):
                self.browser_sessions.keep_unmanaged(project_id, task_run_id)
                return process_env, None
            session = self.browser_sessions.prepare(
                project_id, task_run_id, container.id,
                env=safe_env, context=probe["context"],
            )
            if session is not None:
                process_env["PLAYWRIGHT_CLI_SESSION"] = session.name
            return process_env, session

    def cleanup_browser_session(self, session: BrowserSession) -> str:
        with self._ensure_running_lock(self.container_name(session.project_id)):
            if not self.browser_sessions.cleanup_allowed(session):
                return "skipped"
            identities = self.browser_sessions.execution_ids(session)
            if not identities:
                return "skipped"
            try:
                container = self._get_container(session.container_id)
                if container is None or container.id != session.container_id:
                    return "skipped"
                if container.attrs["Config"]["Labels"].get(self._LABEL_PROJECT_ID) != session.project_id:
                    return "skipped"
                if container.status != "running":
                    return "absent"
                for execution in identities:
                    state = self._client.api.exec_inspect(execution)
                    if state.get("ContainerID") != session.container_id:
                        return "skipped"
                    if state.get("Running") is not False:
                        return "retry"
                response = self._run_browser_helper(
                    session.container_id, dict(session.env),
                    ["cleanup", session.name, session.context_json],
                )
                status = response.get("status")
                return status if status in {"closed", "absent", "skipped", "retry"} else "skipped"
            except ContainerOwnershipError:
                return "skipped"
            except NotFound:
                # 容器已消失或 exec 身份失效，均不重新启动或按同名容器补做关闭。
                return "skipped"
            except Exception as exc:
                LOG.warning("browser cleanup failed error=%s", type(exc).__name__)
                return "retry"

    def _run_browser_helper(
        self, container_id: str, env: dict[str, str], arguments: list[str],
    ) -> dict[str, object]:
        source = Path(__file__).with_name("browser_session_cleanup.py").read_text(encoding="utf-8")
        done = threading.Event()
        response: dict[str, object] = {"status": "retry"}

        def execute() -> None:
            try:
                self._require_container(container_id)
                execution = self._client.api.exec_create(
                    container_id, ["python3", "-c", source, *arguments],
                    stdout=True, stderr=True, stdin=False, tty=False, environment=env,
                )["Id"]
                output = bytearray()
                oversized = False
                for stdout, _stderr in self._client.api.exec_start(execution, stream=True, demux=True):
                    if stdout:
                        if len(output) + len(stdout) > 16384:
                            oversized = True
                        elif not oversized:
                            output.extend(stdout)
                state = self._client.api.exec_inspect(execution)
                exit_code = state.get("ExitCode") if isinstance(state, dict) else None
                if (
                    not oversized
                    and isinstance(state, dict)
                    and state.get("ContainerID") == container_id
                    and state.get("Running") is False
                    and type(exit_code) is int
                    and exit_code == 0
                ):
                    payload = json.loads(output)
                    if isinstance(payload, dict):
                        response.clear()
                        response.update(payload)
            except Exception as exc:
                LOG.warning("browser helper failed error=%s", type(exc).__name__)
            finally:
                self._browser_helper_slots.release()
                done.set()

        # helper 自带八秒截止；超时仅放弃等待，不能用宿主 PID 在容器内强杀。
        if not self._browser_helper_slots.acquire(blocking=False):
            return {"status": "retry"}
        try:
            threading.Thread(target=execute, daemon=True).start()
        except Exception:
            self._browser_helper_slots.release()
            raise
        if not done.wait(BROWSER_HELPER_TIMEOUT_SECONDS):
            return {"status": "retry"}
        return response

    def candidate_execution_status(
        self,
        *,
        docker_daemon_id: str | None,
        container_id: str,
        container_name: str,
        project_id: str,
        exec_id: str,
        control_path: str,
    ) -> CandidateExecutionCheck:
        """区分活执行、可信终止、同一 daemon 上消失与不可判定。"""
        if _EXEC_CONTROL_PATH_RE.fullmatch(control_path) is None:
            return CandidateExecutionCheck("unknown", "execution_control_invalid")
        if not isinstance(docker_daemon_id, str) or not docker_daemon_id:
            return CandidateExecutionCheck("unknown", "docker_daemon_identity_missing")
        try:
            current_daemon_id = self.docker_daemon_id()
        except RuntimeError:
            return CandidateExecutionCheck("unknown", "docker_daemon_unavailable")
        if current_daemon_id != docker_daemon_id:
            return CandidateExecutionCheck("unknown", "docker_daemon_changed")
        try:
            container = self._get_container(container_id)
        except (ContainerOwnershipError, DockerException, RequestException, RuntimeError):
            return CandidateExecutionCheck("unknown", "container_identity_unconfirmed")
        if container is None:
            return CandidateExecutionCheck("gone", "container_missing_on_same_daemon")
        try:
            self._assert_owned(container, project_id=project_id)
        except ContainerOwnershipError:
            return CandidateExecutionCheck("unknown", "container_ownership_mismatch")
        if (
            container.id != container_id
            or str(getattr(container, "name", "")).lstrip("/") != container_name
        ):
            return CandidateExecutionCheck("unknown", "container_identity_mismatch")
        try:
            container_status = self._container_state(container)
        except (ContainerOwnershipError, DockerException, RequestException, RuntimeError):
            return CandidateExecutionCheck("unknown", "container_state_unconfirmed")
        # 精确容器已停止时进程组不可能仍运行；保留的 exec 记录不应触发容器内新 exec。
        if container_status in {"created", "exited", "dead"}:
            return CandidateExecutionCheck("stopped", "container_not_running")
        if container_status != "running":
            return CandidateExecutionCheck("unknown", "container_state_unconfirmed")
        try:
            state = self._client.api.exec_inspect(exec_id)
        except NotFound:
            try:
                container.reload()
                container_status = str(getattr(container, "status", ""))
                if container_status in {"created", "exited", "dead"}:
                    return CandidateExecutionCheck(
                        "stopped",
                        "container_not_running",
                    )
                drained = self._run_candidate_control(
                    container_id,
                    ["python3", "-c", EXEC_VERIFY_DRAINED, control_path],
                )
            except (DockerException, RequestException, RuntimeError):
                return CandidateExecutionCheck("unknown", "exec_missing_process_unconfirmed")
            if drained == 0:
                return CandidateExecutionCheck("stopped", "exec_missing_descendants_drained")
            return CandidateExecutionCheck("unknown", "exec_missing_process_unconfirmed")
        except (DockerException, RequestException):
            return CandidateExecutionCheck("unknown", "exec_inspection_unavailable")
        if not isinstance(state, dict) or state.get("ContainerID") != container_id:
            return CandidateExecutionCheck("unknown", "exec_identity_mismatch")
        if state.get("Running") is True:
            try:
                running_control = self._run_candidate_control(
                    container_id,
                    ["python3", "-c", EXEC_VERIFY_RUNNING, control_path],
                )
            except (DockerException, RequestException, RuntimeError):
                return CandidateExecutionCheck("unknown", "execution_control_unconfirmed")
            if running_control != 0:
                return CandidateExecutionCheck("unknown", "execution_control_unconfirmed")
            return CandidateExecutionCheck("running", "exec_running")
        exit_code = state.get("ExitCode")
        if (
            state.get("Running") is not False
            or not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
            or exit_code < 0
        ):
            return CandidateExecutionCheck("unknown", "exec_terminal_state_invalid")
        try:
            verified = self._run_candidate_control(
                container_id,
                [
                    "python3",
                    "-c",
                    EXEC_VERIFY_EXIT,
                    control_path,
                    str(exit_code),
                ],
            )
        except (DockerException, RequestException, RuntimeError):
            return CandidateExecutionCheck("unknown", "termination_proof_unavailable")
        if verified != 0:
            return CandidateExecutionCheck("unknown", "termination_proof_unavailable")
        return CandidateExecutionCheck("stopped", "exec_terminal_descendants_drained", exit_code)

    def terminate_candidate_execution(
        self,
        *,
        docker_daemon_id: str | None,
        container_id: str,
        container_name: str,
        project_id: str,
        exec_id: str,
        control_path: str,
        reserve_attempt: Callable[[], CandidateExecutionCheck | None] | None = None,
    ) -> CandidateExecutionCheck:
        """只对仍有 v2 live control 的精确执行发送信号；调用方可先持久预留干预预算。"""
        identity = dict(
            docker_daemon_id=docker_daemon_id,
            container_id=container_id,
            container_name=container_name,
            project_id=project_id,
            exec_id=exec_id,
            control_path=control_path,
        )
        before = self.candidate_execution_status(**identity)
        if before.status != "running":
            return before
        if reserve_attempt is not None:
            try:
                reservation = reserve_attempt()
            except Exception:
                return CandidateExecutionCheck("unknown", "task_termination_control_unavailable")
            if reservation is not None:
                return reservation if isinstance(reservation, CandidateExecutionCheck) else CandidateExecutionCheck(
                    "unknown", "task_termination_control_unavailable"
                )
        try:
            # TERM + KILL 的助手最长约 15 秒，不能用核验专用的 2 秒预算。
            self._run_candidate_control(
                container_id,
                ["python3", "-c", EXEC_TERMINATOR, control_path],
                timeout=18.0,
            )
        except (DockerException, RequestException, RuntimeError):
            return CandidateExecutionCheck("unknown", "task_termination_control_unavailable")
        # 助手返回 0、404 或 .cancel 文件都不是停止证明；复核原 exec 与后代。
        after = self.candidate_execution_status(**identity)
        if after.status == "running":
            return CandidateExecutionCheck("unknown", "task_termination_unconfirmed")
        return after

    def cleanup_candidate_execution_control(
        self,
        *,
        container_id: str,
        container_name: str,
        project_id: str,
        control_path: str,
    ) -> None:
        """仅在 fence 释放后删除精确且受控的 terminal proof。"""
        if _EXEC_CONTROL_PATH_RE.fullmatch(control_path) is None:
            raise ValueError("invalid candidate execution control path")
        container = self._require_container(container_id, project_id=project_id)
        if (
            container.id != container_id
            or str(getattr(container, "name", "")).lstrip("/") != container_name
        ):
            raise RuntimeError("candidate execution container identity mismatch")
        try:
            status = self._run_candidate_control(
                container_id,
                ["python3", "-c", EXEC_CLEANUP, control_path],
            )
        except DockerException as exc:
            raise RuntimeError(
                "failed to clean candidate execution control proof"
            ) from exc
        if status != 0:
            raise RuntimeError("failed to clean candidate execution control proof")

    def _run_candidate_control(
        self,
        container_id: str,
        command: list[str],
        *,
        timeout: float = _EXEC_CONTROL_TIMEOUT_SECONDS,
    ) -> int | None:
        control = self._client.api.exec_create(
            container_id,
            command,
            stdout=False,
            stderr=False,
            stdin=False,
            tty=False,
        )
        control_id = control.get("Id") if isinstance(control, dict) else None
        if not isinstance(control_id, str) or not control_id:
            raise RuntimeError("candidate control exec identity is unavailable")
        self._client.api.exec_start(control_id, detach=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self._client.api.exec_inspect(control_id)
            if not isinstance(state, dict):
                raise RuntimeError("candidate control exec state is invalid")
            if state.get("ContainerID") != container_id:
                raise RuntimeError(
                    "candidate control exec container identity is invalid"
                )
            exit_code = state.get("ExitCode")
            if state.get("Running") is False:
                if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                    raise RuntimeError("candidate control exec exit code is invalid")
                return exit_code
            time.sleep(0.05)
        return None

    def update_config(self, config: ContainerConfig) -> None:
        self._config = config

    def close(self) -> None:
        self._client.close()

    def container_name(self, project_id: str) -> str:
        sanitized = project_id.replace("/", "-")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", sanitized):
            raise ValueError("invalid project container identity")
        return f"{self._project_prefix}{sanitized}"

    def project_id_from_container_name(self, name: str) -> str | None:
        # 归属校验后返回原始项目标签，避免名称转义导致orphan误判。
        if not name.startswith(self._project_prefix):
            return None
        container = self._get_container(name)
        if container is None:
            return None
        return str(container.attrs["Config"]["Labels"][self._LABEL_PROJECT_ID])

    def ensure_running(self, project_id: str, options: ContainerRuntimeOptions | None = None) -> str:
        name = self.container_name(project_id)
        runtime_options = options or self.default_runtime_options()
        # 同一项目容器可能被多个调度线程触发，按容器名加锁避免并发创建冲突。
        with self._ensure_running_lock(name):
            return self._ensure_running_locked(
                project_id,
                name,
                options=runtime_options,
            )

    def activate_for_readiness(
        self,
        project_id: str,
        options: ContainerRuntimeOptions,
        *,
        register: Callable[[str], str],
        finish: Callable[[str, str | None], None],
    ) -> str:
        """先持久登记激活，再启动容器；daemon 变化时不伪造完成证明。"""
        name = self.container_name(project_id)
        with self._ensure_running_lock(name):
            daemon_id = self.docker_daemon_id()
            token = register(daemon_id)
            try:
                return self._ensure_running_locked(project_id, name, options=options)
            finally:
                if self.docker_daemon_id() == daemon_id:
                    container = self._get_container(name)
                    finish(token, container.id if container is not None else None)

    def activate_existing_for_continuation(
        self,
        project_id: str,
        options: ContainerRuntimeOptions,
        expected_container_id: str,
        *,
        register: Callable[[str], str],
        finish: Callable[[str, str], None],
        reuse: Callable[[str, str], bool],
    ) -> str:
        """在锁内启动停止的原工作区，或复用本代已确认激活的同一容器。"""
        if not isinstance(expected_container_id, str) or not expected_container_id:
            raise ValueError("continuation requires exact container id")
        name = self.container_name(project_id)
        with self._ensure_running_lock(name):
            daemon_id = self.docker_daemon_id()
            container = self._require_container(name, project_id=project_id)
            if container.id != expected_container_id or self._container_runtime_mismatch(container, options):
                raise ContainerOwnershipError("continuation container identity or runtime mismatch")
            state = self._container_state(container)
            if state == "running":
                if not reuse(daemon_id, expected_container_id):
                    raise RuntimeError("continuation activation is not confirmed for this generation")
                if self.docker_daemon_id() != daemon_id:
                    raise RuntimeError("continuation Docker daemon changed during reuse")
                current = self._require_container(name, project_id=project_id)
                if (current.id != expected_container_id or self._container_runtime_mismatch(current, options)
                        or self._container_state(current) != "running"):
                    raise ContainerOwnershipError("continuation container changed during reuse")
                return name
            if state not in {"exited", "created"}:
                raise RuntimeError("continuation container is not stopped")
            token = register(daemon_id)
            if not isinstance(token, str) or not token:
                raise RuntimeError("continuation activation token is unavailable")
            if self.docker_daemon_id() != daemon_id:
                raise RuntimeError("continuation Docker daemon changed before start")
            current = self._require_container(name, project_id=project_id)
            if current.id != expected_container_id or self._container_runtime_mismatch(current, options):
                raise ContainerOwnershipError("continuation container changed before start")
            if self._container_state(current) not in {"exited", "created"}:
                raise RuntimeError("continuation container state changed before start")
            self._start_existing(name)
            if self.docker_daemon_id() != daemon_id:
                raise RuntimeError("continuation Docker daemon changed after start")
            current = self._require_container(name, project_id=project_id)
            if (
                current.id != expected_container_id
                or self._container_runtime_mismatch(current, options)
                or self._container_state(current) != "running"
            ):
                raise ContainerOwnershipError("continuation container changed after start")
            finish(token, expected_container_id)
            return name

    def default_runtime_options(self) -> ContainerRuntimeOptions:
        return ContainerRuntimeOptions.build(
            image=self._config.image,
            network_mode=self._config.network_mode,
            dns=self._config.dns,
            cap_add=self._config.cap_add,
            features={},
            scenario_key="default",
        )

    def _ensure_running_locked(self, project_id: str, name: str, *, options: ContainerRuntimeOptions) -> str:
        container = self._get_container(name)
        state = self._container_state(container)
        if container is not None and container.attrs["Config"]["Labels"].get(self._LABEL_PROJECT_ID) != project_id:
            raise ContainerOwnershipError("container project identity does not match the requested project")
        if container is not None and self._container_runtime_mismatch(container, options):
            # 工作区只存在于原容器中；运行边界变化时保留材料并明确阻断，而非删除重建。
            raise ContainerRuntimeMismatchError(
                "container runtime differs from scenario; preserved project workspace; "
                f"project={project_id} container={name} scenario={options.scenario_key}"
            )

        if state == "running":
            LOG.debug("container already running project=%s container=%s", project_id, name)
            return name
        if state is not None:
            LOG.info("starting existing container project=%s container=%s state=%s", project_id, name, state)
            self._start_existing(name)
            return name
        LOG.info(
            "creating container project=%s container=%s image=%s scenario=%s caps=%s dns=%s",
            project_id,
            name,
            options.image,
            options.scenario_key,
            list(options.cap_add),
            list(options.dns) or "docker-default",
        )
        try:
            self._client.containers.run(
                options.image,
                ["sleep", "infinity"],
                detach=True,
                init=True,
                name=name,
                network_mode=options.network_mode,
                dns=list(options.dns) or None,
                cap_add=list(options.cap_add) or None,
                mem_limit=options.mem_limit,
                memswap_limit=options.memswap_limit,
                pids_limit=options.pids_limit,
                volumes=self._advisory_volume_bind(options),
                labels=self._container_labels(project_id, options),
            )
            self._require_created_running_container(
                name,
                project_id=project_id,
                runtime_signature=options.runtime_signature,
            )
            LOG.info("created container project=%s container=%s", project_id, name)
            return name
        except APIError as exc:
            if not self._is_name_conflict(exc):
                raise RuntimeError(f"failed to create container {name}: {exc}") from exc
        LOG.info("container name conflict, checking owned container project=%s container=%s", project_id, name)
        container = self._get_container(name)
        state = self._container_state(container)
        if container is not None and (
            container.attrs["Config"]["Labels"].get(self._LABEL_PROJECT_ID) != project_id
            or self._container_runtime_mismatch(container, options)
        ):
            raise ContainerOwnershipError("conflicting container project or runtime signature does not match")
        if state == "running":
            return name
        if state is not None:
            LOG.info("starting conflicted existing container project=%s container=%s state=%s", project_id, name, state)
            self._start_existing(name)
            return name
        raise RuntimeError(f"failed to create container {name}")

    @contextmanager
    def _ensure_running_lock(self, name: str):
        with self._ensure_running_locks_guard:
            lock = self._ensure_running_locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._ensure_running_locks[name] = lock
        with lock:
            # Docker 操作跨线程、进程共用同一边界；进程退出时内核自动释放文件锁。
            path = runtime_temp_dir() / f"container-{sha256(name.encode()).hexdigest()}.lock"
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "rb") as file:
                fcntl.flock(file, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(file, fcntl.LOCK_UN)

    def create_startup_container(self) -> str:
        name = f"{self._startup_prefix}{uuid.uuid4().hex[:12]}"
        # 启动健康检查使用独立容器，不污染任何项目容器状态。
        LOG.debug("creating startup healthcheck container container=%s image=%s", name, self._config.image)
        try:
            self._client.containers.run(
                self._config.image,
                ["sleep", "infinity"],
                detach=True,
                init=True,
                name=name,
                network_mode=self._config.network_mode,
                dns=self._config.dns or None,
                cap_add=self._config.cap_add or None,
                volumes=None,
                labels={
                    self._LABEL_INSTANCE_ID: self.instance_id,
                    self._LABEL_ENGINE: "pi",
                    self._LABEL_PROJECT_ID: "startup-healthcheck",
                    self._LABEL_SCENARIO_KEY: "startup",
                    self._LABEL_RUNTIME_SIGNATURE: "startup",
                },
            )
            self._require_created_running_container(
                name,
                project_id="startup-healthcheck",
                runtime_signature="startup",
            )
        except DockerException as exc:
            raise RuntimeError(f"failed to create startup container {name}: {exc}") from exc
        return name

    def inspect_state(self, name: str) -> str | None:
        container = self._get_container(name)
        return self._container_state(container)

    def runtime_metadata(self, name: str) -> dict[str, str]:
        # 元数据采集失败不影响任务执行，监控中用 unknown 占位。
        metadata = {
            "container_name": name,
            "container_id": "unknown",
            "container_state": "unknown",
            "image_id": "unknown",
            "image_digest": "unknown",
        }
        try:
            container = self._require_container(name)
            container.reload()
            metadata["container_id"] = container.id
            state = container.attrs.get("State", {}).get("Status")
            if state:
                metadata["container_state"] = str(state)
            image_id = container.attrs.get("Image")
            if image_id:
                metadata["image_id"] = str(image_id)
            repo_digests = container.image.attrs.get("RepoDigests") or []
            if repo_digests:
                metadata["image_digest"] = str(repo_digests[0])
        except DockerException as exc:
            LOG.debug("failed to collect container runtime metadata container=%s error=%s", name, exc)
        except RuntimeError as exc:
            LOG.debug("failed to collect container runtime metadata container=%s error=%s", name, exc)
        return metadata

    def oom_kill_count(self, name: str) -> int | None:
        """读取项目 cgroup 的累计 oom_kill 计数；不可用时返回 None。"""
        try:
            container = self._require_container(name)
            result = container.exec_run(
                ["cat", "/sys/fs/cgroup/memory.events"],
                demux=False,
            )
        except (DockerException, RuntimeError):
            return None
        if not _docker_exec_succeeded(result):
            return None
        output = getattr(result, "output", b"")
        if isinstance(output, tuple):
            output = b"".join(part or b"" for part in output)
        if isinstance(output, bytes):
            text = output.decode("utf-8", errors="replace")
        else:
            text = str(output or "")
        for line in text.splitlines():
            key, _, raw_value = line.partition(" ")
            if key != "oom_kill":
                continue
            try:
                return int(raw_value.strip())
            except ValueError:
                return None
        return None

    def detect_oom_kill(
        self,
        name: str,
        *,
        before_count: int | None,
    ) -> tuple[bool, str | None]:
        """仅依据 cgroup 增量或 Docker OOMKilled 状态给出正向证据。"""
        after_count = self.oom_kill_count(name)
        if (
            before_count is not None
            and after_count is not None
            and after_count > before_count
        ):
            return True, "cgroup_v2_memory_events"
        try:
            container = self._require_container(name)
            container.reload()
            if container.attrs.get("State", {}).get("OOMKilled") is True:
                return True, "docker_state_oom_killed"
        except (DockerException, RuntimeError):
            pass
        return False, None

    def resource_snapshot(self, name: str) -> dict[str, object]:
        # Docker stats 是 best-effort 快照，仅用于监控，不作为调度判定。
        snapshot: dict[str, object] = {
            "container_name": name,
            "source": "docker_stats_once",
            "status": "unknown",
            "cpu_percent": None,
            "memory_usage_bytes": None,
            "memory_limit_bytes": None,
            "memory_percent": None,
            "pids_current": None,
        }
        try:
            container = self._require_container(name)
            stats = container.stats(stream=False)
        except DockerException as exc:
            LOG.debug("failed to collect container resource snapshot container=%s error=%s", name, exc)
            return snapshot
        except RuntimeError as exc:
            LOG.debug("failed to collect container resource snapshot container=%s error=%s", name, exc)
            return snapshot
        if not isinstance(stats, dict):
            return snapshot

        snapshot["status"] = "observed"
        snapshot["cpu_percent"] = cpu_percent_from_stats(stats)

        memory_stats = stats.get("memory_stats")
        if isinstance(memory_stats, dict):
            usage = int_value(memory_stats.get("usage"))
            limit = int_value(memory_stats.get("limit"))
            snapshot["memory_usage_bytes"] = usage
            snapshot["memory_limit_bytes"] = limit
            if usage is not None and limit is not None and limit > 0:
                snapshot["memory_percent"] = round((usage / limit) * 100, 2)

        pids_stats = stats.get("pids_stats")
        if isinstance(pids_stats, dict):
            snapshot["pids_current"] = int_value(pids_stats.get("current"))
        return snapshot

    def cleanup_completed(self, project_id: str) -> bool:
        ok = True
        for name in self._project_container_names(project_id):
            state = self.inspect_state(name)
            if state is None:
                continue
            container = self._require_container(name, project_id=project_id)
            if self._config.completed_action == "remove":
                # completed_action=remove 适合一次性任务；stop 则保留容器便于排查。
                LOG.info("removing completed project container project=%s container=%s", project_id, name)
                try:
                    container.remove(force=True)
                except NotFound:
                    continue
                except DockerException as exc:
                    LOG.warning("failed to remove container=%s error=%s", name, exc)
                    ok = False
                    continue
                ok = self.inspect_state(name) is None and ok
            elif state == "running":
                LOG.info("stopping completed project container project=%s container=%s", project_id, name)
                try:
                    container.stop(timeout=1)
                except NotFound:
                    continue
                except DockerException as exc:
                    LOG.warning("failed to stop container=%s error=%s", name, exc)
                    ok = False
                    continue
                ok = self.inspect_state(name) != "running" and ok
        return ok

    def cleanup_paused(self, project_id: str, *, container_ids: list[str]) -> bool:
        """精确停止并保留暂停项目的容器文件系统；不把 unknown 当成退出。"""
        name = self.container_name(project_id)
        with self._ensure_running_lock(name):
            return self._cleanup_paused_locked(project_id, name, container_ids)

    def pause_with_confirmation(
        self,
        project_id: str,
        *,
        container_ids: list[str],
        claim: Callable[[str | None, str], tuple[str | None, str | None]],
        confirm: Callable[[str, str | None, str], bool],
    ) -> bool:
        """同一容器锁内完成 Server fence、Docker stop 和持久 ACK。"""
        name = self.container_name(project_id)
        with self._ensure_running_lock(name):
            daemon_id = self.docker_daemon_id()
            current = self._get_container(name)
            current_id = current.id if current is not None else None
            token, registered_id = claim(current_id, daemon_id)
            if token is None:
                # 已确认的代次不得再对恢复后重新运行的同名资源执行 stop。
                return current is None or self._container_state(current) in {"exited", "created"}
            if current_id is not None and current_id != registered_id:
                return False
            if current_id is None and registered_id is not None and self._get_container(registered_id) is not None:
                return False
            identities = list(dict.fromkeys([*container_ids, *([registered_id] if registered_id else [])]))
            if not self._cleanup_paused_locked(project_id, name, identities):
                return False
            if self.docker_daemon_id() != daemon_id:
                return False
            return confirm(token, registered_id, daemon_id)

    def _cleanup_paused_locked(self, project_id: str, name: str, container_ids: list[str]) -> bool:
        current = self._get_container(name)
        if current is not None:
            self._assert_owned(current, project_id=project_id)
            if container_ids and current.id not in container_ids:
                return False
        identities = list(dict.fromkeys([*container_ids, name]))
        for identity in identities:
            container = self._get_container(identity)
            if container is None:
                continue
            self._assert_owned(container, project_id=project_id)
            state = self._container_state(container)
            if state == "running":
                try:
                    container.stop(timeout=1)
                except DockerException:
                    return False
            elif state not in {"exited", "created"}:
                return False
            verified = self._get_container(container.id)
            if verified is None or self._container_state(verified) not in {"exited", "created"}:
                return False
        # 清理时同名资源发生替换，不能确认暂停的是原工作区。
        current_after = self._get_container(name)
        if current is None:
            return current_after is None
        return current_after is not None and current_after.id == current.id

    def cleanup_finishing(self, project_id: str, *, container_ids: list[str]) -> bool:
        """人工结束及已停止项目仍按原策略删除精确归属资源。"""
        name = self.container_name(project_id)
        with self._ensure_running_lock(name):
            identities = list(dict.fromkeys([*container_ids, name]))
            owned = {}
            for identity in identities:
                container = self._get_container(identity)
                if container is not None:
                    self._assert_owned(container, project_id=project_id)
                    owned[container.id] = container
            for actual_id, container in owned.items():
                try:
                    container.remove(force=True)
                except NotFound:
                    pass
                except DockerException:
                    return False
                if self._get_container(actual_id) is not None:
                    return False
            return all(self._get_container(identity) is None for identity in identities)

    def cleanup_stopped(self, project_id: str) -> bool:
        ok = True
        for name in self._project_container_names(project_id):
            state = self.inspect_state(name)
            if state != "running":
                continue
            LOG.info("stopping stopped project container project=%s container=%s", project_id, name)
            container = self._require_container(name, project_id=project_id)
            try:
                container.stop(timeout=1)
            except NotFound:
                continue
            except DockerException as exc:
                LOG.warning("failed to stop stopped project container=%s error=%s", name, exc)
                ok = False
                continue
            ok = self.inspect_state(name) != "running" and ok
        return ok

    def cleanup_orphan(self, name: str) -> bool:
        state = self.inspect_state(name)
        if state is None:
            return True
        LOG.info("removing orphan project container container=%s state=%s", name, state)
        container = self._require_container(name)
        try:
            container.remove(force=True)
        except NotFound:
            return True
        except DockerException as exc:
            LOG.warning("failed to remove orphan container=%s error=%s", name, exc)
            return False
        return self.inspect_state(name) is None

    def managed_container_names(self) -> list[str]:
        try:
            containers = self._client.containers.list(
                all=True,
                filters={"label": [f"{self._LABEL_INSTANCE_ID}={self.instance_id}", f"{self._LABEL_ENGINE}=pi"]},
            )
        except DockerException as exc:
            LOG.warning("failed to list managed containers error=%s", exc)
            return []
        # Docker过滤只是缩小候选集，名称namespace和完整owner标签仍必须同时满足。
        return sorted(
            container.name for container in containers
            if container.name.startswith(self._project_prefix) and self._owns_container(container)
        )

    def needs_completed_cleanup(self, project_id: str) -> bool:
        for name in self._project_container_names(project_id):
            state = self.inspect_state(name)
            if state is None:
                continue
            if self._config.completed_action == "remove" or state == "running":
                return True
        return False

    def needs_orphan_cleanup(self, name: str) -> bool:
        return self.inspect_state(name) is not None

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return any(self.inspect_state(name) == "running" for name in self._project_container_names(project_id))

    def _project_container_names(self, project_id: str) -> list[str]:
        name = self.container_name(project_id)
        container = self._get_container(name)
        if container is not None:
            self._assert_owned(container, project_id=project_id)
        return [name]

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> ManagedProcess:
        container = self._require_container(container_name)
        argv: list[str] = []
        if timeout_seconds is not None:
            # 容器内 timeout 负责杀子进程树，Python 侧 communicate 仍有自己的保护超时。
            argv.extend(
                [
                    "timeout",
                    "-k",
                    f"{kill_after_seconds}s",
                    f"{timeout_seconds}s",
                ]
            )
        argv.extend(command)
        return ManagedProcess(container, argv, env)

    def write_text_file(
        self,
        container_name: str,
        path: str,
        content: str,
        *,
        owner: str | None = None,
        mode: int = 0o644,
        archive_root: str | None = None,
    ) -> None:
        # Docker put_archive 会应用 tar 内的 owner；工作区文件必须匹配 Worker 用户。
        container = self._require_container(container_name)
        uid, gid = self._container_user_ids(container, owner) if owner is not None else (0, 0)
        archive_path, archive = text_file_archive(
            path,
            content,
            uid=uid,
            gid=gid,
            mode=mode,
            archive_root=archive_root,
        )
        try:
            ok = container.put_archive(archive_path, archive)
        except DockerException as exc:
            raise RuntimeError(f"failed to write container file {path}: {exc}") from exc
        if not ok:
            raise RuntimeError(f"failed to write container file {path}")

    def _container_user_ids(self, container: Container, owner: str) -> tuple[int, int]:
        container_id = str(getattr(container, "id", "") or getattr(container, "name", "unknown"))
        cache_key = (container_id, owner)
        cached = self._container_user_ids_cache.get(cache_key)
        if cached is not None:
            return cached
        values: list[int] = []
        for flag in ("-u", "-g"):
            try:
                result = container.exec_run(["id", flag, owner], demux=False)
            except DockerException as exc:
                raise RuntimeError(f"failed to resolve container user {owner}: {exc}") from exc
            output = getattr(result, "output", b"") or b""
            if isinstance(output, tuple):
                output = b"".join(part or b"" for part in output)
            try:
                value = int(output.decode("utf-8", errors="replace").strip())
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"invalid container user {owner} identity") from exc
            if not _docker_exec_succeeded(result) or value < 0:
                raise RuntimeError(f"failed to resolve container user {owner}")
            values.append(value)
        resolved = (values[0], values[1])
        self._container_user_ids_cache[cache_key] = resolved
        return resolved

    def prepare_readonly_input_directory(self, container_name: str, path: str) -> None:
        """在不可由 Worker 改名的 root-owned 路径创建单次报告只读输入目录。"""
        directory = _readonly_input_directory(path)
        container = self._require_container(container_name)
        script = """
import os, stat, sys
child = sys.argv[1]
root = '/opt/cairn/readonly-inputs'
for parent in ('/opt', '/opt/cairn'):
    info = os.lstat(parent)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise RuntimeError('unsafe readonly input ancestor')
try:
    info = os.lstat(root)
except FileNotFoundError:
    os.mkdir(root, 0o555)
    os.chown(root, 0, 0)
    os.chmod(root, 0o555)
else:
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o555):
        raise RuntimeError('unsafe readonly input root')
if os.path.dirname(child) != root:
    raise RuntimeError('invalid readonly input child')
os.mkdir(child, 0o555)
os.chown(child, 0, 0)
os.chmod(child, 0o555)
"""
        try:
            result = container.exec_run(
                ["python3", "-c", script, str(directory)],
                user="0:0",
                demux=False,
            )
        except DockerException as exc:
            raise RuntimeError("failed to prepare readonly worker inputs") from exc
        if not _docker_exec_succeeded(result):
            raise RuntimeError("failed to prepare readonly worker inputs")

    def remove_readonly_input_directory(
        self,
        container_name: str,
        path: str,
        *,
        filenames: tuple[str, ...],
    ) -> None:
        """以 root 删除已知只读文件并 rmdir 单次目录；不递归删除共享根。"""
        directory = _readonly_input_directory(path)
        if not filenames or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}", name)
            for name in filenames
        ):
            raise ValueError("invalid readonly input filenames")
        container = self._require_container(container_name)
        script = """
import os, stat, sys
child, names = sys.argv[1], sys.argv[2:]
root = '/opt/cairn/readonly-inputs'
try:
    root_info = os.lstat(root)
except FileNotFoundError:
    raise SystemExit(0)
if (stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != 0 or root_info.st_gid != 0
        or stat.S_IMODE(root_info.st_mode) != 0o555):
    raise RuntimeError('unsafe readonly input root')
try:
    child_info = os.lstat(child)
except FileNotFoundError:
    raise SystemExit(0)
if (os.path.dirname(child) != root or stat.S_ISLNK(child_info.st_mode)
        or not stat.S_ISDIR(child_info.st_mode)
        or child_info.st_uid != 0 or child_info.st_gid != 0
        or stat.S_IMODE(child_info.st_mode) != 0o555):
    raise RuntimeError('unsafe readonly input child')
for name in names:
    target = os.path.join(child, name)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError('unsafe readonly input file')
    os.unlink(target)
os.rmdir(child)
"""
        try:
            result = container.exec_run(
                ["python3", "-c", script, str(directory), *filenames],
                user="0:0",
                demux=False,
            )
        except DockerException as exc:
            raise RuntimeError("failed to remove readonly worker inputs") from exc
        if not _docker_exec_succeeded(result):
            raise RuntimeError("failed to remove readonly worker inputs")

    def materialize_file(self, container_name: str, path: str) -> MaterializedContainerFile:
        """把 Docker tar 流分块解包到宿主临时文件，避免整文件驻留内存。"""
        container = self._require_container(container_name)
        try:
            stream, _stat = container.get_archive(path)
        except DockerException as exc:
            raise RuntimeError(f"failed to read container file {path}: {exc}") from exc

        output_path: Path | None = None
        try:
            # tarfile 的随机访问模式需要可 seek；大归档溢写到持久磁盘，避免占用 /tmp tmpfs。
            with tempfile.SpooledTemporaryFile(
                max_size=CONTAINER_ARCHIVE_SPOOL_BYTES,
                mode="w+b",
                dir=runtime_temp_dir(),
            ) as archive:
                for chunk in stream:
                    archive.write(chunk)
                archive.seek(0)
                with tarfile.open(fileobj=archive, mode="r:*") as tar:
                    for member in tar.getmembers():
                        if not member.isfile():
                            continue
                        extracted = tar.extractfile(member)
                        if extracted is None:
                            continue
                        with tempfile.NamedTemporaryFile(
                            prefix="cairn-container-file-",
                            dir=runtime_temp_dir(),
                            delete=False,
                        ) as output:
                            output_path = Path(output.name)
                            shutil.copyfileobj(extracted, output, length=CONTAINER_FILE_COPY_BYTES)
                            output.flush()
                            os.fsync(output.fileno())
                        os.chmod(output_path, 0o600)
                        return MaterializedContainerFile(
                            path=output_path,
                            size=output_path.stat().st_size,
                        )
        except tarfile.TarError as exc:
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            raise RuntimeError(f"failed to unpack container file {path}: {exc}") from exc
        except Exception:
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            raise
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        raise RuntimeError(f"container file {path} not found in archive")

    def read_file(self, container_name: str, path: str) -> bytes:
        # 小型配置读取保留 bytes API；大型 Artifact 应使用 materialize_file()。
        with self.materialize_file(container_name, path) as materialized:
            return materialized.path.read_bytes()

    def remove_file(
        self,
        container_name: str,
        path: str,
        *,
        allowed_root: str,
        user: str | None = None,
    ) -> None:
        """只删除受控根目录内的单个容器文件。"""
        target, _root = _controlled_container_path(path, allowed_root)
        container = self._require_container(container_name)
        kwargs = {"user": user} if user is not None else {}
        try:
            result = container.exec_run(["rm", "-f", "--", str(target)], **kwargs)
        except DockerException as exc:
            raise RuntimeError(f"failed to remove container file {path}: {exc}") from exc
        if not _docker_exec_succeeded(result):
            raise RuntimeError(f"failed to remove container file {path}")

    def remove_container(self, name: str, *, force: bool = True) -> None:
        container = self._get_container(name)
        if container is None:
            return
        try:
            container.remove(force=force)
        except NotFound:
            return
        except DockerException as exc:
            LOG.warning("failed to remove container=%s error=%s", name, exc)

    def _require_created_running_container(
        self,
        name: str,
        *,
        project_id: str,
        runtime_signature: str,
    ) -> Container:
        """按 exact name 重新核验刚创建容器的归属、签名和运行态。"""
        container = self._get_container(name)
        if container is None:
            raise RuntimeError(
                f"container {name} is not running after creation"
            )
        self._assert_owned(container, project_id=project_id)
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        if (
            not isinstance(labels, dict)
            or labels.get(self._LABEL_RUNTIME_SIGNATURE)
            != runtime_signature
        ):
            raise ContainerOwnershipError(
                "created container runtime signature does not match"
            )
        if self._container_state(container) != "running":
            raise RuntimeError(
                f"container {name} is not running after creation"
            )
        return container

    def _start_existing(self, name: str) -> None:
        LOG.debug("starting container=%s", name)
        container = self._require_container(name)
        expected_id = container.id
        start_error: DockerException | None = None
        try:
            container.start()
        except DockerException as exc:
            start_error = exc

        current = self._get_container(name)
        if (
            current is not None
            and current.id == expected_id
            and self._container_state(current) == "running"
        ):
            return
        if start_error is not None:
            raise RuntimeError(
                f"failed to start container {name}: {start_error}"
            ) from start_error
        raise RuntimeError(
            f"container {name} did not reach running state"
        )

    def _owns_container(self, container: Container) -> bool:
        name = str(getattr(container, "name", "") or "").lstrip("/")
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        if not isinstance(labels, dict) or labels.get(self._LABEL_INSTANCE_ID) != self.instance_id or labels.get(self._LABEL_ENGINE) != "pi":
            return False
        if name.startswith(self._startup_prefix):
            return bool(re.fullmatch(r"[a-f0-9]{12}", name[len(self._startup_prefix):])) and labels.get(self._LABEL_PROJECT_ID) == "startup-healthcheck"
        project_id = labels.get(self._LABEL_PROJECT_ID)
        if not name.startswith(self._project_prefix) or not isinstance(project_id, str):
            return False
        try:
            return name == self.container_name(project_id)
        except ValueError:
            return False

    def _assert_owned(self, container: Container, *, project_id: str | None = None) -> None:
        if not self._owns_container(container):
            raise ContainerOwnershipError("container is not owned by this pi dispatcher instance")
        # 项目名中的“/”会转为“-”，不能仅凭转义后同名就清理另一个项目。
        if project_id is not None and container.attrs["Config"]["Labels"].get(self._LABEL_PROJECT_ID) != project_id:
            raise ContainerOwnershipError("container project identity does not match the requested project")

    def _get_container(self, name: str) -> Container | None:
        try:
            container = self._client.containers.get(name)
        except NotFound:
            return None
        except DockerException as exc:
            raise RuntimeError(f"failed to get container {name}: {exc}") from exc
        self._assert_owned(container)
        if name not in {container.name, getattr(container, "id", None)}:
            raise ContainerOwnershipError("container lookup identity did not match its inspected identity")
        return container

    def _require_container(self, name: str, *, project_id: str | None = None) -> Container:
        container = self._get_container(name)
        if container is None:
            raise RuntimeError(f"container not found: {name}")
        self._assert_owned(container, project_id=project_id)
        return container

    def _container_state(self, container: Container | None) -> str | None:
        if container is None:
            return None
        try:
            container.reload()
        except DockerException as exc:
            name = getattr(container, "name", "unknown")
            raise RuntimeError(f"failed to inspect container {name}: {exc}") from exc
        self._assert_owned(container)
        state = container.attrs.get("State", {}).get("Status")
        return str(state) if state else None

    def _container_runtime_mismatch(self, container: Container, options: ContainerRuntimeOptions) -> bool:
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        if not isinstance(labels, dict):
            return True
        existing_signature = labels.get(self._LABEL_RUNTIME_SIGNATURE)
        if existing_signature is None:
            return True
        return existing_signature != options.runtime_signature

    @staticmethod
    def _advisory_volume_bind(
        options: ContainerRuntimeOptions,
    ) -> dict[str, dict[str, str]] | None:
        source = options.advisory_mount_source
        target = options.advisory_mount_target
        if not source or not target:
            return None
        source_path = Path(source).expanduser()
        if not source_path.is_absolute():
            source_path = source_path.resolve()
        if not source_path.is_dir():
            raise RuntimeError(f"advisory bundle source is unavailable: {source_path}")
        # bundle 由 Dispatcher 原子物化，Worker 只能按需只读访问。
        return {str(source_path): {"bind": target, "mode": "ro"}}

    def _container_labels(self, project_id: str, options: ContainerRuntimeOptions) -> dict[str, str]:
        return {
            self._LABEL_INSTANCE_ID: self.instance_id,
            self._LABEL_ENGINE: "pi",
            self._LABEL_PROJECT_ID: project_id,
            self._LABEL_SCENARIO_KEY: options.scenario_key,
            self._LABEL_RUNTIME_SIGNATURE: options.runtime_signature,
        }

    @staticmethod
    def _is_name_conflict(exc: APIError) -> bool:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        explanation = str(getattr(exc, "explanation", "") or exc)
        return status_code == 409 or "is already in use" in explanation
