"""覆盖槽位与 Web assessment v2 的证据、归属、阶段和预算边界。

通用 Slot/A+B+D 使用冻结 pi 的合成非 Web 场景；Web 使用持久阶段与实验记录，
历史快照只验证可读、不可执行，不恢复旧 Web 的自动完成语义。
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3

import pytest
from fastapi import HTTPException

from cairn.server import db
from cairn.server.auth import RequestPrincipal
from cairn.server.models import (
    CompleteRequest,
    ConcludeRequest,
    ConcludeVulnerabilityInput,
    CoverageResultInput,
    CreateIntentRequest,
    EvidenceRef,
    CreateTaskRunRequest,
    VulnerabilityAssetIdentity,
    VulnerabilityEntrypointIdentity,
    VulnerabilityIdentityInput,
    HeartbeatRequest,
    UpdateTaskRunRequest,
)
from cairn.server.routers import coverage_slots as coverage_slots_router
from cairn.server.routers import intents as intents_router
from cairn.server.services import projects as projects_service
from cairn.server.services.surface_manifest import (
    normalize_surface_key,
    register_discovered_surfaces,
)
from cairn.server.services import (
    advance_structured_convergence,
    clear_open_intent_worker,
    close_assessment_attempt_for_task_run,
    count_open_coverage_slots,
    list_coverage_slots,
    list_scenarios,
    list_surface_manifest_entries,
    mark_slot_explored,
    mark_slot_verified,
    project_completion,
    start_explore_attempt,
    surface_manifest_digest,
    surface_manifest_id,
    upsert_coverage_slots,
)


TEST_DISPATCHER = RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(conn, "sqlite")
    return conn


def _seed_structured_project(conn: sqlite3.Connection, project_id: str = "p1") -> None:
    # 非 Web 的合成场景保留通用 Slot/A+B+D 单元测试，不冒充旧 Web 执行兼容。
    features = {
        "complete_gate": True,
        "coverage_gate": True,
        "verifier": False,
        "execution_contract_version": 1,
        "execution_engine": "pi",
    }
    conn.execute(
        "INSERT INTO scenarios (key, label, profile_id, prompt_group, knowledge, features_json) "
        "VALUES ('coverage-unit', 'Coverage unit', 'default', 'default', '', ?)",
        (json.dumps(features),),
    )
    conn.execute(
        "INSERT INTO projects (id, title, status, created_at, scenario_key, scenario_snapshot_json) "
        "VALUES (?, ?, 'active', ?, 'coverage-unit', ?)",
        (project_id, "Slot Assessment", "2026-07-13T00:00:00Z", json.dumps({"features": features})),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES (?, ?, ?, 'system', ?)",
        ("fsrc", project_id, "source fact", "2026-07-13T00:00:00Z"),
    )


def _seed_default_project(conn: sqlite3.Connection, project_id: str = "d1") -> None:
    # 无 assessment 的非 Web 场景只冻结执行契约，覆盖预算隔离与通用槽位门。
    conn.execute(
        "INSERT INTO scenarios (key, label, profile_id, prompt_group, knowledge, features_json) "
        "VALUES ('non-web-unit', 'Non-Web unit', 'default', 'default', '', '{}')"
    )
    conn.execute(
        "INSERT INTO projects (id, title, status, created_at, scenario_key, scenario_snapshot_json) "
        "VALUES (?, ?, 'active', ?, 'non-web-unit', ?)",
        (project_id, "Non-Web", "2026-07-13T00:00:00Z", json.dumps({"features": {
            "execution_contract_version": 1, "execution_engine": "pi",
        }})),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES (?, ?, ?, 'system', ?)",
        ("fsrc", project_id, "source fact", "2026-07-13T00:00:00Z"),
    )


def _seed_default_copy_project(
    conn: sqlite3.Connection,
    project_id: str = "dc1",
    *,
    outcome_contract_version: int = 2,
    blocker_cooldown_version: int = 0,
    surface_manifest_version: int = 0,
    minimum_coverage_version: int = 0,
) -> None:
    # 各子门按用例独立启用；统一执行、评估版本与阶段不可关闭。
    snapshot = {
        "key": "default-copy-v2",
        "features": {
            "execution_contract_version": 1,
            "execution_engine": "pi",
            "assessment_contract_version": 2,
            "outcome_contract_version": outcome_contract_version,
            "blocker_cooldown_version": blocker_cooldown_version,
            "surface_manifest_version": surface_manifest_version,
            "minimum_coverage_version": minimum_coverage_version,
        },
    }
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, scenario_key, assessment_protocol,
            explore_budget, explore_deadline, scenario_snapshot_json, assessment_progress_json
        ) VALUES (?, ?, 'active', ?, 'default-copy-v2', 'default-copy-v1', 40, ?, ?,
                  '{"version":1,"phase":"baseline"}')
        """,
        (
            project_id,
            "Default Copy",
            "2026-07-13T00:00:00Z",
            "2099-01-01T00:00:00Z",
            json.dumps(snapshot),
        ),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES (?, ?, ?, 'system', ?)",
        ("fsrc", project_id, "source fact", "2026-07-13T00:00:00Z"),
    )


def _seed_reason_run(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    worker: str,
    task_run_id: str = "reason-run",
) -> str:
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, task_type, worker_name, worker_type, status, started_at
        ) VALUES (?, ?, 'reason', ?, 'pi', 'running', ?)
        """,
        (task_run_id, project_id, worker, "2026-07-13T00:01:00Z"),
    )
    return task_run_id


def _seed_current_run_artifact(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    intent_id: str | None = None,
    task_run_id: str = "tr_current",
    artifact_id: str = "a001",
) -> EvidenceRef:
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, intent_id, task_type, worker_name, worker_type, status, started_at
        ) VALUES (?, ?, ?, 'explore', 'w', 'pi', 'running', ?)
        """,
        (task_run_id, project_id, intent_id, "2026-07-13T00:01:00Z"),
    )
    conn.execute(
        """
        INSERT INTO artifacts (
            id, project_id, task_run_id, kind, category, uri, sha256, size, created_at
        ) VALUES (?, ?, ?, 'raw_output', 'evidence', ?, 'digest', 10, ?)
        """,
        (
            artifact_id,
            project_id,
            task_run_id,
            f"artifact://{artifact_id}",
            "2026-07-13T00:02:00Z",
        ),
    )
    if intent_id is not None:
        conn.execute(
            "UPDATE intents SET worker = 'w', last_heartbeat_at = ? WHERE project_id = ? AND id = ?",
            (projects_service.utcnow(), project_id, intent_id),
        )
    return EvidenceRef(artifact_id=artifact_id, pointer="request/response")


def _experiment(result: CoverageResultInput, evidence: EvidenceRef) -> dict:
    """合成实验仍明确保留身份前提、对照与局限，不能替代服务端授权证据检查。"""
    return {
        "version": 1,
        "kind": "experiment",
        "hypothesis": "The selected input may violate the documented security boundary",
        "preconditions": ["Approved test identities and the baseline target state are available"],
        "baseline": result.baseline,
        "variants": result.variants,
        "controls": ["Repeat the authorized baseline without the changed input"],
        "observations": ["The current-run request/response records the comparison"],
        "impact": "Limited to the observed target and test identities",
        "limitations": [result.limitation_details or result.limitation_reason]
        if result.outcome == "temporarily_blocked" else [],
        "evidence_refs": [evidence.model_dump(exclude_none=True)],
    }


def _structured_vulnerability(**updates) -> ConcludeVulnerabilityInput:
    values = {
        "title": "Authorization bypass",
        "severity": "medium",
        "vuln_type": "IDOR",
        "target": "GET /api/orders/123",
        "identity": VulnerabilityIdentityInput(
            vulnerability_class="IDOR",
            asset=VulnerabilityAssetIdentity(protocol="https", host="example.com"),
            entrypoint=VulnerabilityEntrypointIdentity(
                method="GET", route="/api/orders/123"
            ),
        ),
    }
    values.update(updates)
    return ConcludeVulnerabilityInput(**values)


@pytest.mark.parametrize(
    ("features", "progress", "expected_detail"),
    [
        ({}, None, "execution_contract_unsupported"),
        ({"execution_contract_version": 1, "execution_engine": "pi", "assessment_contract_version": 1},
         None, "assessment_protocol_historical"),
        ({"execution_contract_version": 1, "execution_engine": "pi", "assessment_contract_version": 2},
         None, "assessment_protocol_historical"),
    ],
)
def test_historical_web_coverage_is_readable_but_execution_is_rejected(
    monkeypatch: pytest.MonkeyPatch, features: dict, progress: str | None, expected_detail: str,
) -> None:
    """历史快照可读取既有证据投影，但缺执行/评估契约或持久阶段均不得写入执行结果。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        with conn:
            yield conn

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn)
        upsert_coverage_slots(conn, "dc1", [{
            "kind": "endpoint", "surface_kind": "endpoint", "surface_key": "GET https://example.test/account",
            "test_family": "authorization", "applicability_basis": "账户访问边界", "completion_criteria": "跨身份对照",
        }])
        _insert_open_intent(conn, "dc1")
        conn.execute(
            "UPDATE projects SET scenario_snapshot_json = ?, assessment_progress_json = ? WHERE id = 'dc1'",
            (json.dumps({"features": features}), progress),
        )
        conn.commit()
        assert len(list_coverage_slots(conn, "dc1")) == 1
        assert list_coverage_slots(conn, "dc1")[0].status == "pending"
        operations = [
            (
                lambda: start_explore_attempt(
                    conn,
                    "dc1",
                    "i001",
                    _explore_attempt_body(),
                    principal=TEST_DISPATCHER,
                ),
                expected_detail,
            ),
            (
                lambda: intents_router.create_intent(
                    "dc1",
                    CreateIntentRequest(
                        from_=["fsrc"],
                        description="new work",
                        creator="reason",
                    ),
                ),
                "historical_result_task_run_required",
            ),
            (
                lambda: intents_router.conclude(
                    "dc1",
                    "i001",
                    ConcludeRequest(
                        worker="worker-1",
                        description="historical result",
                    ),
                ),
                "historical_result_task_run_required",
            ),
            (
                lambda: project_completion.complete_project_record_with_conn(
                    conn,
                    "dc1",
                    CompleteRequest(
                        from_=["fsrc"],
                        description="done",
                        worker="reason",
                    ),
                ),
                "historical_result_task_run_required",
            ),
        ]
        for operation, detail in operations:
            with pytest.raises(HTTPException) as rejected:
                with conn:
                    operation()
            assert rejected.value.status_code == 409
            assert rejected.value.detail == detail
        assert conn.execute("SELECT COUNT(*) FROM facts WHERE project_id = 'dc1'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE project_id = 'dc1'").fetchone()[0] == 0
        intent = conn.execute("SELECT worker, concluded_at FROM intents WHERE project_id = 'dc1'").fetchone()
        assert dict(intent) == {"worker": None, "concluded_at": None}
        assert conn.execute("SELECT status FROM projects WHERE id = 'dc1'").fetchone()[0] == "active"
    finally:
        conn.close()


def test_coverage_slot_upsert_dedup_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """登记槽位去重、初始 pending、upsert 不重置已 explore 的状态。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(coverage_slots_router, "get_conn", fake_get_conn)
    try:
        _seed_structured_project(conn)
        slots = [
            {"kind": "endpoint", "key": "endpoint:GET /", "description": "web root"},
            {"kind": "service", "key": "service:tcp/22", "description": "SSH"},
            # 同批次重复 key 应被 service 层 UPSERT 去重（ON CONFLICT）。
            {"kind": "endpoint", "key": "endpoint:GET /", "description": "dup"},
        ]
        upsert_coverage_slots(conn, "p1", slots, source_intent_id="i000")
        all_slots = list_coverage_slots(conn, "p1")
        assert len(all_slots) == 2
        assert {s.slot_key for s in all_slots} == {"endpoint:GET /", "service:tcp/22"}
        assert all(s.status == "pending" for s in all_slots)
        assert count_open_coverage_slots(conn, "p1") == 2

        # 标记一个 explored 后再 upsert 同 key，status 不应被重置回 pending。
        mark_slot_explored(conn, "p1", "endpoint:GET /", intent_id="i001", fact_id="f001")
        upsert_coverage_slots(conn, "p1", [{"kind": "endpoint", "key": "endpoint:GET /", "description": "re-enumerated"}])
        endpoint_slot = [s for s in list_coverage_slots(conn, "p1") if s.slot_key == "endpoint:GET /"][0]
        assert endpoint_slot.status == "explored"
        assert endpoint_slot.resulting_fact_id == "f001"
        assert count_open_coverage_slots(conn, "p1") == 1
    finally:
        conn.close()


def test_coverage_gate_blocks_then_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    """结构化评估完成门：有 pending slot 时 complete → 409；全部 explored 后 complete → 成功。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(project_completion, "get_conn", fake_get_conn)
    try:
        _seed_structured_project(conn)
        upsert_coverage_slots(conn, "p1", [
            {"kind": "endpoint", "key": "endpoint:GET /", "description": ""},
            {"kind": "service", "key": "service:tcp/22", "description": ""},
        ])
        reason_run_id = _seed_reason_run(
            conn,
            "p1",
            worker="worker-a",
        )

        # 有 pending 槽位 → 历史在途 Reason 仍按冻结覆盖门结算。
        with pytest.raises(HTTPException) as exc:
            project_completion.complete_project_record(
                "p1",
                CompleteRequest(
                    from_=["fsrc"],
                    description="done",
                    worker="worker-a",
                    task_run_id=reason_run_id,
                ),
            )
        assert exc.value.status_code == 409
        assert "coverage" in str(exc.value.detail).lower()

        # 探完一个仍有 pending → 仍 409
        mark_slot_explored(conn, "p1", "endpoint:GET /", intent_id="i001", fact_id="f001")
        with pytest.raises(HTTPException) as exc2:
            project_completion.complete_project_record(
                "p1",
                CompleteRequest(
                    from_=["fsrc"],
                    description="done",
                    worker="worker-a",
                    task_run_id=reason_run_id,
                ),
            )
        assert exc2.value.status_code == 409

        # 全部 explored → complete 成功
        mark_slot_explored(conn, "p1", "service:tcp/22", intent_id="i002", fact_id="f001")
        ci = project_completion.complete_project_record(
            "p1",
            CompleteRequest(
                from_=["fsrc"],
                description="done",
                worker="worker-a",
                task_run_id=reason_run_id,
            ),
        )
        assert ci.to == "goal"
    finally:
        conn.close()


def test_non_web_scenario_without_coverage_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 Web 场景不开 coverage_gate 时，pending 槽位不改变场景级完成门。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(project_completion, "get_conn", fake_get_conn)
    try:
        _seed_default_project(conn)
        # 通用槽位表是加性的；非 Web 场景未启用覆盖门时不参与完成条件。
        upsert_coverage_slots(conn, "d1", [{"kind": "endpoint", "key": "endpoint:GET /", "description": ""}])
        assert count_open_coverage_slots(conn, "d1") == 1
        reason_run_id = _seed_reason_run(
            conn,
            "d1",
            worker="worker-a",
        )

        # 非 Web 场景无 coverage_gate → 历史在途 Reason 可直接结算。
        ci = project_completion.complete_project_record(
            "d1",
            CompleteRequest(
                from_=["fsrc"],
                description="done",
                worker="worker-a",
                task_run_id=reason_run_id,
            ),
        )
        assert ci.to == "goal"
    finally:
        conn.close()


def test_intent_slot_key_assign_and_conclude_marks_explored(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_intent 带 slot_key 关联 slot；conclude 回写 slot 为 explored。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_structured_project(conn)
        upsert_coverage_slots(conn, "p1", [{"kind": "endpoint", "key": "endpoint:POST /login", "description": "login"}])
        reason_run_id = _seed_reason_run(
            conn,
            "p1",
            worker="reason",
        )

        # 历史项目只允许真实在途 Reason 写回既有协议结果。
        intent = intents_router.create_intent(
            "p1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "test login",
                    "creator": "reason",
                    "task_run_id": reason_run_id,
                    "submission_index": 0,
                    "slot_key": "endpoint:POST /login",
                }
            ),
        )
        assert intent.slot_key == "endpoint:POST /login"
        slot = [s for s in list_coverage_slots(conn, "p1") if s.slot_key == "endpoint:POST /login"][0]
        assert slot.assigned_intent_id == intent.id
        assert slot.status == "pending"  # 创建时仍 pending，待 conclude 才 explored

        # conclude 该 intent → slot 置 explored 并绑定产出 fact
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, intent_id, task_type, worker_name, worker_type,
                status, started_at
            ) VALUES (
                'intent-explore-run', 'p1', ?, 'explore', 'worker-a', 'pi',
                'running', '2026-07-13T00:02:00Z'
            )
            """,
            (intent.id,),
        )
        intents_router.heartbeat(
            "p1",
            intent.id,
            HeartbeatRequest(worker="worker-a"),
        )
        resp = intents_router.conclude(
            "p1",
            intent.id,
            ConcludeRequest(
                worker="worker-a",
                description="login tested",
                evidence=[],
                task_run_id="intent-explore-run",
            ),
        )
        assert resp.fact.id is not None
        slot = [s for s in list_coverage_slots(conn, "p1") if s.slot_key == "endpoint:POST /login"][0]
        assert slot.status == "explored"
        assert slot.resulting_fact_id == resp.fact.id
        assert slot.assigned_intent_id == intent.id
        assert count_open_coverage_slots(conn, "p1") == 0
    finally:
        conn.close()


