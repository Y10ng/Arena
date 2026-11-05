"""无 Docker／模型调用的报告执行域隔离与终止责任验证。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest
from docker.errors import DockerException

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import delivery, report
from cairn.dispatcher.worker_result_gateway import WorkerResultFailure, WorkerResultOutcome
from test_pi_execution import worker


def _delivery_snapshot(configured_worker):
    binding = {"report_segments": {"task_run_id": "report-origin"}}
    return {
        "session_id": "pi:11111111-1111-4111-8111-111111111111",
        "phase": "report_execute",
        "deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        "container_id": "report-exact-container",
        "model_binding_sha256": delivery.model_binding_digest(configured_worker),
        "result_schema_sha256": delivery.result_schema_digest("report_execute", binding, {}),
        "binding": binding,
        "inputs": {},
    }


@pytest.mark.parametrize("changed,value", [
    (None, None), ("project_id", "other"), ("container_role", "research"),
    ("report_task_run_id", "report-intermediate"), ("container_id", "replacement"),
    ("container_state", "exited"), ("container_state", "paused"), ("container_state", "unknown"),
])
def test_report_delivery_uses_only_original_owned_container(changed, value):
    configured_worker = worker()
    snapshot = _delivery_snapshot(configured_worker)
    calls = []
    metadata = {
        "project_id": "p", "container_role": "report", "report_task_run_id": "report-origin",
        "container_id": snapshot["container_id"], "container_state": "running",
    }
    if changed is not None:
        metadata[changed] = value

    def runtime_metadata(identity):
        calls.append(identity)
        return metadata

    manager = SimpleNamespace(runtime_metadata=runtime_metadata)
    context = {"delivery_retry": {"source_task_run_id": "report-intermediate"}, "execution_delivery": snapshot}
    if changed is None:
        identity, restored, remaining = delivery.resume_delivery(manager, "p", configured_worker, context, "report_execute")
        assert identity == snapshot["container_id"]
        assert restored is snapshot and remaining > 0
    else:
        with pytest.raises(delivery.DeliveryUnavailable):
            delivery.resume_delivery(manager, "p", configured_worker, context, "report_execute")
    assert calls == [snapshot["container_id"]]


@pytest.fixture
def report_domain(monkeypatch):
    state = SimpleNamespace(stops=[], starts=[], updates=[], files={}, process_containers=[],
                            stop_confirmed=True, output_status=201, worker_failure=None,
                            process_calls=0, state=report.ReportFinishState())
    configured_worker = SimpleNamespace(name="report-worker", type="pi", model_binding=None)
    template = "frozen report template"
    packet = json.dumps({"project": {"id": "p"}})
    state.metadata = {
        "container_id": "report-exact-container", "container_state": "running", "project_id": "p",
        "container_role": "report", "report_task_run_id": "report-current",
    }
    manager = SimpleNamespace(
        ensure_report_running=lambda project_id, task_run_id, options: (
            state.starts.append((project_id, task_run_id)) or "report-domain"
        ),
        runtime_metadata=lambda identity: state.metadata,
        write_text_file=lambda identity, path, content, **kwargs: state.files.update({path: content}),
        read_file=lambda identity, path: state.files[path].encode(),
        stop_owned_report=lambda project_id, identity: (
            state.stops.append((project_id, identity)) or state.stop_confirmed
        ),
    )
    state.manager = manager

    def create_artifact(project_id, **kwargs):
        return ApiResult(status_code=201, data={"id": "a1", "sha256": sha256(kwargs["content"].encode()).hexdigest()})

    def update_task_run(*args, **kwargs):
        state.updates.append(kwargs)
        return ApiResult(status_code=200)

    client = SimpleNamespace(create_artifact=create_artifact, update_task_run=update_task_run,
                             bind_assessment_report_input_snapshot=lambda *args, **kwargs: ApiResult(status_code=200))
    command = SimpleNamespace(candidate_spool=object(), report_segment_manifest_path="/tmp/segments.json",
                              http_evidence_manifest_path=None, input_files=())
    monkeypatch.setattr(report, "get_driver", lambda kind: SimpleNamespace(
        prepare_session=lambda: "pi:11111111-1111-4111-8111-111111111111", build_execute=lambda *args, **kwargs: command,
    ))
    monkeypatch.setattr(report, "_collect_report_packet_json", lambda *args, **kwargs: packet)
    monkeypatch.setattr(report, "_readable_input_references", lambda *args: (
        "packet", "graph", [SimpleNamespace(path="/tmp/report/packet-full.json")],
    ))
    monkeypatch.setattr(report, "_packet_vulnerability_context", lambda *args: ("[]", {}))
    monkeypatch.setattr(report, "_packet_coverage_json", lambda *args: "{}")
    monkeypatch.setattr(report, "_packet_list_json", lambda *args: "[]")
    monkeypatch.setattr(report, "record_prompt_rendered", lambda *args, **kwargs: None)
    monkeypatch.setattr(report, "record_process_artifacts", lambda *args, **kwargs: None)
    monkeypatch.setattr(report, "record_validation_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(report, "notify_browser_task", lambda *args, **kwargs: None)
    def register_delivery(*args, **kwargs):
        state.snapshot = {"binding": kwargs["binding"], "inputs": kwargs["inputs"], "session_id": kwargs["session"]}
        return state.snapshot
    monkeypatch.setattr(report, "register_delivery", register_delivery)
    monkeypatch.setattr(report, "remaining_delivery_seconds", lambda snapshot: 30)
    monkeypatch.setattr(report, "capture_report_segments", lambda *args, **kwargs: ({}, []))
    monkeypatch.setattr(report, "worker_process_failure", lambda *args, **kwargs: state.worker_failure)
    monkeypatch.setattr(report, "decode_pi_result", lambda *args, **kwargs: WorkerResultOutcome(
        payload={}, kind="report", validated={"artifacts": [], "segment_refs": []},
    ))
    monkeypatch.setattr(report, "assemble_report_segments", lambda *args, **kwargs: {})
    monkeypatch.setattr(report, "_frozen_report_packet", json.loads)
    monkeypatch.setattr(report, "_report_completeness_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(report, "_prepare_report_output", lambda *args, **kwargs: ("report", {}))
    monkeypatch.setattr(report, "pending_report_output", lambda *args, **kwargs: SimpleNamespace(
        artifact_sha256="a" * 64, replay=lambda client: ApiResult(status_code=state.output_status, data={"id": "a2"}),
    ))
    monkeypatch.setattr(report, "validate_report_artifact_receipt", lambda response, entry: response)

    def run_worker(*args, **kwargs):
        state.process_calls += 1
        state.process_containers.append(args[1])
        return ProcessResult(0, "", "")

    monkeypatch.setattr(report, "run_worker_process", run_worker)
    cancellation = TaskCancellation()
    state.cancellation = cancellation
    state.run = lambda task_run_id="report-current", **kwargs: report.run_report_task(
        SimpleNamespace(dispatcher_id="fixture", tasks=SimpleNamespace(report=SimpleNamespace(timeout=30, max_output_tokens=100))),
        client, manager, SimpleNamespace(project=SimpleNamespace(id="p", scenario_snapshot={}, model_dump=lambda **kwargs: {"id": "p"})), "graph", configured_worker,
        prompt_group="fixture", task_run_id=task_run_id, report_generation_token="generation",
        report_claim_token="claim", report_template_key="fixture", report_template_markdown=template,
        report_template_digest_sha256=sha256(template.encode()).hexdigest(),
        report_template_compatibility_profiles=["fixture"], durable_request=True, frozen_report_prompt="frozen",
        report_execution_plan={"packet_version": 2, "output_contract_version": report.REPORT_OUTPUT_CONTRACT_VERSION,
                               "report_data_schema_version": report.REPORT_DATA_SCHEMA_VERSION, "template_profile": "fixture"},
        finish_state=state.state, finish_outbox=SimpleNamespace(persist=lambda entry: None, replace=lambda old, new: None),
        cancellation=cancellation, **kwargs,
    )
    return state


@pytest.mark.parametrize("failure_class", [None, "process", "submission_missing", "submission_invalid", "cancelled"])
def test_report_terminal_cleanup_stops_only_its_owned_domain(report_domain, failure_class):
    state = report_domain
    if failure_class is not None:
        state.worker_failure = WorkerResultFailure(failure_class, "fixture", "fixture", "")
    if failure_class == "cancelled":
        state.cancellation.cancel("user_cancelled")
    outcome = state.run()
    assert state.starts == [("p", "report-current")]
    assert state.process_calls == 1
    assert state.process_containers == ["report-exact-container"]
    assert outcome == ("success" if failure_class is None else state.worker_failure)
    retained = failure_class in {"submission_missing", "submission_invalid"}
    assert state.stops == ([] if retained else [("p", "report-exact-container")])
    assert state.updates[-1]["runtime_context"]["execution_container_ids"] == ["report-exact-container"]
    if not retained:
        assert state.updates[-1]["runtime_context"]["execution_lifecycle"]["termination_confirmed"] is True


@pytest.mark.parametrize("cancelled", [False, True])
def test_report_output_pending_keeps_materials_but_cancellation_stops_domain(report_domain, cancelled):
    state = report_domain
    state.output_status = 503
    if cancelled:
        state.cancellation.cancel("user_cancelled")
    assert state.run() == "server_write_failed"
    assert state.state.delivery_pending is True
    assert state.stops == ([("p", "report-exact-container")] if cancelled else [])


@pytest.mark.parametrize("error", [None, DockerException("fixture"), RuntimeError("fixture")])
def test_report_stop_failure_cannot_return_success(report_domain, error):
    state = report_domain
    state.stop_confirmed = False
    if error is not None:
        def stop(project_id, container_id):
            raise error
        state.manager.stop_owned_report = stop
    assert state.run() == "container_failed"
    assert state.process_calls == 1 and state.state.artifact is not None
    assert state.state.task_run_result["status"] == "failed"
    assert state.updates[-1]["runtime_context"]["execution_lifecycle"]["termination_confirmed"] is False


@pytest.mark.parametrize("field,value", [
    ("project_id", "other"), ("container_role", "research"), ("report_task_run_id", "other-report"),
    ("container_id", "unknown"), ("container_state", "paused"),
])
def test_report_start_refuses_unconfirmed_or_foreign_domain(report_domain, field, value):
    report_domain.metadata[field] = value
    assert report_domain.run() == "delivery_unavailable"
    assert report_domain.process_calls == 0
    assert report_domain.stops == []


def test_report_cleanup_failure_still_stops_domain(report_domain, monkeypatch):
    def failed_cleanup(result):
        raise OSError("fixture")
    monkeypatch.setattr(ProcessResult, "cleanup", failed_cleanup)
    with pytest.raises(OSError, match="fixture"):
        report_domain.run()
    assert report_domain.stops == [("p", "report-exact-container")]


def test_report_delivery_rejects_missing_original_report_owner():
    configured_worker = worker()
    snapshot = _delivery_snapshot(configured_worker)
    snapshot["binding"]["report_segments"]["task_run_id"] = None
    snapshot["result_schema_sha256"] = delivery.result_schema_digest("report_execute", snapshot["binding"], {})
    manager = SimpleNamespace(runtime_metadata=lambda identity: {
        "project_id": "p", "container_role": "report", "container_id": snapshot["container_id"],
        "container_state": "running",
    })
    with pytest.raises(delivery.DeliveryUnavailable):
        delivery.resume_delivery(manager, "p", configured_worker,
                                 {"delivery_retry": True, "execution_delivery": snapshot}, "report_execute")


def test_report_retry_keeps_original_domain_and_segment_authority(report_domain, monkeypatch):
    state = report_domain
    state.worker_failure = WorkerResultFailure("submission_missing", "fixture", "fixture", "")
    assert state.run() == state.worker_failure
    snapshot = state.snapshot
    state.worker_failure = None
    state.state = report.ReportFinishState()
    state.starts.clear()
    monkeypatch.setattr(report, "resume_delivery", lambda *args: ("report-exact-container", snapshot, 30))
    assert state.run(task_run_id="report-retry", delivery_context={"delivery_retry": True}) == "success"
    assert state.starts == []
    assert state.process_containers == ["report-exact-container", "report-exact-container"]
    assert snapshot["binding"]["report_segments"]["task_run_id"] == "report-current"
    assert state.stops == [("p", "report-exact-container")]
