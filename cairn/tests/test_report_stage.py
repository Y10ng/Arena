"""报告阶段新合同、冻结执行与 Artifact 写回测试；旧漏洞数据仍可读写。"""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import sqlite3
from types import SimpleNamespace

import pytest

from cairn.dispatcher.contracts import validate_report_payload
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import report
from cairn.dispatcher.tasks.report_finish_outbox import ReportFinishOutbox
from cairn.dispatcher.worker_result_gateway import WorkerResultFailure
from cairn.server import db
from cairn.server.models import (
    CreateArtifactRequest,
    CreateVulnerabilityRequest,
    UpdateVulnerabilityRequest,
)
from cairn.server.services import create_vulnerability, list_vulnerabilities, update_vulnerability
from cairn.server.services import audit_artifacts
from report_test_support import ephemeral_report_finish_outbox


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn)
    return conn


def _seed_project(conn: sqlite3.Connection, project_id: str = "p1") -> None:
    conn.execute(
        "INSERT INTO projects (id, title, status, created_at, scenario_key) VALUES (?, ?, 'active', ?, 'default')",
        (project_id, "ReportTest", "2026-08-05T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES (?, ?, ?, 'system', ?)",
        ("fsrc", project_id, "origin", "2026-08-05T00:00:00Z"),
    )


def report_config(timeout: int = 600):
    return SimpleNamespace(
        dispatcher_id="synthetic-dispatcher",
        tasks=SimpleNamespace(report=SimpleNamespace(timeout=timeout)),
    )


def report_data(*ids: str) -> dict[str, object]:
    return {
        "executive_summary": "建议依据已有证据修复权限边界。",
        "findings": [{
            "vulnerability_id": value,
            "impact": "已有证据支持的业务影响。",
            "root_cause": "黑盒推断，未获得源码证据。",
            "repro_steps": "按证据包中的已有身份、请求和预期行为说明复现。",
            "remediation": "建议检查服务端授权边界。",
            "regression_steps": "修复后使用正常对照和不同身份重测。",
            "limitations": "修复和回归方案尚未执行。",
        } for value in ids],
        "limitations": "报告只解释冻结材料。",
        "artifacts": [],
    }


def report_packet(*ids: str) -> dict[str, object]:
    return {
        "version": 2,
        "project": {"id": "p1", "title": "冻结报告", "status": "stopped"},
        "assessment_summary": {
            "project_status": "stopped", "stop_reason": "user_stopped",
            "execution": {"phase": "finalizing", "budget": None},
            "coverage": {"tested": [], "blocked": [], "untested": []},
            "selected_deep": [], "calls_by_task_type": [],
        },
        "vulnerabilities": [{"id": value, "title": "相同标题", "severity": "high"} for value in ids],
        "observations": [], "observation_statuses": [], "coverage_cells": [],
        "chain_candidates": [], "artifacts": [], "evidence_links": [], "verifications": [],
    }


def report_payload(data: dict[str, object]) -> dict[str, object]:
    return {"protocol": "cairn-assessment-v2", "accepted": True, "kind": "report_result", "data": data}


def test_validate_report_payload_envelope() -> None:
    kind, data = validate_report_payload(report_payload(report_data("v001")))
    assert kind == "report"
    assert data == report_data("v001")


def test_validate_report_payload_rejected() -> None:
    kind, data = validate_report_payload({"protocol": "cairn-assessment-v2", "accepted": False, "reason": "evidence_insufficient"})
    assert kind == "rejected"
    assert data is None


def test_validate_report_payload_wrong_kind() -> None:
    payload = report_payload(report_data())
    payload["kind"] = "reason_result"
    with pytest.raises(ValueError, match="unexpected result kind"):
        validate_report_payload(payload)


def test_validate_report_payload_requires_id_not_title() -> None:
    data = report_data("v001")
    data["findings"][0].pop("vulnerability_id")
    data["findings"][0]["title"] = "相同标题"
    with pytest.raises(ValueError, match="vulnerability_id"):
        validate_report_payload(report_payload(data))


def test_validate_report_payload_allows_id_without_title() -> None:
    kind, data = validate_report_payload(report_payload(report_data("v001")))
    assert kind == "report"
    assert data["findings"][0]["vulnerability_id"] == "v001"
    assert "title" not in data["findings"][0]


@pytest.mark.parametrize("field", ["repro_steps", "remediation", "regression_steps"])
def test_validate_report_payload_explanation_fields_must_be_str(field: str) -> None:
    data = report_data("v001")
    data["findings"][0][field] = 42
    with pytest.raises(ValueError, match=field):
        validate_report_payload(report_payload(data))


@pytest.mark.parametrize("summary", ["", "  \n "])
def test_validate_report_payload_empty_summary_invalid(summary: str) -> None:
    data = report_data()
    data["executive_summary"] = summary
    with pytest.raises(ValueError):
        validate_report_payload(report_payload(data))


@pytest.mark.parametrize("data", [
    {"report_markdown": "# Report"},
    {"vulnerability_updates": [{"vulnerability_id": "v001", "repro_request": "fabricated"}]},
])
def test_validate_report_payload_rejects_legacy_output(data) -> None:
    with pytest.raises(ValueError):
        validate_report_payload(report_payload(data))


class FakeReportClient:
    def __init__(self, packet: dict[str, object]) -> None:
        self.packet = packet
        self.artifacts: list[dict[str, object]] = []

    def get_assessment_report_packet(self, project_id: str) -> ApiResult:
        return ApiResult(status_code=200, data=self.packet)

    def update_vulnerability(self, *args, **kwargs):
        pytest.fail("报告禁止修改 canonical 漏洞")

    def create_artifact(self, *args, **kwargs) -> ApiResult:
        self.artifacts.append(kwargs)
        digest = sha256(kwargs["content"].encode("utf-8")).hexdigest()
        return ApiResult(
            status_code=200,
            data={"id": "a001", "sha256": digest},
        )


class FakePostCompletionClient:
    def __init__(
        self,
        *,
        status: str = "completed",
        artifact: bool = False,
        succeeded_run: bool = False,
    ) -> None:
        self.project = SimpleNamespace(project=SimpleNamespace(status=status))
        self.artifact = artifact
        self.succeeded_run = succeeded_run
        self.finalization_finishes: list[str] = []
        self.finalization_errors: list[str] = []
        self.finalization_claim_tokens: list[str] = []
        self.finalization_requests: list[dict[str, object]] = []
        self.generation_token = "generation-1"
        self.claim_token = "claim-1"
        self.task_run_id = "tr-claimed"
        self.lease_seconds: int | None = None
        self.report_prompt = "Report {report_packet_json} {graph_yaml} {knowledge_base}"
        prompt_digest = sha256(self.report_prompt.encode("utf-8")).hexdigest()
        self.execution_plan = json.dumps(
            {
                "completeness_rule_version": 1,
                "execution_profile_id": "default-copy-v2",
                "knowledge_key": None,
                "output_contract_version": 2,
                "packet_version": 2,
                "execution_contract_version": 1,
                "execution_engine": "pi",
                "report_policy_version": 1,
                "report_prompt_digest_sha256": prompt_digest,
                "report_prompt_group": "default-copy-v2",
                "report_prompt_name": "report.md",
                "source": "test",
                "template_profile": "assessment-report-packet-v1",
                "version": 2,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.execution_plan_digest = sha256(
            self.execution_plan.encode("utf-8")
        ).hexdigest()
        self.prompt_digest = prompt_digest

    def get_project(self, project_id: str):
        return self.project

    def claim_assessment_report_finalization(
        self,
        project_id: str,
        worker: str,
        *,
        lease_seconds: int,
    ) -> ApiResult:
        self.lease_seconds = lease_seconds
        return ApiResult(
            status_code=200,
            data={
                "task_run_id": self.task_run_id,
                "status": "running",
                "generation_token": self.generation_token,
                "claim_token": self.claim_token,
                "requested_at": "2026-08-17T00:01:00Z",
                "template_key": "standard-pentest",
                "template_version": 1,
                "template_digest_sha256": sha256(b"# Template").hexdigest(),
                "template_markdown": "# Template",
                "report_execution_plan_json": self.execution_plan,
                "report_execution_plan_sha256": self.execution_plan_digest,
                "report_prompt_digest_sha256": self.prompt_digest,
                "report_prompt_markdown": self.report_prompt,
            },
        )

    def finish_assessment_report_finalization(
        self,
        project_id: str,
        worker: str,
        *,
        claim_token: str,
        generation_token: str,
        task_run_id: str,
        status: str,
        artifact_id: str | None,
        artifact_sha256: str | None,
        input_snapshot_artifact_id: str | None,
        input_snapshot_sha256: str | None,
        task_run_result: dict[str, object],
    ) -> ApiResult:
        request = {
            "project_id": project_id,
            "worker": worker,
            "claim_token": claim_token,
            "generation_token": generation_token,
            "task_run_id": task_run_id,
            "status": status,
            "artifact_id": artifact_id,
            "artifact_sha256": artifact_sha256,
            "input_snapshot_artifact_id": input_snapshot_artifact_id,
            "input_snapshot_sha256": input_snapshot_sha256,
            "task_run_result": task_run_result,
        }
        self.finalization_claim_tokens.append(claim_token)
        self.finalization_finishes.append(status)
        self.finalization_errors.append(
            str(task_run_result.get("error_message") or "")
        )
        self.finalization_requests.append(request)
        request_sha256 = report._report_finish_request_sha256(
            project_id,
            "synthetic-dispatcher",
            worker,
            claim_token=claim_token,
            generation_token=generation_token,
            task_run_id=task_run_id,
            status=status,
            artifact_id=artifact_id,
            artifact_sha256=artifact_sha256,
            input_snapshot_artifact_id=input_snapshot_artifact_id,
            input_snapshot_sha256=input_snapshot_sha256,
            task_run_result=task_run_result,
        )
        return ApiResult(
            status_code=200,
            data={
                "protocol": "cairn-assessment-report-finish-v1",
                "project_id": project_id,
                "task_run_id": task_run_id,
                "generation_token": generation_token,
                "status": status,
                "artifact_id": artifact_id,
                "input_snapshot_artifact_id": input_snapshot_artifact_id,
                "input_snapshot_sha256": input_snapshot_sha256,
                "request_sha256": request_sha256,
                "finish_disposition": "accepted",
            },
        )

    def replay_assessment_report_finalization(
        self,
        project_id: str,
        worker: str,
        **kwargs,
    ) -> ApiResult:
        request = {"project_id": project_id, "worker": worker, **kwargs}
        self.finalization_finishes.append(kwargs["status"])
        self.finalization_errors.append(
            str(kwargs["task_run_result"].get("error_message") or "")
        )
        self.finalization_requests.append(request)
        return ApiResult(
            status_code=200,
            data={
                "protocol": "cairn-assessment-report-finish-v1",
                "project_id": project_id,
                "task_run_id": kwargs["task_run_id"],
                "generation_token": kwargs["generation_token"],
                "status": kwargs["status"],
                "artifact_id": kwargs["artifact_id"],
                "input_snapshot_artifact_id": kwargs[
                    "input_snapshot_artifact_id"
                ],
                "input_snapshot_sha256": kwargs["input_snapshot_sha256"],
                "request_sha256": kwargs["request_sha256"],
                "finish_disposition": "accepted",
            },
        )

    def list_task_runs(self, project_id: str) -> ApiResult:
        data = []
        if self.succeeded_run:
            data.append(
                {
                    "task_type": "report",
                    "status": "succeeded",
                    "started_at": "2026-08-17T00:02:00Z",
                }
            )
        return ApiResult(status_code=200, data=data)

    def list_artifacts(self, project_id: str) -> ApiResult:
        data = []
        if self.artifact:
            data.append(
                {
                    "kind": "report",
                    "uri": "artifact://projects/p1/runs/tr1/a001-vulnerability-report.md",
                    "metadata": {
                        "report_generation_token": self.generation_token
                    },
                }
            )
        return ApiResult(status_code=200, data=data)


def _complete_report_success(kwargs: dict[str, object]) -> str:
    finish_state = kwargs["finish_state"]
    assert isinstance(finish_state, report.ReportFinishState)
    finish_state.artifact = report.ReportArtifactIdentity("report-1", "a" * 64)
    finish_state.input_snapshot = report.ReportArtifactIdentity(
        "snapshot-1",
        "b" * 64,
    )
    finish_state.task_run_result = report._report_task_run_result("success")
    return "success"


class PacketFailureClient:
    def __init__(self) -> None:
        self.task_updates: list[dict[str, object]] = []
        self.task_run_create: dict[str, object] = {}

    def create_task_run(self, *args, **kwargs) -> ApiResult:
        pytest.fail("报告只能复用 claim 创建的 TaskRun")

    def update_task_run(self, project_id, task_run_id, **kwargs) -> ApiResult:
        assert task_run_id == "tr-claimed"
        self.task_updates.append(kwargs)
        return ApiResult(status_code=200, data={"id": task_run_id})

    def get_assessment_report_packet(self, project_id: str) -> ApiResult:
        return ApiResult(status_code=503, data=None, text="packet unavailable")


class PersistedArtifactClient:
    def __init__(self, artifact) -> None:
        self.artifact = artifact

    def list_artifacts(self, project_id: str) -> ApiResult:
        assert project_id == self.artifact.project_id
        return ApiResult(
            status_code=200,
            data=[
                {
                    "kind": self.artifact.kind,
                    "uri": self.artifact.uri,
                }
            ],
        )


def test_has_report_artifact_recognizes_persisted_prefixed_filename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(audit_artifacts, "get_conn", fake_get_conn)
    monkeypatch.setattr(audit_artifacts, "artifact_root", lambda: tmp_path)
    try:
        _seed_project(conn)
        artifact = audit_artifacts.create_artifact_record(
            "p1",
            CreateArtifactRequest(
                kind="report",
                content="# Report",
                filename=report.REPORT_ARTIFACT_FILENAME,
            ),
        )

        assert artifact.uri.endswith("/a001-vulnerability-report.md")
        assert report.has_report_artifact(PersistedArtifactClient(artifact), "p1") is True
    finally:
        conn.close()


def test_post_completion_report_reloads_completed_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakePostCompletionClient()
    calls: list[object] = []

    def fake_run(*args, **kwargs):
        calls.append(args[3])
        return _complete_report_success(kwargs)

    monkeypatch.setattr(report, "run_report_task", fake_run)
    outcome = report.run_post_completion_report(
        report_config(),
        client,
        SimpleNamespace(),
        "p1",
        "stale graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )

    assert outcome == "success"
    assert calls == [client.project]


@pytest.mark.parametrize("timeout, expected_lease", [(30, 330), (600, 900), (1500, 1800)])
def test_post_completion_uses_claim_task_run_and_timeout_sized_lease(monkeypatch, timeout, expected_lease):
    client = FakePostCompletionClient(artifact=True)
    captured = {}

    def run(*args, **kwargs):
        captured.update(kwargs)
        return _complete_report_success(kwargs)

    monkeypatch.setattr(report, "run_report_task", run)
    outcome = report.run_post_completion_report(
        report_config(timeout), client, SimpleNamespace(), "p1", "graph",
        SimpleNamespace(name="worker"), prompt_group="default-copy-v2", durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )
    assert outcome == "success"
    assert client.lease_seconds == expected_lease
    assert captured["task_run_id"] == "tr-claimed"
    assert captured["report_claim_token"] == client.claim_token
    assert captured["report_generation_token"] == client.generation_token
    assert client.finalization_finishes == ["ready"]


def test_post_completion_rejects_timeout_beyond_lease_limit_before_claim():
    client = FakePostCompletionClient()
    outcome = report.run_post_completion_report(
        report_config(1501), client, SimpleNamespace(), "p1", "graph",
        SimpleNamespace(name="worker"), prompt_group="default-copy-v2", durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )
    assert outcome == "report_timeout_exceeds_lease"
    assert client.lease_seconds is None
    assert client.finalization_finishes == []


def test_post_completion_missing_claim_task_run_does_not_execute(monkeypatch):
    client = FakePostCompletionClient()
    client.task_run_id = None
    monkeypatch.setattr(report, "run_report_task", lambda *args, **kwargs: pytest.fail("不完整 claim 不可执行"))
    outcome = report.run_post_completion_report(
        report_config(), client, SimpleNamespace(), "p1", "graph",
        SimpleNamespace(name="worker"), prompt_group="default-copy-v2", durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )
    assert outcome == "server_write_failed"
    assert client.finalization_finishes == []
    assert client.finalization_errors == []


@pytest.mark.parametrize("finish_disposition", ["accepted", "recovered"])
def test_ready_claim_receipt_short_circuits_all_report_work(
    monkeypatch: pytest.MonkeyPatch,
    finish_disposition: str,
) -> None:
    client = FakePostCompletionClient()
    client.claim_assessment_report_finalization = lambda *args, **kwargs: ApiResult(
        status_code=200,
        data={
            "protocol": "cairn-assessment-report-finish-v1",
            "project_id": "p1",
            "status": "ready",
            "finish_disposition": finish_disposition,
            "task_run_id": "completed-report-run",
            "generation_token": client.generation_token,
            "artifact_id": "report-1",
            "input_snapshot_artifact_id": "snapshot-1",
            "input_snapshot_sha256": "b" * 64,
            "request_sha256": "c" * 64,
            "accepted_at": "2026-09-18T00:00:00Z",
        },
    )
    client.replay_assessment_report_finalization = (
        lambda *args, **kwargs: pytest.fail("ready receipt 不得再次 finish")
    )
    monkeypatch.setattr(
        report,
        "run_report_task",
        lambda *args, **kwargs: pytest.fail("ready receipt 不得再次运行 Worker"),
    )

    outcome = report.run_post_completion_report(
        report_config(),
        client,
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )

    assert outcome == "skipped"
    assert client.finalization_requests == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol", "wrong"),
        ("project_id", "other-project"),
        ("status", "failed"),
        ("finish_disposition", "rejected"),
        ("task_run_id", " completed-report-run"),
        ("generation_token", ""),
        ("artifact_id", None),
        ("input_snapshot_artifact_id", "snapshot-1\x00suffix"),
        ("input_snapshot_sha256", "B" * 64),
        ("request_sha256", "c" * 63),
        ("accepted_at", "2026-09-18T00:00:00"),
    ],
)
def test_malformed_ready_claim_receipt_does_not_suppress_report_work(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    client = FakePostCompletionClient()
    receipt = {
        "protocol": "cairn-assessment-report-finish-v1",
        "project_id": "p1",
        "status": "ready",
        "finish_disposition": "recovered",
        "task_run_id": "completed-report-run",
        "generation_token": client.generation_token,
        "artifact_id": "report-1",
        "input_snapshot_artifact_id": "snapshot-1",
        "input_snapshot_sha256": "b" * 64,
        "request_sha256": "c" * 64,
        "accepted_at": "2026-09-18T00:00:00Z",
    }
    receipt[field] = value
    client.claim_assessment_report_finalization = lambda *args, **kwargs: ApiResult(
        status_code=200,
        data=receipt,
    )
    monkeypatch.setattr(
        report,
        "run_report_task",
        lambda *args, **kwargs: pytest.fail("无有效 claim identity 时不得运行 Worker"),
    )

    outcome = report.run_post_completion_report(
        report_config(),
        client,
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )

    assert outcome == "server_write_failed"
    assert client.finalization_requests == []


def test_post_completion_finalization_rejection_is_not_success(monkeypatch):
    client = FakePostCompletionClient()
    client.replay_assessment_report_finalization = lambda *args, **kwargs: ApiResult(status_code=409)
    monkeypatch.setattr(
        report,
        "run_report_task",
        lambda *args, **kwargs: _complete_report_success(kwargs),
    )
    outcome = report.run_post_completion_report(
        report_config(), client, SimpleNamespace(), "p1", "graph",
        SimpleNamespace(name="worker"), prompt_group="default-copy-v2", durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )
    assert outcome == "business_acceptance"


def test_finish_does_not_send_when_durable_outbox_persistence_fails():
    class NeverSendClient:
        def finish_assessment_report_finalization(self, *args, **kwargs):
            pytest.fail("outbox persistence 失败后不得发送带 token 的 finish")

        def replay_assessment_report_finalization(self, *args, **kwargs):
            pytest.fail("outbox persistence 失败后不得发送 token-free replay")

    class UnavailableOutbox:
        def persist(self, entry):
            raise OSError("fixture storage failure")

    disposition = report._finish_report_claim(
        NeverSendClient(),
        SimpleNamespace(),
        "p1",
        SimpleNamespace(name="worker"),
        authenticated_dispatcher_id="synthetic-dispatcher",
        task_run_id="tr-claimed",
        generation_token="generation-1",
        claim_token="claim-1",
        outcome="container_failed",
        finish_state=report.ReportFinishState(),
        finish_outbox=UnavailableOutbox(),
    )

    assert disposition.retry_same_write is True
    assert disposition.accepted is False


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_body",
        "protocol",
        "project_id",
        "task_run_id",
        "generation_token",
        "status",
        "artifact_id",
        "input_snapshot_artifact_id",
        "input_snapshot_sha256",
        "request_sha256",
        "finish_disposition",
        "finish_disposition_list",
        "finish_disposition_object",
    ],
)
def test_post_completion_replays_same_finish_when_2xx_receipt_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    client = FakePostCompletionClient()
    worker_calls: list[str] = []
    terminal_notifications: list[dict[str, object]] = []
    original_finish = client.replay_assessment_report_finalization

    def invalid_finish(*args, **kwargs):
        response = original_finish(*args, **kwargs)
        if corruption == "missing_body":
            response.data = None
        else:
            assert isinstance(response.data, dict)
            response.data = dict(response.data)
            if corruption == "finish_disposition_list":
                response.data["finish_disposition"] = []
            elif corruption == "finish_disposition_object":
                response.data["finish_disposition"] = {}
            else:
                response.data[corruption] = (
                    "rejected" if corruption == "finish_disposition" else "wrong"
                )
        return response

    def run(*args, **kwargs):
        worker_calls.append("run")
        return _complete_report_success(kwargs)

    monkeypatch.setattr(
        "cairn.dispatcher.tasks.conclude_writer.time.sleep",
        lambda _: None,
    )
    monkeypatch.setattr(report, "run_report_task", run)
    monkeypatch.setattr(
        report,
        "notify_browser_task",
        lambda *args, **kwargs: terminal_notifications.append(kwargs),
    )
    client.replay_assessment_report_finalization = invalid_finish

    outcome = report.run_post_completion_report(
        report_config(),
        client,
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )

    assert outcome == "server_write_failed"
    assert worker_calls == ["run"]
    assert len(client.finalization_requests) == 3
    assert all(
        request == client.finalization_requests[0]
        for request in client.finalization_requests
    )
    assert terminal_notifications == []


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_error_type", "expected_exit_code"),
    [
        (
            WorkerResultFailure(
                failure_class="timeout",
                code="deadline_exceeded",
                message="worker deadline exceeded",
                fingerprint="timeout",
                phase="report_execute",
                exit_code=124,
            ),
            "timed_out",
            "worker_timeout",
            124,
        ),
        (
            WorkerResultFailure(
                failure_class="cancelled",
                code="cancelled",
                message="worker cancelled",
                fingerprint="cancelled",
                phase="report_execute",
            ),
            "cancelled",
            "cancelled",
            None,
        ),
        (
            WorkerResultFailure(
                failure_class="submission_invalid",
                code="submission_invalid",
                message="submission did not match schema",
                fingerprint="submission-invalid",
                phase="report_execute",
                exit_code=0,
                submission_diagnostics={
                    "issue": {"keyword": "required", "path": "$"},
                    "submission_attempts": 3,
                    "repair_rounds": 2,
                    "max_repair_rounds": 2,
                },
            ),
            "failed",
            "submission_invalid",
            0,
        ),
        (
            WorkerResultFailure(
                failure_class="process",
                code="worker_command_failed",
                message="worker process failed",
                fingerprint="process",
                phase="report_execute",
                exit_code=2,
            ),
            "failed",
            "worker_command_failed",
            2,
        ),
    ],
)
def test_failed_report_finish_response_loss_preserves_terminal_facts(
    monkeypatch: pytest.MonkeyPatch,
    failure: WorkerResultFailure,
    expected_status: str,
    expected_error_type: str,
    expected_exit_code: int | None,
) -> None:
    client = FakePostCompletionClient()
    original_finish = client.replay_assessment_report_finalization
    finish_attempts = 0
    worker_calls: list[str] = []
    terminal_notifications: list[dict[str, object]] = []

    def lose_first_response(*args, **kwargs):
        nonlocal finish_attempts
        finish_attempts += 1
        response = original_finish(*args, **kwargs)
        if finish_attempts == 1:
            return ApiResult(status_code=0, text="response lost")
        return response

    def run(*args, **kwargs):
        worker_calls.append("run")
        return failure

    monkeypatch.setattr(
        "cairn.dispatcher.tasks.conclude_writer.time.sleep",
        lambda _: None,
    )
    monkeypatch.setattr(report, "run_report_task", run)
    monkeypatch.setattr(
        report,
        "notify_browser_task",
        lambda *args, **kwargs: terminal_notifications.append(kwargs),
    )
    client.replay_assessment_report_finalization = lose_first_response

    outcome = report.run_post_completion_report(
        report_config(),
        client,
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )

    assert outcome is failure
    assert worker_calls == ["run"]
    assert len(client.finalization_requests) == 2
    assert client.finalization_requests[0] == client.finalization_requests[1]
    task_run_result = client.finalization_requests[0]["task_run_result"]
    assert task_run_result["status"] == expected_status
    assert task_run_result["error_type"] == expected_error_type
    assert task_run_result["exit_code"] == expected_exit_code
    assert task_run_result["error_message"] == failure.message
    assert task_run_result["worker_failure"]["code"] == failure.code
    if failure.submission_diagnostics:
        assert task_run_result["worker_failure"]["submission_attempts"] == 3
        assert task_run_result["worker_failure"]["repair_rounds"] == 2
        assert task_run_result["worker_failure"]["max_repair_rounds"] == 2
    assert terminal_notifications == [{"terminal_confirmed": True}]


