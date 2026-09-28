"""Docker exec 进程封装，负责输出收集、超时 kill 和取消原因传递。"""

from __future__ import annotations

import codecs
from contextlib import suppress
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from typing import Any, BinaryIO, Callable

from docker.errors import DockerException, NotFound
from docker.models.containers import Container
from requests.exceptions import RequestException

from cairn.dispatcher.runtime.exec_control import (
    EXEC_CLEANUP,
    EXEC_SUPERVISOR,
    EXEC_TERMINATOR,
    EXEC_VERIFY_EXIT,
)
from cairn.dispatcher.runtime.temporary_files import runtime_temp_dir

LOG = logging.getLogger(__name__)
EXEC_KILL_JOIN_TIMEOUT_SECONDS = 5.0
_EXEC_TERMINATION_ATTEMPTS = 2
PROCESS_OUTPUT_PREVIEW_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_PREVIEW_MARKER = b"\n... output truncated ...\n"
_DOCKER_TRANSPORT_ERRORS = (DockerException, RequestException)


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _docker_exec_id(response: object) -> str:
    """严格提取 Docker exec 身份，避免畸形响应逃逸到 reader 线程。"""
    if not isinstance(response, dict):
        raise DockerException("malformed Docker exec create response")
    exec_id = response.get("Id")
    if not isinstance(exec_id, str) or not exec_id:
        raise DockerException("malformed Docker exec create response")
    return exec_id


def _docker_exec_state(response: object) -> tuple[bool, int | None]:
    """只接受 literal bool 终态和严格非负退出码。"""
    if not isinstance(response, dict) or type(response.get("Running")) is not bool:
        raise DockerException("malformed Docker exec inspect response")
    running = response["Running"]
    if running:
        return True, None
    exit_code = response.get("ExitCode")
    if type(exit_code) is not int or exit_code < 0:
        raise DockerException("malformed Docker exec inspect response")
    return False, exit_code


def _read_file(path: str) -> str:
    return Path(path).read_bytes().decode("utf-8", errors="replace")


def _read_file_part(path: str, limit: int, *, tail: bool) -> str:
    if limit < 0:
        raise ValueError("limit must be non-negative")
    if limit == 0:
        return ""
    with open(path, "rb") as output:
        if tail:
            output.seek(max(0, os.fstat(output.fileno()).st_size - limit))
        return _decode(output.read(limit))


def _file_is_blank(path: str) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    with open(path, "rb") as output:
        while chunk := output.read(_READ_CHUNK_BYTES):
            if any(not character.isspace() for character in decoder.decode(chunk)):
                return False
        return not any(not character.isspace() for character in decoder.decode(b"", final=True))


