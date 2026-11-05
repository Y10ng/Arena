"""定向验证研究交付预算扣暂停、授权不平移及宿主 detach 不释放现场。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from importlib import import_module
import json
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime import pause_clock as clocks
from cairn.dispatcher.tasks import delivery
from cairn.server.services import assessment_attempts as attempts
from test_pi_execution import worker


SESSION = "pi:11111111-1111-4111-8111-111111111111"
START = datetime(2050, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def timer(monkeypatch):
    state = SimpleNamespace(seconds=0.0)

    class WallClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return START + timedelta(seconds=state.seconds)

    monkeypatch.setattr(delivery, "datetime", WallClock)
    monkeypatch.setattr(clocks, "time", SimpleNamespace(monotonic=lambda: state.seconds))
    return state


def _project(*, expiry=None, paused_seconds=0):
    return SimpleNamespace(project=SimpleNamespace(
        id="p", assessment_execution={"enabled": False}, vulnerability_fingerprint_version=None, scenario_snapshot={},
        explore_deadline=None, authorization_expires_at=expiry,
        execution_control={"paused_seconds": paused_seconds},
    ), facts=[], hints=[])


def _snapshot(phase="bootstrap_execute", *, expiry=None, paused_seconds=0):
    binding = {"allowed_fact_ids": ["origin"], "max_intents": 3} if phase == "reason_execute" else None
    inputs = {"open_intent_ids": []} if phase == "reason_execute" else {}
    return {
        "session_id": SESSION, "phase": phase, "deadline_at": (START + timedelta(seconds=60)).isoformat(),
        "authorization_expires_at": expiry, "paused_seconds": paused_seconds,
        "container_id": "exact-container", "model_binding_sha256": delivery.model_binding_digest(worker()),
        "result_schema_sha256": delivery.result_schema_digest(phase, binding, inputs),
        "binding": binding, "inputs": inputs,
    }


def test_pause_clock_counts_current_and_completed_windows(timer):
    clock = clocks.PauseClock()
    timer.seconds = 5
    clock.pause()
    timer.seconds = 305
    assert clock.paused_seconds == 300
    assert clock.now() == 5
    clock.resume()
    timer.seconds = 310
    assert clock.paused_seconds == 300
    assert clock.now() == 10


def test_registration_ack_wait_preserves_budget_and_uses_fresh_project_baseline(timer):
    clock = clocks.PauseClock()
    project = _project(paused_seconds=300)
    stale = _project()
    budget = delivery.ResearchDeliveryBudget(clock)
    updates = []

    def register(*args, **kwargs):
        updates.append(kwargs)
        clock.pause()
        timer.seconds += 600
        clock.resume()
        timer.seconds += 2
        return SimpleNamespace(ok=True)

    client = SimpleNamespace(get_project=lambda _: project, update_task_run=register)
    manager = SimpleNamespace(runtime_metadata=lambda _: {"container_state": "running", "container_id": "exact-container"})
    snapshot = delivery.register_delivery(
        client, manager, "name", stale, "same-task", worker(), phase="bootstrap_execute",
        session=SESSION, timeout_seconds=60, budget=budget,
    )
    assert snapshot["paused_seconds"] == 300
    assert delivery.remaining_delivery_seconds(snapshot, budget=budget) == 58
    assert updates[0]["runtime_context"]["execution_delivery"] == snapshot


class _ExecuteObserved(BaseException):
    pass


@pytest.mark.parametrize("task_name", ["bootstrap", "reason", "explore"])
@pytest.mark.parametrize("case", ["prepare_pause", "authorization_expired", "delivery_retry", "detach_prepare", "detach_result"])
def test_research_entrypoints_preserve_budget_identity_and_detached_claims(monkeypatch, timer, task_name, case):
    module = import_module(f"cairn.dispatcher.tasks.{task_name}")
    clock = clocks.PauseClock()
    cancellation = TaskCancellation()
    events, commands, updates = [], [], []
    expiry = (START + timedelta(seconds=30)).isoformat() if case == "authorization_expired" else None
    project = _project(expiry=expiry)
    lease = SimpleNamespace(failure=None, start=lambda: events.append("lease_start"), stop=lambda: events.append("lease_stop"))
    monkeypatch.setattr(module, "HeartbeatLease", SimpleNamespace(for_intent=lambda *a: lease, for_reason=lambda *a: lease))
    for name in ("surface_manifest_enabled", "minimum_coverage_enabled", "blocker_cooldown_enabled"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, lambda _: False)
    if hasattr(module, "outcome_contract_version"):
        monkeypatch.setattr(module, "outcome_contract_version", lambda _: 1)
    for name in ("bootstrap_prompt_replacements", "explore_prompt_replacements"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, lambda *a, **k: {})
    if hasattr(module, "bootstrap_retry_prompt_context"):
        monkeypatch.setattr(module, "bootstrap_retry_prompt_context", lambda *a, **k: "")
    if hasattr(module, "build_reason_prompt_context"):
        monkeypatch.setattr(module, "build_reason_prompt_context", lambda *a, **k: SimpleNamespace(
            replacements={}, allowed_fact_ids=["origin"], open_intents=[], coverage_slots=[],
            binding={"allowed_fact_ids": ["origin"], "max_intents": 3},
        ))
    monkeypatch.setattr(module, "load_prompt", lambda *a: "fixture")
    monkeypatch.setattr(module, "render_prompt", lambda *a: "fixture")
    monkeypatch.setattr(module, "record_prompt_rendered", lambda *a, **k: None)
    for name in ("best_effort_release", "best_effort_release_reason", "conclude_or_release_on_failure"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, lambda *a, **k: events.append("business_release"))

    def build_execute(*args, **kwargs):
        commands.append(kwargs)
        return SimpleNamespace(session=SESSION)

    monkeypatch.setattr(module, "get_driver", lambda _: SimpleNamespace(
        prepare_session=lambda: SESSION, build_execute=build_execute,
    ))

    def prepare(*args):
        assert cancellation.pause_clock is clock
        events.append("prepare")
        if case == "detach_prepare":
            clock.detach()
            clock.check_attached()
        clock.pause()
        timer.seconds += 600
        clock.resume()
        timer.seconds += 2
        return {"scope_id": "same-scope"}

    def runtime_metadata(_):
        if case == "delivery_retry":
            clock.pause()
            timer.seconds += 300
            clock.resume()
            timer.seconds += 2
        return {"container_state": "paused" if case == "delivery_retry" else "running", "container_id": "exact-container"}

    manager = SimpleNamespace(
        research_clock=lambda _: clock, ensure_running=lambda *a: "name", container_name=lambda _: "name",
        runtime_metadata=runtime_metadata, prepare_task_execution=prepare,
        stop_task_execution=lambda *a: events.append("physical_stop"),
    )
    client = SimpleNamespace(
        get_project=lambda _: project,
        update_task_run=lambda *a, **k: updates.append(k) or SimpleNamespace(ok=True),
    )
    result = SimpleNamespace(read_stdout=lambda: "", cleanup=lambda: events.append("result_cleanup"))

    def run_process(*args, **kwargs):
        assert cancellation.pause_clock is clock
        events.append("execute")
        if case == "detach_result":
            return result
        raise _ExecuteObserved()

    def record_process(*args, **kwargs):
        clock.detach()
        clock.check_attached()

    monkeypatch.setattr(module, "run_worker_process", run_process)
    monkeypatch.setattr(module, "record_process_artifacts", record_process)
    config = SimpleNamespace(
        runtime=SimpleNamespace(interval=1, prompt_group="default"),
        tasks=SimpleNamespace(**{name: SimpleNamespace(timeout=60, conclude_timeout=5, max_intents=3)
                                 for name in ("bootstrap", "reason", "explore")}),
    )
    kwargs = dict(config=config, client=client, container_manager=manager, project=project,
                  worker=worker(), cancellation=cancellation, task_run_id="same-task")
    if task_name != "reason":
        kwargs["intent"] = SimpleNamespace(id="intent", assessment_role="discovery", slot_key=None)
    if task_name != "bootstrap":
        kwargs["export_yaml"] = "fixture"
    if case == "delivery_retry":
        snapshot = _snapshot(f"{task_name}_execute")
        frozen = deepcopy(snapshot)
        timer.seconds = 620
        project.project.execution_control["paused_seconds"] = 600
        kwargs["delivery_context"] = {"delivery_retry": {"source_task_run_id": "original-task"}, "execution_delivery": snapshot}
    run = getattr(module, f"run_{task_name}_task")
    if case == "authorization_expired":
        assert run(**kwargs) == "delivery_unavailable"
        assert commands == [] and "execute" not in events
    else:
        with pytest.raises(clocks.ExecutionDetached if case.startswith("detach") else _ExecuteObserved):
            run(**kwargs)
    if case.startswith("detach"):
        assert "physical_stop" not in events and "business_release" not in events
        assert cancellation.detached and cancellation.reason is None
        if case == "detach_result":
            assert "result_cleanup" in events
    else:
        assert "physical_stop" in events
    assert "lease_stop" in events
    if case == "prepare_pause":
        assert len(commands) == 1 and commands[0]["timeout_seconds"] == 58
        assert commands[0]["task_run_id"] == "same-task"
        assert commands[0]["task_execution"] == {"scope_id": "same-scope"}
        assert updates[0]["session_id"] == SESSION
    elif case == "delivery_retry":
        assert len(commands) == 1 and commands[0]["timeout_seconds"] == 38
        assert commands[0]["resume"] and commands[0]["conclude_only"]
        assert "prepare" not in events and updates == []
        assert snapshot == frozen


class _Rows(list):
    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


def _server_fixture(snapshot):
    context = {"dispatcher_id": "owner", "execution_delivery": snapshot,
               "execution_lifecycle": {"generation": 0, "termination_confirmed": True}}
    project = {"id": "p", "execution_generation": 0, "explore_deadline": (START + timedelta(seconds=10000)).isoformat(),
               "authorization_expires_at": snapshot["authorization_expires_at"], "execution_control_json": "{}"}
    run = {"id": "t", "task_type": "bootstrap", "status": "running", "execution_generation": 0,
           "pause_requested": 0, "session_id": SESSION, "runtime_context_json": json.dumps(context)}
    conn = SimpleNamespace(execute=lambda *a: _Rows())
    body = SimpleNamespace(status="running", session_id=SESSION)
    return project, run, conn, body


def test_server_registration_keeps_execution_budget_separate_from_authorization(monkeypatch):
    snapshot = _snapshot(expiry=(START + timedelta(seconds=20)).isoformat())
    project, run, conn, body = _server_fixture(snapshot)
    monkeypatch.setattr(attempts, "utcnow", lambda: START.isoformat())
    attempts.validate_delivery_registration(conn, project, run, body, snapshot)
    snapshot["authorization_expires_at"] = (START + timedelta(days=1)).isoformat()
    with pytest.raises(HTTPException, match="execution_delivery_invalid"):
        attempts.validate_delivery_registration(conn, project, run, body, snapshot)


@pytest.mark.parametrize("expiry,usable", [(None, True), (200, False)])
def test_server_delivery_retry_subtracts_only_post_registration_pause(expiry, usable):
    snapshot = _snapshot(paused_seconds=300, expiry=(START + timedelta(seconds=expiry)).isoformat() if expiry else None)
    project, run, _, _ = _server_fixture(snapshot)
    project["execution_control_json"] = json.dumps({"paused_seconds": 900})
    run["status"] = "failed"
    assert attempts._delivery_source_usable(project, run, (START + timedelta(seconds=620)).isoformat()) is usable
    if usable:
        assert not attempts._delivery_source_usable(project, run, (START + timedelta(seconds=661)).isoformat())


@pytest.mark.parametrize("baseline", [-1, True, float("nan")])
def test_server_rejects_invalid_pause_baselines(monkeypatch, baseline):
    snapshot = _snapshot(paused_seconds=baseline)
    project, run, conn, body = _server_fixture(snapshot)
    monkeypatch.setattr(attempts, "utcnow", lambda: START.isoformat())
    with pytest.raises(HTTPException, match="execution_delivery_invalid"):
        attempts.validate_delivery_registration(conn, project, run, body, snapshot)


def test_reason_output_retry_uses_original_pause_baseline_without_new_budget(timer):
    clock = clocks.PauseClock()
    budget = delivery.ResearchDeliveryBudget(clock)
    project = _project(paused_seconds=600)
    timer.seconds = 620
    client = SimpleNamespace(get_project=lambda _: project, update_task_run=lambda *a, **k: SimpleNamespace(ok=True))
    manager = SimpleNamespace(runtime_metadata=lambda _: {"container_state": "running", "container_id": "exact-container"})
    snapshot = delivery.register_delivery(
        client, manager, "name", project, "reason-retry", worker(), phase="reason_execute", session=SESSION,
        timeout_seconds=60, binding={"allowed_fact_ids": ["origin"], "max_intents": 3},
        inputs={"open_intent_ids": []}, deadline_at=(START + timedelta(seconds=60)).isoformat(),
        deadline_paused_seconds=0, budget=budget,
    )
    assert delivery.remaining_delivery_seconds(snapshot, budget=budget) == 40
    assert snapshot["paused_seconds"] == 600
