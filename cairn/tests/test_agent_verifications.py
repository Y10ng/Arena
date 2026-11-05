"""T1 直接路径：真实 pi 工具、私有桥、HTTP 和同一复测服务，不调用付费模型或目标。"""

import json
import os
from pathlib import Path
import subprocess
import socket
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import requests
import uvicorn
import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.project_tools import ProjectToolBinding
from cairn.dispatcher.runtime.traffic_reader import TrafficReader
from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import RequestPrincipal, create_machine_principal, create_user_with_personal_tenant
from cairn.server.models import CreateVulnerabilityRequest
from cairn.server.services import agent_conversations as chat, verification_requests as verification
from cairn.server.services.project_agent_configuration import update_configuration
from cairn.server.services.time_utils import utcnow
from cairn.server.services.vulnerabilities import create_vulnerability
from test_external_verification_evidence import execution, private_postgres, verification_database, _upload
from test_external_verification_receipts import ServiceClient, settle_task_run
from test_report_templates import _seed_project
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.process import ManagedProcess
import docker


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated_container():
    image = os.environ.get("CAIRN_T1_TEST_IMAGE")
    if not image:
        pytest.skip("explicit CAIRN_T1_TEST_IMAGE required for network-free Docker execution")
    client = docker.from_env()
    # 只使用已有本地镜像，不拉取依赖，不挂载正式 session 或工作区。
    client.images.get(image)
    container = client.containers.run(image, ["sleep", "infinity"], entrypoint=[],
                                      network_mode="none", detach=True, init=True)
    try:
        yield container
    finally:
        container.stop(timeout=3)
        container.remove()
        client.close()


