"""Dispatcher 运行期内存状态模型。"""

from __future__ import annotations

from dataclasses import dataclass

from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerRuntimeOptions


@dataclass(frozen=True, slots=True)
class BindingCircuitKey:
    """共享 Model/凭据执行绑定的非敏感熔断身份。"""

    model_id: str
    model_revision: int
    credential_version_id: str
    credential_version: int
    protocol: str
    auth_header: str
    inference_endpoint: str


@dataclass(slots=True)
class RunningTask:
    """线程池中正在执行的任务快照，用于并发控制和 shutdown/reconcile。"""

    project_id: str
    task_type: str
    worker_name: str
    worker_type: str
    cancellation: TaskCancellation
    container_runtime: ContainerRuntimeOptions
    task_run_id: str | None = None
    intent_id: str | None = None
    fact_count: int | None = None
    hint_count: int | None = None
    open_intent_count: int | None = None
    coverage_checkpoint_cell_count: int | None = None
    coverage_stop_reason: str | None = None
    binding_circuit_key: BindingCircuitKey | None = None
    settled_intent_count: int | None = None
    execution_generation: int = 0


@dataclass(slots=True)
class ReasonCheckpoint:
    """reason 调度去重用的项目摘要，避免同一图谱状态反复触发 reason。"""

    fact_count: int
    hint_count: int
    open_intent_count: int
    coverage_checkpoint_cell_count: int = 0
    coverage_stop_reason: str | None = None
    settled_intent_count: int = 0
    execution_generation: int = 0