class DurableFallbackClient(FakePostCompletionClient):
    def __init__(self, rejection_code: str) -> None:
        super().__init__()
        self.rejection_code = rejection_code
        self.replay_requests: list[dict[str, object]] = []

    def finish_assessment_report_finalization(self, *args, **kwargs):
        pytest.fail("durable finish 不得回退到含 claim token 的 endpoint")

    def replay_assessment_report_finalization(
        self,
        project_id: str,
        worker: str,
        **kwargs,
    ) -> ApiResult:
        request = {"project_id": project_id, "worker": worker, **kwargs}
        self.replay_requests.append(request)
        if len(self.replay_requests) == 1:
            return ApiResult(
                status_code=422,
                data={"detail": self.rejection_code},
            )
        return ApiResult(
            status_code=200,
            data={
                "protocol": "cairn-assessment-report-finish-v1",
                "project_id": project_id,
                "task_run_id": kwargs["task_run_id"],
                "generation_token": kwargs["generation_token"],
                "status": kwargs["status"],
                "artifact_id": kwargs["artifact_id"],
                "input_snapshot_artifact_id": kwargs[
                    "input_snapshot_artifact_id"
                ],
                "input_snapshot_sha256": kwargs["input_snapshot_sha256"],
                "request_sha256": kwargs["request_sha256"],
                "finish_disposition": "accepted",
            },
        )


