"""四阶段共享的原会话交付约束；不拥有重试额度或来源选择权。"""
from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

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


def result_schema_digest(phase, binding) -> str:
    """冻结实际交给 Pi 的 submit_result Schema 身份。"""
    schema = build_submit_result_schema(phase, binding=binding)
    return candidate_authority_sha256(schema)


def remaining_delivery_seconds(snapshot):
    """准备 I/O 也消耗原截止时间；实际启动前重算，绝不重新分配 timeout。"""
    try:
        deadline = datetime.fromisoformat(snapshot["deadline_at"].replace("Z", "+00:00"))
        remaining = int((deadline - datetime.now(timezone.utc)).total_seconds())
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
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
):
    snapshot = context.get("execution_delivery")
    if (
        not context.get("delivery_retry")
        or not isinstance(snapshot, dict)
        or set(snapshot) != _DELIVERY_FIELDS
    ):
        raise DeliveryUnavailable
    allowed_phases = {phase} if isinstance(phase, str) else set(phase)
    frozen_phase = snapshot.get("phase")
    if not isinstance(frozen_phase, str) or frozen_phase not in allowed_phases:
        raise DeliveryUnavailable
    try:
        expected_schema_sha256 = result_schema_digest(
            frozen_phase,
            snapshot["binding"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DeliveryUnavailable from exc
    if (
        snapshot.get("model_binding_sha256") != model_binding_digest(worker)
        or snapshot.get("result_schema_sha256") != expected_schema_sha256
    ):
        raise DeliveryUnavailable
    try:
        remaining_delivery_seconds(snapshot)
        name = container_manager.container_name(project_id)
        metadata = container_manager.runtime_metadata(name)
        if (
            metadata.get("container_state") != "running"
            or metadata.get("container_id") != snapshot.get("container_id")
        ):
            raise DeliveryUnavailable
        if not snapshot.get("session_id", "").startswith("pi:"):
            raise DeliveryUnavailable
    except (KeyError, TypeError, ValueError, AttributeError, OSError) as exc:
        raise DeliveryUnavailable from exc
    # 返回原精确容器 ID，避免名称随后被替换而在新工作区续交付。
    return metadata["container_id"], snapshot, remaining_delivery_seconds(snapshot)


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
    *, phase, session, timeout_seconds, binding=None, inputs=None,
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
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    project_deadline = getattr(project.project, "explore_deadline", None)
    authorization_expires_at = getattr(
        project.project,
        "authorization_expires_at",
        None,
    )
    if phase != "report_execute":
        for absolute_deadline in (project_deadline, authorization_expires_at):
            if absolute_deadline:
                deadline = min(
                    deadline,
                    datetime.fromisoformat(
                        absolute_deadline.replace("Z", "+00:00")
                    ),
                )
    try:
        schema_sha256 = result_schema_digest(phase, binding)
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
    remaining_delivery_seconds(snapshot)
    response = client.update_task_run(
        project.project.id, task_run_id, status="running", session_id=session,
        runtime_context={"execution_delivery": snapshot},
    )
    if not response.ok:
        raise DeliveryUnavailable
    remaining_delivery_seconds(snapshot)
    return snapshot


DELIVERY_PROMPT = (
    "仅根据原会话已有证据完成 submit_result 交付。禁止新的目标探索、命令执行或写入。"
    "原证据不足时如实拒绝，不补造事实。"
)
