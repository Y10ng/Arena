"""进程输出和声明 artifact 记录器。"""

from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
import re
from typing import BinaryIO, Iterator

from .common_base import *
from .process_runner import worker_process_failure


_SERVER_ARTIFACT_ID_RE = re.compile(r"^a[0-9]+$")


class UnresolvedArtifactEvidenceError(ValueError):
    """Worker 引用了无法映射到本轮声明或既有 Server Artifact 的证据。"""


@contextmanager
def _open_container_artifact(
    container_manager: ContainerManager,
    container_name: str,
    path: str,
) -> Iterator[BinaryIO]:
    materialize = getattr(container_manager, "materialize_file", None)
    if callable(materialize):
        with materialize(container_name, path) as materialized:
            with materialized.path.open("rb") as content:
                yield content
        return
    # 兼容旧 ContainerManager 测试替身；生产实现始终走 materialize_file 流式路径。
    raw = container_manager.read_file(container_name, path)
    payload = raw.encode("utf-8") if isinstance(raw, str) else raw
    with BytesIO(payload) as content:
        yield content


def _resolve_declared_artifact_evidence_ref(
    ref: EvidenceRef,
    declared_artifacts: ProcessArtifactRecord,
) -> EvidenceRef:
    # Worker 只能用本轮声明的精确 handle；既有 Server artifact_id 保持原值。
    identity = ref.artifact_id.strip()
    mapped_id = declared_artifacts.artifact_ids_by_handle.get(identity)
    if mapped_id is not None:
        return EvidenceRef(
            artifact_id=mapped_id,
            pointer=ref.pointer,
            verifier_run_id=ref.verifier_run_id,
        )
    if _SERVER_ARTIFACT_ID_RE.fullmatch(identity):
        return ref
    raise UnresolvedArtifactEvidenceError(
        f"unresolved artifact handle: {identity}"
    )


def resolve_declared_artifact_evidence_refs(
    refs: list[EvidenceRef],
    declared_artifacts: ProcessArtifactRecord,
) -> list[EvidenceRef]:
    return [
        _resolve_declared_artifact_evidence_ref(ref, declared_artifacts)
        for ref in refs
    ]


def resolve_optional_fact_evidence_refs(
    refs: list[EvidenceRef],
    declared_artifacts: ProcessArtifactRecord,
    *,
    field: str = "data.fact.evidence_refs",
) -> tuple[list[EvidenceRef], list[dict[str, object]]]:
    """解析 legacy Fact 的可选证据；只丢弃身份无法解析的单条引用。"""
    resolved: list[EvidenceRef] = []
    warnings: list[dict[str, object]] = []
    for index, ref in enumerate(refs):
        try:
            resolved.append(
                _resolve_declared_artifact_evidence_ref(ref, declared_artifacts)
            )
        except UnresolvedArtifactEvidenceError:
            identity = ref.artifact_id.strip()
            warnings.append(
                {
                    "type": "dropped_unresolved_optional_fact_evidence_ref",
                    "field": f"{field}[{index}]",
                    "reference": identity,
                    "reason": "unresolved_artifact_handle",
                    "message": "可选 Fact 证据引用无法解析，已丢弃",
                }
            )
    return resolved, warnings


def resolve_artifact_id_by_handle(
    handle: str | None,
    declared_artifacts: ProcessArtifactRecord,
) -> str | None:
    if not handle:
        return None
    return declared_artifacts.artifact_ids_by_handle.get(handle.strip())