@pytest.mark.parametrize(
    ("rejection_code", "snapshot_retained"),
    [
        ("assessment_report_artifact_invalid", True),
        ("assessment_report_snapshot_artifact_invalid", False),
    ],
)
def test_ready_artifact_rejection_uses_separate_strict_failed_finish(
    monkeypatch: pytest.MonkeyPatch,
    rejection_code: str,
    snapshot_retained: bool,
) -> None:
    client = FakePostCompletionClient()
    original_finish = client.replay_assessment_report_finalization
    terminal_notifications: list[dict[str, object]] = []

    def reject_ready(*args, **kwargs):
        response = original_finish(*args, **kwargs)
        if len(client.finalization_requests) == 1:
            return ApiResult(
                status_code=409,
                data={"detail": rejection_code},
            )
        return response

    monkeypatch.setattr(
        report,
        "run_report_task",
        lambda *args, **kwargs: _complete_report_success(kwargs),
    )
    monkeypatch.setattr(
        report,
        "notify_browser_task",
        lambda *args, **kwargs: terminal_notifications.append(kwargs),
    )
    client.replay_assessment_report_finalization = reject_ready

    outcome = report.run_post_completion_report(
        report_config(),
        client,
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )

    assert outcome == "artifact_error"
    assert [item["status"] for item in client.finalization_requests] == [
        "ready",
        "failed",
    ]
    fallback = client.finalization_requests[1]
    assert fallback["artifact_id"] is None
    assert fallback["artifact_sha256"] is None
    assert (fallback["input_snapshot_artifact_id"] is not None) is snapshot_retained
    assert (fallback["input_snapshot_sha256"] is not None) is snapshot_retained
    assert fallback["task_run_result"]["error_type"] == "artifact_error"
    assert terminal_notifications == [{"terminal_confirmed": True}]


