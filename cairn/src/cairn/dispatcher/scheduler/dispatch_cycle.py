"""调度循环中的项目排序、入口判断和 reason 触发。"""

from __future__ import annotations

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL

from .common import *
from .decisions import DispatchDecisionsMixin


def _timestamp_is_due(value: object) -> bool:
    """缺失或失效时间交给 Server claim 再做最终并发裁决。"""
    if not isinstance(value, str) or not value.strip():
        return True
    try:
        due_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    return due_at <= datetime.now(timezone.utc)


def _report_retry_is_due(state: dict[str, object]) -> bool:
    """失败报告仅在 Server 给出的退避时间到期后重新派发。"""
    if state.get("status") != "failed":
        return True
    last_error = state.get("last_error")
    if isinstance(last_error, str) and last_error.startswith(
        (
            "assessment_report_execution_plan_",
            "assessment_report_prompt_digest_",
        )
    ):
        # 冻结配置损坏需要用户重新发起请求，自动重试不会改变结果。
        return False
    return _timestamp_is_due(state.get("next_attempt_at"))


def _intent_is_open(intent: Intent) -> bool:
    """开放 Intent 必须尚未连接 Fact，也未被完成或放弃。"""
    return intent.to is None and intent.concluded_at is None


def _goal_driven_execution(project: ProjectDetail) -> bool:
    view = project.project.assessment_execution
    return (
        view.get("goal_driven") is True
        or project.project.assessment_protocol == GOAL_DRIVEN_PROTOCOL
    )


def _intent_priority(intent: Intent) -> int:
    value = getattr(intent, "priority", 1)
    return value if type(value) is int and 0 <= value <= 2 else 1


