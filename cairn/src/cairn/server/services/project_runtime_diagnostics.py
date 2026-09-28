"""只读取项目运行事实；租约回收和生命周期推进不能发生在诊断请求中。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256

from cairn.server.schemas.project_runtime import (
    ProjectRuntime,
    ProjectRuntimeResponse,
    ProjectRuntimeV2,
    RuntimeAcceptanceAxis,
    RuntimeActivity,
    RuntimeAttempt,
    RuntimeAxis,
    RuntimeAxes,
    RuntimeCheckpointStaging,
    RuntimeDispatcher,
    RuntimeError,
    RuntimeProcessAxis,
    RuntimeRecovery,
    RuntimeResourceAxis,
    RuntimeScheduling,
)
from .infra import DISPATCHER_HEARTBEAT_STALE_SECONDS
from .project_authorization import project_authorization_block_reason
from .time_utils import parse_utc_timestamp


AUTHORIZATION_DECISIONS = {
    "project_authorization_scope_upgrade_required": (
        "blocked", "项目使用旧版授权范围格式，新任务尚未开始；请在项目暂停后基于当前范围显式确认一次授权，不是等待 Worker",
    ),
    "project_authorization_scope_changed": (
        "blocked", "项目授权范围已变化，新任务尚未开始；请在暂停状态下核对当前范围并重新确认授权",
    ),
    "project_authorization_expired": (
        "blocked", "项目授权已过期，新任务尚未开始；请在项目暂停后重新确认授权",
    ),
    "project_reauthorization_required": (
        "blocked", "当前项目需要显式确认授权，新任务尚未开始；请在项目暂停后重新确认授权",
    ),
}

# 只允许固定错误码进入租户视图，不转发 dispatcher 的原始异常或运行配置。
DECISIONS = {
    **AUTHORIZATION_DECISIONS,
    "no_candidates": ("blocked", "没有可执行的 Worker，请检查项目绑定与模型配置"),
    "engine_unsupported": ("blocked", "绑定的 Worker 引擎不受支持，请重新绑定 pi Worker"),
    "worker_disabled": ("blocked", "绑定的 Worker 已禁用，请检查 Worker 配置"),
    "worker_task_type": ("blocked", "Worker 不支持当前任务类型，请调整项目绑定"),
    "model_disabled": ("blocked", "Worker 对应的模型已禁用"),
    "model_protocol_unsupported": ("blocked", "Worker 对应的模型协议不受支持"),
    "model_incomplete": ("blocked", "模型配置不完整，请检查模型名称和接口"),
    "credential_missing": ("blocked", "模型凭据尚未配置，请在模型设置中补全"),
    "binding_stale": ("blocked", "Worker 配置已变更，请重新确认项目的 Worker 绑定"),
    "binding_failed": ("blocked", "无法获取执行绑定，请检查 Worker 和模型配置"),
    "binding_auth_circuit": ("waiting", "模型认证失败后暂缓执行，请检查凭据和重试窗口"),
    "worker_capacity": ("waiting", "Worker 执行容量已满，等待当前任务释放容量"),
    "global_capacity": ("waiting", "调度器执行容量已满，等待运行槽位"),
    "project_capacity": ("waiting", "本项目执行容量已满，等待当前任务完成"),
    "project_limit": ("waiting", "并行项目数量已达上限，等待项目运行槽位"),
    "worker_cooldown": ("waiting", "Worker 处于退避窗口，等待下一次重试"),
    "worker_rejected": ("blocked", "Worker 暂时拒绝当前任务，请查看相关运行错误"),
    "cleanup_pending": ("waiting", "正在清理上一次执行的资源，暂不启动新任务"),
    "execution_unsupported": ("blocked", "项目的执行契约不受支持，不能隐式转换历史评估"),
    "container_runtime_mismatch": ("blocked", "保留的项目容器运行环境与当前配置不一致；工作区未删除，请恢复原容器配置"),
    "container_inventory_unresolved": ("blocked", "升级前的项目容器来源未知，不能确认旧执行已停；请管理员核查原运行环境"),
    "project_inactive": ("unknown", "调度器观测到项目未运行，等待新的状态同步"),
    "claim_conflict": ("waiting", "任务领取存在竞争，等待调度器重新确认"),
    "claim_denied": ("blocked", "任务领取被拒绝，请检查项目状态与执行授权"),
    "claim_failed": ("blocked", "任务领取失败，等待调度器重试或恢复连接"),
    "execution_retry_blocked": ("blocked", "本代执行发生不可自动重试的错误，请修复后暂停并恢复"),
    "execution_retry_exhausted": ("blocked", "本代执行已用完自动重试额度，请检查失败记录"),
    "execution_retry_cooldown": ("waiting", "执行失败后暂缓重试，等待持久化退避窗口"),
    "execution_delivery_unavailable": ("blocked", "原会话或交付绑定不可用，不能重新探索替代交付"),
    "execution_continuation_unavailable": ("blocked", "原项目容器或保留的 pi runner 不支持原对话续作；未领取新任务，不能改用空会话"),
    "admission_denied": ("blocked", "当前评估义务不满足执行条件"),
    "admission_failed": ("blocked", "执行资格校验暂时失败，请查看调度状态"),
    "task_create_failed": ("failed", "无法创建执行记录，请检查服务端状态"),
    "submit_failed": ("failed", "任务未能提交到执行器，请查看运行错误"),
    "dispatched": ("starting", "任务已分配，等待执行进程启动确认"),
    "already_claimed": ("waiting", "任务已有领取记录，等待执行状态确认"),
    "no_work": ("waiting", "调度器本轮没有发现可派发工作，等待下一轮评估"),
}
CANDIDATE_RECOVERY_MESSAGES = {
    "docker_daemon_identity_missing": "旧执行缺少 Docker daemon 身份，不能把容器缺失误判为执行已结束，请管理员核查原运行环境",
    "docker_daemon_unavailable": "Docker daemon 当前不可达，无法确认原执行是否仍在运行，请恢复 Docker 后等待自动重试",
    "docker_daemon_changed": "当前 Docker daemon 与原执行不一致，无法确认旧执行是否仍在其他环境运行，请管理员核查",
    "container_identity_unconfirmed": "无法确认原容器身份或归属，已隔离本项目并等待管理员核查",
    "container_ownership_mismatch": "原容器归属与执行记录不一致，已隔离本项目并等待管理员核查",
    "container_identity_mismatch": "原容器身份与执行记录不一致，已隔离本项目并等待管理员核查",
    "exec_missing_process_unconfirmed": "Docker 已找不到原 exec，但容器仍在且进程组停止尚未证实，暂不释放执行",
    "exec_inspection_unavailable": "Docker 暂时无法检查原 exec，已保留执行现场并等待自动重试",
    "exec_identity_mismatch": "原 exec 身份与容器不一致，已隔离本项目并等待管理员核查",
    "exec_terminal_state_invalid": "原 exec 状态不完整，尚不能确认执行已停止",
    "termination_proof_unavailable": "原 exec 已退出，但进程组停止凭据尚未成立，暂不释放执行",
    "execution_control_invalid": "原执行控制凭据无效，已隔离本项目并等待管理员核查",
    "task_run_authority_unavailable": "无法从 Server 核对原 Run 终态，不能对共享容器中的执行发送信号",
    "task_run_owner_mismatch": "原 Run 的 Dispatcher 归属不符，不能终止该任务执行",
    "task_termination_control_unavailable": "任务级终止控制失败，尚无停止证明；请管理员核查原执行，必要时暂停项目",
    "task_termination_unconfirmed": "已尝试一次任务级终止，但原执行仍未证实停止；请管理员核查，必要时暂停项目",
    "task_termination_fence_persist_failed": "无法持久记录任务级终止尝试，未发送终止信号",
}
_CANDIDATE_AUTOMATIC_RECHECK_REASONS = frozenset(
    {
        "docker_daemon_unavailable",
        "exec_inspection_unavailable",
    }
)
_CANDIDATE_PHYSICAL_WAIT_REASONS = frozenset(
    {
        "exec_missing_process_unconfirmed",
        "exec_terminal_state_invalid",
        "termination_proof_unavailable",
    }
)
ERRORS = {
    "container_failed": "容器或执行环境失败，请查看对应运行记录",
    "container_runtime_mismatch": "当前容器运行环境与项目原配置不一致；工作区已保留，请恢复原容器配置后再继续",
    "dispatcher_internal_error": "调度器内部执行失败，请联系管理员检查关联运行",
    "startup_healthcheck_failed": "调度器启动自检失败，请联系管理员检查执行环境",
    "config_invalid": "调度配置不可用，请联系管理员修正配置",
    "server_unreachable": "调度器无法连接服务端，请检查服务连接",
    "provider_auth": "模型认证失败，请检查模型凭据",
    "provider_rate_limit": "模型请求受到限流，请等待退避窗口",
    "provider_error": "模型请求失败，请查看对应运行记录",
    "request_contract": "执行请求不符合协议，请修复配置或请求构造后再恢复",
    "business_acceptance": "执行结果未通过业务验收，本代不再自动重试",
    "contract_error": "执行契约校验失败，本代不再自动重试",
    "protocol_error": "执行结果协议校验失败，本代不再自动重试",
    "result_schema": "执行结果不符合约定，尚未被接受为评估成果",
    "submission_missing": "模型未提交结构化结果，尚未被接受为评估成果",
    "submission_invalid": "模型提交未通过严格校验，尚未被接受为评估成果",
    "delivery_unavailable": "原会话、证据或交付绑定不可用，不能重新探索替代交付",
    "artifact_error": "提交所需证据不可用或已发生变化，请查看关联运行",
    "policy_denied": "执行或写回权限被拒绝，本代不再自动重试",
    "session_error": "原执行会话不可用，不能隐式创建新会话",
    "result_protocol": "执行结果协议校验失败",
    "deadline_exceeded": "执行超过允许的截止时间",
    "execution_failed": "执行失败，请查看对应运行记录",
}


def _object(value) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _fresh(timestamp, now: str) -> bool:
    observed = parse_utc_timestamp(timestamp)
    current = parse_utc_timestamp(now)
    if observed is None or current is None:
        return False
    age = (current - observed).total_seconds()
    return 0 <= age <= DISPATCHER_HEARTBEAT_STALE_SECONDS


def _dispatcher(conn, project, now: str):
    from .project_records import project_execution_owners

    owners, unknown = project_execution_owners(conn, project)
    if unknown:
        return RuntimeDispatcher(status="unknown", freshness="unknown"), {}, None
    if owners:
        rows = [conn.execute("SELECT * FROM dispatcher_status WHERE id = ?", (owner,)).fetchone() for owner in sorted(owners)]
        if any(row is None for row in rows):
            return RuntimeDispatcher(status="unknown", freshness="unknown"), {}, None
        # 多个存量 owner 必须全部新鲜；一个无关新实例不能覆盖失联执行。
        row = min(rows, key=lambda item: (int(_fresh(item["updated_at"], now)), item["updated_at"]))
    else:
        from .project_records import project_ready_dispatcher

        # 已无物理 owner 时与恢复动作选择同一就绪实例，不能被更新的 starting 心跳遮蔽。
        row, _reason = project_ready_dispatcher(conn, now=now)
        if row is None:
            row = conn.execute("SELECT * FROM dispatcher_status ORDER BY updated_at DESC, id LIMIT 1").fetchone()
    if row is None:
        return RuntimeDispatcher(status="unknown", freshness="unknown"), {}, None
    status = row["status"] if row["status"] in {"starting", "running", "idle", "error", "stopping"} else "unknown"
    return (
        RuntimeDispatcher(
            status=status,
            freshness="fresh" if _fresh(row["updated_at"], now) else "stale",
            updated_at=row["updated_at"],
            last_loop_finished_at=row["last_loop_finished_at"],
        ),
        _object(row["runtime_json"]),
        str(row["id"]),
    )


def _decision(runtime: dict, project_id: str, generation: int, now: str):
    capabilities = runtime.get("runtime_capabilities")
    if not isinstance(capabilities, list) or "project_runtime_v1" not in capabilities:
        return None
    items = runtime.get("project_decisions")
    if not isinstance(items, list):
        return None
    valid = [item for item in items[:256] if isinstance(item, dict)
             and item.get("project_id") == project_id
             and type(item.get("execution_generation")) is int
             and item["execution_generation"] == generation
             and isinstance(item.get("code"), str)
             and item["code"] in DECISIONS
             and _fresh(item.get("observed_at"), now)]
    if not valid:
        return None
    item = max(valid, key=lambda item: parse_utc_timestamp(item["observed_at"]))
    retry_at = item.get("retry_at")
    if parse_utc_timestamp(retry_at) is None:
        retry_at = None
    return RuntimeScheduling(code=item["code"], observed_at=item["observed_at"], retry_at=retry_at)


def _candidate_recovery_route(
    reason: str,
    *,
    diagnostic_exhausted: bool = False,
) -> str:
    if diagnostic_exhausted:
        return "manual"
    if reason in _CANDIDATE_AUTOMATIC_RECHECK_REASONS:
        return "automatic_retry"
    if reason in _CANDIDATE_PHYSICAL_WAIT_REASONS:
        return "wait"
    return "manual"


def _candidate_recovery_message(item: dict) -> str:
    if item.get("diagnostic_exhausted") is True:
        return "持续无法确认旧执行状态，已降低自动检查频率；请管理员核查 Docker 与原运行环境"
    return CANDIDATE_RECOVERY_MESSAGES.get(
        item["reason"],
        "旧执行当前无法安全判定，已保留成果并隔离本项目，请管理员核查运行环境",
    )


def _candidate_recovery(
    conn,
    runtime: dict,
    project,
    dispatcher_id: str | None,
    now: str,
) -> dict | None:
    items = runtime.get("candidate_recovery")
    if not isinstance(items, list) or dispatcher_id is None:
        return None
    project_id = str(project["id"])
    generation = int(project["execution_generation"])
    runs = {
        str(row["id"]): row
        for row in conn.execute(
            "SELECT id, runtime_context_json FROM task_runs "
            "WHERE project_id = ? AND execution_generation <= ?",
            (project_id, generation),
        ).fetchall()
    }
    valid: list[dict] = []
    for item in items[:256]:
        if (
            not isinstance(item, dict)
            or item.get("project_id") != project_id
            or not isinstance(item.get("task_run_id"), str)
            or not isinstance(item.get("status"), str)
            or item["status"] not in {"running", "stopped", "gone", "unknown"}
            or not isinstance(item.get("reason"), str)
            or not _fresh(item.get("observed_at"), now)
        ):
            continue
        run = runs.get(item["task_run_id"])
        if run is None:
            continue
        context = _object(run["runtime_context_json"])
        if context.get("dispatcher_id") != dispatcher_id:
            continue
        lifecycle = context.get("execution_lifecycle")
        if isinstance(lifecycle, dict) and lifecycle.get("termination_confirmed") is True and lifecycle.get("cleanup_confirmed") is True:
            continue
        valid.append(item)
    if not valid:
        return None
    priority = {"unknown": 3, "running": 2, "stopped": 1, "gone": 0}
    route_priority = {"manual": 2, "wait": 1, "automatic_retry": 0}
    # 任一 unknown 覆盖可判定观测；其中人工核查不能被较新的瞬时重试状态遮蔽。
    return max(
        valid,
        key=lambda item: (
            priority[item["status"]],
            (
                route_priority[
                    _candidate_recovery_route(
                        item["reason"],
                        diagnostic_exhausted=(
                            item.get("diagnostic_exhausted") is True
                        ),
                    )
                ]
                if item["status"] == "unknown"
                else 0
            ),
            parse_utc_timestamp(item["observed_at"]),
        ),
    )


def _retry_decision(conn, project, now: str):
    from .assessment_attempts import execution_retry_eligibility

    scopes = conn.execute(
        "SELECT DISTINCT task_type, intent_id FROM task_runs WHERE project_id = ? AND execution_generation = ? AND task_type IN ('reason', 'bootstrap', 'report') AND status IN ('failed', 'timed_out')",
        (project["id"], project["execution_generation"]),
    ).fetchall()
    for scope in scopes:
        token = None
        if scope["task_type"] == "report":
            report = conn.execute("SELECT generation_token, status FROM assessment_report_finalizations WHERE project_id = ?", (project["id"],)).fetchone()
            if report is None or report["status"] == "ready":
                continue
            token = report["generation_token"]
        eligibility = execution_retry_eligibility(conn, project, scope["task_type"], intent_id=scope["intent_id"], report_generation_token=token, now=now)
        if not eligibility["allowed"]:
            return RuntimeScheduling(code=eligibility["reason"], observed_at=now, retry_at=eligibility["retry_at"])
    return None


def _runtime_attempt(row) -> RuntimeAttempt:
    lifecycle = _object(row["runtime_context_json"]).get("execution_lifecycle", {})
    if not isinstance(lifecycle, dict):
        lifecycle = {}
    confirmed_generation = lifecycle.get("generation") == row["execution_generation"]
    stage = (
        "running"
        if confirmed_generation and lifecycle.get("process_started") is True
        else "assigned"
    )
    if confirmed_generation and lifecycle.get("termination_confirmed") is True:
        stage = "stopped"
    if "pause_requested" in row.keys() and row["pause_requested"]:
        stage = "stopping"
    return RuntimeAttempt(
        id=row["id"], task_type=row["task_type"], status=row["status"],
        worker_name=row["worker_name"], execution_stage=stage, started_at=row["started_at"],
    )


def _report_retry_decision(conn, project, now: str):
    from .assessment_attempts import execution_retry_eligibility

    finalization = conn.execute(
        "SELECT generation_token, status FROM assessment_report_finalizations WHERE project_id = ?",
        (project["id"],),
    ).fetchone()
    if finalization is None or finalization["status"] == "ready":
        return None
    failed = conn.execute(
        """
        SELECT runtime_context_json FROM task_runs
        WHERE project_id = ? AND execution_generation = ?
          AND task_type = 'report' AND status IN ('failed', 'timed_out')
        """,
        (project["id"], project["execution_generation"]),
    ).fetchall()
    if not any(
        isinstance((claim := _object(row["runtime_context_json"]).get("report_claim")), dict)
        and claim.get("generation_token") == finalization["generation_token"]
        for row in failed
    ):
        return None
    eligibility = execution_retry_eligibility(
        conn,
        project,
        "report",
        report_generation_token=finalization["generation_token"],
        now=now,
    )
    if eligibility["allowed"]:
        return None
    return RuntimeScheduling(
        code=eligibility["reason"],
        observed_at=now,
        retry_at=eligibility["retry_at"],
    )


def _attempts(conn, project_id: str) -> list[RuntimeAttempt]:
    rows = conn.execute(
        "SELECT * FROM task_runs WHERE project_id = ? AND status IN ('queued', 'running') ORDER BY id",
        (project_id,),
    ).fetchall()
    return [_runtime_attempt(row) for row in rows]


def _current_report_attempts(conn, project) -> list[RuntimeAttempt]:
    finalization = conn.execute(
        "SELECT generation_token FROM assessment_report_finalizations WHERE project_id = ?",
        (project["id"],),
    ).fetchone()
    if finalization is None:
        return []
    attempts = []
    rows = conn.execute(
        """
        SELECT * FROM task_runs
        WHERE project_id = ? AND task_type = 'report'
          AND execution_generation = ?
        ORDER BY CASE WHEN status IN ('queued', 'running') THEN 0 ELSE 1 END,
                 COALESCE(ended_at, started_at) DESC, id DESC
        """,
        (project["id"], project["execution_generation"]),
    ).fetchall()
    for row in rows:
        context = _object(row["runtime_context_json"])
        claim = context.get("report_claim")
        lifecycle = context.get("execution_lifecycle")
        if (
            not isinstance(claim, dict)
            or claim.get("generation_token") != finalization["generation_token"]
            or not isinstance(lifecycle, dict)
            or lifecycle.get("generation") != row["execution_generation"]
        ):
            continue
        physically_resolved = (
            row["status"] not in {"queued", "running"}
            and lifecycle.get("termination_confirmed") is True
            and lifecycle.get("cleanup_confirmed") is True
        )
        if not physically_resolved:
            attempts.append(_runtime_attempt(row))
    return attempts


def _last_error(conn, project_id: str):
    row = conn.execute(
        "SELECT id, error_type, runtime_context_json FROM task_runs WHERE project_id = ? AND status IN ('failed', 'timed_out') ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    code = row["error_type"] if row["error_type"] in ERRORS else "execution_failed"
    from cairn.dispatcher.worker_protocol.submission import sanitize_submission_diagnostics
    from .runtime_context import sanitize_task_run_runtime_context

    failure = sanitize_task_run_runtime_context(_object(row["runtime_context_json"])).get("worker_failure")
    submission = None
    if isinstance(failure, dict) and failure.get("origin") == "pi_submission":
        submission = {"code": failure["code"], **sanitize_submission_diagnostics(failure, failure.get("phase", ""))}
    return RuntimeError(code=code, message=ERRORS[code], task_run_id=row["id"], submission=submission)


def _last_activity(conn, project_id: str):
    rows = conn.execute(
        """
        SELECT e.task_run_id, e.ts, e.kind, e.payload_json,
               r.execution_generation, r.runtime_context_json
        FROM agent_events e
        JOIN task_runs r ON r.project_id = e.project_id AND r.id = e.task_run_id
        WHERE e.project_id = ? AND e.kind IN ('command_finished', 'hook_operation', 'runtime_context_updated')
        ORDER BY e.ts DESC, e.id DESC LIMIT 32
        """,
        (project_id,),
    ).fetchall()
    for row in rows:
        kind = row["kind"]
        if kind == "runtime_context_updated":
            context = _object(row["payload_json"]).get("runtime_context", {})
            lifecycle = context.get("execution_lifecycle", {}) if isinstance(context, dict) else {}
            confirmed = _object(row["runtime_context_json"]).get("execution_lifecycle", {})
            if not isinstance(lifecycle, dict) or not isinstance(confirmed, dict):
                continue
            if lifecycle.get("generation") != row["execution_generation"] or confirmed.get("generation") != row["execution_generation"]:
                continue
            # 历史通用审计事件不具备独立的进程证明效力，必须与受信 TaskRun 状态相符。
            if lifecycle.get("termination_confirmed") is True and confirmed.get("termination_confirmed") is True:
                kind = "process_ended"
            elif lifecycle.get("process_started") is True and confirmed.get("process_started") is True:
                kind = "process_started"
            else:
                continue
        return RuntimeActivity(kind=kind, recorded_at=row["ts"], task_run_id=row["task_run_id"])
    return None


_HEX_DIGITS = frozenset("0123456789abcdef")


def _sha256_digest(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX_DIGITS for character in value)
    )


def _strict_object(value) -> dict | None:
    if not isinstance(value, str):
        return None

    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    try:
        parsed = json.loads(value, object_pairs_hook=unique_object)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _canonical_json(value: dict) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _verified_conclusion_receipt(conn, project_id: str, run, context: dict) -> bool:
    digest = context.get("accepted_result_digest")
    fact_id = context.get("accepted_fact_id")
    intent_id = run["intent_id"]
    if (
        run["task_type"] not in {"bootstrap", "explore"}
        or not _sha256_digest(digest)
        or not isinstance(fact_id, str)
        or not fact_id
        or not isinstance(intent_id, str)
        or not intent_id
    ):
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM facts f
        JOIN intents i
          ON i.project_id = f.project_id AND i.to_fact_id = f.id
        WHERE f.project_id = ? AND f.id = ? AND f.task_run_id = ?
          AND i.id = ? AND i.concluded_at IS NOT NULL
          AND i.conclusion IS NOT NULL
        """,
        (project_id, fact_id, run["id"], intent_id),
    ).fetchone()
    return row is not None


