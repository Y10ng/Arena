from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from cairn.server import db, migrations
from cairn.server.auth import RequestPrincipal
from cairn.server.schemas.projects import AssessmentReportGenerationRequest
from cairn.server.services import assessment_attempts, assessment_reports, report_templates
from cairn.server.services.report_inventory import list_report_inventory
from cairn.server.services.report_policy import parse_report_execution_plan


@pytest.fixture
def pg_conn():
    # 只接受显式测试实例；不读取应用配置，更不复用活动数据库。
    url = os.environ.get("CAIRN_TEST_REPORT_TEMPLATES_POSTGRES_URL")
    if not url:
        pytest.skip("requires an isolated PostgreSQL test instance")
    name = f"report_templates_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        raw = psycopg.connect(url, dbname=name, row_factory=dict_row)
        conn = db.PostgresConnection(raw)
        try:
            migrations.ensure_registry(conn)
            for migration in migrations.POSTGRES_MIGRATIONS:
                if migration.version < 58:
                    migrations.verify_or_apply(conn, migration, lambda _conn: None)
            conn.commit()
            yield conn
        finally:
            conn.close()
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def _migrate(conn):
    migrations.verify_or_apply(conn, migrations.POSTGRES_CURRENT_REPORT_TEMPLATES, lambda _conn: None)


def _seed_project(conn):
    snapshot = {
        "key": "goal-driven-v1", "prompt_group": "goal-driven-v1", "profile_id": "default",
        "features": {
            "execution_contract_version": 1, "execution_engine": "pi",
            "assessment_contract_version": 2, "assessment_protocol": "goal-driven-v1",
            "outcome_contract_version": 2, "verifier_contract_version": 1,
            "report_policy_version": "1", "report_prompt_group": "goal-driven-v1",
        },
    }
    conn.execute(
        """INSERT INTO projects (id, title, status, created_at, scenario_key,
           assessment_protocol, scenario_snapshot_json)
           VALUES ('p1', '受控报告', 'completed', '2026-10-19T00:00:00Z',
                   'goal-driven-v1', 'goal-driven-v1', ?)""",
        (json.dumps(snapshot),),
    )


def _request(conn, key="standard-pentest"):
    return assessment_reports.request_assessment_report_generation(
        conn, "p1", template_key=key, requested_by="synthetic-user"
    )


def test_migration_keeps_latest_content_profiles_and_empty_editable_record(pg_conn):
    conn = pg_conn
    content = "# 当前正文\n\n## 自定义章节\n"
    conn.execute(
        """INSERT INTO report_template_versions
           (template_key, version, markdown_content, digest_sha256, created_at)
           VALUES ('standard-pentest', 3, ?, ?, '2026-10-19T00:00:00Z')""",
        (content, report_templates.report_template_digest(content)),
    )
    conn.execute(
        """INSERT INTO report_template_version_profiles
           (template_key, template_version, profile_id, created_at)
           VALUES ('standard-pentest', 3, 'custom-profile', '2026-10-19T00:00:00Z')"""
    )
    conn.execute(
        """INSERT INTO report_templates (key, name, created_at)
           VALUES ('empty', '尚无正文', '2026-10-19T00:00:00Z')"""
    )
    _seed_project(conn)
    conn.execute(
        """INSERT INTO assessment_report_finalizations
           (project_id, version, status, generation_started_at, generation_token,
            template_key, template_version, template_markdown, created_at, updated_at)
           VALUES ('p1', 1, 'ready', 't', 'historic-token', 'standard-pentest', 1,
                   '历史冻结正文', 't', 't')"""
    )
    before = dict(conn.execute("SELECT * FROM assessment_report_finalizations").fetchone())
    _migrate(conn)
    item = report_templates.get_report_template(conn, "standard-pentest")
    assert item.markdown_content == content
    assert item.compatibility_profiles == ["custom-profile"]
    assert item.updated_at == "2026-10-19T00:00:00Z"
    after = dict(conn.execute("SELECT * FROM assessment_report_finalizations").fetchone())
    assert {key: after[key] for key in before} == before
    assert conn.execute("SELECT to_regclass('report_template_versions') AS relation").fetchone()["relation"] is None
    assert conn.execute("SELECT to_regclass('report_template_version_profiles') AS relation").fetchone()["relation"] is None
    assert report_templates.get_report_template(conn, "empty").markdown_content is None
    assert "empty" not in {item.key for item in report_templates.list_report_template_catalog(conn)}
    with pytest.raises(HTTPException, match="content_missing"):
        report_templates.require_report_template_profile(conn, "empty", "assessment-report-packet-v1")
    report_templates.update_report_template(conn, "empty", "可编辑", "", "# 补充正文")
    assert "empty" in {item.key for item in report_templates.list_report_template_catalog(conn)}


