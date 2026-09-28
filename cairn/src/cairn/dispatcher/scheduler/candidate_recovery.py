"""CandidateStore durable fence 恢复与 exact request 重放。"""

from __future__ import annotations

from .common import *
from cairn.dispatcher.runtime.containers import CandidateExecutionCheck
from cairn.dispatcher.tasks.candidate_capture_outbox import (
    CandidateCaptureOutboxError,
    CandidateExecutionFence,
    TERMINATION_ATTEMPT_LIMIT,
)
from cairn.dispatcher.tasks.candidate_intake import (
    intake_worker_candidates,
    replay_candidate_outbox,
)
from cairn.dispatcher.tasks.report_finish_outbox import (
    PendingReportExecution,
    PendingReportFinish,
    PendingReportOutput,
    ReportFinishOutboxConflict,
    failed_finish_for_recovery,
)
from cairn.server.schemas.common import TaskRunErrorType


_CANDIDATE_RECOVERY_FENCE_LIMIT = 4
_CANDIDATE_REPLAY_ENTRY_LIMIT = 16
_TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}
_RECOVERY_LOG_INITIAL_SECONDS = 15.0
_RECOVERY_LOG_MAX_SECONDS = 300.0
_RECOVERY_RETRY_INITIAL_SECONDS = 15.0
_RECOVERY_RETRY_MAX_SECONDS = 300.0
_RECOVERY_DIAGNOSTIC_WINDOW_SECONDS = 900.0


def _candidate_recovery_failure(
    check: CandidateExecutionCheck,
    *,
    process_started: bool | None,
) -> tuple[TaskRunErrorType, str]:
    if check.status == "gone":
        return (
            "container_failed",
            "受控执行所在容器已在同一 Docker daemon 上确认不存在；"
            "已保留宿主机收到的候选，容器内未同步内容无法恢复",
        )
    if process_started is False:
        return (
            "delivery_unavailable",
            "Dispatcher 重启前执行尚未启动，已安全结束本次任务",
        )
    return (
        "delivery_unavailable",
        "Dispatcher 重启前未持久化任务结果；已保留候选并确认原执行停止",
    )


