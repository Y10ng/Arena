"""Worker 进程和健康检查执行器。"""

from __future__ import annotations

from .common_base import *
from dataclasses import replace
from cairn.dispatcher.runtime.process import ManagedProcess

from .candidate_capture_outbox import (
    CandidateCaptureOutbox,
    CandidateCaptureOutboxError,
    CandidateExecutionFence,
)
from .candidate_intake import CandidateIntakeSummary, intake_worker_candidates
from .checkpoint_intake import intake_worker_checkpoints
from .command_events import record_command_finished, record_command_started
from .hook_records import record_pi_tool_events
from cairn.dispatcher.worker_result_gateway import WorkerResultFailure, decode_pi_result, worker_failure_error_type
from .validation_records import record_validation_result
from .payload_records import (
    payload_recorder_env,
    prepare_payload_recorder,
    record_payload_operations,
)
from cairn.dispatcher.workers.base import (
    DriverResult,
    REPORT_READONLY_INPUT_ROOT,
    WorkerInputFile,
)


def _oom_kill_count(container_manager: ContainerManager, container_name: str) -> int | None:
    reader = getattr(container_manager, "oom_kill_count", None)
    return reader(container_name) if callable(reader) else None


def _apply_oom_evidence(
    container_manager: ContainerManager,
    container_name: str,
    result: ProcessResult,
    *,
    before_count: int | None,
) -> None:
    # cgroup 计数是容器级信号；只有当前 exec 自身以 SIGKILL/137 退出时才归因，
    # 避免同容器并发命令观察到别的进程 OOM 后被误标。
    if result.returncode != 137:
        return
    detector = getattr(container_manager, "detect_oom_kill", None)
    if not callable(detector):
        return
    oom_killed, source = detector(container_name, before_count=before_count)
    result.oom_killed = oom_killed
    result.oom_evidence_source = source


def _keep_browser_task_unmanaged(
    container_manager: ContainerManager, project_id: str | None, task_run_id: str | None,
) -> None:
    if project_id is None or task_run_id is None:
        return
    try:
        store = getattr(container_manager, "browser_sessions", None)
        if store is not None:
            store.keep_unmanaged(project_id, task_run_id)
    except BaseException as exc:
        LOG.warning(
            "browser session fence failed project=%s task_run=%s error=%s",
            project_id, task_run_id, type(exc).__name__,
        )


def _worker_process_env(worker: WorkerConfig) -> dict[str, str]:
    return dict(worker.env)


def _execution_completed(process: object) -> bool:
    """读取物理终态证明；缺失或异常都按未决执行处理。"""
    try:
        return getattr(process, "execution_completed", False) is True
    except BaseException as exc:
        LOG.warning(
            "execution completion proof unavailable error=%s",
            type(exc).__name__,
        )
        return False


def _write_worker_input_files(
    container_manager: ContainerManager,
    container_name: str,
    files: tuple[WorkerInputFile, ...],
    *,
    tracked: list[WorkerInputFile] | None = None,
) -> list[WorkerInputFile]:
    written = tracked if tracked is not None else []
    prepared_directories = {
        item.readonly_directory
        for item in written
        if item.readonly_directory is not None
    }
    for item in files:
        # 先登记再执行特权物化；部分创建或 BaseException 也能由外层 finally 清理。
        written.append(item)
        if (
            item.readonly_directory is not None
            and item.readonly_directory not in prepared_directories
        ):
            prepared_directories.add(item.readonly_directory)
            container_manager.prepare_readonly_input_directory(
                container_name,
                item.readonly_directory,
            )
        if item.readonly_directory is not None:
            container_manager.write_text_file(
                container_name,
                item.path,
                item.content,
                owner=item.owner,
                mode=item.mode,
                archive_root=item.readonly_directory,
            )
        else:
            container_manager.write_text_file(
                container_name,
                item.path,
                item.content,
                owner=item.owner,
                mode=item.mode,
            )
    return written