def test_ready_rejection_atomically_replaces_durable_entry_before_failed_replay(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DurableFallbackClient("assessment_report_artifact_invalid")
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    terminal_notifications: list[dict[str, object]] = []
    monkeypatch.setattr(
        report,
        "run_report_task",
        lambda *args, **kwargs: _complete_report_success(kwargs),
    )
    monkeypatch.setattr(
        report,
        "notify_browser_task",
        lambda *args, **kwargs: terminal_notifications.append(kwargs),
    )

    outcome = report.run_post_completion_report(
        report_config(),
        client,
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=outbox,
    )

    assert outcome == "artifact_error"
    assert [item["status"] for item in client.replay_requests] == [
        "ready",
        "failed",
    ]
    assert all("claim_token" not in item for item in client.replay_requests)
    assert outbox.entries() == []
    assert terminal_notifications == [{"terminal_confirmed": True}]


@pytest.mark.parametrize(
    ("failure_point", "durable_status"),
    [("before_replace", "ready"), ("after_replace", "failed")],
)
def test_ready_to_failed_transition_never_leaves_empty_outbox_on_storage_error(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    durable_status: str,
) -> None:
    client = DurableFallbackClient("assessment_report_artifact_invalid")
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    original_replace = outbox.replace

    def fail_transition(expected, replacement):
        if failure_point == "after_replace":
            original_replace(expected, replacement)
        raise OSError("fixture transition interruption")

    monkeypatch.setattr(outbox, "replace", fail_transition)
    monkeypatch.setattr(
        report,
        "run_report_task",
        lambda *args, **kwargs: _complete_report_success(kwargs),
    )

    outcome = report.run_post_completion_report(
        report_config(),
        client,
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=outbox,
    )

    assert outcome == "server_write_failed"
    entries = outbox.entries()
    assert len(entries) == 1
    assert entries[0].status == durable_status
    assert [item["status"] for item in client.replay_requests] == ["ready"]


@pytest.mark.parametrize(
    ("status_code", "detail", "expected_outcome"),
    [
        (409, "assessment_report_finish_receipt_conflict", "receipt_conflict"),
        (409, "assessment_report_claim_mismatch", "stale_business_state"),
        (403, "execution_owner_mismatch", "policy_denied"),
    ],
)
def test_ready_finish_policy_stale_and_conflict_rejections_do_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    detail: str,
    expected_outcome: str,
) -> None:
    client = FakePostCompletionClient()
    original_finish = client.replay_assessment_report_finalization
    terminal_notifications: list[dict[str, object]] = []

    def reject(*args, **kwargs):
        original_finish(*args, **kwargs)
        return ApiResult(status_code=status_code, data={"detail": detail})

    monkeypatch.setattr(
        report,
        "run_report_task",
        lambda *args, **kwargs: _complete_report_success(kwargs),
    )
    monkeypatch.setattr(
        report,
        "notify_browser_task",
        lambda *args, **kwargs: terminal_notifications.append(kwargs),
    )
    client.replay_assessment_report_finalization = reject

    outcome = report.run_post_completion_report(
        report_config(),
        client,
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )

    assert outcome == expected_outcome
    assert len(client.finalization_requests) == 1
    assert client.finalization_requests[0]["status"] == "ready"
    assert terminal_notifications == []


