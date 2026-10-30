"""无付费的顶层报告样本：真实 Pi/ProcessRunner、候选 HTTP/PG 与冻结成品接受。"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairn.dispatcher.config import PiModelConfig, WorkerConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import report
from cairn.dispatcher.tasks.candidate_capture_outbox import CandidateCaptureOutbox
from cairn.dispatcher.tasks.report_finish_outbox import ReportFinishOutbox
from cairn.server import db
from cairn.server.auth import RequestPrincipal, require_dispatcher, require_tenant_project
from cairn.server.routers import audit, candidates, projects
from cairn.server.schemas.candidates import canonical_json_bytes
from cairn.server.services import assessment_reports, audit_artifacts, report_templates
from cairn.server.services.report_inventory import list_report_inventory
from test_report_templates import defaults_v60_conn, private_postgres, _seed_project


class LocalPiProcess:
    """只替换 Docker 运输；执行真实 CLI，终态仅在本地子进程退出后确认。"""

    def __init__(self, request_path, repository):
        self.request_path = request_path
        self.repository = repository
        self.execution_identity = ("isolated-container", uuid4().hex)
        self.execution_control_path = f"/tmp/cairn-exec-{uuid4().hex}.json"
        self.execution_start_attempted = False
        self.execution_started = False
        self.execution_completed = False
        self.on_execution_prepared = None
        self.on_execution_starting = None
        self.process = None

    def retain_execution_control(self):
        pass

    def start(self):
        self.on_execution_prepared(*self.execution_identity, self.execution_control_path)
        self.on_execution_starting()
        self.execution_start_attempted = True
        self.process = subprocess.Popen(
            ["node", str(self.repository / "container/pi-agent/dist/cli.js"), "--request", self.request_path],
            cwd=self.repository, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.execution_started = True

    def communicate(self, timeout):
        stdout, stderr = self.process.communicate(timeout=timeout)
        self.execution_completed = True
        return ProcessResult(self.process.returncode, stdout, stderr)

    def cancel(self, reason):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()


class LocalPiContainer:
    """独占样本工作区；请求和候选保持生产路径及原始字节，不重写身份。"""

    def __init__(self, repository):
        self.repository = repository
        self.files = set()
        self.directories = set()
        self.request = None
        self.result = None
        self.process = None

    def container_name(self, project_id):
        assert project_id == "p1"
        return "isolated-container"

    def ensure_running(self, project_id, options):
        return self.container_name(project_id)

    def runtime_metadata(self, name):
        return {"container_id": "isolated-container", "container_state": "running"}

    def docker_daemon_id(self):
        return "isolated-local-process"

    def write_text_file(self, name, path, content, **kwargs):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        assert not target.exists() and not target.is_symlink(), path
        target.write_text(content, encoding="utf-8")
        target.chmod(kwargs.get("mode", 0o600))
        self.files.add(target)

    def prepare_readonly_input_directory(self, name, directory):
        target = Path(directory)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(mode=0o755)
        self.directories.add(target)

    def remove_file(self, name, path, **kwargs):
        target = Path(path)
        assert target in self.files
        target.unlink(missing_ok=True)

    def remove_readonly_input_directory(self, name, directory, **kwargs):
        target = Path(directory)
        assert target in self.directories
        shutil.rmtree(target)

    @contextmanager
    def materialize_file(self, name, path):
        target = Path(path)
        assert target.is_file() and not target.is_symlink()
        yield SimpleNamespace(path=target, size=target.stat().st_size)

    def build_exec_process(self, name, env, argv, **kwargs):
        assert argv[:2] == ["cairn-agent", "--request"]
        self.request = json.loads(Path(argv[2]).read_text())
        process = LocalPiProcess(argv[2], self.repository)
        self.process = process
        original = process.communicate

        def communicate(timeout):
            self.result = original(timeout)
            return self.result

        process.communicate = communicate
        return process

    def cleanup(self):
        if self.process is not None and self.process.process is not None:
            child = self.process.process
            if child.poll() is None:
                child.terminate()
                try:
                    child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()
        # 只删除本样本创建的随机路径；报告分段位于独占 workspace，由 fixture 释放。
        for path in self.files:
            path.unlink(missing_ok=True)
        if self.request is not None:
            Path(self.request["capabilities"]["report_segments"]["manifest_path"]).unlink(missing_ok=True)
        for directory in self.directories:
            if directory.exists():
                shutil.rmtree(directory)


@contextmanager
def synthetic_provider(artifact_id, original):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/chat/completions"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            if len(calls) == 1:
                identity = {"kind": "finding", "vulnerability_id": "v1", "observation_id": "o1"}
                tools = [{"name": "save_report_segment", "args": {"key": "finding", "content": {
                    **identity, "description": "冻结发现：合成用户甲能够读取用户乙的对象；没有验证写入。",
                    "steps": [{"operation": "用合成用户甲身份读取对象 id=sample-7。", "observed": "返回用户乙拥有的 sample-7。", "evidence": [{
                        "evidence_item_id": "e1", "selections": [{"side": "response", "artifact_id": artifact_id, "byte_start": 0, "byte_end": len(original)}],
                    }]}], "remediation": "在服务端读取前检查对象归属。",
                }}}, {"name": "save_report_segment", "args": {"key": "summary", "content": {
                    "kind": "executive_summary", "text": "项目综合：仅一条冻结的合成读取越权发现，未访问真实目标。",
                }}}, {"name": "save_report_segment", "args": {"key": "remediation", "content": {
                    "kind": "overall_remediation", "text": "优先修复对象归属检查；修复效果尚未验证。",
                }}}]
            else:
                # 真实 runner 会从工作上下文移除已保存正文，仅向 provider 投影持久引用。
                prefix = "Durably saved report segments (local material, not Server acceptance): "
                references = []
                for message in body["messages"]:
                    content = message.get("content")
                    if message.get("role") == "user" and isinstance(content, str) and content.startswith(prefix):
                        saved, _ = json.JSONDecoder().raw_decode(content[len(prefix):])
                        references = [{"artifact_handle": item["artifact_handle"]} for item in saved]
                assert len(references) == 3, len(references)
                tools = [{"name": "submit_result", "args": {"accepted": True, "data": {"schema_version": 5, "segment_refs": references}}}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def chunk(delta, finish=None):
                data = {"id": f"chat_{len(calls)}", "object": "chat.completion.chunk", "created": 1, "model": "synthetic-report", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode())

            chunk({"role": "assistant"})
            for index, tool in enumerate(tools):
                chunk({"tool_calls": [{"index": index, "id": f"call_{len(calls)}_{index}", "type": "function", "function": {"name": tool["name"], "arguments": json.dumps(tool["args"], ensure_ascii=False)}}]})
            chunk({}, "tool_calls")
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def isolated_worker_workspace():
    workspace = Path("/home/kali/workspace")
    if not workspace.parent.is_dir():
        pytest.skip("需要既有 /home/kali 以创建独占测试工作区")
    try:
        workspace.mkdir(mode=0o700)
    except FileExistsError:
        pytest.skip("不得接触既有 Worker 工作区")
    try:
        yield workspace
    finally:
        shutil.rmtree(workspace)


def test_top_level_report_registers_worker_binding_and_accepts_frozen_report(defaults_v60_conn, isolated_worker_workspace, monkeypatch, tmp_path):
    repository = Path(__file__).resolve().parents[2]
    assert (repository / "container/pi-agent/dist/cli.js").is_file(), "先构建现有 Pi 源码"
    manager = LocalPiContainer(repository)
    conn = defaults_v60_conn
    db.run_migrations(conn, "postgresql")
    _seed_project(conn)
    conn.execute("UPDATE projects SET status = 'stopped' WHERE id = 'p1'")
    conn.commit()

    @contextmanager
    def connection():
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    # 所有业务读写均指向一次性 Unix socket PG；不读取运行实例的配置。
    for module in tuple(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("cairn.server."):
            if hasattr(module, "get_conn"):
                monkeypatch.setattr(module, "get_conn", connection)
            if hasattr(module, "artifact_root"):
                monkeypatch.setattr(module, "artifact_root", lambda: tmp_path / "artifacts")
    app = FastAPI()
    for router in (audit.router, projects.router, candidates.router):
        app.include_router(router)
    principal = RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")
    app.dependency_overrides[require_tenant_project] = lambda: principal
    app.dependency_overrides[require_dispatcher] = lambda: principal
    client = CairnClient("http://testserver", token="synthetic-only")
    try:
        with TestClient(app) as transport:
            failures = []
            transport.event_hooks["response"].append(lambda response: failures.append((str(response.request.url), response.status_code, response.read().decode())) if response.status_code >= 400 else None)
            monkeypatch.setattr(client, "_session", lambda: transport)
            original = "HTTP/1.1 200 OK\r\n\r\n合成 sample-7 属于用户乙；没有验证写入。\r\n".encode()
            uploaded = client.create_artifact("p1", task_run_id=None, kind="raw_output", content=original, filename="synthetic-response.http", metadata={"source": "synthetic_capture"})
            assert uploaded.status_code == 201, uploaded.text
            artifact_id = uploaded.data["id"]
            conn.execute("INSERT INTO vulnerabilities (id, project_id, title, severity, created_at, updated_at) VALUES ('v1', 'p1', '合成读取越权', 'high', 'before', 'before')")
            conn.execute("""INSERT INTO vulnerability_observations
                (id, project_id, vulnerability_id, severity, confidence, observed_at, evidence_items_json)
                VALUES ('o1', 'p1', 'v1', 'high', 0.8, 'before', ?)""", (json.dumps([{"id": "e1", "kind": "http", "source": "capture", "title": "合成响应", "response": {"artifact_id": artifact_id}}]),))
            current = report_templates.get_report_template(conn, "vulnerability-report")
            frozen_template = current.markdown_content.replace("### 漏洞说明", "### 顶层冻结漏洞说明") + "\n## 项目综合\n\n{{ executive_summary }}\n"
            report_templates.update_report_template(conn, current.key, current.name, "", frozen_template)
            requested = assessment_reports.request_assessment_report_generation(conn, "p1", template_key=current.key, requested_by="synthetic-user")
            conn.commit()
            # 请求受理后修改 live 模板，执行仍须使用原来选定且冻结的版本。
            report_templates.update_report_template(conn, current.key, current.name, "", current.markdown_content.replace("### 漏洞说明", "### 后来修改的模板"))
            conn.commit()
            with synthetic_provider(artifact_id, original) as (base, provider_calls):
                worker = WorkerConfig(name="worker", type="pi", task_types=["reason"], max_running=1, priority=1, model_binding=PiModelConfig(protocol="openai-chat-completions", model_name="synthetic-report", inference_endpoint=base + "/chat/completions", base_url=base, auth_header="authorization-bearer", api_key="synthetic-only"))
                config = SimpleNamespace(dispatcher_id="synthetic-dispatcher", tasks=SimpleNamespace(report=SimpleNamespace(timeout=60, max_output_tokens=4096)))
                candidate_outbox = CandidateCaptureOutbox(tmp_path / "candidates", owner_id="synthetic-dispatcher")
                finish_outbox = ReportFinishOutbox(tmp_path / "finishes")
                outcome = report.run_post_completion_report(config, client, manager, "p1", "project: p1\nfacts: []\n", worker, prompt_group="goal-driven-v1", durable_request=True, generation_token=requested.generation_token, finish_outbox=finish_outbox, candidate_outbox=candidate_outbox)
            assert outcome == "success", (outcome, failures, manager.result.read_stdout() if manager.result else None)
            assert len(provider_calls) == 2
            run = conn.execute("SELECT * FROM task_runs WHERE task_type = 'report'").fetchone()
            assert run["status"] == "succeeded"
            delivery = json.loads(run["runtime_context_json"])["execution_delivery"]
            assert delivery["binding"] == manager.request["binding"]
            assert delivery["binding"]["report_segments"]["task_run_id"] == run["id"]
            captured = conn.execute("SELECT * FROM task_run_candidates WHERE task_run_id = ?", (run["id"],)).fetchall()
            assert len(captured) == 1
            assert captured[0]["binding_sha256"] == sha256(json.dumps(delivery["binding"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            assert captured[0]["result_schema_sha256"] == delivery["result_schema_sha256"]
            assert conn.execute("SELECT 1 FROM task_run_candidate_outcomes WHERE task_run_id = ? AND code = 'locally_validated'", (run["id"],)).fetchone() is not None
            assert manager.result.candidate_host_durable is True
            assert manager.result.candidate_capture_integrity_error is None
            assert candidate_outbox.entries() == []
            inventory = list_report_inventory(conn, tenant_id=None, project_id="p1")
            assert inventory.total == 1
            item = inventory.items[0]
            receipt = conn.execute("SELECT * FROM assessment_report_finish_receipts WHERE task_run_id = ?", (run["id"],)).fetchone()
            assert receipt["generation_token"] == requested.generation_token
            assert receipt["artifact_id"] == item.artifact_id
            assert json.loads(receipt["response_json"])["status"] == "ready"
            content = audit_artifacts.artifact_content_file("p1", item.artifact_id).disk_path.read_bytes().decode()
            assert "顶层冻结漏洞说明" in content and "后来修改的模板" not in content
            assert "合成读取越权" in content and "sample-7" in content
            assert "项目综合：仅一条冻结的合成读取越权发现，未访问真实目标。" in content
            assert original.decode() in content
            assert audit_artifacts.artifact_content_file("p1", artifact_id).disk_path.read_bytes() == original
            # 顶层函数保留 ACK journal，等待既有调度器释放 terminal fence；不伪造物理清理。
            pending = finish_outbox.entries()
            assert len(pending) == 1 and pending[0].status == "ready"
            assert pending[0].request_sha256 == receipt["request_sha256"]

            # 使用同一真实 HTTP/PG 接受事实验证恢复读取权限，不重新调用模型或伪造 receipt。
            readback = client.get_assessment_report_finish_receipt("p1", run["id"])
            assert readback.ok and readback.data == json.loads(receipt["response_json"])
            app.dependency_overrides[require_tenant_project] = lambda: RequestPrincipal(kind="machine", subject_id="other-dispatcher")
            try:
                forbidden = client.get_assessment_report_finish_receipt("p1", run["id"])
                assert forbidden.status_code == 403
                assert forbidden.data == {"detail": "execution_owner_mismatch"}
            finally:
                app.dependency_overrides[require_tenant_project] = lambda: principal

            # 保留登记与原候选不变；篡改重投的绑定仍须在真实候选入口被明确拒绝。
            mismatched = json.loads(captured[0]["manifest_json"])
            mismatched["binding_sha256"] = "0" * 64
            rejected = client.capture_candidate("p1", run["id"], canonical_json_bytes(mismatched), bytes(captured[0]["raw_bytes"]))
            assert rejected.status_code == 409
            assert rejected.data == {"detail": "candidate_binding_digest_mismatch"}
            assert conn.execute("SELECT COUNT(*) AS n FROM task_run_candidates").fetchone()["n"] == 1
            assert list_report_inventory(conn, tenant_id=None, project_id="p1").total == 1
    finally:
        manager.cleanup()
