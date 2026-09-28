"""任务心跳租约，负责在 Server 租约失效时终止本地执行进程。"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ManagedProcess


LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class HeartbeatFailure:
    """最近一次不可恢复心跳失败，用于 task 结束时归类错误。"""

    status_code: int | None
    text: str


class HeartbeatLease:
    """后台线程定期续租 intent/reason，失败后 kill 关联进程。"""

    def __init__(
        self,
        heartbeat: Callable[[], ApiResult],
        scope: str,
        worker_name: str,
        interval: int,
    ):
        self._heartbeat = heartbeat
        self._scope = scope
        self._worker_name = worker_name
        self._interval = interval
        self._cancellation = TaskCancellation()
        self._failure: HeartbeatFailure | None = None
        self._last_success_at = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @classmethod
    def for_intent(
        cls,
        client: CairnClient,
        project_id: str,
        intent_id: str,
        worker_name: str,
        task_run_id: str,
        interval: int,
    ) -> "HeartbeatLease":
        return cls(
            heartbeat=lambda: client.heartbeat(
                project_id,
                intent_id,
                worker_name,
                task_run_id=task_run_id,
            ),
            scope=f"project={project_id} intent={intent_id} task_run={task_run_id}",
            worker_name=worker_name,
            interval=interval,
        )

    @classmethod
    def for_reason(
        cls,
        client: CairnClient,
        project_id: str,
        worker_name: str,
        interval: int,
        task_run_id: str | None = None,
    ) -> "HeartbeatLease":
        return cls(
            heartbeat=lambda: client.reason_heartbeat(project_id, worker_name, **({"task_run_id": task_run_id} if task_run_id is not None else {})),
            scope=f"project={project_id} reason",
            worker_name=worker_name,
            interval=interval,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def attach_process(self, process: ManagedProcess | None) -> None:
        self._cancellation.attach_process(process)

    @property
    def failure(self) -> HeartbeatFailure | None:
        return self._failure

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            result = self._heartbeat()
            if result.ok:
                self._last_success_at = time.monotonic()
                continue
            if result.status_code in (403, 409):
                # 403/409 表示 Server 端租约或项目状态已不可继续，立即失败。
                self._fail(result.status_code, result.text)
                return
            elapsed = time.monotonic() - self._last_success_at
            LOG.warning(
                "heartbeat transient failure scope=%s worker=%s status=%s elapsed=%.1fs; retrying",
                self._scope,
                self._worker_name,
                result.status_code,
                elapsed,
            )
            # read timeout 无法证明 Server 未提交续租；等待明确拒绝，避免误杀仍有效的长任务。

    def _fail(self, status_code: int | None, text: str) -> None:
        self._failure = HeartbeatFailure(status_code, text)
        LOG.warning(
            "heartbeat failed scope=%s worker=%s status=%s",
            self._scope,
            self._worker_name,
            status_code,
        )
        # 与项目暂停共用粘性取消：拒绝先于 attach 也必须生效。
        self._cancellation.cancel("heartbeat_lost")