@pytest.mark.parametrize(
    ("status_code", "detail", "expected_outcome"),
    [
        (403, "execution_owner_mismatch", "policy_denied"),
        (404, "route_not_found", "business_acceptance"),
        (409, "assessment_report_unknown_conflict", "business_acceptance"),
        (422, "assessment_report_terminal_facts_invalid", "business_acceptance"),
    ],
)
def test_durable_ready_finish_retains_non_allowlisted_rejection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    detail: str,
    expected_outcome: str,
) -> None:
    class RejectingReplayClient(FakePostCompletionClient):
        def __init__(self):
            super().__init__()
            self.replay_requests: list[dict[str, object]] = []

        def replay_assessment_report_finalization(
            self,
            project_id: str,
            worker: str,
            **kwargs,
        ) -> ApiResult:
            self.replay_requests.append(
                {"project_id": project_id, "worker": worker, **kwargs}
            )
            return ApiResult(status_code=status_code, data={"detail": detail})

    client = RejectingReplayClient()
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    monkeypatch.setattr(
        report,
        "run_report_task",
        lambda *args, **kwargs: _complete_report_success(kwargs),
    )

    outcome = report.run_post_completion_report(
        report_config(),
        client,
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=outbox,
    )

    assert outcome == expected_outcome
    assert len(client.replay_requests) == 1
    assert client.replay_requests[0]["status"] == "ready"
    entries = outbox.entries()
    assert len(entries) == 1
    assert entries[0].status == "ready"


