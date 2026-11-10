"""正式任务的采集资源桥接；恢复只停止旧执行并继续上传材料。"""

from __future__ import annotations

import logging
from pathlib import Path
import threading

from docker.errors import DockerException
from requests.exceptions import RequestException

from cairn.dispatcher.runtime.traffic import ProjectTrafficRuntime, WORKSPACE
from cairn.dispatcher.runtime.traffic_sync import TrafficSync
from cairn.dispatcher.worker_result_gateway import WorkerRequestError

LOG = logging.getLogger(__name__)
PRIVATE_SESSION_ROOT = "/home/kali/.cairn/pi"
LEGACY_SESSION_ROOT = f"{WORKSPACE}/.cairn/pi"


def traffic_execution_enabled(project) -> bool:
    # 此属性是 Server 对 scenario_snapshot_json 的解析，不查询当前场景目录。
    features = project.scenario_snapshot.get("features", {})
    version = features.get("http_traffic_execution_version") if isinstance(features, dict) else None
    return type(version) is int and version == 1


def session_root_for_project(project) -> str:
    return PRIVATE_SESSION_ROOT if traffic_execution_enabled(project) else LEGACY_SESSION_ROOT


class TaskTrafficExecution:
    def __init__(self, runtime, scope_id: str, cancellation) -> None:
        self.runtime = runtime
        self.scope_id = scope_id
        self.cancellation = cancellation
        self.process = None
        self.failure: str | None = None
        self.health_error: str | None = None
        self.stopping = threading.Event()
        self._stop_lock = threading.Lock()
        self._cancel_lock = threading.Lock()
        self._cancel_thread: threading.Thread | None = None
        self._termination: dict = {}

    @property
    def execution_completed(self) -> bool:
        return self._termination.get("termination_confirmed") is True

    def cancel(self, reason: str) -> None:
        # Scheduler 广播不得同步等待 Docker；最终任务 finally 负责等待有界停止及核验。
        with self._cancel_lock:
            if self._cancel_thread is None:
                self._cancel_thread = threading.Thread(
                    target=self.stop, args=(reason,), name="traffic-stop", daemon=True,
                )
                self._cancel_thread.start()

    def stop(self, reason: str) -> dict:
        if self.cancellation.detached:
            return dict(self._termination)
        self.stopping.set()
        with self._stop_lock:
            if not self.execution_completed:
                try:
                    self._termination = self.runtime.stop_scope(self.scope_id, reason)
                except (DockerException, RequestException, OSError, RuntimeError):
                    self.failure = "traffic_termination_unconfirmed"
                    LOG.warning("traffic stop unconfirmed scope=%s", self.scope_id)
            return dict(self._termination)