def test_discovered_slots_upsert_via_reason_payload() -> None:
    """Phase 2: reason discovered_slots 提取后登记为 pending（同 key 去重，不重置已 explored）。"""
    conn = make_conn()
    try:
        _seed_structured_project(conn)
        upsert_coverage_slots(conn, "p1", [{"kind": "endpoint", "key": "endpoint:GET /", "description": ""}])
        # 模拟 reason 输出的 discovered_slots（新入口点）
        discovered = [
            {"kind": "service", "key": "service:tcp/443", "description": "HTTPS"},
            {"kind": "service", "key": "service:tcp/443", "description": "dup"},  # 去重
        ]
        upsert_coverage_slots(conn, "p1", discovered)
        keys = {s.slot_key for s in list_coverage_slots(conn, "p1")}
        assert keys == {"endpoint:GET /", "service:tcp/443"}
    finally:
        conn.close()


def test_verifier_gate_requires_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 3: verifier 开启时 explored-unverified 也算未收敛；verified 后才放行。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(project_completion, "get_conn", fake_get_conn)
    try:
        _seed_structured_project(conn)
        # 用 core tier：verifier 开时 core explored-unverified 由 A 门直接拦截。
        upsert_coverage_slots(conn, "p1", [{"kind": "endpoint", "key": "endpoint:GET /", "description": ""}], tier="core")
        # 本用例显式打开 verifier，验证 explored 与 verified 的差异。
        conn.execute(
            "UPDATE scenarios SET features_json = ? WHERE key = 'coverage-unit'",
            (json.dumps({"complete_gate": True, "coverage_gate": True, "verifier": True}),),
        )
        # explored 但未 verified → 仍 409（core A 门 require_verified）
        mark_slot_explored(conn, "p1", "endpoint:GET /", intent_id="i001", fact_id="fsrc")
        assert count_open_coverage_slots(conn, "p1", require_verified=True, tier="core") == 1
        reason_run_id = _seed_reason_run(conn, "p1", worker="w")
        with pytest.raises(HTTPException) as exc:
            project_completion.complete_project_record(
                "p1",
                CompleteRequest(
                    from_=["fsrc"],
                    description="done",
                    worker="w",
                    task_run_id=reason_run_id,
                ),
            )
        assert exc.value.status_code == 409
        assert "core" in str(exc.value.detail).lower()

        # verified 后放行
        mark_slot_verified(conn, "p1", "endpoint:GET /", intent_id="i002", fact_id="fsrc", verifier_run_id="tr_v1")
        assert count_open_coverage_slots(conn, "p1", require_verified=True) == 0
        ci = project_completion.complete_project_record(
            "p1",
            CompleteRequest(
                from_=["fsrc"],
                description="done",
                worker="w",
                task_run_id=reason_run_id,
            ),
        )
        assert ci.to == "goal"
    finally:
        conn.close()


def test_conclude_upgrades_explored_slot_to_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 3: 同 slot 第二次 conclude（verifier 路径）将 slot 从 explored 升级为 verified。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_structured_project(conn)
        upsert_coverage_slots(conn, "p1", [{"kind": "endpoint", "key": "endpoint:POST /login", "description": ""}])
        reason_run_id = _seed_reason_run(conn, "p1", worker="r")
        # 第一次 conclude：pending → explored
        i1 = intents_router.create_intent(
            "p1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "t",
                    "creator": "r",
                    "task_run_id": reason_run_id,
                    "submission_index": 0,
                    "slot_key": "endpoint:POST /login",
                }
            ),
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, intent_id, task_type, worker_name, worker_type,
                status, started_at
            ) VALUES (
                'tr_first', 'p1', ?, 'explore', 'w', 'pi', 'running',
                '2026-07-13T00:01:00Z'
            )
            """,
            (i1.id,),
        )
        intents_router.heartbeat(
            "p1",
            i1.id,
            HeartbeatRequest(worker="w"),
        )
        intents_router.conclude(
            "p1",
            i1.id,
            ConcludeRequest(
                worker="w",
                description="explored login",
                evidence=[],
                task_run_id="tr_first",
            ),
        )
        slot = [s for s in list_coverage_slots(conn, "p1") if s.slot_key == "endpoint:POST /login"][0]
        assert slot.status == "explored"

        # 第二次 conclude（verify intent，同 slot_key）：explored → verified
        i2 = intents_router.create_intent(
            "p1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "verify",
                    "creator": "r",
                    "task_run_id": reason_run_id,
                    "submission_index": 1,
                    "slot_key": "endpoint:POST /login",
                }
            ),
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, intent_id, task_type, worker_name, worker_type, status, started_at
            ) VALUES ('tr_verify', 'p1', ?, 'explore', 'w', 'pi', 'running', ?)
            """,
            (i2.id, "2026-07-13T00:01:00Z"),
        )
        intents_router.heartbeat("p1", i2.id, HeartbeatRequest(worker="w"))
        resp = intents_router.conclude("p1", i2.id, ConcludeRequest(worker="w", description="reproduced", evidence=[], task_run_id="tr_verify"))
        slot = [s for s in list_coverage_slots(conn, "p1") if s.slot_key == "endpoint:POST /login"][0]
        assert slot.status == "verified"
        assert slot.verified_run_id == "tr_verify"
    finally:
        conn.close()


def _claim_release_reason(conn, project_id, worker="w"):
    """模拟 reason 一轮：claim → release，触发 idle streak 更新与 B/D 收尾。"""
    from cairn.server.services.project_reason import (
        claim_project_reason_record,
        release_project_reason_record,
    )
    from cairn.server.models import ReasonClaimRequest
    claim_project_reason_record(project_id, ReasonClaimRequest(worker=worker, trigger="test"))
    release_project_reason_record(project_id, HeartbeatRequest(worker=worker))


def test_tier_core_vs_extended_and_complete_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 分层：core pending 阻塞 complete；core 全 explored 后 extended 仍 pending 不卡 complete 候选（B 评估）。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(project_completion, "get_conn", fake_get_conn)
    try:
        _seed_structured_project(conn)
        # 1 core + 1 extended
        upsert_coverage_slots(conn, "p1", [{"kind": "endpoint", "key": "endpoint:core", "description": ""}], tier="core")
        upsert_coverage_slots(conn, "p1", [{"kind": "service", "key": "service:ext", "description": ""}], tier="extended")

        # core pending → 409（A 门）
        with pytest.raises(HTTPException) as exc:
            project_completion.complete_project_record("p1", CompleteRequest(from_=["fsrc"], description="done", worker="w"))
        assert exc.value.status_code == 409
        assert "core" in str(exc.value.detail).lower()

        # core explored 后：extended 仍 pending，B 门 idle=0<3 → 409 extended not saturated
        mark_slot_explored(conn, "p1", "endpoint:core", intent_id="i1", fact_id="fsrc")
        with pytest.raises(HTTPException) as exc2:
            project_completion.complete_project_record("p1", CompleteRequest(from_=["fsrc"], description="done", worker="w"))
        assert exc2.value.status_code == 409
        assert "extended" in str(exc2.value.detail).lower()

        # 验证 tier 标记
        core_slot = [s for s in list_coverage_slots(conn, "p1") if s.slot_key == "endpoint:core"][0]
        ext_slot = [s for s in list_coverage_slots(conn, "p1") if s.slot_key == "service:ext"][0]
        assert core_slot.tier == "core"
        assert ext_slot.tier == "extended"
    finally:
        conn.close()


def test_tier_not_downgraded_on_upsert() -> None:
    """A：已存在的 core slot 被 extended upsert 不降级（保留 core）。"""
    conn = make_conn()
    try:
        _seed_structured_project(conn)
        upsert_coverage_slots(conn, "p1", [{"kind": "endpoint", "key": "endpoint:x", "description": ""}], tier="core")
        # 同 key 用 extended 再 upsert，tier 应保留 core
        upsert_coverage_slots(conn, "p1", [{"kind": "endpoint", "key": "endpoint:x", "description": "rediscovered"}], tier="extended")
        slot = [s for s in list_coverage_slots(conn, "p1") if s.slot_key == "endpoint:x"][0]
        assert slot.tier == "core"
        assert slot.description == "rediscovered"
    finally:
        conn.close()


def test_b_diminishing_returns_skips_extended(monkeypatch: pytest.MonkeyPatch) -> None:
    """B：连续 K 轮无新 fact → extended pending 自动标 skipped → complete 放行。"""
    from cairn.server.services import project_reason as pr_module

    @contextmanager
    def fake_get_conn():
        yield conn

    conn = make_conn()
    monkeypatch.setattr(pr_module, "get_conn", fake_get_conn)
    monkeypatch.setattr(project_completion, "get_conn", fake_get_conn)
    try:
        _seed_structured_project(conn)
        upsert_coverage_slots(conn, "p1", [{"kind": "endpoint", "key": "endpoint:core", "description": ""}], tier="core")
        upsert_coverage_slots(conn, "p1", [{"kind": "service", "key": "service:ext", "description": ""}], tier="extended")
        mark_slot_explored(conn, "p1", "endpoint:core", intent_id="i1", fact_id="fsrc")
        # K=3：连续 3 轮 reason release 无新 fact
        for _ in range(4):
            _claim_release_reason(conn, "p1")
        # extended 应被标 skipped
        ext = [s for s in list_coverage_slots(conn, "p1") if s.slot_key == "service:ext"][0]
        assert ext.status == "skipped"
        # core 仍是 explored（不被 B 动）
        core = [s for s in list_coverage_slots(conn, "p1") if s.slot_key == "endpoint:core"][0]
        assert core.status == "explored"
        # complete 放行（core 全 resolved，extended 已 skip）
        ci = project_completion.complete_project_record("p1", CompleteRequest(from_=["fsrc"], description="done", worker="w"))
        assert ci.to == "goal"
    finally:
        conn.close()


def test_d_budget_forces_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """D：explore 预算到点 → 所有 pending（含 core）标 skipped → complete 放行。"""
    from cairn.server.services import project_reason as pr_module

    @contextmanager
    def fake_get_conn():
        yield conn

    conn = make_conn()
    monkeypatch.setattr(pr_module, "get_conn", fake_get_conn)
    monkeypatch.setattr(project_completion, "get_conn", fake_get_conn)
    try:
        _seed_structured_project(conn)
        # 设 explore_budget=1
        conn.execute("UPDATE projects SET explore_budget = 1 WHERE id = 'p1'")
        upsert_coverage_slots(conn, "p1", [{"kind": "endpoint", "key": "endpoint:core", "description": ""}], tier="core")
        # 插一条 succeeded explore task_run（满足 budget）
        conn.execute(
            "INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, started_at) VALUES ('tr_e1','p1','explore','w','pi','succeeded','2026-07-15T00:00:00Z')"
        )
        # reason release 触发 D：core pending 标 skipped
        _claim_release_reason(conn, "p1")
        core = [s for s in list_coverage_slots(conn, "p1") if s.slot_key == "endpoint:core"][0]
        assert core.status == "skipped"
        # complete 放行
        ci = project_completion.complete_project_record("p1", CompleteRequest(from_=["fsrc"], description="done", worker="w"))
        assert ci.to == "goal"
    finally:
        conn.close()


def test_default_copy_cells_dedupe_by_normalized_surface_and_family() -> None:
    """结构化 Cell 由规范化攻击面与测试族唯一确定，不受展示 key/描述影响。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn)
        common = {
            "kind": "endpoint",
            "description": "login",
            "surface_kind": "endpoint",
            "test_family": "authentication",
            "applicability_basis": "登录端点接受匿名凭据",
            "completion_criteria": "比较基线与错误凭据变体",
        }
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    **common,
                    "key": "model-supplied-a",
                    "surface_key": "get HTTPS://Example.COM:443/login?next=/",
                },
                {
                    **common,
                    "key": "model-supplied-b",
                    "surface_key": "GET https://example.com/login?next=/",
                    "description": "rediscovered",
                },
            ],
            tier="core",
        )
        slots = list_coverage_slots(conn, "dc1")
        assert len(slots) == 1
        assert slots[0].slot_key.startswith("cell:")
        assert slots[0].surface_key == "GET https://example.com/login?next=/"
        assert slots[0].test_family == "authentication"
        assert slots[0].description == "rediscovered"
        assert slots[0].tier == "core"

        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    **common,
                    "key": "another-display-key",
                    "surface_key": "GET https://example.com/login?next=/",
                    "test_family": "injection",
                }
            ],
        )
        assert len(list_coverage_slots(conn, "dc1")) == 2
    finally:
        conn.close()


def test_default_copy_surface_manifest_dedupes_surface_and_keeps_family_cells() -> None:
    """Manifest 按规范化 Surface 去重，Cell 仍按适用测试族分开。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn, surface_manifest_version=1)
        common = {
            "kind": "endpoint",
            "surface_kind": "endpoint",
            "surface_key": "GET HTTPS://Example.COM:443/account",
            "applicability_basis": "账户端点暴露认证与对象访问边界",
            "completion_criteria": "完成基线与至少一个变体",
        }
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {**common, "test_family": "authentication"},
                {
                    **common,
                    "surface_key": "GET https://example.com/account",
                    "test_family": "authorization",
                },
            ],
            tier="extended",
        )

        entries = conn.execute(
            "SELECT surface_id, surface_kind, surface_key FROM surface_manifest_entries "
            "WHERE project_id = ? ORDER BY entry_id",
            ("dc1",),
        ).fetchall()
        assert len({row["surface_id"] for row in entries}) == 1
        assert {row["surface_kind"] for row in entries} == {"endpoint"}
        assert {row["surface_key"] for row in entries} == {
            "GET https://example.com/account"
        }
        assert len(list_coverage_slots(conn, "dc1")) == 2
    finally:
        conn.close()


def test_default_copy_surface_manifest_is_frozen_and_promotes_core_tier() -> None:
    """旧快照不写 Manifest；新版重复登记只提升 Cell tier，不重置状态。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn, "legacy")
        _seed_default_copy_project(conn, "manifest", surface_manifest_version=1)
        cell = {
            "kind": "endpoint",
            "surface_kind": "endpoint",
            "surface_key": "POST https://example.com/login",
            "test_family": "authentication",
            "applicability_basis": "登录端点处理凭据",
            "completion_criteria": "比较有效与无效凭据",
        }
        upsert_coverage_slots(conn, "legacy", [cell], tier="core")
        upsert_coverage_slots(conn, "manifest", [cell], tier="extended")
        manifest_slot = list_coverage_slots(conn, "manifest")[0]
        conn.execute(
            "UPDATE coverage_slots SET status = 'explored', result = 'tested_no_finding' "
            "WHERE project_id = ? AND slot_key = ?",
            ("manifest", manifest_slot.slot_key),
        )

        upsert_coverage_slots(conn, "manifest", [cell], tier="core")

        assert conn.execute(
            "SELECT COUNT(*) AS c FROM surface_manifest_entries WHERE project_id = 'legacy'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM surface_manifest_entries WHERE project_id = 'manifest'"
        ).fetchone()["c"] == 1
        promoted = list_coverage_slots(conn, "manifest")[0]
        assert promoted.tier == "core"
        assert promoted.status == "explored"
        assert promoted.result == "tested_no_finding"
    finally:
        conn.close()


def test_surface_manifest_identity_and_digest_are_deterministic() -> None:
    """Surface kind 参与身份；登记顺序与项目边界不改变同一 Manifest 摘要。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn, "ordered", surface_manifest_version=1)
        _seed_default_copy_project(conn, "reversed", surface_manifest_version=1)
        cells = [
            {
                "kind": "endpoint",
                "surface_kind": "endpoint",
                "surface_key": "GET https://example.com/account",
                "test_family": "authorization",
                "applicability_basis": "对象端点暴露访问控制边界",
                "completion_criteria": "比较不同角色访问结果",
            },
            {
                "kind": "host",
                "surface_kind": "host",
                "surface_key": "API.EXAMPLE.COM.",
                "test_family": "authentication",
                "applicability_basis": "认证服务由该主机承载",
                "completion_criteria": "确认认证入口与拒绝行为",
            },
        ]
        upsert_coverage_slots(conn, "ordered", cells, tier="core")
        upsert_coverage_slots(conn, "reversed", list(reversed(cells)), tier="core")

        assert surface_manifest_digest(conn, "ordered") == surface_manifest_digest(
            conn, "reversed"
        )
        assert surface_manifest_id("endpoint", "GET https://example.com/account") != (
            surface_manifest_id("host", "GET https://example.com/account")
        )
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM surface_manifest_entries WHERE project_id = ?",
            ("ordered",),
        ).fetchone()["c"] == 2
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM surface_manifest_entries WHERE project_id = ?",
            ("reversed",),
        ).fetchone()["c"] == 2
    finally:
        conn.close()