def test_failed_fallback_response_loss_replays_only_the_failed_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakePostCompletionClient()
    original_finish = client.replay_assessment_report_finalization
    terminal_notifications: list[dict[str, object]] = []

    def reject_then_lose_ack(*args, **kwargs):
        response = original_finish(*args, **kwargs)
        attempt = len(client.finalization_requests)
        if attempt == 1:
            return ApiResult(
                status_code=409,
                data={"detail": "assessment_report_artifact_invalid"},
            )
        if attempt == 2:
            return ApiResult(status_code=0, text="response lost")
        return response

    monkeypatch.setattr(
        "cairn.dispatcher.tasks.conclude_writer.time.sleep",
        lambda _: None,
    )
    monkeypatch.setattr(
        report,
        "run_report_task",
        lambda *args, **kwargs: _complete_report_success(kwargs),
    )
    monkeypatch.setattr(
        report,
        "notify_browser_task",
        lambda *args, **kwargs: terminal_notifications.append(kwargs),
    )
    client.replay_assessment_report_finalization = reject_then_lose_ack

    outcome = report.run_post_completion_report(
        report_config(),
        client,
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )

    assert outcome == "artifact_error"
    assert [item["status"] for item in client.finalization_requests] == [
        "ready",
        "failed",
        "failed",
    ]
    assert client.finalization_requests[1] == client.finalization_requests[2]
    assert terminal_notifications == [{"terminal_confirmed": True}]


