"""以独立有界容量领取项目对话；完成回执失败只重送回执，不重跑模型。"""

from __future__ import annotations

import logging
import json
import os
import time
from uuid import UUID, uuid4
from hashlib import sha256
from pathlib import Path
from typing import Literal

from concurrent.futures import Future
from pydantic import BaseModel, ConfigDict, Field

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.container_options import ContainerRuntimeOptions
from cairn.dispatcher.runtime.traffic_sync import _save_receipt
from cairn.dispatcher.tasks.agent_chat import run_agent_turn
from cairn.server.schemas.models import TenantModelExecutionBinding

from .worker_runtime import _pi_model_binding

LOG = logging.getLogger(__name__)
AGENT_CAPACITY = 1
AGENT_FINISH_ATTEMPTS = 6


class _PendingAgentFinish(BaseModel):
    """只保存已确认停止的回执；原结果与被拒后独立的失败回执不互相覆盖。"""

    model_config = ConfigDict(extra="forbid")
    conversation_id: str
    message_id: str
    task_run_id: str
    result: dict
    failure: dict | None = None
    attempts: int = Field(default=0, ge=0, le=AGENT_FINISH_ATTEMPTS)
    next_attempt_at: float = 0
    state: Literal["pending", "rejected", "exhausted"] = "pending"
    response_status: int | None = None

    @property
    def key(self) -> str:
        return sha256(f"{self.conversation_id}\0{self.task_run_id}".encode()).hexdigest()


