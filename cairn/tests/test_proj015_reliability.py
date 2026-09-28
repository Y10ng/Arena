from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from cairn.dispatcher.models import BindingCircuitKey, ReasonCheckpoint, RunningTask
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db
from cairn.server.models import CompleteRequest
from cairn.server.services import project_completion


class DoneFuture:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome

    def done(self) -> bool:
        return True

    def result(self) -> str:
        return self.outcome




def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn)
    return conn


def binding_circuit_key(
    *,
    model_revision: int = 3,
    credential_version: int = 4,
    inference_endpoint: str = "https://model.test/v1/messages",
) -> BindingCircuitKey:
    return BindingCircuitKey(
        model_id="model-1",
        model_revision=model_revision,
        credential_version_id="tmcv-1",
        credential_version=credential_version,
        protocol="anthropic-messages",
        auth_header="x-api-key",
        inference_endpoint=inference_endpoint,
    )


def bare_loop() -> DispatcherLoop:
    loop = object.__new__(DispatcherLoop)
    loop.worker_cooldown_until = {}
    loop.worker_rejected_until = {}
    loop.worker_cooldown_attempts = {}
    loop.worker_rejected_attempts = {}
    loop.binding_auth_circuit_until = {}
    loop.binding_auth_circuit_attempts = {}
    loop.reason_checkpoints = {}
    loop.report_finish_outbox = SimpleNamespace(entries=lambda: [])
    loop._clear_project_log_state = lambda project_id: None
    loop._clear_log_state = lambda key: None
    return loop


def _reason_project(
    *,
    enabled: bool = True,
    cell_count: int = 0,
    stop_reason: str | None = None,
):
    return SimpleNamespace(
        project=SimpleNamespace(
            id="p1",
            execution_generation=0,
            assessment_protocol="default-copy-v1",
            assessment_execution={
                "version": 2, "executable": True, "execution_contract_version": 1,
                "execution_engine": "pi", "enabled": enabled, "phase": "baseline",
            },
            coverage_checkpoint_cell_count=cell_count,
            coverage_stop_reason=stop_reason,
        ),
        facts=[],
        hints=[],
        intents=[],
    )


def test_structured_coverage_state_changes_trigger_reason_once() -> None:
    loop = bare_loop()
    loop.reason_checkpoints["p1"] = ReasonCheckpoint(
        fact_count=0,
        hint_count=0,
        open_intent_count=0,
        coverage_checkpoint_cell_count=0,
        coverage_stop_reason=None,
    )

    assert loop._reason_trigger(_reason_project(cell_count=1)) == "coverage_cells:0->1"
    assert loop._reason_trigger(
        _reason_project(stop_reason="budget_exhausted")
    ) == "coverage_stop:none->budget_exhausted"

    loop.reason_checkpoints["p1"] = ReasonCheckpoint(
        fact_count=0,
        hint_count=0,
        open_intent_count=0,
        coverage_checkpoint_cell_count=1,
        coverage_stop_reason="budget_exhausted",
    )
    assert loop._reason_trigger(
        _reason_project(cell_count=1, stop_reason="budget_exhausted")
    ) is None


def test_disabled_execution_view_ignores_coverage_changes_despite_protocol_label() -> None:
    loop = bare_loop()
    loop.reason_checkpoints["p1"] = ReasonCheckpoint(
        fact_count=0,
        hint_count=0,
        open_intent_count=0,
        coverage_checkpoint_cell_count=0,
        coverage_stop_reason=None,
    )

    assert loop._reason_trigger(
        _reason_project(
            enabled=False,
            cell_count=3,
            stop_reason="deadline_exceeded",
        )
    ) is None








def test_contract_error_is_terminal_without_clearing_worker_health_attempts() -> None:
    loop = object.__new__(DispatcherLoop)
    loop.worker_cooldown_until = {}
    loop.worker_rejected_until = {}
    loop.worker_cooldown_attempts = {("worker-a", "api_incompatible"): 2}
    loop.worker_rejected_attempts = {}
    loop.reason_checkpoints = {}
    loop.futures = {
        DoneFuture("contract_error"): RunningTask(
            project_id="p1",
            task_type="explore",
            worker_name="worker-a",
            worker_type="pi",
            cancellation=TaskCancellation(),
            container_runtime=None,
            task_run_id="tr001",
            intent_id="i001",
        )
    }
    finishes: list[dict[str, object]] = []
    loop._finish_task_run = lambda *args, **kwargs: finishes.append({"args": args, "kwargs": kwargs})
    loop._clear_project_log_state = lambda project_id: None

    loop._reap_futures()

    assert loop.worker_cooldown_attempts[("worker-a", "api_incompatible")] == 2
    assert loop.worker_rejected_attempts == {}
    assert finishes[0]["kwargs"]["error_type"] == "contract_error"


