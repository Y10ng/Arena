"""四阶段共享的原会话交付约束；不拥有重试额度或来源选择权。"""
from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math

from cairn.dispatcher.runtime.pause_clock import PauseClock

from cairn.dispatcher.worker_protocol.candidate_authority import (
    candidate_authority_sha256,
)
from cairn.dispatcher.worker_protocol.submission import build_submit_result_schema


_DELIVERY_FIELDS = frozenset(
    {
        "session_id",
        "phase",
        "deadline_at",
        "container_id",
        "model_binding_sha256",
        "result_schema_sha256",
        "binding",
        "inputs",
    }
)


class DeliveryUnavailable(Exception):
    """原交付上下文不可证明时明确阻断，禁止重新探索。"""


def model_binding_digest(worker) -> str:
    serialized = json.dumps(worker.model_binding.model_dump(mode="json"), sort_keys=True)
    return sha256(serialized.encode()).hexdigest()


def result_schema_digest(phase, binding, inputs=None) -> str:
    """冻结实际交给 Pi 的 submit_result Schema 身份。"""
    schema = build_submit_result_schema(phase, binding=binding, inputs=inputs)
    return candidate_authority_sha256(schema)


_RESEARCH_PHASES = {"bootstrap_execute", "reason_execute", "explore_execute", "verifier_execute"}
_RESEARCH_DELIVERY_FIELDS = _DELIVERY_FIELDS | {"authorization_expires_at", "paused_seconds"}


def _timestamp(value):
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("delivery_timestamp_timezone_required")
    return timestamp


def project_paused_seconds(control, *, now=None):
    """与 Server 的累计暂停记录同源，登记时也包括尚未关闭的暂停窗口。"""
    seconds = control.get("paused_seconds", 0)
    if type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("delivery_pause_clock_invalid")
    if control.get("paused_at"):
        seconds += max(0.0, ((now or datetime.now(timezone.utc)) - _timestamp(control["paused_at"])).total_seconds())
    return float(seconds)


def delivery_execution_deadline(snapshot, *, paused_seconds=None):
    """只补回执行预算中的暂停；绝对授权期限不参与平移。"""
    deadline = _timestamp(snapshot["deadline_at"])
    if paused_seconds is not None:
        baseline = snapshot["paused_seconds"]
        if type(baseline) not in {int, float} or not math.isfinite(baseline) or baseline < 0:
            raise ValueError("delivery_pause_clock_invalid")
        deadline += timedelta(seconds=max(0.0, paused_seconds - baseline))
    return deadline


class ResearchDeliveryBudget:
    """持久快照用项目暂停基线，同一 TaskRun 的准备等待用共享物理时钟增量。"""

    def __init__(self, clock: PauseClock):
        self.clock = clock

    def bind(self, project):
        self._local_baseline = self.clock.paused_seconds
        self._project_baseline = project_paused_seconds(project.execution_control)
        self.authorization_expires_at = project.authorization_expires_at

    @property
    def paused_seconds(self):
        return self._project_baseline + self.clock.paused_seconds - self._local_baseline


def remaining_delivery_seconds(snapshot, *, budget=None):
    """准备 I/O 消耗执行预算，但热暂停不消耗；授权始终按真实时间检查。"""
    try:
        now = datetime.now(timezone.utc)
        deadline = delivery_execution_deadline(
            snapshot, paused_seconds=budget.paused_seconds if budget is not None else None,
        )
        if snapshot["phase"] in _RESEARCH_PHASES:
            for expiry in (snapshot["authorization_expires_at"], budget.authorization_expires_at):
                if expiry is not None:
                    deadline = min(deadline, _timestamp(expiry))
        remaining = int((deadline - now).total_seconds())
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise DeliveryUnavailable from exc
    if remaining <= 0:
        raise DeliveryUnavailable
    return remaining