def test_rejected_failed_fallback_does_not_confirm_authoritative_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakePostCompletionClient()
    original_finish = client.replay_assessment_report_finalization
    terminal_notifications: list[dict[str, object]] = []

    def reject_both(*args, **kwargs):
        original_finish(*args, **kwargs)
        if len(client.finalization_requests) == 1:
            return ApiResult(
                status_code=409,
                data={"detail": "assessment_report_artifact_invalid"},
            )
        return ApiResult(
            status_code=422,
            data={"detail": "assessment_report_terminal_facts_invalid"},
        )

    monkeypatch.setattr(
        report,
        "run_report_task",
        lambda *args, **kwargs: _complete_report_success(kwargs),
    )
    monkeypatch.setattr(
        report,
        "notify_browser_task",
        lambda *args, **kwargs: terminal_notifications.append(kwargs),
    )
    client.replay_assessment_report_finalization = reject_both

    outcome = report.run_post_completion_report(
        report_config(),
        client,
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )

    assert outcome == "business_acceptance"
    assert [item["status"] for item in client.finalization_requests] == [
        "ready",
        "failed",
    ]
    assert terminal_notifications == []


def test_stopped_project_runs_only_for_durable_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def fake_run(*args, **kwargs):
        calls.append(args[3])
        return _complete_report_success(kwargs)

    monkeypatch.setattr(report, "run_report_task", fake_run)
    durable_client = FakePostCompletionClient(status="stopped", succeeded_run=True)
    durable = report.run_post_completion_report(
        report_config(),
        durable_client,
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )
    legacy = report.run_post_completion_report(
        SimpleNamespace(),
        FakePostCompletionClient(status="stopped"),
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
    )

    assert durable == "success"
    assert legacy == "skipped"
    assert calls == [durable_client.project]


def test_durable_report_retries_when_successful_run_has_no_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_run(*args, **kwargs):
        calls.append("run")
        return _complete_report_success(kwargs)

    monkeypatch.setattr(report, "run_report_task", fake_run)
    client = FakePostCompletionClient(succeeded_run=True)

    outcome = report.run_post_completion_report(
        report_config(),
        client,
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )

    assert outcome == "success"
    assert calls == ["run"]
    assert client.finalization_finishes == ["ready"]


def test_default_copy_packet_failure_stops_before_worker_and_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = PacketFailureClient()
    worker_started: list[str] = []
    monkeypatch.setattr(
        report,
        "get_driver",
        lambda worker_type: SimpleNamespace(
            prepare_session=lambda: worker_started.append(worker_type)
        ),
    )
    container_manager = SimpleNamespace(
        ensure_running=lambda *args, **kwargs: pytest.fail(
            "container should not start without the authoritative packet"
        )
    )
    project = SimpleNamespace(project=SimpleNamespace(id="p1"))

    container_runtime = SimpleNamespace(
        advisory_digest="a" * 64,
        advisory_manifest={
            "legacy_files": [],
            "project_resources": [
                {
                    "id": "r001",
                    "kind": "script",
                    "path": "auth/login.py",
                    "size_bytes": 12,
                    "sha256": "b" * 64,
                }
            ],
        },
    )
    outcome = report.run_report_task(
        report_config(),
        client,
        container_manager,
        project,
        "graph",
        SimpleNamespace(name="worker", type="pi"),
        prompt_group="default-copy-v2",
        task_run_id="tr-claimed",
        durable_request=True,
        report_claim_token="claim-1",
        report_generation_token="generation-1",
        report_execution_plan={},
        frozen_report_prompt="frozen prompt",
        container_runtime=container_runtime,
        finish_outbox=ephemeral_report_finish_outbox(),
    )

    assert outcome == "report_packet_read_failed"
    assert worker_started == []
    assert client.task_run_create == {}
    assert client.task_updates == [
        {
            "runtime_context": {
                "execution_container_ids": [],
                "execution_lifecycle": {
                    "generation": 0,
                    "process_started": False,
                    "termination_confirmed": True,
                    "cleanup_confirmed": False,
                },
            },
        },
    ]


def test_default_copy_packet_failure_enters_durable_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        report,
        "run_report_task",
        lambda *args, **kwargs: "report_packet_read_failed",
    )
    client = FakePostCompletionClient()

    outcome = report.run_post_completion_report(
        report_config(),
        client,
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )

    assert outcome == "report_packet_read_failed"
    assert client.finalization_finishes == ["failed"]
    assert client.finalization_errors == ["report_packet_read_failed"]


def test_report_without_frozen_claim_never_starts_worker(monkeypatch) -> None:
    monkeypatch.setattr(report, "get_driver", lambda *args: pytest.fail("不得启动旧报告路径"))
    outcome = report.run_report_task(
        report_config(), PacketFailureClient(), SimpleNamespace(),
        SimpleNamespace(project=SimpleNamespace(id="p1")), "graph",
        SimpleNamespace(name="worker", type="pi"), prompt_group="default-report",
    )
    assert outcome == "report_execution_plan_missing"


def test_post_completion_report_defers_and_skips_existing_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        report,
        "run_report_task",
        lambda *args, **kwargs: pytest.fail("report task should not run"),
    )
    deferred = report.run_post_completion_report(
        SimpleNamespace(),
        FakePostCompletionClient(status="active"),
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )
    skipped = report.run_post_completion_report(
        SimpleNamespace(),
        FakePostCompletionClient(artifact=True),
        SimpleNamespace(),
        "p1",
        "graph",
        SimpleNamespace(name="worker"),
        prompt_group="default-copy-v2",
    )

    assert deferred == "deferred"
    assert skipped == "skipped"