@pytest.fixture
def http_app(execution, monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    monkeypatch.setenv("CAIRN_TRUSTED_ORIGINS", origin)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started

    class Client(requests.Session):
        def request(self, method, path, **kwargs):
            return super().request(method, origin + path, timeout=20, **kwargs)

    client = Client()
    try:
        yield client, origin
    finally:
        client.close()
        server.should_exit = True
        thread.join(10)
        listener.close()


def test_shared_verification_tools_receipts_actor_sources_and_cancel(execution, http_app, isolated_container, tmp_path):
    admin, execution_machine, model, worker_id, running, running_claim = execution
    actor = RequestPrincipal("user", admin.subject_id, tenant_id=admin.tenant_id, role="user")
    client, origin = http_app
    machine_id, token = create_machine_principal("t1-chat-dispatcher")
    machine_headers = {"Authorization": "Bearer " + token}
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET role = 'user' WHERE id = ?", (actor.subject_id,))
        update_configuration(conn, model.id, actor)
        _seed_project(conn)
        conn.execute("UPDATE projects SET tenant_id = ?, status = 'paused' WHERE id = 'p1'", (actor.tenant_id,))
        conn.execute("INSERT INTO facts(id,project_id,description,creator,created_at) VALUES ('origin','p1','仅本地合成材料，不访问目标','system',?)", (utcnow(),))
        vuln = create_vulnerability(conn, "p1", CreateVulnerabilityRequest(title="T1 来源发现", severity="high", remediation="受控材料"))
        before = dict(conn.execute("SELECT status,execution_generation FROM projects WHERE id = 'p1'").fetchone())
    login = client.post("/auth/login", json={"login": "evidence-admin", "password": "synthetic password"})
    assert login.status_code == 200, login.text
    headers = {"Origin": origin, "X-Cairn-CSRF-Token": login.json()["csrf_token"]}
    cid_response = client.post("/agent-conversations", json={}, headers=headers)
    assert cid_response.status_code == 201, cid_response.text
    cid = cid_response.json()["id"]
    attachment = client.post(f"/agent-conversations/{cid}/attachments", params={"filename": "t1-source.md"}, data=b"# T1 original\r\n", headers=headers)
    assert attachment.status_code == 201, attachment.text
    attachment_id = attachment.json()["id"]
    with db.get_conn() as conn:
        message = chat.submit_message(conn, cid, str(uuid4()), "仅合成材料验证", actor, [attachment_id], project_id=None)
        claim = chat.claim_turn(conn, cid, str(uuid4()), RequestPrincipal("machine", machine_id))
    class HTTPBridge:
        drop_response = False

        def agent_turn_action(self, conversation_id, message_id, action, body):
            response = client.post(f"/agent-conversations/{conversation_id}/turns/{message_id}/{action}", json=body, headers=machine_headers)
            if self.drop_response:
                self.drop_response = False
                assert response.ok
                return ApiResult(0)
            return ApiResult(response.status_code, response.json())

    bridge = HTTPBridge()
    binding = ProjectToolBinding(bridge, None, cid, message["id"], claim["task_run_id"], [])
    reader = TrafficReader(tmp_path, "t1.sock", binding)

    def tool(method, params, call_id=None):
        # 调用实际编译后的工具定义和参数校验，经正式 Unix bridge 送至 HTTP Server。
        program = """
import { createProjectTools } from './container/pi-agent/dist/project-tools.js';
const input = JSON.parse(process.argv[1]);
const tool = createProjectTools(input.binding, () => {}).find(t => t.name === input.method);
try {
  const result = await tool.execute(input.call_id, tool.prepareArguments(input.params));
  console.log(JSON.stringify({data: JSON.parse(result.content[0].text)}));
} catch (error) { console.log(JSON.stringify({error: error.code, message: error.message})); }
"""
        data = {"binding": {"protocol": "cairn-project-tools-v1", "socket_path": str(reader.path),
                            "workspace_path": str(tmp_path), "forbidden_literals": [], "allowed_tools": claim["allowed_tools"]},
                "method": method, "params": params, "call_id": call_id or str(uuid4())}
        completed = subprocess.run(["node", "--input-type=module", "-e", program, json.dumps(data)], cwd=ROOT, text=True, capture_output=True, timeout=30)
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        return result

    def call(method, params, call_id=None):
        response = tool(method, params, call_id)
        assert "data" in response, response
        return response["data"]

    try:
        detail = call("read_vulnerability", {"project_id": "p1", "vulnerability_id": vuln.id})
        params = {"mode": "reproduce", "title": "T1 Agent 创建", "report": {"type": "platform_vulnerability", "project_id": "p1", "vulnerability_id": vuln.id}}
        if detail["observation_id"]:
            params["report"]["observation_id"] = detail["observation_id"]
        created = call("create_verification_request", params, "t1-original-call")
        request_id = created["request_id"]
        page = client.get("/api/v1/verification-requests").json()
        assert request_id in {item["id"] for item in page["items"]}
        page_detail = client.get(f"/api/v1/verification-requests/{request_id}").json()
        assert page_detail["input_source"]["project_id"] == "p1" and page_detail["input_source"]["vulnerability_id"] == vuln.id
        assert call("read_verification_request", {"request_id": request_id})["request"] == page_detail
        assert call("create_verification_request", params, "t1-original-call") == {**created, "replayed": True}
        assert call("read_verification_request", {"creation_tool_call_id": "t1-original-call"})["request"]["id"] == request_id
        assert call("read_verification_request", {"creation_tool_call_id": "not-accepted"})["accepted"] is False
        assert "error" in tool("create_verification_request", {**params, "title": "改写同一动作"}, "t1-original-call")
        assert call("create_verification_request", params, "t1-explicit-new-experiment")["request_id"] != request_id
        bridge.drop_response = True
        unknown = tool("create_verification_request", params, "t1-response-lost")
        assert unknown["error"] == "verification_acceptance_unknown"
        assert json.loads(unknown["message"])["creation_tool_call_id"] == "t1-response-lost"
        recovered = call("read_verification_request", {"creation_tool_call_id": "t1-response-lost"})["request"]
        assert call("create_verification_request", params, "t1-response-lost")["request_id"] == recovered["id"]
        with db.get_conn() as conn:
            row = conn.execute("SELECT caller_type,caller_id FROM verification_requests WHERE id = ?", (request_id,)).fetchone()
            assert (row["caller_type"], row["caller_id"]) == ("user", actor.subject_id)
            assert dict(conn.execute("SELECT status,execution_generation FROM projects WHERE id = 'p1'").fetchone()) == before
            action = conn.execute("SELECT params_json FROM agent_actions WHERE id = ?", (created["action_id"],)).fetchone()
            assert set(json.loads(action["params_json"])) == {"request_sha256"}
        works = client.get(f"/agent-conversations/{cid}/actions").json()
        assert next(work for work in works if work["id"] == created["action_id"])["url"] == f"/verifications/{request_id}"

        page_body = {"schema_version": 1, "request_id": str(uuid4()), "mode": "retest", "title": "T1 页面创建", "report": {"type": "text", "content": "仅合成外部资料"}}
        page_created = client.post("/api/v1/verification-requests", json=page_body, headers=headers)
        assert page_created.status_code == 202, page_created.text
        page_id = page_created.json()["id"]
        assert page_id in {item["id"] for item in call("list_verification_requests", {})["items"]}
        assert call("cancel_verification_request", {"request_id": page_id})["request"]["execution_status"] == "cancelled"
        assert client.get(f"/api/v1/verification-requests/{page_id}").json()["execution_status"] == "cancelled"

        attachment_created = call("create_verification_request", {"mode": "retest", "title": "T1 附件新实例", "target_url": "https://new.example.test/instance", "report": {"type": "conversation_attachment", "attachment_id": attachment_id}}, "t1-attachment")
        attached = call("read_verification_request", {"request_id": attachment_created["request_id"]})["request"]
        assert attached["target_url"] == "https://new.example.test/instance"
        assert verification.verification_material(attached["id"], attached["report"]["resource_id"], actor).content == b"# T1 original\r\n"
        assert attached["report"]["resource_id"] != attachment_id

        other_id, other_tenant = create_user_with_personal_tenant("t1-other", "synthetic password")
        with db.get_conn() as conn:
            # 执行 machine 权限不应替代真实消息操作者的范围。
            conn.execute("UPDATE agent_messages SET submitted_by = ?, submitted_tenant_id = ? WHERE id = ?", (other_id, other_tenant, message["id"]))
        assert "error" in tool("cancel_verification_request", {"request_id": request_id})
        with db.get_conn() as conn:
            assert conn.execute("SELECT cancel_requested_at FROM verification_requests WHERE id = ?", (request_id,)).fetchone()["cancel_requested_at"] is None
            conn.execute("UPDATE agent_messages SET submitted_by = ?, submitted_tenant_id = ? WHERE id = ?", (actor.subject_id, actor.tenant_id, message["id"]))

        artifact = _upload(running, running_claim, "file", "t1-current.txt", b"T1 current material\r\n")
        service_client = ServiceClient(running, running_claim, execution_machine)
        cancellation = TaskCancellation(generation=running_claim["execution_generation"])

        program = """
import json, os, pathlib, subprocess, time
child = subprocess.Popen(['sleep', '3600'])
pathlib.Path('/tmp/t1-material').write_bytes(b'T1 retained workspace\\r\\n')
pathlib.Path('/tmp/t1-pids').write_text(json.dumps([os.getpid(), child.pid]))
time.sleep(3600)
"""
        process = ManagedProcess(isolated_container, ["python3", "-c", program], {})
        cancellation.register_execution(process)
        process.on_execution_started = lambda: cancellation.observe_execution_start(process)
        cancellation.attach_process(process)
        process.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready = isolated_container.exec_run(["python3", "-c", "from pathlib import Path; print(Path('/tmp/t1-pids').read_text())"])
            if ready.exit_code == 0:
                break
            time.sleep(0.05)
        assert ready.exit_code == 0
        pids = json.loads(ready.output)
        assert call("cancel_verification_request", {"request_id": running["id"]})["request"]["execution_status"] == "cancelling"
        assert cancellation.execution_lifecycle(task_finished=True)["termination_confirmed"] is False
        binding_body = {key: running_claim[key] for key in ("project_id", "execution_generation", "claim_token")}
        binding_body["task_run_id"] = running_claim["task_run"]["id"]
        lease = HeartbeatLease(lambda: service_client.verification_heartbeat(running["id"], binding_body),
                               "T1 isolated verification", "t1", interval=1)
        lease.attach_process(process)
        lease.start()
        result = None
        try:
            result = process.communicate(timeout=15)
            assert result.cancelled and not result.timed_out and process.execution_completed
            assert lease.failure.status_code == 409
            cancellation.finish_execution(process)
            probe = isolated_container.exec_run(["python3", "-c",
                "import pathlib,sys; assert all(not pathlib.Path('/proc/'+p).exists() for p in sys.argv[1:]); "
                "assert pathlib.Path('/tmp/t1-material').read_bytes() == b'T1 retained workspace\\r\\n'", *map(str, pids)])
            assert probe.exit_code == 0, probe.output
            assert call("read_verification_request", {"request_id": running["id"]})["request"]["execution_status"] == "cancelling"
            settle_task_run(service_client, running_claim, "cancelled", cancellation)
        finally:
            lease.stop()
            if result is not None:
                result.cleanup()
        assert call("read_verification_request", {"request_id": running["id"]})["request"]["execution_status"] == "cancelled"
        assert verification.verification_artifact(running["id"], artifact.id, actor).disk_path.read_bytes() == b"T1 current material\r\n"
        with db.get_conn() as conn:
            chat.finish_turn(conn, cid, message["id"], SimpleNamespace(task_run_id=claim["task_run_id"], status="cancelled", text="", error_code="chat_cancelled", usage=None, usage_missing_responses=None, session_initialized=False), RequestPrincipal("machine", machine_id))
        # 聊天取消不级联取消已受理的独立请求；来源探索保持原状。
        assert client.get(f"/api/v1/verification-requests/{request_id}").json()["execution_status"] == "queued"
        with db.get_conn() as conn:
            assert dict(conn.execute("SELECT status,execution_generation FROM projects WHERE id = 'p1'").fetchone()) == before
        print(f"T1 shared request={request_id}; page request={page_id}; replay/actor/source/attachment/cancel/materials verified")
    finally:
        reader.close()
        client.close()
