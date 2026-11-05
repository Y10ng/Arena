"""仅扣除平台热暂停的执行时钟；授权截止仍由 Server 按真实时间判断。"""

from __future__ import annotations

import threading
import time


class ExecutionSceneLost(RuntimeError):
    """原物理现场已不存在，禁止将重新启动冒充热恢复。"""


class ExecutionDetached(BaseException):
    """Dispatcher 退出时只脱离宿主等待，不能解释为业务结束或进程退出。"""


class PauseClock:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._paused_at: float | None = None
        self._paused_seconds = 0.0
        self._detached = threading.Event()

    @property
    def detached(self) -> bool:
        return self._detached.is_set()

    def detach(self) -> None:
        self._detached.set()
        with self._condition:
            self._condition.notify_all()

    def check_attached(self) -> None:
        if self.detached:
            raise ExecutionDetached()

    def now(self) -> float:
        with self._condition:
            return (self._paused_at if self._paused_at is not None else time.monotonic()) - self._paused_seconds

    @property
    def paused_seconds(self) -> float:
        """交付准备也扣除当前冻结窗口，不要求执行进程已经附着。"""
        with self._condition:
            current = 0.0 if self._paused_at is None else time.monotonic() - self._paused_at
            return self._paused_seconds + current

    @property
    def paused(self) -> bool:
        with self._condition:
            return self._paused_at is not None

    def pause(self) -> None:
        with self._condition:
            if self._paused_at is None:
                self._paused_at = time.monotonic()

    def resume(self) -> None:
        with self._condition:
            if self._paused_at is not None:
                self._paused_seconds += time.monotonic() - self._paused_at
                self._paused_at = None
            self._condition.notify_all()

    def wait(self, cancelled) -> None:
        with self._condition:
            self.check_attached()
            while self._paused_at is not None and not cancelled():
                self._condition.wait(0.1)
                self.check_attached()

    def start(self, operation, cancelled):
        # 只包住发布／启动边界；准备 I/O 与 readiness 等待不得放入 operation。
        # exec_start 与冻结广播互斥，避免尚未启动的同一 exec 被误判为失败重跑。
        with self._condition:
            self.check_attached()
            while self._paused_at is not None and not cancelled():
                self._condition.wait(0.1)
                self.check_attached()
            if cancelled():
                return None
            return operation()