def test_surface_key_normalization_preserves_ipv6_authority() -> None:
    assert normalize_surface_key(
        "endpoint",
        "GET HTTPS://[2001:DB8::1]:443/a?x=1#ignored",
    ) == "GET https://[2001:db8::1]/a?x=1"
    assert normalize_surface_key(
        "endpoint",
        "GET https://[2001:db8::1]:8443/a",
    ) == "GET https://[2001:db8::1]:8443/a"


def test_surface_key_normalization_rejects_invalid_endpoint_port() -> None:
    with pytest.raises(HTTPException) as exc:
        normalize_surface_key(
            "endpoint",
            "GET https://example.com:99999/a",
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == "surface_key contains an invalid endpoint URL"


def test_surface_manifest_reader_preserves_observations_and_project_isolation() -> None:
    """读取模型解码 traits，并保留同一 Surface 的追加观察与项目隔离。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn, "manifest", surface_manifest_version=1)
        _seed_default_copy_project(conn, "other", surface_manifest_version=1)
        cell = {
            "kind": "endpoint",
            "surface_kind": "endpoint",
            "surface_key": "GET https://example.com/account",
            "test_family": "authorization",
            "applicability_basis": "对象端点暴露访问控制边界",
            "completion_criteria": "比较不同角色访问结果",
        }
        upsert_coverage_slots(conn, "manifest", [cell], tier="extended")
        upsert_coverage_slots(
            conn,
            "manifest",
            [{**cell, "description": "第二次确认的账户入口"}],
            tier="extended",
        )
        upsert_coverage_slots(conn, "other", [cell], tier="extended")

        entries = list_surface_manifest_entries(conn, "manifest")
        assert len(entries) == 2
        assert len({entry.surface_id for entry in entries}) == 1
        assert entries[0].traits["test_family"] == "authorization"
        assert entries[0].evidence == []
        assert all(entry.project_id == "manifest" for entry in entries)
        assert len(list_surface_manifest_entries(conn, "other")) == 1
    finally:
        conn.close()


def test_surface_manifest_registration_rolls_back_with_cell_registration() -> None:
    """Manifest 与 Coverage Cell 共享调用方事务，不留下半写入 Surface。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn, surface_manifest_version=1)
        conn.commit()
        conn.execute("BEGIN")
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "endpoint",
                    "surface_kind": "endpoint",
                    "surface_key": "POST https://example.com/login",
                    "test_family": "authentication",
                    "applicability_basis": "登录端点处理凭据",
                    "completion_criteria": "比较有效与无效凭据",
                }
            ],
            tier="core",
        )
        conn.rollback()

        assert conn.execute(
            "SELECT COUNT(*) AS c FROM surface_manifest_entries WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM coverage_slots WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_default_copy_conclude_registers_surfaces_with_fact_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bootstrap Surface 与 Fact、TaskRun、Evidence、core Cell 在一次 conclude 中落地。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn, surface_manifest_version=1)
        intent = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "bootstrap target",
                    "creator": "bootstrap",
                }
            ),
        )
        evidence = _seed_current_run_artifact(
            conn,
            "dc1",
            intent_id=intent.id,
            task_run_id="tr_bootstrap",
        )
        conn.execute(
            "UPDATE task_runs SET task_type = 'bootstrap' WHERE id = 'tr_bootstrap' AND project_id = 'dc1'"
        )
        conn.commit()

        response = intents_router.conclude(
            "dc1",
            intent.id,
            ConcludeRequest(
                worker="w",
                description="target mapped",
                task_run_id="tr_bootstrap",
                evidence=[evidence],
                discovered_surfaces=[
                    {
                        "surface_kind": "endpoint",
                        "surface_key": "POST https://example.com/login",
                        "description": "credential entrypoint",
                        "traits": {"auth": "password"},
                        "applicable_tests": [
                            {
                                "test_family": "authentication",
                                "applicability_basis": "登录端点处理凭据",
                                "completion_criteria": "比较有效与无效凭据",
                            }
                        ],
                    }
                ],
            ),
        )

        entries = list_surface_manifest_entries(conn, "dc1")
        assert len(entries) == 1
        assert entries[0].source_intent_id == intent.id
        assert entries[0].source_fact_id == response.fact.id
        assert entries[0].source_task_run_id == "tr_bootstrap"
        assert entries[0].evidence == [evidence]
        cells = list_coverage_slots(conn, "dc1")
        assert len(cells) == 1
        assert cells[0].tier == "core"
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM fact_evidence WHERE project_id = 'dc1' AND fact_id = ?",
            (response.fact.id,),
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_default_copy_conclude_binds_explicit_surface_evidence_to_source_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn, surface_manifest_version=1)
        intent = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "bootstrap target",
                    "creator": "bootstrap",
                }
            ),
        )
        evidence = _seed_current_run_artifact(
            conn,
            "dc1",
            intent_id=intent.id,
            task_run_id="tr_surface",
            artifact_id="a_surface",
        )
        conn.execute(
            "UPDATE task_runs SET task_type = 'bootstrap' WHERE id = 'tr_surface' AND project_id = 'dc1'"
        )
        conn.commit()

        response = intents_router.conclude(
            "dc1",
            intent.id,
            ConcludeRequest(
                worker="w",
                description="target mapped",
                task_run_id="tr_surface",
                evidence=[],
                discovered_surfaces=[
                    {
                        "surface_kind": "endpoint",
                        "surface_key": "GET https://example.com/account",
                        "evidence_refs": [evidence.model_dump(exclude_none=True)],
                    }
                ],
            ),
        )

        entry = list_surface_manifest_entries(conn, "dc1")[0]
        assert entry.source_fact_id == response.fact.id
        assert entry.evidence == [evidence]
        bound = conn.execute(
            "SELECT artifact_id FROM fact_evidence WHERE project_id = 'dc1' AND fact_id = ?",
            (response.fact.id,),
        ).fetchall()
        assert [row["artifact_id"] for row in bound] == ["a_surface"]
    finally:
        conn.close()


def test_conclude_rejects_task_run_from_different_intent_before_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn, outcome_contract_version=1)
        first = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(**{"from": ["fsrc"], "description": "first", "creator": "reason"}),
        )
        second = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(**{"from": ["fsrc"], "description": "second", "creator": "reason"}),
        )
        _seed_current_run_artifact(
            conn,
            "dc1",
            intent_id=first.id,
            task_run_id="tr_other",
        )
        conn.commit()

        with pytest.raises(HTTPException) as exc:
            intents_router.conclude(
                "dc1",
                second.id,
                ConcludeRequest(
                    worker="w",
                    description="wrong provenance",
                    task_run_id="tr_other",
                    evidence=[],
                ),
            )

        assert exc.value.status_code == 422
        assert exc.value.detail == "task_run_id does not belong to the concluded intent"
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM facts WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT to_fact_id FROM intents WHERE project_id = 'dc1' AND id = ?",
            (second.id,),
        ).fetchone()["to_fact_id"] is None
    finally:
        conn.close()


def test_default_copy_conclude_rolls_back_partial_surface_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后续 Surface Evidence 无效时回滚 Fact、Manifest、Cell 与 Intent 收敛。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn, surface_manifest_version=1)
        intent = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "bootstrap target",
                    "creator": "bootstrap",
                }
            ),
        )
        _seed_current_run_artifact(conn, "dc1", intent_id=intent.id, task_run_id="tr_bootstrap")
        conn.execute(
            "UPDATE task_runs SET task_type = 'bootstrap' WHERE project_id = 'dc1' AND id = 'tr_bootstrap'"
        )
        conn.commit()

        surfaces = [
            {
                "surface_kind": "endpoint",
                "surface_key": "GET https://example.com/",
                "applicable_tests": [
                    {
                        "test_family": "authentication",
                        "applicability_basis": "入口需要认证确认",
                        "completion_criteria": "确认匿名与认证响应",
                    }
                ],
            },
            {
                "surface_kind": "endpoint",
                "surface_key": "POST https://example.com/login",
                "evidence_refs": [{"artifact_id": "a999"}],
            },
        ]
        with pytest.raises(HTTPException) as invalid:
            intents_router.conclude(
                "dc1",
                intent.id,
                ConcludeRequest(
                    worker="w",
                    description="target mapped",
                    task_run_id="tr_bootstrap",
                    discovered_surfaces=surfaces,
                ),
            )
        assert invalid.value.status_code == 404
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM facts WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM surface_manifest_entries WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM coverage_slots WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT concluded_at FROM intents WHERE id = ? AND project_id = 'dc1'",
            (intent.id,),
        ).fetchone()["concluded_at"] is None
    finally:
        conn.close()


def test_default_copy_conclude_validates_and_applies_structured_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无效结构化结果在 Fact 前拒绝；有效无发现结果原子推进 Cell。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn)
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "endpoint",
                    "key": "ignored",
                    "description": "login",
                    "surface_key": "POST https://example.com/login",
                    "surface_kind": "endpoint",
                    "test_family": "authentication",
                    "applicability_basis": "登录流程需要认证测试",
                    "completion_criteria": "完成有效与无效凭据对照",
                }
            ],
            tier="core",
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        intent = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "test login",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                }
            ),
        )
        assert list_coverage_slots(conn, "dc1")[0].status == "exploring"

        evidence = _seed_current_run_artifact(conn, "dc1", intent_id=intent.id)
        with pytest.raises(HTTPException) as missing:
            intents_router.conclude(
                "dc1", intent.id, ConcludeRequest(worker="w", description="missing", task_run_id="tr_current")
            )
        assert missing.value.status_code == 422
        assert conn.execute("SELECT COUNT(*) AS c FROM facts WHERE project_id='dc1'").fetchone()["c"] == 1

        incomplete = CoverageResultInput(
            slot_key=slot.slot_key,
            surface_key=slot.surface_key,
            test_family=slot.test_family,
            outcome="tested_no_finding",
            baseline="valid request",
        )
        with pytest.raises(HTTPException) as invalid:
            intents_router.conclude(
                "dc1",
                intent.id,
                ConcludeRequest(
                    worker="w", description="incomplete", task_run_id="tr_current", coverage_result=incomplete
                ),
            )
        assert invalid.value.status_code == 422
        assert conn.execute("SELECT COUNT(*) AS c FROM facts WHERE project_id='dc1'").fetchone()["c"] == 1

        result = incomplete.model_copy(update={"variants": ["invalid password"], "evidence_refs": [evidence]})
        response = intents_router.conclude(
            "dc1",
            intent.id,
            ConcludeRequest(
                worker="w", description="tested", task_run_id="tr_current",
                evidence=[evidence], coverage_result=result, assessment_record=_experiment(result, evidence)
            ),
        )
        updated = list_coverage_slots(conn, "dc1")[0]
        assert response.fact.description == "tested"
        assert updated.status == "explored"
        assert updated.result == "tested_no_finding"
        assert updated.resulting_fact_id == response.fact.id
    finally:
        conn.close()


def test_default_copy_v2_no_finding_requires_current_run_artifact_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2 无发现结论必须绑定当前运行生成的可审阅 Artifact。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn, outcome_contract_version=2)
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "endpoint",
                    "surface_key": "POST https://example.com/login",
                    "surface_kind": "endpoint",
                    "test_family": "authentication",
                    "applicability_basis": "登录接口处理凭据",
                    "completion_criteria": "比较有效和无效凭据",
                }
            ],
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        intent = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "test login",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                }
            ),
        )
        result = CoverageResultInput(
            slot_key=slot.slot_key,
            surface_key=slot.surface_key,
            test_family=slot.test_family,
            outcome="tested_no_finding",
            baseline="valid login request returned 200",
            variants=["invalid password returned 401"],
        )

        evidence = _seed_current_run_artifact(conn, "dc1", intent_id=intent.id)
        with pytest.raises(HTTPException) as missing_evidence:
            intents_router.conclude(
                "dc1",
                intent.id,
                ConcludeRequest(worker="w", description="tested", task_run_id="tr_current", coverage_result=result),
            )
        assert missing_evidence.value.detail == "coverage_evidence_current_run_required"

        qualified = result.model_copy(update={"evidence_refs": [evidence]})
        response = intents_router.conclude(
            "dc1",
            intent.id,
            ConcludeRequest(
                worker="w",
                description="tested",
                task_run_id="tr_current",
                evidence=[evidence],
                coverage_result=qualified,
                assessment_record=_experiment(qualified, evidence),
            ),
        )
        assert response.fact.description == "tested"
        assert list_coverage_slots(conn, "dc1")[0].result == "tested_no_finding"
    finally:
        conn.close()


@pytest.mark.parametrize(
    "outcome",
    ("permanently_unreachable", "out_of_scope", "not_applicable"),
)
def test_default_copy_v2_rejects_unverifiable_terminal_outcome(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """v2 不允许任意 Artifact 与自由文本把 Cell 管理性跳过。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn, outcome_contract_version=2)
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "endpoint",
                    "surface_key": "GET https://example.com/account",
                    "surface_kind": "endpoint",
                    "test_family": "authorization",
                    "applicability_basis": "账户端点包含对象访问边界",
                    "completion_criteria": "比较本人和他人对象访问",
                }
            ],
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        intent = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "test account access",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                }
            ),
        )
        evidence = _seed_current_run_artifact(conn, "dc1", intent_id=intent.id)
        result = CoverageResultInput(
            slot_key=slot.slot_key,
            surface_key=slot.surface_key,
            test_family=slot.test_family,
            outcome=outcome,
            evidence_refs=[evidence],
            limitation_reason="worker supplied terminal claim",
            limitation_details="arbitrary current-run output was attached",
            attempted_strategies=["baseline request", "alternate request"],
        )

        with pytest.raises(HTTPException) as rejected:
            intents_router.conclude(
                "dc1",
                intent.id,
                ConcludeRequest(
                    worker="w",
                    description="unsupported terminal claim",
                    task_run_id="tr_current",
                    evidence=[evidence],
                    coverage_result=result,
                ),
            )

        assert rejected.value.status_code == 422
        assert (
            rejected.value.detail
            == "coverage_terminal_outcome_not_authoritatively_verifiable"
        )
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM facts WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 1
        current = list_coverage_slots(conn, "dc1")[0]
        assert current.status == "exploring"
        assert current.result == ""
    finally:
        conn.close()


def test_default_copy_v2_finding_requires_complete_reproduction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2 finding 不能用自由文本或单边 HTTP 内容替代完整复现证据。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn, outcome_contract_version=2)
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "endpoint",
                    "surface_key": "GET https://example.com/api/orders/123",
                    "surface_kind": "endpoint",
                    "test_family": "authorization",
                    "applicability_basis": "对象接口受角色边界约束",
                    "completion_criteria": "比较本人和他人对象访问",
                }
            ],
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        intent = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "test object access",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                }
            ),
        )
        evidence = _seed_current_run_artifact(conn, "dc1", intent_id=intent.id)
        result = CoverageResultInput(
            slot_key=slot.slot_key,
            surface_key=slot.surface_key,
            test_family=slot.test_family,
            outcome="finding",
            baseline="own order returned 200",
            variants=["other user's order returned 200"],
            evidence_refs=[evidence],
        )

        with pytest.raises(HTTPException) as incomplete:
            intents_router.conclude(
                "dc1",
                intent.id,
                ConcludeRequest(
                    worker="w",
                    description="finding",
                    task_run_id="tr_current",
                    evidence=[evidence],
                    coverage_result=result,
                    assessment_record=_experiment(result, evidence),
                    vulnerability=_structured_vulnerability(
                        repro_request="GET /api/orders/123"
                    ),
                ),
            )
        assert incomplete.value.detail == "finding_complete_reproduction_required"

        response = intents_router.conclude(
            "dc1",
            intent.id,
            ConcludeRequest(
                worker="w",
                description="finding",
                task_run_id="tr_current",
                evidence=[evidence],
                coverage_result=result,
                assessment_record=_experiment(result, evidence),
                vulnerability=_structured_vulnerability(
                    repro_request="GET /api/orders/123",
                    repro_response="HTTP/1.1 200 OK\n\n{\"owner\":\"other\"}",
                ),
            ),
        )
        assert response.fact.description == "finding"
    finally:
        conn.close()


@pytest.mark.parametrize("outcome", ("tested_no_finding", "temporarily_blocked"))
def test_default_copy_rejects_vulnerability_for_non_finding_outcome_before_writes(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """非 finding 不能携带漏洞，且拒绝发生在任何持久化写入之前。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn)
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "endpoint",
                    "surface_key": "GET https://example.com/api/orders/123",
                    "surface_kind": "endpoint",
                    "test_family": "authorization",
                    "applicability_basis": "对象接口受授权边界约束",
                    "completion_criteria": "比较本人和他人对象访问",
                }
            ],
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        intent = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "test object access",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                }
            ),
        )
        values: dict[str, object] = {
            "slot_key": slot.slot_key,
            "surface_key": slot.surface_key,
            "test_family": slot.test_family,
            "outcome": outcome,
        }
        if outcome == "tested_no_finding":
            values.update(
                baseline="own order returned 200",
                variants=["other order returned 404"],
            )
        else:
            values.update(
                blocker_code="missing_second_principal",
                strategy_key="auth.owner-vs-other-v1",
                limitation_reason="a second principal is unavailable",
                limitation_details="only the owner session is available",
                attempted_strategies=["obtain another approved account"],
            )
        result = CoverageResultInput(**values)
        _seed_current_run_artifact(conn, "dc1", intent_id=intent.id)

        with pytest.raises(HTTPException) as rejected:
            intents_router.conclude(
                "dc1",
                intent.id,
                ConcludeRequest(
                    worker="w",
                    description="invalid mixed outcome",
                    task_run_id="tr_current",
                    coverage_result=result,
                    vulnerability=_structured_vulnerability(
                        repro_request="GET /api/orders/123",
                        repro_response="HTTP/1.1 200 OK",
                    ),
                ),
            )

        assert rejected.value.status_code == 422
        assert (
            rejected.value.detail
            == "non_finding_outcome_must_not_include_vulnerability"
        )
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM facts WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM vulnerabilities WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 0
        intent_row = conn.execute(
            "SELECT concluded_at FROM intents WHERE project_id = 'dc1' AND id = ?",
            (intent.id,),
        ).fetchone()
        assert intent_row["concluded_at"] is None
        current = list_coverage_slots(conn, "dc1")[0]
        assert current.status == "exploring"
        assert current.result == ""
    finally:
        conn.close()


