"""Prompt 渲染审计事件记录器。"""

from __future__ import annotations

from .common_base import *

def record_prompt_rendered(
    client: CairnClient | None,
    project_id: str | None,
    task_run_id: str | None,
    *,
    phase: str,
    prompt_group: str,
    prompt_name: str,
    prompt: str,
    worker: WorkerConfig,
    replacement_keys: list[str],
) -> None:
    if client is None or project_id is None or task_run_id is None:
        return
    prompt_bytes = prompt.encode("utf-8")
    # 不保存完整 prompt，只保存 hash/大小/替换键，降低敏感上下文泄露面。
    payload: dict[str, object] = {
        "phase": phase,
        "prompt_group": prompt_group,
        "prompt_name": prompt_name,
        "prompt_hash": sha256(prompt_bytes).hexdigest(),
        "prompt_bytes": len(prompt_bytes),
        "prompt_chars": len(prompt),
        "replacement_keys": sorted(replacement_keys),
        "worker_name": worker.name,
        "worker_type": worker.type,
    }
    try:
        response = client.create_agent_event(
            project_id,
            task_run_id=task_run_id,
            kind="prompt_rendered",
            payload=payload,
        )
    except Exception as exc:
        # prompt 审计失败不应阻塞任务执行；后续仍可从 task_run 和输出 artifact 排查。
        LOG.warning(
            "prompt rendered event create raised project=%s task_run=%s phase=%s prompt=%s error=%s",
            project_id,
            task_run_id,
            phase,
            prompt_name,
            exc,
        )
        return
    if response.ok:
        return
    LOG.warning(
        "prompt rendered event create failed project=%s task_run=%s phase=%s prompt=%s status=%s body=%s",
        project_id,
        task_run_id,
        phase,
        prompt_name,
        response.status_code,
        response.text,
    )
