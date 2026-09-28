"""启动时收口旧 running task_run。"""

from __future__ import annotations

from .common import *


class StaleReconcileMixin:
    """启动时收口旧 running task_run。"""

    def _reconcile_stale_running_task_runs(self, summaries: list[ProjectSummary]) -> None:
        # 只处理 Server 仍标记 running、但当前进程没有 future 的 task_run。
        current_task_run_ids = {
            task.task_run_id
            for task in self.futures.values()
            if task.task_run_id is not None
        }
        report_journal_task_runs = {
            (entry.project_id, entry.task_run_id)
            for entry in self.report_finish_outbox.entries()
        }
        candidate_fenced_task_runs = getattr(
            self,
            "_candidate_fenced_task_runs",
            set(),
        )
        candidate_fences_invalid = getattr(
            self,
            "_candidate_fence_invalid",
            False,
        )
        reconciled_count = 0
        for summary in summaries:
            response = self.client.list_task_runs(summary.id)
            if not response.ok:
                LOG.warning(
                    "startup stale task reconcile list failed project=%s status=%s body=%s",
                    summary.id,
                    response.status_code,
                    response.text,
                )
                continue
            if not isinstance(response.data, list):
                LOG.warning("startup stale task reconcile returned non-list project=%s", summary.id)
                continue
            stale_items: list[dict[str, object]] = []
            for item in response.data:
                if not isinstance(item, dict):
                    continue
                task_run_id = item.get("id")
                if not isinstance(task_run_id, str) or not task_run_id:
                    continue
                if item.get("status") != "running":
                    continue
                if task_run_id in current_task_run_ids:
                    continue
                if (summary.id, task_run_id) in report_journal_task_runs:
                    # report journal 自己持有 authoritative recovery，generic PATCH 不得抢先终结。
                    continue
                if (
                    candidate_fences_invalid
                    or (summary.id, task_run_id) in candidate_fenced_task_runs
                ):
                    # candidate recovery 未完成前，generic PATCH 不能抢先终结执行。
                    continue
                stale_items.append(item)

            command_events = self._stale_running_command_events(summary.id, stale_items)
            for item in stale_items:
                task_run_id = item.get("id")
                if not isinstance(task_run_id, str):
                    continue
                finish = self._stale_running_finish(item, command_events.get(task_run_id, []))
                error_message = finish["error_message"]
                if finish["error_type"] == "worker_unhealthy" and item.get("error_message"):
                    error_message = str(item.get("error_message"))
                update = self.client.update_task_run(
                    summary.id,
                    task_run_id,
                    status=str(finish["status"]),
                    error_type=finish["error_type"],
                    error_message=error_message,
                    runtime_context={
                        "startup_reconcile": {
                            # 记录观察到的旧状态，后续审计能解释为什么 dispatcher 主动收口。
                            "dispatcher_id": self.dispatcher_id,
                            **finish["runtime_context"],
                            "observed_status": item.get("status"),
                            "observed_worker_name": item.get("worker_name"),
                            "observed_worker_type": item.get("worker_type"),
                            "observed_started_at": item.get("started_at"),
                            "reconciled_at": _utcnow(),
                        }
                    },
                )
                if update.ok:
                    reconciled_count += 1
                    if finish["error_type"] == "worker_oom":
                        task_type = item.get("task_type")
                        worker_name = item.get("worker_name")
                        if (
                            isinstance(task_type, str)
                            and task_type
                            and isinstance(worker_name, str)
                            and worker_name
                        ):
                            self._mark_worker_rejected(
                                (summary.id, task_type, worker_name),
                                "worker_oom",
                            )
                        else:
                            LOG.warning(
                                "startup OOM reconcile missing rejection scope project=%s task_run=%s task_type=%r worker=%r",
                                summary.id,
                                task_run_id,
                                task_type,
                                worker_name,
                            )
                    LOG.info(
                        "startup reconciled stale running task project=%s task_run=%s error_type=%s",
                        summary.id,
                        task_run_id,
                        finish["error_type"],
                    )
                    continue
                LOG.warning(
                    "startup stale task reconcile update failed project=%s task_run=%s status=%s body=%s",
                    summary.id,
                    task_run_id,
                    update.status_code,
                    update.text,
                )
        if reconciled_count:
            LOG.info("startup stale task reconcile completed count=%s", reconciled_count)

    def _stale_running_command_events(
        self,
        project_id: str,
        stale_items: list[dict[str, object]],
    ) -> dict[str, list[dict[str, object]]]:
        task_run_ids = [str(item["id"]) for item in stale_items if isinstance(item.get("id"), str)]
        if not task_run_ids or not hasattr(self.client, "list_task_run_command_events"):
            return {}
        response = self.client.list_task_run_command_events(project_id, task_run_ids)
        if not response.ok:
            LOG.warning(
                "startup stale task command event list failed project=%s status=%s body=%s",
                project_id,
                response.status_code,
                response.text,
            )
            return {}
        if not isinstance(response.data, list):
            LOG.warning("startup stale task command event list returned non-list project=%s", project_id)
            return {}
        events_by_run: dict[str, list[dict[str, object]]] = {}
        for item in response.data:
            if not isinstance(item, dict):
                continue
            task_run_id = item.get("task_run_id")
            events = item.get("events")
            if isinstance(task_run_id, str) and isinstance(events, list):
                events_by_run[task_run_id] = [event for event in events if isinstance(event, dict)]
        return events_by_run

    def _stale_running_finish(
        self,
        task_run: dict[str, object],
        command_events: list[dict[str, object]],
    ) -> dict[str, object]:
        latest_finished = self._latest_command_finished(command_events)
        if latest_finished is None:
            # 无命令完成事件通常意味着旧 dispatcher 崩溃或心跳中断。
            return {
                "status": "failed",
                "error_type": "heartbeat_lost",
                "error_message": "Dispatcher 启动时发现旧 running 且无当前 future，已按心跳丢失收口",
                "runtime_context": {
                    "reason": "running_without_current_future",
                    "command_event_status": "missing",
                },
            }

        phase = str(latest_finished.get("phase") or "unknown")
        # 有命令完成事件时尽量按最近命令的状态还原更准确的失败类型。
        oom_killed = latest_finished.get("oom_killed") is True
        timed_out = (
            not oom_killed
            and (
                latest_finished.get("timed_out") is True
                or latest_finished.get("returncode") in (124, 137)
            )
        )
        cancelled = latest_finished.get("cancelled") is True
        failed = (
            latest_finished.get("status") == "failed"
            or _int_or_none(latest_finished.get("exit_code")) not in (None, 0)
            or _int_or_none(latest_finished.get("returncode")) not in (None, 0)
        )
        if cancelled:
            status = "cancelled"
            error_type = "cancelled"
            error_message = ERROR_MESSAGES[error_type]
            reason = "latest_command_finished_cancelled"
        elif oom_killed:
            status = "failed"
            error_type = "worker_oom"
            error_message = ERROR_MESSAGES[error_type]
            reason = "latest_command_finished_oom"
        elif timed_out:
            status = "timed_out"
            error_type = "conclude_timeout" if "conclude" in phase else "worker_timeout"
            error_message = ERROR_MESSAGES[error_type]
            reason = "latest_command_finished_timeout"
        elif failed:
            status = "failed"
            error_type = "worker_unhealthy" if "healthcheck" in phase else "worker_command_failed"
            error_message = ERROR_MESSAGES[error_type]
            reason = "latest_command_finished_failed"
        else:
            status = "failed"
            error_type = "heartbeat_lost"
            error_message = "Dispatcher 启动时发现旧 running 且无当前 future，已按心跳丢失收口"
            reason = "running_without_current_future"
        return {
            "status": status,
            "error_type": error_type,
            "error_message": error_message,
            "runtime_context": {
                "reason": reason,
                "command_event_status": "observed",
                "latest_command_kind": latest_finished.get("command_kind"),
                "latest_command_phase": latest_finished.get("phase"),
                "latest_command_status": latest_finished.get("status"),
                "latest_command_exit_code": latest_finished.get("exit_code"),
                "latest_command_returncode": latest_finished.get("returncode"),
                "latest_command_timed_out": latest_finished.get("timed_out"),
                "latest_command_cancelled": latest_finished.get("cancelled"),
                "latest_command_oom_killed": latest_finished.get("oom_killed"),
                "latest_command_oom_evidence_source": latest_finished.get(
                    "oom_evidence_source"
                ),
            },
        }

    @staticmethod
    def _latest_command_finished(command_events: list[dict[str, object]]) -> dict[str, object] | None:
        finished = [event for event in command_events if event.get("kind") == "command_finished"]
        return finished[-1] if finished else None