def _remove_worker_input_files(
    container_manager: ContainerManager,
    container_name: str,
    files: list[WorkerInputFile],
    *,
    retained_paths: set[str] | None = None,
) -> None:
    retained = retained_paths or set()
    retained_readonly_directories = {
        item.readonly_directory
        for item in files
        if item.path in retained and item.readonly_directory is not None
    }
    readonly_files: dict[str, list[str]] = {}
    for item in files:
        if (
            item.readonly_directory is not None
            and item.readonly_directory not in retained_readonly_directories
        ):
            readonly_files.setdefault(item.readonly_directory, []).append(
                PurePosixPath(item.path).name
            )
    for item in reversed(files):
        if item.readonly_directory is not None or item.path in retained:
            continue
        try:
            container_manager.remove_file(
                container_name,
                item.path,
                allowed_root=item.allowed_root,
            )
        except BaseException as exc:
            LOG.warning(
                "worker input cleanup failed container=%s path=%s error=%s",
                container_name,
                item.path,
                type(exc).__name__,
            )
    for directory, filenames in reversed(tuple(readonly_files.items())):
        try:
            container_manager.remove_readonly_input_directory(
                container_name,
                directory,
                filenames=tuple(dict.fromkeys(filenames)),
            )
        except BaseException as exc:
            LOG.warning(
                "readonly worker input cleanup failed container=%s root=%s error=%s",
                container_name,
                REPORT_READONLY_INPUT_ROOT,
                type(exc).__name__,
            )


def write_prompt_payload_reference(
    container_manager: ContainerManager,
    container_name: str,
    content: str,
    *,
    phase: str,
    filename: str,
    label: str,
) -> str:
    """把大型 Prompt 输入写入容器文件，避免超过 exec argv 上限。"""
    path = f"{GRAPH_SNAPSHOT_ROOT}/{phase}-{uuid.uuid4().hex[:12]}/{filename}"
    container_manager.write_text_file(container_name, path, content)
    return (
        f"The {label} is stored in this file inside the current container:\n\n"
        f"{path}\n\n"
        f"Read the entire file before producing the result and treat it as the authoritative {label}."
    )


def write_graph_snapshot_reference(
    container_manager: ContainerManager,
    container_name: str,
    graph_yaml: str,
    *,
    phase: str,
) -> str:
    path = f"{GRAPH_SNAPSHOT_ROOT}/{phase}-{uuid.uuid4().hex[:12]}/graph.yaml"
    container_manager.write_text_file(container_name, path, graph_yaml)
    # 大 graph 不直接塞进 prompt，改为写容器文件并在 prompt 中引用路径。
    return (
        "The graph YAML snapshot is stored in this file inside the current container:\n\n"
        f"{path}\n\n"
        "Use this authoritative snapshot to read only the Facts and Artifact references relevant "
        "to the current decision; reading the entire graph is not required."
    )


def build_report_readonly_input_references(
    report_packet_json: str,
    graph_yaml: str,
) -> tuple[str, str, tuple[WorkerInputFile, WorkerInputFile]]:
    """延迟描述一次报告执行的只读冻结输入，不在构建 Prompt 时写容器。"""
    directory = f"{REPORT_READONLY_INPUT_ROOT}/{uuid.uuid4().hex}"
    packet_path = f"{directory}/report-packet.json"
    graph_path = f"{directory}/graph.yaml"
    files = (
        WorkerInputFile(
            path=packet_path,
            content=report_packet_json,
            owner="root",
            mode=0o444,
            allowed_root=REPORT_READONLY_INPUT_ROOT,
            readonly_directory=directory,
        ),
        WorkerInputFile(
            path=graph_path,
            content=graph_yaml,
            owner="root",
            mode=0o444,
            allowed_root=REPORT_READONLY_INPUT_ROOT,
            readonly_directory=directory,
        ),
    )
    packet_reference = (
        "The authoritative Report Packet is a Dispatcher-managed read-only input at:\n\n"
        f"{packet_path}\n\n"
        "Read it with read_evidence before producing the result. The tool returns bounded chunks; "
        "continue with next_offset until bytes_read is 0 so the entire packet is considered. "
        "This input is not a new Artifact and must not be passed to register_artifact."
    )
    graph_reference = (
        "The authoritative frozen graph YAML is a Dispatcher-managed read-only input at:\n\n"
        f"{graph_path}\n\n"
        "Use read_evidence to read only the Facts and Artifact references relevant to the current "
        "decision. This input is not a new Artifact and must not be passed to register_artifact."
    )
    return packet_reference, graph_reference, files


