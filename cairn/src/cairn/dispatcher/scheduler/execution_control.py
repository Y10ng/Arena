"""独立观察项目控制意图，不占用研究 executor，也不重建执行现场。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
import threading
import time

from requests import RequestException

from cairn.dispatcher.runtime.pause_clock import ExecutionSceneLost

LOG = logging.getLogger(__name__)
RESEARCH_TYPES = {"bootstrap", "reason", "explore"}


class ExecutionControlObserver:
    def __init__(self, loop) -> None:
        self.loop = loop
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="execution-control")
        self._pending = {}
        self._acknowledged = {}
        self._scenes = {}
        self._retry_after = {}
        self._thread = threading.Thread(target=self._watch, name="execution-control-observer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=12)
        self._pool.shutdown(wait=True)

    def _tasks(self, project_id):
        # copy 不跨线程迭代主循环正在修改的 dict；业务状态仍以 Server 为准。
        return [task for future, task in self.loop.futures.copy().items()
                if task.project_id == project_id and task.task_type in RESEARCH_TYPES
                and not (future.done() and task.cancellation.execution_lifecycle(task_finished=True)["termination_confirmed"])]

    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                self.observe(self.loop.client.list_projects())
            except (RequestException, RuntimeError, ValueError):
                LOG.warning("execution control observation deferred")
            self._stop.wait(0.2)

    def observe(self, summaries) -> None:
        for project_id, future in list(self._pending.items()):
            if future.done():
                try:
                    future.result()
                except Exception:
                    # 原始异常可能含私有控制路径，不进入公开错误投影。
                    LOG.exception("execution control deferred project=%s", project_id)
                self._pending.pop(project_id)
        for summary in summaries:
            expiry = summary.authorization_expires_at
            if expiry is not None and datetime.now(timezone.utc) >= datetime.fromisoformat(expiry.replace("Z", "+00:00")):
                # 平台执行时钟可以冻结，真实授权不可以；取消广播不等待模型或 Docker。
                for task in self._tasks(summary.id):
                    task.cancellation.cancel("project_authorization_expired")
            control = getattr(summary, "execution_control", {}) or {}
            if self.loop.dispatcher_id not in control.get("dispatcher_ids", []):
                continue
            request_id = control.get("request_id")
            if not request_id or self._acknowledged.get(summary.id) == request_id:
                continue
            if summary.id in self._pending or len(self._pending) >= 4:
                continue
            if time.monotonic() < self._retry_after.get(request_id, 0):
                continue
            if control.get("action") == "pause" and control.get("state") == "paused":
                # 重启只保留现场，不冒充重新接管了旧模型流。
                self.loop.container_manager.research_clock(summary.id).pause()
                self._acknowledged[summary.id] = request_id
                continue
            if control.get("state") not in {"requested", "failed"}:
                continue
            if control.get("action") == "resume" and control.get("state") == "failed":
                # 恢复失败后由正常入口重新核验；部署或轮询不能替用户继续研究。
                continue
            self._pending[summary.id] = self._pool.submit(self.apply, summary.id, control)

    def apply(self, project_id: str, control: dict) -> None:
        project = self.loop.client.get_project(project_id).project
        current = project.execution_control
        if current.get("request_id") != control["request_id"]:
            return
        manager = self.loop.container_manager
        action = control["action"]
        tasks = self._tasks(project_id)
        resources = []
        state = {"pause": "paused", "resume": "running", "finish": "stopped"}[action]
        error = None
        try:
            if action == "resume" and project.authorization_expires_at is not None and datetime.now(timezone.utc) >= datetime.fromisoformat(project.authorization_expires_at.replace("Z", "+00:00")):
                raise RuntimeError("project_authorization_expired")
            ids = {identity for task in tasks for identity in task.cancellation.execution_container_ids()}
            # readiness 在创建 Future 之前登记；不能把本地 Future 列表当作全部物理库存。
            response = self.loop.client.list_task_runs(project_id)
            activation_response = self.loop.client.get_container_activation(project_id)
            if not response.ok or not isinstance(response.data, list) or not activation_response.ok:
                raise RuntimeError("execution owner inventory unavailable")
            activation = activation_response.data.get("activation")
            if activation and activation.get("state") != "stopped" and activation.get("container_id"):
                ids.add(activation["container_id"])
            has_retained_run = False
            for run in response.data:
                context = run.get("runtime_context") or {}
                if run.get("task_type") not in RESEARCH_TYPES or context.get("dispatcher_id") != self.loop.dispatcher_id:
                    continue
                lifecycle = context.get("execution_lifecycle") or {}
                terminated = (run.get("status") in {"succeeded", "failed", "cancelled", "timed_out"}
                              and type(lifecycle.get("generation")) is int
                              and lifecycle["generation"] == run.get("execution_generation")
                              and lifecycle.get("termination_confirmed") is True)
                if terminated and lifecycle.get("cleanup_confirmed") is True and action != "finish":
                    continue
                has_retained_run = True
                if action == "resume":
                    if run.get("execution_generation") != project.execution_generation:
                        raise ExecutionSceneLost("原执行代次与当前项目不一致")
                    # 终止证明与环境清理是两件事；只有仍在途的每个 Run 需要自己的宿主连接。
                    if not terminated and not any(
                        task.task_run_id == run.get("id")
                        and task.execution_generation == run.get("execution_generation")
                        for task in tasks
                    ):
                        raise ExecutionSceneLost("原 Dispatcher 执行连接不在本进程中，不能伪造原地恢复")
                ids.update(identity for identity in context.get("execution_container_ids", []) if identity and identity != "unknown")
                delivery = context.get("execution_delivery") or {}
                ids.update(identity for identity in (context.get("container_id"), delivery.get("container_id"))
                           if identity not in {None, "", "unknown"})
            if action == "resume":
                # 聚合 resources 可能属于其他 owner 或前次观测；只用本 owner 的原始现场身份。
                ids.update(item["container_id"] for item in self._scenes.get(project_id, []) if item.get("kind") == "research")
                if (ids or has_retained_run) and (not activation or activation.get("state") == "stopped" or activation.get("container_id") not in ids):
                    raise ExecutionSceneLost("原研究容器的登记身份不可确认")
                if activation and activation.get("state") != "stopped":
                    if (activation.get("dispatcher_id") != self.loop.dispatcher_id
                        or activation.get("activation_generation") != project.execution_generation
                        or activation.get("docker_daemon_id") != manager.docker_daemon_id()):
                        raise ExecutionSceneLost("原研究容器的 owner、代次或 Docker daemon 已改变")
            elif action == "finish":
                for task in tasks:
                    task.cancellation.cancel(project.coverage_stop_reason or "user_stopped")
            else:
                for task in tasks:
                    task.cancellation.pause_clock.pause()
            resources = manager.hot_control(project_id, action, container_ids=sorted(ids), in_flight=bool(tasks))
            if action == "pause":
                self._scenes[project_id] = resources
            elif action == "resume":
                for task in tasks:
                    task.cancellation.pause_clock.resume()
                self._scenes.pop(project_id, None)
            else:
                stopped_ids = {item["container_id"] for item in resources if item.get("kind") == "research"}
                for task in tasks:
                    task.cancellation.confirm_stopped_containers(stopped_ids)
                self._scenes.pop(project_id, None)
                if project.coverage_stop_reason == "pause_timeout":
                    # 重启遗留的暂停 Run 没有 Future 回调；精确实停后沿原更新接口收口。
                    # 不写 execution_lifecycle，控制回执与进程退出证明仍各自保留事实边界。
                    local_runs = {task.task_run_id for task in self.loop.futures.copy().values()
                                  if task.project_id == project_id}
                    for run in response.data:
                        if (run.get("task_type") not in RESEARCH_TYPES or run.get("status") not in {"queued", "running"}
                            or run.get("id") in local_runs
                            or (run.get("runtime_context") or {}).get("dispatcher_id") != self.loop.dispatcher_id):
                            continue
                        settled = self.loop.client.update_task_run(
                            project_id, run["id"], status="cancelled", error_type="cancelled", error_message="pause_timeout",
                        )
                        if not settled.ok:
                            raise RuntimeError("pause_timeout_run_settlement_deferred")
        except ExecutionSceneLost:
            state, error = "lost", "execution_scene_lost"
        except Exception:
            state, error = "failed", "execution_control_failed"
            LOG.warning("execution control physical state unconfirmed project=%s action=%s", project_id, action)
        response = self.loop.client.confirm_execution_control(
            project_id, request_id=control["request_id"], state=state, resources=resources, error=error,
        )
        if response.ok and state != "failed":
            self._acknowledged[project_id] = control["request_id"]
        else:
            # 回执失败不算物理成功；pause/finish 可继续收敛，resume 失败须由用户发起新请求。
            self._retry_after[control["request_id"]] = time.monotonic() + 2
