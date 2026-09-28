"""只记录真实分支的有界调度观测，不重新计算候选或刷新旧决定。"""
from __future__ import annotations

from datetime import datetime, timezone
import time

from cairn.dispatcher.protocol.client import ApiResult


def claim_failure_code(response: ApiResult) -> str:
    """只透传 Server 的有限重试裁决，不把错误正文写入心跳。"""
    if response.status_code == 409:
        detail = response.data.get("detail") if isinstance(response.data, dict) else None
        if detail == "reason_execution_pending":
            return "cleanup_pending"
        if isinstance(detail, str) and detail in {
            "execution_retry_blocked", "execution_retry_exhausted", "execution_retry_cooldown",
            "execution_continuation_unavailable",
            "project_authorization_scope_upgrade_required", "project_authorization_scope_changed",
            "project_authorization_expired", "project_reauthorization_required",
        }:
            return detail
        return "claim_conflict"
    return "claim_denied" if response.status_code == 403 else "claim_failed"


class DispatchDecisionsMixin:
    def _begin_project_decisions(self, summaries) -> None:
        self._project_decisions = {}
        self._project_generations = {
            item.id: getattr(item, "execution_generation", 0) for item in summaries
        }

    def _observe_decision(
        self, project_id: str, code: str, *, task_type: str | None = None,
        worker_id: str | None = None, retry_at: str | None = None,
    ) -> None:
        decisions = getattr(self, "_project_decisions", None)
        if decisions is None:
            decisions = self._project_decisions = {}
        if project_id not in decisions and len(decisions) >= 256:
            return
        item = {
            "project_id": project_id,
            "execution_generation": getattr(self, "_project_generations", {}).get(project_id, 0),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "code": code,
        }
        for key, value in (("task_type", task_type), ("worker_id", worker_id), ("retry_at", retry_at)):
            if value is not None:
                item[key] = value
        decisions[project_id] = item

    def _observe_selection(self, project_id: str, task_type: str, selection) -> None:
        # 只投影选择器已有分类；不向 heartbeat 复制配置、异常或候选上下文。
        code, items = "no_candidates", []
        for name, candidate_code in (
            ("blocked_busy", "worker_capacity"),
            ("blocked_cooldown", "worker_cooldown"),
            ("blocked_rejected", "worker_rejected"),
            ("blocked_disabled", "worker_disabled"),
            ("blocked_task_type", "worker_task_type"),
        ):
            blocked = getattr(selection, name)
            if blocked:
                code, items = candidate_code, blocked
                break
        reasons = {item.get("reason") for item in items}
        if len(reasons) == 1 and reasons <= {"engine_unsupported", "model_disabled", "credential_missing", "binding_stale"}:
            code = next(iter(reasons))
        retries = [item["retry_after_seconds"] for item in items if isinstance(item.get("retry_after_seconds"), (int, float))]
        retry_at = None
        if retries:
            try:
                retry_at = datetime.fromtimestamp(time.time() + min(retries), timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                # 观测字段异常不能影响已完成的候选裁决，也不编造可重试时间。
                pass
        self._observe_decision(project_id, code, task_type=task_type, retry_at=retry_at)
