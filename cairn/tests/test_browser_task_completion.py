from __future__ import annotations

from concurrent.futures import Future
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cairn.dispatcher.config import PiModelConfig, RuntimeConfig, WorkerConfig
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.browser_sessions import BrowserSessions
from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import report
from cairn.dispatcher.worker_result_gateway import WorkerResultFailure, worker_failure_error_type
from cairn.dispatcher.scheduler.cleanup import CleanupMixin
from cairn.dispatcher.scheduler.task_lifecycle import TaskLifecycleMixin
from cairn.dispatcher.scheduler.task_run_finish import TaskRunFinishMixin
from cairn.dispatcher.scheduler.worker_runtime import WorkerRuntimeMixin
from report_test_support import ephemeral_report_finish_outbox


class FinishClient:
    def __init__(self, *statuses):
        self.statuses = iter(statuses or (200,))
        self.updates = []

    def update_task_run(self, project_id, task_run_id, **kwargs):
        self.updates.append((project_id, task_run_id, kwargs))
        return ApiResult(status_code=next(self.statuses))


class FinishHarness(TaskRunFinishMixin):
    def __init__(self, client, manager):
        self.client = client
        self.container_manager = manager
        self.futures = {}
        self.cleanup_executor = ManualExecutor()

    def complete_finishes(self):
        self._flush_pending_task_run_finishes()
        for index, (future, _, _) in enumerate(list(self.cleanup_executor.submitted)):
            if not future.done():
                self.cleanup_executor.complete(index)
        self._flush_pending_task_run_finishes()
        self.cleanup_executor.submitted = [item for item in self.cleanup_executor.submitted if not item[0].done()]

    def _finish_runtime_context(self, *args):
        # 测试 seam 不执行 Docker 版本探针，只验证收尾门槛。
        return {}


class LifecycleHarness(TaskLifecycleMixin, FinishHarness, CleanupMixin, WorkerRuntimeMixin):
    def __init__(self, client, manager):
        super().__init__(client, manager)
        self.futures = {}
        self._log_state = {}
        self.worker_cooldown_until = {}
        self.worker_cooldown_attempts = {}
        self.worker_rejected_until = {}
        self.worker_rejected_attempts = {}
        self.cleanup_futures = {}
        self._cleanup_pending = set()
        self._inactive_cleanup_done = {}


def running_task(*, task_run_id="run-1", task_type="explore"):
    return RunningTask(
        "project-1", task_type, "worker-1", "mock",
        TaskCancellation(), None, task_run_id=task_run_id,
    )


def browser_manager(*, clock=lambda: 0):
    return SimpleNamespace(browser_sessions=BrowserSessions(clock=clock))


def prepare_session(manager, task_run_id="run-1"):
    session = manager.browser_sessions.prepare(
        "project-1", task_run_id, "container-1", env={}, context={}
    )
    assert session is not None
    return session


def runtime_config(**overrides):
    return RuntimeConfig.model_validate({
        "max_workers": 4,
        "max_running_projects": 2,
        "max_project_workers": 2,
        "interval": 3,
        "healthcheck_timeout": 30,
        "prompt_group": "mock",
        **overrides,
    })


def test_browser_policy_is_opt_in_and_rejects_unknown_policy():
    assert runtime_config().playwright_session_policy == "legacy"
    assert runtime_config(playwright_session_policy="task").playwright_session_policy == "task"
    with pytest.raises(ValidationError):
        runtime_config(playwright_session_policy="shared")


def test_apply_config_updates_browser_policy_and_supports_legacy_managers():
    from cairn.dispatcher.scheduler.config_runtime import ConfigRuntimeMixin

    class Harness(ConfigRuntimeMixin):
        pass

    policies = []
    manager = SimpleNamespace(
        update_config=lambda config: None,
        configure_browser_sessions=policies.append,
    )
    loop = Harness()
    loop.dispatcher_id = "synthetic-dispatcher"
    loop.bootstrap = SimpleNamespace(hard_cap=4)
    loop.config = None
    loop.container_manager = manager
    for policy in ("task", "legacy"):
        config = SimpleNamespace(
            runtime=runtime_config(playwright_session_policy=policy),
            dispatcher_id="test-dispatcher",
            server="https://server.example.test",
            container=object(),
        )
        loop._apply_config(config, force=False, config_sha="test-hash")
    assert policies == ["task", "legacy"]
    loop.container_manager = SimpleNamespace(update_config=lambda config: None)
    loop._apply_config(config, force=False, config_sha="test-hash")


