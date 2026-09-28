"""任务 runtime_context 组装。"""

from __future__ import annotations

from .common import *


class RuntimeContextMixin:
    """任务 runtime_context 组装。"""

    def _task_run_runtime_context(
        self,
        project: ProjectDetail,
        task_type: str,
        worker: WorkerConfig,
        intent_id: str | None,
        *,
        scenario: Scenario,
        container_runtime: ContainerRuntimeOptions,
        selection: WorkerSelection | None = None,
        binding_provenance: dict[str, object] | None = None,
    ) -> dict[str, object]:
        project_id = project.project.id
        # runtime_context 是调度决策的审计副本，不参与 worker prompt 语义。
        advisory_context = knowledge_base_runtime_context(
            scenario.knowledge,
            advisory=container_runtime,
        )
        context: dict[str, object] = {
            "project_id": project_id,
            "intent_id": intent_id,
            "task_type": task_type,
            "worker_name": worker.name,
            "worker_type": worker.type,
            "model": self._worker_model(worker),
            "prompt_group": scenario.prompt_group,
            "scenario": self._scenario_runtime_context(scenario, container_runtime),
            "dispatcher_id": self.dispatcher_id,
            "config_hash": self._config_hash(),
            "config_path": str(self._config_path),
            "config_sha256": self._config_sha256 or self._config_hash(),
            "container_name": self.container_manager.container_name(project_id),
            "container_image": container_runtime.image,
            "network_mode": container_runtime.network_mode,
            "execution_environment": self._execution_environment_runtime_context(project_id, container_runtime),
            "target_scope": self._target_scope_runtime_context(project),
            "knowledge_base": advisory_context,
            "project_resources": {
                "bundle_digest": advisory_context.get("bundle_digest"),
                "manifest_path": advisory_context.get("manifest_path"),
                "read_only": True,
                "resources": [
                    item
                    for item in advisory_context.get("resources", [])
                    if isinstance(item, dict)
                    and item.get("source") == "project_resource"
                ],
            },
        }
        if selection is not None:
            context["worker_selection"] = self._worker_selection_runtime_context(selection, task_type, worker)
        if binding_provenance is not None:
            context["execution_binding"] = dict(binding_provenance)
        return context

    def _execution_environment_runtime_context(
        self,
        project_id: str,
        container_runtime: ContainerRuntimeOptions | None = None,
    ) -> dict[str, object]:
        container_name = self.container_manager.container_name(project_id)
        runtime = container_runtime or self.container_manager.default_runtime_options()
        profile_id = str(runtime.features.get("profile_id") or self.config.container.execution_profile)
        return {
            "profile": profile_id,
            "isolation": str(runtime.features.get("isolation") or "shared_container"),
            "workspace": self._workspace_runtime_context(),
            "container": {
                "container_name": container_name,
                "image": runtime.image,
                "network_mode": runtime.network_mode,
                "dns": list(runtime.dns) or "docker-default",
                "cap_add": list(runtime.cap_add),
                "features": dict(runtime.features),
                "scenario_key": runtime.scenario_key,
                "advisory_mount_target": runtime.advisory_mount_target,
                "advisory_digest": runtime.advisory_digest,
                "runtime_signature": runtime.runtime_signature,
                "artifact_root": f"artifact://projects/{project_id}/runs/pending",
            },
        }

    @staticmethod
    def _workspace_runtime_context() -> dict[str, object]:
        # 当前 workspace 共享写入，风险通过 runtime_context 明示给监控和审计。
        return {
            "mode": "shared_project_workspace",
            "project_path": PROJECT_WORKSPACE_PATH,
            "task_path_template": f"{PROJECT_WORKSPACE_PATH}/.cairn/runs/{{task_run_id}}",
            "write_isolation": "not_enforced",
            "concurrent_write_risk": True,
        }

    def _target_scope_runtime_context(self, project: ProjectDetail) -> dict[str, object]:
        return _target_scope_runtime_context(project.targets)

    def _scenario_runtime_context(
        self,
        scenario: Scenario,
        container_runtime: ContainerRuntimeOptions | None = None,
    ) -> dict[str, object]:
        return {
            "key": scenario.key,
            "label": scenario.label,
            "profile_id": scenario.profile_id,
            "prompt_group": scenario.prompt_group,
            "knowledge": scenario.knowledge,
            "image": scenario.image,
            "cap_add": list(scenario.cap_add),
            "features": dict(scenario.features),
            "is_default": scenario.is_default,
            "container_runtime": self._scenario_container_runtime_context(scenario, container_runtime)
            if container_runtime is not None
            else None,
        }

    def _scenario_container_runtime_context(
        self,
        scenario: Scenario,
        container_runtime: ContainerRuntimeOptions,
    ) -> dict[str, object]:
        if scenario.profile_id:
            profile_features = dict(container_runtime.features)
            requested_image = container_runtime.image
            requested_network_mode = container_runtime.network_mode
            requested_caps = list(container_runtime.cap_add)
            return {
                "requested": {
                    "profile_id": scenario.profile_id,
                    "image": requested_image,
                    "network_mode": requested_network_mode,
                    "cap_add": requested_caps,
                    "features": profile_features,
                },
                "effective": {
                    "image": container_runtime.image,
                    "network_mode": container_runtime.network_mode,
                    "dns": list(container_runtime.dns) or "docker-default",
                    "cap_add": list(container_runtime.cap_add),
                    "features": profile_features,
                    "advisory_mount_target": container_runtime.advisory_mount_target,
                    "advisory_digest": container_runtime.advisory_digest,
                    "runtime_signature": container_runtime.runtime_signature,
                },
                "rejected": {
                    "image": None,
                    "cap_add": [],
                },
                "policy": {
                    "profile_source": "dispatch_yaml.execution_profiles",
                    "image_source": "dispatch_yaml.images",
                    "cap_add_source": "dispatch_yaml.container.cap_add_baseline",
                    "network_mode_source": "execution_profile.network_policy",
                    "advisory_mount_source": "dispatcher_staged_bundle",
                },
            }
        requested_image = scenario.image.strip() or self.config.container.image
        requested_network_mode = (
            self.config.container.network_mode
            if bool(scenario.features.get("network_flow", False))
            else "none"
        )
        effective_caps = set(container_runtime.cap_add)
        requested_caps = list(scenario.cap_add)
        rejected_caps = [cap for cap in requested_caps if cap not in effective_caps]
        rejected_image = requested_image if container_runtime.image != requested_image else None
        # 同时记录 requested/effective/rejected，便于解释场景配置为何被降级。
        return {
            "requested": {
                "image": requested_image,
                "network_mode": requested_network_mode,
                "cap_add": requested_caps,
                "features": dict(scenario.features),
            },
            "effective": {
                "image": container_runtime.image,
                "network_mode": container_runtime.network_mode,
                "cap_add": list(container_runtime.cap_add),
                "features": dict(container_runtime.features),
                "advisory_mount_target": container_runtime.advisory_mount_target,
                "advisory_digest": container_runtime.advisory_digest,
                "runtime_signature": container_runtime.runtime_signature,
            },
            "rejected": {
                "image": rejected_image,
                "cap_add": rejected_caps,
            },
            "policy": {
                "image_source": "dispatch_yaml.container.image_or_allowed_images",
                "cap_add_source": "dispatch_yaml.container.cap_add_baseline",
                "features_source": "scenario_schema",
                "network_mode_source": "scenario.features.network_flow",
                "advisory_mount_source": "dispatcher_staged_bundle",
            },
        }