def test_default_copy_v2_rejects_runtime_failure_as_target_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行时基础设施错误不能被伪装成目标自身的 temporary blocker。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn, outcome_contract_version=2)
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "service",
                    "surface_key": "TCP/443 example.com",
                    "surface_kind": "service",
                    "test_family": "configuration",
                    "applicability_basis": "公开 TLS 服务",
                    "completion_criteria": "完成配置检查",
                }
            ],
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        intent = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "inspect TLS",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                }
            ),
        )
        blocked = CoverageResultInput(
            slot_key=slot.slot_key,
            surface_key=slot.surface_key,
            test_family=slot.test_family,
            outcome="temporarily_blocked",
            blocker_code="heartbeat_lost",
            limitation_reason="heartbeat_lost",
            limitation_details="dispatcher lease expired",
            attempted_strategies=["retry worker session"],
        )
        _seed_current_run_artifact(conn, "dc1", intent_id=intent.id)
        with pytest.raises(HTTPException) as invalid:
            intents_router.conclude(
                "dc1",
                intent.id,
                ConcludeRequest(worker="w", description="blocked", task_run_id="tr_current", coverage_result=blocked),
            )
        assert invalid.value.detail == "runtime_failure_is_not_target_blocker"
    finally:
        conn.close()


def test_default_copy_temporary_block_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """临时受阻只结束当前 Intent，Cell 回到 pending 并可绑定下一次尝试。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn)
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "service",
                    "key": "ignored",
                    "surface_key": "TCP/443 example.com",
                    "surface_kind": "service",
                    "test_family": "configuration",
                    "applicability_basis": "公开 TLS 服务",
                    "completion_criteria": "完成协议与配置基线检查",
                }
            ],
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        first = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "inspect TLS",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                }
            ),
        )
        evidence = _seed_current_run_artifact(conn, "dc1", intent_id=first.id)
        conn.commit()
        blocked = CoverageResultInput(
            slot_key=slot.slot_key,
            surface_key=slot.surface_key,
            test_family=slot.test_family,
            outcome="temporarily_blocked",
            blocker_code="target_connection_reset",
            limitation_reason="target_connection_reset",
            limitation_details="the approved target reset both protocol probes",
            attempted_strategies=["TLS baseline", "alternate supported TLS version"],
        )
        intents_router.conclude(
            "dc1",
            first.id,
            ConcludeRequest(
                worker="w", description="blocked", task_run_id="tr_current",
                evidence=[evidence], coverage_result=blocked,
                assessment_record=_experiment(blocked, evidence),
            ),
        )
        pending = list_coverage_slots(conn, "dc1")[0]
        assert pending.status == "pending"
        assert pending.result == "temporarily_blocked"
        assert pending.assigned_intent_id is None

        second = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "retry TLS",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                }
            ),
        )
        retrying = list_coverage_slots(conn, "dc1")[0]
        assert retrying.status == "exploring"
        assert retrying.assigned_intent_id == second.id
    finally:
        conn.close()


def test_default_copy_release_keeps_cell_bound_for_same_intent_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行故障只释放 worker；同一 Intent 重试仍持有原 Cell。"""
    conn = make_conn()

    @contextmanager
    def transactional_get_conn():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr(intents_router, "get_conn", transactional_get_conn)
    try:
        _seed_default_copy_project(
            conn,
            outcome_contract_version=2,
            blocker_cooldown_version=1,
        )
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "endpoint",
                    "surface_key": "POST https://example.com/login",
                    "surface_kind": "endpoint",
                    "test_family": "authentication",
                    "applicability_basis": "登录接口处理凭据",
                    "completion_criteria": "比较有效与无效凭据",
                }
            ],
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        first = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "test login",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                    "strategy_key": "auth.baseline-v1",
                }
            ),
        )
        intents_router.heartbeat("dc1", first.id, HeartbeatRequest(worker="w"))

        intents_router.release("dc1", first.id, HeartbeatRequest(worker="w"))

        released = list_coverage_slots(conn, "dc1")[0]
        assert released.status == "exploring"
        assert released.assigned_intent_id == first.id
        intent_row = conn.execute(
            "SELECT worker, concluded_at FROM intents WHERE project_id = ? AND id = ?",
            ("dc1", first.id),
        ).fetchone()
        assert intent_row["worker"] is None
        assert intent_row["concluded_at"] is None

        reclaimed = intents_router.heartbeat(
            "dc1", first.id, HeartbeatRequest(worker="w2")
        )
        assert reclaimed.worker == "w2"
        retrying = list_coverage_slots(conn, "dc1")[0]
        assert retrying.status == "exploring"
        assert retrying.assigned_intent_id == first.id
    finally:
        conn.close()


def test_default_copy_abandon_releases_cell_for_rescheduling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主动 abandon 未认领 Intent 时同步释放 Cell，但不得重开终态 Cell。"""
    from cairn.dispatcher.prompting import format_structured_coverage_slots
    from cairn.server.services.coverage_slots import reset_slot_for_abandoned_intent

    conn = make_conn()

    @contextmanager
    def transactional_get_conn():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr(intents_router, "get_conn", transactional_get_conn)
    try:
        _seed_default_copy_project(conn)
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "endpoint",
                    "surface_key": "GET https://example.com/admin",
                    "surface_kind": "endpoint",
                    "test_family": "authorization",
                    "applicability_basis": "管理端点需要权限边界验证",
                    "completion_criteria": "比较普通用户与管理员访问结果",
                }
            ],
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        first = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "test admin authorization",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                    "strategy_key": "auth.baseline-v1",
                }
            ),
        )
        exploring = list_coverage_slots(conn, "dc1")[0]
        assert exploring.status == "exploring"
        assert exploring.assigned_intent_id == first.id

        abandoned = projects_service.abandon_intent_record(
            conn, "dc1", first.id, "reason"
        )

        assert abandoned.concluded_at is not None
        assert abandoned.conclusion == "abandoned"
        released = list_coverage_slots(conn, "dc1")[0]
        assert released.status == "pending"
        assert released.assigned_intent_id is None
        prompt_coverage = json.loads(
            format_structured_coverage_slots([released.model_dump()])
        )
        assert [cell["slot_key"] for cell in prompt_coverage["eligible_cells"]] == [
            slot.slot_key
        ]

        replacement = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "retry admin authorization",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                    "strategy_key": "auth.alternate-v1",
                }
            ),
        )
        reassigned = list_coverage_slots(conn, "dc1")[0]
        assert reassigned.assigned_intent_id == replacement.id

        conn.execute(
            """
            UPDATE coverage_slots
            SET status = 'explored'
            WHERE project_id = 'dc1' AND slot_key = ?
            """,
            (slot.slot_key,),
        )
        reset_slot_for_abandoned_intent(
            conn,
            "dc1",
            replacement.id,
            updated_at="2026-07-13T01:00:00Z",
        )
        terminal = list_coverage_slots(conn, "dc1")[0]
        assert terminal.status == "explored"
        assert terminal.assigned_intent_id == replacement.id
    finally:
        conn.close()


def test_default_copy_blocker_cooldown_requires_and_changes_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新协议拒绝无策略和 cooldown 内同策略，但允许实质不同的策略。"""
    conn = make_conn()

    @contextmanager
    def transactional_get_conn():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr(intents_router, "get_conn", transactional_get_conn)
    try:
        _seed_default_copy_project(
            conn,
            outcome_contract_version=2,
            blocker_cooldown_version=1,
        )
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "endpoint",
                    "surface_key": "POST https://example.com/login",
                    "surface_kind": "endpoint",
                    "test_family": "authentication",
                    "applicability_basis": "登录接口处理凭据",
                    "completion_criteria": "比较有效与无效凭据",
                }
            ],
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        conn.commit()

        with pytest.raises(HTTPException) as missing_strategy:
            intents_router.create_intent(
                "dc1",
                CreateIntentRequest(
                    **{
                        "from": ["fsrc"],
                        "description": "test login",
                        "creator": "reason",
                        "slot_key": slot.slot_key,
                    }
                ),
            )
        assert missing_strategy.value.detail == "coverage_cell_strategy_required"
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 0

        first = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "test login without employee session",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                    "strategy_key": "auth.anonymous-baseline-v1",
                }
            ),
        )
        evidence = _seed_current_run_artifact(conn, "dc1", intent_id=first.id)
        conn.commit()
        blocked = CoverageResultInput(
            slot_key=slot.slot_key,
            surface_key=slot.surface_key,
            test_family=slot.test_family,
            outcome="temporarily_blocked",
            blocker_code="missing_employee_session",
            strategy_key="auth.anonymous-baseline-v1",
            limitation_reason="Employee session unavailable",
            limitation_details="Only an anonymous session is available for the role comparison",
            attempted_strategies=["anonymous request", "public login discovery"],
        )
        with pytest.raises(HTTPException) as mismatched_strategy:
            intents_router.conclude(
                "dc1",
                first.id,
                ConcludeRequest(
                    worker="w",
                    description="blocked",
                    task_run_id="tr_current",
                    coverage_result=blocked.model_copy(
                        update={"strategy_key": "auth.guessed-v2"}
                    ),
                ),
            )
        assert mismatched_strategy.value.detail == "blocker_strategy_key_mismatch"

        intents_router.conclude(
            "dc1",
            first.id,
            ConcludeRequest(
                worker="w", description="blocked", task_run_id="tr_current",
                evidence=[evidence], coverage_result=blocked,
                assessment_record=_experiment(blocked, evidence),
            ),
        )
        pending = list_coverage_slots(conn, "dc1")[0]
        assert pending.status == "pending"
        assert pending.blocker_fingerprint is not None
        assert pending.blocker_strategy_key == "auth.anonymous-baseline-v1"
        assert pending.blocker_cooldown_until is not None

        with pytest.raises(HTTPException) as repeated:
            intents_router.create_intent(
                "dc1",
                CreateIntentRequest(
                    **{
                        "from": ["fsrc"],
                        "description": "repeat anonymous baseline",
                        "creator": "reason",
                        "slot_key": slot.slot_key,
                        "strategy_key": "auth.anonymous-baseline-v1",
                    }
                ),
            )
        assert repeated.value.detail == "coverage_cell_blocker_cooldown_active"
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 1

        changed = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "obtain and compare employee session",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                    "strategy_key": "auth.employee-session-v1",
                }
            ),
        )
        retrying = list_coverage_slots(conn, "dc1")[0]
        assert retrying.status == "exploring"
        assert retrying.assigned_intent_id == changed.id
    finally:
        conn.close()


def test_default_copy_blocker_cooldown_expiry_and_terminal_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cooldown 到期后同策略可重试，合格终态会清除旧 blocker 投影。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(
            conn,
            outcome_contract_version=2,
            blocker_cooldown_version=1,
        )
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "endpoint",
                    "surface_key": "POST https://example.com/login",
                    "surface_kind": "endpoint",
                    "test_family": "authentication",
                    "applicability_basis": "登录接口处理凭据",
                    "completion_criteria": "比较有效与无效凭据",
                }
            ],
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        first = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "test login",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                    "strategy_key": "auth.employee-session-v1",
                }
            ),
        )
        evidence = _seed_current_run_artifact(conn, "dc1", intent_id=first.id)
        conn.commit()
        blocked = CoverageResultInput(
            slot_key=slot.slot_key,
            surface_key=slot.surface_key,
            test_family=slot.test_family,
            outcome="temporarily_blocked",
            blocker_code="session_expired",
            strategy_key="auth.employee-session-v1",
            limitation_reason="Employee session expired",
            limitation_details="The target rejected the available employee session",
            attempted_strategies=["refresh existing session"],
        )
        intents_router.conclude(
            "dc1",
            first.id,
            ConcludeRequest(
                worker="w", description="blocked", task_run_id="tr_current",
                evidence=[evidence], coverage_result=blocked,
                assessment_record=_experiment(blocked, evidence),
            ),
        )
        conn.execute(
            "UPDATE coverage_slots SET blocker_cooldown_until = ? WHERE project_id = ? AND slot_key = ?",
            ("2000-01-01T00:00:00Z", "dc1", slot.slot_key),
        )

        second = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "retry refreshed employee session",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                    "strategy_key": "auth.employee-session-v1",
                }
            ),
        )
        evidence = _seed_current_run_artifact(
            conn,
            "dc1",
            intent_id=second.id,
            task_run_id="tr_terminal",
            artifact_id="a_terminal",
        )
        result = CoverageResultInput(
            slot_key=slot.slot_key,
            surface_key=slot.surface_key,
            test_family=slot.test_family,
            outcome="tested_no_finding",
            baseline="valid employee login returned 200",
            variants=["invalid password returned 401"],
            evidence_refs=[evidence],
        )
        intents_router.conclude(
            "dc1",
            second.id,
            ConcludeRequest(
                worker="w",
                description="tested",
                task_run_id="tr_terminal",
                evidence=[evidence],
                coverage_result=result,
                assessment_record=_experiment(result, evidence),
            ),
        )
        terminal = list_coverage_slots(conn, "dc1")[0]
        assert terminal.status == "explored"
        assert terminal.blocker_fingerprint is None
        assert terminal.blocker_strategy_key == ""
        assert terminal.blocker_cooldown_until is None
    finally:
        conn.close()


def test_minimum_coverage_derives_manifest_bound_required_and_optional_cells() -> None:
    conn = make_conn()
    try:
        _seed_default_copy_project(
            conn,
            surface_manifest_version=1,
            minimum_coverage_version=1,
        )

        surface_ids = register_discovered_surfaces(
            conn,
            "dc1",
            [
                {
                    "surface_kind": "endpoint",
                    "surface_key": "POST https://example.com/login",
                    "description": "Login endpoint",
                    "traits": {
                        "accepts_credentials": "true",
                        "sets_cookie": "true",
                    },
                    "applicable_tests": [
                        {
                            "test_family": "data_exposure",
                            "applicability_basis": "Response includes account metadata",
                            "completion_criteria": "Inspect least-privileged response fields",
                        }
                    ],
                }
            ],
            tier="core",
        )

        cells = list_coverage_slots(conn, "dc1")
        assert len(surface_ids) == 1
        assert {cell.test_family for cell in cells} == {
            "authentication",
            "session",
            "data_exposure",
        }
        by_family = {cell.test_family: cell for cell in cells}
        assert by_family["authentication"].surface_id == surface_ids[0]
        assert by_family["authentication"].coverage_requirement == "required"
        assert by_family["authentication"].coverage_rule_id == "v1.endpoint.credentials"
        assert by_family["data_exposure"].coverage_requirement == "optional"
        assert by_family["data_exposure"].coverage_rule_id == "v1.worker-explicit"

        conn.execute(
            "UPDATE projects SET explore_deadline = '2099-01-01T00:00:00Z' WHERE id = 'dc1'"
        )
        advance_structured_convergence(conn, "dc1")
        metrics = conn.execute(
            """
            SELECT coverage_checkpoint_required_count,
                   coverage_checkpoint_optional_count,
                   coverage_checkpoint_required_terminal_count
            FROM projects WHERE id = 'dc1'
            """
        ).fetchone()
        assert dict(metrics) == {
            "coverage_checkpoint_required_count": 2,
            "coverage_checkpoint_optional_count": 1,
            "coverage_checkpoint_required_terminal_count": 0,
        }
    finally:
        conn.close()