@pytest.mark.parametrize("status_code", (200, 403, 404, 409, 0, 500))
def test_finish_authorizes_browser_cleanup_only_after_real_ok(status_code):
    manager = browser_manager()
    session = prepare_session(manager)
    manager.browser_sessions.local_finished("project-1", "run-1")
    loop = FinishHarness(FinishClient(status_code), manager)

    loop._finish_task_run("project-1", "run-1", "succeeded")
    loop.complete_finishes()

    assert manager.browser_sessions.claim_ready() == ([session] if status_code == 200 else [])
    assert bool(loop.pending_task_run_finishes) == (status_code in (0, 500))


def test_pending_finish_retry_confirms_only_after_server_accepts():
    manager = browser_manager()
    session = prepare_session(manager)
    manager.browser_sessions.local_finished("project-1", "run-1")
    client = FinishClient(0, 500, 200)
    loop = FinishHarness(client, manager)

    loop._finish_task_run("project-1", "run-1", "succeeded")
    loop.complete_finishes()
    assert manager.browser_sessions.claim_ready() == []
    loop.complete_finishes()
    assert manager.browser_sessions.claim_ready() == []
    loop.complete_finishes()

    assert manager.browser_sessions.claim_ready() == [session]
    assert not loop.pending_task_run_finishes
    assert [update[2]["status"] for update in client.updates] == ["succeeded"] * 3


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    (("success", "succeeded"), ("worker_timeout", "timed_out"), (RuntimeError("worker failed"), "failed")),
)
def test_reap_marks_local_finished_for_whole_future_including_exceptions(outcome, expected_status):
    manager = browser_manager()
    session = prepare_session(manager)
    client = FinishClient()
    loop = LifecycleHarness(client, manager)
    future = Future()
    task = running_task()
    loop.futures[future] = task
    manager.browser_sessions.terminal_confirmed("project-1", "run-1")

    task.cancellation.cancel("stopped")
    loop._reap_futures()
    loop.complete_finishes()
    assert manager.browser_sessions.claim_ready() == []
    assert client.updates == []
    if isinstance(outcome, Exception):
        future.set_exception(outcome)
    else:
        future.set_result(outcome)
    loop._reap_futures()
    loop.complete_finishes()

    assert manager.browser_sessions.claim_ready() == [session]
    assert not loop.futures
    assert [update[2]["status"] for update in client.updates] == [expected_status]


class ManualExecutor:
    def __init__(self):
        self.submitted = []
        self.reject = False

    def submit(self, function, *args):
        if self.reject:
            raise RuntimeError("private submit detail")
        future = Future()
        self.submitted.append((future, function, args))
        return future

    def complete(self, index):
        future, function, args = self.submitted[index]
        try:
            future.set_result(function(*args))
        except Exception as exc:
            future.set_exception(exc)


def cleanup_loop(manager):
    manager.managed_container_names = lambda: []
    manager.cleanup_browser_session = lambda session: "closed"
    loop = LifecycleHarness(FinishClient(), manager)
    loop.cleanup_executor = ManualExecutor()
    return loop


def ready_session(manager, task_run_id="run-1"):
    session = prepare_session(manager, task_run_id)
    manager.browser_sessions.local_finished("project-1", task_run_id)
    manager.browser_sessions.terminal_confirmed("project-1", task_run_id)
    return session


def test_browser_cleanup_uses_existing_cleanup_pool_with_two_item_limit():
    manager = browser_manager()
    sessions = [ready_session(manager, f"run-{index}") for index in range(5)]
    loop = cleanup_loop(manager)
    business_future = Future()
    loop.futures[business_future] = running_task()

    loop._queue_container_cleanups([])
    assert len(loop.cleanup_executor.submitted) == 2
    assert [args[0] for _, _, args in loop.cleanup_executor.submitted] == sessions[:2]
    loop._queue_container_cleanups([])
    assert len(loop.cleanup_executor.submitted) == 2
    assert list(loop.futures) == [business_future]
    assert loop.cleanup_futures == {}

    loop.cleanup_executor.complete(0)
    loop._reap_cleanup_futures()
    loop._queue_container_cleanups([])
    assert len(loop.cleanup_executor.submitted) == 3
    assert loop.cleanup_executor.submitted[2][2] == (sessions[2],)
    assert manager.browser_sessions.prepare("project-1", "run-0", "container-1", env={}, context={}) is None