class DispatchCycleMixin(DispatchDecisionsMixin):
    """调度循环中的项目排序、入口判断和 reason 触发。"""

    def _dispatch_available(self, summaries: list[ProjectSummary]) -> None:
        self._begin_project_decisions(summaries)
        # Tenant Worker 来自项目 durable binding，静态 worker pool 为空时仍需进入项目级调度。
        if len(self.futures) >= self.config.runtime.max_workers:
            for summary in summaries:
                if summary.status in {"active", "completed", "stopped"}:
                    self._observe_decision(summary.id, "global_capacity")
            self._log_changed(
                "dispatch/global",
                logging.INFO,
                "skip dispatch because max_workers reached running_tasks=%s",
                len(self.futures),
            )
            return
        active = [summary for summary in summaries if summary.status == "active"]
        if not active:
            self._log_changed("dispatch/global", logging.INFO, "skip active dispatch because no active projects")
            self._dispatch_completed_reports(summaries)
            return

        running_projects = self._ordered_projects(
            [summary for summary in active if summary.id in self.runtime_project_ids]
        )
        idle_projects = self._ordered_projects(
            [summary for summary in active if summary.id not in self.runtime_project_ids]
        )

        # 先尝试让 idle 项目进入运行集，再给已有运行项目补 explore/reason，避免少数项目独占。
        idle_attempted: set[str] = set()
        dispatched = True
        while dispatched and len(self.futures) < self.config.runtime.max_workers:
            dispatched = False
            if self._try_dispatch_idle_project(idle_projects, active, idle_attempted):
                dispatched = True
                continue
            for summary in running_projects:
                if self._try_dispatch_project(summary):
                    dispatched = True
                    if len(self.futures) >= self.config.runtime.max_workers:
                        return
            if dispatched:
                continue
            if self._running_project_count(active) >= self.config.runtime.max_running_projects:
                self._log_changed(
                    "dispatch/idle-limit",
                    logging.INFO,
                    "skip idle project dispatch because max_running_projects reached running_projects=%s",
                    self._running_project_count(active),
                )
                self._dispatch_completed_reports(summaries)
                return
        # 报告只使用 active assessment 派发完成后的剩余全局容量。
        self._dispatch_completed_reports(summaries)

    def _dispatch_completed_reports(self, summaries: list[ProjectSummary]) -> None:
        for summary in self._ordered_projects(
            [item for item in summaries if item.status in {"completed", "stopped"}]
        ):
            if len(self.futures) >= self.config.runtime.max_workers:
                return
            project_tasks = [
                task for task in self.futures.values() if task.project_id == summary.id
            ]
            if any(task.task_type == "report" for task in project_tasks):
                continue
            if summary.status == "stopped":
                # 已取消但尚未 reap 的评估 future 仍可能写回，必须先等待其退出。
                if any(task.task_type != "report" for task in project_tasks):
                    continue
                container_name = self.container_manager.container_name(summary.id)
                if container_name in self._cleanup_pending:
                    continue
            state = self.client.get_assessment_report_finalization(summary.id)
            if state.ok and isinstance(state.data, dict):
                # 未显式请求或 legacy pending/failed 不进入报告调度。
                if not state.data.get("requested_at"):
                    continue
                status = state.data.get("status")
                if status == "ready":
                    continue
                if status == "running" and not _timestamp_is_due(
                    state.data.get("lease_expires_at")
                ):
                    continue
                # 交付链由同一 Server route 决定退避，并在选择前留下明确的阻断观测。
                if state.data.get("delivery_retry_route") is None and not _report_retry_is_due(state.data):
                    continue
            else:
                # 404 表示项目尚未请求报告，其他读失败也留待下轮重试。
                continue
            self._try_dispatch_completed_report(summary.id)

    def _try_dispatch_completed_report(self, project_id: str) -> bool:
        if self._candidate_project_blocked(project_id):
            self._observe_decision(project_id, "cleanup_pending", task_type="report")
            return False
        if self.report_finish_outbox.has_project(project_id):
            # 未决 immutable finish 只能继续交付，不能重新进入模型执行。
            self._observe_decision(project_id, "delivery_pending", task_type="report")
            self._log_changed(
                f"project:{project_id}:report_finish_pending",
                logging.WARNING,
                "skip report dispatch because durable finish is pending project=%s",
                project_id,
            )
            return False
        project = self.client.get_project(project_id)
        if not self._execution_supported(project):
            return False
        try:
            export_yaml = self.client.export_project(project_id)
        except requests.RequestException:
            self._observe_decision(project_id, "admission_failed", task_type="report")
            return False
        if not isinstance(export_yaml, str) or not export_yaml.strip():
            self._observe_decision(project_id, "admission_failed", task_type="report")
            self._log_changed(
                f"project:{project_id}:report_graph_invalid",
                logging.WARNING,
                "skip report dispatch because exported graph is empty project=%s",
                project_id,
            )
            return False
        scenario = self._scenario_for_project(project)
        container_runtime = self._container_runtime_options(scenario, project_id)
        try:
            state = self.client.get_assessment_report_finalization(project_id)
        except requests.RequestException:
            self._observe_decision(project_id, "admission_failed", task_type="report")
            return False
        if not state.ok or not isinstance(state.data, dict):
            self._observe_decision(project_id, "admission_failed", task_type="report")
            return False
        route = self._delivery_route_workers(
            project,
            "report",
            report_state=state.data,
        )
        if route is None:
            return False
        selection_task_type = "reason"
        selection = self._select_worker(
            project_id,
            selection_task_type,
            scenario,
            route.workers,
        )
        if selection.worker is None:
            selection_task_type = "bootstrap"
            selection = self._select_worker(
                project_id,
                selection_task_type,
                scenario,
                route.workers,
            )
        if selection.worker is None:
            return False
        materialized = self._materialize_selected_worker(
            project_id,
            "report",
            selection,
            project.project.tenant_workers,
        )
        if materialized is None:
            return False
        worker, _binding_provenance, binding_circuit_key = materialized
        try:
            future = self.executor.submit(
                run_post_completion_report,
                self.config,
                self.client,
                self.container_manager,
                project_id,
                export_yaml,
                worker,
                prompt_group=scenario.prompt_group,
                knowledge=scenario.knowledge,
                container_runtime=container_runtime,
                durable_request=True,
                finish_outbox=self.report_finish_outbox,
                candidate_outbox=self.candidate_capture_outbox,
            )
        except Exception:
            LOG.exception(
                "failed to submit durable report project=%s worker=%s",
                project_id,
                worker.name,
            )
            return False
        self.futures[future] = RunningTask(
            project_id,
            "report",
            worker.name,
            worker.type,
            TaskCancellation(),
            container_runtime,
            task_run_id=None,
            binding_circuit_key=binding_circuit_key,
        )
        # stopped 项目可能已完成上一轮容器清理；报告重新使用容器后需允许后续再次回收。
        self._inactive_cleanup_done.pop(project_id, None)
        LOG.info(
            "dispatched durable report project=%s worker=%s scenario=%s",
            project_id,
            worker.name,
            scenario.key,
        )
        return True

    def _try_dispatch_idle_project(
        self,
        idle_projects: list[ProjectSummary],
        active_projects: list[ProjectSummary],
        attempted_project_ids: set[str],
    ) -> bool:
        if self._running_project_count(active_projects) >= self.config.runtime.max_running_projects:
            for summary in idle_projects:
                if summary.id not in self.runtime_project_ids:
                    self._observe_decision(summary.id, "project_limit")
            return False
        for summary in idle_projects:
            if summary.id in attempted_project_ids or summary.id in self.runtime_project_ids:
                continue
            attempted_project_ids.add(summary.id)
            if self._running_project_count(active_projects) >= self.config.runtime.max_running_projects:
                return False
            if self._try_dispatch_project(summary):
                return True
        return False

    def _ordered_projects(self, summaries: list[ProjectSummary]) -> list[ProjectSummary]:
        if not summaries:
            return []
        ids = [summary.id for summary in summaries]
        ids.sort()
        # 轮转游标让多个 active 项目在不同循环中获得公平起点。
        offset = self.project_cursor % len(ids)
        ordered_ids = ids[offset:] + ids[:offset]
        by_id = {summary.id: summary for summary in summaries}
        self.project_cursor += 1
        return [by_id[project_id] for project_id in ordered_ids]

    def _execution_supported(self, project: ProjectDetail) -> bool:
        view = project.project.assessment_execution
        supported = (
            view.get("version") == 2 and view.get("executable") is True
            and view.get("execution_contract_version") == 1
            and view.get("execution_engine") == "pi"
        )
        if not supported:
            self._observe_decision(project.project.id, "execution_unsupported")
            self._log_changed(
                f"project:{project.project.id}:execution_contract", logging.WARNING,
                "project execution rejected project=%s reason=unsupported_frozen_execution", project.project.id,
            )
        return supported

    def _try_dispatch_project(self, summary: ProjectSummary) -> bool:
        skip_scope = f"project:{summary.id}:skip"
        if self._candidate_project_blocked(summary.id):
            self._observe_decision(summary.id, "cleanup_pending")
            self._log_changed(
                f"{skip_scope}:candidate_recovery",
                logging.WARNING,
                "skip project=%s because candidate recovery is unresolved",
                summary.id,
            )
            return False
        container_name = self.container_manager.container_name(summary.id)
        if container_name in self._cleanup_pending or any(key[0] == summary.id for key in getattr(self, "_pause_cleanup_confirmations", {})):
            self._observe_decision(summary.id, "cleanup_pending")
            self._log_changed(
                f"{skip_scope}:cleanup_pending",
                logging.DEBUG,
                "skip project=%s because container cleanup is still pending container=%s",
                summary.id,
                container_name,
            )
            return False
        if self._project_running_task_count(summary.id) >= self.config.runtime.max_project_workers:
            self._observe_decision(summary.id, "project_capacity")
            self._log_changed(
                f"{skip_scope}:max_project_workers",
                logging.INFO,
                "skip project=%s because max_project_workers reached running_tasks=%s",
                summary.id,
                self._project_running_task_summary(summary.id),
            )
            return False

        project = self.client.get_project(summary.id)
        generations = getattr(self, "_project_generations", {})
        generations[summary.id] = getattr(project.project, "execution_generation", 0)
        self._project_generations = generations
        if not self._execution_supported(project):
            return False
        if project.project.status != "active":
            self._observe_decision(summary.id, "project_inactive")
            self._log_changed(
                f"{skip_scope}:status",
                logging.INFO,
                "skip project=%s because status=%s",
                summary.id,
                project.project.status,
            )
            return False
        # 每轮使用 Server 新鲜裁决，不从摘要推断授权，也不缓存上轮的 409。
        if project.project.authorization_block_reason is not None:
            self._observe_decision(summary.id, project.project.authorization_block_reason)
            return False
        if self._is_initial_project(project):
            # 新项目只有 origin/goal 时先跑 bootstrap，建立第一条事实链路。
            if project.project.reason is not None:
                return False
            return self._dispatch_initial_project(project)
        running_intent_ids = self._project_running_explore_intents(summary.id)
        # 构建 intent_id → concluded_at 映射，用于 depends_on 依赖检查。
        concluded_map = {
            intent.id: intent.concluded_at for intent in project.intents
        }
        unclaimed_intents = [
            intent
            for intent in project.intents
            if _intent_is_open(intent)
            and intent.worker is None
            and intent.id not in running_intent_ids
            and not self._is_bootstrap_intent(intent)
            and (
                not intent.depends_on
                or concluded_map.get(intent.depends_on) is not None
            )
        ]
        if running_intent_ids and not unclaimed_intents:
            self._log_changed(
                f"{skip_scope}:explore_running",
                logging.DEBUG,
                "skip explore project=%s because all unclaimed intents are already running locally intents=%s",
                summary.id,
                sorted(running_intent_ids),
            )
        verifier_intents = [item for item in unclaimed_intents if item.assessment_role == "verifier"]
        if verifier_intents:
            # 必要复核优先于新增 discovery，避免新工作持续延后复核。
            target = min(verifier_intents, key=lambda item: (item.created_at, item.id))
            return self._dispatch_explore(project, self.client.export_project(summary.id), target)
        view = project.project.assessment_execution
        goal_driven = _goal_driven_execution(project)
        if not goal_driven and view.get("phase") == "depth_review":
            if project.project.reason is not None:
                return False
            return self._dispatch_reason(project, self.client.export_project(summary.id), "depth_review")
        if unclaimed_intents:
            if goal_driven:
                # 新路径严格按业务优先级降序，再按创建时间和 ID 做 FIFO。
                target = min(
                    unclaimed_intents,
                    key=lambda item: (
                        -_intent_priority(item),
                        item.created_at,
                        item.id,
                    ),
                )
            else:
                selected = [
                    item for item in unclaimed_intents if item.depth_review_fact_id
                ]
                target = max(
                    selected or unclaimed_intents,
                    key=lambda item: item.created_at,
                )
            export_yaml = self.client.export_project(summary.id)
            return self._dispatch_explore(project, export_yaml, target)
        if project.project.reason is not None:
            self._log_changed(
                f"{skip_scope}:reason_claimed",
                logging.DEBUG,
                "skip reason project=%s because reason is already claimed by %s",
                summary.id,
                project.project.reason.worker,
            )
            return False
        reason_trigger = self._reason_trigger(project)
        if reason_trigger is None:
            self._observe_decision(summary.id, "no_work")
            self._log_changed(
                f"{skip_scope}:graph_unchanged",
                logging.DEBUG,
                "skip reason project=%s because reason state unchanged facts=%s hints=%s open_intents=%s intents=%s",
                summary.id,
                len(project.facts),
                len(project.hints),
                self._project_open_intent_count(project),
                len(project.intents),
            )
            return False
        export_yaml = self.client.export_project(summary.id)
        return self._dispatch_reason(project, export_yaml, reason_trigger)

    def _dispatch_initial_project(self, project: ProjectDetail) -> bool:
        intent = self._get_bootstrap_intent(project)
        if intent is None:
            intent = self._create_bootstrap_intent(project.project.id)
            if intent is None:
                return False
        if self._project_has_running_bootstrap(project.project.id):
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_running",
                logging.DEBUG,
                "skip bootstrap project=%s because bootstrap task is already running locally",
                project.project.id,
            )
            return False
        if intent.worker is not None:
            self._observe_decision(project.project.id, "already_claimed", task_type="bootstrap")
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_claimed",
                logging.DEBUG,
                "skip bootstrap project=%s because bootstrap intent=%s is already claimed by %s",
                project.project.id,
                intent.id,
                intent.worker,
            )
            return False
        return self._dispatch_bootstrap(project, intent)

    def _project_running_task_count(self, project_id: str) -> int:
        return sum(1 for task in self.futures.values() if task.project_id == project_id)

    def _project_running_task_summary(self, project_id: str) -> list[str]:
        summary: list[str] = []
        for task in self.futures.values():
            if task.project_id != project_id:
                continue
            if task.intent_id is None:
                summary.append(f"{task.task_type}:{task.worker_name}")
            else:
                summary.append(f"{task.task_type}:{task.worker_name}:{task.intent_id}")
        summary.sort()
        return summary

    def _project_has_running_bootstrap(self, project_id: str) -> bool:
        return any(task.project_id == project_id and task.task_type == "bootstrap" for task in self.futures.values())

    def _project_running_explore_intents(self, project_id: str) -> set[str]:
        return {
            task.intent_id
            for task in self.futures.values()
            if task.project_id == project_id and task.task_type == "explore" and task.intent_id is not None
        }

    def _running_project_count(self, summaries: list[ProjectSummary]) -> int:
        active_ids = {summary.id for summary in summaries if summary.status == "active"}
        return len(self.runtime_project_ids & active_ids)

    def _project_open_intent_count(self, project: ProjectDetail) -> int:
        return sum(1 for intent in project.intents if _intent_is_open(intent))

    def _project_settled_intent_count(self, project: ProjectDetail) -> int:
        return len(project.intents) - self._project_open_intent_count(project)

    def _is_bootstrap_intent(self, intent: Intent) -> bool:
        return (
            intent.description == BOOTSTRAP_INTENT_DESCRIPTION
            and intent.creator == BOOTSTRAP_INTENT_CREATOR
            and intent.from_ == ["origin"]
            and intent.to is None
        )

    def _get_bootstrap_intent(self, project: ProjectDetail) -> Intent | None:
        intents = [
            intent
            for intent in project.intents
            if self._is_bootstrap_intent(intent) and _intent_is_open(intent)
        ]
        if not intents:
            return None
        if len(intents) > 1:
            LOG.warning("project has multiple bootstrap intents project=%s intents=%s", project.project.id, [intent.id for intent in intents])
        # 优先选择未被认领的 bootstrap intent，避免重复创建。
        intents.sort(key=lambda intent: (intent.worker is not None, intent.created_at, intent.id))
        return intents[0]

    def _is_initial_project(self, project: ProjectDetail) -> bool:
        fact_ids = {fact.id for fact in project.facts}
        if fact_ids != {"origin", "goal"} or len(project.facts) != 2:
            return False
        if not project.intents:
            return True
        return all(self._is_bootstrap_intent(intent) for intent in project.intents)

    def _create_bootstrap_intent(self, project_id: str) -> Intent | None:
        response = self.client.create_intent(
            project_id,
            ["origin"],
            BOOTSTRAP_INTENT_DESCRIPTION,
            BOOTSTRAP_INTENT_CREATOR,
        )
        if response.status_code == 403:
            LOG.info("project became inactive before bootstrap intent create project=%s", project_id)
            return None
        if not response.ok:
            LOG.warning(
                "bootstrap intent write failed project=%s status=%s body=%s",
                project_id,
                response.status_code,
                response.text,
            )
            return None
        if not isinstance(response.data, dict):
            LOG.warning("bootstrap intent create returned empty body project=%s", project_id)
            return None
        intent = Intent.model_validate(response.data)
        LOG.info("created bootstrap intent project=%s intent=%s", project_id, intent.id)
        return intent

    def _reason_trigger(self, project: ProjectDetail) -> str | None:
        view = project.project.assessment_execution
        goal_driven = _goal_driven_execution(project)
        if not goal_driven and view.get("phase") in {"depth_review", "finalizing"}:
            return str(view["phase"])
        open_intent_count = self._project_open_intent_count(project)
        settled_intent_count = self._project_settled_intent_count(project)
        checkpoint = self.reason_checkpoints.get(project.project.id)
        if checkpoint is None:
            return "initial"
        changes: list[str] = []
        if project.project.execution_generation != checkpoint.execution_generation:
            changes.append(
                "execution_generation:"
                f"{checkpoint.execution_generation}->{project.project.execution_generation}"
            )
        if len(project.facts) > checkpoint.fact_count:
            changes.append(f"facts:{checkpoint.fact_count}->{len(project.facts)}")
        if len(project.hints) > checkpoint.hint_count:
            changes.append(f"hints:{checkpoint.hint_count}->{len(project.hints)}")
        if checkpoint.open_intent_count > 0 and open_intent_count == 0:
            # 所有开放 intent 被消化后触发 reason，决定是否完成或继续分解。
            changes.append(f"open_intents:{checkpoint.open_intent_count}->0")
        elif (
            open_intent_count == 0
            and settled_intent_count > checkpoint.settled_intent_count
        ):
            # Reason 从空队列生成的整批 Intent 可能在下一次轮询前全部失败。
            changes.append(
                "settled_intents:"
                f"{checkpoint.settled_intent_count}->{settled_intent_count}"
            )
        if not goal_driven and view.get("enabled") is True:
            cell_count = project.project.coverage_checkpoint_cell_count
            if cell_count != checkpoint.coverage_checkpoint_cell_count:
                changes.append(
                    "coverage_cells:"
                    f"{checkpoint.coverage_checkpoint_cell_count}->{cell_count}"
                )
            stop_reason = project.project.coverage_stop_reason
            if stop_reason != checkpoint.coverage_stop_reason:
                previous = checkpoint.coverage_stop_reason or "none"
                current = stop_reason or "none"
                changes.append(f"coverage_stop:{previous}->{current}")
        if not changes:
            return None
        return ",".join(changes)