@pytest.mark.parametrize("blocker", ["archive", "pending", "running"])
def test_migration_refuses_to_silently_restore_or_cancel_existing_state(pg_conn, blocker):
    conn = pg_conn
    if blocker == "archive":
        conn.execute("UPDATE report_templates SET archived_at = 't' WHERE key = 'standard-pentest'")
        error = "archive_resolution_required"
    else:
        _seed_project(conn)
        conn.execute(
            """INSERT INTO assessment_report_finalizations
               (project_id, version, status, generation_started_at, generation_token, created_at, updated_at)
               VALUES ('p1', 1, ?, 't', 'token', 't', 't')""",
            (blocker,),
        )
        error = "outstanding_generation_resolution_required"
    conn.commit()
    with pytest.raises(psycopg.errors.RaiseException, match=error):
        _migrate(conn)
    conn.rollback()
    assert conn.execute("SELECT to_regclass('report_template_versions') AS relation").fetchone()["relation"] is not None
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE version = 58").fetchone() is None


@pytest.mark.parametrize(("case", "blocks"), [
    ("rate_limit_backoff", True),
    ("timeout_without_next_attempt", True),
    ("terminal_with_next_attempt", False),
    ("retry_exhausted", False),
    ("accepted_result", False),
    ("older_execution_generation", False),
    ("other_report_generation", False),
    ("unrequested_history", False),
    ("no_execution_history", False),
    ("valid_delivery", True),
    ("expired_delivery", False),
    ("missing_delivery", False),
    ("unconfirmed_delivery", False),
    ("invalid_delivery_predecessor", False),
])
def test_migration_blocks_only_recoverable_failed_report_generation(pg_conn, case, blocks):
    conn = pg_conn
    _seed_project(conn)
    conn.execute("UPDATE projects SET execution_generation = 2 WHERE id = 'p1'")
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    future = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    context = {"report_claim": {"generation_token": "token"}}
    error_type = "provider_rate_limit"
    if case == "timeout_without_next_attempt":
        error_type = "worker_timeout"
    elif case == "terminal_with_next_attempt":
        error_type = "provider_auth"
    elif case == "accepted_result":
        context["accepted_completion_digest"] = "accepted-result"
    elif case == "other_report_generation":
        context["report_claim"]["generation_token"] = "previous-token"
    elif "delivery" in case:
        error_type = "submission_missing"
        context.update({
            "dispatcher_id": "synthetic-dispatcher",
            "execution_lifecycle": {"generation": 2, "termination_confirmed": True},
            "execution_delivery": {
                "session_id": "synthetic-session", "container_id": "synthetic-container",
                "deadline_at": future,
            },
        })
        if case == "expired_delivery":
            context["execution_delivery"]["deadline_at"] = timestamp
        elif case == "missing_delivery":
            context.pop("execution_delivery")
        elif case == "unconfirmed_delivery":
            context["execution_lifecycle"]["termination_confirmed"] = False
        elif case == "invalid_delivery_predecessor":
            context["delivery_retry"] = {"source_task_run_id": "missing-run"}
    count = assessment_attempts.DEFAULT_MAX_RUNTIME_RETRIES + 1 if case == "retry_exhausted" else 1
    for index in range(0 if case == "no_execution_history" else count):
        conn.execute(
            """INSERT INTO task_runs
               (id, project_id, task_type, worker_name, worker_type, status, session_id,
                started_at, ended_at, error_type, runtime_context_json, execution_generation)
               VALUES (?, 'p1', 'report', 'worker', 'report', 'failed', 'synthetic-session',
                       ?, ?, ?, ?, ?)""",
            (f"failed-{index}", timestamp, timestamp, error_type, json.dumps(context),
             1 if case == "older_execution_generation" else 2),
        )
    conn.execute(
        """INSERT INTO assessment_report_finalizations
           (project_id, status, generation_started_at, generation_token, created_at, updated_at,
            requested_at, next_attempt_at, template_key, template_version, template_markdown,
            report_execution_plan_json, report_prompt_markdown)
           VALUES ('p1', 'failed', ?, 'token', ?, ?, ?, ?, 'standard-pentest', 1,
                   '历史冻结正文', '{"packet_version": 1}', '历史冻结 Prompt')""",
        (timestamp, timestamp, timestamp, None if case == "unrequested_history" else timestamp,
         None if case == "timeout_without_next_attempt" else future),
    )
    before = dict(conn.execute("SELECT * FROM assessment_report_finalizations").fetchone())
    runs_before = conn.execute("SELECT * FROM task_runs ORDER BY id").fetchall()
    conn.commit()
    if blocks:
        with pytest.raises(RuntimeError, match="report_template_outstanding_generation_resolution_required"):
            _migrate(conn)
        conn.rollback()
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version = 58").fetchone() is None
        assert conn.execute("SELECT to_regclass('report_template_versions') AS relation").fetchone()["relation"] is not None
    else:
        _migrate(conn)
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version = 58").fetchone() is not None
    after = dict(conn.execute("SELECT * FROM assessment_report_finalizations").fetchone())
    assert {key: after[key] for key in before} == before
    assert conn.execute("SELECT * FROM task_runs ORDER BY id").fetchall() == runs_before


