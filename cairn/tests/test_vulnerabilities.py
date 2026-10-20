"""漏洞管理端到端测试：自动确认 + verified 升级 + 独立创建 + 状态流转 + 统计。"""

from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3

import pytest
from fastapi import HTTPException

from cairn.server import db, migrations
from cairn.evidence_contracts import EvidenceItem, WorkerEvidenceItem, evidence_refs
from cairn.server.services.vulnerabilities import read_vulnerability_evidence
from cairn.server.models import (
    ConcludeRequest,
    ConcludeVulnerabilityInput,
    CreateVulnerabilityRequest,
    UpdateVulnerabilityRequest,
    VulnerabilityAssetIdentity,
    VulnerabilityEntrypointIdentity,
    VulnerabilityIdentityInput,
    VulnerabilityVectorIdentity,
)
from cairn.server.routers import intents as intents_router
from cairn.server.routers import vulnerabilities as vuln_router
from cairn.server.services import (
    create_vulnerability,
    confirm_vulnerability_for_slot,
    get_vulnerability_detail,
    get_vulnerability_provenance,
    list_vulnerabilities,
    register_discovered_vulnerability,
    update_vulnerability,
    vulnerabilities_summary,
)


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(conn, "sqlite")
    return conn


def _seed_project(conn: sqlite3.Connection, project_id: str = "p1") -> None:
    # 非 Web 漏洞管理场景只冻结 pi 执行契约；identity-only 不隐式启用 Web assessment。
    conn.execute(
        "INSERT OR IGNORE INTO scenarios (key, label, profile_id, prompt_group, knowledge, features_json) "
        "VALUES ('vulnerability-unit', 'Vulnerability unit', 'default', 'default', '', '{}')"
    )
    conn.execute(
        "INSERT INTO projects (id, title, status, created_at, scenario_key, scenario_snapshot_json) "
        "VALUES (?, ?, 'active', ?, 'vulnerability-unit', ?)",
        (project_id, "VulnTest", "2026-07-16T00:00:00Z", json.dumps({"features": {
            "execution_contract_version": 1, "execution_engine": "pi",
        }})),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES (?, ?, ?, 'system', ?)",
        ("fsrc", project_id, "origin", "2026-07-16T00:00:00Z"),
    )
    # 一个 open intent i001（from fsrc，待 conclude）
    conn.execute(
        "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) "
        "VALUES ('i001', ?, NULL, 'test vuln', 'reason', 'w', '2026-07-16T00:01:00Z', '2026-07-16T00:01:00Z', NULL)",
        (project_id,),
    )
    conn.execute(
        "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES ('i001', ?, 'fsrc')",
        (project_id,),
    )


def _conclude_run(conn: sqlite3.Connection, intent_id: str) -> str:
    run_id = f"run-{intent_id}"
    conn.execute(
        "INSERT INTO task_runs (id, project_id, intent_id, task_type, worker_name, worker_type, status, session_id, started_at) "
        "VALUES (?, 'p1', ?, 'explore', 'w', 'pi', 'running', ?, '2026-10-15T00:00:00Z')",
        (run_id, intent_id, f"session-{intent_id}"),
    )
    return run_id


def test_auto_confirm_vulnerability_on_conclude(monkeypatch: pytest.MonkeyPatch) -> None:
    """conclude 带 vulnerability 字段 → 漏洞自动入库，source_fact_id 绑定新 fact。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_project(conn)
        resp = intents_router.conclude(
            "p1", "i001",
            ConcludeRequest(
                worker="w",
                description="SQLi found in login",
                task_run_id=_conclude_run(conn, "i001"),
                evidence=[],
                vulnerability=ConcludeVulnerabilityInput(
                    schema_version=2,
                    evidence_items=[_capture_item(conn, "auto-request", "run-i001", "request")],
                    title="Login SQL Injection",
                    severity="high",
                    vuln_type="SQLi",
                    target="POST /api/login param:user",
                    remediation="use prepared statements",
                    confidence=0.7,
                ),
            ),
        )
        new_fact_id = resp.fact.id
        vulns = list_vulnerabilities(conn, "p1")
        assert len(vulns) == 1
        v = vulns[0]
        assert v.title == "Login SQL Injection"
        assert v.severity == "high"
        assert v.source_fact_id == new_fact_id
        assert v.confidence == 0.7
        assert v.status == "new"
        assert v.discovered_by_worker == "w"
        observation = conn.execute("SELECT * FROM vulnerability_observations WHERE vulnerability_id = ?", (v.id,)).fetchone()
        assert observation["source_fact_id"] == new_fact_id
        assert observation["task_run_id"] == "run-i001"
        assert v.evidence_items == []
        assert read_vulnerability_evidence(conn, "p1", v.id)["evidence_items"][0]["request"]["artifact_id"] == "auto-request"
        assert conn.execute("SELECT artifact_id FROM fact_evidence WHERE fact_id = ?", (new_fact_id,)).fetchone()["artifact_id"] == "auto-request"
    finally:
        conn.close()


def test_identity_only_concludes_share_canonical_vulnerability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_project(conn)
        conn.execute(
            "UPDATE projects SET vulnerability_fingerprint_version = 1 WHERE id = 'p1'"
        )
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, worker,
                last_heartbeat_at, created_at
            ) VALUES ('i002', 'p1', 'second', 'reason', 'w',
                      '2026-07-16T00:02:00Z', '2026-07-16T00:02:00Z')
            """
        )
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) "
            "VALUES ('i002', 'p1', 'fsrc')"
        )
        for intent_id, title in (("i001", "First title"), ("i002", "Second title")):
            intents_router.conclude(
                "p1",
                intent_id,
                ConcludeRequest(
                    worker="w",
                    description=title,
                    task_run_id=_conclude_run(conn, intent_id),
                    evidence=[],
                    vulnerability=ConcludeVulnerabilityInput(
                        schema_version=2, evidence_items=[],
                        title=title,
                        severity="high",
                        vuln_type="IDOR",
                        target="GET /api/orders/123",
                        confidence=0.8,
                        identity=_structured_identity(),
                    ),
                ),
            )

        project = conn.execute("SELECT * FROM projects WHERE id = 'p1'").fetchone()
        assert project["assessment_protocol"] in (None, "")
        assert len(list_vulnerabilities(conn, "p1")) == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM vulnerability_observations WHERE project_id = 'p1'"
        ).fetchone()["c"] == 2
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM intents "
            "WHERE project_id = 'p1' AND assessment_role = 'verifier'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM coverage_slots WHERE project_id = 'p1'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_identity_only_conclude_without_identity_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_project(conn)
        conn.execute(
            "UPDATE projects SET vulnerability_fingerprint_version = 1 WHERE id = 'p1'"
        )
        conn.commit()
        with pytest.raises(HTTPException) as exc:
            intents_router.conclude(
                "p1",
                "i001",
                ConcludeRequest(
                    worker="w",
                    description="missing identity",
                    task_run_id=_conclude_run(conn, "i001"),
                    evidence=[],
                    vulnerability=ConcludeVulnerabilityInput(
                        schema_version=2, evidence_items=[],
                        title="IDOR",
                        severity="high",
                        confidence=0.8,
                    ),
                ),
            )

        assert exc.value.status_code == 422
        assert exc.value.detail == "vulnerability identity is required"
        assert conn.execute("SELECT COUNT(*) AS c FROM facts").fetchone()["c"] == 1
        row = conn.execute("SELECT concluded_at FROM intents WHERE id = 'i001'").fetchone()
        assert row["concluded_at"] is None
        assert len(list_vulnerabilities(conn, "p1")) == 0
    finally:
        conn.close()


