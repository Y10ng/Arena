"""用一次性 PG 与本地合成 provider 跑通实际 SDK 分段至真实 HTTP/PG 接受边界。"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.tasks import report
from cairn.dispatcher.tasks.report_segments import capture_report_segments
from cairn.report_segments import REPORT_SEGMENT_ADAPTER, assemble_report_segments
from cairn.server import db
from cairn.server.auth import RequestPrincipal, require_tenant_project
from cairn.server.routers import audit, projects
from cairn.server.schemas.projects import AssessmentReportClaimView
from cairn.server.services import assessment_reports as reports, audit_artifacts, report_templates
from cairn.server.services.report_inventory import list_report_inventory
from cairn.server.services.report_policy import REPORT_DATA_SCHEMA_VERSION
from test_report_templates import defaults_v60_conn, private_postgres, _seed_project


@pytest.fixture
def acceptance(defaults_v60_conn, monkeypatch, tmp_path):
    conn = defaults_v60_conn
    db.run_migrations(conn, "postgresql")
    _seed_project(conn)
    conn.execute("UPDATE projects SET status = 'stopped' WHERE id = 'p1'")

    @contextmanager
    def connection():
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    for module in (reports, audit_artifacts, projects):
        monkeypatch.setattr(module, "get_conn", connection)
    for module in (reports, audit_artifacts):
        monkeypatch.setattr(module, "artifact_root", lambda: tmp_path / "artifacts")
    app = FastAPI()
    app.include_router(audit.router)
    app.include_router(projects.router)
    principal = RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")
    app.dependency_overrides[require_tenant_project] = lambda: principal
    client = CairnClient("http://testserver", token="synthetic-only")
    # 只替换 HTTP transport；保留 CairnClient 序列化、真实 router/service 和 PG 写入。
    with TestClient(app) as transport:
        monkeypatch.setattr(client, "_session", lambda: transport)
        yield SimpleNamespace(conn=conn, client=client, root=tmp_path / "artifacts", tmp=tmp_path)


@pytest.fixture
def sdk_workspace():
    # capture 的生产合同固定该路径；仅独占新目录，不把 /tmp 产物重写成生产身份。
    workspace = Path("/home/kali/workspace")
    if not workspace.parent.is_dir():
        pytest.skip("requires an existing /home/kali parent for the isolated SDK workspace")
    try:
        workspace.mkdir(mode=0o700)
    except FileExistsError:
        pytest.skip("refusing to touch an existing /home/kali/workspace")
    manifests = []
    try:
        yield manifests
    finally:
        for manifest in manifests:
            Path(manifest).unlink(missing_ok=True)
        shutil.rmtree(workspace)


def _seed_long_findings(state):
    originals = {}
    for index in range(1, 16):
        original = (f"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
                    f"合成用户甲读取到用户乙的对象 {index}。\r\n尾部反证：没有验证写入权限。\r\n").encode()
        response = state.client.create_artifact(
            "p1", task_run_id=None, kind="raw_output", content=original,
            filename=f"synthetic-response-{index}.http", metadata={"source": "synthetic_capture"},
        )
        assert response.status_code == 201, response.text
        artifact_id = response.data["id"]
        originals[artifact_id] = original
        state.conn.execute(
            """INSERT INTO vulnerabilities
               (id, project_id, title, severity, created_at, updated_at)
               VALUES (?, 'p1', ?, 'high', 'before', 'before')""",
            (f"v{index}", f"合成越权发现 {index}"),
        )
        state.conn.execute(
            """INSERT INTO vulnerability_observations
               (id, project_id, vulnerability_id, severity, confidence, observed_at, evidence_items_json)
               VALUES (?, 'p1', ?, 'high', 0.8, 'before', ?)""",
            (f"o{index}", f"v{index}", json.dumps([{
                "id": f"e{index}", "kind": "http", "source": "capture", "title": f"合成交互 {index}",
                "response": {"artifact_id": artifact_id},
            }])),
        )
    state.conn.commit()
    return originals


def _claim_and_freeze(state):
    requested = reports.request_assessment_report_generation(
        state.conn, "p1", template_key="vulnerability-report", requested_by="synthetic-user",
    )
    state.conn.commit()
    response = state.client.claim_assessment_report_finalization(
        "p1", "worker", generation_token=requested.generation_token,
    )
    assert response.ok, response.text
    claimed = AssessmentReportClaimView.model_validate(response.data)
    response = state.client.get_assessment_report_packet("p1", generation_token=claimed.generation_token)
    assert response.ok, response.text
    packet = response.data
    snapshot = json.dumps({
        "version": 1, "graph_yaml": "project: p1\nfacts: []\n", "report_packet": packet,
        "report_template": {
            "key": claimed.template_key, "markdown_content": claimed.template_markdown,
            "digest_sha256": claimed.template_digest_sha256,
            "compatibility_profiles": claimed.template_compatibility_profiles,
        },
    }, ensure_ascii=False)
    digest = sha256(snapshot.encode()).hexdigest()
    common = {
        "source": "report_task", "report_generation_token": claimed.generation_token,
        "report_template_key": claimed.template_key, "report_template_digest_sha256": claimed.template_digest_sha256,
        "report_execution_plan_sha256": claimed.report_execution_plan_sha256,
        "report_prompt_digest_sha256": claimed.report_prompt_digest_sha256,
        "report_policy_version": claimed.report_policy_version,
        "packet_version": 2, "output_contract_version": 4, "report_data_schema_version": REPORT_DATA_SCHEMA_VERSION,
    }
    uploaded = state.client.create_artifact(
        "p1", task_run_id=claimed.task_run_id, kind="report", category="deliverable",
        filename="report-input-snapshot.json", content=snapshot,
        metadata={**common, "format": "json", "snapshot_sha256": digest},
    )
    assert uploaded.status_code == 201, uploaded.text
    snapshot_id = uploaded.data["id"]
    bound = state.client.bind_assessment_report_input_snapshot(
        "p1", "worker", claim_token=claimed.claim_token, artifact_id=snapshot_id, digest_sha256=digest,
    )
    assert bound.ok, bound.text
    binding = {
        "project_id": "p1", "task_run_id": claimed.task_run_id, "generation_token": claimed.generation_token,
        "input_snapshot_sha256": digest, "report_template_digest_sha256": claimed.template_digest_sha256,
    }
    return SimpleNamespace(claimed=claimed, packet=packet, snapshot_id=snapshot_id, digest=digest, binding=binding)


def _sdk(state, frozen, manifests, *, mode="success"):
    repository = Path(__file__).resolve().parents[2]
    input_path = state.tmp / f"{frozen.claimed.task_run_id}-sdk-input.json"
    output_path = state.tmp / f"{frozen.claimed.task_run_id}-sdk-output.json"
    input_path.write_text(json.dumps({
        "mode": mode, "packet": frozen.packet, "binding": frozen.binding,
        "segment_schema": REPORT_SEGMENT_ADAPTER.json_schema(),
    }), encoding="utf-8")
    executed = subprocess.run(
        ["node", str(repository / "container/pi-agent/tests/segmented-report-acceptance-fixture.mjs"),
         str(input_path), str(output_path)],
        cwd=repository, capture_output=True, text=True, timeout=90,
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr
    bundle = json.loads(output_path.read_text())
    manifests.append(bundle["manifest_path"])
    return bundle


class LocalSdkFiles:
    @contextmanager
    def materialize_file(self, container_name, path):
        assert container_name == "isolated-sdk"
        actual = Path(path)
        assert actual.is_file() and not actual.is_symlink()
        yield SimpleNamespace(path=actual)


def _capture(state, frozen, bundle):
    return capture_report_segments(
        state.client, LocalSdkFiles(), "isolated-sdk",
        manifest_path=bundle["manifest_path"], binding=frozen.binding,
    )


def _finish(state, frozen, *, artifact_id=None, artifact_sha256=None, status="ready", **changes):
    values = {
        "claim_token": frozen.claimed.claim_token, "generation_token": frozen.claimed.generation_token,
        "task_run_id": frozen.claimed.task_run_id, "status": status,
        "artifact_id": artifact_id, "artifact_sha256": artifact_sha256,
        "input_snapshot_artifact_id": frozen.snapshot_id, "input_snapshot_sha256": frozen.digest,
        "task_run_result": {"status": "succeeded", "exit_code": 0},
        **changes,
    }
    return state.client.finish_assessment_report_finalization("p1", "worker", **values)


def test_real_sdk_segments_capture_template_and_postgres_acceptance(acceptance, sdk_workspace):
    state = acceptance
    originals = _seed_long_findings(state)
    current = report_templates.get_report_template(state.conn, "vulnerability-report")
    frozen_template = current.markdown_content.replace("### 漏洞说明", "### 冻结合成漏洞说明")
    assert frozen_template != current.markdown_content
    report_templates.update_report_template(state.conn, current.key, current.name, "", frozen_template)
    state.conn.commit()
    frozen = _claim_and_freeze(state)
    assert len(frozen.packet["vulnerabilities"]) == len(frozen.packet["observations"]) == 15
    assert {key: item["text"].encode() for key, item in frozen.packet["artifact_contents"].items()} == originals
    bundle = _sdk(state, frozen, sdk_workspace)
    assert bundle["sdk_calls"] == 18 and bundle["total_content_bytes"] > 256 * 1024
    submission = bundle["result"]["result"]["data"]
    assert set(submission) == {"schema_version", "segment_refs", "artifacts"}
    assert submission["schema_version"] == 5 and len(submission["segment_refs"]) == 47
    assert all(set(item) == {"artifact_handle"} for item in submission["segment_refs"])
    assert sum(event["event"] == "submission_validated" for event in bundle["audit"]) == 1
    assert sum(event["event"] == "report_output_recovery" for event in bundle["audit"]) == 1
    assert Path(bundle["candidate_spool_path"]).read_bytes().count(b"strict_json") >= 1
    records, declarations = _capture(state, frozen, bundle)
    assert declarations == submission["artifacts"] and len(records) == 47
    segment_rows = state.conn.execute(
        "SELECT * FROM artifacts WHERE task_run_id = ? AND kind = 'file'", (frozen.claimed.task_run_id,),
    ).fetchall()
    assert len(segment_rows) == 47
    for row in segment_rows:
        metadata = json.loads(row["metadata_json"])
        actual = audit_artifacts.artifact_content_file("p1", row["id"]).disk_path.read_bytes()
        declaration = next(item for item in declarations if item["handle"] == metadata["registered_handle"])
        assert actual == Path(declaration["path"]).read_bytes()
        assert row["sha256"] == sha256(actual).hexdigest() == declaration["sha256"]
        assert row["size"] == len(actual) == declaration["size_bytes"]
        assert metadata["source"] == "report_segment"
    assert list_report_inventory(state.conn, tenant_id=None).total == 0
    refs = submission["segment_refs"]
    with pytest.raises(ValueError, match="findings_incomplete"):
        assemble_report_segments(refs[3:], records, binding=frozen.binding, packet=frozen.packet)
    wrong_observation = deepcopy(records)
    first = next(record for record in wrong_observation.values() if record["content"].get("vulnerability_id") == "v1")
    first["content"]["observation_id"] = "o2"
    with pytest.raises(ValueError, match="observation_mismatch"):
        assemble_report_segments(refs, wrong_observation, binding=frozen.binding, packet=frozen.packet)
    with pytest.raises(ValueError, match="manifest_invalid"):
        capture_report_segments(
            state.client, LocalSdkFiles(), "isolated-sdk", manifest_path=bundle["manifest_path"],
            binding={**frozen.binding, "generation_token": "0" * 32},
        )
    data = assemble_report_segments(refs, records, binding=frozen.binding, packet=frozen.packet)
    assert data["schema_version"] == 4 and len(data["findings"]) == 15
    assert all(len(finding["steps"]) == 3 for finding in data["findings"])
    # 冻结后改变 live 数据与模板，输出必须仍由原快照与原模板决定。
    state.conn.execute("UPDATE vulnerabilities SET title = '后来可变标题' WHERE id = 'v1'")
    report_templates.update_report_template(
        state.conn, current.key, current.name, "", frozen_template.replace("冻结合成漏洞说明", "后来可变漏洞说明"),
    )
    state.conn.commit()
    plan = json.loads(frozen.claimed.report_execution_plan_json)
    identity = report._write_report_results(
        state.client, "p1", frozen.claimed.task_run_id, data=data,
        report_packet_json=json.dumps(frozen.packet, ensure_ascii=False), report_template_markdown=frozen.claimed.template_markdown,
        report_generation_token=frozen.claimed.generation_token, report_template_key=frozen.claimed.template_key,
        report_template_digest_sha256=frozen.claimed.template_digest_sha256,
        report_policy_version=plan["report_policy_version"],
        report_execution_plan_sha256=frozen.claimed.report_execution_plan_sha256,
        report_prompt_digest_sha256=frozen.claimed.report_prompt_digest_sha256,
        completeness_rule_version=plan["completeness_rule_version"],
        input_snapshot_artifact_id=frozen.snapshot_id, input_snapshot_sha256=frozen.digest,
    )
    assert isinstance(identity, report.ReportArtifactIdentity), identity
    assert list_report_inventory(state.conn, tenant_id=None).total == 0
    markdown = audit_artifacts.artifact_content_file("p1", identity.artifact_id).disk_path.read_bytes()
    assert len(markdown) > 256 * 1024 and sha256(markdown).hexdigest() == identity.sha256
    assert "冻结合成漏洞说明" in markdown.decode() and "后来可变" not in markdown.decode()
    for index in range(1, 16):
        assert f"合成越权发现 {index}" in markdown.decode()
    for original in originals.values():
        assert original in markdown
    receipt = _finish(state, frozen, artifact_id=identity.artifact_id, artifact_sha256=identity.sha256)
    assert receipt.ok and receipt.data["status"] == "ready", receipt.text
    assert _finish(state, frozen, artifact_id=identity.artifact_id, artifact_sha256=identity.sha256).data == receipt.data
    inventory = list_report_inventory(state.conn, tenant_id=None)
    assert inventory.total == 1 and inventory.items[0].artifact_id == identity.artifact_id
    assert state.conn.execute("SELECT count(*) AS n FROM task_runs").fetchone()["n"] == 1
    assert state.conn.execute("SELECT status FROM task_runs WHERE id = ?", (frozen.claimed.task_run_id,)).fetchone()["status"] == "succeeded"
    assert state.conn.execute("SELECT count(*) AS n FROM facts").fetchone()["n"] == 0
    assert state.conn.execute("SELECT count(*) AS n FROM vulnerability_observations").fetchone()["n"] == 15
    for artifact_id, original in originals.items():
        assert audit_artifacts.artifact_content_file("p1", artifact_id).disk_path.read_bytes() == original


def test_length_partial_material_old_generation_and_cancellation_cannot_be_accepted(acceptance, sdk_workspace):
    state = acceptance
    _seed_long_findings(state)
    frozen = _claim_and_freeze(state)
    bundle = _sdk(state, frozen, sdk_workspace, mode="length")
    assert bundle["result"]["status"] == "failed"
    assert bundle["result"]["error"]["code"] == "model_output_limit"
    assert bundle["result"]["result"] is None and bundle["sdk_calls"] == 4
    assert sum(event["event"] == "report_output_recovery" for event in bundle["audit"]) == 2
    assert not any(event["event"] == "submission_validated" for event in bundle["audit"])
    records, declarations = _capture(state, frozen, bundle)
    assert len(records) == len(declarations) == 1
    with pytest.raises(ValueError, match="findings_incomplete"):
        assemble_report_segments(
            [{"artifact_handle": declarations[0]["handle"]}], records, binding=frozen.binding, packet=frozen.packet,
        )
    row = state.conn.execute("SELECT * FROM artifacts WHERE task_run_id = ? AND kind = 'file'", (frozen.claimed.task_run_id,)).fetchone()
    rejected = _finish(state, frozen, artifact_id=row["id"], artifact_sha256=row["sha256"])
    assert rejected.status_code == 409 and rejected.data["detail"] == "assessment_report_artifact_invalid"
    stale = _finish(state, frozen, artifact_id=row["id"], artifact_sha256=row["sha256"], generation_token="0" * 32)
    assert stale.status_code == 404 and stale.data["detail"] == "assessment_report_finalization_not_found"
    failed = _finish(state, frozen, status="failed", task_run_result={
        "status": "failed", "error_type": "model_output_limit", "error_message": "输出达到上限，当前段未完成。",
    })
    assert failed.ok and failed.data["status"] == "failed", failed.text
    run = state.conn.execute("SELECT status, error_type FROM task_runs WHERE id = ?", (frozen.claimed.task_run_id,)).fetchone()
    assert dict(run) == {"status": "failed", "error_type": "model_output_limit"}
    assert list_report_inventory(state.conn, tenant_id=None).total == 0
    assert audit_artifacts.artifact_content_file("p1", row["id"]).disk_path.read_bytes() == Path(declarations[0]["path"]).read_bytes()
    cancelled = _claim_and_freeze(state)
    reports.cancel_assessment_report_generation(state.conn, "p1")
    state.conn.commit()
    rejected = _finish(state, cancelled, artifact_id=row["id"], artifact_sha256=row["sha256"])
    assert rejected.status_code == 409 and rejected.data["detail"] == "assessment_report_cancellation_pending"
    assert list_report_inventory(state.conn, tenant_id=None).total == 0
    assert state.conn.execute("SELECT count(*) AS n FROM assessment_report_finish_receipts WHERE artifact_id IS NOT NULL").fetchone()["n"] == 0
