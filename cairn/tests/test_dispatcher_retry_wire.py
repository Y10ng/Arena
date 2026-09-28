"""真实 HTTP 失败记录必须阻止重建后的 Dispatcher 再次启动同代 Reason。"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cairn.dispatcher.protocol.client import CairnClient
from test_dispatcher_multi_project_capacity import _loop
from test_pi_project_lifecycle import conn, client, _seed


@pytest.mark.parametrize("phase", ["baseline", "depth_review", "finalizing"])
@pytest.mark.parametrize("error_type", ["contract_error", "request_contract"])
def test_persisted_reason_failure_survives_dispatcher_recreation(conn, client, monkeypatch, phase, error_type):
    _seed(conn)
    conn.execute(
        "UPDATE projects SET assessment_progress_json = ? WHERE id = 'p'",
        (json.dumps({"version": 1, "phase": phase}),),
    )
    conn.commit()
    api = CairnClient("http://testserver", token="synthetic-test-only")
    monkeypatch.setattr(api, "_session", lambda: client)
    assert api.claim_reason("p", "pi-worker", phase).ok
    created = api.create_task_run(
        "p", intent_id=None, task_type="reason", worker_name="pi-worker",
        worker_type="pi", prompt_group="default", expected_execution_generation=0,
        runtime_context={"dispatcher_id": "synthetic-dispatcher"},
    )
    assert created.status_code == 201, created.data
    run_id = created.data["id"]
    assert api.update_task_run("p", run_id, status="failed", error_type=error_type).ok
    assert api.release_reason("p", "pi-worker", task_run_id=run_id).ok
    api.close()

    for _ in range(3):
        # 每轮重建进程内状态；禁止依赖内存冷却或上一次 tick 的失败标记。
        api = CairnClient("http://testserver", token="synthetic-test-only")
        monkeypatch.setattr(api, "_session", lambda: client)
        loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
        loop.client = api
        worker = SimpleNamespace(id="worker", name="pi-worker", type="pi")
        selection = SimpleNamespace(worker=worker)
        loop._scenario_for_project = lambda project: SimpleNamespace(key="default")
        loop._container_runtime_options = lambda *args: {}
        loop._select_worker = lambda *args: selection
        loop._materialize_selected_worker = lambda *args: (worker, {}, None)
        loop._create_task_run = Mock(side_effect=AssertionError("blocked failure created another TaskRun"))
        loop.executor = SimpleNamespace(submit=Mock(side_effect=AssertionError("blocked failure executed work")))
        loop._project_generations = {"p": 0}
        project = SimpleNamespace(project=SimpleNamespace(id="p", tenant_workers=[]))

        assert loop._dispatch_reason(project, "", phase) is False
        assert loop._project_decisions["p"]["code"] == "execution_retry_blocked"
        loop._create_task_run.assert_not_called()
        loop.executor.submit.assert_not_called()
        assert loop.futures == {}
        assert len(client.get("/projects/p/task-runs").json()) == 1
        api.close()


def test_released_reason_cannot_restart_before_previous_finish_is_persisted(conn, client):
    _seed(conn)
    claim = {"worker": "pi-worker", "trigger": "finalizing"}
    assert client.post("/projects/p/reason/claim", json=claim).status_code == 200
    response = client.post("/projects/p/task-runs", json={
        "task_type": "reason", "worker_name": "pi-worker", "worker_type": "pi",
        "expected_execution_generation": 0,
    })
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    assert client.post("/projects/p/reason/release", json={"worker": "pi-worker", "task_run_id": run_id}).status_code == 200

    # 结果写回尚未完成时，释放租约不能让下一轮绕过尚未持久化的失败事实。
    for _ in range(3):
        response = client.post("/projects/p/reason/claim", json=claim)
        assert response.status_code == 409, response.text
    assert len(client.get("/projects/p/task-runs").json()) == 1