def test_pentest_verified_upgrades_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """pentest slot verified 时，同 slot 的漏洞升级 confidence=1.0 status=confirmed。"""
    conn = make_conn()
    try:
        _seed_project(conn)
        # 先登记一个漏洞（slot_key=endpoint:X，confidence=0.5，status=new）
        create_vulnerability(
            conn, "p1",
            CreateVulnerabilityRequest(
                title="IDOR", severity="high", vuln_type="IDOR",
                target="GET /api/orders", confidence=0.5, slot_key="endpoint:X",
                source_fact_id="fsrc",
            ),
            discovered_by_worker="w",
        )
        v = list_vulnerabilities(conn, "p1")[0]
        assert v.status == "new" and v.confidence == 0.5
        # slot verified → 升级 confirmed
        n = confirm_vulnerability_for_slot(conn, "p1", "endpoint:X")
        assert n == 1
        v2 = list_vulnerabilities(conn, "p1")[0]
        assert v2.status == "confirmed"
        assert v2.confidence == 1.0
    finally:
        conn.close()


def test_independent_create_without_source_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    """人工独立创建漏洞（无 source_fact_id）。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(vuln_router, "get_conn", fake_get_conn)
    try:
        _seed_project(conn)
        v = vuln_router.create_vulnerability_route(
            "p1",
            CreateVulnerabilityRequest(
                title="Manual finding", severity="medium", vuln_type="info-disclosure",
                target="/.git/config", remediation="限制访问", confidence=0.6,
            ),
        )
        assert v.source_fact_id is None
        assert v.status == "new"
        assert v.discovered_by_worker is None
    finally:
        conn.close()


def test_status_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    """漏洞状态流转：new → dismissed → remediated。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(vuln_router, "get_conn", fake_get_conn)
    try:
        _seed_project(conn)
        v = vuln_router.create_vulnerability_route(
            "p1", CreateVulnerabilityRequest(title="X", severity="low"),
        )
        v2 = vuln_router.update_vulnerability_route("p1", v.id, UpdateVulnerabilityRequest(status="dismissed"))
        assert v2.status == "dismissed"
        v3 = vuln_router.update_vulnerability_route("p1", v.id, UpdateVulnerabilityRequest(status="remediated", confidence=0.9))
        assert v3.status == "remediated"
        assert v3.confidence == 0.9
    finally:
        conn.close()


def test_vulnerabilities_summary() -> None:
    """统计：按 severity/status 聚合 + 总数 + per_project。"""
    conn = make_conn()
    try:
        _seed_project(conn)
        create_vulnerability(conn, "p1", CreateVulnerabilityRequest(title="a", severity="high", source_fact_id="fsrc"))
        create_vulnerability(conn, "p1", CreateVulnerabilityRequest(title="b", severity="critical", source_fact_id="fsrc"))
        create_vulnerability(conn, "p1", CreateVulnerabilityRequest(title="c", severity="low"))
        s = vulnerabilities_summary(conn)
        assert s["total"] == 3
        assert s["by_severity"].get("high") == 1
        assert s["by_severity"].get("critical") == 1
        assert s["by_status"].get("new") == 3
        assert any(p["project_id"] == "p1" for p in s["per_project"])
    finally:
        conn.close()