def test_worker_oom_uses_project_task_rejection_without_global_cooldown() -> None:
    loop = bare_loop()
    loop.futures = {
        DoneFuture("worker_oom"): RunningTask(
            project_id="p1",
            task_type="bootstrap",
            worker_name="worker-a",
            worker_type="pi",
            cancellation=TaskCancellation(),
            container_runtime=None,
            task_run_id="tr001",
            intent_id="i001",
        )
    }
    finishes: list[dict[str, object]] = []
    loop._finish_task_run = lambda *args, **kwargs: finishes.append(
        {"args": args, "kwargs": kwargs}
    )

    loop._reap_futures()

    rejection_key = ("p1", "bootstrap", "worker-a")
    assert finishes[0]["kwargs"]["error_type"] == "worker_oom"
    assert rejection_key in loop.worker_rejected_until
    assert loop.worker_rejected_attempts[(*rejection_key, "worker_oom")] == 1
    assert ("p2", "bootstrap", "worker-a") not in loop.worker_rejected_until
    assert ("p1", "reason", "worker-a") not in loop.worker_rejected_until
    assert loop.worker_cooldown_until == {}
    assert loop.worker_cooldown_attempts == {}


def test_stale_reconcile_restores_worker_oom_from_command_event() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.updates: list[dict[str, object]] = []

        def list_task_runs(self, project_id: str):
            return SimpleNamespace(
                ok=True,
                data=[
                    {
                        "id": "tr001",
                        "status": "running",
                        "task_type": "bootstrap",
                        "worker_name": "worker-a",
                        "worker_type": "pi",
                        "started_at": "2026-08-19T00:00:00Z",
                    }
                ],
            )

        def list_task_run_command_events(
            self,
            project_id: str,
            task_run_ids: list[str],
        ):
            return SimpleNamespace(
                ok=True,
                data=[
                    {
                        "task_run_id": "tr001",
                        "events": [
                            {
                                "kind": "command_finished",
                                "phase": "bootstrap_execute",
                                "command_kind": "cairn-agent",
                                "status": "failed",
                                "returncode": 137,
                                "exit_code": 137,
                                "timed_out": True,
                                "cancelled": False,
                                "oom_killed": True,
                                "oom_evidence_source": "cgroup_v2_memory_events",
                            }
                        ],
                    }
                ],
            )

        def update_task_run(self, project_id: str, task_run_id: str, **kwargs):
            self.updates.append(
                {"project_id": project_id, "task_run_id": task_run_id, **kwargs}
            )
            return SimpleNamespace(ok=True)

    loop = bare_loop()
    loop.futures = {}
    loop.dispatcher_id = "dispatcher-test"
    loop.client = FakeClient()

    loop._reconcile_stale_running_task_runs([SimpleNamespace(id="p1")])

    update = loop.client.updates[0]
    startup = update["runtime_context"]["startup_reconcile"]
    assert update["status"] == "failed"
    assert update["error_type"] == "worker_oom"
    assert startup["reason"] == "latest_command_finished_oom"
    assert startup["latest_command_oom_killed"] is True
    assert startup["latest_command_oom_evidence_source"] == "cgroup_v2_memory_events"
    rejection_key = ("p1", "bootstrap", "worker-a")
    assert rejection_key in loop.worker_rejected_until
    assert loop.worker_rejected_attempts[(*rejection_key, "worker_oom")] == 1
    assert ("p2", "bootstrap", "worker-a") not in loop.worker_rejected_until
    assert ("p1", "reason", "worker-a") not in loop.worker_rejected_until
    assert loop.worker_cooldown_until == {}
    assert loop.worker_cooldown_attempts == {}