def _verified_completion_receipt(conn, project_id: str, run, context: dict) -> bool:
    digest = context.get("accepted_completion_digest")
    intent_id = context.get("accepted_completion_intent_id")
    if (
        run["task_type"] not in {"bootstrap", "reason"}
        or not _sha256_digest(digest)
        or not isinstance(intent_id, str)
        or not intent_id
    ):
        return False
    row = conn.execute(
        """
        SELECT 1 FROM intents
        WHERE project_id = ? AND id = ? AND task_run_id = ?
          AND to_fact_id = 'goal' AND concluded_at IS NOT NULL
          AND conclusion = 'completed'
        """,
        (project_id, intent_id, run["id"]),
    ).fetchone()
    return row is not None


def _verified_intent_prefix(conn, project_id: str, run, context: dict) -> bool:
    receipts = context.get("accepted_intent_submissions")
    if run["task_type"] != "reason" or not isinstance(receipts, dict) or not receipts:
        return False
    seen_intents = set()
    for index, receipt in receipts.items():
        if (
            not isinstance(index, str)
            or not index.isdigit()
            or str(int(index)) != index
            or not isinstance(receipt, dict)
            or not _sha256_digest(receipt.get("digest"))
        ):
            return False
        intent_id = receipt.get("intent_id")
        if not isinstance(intent_id, str) or not intent_id or intent_id in seen_intents:
            return False
        seen_intents.add(intent_id)
        row = conn.execute(
            """
            SELECT 1 FROM intents
            WHERE project_id = ? AND id = ? AND task_run_id = ?
            """,
            (project_id, intent_id, run["id"]),
        ).fetchone()
        if row is None:
            return False
    return True