@pytest.mark.parametrize("failure", ("submit", "exception", "timeout", "retry"))
@pytest.mark.parametrize("outcome", ("success", "worker_timeout"))
def test_browser_cleanup_failure_retries_without_overwriting_business_result(failure, outcome, caplog):
    now = [0]
    manager = browser_manager(clock=lambda: now[0])
    session = prepare_session(manager)
    loop = cleanup_loop(manager)
    business_future = Future()
    loop.futures[business_future] = running_task()
    business_future.set_result(outcome)
    loop._reap_futures()
    loop.complete_finishes()
    expected_status = "succeeded" if outcome == "success" else "timed_out"
    assert [update[2]["status"] for update in loop.client.updates] == [expected_status]

    def cleanup(_session):
        if failure == "exception":
            raise RuntimeError("private cleanup detail")
        if failure == "timeout":
            raise TimeoutError("private timeout detail")
        return "retry"

    manager.cleanup_browser_session = cleanup
    loop.cleanup_executor.reject = failure == "submit"
    loop._queue_container_cleanups([])
    if failure != "submit":
        loop.cleanup_executor.complete(0)
        loop._reap_cleanup_futures()
    assert manager.browser_sessions.claim_ready() == []
    now[0] = 6
    loop.cleanup_executor.reject = False
    loop._queue_container_cleanups([])

    assert loop.cleanup_executor.submitted[-1][2] == (session,)
    assert len(loop.cleanup_executor.submitted) == (1 if failure == "submit" else 2)
    assert [update[2]["status"] for update in loop.client.updates] == [expected_status]
    assert not loop.futures
    assert "private" not in caplog.text
    assert "task crashed" not in caplog.text


def test_legacy_manager_cleanup_paths_need_no_browser_interfaces():
    loop = cleanup_loop(SimpleNamespace())
    del loop.container_manager.cleanup_browser_session
    loop._queue_container_cleanups([])
    loop._reap_cleanup_futures()
    assert loop.cleanup_executor.submitted == []


