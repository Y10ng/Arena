"""容器清理、取消和日志状态。"""

from __future__ import annotations

from .common import *


class CleanupMixin:
    """容器清理、取消和日志状态。"""

    @staticmethod
    def _pause_cleanup_state(summary: ProjectSummary) -> str | None:
        """人工结束排空期间项目仍为 active，但物理资源必须继续走暂停清理链。"""
        if (
            summary.status == "active"
            and summary.coverage_stop_reason == "user_stopped"
        ):
            return "user_stopped"
        if summary.status in {"paused", "stopped"}:
            return summary.status
        return None

    def _run_candidate_protected_cleanup(
        self,
        container_name: str,
        operation,
        *args,
        **kwargs,
    ) -> bool:
        # 排队后仍重新核验，避免 recovery fence 与清理线程之间的竞态。
        if self._candidate_container_blocked(container_name):
            return False
        return operation(*args, **kwargs)

    def _run_paused_container_cleanup(
        self, project_id: str, generation: int, container_ids: list[str],
    ) -> bool:
        # 清理池可能在继续或再次暂停后才取到任务，Server fence 必须在 stop 前占有。
        def claim(current_id: str | None, daemon_id: str) -> tuple[str | None, str | None]:
            response = self.client.get_container_activation(project_id)
            if not response.ok or not isinstance(response.data, dict):
                raise RuntimeError("container activation unavailable")
            activation = response.data.get("activation")
            if (activation is None and current_id is not None) or (
                isinstance(activation, dict) and activation.get("state") == "inventory"
            ):
                adopted = self.client.adopt_container_pause(project_id, generation, current_id, daemon_id)
                if not adopted.ok:
                    raise RuntimeError("legacy container adoption failed")
                response = self.client.get_container_activation(project_id)
                activation = response.data.get("activation") if response.ok and isinstance(response.data, dict) else None
            if activation is None:
                return None, None
            if not isinstance(activation, dict):
                raise RuntimeError("container activation invalid")
            if activation.get("docker_daemon_id") != daemon_id:
                raise RuntimeError("container daemon identity changed")
            if activation.get("state") == "activating":
                token = activation.get("activation_token")
                if not isinstance(token, str) or not token:
                    raise RuntimeError("container activation token unavailable")
                finished = self.client.finish_container_activation(project_id, token, current_id)
                if not finished.ok:
                    raise RuntimeError("container activation still pending")
            claimed = self.client.claim_container_pause(project_id, generation, daemon_id)
            if not claimed.ok or not isinstance(claimed.data, dict):
                raise RuntimeError("container pause claim failed")
            if claimed.data.get("state") == "stopped":
                return None, claimed.data.get("container_id")
            token = claimed.data.get("stop_token")
            registered_id = claimed.data.get("container_id")
            if not isinstance(token, str) or not token or (registered_id is not None and not isinstance(registered_id, str)):
                raise RuntimeError("container pause claim invalid")
            return token, registered_id

        def confirm(token: str, container_id: str | None, daemon_id: str) -> bool:
            response = self.client.confirm_container_pause(project_id, generation, token, container_id, daemon_id)
            return response.ok and isinstance(response.data, dict) and response.data.get("state") == "stopped"

        project = self.client.get_project(project_id).project
        if project.status != "paused" or project.execution_generation != generation:
            return False
        # 暂停的保留式 stop 是 fence 恢复的前提；claim 与精确资源核验仍在容器锁内完成。
        if self.container_manager.pause_with_confirmation(
            project_id, container_ids=container_ids, claim=claim, confirm=confirm,
        ):
            project = self.client.get_project(project_id).project
            return project.status == "paused" and project.execution_generation == generation
        return False

    def _retain_report_delivery_container(self, project_id: str) -> bool:
        try:
            response = self.client.get_assessment_report_finalization(project_id)
        except requests.RequestException:
            LOG.warning("report delivery cleanup deferred project=%s reason=api_unreachable", project_id)
            return True
        if response.status_code == 404:
            return False
        if not response.ok or not isinstance(response.data, dict) or "delivery_retry_route" not in response.data:
            # 暂时无法取得权威资格不是销毁原 session 的许可；下轮重新读取，不另存状态。
            LOG.warning("report delivery cleanup deferred project=%s reason=eligibility_unavailable status=%s", project_id, response.status_code)
            return True
        route = response.data.get("delivery_retry_route")
        if not isinstance(route, dict) or route.get("dispatcher_id") != self.dispatcher_id:
            return False
        if route.get("allowed") is not True and route.get("reason") != "execution_retry_cooldown":
            return False
        metadata = self.container_manager.runtime_metadata(self.container_manager.container_name(project_id))
        # 仅保留 Server 指定且本 owner 仍持有的原容器；不扩大到同名替换资源。
        return bool(route.get("container_id") and metadata.get("container_id") == route["container_id"] and metadata.get("container_state") == "running")

    def _cleanup_completed_containers(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "completed":
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            if any(
                task.project_id == summary.id for task in self.futures.values()
            ):
                # reason/bootstrap 的完成回调可能仍在同一 future 内生成报告。
                continue
            if self._retain_report_delivery_container(summary.id):
                continue
            container_name = self.container_manager.container_name(summary.id)
            if self._candidate_container_blocked(container_name):
                continue
            if container_name in self._cleanup_pending:
                continue
            if not self.container_manager.needs_completed_cleanup(summary.id):
                self._inactive_cleanup_done[summary.id] = summary.status
                continue
            future = self.cleanup_executor.submit(
                self._run_candidate_protected_cleanup,
                container_name,
                self.container_manager.cleanup_completed,
                summary.id,
            )
            self.cleanup_futures[future] = (container_name, summary.id, summary.status)
            self._cleanup_pending.add(container_name)

    def _cleanup_stopped_containers(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "stopped":
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            if any(
                task.project_id == summary.id and task.task_type == "report"
                for task in self.futures.values()
            ):
                # 暂停报告仍在使用项目容器，结束后再执行 stopped cleanup。
                continue
            if self._retain_report_delivery_container(summary.id):
                continue
            container_name = self.container_manager.container_name(summary.id)
            if self._candidate_container_blocked(container_name):
                continue
            if container_name in self._cleanup_pending:
                continue
            if not self.container_manager.needs_stopped_cleanup(summary.id):
                self._inactive_cleanup_done[summary.id] = summary.status
                continue
            future = self.cleanup_executor.submit(
                self._run_candidate_protected_cleanup,
                container_name,
                self.container_manager.cleanup_stopped,
                summary.id,
            )
            self.cleanup_futures[future] = (container_name, summary.id, summary.status)
            self._cleanup_pending.add(container_name)

    def _cleanup_paused_containers(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            cleanup_state = self._pause_cleanup_state(summary)
            if cleanup_state is None:
                continue
            confirmation_state = cleanup_state
            name = self.container_manager.container_name(summary.id)
            # 只允许暂停项目的保留式 stop 越过 candidate fence；结束项目的删除路径仍须等待捕获完成。
            if cleanup_state != "paused" and self._candidate_container_blocked(name):
                continue
            if name in self._cleanup_pending or any(task.project_id == summary.id for task in self.futures.values()):
                continue
            response = self.client.list_task_runs(summary.id)
            if not response.ok or not isinstance(response.data, list):
                continue
            runs, container_ids = [], []
            for item in response.data:
                if not isinstance(item, dict):
                    continue
                # finish 不应截断已请求暂停的清理链；普通 stopped 任务仍走原保留容器策略。
                if cleanup_state == "stopped" and item.get("pause_requested") is not True:
                    continue
                context = item.get("runtime_context")
                if not isinstance(context, dict) or context.get("dispatcher_id") != self.dispatcher_id or context.get("container_name") != name:
                    continue
                container_id = context.get("container_id")
                exact_ids = context.get("execution_container_ids")
                if not isinstance(exact_ids, list):
                    exact_ids = [container_id] if isinstance(container_id, str) else []
                exact_ids = [identity for identity in exact_ids if isinstance(identity, str) and identity and identity != "unknown"]
                container_ids.extend(exact_ids)
                lifecycle = context.get("execution_lifecycle")
                if not isinstance(lifecycle, dict) or lifecycle.get("cleanup_confirmed") is True:
                    continue
                if type(lifecycle.get("generation")) is not int or lifecycle["generation"] != item.get("execution_generation"):
                    continue
                if item.get("status") not in {"cancelled", "succeeded", "failed", "timed_out"}:
                    continue
                # 候选仍在原容器内时先 stop，再由 recovery 读取 spool 和确认原执行；不可抢先释放 claim。
                if getattr(self, "_candidate_fence_invalid", False) or (
                    summary.id, item.get("id")
                ) in getattr(self, "_candidate_fenced_task_runs", set()):
                    continue
                # 已启动执行缺精确容器 ID 时，不能用当前同名容器替代历史资源证明。
                if lifecycle.get("process_started") is True and not exact_ids:
                    continue
                runs.append(item)
            confirmation_key = (
                ("paused", summary.execution_generation)
                if cleanup_state == "paused" else confirmation_state
            )
            if not runs and (
                cleanup_state == "stopped"
                or self._inactive_cleanup_done.get(summary.id) == confirmation_key
            ):
                continue
            snapshots = getattr(self, "_pause_cleanup_runs", None)
            if snapshots is None:
                snapshots = self._pause_cleanup_runs = {}
            if cleanup_state == "paused":
                future = self.cleanup_executor.submit(
                    self._run_paused_container_cleanup,
                    summary.id,
                    summary.execution_generation,
                    container_ids,
                )
            else:
                future = self.cleanup_executor.submit(
                    self._run_candidate_protected_cleanup,
                    name,
                    self.container_manager.cleanup_finishing,
                    summary.id,
                    container_ids=container_ids,
                )
            snapshots[summary.id] = runs
            self.cleanup_futures[future] = (name, summary.id, confirmation_key)
            self._cleanup_pending.add(name)

    def _confirm_pause_cleanup(self, project_id: str) -> None:
        pending = getattr(self, "_pause_cleanup_confirmations", None)
        if pending is None:
            pending = self._pause_cleanup_confirmations = {}
        for item in getattr(self, "_pause_cleanup_runs", {}).pop(project_id, []):
            pending[(project_id, item["id"])] = item
        self._flush_pause_cleanup_confirmations()

    def _flush_pause_cleanup_confirmations(self) -> None:
        pending = getattr(self, "_pause_cleanup_confirmations", {})
        for (project_id, task_run_id), item in list(pending.items()):
            if getattr(self, "_candidate_fence_invalid", False) or (
                project_id, task_run_id
            ) in getattr(self, "_candidate_fenced_task_runs", set()):
                continue
            lifecycle = {
                **item["runtime_context"]["execution_lifecycle"],
                "termination_confirmed": True,
                "cleanup_confirmed": True,
            }
            try:
                response = self.client.update_task_run(
                    project_id, task_run_id, status=item["status"],
                    runtime_context={"execution_lifecycle": lifecycle},
                )
            except requests.RequestException:
                continue
            if response.ok or response.status_code == 404:
                pending.pop((project_id, task_run_id), None)
            # 拒绝或冲突不是已确认；保留原代次快照等待可靠接受。

    def _queue_container_cleanups(self, summaries: list[ProjectSummary]) -> None:
        # 清理走独立线程池，避免 Docker stop/remove 阻塞调度主循环。
        self._cleanup_completed_containers(summaries)
        self._cleanup_stopped_containers(summaries)
        self._cleanup_paused_containers(summaries)
        self._reconcile_orphan_containers(summaries)
        self._queue_browser_cleanups()

    def _queue_browser_cleanups(self) -> None:
        manager = getattr(self, "container_manager", None)
        store = getattr(manager, "browser_sessions", None)
        cleanup = getattr(manager, "cleanup_browser_session", None)
        if store is None or not callable(cleanup):
            return
        futures = getattr(self, "browser_cleanup_futures", None)
        if futures is None:
            futures = self.browser_cleanup_futures = {}
        # 浏览器清理复用独立清理池，但不挤占业务 worker 或无限积压队列。
        for session in store.claim_ready(limit=max(0, 2 - len(futures))):
            try:
                future = self.cleanup_executor.submit(cleanup, session)
            except Exception as exc:
                LOG.warning(
                    "browser cleanup submit failed project=%s task_run=%s error=%s",
                    session.project_id,
                    session.task_run_id,
                    type(exc).__name__,
                )
                store.finish_cleanup(session, "retry")
                continue
            futures[future] = session

    def _reconcile_orphan_containers(self, summaries: list[ProjectSummary]) -> None:
        # 仅清理本pi实例namespace与owner标签同时匹配的orphan，绝不认领旧Dispatcher容器。
        # manager在列举、反查项目和删除时各自核验归属，防止列举后的名称替换扩大清理范围。
        # 启动健康检查容器使用独立前缀，不会被managed_container_names列出。
        known_ids = {summary.id for summary in summaries}
        for name in self.container_manager.managed_container_names():
            if self._candidate_container_blocked(name):
                continue
            if name in self._cleanup_pending:
                continue
            project_id = self.container_manager.project_id_from_container_name(name)
            # 项目仍存在于 server（含 completed/stopped，它们由各自的 cleanup 路径处理）则跳过。
            if project_id is not None and project_id in known_ids:
                continue
            if not self.container_manager.needs_orphan_cleanup(name):
                continue
            LOG.info("queueing orphan container cleanup container=%s project=%s", name, project_id)
            future = self.cleanup_executor.submit(
                self._run_candidate_protected_cleanup,
                name,
                self.container_manager.cleanup_orphan,
                name,
            )
            self.cleanup_futures[future] = (name, None, None)
            self._cleanup_pending.add(name)

    def _reap_cleanup_futures(self) -> None:
        self._flush_pause_cleanup_confirmations()
        done = [future for future in self.cleanup_futures if future.done()]
        for future in done:
            name, project_id, target_status = self.cleanup_futures.pop(future)
            self._cleanup_pending.discard(name)
            try:
                success = future.result()
                if success is True and project_id is not None and (
                    target_status in ("completed", "stopped", "user_stopped")
                    or isinstance(target_status, tuple) and target_status[0] == "paused"
                ):
                    self._inactive_cleanup_done[project_id] = target_status
                    if target_status in {"user_stopped", "stopped"} or isinstance(target_status, tuple):
                        self._confirm_pause_cleanup(project_id)
                elif project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
            except Exception:
                if project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
                LOG.exception("container cleanup failed container=%s", name)
        browser_futures = getattr(self, "browser_cleanup_futures", {})
        for future in [future for future in browser_futures if future.done()]:
            session = browser_futures.pop(future)
            try:
                status = future.result()
            except Exception as exc:
                LOG.warning(
                    "browser cleanup failed project=%s task_run=%s error=%s",
                    session.project_id,
                    session.task_run_id,
                    type(exc).__name__,
                )
                status = "retry"
            self.container_manager.browser_sessions.finish_cleanup(session, status)

    def _refresh_runtime_projects(self, summaries: list[ProjectSummary]) -> None:
        active_ids = {
            summary.id
            for summary in summaries
            if summary.status == "active"
            and summary.coverage_stop_reason != "user_stopped"
        }
        # 人工结束排空和非 active 项目都从运行集合移出，后续只保留清理任务。
        self.runtime_project_ids.intersection_update(active_ids)
        cleanup_state_by_id = {
            summary.id: (
                ("paused", summary.execution_generation)
                if cleanup_state == "paused" else cleanup_state
            )
            for summary in summaries
            if (cleanup_state := self._pause_cleanup_state(summary)) is not None
        }
        cleanup_state_by_id.update(
            {
                summary.id: summary.status
                for summary in summaries
                if summary.status == "completed"
            }
        )
        for project_id, status in list(self._inactive_cleanup_done.items()):
            current_status = cleanup_state_by_id.get(project_id)
            if current_status != status:
                self._inactive_cleanup_done.pop(project_id, None)

    def _cancel_inactive_tasks(self, summaries: list[ProjectSummary]) -> None:
        summary_by_project = {summary.id: summary for summary in summaries}
        for task in self.futures.values():
            summary = summary_by_project.get(task.project_id)
            status = summary.status if summary is not None else "deleted"
            user_stopping = bool(
                summary is not None
                and status == "active"
                and summary.coverage_stop_reason == "user_stopped"
            )
            if status == "completed":
                # 活跃 future 可能正在完成回调中内联生成报告，等待其自然结束。
                continue
            if (status == "stopped" or user_stopping) and task.task_type == "report":
                # durable 暂停报告不会恢复自治任务，应允许其自然完成并写入冻结交付物。
                continue
            cancellation_reason = "user_stopped" if user_stopping else status
            if (status != "active" or user_stopping) and task.cancellation.cancel(
                cancellation_reason
            ):
                # 项目停止/删除或人工结束排空后取消本地进程，避免继续写回不可接收的新工作。
                LOG.info(
                    "cancelling running task for inactive project project=%s task=%s worker=%s status=%s",
                    task.project_id,
                    task.task_type,
                    task.worker_name,
                    cancellation_reason,
                )

    def _initialize_reason_checkpoints(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "active":
                continue
            if summary.id in self.reason_checkpoints:
                continue
            open_intent_count = summary.working_intent_count + summary.unclaimed_intent_count
            if open_intent_count == 0:
                continue
            self.reason_checkpoints[summary.id] = ReasonCheckpoint(
                fact_count=summary.fact_count,
                hint_count=summary.hint_count,
                open_intent_count=open_intent_count,
                coverage_checkpoint_cell_count=(
                    summary.coverage_checkpoint_cell_count
                ),
                coverage_stop_reason=summary.coverage_stop_reason,
                settled_intent_count=summary.intent_count - open_intent_count,
                execution_generation=summary.execution_generation,
            )
            LOG.debug(
                "reason checkpoint initialized project=%s facts=%s hints=%s open_intents=%s settled_intents=%s generation=%s",
                summary.id,
                summary.fact_count,
                summary.hint_count,
                open_intent_count,
                summary.intent_count - open_intent_count,
                summary.execution_generation,
            )

    def _best_effort_release(self, project_id: str, intent_id: str, worker_name: str, *, task_run_id: str | None = None) -> None:
        response = self.client.release(project_id, intent_id, worker_name, **({"task_run_id": task_run_id} if task_run_id is not None else {}))
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("release failed project=%s intent=%s worker=%s status=%s", project_id, intent_id, worker_name, response.status_code)

    def _best_effort_release_reason(self, project_id: str, worker_name: str, *, task_run_id: str | None = None) -> None:
        response = self.client.release_reason(project_id, worker_name, **({"task_run_id": task_run_id} if task_run_id is not None else {}))
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("reason release failed project=%s worker=%s status=%s", project_id, worker_name, response.status_code)

    def _log_changed(self, scope: str, level: int, message: str, *args: object) -> None:
        state = (level, message, args)
        if self._log_state.get(scope) == state:
            return
        self._log_state[scope] = state
        LOG.log(level, message, *args)

    def _clear_log_state(self, scope: str) -> None:
        self._log_state.pop(scope, None)

    def _clear_project_log_state(self, project_id: str) -> None:
        prefix = f"project:{project_id}:"
        for scope in list(self._log_state):
            if scope.startswith(prefix):
                self._log_state.pop(scope, None)
