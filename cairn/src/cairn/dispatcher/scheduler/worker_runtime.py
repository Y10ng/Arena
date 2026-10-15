"""Worker 选择、能力描述和退避窗口。"""

from __future__ import annotations

from docker.errors import DockerException

from .common import *
from .decisions import DispatchDecisionsMixin
from cairn.dispatcher.advisory_bundle import AdvisoryBundle, stage_advisory_bundle
from cairn.dispatcher.runtime.containers import ContainerRuntimeMismatchError


from cairn.dispatcher.config import PiModelConfig


def _pi_model_binding(binding: object) -> PiModelConfig:
    if getattr(binding, "engine", None) != "pi":
        raise ValueError("engine_unsupported: legacy bindings cannot execute")
    return PiModelConfig(
        protocol=getattr(binding, "protocol"),
        model_name=getattr(binding, "model_name"),
        inference_endpoint=getattr(binding, "inference_endpoint"),
        base_url=getattr(binding, "base_url"),
        auth_header=getattr(binding, "auth_header"),
        api_key=getattr(binding, "api_key"),
        custom_headers=[{"name": header.name, "value": header.value} for header in getattr(binding, "custom_headers")],
    )


class ContainerInventoryUnresolvedError(RuntimeError):
    """升级前的容器来源未知，不能在新环境启动执行。"""


