"""Intent conclude 写回与释放工具。"""

from __future__ import annotations

import time
from collections.abc import Callable

from .common_base import *

from .protocol_records import record_protocol_write
from .write_outcomes import classify_write_response

GRAPH_WRITE_RETRY_ATTEMPTS = 3
GRAPH_WRITE_RETRY_BASE_SECONDS = 0.5


class CoverageEvidenceInsufficientError(ValueError):
    """当前 TaskRun 证据不足，不能向 Server 提交结构化 Coverage 结论。"""


class VerificationEvidenceInsufficientError(ValueError):
    """Verifier 证据无法解析为当前 TaskRun 的 Artifact，禁止写回 Server。"""


def _current_artifact_ids(declared_artifacts: object) -> set[str]:
    refs = getattr(declared_artifacts, "refs", [])
    handles = getattr(declared_artifacts, "artifact_ids_by_handle", {})
    return {
        artifact_id
        for artifact_id in (
            [getattr(ref, "artifact_id", "") for ref in refs]
            + list(handles.values())
        )
        if isinstance(artifact_id, str) and artifact_id
    }


def project_allows_conclude_fallback(client: CairnClient, project_id: str, *, worker_name: str, intent_id: str) -> bool:
    project = client.get_project(project_id)
    if project.project.status == "active":
        return True
    # fallback 写回前重新读项目状态，避免在项目停止后补写 fact。
    LOG.info(
        "skip conclude fallback because project is no longer active project=%s intent=%s worker=%s status=%s",
        project_id,
        intent_id,
        worker_name,
        project.project.status,
    )
    return False


