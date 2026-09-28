"""配置拉取、应用和启动健康检查。"""

from __future__ import annotations

from cairn.dispatcher.runtime.containers import container_instance_id
from cairn.dispatcher.runtime.startup_healthcheck import StartupHealthcheckResult
from cairn.dispatcher.tasks.candidate_capture_outbox import CandidateCaptureOutbox
from cairn.dispatcher.workers.base import (
    CANDIDATE_MAX_ARTIFACTS,
    CANDIDATE_MAX_BYTES,
    CANDIDATE_MAX_CANDIDATES,
    CANDIDATE_MAX_MANIFEST_BYTES,
    CANDIDATE_MAX_OUTCOMES_PER_CANDIDATE,
    CANDIDATE_MAX_SPOOL_BYTES,
    EXECUTION_CHECKPOINT_MAX_EVENT_BYTES,
    EXECUTION_CHECKPOINT_MAX_EVENTS,
    EXECUTION_CHECKPOINT_MAX_SPOOL_BYTES,
)

from .common import *


_CANDIDATE_RUNNER_CAPABILITY = "candidate-spool-v1"
_EXECUTION_CHECKPOINT_RUNNER_CAPABILITY = "execution-checkpoint-spool-v1"


class ConfigRuntimeMixin:
    """配置拉取、应用和启动健康检查。"""

    def _ensure_config_loaded(self, *, once: bool) -> None:
        while self.config is None:
            try:
                self._refresh_config(force=True)
                return
            except Exception as exc:
                if once:
                    raise
                # 长驻模式下配置文件可稍后生成/修复，因此启动阶段按 loop interval 重试。
                LOG.warning(
                    "dispatcher config load failed dispatcher=%s config_path=%s error=%s retry_in=%ss",
                    self.dispatcher_id,
                    self._config_path,
                    exc,
                    self._sleep_interval(),
                )
                time.sleep(self._sleep_interval())

    def _refresh_config(self, *, force: bool = False) -> bool:
        try:
            stat = self._config_path.stat()
            raw = self._config_path.read_bytes()
            config_sha = sha256(raw).hexdigest()
            # 仅 mtime 变化可能来自原子替换或权限调整，sha256 不变时无需重新应用配置。
            if (
                not force
                and self._config_mtime_ns == stat.st_mtime_ns
                and self._config_sha256 == config_sha
            ):
                self._config_mtime_ns = stat.st_mtime_ns
                return False
            if not force and self._config_sha256 == config_sha:
                self._config_mtime_ns = stat.st_mtime_ns
                return False
            config = DispatchConfig.load(self._config_path)
            self._apply_config(config, force=force, config_sha=config_sha)
        except Exception as exc:
            self._config_error_message = f"{type(exc).__name__}: {exc}"
            if force:
                raise
            # 热加载失败时继续使用上一份有效配置，避免一次坏编辑打断正在运行的任务。
            LOG.exception(
                "dispatcher config reload failed; keeping previous config dispatcher=%s config_path=%s",
                self.dispatcher_id,
                self._config_path,
            )
            return False
        self._config_mtime_ns = stat.st_mtime_ns
        self._config_sha256 = config_sha
        self._config_error_message = None
        LOG.info(
            "dispatcher config %s dispatcher=%s config_hash=%s workers=%s max_workers=%s",
            "loaded" if force or self._last_config_hash is None else "updated",
            self.dispatcher_id,
            self._last_config_hash,
            len(config.workers),
            config.runtime.max_workers,
        )
        return True

    def _apply_config(self, config: DispatchConfig, *, force: bool, config_sha: str | None = None) -> None:
        previous_config = self.config
        if config.runtime.max_workers > self.bootstrap.hard_cap:
            raise RuntimeError(
                f"runtime.max_workers={config.runtime.max_workers} exceeds dispatcher hard_cap={self.bootstrap.hard_cap}"
            )
        instance_id = container_instance_id(self.dispatcher_id, config.server) if self.dispatcher_id else None
        if self.container_manager is not None and self.config is not None:
            previous_instance_id = container_instance_id(self.dispatcher_id, self.config.server)
            if instance_id != previous_instance_id:
                # 在途任务与浏览器清理必须继续使用原归属，身份变更需用户显式重启。
                raise RuntimeError("dispatcher/server identity changed; restart the dispatcher explicitly")
        previous_interval = self.config.runtime.interval if self.config is not None else None
        if getattr(self, "client", None) is not None and self.client.base_url != config.server.rstrip("/"):
            # Server 地址变化后重建 HTTP session，避免连接池继续打到旧目标。
            self.client.close()
            self.client = CairnClient(config.server)
        self.config = config
        self._last_config_hash = config_sha or self._config_sha256 or self._hash_config(config)
        if self.dispatcher_id is not None and self.candidate_capture_outbox is None:
            owner_namespace = sha256(self.dispatcher_id.encode("utf-8")).hexdigest()
            self.candidate_capture_outbox = CandidateCaptureOutbox(
                self._candidate_outbox_parent
                / (
                    "dispatcher-candidate-capture-outbox-v1-"
                    f"{self._candidate_server_namespace}-{owner_namespace}"
                ),
                owner_id=self.dispatcher_id,
            )
        if self.container_manager is None:
            if instance_id is not None:
                self.container_manager = ContainerManager(config.container, instance_id=instance_id)
        else:
            self.container_manager.update_config(config.container)
        configure_browser_sessions = getattr(self.container_manager, "configure_browser_sessions", None)
        if callable(configure_browser_sessions):
            configure_browser_sessions(config.runtime.playwright_session_policy)
        if force or previous_interval != config.runtime.interval:
            self._settings_checked = False
        if previous_config is not None and previous_config != config:
            # 配置变化后旧 runner/image 能力证据不可复用，下一次派发前必须重新自检。
            self._startup_healthchecks_checked = False
            self._worker_capabilities = {}

    def _sleep_interval(self) -> int:
        if self.config is None:
            return 3
        return self.config.runtime.interval

    @staticmethod
    def _hash_config(config: DispatchConfig) -> str:
        raw = json.dumps(config.model_dump(), sort_keys=True).encode("utf-8")
        return sha256(raw).hexdigest()

    def _config_hash(self) -> str:
        if self._last_config_hash:
            return self._last_config_hash
        return self._hash_config(self.config)

    def _dispatcher_id(self) -> str:
        return self.dispatcher_id

    def _config_payload(self) -> dict[str, object]:
        assert self.config is not None
        return self.config.model_dump()

    def _validate_server_settings(self) -> None:
        settings = self.client.get_settings()
        interval = self.config.runtime.interval
        for name, value in (("intent_timeout", settings.intent_timeout), ("reason_timeout", settings.reason_timeout)):
            if value <= interval:
                # Server 租约必须大于 dispatcher loop interval，否则心跳可能天然超时。
                raise RuntimeError(
                    f"server {name}={value}s must be greater than dispatcher interval={interval}s"
                )
            if value < interval * 2:
                # 小于两倍 interval 仍可运行，但心跳抖动余量很小，提前在日志中暴露风险。
                LOG.warning(
                    "server %s is tight %s=%ss interval=%ss; heartbeat slack is only %ss",
                    name,
                    name,
                    value,
                    interval,
                    value - interval,
                )
                continue
            LOG.info(
                "server setting validated %s=%ss interval=%ss",
                name,
                value,
                interval,
            )

    def _run_startup_healthchecks(self, *, show_commands: bool) -> None:
        assert self.config is not None
        assert self.container_manager is not None
        results = run_startup_healthchecks(
            self.config,
            self.container_manager,
            show_commands=show_commands,
        )
        candidate_capability = self.client.get_candidate_capabilities(
            force_refresh=True
        )
        if not self._server_candidate_capability_usable(candidate_capability):
            raise RuntimeError(
                "candidate capture requires a compatible mandatory Server capability"
            )
        required_workers = {
            worker.name
            for worker in self.config.workers
            if worker.enabled and worker.type == "pi"
        }
        results_by_worker: dict[str, list[StartupHealthcheckResult]] = {}
        for result in results:
            results_by_worker.setdefault(result.worker_name, []).append(result)
        invalid_candidates = [
            worker_name
            for worker_name in sorted(required_workers)
            if len(results_by_worker.get(worker_name, [])) != 1
            or not results_by_worker[worker_name][0].ok
            or _CANDIDATE_RUNNER_CAPABILITY
            not in results_by_worker[worker_name][0].capabilities
        ]
        invalid_candidates.extend(
            worker_name
            for worker_name in sorted(results_by_worker.keys() - required_workers)
        )
        if invalid_candidates:
            raise RuntimeError(
                "candidate capture requires one unambiguous runner capability result for workers: "
                + ", ".join(invalid_candidates)
            )
        worker_capabilities = {
            result.worker_name: result.capabilities
            for result in results
            if result.ok
        }
        if getattr(self.config.runtime, "execution_checkpoints", "off") == "stage":
            capability = self.client.get_execution_event_capabilities(force_refresh=True)
            if not self._server_execution_checkpoint_capability_usable(capability):
                raise RuntimeError("execution checkpoint staging requires a compatible enabled Server capability")
            missing = [
                worker_name
                for worker_name in sorted(required_workers)
                if _EXECUTION_CHECKPOINT_RUNNER_CAPABILITY
                not in worker_capabilities.get(worker_name, frozenset())
            ]
            if missing:
                raise RuntimeError(
                    "execution checkpoint staging requires runner capability for workers: "
                    + ", ".join(missing)
                )
        self._worker_capabilities = worker_capabilities
        if not results or any(result.ok for result in results):
            return
        # 所有 worker 都不可用时直接失败，让操作者先修复凭据/镜像/CLI。
        raise RuntimeError(format_failure_summary(results))

    @staticmethod
    def _server_candidate_capability_usable(
        capability: dict[str, object] | None,
    ) -> bool:
        if capability is None:
            return False
        fixed = {
            "required": True,
            "spool_protocol": "cairn-candidate-spool-v1",
            "frame_protocol": "cairn-candidate-frame-v1",
            "receipt_protocol": "cairn-candidate-capture-receipt-v1",
            "manifest_header": "X-Cairn-Candidate-Manifest",
            "manifest_encoding": "base64url-utf8-canonical-json-no-padding",
            "content_type": "application/octet-stream",
            "source_format": "pi-toolcall-delta-utf8-v1",
        }
        if capability.get("required") is not True or any(
            capability.get(key) != value
            for key, value in fixed.items()
            if key != "required"
        ):
            return False
        required = {
            "max_candidates_per_task_run": CANDIDATE_MAX_CANDIDATES,
            "max_raw_bytes": CANDIDATE_MAX_BYTES,
            "max_manifest_bytes": CANDIDATE_MAX_MANIFEST_BYTES,
            "max_spool_bytes": CANDIDATE_MAX_SPOOL_BYTES,
            "max_outcomes_per_candidate": CANDIDATE_MAX_OUTCOMES_PER_CANDIDATE,
            "max_artifacts_per_candidate": CANDIDATE_MAX_ARTIFACTS,
        }
        return all(
            isinstance(capability.get(key), int)
            and not isinstance(capability.get(key), bool)
            and capability[key] >= minimum
            for key, minimum in required.items()
        )

    @staticmethod
    def _server_execution_checkpoint_capability_usable(
        capability: dict[str, object] | None,
    ) -> bool:
        if capability is None:
            return False
        required = {
            "max_events": EXECUTION_CHECKPOINT_MAX_EVENTS,
            "max_event_bytes": EXECUTION_CHECKPOINT_MAX_EVENT_BYTES,
            "max_spool_bytes": EXECUTION_CHECKPOINT_MAX_SPOOL_BYTES,
        }
        return (
            capability.get("enabled") is True
            and capability.get("protocol") == "cairn-execution-event-v1"
            and all(
                isinstance(capability.get(key), int)
                and not isinstance(capability.get(key), bool)
                and capability[key] >= minimum
                for key, minimum in required.items()
            )
        )

    def _execution_checkpoints_enabled_for(self, worker: WorkerConfig) -> bool:
        """按配置、runner 与 Server 三方能力决定是否注入可选 spool。"""
        if getattr(self.config.runtime, "execution_checkpoints", "off") != "stage":
            return False
        worker_capabilities = getattr(self, "_worker_capabilities", {})
        if _EXECUTION_CHECKPOINT_RUNNER_CAPABILITY not in worker_capabilities.get(
            worker.name,
            frozenset(),
        ):
            return False
        capability = self.client.get_execution_event_capabilities()
        return self._server_execution_checkpoint_capability_usable(capability)