def worker_process_failure(
    result: ProcessResult,
    *,
    phase: str,
    project_id: str,
    task_run_id: str,
    session: str | None,
    cancelled: bool = False,
) -> WorkerResultFailure | None:
    """进程事实优先于 stdout；只解析程序信封，不消费 stderr 或异常正文。"""
    def process_failure(failure_class: str, code: str) -> WorkerResultFailure:
        return WorkerResultFailure(
            failure_class=failure_class, code=code, message=f"Pi 进程失败：{code}",
            fingerprint="", origin="dispatcher_process", phase=phase, exit_code=result.returncode,
        )

    if cancelled or result.cancelled:
        return process_failure("cancelled", "cancelled")
    if did_oom(result):
        return process_failure("oom", "process_oom")
    if did_timeout(result):
        return process_failure("timeout", "deadline_exceeded")
    if result.transport_error:
        return process_failure("transport", "transport_failed")
    if result.candidate_capture_integrity_error is not None and result.returncode != 0:
        return process_failure("process", "process_exit_nonzero")
    outcome = decode_pi_result(
        result.read_stdout(), phase=phase, validator=lambda payload: ("ignored", None),
        project_id=project_id, task_run_id=task_run_id, session=session,
    )
    if outcome.failure is not None:
        return replace(outcome.failure, exit_code=result.returncode)
    if result.returncode != 0:
        return process_failure("process", "process_exit_nonzero")
    if result.candidate_capture_integrity_error is not None:
        return process_failure(
            "candidate_integrity",
            result.candidate_capture_integrity_error,
        )
    return None


