"""Dispatcher 到 Server 的 HTTP client，封装协议写回和容错返回。"""

from __future__ import annotations

from base64 import b64encode, urlsafe_b64encode
from dataclasses import dataclass
import json
from typing import Any, BinaryIO
import logging
import os
import threading
import time
from urllib.parse import quote

from pydantic import TypeAdapter
import requests
from requests.adapters import HTTPAdapter

from cairn.server.models import (
    EvidenceRef,
    Intent,
    LibraryFile,
    ProjectDetail,
    ProjectLibraryBinding,
    ProjectResource,
    ProjectSummary,
    Scenario,
    Settings,
    VulnerabilityVerifierTarget,
)
from cairn.server.schemas.tenant_workers import TenantWorkerExecutionBinding

LOG = logging.getLogger(__name__)
DISPATCHER_TOKEN_ENV = "CAIRN_DISPATCHER_TOKEN"
EXECUTION_EVENT_CAPABILITY_CACHE_SECONDS = 30.0
CANDIDATE_CAPABILITY_CACHE_SECONDS = 30.0


@dataclass(slots=True)
class ApiResult:
    """非抛异常式请求结果，用于任务写回阶段记录失败而不中断清理流程。"""

    status_code: int
    data: Any | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class CairnClient:
    """线程内复用 requests.Session 的 Server 协议客户端。"""

    def __init__(self, base_url: str, timeout: float = 10.0, token: str | None = None):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._token = (token or os.environ.get(DISPATCHER_TOKEN_ENV, "")).strip()
        if not self._token:
            raise ValueError(f"{DISPATCHER_TOKEN_ENV} must be set for Dispatcher authentication")
        self._summary_adapter = TypeAdapter(list[ProjectSummary])
        self._scenario_adapter = TypeAdapter(list[Scenario])
        self._library_file_adapter = TypeAdapter(list[LibraryFile])
        self._project_library_binding_adapter = TypeAdapter(list[ProjectLibraryBinding])
        self._project_resource_adapter = TypeAdapter(list[ProjectResource])
        self._local = threading.local()
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()
        self._execution_event_capability_cache: tuple[float, dict[str, Any] | None] | None = None
        self._execution_event_capability_lock = threading.Lock()
        self._candidate_capability_cache: tuple[float, dict[str, Any] | None] | None = None
        self._candidate_capability_lock = threading.Lock()

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        # 热加载切换 Server 或进程退出时关闭所有线程创建过的 session。
        for session in sessions:
            session.close()

    @property
    def base_url(self) -> str:
        return self._base_url

    def consume_project_schedules(self) -> dict[str, int]:
        # 消费失败进入外层循环的可见错误路径，不能作为已处理返回。
        response = self._session().post(
            self._url("/dispatcher/project-schedules/consume"), timeout=self._timeout,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or any(
            type(result.get(key)) is not int or result[key] < 0
            for key in ("processed", "errors")
        ):
            raise ValueError("Invalid project schedule consumption response")
        return {"processed": result["processed"], "errors": result["errors"]}

    def list_projects(self) -> list[ProjectSummary]:
        # 调度主循环的读请求使用 raise_for_status，Server 不可用时直接进入本轮异常处理。
        response = self._session().get(self._url("/projects"), timeout=self._timeout)
        response.raise_for_status()
        return self._summary_adapter.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self._session().get(self._url(f"/projects/{project_id}"), timeout=self._timeout)
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_verification_input(self, request_id: str) -> ApiResult:
        return self._request_json("GET", f"/api/v1/verification-requests/{request_id}/input")

    def claim_verification(self, request_id: str, task_run: dict[str, Any]) -> ApiResult:
        return self._request_json(
            "POST", f"/api/v1/verification-requests/{request_id}/claim", json={"task_run": task_run},
        )

    def finish_verification(self, request_id: str, action: str, payload: dict[str, Any]) -> ApiResult:
        if action not in {"result", "failure", "cancel-ack"}:
            raise ValueError("unsupported verification terminal action")
        return self._request_json(
            "POST", f"/api/v1/verification-requests/{request_id}/{action}", json=payload,
        )

    def verification_heartbeat(self, request_id: str, payload: dict[str, Any]) -> ApiResult:
        return self._request_json(
            "POST", f"/api/v1/verification-requests/{request_id}/heartbeat", json=payload,
        )

    def get_container_activation(self, project_id: str) -> ApiResult:
        return self._request_json("GET", f"/projects/{project_id}/container-activation")

    def register_container_activation(self, project_id: str, generation: int, docker_daemon_id: str) -> ApiResult:
        return self._request_json(
            "POST", f"/projects/{project_id}/container-activation",
            json={"generation": generation, "docker_daemon_id": docker_daemon_id},
        )

    def finish_container_activation(self, project_id: str, token: str, container_id: str | None) -> ApiResult:
        return self._request_json(
            "POST", f"/projects/{project_id}/container-activation/finish",
            json={"activation_token": token, "container_id": container_id},
        )

    def adopt_container_pause(
        self, project_id: str, generation: int, container_id: str | None, docker_daemon_id: str,
    ) -> ApiResult:
        return self._request_json(
            "POST", f"/projects/{project_id}/container-pause/adopt",
            json={"generation": generation, "container_id": container_id, "docker_daemon_id": docker_daemon_id},
        )

    def claim_container_pause(self, project_id: str, generation: int, docker_daemon_id: str) -> ApiResult:
        return self._request_json(
            "POST", f"/projects/{project_id}/container-pause/claim",
            json={"generation": generation, "docker_daemon_id": docker_daemon_id},
        )

    def confirm_container_pause(
        self, project_id: str, generation: int, token: str,
        container_id: str | None, docker_daemon_id: str,
    ) -> ApiResult:
        return self._request_json(
            "POST", f"/projects/{project_id}/container-pause/confirm",
            json={"generation": generation, "stop_token": token,
                  "container_id": container_id, "docker_daemon_id": docker_daemon_id},
        )

    def list_project_targets(self, project_id: str) -> list[str]:
        """流量 scope 用：取项目 seed/approved 目标的 host 值（domain/url/ip/cidr）。"""
        try:
            response = self._session().get(
                self._url(f"/projects/{project_id}/targets"),
                timeout=self._timeout,
            )
            response.raise_for_status()
        except requests.RequestException:
            return []
        try:
            data = response.json()
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        hosts: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if item.get("status") not in ("seed", "approved"):
                continue
            value = str(item.get("value") or "").strip()
            if value:
                hosts.append(value)
        return hosts

    def list_scenarios(self) -> list[Scenario]:
        response = self._session().get(self._url("/scenarios"), timeout=self._timeout)
        response.raise_for_status()
        return self._scenario_adapter.validate_python(response.json())

    def list_project_library_bindings(
        self,
        project_id: str,
    ) -> list[ProjectLibraryBinding]:
        project = self.get_project(project_id)
        return self._project_library_binding_adapter.validate_python(
            project.library_bindings
        )

    def list_project_library_files(
        self,
        project_id: str,
        key: str,
        version: int,
    ) -> list[LibraryFile]:
        response = self._session().get(
            self._url(
                f"/projects/{project_id}/advisory-libraries/{key}/versions/{version}/files"
            ),
            timeout=self._timeout,
        )
        response.raise_for_status()
        return self._library_file_adapter.validate_python(response.json())

    def read_project_library_file(
        self,
        project_id: str,
        key: str,
        version: int,
        path: str,
    ) -> bytes:
        response = self._session().get(
            self._url(
                f"/projects/{project_id}/advisory-libraries/{quote(key, safe='')}/versions/"
                f"{version}/files/{quote(path, safe='/')}"
            ),
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.content

    def list_project_resources(self, project_id: str) -> list[ProjectResource]:
        response = self._session().get(
            self._url(f"/projects/{project_id}/resources"),
            timeout=self._timeout,
        )
        response.raise_for_status()
        return self._project_resource_adapter.validate_python(response.json())

    def read_project_resource(self, project_id: str, resource_id: str) -> bytes:
        response = self._session().get(
            self._url(f"/projects/{project_id}/resources/{resource_id}"),
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.content

    def resolve_execution_binding(
        self,
        project_id: str,
        worker_id: str,
        task_type: str,
    ) -> TenantWorkerExecutionBinding:
        response = self._session().post(
            self._url(
                f"/dispatcher/projects/{project_id}/tenant-workers/{worker_id}/execution-bindings"
            ),
            params={"task_type": task_type},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return TenantWorkerExecutionBinding.model_validate(response.json())

    def list_task_runs(self, project_id: str) -> ApiResult:
        try:
            response = self._session().get(
                self._url(f"/projects/{project_id}/task-runs"),
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            # 调度循环查询审计状态失败时返回 status_code=0，由调用方决定是否重试。
            LOG.warning("request failed method=GET path=/projects/%s/task-runs error=%s", project_id, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def list_task_run_command_events(self, project_id: str, task_run_ids: list[str]) -> ApiResult:
        params = [("task_run_id", task_run_id) for task_run_id in task_run_ids if task_run_id]
        if not params:
            return ApiResult(status_code=200, data=[])
        try:
            response = self._session().get(
                self._url(f"/projects/{project_id}/task-runs/command-events"),
                params=params,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=GET path=/projects/%s/task-runs/command-events error=%s", project_id, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def list_task_run_hook_events(self, project_id: str, task_run_ids: list[str]) -> ApiResult:
        params = [("task_run_id", task_run_id) for task_run_id in task_run_ids if task_run_id]
        if not params:
            return ApiResult(status_code=200, data=[])
        try:
            response = self._session().get(
                self._url(f"/projects/{project_id}/task-runs/hook-events"),
                params=params,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=GET path=/projects/%s/task-runs/hook-events error=%s", project_id, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def get_settings(self) -> Settings:
        response = self._session().get(self._url("/settings"), timeout=self._timeout)
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def export_project(self, project_id: str) -> str:
        response = self._session().get(
            self._url(f"/projects/{project_id}/export"),
            params={"format": "yaml"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.text

    def heartbeat(
        self,
        project_id: str,
        intent_id: str,
        worker: str,
        *,
        task_run_id: str | None = None,
    ) -> ApiResult:
        payload = {"worker": worker}
        if task_run_id is not None:
            payload["task_run_id"] = task_run_id
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/heartbeat",
            json=payload,
        )

    def start_explore_attempt(
        self,
        project_id: str,
        intent_id: str,
        worker_name: str,
        *,
        worker_type: str,
        prompt_group: str,
        session_id: str | None = None,
        execution_profile: str | None = None,
        runtime_context: dict[str, Any] | None = None,
        expected_execution_generation: int | None = None,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/explore-attempts",
            json={
                "intent_id": intent_id,
                "task_type": "explore",
                "worker_name": worker_name,
                "worker_type": worker_type,
                "status": "running",
                "session_id": session_id,
                "prompt_group": prompt_group,
                "execution_profile": execution_profile,
                "runtime_context": runtime_context or {},
                "expected_execution_generation": expected_execution_generation,
            },
        )

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/claim",
            json={"worker": worker, "trigger": trigger},
        )

    def reason_heartbeat(self, project_id: str, worker: str, *, task_run_id: str | None = None) -> ApiResult:
        payload = {"worker": worker}
        if task_run_id is not None:
            payload["task_run_id"] = task_run_id
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/heartbeat",
            json=payload,
        )

    def submit_depth_review(
        self,
        project_id: str,
        decision: dict[str, Any],
        *,
        worker: str,
        task_run_id: str,
    ) -> ApiResult:
        # 深测决策与选中 Intent 由 Server 同一事务接受，不能逐条创建后再标记成功。
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/depth-review",
            json={**decision, "worker": worker, "task_run_id": task_run_id},
        )

    def release_reason(self, project_id: str, worker: str, *, task_run_id: str | None = None) -> ApiResult:
        payload = {"worker": worker}
        if task_run_id is not None:
            payload["task_run_id"] = task_run_id
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/release",
            json=payload,
        )

    def release(self, project_id: str, intent_id: str, worker: str, *, task_run_id: str | None = None) -> ApiResult:
        payload = {"worker": worker}
        if task_run_id is not None:
            payload["task_run_id"] = task_run_id
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/release",
            json=payload,
        )

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        return self.conclude_with_evidence(project_id, intent_id, worker, description, [], task_run_id=None)

    def conclude_with_evidence(
        self,
        project_id: str,
        intent_id: str,
        worker: str,
        description: str,
        evidence: list[EvidenceRef],
        task_run_id: str | None = None,
        conclusion: str = "completed",
        vulnerability: dict[str, object] | None = None,
        coverage_result: dict[str, object] | None = None,
        discovered_surfaces: list[dict[str, object]] | None = None,
        coverage_scope_disposition: dict[str, object] | None = None,
        verification_result: dict[str, object] | None = None,
        assessment_record: dict[str, object] | None = None,
    ) -> ApiResult:
        # vulnerability 为 dispatcher resolve 后的 server 格式 dict（含 packet_artifact_id），
        # 透传 server conclude 自动确认漏洞。None=该 fact 无漏洞字段。
        body: dict[str, object] = {
            "worker": worker,
            "description": description,
            "evidence": [ref.model_dump() for ref in evidence],
            "task_run_id": task_run_id,
            "conclusion": conclusion,
        }
        if vulnerability is not None:
            body["vulnerability"] = vulnerability
        if coverage_result is not None:
            body["coverage_result"] = coverage_result
        if discovered_surfaces:
            body["discovered_surfaces"] = discovered_surfaces
        if coverage_scope_disposition is not None:
            body["coverage_scope_disposition"] = coverage_scope_disposition
        if verification_result is not None:
            body["verification_result"] = verification_result
        if assessment_record is not None:
            body["assessment_record"] = assessment_record
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json=body,
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str) -> ApiResult:
        return self.complete_with_evidence(project_id, from_ids, description, worker, [], task_run_id=None)

    def abandon_intent(
        self,
        project_id: str,
        intent_id: str,
        worker: str,
        task_run_id: str | None = None,
    ) -> ApiResult:
        # 放弃一个 open intent：仅对未认领的 open intent 生效；运行中/已收敛返回 409。
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/abandon",
            json={"worker": worker, "task_run_id": task_run_id},
        )

    def complete_with_evidence(
        self,
        project_id: str,
        from_ids: list[str],
        description: str,
        worker: str,
        evidence: list[EvidenceRef],
        task_run_id: str | None = None,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/complete",
            json={
                "from": from_ids,
                "description": description,
                "worker": worker,
                "evidence": [ref.model_dump() for ref in evidence],
                "task_run_id": task_run_id,
            },
        )

    def create_intent(
        self,
        project_id: str,
        from_ids: list[str],
        description: str,
        creator: str,
        task_run_id: str | None = None,
        depends_on: str | None = None,
        slot_key: str | None = None,
        strategy_key: str = "",
        submission_index: int | None = None,
        priority: int | None = None,
    ) -> ApiResult:
        body: dict[str, object] = {
            "from": from_ids,
            "description": description,
            "creator": creator,
            "task_run_id": task_run_id,
            "worker": None,
        }
        if submission_index is not None:
            body["submission_index"] = submission_index
        if priority is not None:
            body["priority"] = priority
        if depends_on:
            body["depends_on"] = depends_on
        # pentest 场景把 intent 绑定到 coverage slot；其他场景不传，保持兼容。
        if slot_key:
            body["slot_key"] = slot_key
        if strategy_key:
            body["strategy_key"] = strategy_key
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents",
            json=body,
        )

    def upsert_coverage_slots(
        self,
        project_id: str,
        slots: list[dict[str, str]],
        source_intent_id: str | None = None,
        tier: str = "extended",
    ) -> ApiResult:
        # pentest 场景登记攻击面槽位；tier=core 由 bootstrap 传，extended 由 explore/reason 传。
        # 空 slots 列表跳过，避免无意义请求。
        if not slots:
            return ApiResult(status_code=200)
        return self._request_json(
            "POST",
            f"/projects/{project_id}/coverage-slots",
            json={"slots": slots, "source_intent_id": source_intent_id, "tier": tier},
        )

    def register_discovered_surfaces(
        self,
        project_id: str,
        surfaces: list[dict[str, object]],
        *,
        evidence: list[EvidenceRef] | None = None,
        source_intent_id: str | None = None,
        source_fact_id: str | None = None,
        source_task_run_id: str | None = None,
        tier: str = "extended",
    ) -> ApiResult:
        if not surfaces:
            return ApiResult(status_code=200, data=[])
        return self._request_json(
            "POST",
            f"/projects/{project_id}/surface-manifest",
            json={
                "surfaces": surfaces,
                "evidence": [ref.model_dump() for ref in (evidence or [])],
                "source_intent_id": source_intent_id,
                "source_fact_id": source_fact_id,
                "source_task_run_id": source_task_run_id,
                "tier": tier,
            },
        )

    def list_coverage_slots(self, project_id: str) -> ApiResult:
        # reason 注入待覆盖槽位时拉取当前状态。
        return self._request_json(
            "GET",
            f"/projects/{project_id}/coverage-slots",
            json={},
        )

    def advance_coverage_convergence(self, project_id: str) -> ApiResult:
        # admission 命中硬限制后立即排空未认领 Intent，避免调度器重复选择同一任务。
        return self._request_json(
            "POST",
            f"/projects/{project_id}/coverage-convergence",
            json={},
        )

    def get_observation_verifier_target(
        self,
        project_id: str,
        observation_id: str,
    ) -> VulnerabilityVerifierTarget:
        response = self._session().get(
            self._url(
                f"/projects/{project_id}/vulnerability-observations/"
                f"{observation_id}/verifier-target"
            ),
            timeout=self._timeout,
        )
        response.raise_for_status()
        return VulnerabilityVerifierTarget.model_validate(response.json())

    def list_vulnerability_chain_candidates(self, project_id: str) -> ApiResult:
        return self._request_json(
            "GET",
            f"/projects/{project_id}/vulnerability-chain-candidates",
            json={},
        )

    def list_vulnerabilities(self, project_id: str) -> ApiResult:
        # 报告阶段先拉取漏洞列表，再按 ID 获取详情补齐来源证据。
        return self._request_json(
            "GET",
            f"/projects/{project_id}/vulnerabilities",
            json={},
        )

    def get_vulnerability(self, project_id: str, vuln_id: str) -> ApiResult:
        return self._request_json(
            "GET",
            f"/projects/{project_id}/vulnerabilities/{vuln_id}",
            json={},
        )

    def update_vulnerability(
        self,
        project_id: str,
        vuln_id: str,
        *,
        repro_request: str | None = None,
        repro_response: str | None = None,
        repro_steps: str | None = None,
        remediation: str | None = None,
    ) -> ApiResult:
        # 报告阶段按稳定漏洞 ID 回写干净复现包；None 字段不改动。
        body: dict[str, object] = {}
        if repro_request is not None:
            body["repro_request"] = repro_request
        if repro_response is not None:
            body["repro_response"] = repro_response
        if repro_steps is not None:
            body["repro_steps"] = repro_steps
        if remediation is not None:
            body["remediation"] = remediation
        return self._request_json(
            "PATCH",
            f"/projects/{project_id}/vulnerabilities/{vuln_id}",
            json=body,
        )

    def create_task_run(
        self,
        project_id: str,
        *,
        intent_id: str | None,
        task_type: str,
        worker_name: str,
        worker_type: str,
        prompt_group: str,
        session_id: str | None = None,
        execution_profile: str | None = None,
        runtime_context: dict[str, Any] | None = None,
        expected_execution_generation: int | None = None,
    ) -> ApiResult:
        # task_run 创建是拿到租约后的审计起点，失败由调度层负责释放租约和收口。
        return self._request_json(
            "POST",
            f"/projects/{project_id}/task-runs",
            json={
                "intent_id": intent_id,
                "task_type": task_type,
                "worker_name": worker_name,
                "worker_type": worker_type,
                "status": "running",
                "session_id": session_id,
                "prompt_group": prompt_group,
                "execution_profile": execution_profile,
                "runtime_context": runtime_context or {},
                "expected_execution_generation": expected_execution_generation,
            },
        )

    def get_assessment_report_packet(self, project_id: str) -> ApiResult:
        return self._request_json(
            "GET",
            f"/projects/{project_id}/assessment-report-packet",
        )

    def get_assessment_report_finalization(self, project_id: str) -> ApiResult:
        return self._request_json(
            "GET",
            f"/projects/{project_id}/assessment-report-finalization",
        )

    def claim_assessment_report_finalization(
        self,
        project_id: str,
        worker: str,
        *,
        lease_seconds: int = 300,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/assessment-report-finalization/claim",
            json={"worker": worker, "lease_seconds": lease_seconds},
        )

    def bind_assessment_report_input_snapshot(
        self,
        project_id: str,
        worker: str,
        *,
        claim_token: str,
        artifact_id: str,
        digest_sha256: str,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/assessment-report-finalization/snapshot",
            json={
                "worker": worker,
                "claim_token": claim_token,
                "artifact_id": artifact_id,
                "digest_sha256": digest_sha256,
            },
        )

    def finish_assessment_report_finalization(
        self,
        project_id: str,
        worker: str,
        *,
        claim_token: str,
        generation_token: str,
        task_run_id: str,
        status: str,
        artifact_id: str | None,
        artifact_sha256: str | None,
        input_snapshot_artifact_id: str | None,
        input_snapshot_sha256: str | None,
        task_run_result: dict[str, object],
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/assessment-report-finalization/finish",
            json={
                "protocol": "cairn-assessment-report-finish-v1",
                "worker": worker,
                "claim_token": claim_token,
                "generation_token": generation_token,
                "task_run_id": task_run_id,
                "status": status,
                "artifact_id": artifact_id,
                "artifact_sha256": artifact_sha256,
                "input_snapshot_artifact_id": input_snapshot_artifact_id,
                "input_snapshot_sha256": input_snapshot_sha256,
                "task_run_result": task_run_result,
            },
        )

    def replay_assessment_report_finalization(
        self,
        project_id: str,
        worker: str,
        *,
        claim_sha256: str,
        request_sha256: str,
        generation_token: str,
        task_run_id: str,
        status: str,
        artifact_id: str | None,
        artifact_sha256: str | None,
        input_snapshot_artifact_id: str | None,
        input_snapshot_sha256: str | None,
        task_run_result: dict[str, object],
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/assessment-report-finalization/finish-replay",
            json={
                "protocol": "cairn-assessment-report-finish-v1",
                "worker": worker,
                "claim_sha256": claim_sha256,
                "request_sha256": request_sha256,
                "generation_token": generation_token,
                "task_run_id": task_run_id,
                "status": status,
                "artifact_id": artifact_id,
                "artifact_sha256": artifact_sha256,
                "input_snapshot_artifact_id": input_snapshot_artifact_id,
                "input_snapshot_sha256": input_snapshot_sha256,
                "task_run_result": task_run_result,
            },
        )

    def export_project_result(self, project_id: str) -> ApiResult:
        # 报告阶段用：非抛异常式导出图谱 YAML（export_project 的 raise 版本供调度主循环用）。
        try:
            response = self._session().get(
                self._url(f"/projects/{project_id}/export"),
                params={"format": "yaml"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=GET path=/projects/%s/export error=%s", project_id, exc)
            return ApiResult(status_code=0, text=str(exc))
        return ApiResult(status_code=response.status_code, data=None, text=response.text)

    def update_task_run(
        self,
        project_id: str,
        task_run_id: str,
        *,
        status: str | None = None,
        session_id: str | None = None,
        exit_code: int | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        runtime_context: dict[str, Any] | None = None,
    ) -> ApiResult:
        # 收尾更新可携带 runtime_context 增量，Server 侧会清洗并合并可信字段。
        return self._request_json(
            "PATCH",
            f"/projects/{project_id}/task-runs/{task_run_id}",
            json={
                "status": status,
                "session_id": session_id,
                "exit_code": exit_code,
                "error_type": error_type,
                "error_message": error_message,
                "runtime_context": runtime_context,
            },
        )

    def get_candidate_capabilities(
        self,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any] | None:
        """读取并短暂缓存 mandatory CandidateStore transport contract。"""
        now = time.monotonic()
        with self._candidate_capability_lock:
            cached = self._candidate_capability_cache
            if not force_refresh and cached is not None and cached[0] > now:
                return dict(cached[1]) if cached[1] is not None else None
            response = self._request_json("GET", "/candidates/capabilities")
            capability = self._normalize_candidate_capability(response)
            self._candidate_capability_cache = (
                now + CANDIDATE_CAPABILITY_CACHE_SECONDS,
                capability,
            )
            return dict(capability) if capability is not None else None

    @staticmethod
    def _normalize_candidate_capability(response: ApiResult) -> dict[str, Any] | None:
        data = response.data
        if not response.ok or not isinstance(data, dict):
            return None
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
        if data.get("required") is not True or any(
            data.get(key) != value
            for key, value in fixed.items()
            if key != "required"
        ):
            return None
        limits: dict[str, int] = {}
        for key in (
            "max_candidates_per_task_run",
            "max_raw_bytes",
            "max_manifest_bytes",
            "max_spool_bytes",
            "max_outcomes_per_candidate",
            "max_artifacts_per_candidate",
        ):
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                return None
            limits[key] = value
        return {**fixed, **limits}

    def capture_candidate(
        self,
        project_id: str,
        task_run_id: str,
        manifest_bytes: bytes,
        raw: bytes,
    ) -> ApiResult:
        """按 exact manifest header 与 exact body 提交一个 candidate/outcome frame。"""
        manifest_header = urlsafe_b64encode(manifest_bytes).rstrip(b"=").decode("ascii")
        path = (
            f"/projects/{quote(project_id, safe='')}/task-runs/"
            f"{quote(task_run_id, safe='')}/candidates"
        )
        try:
            response = self._session().request(
                "POST",
                self._url(path),
                data=raw,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Cairn-Candidate-Manifest": manifest_header,
                },
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning(
                "request failed method=POST path=%s error=%s",
                path,
                type(exc).__name__,
            )
            return ApiResult(status_code=0)
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                data = response.json()
            except ValueError:
                LOG.warning(
                    "invalid json response method=POST path=%s status=%s",
                    path,
                    response.status_code,
                )
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def get_execution_event_capabilities(
        self,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any] | None:
        """读取并短暂缓存 Server staging 能力；异常或结构不符时 fail closed。"""
        now = time.monotonic()
        with self._execution_event_capability_lock:
            cached = self._execution_event_capability_cache
            if not force_refresh and cached is not None and cached[0] > now:
                return dict(cached[1]) if cached[1] is not None else None
            response = self._request_json("GET", "/execution-events/capabilities")
            capability = self._normalize_execution_event_capability(response)
            self._execution_event_capability_cache = (
                now + EXECUTION_EVENT_CAPABILITY_CACHE_SECONDS,
                capability,
            )
            return dict(capability) if capability is not None else None

    @staticmethod
    def _normalize_execution_event_capability(response: ApiResult) -> dict[str, Any] | None:
        data = response.data
        if not response.ok or not isinstance(data, dict):
            return None
        if data.get("enabled") is not True or data.get("protocol") != "cairn-execution-event-v1":
            return None
        limits: dict[str, int] = {}
        for key in ("max_events", "max_event_bytes", "max_spool_bytes"):
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                return None
            limits[key] = value
        return {
            "enabled": True,
            "protocol": "cairn-execution-event-v1",
            **limits,
        }

    def create_execution_event(
        self,
        project_id: str,
        task_run_id: str,
        event: dict[str, object],
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/task-runs/{task_run_id}/execution-events",
            json=event,
        )

    def create_agent_event(
        self,
        project_id: str,
        *,
        task_run_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/agent-events",
            json={"task_run_id": task_run_id, "kind": kind, "payload": payload},
        )

    def apply_injection_payload_operations(
        self,
        project_id: str,
        operations: list[dict[str, Any]],
    ) -> ApiResult:
        # 机器凭据只由 Dispatcher transport 持有，Worker 仅生成无凭据 JSONL。
        return self._request_json(
            "POST",
            f"/projects/{project_id}/injection-payloads/operations",
            json={"operations": operations},
        )

    def list_artifacts(
        self,
        project_id: str,
        *,
        task_run_id: str | None = None,
    ) -> ApiResult:
        path = f"/projects/{project_id}/artifacts"
        if task_run_id is not None:
            path += f"?task_run_id={quote(task_run_id, safe='')}"
        return self._request_json("GET", path)

    def create_artifact(
        self,
        project_id: str,
        *,
        task_run_id: str | None,
        kind: str,
        content: str | bytes | BinaryIO,
        filename: str,
        metadata: dict[str, Any],
        category: str | None = None,
    ) -> ApiResult:
        if not isinstance(content, (str, bytes)):
            # 文件对象直接作为 requests data 发送，避免在 Dispatcher 内存中聚合或 base64 膨胀。
            return self._request_raw_artifact(
                project_id,
                task_run_id=task_run_id,
                kind=kind,
                content=content,
                filename=filename,
                metadata=metadata,
                category=category,
            )

        json_body: dict[str, Any] = {
            "task_run_id": task_run_id,
            "kind": kind,
            "filename": filename,
            "metadata": metadata,
        }
        if category is not None:
            json_body["category"] = category
        if isinstance(content, bytes):
            # 旧 JSON POST 保持 base64 二进制传输，文本 artifact 仍发送原始字符串。
            json_body["content_base64"] = b64encode(content).decode("ascii")
        else:
            json_body["content"] = content
        return self._request_json(
            "POST",
            f"/projects/{project_id}/artifacts",
            json=json_body,
        )

    def _request_raw_artifact(
        self,
        project_id: str,
        *,
        task_run_id: str | None,
        kind: str,
        content: BinaryIO,
        filename: str,
        metadata: dict[str, Any],
        category: str | None,
    ) -> ApiResult:
        path = f"/projects/{project_id}/artifacts/raw"
        params: dict[str, str] = {
            "kind": kind,
            "filename": filename,
            "metadata_json": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
        }
        if task_run_id is not None:
            params["task_run_id"] = task_run_id
        if category is not None:
            params["category"] = category
        try:
            response = self._session().post(
                self._url(path),
                params=params,
                data=content,
                headers={"Content-Type": "application/octet-stream"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=POST path=%s error=%s", path, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def dispatcher_heartbeat(
        self,
        *,
        dispatcher_id: str,
        status: str,
        config_hash: str,
        running_task_count: int,
        active_project_count: int,
        started_at: str | None = None,
        host: str | None = None,
        config_path: str | None = None,
        config_mtime: int | None = None,
        config_sha256: str | None = None,
        runtime: dict[str, Any] | None = None,
        container: dict[str, Any] | None = None,
        server_reachable_path: str | None = None,
        loop_started_at: str | None = None,
        loop_finished_at: str | None = None,
        loop_duration_ms: int | None = None,
        worker_cooldowns: list[dict[str, Any]] | None = None,
        worker_rejections: list[dict[str, Any]] | None = None,
        worker_pool: list[dict[str, Any]] | None = None,
        container_images: list[str] | None = None,
        cap_baseline: list[str] | None = None,
        declared_inventory: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> ApiResult:
        # heartbeat payload 是完整快照，Server 以 dispatcher_id upsert，不依赖增量语义。
        return self._request_json(
            "POST",
            "/dispatcher/heartbeat",
            json={
                "dispatcher_id": dispatcher_id,
                "status": status,
                "started_at": started_at,
                "config_hash": config_hash,
                "host": host,
                "config_path": config_path,
                "config_mtime": config_mtime,
                "config_sha256": config_sha256,
                "runtime": runtime or {},
                "container": container or {},
                "server_reachable_path": server_reachable_path,
                "loop_started_at": loop_started_at,
                "loop_finished_at": loop_finished_at,
                "loop_duration_ms": loop_duration_ms,
                "running_task_count": running_task_count,
                "active_project_count": active_project_count,
                "worker_cooldowns": worker_cooldowns or [],
                "worker_rejections": worker_rejections or [],
                "worker_pool": worker_pool or [],
                "container_images": container_images or [],
                "cap_baseline": cap_baseline,
                "declared_inventory": declared_inventory or {},
                "error_message": error_message,
            },
        )

    def _request_json(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
    ) -> ApiResult:
        try:
            response = self._session().request(
                method,
                self._url(path),
                json=json,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            # 写回失败要被 task common 记录为 protocol_write，而不是直接吞掉异常。
            LOG.warning("request failed method=%s path=%s error=%s", method, path, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                data = response.json()
            except ValueError:
                # 非法 JSON 只能视为缺失结构化响应，不能让 capability 或写回调用崩溃。
                LOG.warning(
                    "invalid json response method=%s path=%s status=%s",
                    method,
                    path,
                    response.status_code,
                )
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        # 凭据只进入 Dispatcher 到 Server 的 transport session，不加入 worker env 或请求日志。
        session.headers["Authorization"] = f"Bearer {self._token}"
        # dispatcher 使用线程池并发执行任务；每个线程持有自己的 session 以复用连接池。
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._local.session = session
        with self._sessions_lock:
            self._sessions[threading.get_ident()] = session
        return session