class WorkerRuntimeMixin(DispatchDecisionsMixin):
    """Worker 选择、能力描述和退避窗口。"""

    def _scenario_for_project(self, project: ProjectDetail) -> Scenario:
        snapshot = project.project.scenario_snapshot
        if snapshot:
            try:
                return Scenario.model_validate(snapshot)
            except (TypeError, ValueError):
                LOG.warning(
                    "project scenario snapshot is invalid; falling back to catalog project=%s scenario=%s",
                    project.project.id,
                    project.project.scenario_key,
                )
        scenarios = self.client.list_scenarios()
        by_key = {scenario.key: scenario for scenario in scenarios}
        requested = project.project.scenario_key
        if requested in by_key:
            return by_key[requested]
        default = next((scenario for scenario in scenarios if scenario.is_default), None)
        if default is not None:
            # 项目引用的场景被删除/重命名时用默认场景兜底，避免项目永久卡死。
            LOG.warning(
                "project scenario missing, falling back to default project=%s requested=%s default=%s",
                project.project.id,
                requested,
                default.key,
            )
            return default
        LOG.warning(
            "server returned no usable scenarios, falling back to dispatcher runtime config project=%s requested=%s",
            project.project.id,
            requested,
        )
        return self._runtime_default_scenario()

    def _runtime_default_scenario(self) -> Scenario:
        return Scenario(
            key="default",
            label="Runtime default",
            is_default=True,
            profile_id="default",
            image=self.config.container.image,
            prompt_group=self.config.runtime.prompt_group,
            knowledge=None,
            cap_add=list(self.config.container.cap_add),
            features={},
        )

    def _container_runtime_options(
        self,
        scenario: Scenario,
        project_id: str | None = None,
    ) -> ContainerRuntimeOptions:
        advisory_bundle = (
            stage_advisory_bundle(self.client, project_id, scenario.knowledge)
            if project_id is not None
            else None
        )
        if scenario.profile_id:
            return self._container_runtime_options_from_profile(
                scenario,
                advisory_bundle=advisory_bundle,
            )
        return self._container_runtime_options_from_legacy_fields(
            scenario,
            advisory_bundle=advisory_bundle,
        )

    def _container_runtime_options_from_profile(
        self,
        scenario: Scenario,
        *,
        advisory_bundle: AdvisoryBundle | None = None,
    ) -> ContainerRuntimeOptions:
        profile = self._execution_profile(scenario.profile_id)
        if profile is None and scenario.profile_id == "restricted":
            # 受限画像禁止静默降级到无限制 default。
            raise ValueError(
                "restricted requires the restricted execution profile"
            )
        if profile is None:
            self._log_changed(
                f"scenario:{scenario.key}:profile_rejected",
                logging.WARNING,
                "scenario profile_id is not in dispatcher execution_profiles; using default profile scenario=%s requested=%s",
                scenario.key,
                scenario.profile_id,
            )
            profile = self._execution_profile("default") or self.config.execution_profiles[0]

        image = self._image_for_ref(profile.image_ref)
        allowed_images = self._allowed_container_images()
        if image not in allowed_images:
            self._log_changed(
                f"scenario:{scenario.key}:profile_image_rejected",
                logging.WARNING,
                "execution profile image is not in dispatcher allowed images; using default image scenario=%s profile=%s image=%s",
                scenario.key,
                profile.id,
                image,
            )
            image = self.config.container.image

        allowed_caps = set(self.config.container.cap_add)
        effective_caps = [cap for cap in profile.cap_add if cap in allowed_caps]
        rejected_caps = sorted(set(profile.cap_add) - allowed_caps)
        if rejected_caps:
            self._log_changed(
                f"scenario:{scenario.key}:profile_cap_rejected",
                logging.WARNING,
                "execution profile cap_add contains values outside dispatcher baseline; dropping caps scenario=%s profile=%s rejected=%s",
                scenario.key,
                profile.id,
                rejected_caps,
            )

        return ContainerRuntimeOptions.build(
            image=image,
            network_mode=profile.network_policy,
            dns=self.config.container.dns,
            cap_add=effective_caps,
            features={
                "profile_id": profile.id,
                "image_ref": profile.image_ref,
                "isolation": profile.isolation,
                "completed_action": profile.completed_action,
            },
            scenario_key=scenario.key,
            mem_limit=profile.mem_limit if isinstance(profile.mem_limit, int) else None,
            memswap_limit=(
                profile.memswap_limit
                if isinstance(profile.memswap_limit, int)
                else None
            ),
            pids_limit=profile.pids_limit,
            advisory_bundle=advisory_bundle,
        )

    def _container_runtime_options_from_legacy_fields(
        self,
        scenario: Scenario,
        *,
        advisory_bundle: AdvisoryBundle | None = None,
    ) -> ContainerRuntimeOptions:
        requested_image = scenario.image.strip()
        allowed_images = self._allowed_container_images()
        image = requested_image if requested_image and requested_image in allowed_images else self.config.container.image
        if requested_image and image != requested_image:
            self._log_changed(
                f"scenario:{scenario.key}:image_rejected",
                logging.WARNING,
                "scenario image is not in dispatcher allowed_images; using default image scenario=%s requested=%s default=%s",
                scenario.key,
                requested_image,
                self.config.container.image,
            )

        allowed_caps = set(self.config.container.cap_add)
        effective_caps = [cap for cap in scenario.cap_add if cap in allowed_caps]
        rejected_caps = sorted(set(scenario.cap_add) - allowed_caps)
        if rejected_caps:
            self._log_changed(
                f"scenario:{scenario.key}:cap_rejected",
                logging.WARNING,
                "scenario cap_add contains values outside dispatcher baseline; dropping caps scenario=%s rejected=%s",
                scenario.key,
                rejected_caps,
            )

        network_mode = (
            self.config.container.network_mode
            if bool(scenario.features.get("network_flow", False))
            else "none"
        )
        # network_flow=false 时强制 none，场景只能显式打开配置里的网络模式。

        return ContainerRuntimeOptions.build(
            image=image,
            network_mode=network_mode,
            dns=self.config.container.dns,
            cap_add=effective_caps,
            features=dict(scenario.features),
            scenario_key=scenario.key,
            advisory_bundle=advisory_bundle,
        )

    def _execution_profile(self, profile_id: str):
        return next((profile for profile in self.config.execution_profiles if profile.id == profile_id), None)

    def _image_for_ref(self, image_ref: str) -> str:
        image = next((image for image in self.config.images if image.id == image_ref), None)
        return image.image if image is not None else self.config.container.image

    def _allowed_container_images(self) -> set[str]:
        images = {self.config.container.image}
        images.update(image for image in self.config.container.allowed_images if image)
        images.update(image.image for image in self.config.images if image.image)
        return images

    def _select_worker(
        self,
        project_id: str,
        task_type: str,
        scenario: Scenario,
        project_workers: list[object] | None = None,
        tried_workers: set[str] | None = None,
    ) -> WorkerSelection:
        now = time.time()
        # Tenant 项目只从 durable binding 构建候选；空 binding 必须 fail closed。
        candidates: list[WorkerConfig] = []
        blocked_busy: list[dict[str, object]] = []
        blocked_cooldown: list[dict[str, object]] = []
        blocked_rejected: list[dict[str, object]] = []
        blocked_task_type: list[dict[str, object]] = []
        blocked_disabled: list[dict[str, object]] = []
        blocked_not_preferred: list[dict[str, object]] = []
        running_counts = self._worker_counts()
        bound_workers = project_workers or []
        workers = []
        unavailable_reasons = {}
        for binding in bound_workers:
            if getattr(binding, "engine", None) != "pi":
                blocked_rejected.append({
                    "id": str(getattr(binding, "id")),
                    "reason": "engine_unsupported",
                    "engine": getattr(binding, "engine", None),
                })
                continue
            workers.append(self._project_worker_config(binding))
            reason = getattr(binding, "unavailable_reason", None)
            if reason in {"model_disabled", "credential_missing", "binding_stale", "engine_unsupported"}:
                unavailable_reasons[str(getattr(binding, "id"))] = reason
        for worker in workers:
            # 排除顺序决定审计中的 blocked 分类，优先解释启用状态和能力，再解释容量和退避。
            if not worker.enabled:
                blocked_disabled.append(
                    self._worker_blocked_runtime_context(worker, running_counts, reason=unavailable_reasons.get(worker.id, "disabled"))
                )
                continue
            if task_type not in worker.task_types:
                blocked_task_type.append(
                    self._worker_blocked_runtime_context(worker, running_counts, reason="task_type")
                )
                continue
            running = running_counts.get(worker.name, 0)
            if running >= worker.max_running:
                blocked_busy.append(
                    self._worker_blocked_runtime_context(worker, running_counts, reason="busy")
                )
                continue
            cooldown = self.worker_cooldown_until.get(worker.name)
            if cooldown is not None:
                cooldown_until, cooldown_reason = cooldown
                if cooldown_until > now:
                    blocked_cooldown.append(
                        self._worker_blocked_runtime_context(
                            worker,
                            running_counts,
                            reason="cooldown",
                            error_type=cooldown_reason,
                            retry_after_seconds=cooldown_until - now,
                        )
                    )
                    continue
                # 到期的 cooldown/rejection 在选择时惰性清理。
                self.worker_cooldown_until.pop(worker.name, None)
            rejection_key = (project_id, task_type, worker.name)
            rejection = self.worker_rejected_until.get(rejection_key)
            if rejection is not None:
                rejected_until, rejected_reason = rejection
                if rejected_until > now:
                    blocked_rejected.append(
                        self._worker_blocked_runtime_context(
                            worker,
                            running_counts,
                            reason="rejected",
                            error_type=rejected_reason,
                            retry_after_seconds=rejected_until - now,
                        )
                    )
                    continue
                self.worker_rejected_until.pop(rejection_key, None)
            candidates.append(worker)
        if not candidates:
            LOG.debug(
                "worker selection project=%s task=%s scenario=%s no candidates blocked_busy=%s blocked_cooldown=%s blocked_rejected=%s blocked_task_type=%s blocked_disabled=%s blocked_not_preferred=%s",
                project_id,
                task_type,
                scenario.key,
                blocked_busy,
                blocked_cooldown,
                blocked_rejected,
                blocked_task_type,
                blocked_disabled,
                blocked_not_preferred,
            )
            selection = WorkerSelection(
                worker=None,
                candidate_order=[],
                blocked_busy=blocked_busy,
                blocked_cooldown=blocked_cooldown,
                blocked_rejected=blocked_rejected,
                blocked_task_type=blocked_task_type,
                blocked_disabled=blocked_disabled,
                blocked_not_preferred=blocked_not_preferred,
            )
            self._observe_selection(project_id, task_type, selection)
            return selection
        ordered = choose_worker(candidates, running_counts, tried_workers=tried_workers)
        candidate_order = [
            self._worker_candidate_runtime_context(worker, running_counts)
            for worker in ordered
        ]
        LOG.debug(
            "worker selection project=%s task=%s scenario=%s candidates=%s blocked_busy=%s blocked_cooldown=%s blocked_rejected=%s blocked_task_type=%s blocked_disabled=%s chosen=%s",
            project_id,
            task_type,
            scenario.key,
            [f"{worker.name}({running_counts.get(worker.name, 0)}/{worker.max_running},p{worker.priority})" for worker in candidates],
            blocked_busy,
            blocked_cooldown,
            blocked_rejected,
            blocked_task_type,
            blocked_disabled,
            ordered[0].name if ordered else None,
        )
        return WorkerSelection(
            worker=ordered[0] if ordered else None,
            candidate_order=candidate_order,
            blocked_busy=blocked_busy,
            blocked_cooldown=blocked_cooldown,
            blocked_rejected=blocked_rejected,
            blocked_task_type=blocked_task_type,
            blocked_disabled=blocked_disabled,
            blocked_not_preferred=blocked_not_preferred,
        )

    @staticmethod
    def _project_worker_config(binding: object) -> WorkerConfig:
        if getattr(binding, "engine", None) != "pi":
            raise ValueError("engine_unsupported: legacy bindings cannot execute")
        return WorkerConfig(
            id=str(getattr(binding, "id")),
            name=str(getattr(binding, "id")),
            engine="pi",
            model_id="",
            task_types=list(getattr(binding, "task_types")),
            concurrency=int(getattr(binding, "max_running")),
            priority=max(0, int(getattr(binding, "priority"))),
            enabled=bool(getattr(binding, "enabled")) and bool(
                getattr(binding, "currently_available")
            ),
            # 候选不持有凭据；执行前通过 machine route materialize。
            model_binding=None,
        )

    def _materialize_worker(
        self,
        project_id: str,
        worker: WorkerConfig,
        task_type: str,
    ) -> tuple[WorkerConfig, dict[str, object], BindingCircuitKey] | None:
        try:
            binding = self.client.resolve_execution_binding(
                project_id,
                worker.id,
                task_type,
            )
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            self._observe_decision(project_id, "binding_stale" if status in (404, 409) else "binding_failed", task_type=task_type, worker_id=worker.id)
            LOG.warning(
                "execution binding resolution failed project=%s worker=%s task=%s error=%s",
                project_id,
                worker.id,
                task_type,
                exc,
            )
            return None
        return self._materialized_execution_binding(binding)

    def _materialized_execution_binding(self, binding):
        effective = WorkerConfig(
            id=binding.worker_id,
            name=binding.worker_id,
            engine="pi",
            model_id=binding.model_id,
            task_types=binding.task_types,
            concurrency=binding.max_running,
            priority=max(0, binding.priority),
            enabled=True,
            model_binding=_pi_model_binding(binding),
        )
        return effective, binding.execution_provenance(), self._binding_circuit_key(binding)

    @staticmethod
    def _binding_circuit_key(binding: object) -> BindingCircuitKey:
        return BindingCircuitKey(
            model_id=str(getattr(binding, "model_id")),
            model_revision=int(getattr(binding, "model_revision")),
            credential_version_id=str(getattr(binding, "credential_version_id")),
            credential_version=int(getattr(binding, "credential_version")),
            protocol=str(getattr(binding, "protocol")),
            auth_header=str(getattr(binding, "auth_header")),
            inference_endpoint=str(getattr(binding, "inference_endpoint")),
        )

    def _materialize_selected_worker(
        self,
        project_id: str,
        task_type: str,
        selection: WorkerSelection,
        project_workers: list[object],
    ) -> tuple[WorkerConfig, dict[str, object], BindingCircuitKey] | None:
        bindings = {
            str(getattr(binding, "id")): binding for binding in project_workers
        }
        for candidate in selection.candidate_order:
            worker_id = str(candidate.get("id") or "")
            binding = bindings.get(worker_id)
            if binding is None:
                self._observe_decision(project_id, "binding_stale", task_type=task_type)
                continue
            materialized = self._materialize_worker(
                project_id,
                self._project_worker_config(binding),
                task_type,
            )
            if materialized is None:
                continue
            worker, provenance, circuit_key = materialized
            retry_after = self._binding_auth_circuit_retry_after(circuit_key)
            if retry_after is None:
                return worker, provenance, circuit_key
            selection.blocked_cooldown.append(
                self._worker_blocked_runtime_context(
                    worker,
                    self._worker_counts(),
                    reason="binding_auth_circuit",
                    error_type="provider_auth",
                    retry_after_seconds=retry_after,
                )
            )
            self._observe_selection(project_id, task_type, selection)
            self._log_changed(
                f"binding-auth-circuit:{circuit_key}",
                logging.INFO,
                "execution binding auth circuit open model=%s credential_version=%s retry_after=%.0fs",
                circuit_key.model_id,
                circuit_key.credential_version_id,
                retry_after,
            )
        return None

    def _binding_auth_circuit_retry_after(
        self, key: BindingCircuitKey
    ) -> float | None:
        until = getattr(self, "binding_auth_circuit_until", {}).get(key)
        if until is None:
            return None
        remaining = until - time.time()
        if remaining > 0:
            return remaining
        # 到期只重新放行一次请求，历史失败次数保留用于下一次指数退避。
        getattr(self, "binding_auth_circuit_until", {}).pop(key, None)
        self._clear_log_state(f"binding-auth-circuit:{key}")
        return None

    def _mark_binding_auth_circuit(
        self, key: BindingCircuitKey
    ) -> tuple[float, int]:
        attempts = getattr(self, "binding_auth_circuit_attempts", None)
        if attempts is None:
            attempts = {}
            self.binding_auth_circuit_attempts = attempts
        attempt = int(attempts.get(key, 0)) + 1
        attempts[key] = attempt
        retry_after = self._backoff_retry_after_seconds(
            WORKER_COOLDOWN_RETRY_AFTER_SECONDS,
            attempt,
            max_seconds=WORKER_HEALTH_BACKOFF_MAX_SECONDS,
        )
        circuits = getattr(self, "binding_auth_circuit_until", None)
        if circuits is None:
            circuits = {}
            self.binding_auth_circuit_until = circuits
        circuits[key] = time.time() + retry_after
        return retry_after, attempt

    def _clear_binding_auth_circuit(self, key: BindingCircuitKey) -> None:
        getattr(self, "binding_auth_circuit_until", {}).pop(key, None)
        getattr(self, "binding_auth_circuit_attempts", {}).pop(key, None)
        self._clear_log_state(f"binding-auth-circuit:{key}")

    def _worker_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.futures.values():
            counts[task.worker_name] = counts.get(task.worker_name, 0) + 1
        return counts

    def _worker_selection_runtime_context(
        self,
        selection: WorkerSelection,
        task_type: str,
        worker: WorkerConfig,
    ) -> dict[str, object]:
        return {
            "task_type": task_type,
            "chosen": self._worker_candidate_runtime_context(worker, self._worker_counts()),
            "candidate_order": selection.candidate_order,
            "blocked": {
                "busy": selection.blocked_busy,
                "cooldown": selection.blocked_cooldown,
                "rejected": selection.blocked_rejected,
                "task_type": selection.blocked_task_type,
                "disabled": selection.blocked_disabled,
                "not_preferred": selection.blocked_not_preferred,
            },
            "sort_keys": ["priority", "running_count", "random_tiebreaker"],
        }

    @staticmethod
    def _worker_candidate_runtime_context(worker: WorkerConfig, running_counts: dict[str, int]) -> dict[str, object]:
        return {
            "id": worker.id,
            "name": worker.name,
            "engine": worker.engine,
            "type": worker.type,
            "model_id": worker.model_id,
            "priority": worker.priority,
            "running": running_counts.get(worker.name, 0),
            "max_running": worker.max_running,
            "concurrency": worker.concurrency,
            "enabled": worker.enabled,
        }

    @classmethod
    def _worker_blocked_runtime_context(
        cls,
        worker: WorkerConfig,
        running_counts: dict[str, int],
        *,
        reason: str,
        error_type: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> dict[str, object]:
        item = cls._worker_candidate_runtime_context(worker, running_counts)
        item["reason"] = reason
        if error_type is not None:
            item["error_type"] = error_type if error_type in STANDARD_ERROR_TYPES else "unknown"
        if retry_after_seconds is not None:
            item["retry_after_seconds"] = round(max(float(retry_after_seconds), 0.0), 3)
        return item

    def _worker_features_runtime_context(self, worker: WorkerConfig) -> dict[str, object]:
        defaults = dict(DEFAULT_WORKER_FEATURES["pi"])
        return {
            "worker_name": worker.name,
            "worker_type": worker.type,
            **defaults,
        }

    def _worker_model(self, worker: WorkerConfig) -> str:
        return worker.model_binding.model_name if worker.model_binding is not None else "unknown"

    def _worker_type_by_name(self) -> dict[str, str]:
        return {worker.name: worker.type for worker in self.config.workers}

    def _probe_worker_readiness(
        self,
        project_id: str,
        task_type: str,
        worker: WorkerConfig,
        container_runtime: ContainerRuntimeOptions,
        generation: int,
    ) -> bool:
        """在 Server admission 前登记容器并验证 Worker；项目级阻断不污染共享 Worker 退避。"""
        inventory_blocked = getattr(self, "_container_inventory_blocked", set())
        if (project_id, generation) in inventory_blocked:
            self._observe_decision(project_id, "container_inventory_unresolved", task_type=task_type, worker_id=worker.id)
            return False
        signature = getattr(container_runtime, "runtime_signature", None)
        mismatches = getattr(self, "_container_mismatches", {})
        if isinstance(signature, str) and mismatches.get(project_id) == (generation, signature):
            self._observe_decision(project_id, "container_runtime_mismatch", task_type=task_type, worker_id=worker.id)
            return False
        mismatches.pop(project_id, None)

        def register(daemon_id: str) -> str:
            response = self.client.register_container_activation(project_id, generation, daemon_id)
            if (response.status_code == 409 and isinstance(response.data, dict)
                    and response.data.get("detail") == "container_inventory_unresolved"):
                raise ContainerInventoryUnresolvedError
            token = response.data.get("activation_token") if isinstance(response.data, dict) else None
            if not response.ok or not isinstance(token, str) or not token:
                raise RuntimeError("container activation registration failed")
            return token

        def finish(token: str, container_id: str | None) -> None:
            response = self.client.finish_container_activation(project_id, token, container_id)
            if not response.ok:
                raise RuntimeError("container activation completion failed")

        try:
            container_name = self.container_manager.activate_for_readiness(
                project_id, container_runtime, register=register, finish=finish,
            )
        except ContainerInventoryUnresolvedError:
            self._container_inventory_blocked = getattr(self, "_container_inventory_blocked", set())
            self._container_inventory_blocked.add((project_id, generation))
            self._observe_decision(project_id, "container_inventory_unresolved", task_type=task_type, worker_id=worker.id)
            LOG.warning("worker readiness blocked by unresolved legacy container project=%s task=%s", project_id, task_type)
            return False
        except ContainerRuntimeMismatchError:
            if isinstance(signature, str):
                self._container_mismatches = getattr(self, "_container_mismatches", {})
                self._container_mismatches[project_id] = (generation, signature)
            self._observe_decision(project_id, "container_runtime_mismatch", task_type=task_type, worker_id=worker.id)
            LOG.warning("worker readiness blocked by preserved container runtime project=%s task=%s", project_id, task_type)
            return False
        except (DockerException, OSError, RuntimeError):
            LOG.exception(
                "worker readiness container preparation failed project=%s task=%s worker=%s",
                project_id,
                task_type,
                worker.name,
            )
            error_type = "readiness_transport"
            result = None
        else:
            result = run_readiness_check(
                self.container_manager,
                container_name,
                timeout_seconds=self.config.runtime.healthcheck_timeout,
            )
            error_type = result.error_type

        if result is not None and result.ok:
            self._clear_worker_readiness_cooldown(worker.name)
            LOG.debug(
                "worker readiness accepted project=%s task=%s worker=%s duration_ms=%s capabilities=%s",
                project_id,
                task_type,
                worker.name,
                result.duration_ms,
                sorted(result.capabilities),
            )
            return True

        assert error_type in READINESS_ERROR_TYPES
        retry_after_seconds, attempt = self._mark_worker_cooldown(
            worker.name,
            error_type,
        )
        self._observe_decision(
            project_id,
            error_type,
            task_type=task_type,
            worker_id=worker.id,
            retry_at=_utc_from_timestamp(time.time() + retry_after_seconds),
        )
        LOG.warning(
            "worker readiness rejected project=%s task=%s worker=%s error_type=%s returncode=%s duration_ms=%s retry_after=%.0fs attempt=%s",
            project_id,
            task_type,
            worker.name,
            error_type,
            result.returncode if result is not None else None,
            result.duration_ms if result is not None else None,
            retry_after_seconds,
            attempt,
        )
        return False

    @staticmethod
    def _worker_cooldown_error_type(error_type: str | None) -> str | None:
        if error_type in WORKER_COOLDOWN_ERROR_TYPES:
            return error_type
        return None

    @staticmethod
    def _backoff_retry_after_seconds(base_seconds: int, attempt: int, *, max_seconds: int = WORKER_BACKOFF_MAX_SECONDS) -> float:
        # 退避按指数增长但有上限，避免 worker 长时间完全饥饿。
        multiplier = 2 ** max(attempt - 1, 0)
        return float(min(max_seconds, base_seconds * multiplier))

    def _mark_worker_cooldown(self, worker_name: str, error_type: str) -> tuple[float, int]:
        attempts = getattr(self, "worker_cooldown_attempts", None)
        if attempts is None:
            attempts = {}
            self.worker_cooldown_attempts = attempts
        attempt_key = (worker_name, error_type)
        attempt = int(attempts.get(attempt_key, 0)) + 1
        attempts[attempt_key] = attempt
        retry_after_seconds = self._backoff_retry_after_seconds(
            WORKER_COOLDOWN_RETRY_AFTER_SECONDS,
            attempt,
            max_seconds=WORKER_HEALTH_BACKOFF_MAX_SECONDS,
        )
        self.worker_cooldown_until[worker_name] = (time.time() + retry_after_seconds, error_type)
        return retry_after_seconds, attempt

    def _clear_worker_cooldown(self, worker_name: str) -> None:
        getattr(self, "worker_cooldown_until", {}).pop(worker_name, None)
        attempts = getattr(self, "worker_cooldown_attempts", {})
        for key in list(attempts):
            if isinstance(key, tuple) and key and key[0] == worker_name:
                attempts.pop(key, None)
            elif key == worker_name:
                # Backward-compatible cleanup for loops created before the key was widened.
                attempts.pop(key, None)

    def _clear_worker_readiness_cooldown(self, worker_name: str) -> None:
        cooldown = getattr(self, "worker_cooldown_until", {}).get(worker_name)
        if cooldown is not None and cooldown[1] in READINESS_ERROR_TYPES:
            self.worker_cooldown_until.pop(worker_name, None)
        attempts = getattr(self, "worker_cooldown_attempts", {})
        for key in list(attempts):
            if (
                isinstance(key, tuple)
                and len(key) == 2
                and key[0] == worker_name
                and key[1] in READINESS_ERROR_TYPES
            ):
                attempts.pop(key, None)

    def _mark_worker_rejected(self, key: tuple[str, str, str], error_type: str) -> tuple[float, int]:
        attempts = getattr(self, "worker_rejected_attempts", None)
        if attempts is None:
            attempts = {}
            self.worker_rejected_attempts = attempts
        attempt_key = (*key, error_type)
        attempt = int(attempts.get(attempt_key, 0)) + 1
        attempts[attempt_key] = attempt
        retry_after_seconds = self._backoff_retry_after_seconds(REJECTED_RETRY_AFTER_SECONDS, attempt)
        self.worker_rejected_until[key] = (time.time() + retry_after_seconds, error_type)
        return retry_after_seconds, attempt

    def _clear_worker_rejected(self, key: tuple[str, str, str]) -> None:
        getattr(self, "worker_rejected_until", {}).pop(key, None)
        attempts = getattr(self, "worker_rejected_attempts", {})
        for attempt_key in list(attempts):
            if isinstance(attempt_key, tuple) and attempt_key[:3] == key:
                attempts.pop(attempt_key, None)
            elif attempt_key == key:
                attempts.pop(attempt_key, None)
