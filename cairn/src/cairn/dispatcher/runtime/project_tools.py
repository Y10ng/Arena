"""会话绑定的私有项目工具桥；平台认证与项目身份不进入模型参数。"""

from __future__ import annotations

import base64
import binascii
import json
import re

from docker.errors import DockerException
from requests.exceptions import RequestException

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.research_sessions import read_session_page


class ProjectToolBinding:
    def __init__(self, client: CairnClient, project_id: str | None, conversation_id: str,
                 message_id: str, task_run_id: str, forbidden_literals: list[str], *, manager=None, cancellation=None):
        self.cancellation = cancellation
        self.workspace = None
        self.manager = manager
        self.client = client
        self.project_id = project_id
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.task_run_id = task_run_id
        self.forbidden_literals = forbidden_literals

    def dispatch(self, request: dict) -> dict:
        if (not isinstance(request, dict) or set(request) != {"method", "params", "tool_call_id"}
                or not isinstance(request["method"], str)
                or (request["method"].startswith("_") and request["method"] != "_workspace_bash")
                or not isinstance(request["params"], dict)
                or not isinstance(request["tool_call_id"], str) or not 1 <= len(request["tool_call_id"]) <= 512):
            raise ValueError("project_tool_request_invalid")
        method, params = request["method"], request["params"]
        transfer = method in {"read_conversation_attachment", "read_conversation_research"} and "workspace_path" in params
        command = method == "_workspace_bash"
        outgoing = request
        if transfer:
            if method == "read_conversation_attachment":
                if set(params) != {"attachment_id", "workspace_path"}:
                    raise ValueError("project_tool_attachment_arguments")
                outgoing = {**request, "method": "_read_conversation_attachment_bytes", "params": {"attachment_id": params["attachment_id"]}}
            else:
                if "artifact_id" not in params or set(params) - {"conversation_id", "task_run_id", "artifact_id", "workspace_path", "source"}:
                    raise ValueError("project_tool_material_arguments")
                outgoing = {**request, "method": "_read_conversation_research_bytes",
                            "params": {key: value for key, value in params.items() if key != "workspace_path"}}
        elif method == "register_conversation_material":
            if set(params) != {"workspace_path"} or self.workspace is None:
                raise ValueError("project_tool_material_arguments")
            content = self.workspace.read_bytes(params["workspace_path"])
            if any(value and value.encode("utf-8") in content for value in self.forbidden_literals):
                raise ValueError("project_tool_protected_material")
            path = self.workspace.path(params["workspace_path"])
            outgoing = {**request, "params": {"filename": path.name,
                        "source_path": f"{self.workspace.cwd}/{path.relative_to(self.workspace.directory)}",
                        "content_base64": base64.b64encode(content).decode("ascii")}}
        elif command:
            # 只核验本轮身份与存活，不读取无关项目详情。
            outgoing = {**request, "method": "_validate_workspace_claim", "params": {}}
        elif method == "publish_custom_markdown" and "workspace_path" in params:
            if "content" in params or self.workspace is None:
                raise ValueError("project_tool_markdown_arguments")
            content = self.workspace.read(params["workspace_path"])
            if any(value and value in content for value in self.forbidden_literals):
                raise ValueError("project_tool_protected_material")
            outgoing = {**request, "params": {key: value for key, value in params.items() if key != "workspace_path"} | {"content": content}}
        # 具体业务参数由 TS 单处定义、服务端验证；socket 仅添加不可由模型覆盖的执行身份。
        response = self.client.agent_turn_action(
            self.conversation_id, self.message_id, "tools",
            {"task_run_id": self.task_run_id, **outgoing},
        )
        data = response.data
        if response.ok and method == "start_conversation_research":
            if self.workspace is None or self.cancellation is None or self.manager.traffic_execution is None:
                raise ValueError("chat_research_backend_missing")
            # 出口认证只经可信 Server→Dispatcher→测试后端，不进入模型返回或控制环境。
            resolved = self.client.agent_turn_action(
                self.conversation_id, self.message_id, "tools",
                {"task_run_id": self.task_run_id, "tool_call_id": request["tool_call_id"],
                 "method": "_resolve_conversation_research", "params": {}},
            )
            if not resolved.ok:
                response, data = resolved, resolved.data
            else:
                try:
                    self.workspace.research_execution = self.manager.traffic_execution.prepare_conversation(
                        self.conversation_id, self.task_run_id, self.cancellation,
                        test_egress=resolved.data["test_egress"],
                    )
                except (DockerException, RequestException, OSError, RuntimeError):
                    # 准备失败由后端收口已登记资源；不将 Docker/HTTP 原文或堆栈传给模型。
                    raise ValueError("project_tool_research_unavailable") from None
                data = {**data, "state": "ready", "scope_id": self.workspace.research_execution.scope_id}
        if response.ok and (transfer or command):
            if self.workspace is None:
                raise ValueError("project_tool_workspace_missing")
            if command:
                if not isinstance(data, dict) or data.get("active") is not True:
                    raise ValueError("project_tool_claim_invalid")
                data = self.workspace.execute(params)
            else:
                material_key = "attachment" if method == "read_conversation_attachment" else "artifact"
                if not isinstance(data, dict) or not isinstance(data.get("content_base64"), str) or not isinstance(data.get(material_key), dict):
                    raise ValueError("project_tool_attachment_invalid")
                try:
                    content = base64.b64decode(data["content_base64"], validate=True)
                except (ValueError, binascii.Error):
                    raise ValueError("project_tool_attachment_invalid") from None
                if data[material_key].get("size") != len(content):
                    raise ValueError("project_tool_attachment_size_mismatch")
                if any(value and value.encode("utf-8") in content for value in self.forbidden_literals):
                    raise ValueError("project_tool_protected_material")
                path = self.workspace.write(params["workspace_path"], content)
                data = {material_key: data[material_key], "workspace_path": path, "size_bytes": len(content),
                        "source_reference": data["source_reference"], "source_scope": data["source_scope"]}
        if (response.ok and request["method"] == "read_research_session"
                and isinstance(data, dict) and data.get("session_id")):
            page = read_session_page(self.manager, data)
            # 文件原文先回可信 Server 检查来源凭据；审核前的页和路径不返回模型。
            response = self.client.agent_turn_action(
                self.conversation_id, self.message_id, "tools",
                {"task_run_id": self.task_run_id, "tool_call_id": request["tool_call_id"],
                 "method": "_admit_research_session_page", "params": {"project_id": data["project_id"], "task_run_id": data["task_run_id"], "page": page}},
            )
            data = response.data
        if not response.ok:
            # 只传递服务端明确审查过的业务错误，不公开任意 HTTP 正文或内部异常。
            detail = data.get("detail") if isinstance(data, dict) else None
            if (not isinstance(detail, dict) or set(detail) - {"code", "message", "action_url"}
                    or not isinstance(detail.get("code"), str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,100}", detail["code"])
                    or not isinstance(detail.get("message"), str) or not detail["message"]
                    or ("action_url" in detail and not isinstance(detail["action_url"], str))):
                raise ValueError("project_tool_failed")
            data = {"error": detail}
        if not isinstance(data, dict):
            raise ValueError("project_tool_failed")
        raw = json.dumps(data, ensure_ascii=False)
        if any(value and value in raw for value in self.forbidden_literals):
            raise ValueError("project_tool_protected_material")
        return data
