"""TaskRun 私有 Unix socket 执行端；不监听网络，不持有 Docker 或模型凭据。"""

from __future__ import annotations

import base64
import http.server
import json
import os
from pathlib import Path
import selectors
import signal
import socketserver
import subprocess
import threading
import time


SOCKET_PATH = "/run/cairn-exec/execute.sock"
MAX_REQUEST_BYTES = 2 * 1024 * 1024


def capture_ready() -> bool:
    try:
        timestamp = float(Path("/run/cairn-exec/traffic-ready").read_text())
        return 0 <= time.time() - timestamp <= 3
    except (OSError, ValueError):
        return False


def watch_capture() -> None:
    admitted = False
    while True:
        ready = capture_ready()
        if admitted and not ready:
            # 主进程退出使 Docker 结束整个测试容器，包括脱离命令进程组的浏览器/后台任务。
            os._exit(75)
        admitted = admitted or ready
        time.sleep(0.25)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        # 工具参数、请求正文和输出只走私有执行通道，不进入服务日志。
        return

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        if not capture_ready():
            self.send_error(503, "traffic_not_ready")
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ready\n")

    def do_POST(self) -> None:
        if self.path != "/execute":
            self.send_error(404)
            return
        if not capture_ready():
            self.send_error(503, "traffic_not_ready")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_REQUEST_BYTES:
                raise ValueError
            request = json.loads(self.rfile.read(size))
            if (request.get("scope_id") != os.environ["CAIRN_EXECUTION_SCOPE"]
                    or request.get("task_run_id") != os.environ["CAIRN_EXECUTION_TASK_RUN"]):
                raise ValueError
            command, cwd = request["command"], request["cwd"]
            timeout = request["timeout_seconds"]
            env = request.get("env", {})
            stdin = base64.b64decode(request.get("stdin_base64", ""), validate=True)
            if not isinstance(command, str) or "\0" in command:
                raise ValueError
            if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                raise ValueError
            if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
                raise ValueError
            if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) and "\0" not in k + v and "=" not in k for k, v in env.items()):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            self.send_error(400, "invalid execution request")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        process = None
        try:
            process = subprocess.Popen(
                ["/bin/bash", "-c", command], cwd=cwd,
                env={**os.environ, **env}, start_new_session=True,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            assert process.stdin is not None
            assert process.stdout is not None and process.stderr is not None

            def feed() -> None:
                try:
                    process.stdin.write(stdin)
                    process.stdin.close()
                except BrokenPipeError:
                    pass

            threading.Thread(target=feed, daemon=True).start()
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while True:
                    if time.monotonic() >= deadline:
                        self.terminate(process)
                        self.emit({"error": "execution_timeout"})
                        return
                    ready = selector.select(0.2)
                    for key, _ in ready:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if chunk:
                            self.emit({key.data: base64.b64encode(chunk).decode("ascii")})
                        else:
                            selector.unregister(key.fileobj)
                    result = process.poll()
                    if result is not None and (not selector.get_map() or not ready):
                        self.emit({"exit_code": result})
                        return
                    # 即使子进程没有输出，也能发现取消后断开的控制连接。
                    self.emit({"running": True})
        except (BrokenPipeError, ConnectionResetError):
            if process is not None:
                self.terminate(process)
        except OSError:
            if process is not None:
                self.terminate(process)
            self.emit({"error": "execution_unavailable"})
        finally:
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

    def emit(self, value: dict) -> None:
        self.wfile.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
        self.wfile.flush()

    @staticmethod
    def terminate(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


if __name__ == "__main__":
    # Chromium 使用 NSS trust store，不读取 curl/Node 的 CA 环境变量。
    trust = Path.home() / ".pki" / "nssdb"
    trust.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not (trust / "cert9.db").exists():
        subprocess.run(["certutil", "-N", "-d", f"sql:{trust}", "--empty-password"], check=True)
    subprocess.run([
        "certutil", "-A", "-d", f"sql:{trust}", "-n", "cairn-project", "-t", "C,,",
        "-i", "/run/cairn-ca/project.pem",
    ], check=True)
    # 每个执行代次使用新目录；不删除未知来源的旧 socket 来掩盖双启动。
    with Server(SOCKET_PATH, Handler) as server:
        os.chmod(SOCKET_PATH, 0o600)
        threading.Thread(target=watch_capture, daemon=True).start()
        server.serve_forever()
