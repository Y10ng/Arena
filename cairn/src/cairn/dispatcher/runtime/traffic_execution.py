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
    def __init__(self, runtime, scope, cancellation) -> None:
        self.runtime = runtime
        self.scope = scope
        self.cancellation = cancellation
        self.process = None
        self.failure: str | None = None
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
        self.stopping.set()
        with self._stop_lock:
            if not self.execution_completed:
                try:
                    self._termination = self.runtime.stop_scope(self.scope.id, reason)
                except (DockerException, RequestException, OSError, RuntimeError):
                    self.failure = "traffic_termination_unconfirmed"
                    LOG.warning("traffic stop unconfirmed scope=%s", self.scope.id)
            return dict(self._termination)


class TrafficExecutionBridge:
    def __init__(
        self, docker_client, client, *, root: Path, dispatcher_id: str, config,
        isolated: bool = False, upstream_ca: Path | None = None,
    ) -> None:
        self.docker_client = docker_client
        self.client = client
        self.root = root
        self.dispatcher_id = dispatcher_id
        self.config = config
        self.isolated = isolated
        self.upstream_ca = upstream_ca
        self._lock = threading.RLock()
        self._runtimes: dict[str, ProjectTrafficRuntime] = {}
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
                    executor_image=self.config.task_executor_image, isolated=bool(isolated) or self.isolated,
                    dispatcher_id=self.dispatcher_id,
                )
                self._runtimes[project_id] = runtime
            if isolated is not None and runtime.isolated != (isolated or self.isolated):
                raise RuntimeError("traffic_network_policy_mismatch")
            return runtime

    def _start_sync(self, runtime) -> None:
        with self._lock:
            if runtime.project_id not in self._syncs:
                sync = TrafficSync(
                    self.client, capture=runtime.capture, receipts=runtime.root / "receipts",
                )
                sync.start()
                self._syncs[runtime.project_id] = sync
            if not self._threads:
                for target, name in ((self._watch, "traffic-health"), (self._publish, "traffic-status")):
                    thread = threading.Thread(target=target, name=name, daemon=True)
                    self._threads.append(thread)
                    thread.start()

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

    def prepare_task(self, project, task_run_id: str, cancellation, *, isolated: bool = False) -> dict:
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
        scope = runtime.prepare_scope(task_run_id, cancellation.generation, test_egress=test_egress)
        execution = TaskTrafficExecution(runtime, scope, cancellation)
        with self._lock:
            self._tasks[key] = execution
        cancellation.register_resource(execution)
        self._start_sync(runtime)
        binding = {
            "id": scope.id, "project_id": scope.project_id,
            "task_run_id": scope.task_run_id, "generation": scope.generation,
            "container_id": scope.container_id,
            "test_egress": runtime.manifest['scopes'][scope.id]['test_egress'],
        }
        try:
            result = self.client.register_traffic_scope(project.id, binding)
            if not result.ok:
                raise RuntimeError("traffic_scope_registration_failed")
            error = runtime.check_scope(scope.id)
            if error or cancellation.is_cancelled:
                raise RuntimeError(error or "traffic_task_cancelled")
        except BaseException:
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
                item.get("termination_confirmed") is not True
                for item in runtime.manifest.get("scopes", {}).values()
            ):
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
                    code = execution.runtime.check_scope(execution.scope.id)
                except (DockerException, RequestException, OSError, RuntimeError):
                    code = "traffic_health_unavailable"
                if code and not execution.stopping.is_set():
                    execution.failure = code
                    execution.cancellation.cancel(code)

    def _publish(self) -> None:
        while not self._stop.wait(1.0):
            with self._lock:
                runtimes = tuple(self._runtimes.values())
                syncs = dict(self._syncs)
                tasks = {execution.scope.id: execution for execution in self._tasks.values()}
            for runtime in runtimes:
                sync = syncs.get(runtime.project_id)
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
                        self.client.update_traffic_scope_status(runtime.project_id, scope_id, payload)
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
        return {
            "state": state, "code": code, "failure_code": failure,
            # Server 读取时以已接受的同 generation lifecycle 证明提升此观测。
            "control_stopped": process.execution_completed if process is not None else None,
            "test_stopped": facts.get("test_stopped"), "collector_stopped": collector_stopped,
            "upload_complete": upload_complete,
            "pending": upload.get("pending"), "pending_bytes": upload.get("pending_bytes"),
        }

    def pending_cleanup_projects(self) -> set[str]:
        with self._lock:
            return {
                project_id for project_id, runtime in self._runtimes.items()
                if any(not resource.get("removed") for resource in runtime.manifest.get("resources", {}).values())
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
            runtimes = tuple(self._runtimes.values())
            syncs = tuple(self._syncs.values())
        for runtime in runtimes:
            runtime.close()
        for sync in syncs:
            sync.stop(timeout=2.0)
        for thread in self._threads:
            thread.join(timeout=2.0)
