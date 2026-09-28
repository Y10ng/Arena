"""TaskRun 候选原文、结果诊断和不可变捕获收据。"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, AsyncIterable
from uuid import uuid4

from fastapi import HTTPException

from cairn.dispatcher.worker_protocol.submission import (
    CANDIDATE_OUTCOME_CODES,
    SUBMISSION_ISSUE_KEYWORDS,
    submission_schema_paths,
)
from cairn.server.auth import RequestPrincipal
from cairn.server.db import get_conn
from cairn.server.schemas.candidates import (
    CANDIDATE_MAX_CANDIDATES_PER_TASK_RUN,
    CANDIDATE_MAX_MANIFEST_BYTES,
    CANDIDATE_MAX_OUTCOMES_PER_CANDIDATE,
    CANDIDATE_MAX_RAW_BYTES,
    CANDIDATE_SOURCE_FORMAT,
    CandidateCapabilities,
    CandidateFrameManifest,
    CandidateGenerationDisposition,
    CandidateMetadata,
    CandidateOutcomeManifest,
    CandidateReceipt,
    CandidateReceiptDisposition,
    canonical_json_bytes,
    canonical_value_sha256,
)

from .audit_task_runs import authenticated_execution_owner
from .time_utils import utcnow

_PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN PGP PRIVATE KEY BLOCK-----",
)


def candidate_capabilities() -> CandidateCapabilities:
    return CandidateCapabilities()


async def read_candidate_raw(stream: AsyncIterable[bytes]) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in stream:
        size += len(chunk)
        if size > CANDIDATE_MAX_RAW_BYTES:
            raise HTTPException(413, "candidate_raw_too_large")
        chunks.append(bytes(chunk))
    return b"".join(chunks)


def _runtime_context(row: Any) -> dict[str, Any]:
    try:
        loaded = json.loads(row["runtime_context_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(409, "candidate_execution_delivery_unavailable") from None
    if not isinstance(loaded, dict):
        raise HTTPException(409, "candidate_execution_delivery_unavailable")
    return loaded


def _generation_disposition(
    run_generation: int,
    project_generation: int,
) -> CandidateGenerationDisposition:
    if run_generation < 0:
        raise HTTPException(409, "candidate_generation_invalid")
    if run_generation > project_generation:
        raise HTTPException(409, "candidate_future_generation")
    if run_generation == project_generation:
        return "current_at_receipt"
    return "historical_at_receipt"


def _load_locked_execution(conn, project_id: str, task_run_id: str) -> tuple[Any, Any]:
    # 项目写锁把计数、parent、重放和并发唯一键判定串行化。
    locked = conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
    if locked.rowcount != 1:
        raise HTTPException(404, "Project not found")
    project = conn.execute(
        "SELECT * FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    run = conn.execute(
        "SELECT * FROM task_runs WHERE project_id = ? AND id = ?",
        (project_id, task_run_id),
    ).fetchone()
    if run is None:
        raise HTTPException(404, "Task run not found")
    return project, run


def _verified_delivery(
    run: Any,
    *,
    principal: RequestPrincipal,
) -> tuple[str, dict[str, Any]]:
    context = _runtime_context(run)
    owner = context.get("dispatcher_id")
    if not isinstance(owner, str) or not owner:
        raise HTTPException(403, "execution_owner_mismatch")
    verified_dispatcher_id = authenticated_execution_owner(principal, owner)
    delivery = context.get("execution_delivery")
    if not isinstance(delivery, dict):
        raise HTTPException(409, "candidate_execution_delivery_unavailable")
    required = {
        "session_id",
        "phase",
        "deadline_at",
        "container_id",
        "model_binding_sha256",
        "result_schema_sha256",
        "binding",
        "inputs",
    }
    if set(delivery) != required:
        raise HTTPException(409, "candidate_execution_delivery_unavailable")
    result_schema_sha256 = delivery["result_schema_sha256"]
    if (
        not isinstance(result_schema_sha256, str)
        or len(result_schema_sha256) != 64
        or any(character not in "0123456789abcdef" for character in result_schema_sha256)
    ):
        raise HTTPException(409, "candidate_execution_delivery_unavailable")
    return verified_dispatcher_id, delivery


def _require_candidate_delivery(
    run: Any,
    manifest: CandidateFrameManifest,
    *,
    principal: RequestPrincipal,
) -> tuple[str, dict[str, Any]]:
    verified_dispatcher_id, delivery = _verified_delivery(run, principal=principal)
    if manifest.phase != delivery["phase"]:
        raise HTTPException(409, "candidate_phase_mismatch")
    if manifest.session != delivery["session_id"] or manifest.session != run["session_id"]:
        raise HTTPException(409, "candidate_session_mismatch")
    if manifest.binding_sha256 != canonical_value_sha256(delivery["binding"]):
        raise HTTPException(409, "candidate_binding_digest_mismatch")
    if manifest.result_schema_sha256 != delivery["result_schema_sha256"]:
        raise HTTPException(409, "candidate_result_schema_digest_mismatch")
    return verified_dispatcher_id, delivery


def _require_safe_outcome_diagnostic(
    *,
    code: str,
    path: str | None,
    keyword: str | None,
    count: int | None,
    artifact_status: str,
    binding_status: str,
    allowed_paths: frozenset[str] | None,
    status_code: int,
) -> None:
    valid = code in CANDIDATE_OUTCOME_CODES and count in {None, 1}
    valid = valid and ((path is None) == (keyword is None))
    if path is not None:
        valid = valid and (
            code == "submission_invalid"
            and keyword in SUBMISSION_ISSUE_KEYWORDS
            and (path == "$" or path.startswith("/"))
            and count == 1
            and artifact_status == "not_evaluated"
            and binding_status == "not_evaluated"
        )
        if allowed_paths is not None:
            valid = valid and path in allowed_paths
        if keyword in {"strict_json", "input_size"}:
            valid = valid and path == "$"
    if code in {"locally_validated", "submission_invalid"}:
        valid = valid and count == 1
    if not valid:
        raise HTTPException(status_code, "candidate_outcome_diagnostic_invalid")


def _candidate_receipt(
    row: Any,
    *,
    disposition: CandidateReceiptDisposition,
) -> CandidateReceipt:
    return CandidateReceipt(
        record_kind="candidate",
        candidate_id=row["id"],
        producer_candidate_key=row["producer_candidate_key"],
        request_sha256=row["request_sha256"],
        raw_sha256=row["raw_sha256"],
        disposition=disposition,
        generation_disposition=row["generation_disposition"],
        received_at=row["received_at"],
    )


def _outcome_receipt(
    row: Any,
    *,
    disposition: CandidateReceiptDisposition,
) -> CandidateReceipt:
    return CandidateReceipt(
        record_kind="outcome",
        candidate_id=row["candidate_id"],
        producer_candidate_key=row["producer_candidate_key"],
        outcome_sequence=row["outcome_sequence"],
        request_sha256=row["request_sha256"],
        raw_sha256=None,
        disposition=disposition,
        generation_disposition=row["generation_disposition"],
        received_at=row["received_at"],
    )


def capture_candidate(
    project_id: str,
    task_run_id: str,
    manifest: CandidateFrameManifest,
    manifest_bytes: bytes,
    raw: bytes,
    *,
    principal: RequestPrincipal,
) -> CandidateReceipt:
    if manifest.project_id != project_id or manifest.task_run_id != task_run_id:
        raise HTTPException(409, "candidate_route_identity_mismatch")
    if len(manifest_bytes) > CANDIDATE_MAX_MANIFEST_BYTES:
        raise HTTPException(413, "candidate_manifest_too_large")
    if len(raw) != manifest.raw_size_bytes:
        raise HTTPException(422, "candidate_raw_size_mismatch")
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise HTTPException(422, "candidate_raw_utf8_invalid") from None
    raw_sha256 = sha256(raw).hexdigest()
    if raw_sha256 != manifest.raw_sha256:
        raise HTTPException(422, "candidate_raw_digest_mismatch")
    request_sha256 = sha256(manifest_bytes + raw).hexdigest()
    artifact_identities_json = canonical_json_bytes(
        [artifact.model_dump(mode="json") for artifact in manifest.artifacts]
    ).decode("utf-8")

    with get_conn() as conn:
        project, run = _load_locked_execution(conn, project_id, task_run_id)
        verified_dispatcher_id, delivery = _require_candidate_delivery(
            run,
            manifest,
            principal=principal,
        )
        run_generation = int(run["execution_generation"])
        generation_disposition = _generation_disposition(
            run_generation,
            int(project["execution_generation"]),
        )

        existing = conn.execute(
            """
            SELECT * FROM task_run_candidates
            WHERE project_id = ? AND task_run_id = ? AND producer_candidate_key = ?
            """,
            (project_id, task_run_id, manifest.producer_candidate_key),
        ).fetchone()
        if existing is not None:
            if existing["request_sha256"] != request_sha256:
                raise HTTPException(409, "candidate_receipt_conflict")
            return _candidate_receipt(existing, disposition="replayed")

        if any(marker in raw for marker in _PRIVATE_KEY_MARKERS):
            raise HTTPException(422, "candidate_sensitive_content_rejected")

        latest_capture_candidate = conn.execute(
            """
            SELECT id, producer_candidate_key, assistant_turn, content_index
            FROM task_run_candidates
            WHERE project_id = ? AND task_run_id = ? AND capture_id = ?
            ORDER BY assistant_turn DESC, content_index DESC
            LIMIT 1
            """,
            (project_id, task_run_id, manifest.capture_id),
        ).fetchone()
        parent_candidate_id = None
        if latest_capture_candidate is None:
            if manifest.parent_producer_candidate_key is not None:
                parent = conn.execute(
                    """
                    SELECT 1 FROM task_run_candidates
                    WHERE project_id = ? AND task_run_id = ?
                      AND producer_candidate_key = ?
                    """,
                    (
                        project_id,
                        task_run_id,
                        manifest.parent_producer_candidate_key,
                    ),
                ).fetchone()
                error = (
                    "candidate_parent_missing"
                    if parent is None
                    else "candidate_parent_capture_mismatch"
                )
                raise HTTPException(409, error)
        else:
            expected_parent = latest_capture_candidate["producer_candidate_key"]
            if manifest.parent_producer_candidate_key != expected_parent:
                if manifest.parent_producer_candidate_key is not None:
                    parent = conn.execute(
                        """
                        SELECT 1 FROM task_run_candidates
                        WHERE project_id = ? AND task_run_id = ?
                          AND producer_candidate_key = ?
                        """,
                        (
                            project_id,
                            task_run_id,
                            manifest.parent_producer_candidate_key,
                        ),
                    ).fetchone()
                    if parent is None:
                        raise HTTPException(409, "candidate_parent_missing")
                raise HTTPException(409, "candidate_parent_lineage_mismatch")
            previous_position = (
                int(latest_capture_candidate["assistant_turn"]),
                int(latest_capture_candidate["content_index"]),
            )
            if (manifest.assistant_turn, manifest.content_index) <= previous_position:
                raise HTTPException(409, "candidate_position_not_monotonic")
            parent_candidate_id = latest_capture_candidate["id"]

        candidate_count = conn.execute(
            """
            SELECT COUNT(*) AS count FROM task_run_candidates
            WHERE project_id = ? AND task_run_id = ?
            """,
            (project_id, task_run_id),
        ).fetchone()["count"]
        if int(candidate_count) >= CANDIDATE_MAX_CANDIDATES_PER_TASK_RUN:
            raise HTTPException(409, "candidate_limit_reached")

        candidate_id = f"cnd_{uuid4().hex}"
        received_at = utcnow()
        cursor = conn.execute(
            """
            INSERT INTO task_run_candidates (
                id, project_id, task_run_id, protocol, source_format,
                capture_id, producer_candidate_key, parent_candidate_id,
                assistant_turn, content_index, phase, session_id,
                binding_sha256, result_schema_sha256, delivery_sha256,
                artifact_identities_json, artifact_count,
                raw_bytes, raw_size_bytes, raw_sha256,
                request_sha256, manifest_json, verified_dispatcher_id,
                execution_generation, generation_disposition, received_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT (project_id, task_run_id, producer_candidate_key) DO NOTHING
            """,
            (
                candidate_id,
                project_id,
                task_run_id,
                manifest.protocol,
                CANDIDATE_SOURCE_FORMAT,
                manifest.capture_id,
                manifest.producer_candidate_key,
                parent_candidate_id,
                manifest.assistant_turn,
                manifest.content_index,
                manifest.phase,
                manifest.session,
                manifest.binding_sha256,
                manifest.result_schema_sha256,
                canonical_value_sha256(delivery),
                artifact_identities_json,
                len(manifest.artifacts),
                raw,
                len(raw),
                raw_sha256,
                request_sha256,
                manifest_bytes.decode("utf-8"),
                verified_dispatcher_id,
                run_generation,
                generation_disposition,
                received_at,
            ),
        )
        stored = conn.execute(
            """
            SELECT * FROM task_run_candidates
            WHERE project_id = ? AND task_run_id = ? AND producer_candidate_key = ?
            """,
            (project_id, task_run_id, manifest.producer_candidate_key),
        ).fetchone()
        if stored is None:
            raise RuntimeError("candidate receipt was not persisted")
        if stored["request_sha256"] != request_sha256:
            raise HTTPException(409, "candidate_receipt_conflict")
        disposition: CandidateReceiptDisposition = (
            "received" if cursor.rowcount == 1 else "replayed"
        )
        return _candidate_receipt(stored, disposition=disposition)


def capture_candidate_outcome(
    project_id: str,
    task_run_id: str,
    manifest: CandidateOutcomeManifest,
    manifest_bytes: bytes,
    raw: bytes,
    *,
    principal: RequestPrincipal,
) -> CandidateReceipt:
    if raw:
        raise HTTPException(422, "candidate_outcome_raw_must_be_empty")
    if len(manifest_bytes) > CANDIDATE_MAX_MANIFEST_BYTES:
        raise HTTPException(413, "candidate_manifest_too_large")
    request_sha256 = sha256(manifest_bytes).hexdigest()

    with get_conn() as conn:
        project, run = _load_locked_execution(conn, project_id, task_run_id)
        verified_dispatcher_id, delivery = _verified_delivery(run, principal=principal)
        run_generation = int(run["execution_generation"])
        generation_disposition = _generation_disposition(
            run_generation,
            int(project["execution_generation"]),
        )
        candidate = conn.execute(
            """
            SELECT id, capture_id, phase, verified_dispatcher_id,
                   execution_generation, delivery_sha256
            FROM task_run_candidates
            WHERE project_id = ? AND task_run_id = ? AND producer_candidate_key = ?
            """,
            (project_id, task_run_id, manifest.producer_candidate_key),
        ).fetchone()
        if candidate is None:
            raise HTTPException(409, "candidate_outcome_parent_missing")
        if candidate["capture_id"] != manifest.capture_id:
            raise HTTPException(409, "candidate_capture_identity_mismatch")
        if candidate["verified_dispatcher_id"] != verified_dispatcher_id:
            raise HTTPException(403, "execution_owner_mismatch")
        if int(candidate["execution_generation"]) != run_generation:
            raise HTTPException(409, "candidate_outcome_generation_mismatch")
        if candidate["delivery_sha256"] != canonical_value_sha256(delivery):
            raise HTTPException(409, "candidate_outcome_delivery_mismatch")
        existing = conn.execute(
            """
            SELECT * FROM task_run_candidate_outcomes
            WHERE candidate_id = ? AND outcome_sequence = ?
            """,
            (candidate["id"], manifest.outcome_sequence),
        ).fetchone()
        if existing is not None:
            if existing["request_sha256"] != request_sha256:
                raise HTTPException(409, "candidate_outcome_receipt_conflict")
            # 已接收 outcome 的 exact replay 只依赖冻结收据，不能被未来部署的 Schema 路径变化推翻。
            from .assessment_convergence import advance_goal_driven_convergence

            advance_goal_driven_convergence(conn, project_id)
            return _outcome_receipt(existing, disposition="replayed")

        _require_safe_outcome_diagnostic(
            code=manifest.code,
            path=manifest.path,
            keyword=manifest.keyword,
            count=manifest.count,
            artifact_status=manifest.artifact_status,
            binding_status=manifest.binding_status,
            allowed_paths=submission_schema_paths(candidate["phase"]),
            status_code=422,
        )

        outcome_state = conn.execute(
            """
            SELECT COUNT(*) AS count, COALESCE(MAX(outcome_sequence), 0) AS maximum
            FROM task_run_candidate_outcomes
            WHERE candidate_id = ?
            """,
            (candidate["id"],),
        ).fetchone()
        if int(outcome_state["count"]) >= CANDIDATE_MAX_OUTCOMES_PER_CANDIDATE:
            raise HTTPException(409, "candidate_outcome_limit_reached")
        if manifest.outcome_sequence != int(outcome_state["maximum"]) + 1:
            raise HTTPException(409, "candidate_outcome_sequence_mismatch")

        outcome_id = f"cno_{uuid4().hex}"
        received_at = utcnow()
        cursor = conn.execute(
            """
            INSERT INTO task_run_candidate_outcomes (
                id, candidate_id, project_id, task_run_id, protocol,
                producer_candidate_key, outcome_sequence, code,
                diagnostic_path, diagnostic_keyword, diagnostic_count,
                artifact_status, binding_status, request_sha256,
                manifest_json, verified_dispatcher_id, execution_generation,
                generation_disposition, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (candidate_id, outcome_sequence) DO NOTHING
            """,
            (
                outcome_id,
                candidate["id"],
                project_id,
                task_run_id,
                manifest.protocol,
                manifest.producer_candidate_key,
                manifest.outcome_sequence,
                manifest.code,
                manifest.path,
                manifest.keyword,
                manifest.count,
                manifest.artifact_status,
                manifest.binding_status,
                request_sha256,
                manifest_bytes.decode("utf-8"),
                verified_dispatcher_id,
                run_generation,
                generation_disposition,
                received_at,
            ),
        )
        stored = conn.execute(
            """
            SELECT * FROM task_run_candidate_outcomes
            WHERE candidate_id = ? AND outcome_sequence = ?
            """,
            (candidate["id"], manifest.outcome_sequence),
        ).fetchone()
        if stored is None:
            raise RuntimeError("candidate outcome receipt was not persisted")
        if stored["request_sha256"] != request_sha256:
            raise HTTPException(409, "candidate_outcome_receipt_conflict")
        disposition: CandidateReceiptDisposition = (
            "received" if cursor.rowcount == 1 else "replayed"
        )
        from .assessment_convergence import advance_goal_driven_convergence

        advance_goal_driven_convergence(conn, project_id)
        return _outcome_receipt(stored, disposition=disposition)


def _metadata(row: Any) -> CandidateMetadata:
    if row["latest_outcome_code"] is not None:
        # immutable 收据只复核有限诊断形状；历史路径不能借用当前部署的 Schema 重新解释。
        _require_safe_outcome_diagnostic(
            code=row["latest_outcome_code"],
            path=row["latest_outcome_path"],
            keyword=row["latest_outcome_keyword"],
            count=row["latest_outcome_count"],
            artifact_status=row["artifact_status"],
            binding_status=row["binding_status"],
            allowed_paths=None,
            status_code=409,
        )
    return CandidateMetadata(
        candidate_id=row["id"],
        task_run_id=row["task_run_id"],
        protocol=row["protocol"],
        phase=row["phase"],
        assistant_turn=row["assistant_turn"],
        content_index=row["content_index"],
        parent_candidate_id=row["parent_candidate_id"],
        raw_size_bytes=row["raw_size_bytes"],
        received_at=row["received_at"],
        execution_generation=row["execution_generation"],
        generation_disposition=row["generation_disposition"],
        latest_outcome_code=row["latest_outcome_code"],
        latest_outcome_path=row["latest_outcome_path"],
        latest_outcome_keyword=row["latest_outcome_keyword"],
        latest_outcome_count=row["latest_outcome_count"],
        artifact_status=row["artifact_status"],
        binding_status=row["binding_status"],
    )


def list_candidates(
    project_id: str,
    task_run_id: str,
    *,
    limit: int,
    offset: int,
) -> list[CandidateMetadata]:
    with get_conn() as conn:
        run = conn.execute(
            "SELECT 1 FROM task_runs WHERE project_id = ? AND id = ?",
            (project_id, task_run_id),
        ).fetchone()
        if run is None:
            raise HTTPException(404, "Task run not found")
        # 读模型明确列字段，禁止普通列表查询把 raw、producer key 或内部 digest 带出。
        rows = conn.execute(
            """
            SELECT c.id, c.task_run_id, c.protocol, c.phase,
                   c.assistant_turn, c.content_index, c.parent_candidate_id,
                   c.raw_size_bytes, c.received_at, c.execution_generation,
                   c.generation_disposition,
                   o.code AS latest_outcome_code,
                   o.diagnostic_path AS latest_outcome_path,
                   o.diagnostic_keyword AS latest_outcome_keyword,
                   o.diagnostic_count AS latest_outcome_count,
                   o.artifact_status, o.binding_status
            FROM task_run_candidates c
            LEFT JOIN task_run_candidate_outcomes o
              ON o.candidate_id = c.id
             AND o.outcome_sequence = (
                 SELECT MAX(latest.outcome_sequence)
                 FROM task_run_candidate_outcomes latest
                 WHERE latest.candidate_id = c.id
             )
            WHERE c.project_id = ? AND c.task_run_id = ?
            ORDER BY c.assistant_turn ASC, c.content_index ASC, c.id ASC
            LIMIT ? OFFSET ?
            """,
            (project_id, task_run_id, limit, offset),
        ).fetchall()
    return [_metadata(row) for row in rows]


def read_candidate_bytes(
    project_id: str,
    task_run_id: str,
    candidate_id: str,
) -> bytes:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT raw_bytes FROM task_run_candidates
            WHERE project_id = ? AND task_run_id = ? AND id = ?
            """,
            (project_id, task_run_id, candidate_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Candidate not found")
        raw = row["raw_bytes"]
    return raw if isinstance(raw, bytes) else bytes(raw)


__all__ = [name for name in globals() if not name.startswith("_")]