def record_process_artifacts(
    client: CairnClient,
    project_id: str,
    task_run_id: str | None,
    *,
    phase: str,
    result: ProcessResult,
) -> ProcessArtifactRecord:
    if task_run_id is None:
        return ProcessArtifactRecord(refs=[])

    wire_phase = "healthcheck" if phase.endswith("healthcheck") else phase
    if worker_process_failure(
        result, phase=wire_phase, project_id=project_id, task_run_id=task_run_id, session=None,
    ) is not None:
        # 失败通道只发布独立 worker_failure 诊断，不把原始 provider/stderr 正文作交付物，
        # 也不将诊断投影伪装为 worker_stdout/stderr；成功业务证据保持原样。
        return ProcessArtifactRecord(refs=[])

    refs: list[EvidenceRef] = []
    write_failed = False
    timed_out = did_timeout(result)
    streams = _process_artifact_streams(phase, result, timed_out=timed_out)
    for stream, content, synthetic in streams:
        if content is None:
            if _process_stream_blank(result, stream):
                continue
            stored_content, storage_metadata = _bounded_process_result_content(result, stream)
        else:
            if not content.strip():
                continue
            stored_content, storage_metadata = _bounded_process_artifact_content(content)
        response = client.create_artifact(
            project_id,
            task_run_id=task_run_id,
            kind="log" if synthetic else "raw_output",
            content=stored_content,
            filename=f"{phase}-{stream}.log",
            metadata={
                "phase": phase,
                "stream": stream,
                "returncode": result.returncode,
                "timed_out": timed_out,
                "cancelled": result.cancelled,
                "cancel_reason": result.cancel_reason,
                "oom_killed": result.oom_killed,
                "oom_evidence_source": result.oom_evidence_source,
                "synthetic": synthetic,
                **storage_metadata,
            },
        )
        if not response.ok:
            # artifact 写入失败会回传 write_failed，由任务层映射为 artifact_write_failed。
            LOG.warning(
                "audit artifact create failed project=%s task_run=%s phase=%s stream=%s status=%s body=%s",
                project_id,
                task_run_id,
                phase,
                stream,
                response.status_code,
                response.text,
            )
            write_failed = True
            continue
        artifact_id = _artifact_id_from_response(response.data)
        if artifact_id is None:
            LOG.warning(
                "audit artifact create returned empty id project=%s task_run=%s phase=%s stream=%s",
                project_id,
                task_run_id,
                phase,
                stream,
            )
            write_failed = True
            continue
        refs.append(EvidenceRef(artifact_id=artifact_id, pointer=stream))
        event_response = client.create_agent_event(
            project_id,
            task_run_id=task_run_id,
            kind=f"worker_{stream}" if stream in {"stdout", "stderr"} else "worker_stderr",
            payload={
                "artifact_id": artifact_id,
                "phase": phase,
                "stream": stream,
                "size": len(stored_content.encode("utf-8")),
                "original_size": storage_metadata.get("original_size"),
                "truncated": storage_metadata.get("truncated", False),
                "returncode": result.returncode,
                "timed_out": timed_out,
                "cancelled": result.cancelled,
                "oom_killed": result.oom_killed,
                "oom_evidence_source": result.oom_evidence_source,
                "synthetic": synthetic,
            },
        )
        if not event_response.ok:
            LOG.warning(
                "audit event create failed project=%s task_run=%s phase=%s stream=%s status=%s body=%s",
                project_id,
                task_run_id,
                phase,
                stream,
                event_response.status_code,
                event_response.text,
            )
    return ProcessArtifactRecord(refs=refs, write_failed=write_failed)


def _bounded_process_artifact_content(content: str) -> tuple[str, dict[str, object]]:
    raw = content.encode("utf-8")
    if len(raw) <= PROCESS_ARTIFACT_MAX_BYTES:
        return content, {"truncated": False, "original_size": len(raw)}
    return _truncated_process_artifact(
        raw[:PROCESS_ARTIFACT_HEAD_BYTES].decode("utf-8", errors="replace"),
        raw[-PROCESS_ARTIFACT_TAIL_BYTES:].decode("utf-8", errors="replace"),
        original_size=len(raw),
    )


def _bounded_process_result_content(
    result: ProcessResult,
    stream: str,
) -> tuple[str, dict[str, object]]:
    size = result.stdout_size if stream == "stdout" else result.stderr_size
    if size <= PROCESS_ARTIFACT_MAX_BYTES:
        content = result.read_stdout() if stream == "stdout" else result.read_stderr()
        return content, {"truncated": False, "original_size": size}
    head = (
        result.stdout_head(PROCESS_ARTIFACT_HEAD_BYTES)
        if stream == "stdout"
        else result.stderr_head(PROCESS_ARTIFACT_HEAD_BYTES)
    )
    tail = (
        result.stdout_tail(PROCESS_ARTIFACT_TAIL_BYTES)
        if stream == "stdout"
        else result.stderr_tail(PROCESS_ARTIFACT_TAIL_BYTES)
    )
    return _truncated_process_artifact(head, tail, original_size=size)


