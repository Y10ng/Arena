"""项目、目标、Intent 与 TaskRun 服务。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL

from .assessment_attempts import finalize_non_retryable_assessment_execution
from .assessment_policy import parse_scenario_snapshot, row_value
from .common import *

from .coverage_slots import reset_slot_for_abandoned_intent
from .ids import get_project_or_404
from .payload_values import dump_json, payload_text
from cairn.server.models import ReportCapabilityView

from .report_policy import report_policy_from_project_row
from .runtime_context import (
    raw_task_run_runtime_context,
    standard_task_run_error_type,
    task_run_runtime_context,
)
from .time_utils import utcnow

_BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def lease_clock() -> tuple[str, int]:
    # BOOTTIME 跨 Server 进程可比较，且包含休眠时间；boot ID 防止重启后误用旧刻度。
    return _BOOT_ID, time.clock_gettime_ns(time.CLOCK_BOOTTIME)


def check_project_active(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    # 与阶段决策/报告冻结共享项目行锁，避免 PG 并发接受过期输入。
    conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
    row = get_project_or_404(conn, project_id)
    if row["status"] != "active":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_hint_writable(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    # hints 是人工备注，completed 项目也允许追加；真正不可写状态预留给未来扩展。
    row = get_project_or_404(conn, project_id)
    if row["status"] not in ("active", "paused", "stopped", "completed"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_completed(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "completed":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def validate_facts_exist(
    conn: sqlite3.Connection, project_id: str, fact_ids: list[str]
) -> None:
    for fid in fact_ids:
        row = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?", (fid, project_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Fact {fid} not found")


def validate_task_runs_exist(
    conn: sqlite3.Connection, project_id: str, task_run_ids: list[str]
) -> None:
    for task_run_id in task_run_ids:
        validate_task_run_exists(conn, project_id, task_run_id)


def validate_task_run_exists(
    conn: sqlite3.Connection, project_id: str, task_run_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM task_runs WHERE id = ? AND project_id = ?",
        (task_run_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"Task run {task_run_id} not found")
    return row


def validate_execution_admission_generation(project, expected_generation: int | None) -> None:
    """在项目锁内校验派发快照；旧请求不能被自动提升为恢复后的新执行。"""
    generation = int(project["execution_generation"])
    if expected_generation is None:
        if generation > 0:
            raise HTTPException(409, "execution_generation_required")
    elif expected_generation != generation:
        raise HTTPException(409, "execution_generation_mismatch")


def validate_execution_generation(
    conn,
    project_id: str,
    task_run_id: str | None,
    *,
    allow_stopping_settlement: bool = False,
):
    """新业务写入须属于当前代次；人工停止仅为既有运行开放结果结算。"""
    project = get_project_or_404(conn, project_id)
    if task_run_id is None:
        if int(row_value(project, "execution_generation") or 0) > 0:
            raise HTTPException(409, "execution_task_run_required")
        return None
    run = validate_task_run_exists(conn, project_id, task_run_id)
    generation_matches = (
        int(row_value(run, "execution_generation") or 0)
        == int(row_value(project, "execution_generation") or 0)
    )
    if generation_matches and not row_value(run, "pause_requested"):
        return run
    stopping_settlement = (
        allow_stopping_settlement
        and row_value(project, "assessment_protocol") == GOAL_DRIVEN_PROTOCOL
        and row_value(project, "coverage_stop_reason") == "user_stopped"
        and project["status"] == "active"
        and row_value(run, "pause_requested")
        and int(row_value(run, "execution_generation") or 0) + 1
        == int(row_value(project, "execution_generation") or 0)
    )
    if not stopping_settlement:
        raise HTTPException(409, "execution_generation_mismatch")
    return run


def validate_execution_lease(conn, project_id: str, task_run_id: str | None, worker: str, *, intent_id: str | None = None):
    run = validate_execution_generation(conn, project_id, task_run_id)
    if run is None:
        return
    if run["worker_name"] != worker or (run["intent_id"] != intent_id if intent_id is not None else run["task_type"] != "reason"):
        raise HTTPException(409, "execution_lease_mismatch")
    clause = "intent_id = ?" if intent_id is not None else "task_type = 'reason'"
    params = (project_id, intent_id) if intent_id is not None else (project_id,)
    current = conn.execute(f"SELECT id FROM task_runs WHERE project_id = ? AND {clause} ORDER BY LENGTH(id) DESC, id DESC LIMIT 1", params).fetchone()
    if current is None or current["id"] != task_run_id:
        raise HTTPException(409, "execution_lease_mismatch")


def validate_artifacts_exist(
    conn: sqlite3.Connection, project_id: str, evidence: list[EvidenceRef]
) -> None:
    for ref in evidence:
        row = conn.execute(
            "SELECT 1 FROM artifacts WHERE id = ? AND project_id = ?",
            (ref.artifact_id, project_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Artifact {ref.artifact_id} not found")
        if ref.verifier_run_id is not None:
            validate_task_run_exists(conn, project_id, ref.verifier_run_id)


def bind_fact_evidence(
    conn: sqlite3.Connection,
    project_id: str,
    fact_id: str,
    evidence: list[EvidenceRef],
) -> None:
    if not evidence:
        return
    # 先验证所有引用，再批量写入，避免部分 evidence 成功造成链路不完整。
    validate_facts_exist(conn, project_id, [fact_id])
    validate_artifacts_exist(conn, project_id, evidence)
    now = utcnow()
    for ref in evidence:
        conn.execute(
            """
            INSERT INTO fact_evidence
                (project_id, fact_id, artifact_id, pointer, verifier_run_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                project_id,
                fact_id,
                ref.artifact_id,
                ref.pointer,
                ref.verifier_run_id,
                now,
            ),
        )