@dataclass(slots=True)
class ProcessResult:
    """容器命令执行结果；文件型结果的 ``stdout``/``stderr`` 仅保存有界预览。"""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None
    oom_killed: bool = False
    oom_evidence_source: str | None = None
    transport_error: bool = False
    candidate_capture_required: bool = False
    candidate_host_durable: bool = False
    candidate_capture_pending: bool = False
    candidate_capture_frames: int = 0
    candidate_capture_integrity_error: str | None = None
    _stdout_path: str | None = field(default=None, repr=False, compare=False, kw_only=True)
    _stderr_path: str | None = field(default=None, repr=False, compare=False, kw_only=True)
    _cleanup_callbacks: list[Callable[[], None]] = field(
        default_factory=list,
        init=False,
        repr=False,
        compare=False,
    )
    _cleaned: bool = field(default=False, init=False, repr=False, compare=False)

    def read_stdout(self) -> str:
        """读取完整 stdout；inline 构造的结果直接返回原字符串。"""
        return self._read_complete(self._stdout_path, self.stdout)

    def read_stderr(self) -> str:
        """读取完整 stderr；inline 构造的结果直接返回原字符串。"""
        return self._read_complete(self._stderr_path, self.stderr)

    def stdout_head(self, limit: int = PROCESS_OUTPUT_PREVIEW_BYTES) -> str:
        return self._read_part(self._stdout_path, self.stdout, limit, tail=False)

    def stdout_tail(self, limit: int = PROCESS_OUTPUT_PREVIEW_BYTES) -> str:
        return self._read_part(self._stdout_path, self.stdout, limit, tail=True)

    def stderr_head(self, limit: int = PROCESS_OUTPUT_PREVIEW_BYTES) -> str:
        return self._read_part(self._stderr_path, self.stderr, limit, tail=False)

    def stderr_tail(self, limit: int = PROCESS_OUTPUT_PREVIEW_BYTES) -> str:
        return self._read_part(self._stderr_path, self.stderr, limit, tail=True)

    # 保留 read_* 命名别名，让调用方能明确区分完整读取与预览字段。
    def read_stdout_head(self, limit: int = PROCESS_OUTPUT_PREVIEW_BYTES) -> str:
        return self.stdout_head(limit)

    def read_stdout_tail(self, limit: int = PROCESS_OUTPUT_PREVIEW_BYTES) -> str:
        return self.stdout_tail(limit)

    def read_stderr_head(self, limit: int = PROCESS_OUTPUT_PREVIEW_BYTES) -> str:
        return self.stderr_head(limit)

    def read_stderr_tail(self, limit: int = PROCESS_OUTPUT_PREVIEW_BYTES) -> str:
        return self.stderr_tail(limit)

    @property
    def stdout_size(self) -> int:
        return self._size(self._stdout_path, self.stdout)

    @property
    def stderr_size(self) -> int:
        return self._size(self._stderr_path, self.stderr)

    @property
    def stdout_blank(self) -> bool:
        return self._is_blank(self._stdout_path, self.stdout)

    @property
    def stderr_blank(self) -> bool:
        return self._is_blank(self._stderr_path, self.stderr)

    def is_stdout_blank(self) -> bool:
        return self.stdout_blank

    def is_stderr_blank(self) -> bool:
        return self.stderr_blank

    def add_cleanup_callback(self, callback: Callable[[], None]) -> None:
        """登记业务消费结果后执行的临时资源清理；已清理时立即执行。"""
        if self._cleaned:
            callback()
            return
        self._cleanup_callbacks.append(callback)

    def cleanup(self) -> None:
        """删除 ManagedProcess 临时资源；可安全重复调用。"""
        if self._cleaned:
            return
        callbacks = tuple(reversed(self._cleanup_callbacks))
        self._cleanup_callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except BaseException as exc:
                LOG.warning("process deferred cleanup failed error=%s", type(exc).__name__)
        for path in (self._stdout_path, self._stderr_path):
            if path is not None:
                with suppress(FileNotFoundError):
                    os.unlink(path)
        self._cleaned = True

    def __enter__(self) -> ProcessResult:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.cleanup()

    def __del__(self) -> None:
        # 调用方遗漏显式 cleanup 时仍尽量回收临时文件。
        with suppress(Exception):
            self.cleanup()

    def _require_available(self) -> None:
        if self._cleaned and (self._stdout_path is not None or self._stderr_path is not None):
            raise RuntimeError("process output has been cleaned up")

    def _read_complete(self, path: str | None, inline: str) -> str:
        self._require_available()
        return _read_file(path) if path is not None else inline

    def _read_part(self, path: str | None, inline: str, limit: int, *, tail: bool) -> str:
        self._require_available()
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if path is not None:
            return _read_file_part(path, limit, tail=tail)
        encoded = inline.encode("utf-8")
        part = encoded[-limit:] if tail and limit else encoded[:limit]
        return _decode(part)

    def _size(self, path: str | None, inline: str) -> int:
        self._require_available()
        return os.path.getsize(path) if path is not None else len(inline.encode("utf-8"))

    def _is_blank(self, path: str | None, inline: str) -> bool:
        self._require_available()
        return _file_is_blank(path) if path is not None else not inline.strip()


class _BoundedPreview:
    """只保留输出首尾固定字节，避免预览随进程输出增长。"""

    def __init__(self, limit: int = PROCESS_OUTPUT_PREVIEW_BYTES):
        self._limit = limit
        self._head_limit = (limit - len(_PREVIEW_MARKER)) // 2
        self._tail_limit = limit - len(_PREVIEW_MARKER) - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self.size = 0

    def append(self, data: bytes) -> None:
        self.size += len(data)
        if len(self._head) < self._head_limit:
            take = min(self._head_limit - len(self._head), len(data))
            self._head.extend(data[:take])
        if self._tail_limit:
            if len(data) >= self._tail_limit:
                self._tail[:] = data[-self._tail_limit :]
            else:
                self._tail.extend(data)
                if len(self._tail) > self._tail_limit:
                    del self._tail[:-self._tail_limit]

    def text(self) -> str:
        if self.size <= self._limit:
            # 小输出可能同时存在于 head/tail，按总长度去除重叠部分。
            overlap = max(0, len(self._head) + len(self._tail) - self.size)
            return _decode(bytes(self._head) + bytes(self._tail[overlap:]))
        return _decode(bytes(self._head) + _PREVIEW_MARKER + bytes(self._tail))