def best_effort_release_reason(client: CairnClient, project_id: str, worker_name: str, *, task_run_id: str | None = None) -> None:
    response = client.release_reason(project_id, worker_name, **({"task_run_id": task_run_id} if task_run_id is not None else {}))
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "reason release failed project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released reason project=%s worker=%s", project_id, worker_name)
    else:
        LOG.info(
            "reason release skipped project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )


def build_vulnerability_for_conclude(
    payload: dict[str, object] | None,
    declared_artifacts: object,
    *,
    slot_key: str | None = None,
    require_current_run_reproduction: bool = False,
) -> dict[str, object] | None:
    """从 Worker payload 提取漏洞并把 packet Artifact handle 映射为 Server ID。"""
    from .artifact_records import resolve_artifact_id_by_handle
    from ..contracts import extract_vulnerability
    vuln = extract_vulnerability(payload) if isinstance(payload, dict) else None
    if vuln is None:
        return None
    packet_handle = vuln.get("packet_artifact_handle")
    packet_aid = resolve_artifact_id_by_handle(
        packet_handle,
        declared_artifacts,
    )
    current_artifact_ids = _current_artifact_ids(declared_artifacts)
    if packet_handle and packet_aid not in current_artifact_ids:
        from .artifact_records import UnresolvedArtifactEvidenceError

        raise UnresolvedArtifactEvidenceError(
            f"unresolved packet artifact handle: {packet_handle}"
        )
    if packet_aid not in current_artifact_ids:
        packet_aid = None
    complete_exchange = bool(
        str(vuln.get("repro_request") or "").strip()
        and str(vuln.get("repro_response") or "").strip()
    )
    if (
        require_current_run_reproduction
        and packet_aid is None
        and not complete_exchange
    ):
        raise CoverageEvidenceInsufficientError(
            "finding requires a current-run packet artifact or complete request/response"
        )
    return {
        "title": vuln["title"],
        "severity": vuln["severity"],
        "vuln_type": vuln["vuln_type"],
        "target": vuln["target"],
        "remediation": vuln["remediation"],
        "confidence": vuln["confidence"],
        "packet_artifact_id": packet_aid,
        "repro_request": vuln.get("repro_request", ""),
        "repro_response": vuln.get("repro_response", ""),
        "identity": vuln.get("identity"),
        "slot_key": slot_key,
    }


def build_discovered_surfaces_for_conclude(payload, declared_artifacts):
    """解析 Surface 发现并将 Worker Artifact handle 转换为 Server ID。"""
    from .artifact_records import resolve_declared_artifact_evidence_refs
    from ..contracts import extract_discovered_surfaces

    surfaces = extract_discovered_surfaces(payload) if isinstance(payload, dict) else []
    resolved: list[dict[str, object]] = []
    for surface in surfaces:
        item = dict(surface)
        refs = [EvidenceRef.model_validate(value) for value in item.get("evidence_refs", [])]
        item["evidence_refs"] = [
            ref.model_dump(exclude_none=True)
            for ref in resolve_declared_artifact_evidence_refs(
                refs,
                declared_artifacts,
            )
        ]
        resolved.append(item)
    return resolved


def build_coverage_scope_disposition_for_conclude(payload, declared_artifacts):
    """解析零 Surface 处置并严格解析其中的 Artifact handle。"""
    from .artifact_records import resolve_declared_artifact_evidence_refs
    from ..contracts import extract_coverage_scope_disposition

    disposition = (
        extract_coverage_scope_disposition(payload)
        if isinstance(payload, dict)
        else None
    )
    if disposition is None:
        return None
    refs = [
        EvidenceRef.model_validate(value)
        for value in disposition.get("evidence_refs", [])
    ]
    disposition["evidence_refs"] = [
        ref.model_dump(exclude_none=True)
        for ref in resolve_declared_artifact_evidence_refs(
            refs,
            declared_artifacts,
        )
    ]
    return disposition


def build_verification_result_for_conclude(payload, declared_artifacts):
    """解析 Verifier 结论并将证据别名转换为当前 TaskRun 的 Artifact ID。"""
    from .artifact_records import resolve_declared_artifact_evidence_refs
    from ..contracts import extract_verification_result

    result = extract_verification_result(payload) if isinstance(payload, dict) else None
    if result is None:
        return None, []
    try:
        refs = resolve_declared_artifact_evidence_refs(
            [EvidenceRef.model_validate(value) for value in result.get("evidence_refs", [])],
            declared_artifacts,
        )
    except ValueError as exc:
        raise VerificationEvidenceInsufficientError(str(exc)) from exc
    if not refs:
        raise VerificationEvidenceInsufficientError(
            "verification_result requires resolved current-run evidence"
        )
    result["evidence_refs"] = [ref.model_dump(exclude_none=True) for ref in refs]
    result["notes"] = str(result.get("notes") or "").strip()
    return result, refs


def build_assessment_record_for_conclude(payload, declared_artifacts):
    """实验记录与 Fact 一同提交；句柄不能漏进 Server 持久化结构。"""
    from cairn.assessment_contracts import ExperimentRecord
    from .artifact_records import resolve_declared_artifact_evidence_refs

    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    raw = data.get("assessment_record") if isinstance(data, dict) else None
    if raw is None:
        return None, []
    try:
        record = ExperimentRecord.model_validate(raw)
        refs = resolve_declared_artifact_evidence_refs([
            EvidenceRef(
                artifact_id=ref.artifact_id or ref.artifact_handle,
                pointer=ref.pointer,
                verifier_run_id=ref.verifier_run_id,
            ) for ref in record.evidence_refs
        ], declared_artifacts)
    except ValueError as exc:
        raise CoverageEvidenceInsufficientError("experiment evidence could not be resolved") from exc
    wire = record.model_dump(by_alias=True, exclude_none=True)
    wire["evidence_refs"] = [ref.model_dump(exclude_none=True) for ref in refs]
    return wire, refs


def build_coverage_result_for_conclude(
    payload,
    declared_artifacts,
    *,
    require_current_run_evidence: bool = False,
):
    """解析并 resolve Cell 证据引用，生成 Server conclude 可直接验证的结构化结果。"""
    from .artifact_records import resolve_declared_artifact_evidence_refs
    from ..contracts import extract_coverage_result

    coverage_result = extract_coverage_result(payload) if isinstance(payload, dict) else None
    if coverage_result is None:
        return None, []
    refs = resolve_declared_artifact_evidence_refs(
        coverage_result.get("evidence_refs", []), declared_artifacts
    )
    final_refs = refs
    if require_current_run_evidence:
        current_artifact_ids = _current_artifact_ids(declared_artifacts)
        final_refs = [ref for ref in refs if ref.artifact_id in current_artifact_ids]
        if (
            coverage_result.get("outcome") in {"tested_no_finding", "finding"}
            and not final_refs
        ):
            raise CoverageEvidenceInsufficientError(
                "coverage result requires current-run artifact evidence"
            )
    coverage_result["evidence_refs"] = [ref.model_dump() for ref in final_refs]
    return coverage_result, final_refs


def write_conclude_result(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
    evidence: list[EvidenceRef] | None = None,
    task_run_id: str | None = None,
    vulnerability: dict[str, object] | None = None,
    coverage_result: dict[str, object] | None = None,
    discovered_surfaces: list[dict[str, object]] | None = None,
    coverage_scope_disposition: dict[str, object] | None = None,
    verification_result: dict[str, object] | None = None,
    assessment_record: dict[str, object] | None = None,
) -> str:
    return write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
        evidence=evidence,
        task_run_id=task_run_id,
        vulnerability=vulnerability,
        coverage_result=coverage_result,
        discovered_surfaces=discovered_surfaces,
        coverage_scope_disposition=coverage_scope_disposition,
        verification_result=verification_result,
        assessment_record=assessment_record,
    ).status


