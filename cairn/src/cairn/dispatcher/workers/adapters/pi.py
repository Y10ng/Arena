"""唯一生产 Agent driver：用私有请求文件调用 cairn-agent SDK runner。"""

from __future__ import annotations

import json
import uuid
from typing import Any, NoReturn

from cairn.dispatcher.config import WorkerConfig, is_sensitive_env_key
from cairn.dispatcher.worker_protocol.candidate_authority import (
    candidate_authority_sha256,
)
from cairn.dispatcher.worker_protocol.submission import (
    PHASE_DATA_MODELS,
    build_submit_result_schema,
)
from cairn.dispatcher.worker_result_gateway import WorkerRequestError
from cairn.dispatcher.workers.base import (
    CANDIDATE_MAX_FORBIDDEN_LITERAL_BYTES,
    CANDIDATE_MAX_FORBIDDEN_LITERALS,
    DriverResult,
    WorkerCandidateSpool,
    WorkerCheckpointSpool,
    WorkerInputFile,
)


def _candidate_forbidden_literals(worker: WorkerConfig, phase: str) -> list[str]:
    """把部署方显式注入的敏感环境值加入 exact-value admission。"""
    values: list[str] = []
    for key, value in sorted(worker.env.items()):
        if not is_sensitive_env_key(key) or not value or value in values:
            continue
        if "\0" in value or len(value.encode("utf-8")) > CANDIDATE_MAX_FORBIDDEN_LITERAL_BYTES:
            raise WorkerRequestError(
                "capabilities.candidate_capture.forbidden_literals",
                code="candidate_literal_limit",
                message="candidate forbidden literal exceeds dispatcher hard limits",
                phase=phase,
            )
        values.append(value)
    if len(values) > CANDIDATE_MAX_FORBIDDEN_LITERALS:
        raise WorkerRequestError(
            "capabilities.candidate_capture.forbidden_literals",
            code="candidate_literal_limit",
            message="candidate forbidden literal count exceeds dispatcher hard limits",
            phase=phase,
        )
    return values