class ManagedProcess:
    """对单次 docker exec 的生命周期管理。"""

    def __init__(self, container: Container, command: list[str], env: dict[str, str]):
        self.command = command
        self.env = env
        self._container = container
        self._api = container.client.api
        self._exec_id: str | None = None
        self._execution_identity: tuple[str, str] | None = None
        self._reader: threading.Thread | None = None
        self._stdout_preview = _BoundedPreview()
        self._stderr_preview = _BoundedPreview()
        self._stdout_path: str | None = None
        self._stderr_path: str | None = None
        self._stdout_file: BinaryIO | None = None
        self._stderr_file: BinaryIO | None = None
        self._stream: Any | None = None
        self._stream_completed = False
        self._execution_start_attempted = False
        self._exec_start_finished = False
        self._execution_started = False
        self.on_execution_prepared = None
        self.on_execution_starting = None
        self.on_execution_started = None
        self._terminal_confirmed = False
        self._returncode: int | None = None
        self._timed_out = False
        self._cancel_reason: str | None = None
        self._read_error: str | None = None
        self._done = threading.Event()
        self._output_lock = threading.Lock()
        self._stream_lock = threading.Lock()
        self._communicate_lock = threading.Lock()
        self._output_closed = False
        self._reader_abandoned = False
        self._result: ProcessResult | None = None
        self._lifecycle_lock = threading.Lock()
        self._termination_requested = threading.Event()
        self._terminator: threading.Thread | None = None
        self._terminator_abandoned = False
        self._start_skipped = False
        self._control_path = f"/tmp/cairn-exec-{uuid.uuid4().hex}.json"
        self._retain_control_proof = False

    @property
    def execution_identity(self) -> tuple[str, str] | None:
        """返回实际创建的 exec 身份；即使后续启动失败，也保留精确容器 ID 用于清理核验。"""
        return self._execution_identity

    @property
    def execution_control_path(self) -> str:
        """返回本次 exec 唯一且不含业务正文的终态凭据路径。"""
        return self._control_path

    @property
    def execution_start_attempted(self) -> bool:
        """只有跨过 durable 回调并实际调用 exec_start 才算启动尝试。"""
        return self._execution_start_attempted

    @property
    def execution_started(self) -> bool:
        """只有 Docker exec_start 返回才有启动证据，创建 reader 不算。"""
        return self._execution_started

    def retain_execution_control(self) -> None:
        """把终态凭据所有权交给 durable candidate fence。"""
        if self._reader is not None or self._execution_started:
            raise RuntimeError("execution control retention must precede process start")
        self._retain_control_proof = True

    @property
    def execution_completed(self) -> bool:
        """只承认 supervisor 的 Docker 终态；输出传输错误另行阻止结果被接受。"""
        return self._start_skipped or (
            not self._reader_abandoned
            and not self._terminator_abandoned
            and self._terminal_confirmed
            and self._reader is not None
            and not self._reader.is_alive()
        )

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._reader is not None or self._start_skipped:
                raise RuntimeError("process already started")
            if self._cancel_reason is not None:
                self._start_skipped = True
                self._result = ProcessResult(1, "", "", cancelled=True, cancel_reason=self._cancel_reason)
                return
        container_id = self._container.id
        exec_info = self._api.exec_create(
            container_id,
            ["python3", "-c", EXEC_SUPERVISOR, self._control_path, *self.command],
            stdout=True,
            stderr=True,
            stdin=False,
            tty=False,
            environment=self.env,
        )
        self._exec_id = _docker_exec_id(exec_info)
        self._execution_identity = (container_id, self._exec_id)
        # mandatory durable 绑定必须先于 reader/exec_start；回调失败时命令仍未执行。
        if self.on_execution_prepared is not None:
            self.on_execution_prepared(
                container_id,
                self._exec_id,
                self._control_path,
            )
        try:
            self._stdout_path, self._stdout_file = self._create_output_file("stdout")
            self._stderr_path, self._stderr_file = self._create_output_file("stderr")
            self._reader = threading.Thread(target=self._read_stream, daemon=True)
            self._reader.start()
        except Exception:
            self._finalize_output_files()
            self._unlink_output_files()
            raise

    def communicate(self, timeout: float | None) -> ProcessResult:
        with self._communicate_lock:
            if self._result is not None:
                return self._result
            if self._reader is None:
                raise RuntimeError("process has not been started")
            self._reader.join(timeout=timeout)
            if self._reader.is_alive():
                # 超时时同时 kill 和关闭 HTTP stream，避免 reader 永久阻塞在迭代器中。
                self._timed_out = self._cancel_reason is None
                self.kill()
                self._reader.join(timeout=EXEC_KILL_JOIN_TIMEOUT_SECONDS)
            if self._reader.is_alive():
                # 极端情况下隔离仍阻塞的 daemon reader，并先封存输出，禁止其继续写文件。
                self._reader_abandoned = True
                self._finalize_output_files()
                if self._returncode is None:
                    self._returncode = 137
                self._done.set()
            elif not self._done.is_set():
                # reader 已退出却未发布完成信号时，不能无界等待未知线程异常。
                self._read_error = self._read_error or "Docker exec reader finalization failed"
                if self._returncode is None:
                    self._returncode = 137 if self._timed_out else 1
                self._finalize_output_files()
                self._done.set()
            if self._terminator is not None:
                # Docker SDK 单次调用本身没有可靠 deadline；只等待固定预算，超时后按未决执行处理。
                self._terminator.join(timeout=EXEC_KILL_JOIN_TIMEOUT_SECONDS)
                if self._terminator.is_alive():
                    self._terminator_abandoned = True
                    self._read_error = (
                        self._read_error
                        or "Docker exec termination did not finish within the control budget"
                    )
            self._result = ProcessResult(
                returncode=self._returncode if self._returncode is not None else 1,
                stdout=self._stdout_preview.text(),
                stderr=self._stderr_preview.text(),
                timed_out=self._timed_out,
                cancelled=self._cancel_reason is not None,
                cancel_reason=self._cancel_reason,
                transport_error=not self.execution_completed or self._read_error is not None or (not self._start_skipped and not self._stream_completed),
                _stdout_path=self._stdout_path,
                _stderr_path=self._stderr_path,
            )
            # 文件所有权转交给 ProcessResult，ManagedProcess 不再负责删除。
            self._stdout_path = None
            self._stderr_path = None
            return self._result

    def cleanup_output(self) -> None:
        """在 communicate 未能交付 ProcessResult 时也清理临时输出。"""
        if self._result is not None:
            self._result.cleanup()
        self._finalize_output_files()
        self._unlink_output_files()

    def kill(self) -> None:
        # 每个活跃 exec 最多一个终止线程；数量随现有任务并发上限受限。
        # 尚未完成 exec_start 时只保存意图，由 reader 完成附着后回放。
        self._termination_requested.set()
        with self._lifecycle_lock:
            execution_may_be_running = self._execution_started or (
                self._execution_start_attempted and self._exec_start_finished
            )
            if not execution_may_be_running or self._terminator is not None:
                return
            self._terminator = threading.Thread(target=self._terminate, daemon=True)
            self._terminator.start()

    def _terminate(self) -> None:
        # 先关闭输出连接，不能让它无界排在 Docker kill 后；close 不是退出证明。
        self._close_active_stream()
        assert self._exec_id is not None
        running: bool | None = None
        try:
            details = self._api.exec_inspect(self._exec_id)
            running, _exit_code = _docker_exec_state(details)
        except _DOCKER_TRANSPORT_ERRORS as exc:
            # exact exec 的取消是幂等操作；初次 inspect 不可用时仍尝试终止，不能把瞬时传输错误当作结束。
            LOG.warning(
                "failed to inspect exec before kill exec_id=%s error=%s",
                self._exec_id,
                type(exc).__name__,
            )
        if running is False:
            return
        try:
            control_finished = self._signal_execution()
            # supervisor 与终止助手均已退出后，才可清理取消自然退出竞态留下的标记。
            details = self._api.exec_inspect(self._exec_id)
            running, _exit_code = _docker_exec_state(details)
            if (
                not self._retain_control_proof
                and control_finished is not None
                and running is False
            ):
                self._run_control(
                    [
                        "python3",
                        "-c",
                        "import os,sys\ntry: os.unlink(sys.argv[1])\nexcept FileNotFoundError: pass",
                        self._control_path + ".cancel",
                    ],
                    timeout=2,
                )
        except _DOCKER_TRANSPORT_ERRORS as exc:
            LOG.warning("exec termination failed exec_id=%s error=%s", self._exec_id, type(exc).__name__)

    def cancel(self, reason: str) -> None:
        with self._lifecycle_lock:
            if self._cancel_reason is not None:
                return
            self._cancel_reason = reason
        self.kill()

    def _read_stream(self) -> None:
        assert self._exec_id is not None
        stream: Any | None = None
        try:
            with self._lifecycle_lock:
                if self._cancel_reason is not None:
                    # exec_create 并不执行命令；此时取消仍可证明没有启动。
                    self._start_skipped = True
                    self._returncode = 1
                    return
            # 启动尝试必须先跨过调用方 durable 边界；失败时 Docker exec 尚未启动。
            if self.on_execution_starting is not None:
                self.on_execution_starting()
            with self._lifecycle_lock:
                self._execution_start_attempted = True
            try:
                # demux=True 将 stdout/stderr 分离，便于 artifact 和监控分别记录。
                stream = self._api.exec_start(
                    self._exec_id,
                    detach=False,
                    tty=False,
                    stream=True,
                    demux=True,
                )
            except BaseException:
                with self._lifecycle_lock:
                    self._exec_start_finished = True
                if self._termination_requested.is_set():
                    self.kill()
                raise
            with self._stream_lock:
                self._stream = stream
            with self._lifecycle_lock:
                self._execution_started = True
                self._exec_start_finished = True
            if self._termination_requested.is_set():
                self.kill()
            if self.on_execution_started is not None:
                try:
                    self.on_execution_started()
                except Exception:
                    # 观测失败不应改变 exec 结果或让输出 reader 提前退出。
                    LOG.warning("exec start observation failed")
            if self._cancel_reason is not None or self._reader_abandoned:
                # exec_start 晚于取消返回时，再按原 exec 身份取消，不能遗留迟到进程。
                self.kill()
            for chunk in stream:
                stdout, stderr = self._split_chunk_bytes(chunk)
                if stdout:
                    self._write_output(self._stdout_file, self._stdout_preview, stdout)
                if stderr:
                    self._write_output(self._stderr_file, self._stderr_preview, stderr)
            self._stream_completed = True
        except Exception as exc:
            # reader 异常不能静默丢失；若 stderr 为空，将诊断写入同一受控输出文件。
            self._read_error = str(exc)
            if self._stderr_preview.size == 0:
                self._write_output(self._stderr_file, self._stderr_preview, self._read_error.encode("utf-8", errors="replace"))
        finally:
            with self._stream_lock:
                if self._stream is stream:
                    self._stream = None
            self._close_stream(stream)
            # 即使调用方已放弃等待，也只能通过 inspect 获取晚到的明确终态；
            # 已返回的超时结果与 browser unmanaged fence 不因此被重新解释。
            exit_code = 137 if self._timed_out else 1
            try:
                exit_code = (
                    1 if self._start_skipped else self._resolve_exit_code()
                )
            except BaseException as exc:
                self._read_error = self._read_error or (
                    "Docker exec terminal resolution failed: "
                    f"{type(exc).__name__}"
                )
            finally:
                if not self._reader_abandoned:
                    self._returncode = exit_code
                self._finalize_output_files()
                self._done.set()

    def _write_output(self, output: BinaryIO | None, preview: _BoundedPreview, data: bytes) -> None:
        with self._output_lock:
            if self._output_closed or output is None:
                return
            output.write(data)
            preview.append(data)

    def _finalize_output_files(self) -> None:
        with self._output_lock:
            if self._output_closed:
                return
            self._output_closed = True
            for output in (self._stdout_file, self._stderr_file):
                if output is not None:
                    with suppress(Exception):
                        output.close()
            self._stdout_file = None
            self._stderr_file = None

    def _close_active_stream(self) -> None:
        with self._stream_lock:
            stream = self._stream
        # SDK close 以 shutdown 唤醒 poll；response 保持 fd 引用直到 reader 退出，
        # 否则跨线程回收的 fd 可能被控制请求复用，让旧 poll 永久等待另一个连接。
        self._close_stream(stream, release_response=False)

    @staticmethod
    def _close_stream(stream: Any | None, *, release_response: bool = True) -> None:
        if stream is None:
            return
        close = getattr(stream, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
        response = getattr(stream, "_response", None)
        response_close = getattr(response, "close", None)
        if release_response and callable(response_close):
            with suppress(Exception):
                response_close()

    def _resolve_exit_code(self) -> int:
        assert self._exec_id is not None
        deadline = time.monotonic() + EXEC_KILL_JOIN_TIMEOUT_SECONDS
        while True:
            try:
                details = self._api.exec_inspect(self._exec_id)
                running, exit_code = _docker_exec_state(details)
            except _DOCKER_TRANSPORT_ERRORS as exc:
                if self._read_error is None:
                    self._read_error = str(exc)
                return 137 if self._timed_out else 1
            if running is False:
                assert exit_code is not None
                try:
                    verified = self._run_control(
                        ["python3", "-c", EXEC_VERIFY_EXIT, self._control_path, str(exit_code)], timeout=2,
                    )
                except _DOCKER_TRANSPORT_ERRORS:
                    verified = None
                self._terminal_confirmed = verified == 0
                if not self._terminal_confirmed:
                    self._read_error = self._read_error or "Docker exec descendant exit was not confirmed"
                elif not self._retain_control_proof:
                    try:
                        cleanup_status = self._run_control(
                            ["python3", "-c", EXEC_CLEANUP, self._control_path],
                            timeout=2,
                        )
                    except _DOCKER_TRANSPORT_ERRORS:
                        cleanup_status = None
                    if cleanup_status != 0:
                        LOG.warning(
                            "exec control proof cleanup deferred exec_id=%s",
                            self._exec_id,
                        )
                return exit_code
            if time.monotonic() >= deadline:
                # 保留既有合成退出码，但它不能证明 exec 已结束或允许资源清理。
                self._read_error = self._read_error or "Docker exec terminal state was not confirmed"
                return 137 if self._timed_out else 1
            time.sleep(0.1)

    def _signal_execution(self) -> int | None:
        command = ["python3", "-c", EXEC_TERMINATOR, self._control_path]
        for attempt in range(_EXEC_TERMINATION_ATTEMPTS):
            try:
                return self._run_control(command, timeout=18)
            except _DOCKER_TRANSPORT_ERRORS as exc:
                if attempt + 1 >= _EXEC_TERMINATION_ATTEMPTS:
                    raise
                # exact terminator 幂等；瞬时传输失败不能永久耗尽本次取消机会。
                LOG.warning(
                    "retrying exec termination helper exec_id=%s error=%s",
                    self._exec_id,
                    type(exc).__name__,
                )
        raise AssertionError("unreachable")

    def _run_control(self, command: list[str], *, timeout: float) -> int | None:
        assert self._execution_identity is not None
        # detach 避免 SDK exec_run 的 socket 帧读取绕过 HTTP transport timeout。
        control = _docker_exec_id(
            self._api.exec_create(
                self._execution_identity[0], command,
                stdout=False, stderr=False, stdin=False, tty=False,
            )
        )
        self._api.exec_start(control, detach=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            details = self._api.exec_inspect(control)
            running, exit_code = _docker_exec_state(details)
            if running is False:
                assert exit_code is not None
                if exit_code != 0:
                    LOG.warning("exec control helper did not confirm termination exec_id=%s", self._exec_id)
                return exit_code
            time.sleep(.05)
        LOG.warning("exec control helper timed out exec_id=%s", self._exec_id)
        return None

    def _unlink_output_files(self) -> None:
        for path in (self._stdout_path, self._stderr_path):
            if path is not None:
                with suppress(FileNotFoundError):
                    os.unlink(path)
        self._stdout_path = None
        self._stderr_path = None

    @staticmethod
    def _create_output_file(stream_name: str) -> tuple[str, BinaryIO]:
        fd, path = tempfile.mkstemp(
            prefix="cairn-process-",
            suffix=f".{stream_name}",
            dir=runtime_temp_dir(),
        )
        # mkstemp 已使用 0600；再次收紧可防御非标准实现或异常 umask 环境。
        os.chmod(path, 0o600)
        return path, os.fdopen(fd, "wb", buffering=0)

    @staticmethod
    def _split_chunk_bytes(chunk: Any) -> tuple[bytes, bytes]:
        if isinstance(chunk, tuple):
            stdout, stderr = chunk
        else:
            stdout, stderr = chunk, None
        return ManagedProcess._to_bytes(stdout), ManagedProcess._to_bytes(stderr)

    @staticmethod
    def _split_chunk(chunk: Any) -> tuple[str, str]:
        stdout, stderr = ManagedProcess._split_chunk_bytes(chunk)
        return _decode(stdout), _decode(stderr)

    @staticmethod
    def _to_bytes(chunk: bytes | str | None) -> bytes:
        if chunk is None:
            return b""
        if isinstance(chunk, bytes):
            return chunk
        return chunk.encode("utf-8")

    @staticmethod
    def _decode(chunk: bytes | str | None) -> str:
        return _decode(ManagedProcess._to_bytes(chunk))
