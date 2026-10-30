"""显式 opt-in 的 S1-A 实验：真实采集持续上传至私有 PostgreSQL，并经项目 API 读取。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import base64
import ipaddress
from io import BytesIO
import json
import os
from pathlib import Path
import shlex
import socket
import tempfile
import threading
import time
from hashlib import sha256

from traffic_api_fixture import traffic_api, postgres_instance, empty_postgres_url

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import docker
import pytest

from cairn.dispatcher.runtime.traffic import ProjectTrafficRuntime, WORKSPACE
from cairn.dispatcher.runtime.traffic_sync import TrafficSync
from cairn.dispatcher.protocol.client import ApiResult
from cairn.server import db


SITE = r'''
import http.server, ssl, threading, sys, time, socket
from pathlib import Path
site = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('/site')
instance = sys.argv[2] if len(sys.argv) > 2 else 'external'
ports = (8990, 9444) if instance != 'external' else (8989, 9443)
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body[::-1])
    def do_GET(self):
        if self.path == '/no-response':
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if self.path == '/interrupted':
            self.send_response(200)
            self.send_header('Content-Length', '100000')
            self.end_headers()
            self.wfile.write(b'actual-prefix\x00\xff')
            self.wfile.flush()
            deadline = time.monotonic() + 20
            while not (site / 'release-interrupted').exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        body = b'<html><title>traffic-synthetic</title><body>synthetic</body></html>'
        if self.path.startswith('/control-file'):
            body = Path('control-file').read_bytes() + b':' + instance.encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Duplicate', 'one')
        self.send_header('X-Duplicate', 'two')
        self.end_headers()
        self.wfile.write(body)
a = http.server.ThreadingHTTPServer(('0.0.0.0', ports[0]), Handler)
b = http.server.ThreadingHTTPServer(('0.0.0.0', ports[1]), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(site / 'server.pem', site / 'server.key')
b.socket = context.wrap_socket(b.socket, server_side=True)
threading.Thread(target=a.serve_forever, daemon=True).start()
if instance != 'external': (site / 'ready').touch()
b.serve_forever()
'''

NODE = r'''
import { createRequestBashToolDefinition } from '/opt/s0-pi/dist/runner.js';
import { createTaskBashOperations } from '/opt/s0-pi/dist/task-execution.js';
const input = JSON.parse(process.argv[1]);
const tool = createRequestBashToolDefinition('/home/kali/workspace', 30, () => 30,
    createTaskBashOperations(input.binding));
const result = await tool.execute('synthetic-tool', {command: input.command});
console.log(JSON.stringify(result));
'''


def certificate(directory, *, issuer=None, address=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "traffic-synthetic-server" if issuer else "traffic-synthetic-ca")])
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(subject)
               .issuer_name(issuer[0].subject if issuer else subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
               .add_extension(x509.BasicConstraints(ca=issuer is None, path_length=None), critical=True))
    if address:
        addresses = address if isinstance(address, list) else [address]
        builder = builder.add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address(value)) for value in addresses
        ]), critical=False)
    cert = builder.sign(issuer[1] if issuer else key, hashes.SHA256())
    prefix = "server" if issuer else "ca"
    (directory / f"{prefix}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / f"{prefix}.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert, key


@pytest.mark.skipif(
    os.environ.get("CAIRN_TRAFFIC_INTEGRATION") != "1" or os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1",
    reason="需明确启用隔离 Docker 与私有 PostgreSQL 实验",
)
def test_real_tool_traffic_scopes_and_coverage(monkeypatch, traffic_api):
    client = docker.from_env()
    repository = Path(__file__).resolve().parents[2]
    # 使用短私有路径，避免 Unix socket 的 108 字节地址上限。
    with tempfile.TemporaryDirectory(prefix="cairn-traffic-") as temporary:
        root = Path(temporary)
        site = root / "site"
        site.mkdir()
        ca = certificate(site)
        (site / "server.py").write_text(SITE)
        runtime = ProjectTrafficRuntime(
            client, project_id=traffic_api.project_id, root=root / "project",
            gateway_image="arena-traffic-gateway:s1-a", executor_image="arena-task-executor:s0-b", isolated=True,
        )
        sync = None
        api = traffic_api.user
        prefix = f"{traffic_api.base_url}/projects/{traffic_api.project_id}/traffic"

        def get(path="", **params):
            response = api.get(prefix + path, params=params, timeout=10)
            assert response.status_code == 200, response.text
            return response.json()

        def wait_api(predicate):
            deadline = time.monotonic() + 20
            while True:
                result = get(limit=200)
                if predicate(result):
                    return result
                assert time.monotonic() < deadline, sync.status()
                time.sleep(0.05)

        try:
            runtime.prepare(upstream_ca=site / "ca.pem")
            target = client.containers.create(
                "arena-traffic-gateway-dependencies:s0", ["sh", "-c", "while ! test -f /site/server.key; do sleep 0.1; done; exec python /site/server.py"],
                network=runtime.egress.name, labels=runtime.labels,
                volumes={str(site): {"bind": "/site", "mode": "ro"}},
            )
            runtime.containers.append(target)
            # Docker 分配地址后才签发目标证书；合成 CA 私钥不挂给测试执行容器。
            target.start()
            target.reload()
            address = target.attrs["NetworkSettings"]["Networks"][runtime.egress.name]["IPAddress"]
            certificate(site, issuer=ca, address=address)
            initialize_network = runtime._initialize_network
            gated_scopes = []

            def initialize_with_gate_check(container, command):
                if container.id != runtime.gateway.id:
                    scope = runtime.scopes[-1]
                    deadline = time.monotonic() + 10
                    while not (scope.socket_directory / "execute.sock").exists():
                        assert time.monotonic() < deadline, "execution socket did not start"
                        time.sleep(0.05)
                    # 在实际规则安装前调用真实执行服务，不能运行这条写文件命令。
                    payload = json.dumps({"command": "touch gate-bypassed", "cwd": WORKSPACE, "timeout_seconds": 1}).encode()
                    with socket.socket(socket.AF_UNIX) as connection:
                        connection.settimeout(2)
                        connection.connect(str(scope.socket_directory / "execute.sock"))
                        connection.sendall(b"POST /execute HTTP/1.0\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
                        assert b" 503 " in connection.recv(1024)
                    assert not (runtime.workspace / "gate-bypassed").exists()
                    gated_scopes.append(scope.id)
                initialize_network(container, command)

            monkeypatch.setattr(runtime, "_initialize_network", initialize_with_gate_check)
            scopes = [runtime.prepare_scope(run["id"], run["execution_generation"]) for run in traffic_api.task_runs]
            assert gated_scopes == [scope.id for scope in scopes]
            receipts = root / "receipts"
            accepted_without_receipt = {}
            lost_receipt = threading.Event()
            seal_body = traffic_api.machine.seal_traffic_body

            def lose_one_receipt(*args, **kwargs):
                result = seal_body(*args, **kwargs)
                if result.ok and not lost_receipt.is_set():
                    accepted_without_receipt.update(result.data)
                    lost_receipt.set()
                    return ApiResult(status_code=0, error_code="injected_receipt_loss")
                return result

            # 只丢独立封存回执；范围接收、Artifact 落盘及数据库事务都真实执行。
            monkeypatch.setattr(traffic_api.machine, "seal_traffic_body", lose_one_receipt)
            sync = TrafficSync(traffic_api.machine, capture=runtime.capture, receipts=receipts, interval=0.1)
            sync.start()
            collectors = [container for container in runtime.containers if container.labels.get("io.cairn.traffic.role") == "collector"]
            assert len(collectors) == 2
            for scope in scopes:
                executor = client.containers.get(scope.container_id)
                assert {mount["Destination"] for mount in executor.attrs["Mounts"]} == {
                    WORKSPACE, "/run/cairn-exec", "/run/cairn-ca/project.pem",
                }
                assert not executor.attrs["HostConfig"]["CapAdd"]
                collector = next(item for item in collectors if item.labels["io.cairn.traffic.scope"] == scope.id)
                collector.reload()
                host = collector.attrs["HostConfig"]
                assert host["NetworkMode"] == f"container:{scope.container_id}"
                assert not host["CapAdd"] and host["CapDrop"] == ["ALL"]
                assert not host["Privileged"] and not host["PidMode"] and host["IpcMode"] != f"container:{scope.container_id}"
                mounts = {item["Destination"]: item for item in collector.attrs["Mounts"]}
                assert mounts["/capture"]["Source"] == str(runtime.capture / scope.id)
                assert not mounts["/run/cairn-traffic/scope.json"]["RW"]
                assert not mounts["/run/cairn-traffic/ca"]["RW"]
            assert not runtime.gateway.attrs["Mounts"]
            volumes = {
                str(runtime.workspace): {"bind": WORKSPACE, "mode": "rw"},
                str(repository / "container/pi-agent"): {"bind": "/opt/s0-pi", "mode": "ro"},
            }
            for scope in scopes:
                volumes[str(scope.socket_directory)] = {"bind": f"/run/cairn-exec/{scope.id}", "mode": "rw"}
            control = client.containers.create(
                "arena-worker:local", ["sleep", "infinity"], network_mode="none",
                labels=runtime.labels, volumes=volumes,
                environment={"S0_CONTROL_SECRET": "synthetic-control-only"},
            )
            runtime.containers.append(control)
            control.start()

            def execute(scope, command):
                result = control.exec_run([
                    "node", "--input-type=module", "-e", NODE,
                    json.dumps({"binding": scope.capability(), "command": command}),
                ])
                assert result.exit_code == 0, result.output.decode()
                parsed = json.loads(result.output)
                return "\n".join(item.get("text", "") for item in parsed["content"])

            def records():
                return [json.loads(path.read_text()) for path in runtime.capture.glob("*/flows/*/flow.json")]

            expected = {}
            html = b'<html><title>traffic-synthetic</title><body>synthetic</body></html>'

            def expect(scope, target, destination, scheme, body=html, request_id=None):
                expected[scope.id, target] = (scope, destination, scheme, body, request_id)

            def wait_for_flows():
                deadline = time.monotonic() + 10
                while True:
                    observed = records()
                    grouped = {}
                    for flow in observed:
                        target = base64.b64decode(flow["request"]["target_base64"]).decode()
                        grouped.setdefault((flow["scope"]["id"], target), []).append(flow)
                    missing = [key for key in expected if key not in grouped or grouped[key][0]["state"] == "receiving"]
                    if not missing:
                        break
                    assert time.monotonic() < deadline, f"missing/unfinished flows: {missing}; observed: {grouped}"
                    time.sleep(0.05)
                assert set(grouped) == set(expected), f"unexpected flows: {set(grouped) - set(expected)}"
                for key, (scope, destination, scheme, body, request_id) in expected.items():
                    assert len(grouped[key]) == 1, f"duplicate capture: {key}"
                    flow = grouped[key][0]
                    assert flow["scope"] == {
                        "id": scope.id, "project_id": scope.project_id, "task_run_id": scope.task_run_id,
                        "generation": scope.generation, "container_id": scope.container_id,
                    }
                    assert flow["state"] == "complete", flow
                    assert flow["observation_point"] == "task-network-output"
                    assert flow["original_destination"] == list(destination), flow
                    assert (flow["request"]["host"], flow["request"]["port"], flow["request"]["scheme"]) == (*destination, scheme)
                    assert flow["request"]["method"] == "GET"
                    assert flow["response"]["status_code"] == 200
                    headers = [(base64.b64decode(name), base64.b64decode(value)) for name, value in flow["response"]["headers_base64"]]
                    assert [value for name, value in headers if name.lower() == b"x-duplicate"] == [b"one", b"two"]
                    directory = runtime.capture / scope.id / "flows" / flow["id"]
                    assert (directory / "response.body").read_bytes() == body
                    assert flow["response"]["body_bytes"] == len(body)
                    assert flow["request"]["body_bytes"] == 0
                    assert (directory / "request.body").read_bytes() == b""
                    assert flow["request"]["body_complete"] and flow["response"]["body_complete"]
                    if request_id is not None:
                        headers = [(base64.b64decode(name).lower(), base64.b64decode(value)) for name, value in flow["request"]["headers_base64"]]
                        assert (b"x-s0-request", request_id.encode()) in headers
                return observed

            (runtime.workspace / "control-file").write_text("shared-input")
            os.chmod(runtime.workspace / "control-file", 0o644)
            commands = []
            for scope in scopes:
                parts = ["test -z \"$S0_CONTROL_SECRET\" && test -z \"$HTTP_PROXY\" && test -z \"$HTTPS_PROXY\" && test -z \"$ALL_PROXY\"", "test \"$(<control-file)\" = shared-input"]
                for scheme, port in (("https", 9443), ("http", 8989)):
                    request_id = f"{scope.task_run_id}-external-{scheme}"
                    target_path = f"/same?case=external-{scheme}"
                    expect(scope, target_path, (address, port), scheme, request_id=request_id)
                    parts.append(f"curl --proxy '' --fail --silent --show-error --max-time 10 -H 'X-S0-Request: {request_id}' '{scheme}://{address}:{port}{target_path}'")
                commands.append(" && ".join(parts))
            with ThreadPoolExecutor(max_workers=2) as pool:
                outputs = list(pool.map(execute, scopes, commands))
            assert all(output.count("traffic-synthetic") == 2 for output in outputs), outputs
            assert len(wait_for_flows()) == 4
            assert lost_receipt.wait(10), sync.status()
            sync.stop()
            monkeypatch.setattr(traffic_api.machine, "seal_traffic_body", seal_body)
            sync = TrafficSync(traffic_api.machine, capture=runtime.capture, receipts=receipts, interval=0.1)
            sync.start()
            initial = wait_api(lambda result: len(result["items"]) == 4 and all(item["state"] == "complete" for item in result["items"]))
            lost = get("/" + accepted_without_receipt["flow_id"])
            assert lost["sync"]["bodies"][accepted_without_receipt["side"]]["artifact_id"] == accepted_without_receipt["artifact_id"]
            for item in initial["items"]:
                for side, expected_body in (("request", b""), ("response", html)):
                    download = api.get(prefix + f"/{item['id']}/bodies/{side}", timeout=10)
                    assert download.status_code == 200 and download.content == expected_body
                    assert download.headers["content-disposition"].startswith("attachment")
                run = next(run for run in traffic_api.task_runs if run["id"] == item["scope"]["task_run_id"])
                assert run["status"] == "running"
                executor = client.containers.get(item["scope"]["container_id"])
                assert executor.status == "running"
            execute(scopes[0], "printf shared-output > test-file")
            assert (runtime.workspace / "test-file").read_text() == "shared-output"
            expect(scopes[0], "/python?case=python", (address, 9443), "https", request_id="python")
            python = f"import requests; print(requests.get('https://{address}:9443/python?case=python', headers={{'X-S0-Request': 'python'}}, timeout=10).status_code)"
            assert "200" in execute(scopes[0], "python3 -c " + shlex.quote(python))
            expect(scopes[0], "/scanner?case=scanner", (address, 8989), "http", request_id="scanner")
            scanner = execute(scopes[0], f"printf 'scanner\\n' > words; ffuf -u 'http://{address}:8989/FUZZ?case=scanner' -H 'X-S0-Request: scanner' -w words -s -noninteractive -timeout 5")
            assert "scanner" in scanner
            # 两次调用必须保留同一个真实 Chromium 页面，而不是每次重新打开。
            trust = "mkdir -p ~/.pki/nssdb; certutil -N -d sql:$HOME/.pki/nssdb --empty-password; certutil -A -d sql:$HOME/.pki/nssdb -n cairn-synthetic -t 'C,,' -i /run/cairn-ca/project.pem"
            execute(scopes[0], trust)
            expect(scopes[0], "/browser?case=browser", (address, 9443), "https")
            expect(scopes[0], "/favicon.ico", (address, 9443), "https")
            browser = execute(scopes[0], f"playwright-cli open 'https://{address}:9443/browser?case=browser'")
            assert "traffic-synthetic" in browser, browser
            assert "traffic-synthetic" in execute(scopes[0], "playwright-cli eval 'document.title'")
            # 当前 CLI 用 Unix socket、Chromium 用 pipe；不为管理通信排除整个 localhost。
            management = r'''from pathlib import Path
flags = []
for path in Path('/proc').glob('[0-9]*/cmdline'):
    try: args = path.read_bytes().split(b'\0')
    except FileNotFoundError: continue
    if args and Path(args[0].decode()).name in ('chrome', 'chromium', 'chromium-browser', 'headless_shell'):
        flags.extend(arg.decode() for arg in args if arg.startswith(b'--remote-debugging-'))
print(flags)
print('\n'.join(line for line in Path('/proc/net/unix').read_text().splitlines() if '/tmp/pw-' in line))
'''
            management_output = execute(scopes[0], "python3 -c " + shlex.quote(management))
            assert "--remote-debugging-pipe" in management_output, management_output
            assert "--remote-debugging-port" not in management_output, management_output
            assert f"-cairn-{scopes[0].id}.sock" in management_output, management_output
            execute(scopes[0], "playwright-cli close")
            assert len(wait_for_flows()) == 8

            # 两个 namespace 同端口提供不同正文，证明回环连接没有被送到远端网关。
            for scope in scopes:
                local_site = runtime.workspace / scope.id
                local_site.mkdir()
                os.chown(local_site, 1000, 1000)
                certificate(local_site, issuer=ca, address=["127.0.0.1", scope.address])
                (local_site / "server.py").write_text(SITE)
                local = (
                    "import subprocess; subprocess.Popen(['python3', "
                    f"'{WORKSPACE}/{scope.id}/server.py', '{WORKSPACE}/{scope.id}', '{scope.id}'], "
                    "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)"
                )
                execute(scope, "python3 -c " + shlex.quote(local))
                deadline = time.monotonic() + 10
                while not (local_site / "ready").exists():
                    assert time.monotonic() < deadline, f"local service not ready: {scope.id}"
                    time.sleep(0.05)
            for source, destination, host, route in (
                (scopes[0], scopes[0], "127.0.0.1", "localhost-0"),
                (scopes[1], scopes[1], "127.0.0.1", "localhost-1"),
                (scopes[1], scopes[0], scopes[0].address, "same-subnet"),
            ):
                for scheme, port in (("http", 8990), ("https", 9444)):
                    request_id = f"{route}-{scheme}"
                    path = f"/control-file?case={request_id}"
                    body = f"shared-input:{destination.id}".encode()
                    expect(source, path, (host, port), scheme, body, request_id)
                    output = execute(source, f"curl --proxy '' --fail --silent --show-error --max-time 10 -H 'X-S0-Request: {request_id}' '{scheme}://{host}:{port}{path}'")
                    assert body.decode() in output, output
            # 原 localhost／同网段两条 HTTP 反例保留，并扩展为 HTTPS 和双回环身份。
            observed = wait_for_flows()
            assert len(observed) == 14, "S0-B coverage gap or duplicate capture"
            saved = wait_api(lambda result: len(result["items"]) == 14 and all(item["state"] == "complete" for item in result["items"]))
            for item in saved["items"]:
                raw = next(flow for flow in observed if flow["id"] == item["id"])
                assert item["scope"] == raw["scope"]
                assert item["request"] == raw["request"] and item["response"] == raw["response"]
                for side in ("request", "response"):
                    actual = (runtime.capture / item["scope"]["id"] / "flows" / item["id"] / f"{side}.body").read_bytes()
                    assert api.get(prefix + f"/{item['id']}/bodies/{side}", timeout=10).content == actual

            # 非空二进制双方原件在执行端尚存活时读取，不用 UTF-8 或 base64 JSON 上传正文。
            binary = bytes(range(256)) * 1024
            (runtime.workspace / "binary-input").write_bytes(binary)
            execute(scopes[0], f"curl --proxy '' --silent --show-error --max-time 10 --data-binary @binary-input 'http://{address}:8989/binary' -o binary-output")
            assert (runtime.workspace / "binary-output").read_bytes() == binary[::-1]
            saved = wait_api(lambda result: len(result["items"]) == 15 and all(item["state"] == "complete" for item in result["items"]))
            post = next(item for item in saved["items"] if item["request"]["method"] == "POST")
            for side, content in (("request", binary), ("response", binary[::-1])):
                assert api.get(prefix + f"/{post['id']}/bodies/{side}", timeout=10).content == content
                preview = get(f"/{post['id']}/bodies/{side}", preview="true", offset=5, limit=17)
                assert base64.b64decode(preview["content_base64"]) == content[5:22] and preview["has_more"]

            # 响应保持打开时，已封存请求可读，增长响应不能冒充可下载的空正文。
            with ThreadPoolExecutor(max_workers=1) as pool:
                running = pool.submit(execute, scopes[0], f"curl --proxy '' --silent --show-error --max-time 25 'http://{address}:8989/interrupted' -o interrupted-output; status=$?; test \"$status\" -eq 18")
                active = wait_api(lambda result: any(
                    item["request"]["target_base64"] == base64.b64encode(b"/interrupted").decode()
                    and item["response"] is not None and item["sync"]["bodies"]["request"]["status"] == "available"
                    for item in result["items"]
                ))
                partial = next(item for item in active["items"] if item["request"]["target_base64"] == base64.b64encode(b"/interrupted").decode())
                assert partial["state"] == "receiving" and partial["sync"]["bodies"]["response"]["status"] == "receiving"
                assert api.get(prefix + f"/{partial['id']}/bodies/request", timeout=10).content == b""
                assert api.get(prefix + f"/{partial['id']}/bodies/response", timeout=10).status_code == 409
                old_record = json.loads((runtime.capture / scopes[0].id / "flows" / partial["id"] / "flow.json").read_text())
                watermark = active["update_cursor"]
                (site / "release-interrupted").touch()
                running.result(timeout=15)
            terminal = wait_api(lambda result: any(item["id"] == partial["id"] and item["state"] == "partial" for item in result["items"]))
            partial = get("/" + partial["id"])
            assert partial["request"]["body_complete"] and not partial["response"]["body_complete"]
            assert api.get(prefix + f"/{partial['id']}/bodies/response", timeout=10).content == b"actual-prefix\x00\xff"
            updates = get(after_update=watermark, limit=1)
            changed = []
            while True:
                changed.extend(item["id"] for item in updates["items"])
                if not updates["has_more"]:
                    break
                updates = get(after_update=updates["update_cursor"], limit=1)
            assert partial["id"] in changed

            # 原采集旧版本不能回退终态；同版本冲突与正文冲突都必须拒绝。
            replay = traffic_api.machine.sync_traffic_flow(runtime.project_id, scopes[0].id, old_record)
            assert replay.ok and replay.data["state"] == "partial"
            conflict = json.loads(json.dumps(old_record))
            conflict["request"]["host"] = "conflict.invalid"
            assert traffic_api.machine.sync_traffic_flow(runtime.project_id, scopes[0].id, conflict).status_code == 409
            post_record = json.loads((runtime.capture / scopes[0].id / "flows" / post["id"] / "flow.json").read_text())
            wrong_body = bytes([1]) + binary[1:]
            conflict_body = traffic_api.machine.sync_traffic_body(
                runtime.project_id, scopes[0].id, post["id"], "request", offset=0,
                size=len(wrong_body), sha256=sha256(wrong_body).hexdigest(), content=wrong_body,
            )
            assert conflict_body.status_code == 409
            conflict_seal = traffic_api.machine.seal_traffic_body(
                runtime.project_id, scopes[0].id, post["id"], "request",
                revision=post_record["request"]["body_revision"], size=len(wrong_body), sha256=sha256(wrong_body).hexdigest(),
            )
            assert conflict_seal.status_code == 409
            replay_seal = traffic_api.machine.seal_traffic_body(
                runtime.project_id, scopes[0].id, post["id"], "request",
                revision=post_record["request"]["body_revision"], size=len(binary), sha256=sha256(binary).hexdigest(),
            )
            assert replay_seal.ok and replay_seal.data["artifact_id"] == post["sync"]["bodies"]["request"]["artifact_id"]
            rejection = api.post(prefix + "/scopes", json=post_record["scope"], timeout=10)
            assert rejection.status_code == 401 and rejection.json()["detail"] == "Machine authentication required"
            other = f"{traffic_api.base_url}/projects/{traffic_api.other_project_id}/traffic/{post['id']}"
            assert api.get(other, timeout=10).status_code == 404
            assert api.get(other + "/bodies/request", timeout=10).status_code == 404

            assert traffic_api.outsider.get(prefix + f"/{post['id']}", timeout=10).status_code == 404
            assert traffic_api.outsider.get(prefix + f"/{post['id']}/bodies/request", timeout=10).status_code == 404
            execute(scopes[1], f"curl --proxy '' --silent --show-error --max-time 10 'http://{address}:8989/no-response' -o no-response-output")
            failed = wait_api(lambda result: len(result["items"]) == 17 and any(item["state"] == "failed" for item in result["items"]))
            absent = next(item for item in failed["items"] if item["state"] == "failed")
            assert absent["response"] is None and absent["sync"]["bodies"]["response"]["status"] == "absent"
            assert api.get(prefix + f"/{absent['id']}/bodies/response", timeout=10).status_code == 404
            assert absent["sync"]["bodies"]["request"]["status"] == "available"

            page, ids = get(limit=3), []
            while True:
                ids.extend(item["id"] for item in page["items"])
                if not page["has_more"]:
                    break
                page = get(limit=3, cursor=page["next_cursor"])
            assert len(ids) == len(set(ids)) == 17
            sync.stop()
            before = get(limit=200)
            artifact_ids = {item["id"]: {side: item["sync"]["bodies"][side]["artifact_id"] for side in ("request", "response")} for item in before["items"]}
            with db.get_conn() as conn:
                assert conn.execute("SELECT count(*) AS n FROM http_flows WHERE project_id = ?", (runtime.project_id,)).fetchone()["n"] == 17
                assert conn.execute("SELECT count(*) AS n FROM artifacts WHERE project_id = ?", (runtime.project_id,)).fetchone()["n"] == 33
                assert conn.execute("SELECT count(*) AS n FROM agent_events WHERE project_id = ? AND kind = 'artifact_created'", (runtime.project_id,)).fetchone()["n"] == 0
            # 不仅依赖本地已发集合：全新回执目录重传同一私有 spool，实际执行容器已退出。
            runtime.close()
            sync = TrafficSync(traffic_api.machine, capture=runtime.capture, receipts=root / "replay-receipts", interval=0.1)
            sync.start()
            deadline = time.monotonic() + 20
            while len(list((root / "replay-receipts").glob("*.json"))) != 17 or sum(
                len(json.loads(path.read_text())["bodies"]) for path in (root / "replay-receipts").glob("*.json")
            ) != 33:
                assert time.monotonic() < deadline, sync.status()
                time.sleep(0.05)
            sync.stop()
            after = get(limit=200)
            assert after["update_cursor"] == before["update_cursor"]
            assert {item["id"]: {side: item["sync"]["bodies"][side]["artifact_id"] for side in ("request", "response")} for item in after["items"]} == artifact_ids
            with db.get_conn() as conn:
                assert conn.execute("SELECT count(*) AS n FROM artifacts WHERE project_id = ?", (runtime.project_id,)).fetchone()["n"] == 33
            assert api.get(prefix + f"/{post['id']}/bodies/request", timeout=10).content == binary
            # 共用 raw 存储抽取后，旧 Artifact 调用方仍流式写入并产生原有审计事件。
            legacy = traffic_api.machine.create_artifact(
                runtime.project_id, task_run_id=scopes[0].task_run_id, kind="file",
                content=BytesIO(binary), filename="synthetic-legacy.bin", metadata={},
            )
            assert legacy.ok
            download = api.get(f"{traffic_api.base_url}/projects/{runtime.project_id}/artifacts/{legacy.data['id']}/content", timeout=10)
            assert download.status_code == 200 and download.content == binary
            with db.get_conn() as conn:
                assert conn.execute("SELECT count(*) AS n FROM agent_events WHERE project_id = ? AND kind = 'artifact_created'", (runtime.project_id,)).fetchone()["n"] == 1
            print("S1_A_RESULT=" + json.dumps({"flows": 17, "complete": 15, "partial": 1, "failed": 1, "artifacts": 33,
                "binary_request_bytes": len(binary), "receipt_loss_recovered": True, "replay_after_container_exit": True,
                "gated_scopes": len(gated_scopes), "browser_management": "unix-socket-and-pipe"}))
        except AssertionError:
            print("S0_FLOW_FAILURES=" + json.dumps([
                {"state": flow["state"], "error": flow["error"]}
                for path in runtime.capture.glob("*/flows/*/flow.json")
                for flow in [json.loads(path.read_text())]
            ]))
            raise
        finally:
            if sync is not None:
                sync.stop()
            runtime.close()
            client.close()