class AgentChatMixin:
    def _agent_finish_directory(self) -> Path:
        owner = sha256(self.dispatcher_id.encode()).hexdigest()
        return self._candidate_outbox_parent / f"dispatcher-agent-finish-v1-{self._candidate_server_namespace}-{owner}"

    def _pending_agent_finishes(self) -> dict[str, _PendingAgentFinish]:
        if not hasattr(self, "_agent_finishes"):
            root = self._agent_finish_directory()
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            pending = {}
            for path in root.glob("*.json"):
                receipt = _PendingAgentFinish.model_validate_json(path.read_bytes())
                if path.stem != receipt.key:
                    raise ValueError("chat_finish_identity_invalid")
                pending[receipt.key] = receipt
                if receipt.state != "pending":
                    LOG.error("agent turn finish retained project=%s task_run=%s code=chat_finish_%s",
                              receipt.conversation_id, receipt.task_run_id, receipt.state)
            self._agent_finishes = pending
        return self._agent_finishes

    def _save_agent_finish(self, receipt: _PendingAgentFinish) -> None:
        # 复用已有 receipt 原子替换/fsync；目录归宿主所有，不挂入可执行模型的容器。
        _save_receipt(self._agent_finish_directory() / f"{receipt.key}.json", receipt.model_dump())

    def _send_agent_finish(self, receipt: _PendingAgentFinish) -> None:
        if receipt.state != "pending" or time.time() < receipt.next_attempt_at:
            return
        if receipt.attempts >= AGENT_FINISH_ATTEMPTS:
            receipt.state = "exhausted"
            self._save_agent_finish(receipt)
            LOG.error("agent turn finish unconfirmed project=%s task_run=%s code=chat_finish_exhausted",
                      receipt.conversation_id, receipt.task_run_id)
            return
        # 先保存消耗的预算，重启也不能把重送计数归零。崩溃可少送一次，但不能无限重送。
        receipt.attempts += 1
        receipt.next_attempt_at = time.time() + (5 * 2 ** (receipt.attempts - 3) if receipt.attempts >= 3 else 0)
        self._save_agent_finish(receipt)
        body = receipt.failure if receipt.failure is not None else receipt.result
        response = self.client.agent_turn_action(
            receipt.conversation_id, receipt.message_id, "finish",
            {"task_run_id": receipt.task_run_id, **body},
        )
        if response.ok:
            root = self._agent_finish_directory()
            (root / f"{receipt.key}.json").unlink()
            descriptor = os.open(root, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._pending_agent_finishes().pop(receipt.key)
            claim_path = self._agent_claim_directory() / (sha256(receipt.conversation_id.encode()).hexdigest() + ".json")
            claim_path.unlink(missing_ok=True)
            return
        receipt.response_status = response.status_code
        detail = response.data.get("detail") if isinstance(response.data, dict) else None
        if (receipt.failure is None and (
                response.status_code == 422 and detail in ("agent_public_content_blocked", "agent_answer_required")
                or response.status_code == 409 and detail == "agent_execution_source_unavailable")):
            # 不改写或再次公开被拒原件；独立失败回执也有自己的有限传输预算。
            receipt.failure = {"status": "failed", "text": "", "error_code": (
                "public_output_forbidden" if detail == "agent_public_content_blocked" else "chat_execution_failed"
            ), "usage": body.get("usage"), "usage_missing_responses": body.get("usage_missing_responses"),
                "session_initialized": body.get("session_initialized")}
            receipt.attempts = 0
            receipt.next_attempt_at = 0
        elif response.status_code not in (0, 408, 429) and response.status_code < 500:
            receipt.state = "rejected"
        elif receipt.attempts >= AGENT_FINISH_ATTEMPTS:
            receipt.state = "exhausted"
        self._save_agent_finish(receipt)
        if receipt.state != "pending":
            LOG.error("agent turn finish unconfirmed project=%s task_run=%s status=%s code=chat_finish_%s",
                      receipt.conversation_id, receipt.task_run_id, response.status_code, receipt.state)
        else:
            LOG.warning("agent turn finish deferred project=%s task_run=%s code=chat_finish_retry",
                        receipt.conversation_id, receipt.task_run_id)

    def _flush_agent_finishes(self) -> None:
        active = {(task.agent_conversation_id, task.task_run_id) for task in self.futures.values() if task.task_type == "agent_chat"}
        for receipt in list(self._pending_agent_finishes().values()):
            if (receipt.conversation_id, receipt.task_run_id) not in active:
                self._send_agent_finish(receipt)

    def _agent_claim_directory(self) -> Path:
        root = self._agent_finish_directory().with_name(self._agent_finish_directory().name + "-claims")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    def _read_agent_claim(self, path: Path) -> dict:
        value = json.loads(path.read_bytes())
        if (not isinstance(value, dict) or set(value) != {"conversation_id", "request_id", "attempts", "state"}
                or not isinstance(value["conversation_id"], str)
                or path.stem != sha256(value["conversation_id"].encode()).hexdigest()
                or not isinstance(value["request_id"], str) or str(UUID(value["request_id"])) != value["request_id"]
                or type(value["attempts"]) is not int or not 0 <= value["attempts"] <= 3
                or value["state"] not in ("pending", "accepted")):
            raise ValueError("chat_claim_receipt_invalid")
        return value

    def _claim_agent_turn(self, conversation_id: str) -> None:
        scope = "agent-claim:" + sha256(conversation_id.encode()).hexdigest()
        try:
            self._claim_agent_turn_once(conversation_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            code = "chat_claim_storage_unavailable" if isinstance(exc, OSError) else "chat_claim_receipt_invalid"
            self._log_changed(scope, logging.ERROR, "agent claim unavailable code=%s", code)

    def _claim_agent_turn_once(self, conversation_id: str) -> None:
        path = self._agent_claim_directory() / (sha256(conversation_id.encode()).hexdigest() + ".json")
        claim_request = self._read_agent_claim(path) if path.exists() else {
            "conversation_id": conversation_id, "request_id": str(uuid4()), "attempts": 0, "state": "pending",
        }
        if claim_request["conversation_id"] != conversation_id:
            raise ValueError("chat_claim_identity_invalid")
        scope = "agent-claim:" + path.stem
        if claim_request["state"] != "pending" or claim_request["attempts"] >= 3:
            self._log_changed(scope, logging.ERROR, "agent claim retained code=%s",
                              "chat_claim_execution_unknown" if claim_request["state"] == "accepted" else "chat_claim_exhausted")
            return
        self._clear_log_state(scope)
        claim_request["attempts"] += 1
        _save_receipt(path, claim_request)
        response = self.client.claim_agent_turn(conversation_id, claim_request["request_id"])
        if response.status_code == 204 or (not response.ok and response.status_code not in (0, 408, 429) and response.status_code < 500):
            path.unlink()
            return
        if not response.ok:
            return
        claim = response.data
        if (not isinstance(claim, dict) or claim.get("conversation_id") != conversation_id
                or claim.get("request_id") != claim_request["request_id"]):
            raise ValueError("chat_claim_identity_invalid")
        if (claim.get("status") not in {"running", "succeeded", "failed", "cancelled"}
                or not isinstance(claim.get("task_run_id"), str) or not claim["task_run_id"]
                or type(claim.get("message_id")) not in (str, int)):
            raise ValueError("chat_claim_identity_invalid")
        if claim["status"] != "running":
            path.unlink()
            return
        # 在启动前持久禁止第二次启动；进程崩溃后的未知执行不能自动重开。
        claim_request["state"] = "accepted"
        _save_receipt(path, claim_request)
        cancellation = TaskCancellation()
        runtime = ContainerRuntimeOptions.build(
            image=self.config.container.image, network_mode=self.config.container.network_mode,
            dns=self.config.container.dns, cap_add=[], features={"conversation_chat": 1}, scenario_key="conversation-chat",
        )
        circuit_key = None
        try:
            if claim.get("claim_error"):
                raise ValueError("chat_claim_unavailable")
            if claim.get("session_machine_id") != self.dispatcher_id:
                raise ValueError("chat_claim_machine_mismatch")
            binding = TenantModelExecutionBinding.model_validate(claim["execution_binding"])
            worker = WorkerConfig(id="project-agent", engine="pi", model_id=binding.model_id,
                                  task_types=["agent_chat"], concurrency=1, priority=0,
                                  model_binding=_pi_model_binding(binding))
            circuit_key = self._binding_circuit_key(binding)
            future = self.agent_executor.submit(run_agent_turn, self.config, self.client, self.container_manager,
                                                worker, cancellation, claim, runtime)
        except (RuntimeError, ValueError, KeyError):
            future = Future()
            future.set_result({"status": "failed", "text": "", "error_code": "chat_claim_unavailable",
                               "usage": None, "session_initialized": None})
        self.futures[future] = RunningTask(
            None, "agent_chat", "project-agent", "pi", cancellation, runtime,
            task_run_id=claim["task_run_id"], binding_circuit_key=circuit_key,
            agent_conversation_id=conversation_id, agent_message_id=claim["message_id"],
        )

    def _dispatch_agent_turns(self) -> None:
        # 重启只重送已有终止证明的持久回执；不依据 Server running 或超时重建模型任务。
        scope = "agent-finish:load"
        try:
            self._flush_agent_finishes()
        except (OSError, ValueError) as exc:
            # 格式／身份错误不是磁盘故障；只记诊断变化，不输出带原文的异常。
            code = "chat_finish_storage_unavailable" if isinstance(exc, OSError) else "chat_finish_receipt_invalid"
            self._log_changed(scope, logging.ERROR, "agent turn finish unavailable code=%s", code)
            return
        self._clear_log_state(scope)
        if sum(task.task_type == "agent_chat" for task in self.futures.values()) >= AGENT_CAPACITY:
            return
        try:
            claim_paths = list(self._agent_claim_directory().glob("*.json"))
        except OSError:
            self._log_changed("agent-claim:load", logging.ERROR, "agent claim unavailable code=chat_claim_storage_unavailable")
            return
        self._clear_log_state("agent-claim:load")
        for path in claim_paths:
            if sum(task.task_type == "agent_chat" for task in self.futures.values()) >= AGENT_CAPACITY:
                return
            try:
                request = self._read_agent_claim(path)
            except (OSError, ValueError) as exc:
                code = "chat_claim_storage_unavailable" if isinstance(exc, OSError) else "chat_claim_receipt_invalid"
                self._log_changed("agent-claim:" + path.stem, logging.ERROR, "agent claim unavailable code=%s", code)
                continue
            if not any(receipt.conversation_id == request["conversation_id"] for receipt in self._pending_agent_finishes().values()):
                self._claim_agent_turn(request["conversation_id"])
        if sum(task.task_type == "agent_chat" for task in self.futures.values()) >= AGENT_CAPACITY:
            return
        response = self.client.pending_agent_turns(after=getattr(self, "_agent_pending_after", ""))
        if not response.ok or not isinstance(response.data, list):
            return
        for pending in response.data:
            if sum(task.task_type == "agent_chat" for task in self.futures.values()) >= AGENT_CAPACITY:
                break
            conversation_id = pending["conversation_id"]
            # 异机跳过也推进游标；只有原持有机器可以续聊。
            self._agent_pending_after = conversation_id
            if pending.get("session_machine_id") not in (None, self.dispatcher_id):
                continue
            if any(receipt.conversation_id == conversation_id for receipt in self._pending_agent_finishes().values()):
                continue
            if any(task.agent_conversation_id == conversation_id for task in self.futures.values()):
                continue
            self._claim_agent_turn(conversation_id)
        else:
            if len(response.data) < 100:
                self._agent_pending_after = ""

    def _reap_agent_turn(self, future, task) -> None:
        # future完成不等于Docker执行已终止；未知执行保留容量与会话占位，等待人工收口。
        if not task.cancellation.execution_lifecycle(task_finished=True)["termination_confirmed"]:
            if not task.agent_execution_unconfirmed:
                LOG.error("agent turn execution unconfirmed project=%s task_run=%s code=execution_unconfirmed",
                          task.agent_conversation_id, task.task_run_id)
                task.agent_execution_unconfirmed = True
            return
        scope = f"agent-finish:{task.task_run_id}"
        try:
            pending = self._pending_agent_finishes()
            key = sha256(f"{task.agent_conversation_id}\0{task.task_run_id}".encode()).hexdigest()
            receipt = pending.get(key)
            if receipt is None:
                try:
                    body = future.result()
                except Exception:
                    body = {"status": "failed", "text": "", "error_code": "chat_execution_failed", "usage": None}
                receipt = _PendingAgentFinish(
                    conversation_id=task.agent_conversation_id,
                    message_id=str(task.agent_message_id), task_run_id=task.task_run_id, result=body,
                )
                # 先落原结果与待处理身份，再尝试 finish 或释放已停止执行的容量。
                self._save_agent_finish(receipt)
                pending[key] = receipt
            self._send_agent_finish(receipt)
        except (OSError, ValueError) as exc:
            code = "chat_finish_storage_unavailable" if isinstance(exc, OSError) else "chat_finish_receipt_invalid"
            self._log_changed(scope, logging.ERROR, "agent turn finish unavailable project=%s task_run=%s code=%s",
                              task.agent_conversation_id, task.task_run_id, code)
            return
        self._clear_log_state(scope)
        if key not in pending or receipt.state != "pending" or receipt.attempts >= 3:
            # 后续重送由持久 receipt 接管；未接受的会话仍被隔离，无关会话可继续。
            self.futures.pop(future)
