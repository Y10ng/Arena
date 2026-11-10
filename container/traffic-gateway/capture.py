"""将实际 HTTP 观察写入 scope 私有 spool；执行来源只从该实例的运行时绑定读取。

heartbeat 由独立 OS 线程推进，用于启动就绪与现场诊断；陈旧心跳不能证明
进程死亡，宿主不再据其年龄取消任务。采集进展与写延迟由 save/chunk 路径
写入 spool 诊断文件；真实落盘失败仍终止观察点，不能无记录地继续转发。
"""

from __future__ import annotations

import base64
from functools import wraps
import time
import json
import os
from pathlib import Path
import threading
from typing import BinaryIO

from mitmproxy import http, version


ROOT = Path("/capture")
# body 侧每累计 BODY_FSYNC_THRESHOLD 字节执行一次 flush+fsync 并保存记录，
# 而不是每个 chunk 都做全量重序列化与两段 fsync。
BODY_FSYNC_THRESHOLD = 1024 * 1024
# 单次 chunk 落盘耗时超过该值记入延迟诊断（毫秒）；正常 page cache 写远低于此。
SLOW_WRITE_MILLISECONDS = 25
# 保留既有诊断采样周期，不承担执行授权或死亡判定。
HEARTBEAT_PERIOD_SECONDS = 0.25