def validate_goal_not_in_sources(fact_ids: list[str]) -> None:
    if "goal" in fact_ids:
        raise HTTPException(400, "goal cannot be used in from")


def validate_intent_creator_worker(creator: str, worker: str | None) -> None:
    if worker is not None and worker != creator:
        raise HTTPException(400, "worker must be null or equal to creator")


def get_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Intent not found")
    return row


def get_claimable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    # 每次认领/心跳前过期失联 worker，让锁不会永久卡死。
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None or row["concluded_at"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is not None and row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    # depends_on 依赖检查：引用的 intent 必须已收敛（concluded_at 非空）才能 claim。
    depends_on = row["depends_on"] if "depends_on" in row.keys() else None
    if depends_on:
        dep_row = conn.execute(
            "SELECT concluded_at FROM intents WHERE id = ? AND project_id = ?",
            (depends_on, project_id),
        ).fetchone()
        if dep_row is not None and dep_row["concluded_at"] is None:
            raise HTTPException(409, "Intent has unmet dependencies")
    return row


def get_releasable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None or row["concluded_at"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is None:
        return row
    if row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_completion_intent_or_409(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    # completed 项目应只有一条指向 goal 的 completion intent；reopen 依赖这个不变量。
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND to_fact_id = 'goal'",
        (project_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "Completed project is missing its completion intent")
    if len(rows) != 1:
        raise HTTPException(409, "Completed project has multiple completion intents")
    return rows[0]


def intent_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str) -> Intent:
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY source_order, fact_id",
        (row["id"], project_id),
    ).fetchall()
    return Intent(
        id=row["id"],
        **{"from": [s["fact_id"] for s in sources]},
        to=row["to_fact_id"],
        description=row["description"],
        creator=row["creator"],
        task_run_id=row["task_run_id"],
        worker=row["worker"],
        worker_history=intent_worker_history(row["worker_history_json"]),
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
        conclusion=row["conclusion"] if "conclusion" in row.keys() else None,
        conclusion_reason=row_value(row, "conclusion_reason"),
        depth_review_fact_id=row_value(row, "depth_review_fact_id"),
        depends_on=row["depends_on"] if "depends_on" in row.keys() else None,
        slot_key=row["slot_key"] if "slot_key" in row.keys() else None,
        strategy_key=(
            (row["strategy_key"] or "")
            if "strategy_key" in row.keys()
            else ""
        ),
        assessment_role=(
            str(row["assessment_role"] or "discovery")
            if "assessment_role" in row.keys()
            else "discovery"
        ),
        priority=int(
            row_value(row, "priority")
            if row_value(row, "priority") is not None
            else 1
        ),
        target_observation_id=(
            row["target_observation_id"]
            if "target_observation_id" in row.keys()
            else None
        ),
    )


def task_run_to_model(row: sqlite3.Row) -> TaskRun:
    error_type = standard_task_run_error_type(row["error_type"])
    return TaskRun(
        id=row["id"],
        project_id=row["project_id"],
        intent_id=row["intent_id"],
        task_type=row["task_type"],
        worker_name=row["worker_name"],
        worker_type=row["worker_type"],
        status=row["status"],
        session_id=row["session_id"],
        prompt_group=row["prompt_group"],
        prompt_version=row["prompt_version"],
        execution_profile=row["execution_profile"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        exit_code=row["exit_code"],
        error_type=error_type,
        error_message=payload_text(row["error_message"]),
        assessment_attempt_id=(
            row["assessment_attempt_id"]
            if "assessment_attempt_id" in row.keys()
            else None
        ),
        execution_generation=int(row_value(row, "execution_generation") or 0),
        pause_requested=bool(row_value(row, "pause_requested")),
        runtime_context=task_run_runtime_context(row),
    )


def intent_worker_history(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        loaded = loads(value)
    except (TypeError, ValueError):
        # 历史坏数据不阻断项目读取，只丢弃无法解析的 worker history。
        return []
    if not isinstance(loaded, list):
        return []
    history: list[dict[str, Any]] = []
    for item in loaded:
        if isinstance(item, dict):
            history.append(item)
    return history


def _fail_running_task_for_expired_intent(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    *,
    ended_at: str,
) -> None:
    """租约过期时同步终结未知 TaskRun，避免 active attempt 永久悬挂。"""
    task_run = conn.execute(
        """
        SELECT * FROM task_runs
        WHERE project_id = ? AND intent_id = ? AND status = 'running'
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        (project_id, intent_id),
    ).fetchone()
    if task_run is None:
        return
    runtime_context = raw_task_run_runtime_context(task_run)
    runtime_context.update(
        {
            "status": "failed",
            "ended_at": ended_at,
            "error_type": "heartbeat_lost",
            "error_message": "Intent lease expired before TaskRun completion",
        }
    )
    updated = conn.execute(
        """
        UPDATE task_runs
        SET status = 'failed', ended_at = ?, error_type = 'heartbeat_lost',
            error_message = ?, runtime_context_json = ?
        WHERE id = ? AND project_id = ? AND status = 'running'
        """,
        (
            ended_at,
            "Intent lease expired before TaskRun completion",
            dump_json(runtime_context),
            task_run["id"],
            project_id,
        ),
    )
    if updated.rowcount == 0:
        return
    finalize_non_retryable_assessment_execution(
        conn,
        project_id,
        (
            task_run["assessment_attempt_id"]
            if "assessment_attempt_id" in task_run.keys()
            else None
        ),
        task_run_id=task_run["id"],
        status="failed",
        error_type="heartbeat_lost",
    )


def clear_open_intent_worker(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    reason: IntentWorkerHistoryReason,
    *,
    cleared_at: str | None = None,
) -> sqlite3.Row:
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None or row["concluded_at"] is not None or row["worker"] is None:
        return row
    cleared = cleared_at or utcnow()
    if reason == "heartbeat_timeout":
        _fail_running_task_for_expired_intent(
            conn,
            project_id,
            intent_id,
            ended_at=cleared,
        )
    history = intent_worker_history(row["worker_history_json"])
    # 清 worker 时保留历史原因，监控/调试可区分主动释放、项目停止和心跳超时。
    history.append(
        {
            "worker": row["worker"],
            "reason": reason,
            "cleared_at": cleared,
            "last_heartbeat_at": row["last_heartbeat_at"],
        }
    )
    conn.execute(
        """
        UPDATE intents
        SET worker = NULL,
            heartbeat_boot_id = NULL,
            heartbeat_boottime_ns = NULL,
            worker_history_json = ?
        WHERE id = ? AND project_id = ?
        """,
        (dumps(history, ensure_ascii=False, sort_keys=True, separators=(",", ":")), intent_id, project_id),
    )
    updated = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()
    assert updated is not None
    return updated


def clear_open_intent_workers_for_project(
    conn: sqlite3.Connection,
    project_id: str,
    reason: IntentWorkerHistoryReason,
    *,
    cleared_at: str | None = None,
) -> None:
    rows = conn.execute(
        """
        SELECT * FROM intents
        WHERE project_id = ?
          AND to_fact_id IS NULL
          AND concluded_at IS NULL
          AND worker IS NOT NULL
        ORDER BY created_at, id
        """,
        (project_id,),
    ).fetchall()
    cleared = cleared_at or utcnow()
    for row in rows:
        clear_open_intent_worker(conn, project_id, row["id"], reason, cleared_at=cleared)


def close_open_intents_for_project_completion(
    conn: sqlite3.Connection,
    project_id: str,
    completion_intent_id: str,
    *,
    closed_at: str | None = None,
) -> None:
    rows = conn.execute(
        """
        SELECT * FROM intents
        WHERE project_id = ?
          AND id != ?
          AND to_fact_id IS NULL
          AND concluded_at IS NULL
        ORDER BY created_at, id
        """,
        (project_id, completion_intent_id),
    ).fetchall()
    closed = closed_at or utcnow()
    for row in rows:
        if row["worker"] is not None:
            clear_open_intent_worker(conn, project_id, row["id"], "project_completed", cleared_at=closed)
        conn.execute(
            """
            UPDATE intents
            SET worker = NULL,
                last_heartbeat_at = NULL,
                concluded_at = ?,
                conclusion = 'abandoned'
            WHERE id = ? AND project_id = ?
            """,
            (closed, row["id"], project_id),
        )


def build_intents(conn: sqlite3.Connection, project_id: str) -> list[Intent]:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [intent_to_model(conn, r, project_id) for r in rows]


def get_intent_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT intent_timeout FROM settings WHERE id = 1").fetchone()
    return row["intent_timeout"]


def get_reason_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT reason_timeout FROM settings WHERE id = 1").fetchone()
    return row["reason_timeout"]


def project_reason_from_row(row: sqlite3.Row) -> ProjectReason | None:
    if row["reason_worker"] is None:
        return None
    return ProjectReason(
        worker=row["reason_worker"],
        trigger=row["reason_trigger"],
        started_at=row["reason_started_at"],
        last_heartbeat_at=row["reason_last_heartbeat_at"],
    )


def project_meta_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> ProjectMeta:
    tenant_id = str(row_value(row, "tenant_id") or "")
    tenant_name = ""
    tenant_owner_login = None
    group_name = None
    if tenant_id:
        attribution = conn.execute(
            """
            SELECT t.name AS tenant_name,
                   (SELECT u.login
                    FROM tenant_memberships tm
                    JOIN users u ON u.id = tm.user_id
                    WHERE tm.tenant_id = t.id AND tm.role = 'owner'
                    ORDER BY tm.created_at, u.id
                    LIMIT 1) AS tenant_owner_login,
                   (SELECT pg.name
                    FROM project_groups pg
                    WHERE pg.id = ? AND pg.tenant_id = t.id
                    LIMIT 1) AS group_name
            FROM tenants t
            WHERE t.id = ?
            """,
            (row_value(row, "group_id"), tenant_id),
        ).fetchone()
        if attribution is None:
            raise HTTPException(500, "Project tenant not found")
        tenant_name = str(attribution["tenant_name"])
        tenant_owner_login = attribution["tenant_owner_login"]
        group_name = attribution["group_name"]
    from .assessment_convergence import assessment_execution_view, assessment_progress

    report_policy = report_policy_from_project_row(row)
    from .project_records import _authorization_meta
    from .project_authorization import project_authorization_block_reason

    authorization = _authorization_meta(conn, row)
    return ProjectMeta(
        assessment_execution=assessment_execution_view(conn, row),
        assessment_progress=assessment_progress(row) or None,
        verification_attempts_started=int(row_value(row, "verification_attempts_started") or 0),
        id=row["id"],
        title=row["title"],
        status=row["status"],
        execution_generation=int(row_value(row, "execution_generation") or 0),
        authorization_epoch=int(row_value(row, "authorization_epoch") or 0),
        authorization_scope_digest=authorization.scope_digest,
        authorization_window_seconds=authorization.window_seconds,
        authorization_block_reason=project_authorization_block_reason(conn, row),
        created_at=row["created_at"],
        scenario_key=row["scenario_key"],
        scenario_snapshot=parse_scenario_snapshot(row_value(row, "scenario_snapshot_json")),
        assessment_protocol=str(row_value(row, "assessment_protocol") or ""),
        vulnerability_fingerprint_version=row_value(
            row, "vulnerability_fingerprint_version"
        ),
        report_capability=ReportCapabilityView(
            supported=report_policy.supported,
            policy_version=report_policy.version,
            template_profile=report_policy.template_profile or None,
            reason=report_policy.reason,
        ),
        explore_deadline=row_value(row, "explore_deadline"),
        authorization_expires_at=row_value(row, "authorization_expires_at"),
        explore_attempts_started=int(row_value(row, "explore_attempts_started") or 0),
        coverage_checkpoint_cell_count=int(
            row_value(row, "coverage_checkpoint_cell_count") or 0
        ),
        coverage_checkpoint_required_count=int(
            row_value(row, "coverage_checkpoint_required_count") or 0
        ),
        coverage_checkpoint_optional_count=int(
            row_value(row, "coverage_checkpoint_optional_count") or 0
        ),
        coverage_checkpoint_required_terminal_count=int(
            row_value(row, "coverage_checkpoint_required_terminal_count") or 0
        ),
        coverage_stop_reason=row_value(row, "coverage_stop_reason"),
        preferred_workers=_parse_preferred_workers(row["preferred_workers_json"]),
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        tenant_owner_login=tenant_owner_login,
        group_id=row_value(row, "group_id"),
        group_name=group_name,
        reason=project_reason_from_row(row),
    )


def _parse_preferred_workers(raw: str | None) -> list[str]:
    # 容错解析历史/异常数据：非法 JSON 或非数组一律视为“不限制”。
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def abandon_intent_record(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    worker: str,
) -> Intent:
    """主动放弃一个 open intent：仅对未认领（worker=null）的 open intent 生效。

    运行中（worker 非空）或已收敛的 intent 不可放弃，返回 409；让 reason 在 complete 前
    剪掉不再追的分支，避免 complete 留下"没有下文"的死节点。
    """
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None or row["concluded_at"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is not None:
        # 运行中的 explore 不能强行放弃，须等其 conclude/release 后再处理。
        raise HTTPException(409, "Intent is currently being explored; wait for it to finish")
    if row_value(row, "depth_review_fact_id") or row_value(row, "assessment_role") == "verifier":
        raise HTTPException(409, "assessment_obligation_requires_explicit_disposition")
    now = utcnow()
    history = intent_worker_history(row["worker_history_json"])
    history.append(
        {
            "worker": row["worker"],
            "reason": "abandoned",
            "cleared_at": now,
            "last_heartbeat_at": row["last_heartbeat_at"],
        }
    )
    conn.execute(
        """
        UPDATE intents
        SET worker = NULL,
            last_heartbeat_at = NULL,
            concluded_at = ?,
            conclusion = 'abandoned',
            worker_history_json = ?
        WHERE id = ? AND project_id = ?
        """,
        (
            now,
            dumps(history, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            intent_id,
            project_id,
        ),
    )
    reset_slot_for_abandoned_intent(
        conn,
        project_id,
        intent_id,
        updated_at=now,
    )
    updated = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()
    assert updated is not None
    return intent_to_model(conn, updated, project_id)


def clear_project_reason(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL,
            reason_heartbeat_boot_id = NULL,
            reason_heartbeat_boottime_ns = NULL
        WHERE id = ?
        """,
        (project_id,),
    )


def expire_workers(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    boot_id, now_ns = lease_clock()
    active = """to_fact_id IS NULL AND worker IS NOT NULL AND last_heartbeat_at IS NOT NULL
        AND EXISTS (SELECT 1 FROM projects p WHERE p.id = intents.project_id
                    AND p.status != 'paused' AND p.assessment_protocol = ?)"""
    scope = " AND project_id = ?" if project_id is not None else ""
    params = (GOAL_DRIVEN_PROTOCOL, *((project_id,) if project_id is not None else ()))
    # 旧租约或不同 boot 的刻度不可比较；从首次观察起重新计时，不按 UTC 瞬间判死。
    conn.execute(
        f"""UPDATE intents SET heartbeat_boot_id = ?, heartbeat_boottime_ns = ?
            WHERE {active}{scope} AND (heartbeat_boot_id IS NULL OR heartbeat_boot_id != ?
                                      OR heartbeat_boottime_ns IS NULL)""",
        (boot_id, now_ns, *params, boot_id),
    )
    rows = conn.execute(
        f"""SELECT * FROM intents WHERE {active}{scope}
            AND heartbeat_boot_id = ? AND heartbeat_boottime_ns < ?
            ORDER BY created_at, id""",
        (*params, boot_id, now_ns - get_intent_timeout(conn) * 1_000_000_000),
    ).fetchall()
    now = utcnow()
    for row in rows:
        clear_open_intent_worker(conn, row["project_id"], row["id"], "heartbeat_timeout", cleared_at=now)


def expire_reason_leases(conn: sqlite3.Connection, project_id: str | None = None) -> bool:
    boot_id, now_ns = lease_clock()
    active = "reason_worker IS NOT NULL AND status != 'paused' AND assessment_protocol = ? AND reason_last_heartbeat_at IS NOT NULL"
    scope = " AND id = ?" if project_id is not None else ""
    params = (GOAL_DRIVEN_PROTOCOL, *((project_id,) if project_id is not None else ()))
    conn.execute(
        f"""UPDATE projects SET reason_heartbeat_boot_id = ?, reason_heartbeat_boottime_ns = ?
            WHERE {active}{scope} AND (reason_heartbeat_boot_id IS NULL OR reason_heartbeat_boot_id != ?
                                      OR reason_heartbeat_boottime_ns IS NULL)""",
        (boot_id, now_ns, *params, boot_id),
    )
    cutoff_ns = now_ns - get_reason_timeout(conn) * 1_000_000_000
    expired = conn.execute(
        f"""SELECT id, reason_worker, execution_generation FROM projects
            WHERE {active}{scope} AND reason_heartbeat_boot_id = ?
                AND reason_heartbeat_boottime_ns < ?""",
        (*params, boot_id, cutoff_ns),
    ).fetchall()
    revoked = False
    for row in expired:
        # 仅撤销当前代次、同 worker 的运行权；TaskRun 终态不代表容器进程已经停止。
        cleared = conn.execute(
            f"""UPDATE projects SET reason_worker = NULL, reason_trigger = NULL,
                reason_started_at = NULL, reason_last_heartbeat_at = NULL,
                reason_heartbeat_boot_id = NULL, reason_heartbeat_boottime_ns = NULL
                WHERE id = ? AND {active} AND reason_worker = ?
                    AND execution_generation = ? AND reason_heartbeat_boot_id = ?
                    AND reason_heartbeat_boottime_ns < ?""",
            (row["id"], GOAL_DRIVEN_PROTOCOL, row["reason_worker"],
             row["execution_generation"], boot_id, cutoff_ns),
        )
        if cleared.rowcount == 0:
            continue
        revoked = True
        ended_at = utcnow()
        runs = conn.execute(
            """SELECT * FROM task_runs WHERE project_id = ? AND execution_generation = ?
                AND worker_name = ? AND task_type = 'reason' AND status = 'running'""",
            (row["id"], row["execution_generation"], row["reason_worker"]),
        ).fetchall()
        for run in runs:
            context = raw_task_run_runtime_context(run)
            context.update({
                "status": "failed", "ended_at": ended_at,
                "error_type": "heartbeat_lost",
                "error_message": "Reason lease expired before TaskRun completion",
            })
            updated = conn.execute(
                """UPDATE task_runs SET status = 'failed', ended_at = ?,
                    error_type = 'heartbeat_lost', error_message = ?, runtime_context_json = ?
                    WHERE id = ? AND project_id = ? AND execution_generation = ?
                        AND worker_name = ? AND task_type = 'reason' AND status = 'running'""",
                (ended_at, "Reason lease expired before TaskRun completion",
                 dump_json(context), run["id"], row["id"], row["execution_generation"],
                 row["reason_worker"]),
            )
            if updated.rowcount:
                finalize_non_retryable_assessment_execution(
                    conn, row["id"], row_value(run, "assessment_attempt_id"),
                    task_run_id=run["id"], status="failed", error_type="heartbeat_lost",
                )
    return revoked