class TrafficExecutionBridge:
    def __init__(
        self, docker_client, client, *, root: Path, dispatcher_id: str, config,
        isolated: bool = False, upstream_ca: Path | None = None,
        research_clock=None, research_stopped=None, instance_id: str | None = None,
    ) -> None:
        self.docker_client = docker_client
        self.client = client
        self.root = root
        self.dispatcher_id = dispatcher_id
        self.instance_id = instance_id
        self.config = config
        self.isolated = isolated
        self.upstream_ca = upstream_ca
        self._research_clock = research_clock
        self._research_stopped = research_stopped
        self._lock = threading.RLock()
        self._runtimes: dict[str, ProjectTrafficRuntime] = {}
        self._conversation_runtimes: dict[str, ProjectTrafficRuntime] = {}
        self._syncs: dict[str, TrafficSync] = {}
        self._tasks: dict[tuple[str, str], TaskTrafficExecution] = {}
        self._stop = threading.Event()
        self._recovered = False
        self._threads: list[threading.Thread] = []
        self._reconciled_seals: dict[str, float] = {}

    def _runtime(self, project_id: str, *, isolated: bool | None = None):
        with self._lock:
            runtime = self._runtimes.get(project_id)
            if runtime is None:
                if not project_id or Path(project_id).name != project_id or project_id in {".", ".."}:
                    raise ValueError("traffic_project_identity_invalid")
                runtime = ProjectTrafficRuntime(
                    self.docker_client, project_id=project_id, root=self.root / project_id,
                    gateway_image=self.config.traffic_gateway_image,
                    executor_image=self.config.image,
                    isolated=bool(isolated) or self.isolated,
                    dispatcher_id=self.dispatcher_id,
                    pause_clock=self._research_clock(project_id) if self._research_clock is not None else None,
                    cancelled=(lambda: self._research_stopped(project_id)) if self._research_stopped is not None else None,
                )
                self._runtimes[project_id] = runtime
            if isolated is not None and runtime.isolated != (isolated or self.isolated):
                raise RuntimeError("traffic_network_policy_mismatch")
            return runtime

    @staticmethod
    def _runtime_key(runtime):
        return runtime.traffic_owner if runtime.conversation_id is not None else runtime.project_id

    def conversation_runtime(self, conversation_id: str):
        with self._lock:
            if not conversation_id or Path(conversation_id).name != conversation_id or conversation_id in {".", ".."}:
                raise ValueError("traffic_conversation_identity_invalid")
            runtime = self._conversation_runtimes.get(conversation_id)
            if runtime is None:
                directory = self.root.with_name(self.root.name + "-conversations") / conversation_id
                runtime = ProjectTrafficRuntime(
                    self.docker_client, project_id=None, conversation_id=conversation_id, root=directory / "research",
                    workspace=directory / "workspace", gateway_image=self.config.traffic_gateway_image,
                    executor_image=self.config.image, dispatcher_id=self.dispatcher_id, isolated=self.isolated,
                    instance_id=self.instance_id,
                )
                self._conversation_runtimes[conversation_id] = runtime
            return runtime

    def prepare_conversation(self, conversation_id, task_run_id, cancellation, *, test_egress):
        runtime = self.conversation_runtime(conversation_id)
        key = (runtime.traffic_owner, task_run_id)
        with self._lock:
            if key in self._tasks:
                execution = self._tasks[key]
                if cancellation.is_cancelled or execution.stopping.is_set() or execution.failure:
                    raise RuntimeError("traffic_task_stopped")
                return execution
        if cancellation.is_cancelled:
            raise RuntimeError("traffic_task_cancelled")
        runtime.prepare(upstream_ca=self.upstream_ca)
        execution = None

        def claim(scope_id):
            nonlocal execution
            execution = TaskTrafficExecution(runtime, scope_id, cancellation)
            cancellation.register_resource(execution)

        def register(binding):
            if not self.client.register_traffic_scope(runtime.traffic_owner, binding).ok:
                raise RuntimeError("traffic_scope_registration_failed")

        try:
            runtime.prepare_scope(task_run_id, cancellation.generation, test_egress=test_egress, claim=claim, register=register)
            # 取消资源须先登记，但健康检查只能看到准备完整的 scope，避免读到尚未创建的 collector。
            with self._lock:
                self._tasks[key] = execution
            self._start_sync(runtime)
            if cancellation.is_cancelled or runtime.check_scope(execution.scope_id):
                raise RuntimeError("traffic_task_cancelled")
            return execution
        except BaseException:
            if execution is not None:
                execution.stop("traffic_scope_preparation_failed")
            raise

    def _start_sync(self, runtime) -> None:
        with self._lock:
            key = self._runtime_key(runtime)
            if key not in self._syncs:
                sync = TrafficSync(
                    self.client, capture=runtime.capture, receipts=runtime.root / "receipts",
                )
                sync.start()
                self._syncs[key] = sync
            if not self._threads:
                for target, name in ((self._watch, "traffic-health"), (self._publish, "traffic-status")):
                    thread = threading.Thread(target=target, name=name, daemon=True)
                    self._threads.append(thread)
                    thread.start()

    def workspace_directory(self, project_id: str, *, isolated: bool) -> Path:
        return self._runtime(project_id, isolated=isolated).workspace

    def control_volumes(self, project_id: str, *, isolated: bool) -> dict:
        runtime = self._runtime(project_id, isolated=isolated)
        runtime.prepare(upstream_ca=self.upstream_ca)
        self._start_sync(runtime)
        return {
            str(runtime.workspace): {"bind": WORKSPACE, "mode": "rw"},
            str(runtime.control): {"bind": "/home/kali/.cairn", "mode": "rw"},
            str(runtime.control_prompts): {"bind": "/tmp/cairn-prompts", "mode": "rw"},
            str(runtime.execution): {"bind": "/run/cairn-exec", "mode": "ro"},
        }

    def prepare_task(self, project, task_run_id: str, cancellation, *, isolated: bool = False, image: str | None = None) -> dict:
        if not traffic_execution_enabled(project):
            raise WorkerRequestError(
                "capabilities.task_execution", code="http_traffic_execution_required",
                message="旧项目不能继续目标执行；已有材料仍可读取和交付，请新建项目。",
            )
        if cancellation.is_cancelled:
            raise RuntimeError("traffic_task_cancelled")
        runtime = self._runtime(project.id, isolated=isolated)
        key = (project.id, task_run_id)
        with self._lock:
            if key in self._tasks:
                raise RuntimeError("traffic_task_execution_already_prepared")
        # 每次创建 scope 读取权威出口，不能由项目 runtime 缓存冻结首次设置。
        test_egress = self.client.resolve_task_test_egress(project.id, task_run_id)
        runtime.prepare(upstream_ca=self.upstream_ca)
        execution = None

        def claim(scope_id):
            nonlocal execution
            execution = TaskTrafficExecution(runtime, scope_id, cancellation)
            cancellation.register_resource(execution)

        def register(binding):
            result = self.client.register_traffic_scope(project.id, binding)
            if not result.ok:
                raise RuntimeError("traffic_scope_registration_failed")

        try:
            # 取消绑定先于慢创建，Server scope 归属先于执行者启动。
            scope = runtime.prepare_scope(
                task_run_id, cancellation.generation, test_egress=test_egress, executor_image=image,
                claim=claim, register=register,
            )
            with self._lock:
                self._tasks[key] = execution
            self._start_sync(runtime)
            error = runtime.check_scope(scope.id)
            if error or cancellation.is_cancelled:
                raise RuntimeError(error or "traffic_task_cancelled")
        except BaseException:
            if execution is not None:
                execution.stop("traffic_scope_preparation_failed")
            raise
        return scope.capability()

    def task(self, project_id: str | None, task_run_id: str | None):
        with self._lock:
            return self._tasks.get((project_id, task_run_id))

    def stop_task(self, project_id: str, task_run_id: str, reason: str = "task_finished") -> None:
        execution = self.task(project_id, task_run_id)
        if execution is not None:
            execution.stop(reason)

    def recover(self) -> None:
        if self._recovered:
            return
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in sorted(self.root.glob("*/manifest.json")):
            runtime = self._runtime(path.parent.name)
            # 不调用 prepare：恢复不能创建网关或新 scope，更不能重发目标请求。
            runtime.recover()
            self._start_sync(runtime)
            if any(
                item.get("termination_confirmed") is not True and not item.get("hot_paused")
                for item in runtime.manifest.get("scopes", {}).values()
            ):
                raise RuntimeError("traffic_recovery_termination_unconfirmed")
        conversations = self.root.with_name(self.root.name + "-conversations")
        for path in sorted(conversations.glob("*/research/manifest.json")):
            runtime = self.conversation_runtime(path.parent.parent.name)
            runtime.recover()
            self._start_sync(runtime)
            if any(item.get("termination_confirmed") is not True for item in runtime.manifest.get("scopes", {}).values()):
                raise RuntimeError("traffic_recovery_termination_unconfirmed")
        self._recovered = True

    def _watch(self) -> None:
        while not self._stop.wait(0.5):
            with self._lock:
                tasks = tuple(self._tasks.values())
            for execution in tasks:
                if execution.stopping.is_set():
                    continue
                try:
                    code = execution.runtime.check_scope(execution.scope_id)
                except (DockerException, RequestException, OSError):
                    # 查询失败不是容器退出。保留执行并公开观测未知，下一轮恢复查询；
                    # 用户停止、任务截止和所有者断连都不依赖这次查询成功。
                    if execution.health_error is None:
                        LOG.warning("traffic health observation unavailable scope=%s", execution.scope_id)
                    execution.health_error = "traffic_health_unavailable"
                    continue
                except RuntimeError:
                    # 资源归属核验等确定性错误不能按暂时的观测故障放行。
                    code = "traffic_health_unavailable"
                execution.health_error = None
                if code and not execution.stopping.is_set():
                    execution.failure = code
                    execution.cancellation.cancel(code)

    def _publish(self) -> None:
        while not self._stop.wait(1.0):
            with self._lock:
                runtimes = (*self._runtimes.values(), *self._conversation_runtimes.values())
                syncs = dict(self._syncs)
                tasks = {execution.scope_id: execution for execution in self._tasks.values()}
            for runtime in runtimes:
                sync = syncs.get(self._runtime_key(runtime))
                if sync is None:
                    continue
                sync_status = sync.status()
                uploads = sync_status.get("scopes", {})
                for scope_id, facts in tuple(runtime.manifest.get("scopes", {}).items()):
                    execution = tasks.get(scope_id)
                    upload = uploads.get(scope_id, {})
                    sealed_at = facts.get("sealed_at")
                    if (type(sealed_at) in (int, float)
                            and self._reconciled_seals.get(scope_id) != sealed_at):
                        sync.request_reconcile(scope_id)
                        self._reconciled_seals[scope_id] = sealed_at
                        upload = {}
                    payload = self._scope_status(facts, execution, upload, sync_status)
                    try:
                        self.client.update_traffic_scope_status(runtime.traffic_owner, scope_id, payload)
                    except RequestException:
                        # 状态发布失败不阻断健康检查或测试停止；私有 manifest/spool 继续保留。
                        LOG.warning("traffic status publication deferred scope=%s", scope_id)

    @staticmethod
    def _scope_status(facts: dict, execution, upload: dict, sync_status: dict) -> dict:
        collector_stopped = facts.get("collector_stopped")
        failure = execution.failure if execution is not None else None
        failure = facts.get("failure_code") or failure or (facts.get("code") if str(facts.get("code", "")).startswith("traffic_") else None)
        process = execution.process if execution is not None else None
        stopped = facts.get("termination_confirmed") is True
        stopping = facts.get("entry_closed") or (execution is not None and execution.stopping.is_set())
        state = "stopped" if stopped else "stopping" if stopping else "running"
        sealed_at, scan_started_at = facts.get("sealed_at"), upload.get("scan_started_at")
        final_scan = (type(sealed_at) in (int, float) and type(scan_started_at) in (int, float)
                      and scan_started_at >= sealed_at)
        upload_complete = upload.get("upload_complete") if collector_stopped is True and final_scan else False
        code = None
        if sync_status.get("code") and sync_status.get("running") is False and upload_complete is not True:
            # 全局上传者已退出，旧 scope 快照不能继续声称正在同步或重试。
            state, code = "blocked", sync_status["code"]
        elif upload.get("state") in {"blocked", "retrying"}:
            state, code = upload["state"], upload.get("code")
        elif sync_status.get("code") and not upload:
            state, code = "retrying", sync_status["code"]
        elif failure and not stopped:
            state, code = ("stopping" if stopping else "blocked"), failure
        elif not stopped and not stopping and execution is not None and execution.health_error:
            state, code = "retrying", execution.health_error
        return {
            "state": state, "code": code, "failure_code": failure,
            # Server 读取时以已接受的同 generation lifecycle 证明提升此观测。
            "control_stopped": process.execution_completed if process is not None else None,
            "test_stopped": facts.get("test_stopped"), "collector_stopped": collector_stopped,
            "upload_complete": upload_complete,
            "pending": upload.get("pending"), "pending_bytes": upload.get("pending_bytes"),
        }

    def hot_control(self, project_id: str, paused: bool) -> list[dict[str, str]]:
        with self._lock:
            runtime = self._runtimes.get(project_id)
        return runtime.hot_control(paused) if runtime is not None else []

    def hot_resume_completed(self, project_id: str) -> None:
        with self._lock:
            runtime = self._runtimes.get(project_id)
        if runtime is not None:
            runtime.hot_resume_completed()

    def stopped_resources(self, project_id: str) -> list[dict[str, str]]:
        with self._lock:
            runtime = self._runtimes.get(project_id)
        if runtime is None:
            return []
        # close 已逐个核验完整 scope；不依赖异步状态上传赶在终止回执之前完成。
        return [{"kind": "test", "container_id": state["container_id"], "scope_id": scope_id,
                 "task_run_id": state["task_run_id"], "state": "stopped"}
                for scope_id, state in runtime.manifest.get("scopes", {}).items()
                if state.get("termination_confirmed") is True and state.get("container_id")]

    def retained_projects(self) -> set[str]:
        with self._lock:
            return set(self._runtimes)

    def destroy_project(self, project_id: str) -> bool:
        import shutil

        with self._lock:
            runtime = self._runtimes.get(project_id)
            sync = self._syncs.get(project_id)
        if runtime is not None:
            runtime.destroy()
            if sync is not None:
                sync.stop(timeout=2.0)
            # 目录身份来自已校验的 manifest，而非请求传入任意路径。
            if runtime.root.is_symlink() or runtime.root.parent != self.root.resolve():
                raise RuntimeError("traffic_project_identity_invalid")
            shutil.rmtree(runtime.root)
            with self._lock:
                self._runtimes.pop(project_id, None)
                self._syncs.pop(project_id, None)
                self._tasks = {key: value for key, value in self._tasks.items() if key[0] != project_id}
        return True

    def pending_cleanup_projects(self) -> set[str]:
        with self._lock:
            return {
                project_id for project_id, runtime in self._runtimes.items()
                if runtime.manifest.get("prepared") is True or any(
                    scope.get("termination_confirmed") is not True
                    for scope in runtime.manifest.get("scopes", {}).values()
                )
            }

    def cleanup_project(self, project_id: str) -> bool:
        with self._lock:
            runtime = self._runtimes.get(project_id)
        if runtime is None:
            return True
        runtime.close()
        return all(
            item.get("termination_confirmed") is True
            for item in runtime.manifest.get("scopes", {}).values()
        )

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            runtimes = (*self._runtimes.values(), *self._conversation_runtimes.values())
            syncs = tuple(self._syncs.values())
        for runtime in runtimes:
            if not any(scope.get("hot_paused") for scope in runtime.manifest.get("scopes", {}).values()):
                runtime.close()
        for sync in syncs:
            sync.stop(timeout=2.0)
        for thread in self._threads:
            thread.join(timeout=2.0)
