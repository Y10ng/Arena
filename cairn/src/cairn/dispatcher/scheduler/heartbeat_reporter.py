"""Dispatcher heartbeat 回显。"""

from __future__ import annotations

import socket

from .common import *


class HeartbeatReporterMixin:
    """Dispatcher heartbeat 回显。"""

    def _dispatcher_worker_cooldowns(self, now: float) -> list[dict[str, object]]:
        worker_types = self._worker_type_by_name()
        items: list[dict[str, object]] = []
        expired: list[str] = []
        for worker_name, (retry_until, error_type) in sorted(getattr(self, "worker_cooldown_until", {}).items()):
            if retry_until <= now:
                # 退避窗口在生成 heartbeat 时惰性清理，避免主循环额外维护定时器。
                expired.append(worker_name)
                continue
            items.append(
                {
                    "worker_name": worker_name,
                    "worker_type": worker_types.get(worker_name),
                    "error_type": error_type,
                    "retry_after_seconds": max(0, int(round(retry_until - now))),
                    "retry_until_at": _utc_from_timestamp(retry_until),
                }
            )
        for worker_name in expired:
            self.worker_cooldown_until.pop(worker_name, None)
        return items

    def _dispatcher_worker_rejections(self, now: float) -> list[dict[str, object]]:
        worker_types = self._worker_type_by_name()
        items: list[dict[str, object]] = []
        expired: list[tuple[str, str, str]] = []
        for key, (retry_until, error_type) in sorted(getattr(self, "worker_rejected_until", {}).items()):
            project_id, task_type, worker_name = key
            if retry_until <= now:
                # rejection 是 project/task/worker 维度，过期后只影响这个精确 key。
                expired.append(key)
                continue
            items.append(
                {
                    "project_id": project_id,
                    "task_type": task_type,
                    "worker_name": worker_name,
                    "worker_type": worker_types.get(worker_name),
                    "error_type": error_type,
                    "retry_after_seconds": max(0, int(round(retry_until - now))),
                    "retry_until_at": _utc_from_timestamp(retry_until),
                }
            )
        for key in expired:
            self.worker_rejected_until.pop(key, None)
        return items

    def _dispatcher_worker_pool(self) -> list[dict[str, object]]:
        running_counts = self._worker_counts()
        items: list[dict[str, object]] = []
        for worker in self.config.workers:
            features = self._worker_features_runtime_context(worker)
            items.append(
                {
                    "worker_id": worker.id,
                    "worker_name": worker.name,
                    "engine": worker.engine,
                    "worker_type": worker.type,
                    "model_id": worker.model_id,
                    "task_types": list(worker.task_types),
                    "priority": worker.priority,
                    "concurrency": worker.concurrency,
                    "max_running": worker.max_running,
                    "enabled": worker.enabled,
                    "running_count": running_counts.get(worker.name, 0),
                    "supports_resume": features.get("supports_resume"),
                    "supports_json_mode": features.get("supports_json_mode"),
                    "max_context": features.get("max_context"),
                    # 只上报 env key 供控制台排查配置漂移，真实 secret 不进入 Server。
                    "env_keys": sorted(worker.env),
                }
            )
        return items

    def _dispatcher_runtime_summary(self) -> dict[str, object]:
        candidate_recovery = sorted(
            getattr(self, "_candidate_recovery_observations", {}).values(),
            key=lambda item: (
                item.get("observed_at", "") if isinstance(item, dict) else ""
            ),
            reverse=True,
        )[:256]
        return {
            "runtime_capabilities": ["project_pause_v1", "project_runtime_v1"],
            "project_decisions": list(getattr(self, "_project_decisions", {}).values()),
            "candidate_recovery": candidate_recovery,
            "startup_check": getattr(self, "_startup_check", {"status": "unknown"}),
            "max_workers": self.config.runtime.max_workers,
            "hard_cap": self.bootstrap.hard_cap,
            "verification_capacity": min(1, max(0, self.bootstrap.hard_cap - self.config.runtime.max_workers)),
            "max_running_projects": self.config.runtime.max_running_projects,
            "max_project_workers": self.config.runtime.max_project_workers,
            "interval": self.config.runtime.interval,
            "healthcheck_timeout": self.config.runtime.healthcheck_timeout,
            "prompt_group": self.config.runtime.prompt_group,
            "tasks": {
                "bootstrap": self.config.tasks.bootstrap.model_dump(mode="json"),
                "reason": self.config.tasks.reason.model_dump(mode="json"),
                "explore": self.config.tasks.explore.model_dump(mode="json"),
            },
        }

    def _dispatcher_container_summary(self) -> dict[str, object]:
        return {
            "image": self.config.container.image,
            "allowed_images": list(self.config.container.allowed_images),
            "network_mode": self.config.container.network_mode,
            "dns": list(self.config.container.dns) or "docker-default",
            "completed_action": self.config.container.completed_action,
            "execution_profile": self.config.container.execution_profile,
            "cap_add": list(self.config.container.cap_add),
        }

    def _fail_dispatcher_identity(self, code: str) -> None:
        # 身份失败不可被后续监控成功解除；旧任务保留原归属收尾，重启才可重新握手。
        self._identity_error = code
        raise RuntimeError(code)

    def _record_dispatcher_heartbeat(
        self,
        *,
        status: str,
        loop_started_at: str,
        loop_started: float,
        summaries: list[ProjectSummary],
        error_message: str | None = None,
        loop_completed: bool = True,
    ) -> None:
        if getattr(self, "_identity_error", None):
            raise RuntimeError(self._identity_error)
        loop_finished_at = _utcnow() if loop_completed else None
        try:
            now = time.time()
            config_error = "config_invalid" if getattr(self, "_config_error_message", None) else None
            # heartbeat 汇总当前 loop、配置、容器和退避窗口，是监控页唯一的 dispatcher 运行态入口。
            response = self.client.dispatcher_heartbeat(
                dispatcher_id=self.dispatcher_id or self.config.dispatcher_id,
                status=status,
                started_at=getattr(self, "started_at", None),
                config_hash=self._config_hash(),
                host=socket.gethostname(),
                config_path=str(self._config_path),
                config_mtime=int(self._config_mtime_ns // 1_000_000_000) if self._config_mtime_ns else None,
                config_sha256=self._config_sha256,
                runtime=self._dispatcher_runtime_summary(),
                container=self._dispatcher_container_summary(),
                server_reachable_path=self.config.server_reachable_path,
                loop_started_at=loop_started_at,
                loop_finished_at=loop_finished_at,
                loop_duration_ms=max(0, int((time.perf_counter() - loop_started) * 1000)) if loop_completed else None,
                running_task_count=len(self.futures),
                active_project_count=sum(1 for summary in summaries if summary.status == "active"),
                worker_cooldowns=self._dispatcher_worker_cooldowns(now),
                worker_rejections=self._dispatcher_worker_rejections(now),
                worker_pool=self._dispatcher_worker_pool(),
                container_images=sorted(self._allowed_container_images()),
                cap_baseline=list(self.config.container.cap_add),
                declared_inventory=self.config.declared_inventory(),
                error_message=error_message or config_error,
            )
        except Exception as exc:
            if self.dispatcher_id is None:
                self._fail_dispatcher_identity("dispatcher_identity_handshake_failed")
            LOG.debug("dispatcher heartbeat update raised dispatcher=%s status=%s error=%s", self.dispatcher_id, status, exc)
            return
        if response.ok:
            authoritative_id = response.data.get("dispatcher_id") if isinstance(response.data, dict) else None
            if not isinstance(authoritative_id, str) or not authoritative_id.strip():
                self._fail_dispatcher_identity("dispatcher_identity_handshake_failed")
            if self.dispatcher_id is None:
                self.dispatcher_id = authoritative_id
            elif authoritative_id != self.dispatcher_id:
                self._fail_dispatcher_identity("dispatcher_identity_changed")
            return
        if self.dispatcher_id is None:
            self._fail_dispatcher_identity("dispatcher_identity_handshake_failed")
        if response.status_code in {401, 403}:
            self._fail_dispatcher_identity("dispatcher_identity_authentication_failed")
        LOG.debug(
            "dispatcher heartbeat update failed dispatcher=%s status=%s response_status=%s body=%s",
            self.dispatcher_id,
            status,
            response.status_code,
            response.text,
        )