class PiDriver:
    type_name = "pi"

    @staticmethod
    def prepare_session() -> str:
        # 这里只预分配身份，不代表 SDK 已经创建会话。
        return f"pi:{uuid.uuid4()}"

    def build_execute(
        self,
        worker: WorkerConfig,
        prompt: str,
        session: str | None,
        *,
        phase: str,
        project_id: str,
        task_run_id: str,
        timeout_seconds: int,
        conclude_timeout_seconds: int = 0,
        binding: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        resume: bool = False,
        conclude_only: bool = False,
        enable_checkpoints: bool = False,
        continuation: dict[str, object] | None = None,
    ) -> DriverResult:
        request_phase = phase if isinstance(phase, str) and phase else "unknown"

        def reject(path: str, message: str, *, code: str = "invalid_request") -> NoReturn:
            raise WorkerRequestError(path, code=code, message=message, phase=request_phase)

        if worker.type != "pi" or worker.engine != "pi":
            reject("worker.engine", "engine_unsupported: only pi workers can execute", code="engine_unsupported")
        if worker.model_binding is None:
            reject("model", "model_binding_missing: pi execution requires an explicit model binding", code="model_binding_missing")
        for name, value in (
            ("prompt", prompt),
            ("phase", phase),
            ("project_id", project_id),
            ("task_run_id", task_run_id),
        ):
            if not isinstance(value, str) or not value:
                reject(name, f"pi execution requires a non-empty string {name}")
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            reject("timeout_seconds", "pi execution requires a positive integer remaining timeout")
        if (
            type(conclude_timeout_seconds) is not int
            or conclude_timeout_seconds < 0
            or conclude_timeout_seconds > timeout_seconds
        ):
            reject(
                "conclude_timeout_seconds",
                "pi execution requires conclude timeout within the remaining timeout",
            )
        for name, value in (
            ("resume", resume),
            ("conclude_only", conclude_only),
            ("enable_checkpoints", enable_checkpoints),
        ):
            if type(value) is not bool:
                reject(name, f"pi execution requires literal boolean {name}")
        if session is None:
            if resume:
                reject("session", "session_missing: cannot resume without a pi session", code="session_missing")
            session = self.prepare_session()
        if not isinstance(session, str) or not session.startswith("pi:"):
            reject("session", "session_unsupported: legacy sessions cannot execute", code="session_unsupported")
        try:
            identifier = uuid.UUID(session[3:])
        except ValueError as exc:
            raise WorkerRequestError(
                "session", code="session_invalid",
                message="session_invalid: expected pi:<UUID>", phase=request_phase,
            ) from exc
        if str(identifier) != session[3:]:
            reject("session", "session_invalid: expected canonical pi:<UUID>", code="session_invalid")
        conclude_phase = phase.endswith("_conclude")
        if conclude_only and not resume:
            reject("resume", "conclude requires an existing pi session")
        if conclude_phase and not (resume and conclude_only):
            reject("conclude_only", "conclude phase requires resume and conclude_only")
        if continuation is not None:
            if not (resume and not conclude_only and not conclude_phase) or not isinstance(continuation, dict):
                reject("continuation", "continuation requires a normal resumed execution")
            if (
                not isinstance(continuation.get("source_task_run_id"), str)
                or not continuation["source_task_run_id"]
                or type(continuation.get("execution_generation")) is not int
                or continuation["execution_generation"] < 0
            ):
                reject("continuation", "continuation requires a source run and current generation")
        if phase not in PHASE_DATA_MODELS:
            reject("phase", "unsupported result phase", code="phase_unsupported")
        try:
            result_schema = build_submit_result_schema(phase, binding=binding, inputs=inputs)
        except ValueError as exc:
            raise WorkerRequestError(
                "result_schema",
                code="invalid_result_schema",
                message="request cannot produce a valid result schema",
                phase=request_phase,
            ) from exc
        try:
            binding_sha256 = candidate_authority_sha256(binding)
        except ValueError as exc:
            raise WorkerRequestError(
                "binding",
                code="invalid_request",
                message="binding is not valid candidate authority JSON",
                phase=request_phase,
            ) from exc
        try:
            result_schema_sha256 = candidate_authority_sha256(result_schema)
        except ValueError as exc:
            raise WorkerRequestError(
                "result_schema",
                code="invalid_result_schema",
                message="generated result schema is not valid candidate authority JSON",
                phase=request_phase,
            ) from exc
        capture_id = str(uuid.uuid4())
        candidate_spool = WorkerCandidateSpool(
            path=f"/tmp/cairn-prompts/pi-candidates-{capture_id}.bin",
            capture_id=capture_id,
            binding_sha256=binding_sha256,
            result_schema_sha256=result_schema_sha256,
        )
        capabilities: dict[str, object] = {
            "candidate_capture": {
                "protocol": candidate_spool.protocol,
                "spool_path": candidate_spool.path,
                "capture_id": candidate_spool.capture_id,
                "max_candidates": candidate_spool.max_candidates,
                "max_candidate_bytes": candidate_spool.max_candidate_bytes,
                "max_manifest_bytes": candidate_spool.max_manifest_bytes,
                "max_spool_bytes": candidate_spool.max_spool_bytes,
                "forbidden_literals": _candidate_forbidden_literals(worker, phase),
            }
        }
        request = {
            "protocol": "cairn-agent-request-v1",
            "project_id": project_id,
            "task_run_id": task_run_id,
            "phase": phase,
            "prompt": prompt,
            "session": session,
            "resume": resume,
            **({"continuation": {
                "source_task_run_id": continuation["source_task_run_id"],
                "execution_generation": continuation["execution_generation"],
            }} if continuation is not None else {}),
            "model": worker.model_binding.model_dump(mode="json"),
            "result_schema": result_schema,
            "binding": binding,
            "timeout_seconds": timeout_seconds,
            "conclude_timeout_seconds": conclude_timeout_seconds,
            "max_result_repairs": 2,
            "conclude_only": conclude_only,
            "cwd": "/home/kali/workspace",
            "capabilities": capabilities,
        }
        checkpoint_spool: WorkerCheckpointSpool | None = None
        if (
            enable_checkpoints
            and (not resume or continuation is not None)
            and not conclude_only
            and phase != "report_execute"
            and not phase.endswith("_conclude")
        ):
            checkpoint_spool = WorkerCheckpointSpool(
                path=f"/tmp/cairn-prompts/pi-checkpoints-{uuid.uuid4().hex}.jsonl"
            )
            capabilities["execution_checkpoints"] = {
                "protocol": checkpoint_spool.protocol,
                "spool_path": checkpoint_spool.path,
                "max_events": checkpoint_spool.max_events,
                "max_event_bytes": checkpoint_spool.max_event_bytes,
                "max_spool_bytes": checkpoint_spool.max_spool_bytes,
            }
        path = f"/tmp/cairn-prompts/pi-request-{uuid.uuid4().hex}.json"
        input_files = [
            WorkerInputFile(path=path, content=json.dumps(request, ensure_ascii=False)),
            # 预创建 0600 regular file，让 runner 只能追加到本次随机 binary spool。
            WorkerInputFile(path=candidate_spool.path, content=""),
        ]
        if checkpoint_spool is not None:
            # checkpoint 与 candidate 使用独立 spool，互不改变各自失败语义。
            input_files.append(WorkerInputFile(path=checkpoint_spool.path, content=""))
        return DriverResult(
            argv=["cairn-agent", "--request", path],
            session=session,
            input_files=tuple(input_files),
            checkpoint_spool=checkpoint_spool,
            candidate_spool=candidate_spool,
        )

    def build_conclude(
        self,
        worker: WorkerConfig,
        prompt: str,
        session: str,
        *,
        phase: str,
        project_id: str,
        task_run_id: str,
        timeout_seconds: int,
        binding: dict[str, Any] | None = None,
    ) -> DriverResult:
        return self.build_execute(
            worker, prompt, session, phase=phase, project_id=project_id,
            task_run_id=task_run_id, timeout_seconds=timeout_seconds, binding=binding,
            resume=True, conclude_only=True,
        )

    @staticmethod
    def build_version_probe(worker: WorkerConfig) -> list[str]:
        # 版本探针仅供显示版本；readiness 使用独立的 --self-check。
        return ["cairn-agent", "--version"]
