"""漏洞管理端到端测试：自动确认 + verified 升级 + 独立创建 + 状态流转 + 统计。"""

from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3

import pytest
from fastapi import HTTPException

from cairn.server import db
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
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn)
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
                evidence=[],
                vulnerability=ConcludeVulnerabilityInput(
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
                    evidence=[],
                    vulnerability=ConcludeVulnerabilityInput(
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
                    evidence=[],
                    vulnerability=ConcludeVulnerabilityInput(
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
                repro_request="GET /api/orders/123",
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
                repro_response="HTTP/1.1 200 OK",
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
        assert observations_by_run["tr1"]["repro_request"] == (
            "GET /api/orders/123"
        )
        assert observations_by_run["tr2"]["repro_response"] == (
            "HTTP/1.1 200 OK"
        )
        detail = get_vulnerability_detail(conn, "p1", first.id)
        assert len(detail.observations) == 2
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
    """人工与 legacy 路径不启用语义合并，保持既有重复创建行为。"""
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
        ).fetchone()["c"] == 0
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

        assert [item.id for item in detail.observations] == ["o1", "o2"]
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
                packet_artifact_id="packet-artifact",
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
            """
            INSERT INTO vulnerability_observations (
                id, project_id, vulnerability_id, source_fact_id, task_run_id,
                worker, packet_artifact_id, identity_json, severity,
                confidence, observed_at
            ) VALUES ('observation-1', 'p1', ?, 'finding-fact',
                      'discovery-run', 'discoverer', 'packet-artifact', '{}',
                      'high', 0.9, '2026-08-25T00:02:00Z')
            """,
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