def test_packet_vulnerability_context_uses_frozen_packet_identity() -> None:
    packet = json.dumps(
        {
            **report_packet(),
            "vulnerabilities": [
                {"id": "v001", "title": "SQLi", "severity": "high"},
                {"id": "v002", "title": "SQLi", "severity": "medium"},
                {"id": "v003", "title": "SSRF", "severity": "high"},
            ]
        }
    )

    vulnerabilities_json, id_to_title = report._packet_vulnerability_context(packet)

    assert json.loads(vulnerabilities_json)[0]["id"] == "v001"
    assert id_to_title == {"v001": "SQLi", "v002": "SQLi", "v003": "SSRF"}


def test_collect_report_vulnerabilities_includes_id_fact_and_evidence() -> None:
    detail = {
        "id": "v001",
        "title": "SQLi",
        "severity": "high",
        "target": "/login",
        "source_fact": {
            "description": "确认登录绕过",
            "evidence": [{"id": "a001", "kind": "file", "filename": "poc.http", "pointer": "line:1", "source": "fact_evidence"}],
        },
        "run_artifacts": [{"id": "a002", "kind": "raw_output", "filename": "stdout.log", "pointer": "", "source": "discovered_run"}],
        "fingerprint": "abc123",
        "identity": {"vulnerability_class": "sql_injection"},
        "observations": [
            {
                "id": "vo_1",
                "task_run_id": "tr001",
                "repro_request": "POST /login HTTP/1.1",
            }
        ],
    }
    packet = report_packet("v001")
    packet["vulnerabilities"] = [detail]
    client = FakeReportClient(packet)
    packet_json = report._collect_report_packet_json(client, "p1")
    vulns_json, id_to_title = report._packet_vulnerability_context(packet_json)

    assert '"id": "v001"' in vulns_json
    assert "确认登录绕过" in vulns_json
    assert "poc.http" in vulns_json
    assert "stdout.log" in vulns_json
    assert '"fingerprint": "abc123"' in vulns_json
    assert '"observations"' in vulns_json
    assert "POST /login HTTP/1.1" in vulns_json
    assert id_to_title == {"v001": "SQLi"}


def test_report_duplicate_titles_write_only_frozen_artifact() -> None:
    packet = report_packet("v001", "v002")
    client = FakeReportClient(packet)
    identity = report._write_report_results(
        client, "p1", "tr001", data=report_data("v001", "v002"),
        report_packet_json=json.dumps(packet), report_generation_token="generation-1",
    )
    assert isinstance(identity, report.ReportArtifactIdentity)
    assert identity.artifact_id == "a001"
    assert len(client.artifacts) == 1
    assert client.artifacts[0]["task_run_id"] == "tr001"
    assert client.artifacts[0]["metadata"]["report_generation_token"] == "generation-1"


def test_report_unknown_id_does_not_fallback_to_title() -> None:
    assert report._report_completeness_error(
        report_data("missing"), id_to_title={"v001": "Unique"},
    ) is not None


# ---------------------------------------------------------------------------
# repro_steps 写入与迁移
# ---------------------------------------------------------------------------


def test_update_vulnerability_writes_repro_steps_and_remediation() -> None:
    """报告阶段回写：repro_request/repro_response/repro_steps/remediation 均可更新。"""
    conn = make_conn()
    try:
        _seed_project(conn)
        v = create_vulnerability(
            conn,
            "p1",
            CreateVulnerabilityRequest(title="SQLi", severity="high", source_fact_id="fsrc"),
        )
        assert v.repro_steps == ""
        updated = update_vulnerability(
            conn,
            "p1",
            v.id,
            UpdateVulnerabilityRequest(
                repro_request="GET /x HTTP/1.1\nHost: h",
                repro_response="HTTP/1.1 200 OK",
                repro_steps="1. curl ...\n2. observe ...",
                remediation="use prepared statements",
            ),
        )
        assert updated.repro_request.startswith("GET /x")
        assert updated.repro_response == "HTTP/1.1 200 OK"
        assert updated.repro_steps.startswith("1. curl")
        assert updated.remediation == "use prepared statements"
        # 二次空更新（全 None）不清空已写入字段。
        untouched = update_vulnerability(conn, "p1", v.id, UpdateVulnerabilityRequest())
        assert untouched.repro_steps.startswith("1. curl")
    finally:
        conn.close()


def test_repro_steps_migration_adds_column_to_legacy_db() -> None:
    """旧库无 repro_steps 列：迁移幂等补列后漏洞读写正常，且重复迁移不报错。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 用全量 SCHEMA 建库后删掉加性列，模拟"repro_steps 尚未引入"的旧库形态；
    # 迁移函数依赖 settings 等全量表，无法用裁剪 schema 模拟。
    conn.executescript(db.SCHEMA)
    conn.execute("ALTER TABLE vulnerabilities DROP COLUMN repro_steps")
    conn.execute("ALTER TABLE vulnerabilities DROP COLUMN repro_request")
    conn.execute("ALTER TABLE vulnerabilities DROP COLUMN repro_response")
    try:
        db._ensure_schema_migrations(conn)
        # 幂等：再跑一次不报错。
        db._ensure_schema_migrations(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(vulnerabilities)").fetchall()}
        assert "repro_steps" in cols
        assert "repro_request" in cols
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, scenario_key) VALUES ('p1', 't', 'active', '2026-08-05T00:00:00Z', 'default')"
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES ('fsrc', 'p1', 'origin', 'system', '2026-08-05T00:00:00Z')"
        )
        v = create_vulnerability(
            conn,
            "p1",
            CreateVulnerabilityRequest(title="XSS", severity="medium", source_fact_id="fsrc"),
        )
        assert v.repro_steps == ""
        updated = update_vulnerability(
            conn, "p1", v.id, UpdateVulnerabilityRequest(repro_steps="1. open page\n2. alert fires")
        )
        assert updated.repro_steps.startswith("1. open page")
        # 读出路径（vulnerability_from_row）同样带 repro_steps。
        listed = list_vulnerabilities(conn, "p1")
        assert listed[0].repro_steps.startswith("1. open page")
    finally:
        conn.close()