def encoded(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def durable_hook(callback):
    @wraps(callback)
    def invoke(*args, **kwargs):
        try:
            return callback(*args, **kwargs)
        except OSError:
            # mitmproxy 默认记录 addon 异常后继续转发；落盘失败必须使观察点退出。
            os._exit(74)
    return invoke


class Capture:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.bodies: dict[tuple[str, str], BinaryIO] = {}
        self.binding = json.loads(Path("/run/cairn-traffic/scope.json").read_text())
        # 每个 flow 侧自上次阶段化 fsync 以来的累计字节数。
        self._pending_bytes: dict[tuple[str, str], int] = {}
        self._slow_writes = 0
        self._last_write_milliseconds = 0.0
        self._last_save_at = None
        self._heartbeat_stop = threading.Event()

    def _write_heartbeat(self) -> None:
        # 写失败即进程失去 spool 访问，须退出（线程内由 beat 捕获 OSError）。
        with (ROOT / "heartbeat.tmp").open("w") as output:
            output.write(str(time.time()))
            output.flush()
            os.fsync(output.fileno())
        (ROOT / "heartbeat.tmp").replace(ROOT / "heartbeat")

    def _write_diagnostics(self) -> None:
        # 崩溃前的最后状态也要可读；诊断文件本身容忍写入失败（非权威事实）。
        try:
            (ROOT / "capture-diagnostics.json").write_text(json.dumps({
                "written_at": time.time(),
                "last_save_at": self._last_save_at,
                "slow_writes": self._slow_writes,
                "last_write_milliseconds": round(self._last_write_milliseconds, 2),
            }), encoding="ascii")
        except OSError:
            pass

    def _start_heartbeat_thread(self) -> None:
        # 心跳与采集事件循环分离，便于诊断调度/I/O 停顿；不据此推导进程死亡。
        def beat() -> None:
            while not self._heartbeat_stop.wait(HEARTBEAT_PERIOD_SECONDS):
                try:
                    self._write_heartbeat()
                except OSError:
                    # 落盘失败必须使观察点退出，采集不得在失去 spool 后继续。
                    os._exit(74)

        self._heartbeat_thread = threading.Thread(
            target=beat, name="cairn-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    @durable_hook
    def running(self) -> None:
        (ROOT / "flows").mkdir(mode=0o700, exist_ok=True)
        (ROOT / "ready").touch()
        self._write_heartbeat()
        self._write_diagnostics()
        # heartbeat 周期推进移入独立线程；事件循环与采集 I/O 不再参与心跳计时。
        self._start_heartbeat_thread()

    @durable_hook
    def save(self, record: dict) -> None:
        record["revision"] += 1
        directory = ROOT / "flows" / record["id"]
        temporary = directory / "flow.json.tmp"
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(record, output, ensure_ascii=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(directory / "flow.json")
        descriptor = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._last_save_at = time.time()
        self._write_diagnostics()

    @durable_hook
    def requestheaders(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get('cairn_proxy_handshake'):
            return
        directory = ROOT / "flows" / flow.id
        directory.mkdir(mode=0o700)
        record = {
            "id": flow.id,
            "scope": self.binding,
            "revision": 0,
            "representation": "structured-http-v1",
            "collector_version": version.VERSION,
            "observation_point": "task-network-output",
            "original_destination": flow.server_conn.address,
            "connection_id": flow.client_conn.id,
            "started_at": flow.request.timestamp_start,
            "finished_at": None,
            "state": "receiving",
            "request": self.message(flow.request),
            "response": None,
            "error": None,
        }
        self.records[flow.id] = record
        self.begin_body(flow, "request")
        self.save(record)

    @staticmethod
    def message(message: http.Message) -> dict:
        data = {
            "http_version": message.http_version,
            "headers_base64": [[encoded(name), encoded(value)] for name, value in message.headers.fields],
            "body_bytes": 0,
            "body_complete": False,
            "body_sealed": False,
            "body_revision": None,
        }
        if isinstance(message, http.Request):
            data.update({
                "method": message.method,
                "host": message.host,
                "port": message.port,
                "scheme": message.scheme,
                "target_base64": encoded(message.data.path),
                "authority_base64": encoded(message.data.authority),
            })
        else:
            data.update({"status_code": message.status_code, "reason_base64": encoded(message.data.reason)})
        return data

    def begin_body(self, flow: http.HTTPFlow, side: str) -> None:
        record = self.records[flow.id]
        path = ROOT / "flows" / flow.id / f"{side}.body"
        output = path.open("xb")
        self.bodies[flow.id, side] = output
        self._pending_bytes[flow.id, side] = 0
        record[side]["body_file"] = path.name

        @durable_hook
        def stream(chunk: bytes) -> bytes:
            if chunk:
                # 缓冲写为主：单次慢写只记入诊断，不逐 chunk fsync；
                # 每 BODY_FSYNC_THRESHOLD 字节做一次 flush+fsync+记录保存。
                started = time.monotonic()
                output.write(chunk)
                self._last_write_milliseconds = (time.monotonic() - started) * 1000
                if self._last_write_milliseconds >= SLOW_WRITE_MILLISECONDS:
                    self._slow_writes += 1
                self._pending_bytes[flow.id, side] += len(chunk)
                record[side]["body_bytes"] += len(chunk)
                if self._pending_bytes[flow.id, side] >= BODY_FSYNC_THRESHOLD:
                    self._checkpoint_body(flow.id, side, record)
            return chunk

        message = flow.request if side == "request" else flow.response
        assert message is not None
        message.stream = stream

    def _checkpoint_body(self, flow_id: str, side: str, record: dict) -> None:
        # 阶段化持久点：flush+fsync body 后重存记录，宿主上传按已确认前缀推进。
        output = self.bodies[flow_id, side]
        output.flush()
        os.fsync(output.fileno())
        self._pending_bytes[flow_id, side] = 0
        self.save(record)

    @durable_hook
    def end_body(self, flow: http.HTTPFlow, side: str, complete: bool) -> None:
        output = self.bodies.pop((flow.id, side), None)
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
            output.close()
            self._pending_bytes.pop((flow.id, side), None)
            record = self.records[flow.id]
            record[side]["body_complete"] = complete
            # 封存版本只随采集事实产生，后续上传重试不改变该身份。
            record[side]["body_sealed"] = True
            record[side]["body_revision"] = record["revision"] + 1

    def request(self, flow: http.HTTPFlow) -> None:
        if flow.id in self.records:
            self.end_body(flow, "request", True)
            self.save(self.records[flow.id])

    @durable_hook
    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if flow.id not in self.records or flow.response is None:
            return
        record = self.records[flow.id]
        record["response"] = self.message(flow.response)
        self.begin_body(flow, "response")
        self.save(record)

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.id not in self.records:
            return
        record = self.records[flow.id]
        self.end_body(flow, "response", True)
        # 提前响应或取消不能把尚未采全的请求侧标成完整。
        self.end_body(flow, "request", False)
        record["state"] = "complete" if record["request"]["body_complete"] else "partial"
        record["finished_at"] = flow.response.timestamp_end if flow.response else None
        self.save(record)
        del self.records[flow.id]

    def error(self, flow: http.HTTPFlow) -> None:
        if flow.id not in self.records:
            return
        record = self.records[flow.id]
        self.end_body(flow, "request", False)
        self.end_body(flow, "response", False)
        record["state"] = "partial" if record["response"] is not None else "failed"
        record["error"] = {"code": "transport_error", "message": flow.error.msg if flow.error else None}
        if flow.server_conn.sockname:
            diagnostic = Path('/run/cairn-egress/diagnostics') / str(flow.server_conn.sockname[1])
            if diagnostic.is_file():
                code = diagnostic.read_text()
                messages = {
                    'traffic_proxy_connect_failed': '无法连接项目代理；未回退直连。',
                    'traffic_proxy_auth_failed': '项目代理拒绝认证；请检查出口认证设置。',
                    'traffic_proxy_tunnel_rejected': '项目代理拒绝目标隧道；请检查代理 CONNECT 策略或目标可达性。',
                    'traffic_proxy_handshake_failed': '代理握手或目标解析／连接失败；未回退直连。',
                }
                if code in messages:
                    record['error'] = {'code': code, 'message': messages[code]}
        record["finished_at"] = flow.error.timestamp if flow.error else None
        self.save(record)
        del self.records[flow.id]


addons = [Capture()]
