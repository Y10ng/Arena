"""Intent 创建复用调用方事务，保证业务对象与入口收据一起保存。"""

import json
from hashlib import sha256

from fastapi import HTTPException

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL
from cairn.server.auth import RequestPrincipal
from cairn.server.models import CreateIntentRequest
from cairn.server.services import (
    assign_slot_intent,
    check_project_active,
    get_intent_or_404,
    intent_to_model,
    next_intent_id,
    policy_from_project_row,
    utcnow,
    validate_execution_generation,
    validate_facts_exist,
    validate_goal_not_in_sources,
    validate_intent_creator_worker,
    validate_task_run_exists,
)
from .assessment_convergence import advance_goal_driven_convergence
from .assessment_policy import require_result_settlement_contract
from .ids import get_project_or_404
from .projects import lease_clock
from .structured_intent_bindings import (
    ordinary_structured_reason_enabled,
    validate_structured_intent_submission,
)


def create_intent_record(conn, project_id: str, body: CreateIntentRequest, principal: RequestPrincipal):
    # 与停止、阶段推进和 TaskRun 更新共享项目锁，创建与收据必须原子落盘。
    conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
    project = get_project_or_404(conn, project_id)
    # 浏览器 creator 由 session 派生；machine 协议仍要求显式 creator/worker。
    is_user_request = isinstance(principal, RequestPrincipal) and principal.kind == "user"
    creator = principal.subject_id if is_user_request else body.creator
    if creator is None:
        raise HTTPException(422, "creator is required for machine requests")
    worker = None if is_user_request else body.worker
    validate_intent_creator_worker(creator, worker)
    if body.submission_index is not None and (is_user_request or body.task_run_id is None):
        raise HTTPException(422, "intent_submission_requires_reason_task_run")
    require_result_settlement_contract(project, task_run_id=body.task_run_id)
    run = None
    run_context = {}
    receipts = {}
    submission_digest = None
    if body.task_run_id is not None:
        run = validate_task_run_exists(conn, project_id, body.task_run_id)
        if not is_user_request:
            if body.submission_index is None:
                raise HTTPException(422, "intent_submission_index_required")
            if run["worker_name"] != creator or run["worker_type"] != "pi" or run["task_type"] != "reason":
                raise HTTPException(409, "intent_submission_run_mismatch")
            run_context = json.loads(run["runtime_context_json"] or "{}")
            receipts = run_context.get("accepted_intent_submissions", {})
            submission_digest = sha256(json.dumps(
                body.model_dump(by_alias=True, exclude_none=True),
                sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode()).hexdigest()
            receipt = receipts.get(str(body.submission_index))
            if receipt is not None:
                if receipt.get("digest") != submission_digest:
                    raise HTTPException(409, "intent_submission_conflict")
                accepted = get_intent_or_404(conn, project_id, receipt["intent_id"])
                if accepted["task_run_id"] != body.task_run_id or accepted["creator"] != creator:
                    raise HTTPException(409, "intent_submission_conflict")
                # 响应丢失后仅返回原 Intent；项目/阶段/TaskRun 已终结也不会再次创建。
                return intent_to_model(conn, accepted, project_id)
            if run["status"] != "running":
                raise HTTPException(409, "intent_submission_run_mismatch")
    check_project_active(conn, project_id, task_run_id=body.task_run_id)
    if policy_from_project_row(project).protocol == GOAL_DRIVEN_PROTOCOL and project["status"] == "active":
        stop_reason = advance_goal_driven_convergence(conn, project_id)
        if stop_reason is not None:
            conn.commit()
            raise HTTPException(409, "assessment_stopping")
    if body.task_run_id is not None:
        validate_execution_generation(conn, project_id, body.task_run_id)
    # 深测义务只允许由原子选题事务创建，普通入口不得伪造标记。
    if policy_from_project_row(project).phased:
        phase = json.loads(project["assessment_progress_json"] or "{}").get("phase")
        if phase in {"depth_review", "finalizing"}:
            raise HTTPException(409, "assessment_discovery_phase_closed")
    # Intent 必须从已有 fact 出发，且不能把 goal 再作为来源形成无意义闭环。
    validate_facts_exist(conn, project_id, body.from_)
    validate_goal_not_in_sources(body.from_)
    # depends_on 校验：若设置，引用的 intent 必须存在于同项目。
    if body.depends_on:
        get_intent_or_404(conn, project_id, body.depends_on)
    if (
        run is not None
        and run["task_type"] == "reason"
        and ordinary_structured_reason_enabled(project)
    ):
        validate_structured_intent_submission(
            conn,
            project,
            run_context,
            slot_key=body.slot_key,
            strategy_key=body.strategy_key,
        )

    now = utcnow()
    iid = next_intent_id(conn, project_id)
    claimed = worker is not None
    boot_id, now_ns = lease_clock() if claimed else (None, None)
    # worker 为空表示待调度；worker 非空表示创建时已经由调用方认领。
    conn.execute(
        "INSERT INTO intents (id, project_id, to_fact_id, description, creator, task_run_id, worker, last_heartbeat_at, heartbeat_boot_id, heartbeat_boottime_ns, created_at, concluded_at, depends_on, priority, slot_key, strategy_key) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        (
            iid,
            project_id,
            body.description,
            creator,
            body.task_run_id,
            worker,
            now if claimed else None,
            boot_id,
            now_ns,
            now,
            body.depends_on,
            body.priority,
            body.slot_key,
            body.strategy_key,
        ),
    )
    for source_order, fid in enumerate(body.from_):
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id, source_order) VALUES (?, ?, ?, ?)",
            (iid, project_id, fid, source_order),
        )
    # pentest 场景：intent 绑定 slot_key 时关联到 coverage_slots（不改 status，待 conclude 置 explored）。
    if body.slot_key:
        assign_slot_intent(conn, project_id, body.slot_key, iid)

    if submission_digest is not None:
        receipts[str(body.submission_index)] = {"digest": submission_digest, "intent_id": iid}
        run_context["accepted_intent_submissions"] = receipts
        conn.execute(
            "UPDATE task_runs SET runtime_context_json = ? WHERE project_id = ? AND id = ?",
            (json.dumps(run_context, sort_keys=True, ensure_ascii=False), project_id, body.task_run_id),
        )
    return intent_to_model(conn, get_intent_or_404(conn, project_id, iid), project_id)
