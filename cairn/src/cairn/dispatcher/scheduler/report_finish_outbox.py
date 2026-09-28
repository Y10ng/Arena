"""跨重启重放 strict report finish outbox。"""

from __future__ import annotations

from cairn.dispatcher.runtime.browser_sessions import notify_browser_task
from cairn.dispatcher.tasks.report_finish_outbox import (
    PendingReportExecution,
    PendingReportFinish,
    PendingReportOutput,
    ReportFinishOutboxConflict,
    can_fallback_report_finish,
    failed_finish_fallback,
    ready_finish_for_output,
    recover_report_artifact_id,
    validate_finish_receipt,
    validate_report_artifact_receipt,
)
from cairn.dispatcher.tasks.write_outcomes import classify_write_response

from .common import *


class ReportFinishOutboxMixin:
    """在新 claim 和 stale reconcile 前推进 durable report journal。"""

    def _report_journal_task_active(self, project_id: str) -> bool:
        return any(
            task.project_id == project_id and task.task_type == "report"
            for task in getattr(self, "futures", {}).values()
        )

    def _advance_pending_report_output(
        self,
        entry: PendingReportOutput,
    ) -> None:
        """先 reconcile 可能已提交的 Artifact，再重放 exact UTF-8 bytes。"""
        outbox = self.report_finish_outbox
        listed = self.client.list_artifacts(
            entry.project_id,
            task_run_id=entry.task_run_id,
        )
        if not listed.ok or not isinstance(listed.data, list):
            LOG.warning(
                "report output reconcile pending project=%s task_run=%s status=%s",
                entry.project_id,
                entry.task_run_id,
                listed.status_code,
            )
            return
        artifact_id = recover_report_artifact_id(listed, entry)
        if artifact_id is None:
            response = validate_report_artifact_receipt(
                entry.replay(self.client),
                entry,
            )
            disposition = classify_write_response(response)
            if not disposition.accepted:
                LOG.warning(
                    "report output replay blocked project=%s task_run=%s response_status=%s code=%s disposition=%s",
                    entry.project_id,
                    entry.task_run_id,
                    response.status_code,
                    disposition.safe_code,
                    disposition.disposition,
                )
                return
            assert isinstance(response.data, dict)
            artifact_id = response.data["id"]
            assert isinstance(artifact_id, str)
        finish = ready_finish_for_output(entry, artifact_id=artifact_id)
        try:
            # Artifact ACK 后也不留 output→finish 空窗；下一轮再重放 strict finish。
            outbox.replace(entry, finish)
        except ReportFinishOutboxConflict:
            return
        except OSError:
            LOG.error(
                "report output transition failed project=%s task_run=%s code=report_finish_outbox_transition_failed",
                entry.project_id,
                entry.task_run_id,
            )
            raise RuntimeError("report_finish_outbox_transition_failed") from None

    def _replay_pending_report_finish(
        self,
        entry: PendingReportFinish,
        *,
        remove_when_accepted: bool,
    ) -> bool:
        """重放唯一 finish 请求；candidate fence 可暂留 journal 作为 ACK 见证。"""
        outbox = self.report_finish_outbox
        response = validate_finish_receipt(entry.replay(self.client), entry)
        disposition = classify_write_response(response)
        if disposition.retry_same_write:
            LOG.warning(
                "report finish outbox pending project=%s task_run=%s status=%s response_status=%s",
                entry.project_id,
                entry.task_run_id,
                entry.status,
                response.status_code,
            )
            return False
        if entry.status == "ready" and can_fallback_report_finish(
            disposition.safe_code
        ):
            fallback = failed_finish_fallback(
                entry,
                safe_code=disposition.safe_code,
            )
            try:
                # 先 durable 地替换 ready，再由下一轮重放 failed；中间不存在空窗。
                outbox.replace(entry, fallback)
            except ReportFinishOutboxConflict:
                return False
            except OSError:
                LOG.error(
                    "report finish outbox transition failed project=%s task_run=%s code=report_finish_outbox_transition_failed",
                    entry.project_id,
                    entry.task_run_id,
                )
                raise RuntimeError(
                    "report_finish_outbox_transition_failed"
                ) from None
            return False
        if not disposition.accepted:
            # 未获 strict receipt 时保留唯一 durable 事实，阻止凭据恢复后重新运行模型。
            LOG.warning(
                "report finish outbox blocked project=%s task_run=%s status=%s response_status=%s code=%s disposition=%s",
                entry.project_id,
                entry.task_run_id,
                entry.status,
                response.status_code,
                disposition.safe_code,
                disposition.disposition,
            )
            return False
        if not remove_when_accepted:
            return True
        try:
            outbox.remove(entry)
        except ReportFinishOutboxConflict:
            # Worker 可能已用同一 TaskRun 的 strict failed fallback 替换旧 ready entry。
            LOG.info(
                "report finish outbox entry replaced during replay project=%s task_run=%s",
                entry.project_id,
                entry.task_run_id,
            )
            return False
        except OSError:
            LOG.error(
                "report finish outbox cleanup failed project=%s task_run=%s code=report_finish_outbox_cleanup_failed",
                entry.project_id,
                entry.task_run_id,
            )
            raise RuntimeError("report_finish_outbox_cleanup_failed") from None
        notify_browser_task(
            getattr(self, "container_manager", None),
            entry.project_id,
            entry.task_run_id,
            terminal_confirmed=True,
        )
        LOG.info(
            "report finish outbox acknowledged project=%s task_run=%s status=%s",
            entry.project_id,
            entry.task_run_id,
            entry.status,
        )
        return True

    def _flush_pending_report_finishes(self) -> None:
        outbox = self.report_finish_outbox
        try:
            entries = outbox.entries()
        except OSError:
            LOG.error("report finish outbox load failed code=report_finish_outbox_invalid")
            # 损坏状态必须阻断 stale reconcile 和新 claim，且异常不可回显原始文件内容。
            raise RuntimeError("report_finish_outbox_invalid") from None
        if not entries:
            return
        authenticated_owner = getattr(self, "dispatcher_id", None)
        if not isinstance(authenticated_owner, str) or not authenticated_owner:
            LOG.error("report finish outbox owner unavailable code=report_finish_outbox_owner_unavailable")
            raise RuntimeError("report_finish_outbox_owner_unavailable")
        cursor = getattr(self, "_report_finish_outbox_cursor", 0) % len(entries)
        ordered = entries[cursor:] + entries[:cursor]
        batch = ordered[:8]
        # retryable 前缀不能永久饿死后续 entry；每轮都从下一位置继续。
        self._report_finish_outbox_cursor = (cursor + len(batch)) % len(entries)
        candidate_fences_invalid = bool(
            getattr(self, "_candidate_fence_invalid", False)
        )
        candidate_fenced_task_runs = getattr(
            self,
            "_candidate_fenced_task_runs",
            set(),
        )
        for entry in batch:
            if entry.authenticated_dispatcher_id != authenticated_owner:
                LOG.error(
                    "report finish outbox owner mismatch project=%s task_run=%s code=report_finish_outbox_owner_mismatch",
                    entry.project_id,
                    entry.task_run_id,
                )
                # 跨 owner journal 不能由当前认证主体重放；立即阻断整轮，避免掩盖部署身份漂移。
                raise RuntimeError("report_finish_outbox_owner_mismatch")
            journal_key = (entry.project_id, entry.task_run_id)
            candidate_fenced = (
                candidate_fences_invalid
                or journal_key in candidate_fenced_task_runs
            )
            if isinstance(entry, PendingReportExecution):
                if not self._report_journal_task_active(entry.project_id):
                    # execution journal 只证明模型可能已启动；物理终态必须由 candidate recovery 独立确认。
                    LOG.info(
                        "report execution awaits physical recovery project=%s task_run=%s candidate_fenced=%s",
                        entry.project_id,
                        entry.task_run_id,
                        candidate_fenced,
                    )
                continue
            if isinstance(entry, PendingReportOutput):
                if not self._report_journal_task_active(entry.project_id):
                    self._advance_pending_report_output(entry)
                continue
            assert isinstance(entry, PendingReportFinish)
            self._replay_pending_report_finish(
                entry,
                # strict receipt 必须先于 candidate fence；保留 journal 覆盖 ACK→fence-delete 崩溃窗。
                remove_when_accepted=not candidate_fenced,
            )