def _seed_minimum_coverage_completion_tail(
    conn: sqlite3.Connection,
    *,
    optional_worker: str | None = None,
) -> tuple[str, list[str]]:
    _seed_default_copy_project(
        conn,
        surface_manifest_version=1,
        minimum_coverage_version=1,
    )
    conn.execute(
        "UPDATE projects SET explore_deadline = '2099-01-01T00:00:00Z' WHERE id = 'dc1'"
    )
    register_discovered_surfaces(
        conn,
        "dc1",
        [
            {
                "surface_kind": "endpoint",
                "surface_key": "POST https://example.com/login",
                "description": "Login endpoint",
                "traits": {
                    "accepts_credentials": "true",
                    "sets_cookie": "true",
                },
                "applicable_tests": [
                    {
                        "test_family": "data_exposure",
                        "applicability_basis": "Response includes account metadata",
                        "completion_criteria": "Inspect least-privileged response fields",
                    }
                ],
            }
        ],
        tier="core",
    )
    cells = list_coverage_slots(conn, "dc1")
    required = [
        cell for cell in cells if cell.coverage_requirement == "required"
    ]
    optional = next(
        cell for cell in cells if cell.coverage_requirement == "optional"
    )
    for cell in required:
        conn.execute(
            """
            UPDATE coverage_slots
            SET status = 'explored', result = 'tested_no_finding'
            WHERE project_id = 'dc1' AND slot_key = ?
            """,
            (cell.slot_key,),
        )
    conn.execute(
        """
        INSERT INTO intents (
            id, project_id, description, creator, worker,
            last_heartbeat_at, created_at, slot_key
        ) VALUES ('i_optional', 'dc1', 'optional depth', 'reason', ?, ?, ?, ?)
        """,
        (
            optional_worker,
            "2099-01-01T00:00:00Z" if optional_worker else None,
            "2026-08-19T00:00:00Z",
            optional.slot_key,
        ),
    )
    conn.execute(
        """
        UPDATE coverage_slots
        SET status = 'exploring', assigned_intent_id = 'i_optional'
        WHERE project_id = 'dc1' AND slot_key = ?
        """,
        (optional.slot_key,),
    )
    return optional.slot_key, [cell.slot_key for cell in required]


def test_minimum_coverage_reason_release_requires_evidence_backed_depth_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """required 终态只进入选题阶段，显式证据选题接受后才能完成，租约释放不能冒充完成。"""
    from cairn.server.services import project_reason as project_reason_module

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(project_reason_module, "get_conn", fake_get_conn)
    try:
        optional_slot_key, _ = _seed_minimum_coverage_completion_tail(conn)

        _claim_release_reason(conn, "dc1", worker="reason-worker")

        from cairn.server.models import DepthReviewRequest, ReasonClaimRequest
        from cairn.server.services.assessment_convergence import assessment_review_digest

        pending = conn.execute("SELECT * FROM projects WHERE id = 'dc1'").fetchone()
        assert pending["status"] == "active"
        assert json.loads(pending["assessment_progress_json"])["phase"] == "depth_review"
        assert conn.execute("SELECT COUNT(*) FROM intents WHERE to_fact_id = 'goal'").fetchone()[0] == 0
        with pytest.raises(HTTPException, match="assessment_depth_review_or_deep_work_pending"):
            project_completion.complete_project_record_with_conn(
                conn, "dc1", CompleteRequest(from_=["fsrc"], description="premature", worker="reason-worker")
            )

        # 真实调用原子选题 API；合成审阅证据与租约均在本内存数据库中。
        project_reason_module.claim_project_reason_record("dc1", ReasonClaimRequest(worker="reason-worker", trigger="assessment"))
        evidence = _seed_current_run_artifact(conn, "dc1", task_run_id="tr_review", artifact_id="a_review")
        conn.execute("UPDATE task_runs SET task_type = 'reason', worker_name = 'reason-worker' WHERE id = 'tr_review'")
        accepted = project_reason_module.accept_depth_review("dc1", DepthReviewRequest(
            worker="reason-worker", task_run_id="tr_review", input_digest=assessment_review_digest(conn, "dc1"),
            description="The baseline evidence supports no additional discriminating hypothesis",
            evidence_refs=[evidence.model_dump(exclude_none=True)], selected=[],
        ))
        assert accepted.intents == []
        assert accepted.fact.assessment_record["kind"] == "depth_review"
        assert accepted.project.assessment_progress["phase"] == "finalizing"
        project_completion.complete_project_record_with_conn(
            conn, "dc1", CompleteRequest(from_=["fsrc"], description="review accepted", worker="reason-worker", task_run_id="tr_review")
        )

        project = conn.execute(
            "SELECT status, coverage_stop_reason FROM projects WHERE id = 'dc1'"
        ).fetchone()
        optional_intent = conn.execute(
            "SELECT conclusion, concluded_at FROM intents WHERE id = 'i_optional'"
        ).fetchone()
        optional_slot = next(
            cell
            for cell in list_coverage_slots(conn, "dc1")
            if cell.slot_key == optional_slot_key
        )
        completion = conn.execute(
            """
            SELECT id, creator, worker, to_fact_id
            FROM intents
            WHERE project_id = 'dc1' AND to_fact_id = 'goal'
            """
        ).fetchall()
        sources = conn.execute(
            """
            SELECT fact_id
            FROM intent_sources
            WHERE project_id = 'dc1' AND intent_id = ?
            ORDER BY source_order
            """,
            (completion[0]["id"],),
        ).fetchall()
        finalization = conn.execute(
            """
            SELECT status, attempt_count
            FROM assessment_report_finalizations
            WHERE project_id = 'dc1'
            """
        ).fetchone()

        assert dict(project) == {
            "status": "completed",
            "coverage_stop_reason": None,
        }
        assert optional_intent["conclusion"] == "abandoned"
        assert optional_intent["concluded_at"] is not None
        # 未执行 optional Cell 保持未测，不能把没有证据的尾部伪装成安全阴性。
        assert optional_slot.status not in {"explored", "verified"}
        assert optional_slot.result == ""
        assert len(completion) == 1
        assert dict(completion[0]) == {
            "id": completion[0]["id"],
            "creator": "reason-worker",
            "worker": "reason-worker",
            "to_fact_id": "goal",
        }
        assert [row["fact_id"] for row in sources] == ["fsrc"]
        assert finalization is None

        with pytest.raises(HTTPException) as duplicate:
            project_completion.complete_project_record_with_conn(
                conn,
                "dc1",
                CompleteRequest(
                    from_=["fsrc"],
                    description="duplicate completion",
                    worker="reason-worker",
                ),
            )
        assert duplicate.value.status_code == 403
        assert conn.execute(
            """
            SELECT COUNT(*) AS c FROM intents
            WHERE project_id = 'dc1' AND to_fact_id = 'goal'
            """
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_minimum_coverage_reason_release_waits_for_running_optional_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行中的 optional Explore 仍必须先收尾，不能被自动跳过或提前完成。"""
    from cairn.server.services import project_reason as project_reason_module

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(project_reason_module, "get_conn", fake_get_conn)
    try:
        optional_slot_key, _ = _seed_minimum_coverage_completion_tail(
            conn,
            optional_worker="explore-worker",
        )

        _claim_release_reason(conn, "dc1", worker="reason-worker")

        project = conn.execute(
            "SELECT status FROM projects WHERE id = 'dc1'"
        ).fetchone()
        optional_slot = next(
            cell
            for cell in list_coverage_slots(conn, "dc1")
            if cell.slot_key == optional_slot_key
        )
        completion_count = conn.execute(
            """
            SELECT COUNT(*) AS c FROM intents
            WHERE project_id = 'dc1' AND to_fact_id = 'goal'
            """
        ).fetchone()["c"]
        assert project["status"] == "active"
        assert optional_slot.status == "exploring"
        assert optional_slot.assigned_intent_id == "i_optional"
        assert completion_count == 0
    finally:
        conn.close()


def test_minimum_coverage_reason_release_keeps_required_cells_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未终态 required Cell 不得被 optional 尾部清理逻辑跳过。"""
    from cairn.server.services import project_reason as project_reason_module

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(project_reason_module, "get_conn", fake_get_conn)
    try:
        _, required_slot_keys = _seed_minimum_coverage_completion_tail(conn)
        conn.execute(
            """
            UPDATE coverage_slots
            SET status = 'pending', result = NULL
            WHERE project_id = 'dc1' AND slot_key = ?
            """,
            (required_slot_keys[0],),
        )

        _claim_release_reason(conn, "dc1", worker="reason-worker")

        required_slot = next(
            cell
            for cell in list_coverage_slots(conn, "dc1")
            if cell.slot_key == required_slot_keys[0]
        )
        project = conn.execute(
            "SELECT status FROM projects WHERE id = 'dc1'"
        ).fetchone()
        assert project["status"] == "active"
        assert required_slot.status == "pending"
        assert required_slot.skip_reason == ""
    finally:
        conn.close()


def test_minimum_coverage_rejects_cells_that_bypass_manifest_derivation() -> None:
    conn = make_conn()
    try:
        _seed_default_copy_project(
            conn,
            surface_manifest_version=1,
            minimum_coverage_version=1,
        )

        with pytest.raises(HTTPException) as exc_info:
            upsert_coverage_slots(
                conn,
                "dc1",
                [
                    {
                        "kind": "endpoint",
                        "surface_kind": "endpoint",
                        "surface_key": "GET https://example.com/admin",
                        "test_family": "authorization",
                        "applicability_basis": "free-form bypass",
                        "completion_criteria": "try access",
                    }
                ],
                tier="core",
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "minimum_coverage_manifest_authority_required"
        assert list_coverage_slots(conn, "dc1") == []
    finally:
        conn.close()


def test_new_web_policy_freezes_contract_without_upgrading_historical_zero() -> None:
    """新 Web 冻结统一安全版本，历史显式 0 只保留只读解释而非执行兼容。"""
    from cairn.server.models import Scenario
    from cairn.server.services.assessment_policy import policy_for_scenario, policy_from_project_row, execution_rejection_reason

    scenario = Scenario(
        key="default-copy-v2",
        label="Default Copy",
        profile_id="default",
        prompt_group="default-copy-v2",
        knowledge="knowledge_base",
        features={
            "assessment_protocol": "default-copy-v1",
            "blocker_cooldown_version": 0,
            "surface_manifest_version": 1,
            "minimum_coverage_version": 0,
        },
    )

    policy = policy_for_scenario(scenario, created_at="2026-08-17T00:00:00Z")

    assert policy.contract_version == 2
    assert policy.blocker_cooldown_version == 1
    assert policy.blocker_cooldown_enabled is True
    assert policy.surface_manifest_enabled is True
    assert policy.minimum_coverage_version == 1
    assert policy.minimum_coverage_enabled is True
    historical = {"assessment_protocol": "default-copy-v1", "scenario_snapshot_json": json.dumps({"features": scenario.features})}
    old_policy = policy_from_project_row(historical)
    assert old_policy.blocker_cooldown_version == 0
    assert old_policy.minimum_coverage_enabled is False
    assert execution_rejection_reason(historical) == "execution_contract_unsupported"


def test_strategy_key_rejects_free_text_and_normalizes_case() -> None:
    """策略身份使用短稳定键，不能退化为任意说明文字。"""
    normalized = CreateIntentRequest(
        **{
            "from": ["fsrc"],
            "description": "test",
            "creator": "reason",
            "strategy_key": "Auth.Owner-Vs-Other:V1",
        }
    )
    assert normalized.strategy_key == "auth.owner-vs-other:v1"
    with pytest.raises(ValueError, match="strategy_key"):
        CreateIntentRequest(
            **{
                "from": ["fsrc"],
                "description": "test",
                "creator": "reason",
                "strategy_key": "try another employee session",
            }
        )


def test_structured_coverage_update_rejects_stale_assignment() -> None:
    """并发收敛丢失 Cell 所有权时必须回滚，而不是静默成功。"""
    from cairn.server.services.coverage_slots import apply_structured_coverage_result

    conn = make_conn()
    try:
        _seed_default_copy_project(conn)
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "endpoint",
                    "surface_key": "GET https://example.com/account",
                    "surface_kind": "endpoint",
                    "test_family": "authorization",
                    "applicability_basis": "账户端点包含对象身份",
                    "completion_criteria": "比较本人和他人对象访问",
                }
            ],
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        _insert_open_intent(conn, "dc1", intent_id="i001")
        conn.execute(
            "UPDATE coverage_slots SET status = 'exploring', assigned_intent_id = 'i001' "
            "WHERE project_id = 'dc1' AND slot_key = ?",
            (slot.slot_key,),
        )
        conn.execute(
            "UPDATE coverage_slots SET assigned_intent_id = NULL "
            "WHERE project_id = 'dc1' AND slot_key = ?",
            (slot.slot_key,),
        )
        result = CoverageResultInput(
            slot_key=slot.slot_key,
            surface_key=slot.surface_key,
            test_family=slot.test_family,
            outcome="temporarily_blocked",
            limitation_reason="session unavailable",
        )

        with pytest.raises(HTTPException) as stale:
            apply_structured_coverage_result(
                conn,
                "dc1",
                slot.slot_key,
                intent_id="i001",
                fact_id="f-result",
                coverage_result=result,
            )

        assert stale.value.detail == "coverage_cell_assignment_changed"
    finally:
        conn.close()


def test_blocker_fingerprint_normalizes_case_and_whitespace() -> None:
    """等价 blocker 文本应生成稳定 fingerprint。"""
    from cairn.server.services.coverage_slots import blocker_fingerprint

    first = blocker_fingerprint(
        "Missing_Employee_Session",
        " Employee session unavailable ",
        "Only  anonymous   access exists",
    )
    second = blocker_fingerprint(
        "missing_employee_session",
        "employee SESSION unavailable",
        "only anonymous access exists",
    )
    assert first == second
    assert first.startswith("blocker:")


def test_format_structured_coverage_slots_separates_cooldown_candidates() -> None:
    """Reason 只接收普通 eligible Cell、变更策略候选和终态摘要。"""
    from cairn.dispatcher.prompting import format_structured_coverage_slots

    payload = json.loads(
        format_structured_coverage_slots(
            [
                {"slot_key": "eligible", "status": "pending", "tier": "core"},
                {
                    "slot_key": "expired-offset",
                    "status": "pending",
                    "tier": "core",
                    "blocker_cooldown_until": "2026-08-17T01:00:00+01:00",
                },
                {
                    "slot_key": "running",
                    "status": "exploring",
                    "tier": "core",
                    "assigned_intent_id": "i-running",
                },
                {
                    "slot_key": "blocked",
                    "status": "pending",
                    "tier": "core",
                    "surface_kind": "endpoint",
                    "surface_key": "POST https://example.com/login",
                    "test_family": "authentication",
                    "blocker_fingerprint": "blocker:abc",
                    "blocker_strategy_key": "auth.anonymous-v1",
                    "blocker_cooldown_until": "2026-08-17T00:15:00Z",
                    "skip_reason": "Employee session unavailable",
                },
                {"slot_key": "done", "status": "explored", "tier": "core"},
            ],
            now="2026-08-17T00:00:00Z",
        )
    )
    assert [cell["slot_key"] for cell in payload["eligible_cells"]] == [
        "eligible",
        "expired-offset",
    ]
    assert payload["strategy_change_candidates"] == [
        {
            "slot_key": "blocked",
            "tier": "core",
            "surface_kind": "endpoint",
            "surface_key": "POST https://example.com/login",
            "test_family": "authentication",
            "blocker_fingerprint": "blocker:abc",
            "blocked_strategy_key": "auth.anonymous-v1",
            "cooldown_until": "2026-08-17T00:15:00Z",
            "limitation_reason": "Employee session unavailable",
        }
    ]
    assert payload["terminal_summary"] == {"explored": 1}


def test_format_coverage_slots_verifier_header() -> None:
    """2: format_coverage_slots 头部声明 verifier 模式，供 prompt 自适配。"""
    from cairn.dispatcher.prompting import format_coverage_slots
    assert "verifier=on" in format_coverage_slots([{"slot_key": "k", "status": "pending", "tier": "core"}], verifier=True)
    assert "verifier=off" in format_coverage_slots([{"slot_key": "k", "status": "pending", "tier": "core"}], verifier=False)


