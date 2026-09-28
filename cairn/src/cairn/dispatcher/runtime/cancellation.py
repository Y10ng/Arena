"""跨线程取消令牌，把调度层的取消请求转发给正在运行的进程。"""

from __future__ import annotations

from dataclasses import dataclass
import threading

from cairn.dispatcher.runtime.process import ManagedProcess


@dataclass(slots=True)
class _ExecutionRecord:
    process: ManagedProcess
    identity: tuple[str, str] | None = None
    started: bool = False
    completed: bool = False
    finished: bool = False


def _true_property(process: object, name: str) -> bool:
    try:
        return getattr(process, name, False) is True
    except Exception:
        return False


def _execution_identity(process: object) -> tuple[str, str] | None:
    try:
        identity = getattr(process, "execution_identity", None)
    except Exception:
        return None
    if (
        isinstance(identity, tuple)
        and len(identity) == 2
        and all(isinstance(value, str) and value for value in identity)
    ):
        return identity
    return None


class TaskCancellation:
    """保存一次取消原因，并在进程附着前后都能生效。"""

    def __init__(self, *, generation: int = 0) -> None:
        self._lock = threading.Lock()
        self._process: ManagedProcess | None = None
        self._reason: str | None = None
        self.generation = generation
        self._executions: dict[int, _ExecutionRecord] = {}

    def register_execution(self, process: ManagedProcess) -> None:
        with self._lock:
            self._executions.setdefault(id(process), _ExecutionRecord(process))

    def observe_execution_start(self, process: ManagedProcess) -> None:
        with self._lock:
            record = self._executions.setdefault(id(process), _ExecutionRecord(process))
            if not record.finished:
                record.started |= _true_property(process, "execution_started")

    def finish_execution(self, process: ManagedProcess) -> None:
        with self._lock:
            record = self._executions.setdefault(id(process), _ExecutionRecord(process))
            if record.finished:
                return
            # 首次收口同时冻结身份、启动与终态证明，晚到 reader 不得改写已上报事实。
            record.identity = _execution_identity(process)
            record.started |= _true_property(process, "execution_started")
            record.completed = _true_property(process, "execution_completed")
            record.finished = True

    def execution_container_ids(self) -> list[str]:
        with self._lock:
            identities = (
                record.identity
                if record.finished
                else _execution_identity(record.process)
                for record in self._executions.values()
            )
            return sorted(
                {identity[0] for identity in identities if identity is not None}
            )

    def execution_lifecycle(self, *, task_finished: bool = False) -> dict[str, object]:
        with self._lock:
            records = tuple(self._executions.values())
            return {
                "generation": self.generation,
                "process_started": any(
                    record.started
                    if record.finished
                    else record.started
                    or _true_property(record.process, "execution_started")
                    for record in records
                ),
                "termination_confirmed": task_finished and all(
                    record.finished and record.completed for record in records
                ),
                "cleanup_confirmed": False,
            }

    def attach_process(self, process: ManagedProcess | None) -> None:
        with self._lock:
            self._process = process
            reason = self._reason
        if process is not None and reason is not None:
            # 如果取消先于进程启动发生，进程一附着就立即取消。
            process.cancel(reason)

    def cancel(self, reason: str) -> bool:
        with self._lock:
            already_cancelled = self._reason is not None
            if not already_cancelled:
                self._reason = reason
            process = self._process
        if process is not None and not already_cancelled:
            process.cancel(self._reason)
        return not already_cancelled

    @property
    def is_cancelled(self) -> bool:
        return self.reason is not None

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason
