"""复测写回使用隔离真实服务与合成进程，不调用模型或目标。"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import Future
from copy import deepcopy
import json
from types import SimpleNamespace

from fastapi import HTTPException, Response
import pytest

from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import external_verification as task
from cairn.dispatcher.scheduler.task_lifecycle import TaskLifecycleMixin
from cairn.dispatcher.worker_result_gateway import worker_failure_error_type
from cairn.server import db
from cairn.server.models import CreateAgentEventRequest, UpdateTaskRunRequest
from cairn.server.routers import verification_requests as router
from cairn.server.schemas.verification_requests import (
    VerificationExecutionBinding, VerificationFailureSubmission, VerificationResultSubmission,
)
from cairn.server.services import audit_task_runs, verification_requests as service
from cairn.server.services.audit_events import create_agent_event_record
from cairn.server.services.event_payloads import agent_event_to_model, result_event_to_model
from test_external_verification_evidence import execution


@pytest.mark.parametrize("version", [1, 2])
def test_accepted_historical_result_replay_preserves_digest_and_archive(execution, version):
    user, machine, _model, _worker, detail, claim = execution
    result = {"outcome": "insufficient_evidence", "summary": "历史结果",
              "tested_scope": "历史本地材料", "notes": "原始说明\r\n \t",
              "limitations": ["未执行目标"], "reason_code": None, "evidence_refs": []}
    if version == 2:
        result.pop("evidence_refs")
        result.update(schema_version=2, evidence_items=[])
    submission = VerificationResultSubmission(
        project_id=detail["project_id"], task_run_id=claim["task_run"]["id"],
        execution_generation=claim["execution_generation"], claim_token=claim["claim_token"], result=result,
    )
    digest = service._digest({"task_run_id": submission.task_run_id, "result": result})
    with pytest.raises(HTTPException) as new_historical_write:
        service.submit_verification_result(detail["id"], submission, machine)
    assert new_historical_write.value.status_code == 422
    with db.get_conn() as conn:
        row = service._owned_request(conn, detail["id"], user)
        service._write_terminal(conn, row, kind="result", digest=digest, result=result)
        original = dict(conn.execute("SELECT result_json,terminal_digest,terminal_at,accepted_at FROM verification_requests WHERE id = ?",
                                     (detail["id"],)).fetchone())
    assert service.verification_result_receipt(detail["id"], submission, machine)["result_matches"]
    projected = service.submit_verification_result(detail["id"], submission, machine)["result"]
    assert projected == (result if version == 1 else {**result, "evidence_refs": []})
    report = service.verification_report(detail["id"], user)
    assert result["notes"] in report and result["tested_scope"] in report
    assert "历史记录未单独记录判断依据" in report and "### 步骤" not in report
    _confirm_stopped(detail, claim)
    with db.get_conn() as conn:
        before_delete = dict(conn.execute("SELECT result_json,terminal_digest,terminal_at,accepted_at FROM verification_requests WHERE id = ?",
                                          (detail["id"],)).fetchone())
    assert before_delete == original
    service.delete_verification_request(detail["id"], user)
    with pytest.raises(HTTPException) as deleted_receipt:
        service.verification_result_receipt(detail["id"], submission, machine)
    assert deleted_receipt.value.status_code == 410
    with db.get_conn() as conn:
        after = dict(conn.execute("SELECT result_json,terminal_digest,terminal_at,accepted_at FROM verification_requests WHERE id = ?",
                                  (detail["id"],)).fetchone())
    assert all(value is None for value in after.values())


SESSION = "pi:00000000-0000-4000-8000-000000000001"
PRIVATE_RESPONSE = "SYNTHETIC_PRIVATE_RESPONSE_CANARY"


def rejected(status, code):
    return ApiResult(status, data={"detail": {"code": code, "message": PRIVATE_RESPONSE}}, text=PRIVATE_RESPONSE)


class ReceiptClient:
    def __init__(self, responses, *, cancelled=False):
        self.responses = {action: iter(values) for action, values in responses.items()}
        self.cancelled = cancelled
        self.calls = defaultdict(list)
        self.events = []

    def finish_verification(self, request_id, action, payload):
        assert request_id == "synthetic-request"
        self.calls[action].append(deepcopy(payload))
        return next(self.responses[action])

    def get_verification_input(self, request_id):
        return ApiResult(200, data={"cancel_requested": self.cancelled})

    def create_agent_event(self, project_id, *, task_run_id, kind, payload):
        self.events.append({
            "id": f"ev{len(self.events)}", "project_id": project_id,
            "task_run_id": task_run_id, "ts": "2026-10-15T00:00:00Z", "kind": kind,
            "payload_json": json.dumps(payload),
        })
        return ApiResult(200)


def run_synthetic_task(monkeypatch, client, *, process_cancelled=False, claim=None, cancellation=None, on_execute=None,
                       worker_data=None):
    claim = claim or {
        "request_id": "synthetic-request", "project_id": "synthetic-project",
        "task_run": {"id": "synthetic-run", "session_id": SESSION},
        "execution_generation": 0, "claim_token": "synthetic-claim-canary",
        "input": {
            "id": "synthetic-request", "mode": "retest", "title": "合成任务", "origin": "无目标操作",
            "report": {"resource_id": "resource"}, "materials": [{"id": "resource", "path": "report.md"}],
            "previous_request_id": None, "cancel_requested": False,
        },
    }
    cancellation = cancellation or TaskCancellation(generation=claim["execution_generation"])
    project_id, run_id, session = claim["project_id"], claim["task_run"]["id"], claim["task_run"]["session_id"]
    result = {
        "protocol": "cairn-assessment-v2", "accepted": True,
        "kind": "external_verification_result", "data": {
            "outcome": "insufficient_evidence", "summary": "合成证据不足",
            "tested_scope": "没有访问目标", "key_evidence": [], "test_steps": [],
            "schema_version": 3, "limitations": ["没有执行实际复测"], "evidence_items": [],
        },
    }
    if worker_data is not None:
        result["data"] = worker_data
    process = ProcessResult(0, json.dumps({
        "protocol": "cairn-agent-result-v1", "engine": "pi", "project_id": project_id,
        "task_run_id": run_id, "phase": task.PHASE, "session": session,
        "status": "succeeded", "result": result, "submission_id": "a" * 64,
        "usage": None, "error": None,
    }), "", cancelled=process_cancelled)
    executions = []

    def execute(*args, **kwargs):
        executions.append(kwargs)
        managed = SimpleNamespace(execution_started=True, execution_completed=False,
                                  execution_identity=("synthetic-container", "synthetic-exec"))
        cancellation.register_execution(managed)
        cancellation.observe_execution_start(managed)
        if on_execute is not None:
            on_execute(cancellation)
        managed.execution_completed = True
        cancellation.finish_execution(managed)
        return process

    driver = SimpleNamespace(prepare_session=lambda: session, build_execute=lambda *a, **k: "synthetic-invocation")
    monkeypatch.setattr(task, "get_driver", lambda kind: driver)
    monkeypatch.setattr(task, "read_external_verification_instructions", lambda: "合成指引")
    monkeypatch.setattr(task, "record_prompt_rendered", lambda *a, **k: None)
    monkeypatch.setattr(task, "register_delivery", lambda *a, **k: object())
    monkeypatch.setattr(task, "remaining_delivery_seconds", lambda snapshot: 60)
    monkeypatch.setattr(task, "run_worker_process", execute)
    monkeypatch.setattr(task, "record_process_artifacts", lambda *a, **k: None)
    outcome = task.run_external_verification_task(
        SimpleNamespace(runtime=SimpleNamespace(interval=3600), tasks=SimpleNamespace(explore=SimpleNamespace(timeout=60, conclude_timeout=10))),
        client, SimpleNamespace(ensure_running=lambda *a: "synthetic-container"),
        SimpleNamespace(project=SimpleNamespace(id=project_id)),
        SimpleNamespace(name="synthetic-worker", type="pi"), cancellation, run_id,
        claim=claim, container_runtime=None, candidate_outbox=None,
    )
    assert len(executions) == 1
    assert process._cleaned
    return outcome


def protocol_receipts(client):
    events = [event for event in client.events if event["kind"] == "protocol_write"]
    # 走真实的两个 API 事件投影，分类不能只留在发送端或被展示过滤器丢掉。
    payloads = [agent_event_to_model(event).payload for event in events]
    assert payloads == [result_event_to_model(event).payload for event in events]
    assert PRIVATE_RESPONSE not in repr(client.events)
    assert "synthetic-claim-canary" not in repr(client.events)
    return payloads


@pytest.mark.parametrize("status,code,expected", [
    (422, "verification_evidence_not_current_or_deliverable", "business_acceptance"),
    (409, "verification_execution_binding_mismatch", "stale_business_state"),
    (409, "verification_execution_closed", "stale_business_state"),
    (409, "verification_terminal_conflict", "receipt_conflict"),
])
def test_result_rejection_preserves_safe_receipt_and_does_not_rerun(monkeypatch, caplog, status, code, expected):
    client = ReceiptClient({"result": [rejected(status, code)], "failure": [ApiResult(200)]})
    assert run_synthetic_task(monkeypatch, client) == expected
    assert len(client.calls["result"]) == len(client.calls["failure"]) == 1
    assert client.calls["failure"][0]["error"]["code"] == expected
    receipts = protocol_receipts(client)
    assert [(item["operation"], item["status_code"], item["disposition"]) for item in receipts] == [
        ("verification_result", status, expected), ("verification_failure", 200, "accepted"),
    ]
    assert receipts[0]["safe_code"] == code
    assert receipts[0]["error_type"] == expected
    assert f"status={status}" in caplog.text and f"code={code}" in caplog.text
    assert PRIVATE_RESPONSE not in caplog.text
    assert "synthetic-claim-canary" not in caplog.text


def test_result_timeout_replays_the_same_write_and_accepts_original_result(monkeypatch):
    waits = []
    monkeypatch.setattr(task.time, "sleep", waits.append)
    accepted = {"result": {"outcome": "insufficient_evidence"}, "execution_status": "running"}
    client = ReceiptClient({"result": [ApiResult(0, text=PRIVATE_RESPONSE), ApiResult(200, data=accepted)]})
    assert run_synthetic_task(monkeypatch, client) == "success"
    assert len(client.calls["result"]) == 2
    assert client.calls["result"][0] == client.calls["result"][1]
    assert not client.calls["failure"]
    assert waits == [1]
    receipts = protocol_receipts(client)
    assert [item["disposition"] for item in receipts] == ["transient", "accepted"]
    assert receipts[0]["status_code"] == 0


def test_terminal_report_failure_does_not_overwrite_first_result_rejection(monkeypatch):
    waits = []
    monkeypatch.setattr(task.time, "sleep", waits.append)
    client = ReceiptClient({
        "result": [rejected(422, "verification_sensitive_result_rejected")],
        "failure": [rejected(503, "synthetic_unavailable") for _ in range(3)],
    })
    assert run_synthetic_task(monkeypatch, client) == "business_acceptance"
    assert len(client.calls["result"]) == 1
    assert len(client.calls["failure"]) == 3
    assert all(payload == client.calls["failure"][0] for payload in client.calls["failure"])
    assert waits == [1, 2]
    receipts = protocol_receipts(client)
    assert receipts[0]["safe_code"] == "verification_sensitive_result_rejected"
    assert receipts[0]["status_code"] == 422
    assert receipts[0]["error_type"] == "business_acceptance"
    assert all(item["disposition"] == "transient" for item in receipts[1:])
    assert all(item["operation"] == "verification_failure" for item in receipts[1:])


class ServiceClient(CairnClient):
    """仅替换 HTTP 边界，写入、取消与终态均经过隔离 Server 服务。"""

    def __init__(self, detail, claim, machine, *, receipt_mode="same"):
        super().__init__("http://synthetic.invalid", token="synthetic-test-token")
        self.detail, self.claim, self.machine = detail, claim, machine
        self.receipt_mode = receipt_mode
        self.calls = defaultdict(list)
        self.events = []
        self.accepted = []
        self.receipt_headers = []

    def _request_json(self, method, path, **kwargs):
        payload = json.loads(json.dumps(kwargs.get("json"), ensure_ascii=False))
        project_id, request_id = self.detail["project_id"], self.detail["id"]
        if path.startswith(f"/api/v1/verification-requests/{request_id}/"):
            action = path.rsplit("/", 1)[1]
            self.calls[action].append(deepcopy(payload))
            assert method == ("GET" if action == "input" else "POST")
            try:
                if action == "input":
                    return ApiResult(200, data=service.verification_input(request_id, self.machine))
                if action == "heartbeat":
                    return ApiResult(200, data=service.heartbeat_verification_request(
                        request_id, VerificationExecutionBinding.model_validate(payload), self.machine,
                    ))
                if action == "result":
                    if self.receipt_mode == "different_result" and not self.accepted:
                        competing = deepcopy(payload)
                        competing["result"]["summary"] = "另一份结果，不是本次冻结提交"
                        self.accepted.append(service.submit_verification_result(
                            request_id, VerificationResultSubmission.model_validate(competing), self.machine,
                        ))
                    try:
                        accepted = service.submit_verification_result(
                            request_id, VerificationResultSubmission.model_validate(payload), self.machine,
                        )
                    except HTTPException as exc:
                        assert self.receipt_mode == "different_result" and exc.status_code == 409
                    else:
                        self.accepted.append(accepted)
                    # 服务已完成原子接受／重放；全部三次响应在网络边界丢弃。
                    return ApiResult(0, text=PRIVATE_RESPONSE)
                if action == "failure":
                    return ApiResult(200, data=service.submit_verification_failure(
                        request_id, VerificationFailureSubmission.model_validate(payload), self.machine,
                    ))
                if action == "receipt":
                    if self.receipt_mode == "query_failed":
                        return rejected(503, "synthetic_unavailable")
                    if self.receipt_mode == "binding_mismatch":
                        payload["claim_token"] = "synthetic-wrong-claim"
                    response = Response()
                    value = router.execution_receipt(
                        request_id, VerificationResultSubmission.model_validate(payload), response, self.machine,
                    )
                    self.receipt_headers.append(dict(response.headers))
                    return ApiResult(200, data=value)
                if action == "cancel-ack":
                    return ApiResult(200, data=service.acknowledge_verification_cancel(
                        request_id, VerificationExecutionBinding.model_validate(payload), self.machine,
                    ))
                raise AssertionError(f"unexpected verification action: {action}")
            except HTTPException as exc:
                return ApiResult(exc.status_code, data={"detail": exc.detail}, text=PRIVATE_RESPONSE)
        if (method, path) == ("POST", f"/projects/{project_id}/agent-events"):
            value = create_agent_event_record(project_id, CreateAgentEventRequest.model_validate(payload))
            with db.get_conn() as conn:
                self.events.append(dict(conn.execute(
                    "SELECT * FROM agent_events WHERE project_id = ? AND id = ?", (project_id, value.id),
                ).fetchone()))
            return ApiResult(200, data=value.model_dump(mode="json"))
        assert (method, path) == ("PATCH", f"/projects/{project_id}/task-runs/{self.claim['task_run']['id']}")
        self.calls["task_run_finish"].append(payload)
        value = audit_task_runs.update_task_run_record(
            project_id, self.claim["task_run"]["id"], UpdateTaskRunRequest.model_validate(payload), principal=self.machine,
        )
        return ApiResult(200, data=value.model_dump(mode="json"))


def settle_task_run(client, claim, outcome, cancellation):
    class Scheduler(TaskLifecycleMixin):
        def _finish_runtime_context(self, *args):
            # 不执行容器和工具版本探针；真实停止证明仍由生产收尾路径传递。
            return {}

        def _clear_project_log_state(self, *args):
            pass

        def _worker_cooldown_error_type(self, error_type):
            return None

        def _clear_worker_cooldown(self, *args):
            pass

        def _clear_worker_rejected(self, *args):
            pass

    scheduler = Scheduler()
    scheduler.client = client
    future = Future()
    future.set_result(outcome)
    scheduler.futures = {future: SimpleNamespace(
        project_id=claim["project_id"], task_run_id=claim["task_run"]["id"],
        task_type="external_verify", worker_name=claim["task_run"]["worker_name"], worker_type="pi",
        cancellation=cancellation, container_runtime=None, binding_circuit_key=None,
    )}
    scheduler._reap_futures()
    pending = scheduler._pending_task_run_finishes()
    assert list(pending) == [(claim["project_id"], claim["task_run"]["id"])]
    assert scheduler._send_task_run_finish(next(iter(pending.values()))) == "acknowledged"
    assert len(client.calls["task_run_finish"]) == 1
    with db.get_conn() as conn:
        return dict(conn.execute("SELECT * FROM task_runs WHERE project_id = ? AND id = ?",
                                 (claim["project_id"], claim["task_run"]["id"])).fetchone())


def test_cancel_waits_for_real_task_run_finish_without_early_ack(execution, monkeypatch):
    user, machine, model, worker_id, detail, claim = execution
    client = ServiceClient(detail, claim, machine)
    cancellation = TaskCancellation(generation=claim["execution_generation"])
    executions = []

    def cancel_in_execution(token):
        executions.append(token)
        cancelled = service.cancel_verification_request(detail["id"], user)
        assert cancelled["execution_status"] == "cancelling"
        assert token.execution_lifecycle(task_finished=True)["termination_confirmed"] is False
        token.cancel("project_paused")
        assert not client.calls["cancel-ack"]

    outcome = run_synthetic_task(
        monkeypatch, client, process_cancelled=True, claim=claim, cancellation=cancellation,
        on_execute=cancel_in_execution,
    )
    assert worker_failure_error_type(outcome) == "cancelled"
    assert len(executions) == 1
    assert not client.calls["cancel-ack"] and not client.calls["failure"] and not client.calls["result"]
    before_finish = service.get_verification_request(detail["id"], user)
    assert before_finish["execution_status"] == "cancelling"
    with db.get_conn() as conn:
        request = conn.execute("SELECT terminal_kind FROM verification_requests WHERE id = ?", (detail["id"],)).fetchone()
        run = conn.execute("SELECT status, runtime_context_json FROM task_runs WHERE id = ? AND project_id = ?",
                           (claim["task_run"]["id"], detail["project_id"])).fetchone()
    assert request["terminal_kind"] is None
    assert run["status"] in {"queued", "running"}
    assert json.loads(run["runtime_context_json"])["execution_lifecycle"]["termination_confirmed"] is False
    stored_run = settle_task_run(client, claim, outcome, cancellation)
    assert stored_run["status"] == "cancelled" and stored_run["error_type"] == "cancelled"
    assert json.loads(stored_run["runtime_context_json"])["execution_lifecycle"]["termination_confirmed"] is True
    assert service.get_verification_request(detail["id"], user)["execution_status"] == "cancelled"
    assert not client.calls["cancel-ack"]


@pytest.mark.parametrize("receipt_mode", ["same", "different_result", "binding_mismatch", "query_failed"])
def test_all_result_responses_lost_reconcile_only_matching_authoritative_receipt(execution, monkeypatch, caplog, receipt_mode):
    user, machine, model, worker_id, detail, claim = execution
    waits = []
    monkeypatch.setattr(task.time, "sleep", waits.append)
    client = ServiceClient(detail, claim, machine, receipt_mode=receipt_mode)
    cancellation = TaskCancellation(generation=claim["execution_generation"])
    outcome = run_synthetic_task(monkeypatch, client, claim=claim, cancellation=cancellation)
    expected = "success" if receipt_mode == "same" else "server_write_failed"
    assert outcome == expected
    assert len(client.calls["result"]) == 3 and waits == [1, 2]
    assert all(payload == client.calls["result"][0] for payload in client.calls["result"])
    assert len(client.calls["failure"]) == len(client.calls["receipt"]) == 1
    assert client.calls["receipt"][0] == client.calls["result"][0]
    assert client.calls["failure"][0]["error"]["code"] == "server_write_failed"
    assert len(client.accepted) == (1 if receipt_mode == "different_result" else 3)
    original = client.accepted[0]
    assert original["accepted_at"] is not None
    if receipt_mode == "same":
        assert all(value["result"] == original["result"] and value["accepted_at"] == original["accepted_at"]
                   for value in client.accepted)
    stored_run = settle_task_run(client, claim, outcome, cancellation)
    assert stored_run["status"] == ("succeeded" if receipt_mode == "same" else "failed")
    assert stored_run["error_type"] == (None if receipt_mode == "same" else "server_write_failed")
    finished = service.get_verification_request(detail["id"], user)
    assert finished["execution_status"] == "completed"
    assert finished["result"] == original["result"] and finished["accepted_at"] == original["accepted_at"]
    receipts = protocol_receipts(client)
    assert [item["disposition"] for item in receipts[:4]] == ["transient"] * 3 + ["receipt_conflict"]
    assert receipts[3]["safe_code"] == "verification_terminal_conflict"
    assert receipts[4]["operation"] == "verification_receipt"
    if receipt_mode in {"same", "different_result"}:
        assert client.receipt_headers[0]["cache-control"] == "no-store"
    else:
        assert receipts[4]["disposition"] == ("stale_business_state" if receipt_mode == "binding_mismatch" else "transient")
    assert PRIVATE_RESPONSE not in caplog.text
    assert claim["claim_token"] not in repr(client.events) and claim["claim_token"] not in caplog.text


def _confirm_stopped(detail, claim, *, confirmed=True, cleanup="removed"):
    # 只写隔离合成记录；用同 generation 的停止证明区分业务终结和物理退出。
    with db.get_conn() as conn:
        run = conn.execute("SELECT * FROM task_runs WHERE project_id = ? AND id = ?",
                           (detail["project_id"], claim["task_run"]["id"])).fetchone()
        context = json.loads(run["runtime_context_json"])
        context["execution_lifecycle"] = {"generation": claim["execution_generation"],
                                           "termination_confirmed": confirmed}
        context["external_verification_cleanup"] = {"status": cleanup}
        conn.execute("UPDATE task_runs SET status = 'succeeded', runtime_context_json = ? WHERE project_id = ? AND id = ?",
                     (json.dumps(context), detail["project_id"], run["id"]))


def test_v3_production_handle_resolution_acceptance_and_report(execution, monkeypatch):
    from cairn.dispatcher.tasks.common_base import ProcessArtifactRecord
    from cairn.server.services.audit_artifacts import _artifact_disk_path_from_row
    from test_external_verification_evidence import _upload

    user, machine, _model, _worker, detail, claim = execution
    originals = [
        ("file", "request.http", b"GET /synthetic HTTP/1.1\r\nHost: fixture.invalid\r\n\r\n  "),
        ("raw_output", "response.http", b"HTTP/1.1 403 Forbidden\r\n\r\noriginal\n```\nend\n"),
        ("log", "observed.log", "命令观察原文\n".encode()),
        ("file", "legacy.txt", b"legacy-encoding:\xff\xfe"),
        ("screenshot", "proof.png", b"\x89PNG\r\n\x1a\nsynthetic image"),
        ("file", "authorized.http", b"GET /auth HTTP/1.1\r\nAuthorization: Bearer synthetic-target-credential\r\n\r\n"),
    ]
    artifacts = [_upload(detail, claim, kind, filename, content) for kind, filename, content in originals]
    mapping = {f"local-{index}": artifact.id for index, artifact in enumerate(artifacts)}
    monkeypatch.setattr(task, "record_declared_artifacts", lambda *a, **k: ProcessArtifactRecord(
        refs=[], artifact_ids_by_handle=mapping,
    ))
    items = [{"id": "exchange", "kind": "http", "source": "capture",
              "request": {"artifact_handle": "local-0"}, "response": {"artifact_handle": "local-1"}},
             {"id": "partial", "kind": "http", "source": "capture", "request": {"artifact_handle": "local-0"}}]
    items.extend({"id": f"other-{index}", "kind": "artifact", "source": "capture",
                  "artifact": {"artifact_handle": f"local-{index}"}} for index in range(2, len(artifacts)))
    data = {"schema_version": 3, "outcome": "not_reproduced", "summary": "所测资源拒绝访问，本次未复现。",
            "tested_scope": "仅合成角色与所选资源", "limitations": ["没有访问真实目标"],
            "key_evidence": [{"reason": "响应拒绝访问，仅支持本次未复现。", "evidence_item_ids": ["exchange"]}],
            "test_steps": [
                {"action": "登记合成请求与响应", "observation": "收到拒绝访问响应", "evidence_item_ids": ["exchange", "partial"]},
                {"action": "复核已登记交互", "observation": "仍是同一原件，不冒充再次发送", "evidence_item_ids": ["exchange"]},
            ], "evidence_items": items}
    client = ServiceClient(detail, claim, machine)
    monkeypatch.setattr(task.time, "sleep", lambda _: None)
    cancellation = TaskCancellation(generation=claim["execution_generation"])
    outcome = run_synthetic_task(monkeypatch, client, claim=claim, cancellation=cancellation, worker_data=data)
    assert outcome == "success"
    submitted = client.calls["result"][0]["result"]
    assert submitted["key_evidence"] == data["key_evidence"] and submitted["test_steps"] == data["test_steps"]
    assert [item["id"] for item in submitted["evidence_items"]] == [item["id"] for item in items]
    assert submitted["evidence_items"][0]["request"]["artifact_id"] == artifacts[0].id
    assert "notes" not in submitted and "artifact_handle" not in repr(submitted)
    from pydantic import ValidationError
    invalid = deepcopy(client.calls["result"][0])
    invalid["result"]["test_steps"][0]["evidence_item_ids"] = ["missing"]
    with pytest.raises(ValidationError):
        VerificationResultSubmission.model_validate(invalid)
    settle_task_run(client, claim, outcome, cancellation)
    assert service.get_verification_request(detail["id"], user)["can_delete"] is False
    assert "尚未回收" in service.get_verification_request(detail["id"], user)["delete_block_reason"]
    report = service.verification_report(detail["id"], user)
    assert [line for line in report.splitlines() if line.startswith("## ")] == ["## 一、结论", "## 二、关键证据", "## 三、测试过程"]
    assert "影响判断的条件与限制：" not in report and "实际测试范围：" not in report
    for content in (originals[0][2].decode(), originals[1][2].decode(), originals[2][2].decode()):
        assert content in report
    assert "````text" in report and "交互原文见第 1 步" in report and "本条未记录响应" in report
    assert "未记录步骤关联" in report and "原件不是 UTF-8 文本" in report and "受控查看原件" in report
    assert "原件不允许内嵌交付" in report and "synthetic-target-credential" not in report
    for artifact in artifacts[3:5]:
        with pytest.raises(HTTPException) as binary_preview:
            service.verification_artifact_preview(detail["id"], artifact.id, user)
        assert binary_preview.value.status_code == 415
    assert service.verification_artifact_preview(detail["id"], artifacts[0].id, user)["content"] == originals[0][2].decode()
    with db.get_conn() as conn:
        response = conn.execute("SELECT * FROM artifacts WHERE project_id = ? AND id = ?", (detail["project_id"], artifacts[1].id)).fetchone()
        observed = conn.execute("SELECT * FROM artifacts WHERE project_id = ? AND id = ?", (detail["project_id"], artifacts[2].id)).fetchone()
        _artifact_disk_path_from_row(response).unlink()
        _artifact_disk_path_from_row(observed).write_bytes(b"changed")
    damaged_report = service.verification_report(detail["id"], user)
    assert data["summary"] in damaged_report and "原件不可读取" in damaged_report and "SHA256 不符" in damaged_report


def test_permanent_delete_removes_owned_records_and_keeps_existing_copies(execution):
    from uuid import uuid4
    from starlette.requests import Request
    from cairn.server.auth import RequestPrincipal, require_tenant_project, require_user_tenant_project
    from cairn.server.schemas.verification_requests import CreateVerificationRequest
    from cairn.server.services.project_resources import _batch_member_disk_path
    from test_external_verification_evidence import _submission, _upload

    user, machine, _model, _worker, detail, claim = execution
    for terminal in (False, True):
        if terminal:
            artifact = _upload(detail, claim, "log", "result.log", b"synthetic result original\n")
            submission = _submission(detail, claim, [artifact])
            service.submit_verification_result(detail["id"], submission, machine)
            _confirm_stopped(detail, claim, confirmed=False)
        projected = service.get_verification_request(detail["id"], user)
        assert projected["can_delete"] is False and projected["delete_block_reason"] == "请先停止并等待执行结束"
        with pytest.raises(HTTPException) as active:
            service.delete_verification_request(detail["id"], user)
        assert active.value.status_code == 409 and active.value.detail == projected["delete_block_reason"]
    _confirm_stopped(detail, claim)
    ready = service.get_verification_request(detail["id"], user)
    assert ready["can_delete"] is True and ready["delete_block_reason"] is None and ready["cleanup"]["status"] == "removed"
    batch = service.create_verification_batch(detail["id"], user)
    copy_bytes = [_batch_member_disk_path(batch.id, member.id).read_bytes() for member in batch.members]
    followup, _ = service.create_verification_request(CreateVerificationRequest(
        schema_version=1, request_id=uuid4(), mode="retest", title="已有独立后轮",
        report={"type": "text", "content": "独立输入"}, previous_request_id=detail["id"],
    ), user)
    with db.get_conn() as conn:
        before = dict(conn.execute("SELECT result_json,terminal_digest,accepted_at,terminal_at FROM verification_requests WHERE id = ?", (detail["id"],)).fetchone())
    service.delete_verification_request(detail["id"], user)
    service.delete_verification_request(detail["id"], user)
    request = Request({"type": "http", "method": "GET", "path": "/projects/hidden", "headers": []})
    surfaces = [
        lambda: service.get_verification_request(detail["id"], user),
        lambda: service.verification_report(detail["id"], user),
        lambda: service.verification_material(detail["id"], detail["materials"][0]["id"], user),
        lambda: service.verification_material_preview(detail["id"], detail["materials"][0]["id"], user),
        lambda: service.verification_artifact(detail["id"], artifact.id, user),
        lambda: service.verification_artifact_preview(detail["id"], artifact.id, user),
        lambda: service.create_verification_batch(detail["id"], user),
        lambda: service.verification_result_receipt(detail["id"], submission, machine),
    ]
    for surface in surfaces:
        with pytest.raises(HTTPException) as hidden:
            surface()
        assert hidden.value.status_code == 410
    for caller in (user, machine):
        with pytest.raises(HTTPException) as project_hidden:
            require_tenant_project(detail["project_id"], request, caller)
        assert project_hidden.value.status_code == 404
    with pytest.raises(HTTPException) as project_hidden:
        require_user_tenant_project(detail["project_id"], request, user)
    assert project_hidden.value.status_code == 404
    listed = service.list_verification_requests(user, limit=50, offset=0)
    assert listed["total"] == 1 and [item["id"] for item in listed["items"]] == [followup["id"]]
    surviving = service.get_verification_request(followup["id"], user)
    assert surviving["previous_request_id"] is None and surviving["history"] == []
    assert [_batch_member_disk_path(batch.id, member.id).read_bytes() for member in batch.members] == copy_bytes
    for caller in (RequestPrincipal(kind="user", subject_id="other", tenant_id="other", role="user"),
                   RequestPrincipal(kind="integration", subject_id="other", tenant_id=user.tenant_id)):
        with pytest.raises(HTTPException) as wrong_owner:
            service.delete_verification_request(detail["id"], caller)
        assert wrong_owner.value.status_code == 404
    with pytest.raises(HTTPException) as replay:
        service.create_verification_request(CreateVerificationRequest(
            schema_version=1, request_id=detail["request_id"], mode="retest", title="即使输入改变也不能复活",
            report={"type": "text", "content": "changed"},
        ), user)
    assert replay.value.status_code == 410 and replay.value.detail == "此 request_id 对应的复测记录已删除，请使用新的请求 ID"
    with pytest.raises(HTTPException) as deleted_previous:
        service.create_verification_request(CreateVerificationRequest(
            schema_version=1, request_id=uuid4(), mode="retest", title="不能再引用已删轮次",
            report={"type": "text", "content": "new"}, previous_request_id=detail["id"],
        ), user)
    assert deleted_previous.value.status_code == 410 and deleted_previous.value.detail == service.SOURCE_DELETED_DETAIL
    with db.get_conn() as conn:
        after = conn.execute("SELECT * FROM verification_requests WHERE id = ?", (detail["id"],)).fetchone()
        assert after["deleted_at"] and after["purge_state"] == "completed"
        assert all(after[key] is None for key in before)
        assert conn.execute("SELECT 1 FROM projects WHERE id = ?", (detail["project_id"],)).fetchone() is None
        assert conn.execute("SELECT 1 FROM artifacts WHERE project_id = ? AND id = ?", (detail["project_id"], artifact.id)).fetchone() is None
        assert conn.execute("SELECT previous_request_id FROM verification_requests WHERE id = ?", (followup["id"],)).fetchone()["previous_request_id"] == detail["id"]


def test_copy_rechecks_deletion_after_entry_check(execution, monkeypatch):
    from test_external_verification_evidence import _submission, _upload

    user, machine, _model, _worker, detail, claim = execution
    artifact = _upload(detail, claim, "log", "observed.log", b"synthetic evidence")
    service.submit_verification_result(detail["id"], _submission(detail, claim, [artifact]), machine)
    _confirm_stopped(detail, claim)
    create_batch = service.create_project_resource_batch
    created = []

    def deletion_wins(**kwargs):
        # 模拟入口查询之后、实际复制事务之前删除已提交。
        service.delete_verification_request(detail["id"], user)
        batch = create_batch(**kwargs)
        created.append(batch.id)
        return batch

    monkeypatch.setattr(service, "create_project_resource_batch", deletion_wins)
    with pytest.raises(HTTPException) as stale_copy:
        service.create_verification_batch(detail["id"], user)
    assert stale_copy.value.status_code == 410 and stale_copy.value.detail == service.SOURCE_DELETED_DETAIL
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM project_resource_batch_members WHERE batch_id = ?", (created[0],)).fetchone()[0] == 0