def _verified_depth_review_receipt(conn, project, run) -> bool:
    if run["task_type"] != "reason":
        return False
    progress = _strict_object(project["assessment_progress_json"])
    if progress is None:
        return False
    fact_id = progress.get("depth_review_fact_id")
    input_digest = progress.get("accepted_input_digest")
    if not isinstance(fact_id, str) or not fact_id or not _sha256_digest(input_digest):
        return False
    fact = conn.execute(
        """
        SELECT creator, assessment_record_json FROM facts
        WHERE project_id = ? AND id = ? AND task_run_id = ?
        """,
        (project["id"], fact_id, run["id"]),
    ).fetchone()
    if fact is None or fact["creator"] != run["worker_name"]:
        return False
    record = _strict_object(fact["assessment_record_json"])
    return bool(
        record is not None
        and record.get("kind") == "depth_review"
        and record.get("input_digest") == input_digest
        and record.get("worker") == run["worker_name"]
        and record.get("task_run_id") == run["id"]
        and _sha256_digest(record.get("submission_digest"))
        and isinstance(record.get("selected"), list)
    )


def _verified_report_finish_receipt(conn, project_id: str, run):
    if run["task_type"] != "report":
        return None
    receipt = conn.execute(
        """
        SELECT * FROM assessment_report_finish_receipts
        WHERE project_id = ? AND task_run_id = ?
        """,
        (project_id, run["id"]),
    ).fetchone()
    if receipt is None or receipt["protocol"] != "cairn-assessment-report-finish-v1":
        return None
    request = _strict_object(receipt["request_json"])
    response = _strict_object(receipt["response_json"])
    if request is None or response is None:
        return None
    try:
        canonical_request = _canonical_json(request)
        canonical_response = _canonical_json(response)
    except (TypeError, ValueError):
        return None
    if (
        canonical_request != receipt["request_json"]
        or canonical_response != receipt["response_json"]
        or not _sha256_digest(receipt["request_sha256"])
        or sha256(canonical_request.encode("utf-8")).hexdigest()
        != receipt["request_sha256"]
        or request.get("protocol") != receipt["protocol"]
        or request.get("project_id") != project_id
        or request.get("task_run_id") != run["id"]
        or request.get("generation_token") != receipt["generation_token"]
        or request.get("artifact_id") != receipt["artifact_id"]
        or request.get("artifact_sha256") != receipt["artifact_sha256"]
        or request.get("input_snapshot_artifact_id")
        != receipt["input_snapshot_artifact_id"]
        or request.get("input_snapshot_sha256")
        != receipt["input_snapshot_sha256"]
        or response.get("protocol") != receipt["protocol"]
        or response.get("project_id") != project_id
        or response.get("task_run_id") != run["id"]
        or response.get("generation_token") != receipt["generation_token"]
        or response.get("request_sha256") != receipt["request_sha256"]
        or response.get("finish_disposition") not in {"accepted", "recovered"}
        or response.get("status") != request.get("status")
    ):
        return None
    if request["status"] != "ready":
        return "delivered"
    finalization = conn.execute(
        """
        SELECT status, generation_token, artifact_id,
               input_snapshot_artifact_id, input_snapshot_sha256
        FROM assessment_report_finalizations
        WHERE project_id = ?
        """,
        (project_id,),
    ).fetchone()
    if (
        finalization is None
        or finalization["status"] != "ready"
        or finalization["generation_token"] != receipt["generation_token"]
        or finalization["artifact_id"] != receipt["artifact_id"]
        or finalization["input_snapshot_artifact_id"]
        != receipt["input_snapshot_artifact_id"]
        or finalization["input_snapshot_sha256"]
        != receipt["input_snapshot_sha256"]
        or response.get("artifact_id") != receipt["artifact_id"]
        or response.get("input_snapshot_artifact_id")
        != receipt["input_snapshot_artifact_id"]
    ):
        return None
    return "accepted"