class ReportClient(FinishClient):
    def __init__(self, manager, status=200):
        super().__init__()
        self.manager = manager
        self.artifacts = []
        self.finish_status = status
        self.finish_requests = []
        self.proof_updates = []
        self.report_prompt = "Report {report_packet_json} {graph_yaml}"
        prompt_digest = sha256(self.report_prompt.encode("utf-8")).hexdigest()
        self.execution_plan = json.dumps(
            {
                "completeness_rule_version": 1,
                "execution_contract_version": 1,
                "execution_engine": "pi",
                "output_contract_version": 2,
                "packet_version": 2,
                "report_policy_version": 1,
                "report_prompt_digest_sha256": prompt_digest,
                "report_prompt_group": "default-report",
                "report_prompt_name": "report.md",
                "version": 2,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.prompt_digest = prompt_digest

    def create_task_run(self, project_id, **kwargs):
        pytest.fail("TaskRun 必须复用报告 claim 的返回值")

    def get_project(self, project_id):
        assert project_id == "project-1"
        return SimpleNamespace(
            project=SimpleNamespace(
                id=project_id,
                status="completed",
                execution_generation=0,
                model_dump=lambda: {"id": project_id, "status": "completed"},
            )
        )

    def claim_assessment_report_finalization(
        self,
        project_id,
        worker,
        *,
        lease_seconds,
    ):
        assert (project_id, worker, lease_seconds) == (
            "project-1",
            "worker-1",
            330,
        )
        return ApiResult(
            status_code=200,
            data={
                "task_run_id": "report-run",
                "status": "running",
                "generation_token": "report-generation",
                "claim_token": "report-claim",
                "report_execution_plan_json": self.execution_plan,
                "report_execution_plan_sha256": sha256(
                    self.execution_plan.encode("utf-8")
                ).hexdigest(),
                "report_prompt_digest_sha256": self.prompt_digest,
                "report_prompt_markdown": self.report_prompt,
            },
        )

    def finish_assessment_report_finalization(
        self,
        project_id,
        worker,
        **kwargs,
    ):
        assert (project_id, worker) == ("project-1", "worker-1")
        assert self.manager.result.cleanup_done
        assert self.manager.browser_sessions.claim_ready() == []
        self.finish_requests.append(kwargs)
        request_sha256 = report._report_finish_request_sha256(
            project_id,
            "synthetic-dispatcher",
            worker,
            claim_token=kwargs["claim_token"],
            generation_token=kwargs["generation_token"],
            task_run_id=kwargs["task_run_id"],
            status=kwargs["status"],
            artifact_id=kwargs["artifact_id"],
            artifact_sha256=kwargs["artifact_sha256"],
            input_snapshot_artifact_id=kwargs[
                "input_snapshot_artifact_id"
            ],
            input_snapshot_sha256=kwargs["input_snapshot_sha256"],
            task_run_result=kwargs["task_run_result"],
        )
        return ApiResult(
            status_code=self.finish_status,
            data=(
                {
                    "protocol": "cairn-assessment-report-finish-v1",
                    "project_id": project_id,
                    "task_run_id": kwargs["task_run_id"],
                    "generation_token": kwargs["generation_token"],
                    "status": kwargs["status"],
                    "artifact_id": kwargs["artifact_id"],
                    "input_snapshot_artifact_id": kwargs[
                        "input_snapshot_artifact_id"
                    ],
                    "input_snapshot_sha256": kwargs[
                        "input_snapshot_sha256"
                    ],
                    "request_sha256": request_sha256,
                    "finish_disposition": "accepted",
                }
                if self.finish_status == 200
                else {}
            ),
        )

    def replay_assessment_report_finalization(
        self,
        project_id,
        worker,
        **kwargs,
    ):
        assert (project_id, worker) == ("project-1", "worker-1")
        assert self.manager.result.cleanup_done
        assert self.manager.browser_sessions.claim_ready() == []
        assert "claim_token" not in kwargs
        self.finish_requests.append(kwargs)
        return ApiResult(
            status_code=self.finish_status,
            data=(
                {
                    "protocol": "cairn-assessment-report-finish-v1",
                    "project_id": project_id,
                    "task_run_id": kwargs["task_run_id"],
                    "generation_token": kwargs["generation_token"],
                    "status": kwargs["status"],
                    "artifact_id": kwargs["artifact_id"],
                    "input_snapshot_artifact_id": kwargs[
                        "input_snapshot_artifact_id"
                    ],
                    "input_snapshot_sha256": kwargs[
                        "input_snapshot_sha256"
                    ],
                    "request_sha256": kwargs["request_sha256"],
                    "finish_disposition": "accepted",
                }
                if self.finish_status == 200
                else {}
            ),
        )

    def update_task_run(self, project_id, task_run_id, **kwargs):
        assert (project_id, task_run_id) == ("project-1", "report-run")
        if kwargs.get("status") == "running":
            return ApiResult(status_code=200, data={"id": task_run_id})
        if "status" not in kwargs:
            assert self.manager.result.cleanup_done
            self.proof_updates.append((project_id, task_run_id, kwargs))
            return ApiResult(status_code=200, data={"id": task_run_id})
        pytest.fail("report TaskRun 终态必须由 finish receipt 持久化")

    def get_assessment_report_packet(self, project_id):
        return ApiResult(status_code=200, data={
            "version": 2, "project": {"id": project_id, "status": "completed"},
            "assessment_summary": {"project_status": "completed", "execution": {},
                                   "coverage": {"tested": [], "blocked": [], "untested": []}},
            "vulnerabilities": [], "observations": [], "observation_statuses": [],
            "coverage_cells": [], "chain_candidates": [], "artifacts": [],
            "evidence_links": [], "verifications": [],
        })

    def bind_assessment_report_input_snapshot(self, project_id, worker, **kwargs):
        assert kwargs["claim_token"] == "report-claim"
        return ApiResult(status_code=200)

    def export_project_result(self, project_id):
        return ApiResult(status_code=200, text="facts: []")

    def list_vulnerabilities(self, project_id):
        return ApiResult(status_code=200, data=[])

    def list_coverage_slots(self, project_id):
        return ApiResult(status_code=200, data=[])

    def create_agent_event(self, project_id, **kwargs):
        assert self.manager.browser_sessions.claim_ready() == []
        return ApiResult(status_code=200)

    def create_artifact(self, project_id, **kwargs):
        assert self.manager.browser_sessions.claim_ready() == []
        self.artifacts.append(kwargs)
        digest = sha256(kwargs["content"].encode("utf-8")).hexdigest()
        return ApiResult(
            status_code=200,
            data={
                "id": f"artifact-{len(self.artifacts)}",
                "sha256": digest,
            },
        )


class ReportResult(ProcessResult):
    def __init__(self, manager, *, timed_out=False):
        super().__init__(
            returncode=124 if timed_out else 0,
            stdout="",
            stderr="", timed_out=timed_out,
        )
        self.manager = manager
        self.cleanup_done = False

    def cleanup(self):
        assert self.manager.browser_sessions.claim_ready() == []
        super().cleanup()
        self.cleanup_done = True


class ReportManager:
    def __init__(self, *, timed_out=False):
        self.browser_sessions = BrowserSessions()
        self.result = ReportResult(self, timed_out=timed_out)
        self.session = None

    def ensure_running(self, *args):
        return "container-1"

    def runtime_metadata(self, container_name):
        assert container_name == "container-1"
        return {"container_id": "container-1", "container_state": "running"}

    def prepare_readonly_input_directory(self, *args, **kwargs):
        pass

    def remove_readonly_input_directory(self, *args, **kwargs):
        pass

    def remove_file(self, *args, **kwargs):
        pass

    def write_text_file(self, container, path, content, **kwargs):
        if "/pi-request-" in path:
            request = json.loads(content)
            self.result.stdout = json.dumps({
                "protocol": "cairn-agent-result-v1", "engine": "pi",
                "project_id": request["project_id"], "task_run_id": request["task_run_id"],
                "phase": request["phase"], "session": request["session"],
                "status": "succeeded", "submission_id": "a" * 64, "usage": None, "error": None,
                "result": {"protocol": "cairn-assessment-v2", "accepted": True,
                           "kind": "report_result", "data": {
                               "executive_summary": "冻结结果", "findings": [],
                               "limitations": "未执行额外测试", "artifacts": [],
                           }},
            })

    def remove_files(self, *args, **kwargs):
        pass

    def build_exec_process(self, *args, **kwargs):
        # Docker fake 在实际执行入口登记真实 registry，不替换任务函数或登记对象。
        self.session = prepare_session(self, "report-run")
        process = SimpleNamespace(
            execution_identity=("container-1", "report-exec"),
            execution_started=False, execution_completed=False,
        )

        def communicate(**kwargs):
            process.execution_completed = True
            return self.result

        process.start = lambda: setattr(process, "execution_started", True)
        process.communicate = communicate
        return process


def run_report(manager, client):
    return report.run_post_completion_report(
        SimpleNamespace(
            dispatcher_id="synthetic-dispatcher",
            tasks=SimpleNamespace(report=SimpleNamespace(timeout=30)),
        ),
        client,
        manager,
        "project-1",
        "facts: []",
        WorkerConfig(
            id="worker-1", engine="pi", task_types=["reason"], concurrency=1, priority=0,
            model_binding=PiModelConfig(
                protocol="openai-responses", model_name="fixture-model",
                inference_endpoint="https://fixture.invalid/v1/responses",
                base_url="https://fixture.invalid/v1", api_key="fixture-key", auth_header="Authorization",
            ),
        ),
        prompt_group="default-report",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )


def assert_report_lifecycle_proof(client):
    assert client.proof_updates == [("project-1", "report-run", {"runtime_context": {
        "execution_container_ids": ["container-1"],
        "execution_lifecycle": {
            "generation": 0, "process_started": True,
            "termination_confirmed": True, "cleanup_confirmed": False,
        },
    }})]


@pytest.mark.parametrize("status_code", (200, 403, 404, 409, 0))
@pytest.mark.parametrize("timed_out", (False, True))
def test_report_internal_task_run_finishes_only_after_result_cleanup(status_code, timed_out):
    manager = ReportManager(timed_out=timed_out)
    client = ReportClient(manager, status_code)

    outcome = run_report(manager, client)

    actual = (
        worker_failure_error_type(outcome)
        if isinstance(outcome, WorkerResultFailure)
        else outcome
    )
    expected = (
        "worker_timeout"
        if timed_out and status_code == 200
        else "success"
        if not timed_out and status_code == 200
        else "policy_denied"
        if status_code == 403
        else "business_acceptance"
        if status_code in {404, 409}
        else "server_write_failed"
    )
    assert actual == expected
    assert manager.result.cleanup_done
    assert client.updates == []
    initial_status = "failed" if timed_out else "ready"
    expected_finish_statuses = (
        [initial_status] * 3 if status_code == 0 else [initial_status]
    )
    assert [item["status"] for item in client.finish_requests] == (
        expected_finish_statuses
    )
    first_result = client.finish_requests[0]["task_run_result"]
    assert first_result["status"] == ("timed_out" if timed_out else "succeeded")
    if timed_out:
        assert isinstance(outcome, WorkerResultFailure) or status_code != 200
        assert first_result["worker_failure"]["exit_code"] == 124
        assert first_result["exit_code"] == 124
    assert manager.session is not None
    assert_report_lifecycle_proof(client)
    assert manager.browser_sessions.claim_ready() == ([manager.session] if status_code == 200 else [])
    if status_code != 200:
        manager.browser_sessions.terminal_confirmed("project-1", "report-run")
        assert manager.browser_sessions.claim_ready() == [manager.session]


@pytest.mark.parametrize("status_code", (200, 403, 404, 409, 0))
def test_report_exception_cleanup_confirms_only_real_terminal_ok(status_code):
    manager = ReportManager()

    class FailingArtifactClient(ReportClient):
        def create_artifact(self, project_id, **kwargs):
            if kwargs.get("filename") == "vulnerability-report.md":
                raise RuntimeError("report write failed")
            return super().create_artifact(project_id, **kwargs)

    client = FailingArtifactClient(manager, status_code)
    outcome = run_report(manager, client)

    assert outcome == "server_write_failed"
    assert manager.result.cleanup_done
    assert client.updates == []
    # validated output 已持久化后只能重放同一 Artifact，不能改写为 failed finish。
    assert client.finish_requests == []
    assert_report_lifecycle_proof(client)
    assert manager.browser_sessions.claim_ready() == []


def test_report_outer_future_with_no_task_run_does_not_repeat_terminal_update():
    manager = ReportManager()
    client = ReportClient(manager)
    loop = LifecycleHarness(client, manager)
    future = Future()
    loop.futures[future] = running_task(task_type="report", task_run_id=None)

    future.set_result(run_report(manager, client))
    loop._reap_futures()
    loop.complete_finishes()

    assert client.updates == []
    assert [item["status"] for item in client.finish_requests] == ["ready"]
    assert_report_lifecycle_proof(client)
    assert manager.browser_sessions.claim_ready() == [manager.session]
    assert not loop.futures


@pytest.mark.parametrize("outcome", ("success", "worker_timeout"))
def test_notification_accessor_failure_cannot_replace_business_outcome(outcome, caplog):
    class UnavailableManager:
        @property
        def browser_sessions(self):
            raise RuntimeError("private registry access detail")

    client = FinishClient()
    loop = LifecycleHarness(client, UnavailableManager())
    future = Future()
    loop.futures[future] = running_task()
    future.set_result(outcome)

    loop._reap_futures()
    loop.complete_finishes()

    assert [update[2]["status"] for update in client.updates] == [
        "succeeded" if outcome == "success" else "timed_out",
    ]
    assert "task crashed" not in caplog.text
    assert "private" not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("task_run_id", (None, "run-1"))
def test_legacy_scheduler_without_manager_keeps_completion_behavior(task_run_id):
    client = FinishClient()
    loop = LifecycleHarness(client, SimpleNamespace())
    del loop.container_manager
    future = Future()
    loop.futures[future] = running_task(task_run_id=task_run_id)
    future.set_result("success")

    loop._reap_futures()
    loop.complete_finishes()

    assert [update[2]["status"] for update in client.updates] == ([] if task_run_id is None else ["succeeded"])
    assert not loop.futures