def test_scenario_coverage_gate_structured_prompt_consistency(monkeypatch: pytest.MonkeyPatch) -> None:
    """coverage_gate 与结构化 Prompt 必须配套；不完整配置保存时被拒。"""
    from cairn.server.services import scenarios as scenarios_svc
    from cairn.server.models import Scenario

    conn = make_conn()
    try:
        base = list_scenarios(conn)
        default = [s for s in base if s.key == "default"][0]
        # 结构化 Prompt 缺少 coverage_gate 时拒绝保存。
        bad = Scenario(key="bad1", label="Bad1", is_default=False, profile_id=default.profile_id,
                       prompt_group="default-copy-v2", knowledge="pentest", cap_add=[], features={})
        with pytest.raises(HTTPException) as exc:
            scenarios_svc.validate_scenario_values(conn, [default, bad])
        assert exc.value.status_code == 422
        # coverage_gate=true 与非结构化 Prompt 组合时拒绝保存。
        bad2 = Scenario(key="bad2", label="Bad2", is_default=False, profile_id=default.profile_id,
                        prompt_group="default", knowledge="knowledge_base", cap_add=[],
                        features={"coverage_gate": True})
        with pytest.raises(HTTPException) as exc2:
            scenarios_svc.validate_scenario_values(conn, [default, bad2])
        assert exc2.value.status_code == 422
    finally:
        conn.close()


def test_reason_timeout_settings_default() -> None:
    """4: 新库 settings.reason_timeout 默认 180（lease 放大减心跳误杀）。"""
    conn = make_conn()
    try:
        row = conn.execute("SELECT reason_timeout, coverage_idle_rounds FROM settings WHERE rowid = 1").fetchone()
        assert row["reason_timeout"] == 180
        assert row["coverage_idle_rounds"] == 3
    finally:
        conn.close()


def _insert_open_intent(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str = "i001",
    *,
    strategy_key: str = "test.default-v1",
) -> None:
    project = conn.execute(
        "SELECT assessment_protocol FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    slot_key = None
    if project is not None and project["assessment_protocol"] == "default-copy-v1":
        slot = conn.execute(
            """
            SELECT slot_key FROM coverage_slots
            WHERE project_id = ? AND status IN ('pending', 'exploring')
              AND assigned_intent_id IS NULL
            ORDER BY created_at, slot_key
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        slot_key = slot["slot_key"] if slot is not None else f"cell:{intent_id}"
        if slot is None:
            conn.execute(
                """
                INSERT INTO coverage_slots (
                    project_id, slot_key, kind, description, status, tier,
                    surface_key, surface_kind, test_family,
                    applicability_basis, completion_criteria,
                    created_at, updated_at
                ) VALUES (?, ?, 'endpoint', 'fixture cell', 'pending', 'core',
                          ?, 'endpoint', 'configuration',
                          'fixture applicability', 'fixture completion', ?, ?)
                """,
                (
                    project_id,
                    slot_key,
                    f"GET https://example.test/{intent_id}",
                    "2026-07-13T00:00:00Z",
                    "2026-07-13T00:00:00Z",
                ),
            )
    conn.execute(
        """
        INSERT INTO intents (
            id, project_id, description, creator, created_at, slot_key, strategy_key
        ) VALUES (?, ?, 'explore target', 'reason', ?, ?, ?)
        """,
        (
            intent_id,
            project_id,
            "2026-07-13T00:00:00Z",
            slot_key,
            strategy_key,
        ),
    )
    if slot_key is not None:
        conn.execute(
            """
            UPDATE coverage_slots SET assigned_intent_id = ?, updated_at = ?
            WHERE project_id = ? AND slot_key = ?
            """,
            (
                intent_id,
                "2026-07-13T00:00:00Z",
                project_id,
                slot_key,
            ),
        )


def _explore_attempt_body(intent_id: str = "i001") -> CreateTaskRunRequest:
    return CreateTaskRunRequest(
        intent_id=intent_id,
        task_type="explore",
        worker_name="worker-1",
        worker_type="pi",
        status="running",
        prompt_group="default-copy-v2",
        execution_profile="default",
        runtime_context={"source": "test"},
    )


def test_expired_task_run_heartbeat_cannot_resurrect_intent_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TaskRun 被租约回收终结后，迟到续租不能重新认领同一 Intent。"""
    from cairn.server.services.audit_task_runs import insert_task_run_record

    conn = make_conn()

    @contextmanager
    def transactional_get_conn():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr(intents_router, "get_conn", transactional_get_conn)
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        _insert_open_intent(conn, "dc1")

        claimed = intents_router.heartbeat(
            "dc1", "i001", HeartbeatRequest(worker="worker-1")
        )
        assert claimed.worker == "worker-1"

        with conn:
            task_run = insert_task_run_record(
                conn,
                "dc1",
                CreateTaskRunRequest(
                    intent_id="i001",
                    task_type="bootstrap",
                    worker_name="worker-1",
                    worker_type="pi",
                    status="running",
                    prompt_group="default-copy-v2",
                    execution_profile="default",
                    runtime_context={"source": "test"},
                ),
                principal=TEST_DISPATCHER,
            )
        renewed = intents_router.heartbeat(
            "dc1",
            "i001",
            HeartbeatRequest(worker="worker-1", task_run_id=task_run.id),
        )
        assert renewed.worker == "worker-1"

        stale_heartbeat = "2000-01-01T00:00:00Z"
        conn.execute(
            """
            UPDATE intents
            SET last_heartbeat_at = ?
            WHERE id = 'i001' AND project_id = 'dc1'
            """,
            (stale_heartbeat,),
        )
        conn.commit()

        with pytest.raises(HTTPException) as rejected:
            intents_router.heartbeat(
                "dc1",
                "i001",
                HeartbeatRequest(worker="worker-1", task_run_id=task_run.id),
            )

        assert rejected.value.status_code == 409
        intent_row = conn.execute(
            """
            SELECT worker, last_heartbeat_at
            FROM intents
            WHERE id = 'i001' AND project_id = 'dc1'
            """
        ).fetchone()
        assert intent_row["worker"] is None
        assert intent_row["last_heartbeat_at"] == stale_heartbeat
        task_row = conn.execute(
            """
            SELECT status, error_type
            FROM task_runs
            WHERE id = ? AND project_id = 'dc1'
            """,
            (task_run.id,),
        ).fetchone()
        assert task_row["status"] == "failed"
        assert task_row["error_type"] == "heartbeat_lost"
    finally:
        conn.close()


def test_default_copy_explore_attempt_is_claimed_and_counted_once() -> None:
    """Explore admission 在一个事务内认领 Intent、计数并创建 TaskRun。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        _insert_open_intent(
            conn,
            "dc1",
            strategy_key="auth.invalid-credentials-v1",
        )

        with conn:
            task_run = start_explore_attempt(
                conn,
                "dc1",
                "i001",
                _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )

        project = conn.execute(
            "SELECT explore_attempts_started FROM projects WHERE id = 'dc1'"
        ).fetchone()
        intent = conn.execute(
            "SELECT worker FROM intents WHERE project_id = 'dc1' AND id = 'i001'"
        ).fetchone()
        attempt = conn.execute(
            "SELECT * FROM assessment_attempts WHERE project_id = 'dc1'"
        ).fetchone()
        persisted_run = conn.execute(
            "SELECT assessment_attempt_id, runtime_context_json FROM task_runs WHERE project_id = 'dc1' AND id = ?",
            (task_run.id,),
        ).fetchone()
        assert task_run.intent_id == "i001"
        assert task_run.status == "running"
        assert task_run.assessment_attempt_id == attempt["id"]
        assert task_run.runtime_context["assessment_attempt_id"] == attempt["id"]
        assert task_run.runtime_context["explore_attempt_number"] == 1
        assert task_run.runtime_context["assessment_execution_number"] == 1
        binding = task_run.runtime_context["structured_explore_binding"]
        from cairn.server.services.structured_intent_bindings import (
            parse_structured_explore_binding,
        )

        assert parse_structured_explore_binding(binding).intent_id == "i001"
        assert binding["intent_id"] == "i001"
        assert binding["strategy_key"] == "auth.invalid-credentials-v1"
        assert binding["cell"]["slot_key"] == attempt["slot_key"]
        assert len(binding["digest"]) == 64
        assert attempt["intent_id"] == "i001"
        assert attempt["assessment_role"] == "discovery"
        assert attempt["status"] == "active"
        assert attempt["charged"] == 1
        assert attempt["execution_count"] == 1
        assert attempt["strategy_key"] == "auth.invalid-credentials-v1"
        assert persisted_run["assessment_attempt_id"] == attempt["id"]
        assert attempt["id"] in persisted_run["runtime_context_json"]
        assert project["explore_attempts_started"] == 1
        assert intent["worker"] == "worker-1"

        with pytest.raises(HTTPException) as duplicate:
            with conn:
                start_explore_attempt(
                    conn,
                    "dc1",
                    "i001",
                    _explore_attempt_body(),
                    principal=TEST_DISPATCHER,
                )
        assert duplicate.value.status_code == 409
        project = conn.execute(
            "SELECT explore_attempts_started FROM projects WHERE id = 'dc1'"
        ).fetchone()
        assert project["explore_attempts_started"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM task_runs WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_structured_explore_missing_cell_is_rejected_before_any_write() -> None:
    """损坏的历史 Intent 不得消耗预算，也不得留下 claim、Attempt 或 TaskRun。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, created_at, strategy_key
            ) VALUES ('i001', 'dc1', 'historical malformed intent', 'reason', ?, '')
            """,
            ("2026-07-13T00:00:00Z",),
        )
        conn.commit()

        with pytest.raises(HTTPException) as denied:
            with conn:
                start_explore_attempt(
                    conn,
                    "dc1",
                    "i001",
                    _explore_attempt_body(),
                    principal=TEST_DISPATCHER,
                )

        assert (denied.value.status_code, denied.value.detail) == (
            422,
            "structured_intent_binding_required",
        )
        project = conn.execute(
            "SELECT explore_attempts_started FROM projects WHERE id = 'dc1'"
        ).fetchone()
        intent = conn.execute(
            "SELECT worker FROM intents WHERE project_id = 'dc1' AND id = 'i001'"
        ).fetchone()
        assert project["explore_attempts_started"] == 0
        assert intent["worker"] is None
        assert conn.execute(
            "SELECT COUNT(*) FROM assessment_attempts WHERE project_id = 'dc1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE project_id = 'dc1'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("project_update", "expected_detail", "initial_count"),
    [
        (
            "explore_budget = 1, explore_attempts_started = 1, explore_deadline = '2999-01-01T00:00:00Z'",
            "explore_budget_exhausted",
            1,
        ),
        (
            "explore_budget = 40, explore_deadline = '2000-01-01T00:00:00Z'",
            "explore_deadline_exceeded",
            0,
        ),
    ],
)
def test_default_copy_explore_admission_limit_rolls_back(
    project_update: str,
    expected_detail: str,
    initial_count: int,
) -> None:
    """预算或 deadline 拒绝时不得认领 Intent，也不得创建 TaskRun。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn)
        conn.execute(f"UPDATE projects SET {project_update} WHERE id = 'dc1'")
        _insert_open_intent(conn, "dc1")
        conn.commit()

        with pytest.raises(HTTPException) as denied:
            with conn:
                start_explore_attempt(
                    conn,
                    "dc1",
                    "i001",
                    _explore_attempt_body(),
                    principal=TEST_DISPATCHER,
                )
        assert denied.value.status_code == 409
        assert denied.value.detail == expected_detail
        project = conn.execute(
            "SELECT explore_attempts_started FROM projects WHERE id = 'dc1'"
        ).fetchone()
        intent = conn.execute(
            "SELECT worker FROM intents WHERE project_id = 'dc1' AND id = 'i001'"
        ).fetchone()
        assert project["explore_attempts_started"] == initial_count
        assert intent["worker"] is None
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM task_runs WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_default_copy_task_run_failure_rolls_back_claim_and_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TaskRun 写入失败时，前置预算预约与 Intent 认领必须一起回滚。"""
    from cairn.server.services import explore_attempts as attempts_service

    conn = make_conn()
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        _insert_open_intent(conn, "dc1")
        conn.commit()

        def fail_insert(*_args, **_kwargs):
            raise RuntimeError("task run insert failed")

        monkeypatch.setattr(
            attempts_service,
            "insert_task_run_record",
            fail_insert,
        )
        with pytest.raises(RuntimeError, match="task run insert failed"):
            with conn:
                attempts_service.start_explore_attempt(
                    conn,
                    "dc1",
                    "i001",
                    _explore_attempt_body(),
                    principal=TEST_DISPATCHER,
                )

        project = conn.execute(
            "SELECT explore_attempts_started FROM projects WHERE id = 'dc1'"
        ).fetchone()
        intent = conn.execute(
            "SELECT worker FROM intents WHERE project_id = 'dc1' AND id = 'i001'"
        ).fetchone()
        assert project["explore_attempts_started"] == 0
        assert intent["worker"] is None
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM assessment_attempts WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("error_type", ("heartbeat_lost", "worker_oom"))
def test_default_copy_runtime_retry_reuses_assessment_without_recharging(
    monkeypatch: pytest.MonkeyPatch,
    error_type: str,
) -> None:
    """瞬态运行失败创建新 TaskRun，但复用逻辑评估且不重复扣预算。"""
    from cairn.server.services import audit_task_runs as task_run_service

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        with conn:
            yield conn

    monkeypatch.setattr(task_run_service, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        _insert_open_intent(conn, "dc1")
        with conn:
            first = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )
        task_run_service.update_task_run_record(
            "dc1",
            first.id,
            UpdateTaskRunRequest(status="failed", error_type=error_type),
            principal=TEST_DISPATCHER,
        )
        with conn:
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = 'i001' AND project_id = 'dc1'"
            )
        with conn:
            retried = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )

        project = conn.execute(
            "SELECT explore_attempts_started FROM projects WHERE id = 'dc1'"
        ).fetchone()
        attempt = conn.execute(
            "SELECT * FROM assessment_attempts WHERE project_id = 'dc1'"
        ).fetchone()
        assert retried.id != first.id
        assert retried.assessment_attempt_id == first.assessment_attempt_id
        assert retried.runtime_context["assessment_execution_number"] == 2
        assert project["explore_attempts_started"] == 1
        assert attempt["charged"] == 1
        assert attempt["execution_count"] == 2
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM task_runs WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 2
    finally:
        conn.close()


def test_default_copy_orphaned_running_execution_is_recovered_without_recharge() -> None:
    """admission 响应丢失后，失去 Intent lease 的 running execution 可被有界恢复。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        _insert_open_intent(conn, "dc1")
        with conn:
            orphaned = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )
        with conn:
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = 'i001' AND project_id = 'dc1'"
            )
        with pytest.raises(HTTPException, match="assessment_execution_not_quiescent"):
            with conn:
                start_explore_attempt(conn, "dc1", "i001", _explore_attempt_body(), principal=TEST_DISPATCHER)
        assert conn.execute("SELECT status FROM task_runs WHERE id = ?", (orphaned.id,)).fetchone()["status"] == "running"
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1
        # 租约释放不证明进程停止；显式 reconciliation 后才允许免费重试。
        with conn:
            conn.execute("UPDATE task_runs SET status = 'failed', error_type = 'heartbeat_lost' WHERE id = ?", (orphaned.id,))
            recovered = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )

        prior = conn.execute(
            "SELECT status, error_type FROM task_runs WHERE id = ? AND project_id = 'dc1'",
            (orphaned.id,),
        ).fetchone()
        assert prior["status"] == "failed"
        assert prior["error_type"] == "heartbeat_lost"
        assert recovered.assessment_attempt_id == orphaned.assessment_attempt_id
        assert recovered.runtime_context["assessment_execution_number"] == 2
        assert conn.execute(
            "SELECT explore_attempts_started FROM projects WHERE id = 'dc1'"
        ).fetchone()["explore_attempts_started"] == 1
    finally:
        conn.close()