def _truncated_process_artifact(
    head: str,
    tail: str,
    *,
    original_size: int,
) -> tuple[str, dict[str, object]]:
    marker = (
        "\n\n"
        f"[cairn: raw output truncated; original_size={original_size} bytes; "
        f"stored_head={PROCESS_ARTIFACT_HEAD_BYTES} bytes; stored_tail={PROCESS_ARTIFACT_TAIL_BYTES} bytes]"
        "\n\n"
    )
    return (
        head + marker + tail,
        {
            "truncated": True,
            "original_size": original_size,
            "stored_head_bytes": PROCESS_ARTIFACT_HEAD_BYTES,
            "stored_tail_bytes": PROCESS_ARTIFACT_TAIL_BYTES,
        },
    )


def _process_stream_blank(result: ProcessResult, stream: str) -> bool:
    return result.stdout_blank if stream == "stdout" else result.stderr_blank


def _process_artifact_streams(
    phase: str,
    result: ProcessResult,
    *,
    timed_out: bool,
) -> list[tuple[str, str | None, bool]]:
    streams: list[tuple[str, str | None, bool]] = [
        ("stdout", None, False),
        ("stderr", None, False),
    ]
    if not result.stdout_blank or not result.stderr_blank:
        return streams
    if result.returncode == 0 and not timed_out and not result.cancelled:
        return streams
    # 失败但无输出时合成诊断 artifact，避免审计页完全没有失败线索。
    diagnostic = "\n".join(
        [
            "Worker process finished without stdout or stderr.",
            f"phase={phase}",
            f"returncode={result.returncode}",
            f"timed_out={str(timed_out).lower()}",
            f"cancelled={str(result.cancelled).lower()}",
            f"cancel_reason={result.cancel_reason or ''}",
            f"oom_killed={str(result.oom_killed).lower()}",
            f"oom_evidence_source={result.oom_evidence_source or ''}",
            "",
        ]
    )
    streams.append(("diagnostic", diagnostic, True))
    return streams


def record_declared_artifacts(
    client: CairnClient,
    container_manager: ContainerManager,
    container_name: str,
    project_id: str,
    task_run_id: str | None,
    *,
    phase: str,
    artifacts: list[DeclaredArtifact],
) -> ProcessArtifactRecord:
    if task_run_id is None or not artifacts:
        return ProcessArtifactRecord(refs=[])

    refs: list[EvidenceRef] = []
    artifact_ids_by_handle: dict[str, str] = {}
    write_failed = False
    collect_failed = False
    for index, artifact in enumerate(artifacts):
        artifact_kind = normalize_declared_artifact_kind(artifact.kind)
        if artifact_kind is None:
            LOG.warning(
                "declared artifact kind unsupported project=%s task_run=%s phase=%s path=%s kind=%s",
                project_id,
                task_run_id,
                phase,
                artifact.path,
                artifact.kind,
            )
            collect_failed = True
            continue
        filename = PurePosixPath(artifact.path).name or f"{phase}-artifact-{index + 1}"
        try:
            # 声明 artifact 必须从容器文件系统取证，并通过宿主临时文件流式上传。
            with _open_container_artifact(container_manager, container_name, artifact.path) as content:
                response = client.create_artifact(
                    project_id,
                    task_run_id=task_run_id,
                    kind=artifact_kind,
                    content=content,
                    filename=filename,
                    metadata={
                        "phase": phase,
                        "source_path": artifact.path,
                        "declared_by_worker": True,
                    },
                )
        except Exception as exc:
            LOG.warning(
                "declared artifact read failed project=%s task_run=%s phase=%s path=%s error=%s",
                project_id,
                task_run_id,
                phase,
                artifact.path,
                exc,
            )
            collect_failed = True
            continue
        if not response.ok:
            LOG.warning(
                "declared artifact create failed project=%s task_run=%s phase=%s path=%s status=%s body=%s",
                project_id,
                task_run_id,
                phase,
                artifact.path,
                response.status_code,
                response.text,
            )
            write_failed = True
            collect_failed = True
            continue
        artifact_id = _artifact_id_from_response(response.data)
        if artifact_id is None:
            LOG.warning(
                "declared artifact create returned empty id project=%s task_run=%s phase=%s path=%s",
                project_id,
                task_run_id,
                phase,
                artifact.path,
            )
            write_failed = True
            collect_failed = True
            continue
        refs.append(EvidenceRef(artifact_id=artifact_id, pointer=artifact.handle))
        artifact_ids_by_handle[artifact.handle] = artifact_id
    return ProcessArtifactRecord(
        refs=refs,
        write_failed=write_failed,
        collect_failed=collect_failed,
        artifact_ids_by_handle=artifact_ids_by_handle,
    )