def resume_delivery(
    container_manager,
    project_id,
    worker,
    context,
    phase: str | Collection[str],
    *, client=None, budget=None,
):
    snapshot = context.get("execution_delivery")
    if not context.get("delivery_retry") or not isinstance(snapshot, dict):
        raise DeliveryUnavailable
    allowed_phases = {phase} if isinstance(phase, str) else set(phase)
    frozen_phase = snapshot.get("phase")
    if not isinstance(frozen_phase, str) or frozen_phase not in allowed_phases:
        raise DeliveryUnavailable
    fields = _RESEARCH_DELIVERY_FIELDS if frozen_phase in _RESEARCH_PHASES else _DELIVERY_FIELDS
    if set(snapshot) != fields:
        raise DeliveryUnavailable
    try:
        expected_schema_sha256 = result_schema_digest(
            frozen_phase,
            snapshot["binding"],
            snapshot["inputs"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DeliveryUnavailable from exc
    if (
        snapshot.get("model_binding_sha256") != model_binding_digest(worker)
        or snapshot.get("result_schema_sha256") != expected_schema_sha256
    ):
        raise DeliveryUnavailable
    try:
        if frozen_phase in _RESEARCH_PHASES:
            budget.bind(client.get_project(project_id).project)
        remaining_delivery_seconds(snapshot, budget=budget)
        if frozen_phase == "report_execute":
            # 报告续交付只读取冻结的精确容器，不借研究域或新建容器补齐现场。
            name = snapshot["container_id"]
            if not isinstance(name, str) or not name:
                raise DeliveryUnavailable
            metadata = container_manager.runtime_metadata(name)
            report_owner = snapshot["binding"]["report_segments"]["task_run_id"]
            if (
                not isinstance(report_owner, str)
                or not report_owner
                or metadata.get("project_id") != project_id
                or metadata.get("container_role") != "report"
                or metadata.get("report_task_run_id") != report_owner
            ):
                raise DeliveryUnavailable
        else:
            name = container_manager.container_name(project_id)
            metadata = container_manager.runtime_metadata(name)
        states = {"running", "paused"} if frozen_phase in _RESEARCH_PHASES else {"running"}
        if (
            metadata.get("container_state") not in states
            or metadata.get("container_id") != snapshot.get("container_id")
        ):
            raise DeliveryUnavailable
        if not snapshot.get("session_id", "").startswith("pi:"):
            raise DeliveryUnavailable
    except (KeyError, TypeError, ValueError, AttributeError, OSError) as exc:
        raise DeliveryUnavailable from exc
    # 返回原精确容器 ID，避免名称随后被替换而在新工作区续交付。
    return metadata["container_id"], snapshot, remaining_delivery_seconds(snapshot, budget=budget)


def _confirmed_running_activation(client, container_manager, project_id, generation, container_id, daemon_id=None):
    response = client.get_container_activation(project_id)
    activation = response.data.get("activation") if response.ok and isinstance(response.data, dict) else None
    return bool(
        isinstance(activation, dict)
        and activation.get("state") == "running"
        and activation.get("activation_generation") == generation
        and activation.get("container_id") == container_id
        and activation.get("docker_daemon_id") == (daemon_id or container_manager.docker_daemon_id())
    )


def retained_runner_available(container_manager, project_id, expected_container_id, *, client=None, generation=None):
    """领取前检查原 runner；运行容器还须有本代可信激活，执行时在锁内复核。"""
    if not isinstance(expected_container_id, str) or not expected_container_id:
        return False
    name = container_manager.container_name(project_id)
    before = container_manager.runtime_metadata(name)
    state = before.get("container_state")
    if before.get("container_id") != expected_container_id or state not in {"exited", "created", "running"}:
        return False
    if state == "running" and (client is None or type(generation) is not int
                               or not _confirmed_running_activation(client, container_manager, project_id, generation, expected_container_id)):
        return False
    try:
        with container_manager.materialize_file(name, "/opt/cairn/pi-agent/dist/cli.js") as file:
            capable = file.size <= 1024 * 1024 and b"session-continuation-v1" in file.path.read_bytes()
    except (OSError, RuntimeError, ValueError):
        return False
    after = container_manager.runtime_metadata(name)
    return bool(capable and after.get("container_id") == expected_container_id
                and after.get("container_state") == state
                and (state != "running" or _confirmed_running_activation(
                    client, container_manager, project_id, generation, expected_container_id,
                )))


def resume_continuation(client, container_manager, project_id, generation, container_runtime, context):
    """仅原容器可承载暂停续作，先后核验 ID，缺失时绝不新建空工作区。"""
    if not isinstance(context, dict):
        raise DeliveryUnavailable
    session = context.get("session_id")
    container_id = context.get("container_id")
    if type(generation) is not int or context.get("execution_generation") != generation:
        raise DeliveryUnavailable
    if not isinstance(session, str) or not session.startswith("pi:") or not isinstance(container_id, str) or not container_id:
        raise DeliveryUnavailable
    name = container_manager.container_name(project_id)
    before = container_manager.runtime_metadata(name)
    if before.get("container_id") != container_id or before.get("container_state") not in {"exited", "created", "running"}:
        raise DeliveryUnavailable

    def reuse(daemon_id, exact_id):
        return _confirmed_running_activation(client, container_manager, project_id, generation, exact_id, daemon_id)

    def register(daemon_id):
        response = client.register_container_activation(project_id, generation, daemon_id)
        token = response.data.get("activation_token") if isinstance(response.data, dict) else None
        if not response.ok or not isinstance(token, str) or not token:
            raise DeliveryUnavailable("continuation_activation_registration_failed")
        return token

    def finish(token, exact_id):
        if exact_id != container_id:
            raise DeliveryUnavailable("continuation_container_changed")
        response = client.finish_container_activation(project_id, token, exact_id)
        if not response.ok:
            raise DeliveryUnavailable("continuation_activation_confirmation_failed")

    try:
        running = container_manager.activate_existing_for_continuation(
            project_id, container_runtime, container_id, register=register, finish=finish, reuse=reuse,
        )
    except Exception as exc:
        raise DeliveryUnavailable("continuation_container_unavailable") from exc
    after = container_manager.runtime_metadata(running)
    if after.get("container_id") != container_id or after.get("container_state") != "running":
        raise DeliveryUnavailable
    from cairn.dispatcher.runtime.readiness import run_readiness_check

    readiness = run_readiness_check(container_manager, running, timeout_seconds=15)
    if not readiness.ok or "session-continuation-v1" not in readiness.capabilities:
        raise DeliveryUnavailable("retained_container_continuation_unsupported")
    return running, session


def register_delivery(
    client, container_manager, container_name, project, task_run_id, worker,
    *, phase, session, timeout_seconds, binding=None, inputs=None, deadline_at=None,
    deadline_paused_seconds=None, budget=None,
):
    """首次模型调用前由 owner 幂等登记；快照不含 prompt、凭据或业务原文。"""
    metadata = container_manager.runtime_metadata(container_name)
    # 登记失败必须在模型执行前阻断，不能降级为无持久来源的执行。
    if (
        not isinstance(metadata, dict)
        or metadata.get("container_id") in {None, "unknown", ""}
        or metadata.get("container_state") != "running"
    ):
        raise DeliveryUnavailable
    research = phase in _RESEARCH_PHASES
    current = client.get_project(project.project.id).project if research else project.project
    if research:
        if budget is None:
            raise DeliveryUnavailable
        budget.bind(current)
    paused_seconds = budget.paused_seconds if research else None
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    if deadline_at is not None:
        previous = {"deadline_at": deadline_at, "paused_seconds": deadline_paused_seconds}
        deadline = min(deadline, delivery_execution_deadline(
            previous, paused_seconds=paused_seconds if deadline_paused_seconds is not None else None,
        ))
    if phase != "report_execute":
        absolute_deadlines = (getattr(current, "explore_deadline", None),)
        if not research:
            absolute_deadlines += (getattr(current, "authorization_expires_at", None),)
        for absolute_deadline in absolute_deadlines:
            if absolute_deadline:
                deadline = min(deadline, _timestamp(absolute_deadline))
    try:
        schema_sha256 = result_schema_digest(phase, binding, inputs)
    except (KeyError, TypeError, ValueError) as exc:
        raise DeliveryUnavailable from exc
    snapshot = {
        "session_id": session,
        "phase": phase,
        "deadline_at": deadline.isoformat(),
        "container_id": metadata["container_id"],
        "model_binding_sha256": model_binding_digest(worker),
        "result_schema_sha256": schema_sha256,
        "binding": binding,
        "inputs": inputs or {},
    }
    if research:
        snapshot.update(authorization_expires_at=current.authorization_expires_at, paused_seconds=paused_seconds)
    remaining_delivery_seconds(snapshot, budget=budget)
    response = client.update_task_run(
        project.project.id, task_run_id, status="running", session_id=session,
        runtime_context={"execution_delivery": snapshot},
    )
    if not response.ok:
        raise DeliveryUnavailable
    remaining_delivery_seconds(snapshot, budget=budget)
    return snapshot


DELIVERY_PROMPT = (
    "仅根据原会话已有证据完成 submit_result 交付。禁止新的目标探索、命令执行或写入。"
    "原证据不足时如实拒绝，不补造事实。"
)
