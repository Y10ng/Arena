"""task_run 收尾、runtime probe 和 outcome 映射。"""

from __future__ import annotations

from typing import Literal

from cairn.dispatcher.runtime.browser_sessions import notify_browser_task
from cairn.dispatcher.tasks.write_outcomes import classify_write_response

from .common import *


TaskRunFinishDisposition = Literal[
    "acknowledged",
    "retry_pending",
    "rejected",
    "record_gone",
]


class TaskRunFinishMixin:
    """Server task_run 收口与运行时元数据补充。"""

    def _finish_task_run(
        self,
        project_id: str,
        task_run_id: str | None,
        status: str,
        *,
        session_id: str | None = None,
        exit_code: int | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        task_type: str | None = None,
        worker_name: str | None = None,
        worker_type: str | None = None,
        container_runtime: ContainerRuntimeOptions | None = None,
        execution_lifecycle: dict[str, object] | None = None,
        execution_container_ids: list[str] | None = None,
        worker_failure: dict[str, object] | None = None,
    ) -> None:
        if task_run_id is None:
            return
        # 收尾更新失败会缓存到下一个 loop，避免 Server 短暂不可用造成 running 残留。
        finish = PendingTaskRunFinish(
            project_id=project_id,
            task_run_id=task_run_id,
            status=status,
            session_id=session_id,
            exit_code=exit_code,
            error_type=error_type,
            error_message=error_message,
            task_type=task_type,
            worker_name=worker_name,
            worker_type=worker_type,
            container_runtime=container_runtime,
            execution_lifecycle=execution_lifecycle,
            execution_container_ids=execution_container_ids,
            worker_failure=worker_failure,
        )
        pending = self._pending_task_run_finishes()
        key = (project_id, task_run_id)
        pending[key] = finish

    def _flush_pending_task_run_finishes(self, *, attempted: set[tuple[str, str]] | None = None) -> None:
        pending = self._pending_task_run_finishes()
        inflight = getattr(self, "_task_run_finish_futures", None)
        if inflight is None:
            inflight = self._task_run_finish_futures = {}
        for key, (future, finish) in list(inflight.items()):
            if not future.done():
                continue
            del inflight[key]
            try:
                disposition: TaskRunFinishDisposition = future.result()
            except requests.RequestException:
                disposition = "retry_pending"
            except Exception:
                LOG.exception("task run finish crashed project=%s task_run=%s", *key)
                disposition = "retry_pending"
            if disposition != "retry_pending" and pending.get(key) is finish:
                # 只有瞬态失败继续占用本地容量；确定性拒绝或记录消失不会靠盲重放恢复。
                for task_future, task in list(self.futures.items()):
                    if (task.project_id, task.task_run_id) == key and task_future.done():
                        self.futures.pop(task_future)
                pending.pop(key, None)
        for key, finish in list(pending.items()):
            if len(inflight) >= 8:
                break
            if key not in inflight and (attempted is None or key not in attempted):
                # 复用有界清理池；已经进入的版本探针/HTTP 收尾也不能阻挡下一轮暂停。
                inflight[key] = (self.cleanup_executor.submit(self._send_task_run_finish, finish), finish)

    def _pending_task_run_finishes(self) -> dict[tuple[str, str], PendingTaskRunFinish]:
        pending = getattr(self, "pending_task_run_finishes", None)
        if pending is None:
            pending = {}
            self.pending_task_run_finishes = pending
        return pending

    def _send_task_run_finish(self, finish: PendingTaskRunFinish) -> TaskRunFinishDisposition:
        try:
            context = self._finish_runtime_context(
                finish.project_id, finish.task_type, finish.worker_name,
                finish.worker_type, finish.container_runtime,
            )
        except Exception:
            # 工具版本探针/容器读取失败不能丢弃已经取得的执行终止证明。
            context = {}
        if finish.worker_failure is not None:
            context["worker_failure"] = finish.worker_failure
        if finish.execution_lifecycle is not None:
            context["execution_lifecycle"] = finish.execution_lifecycle
        if finish.execution_container_ids is not None:
            context["execution_container_ids"] = finish.execution_container_ids
        response = self.client.update_task_run(
            finish.project_id,
            finish.task_run_id,
            status=finish.status,
            session_id=finish.session_id,
            exit_code=finish.exit_code,
            error_type=finish.error_type,
            error_message=finish.error_message,
            runtime_context=context,
        )
        if response.ok:
            notify_browser_task(
                getattr(self, "container_manager", None),
                finish.project_id,
                finish.task_run_id,
                terminal_confirmed=True,
            )
            return "acknowledged"
        if response.status_code == 404:
            LOG.info(
                "task run finish record gone project=%s task_run=%s status=%s",
                finish.project_id,
                finish.task_run_id,
                finish.status,
            )
            return "record_gone"
        disposition = classify_write_response(response)
        if disposition.retry_same_write:
            LOG.warning(
                "task run finish pending project=%s task_run=%s status=%s response_status=%s",
                finish.project_id,
                finish.task_run_id,
                finish.status,
                response.status_code,
            )
            return "retry_pending"
        # 确定性拒绝不会通过重复 PATCH 改变，且不等同于 Server 已确认终态。
        LOG.info(
            "task run finish rejected project=%s task_run=%s status=%s response_status=%s code=%s disposition=%s",
            finish.project_id,
            finish.task_run_id,
            finish.status,
            response.status_code,
            disposition.safe_code,
            disposition.disposition,
        )
        return "rejected"

    def _finish_runtime_context(
        self,
        project_id: str,
        task_type: str | None,
        worker_name: str | None,
        worker_type: str | None,
        container_runtime: ContainerRuntimeOptions | None = None,
    ) -> dict[str, object]:
        container_name = self.container_manager.container_name(project_id)
        worker = self._worker_by_name(worker_name, worker_type)
        # finish 阶段补充真实容器状态和工具版本，区别于创建时的计划上下文。
        context: dict[str, object] = {
            **self.container_manager.runtime_metadata(container_name),
            "execution_environment": self._execution_environment_runtime_context(project_id, container_runtime),
            "resource_snapshot": self.container_manager.resource_snapshot(container_name),
            "python_version": self._probe_version_cached(container_name, ["python3", "--version"]),
            "node_version": self._probe_version_cached(container_name, ["node", "--version"]),
            "worker_cli_version": "unknown",
            "tool_versions": self._probe_tool_versions(container_name),
        }
        if worker is None:
            return context
        probe = get_driver(worker.type).build_version_probe(worker)
        if probe:
            context["worker_cli_version"] = self._probe_version(container_name, probe, env=dict(worker.env))
        return context

    def _probe_tool_versions(self, container_name: str) -> dict[str, str]:
        return {
            tool: self._probe_version_cached(container_name, command)
            for tool, command in RUNTIME_TOOL_VERSION_PROBES.items()
        }

    def _probe_version_cached(self, container_name: str, command: list[str]) -> str:
        cache = getattr(self, "_runtime_probe_cache", None)
        if cache is None:
            cache = {}
            self._runtime_probe_cache = cache
        key = (container_name, tuple(command))
        if key not in cache:
            # 同一 loop 内多个任务可能共用容器，基础版本探测缓存即可。
            cache[key] = self._probe_version(container_name, command)
        return cache[key]

    def _probe_version(self, container_name: str, command: list[str], *, env: dict[str, str] | None = None) -> str:
        result: ProcessResult | None = None
        try:
            process = self.container_manager.build_exec_process(
                container_name,
                env or {},
                command,
                timeout_seconds=RUNTIME_PROBE_TIMEOUT_SECONDS,
            )
            process.start()
            result = process.communicate(timeout=RUNTIME_PROBE_TIMEOUT_SECONDS + 2)
            return self._version_from_probe_result(result)
        except Exception as exc:
            LOG.debug("runtime probe failed container=%s command=%s error=%s", container_name, command, exc)
            return "unknown"
        finally:
            if result is not None:
                # 版本探针只上送有界单行值，不保留完整输出文件。
                result.cleanup()

    @staticmethod
    def _version_from_probe_result(result: ProcessResult) -> str:
        if result.timed_out or result.cancelled or result.returncode != 0:
            return "unknown"
        stdout = result.read_stdout().strip()
        output = (stdout or result.read_stderr().strip()).splitlines()
        if not output:
            return "unknown"
        return _runtime_probe_value(output[0])

    def _worker_by_name(self, worker_name: str | None, worker_type: str | None) -> WorkerConfig | None:
        if worker_name is None:
            return None
        for worker in self.config.workers:
            if worker.name == worker_name and (worker_type is None or worker.type == worker_type):
                return worker
        return None

    @staticmethod
    def _task_run_status_for_outcome(outcome: str) -> str:
        if outcome == "success":
            return "succeeded"
        if outcome == "cancelled":
            return "cancelled"
        if outcome in ("worker_timeout", "conclude_timeout"):
            return "timed_out"
        return "failed"

    @staticmethod
    def _task_run_error_type_for_outcome(outcome: str, status: str) -> str | None:
        if status == "succeeded":
            return None
        mapped = {
            "unhealthy": "worker_unhealthy",
            "rejected": "worker_rejected",
            "failed": "unknown",
        }.get(outcome, outcome)
        if mapped in STANDARD_ERROR_TYPES:
            return mapped
        return "unknown"

    @staticmethod
    def _task_run_error_message_for_error_type(error_type: str | None) -> str | None:
        if error_type is None:
            return None
        return ERROR_MESSAGES.get(error_type, ERROR_MESSAGES["unknown"])
