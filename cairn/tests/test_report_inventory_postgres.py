"""只用自建 PostgreSQL 验证报告成品接受事实，不调用模型或写入活动数据库。"""

from __future__ import annotations

from hashlib import sha256
import json

import pytest

from cairn.server import db
from cairn.server.schemas.projects import AssessmentReportFinishReceiptView
from cairn.server.services.report_inventory import list_report_inventory
from cairn.server.services.report_policy import (
    REPORT_DATA_SCHEMA_VERSION,
    REPORT_OUTPUT_CONTRACT_VERSION,
    REPORT_PACKET_VERSION,
)
from test_report_templates import defaults_v60_conn, private_postgres


_NOW = "2026-10-21T01:00:00Z"
_DIGEST = "a" * 64


@pytest.fixture
def inventory_conn(defaults_v60_conn):
    conn = defaults_v60_conn
    db.run_migrations(conn, "postgresql")
    conn.execute("INSERT INTO tenants VALUES ('other-tenant', 'personal', '合成租户', ?)", (_NOW,))
    for project_id, tenant_id in (("p1", "tenant_legacy"), ("p2", "other-tenant")):
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, tenant_id) VALUES (?, ?, 'completed', ?, ?)",
            (project_id, project_id, _NOW, tenant_id),
        )
    return conn


def _artifact(conn, artifact_id, *, project_id="p1", historical=False, kind="report", source="report_task"):
    generation_token = "generation-" + artifact_id
    task_run_id = "run-" + artifact_id
    metadata = {
        "source": source, "format": "markdown", "report_generation_token": generation_token,
        "report_template_key": "standard-pentest", "report_template_digest_sha256": _DIGEST,
        "report_policy_version": 1, "report_execution_plan_sha256": _DIGEST,
        "report_prompt_digest_sha256": _DIGEST, "input_snapshot_artifact_id": "snapshot-" + artifact_id,
        "input_snapshot_sha256": _DIGEST, "completeness_rule_version": 1,
    }
    if historical:
        metadata["report_template_version"] = 3
    else:
        metadata.update(
            packet_version=REPORT_PACKET_VERSION, output_contract_version=REPORT_OUTPUT_CONTRACT_VERSION,
            report_data_schema_version=REPORT_DATA_SCHEMA_VERSION,
        )
    conn.execute(
        """INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, started_at, ended_at)
           VALUES (?, ?, 'report', 'synthetic-worker', 'pi', 'succeeded', ?, ?)""",
        (task_run_id, project_id, _NOW, _NOW),
    )
    digest = sha256(artifact_id.encode()).hexdigest()
    conn.execute(
        """INSERT INTO artifacts
           (id, project_id, task_run_id, kind, category, uri, sha256, size, created_at, metadata_json)
           VALUES (?, ?, ?, ?, 'deliverable', ?, ?, 123, ?, ?)""",
        (artifact_id, project_id, task_run_id, kind,
         f"artifact://projects/{project_id}/runs/{task_run_id}/{artifact_id}-vulnerability-report.md",
         digest, _NOW, json.dumps(metadata)),
    )
    return {
        "project_id": project_id, "task_run_id": task_run_id, "generation_token": generation_token,
        "artifact_id": artifact_id, "artifact_sha256": digest,
        "input_snapshot_artifact_id": metadata["input_snapshot_artifact_id"], "input_snapshot_sha256": _DIGEST,
    }


def _receipt(conn, artifact, *, status="ready", disposition="accepted", overrides=None, response_json=None):
    # 布置完整、不可变的合成 finish receipt；这里只测试 inventory 判定，不替代 finish 验证测试。
    values = dict(artifact)
    if status == "failed":
        values.update(artifact_id=None, artifact_sha256=None)
        conn.execute("UPDATE task_runs SET status = 'failed' WHERE id = ?", (values["task_run_id"],))
    values.update(overrides or {})
    receipt = AssessmentReportFinishReceiptView(
        project_id=values["project_id"], task_run_id=values["task_run_id"],
        generation_token=values["generation_token"], status=status, artifact_id=values["artifact_id"],
        input_snapshot_artifact_id=values["input_snapshot_artifact_id"],
        input_snapshot_sha256=values["input_snapshot_sha256"],
        generation_started_at=_NOW, created_at=_NOW, updated_at=_NOW,
        protocol="cairn-assessment-report-finish-v1", request_sha256=_DIGEST,
        finish_disposition=disposition, accepted_at=_NOW,
    )
    conn.execute(
        """INSERT INTO assessment_report_finish_receipts
           (project_id, task_run_id, protocol, generation_token, request_sha256, request_json,
            response_json, artifact_id, artifact_sha256, input_snapshot_artifact_id, input_snapshot_sha256, accepted_at)
           VALUES (?, ?, 'cairn-assessment-report-finish-v1', ?, ?, '{}', ?, ?, ?, ?, ?, ?)""",
        (values["project_id"], values["task_run_id"], values["generation_token"], _DIGEST,
         receipt.model_dump_json() if response_json is None else response_json,
         values["artifact_id"], values["artifact_sha256"], values["input_snapshot_artifact_id"],
         values["input_snapshot_sha256"], _NOW),
    )


