"""Intent 生命周期接口，处理任务认领、心跳、释放和收敛成 fact。"""

import json
from hashlib import sha256

from fastapi import APIRouter, Depends, HTTPException

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL
from cairn.evidence_contracts import evidence_refs
from cairn.server.schemas.projects import HistoricalConcludeVulnerabilityInput
from cairn.server.services.coverage_slots import validate_assessment_experiment
from cairn.server.services.assessment_convergence import (
    advance_assessment_phase,
    advance_goal_driven_convergence,
)
from cairn.server.services.assessment_policy import require_result_settlement_contract
from cairn.server.services.ids import get_project_or_404
from cairn.server.services.projects import lease_clock

from cairn.server.auth import RequestPrincipal, require_tenant_project
from cairn.server.db import get_conn
from cairn.server.models import (
    AbandonRequest,
    ConcludeRequest,
    ConcludeResponse,
    CreateIntentRequest,
    CreateTaskRunRequest,
    CreateVulnerabilityRequest,
    DiscoveredSurfaceInput,
    Fact,
    HeartbeatRequest,
    Intent,
    TaskRun,
)
from cairn.server.services import (
    abandon_intent_record,
    apply_structured_coverage_result,
    bind_fact_evidence,
    check_project_active,
    clear_open_intent_worker,
    close_assessment_attempt_for_task_run,
    confirm_vulnerability_for_slot,
    create_vulnerability,
    get_claimable_open_intent_or_404,
    get_releasable_open_intent_or_404,
    intent_to_model,
    mark_slot_explored,
    mark_slot_verified,
    next_fact_id,
    policy_from_project_row,
    register_coverage_scope_disposition,
    register_discovered_surfaces,
    register_discovered_vulnerability,
    record_vulnerability_verification,
    start_explore_attempt,
    utcnow,
    validate_structured_coverage_result,
    validate_task_run_exists,
    validate_execution_generation,
    validate_execution_lease,
)

router = APIRouter(tags=["intents"], dependencies=[Depends(require_tenant_project)])


@router.post(
    "/projects/{project_id}/intents",
    response_model=Intent,
    status_code=201,
)
def create_intent(
    project_id: str,
    body: CreateIntentRequest,
    principal: RequestPrincipal = Depends(require_tenant_project),
):
    from cairn.server.services.intent_creation import create_intent_record

    with get_conn() as conn:
        return create_intent_record(conn, project_id, body, principal)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/explore-attempts",
    response_model=TaskRun,
    status_code=201,
)
def create_explore_attempt(
    project_id: str, intent_id: str, body: CreateTaskRunRequest,
    principal: RequestPrincipal = Depends(require_tenant_project),
):
    with get_conn() as conn:
        return start_explore_attempt(conn, project_id, intent_id, body, principal=principal)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/heartbeat",
    response_model=Intent,
)
def heartbeat(project_id: str, intent_id: str, body: HeartbeatRequest):
    response: Intent | None = None
    renewal_rejected = False
    with get_conn() as conn:
        project = check_project_active(conn, project_id, task_run_id=body.task_run_id)
        if policy_from_project_row(project).protocol == GOAL_DRIVEN_PROTOCOL and project["status"] == "active":
            stop_reason = advance_goal_driven_convergence(conn, project_id)
            if stop_reason is not None:
                conn.commit()
                raise HTTPException(409, "assessment_stopping")
            if body.task_run_id is None:
                from cairn.server.services.project_authorization import (
                    require_project_authorization_current,
                )

                project = get_project_or_404(conn, project_id)
                require_project_authorization_current(conn, project)
        if body.task_run_id is not None:
            validate_execution_lease(conn, project_id, body.task_run_id, body.worker, intent_id=intent_id)
        # 校验会先回收超时租约；续租随后必须证明原 TaskRun 仍在运行。
        get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)
        now = utcnow()
        boot_id, now_ns = lease_clock()
        if body.task_run_id is None:
            updated = conn.execute(
                """
                UPDATE intents
                SET worker = ?, last_heartbeat_at = ?, heartbeat_boot_id = ?, heartbeat_boottime_ns = ?
                WHERE id = ? AND project_id = ?
                  AND to_fact_id IS NULL AND concluded_at IS NULL
                  AND worker IS NULL
                RETURNING *
                """,
                (body.worker, now, boot_id, now_ns, intent_id, project_id),
            ).fetchone()
            if updated is None:
                # 无 TaskRun 的请求只负责初次认领，不能作为已认领租约的续租。
                raise HTTPException(409, "Intent claim changed concurrently")
            response = intent_to_model(conn, updated, project_id)
        else:
            updated = conn.execute(
                """
                UPDATE intents
                SET last_heartbeat_at = ?, heartbeat_boot_id = ?, heartbeat_boottime_ns = ?
                WHERE id = ? AND project_id = ?
                  AND to_fact_id IS NULL AND concluded_at IS NULL
                  AND worker = ?
                  AND EXISTS (
                      SELECT 1
                      FROM task_runs
                      WHERE id = ? AND project_id = ?
                        AND intent_id = ? AND worker_name = ?
                        AND status = 'running'
                  )
                RETURNING *
                """,
                (
                    now,
                    boot_id,
                    now_ns,
                    intent_id,
                    project_id,
                    body.worker,
                    body.task_run_id,
                    project_id,
                    intent_id,
                    body.worker,
                ),
            ).fetchone()
            if updated is None:
                # 先正常提交 expire_workers 的终态写入，再向迟到 Worker 返回权威拒绝。
                renewal_rejected = True
            else:
                response = intent_to_model(conn, updated, project_id)

    if renewal_rejected:
        raise HTTPException(409, "Intent TaskRun is no longer active")
    assert response is not None
    return response