def test_orphan_reconciliation_survives_budget_denial() -> None:
    """admission 的 409 不得回滚旧 execution 与 attempt 的收敛。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            """
            UPDATE projects
            SET explore_budget = 1, explore_deadline = '2999-01-01T00:00:00Z'
            WHERE id = 'dc1'
            """
        )
        _insert_open_intent(conn, "dc1")
        with conn:
            run = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )
            conn.execute(
                "UPDATE assessment_attempts SET max_runtime_retries = 0 "
                "WHERE id = ? AND project_id = 'dc1'",
                (run.assessment_attempt_id,),
            )
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = 'i001' AND project_id = 'dc1'"
            )

        with pytest.raises(HTTPException, match="assessment_execution_not_quiescent"):
            with conn:
                start_explore_attempt(conn, "dc1", "i001", _explore_attempt_body(), principal=TEST_DISPATCHER)
        with conn:
            conn.execute("UPDATE task_runs SET status = 'failed', error_type = 'heartbeat_lost' WHERE id = ?", (run.id,))
        # 原 Intent 不得在 retry 用尽后重新收费；拒绝仍须持久化旧 attempt 收敛。
        with pytest.raises(HTTPException, match="assessment_attempt_already_closed"):
            with conn:
                start_explore_attempt(conn, "dc1", "i001", _explore_attempt_body(), principal=TEST_DISPATCHER)
        with conn:
            _insert_open_intent(conn, "dc1", intent_id="i002")
        with pytest.raises(HTTPException) as denied:
            with conn:
                start_explore_attempt(
                    conn, "dc1", "i002", _explore_attempt_body("i002"),
                    principal=TEST_DISPATCHER,
                )
        assert denied.value.detail == "explore_budget_exhausted"
        task_run = conn.execute(
            "SELECT status, error_type FROM task_runs WHERE id = ? AND project_id = 'dc1'",
            (run.id,),
        ).fetchone()
        assert task_run["status"] == "failed"
        assert task_run["error_type"] == "heartbeat_lost"
        attempt = conn.execute(
            "SELECT status, outcome FROM assessment_attempts WHERE id = ? AND project_id = 'dc1'",
            (run.assessment_attempt_id,),
        ).fetchone()
        assert attempt["status"] == "failed"
        assert attempt["outcome"] == "runtime_retry_exhausted:heartbeat_lost"
    finally:
        conn.close()


def test_default_copy_runtime_retry_respects_hard_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已存在 attempt 的免费 retry 也不能越过项目硬 deadline。"""
    from cairn.server.services import audit_task_runs as task_run_service

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        with conn:
            yield conn

    monkeypatch.setattr(task_run_service, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        _insert_open_intent(conn, "dc1")
        with conn:
            first = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )
        task_run_service.update_task_run_record(
            "dc1",
            first.id,
            UpdateTaskRunRequest(status="failed", error_type="worker_oom"),
            principal=TEST_DISPATCHER,
        )
        with conn:
            conn.execute(
                """
                UPDATE projects SET explore_deadline = '2000-01-01T00:00:00Z'
                WHERE id = 'dc1'
                """
            )
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = 'i001' AND project_id = 'dc1'"
            )

        with pytest.raises(HTTPException) as denied:
            with conn:
                start_explore_attempt(
                    conn, "dc1", "i001", _explore_attempt_body(),
                    principal=TEST_DISPATCHER,
                )
        assert denied.value.status_code == 409
        assert denied.value.detail == "explore_deadline_exceeded"
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM task_runs WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 1
        attempt = conn.execute(
            "SELECT status, outcome FROM assessment_attempts WHERE project_id = 'dc1'"
        ).fetchone()
        assert attempt["status"] == "failed"
        assert attempt["outcome"] == "deadline_exceeded"
        project = conn.execute("SELECT status, coverage_stop_reason, explore_attempts_started FROM projects WHERE id = 'dc1'").fetchone()
        assert dict(project) == {"status": "stopped", "coverage_stop_reason": "deadline_exceeded", "explore_attempts_started": 1}
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM assessment_attempts "
            "WHERE project_id = 'dc1' AND status = 'active'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_default_copy_runtime_retry_exhaustion_starts_new_charged_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """免费 retry 用尽后结束旧 attempt，避免后续 admission 永久 409。"""
    from cairn.server.services import audit_task_runs as task_run_service

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        with conn:
            yield conn

    monkeypatch.setattr(task_run_service, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        _insert_open_intent(conn, "dc1")
        runs = []
        for execution_number in range(1, 4):
            with conn:
                run = start_explore_attempt(
                    conn, "dc1", "i001", _explore_attempt_body(),
                    principal=TEST_DISPATCHER,
                )
            runs.append(run)
            assert run.runtime_context["assessment_execution_number"] == execution_number
            task_run_service.update_task_run_record(
                "dc1",
                run.id,
                UpdateTaskRunRequest(
                    status="failed", error_type="heartbeat_lost"
                ),
                principal=TEST_DISPATCHER,
            )
            with conn:
                conn.execute(
                    "UPDATE intents SET worker = NULL WHERE id = 'i001' AND project_id = 'dc1'"
                )

        with pytest.raises(HTTPException, match="Intent already concluded"):
            with conn:
                start_explore_attempt(conn, "dc1", "i001", _explore_attempt_body(), principal=TEST_DISPATCHER)
        assert conn.execute("SELECT explore_attempts_started FROM projects WHERE id = 'dc1'").fetchone()[0] == 1
        # 新的逻辑尝试需要显式新 Intent，不能复活已收敛的旧义务。
        with conn:
            _insert_open_intent(conn, "dc1", intent_id="i002")
            next_attempt = start_explore_attempt(
                conn, "dc1", "i002", _explore_attempt_body("i002"),
                principal=TEST_DISPATCHER,
            )
        assert next_attempt.assessment_attempt_id != runs[0].assessment_attempt_id
        assert conn.execute(
            "SELECT explore_attempts_started FROM projects WHERE id = 'dc1'"
        ).fetchone()["explore_attempts_started"] == 2
        assert conn.execute(
            "SELECT status, outcome FROM assessment_attempts WHERE id = ? AND project_id = 'dc1'",
            (runs[0].assessment_attempt_id,),
        ).fetchone()["status"] == "failed"
        assert len(runs) == 3
    finally:
        conn.close()


def test_expired_final_retry_stays_reconciled_when_budget_denies_next_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最后一次 execution 丢失后，租约回收结果不能被后续预算拒绝回滚。"""
    from cairn.server.services import audit_task_runs as task_run_service

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        with conn:
            yield conn

    monkeypatch.setattr(task_run_service, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            """
            UPDATE projects
            SET explore_budget = 1, explore_deadline = '2999-01-01T00:00:00Z'
            WHERE id = 'dc1'
            """
        )
        _insert_open_intent(conn, "dc1")
        final_run = None
        for execution_number in range(1, 4):
            with conn:
                final_run = start_explore_attempt(
                    conn, "dc1", "i001", _explore_attempt_body(),
                    principal=TEST_DISPATCHER,
                )
            if execution_number < 3:
                task_run_service.update_task_run_record(
                    "dc1",
                    final_run.id,
                    UpdateTaskRunRequest(
                        status="failed", error_type="heartbeat_lost"
                    ),
                    principal=TEST_DISPATCHER,
                )
                with conn:
                    conn.execute(
                        "UPDATE intents SET worker = NULL WHERE id = 'i001' AND project_id = 'dc1'"
                    )

        assert final_run is not None
        with conn:
            clear_open_intent_worker(
                conn,
                "dc1",
                "i001",
                "heartbeat_timeout",
                cleared_at="2026-08-17T00:01:00Z",
            )
        with pytest.raises(HTTPException, match="Intent already concluded"):
            with conn:
                start_explore_attempt(conn, "dc1", "i001", _explore_attempt_body(), principal=TEST_DISPATCHER)
        with conn:
            _insert_open_intent(conn, "dc1", intent_id="i002")
        with pytest.raises(HTTPException) as denied:
            with conn:
                start_explore_attempt(
                    conn, "dc1", "i002", _explore_attempt_body("i002"),
                    principal=TEST_DISPATCHER,
                )
        assert denied.value.detail == "explore_budget_exhausted"
        assert conn.execute(
            "SELECT status, error_type FROM task_runs WHERE id = ? AND project_id = 'dc1'",
            (final_run.id,),
        ).fetchone()["status"] == "failed"
        attempt = conn.execute(
            "SELECT status, outcome FROM assessment_attempts WHERE project_id = 'dc1'"
        ).fetchone()
        assert attempt["status"] == "failed"
        assert attempt["outcome"] == "runtime_retry_exhausted:heartbeat_lost"
    finally:
        conn.close()


def test_default_copy_non_runtime_failure_requires_new_charged_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析等业务输出失败结束旧 attempt，后续执行必须重新扣预算。"""
    from cairn.server.services import audit_task_runs as task_run_service

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        with conn:
            yield conn

    monkeypatch.setattr(task_run_service, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        _insert_open_intent(conn, "dc1")
        with conn:
            first = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )
        task_run_service.update_task_run_record(
            "dc1",
            first.id,
            UpdateTaskRunRequest(status="failed", error_type="parse_error"),
            principal=TEST_DISPATCHER,
        )
        with conn:
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = 'i001' AND project_id = 'dc1'"
            )

        with pytest.raises(HTTPException, match="Intent already concluded"):
            with conn:
                start_explore_attempt(conn, "dc1", "i001", _explore_attempt_body(), principal=TEST_DISPATCHER)
        assert conn.execute("SELECT explore_attempts_started FROM projects WHERE id = 'dc1'").fetchone()[0] == 1
        # 新的逻辑尝试需要显式新 Intent，不能复活已收敛的旧义务。
        with conn:
            _insert_open_intent(conn, "dc1", intent_id="i002")
            second = start_explore_attempt(
                conn, "dc1", "i002", _explore_attempt_body("i002"),
                principal=TEST_DISPATCHER,
            )
        assert second.assessment_attempt_id != first.assessment_attempt_id
        assert conn.execute(
            "SELECT explore_attempts_started FROM projects WHERE id = 'dc1'"
        ).fetchone()["explore_attempts_started"] == 2
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM task_runs WHERE project_id = 'dc1'"
        ).fetchone()["c"] == 2
        statuses = conn.execute(
            "SELECT status FROM assessment_attempts WHERE project_id = 'dc1' ORDER BY id"
        ).fetchall()
        assert [row["status"] for row in statuses] == ["failed", "active"]
    finally:
        conn.close()


def test_terminal_task_run_status_cannot_regress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态 TaskRun 不允许被迟到更新恢复为 running。"""
    from cairn.server.services import audit_task_runs as task_run_service

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        with conn:
            yield conn

    monkeypatch.setattr(task_run_service, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        _insert_open_intent(conn, "dc1")
        with conn:
            run = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )
        task_run_service.update_task_run_record(
            "dc1",
            run.id,
            UpdateTaskRunRequest(status="failed", error_type="parse_error"),
            principal=TEST_DISPATCHER,
        )

        with pytest.raises(HTTPException) as rejected:
            task_run_service.update_task_run_record(
                "dc1",
                run.id,
                UpdateTaskRunRequest(status="running"),
                principal=TEST_DISPATCHER,
            )
        assert rejected.value.status_code == 409
        assert rejected.value.detail == "Terminal task run status cannot change"
    finally:
        conn.close()


def test_task_run_update_rejects_stale_status_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发终态写入后，基于旧 running 快照的更新必须返回冲突。"""
    from cairn.server.services import audit_task_runs as task_run_service

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        with conn:
            yield conn

    monkeypatch.setattr(task_run_service, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        _insert_open_intent(conn, "dc1")
        with conn:
            run = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )

        original_validate = task_run_service.validate_task_run_exists
        injected = False

        def validate_with_concurrent_finish(target, project_id, task_run_id):
            nonlocal injected
            row = original_validate(target, project_id, task_run_id)
            if not injected:
                injected = True
                target.execute(
                    """
                    UPDATE task_runs
                    SET status = 'failed', ended_at = ?
                    WHERE id = ? AND project_id = ?
                    """,
                    ("2026-08-17T00:01:00Z", task_run_id, project_id),
                )
            return row

        monkeypatch.setattr(
            task_run_service,
            "validate_task_run_exists",
            validate_with_concurrent_finish,
        )
        with pytest.raises(HTTPException) as rejected:
            task_run_service.update_task_run_record(
                "dc1",
                run.id,
                UpdateTaskRunRequest(runtime_context={"late": True}),
                principal=TEST_DISPATCHER,
            )
        assert rejected.value.status_code == 409
        assert rejected.value.detail == "Task run changed concurrently"
    finally:
        conn.close()


def test_default_copy_conclusion_closes_assessment_attempt_atomically() -> None:
    """Coverage conclude 的事务回滚也必须恢复 active assessment attempt。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        _insert_open_intent(conn, "dc1")
        with conn:
            task_run = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )

        with pytest.raises(RuntimeError, match="rollback conclude"):
            with conn:
                close_assessment_attempt_for_task_run(
                    conn,
                    "dc1",
                    "i001",
                    task_run.id,
                    outcome="tested_no_finding",
                )
                raise RuntimeError("rollback conclude")
        assert conn.execute(
            "SELECT status FROM assessment_attempts WHERE project_id = 'dc1'"
        ).fetchone()["status"] == "active"

        with conn:
            close_assessment_attempt_for_task_run(
                conn,
                "dc1",
                "i001",
                task_run.id,
                outcome="tested_no_finding",
            )
        attempt = conn.execute(
            "SELECT status, outcome, ended_at FROM assessment_attempts WHERE project_id = 'dc1'"
        ).fetchone()
        assert attempt["status"] == "completed"
        assert attempt["outcome"] == "tested_no_finding"
        assert attempt["ended_at"] is not None
    finally:
        conn.close()


def test_default_copy_delayed_failed_execution_cannot_close_retried_attempt() -> None:
    """旧 execution 的迟到结论不能关闭同一逻辑 attempt 的当前 retry。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        _insert_open_intent(conn, "dc1")
        with conn:
            first = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )
        with conn:
            conn.execute(
                "UPDATE task_runs SET status = 'failed', error_type = 'heartbeat_lost', "
                "ended_at = ? WHERE id = ? AND project_id = 'dc1'",
                ("2026-08-17T00:01:00Z", first.id),
            )
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = 'i001' AND project_id = 'dc1'"
            )
            retry = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )

        with pytest.raises(HTTPException) as stale:
            with conn:
                close_assessment_attempt_for_task_run(
                    conn,
                    "dc1",
                    "i001",
                    first.id,
                    outcome="tested_no_finding",
                )
        assert stale.value.status_code == 409
        assert stale.value.detail == "assessment_execution_is_not_current"
        assert conn.execute(
            "SELECT status FROM assessment_attempts WHERE project_id = 'dc1'"
        ).fetchone()["status"] == "active"
        assert conn.execute(
            "SELECT status FROM task_runs WHERE id = ? AND project_id = 'dc1'",
            (retry.id,),
        ).fetchone()["status"] == "running"
    finally:
        conn.close()


def test_current_execution_order_handles_task_run_id_width_rollover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同秒创建 tr1000 时，不能因字符串排序把 tr999 误判为最新。"""
    from cairn.server.services import audit_task_runs as task_run_service

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        with conn:
            yield conn

    monkeypatch.setattr(task_run_service, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        conn.execute(
            "INSERT INTO scoped_counters (project_id, kind, value) "
            "VALUES ('dc1', 'task_run', 998)"
        )
        _insert_open_intent(conn, "dc1")
        with conn:
            first = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )
            conn.execute(
                "UPDATE task_runs SET status = 'failed', error_type = 'worker_oom', ended_at = started_at "
                "WHERE id = ? AND project_id = 'dc1'",
                (first.id,),
            )
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = 'i001' AND project_id = 'dc1'"
            )
            retry = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )
            conn.execute(
                "UPDATE task_runs SET started_at = "
                "(SELECT started_at FROM task_runs WHERE id = ? AND project_id = 'dc1') "
                "WHERE id = ? AND project_id = 'dc1'",
                (first.id, retry.id),
            )
        assert first.id == "tr999"
        assert retry.id == "tr1000"
        assert [
            run.id for run in task_run_service.list_task_run_records("dc1")
        ] == ["tr999", "tr1000"]

        with conn:
            close_assessment_attempt_for_task_run(
                conn,
                "dc1",
                "i001",
                retry.id,
                outcome="tested_no_finding",
            )
        assert conn.execute(
            "SELECT status FROM assessment_attempts WHERE project_id = 'dc1'"
        ).fetchone()["status"] == "completed"
    finally:
        conn.close()


def test_delayed_terminal_update_cannot_close_current_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 execution 的重复终态更新不得重新触发 attempt finalization。"""
    from cairn.server.services import audit_task_runs as task_run_service

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        with conn:
            yield conn

    monkeypatch.setattr(task_run_service, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        _insert_open_intent(conn, "dc1")
        with conn:
            first = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )
        task_run_service.update_task_run_record(
            "dc1",
            first.id,
            UpdateTaskRunRequest(status="failed", error_type="worker_oom"),
            principal=TEST_DISPATCHER,
        )
        with conn:
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = 'i001' AND project_id = 'dc1'"
            )
            retry = start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )

        task_run_service.update_task_run_record(
            "dc1",
            first.id,
            UpdateTaskRunRequest(status="failed", error_type="parse_error"),
            principal=TEST_DISPATCHER,
        )

        attempt = conn.execute(
            "SELECT status, outcome FROM assessment_attempts WHERE project_id = 'dc1'"
        ).fetchone()
        assert attempt["status"] == "active"
        assert attempt["outcome"] is None
        assert conn.execute(
            "SELECT status FROM task_runs WHERE id = ? AND project_id = 'dc1'",
            (retry.id,),
        ).fetchone()["status"] == "running"
    finally:
        conn.close()


def test_default_copy_active_assessment_requires_task_run_on_conclusion() -> None:
    """活跃结构化 attempt 不允许无 TaskRun 来源地被结论关闭。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        _insert_open_intent(conn, "dc1")
        with conn:
            start_explore_attempt(
                conn, "dc1", "i001", _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )

        with pytest.raises(HTTPException) as rejected:
            with conn:
                close_assessment_attempt_for_task_run(
                    conn,
                    "dc1",
                    "i001",
                    None,
                    outcome="temporarily_blocked",
                    require_linked_task_run=True,
                )
        assert rejected.value.status_code == 422
        assert rejected.value.detail == "assessment_task_run_required"
        assert conn.execute(
            "SELECT status FROM assessment_attempts WHERE project_id = 'dc1'"
        ).fetchone()["status"] == "active"
    finally:
        conn.close()


