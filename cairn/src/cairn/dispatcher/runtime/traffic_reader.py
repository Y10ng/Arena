"""控制容器私有的项目流量读取；平台鉴权始终留在 Dispatcher。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socketserver
import threading

from cairn.dispatcher.protocol.client import CairnClient

MAX_REQUEST_BYTES = 3 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _integer(value, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("traffic_range_invalid")
    return value


def _identity(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise ValueError("traffic_identity_invalid")
    return value


def _data(result):
    if not result.ok:
        # Server 错误正文可能包含头或 URL，不转发到工具错误及审计。
        raise ValueError(f"traffic_read_http_{result.status_code}")
    if not isinstance(result.data, dict):
        raise ValueError("traffic_response_invalid")
    return result.data


def _summary(record: dict) -> dict:
    request = record.get("request") or {}
    response = record.get("response") or {}
    return {
        "flow_id": record["id"], "project_id": record["project_id"],
        "scope": record["scope"], "revision": record["revision"],
        "state": record["state"], "source_state": record["source_state"],
        "method": request.get("method"), "host": request.get("host"),
        "status_code": response.get("status_code"),
        "started_at": record.get("started_at"), "finished_at": record.get("finished_at"),
        "sync": record["sync"],
    }


class TrafficReadBinding:
    def __init__(self, client: CairnClient, project_id: str, task_run_id: str):
        self.client = client
        self.project_id = _identity(project_id)
        self.task_run_id = _identity(task_run_id)

    def _source(self, record: dict, historical: bool) -> None:
        scope = record.get("scope") or {}
        if record.get("project_id") != self.project_id or scope.get("project_id") != self.project_id:
            raise ValueError("traffic_project_mismatch")
        if not historical and scope.get("task_run_id") != self.task_run_id:
            raise ValueError("traffic_historical_required")

    def dispatch(self, request: dict) -> dict:
        if not isinstance(request, dict) or set(request) != {"method", "params"}:
            raise ValueError("traffic_request_invalid")
        method, params = request["method"], request["params"]
        if not isinstance(params, dict):
            raise ValueError("traffic_request_invalid")
        historical = params.get("historical", False)
        if type(historical) is not bool:
            raise ValueError("traffic_historical_invalid")
        if method in {"prepare_http_evidence_image", "publish_http_evidence_image"}:
            allowed = {"source", "historical"}
            if method == "publish_http_evidence_image":
                allowed |= {"page", "page_count", "truncated", "content_base64", "expected_source"}
            if set(params) - allowed or not isinstance(params.get("source"), dict) or params["source"].get("kind") == "frozen":
                raise ValueError("traffic_request_invalid")
            # 身份由私有 socket 绑定，不接受工具覆盖执行归属；报告有单独冻结分支。
            body = {**params, "task_run_id": self.task_run_id}
            operation = (self.client.prepare_http_evidence_image if method == "prepare_http_evidence_image"
                         else self.client.publish_http_evidence_image)
            return _data(operation(self.project_id, body))
        if method == "list_http_flows":
            if set(params) - {"historical", "task_run_id", "cursor", "limit", "host", "method", "state", "status_code"}:
                raise ValueError("traffic_request_invalid")
            run = params.get("task_run_id")
            if run is not None:
                run = _identity(run)
                if run != self.task_run_id and not historical:
                    raise ValueError("traffic_historical_required")
            run = run if historical else self.task_run_id
            filters = {
                "cursor": _integer(params.get("cursor", 0), 0, 2**53 - 1),
                "limit": _integer(params.get("limit", 20), 1, 50),
            }
            for key in ("host", "method"):
                if key in params:
                    value = params[key]
                    if not isinstance(value, str) or not 1 <= len(value) <= 255 or "\0" in value:
                        raise ValueError("traffic_filter_invalid")
                    filters[key] = value
            if "state" in params:
                if params["state"] not in ("receiving", "complete", "partial", "failed"):
                    raise ValueError("traffic_filter_invalid")
                filters["state"] = params["state"]
            if "status_code" in params:
                filters["status_code"] = _integer(params["status_code"], 100, 999)
            data = _data(self.client.list_http_flows(self.project_id, task_run_id=run, **filters))
            for record in data["items"]:
                self._source(record, historical)
            return {"items": [_summary(record) for record in data["items"]],
                    "next_cursor": data["next_cursor"], "has_more": data["has_more"]}
        if method != "read_http_flow" or set(params) - {"flow_id", "historical", "side", "offset", "limit"}:
            raise ValueError("traffic_request_invalid")
        flow_id = _identity(params.get("flow_id"))
        side = params.get("side", "metadata")
        if side not in ("metadata", "request", "response"):
            raise ValueError("traffic_side_invalid")
        offset = _integer(params.get("offset", 0), 0, 2**53 - 1)
        limit = _integer(params.get("limit", 32768), 1, 32768)
        record = _data(self.client.read_http_flow(self.project_id, flow_id))
        self._source(record, historical)
        if side == "metadata":
            return _summary(record)
        if record["state"] == "receiving":
            # 进行中只提供已同步 body 前缀，不冒充稳定投影或产生可引用选段。
            preview = _data(self.client.read_http_flow_body_preview(
                self.project_id, flow_id, side, offset=offset, limit=limit,
            ))
            return {**preview, **_summary(record), "side": side,
                    "representation": "body-preview-base64", "preview": True, "excerpt_eligible": False}
        projection = _data(self.client.read_http_flow_projection(
            self.project_id, flow_id, side, offset=offset, limit=limit,
        ))
        self._source(projection, historical)
        return projection


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(15)
        try:
            line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                raise ValueError("traffic_request_too_large")
            data = self.server.binding.dispatch(json.loads(line))
            response = json.dumps({"ok": True, "data": data}, ensure_ascii=False).encode() + b"\n"
            if len(response) > MAX_RESPONSE_BYTES:
                raise ValueError("traffic_response_too_large")
        except (ValueError, UnicodeError) as exc:
            code = str(exc)
            if not re.fullmatch(r"(?:traffic|project_tool)_[a-z0-9_]+", code):
                code = "traffic_request_invalid"
            response = json.dumps({"ok": False, "error": code}).encode() + b"\n"
        except (OSError, KeyError, TypeError):
            response = b'{"ok":false,"error":"traffic_read_unavailable"}\n'
        try:
            self.wfile.write(response)
        except OSError:
            pass


class TrafficReader:
    def __init__(self, directory: Path, name: str, binding: TrafficReadBinding):
        self.path = directory / name
        # Unix socket 地址有长度限制；通过已打开的私有目录绑定，不复制到共享 workspace。
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.server = socketserver.UnixStreamServer(f"/proc/self/fd/{fd}/{name}", _Handler)
        finally:
            os.close(fd)
        try:
            os.chmod(self.path, 0o600)
            owner = directory.stat()
            if self.path.stat().st_uid != owner.st_uid:
                os.chown(self.path, owner.st_uid, owner.st_gid)
            self.server.binding = binding
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            self.thread.start()
        except BaseException:
            self.server.server_close()
            self.path.unlink(missing_ok=True)
            raise

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.path.unlink(missing_ok=True)