@router.post(
    "/projects/{project_id}/intents/{intent_id}/release",
    response_model=Intent,
)
def release(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id, task_run_id=body.task_run_id)
        validate_execution_lease(conn, project_id, body.task_run_id, body.worker, intent_id=intent_id)
        row = get_releasable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        if row["worker"] == body.worker:
            row = clear_open_intent_worker(conn, project_id, intent_id, "released")

        return intent_to_model(conn, row, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/abandon",
    response_model=Intent,
)
def abandon(project_id: str, intent_id: str, body: AbandonRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        if body.task_run_id is not None:
            validate_execution_generation(conn, project_id, body.task_run_id)
        return abandon_intent_record(conn, project_id, intent_id, body.worker)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/conclude",
    response_model=ConcludeResponse,
)
def conclude(project_id: str, intent_id: str, body: ConcludeRequest):
    with get_conn() as conn:
        # 重发只返回原事务结果；与项目停止共用行锁，不重新开放目标写入。
        conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
        project_row = get_project_or_404(conn, project_id)
        from cairn.server.services.project_records import require_unmanaged_project
        require_unmanaged_project(conn, project_id)
        policy = policy_from_project_row(project_row)
        goal_driven = policy.protocol == GOAL_DRIVEN_PROTOCOL
        require_result_settlement_contract(
            project_row,
            task_run_id=body.task_run_id,
        )
        receipt_payload = body.model_dump(by_alias=True, exclude_none=True)
        if isinstance(body.vulnerability, HistoricalConcludeVulnerabilityInput):
            # 只复现旧回执的摘要算法；不修改请求模型、历史原文或已存 digest。
            for field in ("repro_request", "repro_response"):
                receipt_payload["vulnerability"][field] = receipt_payload["vulnerability"][field].rstrip()
        submission_digest = sha256(json.dumps(receipt_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        prior = conn.execute("SELECT * FROM intents WHERE project_id = ? AND id = ?", (project_id, intent_id)).fetchone()
        if prior is not None and prior["to_fact_id"] and body.task_run_id:
            prior_run = validate_task_run_exists(conn, project_id, body.task_run_id)
            context = json.loads(prior_run["runtime_context_json"] or "{}")
            if (
                prior_run["worker_name"] != body.worker or prior_run["intent_id"] != intent_id
                or context.get("accepted_result_digest") != submission_digest
                or context.get("accepted_fact_id") != prior["to_fact_id"]
            ):
                raise HTTPException(409, "assessment_result_conflict")
            accepted = conn.execute("SELECT * FROM facts WHERE project_id = ? AND id = ?", (project_id, prior["to_fact_id"])).fetchone()
            return ConcludeResponse(fact=Fact(**dict(accepted), assessment_record=json.loads(accepted["assessment_record_json"]) if accepted["assessment_record_json"] else None), intent=intent_to_model(conn, prior, project_id))
        # 旧结构仅能命中上方已接受回执，不得作为新发现进入证据链。
        if isinstance(body.vulnerability, HistoricalConcludeVulnerabilityInput):
            raise HTTPException(422, "vulnerability_schema_version_2_required")
        discovery_evidence = evidence_refs(body.vulnerability.evidence_items) if body.vulnerability is not None else []
        check_project_active(conn, project_id, task_run_id=body.task_run_id)
        validate_execution_generation(
            conn,
            project_id,
            body.task_run_id,
            allow_stopping_settlement=goal_driven,
        )
        intent_row = get_claimable_open_intent_or_404(
            conn, project_id, intent_id, body.worker
        )
        task_run = None
        if body.task_run_id is not None:
            task_run = validate_task_run_exists(conn, project_id, body.task_run_id)
            if task_run["intent_id"] != intent_id:
                raise HTTPException(
                    422,
                    "task_run_id does not belong to the concluded intent",
                )
        if task_run is not None:
            if task_run["status"] != "running" or task_run["worker_name"] != body.worker or intent_row["worker"] != body.worker:
                raise HTTPException(409, "assessment_conclude_run_not_owned_or_active")
            if task_run["worker_type"] != "pi":
                raise HTTPException(409, "execution_engine_unsupported")
        if policy.phased or goal_driven:
            if task_run is None:
                raise HTTPException(409, "assessment_conclude_run_not_owned_or_active")
            if (
                policy.phased
                and policy.explore_deadline
                and utcnow() >= policy.explore_deadline
            ):
                raise HTTPException(409, "explore_deadline_exceeded")
        assessment_role = (
            str(intent_row["assessment_role"] or "discovery")
            if "assessment_role" in intent_row.keys()
            else "discovery"
        )
        verification_evidence = (
            body.verification_result.evidence_refs
            if body.verification_result is not None
            else []
        )
        if assessment_role == "verifier":
            if not policy.independent_verifier_enabled:
                raise HTTPException(422, "independent_verifier_not_enabled")
            if body.verification_result is None:
                raise HTTPException(422, "verification_result_required")
            if body.task_run_id is None:
                raise HTTPException(422, "verifier_task_run_required")
            if any(
                (
                    body.coverage_result is not None,
                    body.assessment_record is not None,
                    body.conclusion != "completed",
                    body.vulnerability is not None,
                    bool(body.discovered_surfaces),
                    body.coverage_scope_disposition is not None,
                )
            ):
                raise HTTPException(422, "verifier_output_must_not_mutate_discovery")
            structured_slot = None
        else:
            if body.verification_result is not None:
                raise HTTPException(422, "verification_result_requires_verifier_intent")
            structured_slot = validate_structured_coverage_result(
                conn,
                project_id,
                intent_row,
                body.coverage_result,
                body.vulnerability,
                [*body.evidence, *discovery_evidence],
                task_run_id=body.task_run_id,
            )

        goal_bootstrap = (
            goal_driven
            and task_run is not None
            and task_run["task_type"] == "bootstrap"
        )
        if goal_bootstrap and (
            body.coverage_result is not None or body.assessment_record is not None
        ):
            raise HTTPException(422, "goal_driven_bootstrap_fact_only")
        if assessment_role == "verifier" or goal_bootstrap:
            assessment_record = None
        else:
            assessment_record = validate_assessment_experiment(
                conn, project_id, intent_row, body, structured_slot
            )
        now = utcnow()
        fid = next_fact_id(conn, project_id)

        conn.execute(
            "INSERT INTO facts (id, project_id, description, task_run_id, creator, created_at, assessment_record_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fid, project_id, body.description, body.task_run_id, body.worker, now, json.dumps(assessment_record, ensure_ascii=False, sort_keys=True) if assessment_record is not None else None),
        )
        # 证据、Surface 与 Intent 收敛共享事务，任何后续校验失败都不能留下半成品。
        if body.coverage_scope_disposition is not None and body.discovered_surfaces:
            raise HTTPException(
                422,
                "coverage_scope_disposition_conflicts_with_discovered_surfaces",
            )
        scope_evidence = (
            body.coverage_scope_disposition.evidence_refs
            if body.coverage_scope_disposition is not None
            else []
        )
        bind_fact_evidence(
            conn,
            project_id,
            fid,
            [*body.evidence, *scope_evidence, *verification_evidence, *discovery_evidence],
        )
        if body.coverage_scope_disposition is not None:
            if body.task_run_id is None:
                raise HTTPException(
                    422,
                    "coverage_scope_disposition_bootstrap_required",
                )
            register_coverage_scope_disposition(
                conn,
                project_id,
                body.coverage_scope_disposition,
                source_intent_id=intent_id,
                source_fact_id=fid,
                source_task_run_id=body.task_run_id,
                allow_stopping_settlement=goal_driven,
            )
        surface_evidence = []
        if body.discovered_surfaces:
            surface_tier = "extended"
            if task_run is not None and task_run["task_type"] == "bootstrap":
                surface_tier = "core"
            parsed_surfaces = [
                DiscoveredSurfaceInput.model_validate(surface)
                for surface in body.discovered_surfaces
            ]
            register_discovered_surfaces(
                conn,
                project_id,
                parsed_surfaces,
                tier=surface_tier,
                source_intent_id=intent_id,
                source_fact_id=fid,
                source_task_run_id=body.task_run_id,
                fallback_evidence=body.evidence,
                allow_stopping_settlement=goal_driven,
            )
            surface_evidence = [
                ref
                for surface in parsed_surfaces
                for ref in surface.evidence_refs
            ]
        bind_fact_evidence(
            conn,
            project_id,
            fid,
            [*body.evidence, *surface_evidence, *verification_evidence, *discovery_evidence],
        )
        updated = conn.execute(
            """
            UPDATE intents
            SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ?, conclusion = ?
            WHERE id = ? AND project_id = ?
              AND to_fact_id IS NULL AND concluded_at IS NULL
              AND (worker IS NULL OR worker = ?)
            RETURNING *
            """,
            (
                fid,
                body.worker,
                now,
                now,
                body.conclusion,
                intent_id,
                project_id,
                body.worker,
            ),
        ).fetchone()
        if updated is None:
            raise HTTPException(409, "Intent conclusion changed concurrently")
        if assessment_role == "verifier":
            record_vulnerability_verification(
                conn,
                project_id,
                intent_id,
                fid,
                body.task_run_id,
                body.worker,
                body.verification_result,
            )
        # pentest 场景：该 intent 绑定了 slot_key 时回写 coverage_slot 状态。
        # - conclude 前 slot 处于 pending/exploring（首次 explore）→ 置 explored
        # - conclude 前 slot 已 explored（Phase 3 verifier 对同 slot 再跑一轮复现）→ 升级 verified
        slot_key = updated["slot_key"] if "slot_key" in updated.keys() else None
        verified_now = False
        if policy.phased and intent_row["depth_review_fact_id"]:
            # 补充实验仅追加 Fact/Observation，不覆盖基线结果或改变覆盖分母。
            pass
        elif structured_slot is not None and body.coverage_result is not None:
            apply_structured_coverage_result(
                conn,
                project_id,
                slot_key,
                intent_id=intent_id,
                fact_id=fid,
                coverage_result=body.coverage_result,
            )
        elif slot_key:
            prior = conn.execute(
                "SELECT status FROM coverage_slots WHERE project_id = ? AND slot_key = ?",
                (project_id, slot_key),
            ).fetchone()
            if prior is not None and prior["status"] == "explored":
                mark_slot_verified(
                    conn,
                    project_id,
                    slot_key,
                    intent_id=intent_id,
                    fact_id=fid,
                    verifier_run_id=body.task_run_id,
                )
                verified_now = True
            else:
                mark_slot_explored(
                    conn, project_id, slot_key, intent_id=intent_id, fact_id=fid
                )

        # 漏洞自动确认：dispatcher 从 fact.vulnerability 提取并透传，事务内写漏洞库。
        # source_fact_id=fid 绑定来源 fact；slot_key 用 intent 的（pentest）或 body.vulnerability.slot_key。
        if body.vulnerability is not None:
            vin = body.vulnerability
            vuln_body = CreateVulnerabilityRequest(
                title=vin.title,
                severity=vin.severity,
                vuln_type=vin.vuln_type,
                target=vin.target,
                evidence_items=vin.evidence_items,
                reproduction_steps=vin.reproduction_steps,
                remediation=vin.remediation,
                confidence=vin.confidence,
                source_fact_id=fid,
                slot_key=slot_key or vin.slot_key,
            )
            if policy.vulnerability_identity_enabled:
                if vin.identity is None:
                    # 身份策略独立于 Coverage 协议，所有启用指纹的自动发现都必须提供 identity。
                    raise HTTPException(
                        422,
                        "vulnerability identity is required",
                    )
                register_discovered_vulnerability(
                    conn,
                    project_id,
                    vuln_body,
                    vin.identity,
                    fingerprint_version=(
                        policy.fingerprint_version or 1
                    ),
                    discovered_by_run_id=body.task_run_id,
                    discovered_by_worker=body.worker,
                )
            else:
                create_vulnerability(
                    conn,
                    project_id,
                    vuln_body,
                    discovered_by_run_id=body.task_run_id,
                    discovered_by_worker=body.worker,
                )
            # pentest verifier 复现确认：verified 路径产出的漏洞直接升级 confirmed。
            if verified_now:
                confirm_vulnerability_for_slot(conn, project_id, slot_key)

        attempt_accounted = (
            (policy.structured or goal_driven)
            and task_run is not None
            and task_run["task_type"] == "explore"
        )
        if attempt_accounted:
            close_assessment_attempt_for_task_run(
                conn,
                project_id,
                intent_id,
                body.task_run_id,
                outcome=(
                    body.verification_result.outcome
                    if body.verification_result is not None
                    else (
                        body.assessment_record.outcome
                        if goal_driven and body.assessment_record is not None
                        else (
                            body.coverage_result.outcome
                            if body.coverage_result is not None
                            else body.conclusion
                        )
                    )
                ),
                require_linked_task_run=(
                    goal_driven or policy.outcome_contract_version >= 2
                ),
            )

        if task_run is not None:
            context = json.loads(task_run["runtime_context_json"] or "{}")
            context.update(accepted_result_digest=submission_digest, accepted_fact_id=fid)
            conn.execute("UPDATE task_runs SET runtime_context_json = ? WHERE project_id = ? AND id = ?", (json.dumps(context, sort_keys=True), project_id, body.task_run_id))
        if goal_driven and body.assessment_record is not None:
            if body.assessment_record.outcome in {
                "temporarily_blocked",
                "permanently_unreachable",
                "out_of_scope",
                "not_applicable",
            }:
                conn.execute(
                    """UPDATE intents SET conclusion_reason = ?
                       WHERE project_id = ? AND id = ?""",
                    (
                        body.assessment_record.limitation_reason,
                        project_id,
                        intent_id,
                    ),
                )
                updated = conn.execute(
                    "SELECT * FROM intents WHERE project_id = ? AND id = ?",
                    (project_id, intent_id),
                ).fetchone()
        elif policy.phased:
            if body.coverage_result is not None and body.coverage_result.outcome == "temporarily_blocked":
                conn.execute("UPDATE intents SET conclusion_reason = ? WHERE project_id = ? AND id = ?", (body.coverage_result.limitation_reason, project_id, intent_id))
                updated = conn.execute("SELECT * FROM intents WHERE project_id = ? AND id = ?", (project_id, intent_id)).fetchone()
            advance_assessment_phase(conn, project_id)
        if goal_driven:
            # 结果先原子接受，再推进执行截止收尾；当前 TaskRun 终态 ACK 后才真正 stopped。
            advance_goal_driven_convergence(conn, project_id)
        return ConcludeResponse(
            fact=Fact(id=fid, description=body.description, task_run_id=body.task_run_id, creator=body.worker, created_at=now, assessment_record=assessment_record),
            intent=intent_to_model(conn, updated, project_id),
        )
