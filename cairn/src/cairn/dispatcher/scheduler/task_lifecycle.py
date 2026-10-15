"""任务提交、future 回收和 task_run 收尾。"""

from __future__ import annotations

from cairn.dispatcher.runtime.browser_sessions import notify_browser_task
from cairn.dispatcher.tasks.delivery import retained_runner_available
from cairn.dispatcher.tasks.external_verification import run_external_verification_task
from cairn.dispatcher.worker_result_gateway import WorkerResultFailure, worker_failure_error_type
from cairn.server.schemas.tenant_workers import TenantWorkerExecutionBinding

from .common import *
from .decisions import DispatchDecisionsMixin, claim_failure_code
from .task_run_finish import TaskRunFinishMixin


class TaskLifecycleMixin(TaskRunFinishMixin, DispatchDecisionsMixin):
    """任务提交、future 回收和 task_run 收尾。"""

    def _delivery_route_workers(self, project, task_type: str, intent_id: str | None = None, *, report_state=None):
        project_id = project.project.id
        if report_state is not None:
            route = report_state.get("delivery_retry_route")
        else:
            try:
                response = self.client.list_task_runs(project_id)
            except requests.RequestException:
                self._observe_decision(project_id, "admission_failed", task_type=task_type)
                return None
            if not response.ok or not isinstance(response.data, list):
                self._observe_decision(project_id, "admission_failed", task_type=task_type)
                return None
            routes = [run.get("delivery_retry_route") for run in response.data if isinstance(run, dict)]
            routes = [route for route in routes if isinstance(route, dict)
                      and route.get("task_type") == task_type and route.get("intent_id") == intent_id]
            if len(routes) > 1:
                self._observe_decision(project_id, "execution_delivery_unavailable", task_type=task_type)
                return None
            route = routes[0] if routes else None
        workers = project.project.tenant_workers
        if route is None:
            return DeliveryWorkerRoute(workers=workers, mode="fresh")
        # 只消费 Server 的当前逻辑链裁决；不在调度器复制错误分类、代次或退避策略。
        if not isinstance(route, dict):
            self._observe_decision(project_id, "execution_delivery_unavailable", task_type=task_type)
            return None
        if route.get("allowed") is not True:
            reason = route.get("reason")
            code = reason if reason in {"execution_retry_cooldown", "execution_retry_blocked", "execution_retry_exhausted", "execution_continuation_unavailable"} else (
                "cleanup_pending" if reason == "assessment_execution_not_quiescent" else "execution_delivery_unavailable")
            self._observe_decision(project_id, code, task_type=task_type, retry_at=route.get("retry_at"))
            return None
        if route.get("dispatcher_id") != self.dispatcher_id:
            self._observe_decision(project_id, "execution_delivery_unavailable", task_type=task_type)
            return None
        workers = [worker for worker in workers if str(getattr(worker, "id", "")) == route.get("worker_name")
                   and getattr(worker, "engine", None) == "pi" and getattr(worker, "enabled", False)
                   and getattr(worker, "currently_available", False)]
        if not workers:
            self._observe_decision(project_id, "execution_delivery_unavailable", task_type=task_type)
            return None
        return DeliveryWorkerRoute(
            workers=workers,
            mode="continuation" if route.get("continuation") else "delivery_retry",
            container_id=route.get("container_id") if route.get("continuation") else None,
        )

    def _preflight_retained_runner(self, project_id, task_type, worker, route) -> bool:
        if route.mode != "continuation":
            return True
        if retained_runner_available(
            self.container_manager, project_id, route.container_id,
            client=self.client, generation=self._project_generations.get(project_id),
        ):
            return True
        # 原容器旧 runner 或工作区不可核验时，领取前明确阻断，不能先记失败 Run 再回退空会话。
        self._observe_decision(project_id, "execution_continuation_unavailable", task_type=task_type, worker_id=worker.id)
        return False

    def _dispatch_external_verification(self, project: ProjectDetail) -> bool:
        project_id = project.project.id
        request_id = project.project.verification_request_id
        if project.project.status != "active" or project.project.authorization_block_reason is not None:
            return False
        if self._project_running_task_count(project_id):
            return False
        runs = self.client.list_task_runs(project_id)
        if not runs.ok or not isinstance(runs.data, list):
            return False
        if any(run.get("task_type") == "external_verify" for run in runs.data if isinstance(run, dict)):
            # 已执行的请求只恢复留存／停止／终结，绝不从新会话再次触碰目标。
            return False
        scenario = self._scenario_for_project(project)
        container_runtime = self._container_runtime_options(scenario, project_id)
        selection = self._select_worker(project_id, "explore", scenario, project.project.tenant_workers)
        if selection.worker is None:
            return False
        worker = selection.worker
        # 候选只决定稳定身份；首次 claim 的回执才是本次真正消费的模型与凭据。
        runtime_context = self._task_run_runtime_context(
            project, "external_verify", worker, None, scenario=scenario,
            container_runtime=container_runtime, selection=selection,
        )
        admission = self.client.claim_verification(request_id, {
            "task_type": "external_verify", "worker_name": worker.name,
            "worker_type": worker.type, "prompt_group": scenario.prompt_group,
            "execution_profile": self.config.container.execution_profile,
            "runtime_context": runtime_context,
            "expected_execution_generation": project.project.execution_generation,
        })
        if not admission.ok or not isinstance(admission.data, dict):
            self._observe_decision(project_id, claim_failure_code(admission), task_type="external_verify", worker_id=worker.id)
            return False
        claim = admission.data
        if claim["replayed"]:
            return False
        binding = TenantWorkerExecutionBinding.model_validate(claim["execution_binding"])
        worker, _provenance, circuit_key = self._materialized_execution_binding(binding)
        task_run = claim["task_run"]
        task_run_id = task_run["id"]
        prior_context = task_run.get("runtime_context") or {}
        if "execution_delivery" in prior_context or (prior_context.get("execution_lifecycle") or {}).get("process_started") is True:
            # claim 回执重放不是重复执行许可，现有精确清理与 candidate 恢复链负责收口。
            return False
        cancellation = TaskCancellation(generation=claim["execution_generation"])
        if not self._probe_worker_readiness(
            project_id, "external_verify", worker, container_runtime, cancellation.generation,
        ):
            self._finish_task_run(
                project_id, task_run_id, "failed", task_type="external_verify",
                worker_name=worker.name, worker_type=worker.type,
                error_type="container_failed", error_message="本次验证执行环境未就绪",
                container_runtime=container_runtime,
                execution_lifecycle=cancellation.execution_lifecycle(task_finished=True),
            )
            return False
        try:
            future = self.executor.submit(
                run_external_verification_task, self.config, self.client,
                self.container_manager, project, worker, cancellation, task_run_id,
                claim=claim, container_runtime=container_runtime,
                candidate_outbox=self.candidate_capture_outbox,
                checkpoint_staging=self._execution_checkpoints_enabled_for(worker),
            )
        except RuntimeError:
            self._finish_task_run(
                project_id, task_run_id, "failed", task_type="external_verify",
                worker_name=worker.name, worker_type=worker.type,
                error_type="unknown", error_message="Dispatcher 提交验证任务失败",
                container_runtime=container_runtime,
                execution_lifecycle=cancellation.execution_lifecycle(task_finished=True),
            )
            return False
        self.futures[future] = RunningTask(
            project_id, "external_verify", worker.name, worker.type,
            cancellation, container_runtime, task_run_id=task_run_id,
            intent_id=claim["intent_id"], binding_circuit_key=circuit_key,
            execution_generation=claim["execution_generation"],
        )
        self.runtime_project_ids.add(project_id)
        self._observe_decision(project_id, "dispatched", task_type="external_verify", worker_id=worker.id)
        return True

    def _dispatch_reason(self, project: ProjectDetail, export_yaml: str, trigger: str) -> bool:
        if self._candidate_project_blocked(project.project.id):
            self._observe_decision(project.project.id, "cleanup_pending", task_type="reason")
            return False
        scenario = self._scenario_for_project(project)
        container_runtime = self._container_runtime_options(
            scenario,
            project.project.id,
        )
        route = self._delivery_route_workers(project, "reason")
        if route is None:
            return False
        selection = self._select_worker(
            project.project.id,
            "reason",
            scenario,
            route.workers,
        )
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:reason",
                logging.INFO,
                "no worker available for reason project=%s scenario=%s blocked_busy=%s blocked_cooldown=%s blocked_rejected=%s blocked_disabled=%s",
                project.project.id,
                scenario.key,
                selection.blocked_busy,
                selection.blocked_cooldown,
                selection.blocked_rejected,
                selection.blocked_disabled,
            )
            return False
        materialized = self._materialize_selected_worker(
            project.project.id,
            "reason",
            selection,
            project.project.tenant_workers,
        )
        if materialized is None:
            return False
        worker, binding_provenance, binding_circuit_key = materialized
        self._clear_log_state(f"project:{project.project.id}:worker:reason")
        if route.mode == "fresh" and not self._probe_worker_readiness(
            project.project.id,
            "reason",
            worker,
            container_runtime,
            project.project.execution_generation,
        ):
            return False
        if not self._preflight_retained_runner(project.project.id, "reason", worker, route):
            return False
        claim = self.client.claim_reason(project.project.id, worker.name, trigger)
        # claim 成功后才创建 task_run，避免审计中出现未拿到租约的运行记录。
        if claim.status_code in (403, 409):
            self._observe_decision(project.project.id, claim_failure_code(claim), task_type="reason", worker_id=worker.id)
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "reason claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            self._observe_decision(project.project.id, "claim_failed", task_type="reason", worker_id=worker.id)
            LOG.warning(
                "reason claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        task_run_id = self._create_task_run(
            project,
            "reason",
            worker,
            intent_id=None,
            scenario=scenario,
            container_runtime=container_runtime,
            selection=selection,
            binding_provenance=binding_provenance,
        )
        if task_run_id is None:
            self._best_effort_release_reason(project.project.id, worker.name, task_run_id=task_run_id)
            return False
        try:
            future = self.executor.submit(
                run_reason_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                export_yaml,
                worker,
                cancellation := TaskCancellation(generation=self._take_task_generation(project, task_run_id)),
                task_run_id,
                prompt_group=scenario.prompt_group,
                knowledge=scenario.knowledge,
                container_runtime=container_runtime,
                delivery_context=self._take_delivery_context(project, task_run_id),
                continuation_context=self._take_continuation_context(project, task_run_id),
                admission_context=self._take_admission_context(project, task_run_id),
                checkpoint_staging=self._execution_checkpoints_enabled_for(worker),
                candidate_outbox=self.candidate_capture_outbox,
            )
        except Exception:
            self._observe_decision(project.project.id, "submit_failed", task_type="reason", worker_id=worker.id)
            # submit 失败已经拿到 Server 租约，需要尽力释放并把 task_run 收口。
            LOG.exception("failed to submit reason task project=%s worker=%s", project.project.id, worker.name)
            self._best_effort_release_reason(project.project.id, worker.name, task_run_id=task_run_id)
            self._finish_task_run(
                project.project.id,
                task_run_id,
                "failed",
                task_type="reason",
                worker_name=worker.name,
                worker_type=worker.type,
                error_type="unknown",
                error_message="Dispatcher 提交任务失败",
                container_runtime=container_runtime,
                execution_lifecycle=cancellation.execution_lifecycle(task_finished=True),
                execution_container_ids=cancellation.execution_container_ids(),
            )
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "reason",
            worker.name,
            worker.type,
            cancellation,
            container_runtime,
            task_run_id=task_run_id,
            intent_id=None,
            fact_count=len(project.facts),
            hint_count=len(project.hints),
            open_intent_count=self._project_open_intent_count(project),
            coverage_checkpoint_cell_count=(
                project.project.coverage_checkpoint_cell_count
            ),
            coverage_stop_reason=project.project.coverage_stop_reason,
            binding_circuit_key=binding_circuit_key,
            settled_intent_count=self._project_settled_intent_count(project),
            execution_generation=project.project.execution_generation,
        )
        self._observe_decision(project.project.id, "dispatched", task_type="reason", worker_id=worker.id)
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched reason project=%s worker=%s trigger=%s scenario=%s", project.project.id, worker.name, trigger, scenario.key)
        return True

    def _dispatch_bootstrap(self, project: ProjectDetail, intent: Intent) -> bool:
        if self._candidate_project_blocked(project.project.id):
            self._observe_decision(project.project.id, "cleanup_pending", task_type="bootstrap")
            return False
        scenario = self._scenario_for_project(project)
        container_runtime = self._container_runtime_options(
            scenario,
            project.project.id,
        )
        route = self._delivery_route_workers(project, "bootstrap", intent.id)
        if route is None:
            return False
        selection = self._select_worker(
            project.project.id,
            "bootstrap",
            scenario,
            route.workers,
        )
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:bootstrap",
                logging.INFO,
                "no worker available for bootstrap project=%s intent=%s scenario=%s blocked_busy=%s blocked_cooldown=%s blocked_rejected=%s blocked_disabled=%s",
                project.project.id,
                intent.id,
                scenario.key,
                selection.blocked_busy,
                selection.blocked_cooldown,
                selection.blocked_rejected,
                selection.blocked_disabled,
            )
            return False
        materialized = self._materialize_selected_worker(
            project.project.id,
            "bootstrap",
            selection,
            project.project.tenant_workers,
        )
        if materialized is None:
            return False
        worker, binding_provenance, binding_circuit_key = materialized
        self._clear_log_state(f"project:{project.project.id}:worker:bootstrap")
        if route.mode == "fresh" and not self._probe_worker_readiness(
            project.project.id,
            "bootstrap",
            worker,
            container_runtime,
            project.project.execution_generation,
        ):
            return False
        if not self._preflight_retained_runner(project.project.id, "bootstrap", worker, route):
            return False
        claim = self.client.heartbeat(project.project.id, intent.id, worker.name)
        # bootstrap/explore 都通过 intent heartbeat 完成“认领 + 首次续租”。
        if claim.status_code in (403, 409):
            self._observe_decision(project.project.id, claim_failure_code(claim), task_type="bootstrap", worker_id=worker.id)
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "bootstrap claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            self._observe_decision(project.project.id, "claim_failed", task_type="bootstrap", worker_id=worker.id)
            LOG.warning(
                "bootstrap claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        task_run_id = self._create_task_run(
            project,
            "bootstrap",
            worker,
            intent_id=intent.id,
            scenario=scenario,
            container_runtime=container_runtime,
            selection=selection,
            binding_provenance=binding_provenance,
        )
        if task_run_id is None:
            self._best_effort_release(project.project.id, intent.id, worker.name, task_run_id=task_run_id)
            return False
        try:
            future = self.executor.submit(
                run_bootstrap_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                intent,
                worker,
                cancellation := TaskCancellation(generation=self._take_task_generation(project, task_run_id)),
                task_run_id,
                prompt_group=scenario.prompt_group,
                knowledge=scenario.knowledge,
                container_runtime=container_runtime,
                delivery_context=self._take_delivery_context(project, task_run_id),
                continuation_context=self._take_continuation_context(project, task_run_id),
                checkpoint_staging=self._execution_checkpoints_enabled_for(worker),
                candidate_outbox=self.candidate_capture_outbox,
            )
        except Exception:
            self._observe_decision(project.project.id, "submit_failed", task_type="bootstrap", worker_id=worker.id)
            LOG.exception("failed to submit bootstrap task project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            self._best_effort_release(project.project.id, intent.id, worker.name, task_run_id=task_run_id)
            self._finish_task_run(
                project.project.id,
                task_run_id,
                "failed",
                task_type="bootstrap",
                worker_name=worker.name,
                worker_type=worker.type,
                error_type="unknown",
                error_message="Dispatcher 提交任务失败",
                container_runtime=container_runtime,
                execution_lifecycle=cancellation.execution_lifecycle(task_finished=True),
                execution_container_ids=cancellation.execution_container_ids(),
            )
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "bootstrap",
            worker.name,
            worker.type,
            cancellation,
            container_runtime,
            task_run_id=task_run_id,
            intent_id=intent.id,
            binding_circuit_key=binding_circuit_key,
        )
        self._observe_decision(project.project.id, "dispatched", task_type="bootstrap", worker_id=worker.id)
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched bootstrap project=%s intent=%s worker=%s scenario=%s", project.project.id, intent.id, worker.name, scenario.key)
        return True

    def _explore_tried_workers(self, project_id: str, intent: Intent) -> set[str]:
        """返回应降级排序的 Worker；Verifier 额外避开发现该 Observation 的 Worker。"""
        tried = {entry.worker for entry in intent.worker_history if entry.worker}
        if intent.assessment_role != "verifier" or not intent.target_observation_id:
            return tried
        try:
            target = self.client.get_observation_verifier_target(
                project_id,
                intent.target_observation_id,
            )
        except Exception as exc:
            # Target 仍会在任务执行前强校验；这里只做尽力而为的 Worker 独立性排序。
            LOG.warning(
                "failed to read verifier target for worker preference project=%s intent=%s observation=%s error=%s",
                project_id,
                intent.id,
                intent.target_observation_id,
                exc,
            )
            return tried
        if target.discovery_worker:
            tried.add(target.discovery_worker)
        return tried

    def _dispatch_explore(self, project: ProjectDetail, export_yaml: str, intent: Intent) -> bool:
        if self._candidate_project_blocked(project.project.id):
            self._observe_decision(project.project.id, "cleanup_pending", task_type="explore")
            return False
        scenario = self._scenario_for_project(project)
        container_runtime = self._container_runtime_options(
            scenario,
            project.project.id,
        )
        route = self._delivery_route_workers(project, "explore", intent.id)
        if route is None:
            return False
        # Worker history 仅影响允许的候选；交付续跑不能因已试过原 Worker 而换人。
        tried = self._explore_tried_workers(project.project.id, intent)
        selection = self._select_worker(
            project.project.id,
            "explore",
            scenario,
            route.workers,
            tried_workers=tried,
        )
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:explore",
                logging.INFO,
                "no worker available for explore project=%s intent=%s scenario=%s blocked_busy=%s blocked_cooldown=%s blocked_rejected=%s blocked_disabled=%s",
                project.project.id,
                intent.id,
                scenario.key,
                selection.blocked_busy,
                selection.blocked_cooldown,
                selection.blocked_rejected,
                selection.blocked_disabled,
            )
            return False
        materialized = self._materialize_selected_worker(
            project.project.id,
            "explore",
            selection,
            project.project.tenant_workers,
        )
        if materialized is None:
            return False
        worker, binding_provenance, binding_circuit_key = materialized
        self._clear_log_state(f"project:{project.project.id}:worker:explore")
        if route.mode == "fresh" and not self._probe_worker_readiness(
            project.project.id,
            "explore",
            worker,
            container_runtime,
            project.project.execution_generation,
        ):
            return False
        if not self._preflight_retained_runner(project.project.id, "explore", worker, route):
            return False
        runtime_context = self._task_run_runtime_context(
            project,
            "explore",
            worker,
            intent.id,
            scenario=scenario,
            container_runtime=container_runtime,
            selection=selection,
            binding_provenance=binding_provenance,
        )
        admission = self.client.start_explore_attempt(
            project.project.id,
            intent.id,
            worker.name,
            worker_type=worker.type,
            prompt_group=scenario.prompt_group,
            execution_profile=self.config.container.execution_profile,
            runtime_context=runtime_context,
            # 派发快照是前提条件，不可用暂停后的新代次提升旧认领。
            expected_execution_generation=getattr(project.project, "execution_generation", 0),
        )
        if admission.status_code in (403, 409):
            code = claim_failure_code(admission)
            if code in {"claim_conflict", "claim_denied"}:
                code = "admission_denied"
            self._observe_decision(project.project.id, code, task_type="explore", worker_id=worker.id)
            detail = (
                admission.data.get("detail")
                if isinstance(admission.data, dict)
                else None
            )
            if detail == "explore_deadline_exceeded":
                convergence = self.client.advance_coverage_convergence(
                    project.project.id
                )
                if not convergence.ok:
                    LOG.warning(
                        "coverage convergence failed project=%s status=%s body=%s",
                        project.project.id,
                        convergence.status_code,
                        convergence.text,
                    )
            level = logging.INFO if admission.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "explore admission denied project=%s intent=%s worker=%s status=%s body=%s",
                project.project.id,
                intent.id,
                worker.name,
                admission.status_code,
                admission.text,
            )
            return False
        if not admission.ok:
            self._observe_decision(project.project.id, "admission_failed", task_type="explore", worker_id=worker.id)
            LOG.warning(
                "explore admission failed project=%s intent=%s worker=%s status=%s body=%s",
                project.project.id,
                intent.id,
                worker.name,
                admission.status_code,
                admission.text,
            )
            return False
        if not isinstance(admission.data, dict):
            self._observe_decision(project.project.id, "admission_failed", task_type="explore", worker_id=worker.id)
            LOG.warning(
                "explore admission returned empty body project=%s intent=%s worker=%s",
                project.project.id,
                intent.id,
                worker.name,
            )
            return False
        task_run_id = admission.data.get("id")
        if not isinstance(task_run_id, str) or not task_run_id:
            self._observe_decision(project.project.id, "admission_failed", task_type="explore", worker_id=worker.id)
            LOG.warning(
                "explore admission returned empty task id project=%s intent=%s worker=%s",
                project.project.id,
                intent.id,
                worker.name,
            )
            return False
        self._remember_task_generation(project, task_run_id, admission.data)
        self._record_target_scope_policy_snapshot(
            project.project.id,
            task_run_id,
            runtime_context,
        )
        try:
            future = self.executor.submit(
                run_explore_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                export_yaml,
                intent,
                worker,
                cancellation := TaskCancellation(generation=self._take_task_generation(project, task_run_id)),
                task_run_id,
                prompt_group=scenario.prompt_group,
                knowledge=scenario.knowledge,
                container_runtime=container_runtime,
                delivery_context=self._take_delivery_context(project, task_run_id),
                continuation_context=self._take_continuation_context(project, task_run_id),
                admission_context=self._take_admission_context(project, task_run_id),
                checkpoint_staging=self._execution_checkpoints_enabled_for(worker),
                candidate_outbox=self.candidate_capture_outbox,
            )
        except Exception:
            self._observe_decision(project.project.id, "submit_failed", task_type="explore", worker_id=worker.id)
            LOG.exception("failed to submit explore task project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            self._best_effort_release(project.project.id, intent.id, worker.name, task_run_id=task_run_id)
            self._finish_task_run(
                project.project.id,
                task_run_id,
                "failed",
                task_type="explore",
                worker_name=worker.name,
                worker_type=worker.type,
                error_type="unknown",
                error_message="Dispatcher 提交任务失败",
                container_runtime=container_runtime,
                execution_lifecycle=cancellation.execution_lifecycle(task_finished=True),
                execution_container_ids=cancellation.execution_container_ids(),
            )
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "explore",
            worker.name,
            worker.type,
            cancellation,
            container_runtime,
            task_run_id=task_run_id,
            intent_id=intent.id,
            binding_circuit_key=binding_circuit_key,
        )
        self._observe_decision(project.project.id, "dispatched", task_type="explore", worker_id=worker.id)
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched explore project=%s intent=%s worker=%s scenario=%s", project.project.id, intent.id, worker.name, scenario.key)
        return True

    def _reap_futures(self) -> None:
        pending = self._pending_task_run_finishes()
        done = [
            future for future, task in self.futures.items()
            if future.done() and (task.project_id, task.task_run_id) not in pending
        ]
        for future in done:
            task = self.futures[future]
            if task.task_run_id is None:
                self.futures.pop(future)
            cancellation = getattr(task, "cancellation", None)
            lifecycle = cancellation.execution_lifecycle(task_finished=True) if isinstance(cancellation, TaskCancellation) else None
            try:
                result = future.result()
                failure = result if isinstance(result, WorkerResultFailure) else None
                outcome = worker_failure_error_type(failure) if failure is not None else result
                # 同一 failure 随终态写入及现有 pending 重试，不能依赖先前 running PATCH 成功。
                status = self._task_run_status_for_outcome(outcome)
                error_type = self._task_run_error_type_for_outcome(outcome, status)
                error_message = failure.message if failure is not None else self._task_run_error_message_for_error_type(error_type)
                if failure is None and error_type in {"worker_unhealthy", "healthcheck_timeout", "api_unreachable", "api_incompatible", "config_mismatch"}:
                    error_message = None
                self._finish_task_run(
                    task.project_id,
                    task.task_run_id,
                    status,
                    task_type=task.task_type,
                    worker_name=task.worker_name,
                    worker_type=task.worker_type,
                    error_type=error_type,
                    error_message=error_message,
                    exit_code=failure.exit_code if failure is not None else None,
                    worker_failure=failure.details() if failure is not None else None,
                    container_runtime=task.container_runtime,
                    execution_lifecycle=lifecycle,
                    execution_container_ids=cancellation.execution_container_ids() if isinstance(cancellation, TaskCancellation) else None,
                )
                if outcome == "cancelled":
                    LOG.info(
                        "task cancelled project=%s task=%s worker=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                    )
                elif outcome != "success":
                    LOG.warning(
                        "task finished project=%s task=%s worker=%s outcome=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        outcome,
                    )
                self._clear_project_log_state(task.project_id)
                rejection_key = (task.project_id, task.task_type, task.worker_name)
                if outcome == "success":
                    self._clear_worker_cooldown(task.worker_name)
                    self._clear_worker_rejected(rejection_key)
                    if task.binding_circuit_key is not None:
                        self._clear_binding_auth_circuit(task.binding_circuit_key)
                if (
                    outcome == "provider_auth"
                    and task.binding_circuit_key is not None
                ):
                    retry_after_seconds, attempt = self._mark_binding_auth_circuit(
                        task.binding_circuit_key
                    )
                    LOG.info(
                        "execution binding auth circuit opened model=%s credential_version=%s retry_after=%.0fs attempt=%s",
                        task.binding_circuit_key.model_id,
                        task.binding_circuit_key.credential_version_id,
                        retry_after_seconds,
                        attempt,
                    )
                cooldown_error_type = self._worker_cooldown_error_type(error_type)
                if (
                    outcome != "success"
                    and not (outcome == "provider_auth" and task.binding_circuit_key is not None)
                    and cooldown_error_type is not None
                ):
                    # provider_auth 按完整模型绑定熔断；限流只冷却 Worker，不重跑目标。
                    retry_after_seconds, attempt = self._mark_worker_cooldown(task.worker_name, cooldown_error_type)
                    LOG.info(
                        "worker marked cooldown worker=%s error_type=%s retry_after=%.0fs attempt=%s",
                        task.worker_name,
                        cooldown_error_type,
                        retry_after_seconds,
                        attempt,
                    )
                if outcome != "success" and error_type in REJECTION_RETRY_ERROR_TYPES:
                    # 任务/目标相关拒绝只屏蔽当前 project+task+worker，避免误伤其他项目。
                    retry_after_seconds, attempt = self._mark_worker_rejected(rejection_key, error_type)
                    LOG.info(
                        "worker marked rejected project=%s task=%s worker=%s error_type=%s retry_after=%.0fs attempt=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        error_type,
                        retry_after_seconds,
                        attempt,
                    )
                if outcome == "success" and task.task_type == "reason":
                    assert task.fact_count is not None
                    assert task.hint_count is not None
                    assert task.open_intent_count is not None
                    assert task.settled_intent_count is not None
                    fact_count = task.fact_count
                    hint_count = task.hint_count
                    open_intent_count = task.open_intent_count
                    settled_intent_count = task.settled_intent_count
                    coverage_cell_count = task.coverage_checkpoint_cell_count or 0
                    self.reason_checkpoints[task.project_id] = ReasonCheckpoint(
                        # 保留调度前快照，使 Reason 写入和随后结算的变化都能触发下一轮。
                        fact_count=fact_count,
                        hint_count=hint_count,
                        open_intent_count=open_intent_count,
                        coverage_checkpoint_cell_count=coverage_cell_count,
                        coverage_stop_reason=task.coverage_stop_reason,
                        settled_intent_count=settled_intent_count,
                        execution_generation=task.execution_generation,
                    )
                    LOG.debug(
                        "reason checkpoint updated project=%s facts=%s hints=%s open_intents=%s settled_intents=%s generation=%s coverage_cells=%s coverage_stop=%s",
                        task.project_id,
                        fact_count,
                        hint_count,
                        open_intent_count,
                        settled_intent_count,
                        task.execution_generation,
                        coverage_cell_count,
                        task.coverage_stop_reason,
                    )
            except Exception as exc:
                error_type = getattr(exc, "error_type", None)
                if error_type not in STANDARD_ERROR_TYPES:
                    error_type = "transport_error" if isinstance(exc, requests.RequestException) else "unknown"
                self._finish_task_run(
                    task.project_id,
                    task.task_run_id,
                    "failed",
                    task_type=task.task_type,
                    worker_name=task.worker_name,
                    worker_type=task.worker_type,
                    error_type=error_type,
                    error_message=ERROR_MESSAGES[error_type],
                    container_runtime=task.container_runtime,
                    execution_lifecycle=lifecycle,
                    execution_container_ids=cancellation.execution_container_ids() if isinstance(cancellation, TaskCancellation) else None,
                )
                LOG.exception("task crashed project=%s task=%s worker=%s", task.project_id, task.task_type, task.worker_name)
            finally:
                # 只有完整任务 future 结束才释放本地门槛，阶段切换和取消请求均不能提前收口。
                notify_browser_task(
                    getattr(self, "container_manager", None),
                    task.project_id,
                    task.task_run_id,
                    local_finished=True,
                )

    def _take_delivery_context(self, project, task_run_id):
        return getattr(self, "_task_delivery_contexts", {}).pop((project.project.id, task_run_id), None)

    def _take_continuation_context(self, project, task_run_id):
        return getattr(self, "_task_continuation_contexts", {}).pop((project.project.id, task_run_id), None)

    def _take_admission_context(self, project, task_run_id):
        return getattr(self, "_task_admission_contexts", {}).pop(
            (project.project.id, task_run_id), None
        )

    def _remember_task_generation(self, project, task_run_id, data) -> None:
        # 只转交 admission 返回的权威来源；不从 Worker 输出或本地失败推导重试。
        context = data.get("runtime_context")
        if isinstance(context, dict):
            if "delivery_retry" in context:
                delivery_contexts = getattr(self, "_task_delivery_contexts", None)
                if delivery_contexts is None:
                    delivery_contexts = self._task_delivery_contexts = {}
                delivery_contexts[(project.project.id, task_run_id)] = context
            if "continuation" in context:
                continuations = getattr(self, "_task_continuation_contexts", None)
                if continuations is None:
                    continuations = self._task_continuation_contexts = {}
                continuations[(project.project.id, task_run_id)] = {
                    **context["continuation"], "session_id": data.get("session_id"),
                }
            admission_context = {
                key: context[key]
                for key in (
                    "structured_intent_binding",
                    "structured_explore_binding",
                )
                if key in context
            }
            if admission_context:
                admission_contexts = getattr(self, "_task_admission_contexts", None)
                if admission_contexts is None:
                    admission_contexts = self._task_admission_contexts = {}
                admission_contexts[(project.project.id, task_run_id)] = admission_context
        generations = getattr(self, "_task_generations", None)
        if generations is None:
            generations = self._task_generations = {}
        generation = data.get("execution_generation")
        generations[(project.project.id, task_run_id)] = (
            generation if type(generation) is int else getattr(project.project, "execution_generation", 0)
        )

    def _take_task_generation(self, project, task_run_id) -> int:
        return getattr(self, "_task_generations", {}).pop(
            (project.project.id, task_run_id), getattr(project.project, "execution_generation", 0),
        )

    def _create_task_run(
        self,
        project: ProjectDetail,
        task_type: str,
        worker: WorkerConfig,
        *,
        intent_id: str | None,
        scenario: Scenario,
        container_runtime: ContainerRuntimeOptions,
        selection: WorkerSelection | None = None,
        binding_provenance: dict[str, object] | None = None,
    ) -> str | None:
        project_id = project.project.id
        runtime_context = self._task_run_runtime_context(
            project,
            task_type,
            worker,
            intent_id,
            scenario=scenario,
            container_runtime=container_runtime,
            selection=selection,
            binding_provenance=binding_provenance,
        )
        response = self.client.create_task_run(
            project_id,
            intent_id=intent_id,
            task_type=task_type,
            worker_name=worker.name,
            worker_type=worker.type,
            prompt_group=scenario.prompt_group,
            execution_profile=self.config.container.execution_profile,
            runtime_context=runtime_context,
            # 派发快照是前提条件，不可用暂停后的新代次提升旧认领。
            expected_execution_generation=getattr(project.project, "execution_generation", 0),
        )
        if not response.ok:
            self._observe_decision(project_id, claim_failure_code(response), task_type=task_type, worker_id=worker.id)
        if response.status_code == 403:
            LOG.info(
                "task run create skipped because project is inactive project=%s task=%s worker=%s",
                project_id,
                task_type,
                worker.name,
            )
            return None
        if not response.ok:
            LOG.warning(
                "task run create failed project=%s task=%s worker=%s status=%s body=%s",
                project_id,
                task_type,
                worker.name,
                response.status_code,
                response.text,
            )
            return None
        if not isinstance(response.data, dict):
            LOG.warning("task run create returned empty body project=%s task=%s worker=%s", project_id, task_type, worker.name)
            return None
        task_run_id = response.data.get("id")
        if not isinstance(task_run_id, str) or not task_run_id:
            LOG.warning("task run create returned empty id project=%s task=%s worker=%s", project_id, task_type, worker.name)
            return None
        self._remember_task_generation(project, task_run_id, response.data)
        # task_run 创建后立即补一次 target_scope policy，确保审计有 Server 侧快照。
        self._record_target_scope_policy_snapshot(project_id, task_run_id, runtime_context)
        return task_run_id

    def _record_target_scope_policy_snapshot(
        self,
        project_id: str,
        task_run_id: str,
        runtime_context: dict[str, object],
    ) -> None:
        target_scope = runtime_context.get("target_scope")
        if not isinstance(target_scope, dict) or not isinstance(target_scope.get("policy"), dict):
            return
        response = self.client.update_task_run(
            project_id,
            task_run_id,
            status="running",
            runtime_context={"target_scope": target_scope},
        )
        if response.ok:
            return
        LOG.debug(
            "target scope policy snapshot update failed project=%s task_run=%s status=%s body=%s",
            project_id,
            task_run_id,
            response.status_code,
            response.text,
        )