def test_stale_reconcile_leaves_report_journal_task_run_untouched() -> None:
    from cairn.dispatcher.tasks.report_finish_outbox import pending_report_execution

    entry = pending_report_execution(
        project_id="p1",
        authenticated_dispatcher_id="dispatcher-test",
        worker="worker-a",
        claim_sha256="a" * 64,
        generation_token="generation-1",
        task_run_id="report-run-1",
        input_snapshot_artifact_id="snapshot-1",
        input_snapshot_sha256="b" * 64,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.command_lists: list[list[str]] = []
            self.updates: list[dict[str, object]] = []

        def list_task_runs(self, project_id: str):
            return SimpleNamespace(
                ok=True,
                data=[
                    {
                        "id": entry.task_run_id,
                        "status": "running",
                        "task_type": "report",
                        "worker_name": entry.worker,
                        "worker_type": "pi",
                        "started_at": "2026-09-18T00:00:00Z",
                    }
                ],
            )

        def list_task_run_command_events(
            self,
            project_id: str,
            task_run_ids: list[str],
        ):
            self.command_lists.append(task_run_ids)
            return SimpleNamespace(ok=True, data=[])

        def update_task_run(self, project_id: str, task_run_id: str, **kwargs):
            self.updates.append(
                {"project_id": project_id, "task_run_id": task_run_id, **kwargs}
            )
            return SimpleNamespace(ok=True)

    loop = bare_loop()
    loop.futures = {}
    loop.dispatcher_id = "dispatcher-test"
    loop.client = FakeClient()
    loop.report_finish_outbox = SimpleNamespace(entries=lambda: [entry])

    loop._reconcile_stale_running_task_runs([SimpleNamespace(id="p1")])

    assert loop.client.command_lists == []
    assert loop.client.updates == []


def test_provider_auth_opens_shared_binding_circuit_without_worker_cooldown() -> None:
    loop = bare_loop()
    key = binding_circuit_key()
    loop.futures = {
        DoneFuture("provider_auth"): RunningTask(
            project_id="p1",
            task_type="explore",
            worker_name="worker-a",
            worker_type="pi",
            cancellation=TaskCancellation(),
            container_runtime=None,
            task_run_id="tr001",
            intent_id="i001",
            binding_circuit_key=key,
        )
    }
    finishes: list[dict[str, object]] = []
    loop._finish_task_run = lambda *args, **kwargs: finishes.append(
        {"args": args, "kwargs": kwargs}
    )

    loop._reap_futures()

    assert finishes[0]["kwargs"]["error_type"] == "provider_auth"
    assert loop.worker_rejected_until == {}
    assert loop.binding_auth_circuit_attempts[key] == 1
    assert loop.binding_auth_circuit_until[key] > 0
    assert loop.worker_cooldown_until == {}
    assert loop.worker_cooldown_attempts == {}


def test_regular_config_mismatch_keeps_worker_cooldown_behavior() -> None:
    loop = bare_loop()
    key = binding_circuit_key()
    loop.futures = {
        DoneFuture("config_mismatch"): RunningTask(
            project_id="p1",
            task_type="reason",
            worker_name="worker-a",
            worker_type="pi",
            cancellation=TaskCancellation(),
            container_runtime=None,
            task_run_id="tr001",
            binding_circuit_key=key,
        )
    }
    loop._finish_task_run = lambda *args, **kwargs: None

    loop._reap_futures()

    assert loop.worker_cooldown_attempts[("worker-a", "config_mismatch")] == 1
    assert loop.binding_auth_circuit_until == {}


def test_expired_binding_circuit_preserves_attempt_count_for_backoff() -> None:
    loop = bare_loop()
    key = binding_circuit_key()
    loop.binding_auth_circuit_until[key] = 0.0
    loop.binding_auth_circuit_attempts[key] = 1

    assert loop._binding_auth_circuit_retry_after(key) is None
    assert key not in loop.binding_auth_circuit_until
    assert loop.binding_auth_circuit_attempts[key] == 1

    retry_after, attempt = loop._mark_binding_auth_circuit(key)

    assert attempt == 2
    assert retry_after == 10.0


def test_success_clears_only_the_exact_binding_circuit() -> None:
    loop = bare_loop()
    completed_key = binding_circuit_key()
    other_key = binding_circuit_key(credential_version=5)
    loop.binding_auth_circuit_until = {completed_key: 100.0, other_key: 200.0}
    loop.binding_auth_circuit_attempts = {completed_key: 1, other_key: 2}
    loop.futures = {
        DoneFuture("success"): RunningTask(
            project_id="p1",
            task_type="bootstrap",
            worker_name="worker-a",
            worker_type="pi",
            cancellation=TaskCancellation(),
            container_runtime=None,
            task_run_id="tr001",
            intent_id="i001",
            binding_circuit_key=completed_key,
        )
    }
    loop._finish_task_run = lambda *args, **kwargs: None

    loop._reap_futures()

    assert completed_key not in loop.binding_auth_circuit_until
    assert completed_key not in loop.binding_auth_circuit_attempts
    assert loop.binding_auth_circuit_until[other_key] == 200.0
    assert loop.binding_auth_circuit_attempts[other_key] == 2


def test_binding_circuit_key_changes_with_authoritative_revision() -> None:
    base = binding_circuit_key()

    assert binding_circuit_key(model_revision=4) != base
    assert binding_circuit_key(credential_version=5) != base
    assert binding_circuit_key(
        inference_endpoint="https://model.test/gateway/v1/messages"
    ) != base


def test_complete_gate_blocks_then_allows(monkeypatch) -> None:
    """完成硬门：有 open intent 时 complete → 409；收尾后 complete → 成功。"""
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    try:
        monkeypatch.setattr(project_completion, "get_conn", fake_get_conn)
        snapshot = {"features": {
            "execution_contract_version": 1, "execution_engine": "pi",
            "assessment_contract_version": 2, "surface_manifest_version": 1,
            "minimum_coverage_version": 1,
        }}
        progress = {"version": 1, "phase": "finalizing", "depth_review_fact_id": "f001"}
        conn.execute(
            """INSERT INTO projects
                (id, title, status, created_at, scenario_key, assessment_protocol,
                 scenario_snapshot_json, assessment_progress_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("p1", "Project", "active", "2026-06-08T00:00:00Z", "default-copy-v2",
             "default-copy-v1", json.dumps(snapshot), json.dumps(progress)),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES (?, ?, ?, ?, ?)",
            ("f001", "p1", "source", "system", "2026-06-08T00:00:00Z"),
        )
        # 覆盖义务已处置，只让开放 Intent 成为本测试的完成阻断条件。
        conn.execute(
            """INSERT INTO surface_manifest_entries
                (entry_id, project_id, surface_id, surface_kind, surface_key,
                 traits_json, evidence_json, observation_fingerprint, created_at)
                VALUES ('m1', 'p1', 's1', 'endpoint', 'GET https://example.test/',
                        '{}', '[]', 'fixture', '2026-06-08T00:00:00Z')"""
        )
        conn.execute(
            """INSERT INTO coverage_slots
                (project_id, slot_key, kind, description, status, surface_id, surface_key,
                 surface_kind, test_family, coverage_requirement, result, resulting_fact_id,
                 created_at, updated_at)
                VALUES ('p1', 'cell:001', 'endpoint', 'fixture', 'explored', 's1',
                        'GET https://example.test/', 'endpoint', 'configuration', 'required',
                        'tested_no_finding', 'f001', '2026-06-08T00:00:00Z', '2026-06-08T00:00:00Z')"""
        )
        for intent_id, worker in (("i001", None), ("i002", "worker-a")):
            conn.execute(
                """
                INSERT INTO intents
                    (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at)
                VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    intent_id,
                    "p1",
                    f"open {intent_id}",
                    "reason",
                    worker,
                    "2026-06-08T00:01:00Z" if worker else None,
                    "2026-06-08T00:01:00Z",
                ),
            )
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (intent_id, "p1", "f001"),
            )
        conn.execute(
            "INSERT INTO scoped_counters (project_id, kind, value) VALUES (?, ?, ?)",
            ("p1", "intent", 2),
        )

        # 有 open intent → complete 被硬门拒绝
        with pytest.raises(HTTPException) as exc:
            project_completion.complete_project_record(
                "p1",
                CompleteRequest(from_=["f001"], description="done", worker="worker-a"),
            )
        assert exc.value.status_code == 409
        assert "Resolve open intents before complete" in exc.value.detail
        assert conn.execute("SELECT status FROM projects WHERE id = 'p1'").fetchone()[0] == "active"

        # 收尾：abandon 未认领的 i001；手动收敛 i002（模拟 explore 跑完）
        from cairn.server.services import projects as projects_service
        projects_service.abandon_intent_record(conn, "p1", "i001", "worker-a")
        conn.execute(
            "UPDATE intents SET to_fact_id = 'f001', concluded_at = '2026-06-08T00:02:00Z', conclusion = 'completed' "
            "WHERE id = 'i002' AND project_id = 'p1'"
        )

        # 无 open intent → complete 成功
        ci = project_completion.complete_project_record(
            "p1",
            CompleteRequest(from_=["f001"], description="done", worker="worker-a"),
        )
        assert ci.to == "goal"
    finally:
        conn.close()