def test_invalid_severity_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法 severity 在 pydantic 模型层被拒（ValidationError）。"""
    conn = make_conn()
    try:
        _seed_project(conn)
        with pytest.raises(Exception):
            CreateVulnerabilityRequest(title="X", severity="extreme")
    finally:
        conn.close()


def _structured_identity(
    vulnerability_class: str = "IDOR",
    *,
    protocol: str = "HTTPS",
    port: int | None = None,
    route: str = "/api/orders/123",
    parameter: str = "order_id",
    source_role: str = "user",
    target_role: str = "other_user",
    state: str = "",
) -> VulnerabilityIdentityInput:
    return VulnerabilityIdentityInput(
        vulnerability_class=vulnerability_class,
        asset=VulnerabilityAssetIdentity(
            protocol=protocol,
            host="Example.COM.",
            port=port,
        ),
        entrypoint=VulnerabilityEntrypointIdentity(
            method="get",
            route=route,
        ),
        vector=VulnerabilityVectorIdentity(
            parameter=parameter,
            object_type="order",
            source_role=source_role,
            target_role=target_role,
            state=state,
        ),
    )


def _capture_item(conn: sqlite3.Connection, artifact_id: str, run_id: str, side: str) -> EvidenceItem:
    conn.execute(
        "INSERT OR IGNORE INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, session_id, started_at) "
        "VALUES (?, 'p1', 'explore', 'w', 'pi', 'running', ?, '2026-10-15T00:00:00Z')",
        (run_id, f"session-{run_id}"),
    )
    conn.execute(
        "INSERT INTO artifacts (id, project_id, task_run_id, kind, category, uri, sha256, size, created_at) "
        "VALUES (?, 'p1', ?, 'file', 'evidence', ?, 'digest', 1, '2026-10-15T00:00:00Z')",
        (artifact_id, run_id, f"artifact://projects/p1/runs/{run_id}/{artifact_id}.txt"),
    )
    return EvidenceItem.model_validate({
        "id": artifact_id, "kind": "http", "source": "capture", side: {"artifact_id": artifact_id},
    })


def test_structured_vulnerability_dedupes_alias_and_keeps_observations() -> None:
    """不同标题和 IDOR/BOLA 叫法合并 canonical，但每次发现都保留 observation。"""
    conn = make_conn()
    try:
        _seed_project(conn)
        conn.execute(
            "UPDATE projects SET assessment_protocol = 'default-copy-v1', vulnerability_fingerprint_version = 1 WHERE id = 'p1'"
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES ('f002', 'p1', 'second observation', 'w2', '2026-07-16T00:02:00Z')"
        )
        first = register_discovered_vulnerability(
            conn,
            "p1",
            CreateVulnerabilityRequest(
                title="可越权读取他人订单",
                severity="medium",
                vuln_type="IDOR",
                target="GET /api/orders/123",
                confidence=0.6,
                source_fact_id="fsrc",
                evidence_items=[_capture_item(conn, "request-a", "tr1", "request")],
            ),
            _structured_identity("IDOR"),
            fingerprint_version=1,
            discovered_by_run_id="tr1",
            discovered_by_worker="w1",
        )
        update_vulnerability(
            conn,
            "p1",
            first.id,
            UpdateVulnerabilityRequest(status="dismissed"),
        )
        second = register_discovered_vulnerability(
            conn,
            "p1",
            CreateVulnerabilityRequest(
                title="BOLA on order endpoint",
                severity="high",
                vuln_type="BOLA",
                target="same endpoint, different title",
                confidence=0.9,
                source_fact_id="f002",
                evidence_items=[_capture_item(conn, "response-b", "tr2", "response")],
            ),
            _structured_identity("BOLA"),
            fingerprint_version=1,
            discovered_by_run_id="tr2",
            discovered_by_worker="w2",
        )

        vulns = list_vulnerabilities(conn, "p1")
        observations = conn.execute(
            """
            SELECT * FROM vulnerability_observations
            WHERE project_id = 'p1' AND vulnerability_id = ?
            ORDER BY observed_at, id
            """,
            (first.id,),
        ).fetchall()
        assert second.id == first.id
        assert len(vulns) == 1
        assert len(observations) == 2
        assert vulns[0].severity == "high"
        assert vulns[0].confidence == 0.9
        assert vulns[0].status == "dismissed"
        assert vulns[0].fingerprint
        assert vulns[0].identity["vulnerability_class"] == (
            "broken_object_level_authorization"
        )
        assert vulns[0].identity["asset"] == {
            "protocol": "https",
            "host": "example.com",
            "port": 443,
        }
        observations_by_run = {row["task_run_id"]: row for row in observations}
        first_observation = observations_by_run["tr1"]
        second_observation = observations_by_run["tr2"]
        conn.execute("UPDATE vulnerability_observations SET observed_at = '2026-10-15T00:01:00Z' WHERE id = ?", (first_observation["id"],))
        conn.execute("UPDATE vulnerability_observations SET observed_at = '2026-10-15T00:02:00Z' WHERE id = ?", (second_observation["id"],))
        latest = read_vulnerability_evidence(conn, "p1", first.id)
        assert latest["observation_id"] == second_observation["id"]
        assert latest["evidence_items"][0]["response"]["artifact_id"] == "response-b"
        assert "request" not in latest["evidence_items"][0]
        selected = read_vulnerability_evidence(conn, "p1", first.id, first_observation["id"])
        assert selected["evidence_items"][0]["request"]["artifact_id"] == "request-a"
        assert "response" not in selected["evidence_items"][0]
        assert vulns[0].evidence_items == []
        assert all(row["repro_request"] == row["repro_response"] == "" for row in observations)
        from cairn.server.services.vulnerability_verifications import get_observation_verifier_target
        target = get_observation_verifier_target(conn, "p1", first_observation["id"])
        assert target.neutral_projection["evidence_items"] == selected["evidence_items"]
        assert target.neutral_projection["evidence_refs"][0]["artifact_id"] == "request-a"
        assert "repro_request" not in target.neutral_projection
        from cairn.server.services.assessment_reports import _report_discovery_rows
        report = _report_discovery_rows(conn, "p1")[0]
        assert report["evidence_items"] == latest["evidence_items"]
        assert report["record_evidence_items"] == []
        assert not {"repro_request", "repro_response", "packet_artifact_id"}.intersection(report)
        detail = get_vulnerability_detail(conn, "p1", first.id)
        assert len(detail.observations) == 2
        assert detail.source_fact.id == "f002"
        assert get_vulnerability_detail(conn, "p1", first.id, first_observation["id"]).source_fact.id == "fsrc"
        from cairn.server.services.vulnerabilities import get_vulnerability_for_fact
        assert get_vulnerability_for_fact(conn, "p1", "f002").id == first.id
        assert {item.task_run_id for item in detail.observations} == {"tr1", "tr2"}
    finally:
        conn.close()


def test_canonical_vulnerability_aggregates_severity_and_confidence_independently() -> None:
    conn = make_conn()
    try:
        _seed_project(conn)
        conn.execute(
            "INSERT INTO facts (id, project_id, description, creator, created_at) "
            "VALUES ('f002', 'p1', 'second', 'w', '2026-07-16T00:02:00Z')"
        )
        first = register_discovered_vulnerability(
            conn,
            "p1",
            CreateVulnerabilityRequest(
                title="First canonical title",
                severity="critical",
                confidence=0.4,
                source_fact_id="fsrc",
            ),
            _structured_identity(),
            fingerprint_version=1,
        )
        second = register_discovered_vulnerability(
            conn,
            "p1",
            CreateVulnerabilityRequest(
                title="Later title",
                severity="medium",
                confidence=0.95,
                source_fact_id="f002",
            ),
            _structured_identity(),
            fingerprint_version=1,
        )

        assert second.id == first.id
        canonical = list_vulnerabilities(conn, "p1")[0]
        assert canonical.title == "First canonical title"
        assert canonical.source_fact_id == "fsrc"
        assert canonical.severity == "critical"
        assert canonical.confidence == 0.95
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM vulnerability_observations "
            "WHERE project_id = 'p1' AND vulnerability_id = ?",
            (first.id,),
        ).fetchone()["c"] == 2
    finally:
        conn.close()


@pytest.mark.parametrize(
    "changed_identity",
    [
        {"parameter": "user_id"},
        {"source_role": "support"},
        {"target_role": "admin"},
        {"state": "approved"},
        {"protocol": "http"},
        {"port": 8443},
        {"route": "/api/orders/456"},
    ],
)
def test_structured_vulnerability_keeps_distinct_identity_fields(
    changed_identity: dict[str, object],
) -> None:
    """稳定身份字段任一不同，即使标题相同也不得误合并。"""
    conn = make_conn()
    try:
        _seed_project(conn)
        conn.execute(
            "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES ('f002', 'p1', 'second', 'w', '2026-07-16T00:02:00Z')"
        )
        for fact_id, identity in (
            ("fsrc", _structured_identity()),
            ("f002", _structured_identity(**changed_identity)),
        ):
            register_discovered_vulnerability(
                conn,
                "p1",
                CreateVulnerabilityRequest(
                    title="IDOR",
                    severity="high",
                    source_fact_id=fact_id,
                ),
                identity,
                fingerprint_version=1,
            )

        assert len(list_vulnerabilities(conn, "p1")) == 2
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM vulnerability_observations WHERE project_id = 'p1'"
        ).fetchone()["c"] == 2
    finally:
        conn.close()


def test_legacy_creation_keeps_nullable_fingerprint_duplicates() -> None:
    """非去重自动发现保持独立 canonical，同时追加真实 Observation。"""
    conn = make_conn()
    try:
        _seed_project(conn)
        body = CreateVulnerabilityRequest(
            title="same title",
            severity="medium",
            source_fact_id="fsrc",
        )
        first = create_vulnerability(conn, "p1", body)
        second = create_vulnerability(conn, "p1", body)

        assert first.id != second.id
        vulns = list_vulnerabilities(conn, "p1")
        assert len(vulns) == 2
        assert all(vuln.fingerprint is None for vuln in vulns)
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM vulnerability_observations"
        ).fetchone()["c"] == 2
    finally:
        conn.close()


def test_structured_fingerprint_is_scoped_to_project() -> None:
    """相同语义身份在不同项目中必须形成独立 canonical 成果。"""
    conn = make_conn()
    try:
        _seed_project(conn, "p1")
        _seed_project(conn, "p2")
        first = register_discovered_vulnerability(
            conn,
            "p1",
            CreateVulnerabilityRequest(
                title="IDOR p1",
                severity="high",
                source_fact_id="fsrc",
            ),
            _structured_identity(),
            fingerprint_version=1,
        )
        second = register_discovered_vulnerability(
            conn,
            "p2",
            CreateVulnerabilityRequest(
                title="IDOR p2",
                severity="high",
                source_fact_id="fsrc",
            ),
            _structured_identity(),
            fingerprint_version=1,
        )

        assert first.project_id == "p1"
        assert second.project_id == "p2"
        assert first.fingerprint == second.fingerprint
        assert len(list_vulnerabilities(conn, "p1")) == 1
        assert len(list_vulnerabilities(conn, "p2")) == 1
    finally:
        conn.close()


def _seed_vulnerability_review_record(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    vulnerability_id: str,
    observation_id: str,
    verification_id: str,
    suffix: str,
    outcome: str,
    independence_level: str,
) -> None:
    intent_id = f"verify-{suffix}"
    task_run_id = f"verify-run-{suffix}"
    fact_id = f"verify-fact-{suffix}"
    conn.execute(
        """
        INSERT INTO vulnerability_observations (
            id, project_id, vulnerability_id, source_fact_id, task_run_id,
            worker, identity_json, severity, confidence, observed_at
        ) VALUES (?, ?, ?, 'fsrc', 'discovery-run', 'discoverer', '{}',
                  'high', 0.8, '2026-08-17T00:03:00Z')
        """,
        (observation_id, project_id, vulnerability_id),
    )
    conn.execute(
        """
        INSERT INTO intents (
            id, project_id, description, creator, created_at,
            assessment_role, target_observation_id, verifier_schedule_key
        ) VALUES (?, ?, 'verify', 'system', '2026-08-17T00:04:00Z',
                  'verifier', ?, ?)
        """,
        (intent_id, project_id, observation_id, observation_id),
    )
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, task_type, worker_name, worker_type, status,
            started_at, ended_at
        ) VALUES (?, ?, 'verify', ?, 'claudecode', 'succeeded',
                  '2026-08-17T00:04:00Z', '2026-08-17T00:05:00Z')
        """,
        (task_run_id, project_id, f"worker-{suffix}"),
    )
    conn.execute(
        """
        INSERT INTO facts (
            id, project_id, description, creator, task_run_id, created_at
        ) VALUES (?, ?, ?, ?, ?, '2026-08-17T00:05:00Z')
        """,
        (
            fact_id,
            project_id,
            f"verification {suffix}",
            f"worker-{suffix}",
            task_run_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO vulnerability_verifications (
            id, project_id, vulnerability_id, observation_id, intent_id,
            fact_id, verifier_task_run_id, verifier_worker,
            discovery_task_run_id, discovery_worker, independence_level,
            outcome, evidence_json, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'discovery-run', 'discoverer',
                  ?, ?, ?, ?, '2026-08-17T00:06:00Z')
        """,
        (
            verification_id,
            project_id,
            vulnerability_id,
            observation_id,
            intent_id,
            fact_id,
            task_run_id,
            f"worker-{suffix}",
            independence_level,
            outcome,
            json.dumps(
                [
                    {
                        "artifact_id": f"artifact-{suffix}",
                        "pointer": f"/response/{suffix}",
                        "verifier_run_id": task_run_id,
                    }
                ]
            ),
            f"notes {suffix}",
        ),
    )


def test_vulnerability_detail_preserves_ordered_project_scoped_verifications() -> None:
    """详情按稳定顺序返回 append-only Verification，并保留 Evidence pointer。"""
    conn = make_conn()
    try:
        _seed_project(conn, "p1")
        _seed_project(conn, "p2")
        vulnerability = create_vulnerability(
            conn,
            "p1",
            CreateVulnerabilityRequest(
                title="IDOR",
                severity="high",
                source_fact_id="fsrc",
            ),
        )
        conn.execute(
            """
            INSERT INTO vulnerabilities (
                id, project_id, title, severity, source_fact_id,
                created_at, updated_at
            ) VALUES (?, 'p2', 'other project', 'high', 'fsrc', ?, ?)
            """,
            (
                vulnerability.id,
                "2026-08-17T00:01:00Z",
                "2026-08-17T00:01:00Z",
            ),
        )
        for values in (
            {
                "project_id": "p1",
                "vulnerability_id": vulnerability.id,
                "observation_id": "o2",
                "verification_id": "vv2",
                "suffix": "2",
                "outcome": "not_reproduced",
                "independence_level": "same_worker_new_session",
            },
            {
                "project_id": "p1",
                "vulnerability_id": vulnerability.id,
                "observation_id": "o1",
                "verification_id": "vv1",
                "suffix": "1",
                "outcome": "confirmed",
                "independence_level": "different_worker",
            },
            {
                "project_id": "p2",
                "vulnerability_id": vulnerability.id,
                "observation_id": "o-other",
                "verification_id": "vv0",
                "suffix": "other",
                "outcome": "confirmed",
                "independence_level": "different_worker",
            },
        ):
            _seed_vulnerability_review_record(conn, **values)

        detail = get_vulnerability_detail(conn, "p1", vulnerability.id)

        assert [item.id for item in detail.observations][:2] == ["o1", "o2"]
        assert len(detail.observations) == 3
        assert [item.id for item in detail.verifications] == ["vv1", "vv2"]
        assert [
            (
                item.observation_id,
                item.outcome,
                item.independence_level,
                item.notes,
                item.evidence[0].model_dump(),
            )
            for item in detail.verifications
        ] == [
            (
                "o1",
                "confirmed",
                "different_worker",
                "notes 1",
                {
                    "artifact_id": "artifact-1",
                    "pointer": "/response/1",
                    "verifier_run_id": "verify-run-1",
                },
            ),
            (
                "o2",
                "not_reproduced",
                "same_worker_new_session",
                "notes 2",
                {
                    "artifact_id": "artifact-2",
                    "pointer": "/response/2",
                    "verifier_run_id": "verify-run-2",
                },
            ),
        ]
    finally:
        conn.close()


def test_vulnerability_provenance_aggregates_project_scoped_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """溯源按 Observation 聚合完整发现/验证链，并隔离同 ID 的其他项目记录。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(vuln_router, "get_conn", fake_get_conn)
    try:
        _seed_project(conn, "p1")
        _seed_project(conn, "p2")
        for project_id, worker in (("p1", "discoverer"), ("p2", "other-worker")):
            conn.execute(
                """
                INSERT INTO intents (
                    id, project_id, to_fact_id, description, creator, worker,
                    task_run_id, last_heartbeat_at, created_at, concluded_at,
                    conclusion
                ) VALUES ('discover-intent', ?, 'finding-fact', 'discover',
                          'reason', ?, 'discovery-run', ?, ?, ?, 'completed')
                """,
                (
                    project_id,
                    worker,
                    "2026-08-25T00:01:00Z",
                    "2026-08-25T00:01:00Z",
                    "2026-08-25T00:02:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO intent_sources (
                    intent_id, project_id, fact_id, source_order
                ) VALUES ('discover-intent', ?, 'fsrc', 0)
                """,
                (project_id,),
            )
            conn.execute(
                """
                INSERT INTO task_runs (
                    id, project_id, intent_id, task_type, worker_name,
                    worker_type, status, session_id, started_at, ended_at
                ) VALUES ('discovery-run', ?, 'discover-intent', 'explore', ?,
                          'claudecode', 'succeeded', ?, ?, ?)
                """,
                (
                    project_id,
                    worker,
                    f"session-{project_id}",
                    "2026-08-25T00:01:00Z",
                    "2026-08-25T00:02:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO facts (
                    id, project_id, description, task_run_id, creator, created_at
                ) VALUES ('finding-fact', ?, ?, 'discovery-run', ?, ?)
                """,
                (
                    project_id,
                    f"finding {project_id}",
                    worker,
                    "2026-08-25T00:02:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO artifacts (
                    id, project_id, task_run_id, kind, category, uri, sha256,
                    size, created_at, metadata_json
                ) VALUES ('packet-artifact', ?, 'discovery-run', 'har',
                          'evidence', ?, 'abc', 10, ?, '{}')
                """,
                (
                    project_id,
                    f"artifact://{project_id}/packet.har",
                    "2026-08-25T00:02:00Z",
                ),
            )

        vulnerability = create_vulnerability(
            conn,
            "p1",
            CreateVulnerabilityRequest(
                title="IDOR",
                severity="high",
                source_fact_id="finding-fact",
                evidence_items=[EvidenceItem(id="original", kind="artifact", source="capture", artifact={"artifact_id": "packet-artifact"})],
            ),
            discovered_by_run_id="discovery-run",
            discovered_by_worker="discoverer",
        )
        conn.execute(
            """
            INSERT INTO vulnerabilities (
                id, project_id, title, severity, source_fact_id,
                packet_artifact_id, discovered_by_run_id, created_at, updated_at
            ) VALUES (?, 'p2', 'other project', 'low', 'finding-fact',
                      'packet-artifact', 'discovery-run', ?, ?)
            """,
            (
                vulnerability.id,
                "2026-08-25T00:02:00Z",
                "2026-08-25T00:02:00Z",
            ),
        )
        conn.execute(
            "UPDATE vulnerability_observations SET id = 'observation-1', confidence = 0.9, observed_at = '2026-08-25T00:02:00Z' "
            "WHERE project_id = 'p1' AND vulnerability_id = ?",
            (vulnerability.id,),
        )
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, to_fact_id, description, creator, worker,
                task_run_id, created_at, concluded_at, conclusion,
                assessment_role, target_observation_id, verifier_schedule_key
            ) VALUES ('verify-intent', 'p1', 'verify-fact', 'verify', 'system',
                      'verifier', 'verify-run', '2026-08-25T00:03:00Z',
                      '2026-08-25T00:04:00Z', 'completed', 'verifier',
                      'observation-1', 'observation-1')
            """
        )
        conn.execute(
            """
            INSERT INTO intent_sources (
                intent_id, project_id, fact_id, source_order
            ) VALUES ('verify-intent', 'p1', 'finding-fact', 0)
            """
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, intent_id, task_type, worker_name, worker_type,
                status, session_id, started_at, ended_at
            ) VALUES ('verify-run', 'p1', 'verify-intent', 'verify', 'verifier',
                      'claudecode', 'succeeded', 'verify-session',
                      '2026-08-25T00:03:00Z', '2026-08-25T00:04:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO facts (
                id, project_id, description, task_run_id, creator, created_at
            ) VALUES ('verify-fact', 'p1', 'confirmed', 'verify-run',
                      'verifier', '2026-08-25T00:04:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO artifacts (
                id, project_id, task_run_id, kind, category, uri, sha256, size,
                created_at, metadata_json
            ) VALUES ('verify-artifact', 'p1', 'verify-run', 'raw_output',
                      'evidence', 'artifact://p1/verify.txt', 'def', 12,
                      '2026-08-25T00:04:00Z', '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO fact_evidence (
                project_id, fact_id, artifact_id, pointer, verifier_run_id,
                created_at
            ) VALUES ('p1', 'finding-fact', 'packet-artifact', '/request',
                      'discovery-run', '2026-08-25T00:02:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO vulnerability_verifications (
                id, project_id, vulnerability_id, observation_id, intent_id,
                fact_id, verifier_task_run_id, verifier_worker,
                discovery_task_run_id, discovery_worker, independence_level,
                outcome, evidence_json, notes, created_at
            ) VALUES ('verification-1', 'p1', ?, 'observation-1',
                      'verify-intent', 'verify-fact', 'verify-run', 'verifier',
                      'discovery-run', 'discoverer', 'different_worker',
                      'confirmed', ?, 'reproduced', '2026-08-25T00:04:00Z')
            """,
            (
                vulnerability.id,
                json.dumps(
                    [
                        {
                            "artifact_id": "verify-artifact",
                            "pointer": "/response",
                            "verifier_run_id": "verify-run",
                        }
                    ]
                ),
            ),
        )

        provenance = vuln_router.get_vulnerability_provenance_route(
            "p1", vulnerability.id
        )

        assert provenance.vulnerability.project_id == "p1"
        assert [item.id for item in provenance.observations] == ["observation-1"]
        assert [item.id for item in provenance.verifications] == ["verification-1"]
        assert {item.id for item in provenance.facts} == {
            "fsrc",
            "finding-fact",
            "verify-fact",
        }
        assert {item.id for item in provenance.intents} == {
            "discover-intent",
            "verify-intent",
        }
        assert {item.id for item in provenance.task_runs} == {
            "discovery-run",
            "verify-run",
        }
        assert {item.id for item in provenance.artifacts} == {
            "packet-artifact",
            "verify-artifact",
        }
        assert [item.pointer for item in provenance.evidence] == ["/request"]
        assert provenance.completeness_warnings == []
        assert (
            "observation-1",
            "finding-fact",
            "sourced_from_fact",
            "vulnerability_observations.source_fact_id",
        ) in {
            (item.source_id, item.target_id, item.relation, item.source)
            for item in provenance.relations
        }
        assert all(item.project_id == "p1" for item in provenance.task_runs)
        assert all(item.project_id == "p1" for item in provenance.artifacts)
    finally:
        conn.close()


def test_evidence_contract_projection_and_inline_boundaries() -> None:
    from pydantic import ValidationError

    from cairn.dispatcher.contracts import PhaseContractError, extract_evidence_refs

    raw = {"id": "step", "kind": "http", "source": "capture", "request": {"artifact_handle": "h-request"}}
    item = WorkerEvidenceItem.model_validate(raw)
    assert [ref.artifact_handle for ref in evidence_refs([item, item])] == ["h-request"]
    assert item.response is None
    finding = {"title": "capture", "severity": "high", "schema_version": 2, "evidence_items": [raw]}
    for section in (None, "fact"):
        payload = {"fact": {"vulnerability": finding}}
        assert [ref.artifact_id for ref in extract_evidence_refs(payload, section=section)] == ["h-request"]
        for duplicate in (
            {"fact": {"vulnerability": finding, "evidence_refs": []}},
            {"fact": {"vulnerability": finding}, "evidence_refs": []},
        ):
            with pytest.raises(PhaseContractError, match="projected from evidence_items"):
                extract_evidence_refs(duplicate, section=section)
    with pytest.raises(ValidationError):
        WorkerEvidenceItem.model_validate({**raw, "request_text": "GET /\r\n"})
    with pytest.raises(ValidationError):
        EvidenceItem(id="bad", kind="http", source="capture", response_text="")
    for model in (CreateVulnerabilityRequest, UpdateVulnerabilityRequest):
        with pytest.raises(ValidationError):
            model.model_validate({"title": "old", "severity": "low", "repro_request": "GET /"})
    with pytest.raises(ValidationError):
        ConcludeVulnerabilityInput(title="old", severity="low", schema_version=2, evidence_items=[], packet_artifact_id="a1")


def test_manual_evidence_edit_preserves_history_without_fabricated_observation() -> None:
    conn = make_conn()
    try:
        _seed_project(conn)
        original = EvidenceItem(id="manual", kind="http", source="manual", request_text="GET /\r\n\r\n  ")
        vulnerability = create_vulnerability(conn, "p1", CreateVulnerabilityRequest(title="manual", severity="low", evidence_items=[original]))
        assert get_vulnerability_detail(conn, "p1", vulnerability.id).observations == []
        with pytest.raises(HTTPException, match="automatic_vulnerability_requires_conclude"):
            vuln_router.create_vulnerability_route("p1", CreateVulnerabilityRequest(title="not manual", severity="low", source_fact_id="fsrc"))
        assert read_vulnerability_evidence(conn, "p1", vulnerability.id)["evidence_items"][0]["request_text"] == original.request_text
        history = EvidenceItem(id="legacy", kind="text", source="historical", text="unpaired\r\n \t")
        missing = EvidenceItem(id="lost-original", kind="artifact", source="historical", artifact={"artifact_id": "missing-legacy"})
        conn.execute("UPDATE vulnerabilities SET evidence_items_json = ? WHERE id = ?", (json.dumps([item.model_dump(exclude_none=True) for item in [history, missing]]), vulnerability.id))
        with pytest.raises(HTTPException, match="historical_evidence_is_immutable"):
            update_vulnerability(conn, "p1", vulnerability.id, UpdateVulnerabilityRequest(evidence_items=[original]))
        updated = update_vulnerability(conn, "p1", vulnerability.id, UpdateVulnerabilityRequest(evidence_items=[history, missing, original]))
        assert updated.evidence_items == [history, missing, original]
        assert get_vulnerability_detail(conn, "p1", vulnerability.id).record_evidence_items == [history, missing, original]
        assert conn.execute("SELECT count(*) FROM vulnerability_observations").fetchone()[0] == 0
    finally:
        conn.close()


def test_explicit_evidence_selection_is_vulnerability_and_project_scoped() -> None:
    conn = make_conn()
    try:
        _seed_project(conn)
        _seed_project(conn, "p2")
        first = create_vulnerability(conn, "p1", CreateVulnerabilityRequest(title="first", severity="low", source_fact_id="fsrc"))
        second = create_vulnerability(conn, "p1", CreateVulnerabilityRequest(title="second", severity="low", source_fact_id="fsrc"))
        create_vulnerability(conn, "p2", CreateVulnerabilityRequest(title="other", severity="low"))
        observation = read_vulnerability_evidence(conn, "p1", first.id)["observation_id"]
        for project_id, vuln_id in (("p1", second.id), ("p2", first.id)):
            with pytest.raises(HTTPException) as error:
                read_vulnerability_evidence(conn, project_id, vuln_id, observation)
            assert error.value.status_code == 404
    finally:
        conn.close()


def test_v53_migration_preserves_originals_without_pairing_canonical_sides(monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(db, "SQLITE_MIGRATIONS", tuple(item for item in migrations.SQLITE_MIGRATIONS if item.version <= 52))
        conn = make_conn()
    try:
        _seed_project(conn)
        request, response = "GET /old\r\nX: value  \r\n\r\n \t", "HTTP/1.1 200 OK\r\n\r\nbody \r\n"
        for vuln_id, fact_id in (("legacy", "fsrc"), ("manual", None)):
            conn.execute(
                "INSERT INTO vulnerabilities (id, project_id, title, severity, source_fact_id, repro_request, repro_response, created_at, updated_at) "
                "VALUES (?, 'p1', 'old', 'high', ?, ?, ?, 'old', 'old')", (vuln_id, fact_id, request, response),
            )
        conn.execute(
            "INSERT INTO vulnerability_observations (id, project_id, vulnerability_id, source_fact_id, severity, confidence, repro_request, observed_at) "
            "VALUES ('old-observation', 'p1', 'legacy', 'fsrc', 'high', 0.5, ?, 'old')", (request,),
        )
        old_registry = [dict(row) for row in conn.execute("SELECT * FROM schema_migrations ORDER BY version")]
        conn.commit()
        db.run_migrations(conn, "sqlite")
        conn.commit()
        canonical = conn.execute("SELECT * FROM vulnerabilities WHERE id = 'legacy'").fetchone()
        items = json.loads(canonical["evidence_items_json"])
        assert [item["kind"] for item in items] == ["text", "text"]
        assert [item["text"] for item in items] == [request, response]
        assert canonical["repro_request"] == request and canonical["repro_response"] == response
        selected = read_vulnerability_evidence(conn, "p1", "legacy")
        assert selected["source"] == "historical"
        assert selected["evidence_items"][0]["request_text"] == request
        assert "response_text" not in selected["evidence_items"][0]
        manual = read_vulnerability_evidence(conn, "p1", "manual")
        assert manual["observation_id"] is None and manual["source"] == "manual"
        assert manual["evidence_items"][0]["response_text"] == response
        assert [dict(row) for row in conn.execute("SELECT * FROM schema_migrations WHERE version <= 52 ORDER BY version")] == old_registry
        db.run_migrations(conn, "sqlite")
        assert conn.execute("SELECT count(*) FROM schema_migrations WHERE version = 53").fetchone()[0] == 1
    finally:
        conn.close()


def test_historical_conclude_only_replays_existing_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    from hashlib import sha256
    from cairn.server.schemas.projects import HistoricalConcludeVulnerabilityInput

    conn = make_conn()
    @contextmanager
    def fake_get_conn():
        yield conn
    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed_project(conn)
        body = ConcludeRequest(worker="w", description="accepted", task_run_id=_conclude_run(conn, "i001"), vulnerability=HistoricalConcludeVulnerabilityInput(title="historical", severity="low", repro_request="GET /old\r\n"))
        with pytest.raises(HTTPException) as error:
            intents_router.conclude("p1", "i001", body)
        assert error.value.detail == "vulnerability_schema_version_2_required"
        serialized = body.model_dump(by_alias=True, exclude_none=True)
        assert "evidence_items" not in serialized["vulnerability"]
        assert "schema_version" not in serialized["vulnerability"]
        serialized["vulnerability"]["repro_request"] = "GET /old"
        digest = sha256(json.dumps(serialized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        conn.execute("INSERT INTO facts (id, project_id, description, creator, created_at) VALUES ('accepted', 'p1', 'accepted', 'w', 'old')")
        conn.execute("UPDATE intents SET to_fact_id = 'accepted', concluded_at = 'old' WHERE id = 'i001'")
        context = json.dumps({"accepted_result_digest": digest, "accepted_fact_id": "accepted"})
        conn.execute("UPDATE task_runs SET runtime_context_json = ? WHERE id = ?", (context, body.task_run_id))
        assert intents_router.conclude("p1", "i001", body).fact.id == "accepted"
        assert body.vulnerability.repro_request == "GET /old\r\n"
        assert conn.execute("SELECT runtime_context_json FROM task_runs WHERE id = ?", (body.task_run_id,)).fetchone()[0] == context
        assert conn.execute("SELECT count(*) FROM vulnerability_observations").fetchone()[0] == 0
    finally:
        conn.close()


def test_vulnerability_provenance_reports_incomplete_history() -> None:
    """历史人工漏洞仍可读取，并显式报告没有 Observation 与缺失关联。"""
    conn = make_conn()
    try:
        _seed_project(conn)
        vulnerability = create_vulnerability(
            conn,
            "p1",
            CreateVulnerabilityRequest(title="Manual", severity="medium"),
        )
        conn.execute(
            """
            UPDATE vulnerabilities
            SET discovered_by_run_id = 'missing-run'
            WHERE id = ? AND project_id = 'p1'
            """,
            (vulnerability.id,),
        )

        provenance = get_vulnerability_provenance(conn, "p1", vulnerability.id)

        assert provenance.observations == []
        assert provenance.relations[0].target_id == "missing-run"
        assert provenance.completeness_warnings == [
            f"Vulnerability {vulnerability.id} has no discovery observations.",
            f"Vulnerability {vulnerability.id} references missing task run missing-run.",
        ]
        with pytest.raises(HTTPException) as missing:
            get_vulnerability_provenance(conn, "p1", "v-missing")
        assert missing.value.status_code == 404
    finally:
        conn.close()