def test_default_copy_rejects_task_run_from_another_intent() -> None:
    """v2 结论的 TaskRun 必须属于当前 Intent，不能借用其他执行的证据来源。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn)
        now = "2026-08-17T00:00:00Z"
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, created_at
            ) VALUES ('i001', 'dc1', 'first', 'reason', ?),
                     ('i002', 'dc1', 'second', 'reason', ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, intent_id, task_type, worker_name,
                worker_type, status, started_at, runtime_context_json
            ) VALUES (
                'tr_other', 'dc1', 'i002', 'explore', 'worker-1',
                'explore', 'succeeded', ?, '{}'
            )
            """,
            (now,),
        )

        with pytest.raises(HTTPException) as rejected:
            with conn:
                close_assessment_attempt_for_task_run(
                    conn,
                    "dc1",
                    "i001",
                    "tr_other",
                    outcome="tested_no_finding",
                    require_linked_task_run=True,
                )
        assert rejected.value.status_code == 422
        assert (
            rejected.value.detail
            == "task_run_id does not belong to the concluded intent"
        )
    finally:
        conn.close()


@pytest.mark.parametrize("second_identity_available", [True, False])
def test_default_copy_conclude_route_closes_linked_assessment_attempt(
    monkeypatch: pytest.MonkeyPatch,
    second_identity_available: bool,
) -> None:
    """结构化 Fact、Coverage 与 assessment attempt 在同一 conclude 事务内完成。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(
            conn, outcome_contract_version=2, surface_manifest_version=1, minimum_coverage_version=1,
        )
        register_discovered_surfaces(
            conn, "dc1", [{
                "surface_key": "GET https://example.com/profile",
                "surface_kind": "endpoint",
                "traits": {"object_id": "profile", "role_boundary": "owner/non-owner"},
            }], tier="core", source_fact_id="fsrc",
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        intent = intents_router.create_intent(
            "dc1",
            CreateIntentRequest(
                **{
                    "from": ["fsrc"],
                    "description": "test profile access",
                    "creator": "reason",
                    "slot_key": slot.slot_key,
                    "strategy_key": "authorization.owner-vs-other-v1",
                }
            ),
        )
        with conn:
            task_run = start_explore_attempt(
                conn,
                "dc1",
                intent.id,
                _explore_attempt_body(intent.id),
                principal=TEST_DISPATCHER,
            )
        evidence = EvidenceRef(artifact_id="a_compare", pointer="request/response")
        conn.execute(
            "INSERT INTO artifacts (id, project_id, task_run_id, kind, category, uri, sha256, size, created_at) VALUES ('a_compare', 'dc1', ?, 'raw_output', 'evidence', 'artifact://synthetic/compare', 'digest', 10, '2026-07-13T00:00:00Z')",
            (task_run.id,),
        )
        register_discovered_surfaces(
            conn, "dc1", [{"surface_kind": "endpoint", "surface_key": slot.surface_key,
                           "traits": {"object_id": "profile", "role_boundary": "owner/non-owner",
                                      "second_identity_available": str(second_identity_available).lower()},
                           "evidence_refs": [evidence.model_dump(exclude_none=True)]}],
            tier="core", source_fact_id="fsrc", source_task_run_id=task_run.id,
        )
        result = CoverageResultInput(
            slot_key=slot.slot_key,
            surface_key=slot.surface_key,
            test_family=slot.test_family,
            outcome="tested_no_finding",
            baseline="own profile returned 200",
            variants=["other profile returned 403"],
            evidence_refs=[evidence],
        )

        if not second_identity_available:
            with pytest.raises(HTTPException, match="authorization_second_identity_missing_use_blocked"):
                intents_router.conclude("dc1", intent.id, ConcludeRequest(
                    worker="worker-1", description="unsupported authorization negative", task_run_id=task_run.id,
                    evidence=[evidence], coverage_result=result, assessment_record=_experiment(result, evidence),
                ))
            assert conn.execute("SELECT COUNT(*) FROM facts WHERE project_id = 'dc1'").fetchone()[0] == 1
            assert conn.execute("SELECT status FROM assessment_attempts WHERE project_id = 'dc1'").fetchone()[0] == "active"
            assert list_coverage_slots(conn, "dc1")[0].status == "exploring"
            # 无第二身份时接受的是带证据和局限的 blocked，不是授权安全阴性。
            result = result.model_copy(update={
                "outcome": "temporarily_blocked", "baseline": "Owner profile returned 200", "variants": [],
                "blocker_code": "missing_second_principal", "limitation_reason": "Second approved identity unavailable",
                "limitation_details": "Only the owner session is available; cross-user access was not tested",
                "attempted_strategies": ["Request a second authorized test account"],
            })
        experiment = _experiment(result, evidence)
        if not second_identity_available:
            experiment["preconditions"] = ["Only an authorized owner session is available"]
        response = intents_router.conclude(
            "dc1",
            intent.id,
            ConcludeRequest(
                worker="worker-1",
                description=("authorization boundary enforced" if second_identity_available
                             else "authorization comparison blocked: second identity unavailable"),
                task_run_id=task_run.id,
                coverage_result=result,
                evidence=[evidence],
                assessment_record=experiment,
            ),
        )
        attempt = conn.execute(
            "SELECT status, outcome FROM assessment_attempts WHERE project_id = 'dc1'"
        ).fetchone()
        assert response.fact.task_run_id == task_run.id
        assert attempt["status"] == "completed"
        assert attempt["outcome"] == result.outcome
        cell = list_coverage_slots(conn, "dc1")[0]
        assert cell.result == result.outcome
        assert cell.status == ("explored" if second_identity_available else "pending")
        assert response.fact.assessment_record["evidence_refs"] == [evidence.model_dump(exclude_none=True)]
    finally:
        conn.close()


def test_non_web_pi_explore_attempt_does_not_consume_assessment_budget() -> None:
    """冻结 pi 的非 Web 场景可原子 admission，但不消耗 Web assessment 计数。"""
    conn = make_conn()
    try:
        _seed_default_project(conn)
        _insert_open_intent(conn, "d1")

        with conn:
            task_run = start_explore_attempt(
                conn,
                "d1",
                "i001",
                _explore_attempt_body(),
                principal=TEST_DISPATCHER,
            )

        project = conn.execute(
            "SELECT explore_attempts_started FROM projects WHERE id = 'd1'"
        ).fetchone()
        assert task_run.intent_id == "i001"
        assert task_run.assessment_attempt_id is None
        assert project["explore_attempts_started"] == 0
    finally:
        conn.close()


def test_default_copy_budget_stop_abandons_unclaimed_and_skips_tail() -> None:
    """预算耗尽后不再重试未认领 Intent，并持久化非完整覆盖原因。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn)
        conn.execute("INSERT INTO facts (id, project_id, description, creator, created_at, assessment_record_json) VALUES ('freview', 'dc1', 'accepted depth review', 'reason', '2026-07-13T00:00:00Z', ?)",
                     (json.dumps({"version": 1, "kind": "depth_review", "selected": []}),))
        conn.execute("UPDATE projects SET assessment_progress_json = ? WHERE id = 'dc1'",
                     (json.dumps({"version": 1, "phase": "deep", "depth_review_fact_id": "freview"}),))
        conn.execute(
            """
            UPDATE projects
            SET explore_budget = 1,
                explore_attempts_started = 1,
                explore_deadline = '2999-01-01T00:00:00Z'
            WHERE id = 'dc1'
            """
        )
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "endpoint",
                    "surface_key": "GET https://example.com/admin",
                    "surface_kind": "endpoint",
                    "test_family": "authorization",
                    "applicability_basis": "管理端点需要权限边界验证",
                    "completion_criteria": "比较普通用户与管理员访问结果",
                }
            ],
            tier="core",
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, created_at, slot_key
            ) VALUES ('i001', 'dc1', 'test authorization', 'reason', ?, ?)
            """,
            ("2026-07-13T00:00:00Z", slot.slot_key),
        )
        conn.execute(
            """
            UPDATE coverage_slots
            SET status = 'exploring', assigned_intent_id = 'i001'
            WHERE project_id = 'dc1' AND slot_key = ?
            """,
            (slot.slot_key,),
        )

        stop_reason = advance_structured_convergence(conn, "dc1")

        project = conn.execute(
            """
            SELECT coverage_stop_reason,
                   coverage_checkpoint_terminal_count
            FROM projects WHERE id = 'dc1'
            """
        ).fetchone()
        intent = conn.execute(
            "SELECT conclusion FROM intents WHERE project_id = 'dc1' AND id = 'i001'"
        ).fetchone()
        updated = list_coverage_slots(conn, "dc1")[0]
        assert stop_reason == "budget_exhausted"
        assert project["coverage_stop_reason"] == "budget_exhausted"
        assert project["coverage_checkpoint_terminal_count"] == 1
        assert intent["conclusion"] == "abandoned"
        assert updated.status == "skipped"
        assert updated.skip_reason == "budget_exhausted"
        assert updated.assigned_intent_id is None
    finally:
        conn.close()


def test_default_copy_budget_stop_waits_for_running_explore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """硬限制只排空未认领任务；运行中的 Explore 收尾前不能完成项目。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(project_completion, "get_conn", fake_get_conn)
    try:
        _seed_default_copy_project(conn)
        conn.execute("INSERT INTO facts (id, project_id, description, creator, created_at, assessment_record_json) VALUES ('freview', 'dc1', 'accepted depth review', 'reason', '2026-07-13T00:00:00Z', ?)",
                     (json.dumps({"version": 1, "kind": "depth_review", "selected": []}),))
        conn.execute("UPDATE projects SET assessment_progress_json = ? WHERE id = 'dc1'",
                     (json.dumps({"version": 1, "phase": "deep", "depth_review_fact_id": "freview"}),))
        conn.execute(
            """
            UPDATE projects
            SET explore_budget = 1,
                explore_attempts_started = 1,
                explore_deadline = '2999-01-01T00:00:00Z'
            WHERE id = 'dc1'
            """
        )
        upsert_coverage_slots(
            conn,
            "dc1",
            [
                {
                    "kind": "endpoint",
                    "surface_key": "POST https://example.com/login",
                    "surface_kind": "endpoint",
                    "test_family": "authentication",
                    "applicability_basis": "登录接口处理外部凭据",
                    "completion_criteria": "完成正常与异常凭据比较",
                }
            ],
            tier="core",
        )
        slot = list_coverage_slots(conn, "dc1")[0]
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, worker,
                last_heartbeat_at, created_at, slot_key
            ) VALUES ('i001', 'dc1', 'test login', 'reason', 'worker-1', ?, ?, ?)
            """,
            (
                "2026-07-13T00:00:00Z",
                "2026-07-13T00:00:00Z",
                slot.slot_key,
            ),
        )
        conn.execute(
            """
            UPDATE coverage_slots
            SET status = 'exploring', assigned_intent_id = 'i001'
            WHERE project_id = 'dc1' AND slot_key = ?
            """,
            (slot.slot_key,),
        )
        conn.execute(
            """
            INSERT INTO scoped_counters (project_id, kind, value)
            VALUES ('dc1', 'intent', 1)
            ON CONFLICT (project_id, kind) DO UPDATE SET value = 1
            """
        )

        # 预算已收费不等于 physical execution 已结束，不能抢先回收或新开 retry。
        conn.execute("INSERT INTO task_runs (id, project_id, intent_id, task_type, worker_name, worker_type, status, started_at) VALUES ('tr_running', 'dc1', 'i001', 'explore', 'worker-1', 'pi', 'running', '2026-07-13T00:00:00Z')")
        assert advance_structured_convergence(conn, "dc1") is None
        assert list_coverage_slots(conn, "dc1")[0].status == "exploring"
        with pytest.raises(HTTPException) as blocked:
            project_completion.complete_project_record(
                "dc1",
                CompleteRequest(
                    from_=["fsrc"],
                    description="done",
                    worker="reason",
                ),
            )
        assert blocked.value.status_code == 409
        assert blocked.value.detail == "assessment_depth_review_or_deep_work_pending"

        conn.execute(
            """
            UPDATE intents
            SET worker = NULL, concluded_at = ?, conclusion = 'failed'
            WHERE project_id = 'dc1' AND id = 'i001'
            """,
            ("2026-07-13T00:01:00Z",),
        )
        conn.execute(
            """
            UPDATE coverage_slots
            SET status = 'pending', assigned_intent_id = NULL
            WHERE project_id = 'dc1' AND slot_key = ?
            """,
            (slot.slot_key,),
        )
        conn.execute("UPDATE task_runs SET status = 'failed', error_type = 'parse_error' WHERE id = 'tr_running'")
        assert advance_structured_convergence(conn, "dc1") == "budget_exhausted"
        with pytest.raises(HTTPException) as stopped:
            project_completion.complete_project_record(
                "dc1",
                CompleteRequest(
                    from_=["fsrc"],
                    description="done",
                    worker="reason",
                ),
            )
        assert stopped.value.status_code == 403
        assert conn.execute("SELECT status FROM projects WHERE id = 'dc1'").fetchone()[0] == "stopped"
        assert conn.execute("SELECT COUNT(*) FROM intents WHERE to_fact_id = 'goal'").fetchone()[0] == 0
        assert list_coverage_slots(conn, "dc1")[0].skip_reason == "budget_exhausted"
        finalization = conn.execute(
            "SELECT status, attempt_count FROM assessment_report_finalizations WHERE project_id = 'dc1'"
        ).fetchone()
        assert finalization is None
    finally:
        conn.close()


def test_web_idle_rounds_and_fact_count_do_not_replace_coverage_evidence() -> None:
    """Web v2 不以 idle 次数或新增普通 Fact 替代覆盖证据和显式深测审阅。"""
    conn = make_conn()
    try:
        _seed_default_copy_project(conn)
        conn.execute(
            "UPDATE projects SET explore_deadline = ? WHERE id = 'dc1'",
            ("2999-01-01T00:00:00Z",),
        )
        common = {
            "kind": "endpoint",
            "surface_key": "GET https://example.com/profile",
            "surface_kind": "endpoint",
            "applicability_basis": "用户资料接口存在会话与授权边界",
            "completion_criteria": "完成基线和跨用户变体",
        }
        upsert_coverage_slots(
            conn,
            "dc1",
            [{**common, "test_family": "session"}],
            tier="core",
        )
        upsert_coverage_slots(
            conn,
            "dc1",
            [{**common, "test_family": "authorization"}],
            tier="extended",
        )
        core = [
            slot
            for slot in list_coverage_slots(conn, "dc1")
            if slot.tier == "core"
        ][0]
        conn.execute(
            """
            UPDATE coverage_slots
            SET status = 'explored', result = 'tested_no_finding'
            WHERE project_id = 'dc1' AND slot_key = ?
            """,
            (core.slot_key,),
        )

        assert advance_structured_convergence(conn, "dc1") is None
        assert advance_structured_convergence(conn, "dc1") is None
        assert advance_structured_convergence(conn, "dc1") is None
        conn.execute("INSERT INTO facts (id, project_id, description, creator, created_at) VALUES ('fnoise', 'dc1', 'unrelated new fact', 'reason', '2026-07-13T00:00:00Z')")
        assert advance_structured_convergence(conn, "dc1") is None

        project = conn.execute(
            """
            SELECT reason_idle_streak, coverage_stop_reason
            FROM projects WHERE id = 'dc1'
            """
        ).fetchone()
        extended = [
            slot
            for slot in list_coverage_slots(conn, "dc1")
            if slot.tier == "extended"
        ][0]
        assert project["reason_idle_streak"] == 0
        assert project["coverage_stop_reason"] is None
        assert extended.status == "pending"
        assert extended.skip_reason == ""
        with pytest.raises(HTTPException, match="assessment_depth_review_or_deep_work_pending"):
            project_completion.complete_project_record_with_conn(
                conn, "dc1", CompleteRequest(from_=["fsrc"], description="idle is not completion", worker="reason")
            )
    finally:
        conn.close()
