"""TaskRun 私有浏览器会话登记；任务阶段结束不等于资源生命周期结束。"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
import json
import logging
import threading
import time
import uuid


LOG = logging.getLogger(__name__)


def notify_browser_task(
    container_manager: object,
    project_id: str,
    task_run_id: str | None,
    *,
    local_finished: bool = False,
    terminal_confirmed: bool = False,
) -> None:
    if task_run_id is None:
        return
    try:
        sessions = getattr(container_manager, "browser_sessions", None)
        if sessions is None:
            return
        if local_finished:
            sessions.local_finished(project_id, task_run_id)
        if terminal_confirmed:
            sessions.terminal_confirmed(project_id, task_run_id)
    except Exception as exc:
        # 资源收尾绝不能把已经完成的业务结果重新归类为失败。
        LOG.warning(
            "browser completion notification skipped project=%s task_run=%s error=%s",
            project_id, task_run_id, type(exc).__name__,
        )


@dataclass(frozen=True, slots=True)
class BrowserSession:
    project_id: str
    task_run_id: str
    container_id: str
    name: str
    env: tuple[tuple[str, str], ...]
    context_json: str


@dataclass(slots=True)
class _SessionState:
    session: BrowserSession
    exec_ids: list[str] = field(default_factory=list)
    completed_exec_ids: set[str] = field(default_factory=set)
    local_finished: bool = False
    terminal_confirmed: bool = False
    queued: bool = False
    uncertain: bool = False
    attempts: int = 0
    retry_after: float = 0


class BrowserSessions:
    """仅接受本进程分配的会话，使用本地结束与 Server 确认两个独立条件收口。"""

    def __init__(
        self, *, max_sessions: int = 256, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._clock = clock
        self._max_sessions = max(1, max_sessions)
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, str], _SessionState] = {}
        self._finished: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._policies: dict[tuple[str, str], str] = {}
        self._accept_new = True

    def policy_for_task(self, project_id: str, task_run_id: str, default: str) -> str:
        key = (project_id, task_run_id)
        with self._lock:
            if key in self._finished:
                return "legacy"
            if key in self._policies:
                return self._policies[key]
            if not self._accept_new or len(self._policies) >= self._max_sessions:
                # 登记满后不再认领，避免容量恢复时将既有非托管任务半途纳管。
                self._accept_new = False
                return "legacy"
            self._policies[key] = default
            return default

    def bound_session(self, project_id: str, task_run_id: str) -> BrowserSession | None:
        with self._lock:
            state = self._sessions.get((project_id, task_run_id))
            return state.session if state is not None and not state.local_finished else None

    def keep_unmanaged(self, project_id: str, task_run_id: str) -> None:
        key = (project_id, task_run_id)
        with self._lock:
            state = self._sessions.get(key)
            if state is not None:
                # 关闭授权与阶段绑定分开：退出不确定只能禁止回收，不能让后续阶段丢失会话。
                state.uncertain = True
            elif key in self._policies or len(self._policies) < self._max_sessions:
                self._policies[key] = "legacy"
            else:
                self._accept_new = False

    def prepare(
        self,
        project_id: str,
        task_run_id: str,
        container_id: str,
        *,
        env: dict[str, str],
        context: dict[str, object],
    ) -> BrowserSession | None:
        key = (project_id, task_run_id)
        with self._lock:
            if key in self._finished:
                return None
            existing = self._sessions.get(key)
            if existing is not None:
                prior = existing.session
                if (
                    existing.local_finished or existing.uncertain
                    or prior.container_id != container_id
                    or {k: v for k, v in prior.env if k != "PATH"} != {k: v for k, v in env.items() if k != "PATH"}
                    or prior.context_json != json.dumps(context, sort_keys=True, ensure_ascii=False)
                ):
                    existing.uncertain = True
                    return None
                return prior
            if len(self._sessions) >= self._max_sessions:
                # 宁可不纳管新会话，也不能挤掉仍活跃任务的所有权记录。
                return None
            session = BrowserSession(
                project_id,
                task_run_id,
                container_id,
                f"cairn-task-{uuid.uuid4().hex}",
                tuple(sorted(env.items())),
                json.dumps(context, sort_keys=True, ensure_ascii=False),
            )
            self._sessions[key] = _SessionState(session)
            return session

    def register_exec(self, session: BrowserSession, identity: tuple[str, str] | None) -> None:
        with self._lock:
            state = self._sessions.get((session.project_id, session.task_run_id))
            if state is None or state.session != session:
                return
            if identity is None or identity[0] != session.container_id:
                state.uncertain = True
            elif identity[1] not in state.exec_ids:
                if len(state.exec_ids) >= 256:
                    state.uncertain = True
                else:
                    state.exec_ids.append(identity[1])

    def finish_exec(
        self, session: BrowserSession, identity: tuple[str, str] | None, *, completed: bool,
    ) -> None:
        with self._lock:
            state = self._sessions.get((session.project_id, session.task_run_id))
            if state is None or state.session != session:
                return
            if (
                completed is not True or identity is None
                or identity[0] != session.container_id or identity[1] not in state.exec_ids
            ):
                state.uncertain = True
            else:
                state.completed_exec_ids.add(identity[1])

    def local_finished(self, project_id: str, task_run_id: str) -> None:
        with self._lock:
            state = self._sessions.get((project_id, task_run_id))
            if state is not None:
                state.local_finished = True
            elif (project_id, task_run_id) in self._policies:
                self._remember_finished((project_id, task_run_id))

    def _remember_finished(self, key: tuple[str, str]) -> None:
        self._policies.pop(key, None)
        self._finished[key] = None
        while len(self._finished) > self._max_sessions * 4:
            self._finished.popitem(last=False)

    def terminal_confirmed(self, project_id: str, task_run_id: str) -> None:
        with self._lock:
            state = self._sessions.get((project_id, task_run_id))
            if state is not None:
                state.terminal_confirmed = True

    def claim_ready(self, limit: int = 2) -> list[BrowserSession]:
        ready: list[BrowserSession] = []
        if limit <= 0:
            return ready
        with self._lock:
            for state in self._sessions.values():
                if (
                    state.local_finished
                    and state.terminal_confirmed
                    and not state.queued
                    and state.retry_after <= self._clock()
                ):
                    state.queued = True
                    state.attempts += 1
                    ready.append(state.session)
                    if len(ready) >= limit:
                        break
        return ready

    def finish_cleanup(self, session: BrowserSession, status: str) -> None:
        key = (session.project_id, session.task_run_id)
        with self._lock:
            state = self._sessions.get(key)
            if state is None or state.session != session or not state.queued:
                return
            if status == "retry" and state.attempts < 3:
                state.queued = False
                state.retry_after = self._clock() + 5 * state.attempts
                return
            del self._sessions[key]
            self._remember_finished(key)

    def cleanup_allowed(self, session: BrowserSession) -> bool:
        with self._lock:
            state = self._sessions.get((session.project_id, session.task_run_id))
            return bool(
                state is not None and state.session == session
                and state.local_finished and state.terminal_confirmed
                and state.queued and not state.uncertain
                and bool(state.exec_ids) and state.completed_exec_ids == set(state.exec_ids)
            )

    def execution_ids(self, session: BrowserSession) -> tuple[str, ...] | None:
        with self._lock:
            state = self._sessions.get((session.project_id, session.task_run_id))
            if state is None or state.session != session or state.uncertain:
                return None
            return tuple(state.exec_ids)