def write_conclude_result_with_fact_id(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
    evidence: list[EvidenceRef] | None = None,
    task_run_id: str | None = None,
    vulnerability: dict[str, object] | None = None,
    coverage_result: dict[str, object] | None = None,
    discovered_surfaces: list[dict[str, object]] | None = None,
    coverage_scope_disposition: dict[str, object] | None = None,
    verification_result: dict[str, object] | None = None,
    assessment_record: dict[str, object] | None = None,
) -> ConcludeWriteResult:
    evidence_refs = evidence or []
    # conclude 写回统一入口会同时写协议事件，并在失败时尽力释放 intent。
    # vulnerability 为 dispatcher resolve 后的 server 格式 dict（含 packet_artifact_id），透传自动确认。
    response = _conclude_with_transient_retries(
        client,
        project_id,
        intent_id,
        worker_name,
        description,
        evidence_refs,
        task_run_id=task_run_id,
        vulnerability=vulnerability,
        coverage_result=coverage_result,
        discovered_surfaces=discovered_surfaces,
        coverage_scope_disposition=coverage_scope_disposition,
        verification_result=verification_result,
        assessment_record=assessment_record,
    )
    if response.ok:
        fact_id: str | None = None
        if isinstance(response.data, dict):
            fact = response.data.get("fact")
            if isinstance(fact, dict):
                candidate = fact.get("id")
                if isinstance(candidate, str) and candidate:
                    fact_id = candidate
        record_protocol_write(
            client,
            project_id,
            task_run_id,
            operation="conclude",
            phase=source,
            response=response,
            intent_id=intent_id,
            fact_id=fact_id,
            evidence_count=len(evidence_refs),
        )
        if total_ms is None:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
            )
        else:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s total_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
                total_ms,
            )
        return ConcludeWriteResult(status="success", fact_id=fact_id)
    record_protocol_write(
        client,
        project_id,
        task_run_id,
        operation="conclude",
        phase=source,
        response=response,
        status="skipped" if response.status_code == 403 else "failed",
        intent_id=intent_id,
        evidence_count=len(evidence_refs),
    )
    if response.status_code == 403:
        LOG.info(
            "project became inactive during conclude project=%s intent=%s worker=%s",
            project_id,
            intent_id,
            worker_name,
        )
    else:
        LOG.warning(
            "conclude write failed project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
    best_effort_release(client, project_id, intent_id, worker_name, task_run_id=task_run_id)
    # 确定性拒绝不能冒充暂时写回失败，否则 Server 会重新执行已完成的模型工作。
    disposition = classify_write_response(response)
    return ConcludeWriteResult(
        status=disposition.error_type or "business_acceptance",
        fact_id=None,
    )


def _conclude_with_transient_retries(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    evidence_refs: list[EvidenceRef],
    *,
    task_run_id: str | None,
    vulnerability: dict[str, object] | None = None,
    coverage_result: dict[str, object] | None = None,
    discovered_surfaces: list[dict[str, object]] | None = None,
    coverage_scope_disposition: dict[str, object] | None = None,
    verification_result: dict[str, object] | None = None,
    assessment_record: dict[str, object] | None = None,
) -> ApiResult:
    return retry_graph_write(lambda: client.conclude_with_evidence(
        project_id,
        intent_id,
        worker_name,
        description,
        evidence_refs,
        task_run_id=task_run_id,
        vulnerability=vulnerability,
        coverage_result=coverage_result,
        discovered_surfaces=discovered_surfaces,
        coverage_scope_disposition=coverage_scope_disposition,
        verification_result=verification_result,
        assessment_record=assessment_record,
    ))


def retry_graph_write(operation: Callable[[], ApiResult]) -> ApiResult:
    """仅对支持 Server 收据幂等的写回做有界重试，不重复模型或目标执行。"""
    response = operation()
    attempt = 1
    while _should_retry_graph_write(response) and attempt < GRAPH_WRITE_RETRY_ATTEMPTS:
        sleep_seconds = GRAPH_WRITE_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
        LOG.warning(
            "graph write transient failure status=%s retry_in=%.1fs attempt=%s/%s",
            response.status_code,
            sleep_seconds,
            attempt + 1,
            GRAPH_WRITE_RETRY_ATTEMPTS,
        )
        time.sleep(sleep_seconds)
        response = operation()
        attempt += 1
    return response


def _should_retry_graph_write(response: ApiResult) -> bool:
    return classify_write_response(response).retry_same_write


def best_effort_release(client: CairnClient, project_id: str, intent_id: str, worker_name: str, *, task_run_id: str | None = None) -> None:
    response = client.release(project_id, intent_id, worker_name, **({"task_run_id": task_run_id} if task_run_id is not None else {}))
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "release failed project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released intent project=%s intent=%s worker=%s", project_id, intent_id, worker_name)
    else:
        LOG.info(
            "release skipped project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )


def conclude_or_release_on_failure(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    task_run_id: str | None = None,
    *,
    enabled: bool = False,
    structured: bool = False,
    slot_key: str | None = None,
) -> None:
    """处理终态运行失败，避免遗留被 worker 占用的 Intent。

    结构化评估的运行故障不是目标 blocker，只释放租约并保留 logical attempt 供受控重试。
    非结构化 failure fact 保持既有行为；默认场景仍直接 best-effort release。
    """
    if not enabled or structured:
        best_effort_release(client, project_id, intent_id, worker_name, task_run_id=task_run_id)
        return
    coverage_result = None
    response = client.conclude_with_evidence(
        project_id,
        intent_id,
        worker_name,
        description,
        [],
        task_run_id=task_run_id,
        conclusion="failed",
        coverage_result=coverage_result,
    )
    if response.ok:
        LOG.info(
            "explore failure concluded project=%s intent=%s worker=%s description=%s",
            project_id,
            intent_id,
            worker_name,
            description,
        )
        return
    if response.status_code in (403, 409):
        LOG.info(
            "failure conclude skipped (%s), falling back to release project=%s intent=%s",
            response.status_code,
            project_id,
            intent_id,
        )
    else:
        LOG.warning(
            "failure conclude failed project=%s intent=%s worker=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
            response.text,
        )
    best_effort_release(client, project_id, intent_id, worker_name, task_run_id=task_run_id)
