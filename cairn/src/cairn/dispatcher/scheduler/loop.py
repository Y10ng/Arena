"""Dispatcher 主调度循环，协调项目状态、worker 选择、容器和任务生命周期。"""

from __future__ import annotations

from concurrent.futures import wait

from cairn.dispatcher.runtime.browser_sessions import BrowserSession
from cairn.dispatcher.runtime.containers import CandidateExecutionCheck, canonical_server_address
from cairn.dispatcher.tasks.candidate_capture_outbox import CandidateCaptureOutbox
from cairn.dispatcher.tasks.report_finish_outbox import ReportFinishOutbox

from .candidate_recovery import CandidateRecoveryMixin
from .cleanup import CleanupMixin
from .common import *
from .config_runtime import ConfigRuntimeMixin
from .dispatch_cycle import DispatchCycleMixin
from .heartbeat_reporter import HeartbeatReporterMixin
from .report_finish_outbox import ReportFinishOutboxMixin
from .runtime_context import RuntimeContextMixin
from .stale_reconcile import StaleReconcileMixin
from .task_lifecycle import TaskLifecycleMixin
from .worker_runtime import WorkerRuntimeMixin


class DispatcherLoop(
    ConfigRuntimeMixin,
    CandidateRecoveryMixin,
    StaleReconcileMixin,
    DispatchCycleMixin,
    TaskLifecycleMixin,
    ReportFinishOutboxMixin,
    RuntimeContextMixin,
    WorkerRuntimeMixin,
    HeartbeatReporterMixin,
    CleanupMixin,
):
    """长驻调度器，按固定 interval 拉取 Server 状态并提交可执行任务。"""

    def __init__(
        self,
        bootstrap: DispatcherBootstrap,
        *,
        client: CairnClient | None = None,
        config: DispatchConfig | None = None,
    ):
        self.bootstrap = bootstrap
        self._config_path = bootstrap.config_path.expanduser().resolve()
        self._config_mtime_ns: int | None = None
        self._config_sha256: str | None = None
        self._config_error_message: str | None = None
        if config is None:
            raw = self._config_path.read_bytes()
            stat = self._config_path.stat()
            config = DispatchConfig.load(self._config_path)
            self._config_mtime_ns = stat.st_mtime_ns
            self._config_sha256 = sha256(raw).hexdigest()
        else:
            try:
                raw = self._config_path.read_bytes()
                stat = self._config_path.stat()
                self._config_mtime_ns = stat.st_mtime_ns
                self._config_sha256 = sha256(raw).hexdigest()
            except OSError:
                self._config_sha256 = self._hash_config(config)
        self.client = client or CairnClient(config.server)
        self.config = config
        outbox_parent = self._config_path.parent / "data"
        # config.server 是 _apply_config 最终选择 HTTP client 的权威地址。
        raw_server_address = config.server.rstrip("/")
        canonical_address = canonical_server_address(raw_server_address)
        server_namespace = sha256(
            canonical_address.encode("utf-8")
        ).hexdigest()
        self.report_finish_outbox = ReportFinishOutbox(
            outbox_parent
            / f"dispatcher-report-finish-outbox-v1-{server_namespace}"
        )
        self._candidate_outbox_parent = outbox_parent
        self._candidate_server_namespace = server_namespace
        # durable owner 必须来自认证心跳，不能提前信任配置中的显示身份。
        self.candidate_capture_outbox: CandidateCaptureOutbox | None = None
        legacy_namespace = sha256(
            raw_server_address.encode("utf-8")
        ).hexdigest()
        legacy_root = (
            outbox_parent
            / f"dispatcher-report-finish-outbox-v1-{legacy_namespace}"
        )
        self._legacy_report_finish_outbox = (
            ReportFinishOutbox(legacy_root)
            if legacy_root != self.report_finish_outbox.root
            else None
        )
        self._legacy_report_finish_cutover = None
        # 首次认证心跳前没有执行 namespace，禁止提前接触容器或启动自检。
        self.container_manager = None
        self.executor = ThreadPoolExecutor(max_workers=max(1, bootstrap.hard_cap))
        self.cleanup_executor = ThreadPoolExecutor(max_workers=8)
        # futures 只保存当前进程提交的任务；Server 中旧 running 由启动 reconcile 处理。
        self.futures: dict[Future[str], RunningTask] = {}
        self.cleanup_futures: dict[Future[bool], tuple[str, str | None, str | tuple[str, int] | None]] = {}
        self.browser_cleanup_futures: dict[Future[str], BrowserSession] = {}
        self.reason_checkpoints: dict[str, ReasonCheckpoint] = {}
        # runtime_project_ids 表示当前 dispatcher 认为仍需要保留运行容器的 active 项目。
        self.runtime_project_ids: set[str] = set()
        self.worker_cooldown_until: dict[str, tuple[float, str]] = {}
        self.worker_rejected_until: dict[tuple[str, str, str], tuple[float, str]] = {}
        self.worker_cooldown_attempts: dict[tuple[str, str], int] = {}
        self.worker_rejected_attempts: dict[tuple[str, str, str, str], int] = {}
        self.binding_auth_circuit_until: dict[BindingCircuitKey, float] = {}
        self.binding_auth_circuit_attempts: dict[BindingCircuitKey, int] = {}
        self.pending_task_run_finishes: dict[tuple[str, str], PendingTaskRunFinish] = {}
        self._log_state: dict[str, tuple[int, str, tuple[object, ...]]] = {}
        self._cleanup_pending: set[str] = set()
        self._inactive_cleanup_done: dict[str, str | tuple[str, int]] = {}
        self._runtime_probe_cache: dict[tuple[str, tuple[str, ...]], str] = {}
        self.project_cursor = 0
        self._report_finish_outbox_cursor = 0
        self._settings_checked = False
        self._startup_healthchecks_checked = False
        self._startup_reconcile_checked = False
        self._candidate_recovery_cursor = 0
        self._candidate_stop_futures: dict[str, Future[CandidateExecutionCheck]] = {}
        self._candidate_fence_invalid = False
        self._candidate_fences = ()
        self._candidate_fenced_projects: set[str] = set()
        self._candidate_fenced_task_runs: set[tuple[str, str]] = set()
        self._candidate_fenced_containers: set[str] = set()
        self.dispatcher_id: str | None = None
        self.started_at = _utcnow()
        self._last_config_hash = self._config_sha256 or self._hash_config(config)
        self._apply_config(config, force=True)
        if self._legacy_report_finish_outbox is not None:
            cutover = self._legacy_report_finish_outbox.cutover_to(
                self.report_finish_outbox
            )
            migrated = cutover.__enter__()
            self._legacy_report_finish_cutover = cutover
            if migrated:
                LOG.info(
                    "migrated report finish outbox to canonical server namespace entries=%s",
                    migrated,
                )

    def close(self) -> None:
        try:
            if self.futures:
                LOG.info(
                    "dispatcher shutting down waiting_for_tasks=%s running_projects=%s",
                    len(self.futures),
                    sorted({task.project_id for task in self.futures.values()}),
                )
            self.executor.shutdown(wait=True)
            self._reap_futures()
            try:
                # Docker 与 HTTP client 关闭前只做一次有界 candidate 恢复/重放。
                self._refresh_candidate_recovery()
            except (OSError, RuntimeError) as exc:
                LOG.warning(
                    "dispatcher shutdown candidate recovery deferred error=%s",
                    type(exc).__name__,
                )
            # 退出时分批等待现有收尾；每项最多尝试一次，失败仍保留 pending，不无限重试。
            attempted = set(getattr(self, "_task_run_finish_futures", {}))
            while True:
                self._flush_pending_task_run_finishes(attempted=attempted)
                inflight = self._task_run_finish_futures
                if not inflight:
                    break
                attempted.update(inflight)
                wait([future for future, _finish in inflight.values()])
            if self.pending_task_run_finishes:
                LOG.warning(
                    "dispatcher shutdown pending_task_run_finishes=%s",
                    len(self.pending_task_run_finishes),
                )
            try:
                # 退出前只做一次有界重放；失败 entry 保留给下次进程启动继续处理。
                self._flush_pending_report_finishes()
                remaining_report_finishes = self.report_finish_outbox.entries()
            except Exception:
                # outbox 内容可能来自损坏文件，日志只暴露稳定错误码。
                LOG.error(
                    "dispatcher shutdown report finish outbox unavailable code=report_finish_outbox_invalid"
                )
            else:
                if remaining_report_finishes:
                    LOG.warning(
                        "dispatcher shutdown pending_report_finishes=%s",
                        len(remaining_report_finishes),
                    )
            self.cleanup_executor.shutdown(wait=True)
            if self.container_manager is not None:
                self.container_manager.close()
            self.client.close()
        finally:
            cutover = getattr(self, "_legacy_report_finish_cutover", None)
            if cutover is not None:
                self._legacy_report_finish_cutover = None
                cutover.__exit__(None, None, None)

    def run(self, once: bool = False) -> None:
        try:
            self._ensure_config_loaded(once=once)
            self.run_startup_healthchecks()
            while True:
                loop_started_at = _utcnow()
                loop_started = time.perf_counter()
                summaries: list[ProjectSummary] = []
                self._begin_project_decisions([])
                # 每轮派发前核验认证主体；身份变化不能落入普通监控容错继续派发。
                self._record_dispatcher_heartbeat(
                    status="running" if self.futures else "idle",
                    loop_started_at=loop_started_at, loop_started=loop_started,
                    summaries=[], loop_completed=False,
                )
                try:
                    # 控制观察先于配置刷新、收尾和版本探针；取消广播本身不做 Docker I/O。
                    summaries = self.client.list_projects()
                    self._cancel_inactive_tasks(summaries)
                    self._refresh_config()
                    if not self._startup_healthchecks_checked:
                        # 热加载后的 runner/image 证据必须在任何新派发前重新建立。
                        self.run_startup_healthchecks()
                    if not self._settings_checked:
                        self._validate_server_settings()
                        self._settings_checked = True
                    self._reap_futures()
                    # candidate fence 必须先于 stale reconcile、任何新派发与破坏性清理恢复。
                    candidate_recovery_complete = self._refresh_candidate_recovery()
                    # durable report finish 必须先于 stale reconcile 和新 report claim 重放。
                    self._flush_pending_report_finishes()
                    self._flush_pending_task_run_finishes()
                    self._reap_cleanup_futures()
                    if not self._startup_reconcile_checked:
                        # unresolved candidate task_run 先跳过，fence 释放后再完成启动收口。
                        self._reconcile_stale_running_task_runs(summaries)
                        self._startup_reconcile_checked = candidate_recovery_complete
                        summaries = self.client.list_projects()
                    self._initialize_reason_checkpoints(summaries)
                    self._refresh_runtime_projects(summaries)
                    self._cancel_inactive_tasks(summaries)
                    # 先 claim durable report，再决定 completed 容器是否可清理。
                    self._dispatch_available(summaries)
                    self._queue_container_cleanups(summaries)
                except requests.RequestException as exc:
                    self._record_dispatcher_heartbeat(
                        status="error",
                        loop_started_at=loop_started_at,
                        loop_started=loop_started,
                        summaries=summaries,
                        error_message="server_unreachable",
                        loop_completed=False,
                    )
                    if once:
                        raise
                    interval = self._sleep_interval()
                    LOG.warning(
                        "dispatcher server request failed error=%s retry_in=%ss",
                        exc,
                        interval,
                    )
                    time.sleep(interval)
                    continue
                except Exception:
                    self._record_dispatcher_heartbeat(
                        status="error", loop_started_at=loop_started_at,
                        loop_started=loop_started, summaries=summaries,
                        error_message="dispatcher_error", loop_completed=False,
                    )
                    raise
                self._record_dispatcher_heartbeat(
                    status="running" if self.futures else "idle",
                    loop_started_at=loop_started_at,
                    loop_started=loop_started,
                    summaries=summaries,
                )
                if once:
                    break
                time.sleep(self._sleep_interval())
        finally:
            self.close()

    def run_startup_healthchecks_only(self) -> None:
        try:
            self._ensure_config_loaded(once=True)
            self.run_startup_healthchecks(show_commands=True)
        finally:
            self.close()

    def run_startup_healthchecks(self, *, show_commands: bool = False) -> None:
        if self._startup_healthchecks_checked:
            return
        if self.config is None:
            self._ensure_config_loaded(once=True)
        started_at, started = _utcnow(), time.perf_counter()
        self._startup_check = {"status": "starting", "observed_at": started_at}
        self._record_dispatcher_heartbeat(
            status="starting", loop_started_at=started_at, loop_started=started,
            summaries=[], loop_completed=False,
        )
        self._apply_config(self.config, force=False)
        try:
            # 自检在有界任务线程内执行，主线程持续报告真实 starting，而非伪造 loop 完成。
            with ThreadPoolExecutor(max_workers=1) as startup_executor:
                check = startup_executor.submit(self._run_startup_healthchecks, show_commands=show_commands)
                while not wait([check], timeout=min(5, self._sleep_interval())).done:
                    self._record_dispatcher_heartbeat(
                        status="starting", loop_started_at=started_at, loop_started=started,
                        summaries=[], loop_completed=False,
                    )
                check.result()
        except Exception:
            self._startup_check = {
                "status": "failed", "code": "startup_healthcheck_failed", "observed_at": _utcnow(),
            }
            self._record_dispatcher_heartbeat(
                status="error", loop_started_at=started_at, loop_started=started,
                summaries=[], error_message="startup_healthcheck_failed", loop_completed=False,
            )
            raise
        self._startup_healthchecks_checked = True
        self._startup_check = {"status": "ready", "observed_at": _utcnow()}
        self._record_dispatcher_heartbeat(
            status="idle", loop_started_at=started_at, loop_started=started,
            summaries=[], loop_completed=False,
        )