def _business_receipt_projection(conn, project, run, context: dict):
    if _verified_conclusion_receipt(conn, project["id"], run, context):
        return "accepted", "conclusion_accepted", "conclusion_receipt"
    if _verified_completion_receipt(conn, project["id"], run, context):
        return "accepted", "completion_accepted", "completion_receipt"
    if _verified_depth_review_receipt(conn, project, run):
        return "accepted", "depth_review_accepted", "depth_review_receipt"
    report = _verified_report_finish_receipt(conn, project["id"], run)
    if report == "accepted":
        return "accepted", "report_accepted", "report_finish_receipt"
    if report == "delivered":
        return "not_evaluated", "report_finish_failed", None
    if _verified_intent_prefix(conn, project["id"], run, context):
        # accepted-prefix 没有冻结的期望总数，只能证明部分写入已经被 Server 接受。
        return (
            "partially_accepted",
            "intent_prefix_accepted",
            "intent_submission_prefix",
        )
    return None


def _axis_task_run(
    conn,
    project_id: str,
    generation: int,
    *,
    preferred_task_run_id: str | None = None,
    include_unconfirmed_history: bool = False,
):
    """暂停排空时投影待确认的旧 Run，其余状态只使用当前代次。"""
    if preferred_task_run_id is not None:
        preferred = conn.execute(
            """
            SELECT * FROM task_runs
            WHERE project_id = ? AND execution_generation <= ? AND id = ?
            """,
            (project_id, generation, preferred_task_run_id),
        ).fetchone()
        if preferred is not None:
            return preferred
    if include_unconfirmed_history:
        historical = conn.execute(
            """
            SELECT * FROM task_runs
            WHERE project_id = ? AND execution_generation < ?
            ORDER BY COALESCE(ended_at, started_at) DESC, id DESC
            """,
            (project_id, generation),
        ).fetchall()
        for run in historical:
            context = _strict_object(run["runtime_context_json"])
            lifecycle = context.get("execution_lifecycle") if context is not None else None
            if not isinstance(lifecycle, dict) or lifecycle.get("generation") != run["execution_generation"] or lifecycle.get("termination_confirmed") is not True or lifecycle.get("cleanup_confirmed") is not True:
                return run
    return conn.execute(
        """
        SELECT * FROM task_runs
        WHERE project_id = ? AND execution_generation = ?
        ORDER BY CASE WHEN status IN ('queued', 'running') THEN 0 ELSE 1 END,
                 COALESCE(ended_at, started_at) DESC, id DESC
        LIMIT 1
        """,
        (project_id, generation),
    ).fetchone()