def _finalization(conn, artifact):
    conn.execute(
        """INSERT INTO assessment_report_finalizations
           (project_id, status, generation_started_at, generation_token, artifact_id,
            accepted_artifact_id, accepted_generation_token, accepted_input_sha256, created_at, updated_at)
           VALUES (?, 'failed', ?, 'newer-failed-generation', ?, ?, ?, ?, ?, ?)""",
        (artifact["project_id"], _NOW, artifact["artifact_id"], artifact["artifact_id"],
         artifact["generation_token"], artifact["input_snapshot_sha256"], _NOW, _NOW),
    )


def test_inventory_excludes_uploads_and_accepted_failure_receipts(inventory_conn):
    conn = inventory_conn
    _artifact(conn, "uploaded-only")
    failed = _artifact(conn, "failed-finish")
    _receipt(conn, failed, status="failed")
    ready = _artifact(conn, "ready")
    _receipt(conn, ready)
    _artifact(conn, "segment", kind="file", source="report_segment")
    foreign = _artifact(conn, "foreign-ready", project_id="p2")
    _receipt(conn, foreign)

    inventory = list_report_inventory(conn, tenant_id="tenant_legacy", page_size=1)
    assert inventory.total == 1
    assert [item.artifact_id for item in inventory.items] == ["ready"]
    assert "report_generation_token" not in inventory.items[0].model_dump()
    assert list_report_inventory(conn, tenant_id="tenant_legacy", q="uploaded-only").total == 0
    assert list_report_inventory(conn, tenant_id="tenant_legacy", project_id="p2").total == 0


def test_inventory_preserves_every_successful_receipt_across_new_generations(inventory_conn):
    conn = inventory_conn
    old = _artifact(conn, "a-old", historical=True)
    _receipt(conn, old)
    newer = _artifact(conn, "b-newer")
    _receipt(conn, newer, disposition="recovered")
    failed = _artifact(conn, "z-latest-failed")
    _receipt(conn, failed, status="failed")
    _finalization(conn, newer)

    before = [dict(row) for row in conn.execute("SELECT * FROM assessment_report_finish_receipts").fetchall()]
    inventory = list_report_inventory(conn, tenant_id="tenant_legacy", page_size=1)
    assert inventory.total == 2
    assert [item.artifact_id for item in inventory.items] == ["b-newer"]
    older_page = list_report_inventory(conn, tenant_id="tenant_legacy", page=2, page_size=1)
    assert [(item.artifact_id, item.template_version) for item in older_page.items] == [("a-old", 3)]
    # 当前代次、状态乃至 accepted 指针变化，不得抹掉其他代次已有的成功 receipt。
    conn.execute(
        """UPDATE assessment_report_finalizations SET status = 'running', generation_token = 'next-generation',
           artifact_id = NULL, accepted_artifact_id = NULL, accepted_generation_token = NULL,
           accepted_input_sha256 = NULL WHERE project_id = 'p1'"""
    )
    assert {item.artifact_id for item in list_report_inventory(conn, tenant_id=None).items} == {"a-old", "b-newer"}
    assert before == [dict(row) for row in conn.execute("SELECT * FROM assessment_report_finish_receipts").fetchall()]


@pytest.mark.parametrize("invalid_field", [None, "accepted_generation_token", "accepted_input_sha256", "accepted_artifact_id"])
def test_inventory_uses_only_exact_legacy_accepted_identity(inventory_conn, invalid_field):
    conn = inventory_conn
    legacy = _artifact(conn, "legacy", historical=True)
    _finalization(conn, legacy)
    if invalid_field is not None:
        conn.execute(f"UPDATE assessment_report_finalizations SET {invalid_field} = NULL WHERE project_id = 'p1'")
    inventory = list_report_inventory(conn, tenant_id=None)
    assert [item.artifact_id for item in inventory.items] == (["legacy"] if invalid_field is None else [])


@pytest.mark.parametrize("overrides", [
    {"generation_token": "different-generation"},
    {"artifact_id": "different-artifact"},
    {"artifact_sha256": "b" * 64},
    {"input_snapshot_artifact_id": "different-snapshot"},
    {"input_snapshot_sha256": "c" * 64},
])
def test_inventory_rejects_mismatched_success_receipt(inventory_conn, overrides):
    conn = inventory_conn
    artifact = _artifact(conn, "mismatch")
    _receipt(conn, artifact, overrides=overrides)
    # 即使可变 accepted 指针指向该原件，也不能盖过同任务的矛盾 receipt。
    _finalization(conn, artifact)
    assert list_report_inventory(conn, tenant_id=None).total == 0


@pytest.mark.parametrize("response_json", ["not-json", "[]", '{"status":"failed","finish_disposition":"accepted"}'])
def test_inventory_rejects_invalid_or_failed_receipt_response(inventory_conn, response_json):
    conn = inventory_conn
    artifact = _artifact(conn, "invalid-receipt")
    _receipt(conn, artifact, response_json=response_json)
    _finalization(conn, artifact)
    assert list_report_inventory(conn, tenant_id=None).total == 0
