"""Dispatcher 启动健康检查，在正式调度前验证 worker adapter 可用性。"""

from __future__ import annotations

import logging
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.readiness import (
    READINESS_COMMAND,
    ReadinessErrorType,
    run_readiness_check,
)

LOG = logging.getLogger("runtime.startup")


@dataclass(slots=True)
class StartupHealthcheckResult:
    """单个 worker 的启动检查摘要，日志只展示压缩后的响应片段。"""

    worker_name: str
    ok: bool
    returncode: int
    duration_ms: int
    http_status: str | None
    response_preview: str
    stderr_preview: str
    command: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    error_type: ReadinessErrorType | None = None


def run_startup_healthchecks(
    config: DispatchConfig,
    container_manager: ContainerManager,
    *,
    show_commands: bool = False,
) -> list[StartupHealthcheckResult]:
    workers = [worker for worker in config.workers if worker.enabled]
    if not workers:
        LOG.info("startup healthcheck skipped because no enabled workers are configured")
        return []
    container_name = container_manager.create_startup_container()
    # 启动只检验本地 SDK/协议结构，不构造任务请求或调用模型。
    parallelism = max(1, min(len(workers), config.runtime.max_workers, 8))
    LOG.info(
        "[*] Startup healthcheck: workers=%s parallelism=%s",
        len(workers),
        parallelism,
    )
    try:
        with ThreadPoolExecutor(max_workers=parallelism) as executor:
            future_map = {
                executor.submit(
                    _run_worker_healthcheck,
                    container_manager,
                    container_name,
                    worker,
                    config.runtime.healthcheck_timeout,
                ): worker.name
                for worker in workers
            }
            results: list[StartupHealthcheckResult] = []
            for future in as_completed(future_map):
                results.append(future.result())
    finally:
        # 启动检查容器是临时资源，无论成功失败都不参与项目复用。
        LOG.debug("removing startup healthcheck container container=%s", container_name)
        container_manager.remove_container(container_name, force=True)

    results.sort(key=lambda result: result.worker_name)
    _log_report(results, show_commands=show_commands)
    return results


def format_failure_summary(results: list[StartupHealthcheckResult]) -> str:
    failed = [result for result in results if not result.ok]
    if not failed:
        return "startup healthchecks failed for all workers"
    details = []
    for result in failed:
        preview = result.response_preview or result.stderr_preview or "-"
        details.append(
            f"{result.worker_name}(http={result.http_status or '-'}, code={result.returncode}, preview={preview})"
        )
    return f"startup healthchecks failed for all workers: {', '.join(details)}"


def _run_worker_healthcheck(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    timeout_seconds: int,
) -> StartupHealthcheckResult:
    readiness = run_readiness_check(
        container_manager,
        container_name,
        timeout_seconds=timeout_seconds,
    )
    return StartupHealthcheckResult(
        worker_name=worker.name,
        ok=readiness.ok,
        returncode=readiness.returncode,
        duration_ms=readiness.duration_ms,
        http_status=None,
        response_preview=(
            "offline SDK self-check accepted"
            if readiness.ok
            else readiness.error_type or "readiness_process"
        ),
        stderr_preview="",
        command=shlex.join(READINESS_COMMAND),
        capabilities=readiness.capabilities,
        error_type=readiness.error_type,
    )


def _log_report(results: list[StartupHealthcheckResult], *, show_commands: bool) -> None:
    if not results:
        LOG.warning("[!] Startup healthcheck: no workers configured")
        return
    worker_width = max(len("WORKER"), *(len(result.worker_name) for result in results))
    lines = ["[=] Startup healthcheck results"]
    header = f"{'CHK':<5} {'WORKER':<{worker_width}} {'HTTP':<6} {'CODE':<6} {'TIME_S':>8}  PREVIEW"
    lines.append(header)
    lines.append(f"{'-' * 5} {'-' * worker_width} {'-' * 6} {'-' * 6} {'-' * 8}  {'-' * 50}")
    healthy_count = 0
    for result in results:
        if result.ok:
            healthy_count += 1
        marker = "[+]" if result.ok else "[-]"
        preview = result.response_preview or result.stderr_preview or "-"
        duration_seconds = f"{result.duration_ms / 1000:.2f}"
        lines.append(
            f"{marker:<5} "
            f"{result.worker_name:<{worker_width}} "
            f"{(result.http_status or '-'): <6} "
            f"{result.returncode:<6} "
            f"{duration_seconds:>8}  "
            f"{preview}"
        )
    lines.append(
        f"[=] Summary: total={len(results)} healthy={healthy_count} unhealthy={len(results) - healthy_count}"
    )
    if show_commands:
        lines.append("")
        lines.append("[=] Startup healthcheck commands")
        for result in results:
            lines.append(f"- {result.worker_name}")
            lines.append(f"  {result.command}")
        lines.append("")
    LOG.info("\n%s\n", "\n".join(lines))