def _runtime_axes(
    conn,
    project,
    runtime: ProjectRuntime,
    *,
    preferred_task_run_id: str | None = None,
) -> RuntimeAxes:
    run = _axis_task_run(
        conn,
        runtime.project_id,
        runtime.execution_generation,
        preferred_task_run_id=preferred_task_run_id,
        include_unconfirmed_history=(
            runtime.project_status == "paused"
            and runtime.actions.resume.reason == "project_pause_draining"
        ),
    )
    execution = RuntimeAxis(state=runtime.state, code=runtime.code)
    if run is None:
        return RuntimeAxes(
            task_run_id=None,
            execution=execution,
            delivery=RuntimeAxis(state="none"),
            acceptance=RuntimeAcceptanceAxis(
                state="not_evaluated",
                code="business_acceptance_unproven",
            ),
            process=RuntimeProcessAxis(state="unobserved"),
            resources=RuntimeResourceAxis(state="unobserved"),
        )

    context = _strict_object(run["runtime_context_json"])
    if context is None:
        context = {}
    lifecycle = context.get("execution_lifecycle")
    if not isinstance(lifecycle, dict) or lifecycle.get("generation") != run["execution_generation"]:
        lifecycle = {}
    termination_confirmed = lifecycle.get("termination_confirmed") is True
    cleanup_confirmed = lifecycle.get("cleanup_confirmed") is True
    process_started = lifecycle.get("process_started") is True

    receipt = _business_receipt_projection(conn, project, run, context)
    if receipt is None:
        delivery = RuntimeAxis(
            state="unconfirmed",
            code="business_delivery_unconfirmed",
        )
        acceptance = RuntimeAcceptanceAxis(
            state="not_evaluated",
            code="business_acceptance_unproven",
        )
    else:
        acceptance_state, acceptance_code, evidence = receipt
        delivery = RuntimeAxis(
            state="confirmed",
            code="business_receipt_persisted",
        )
        acceptance = RuntimeAcceptanceAxis(
            state=acceptance_state,
            code=acceptance_code,
            evidence=evidence,
        )

    if termination_confirmed:
        process_state, process_code = "terminated", "termination_confirmed"
    elif process_started:
        process_state, process_code = "running", "process_started"
    else:
        process_state, process_code = "unconfirmed", "process_unconfirmed"

    if cleanup_confirmed:
        resource_state, resource_code = "clean", "cleanup_confirmed"
    elif termination_confirmed:
        resource_state, resource_code = "cleanup_pending", "cleanup_pending"
    elif process_started:
        resource_state, resource_code = "in_use", "process_resources_active"
    else:
        resource_state, resource_code = "unconfirmed", "resource_state_unconfirmed"

    return RuntimeAxes(
        task_run_id=run["id"],
        execution=execution,
        delivery=delivery,
        acceptance=acceptance,
        process=RuntimeProcessAxis(
            state=process_state,
            code=process_code,
            termination_confirmed=termination_confirmed,
        ),
        resources=RuntimeResourceAxis(
            state=resource_state,
            code=resource_code,
            cleanup_confirmed=cleanup_confirmed,
        ),
    )