def test_current_template_crud_preserves_markdown_and_profile_checks(pg_conn):
    conn = pg_conn
    _migrate(conn)
    content = "  # 原文  \n\n    缩进内容\n"
    item = report_templates.create_report_template(conn, None, "自定义", "说明", content)
    assert item.key.startswith("report-")
    assert item.markdown_content == content
    assert item.digest_sha256 == sha256(content.encode()).hexdigest()
    updated = report_templates.update_report_template(
        conn, item.key, "改名", "新说明", "# 新正文", compatibility_profiles=["another-profile"]
    )
    assert (updated.name, updated.description, updated.markdown_content) == ("改名", "新说明", "# 新正文")
    assert updated.created_at == item.created_at
    with pytest.raises(HTTPException, match="incompatible"):
        report_templates.require_report_template_profile(conn, item.key, "assessment-report-packet-v1")
    report_templates.update_report_template(conn, "standard-pentest", "内置也可改", "", "# 新内置正文")
    report_templates.delete_report_template(conn, item.key)
    assert conn.execute("SELECT 1 FROM report_template_profiles WHERE template_key = ?", (item.key,)).fetchone() is None
    with pytest.raises(HTTPException) as deleted:
        report_templates.get_report_template(conn, item.key)
    assert deleted.value.status_code == 404
    with pytest.raises(ValidationError):
        AssessmentReportGenerationRequest(template_key="standard-pentest", template_version=1)