def run_worker_process(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str] | DriverResult,
    *,
    phase: str,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
    client: CairnClient | None = None,
    project_id: str | None = None,
    task_run_id: str | None = None,
    payload_recorder_enabled: bool = False,
    candidate_outbox: CandidateCaptureOutbox | None = None,
) -> ProcessResult:
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError("worker execution requires a positive integer timeout")
    invocation = argv if isinstance(argv, DriverResult) else DriverResult(argv=argv)
    if invocation.candidate_spool is not None and (
        client is None
        or candidate_outbox is None
        or project_id is None
        or task_run_id is None
        or invocation.session is None
        or invocation.candidate_spool.binding_sha256 is None
        or invocation.candidate_spool.result_schema_sha256 is None
    ):
        raise RuntimeError("candidate capture identity is required before model execution")
    if cancellation is not None and cancellation.is_cancelled:
        return ProcessResult(1, "", "", cancelled=True, cancel_reason=cancellation.reason)
    if lease is not None and lease.failure is not None:
        return ProcessResult(1, "", "", cancelled=True, cancel_reason="heartbeat_lost")
    command_argv = invocation.argv
    LOG.info(
        "starting container exec container=%s worker=%s phase=%s timeout=%ss",
        container_name,
        worker.name,
        phase,
        timeout_seconds,
    )
    record_command_started(client, project_id, task_run_id, phase=phase, worker=worker, argv=command_argv)
    payload_log = prepare_payload_recorder(
        container_manager,
        container_name,
        project_id,
        task_run_id,
        phase=phase,
        enabled=payload_recorder_enabled,
    )
    process_env = _worker_process_env(worker)
    if payload_log is not None:
        process_env.update(
            payload_recorder_env(
                payload_log,
                current_path=process_env.get("PATH"),
            )
        )
    browser_session = None
    try:
        prepare_browser_session = getattr(container_manager, "prepare_browser_session", None)
        if callable(prepare_browser_session):
            process_env, browser_session = prepare_browser_session(
                container_name, dict(process_env), project_id=project_id, task_run_id=task_run_id,
            )
    except Exception as exc:
        LOG.warning(
            "browser session prepare failed project=%s task_run=%s error=%s",
            project_id, task_run_id, type(exc).__name__,
        )
        _keep_browser_task_unmanaged(container_manager, project_id, task_run_id)
        if project_id is not None and task_run_id is not None and "PLAYWRIGHT_CLI_SESSION" not in process_env:
            try:
                store = getattr(container_manager, "browser_sessions", None)
                bound_session = getattr(store, "bound_session", None)
                if callable(bound_session):
                    browser_session = bound_session(project_id, task_run_id)
                    if browser_session is not None:
                        # 清理不确定不能丢失续阶段绑定；显式配置的会话始终保持原值。
                        process_env["PLAYWRIGHT_CLI_SESSION"] = browser_session.name
            except Exception as restore_exc:
                LOG.warning(
                    "browser session binding restore failed project=%s task_run=%s error=%s",
                    project_id, task_run_id, type(restore_exc).__name__,
                )
    oom_kill_count_before = _oom_kill_count(container_manager, container_name)
    started = time.perf_counter()
    written_files: list[WorkerInputFile] = []
    process = None
    process_started = False
    process_start_attempted = False
    candidate_fence: CandidateExecutionFence | None = None
    try:
        _write_worker_input_files(
            container_manager,
            container_name,
            invocation.input_files,
            tracked=written_files,
        )
        if invocation.candidate_spool is not None:
            assert candidate_outbox is not None
            assert project_id is not None
            assert task_run_id is not None
            assert invocation.session is not None
            descriptor = invocation.candidate_spool
            assert descriptor.binding_sha256 is not None
            assert descriptor.result_schema_sha256 is not None
            candidate_fence = CandidateExecutionFence.create(
                owner_id=candidate_outbox.owner_id,
                project_id=project_id,
                task_run_id=task_run_id,
                container_name=container_name,
                phase=phase,
                session=invocation.session,
                spool_path=descriptor.path,
                capture_id=descriptor.capture_id,
                max_candidates=descriptor.max_candidates,
                max_candidate_bytes=descriptor.max_candidate_bytes,
                max_manifest_bytes=descriptor.max_manifest_bytes,
                max_spool_bytes=descriptor.max_spool_bytes,
                binding_sha256=descriptor.binding_sha256,
                result_schema_sha256=descriptor.result_schema_sha256,
                docker_daemon_id=container_manager.docker_daemon_id(),
            )
            candidate_outbox.persist_fence(candidate_fence)
        process = container_manager.build_exec_process(
            container_name,
            process_env,
            command_argv,
            timeout_seconds=timeout_seconds,
        )
        if candidate_fence is not None:
            process.retain_execution_control()

            def bind_candidate_execution(
                container_id: str,
                exec_id: str,
                control_path: str,
            ) -> None:
                nonlocal candidate_fence
                assert candidate_outbox is not None
                assert candidate_fence is not None
                promoted = candidate_fence.bind_execution(
                    container_id=container_id,
                    exec_id=exec_id,
                    control_path=control_path,
                )
                candidate_outbox.replace_fence(candidate_fence, promoted)
                candidate_fence = promoted

            process.on_execution_prepared = bind_candidate_execution

            def mark_candidate_start_attempted() -> None:
                nonlocal candidate_fence
                assert candidate_outbox is not None
                assert candidate_fence is not None
                promoted = candidate_fence.mark_start_attempted()
                candidate_outbox.replace_fence(candidate_fence, promoted)
                candidate_fence = promoted

            process.on_execution_starting = mark_candidate_start_attempted
        if cancellation is not None:
            cancellation.register_execution(process)

            def observe_start():
                cancellation.observe_execution_start(process)
                if client is not None and project_id is not None and task_run_id is not None:
                    client.update_task_run(
                        project_id, task_run_id, status="running",
                        runtime_context={
                            "execution_lifecycle": cancellation.execution_lifecycle(),
                            "execution_container_ids": cancellation.execution_container_ids(),
                        },
                    )

            if isinstance(process, ManagedProcess):
                process.on_execution_started = observe_start
        # 先接入粘性取消，再允许 start，堵住 precheck 与实际启动之间的租约失败窗口。
        if lease is not None:
            lease.attach_process(process)
        if cancellation is not None:
            cancellation.attach_process(process)
        process_start_attempted = True
        process.start()
        process_started = True
    except BaseException:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)
            if process is not None:
                cancellation.finish_execution(process)
        retained_paths: set[str] = set()
        if invocation.candidate_spool is not None and process_start_attempted:
            retained_paths.add(invocation.candidate_spool.path)
        elif candidate_fence is not None and candidate_outbox is not None:
            try:
                fence_released = candidate_outbox.remove_fence(candidate_fence)
                if not fence_released:
                    retained_paths.add(candidate_fence.spool_path)
                    LOG.warning(
                        "candidate fence cleanup lost exact state project=%s task_run=%s phase=%s",
                        project_id,
                        task_run_id,
                        phase,
                    )
            except CandidateCaptureOutboxError as exc:
                retained_paths.add(candidate_fence.spool_path)
                LOG.warning(
                    "candidate fence cleanup failed project=%s task_run=%s phase=%s error=%s",
                    project_id,
                    task_run_id,
                    phase,
                    type(exc).__name__,
                )
        _remove_worker_input_files(
            container_manager,
            container_name,
            written_files,
            retained_paths=retained_paths,
        )
        record_command_finished(
            client,
            project_id,
            task_run_id,
            phase=phase,
            worker=worker,
            argv=command_argv,
            result=None,
            duration_seconds=time.perf_counter() - started,
            status="failed",
        )
        raise
    finally:
        if browser_session is not None:
            try:
                # 部分启动失败也必须登记；未知 exec 身份会阻止后续冒险关闭浏览器。
                identity = getattr(process, "execution_identity", None)
                store = container_manager.browser_sessions
                store.register_exec(browser_session, identity)
                if not process_started:
                    store.finish_exec(browser_session, identity, completed=False)
            except BaseException as exc:
                LOG.warning(
                    "browser exec registration failed project=%s task_run=%s error=%s",
                    project_id, task_run_id, type(exc).__name__,
                )
                _keep_browser_task_unmanaged(container_manager, project_id, task_run_id)
    execution_resources_resolved = False
    try:
        try:
            result = process.communicate(timeout=communicate_timeout(timeout_seconds))
            execution_resources_resolved = (
                _execution_completed(process)
            )
        except BaseException:
            execution_resources_resolved = (
                _execution_completed(process)
            )
            record_command_finished(
                client,
                project_id,
                task_run_id,
                phase=phase,
                worker=worker,
                argv=command_argv,
                result=None,
                duration_seconds=time.perf_counter() - started,
                status="failed",
            )
            raise
        if invocation.candidate_spool is not None:
            result.candidate_capture_required = True
            assert client is not None
            assert candidate_outbox is not None
            assert project_id is not None
            assert task_run_id is not None
            assert invocation.session is not None
            execution_completed = execution_resources_resolved
            execution_identity = process.execution_identity
            execution_start_attempted = process.execution_start_attempted is True
            if not execution_start_attempted and execution_completed:
                # 只有未调用 exec_start 才能证明模型未执行；ACK 丢失不能跳过 candidate 摄取。
                candidate_summary = CandidateIntakeSummary(host_durable=True)
            elif (
                not execution_completed
                or candidate_fence is None
                or not candidate_fence.execution_bound
                or not isinstance(execution_identity, tuple)
                or len(execution_identity) != 2
                or execution_identity
                != (candidate_fence.container_id, candidate_fence.exec_id)
                or process.execution_control_path != candidate_fence.control_path
            ):
                # communicate 返回本身不是物理终止证明；spool 仍可能继续增长。
                candidate_summary = CandidateIntakeSummary(
                    integrity_error="candidate_execution_unresolved",
                )
            else:
                try:
                    terminal_fence = candidate_fence.confirm_terminal()
                    candidate_outbox.replace_fence(candidate_fence, terminal_fence)
                except CandidateCaptureOutboxError as exc:
                    candidate_summary = CandidateIntakeSummary(
                        integrity_error="candidate_terminal_fence_persist_failed",
                    )
                    LOG.warning(
                        "candidate terminal fence persist failed project=%s task_run=%s phase=%s error=%s",
                        project_id,
                        task_run_id,
                        phase,
                        type(exc).__name__,
                    )
                else:
                    candidate_fence = terminal_fence
                    assert candidate_fence.container_id is not None
                    candidate_summary = intake_worker_candidates(
                        client,
                        container_manager,
                        candidate_fence.container_id,
                        invocation.candidate_spool,
                        candidate_outbox,
                        project_id=project_id,
                        task_run_id=task_run_id,
                        phase=phase,
                        session=invocation.session,
                    )
            result.candidate_host_durable = candidate_summary.host_durable
            result.candidate_capture_pending = candidate_summary.pending > 0
            result.candidate_capture_frames = candidate_summary.frames_seen
            result.candidate_capture_integrity_error = candidate_summary.integrity_error
            if (
                candidate_summary.host_durable
                and candidate_summary.integrity_error is None
                and candidate_fence is not None
            ):
                # TaskRun／report receipt 尚未权威落库；terminal fence 必须保留到调度器确认终局后再释放。
                LOG.debug(
                    "candidate fence retained pending authoritative finish project=%s task_run=%s phase=%s",
                    project_id,
                    task_run_id,
                    phase,
                )
        if invocation.checkpoint_spool is not None:
            if not execution_resources_resolved:
                # 未取得物理终态时 spool 仍可能增长，不能读取后再删除不完整历史。
                LOG.warning(
                    "checkpoint intake deferred for unresolved execution project=%s task_run=%s phase=%s",
                    project_id,
                    task_run_id,
                    phase,
                )
            elif client is None or project_id is None or task_run_id is None:
                LOG.warning(
                    "checkpoint intake skipped because delivery identity is unavailable phase=%s",
                    phase,
                )
            else:
                try:
                    # communicate 返回后进程已不再写 spool；摄取必须先于临时文件清理。
                    intake_worker_checkpoints(
                        client,
                        container_manager,
                        container_name,
                        invocation.checkpoint_spool,
                        project_id=project_id,
                        task_run_id=task_run_id,
                        phase=phase,
                    )
                except Exception as exc:
                    # checkpoint 是独立 staging 通道，未知摄取故障也不能覆盖真实进程结果。
                    LOG.warning(
                        "checkpoint intake crashed project=%s task_run=%s phase=%s error=%s",
                        project_id,
                        task_run_id,
                        phase,
                        type(exc).__name__,
                    )
    finally:
        # 未决执行可能迟到启动或继续写入；此时保留全部输入，由 fence/container 恢复路径最终回收。
        if not execution_resources_resolved:
            retained_paths = {item.path for item in written_files}
        else:
            # Candidate spool 与 control 跟随 terminal fence 保留，直到权威 TaskRun／receipt 成功后由恢复路径回收。
            retained_paths = (
                {invocation.candidate_spool.path}
                if invocation.candidate_spool is not None
                else set()
            )
        _remove_worker_input_files(
            container_manager,
            container_name,
            written_files,
            retained_paths=retained_paths,
        )
        if cancellation is not None:
            cancellation.finish_execution(process)
        if browser_session is not None:
            try:
                # 超时后 reader 可能尚未开始 exec；仅提交流耗尽且线程退出的正向证明。
                container_manager.browser_sessions.finish_exec(
                    browser_session, getattr(process, "execution_identity", None),
                    completed=getattr(process, "execution_completed", False),
                )
            except BaseException as exc:
                LOG.warning(
                    "browser exec completion failed project=%s task_run=%s error=%s",
                    project_id, task_run_id, type(exc).__name__,
                )
                _keep_browser_task_unmanaged(container_manager, project_id, task_run_id)
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)
    try:
        _apply_oom_evidence(
            container_manager,
            container_name,
            result,
            before_count=oom_kill_count_before,
        )
        record_command_finished(
            client,
            project_id,
            task_run_id,
            phase=phase,
            worker=worker,
            argv=command_argv,
            result=result,
            duration_seconds=time.perf_counter() - started,
        )
        failure = None
        if client is not None and project_id is not None and task_run_id is not None:
            failure = worker_process_failure(
                result, phase=phase, project_id=project_id, task_run_id=task_run_id,
                session=invocation.session,
            )
            if failure is not None:
                record_validation_result(
                    client, project_id, task_run_id, phase=phase, status="invalid",
                    outcome=worker_failure_error_type(failure), failure=failure,
                    error_message=failure.message,
                )
        # 物理终态确认后即保留安全工具审计；业务失败不能抹掉导致重试的最后工具状态。
        record_pi_tool_events(
            client, project_id, task_run_id, phase=phase, session=invocation.session, result=result,
        )
        record_payload_operations(
            client,
            container_manager,
            container_name,
            project_id,
            task_run_id,
            payload_log,
        )
        # pi 审计和 Payload Recorder 独立采集，不依赖容器内 hooks/settings。
        return result
    except BaseException:
        # 返回前的审计步骤失败时调用方拿不到 result，由本层清理磁盘输出。
        result.cleanup()
        raise