def _checkpoint_staging(conn, project_id: str, generation: int) -> RuntimeCheckpointStaging:
    counts = conn.execute(
        """
        SELECT COUNT(*) AS received_count,
               SUM(CASE WHEN execution_generation = ? THEN 1 ELSE 0 END) AS current_count,
               SUM(CASE WHEN execution_generation <> ? THEN 1 ELSE 0 END) AS historical_count
        FROM execution_events
        WHERE project_id = ?
        """,
        (generation, generation, project_id),
    ).fetchone()
    received_count = int(counts["received_count"] or 0)
    last = conn.execute(
        """
        SELECT received_at, task_run_id
        FROM execution_events
        WHERE project_id = ?
        ORDER BY received_at DESC, id DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    return RuntimeCheckpointStaging(
        state="staged" if received_count else "none",
        received_count=received_count,
        current_generation_count=int(counts["current_count"] or 0),
        historical_generation_count=int(counts["historical_count"] or 0),
        last_received_at=last["received_at"] if last is not None else None,
        last_task_run_id=last["task_run_id"] if last is not None else None,
    )


def _attempt_projection(
    attempts: list[RuntimeAttempt],
    candidate_recovery: dict | None,
) -> tuple[str, str, str]:
    running = any(attempt.execution_stage == "running" for attempt in attempts)
    state = "running" if running else "starting"
    code = "process_started" if running else "execution_assigned"
    message = (
        "执行进程已启动；活动记录与模型进展需分别确认"
        if running
        else "任务已分配，执行进程尚未确认启动"
    )
    if all(
        attempt.execution_stage in {"stopping", "stopped"}
        for attempt in attempts
    ):
        state, code, message = (
            "waiting",
            "cleanup_pending",
            "执行正在退出或收尾，等待最终状态与资源清理确认",
        )
    if candidate_recovery is not None:
        if candidate_recovery["status"] == "unknown":
            state, code = "blocked", "candidate_recovery_unknown"
            message = _candidate_recovery_message(candidate_recovery)
        elif candidate_recovery["status"] == "running":
            state, code = "running", "process_started"
            message = "原执行仍在运行；候选恢复将等待物理终止，不会重复启动模型"
    return state, code, message


def _runtime_recovery(
    runtime: ProjectRuntime,
    *,
    candidate_recovery: dict | None = None,
) -> RuntimeRecovery:
    code = runtime.code
    if runtime.project_status == "paused" and runtime.actions.reauthorize.allowed:
        return RuntimeRecovery(
            route="reauthorize",
            code=code,
        )
    if runtime.project_status == "paused" and runtime.actions.resume.allowed:
        return RuntimeRecovery(route="resume", code="project_paused")
    if code in {"cleanup_pending", "project_finish_draining", "project_pause_draining"}:
        return RuntimeRecovery(route="wait", code=code)
    if code == "candidate_recovery_unknown" and candidate_recovery is not None:
        return RuntimeRecovery(
            route=_candidate_recovery_route(
                candidate_recovery["reason"],
                diagnostic_exhausted=(
                    candidate_recovery.get("diagnostic_exhausted") is True
                ),
            ),
            code=code,
        )
    if code == "execution_retry_cooldown":
        return RuntimeRecovery(route="automatic_retry", code=code)
    if code in {
        "execution_delivery_unavailable",
        "execution_retry_blocked",
        "execution_retry_exhausted",
    }:
        return RuntimeRecovery(route="manual", code=code)
    if runtime.state == "waiting":
        return RuntimeRecovery(route="wait", code=code)
    if runtime.state in {"blocked", "failed"}:
        return RuntimeRecovery(route="manual", code=code)
    return RuntimeRecovery(route="none")


def build_project_runtime(
    conn,
    project,
    *,
    now: str | None = None,
    schema_version: int = 1,
) -> ProjectRuntimeResponse:
    from .project_records import project_lifecycle_actions, project_pause_draining

    # Dispatcher 记录到微秒；截断到秒会把同一秒的真实观测误判为未来。
    now = now or datetime.now(timezone.utc).isoformat()
    dispatcher, runtime, dispatcher_id = _dispatcher(conn, project, now)
    project_id = project["id"]
    generation = int(project["execution_generation"]) if "execution_generation" in project.keys() else 0
    actions = project_lifecycle_actions(conn, project)
    runs = _attempts(conn, project_id)
    report_attempts = _current_report_attempts(conn, project)
    preferred_axis_task_run_id = None
    error = _last_error(conn, project_id)
    report_retry = _report_retry_decision(conn, project, now)
    retry = report_retry or _retry_decision(conn, project, now)
    authorization_reason = project_authorization_block_reason(conn, project, now=now)
    decision = _decision(runtime, project_id, generation, now) if dispatcher.freshness == "fresh" else None
    # 授权可在同一 generation 恢复；旧心跳不能覆盖 Server 当前的授权裁决。
    if decision is not None and decision.code in AUTHORIZATION_DECISIONS:
        if decision.code != authorization_reason or project["status"] != "active":
            decision = None
    scheduling = retry or decision
    candidate_recovery = (
        _candidate_recovery(
            conn,
            runtime,
            project,
            dispatcher_id,
            now,
        )
        if dispatcher.freshness == "fresh"
        else None
    )
    status = project["status"]
    state, code, message = "unknown", "scheduling_unobserved", "尚未收到本项目当前代次的调度决定，无法确认运行状态"
    if status in {"stopped", "completed"} and report_attempts:
        preferred_axis_task_run_id = report_attempts[0].id
        state, code, message = _attempt_projection(
            report_attempts,
            candidate_recovery,
        )
    elif status in {"stopped", "completed"} and report_retry is not None:
        code = report_retry.code
        state, message = DECISIONS[code]
    elif status in {"stopped", "completed"}:
        state = code = status
        message = "评估已结束，不再恢复执行" if status == "stopped" else "评估已完成"
        if status == "stopped" and project_pause_draining(conn, project):
            state, code = "stopping", "project_finish_draining"
            message = "已要求结束评估，仍在等待原执行退出和资源清理确认"
            if candidate_recovery is not None:
                if candidate_recovery["status"] == "unknown":
                    code = "candidate_recovery_unknown"
                    message = _candidate_recovery_message(candidate_recovery)
                elif candidate_recovery["status"] == "running":
                    message = "已要求结束评估，但原执行仍在运行；正在等待其退出后保留候选并完成收尾"
    elif status == "paused":
        draining = actions["resume"]["reason"] == "project_pause_draining" or bool(runs)
        state = "pausing" if draining else "paused"
        if draining:
            code = "project_pause_draining"
            message = "正在暂停，等待原执行退出及资源清理确认"
            if candidate_recovery is not None:
                if candidate_recovery["status"] == "unknown":
                    state, code = "blocked", "candidate_recovery_unknown"
                    message = _candidate_recovery_message(candidate_recovery)
                elif candidate_recovery["status"] == "running":
                    message = "评估已暂停，但原执行仍在运行；正在等待其退出后保留候选并完成收尾"
            if code == "project_pause_draining" and conn.execute(
                "SELECT 1 FROM project_container_activations WHERE project_id = ? AND state = 'inventory' LIMIT 1",
                (project_id,),
            ).fetchone() is not None:
                code = "container_inventory_unresolved"
                message = "暂停尚未确认：升级前容器的原 Docker 环境未知，请管理员核查；不能把当前缺席当成已停止"
        elif actions["reauthorize"]["allowed"]:
            code = authorization_reason or "project_reauthorization_required"
            message = AUTHORIZATION_DECISIONS[code][1]
        else:
            code = "project_paused"
            message = "评估已暂停，已保存的进度和证据保留"
    elif (
        authorization_reason is not None
        and not runs
        and not (scheduling is not None and scheduling.code == "cleanup_pending")
        and (candidate_recovery is None or candidate_recovery["status"] in {"stopped", "gone"})
    ):
        code = authorization_reason
        state, message = AUTHORIZATION_DECISIONS[code]
        scheduling = RuntimeScheduling(code=code, observed_at=now)
    elif retry is not None and not runs:
        code = retry.code
        state, message = DECISIONS[code]
    elif dispatcher.freshness == "unknown":
        code, message = "dispatcher_unobserved", "尚未收到调度器上报，无法确认是否可执行"
    elif dispatcher.freshness == "stale":
        code, message = "dispatcher_stale", "调度器上报已过期，当前执行状态暂不可确认"
    elif dispatcher.status == "error":
        startup = runtime.get("startup_check", {})
        startup_code = startup.get("code") if isinstance(startup, dict) else None
        code = startup_code if isinstance(startup_code, str) and startup_code in ERRORS else "dispatcher_internal_error"
        state, message = "failed", ERRORS[code]
    elif dispatcher.status == "starting":
        state, code, message = "starting", "dispatcher_starting", "调度器正在启动检查，尚未确认可派发任务"
    elif dispatcher.status in {"stopping", "unknown"}:
        code, message = "dispatcher_unavailable", "调度器尚未确认就绪，当前执行状态暂不可确认"
    elif not isinstance(runtime.get("runtime_capabilities"), list) or "project_runtime_v1" not in runtime["runtime_capabilities"]:
        code, message = "runtime_unsupported", "调度器尚不支持项目运行诊断，请协调升级服务端与调度器"
    elif runs:
        state, code, message = _attempt_projection(runs, candidate_recovery)
    elif scheduling is not None:
        code = scheduling.code
        state, message = DECISIONS[code]
    runtime_view = ProjectRuntime(
        project_id=project_id, observed_at=now, execution_generation=generation,
        project_status=status, state=state, code=code, message=message,
        dispatcher=dispatcher, scheduling=scheduling, active_runs=runs,
        last_error=error, last_activity=_last_activity(conn, project_id), actions=actions,
    )
    if schema_version == 1:
        return runtime_view
    if schema_version != 2:
        raise ValueError("unsupported project runtime schema version")
    return ProjectRuntimeV2(
        **runtime_view.model_dump(exclude={"schema_version"}),
        axes=_runtime_axes(
            conn,
            project,
            runtime_view,
            preferred_task_run_id=preferred_axis_task_run_id,
        ),
        checkpoint_staging=_checkpoint_staging(conn, project_id, generation),
        recovery=_runtime_recovery(
            runtime_view,
            candidate_recovery=candidate_recovery,
        ),
    )


def build_project_runtimes(
    conn,
    projects,
    *,
    schema_version: int = 1,
) -> list[ProjectRuntimeResponse]:
    now = datetime.now(timezone.utc).isoformat()
    return [
        build_project_runtime(
            conn,
            project,
            now=now,
            schema_version=schema_version,
        )
        for project in projects
    ]