def test_request_freezes_content_profiles_and_serializes_edit_delete(pg_conn):
    conn = pg_conn
    _migrate(conn)
    _seed_project(conn)
    conn.commit()
    first = _request(conn)
    frozen = dict(conn.execute("SELECT * FROM assessment_report_finalizations").fetchone())
    assert frozen["template_version"] is None
    assert json.loads(frozen["template_profiles_json"]) == ["assessment-report-packet-v1"]
    plan = parse_report_execution_plan(frozen["report_execution_plan_json"])
    assert (plan.packet_version, plan.output_contract_version) == (2, 3)
    with psycopg.connect(conn._connection.info.dsn, row_factory=dict_row) as raw:
        other = db.PostgresConnection(raw)
        other.execute("SET lock_timeout = '100ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            report_templates.update_report_template(other, "standard-pentest", "变更", "", "# 后来内容")
        other.rollback()
        conn.commit()
        report_templates.update_report_template(other, "standard-pentest", "变更", "", "# 后来内容")
        other.commit()
        assert _request(conn).generation_token == first.generation_token
        conn.commit()
        report_templates.delete_report_template(other, "standard-pentest")
        other.commit()
    assert _request(conn).generation_token == first.generation_token
    assert dict(conn.execute("SELECT * FROM assessment_report_finalizations").fetchone()) == frozen
    conn.execute("UPDATE assessment_report_finalizations SET status = 'ready'")
    with pytest.raises(HTTPException) as missing:
        _request(conn)
    assert missing.value.status_code == 404


def test_current_finish_and_inventory_keep_historic_metadata_unchanged(pg_conn, monkeypatch, tmp_path):
    conn = pg_conn
    _migrate(conn)
    _seed_project(conn)
    requested = _request(conn)

    @contextmanager
    def get_conn():
        yield conn

    monkeypatch.setattr(assessment_reports, "get_conn", get_conn)
    monkeypatch.setattr(assessment_reports, "artifact_root", lambda: tmp_path)
    principal = RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")
    claim = assessment_reports.claim_assessment_report_finalization("p1", "worker", principal=principal)
    assert claim.template_compatibility_profiles == ["assessment-report-packet-v1"]
    report_templates.delete_report_template(conn, "standard-pentest")
    finalization = conn.execute("SELECT * FROM assessment_report_finalizations").fetchone()
    template = {
        "key": finalization["template_key"], "markdown_content": finalization["template_markdown"],
        "digest_sha256": finalization["template_digest_sha256"],
        "compatibility_profiles": claim.template_compatibility_profiles,
    }
    snapshot = {
        "version": 1, "graph_yaml": "project: p1\n",
        "report_packet": {"version": 2, "project": {"id": "p1"}}, "report_template": template,
    }
    common = {
        "source": "report_task", "report_generation_token": claim.generation_token,
        "report_template_key": claim.template_key, "report_template_digest_sha256": claim.template_digest_sha256,
        "report_execution_plan_sha256": claim.report_execution_plan_sha256,
        "report_prompt_digest_sha256": claim.report_prompt_digest_sha256,
        "report_policy_version": claim.report_policy_version,
        "packet_version": 2, "output_contract_version": 3, "report_data_schema_version": 2,
        "runtime_context": {},
    }

    def artifact(artifact_id, suffix, content, metadata):
        filename = f"{artifact_id}-{suffix}"
        path = tmp_path / "projects" / "p1" / "runs" / claim.task_run_id / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        digest = sha256(content).hexdigest()
        conn.execute(
            """INSERT INTO artifacts (id, project_id, task_run_id, kind, category, uri,
               sha256, size, created_at, metadata_json)
               VALUES (?, 'p1', ?, 'report', 'deliverable', ?, ?, ?, ?, ?)""",
            (artifact_id, claim.task_run_id, f"artifact://projects/p1/runs/{claim.task_run_id}/{filename}",
             digest, len(content), claim.updated_at, json.dumps(metadata)),
        )
        return digest

    snapshot_bytes = json.dumps(snapshot, ensure_ascii=False).encode()
    snapshot_digest = sha256(snapshot_bytes).hexdigest()
    snapshot_metadata = {**common, "format": "json", "snapshot_sha256": snapshot_digest}
    artifact("snapshot", "report-input-snapshot.json", snapshot_bytes, snapshot_metadata)
    assessment_reports.bind_assessment_report_input_snapshot(
        "p1", "worker", claim_token=claim.claim_token, artifact_id="snapshot",
        digest_sha256=snapshot_digest, principal=principal,
    )
    report_metadata = {
        **common, "format": "markdown", "input_snapshot_artifact_id": "snapshot",
        "input_snapshot_sha256": snapshot_digest, "completeness_rule_version": 1,
    }
    report_digest = artifact("new", "vulnerability-report.md", "# 受控报告\n".encode(), report_metadata)
    invalid_metadata = {**report_metadata, "report_template_version": 1}
    conn.execute("UPDATE artifacts SET metadata_json = ? WHERE id = 'new'", (json.dumps(invalid_metadata),))
    with pytest.raises(HTTPException, match="artifact_invalid"):
        assessment_reports._validate_report_artifact(
            conn, finalization, conn.execute("SELECT * FROM task_runs WHERE id = ?", (claim.task_run_id,)).fetchone(),
            artifact_id="new", artifact_sha256=report_digest, snapshot_artifact_id="snapshot", snapshot_sha256=snapshot_digest,
        )
    conn.execute("UPDATE artifacts SET metadata_json = ? WHERE id = 'new'", (json.dumps(report_metadata),))
    body = assessment_reports.AssessmentReportFinishRequest(
        protocol="cairn-assessment-report-finish-v1", worker="worker", claim_token=claim.claim_token,
        generation_token=claim.generation_token, task_run_id=claim.task_run_id, status="ready",
        artifact_id="new", artifact_sha256=report_digest,
        input_snapshot_artifact_id="snapshot", input_snapshot_sha256=snapshot_digest,
        task_run_result={"status": "succeeded", "exit_code": 0},
    )
    receipt = assessment_reports.finish_assessment_report_finalization("p1", body, principal=principal)
    assert receipt.status == "ready"
    assert receipt.template_compatibility_profiles == claim.template_compatibility_profiles == requested.template_compatibility_profiles
    assert assessment_reports.get_assessment_report_finalization(conn, "p1").template_compatibility_profiles == receipt.template_compatibility_profiles
    stored_receipt = conn.execute("SELECT response_json FROM assessment_report_finish_receipts").fetchone()
    assert json.loads(stored_receipt["response_json"])["template_compatibility_profiles"] == receipt.template_compatibility_profiles
    assert assessment_reports.finish_assessment_report_finalization("p1", body, principal=principal) == receipt
    replay = assessment_reports.AssessmentReportFinishReplayRequest(
        **body.model_dump(exclude={"claim_token"}),
        claim_sha256=sha256(claim.claim_token.encode()).hexdigest(),
        request_sha256=receipt.request_sha256,
    )
    assert assessment_reports.replay_assessment_report_finalization("p1", replay, principal=principal) == receipt
    old_metadata = {key: value for key, value in report_metadata.items()
                    if key not in {"packet_version", "output_contract_version", "report_data_schema_version"}}
    old_metadata["report_template_version"] = 3
    artifact("old", "vulnerability-report.md", b"# historic\n", old_metadata)
    before = conn.execute("SELECT metadata_json FROM artifacts WHERE id = 'old'").fetchone()["metadata_json"]
    inventory = list_report_inventory(conn, tenant_id=None, template_key="standard-pentest")
    assert {item.artifact_id: item.template_version for item in inventory.items} == {"new": None, "old": 3}
    assert conn.execute("SELECT metadata_json FROM artifacts WHERE id = 'old'").fetchone()["metadata_json"] == before


def test_two_template_rendered_artifacts_finish_and_enter_inventory_with_historic_v3(pg_conn, monkeypatch, tmp_path):
    from copy import deepcopy

    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.tasks import report
    from cairn.server.schemas.artifacts import CreateArtifactRequest
    from cairn.server.services import audit_artifacts
    from test_report_frozen_rendering import V3_TEMPLATE, _data, _packet

    conn = pg_conn
    _migrate(conn)
    _seed_project(conn)
    conn.execute("UPDATE projects SET status = 'stopped' WHERE id = 'p1'")
    report_templates.update_report_template(conn, "standard-pentest", "四章模板", "", V3_TEMPLATE)
    alternate = "# 冻结材料交付\n\n## 事实及发现\n\n## 修复路线与附件\n"
    report_templates.create_report_template(conn, "alternative", "另一布局", "", alternate)

    @contextmanager
    def get_conn():
        yield conn

    for service in (assessment_reports, audit_artifacts):
        monkeypatch.setattr(service, "get_conn", get_conn)
        monkeypatch.setattr(service, "artifact_root", lambda: tmp_path)

    class Client:
        def create_artifact(self, project_id, **kwargs):
            artifact = audit_artifacts.create_artifact_record(project_id, CreateArtifactRequest(**kwargs))
            return ApiResult(status_code=201, data={"id": artifact.id, "sha256": artifact.sha256})

    client = Client()
    packet_json = json.dumps(_packet(), ensure_ascii=False)
    data = _data("v1", "v2")
    alternate_data = deepcopy(data)
    alternate_data["layout"] = [
        {"type": "markdown", "markdown": "# 冻结材料交付\n\n## 事实及发现"},
        *(item for item in data["layout"] if item["type"] == "fact" and item["ref"] not in {"limitations", "evidence_index"}),
        {"type": "markdown", "markdown": "## 修复路线与附件\n\n建议先修复授权边界，再记录回归证据。"},
        {"type": "fact", "ref": "limitations"}, {"type": "fact", "ref": "evidence_index"},
    ]
    principal = RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")
    registered = []
    outputs = []
    for key, result_data in (("standard-pentest", data), ("alternative", alternate_data)):
        requested = _request(conn, key)
        claim = assessment_reports.claim_assessment_report_finalization("p1", "worker", principal=principal)
        assert claim.generation_token == requested.generation_token
        plan = json.loads(claim.report_execution_plan_json)
        snapshot = json.dumps({
            "version": 1, "report_packet": json.loads(packet_json), "graph_yaml": "project: p1\n",
            "report_template": {
                "key": claim.template_key, "markdown_content": claim.template_markdown,
                "digest_sha256": claim.template_digest_sha256,
                "compatibility_profiles": claim.template_compatibility_profiles,
            },
        }, ensure_ascii=False)
        snapshot_digest = sha256(snapshot.encode()).hexdigest()
        common = {
            "source": "report_task", "report_generation_token": claim.generation_token,
            "report_template_key": claim.template_key, "report_template_digest_sha256": claim.template_digest_sha256,
            "report_execution_plan_sha256": claim.report_execution_plan_sha256,
            "report_prompt_digest_sha256": claim.report_prompt_digest_sha256,
            "report_policy_version": plan["report_policy_version"],
            "packet_version": 2, "output_contract_version": 3, "report_data_schema_version": 2,
        }
        snapshot_result = client.create_artifact(
            "p1", task_run_id=claim.task_run_id, kind="report", category="deliverable",
            filename="report-input-snapshot.json", content=snapshot,
            metadata={**common, "format": "json", "snapshot_sha256": snapshot_digest},
        )
        snapshot_id = snapshot_result.data["id"]
        assessment_reports.bind_assessment_report_input_snapshot(
            "p1", "worker", claim_token=claim.claim_token, artifact_id=snapshot_id,
            digest_sha256=snapshot_digest, principal=principal,
        )
        identity = report._write_report_results(
            client, "p1", claim.task_run_id, data=result_data, report_packet_json=packet_json,
            report_template_markdown=claim.template_markdown,
            report_generation_token=claim.generation_token, report_template_key=claim.template_key,
            report_template_digest_sha256=claim.template_digest_sha256,
            report_policy_version=plan["report_policy_version"],
            report_execution_plan_sha256=claim.report_execution_plan_sha256,
            report_prompt_digest_sha256=claim.report_prompt_digest_sha256,
            completeness_rule_version=plan["completeness_rule_version"],
            input_snapshot_artifact_id=snapshot_id, input_snapshot_sha256=snapshot_digest,
        )
        assert isinstance(identity, report.ReportArtifactIdentity)
        body = assessment_reports.AssessmentReportFinishRequest(
            protocol="cairn-assessment-report-finish-v1", worker="worker", claim_token=claim.claim_token,
            generation_token=claim.generation_token, task_run_id=claim.task_run_id, status="ready",
            artifact_id=identity.artifact_id, artifact_sha256=identity.sha256,
            input_snapshot_artifact_id=snapshot_id, input_snapshot_sha256=snapshot_digest,
            task_run_result={"status": "succeeded", "exit_code": 0},
        )
        assert assessment_reports.finish_assessment_report_finalization("p1", body, principal=principal).status == "ready"
        persisted = audit_artifacts.artifact_content_file("p1", identity.artifact_id).disk_path.read_text()
        assert sha256(persisted.encode()).hexdigest() == identity.sha256
        assert report._report_main_headings(persisted) == report._report_main_headings(
            claim.template_markdown.replace("{{ report_title }}", "安全评估部分报告")
        )
        for text in ("Canonical 漏洞记录数：2；Observation 数：3", "conflicting", "run_failed", "not_run", "缺少第二身份", "/evidence/request", "漏洞 v1", "漏洞 v2"):
            assert text in persisted
        registered.append(identity.artifact_id)
        outputs.append(persisted)
    assert outputs[0] != outputs[1]
    last = conn.execute("SELECT * FROM artifacts WHERE id = ?", (registered[-1],)).fetchone()
    historic_metadata = json.loads(last["metadata_json"])
    for field in ("packet_version", "output_contract_version", "report_data_schema_version"):
        historic_metadata.pop(field)
    historic_metadata["report_template_version"] = 3
    historic = client.create_artifact(
        "p1", task_run_id=last["task_run_id"], kind="report", category="deliverable",
        filename="vulnerability-report.md", content="# 合成历史报告\n", metadata=historic_metadata,
    ).data["id"]
    before = dict(conn.execute("SELECT * FROM artifacts WHERE id = ?", (historic,)).fetchone())
    inventory = list_report_inventory(conn, tenant_id=None)
    items = {item.artifact_id: item for item in inventory.items}
    assert set(items) == {*registered, historic}
    assert {key: items[key].template_version for key in items} == {**dict.fromkeys(registered), historic: 3}
    for artifact_id in registered:
        assert (items[artifact_id].packet_version, items[artifact_id].output_contract_version, items[artifact_id].report_data_schema_version) == (2, 3, 2)
    assert [items[artifact_id].template_key for artifact_id in registered] == ["standard-pentest", "alternative"]
    assert dict(conn.execute("SELECT * FROM artifacts WHERE id = ?", (historic,)).fetchone()) == before
