"""将实际 HTTP 观察写入 scope 私有 spool；执行来源只从该实例的运行时绑定读取。"""

from __future__ import annotations

import asyncio
import base64
from functools import wraps
import time
import json
import os
from pathlib import Path
from typing import BinaryIO

from mitmproxy import http, version


ROOT = Path("/capture")


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

    @durable_hook
    def running(self) -> None:
        (ROOT / "flows").mkdir(mode=0o700, exist_ok=True)
        (ROOT / "ready").touch()
        asyncio.create_task(self.heartbeat())

    async def heartbeat(self) -> None:
        while True:
            try:
                with (ROOT / "heartbeat.tmp").open("w") as output:
                    output.write(str(time.time()))
                    output.flush()
                    os.fsync(output.fileno())
                (ROOT / "heartbeat.tmp").replace(ROOT / "heartbeat")
            except OSError:
                os._exit(74)
            await asyncio.sleep(0.25)

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
        record[side]["body_file"] = path.name

        @durable_hook
        def stream(chunk: bytes) -> bytes:
            if chunk:
                output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
                record[side]["body_bytes"] += len(chunk)
                self.save(record)
            return chunk

        message = flow.request if side == "request" else flow.response
        assert message is not None
        message.stream = stream

    @durable_hook
    def end_body(self, flow: http.HTTPFlow, side: str, complete: bool) -> None:
        output = self.bodies.pop((flow.id, side), None)
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
            output.close()
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