class CandidateRecoveryMixin:
    """只恢复 candidate 捕获，不重建、恢复或重新启动模型执行。"""

    def _refresh_candidate_recovery(self) -> bool:
        outbox = getattr(self, "candidate_capture_outbox", None)
        manager = getattr(self, "container_manager", None)
        if (
            outbox is None
            or manager is None
            or not callable(getattr(outbox, "fences", None))
        ):
            self._set_candidate_fence_snapshot([], invalid=False)
            return True

        try:
            fences = outbox.fences()
        except CandidateCaptureOutboxError:
            self._set_candidate_fence_snapshot([], invalid=True)
            LOG.error("candidate recovery blocked code=candidate_fence_outbox_invalid")
            return False

        # 先发布完整 blocker，再对有界子集做 I/O，避免未轮到的 fence 被派发或清理绕过。
        self._set_candidate_fence_snapshot(fences, invalid=False)
        active_task_runs = {
            (task.project_id, task.task_run_id)
            for task in self.futures.values()
            if getattr(task, "task_run_id", None) is not None
        }
        unknown_owner_projects = {
            task.project_id for task in self.futures.values()
            if getattr(task, "task_run_id", None) is None
        }
        # 同项目健康 B 不应妨碍失联 A 的恢复；仅跳过仍由本地 future 交付的 Run。
        try:
            replay = replay_candidate_outbox(
                self.client,
                outbox,
                max_entries=_CANDIDATE_REPLAY_ENTRY_LIMIT,
                excluded_project_ids=unknown_owner_projects,
                excluded_task_runs=active_task_runs,
            )
        except CandidateCaptureOutboxError:
            self._set_candidate_fence_snapshot(fences, invalid=True)
            LOG.error("candidate recovery blocked code=candidate_outbox_invalid")
            return False
        if replay.global_integrity_error is not None:
            self._set_candidate_fence_snapshot(fences, invalid=True)
            LOG.error(
                "candidate recovery blocked code=%s",
                replay.global_integrity_error,
            )
            return False
        if replay.integrity_error is not None:
            LOG.warning(
                "candidate recovery isolated projects=%s code=%s",
                sorted(replay.blocked_project_ids),
                replay.integrity_error,
            )

        recoverable = [
            fence for fence in fences
            if fence.project_id not in unknown_owner_projects
            and (fence.project_id, fence.task_run_id) not in active_task_runs
        ]
        selected = self._candidate_recovery_batch(recoverable)
        for fence in selected:
            self._recover_candidate_fence(fence)
        try:
            remaining = outbox.fences()
        except CandidateCaptureOutboxError:
            self._set_candidate_fence_snapshot([], invalid=True)
            return False
        self._set_candidate_fence_snapshot(
            remaining,
            invalid=False,
            blocked_project_ids=replay.blocked_project_ids,
        )
        return not remaining

    def _candidate_recovery_batch(
        self,
        fences: list[CandidateExecutionFence],
    ) -> list[CandidateExecutionFence]:
        if not fences:
            return []
        ordered = sorted(fences, key=lambda fence: (fence.created_at, fence.capture_id))
        cursor = getattr(self, "_candidate_recovery_cursor", 0) % len(ordered)
        rotated = ordered[cursor:] + ordered[:cursor]
        selected = rotated[:_CANDIDATE_RECOVERY_FENCE_LIMIT]
        self._candidate_recovery_cursor = (
            cursor + len(selected)
        ) % len(ordered)
        return selected

    def _recover_candidate_fence(self, fence: CandidateExecutionFence) -> None:
        outbox = self.candidate_capture_outbox
        if outbox is None:
            return
        try:
            current = next(
                (item for item in outbox.fences() if item.capture_id == fence.capture_id),
                None,
            )
        except CandidateCaptureOutboxError:
            self._set_candidate_fence_snapshot([], invalid=True)
            return
        if current is None:
            return
        fence = current
        if fence.recovery_status is not None:
            if (
                fence.recovery_status == "stopped"
                and fence.start_attempted is not False
                and fence.terminal_proof_version != 2
            ):
                # 旧 ready fence 即使已取回候选，旧进程组证明也不足以释放整个 Run。
                if not self._candidate_recovery_due(fence):
                    return
                assert fence.container_id is not None
                assert fence.exec_id is not None
                assert fence.control_path is not None
                check = self.container_manager.candidate_execution_status(
                    docker_daemon_id=fence.docker_daemon_id,
                    container_id=fence.container_id,
                    container_name=fence.container_name,
                    project_id=fence.project_id,
                    exec_id=fence.exec_id,
                    control_path=fence.control_path,
                )
                if check.status not in {"stopped", "gone"}:
                    diagnostic_exhausted, retry_at = self._schedule_candidate_recovery_retry(
                        fence, check.reason,
                    )
                    self._observe_candidate_recovery(
                        fence, CandidateExecutionCheck("unknown", check.reason),
                        diagnostic_exhausted=diagnostic_exhausted, retry_at=retry_at,
                    )
                    self._log_candidate_recovery_deferred(fence, check.reason)
                    return
                promoted = fence.confirm_terminal()
                try:
                    outbox.replace_fence(fence, promoted)
                except CandidateCaptureOutboxError:
                    self._log_candidate_recovery_deferred(fence, "terminal_fence_persist_failed")
                    return
                fence = promoted
                self._clear_candidate_recovery_retry(fence.capture_id)
            self._finalize_candidate_recovery_group(fence)
            return
        if not fence.execution_bound:
            # bind callback 在 exec_start 前运行；unbound fence 可正向证明模型未启动。
            check = CandidateExecutionCheck("stopped", "execution_not_started")
            self._observe_candidate_recovery(fence, check)
            self._record_candidate_recovery(fence, check, container_spool_recovered=False)
            return

        assert fence.container_id is not None
        assert fence.exec_id is not None
        assert fence.control_path is not None
        if fence.start_attempted is False:
            # durable bind 先于启动尝试；该边界仍为 false 时可证明 exec_start 从未调用。
            check = CandidateExecutionCheck("stopped", "execution_not_started")
            self._observe_candidate_recovery(fence, check)
            self._record_candidate_recovery(fence, check, container_spool_recovered=False)
            return
        if not self._candidate_recovery_due(fence):
            return
        stop_futures = getattr(self, "_candidate_stop_futures", {})
        stop_future = stop_futures.get(fence.capture_id)
        if stop_future is not None and not stop_future.done():
            self._observe_candidate_recovery(
                fence, CandidateExecutionCheck("running", "task_termination_pending"),
            )
            return
        if fence.terminal_proof_version == 2:
            # 只有后代排空或精确容器停机的新证明才能免于再次物理复查。
            check = CandidateExecutionCheck("stopped", "recorded_terminal_confirmed")
            self._clear_candidate_recovery_retry(fence.capture_id)
        else:
            if stop_future is not None:
                stop_futures.pop(fence.capture_id, None)
                try:
                    check = stop_future.result()
                except Exception:
                    check = CandidateExecutionCheck("unknown", "task_termination_control_unavailable")
            else:
                check = self.container_manager.candidate_execution_status(
                    docker_daemon_id=fence.docker_daemon_id,
                    container_id=fence.container_id,
                    container_name=fence.container_name,
                    project_id=fence.project_id,
                    exec_id=fence.exec_id,
                    control_path=fence.control_path,
                )
            if check.status == "unknown":
                diagnostic_exhausted, retry_at = self._schedule_candidate_recovery_retry(
                    fence,
                    check.reason,
                )
                self._observe_candidate_recovery(
                    fence,
                    check,
                    diagnostic_exhausted=diagnostic_exhausted,
                    retry_at=retry_at,
                )
                self._log_candidate_recovery_deferred(fence, check.reason)
                return
            if check.status != "running":
                self._clear_candidate_recovery_retry(fence.capture_id)
        self._observe_candidate_recovery(fence, check)
        if check.status == "running":
            if fence.termination_attempt_count >= TERMINATION_ATTEMPT_LIMIT:
                check = CandidateExecutionCheck("unknown", "task_termination_budget_exhausted")
                diagnostic_exhausted, retry_at = self._schedule_candidate_recovery_retry(
                    fence, check.reason,
                )
                self._observe_candidate_recovery(
                    fence, check, diagnostic_exhausted=True, retry_at=retry_at,
                )
                self._log_candidate_recovery_deferred(fence, check.reason)
                return
            self._request_candidate_task_stop(fence)
            return
        if check.status == "gone":
            # 同一 daemon 上确认容器消失后，容器内 spool 已不可恢复；宿主 backlog 已在本轮先重放。
            self._record_candidate_recovery(fence, check, container_spool_recovered=False)
            return

        if fence.terminal_proof_version != 2:
            promoted = fence.confirm_terminal()
            try:
                outbox.replace_fence(fence, promoted)
            except CandidateCaptureOutboxError:
                self._log_candidate_recovery_deferred(
                    fence,
                    "terminal_fence_persist_failed",
                )
                return
            fence = promoted
        summary = intake_worker_candidates(
            self.client,
            self.container_manager,
            fence.container_id,
            fence.candidate_spool(),
            outbox,
            project_id=fence.project_id,
            task_run_id=fence.task_run_id,
            phase=fence.phase,
            session=fence.session,
        )
        if summary.integrity_error == "candidate_spool_read_failed":
            # 已确认执行终态后只在 spool 不可读时复查容器，避免把已消失容器当作可恢复内容无限等待。
            spool_check = self.container_manager.candidate_execution_status(
                docker_daemon_id=fence.docker_daemon_id,
                container_id=fence.container_id,
                container_name=fence.container_name,
                project_id=fence.project_id,
                exec_id=fence.exec_id,
                control_path=fence.control_path,
            )
            if spool_check.status == "gone":
                self._clear_candidate_recovery_retry(fence.capture_id)
                self._observe_candidate_recovery(fence, spool_check)
                self._record_candidate_recovery(
                    fence, spool_check, container_spool_recovered=False,
                )
                return
            if spool_check.status == "unknown":
                diagnostic_exhausted, retry_at = self._schedule_candidate_recovery_retry(
                    fence,
                    spool_check.reason,
                )
                self._observe_candidate_recovery(
                    fence,
                    spool_check,
                    diagnostic_exhausted=diagnostic_exhausted,
                    retry_at=retry_at,
                )
                self._log_candidate_recovery_deferred(fence, spool_check.reason)
                return
            self._schedule_candidate_recovery_retry(
                fence,
                "candidate_spool_read_failed",
            )
            self._log_candidate_recovery_deferred(
                fence,
                "candidate_spool_read_failed",
            )
            return
        if not summary.host_durable or summary.integrity_error is not None:
            self._log_candidate_recovery_deferred(
                fence,
                summary.integrity_error or "candidate_capture_incomplete",
            )
            return
        self._record_candidate_recovery(fence, check, container_spool_recovered=True)

    def _candidate_stop_authority(self, fence: CandidateExecutionFence) -> str | None:
        """只有 Server 已撤销原 Run 且 Dispatcher 仍持有同一归属时才能干预。"""
        try:
            response = self.client.list_task_runs(fence.project_id)
        except requests.RequestException:
            return "task_run_authority_unavailable"
        if not response.ok or not isinstance(response.data, list):
            return "task_run_authority_unavailable"
        run = next(
            (item for item in response.data if isinstance(item, dict)
             and item.get("id") == fence.task_run_id),
            None,
        )
        if run is None:
            return "task_run_authority_unavailable"
        if run.get("status") not in _TERMINAL_TASK_STATUSES:
            return "task_run_still_active"
        context = run.get("runtime_context")
        context = context if isinstance(context, dict) else {}
        if (
            context.get("dispatcher_id") != fence.owner_id
            or getattr(self, "dispatcher_id", None) != fence.owner_id
        ):
            return "task_run_owner_mismatch"
        return None

    def _reserve_candidate_stop(self, fence: CandidateExecutionFence) -> CandidateExecutionCheck | None:
        """物理预检之后、信号之前原子预留一次额度；失败不得发送信号。"""
        reason = self._candidate_stop_authority(fence)
        if reason is not None:
            return CandidateExecutionCheck("unknown", reason)
        try:
            current = next(
                (item for item in self.candidate_capture_outbox.fences()
                 if item.capture_id == fence.capture_id),
                None,
            )
            if current != fence:
                return CandidateExecutionCheck("unknown", "task_termination_fence_changed")
            if current.termination_attempt_count >= TERMINATION_ATTEMPT_LIMIT:
                return CandidateExecutionCheck("unknown", "task_termination_budget_exhausted")
            self.candidate_capture_outbox.replace_fence(current, current.mark_termination_attempted())
        except CandidateCaptureOutboxError:
            return CandidateExecutionCheck("unknown", "task_termination_fence_persist_failed")
        return None

    def _request_candidate_task_stop(self, fence: CandidateExecutionFence) -> None:
        """原 Run 已终结且没有本地 future 时，异步干预精确执行。"""
        reason = self._candidate_stop_authority(fence)
        if reason is not None:
            check = CandidateExecutionCheck(
                "running" if reason == "task_run_still_active" else "unknown", reason,
            )
            diagnostic_exhausted, retry_at = self._schedule_candidate_recovery_retry(fence, reason)
            self._observe_candidate_recovery(
                fence, check, diagnostic_exhausted=diagnostic_exhausted, retry_at=retry_at,
            )
            self._log_candidate_recovery_deferred(fence, reason)
            return
        assert fence.container_id is not None
        assert fence.exec_id is not None
        assert fence.control_path is not None
        futures = getattr(self, "_candidate_stop_futures", None)
        if futures is None:
            futures = self._candidate_stop_futures = {}
        try:
            # 管理器先核验仍是原 v2 活执行，回调再持久预留额度；预检失败不消耗预算。
            futures[fence.capture_id] = self.cleanup_executor.submit(
                self.container_manager.terminate_candidate_execution,
                docker_daemon_id=fence.docker_daemon_id,
                container_id=fence.container_id,
                container_name=fence.container_name,
                project_id=fence.project_id,
                exec_id=fence.exec_id,
                control_path=fence.control_path,
                reserve_attempt=lambda: self._reserve_candidate_stop(fence),
            )
        except RuntimeError:
            check = CandidateExecutionCheck("unknown", "task_termination_control_unavailable")
            diagnostic_exhausted, retry_at = self._schedule_candidate_recovery_retry(fence, check.reason)
            self._observe_candidate_recovery(
                fence, check, diagnostic_exhausted=diagnostic_exhausted, retry_at=retry_at,
            )
            self._log_candidate_recovery_deferred(fence, check.reason)
            return
        self._observe_candidate_recovery(
            fence, CandidateExecutionCheck("running", "task_termination_pending"),
        )

    def _record_candidate_recovery(
        self,
        fence: CandidateExecutionFence,
        check: CandidateExecutionCheck,
        *,
        container_spool_recovered: bool,
    ) -> None:
        try:
            ready = fence.mark_recovered(
                status=check.status,
                reason=check.reason,
                container_spool_recovered=container_spool_recovered,
                exit_code=check.exit_code,
            )
            self.candidate_capture_outbox.replace_fence(fence, ready)
        except CandidateCaptureOutboxError:
            self._log_candidate_recovery_deferred(fence, "recovery_fence_persist_failed")
            return
        self._finalize_candidate_recovery_group(ready)

    def _finalize_candidate_recovery_group(self, fence: CandidateExecutionFence) -> None:
        try:
            siblings = [
                item for item in self.candidate_capture_outbox.fences()
                if item.project_id == fence.project_id and item.task_run_id == fence.task_run_id
            ]
        except CandidateCaptureOutboxError:
            self._log_candidate_recovery_deferred(fence, "recovery_fence_read_failed")
            return
        if not siblings or any(item.recovery_status is None for item in siblings):
            return
        # 所有同 Run 的物理执行与 spool 都已持久化解决，才允许释放原代次 claim。
        if not self._persist_candidate_recovery_terminal(siblings):
            return
        for item in siblings:
            if not self._release_candidate_fence(item):
                return
            if not item.execution_bound:
                self._cleanup_unbound_candidate_spool(item)
            elif item.recovery_status == "stopped":
                self._cleanup_stopped_candidate_resources(item)

    def _persist_candidate_recovery_terminal(
        self,
        fences: list[CandidateExecutionFence],
    ) -> bool:
        fence = fences[0]
        if any(
            item.recovery_status == "stopped"
            and item.start_attempted is not False
            and item.terminal_proof_version != 2
            for item in fences
        ):
            self._log_candidate_recovery_deferred(fence, "legacy_terminal_proof_unconfirmed")
            return False
        gone = next((item for item in fences if item.recovery_status == "gone"), None)
        resolved = gone or fence
        assert resolved.recovery_status in {"stopped", "gone"}
        assert resolved.recovery_reason is not None
        check = CandidateExecutionCheck(
            resolved.recovery_status, resolved.recovery_reason,
            resolved.recovery_exit_code,
        )
        process_started = any(
            item.terminal_confirmed or item.recovery_reason in {
                "exec_terminal_descendants_drained",
                "exec_missing_descendants_drained",
                "recorded_terminal_confirmed",
            }
            for item in fences
        )
        try:
            response = self.client.list_task_runs(fence.project_id)
        except requests.RequestException:
            self._log_candidate_recovery_deferred(fence, "task_run_read_unavailable")
            return False
        if response.status_code == 404:
            return True
        if not response.ok or not isinstance(response.data, list):
            self._log_candidate_recovery_deferred(fence, "task_run_read_unavailable")
            return False
        task_run = next(
            (
                item
                for item in response.data
                if isinstance(item, dict) and item.get("id") == fence.task_run_id
            ),
            None,
        )
        if task_run is None:
            LOG.info(
                "candidate recovery task record gone project=%s task_run=%s",
                fence.project_id,
                fence.task_run_id,
            )
            return True
        generation = task_run.get("execution_generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            self._log_candidate_recovery_deferred(fence, "task_run_generation_invalid")
            return False
        context = task_run.get("runtime_context")
        context = context if isinstance(context, dict) else {}
        prior_lifecycle = context.get("execution_lifecycle")
        prior_lifecycle = prior_lifecycle if isinstance(prior_lifecycle, dict) else {}
        if prior_lifecycle.get("generation") != generation:
            self._log_candidate_recovery_deferred(fence, "task_run_lifecycle_generation_mismatch")
            return False
        existing_ids = context.get("execution_container_ids", [])
        if not isinstance(existing_ids, list) or any(
            not isinstance(container_id, str) or not container_id
            for container_id in existing_ids
        ):
            self._log_candidate_recovery_deferred(fence, "task_run_execution_identity_invalid")
            return False
        recovered_ids = {
            item.container_id for item in fences if item.container_id is not None
        }
        prior_recovery = context.get("candidate_recovery")
        if (
            not set(existing_ids).issubset(recovered_ids)
            and not (
                prior_lifecycle.get("termination_confirmed") is True
                and isinstance(prior_recovery, dict)
                and type(prior_recovery.get("physical_proof_version")) is int
                and prior_recovery["physical_proof_version"] == 2
            )
        ):
            # 旧 PGID-only ACK 无法证明已释放子 fence 的独立后代也已停止。
            self._log_candidate_recovery_deferred(fence, "task_run_execution_identity_unresolved")
            return False
        all_ids = list(dict.fromkeys([*existing_ids, *sorted(recovered_ids)]))
        process_started = (
            True if process_started or prior_lifecycle.get("process_started") is True
            else False if all(item.start_attempted is False for item in fences)
            else None
        )
        lifecycle = {
            "generation": generation,
            "process_started": process_started is True,
            "termination_confirmed": True,
            "cleanup_confirmed": True,
        }
        recovery_context: dict[str, object] = {
            "status": check.status,
            "reason": check.reason,
            "recovered_at": _utcnow(),
            "container_spool_recovered": all(
                item.container_spool_recovered is True for item in fences
            ),
            "physical_proof_version": 2,
        }
        if (
            prior_lifecycle.get("termination_confirmed") is True
            and isinstance(prior_recovery, dict)
            and prior_recovery.get("physical_proof_version") == 2
        ):
            # 首轮 v2 ACK 成功后只释放部分 fence，重放时保留完整恢复审计。
            recovery_context = prior_recovery
        runtime_context: dict[str, object] = {
            "execution_lifecycle": lifecycle,
            "candidate_recovery": recovery_context,
        }
        if all_ids:
            runtime_context["execution_container_ids"] = all_ids
        current_status = task_run.get("status")
        terminal = current_status in _TERMINAL_TASK_STATUSES
        task_type = task_run.get("task_type")
        error_type, error_message = _candidate_recovery_failure(
            check,
            process_started=process_started,
        )
        update_kwargs: dict[str, object] = {"runtime_context": runtime_context}
        if not terminal and task_type != "report":
            update_kwargs.update(
                {
                    "status": "failed",
                    "exit_code": check.exit_code,
                    "error_type": error_type,
                    "error_message": error_message,
                }
            )
        elif terminal and task_type != "report":
            update_kwargs["status"] = current_status
        try:
            updated = self.client.update_task_run(
                fence.project_id,
                fence.task_run_id,
                **update_kwargs,
            )
        except requests.RequestException:
            self._log_candidate_recovery_deferred(fence, "task_run_finish_write_unavailable")
            return False
        if updated.status_code == 404:
            self._clear_candidate_recovery_log(fence.capture_id)
            return True
        if updated.ok:
            if task_type == "report":
                return self._persist_report_candidate_recovery(
                    fence,
                    check,
                    task_run,
                    error_type=error_type,
                    error_message=error_message,
                )
            self._clear_candidate_recovery_log(fence.capture_id)
            return True
        self._log_candidate_recovery_deferred(
            fence,
            f"task_run_finish_rejected_{updated.status_code}",
        )
        return False

    def _persist_report_candidate_recovery(
        self,
        fence: CandidateExecutionFence,
        check: CandidateExecutionCheck,
        task_run: dict[str, object],
        *,
        error_type: TaskRunErrorType,
        error_message: str,
    ) -> bool:
        """经现有 report finish receipt 原子终结 TaskRun，再允许释放 candidate fence。"""
        outbox = getattr(self, "report_finish_outbox", None)
        if outbox is None:
            self._log_candidate_recovery_deferred(
                fence,
                "report_finish_outbox_unavailable",
            )
            return False

        def matching_entry():
            entries = outbox.entries()
            matches = [
                entry
                for entry in entries
                if entry.project_id == fence.project_id
                and entry.task_run_id == fence.task_run_id
            ]
            if len(matches) > 1:
                raise OSError("duplicate report journal identity")
            return matches[0] if matches else None

        try:
            entry = matching_entry()
        except OSError:
            self._log_candidate_recovery_deferred(
                fence,
                "report_finish_outbox_invalid",
            )
            return False

        authenticated_owner = getattr(self, "dispatcher_id", None)
        if not isinstance(authenticated_owner, str) or not authenticated_owner:
            self._log_candidate_recovery_deferred(
                fence,
                "report_finish_outbox_owner_unavailable",
            )
            return False
        if entry is not None and entry.authenticated_dispatcher_id != authenticated_owner:
            self._log_candidate_recovery_deferred(
                fence,
                "report_finish_outbox_owner_mismatch",
            )
            return False

        try:
            if isinstance(entry, PendingReportOutput):
                self._advance_pending_report_output(entry)
                entry = matching_entry()
                if isinstance(entry, PendingReportOutput):
                    self._log_candidate_recovery_deferred(
                        fence,
                        "report_output_replay_pending",
                    )
                    return False
            if isinstance(entry, PendingReportExecution):
                finish = failed_finish_for_recovery(
                    project_id=entry.project_id,
                    authenticated_dispatcher_id=entry.authenticated_dispatcher_id,
                    worker=entry.worker,
                    claim_sha256=entry.claim_sha256,
                    generation_token=entry.generation_token,
                    task_run_id=entry.task_run_id,
                    exit_code=check.exit_code,
                    error_type=error_type,
                    error_message=error_message,
                )
                outbox.replace(entry, finish)
                entry = finish
            elif entry is None:
                context = task_run.get("runtime_context")
                context = context if isinstance(context, dict) else {}
                claim = context.get("report_claim")
                dispatcher_owner = context.get("dispatcher_id")
                worker = task_run.get("worker_name")
                claim = claim if isinstance(claim, dict) else {}
                claim_sha256 = claim.get("claim_sha256")
                generation_token = claim.get("generation_token")
                if (
                    dispatcher_owner != authenticated_owner
                    or not isinstance(worker, str)
                    or not worker
                    or not isinstance(claim_sha256, str)
                    or not isinstance(generation_token, str)
                ):
                    self._log_candidate_recovery_deferred(
                        fence,
                        "report_finish_identity_unavailable",
                    )
                    return False
                finish = failed_finish_for_recovery(
                    project_id=fence.project_id,
                    authenticated_dispatcher_id=authenticated_owner,
                    worker=worker,
                    claim_sha256=claim_sha256,
                    generation_token=generation_token,
                    task_run_id=fence.task_run_id,
                    exit_code=check.exit_code,
                    error_type=error_type,
                    error_message=error_message,
                )
                outbox.persist(finish)
                entry = finish
        except requests.RequestException:
            self._log_candidate_recovery_deferred(
                fence,
                "report_finish_write_unavailable",
            )
            return False
        except (OSError, RuntimeError, ValueError, ReportFinishOutboxConflict):
            self._log_candidate_recovery_deferred(
                fence,
                "report_finish_outbox_transition_failed",
            )
            return False

        if not isinstance(entry, PendingReportFinish):
            self._log_candidate_recovery_deferred(
                fence,
                "report_finish_journal_incomplete",
            )
            return False
        try:
            accepted = self._replay_pending_report_finish(
                entry,
                remove_when_accepted=False,
            )
        except requests.RequestException:
            self._log_candidate_recovery_deferred(
                fence,
                "report_finish_write_unavailable",
            )
            return False
        except RuntimeError:
            self._log_candidate_recovery_deferred(
                fence,
                "report_finish_outbox_transition_failed",
            )
            return False
        if not accepted:
            self._log_candidate_recovery_deferred(
                fence,
                "report_finish_receipt_pending",
            )
            return False
        self._clear_candidate_recovery_log(fence.capture_id)
        return True

    def _release_candidate_fence(self, fence: CandidateExecutionFence) -> bool:
        try:
            released = self.candidate_capture_outbox.remove_fence(fence)
        except CandidateCaptureOutboxError:
            self._log_candidate_recovery_deferred(fence, "fence_release_failed")
            return False
        if not released:
            self._log_candidate_recovery_deferred(fence, "fence_release_lost")
            return False
        self._clear_candidate_recovery_log(fence.capture_id)
        return True

    def _cleanup_stopped_candidate_resources(self, fence: CandidateExecutionFence) -> None:
        assert fence.container_id is not None
        assert fence.control_path is not None
        try:
            self.container_manager.remove_file(
                fence.container_id,
                fence.spool_path,
                allowed_root="/tmp/cairn-prompts",
            )
        except (OSError, RuntimeError) as exc:
            # frame 已 durable 且 TaskRun 终局已确认；残留临时文件不影响 exact replay。
            LOG.warning(
                "candidate recovered spool cleanup failed project=%s task_run=%s error=%s",
                fence.project_id,
                fence.task_run_id,
                type(exc).__name__,
            )
        try:
            self.container_manager.cleanup_candidate_execution_control(
                container_id=fence.container_id,
                container_name=fence.container_name,
                project_id=fence.project_id,
                control_path=fence.control_path,
            )
        except (OSError, RuntimeError) as exc:
            LOG.warning(
                "candidate recovered control cleanup failed project=%s task_run=%s error=%s",
                fence.project_id,
                fence.task_run_id,
                type(exc).__name__,
            )

    def _cleanup_unbound_candidate_spool(self, fence: CandidateExecutionFence) -> None:
        try:
            if (
                fence.docker_daemon_id is None
                or self.container_manager.docker_daemon_id() != fence.docker_daemon_id
            ):
                return
            self.container_manager.remove_file(
                fence.container_name,
                fence.spool_path,
                allowed_root="/tmp/cairn-prompts",
            )
        except (OSError, RuntimeError):
            # unbound 已证明未执行；无法访问原容器时只留下无写入者的随机临时文件。
            return

    def _observe_candidate_recovery(
        self,
        fence: CandidateExecutionFence,
        check: CandidateExecutionCheck,
        *,
        diagnostic_exhausted: bool = False,
        retry_at: str | None = None,
    ) -> None:
        observations = getattr(self, "_candidate_recovery_observations", None)
        if observations is None:
            observations = self._candidate_recovery_observations = {}
        # 每次物理复查都是新观测；仅 heartbeat 重传时才保留原时间。
        observation: dict[str, object] = {
            "project_id": fence.project_id,
            "task_run_id": fence.task_run_id,
            "status": check.status,
            "reason": check.reason,
            "observed_at": _utcnow(),
        }
        if check.status == "unknown":
            observation["diagnostic_exhausted"] = diagnostic_exhausted
            observation["retry_at"] = retry_at
        elif retry_at is not None:
            observation["retry_at"] = retry_at
        observations[fence.capture_id] = observation

    def _candidate_recovery_due(self, fence: CandidateExecutionFence) -> bool:
        state = getattr(self, "_candidate_recovery_retry_states", {}).get(
            fence.capture_id
        )
        return not isinstance(state, tuple) or time.monotonic() >= state[1]

    def _schedule_candidate_recovery_retry(
        self,
        fence: CandidateExecutionFence,
        reason: str,
    ) -> tuple[bool, str]:
        states = getattr(self, "_candidate_recovery_retry_states", None)
        if states is None:
            states = self._candidate_recovery_retry_states = {}
        now = time.monotonic()
        prior = states.get(fence.capture_id)
        if isinstance(prior, tuple) and prior[0] == reason:
            _prior_reason, _next_check, prior_delay, started_at = prior
            delay = min(prior_delay * 2, _RECOVERY_RETRY_MAX_SECONDS)
        else:
            delay = _RECOVERY_RETRY_INITIAL_SECONDS
            started_at = now
        states[fence.capture_id] = (reason, now + delay, delay, started_at)
        return (
            now - started_at >= _RECOVERY_DIAGNOSTIC_WINDOW_SECONDS,
            _utc_from_timestamp(time.time() + delay),
        )

    def _clear_candidate_recovery_retry(self, capture_id: str) -> None:
        getattr(self, "_candidate_recovery_retry_states", {}).pop(capture_id, None)

    def _log_candidate_recovery_deferred(
        self,
        fence: CandidateExecutionFence,
        reason: str,
    ) -> None:
        states = getattr(self, "_candidate_recovery_log_states", None)
        if states is None:
            states = self._candidate_recovery_log_states = {}
        now = time.monotonic()
        prior = states.get(fence.capture_id)
        changed = not isinstance(prior, tuple) or prior[0] != reason
        if changed:
            delay = _RECOVERY_LOG_INITIAL_SECONDS
        else:
            _prior_reason, next_log, prior_delay = prior
            if now < next_log:
                return
            delay = min(prior_delay * 2, _RECOVERY_LOG_MAX_SECONDS)
        states[fence.capture_id] = (reason, now + delay, delay)
        LOG.warning(
            "candidate recovery deferred project=%s task_run=%s code=%s retry_after_seconds=%s",
            fence.project_id,
            fence.task_run_id,
            reason,
            int(delay),
        )

    def _clear_candidate_recovery_log(self, capture_id: str) -> None:
        getattr(self, "_candidate_recovery_log_states", {}).pop(capture_id, None)

    def _set_candidate_fence_snapshot(
        self,
        fences: list[CandidateExecutionFence],
        *,
        invalid: bool,
        blocked_project_ids: set[str] | None = None,
    ) -> None:
        self._candidate_fence_invalid = invalid
        self._candidate_fences = tuple(fences)
        self._candidate_fenced_projects = {
            fence.project_id for fence in fences
        } | (blocked_project_ids or set())
        self._candidate_fenced_task_runs = {
            (fence.project_id, fence.task_run_id) for fence in fences
        }
        self._candidate_fenced_containers = {
            fence.container_name for fence in fences
        }
        active_capture_ids = {fence.capture_id for fence in fences}
        for mapping_name in (
            "_candidate_recovery_observations",
            "_candidate_recovery_log_states",
            "_candidate_recovery_retry_states",
        ):
            mapping = getattr(self, mapping_name, {})
            for capture_id in list(mapping):
                if capture_id not in active_capture_ids:
                    mapping.pop(capture_id, None)

    def _candidate_project_blocked(self, project_id: str) -> bool:
        return bool(
            getattr(self, "_candidate_fence_invalid", False)
            or project_id in getattr(self, "_candidate_fenced_projects", set())
        )

    def _candidate_container_blocked(self, container_name: str) -> bool:
        return bool(
            getattr(self, "_candidate_fence_invalid", False)
            or container_name in getattr(self, "_candidate_fenced_containers", set())
        )
