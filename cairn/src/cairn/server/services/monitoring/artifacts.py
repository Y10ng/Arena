"""Artifact 监控聚合。"""

from __future__ import annotations

from .common import *

from .tasks import _task_run_summary

def _artifact_summary(conn, now: str) -> MonitoringArtifactSummary:
    # artifact summary 同时覆盖磁盘规模、类别分布和写入失败任务，供监控页快速定位证据问题。
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_count,
            COALESCE(SUM(size), 0) AS total_size,
            SUM(CASE WHEN kind = 'raw_output' THEN 1 ELSE 0 END) AS raw_output_count,
            COALESCE(SUM(CASE WHEN kind = 'raw_output' THEN size ELSE 0 END), 0) AS raw_output_size,
            SUM(CASE WHEN category = 'evidence' THEN 1 ELSE 0 END) AS evidence_count,
            SUM(CASE WHEN category = 'deliverable' THEN 1 ELSE 0 END) AS deliverable_count
        FROM artifacts
        """
    ).fetchone()
    latest = conn.execute(
        """
        SELECT id, created_at, uri
        FROM artifacts
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    largest_raw_output = conn.execute(
        """
        SELECT id, project_id, task_run_id, size
        FROM artifacts
        WHERE kind = 'raw_output'
        ORDER BY size DESC, created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    failed = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM task_runs
        WHERE error_type = 'artifact_write_failed'
        """
    ).fetchone()
    recent_failure_rows = conn.execute(
        """
        SELECT tr.*, p.title AS project_title, p.status AS project_status,
               COALESCE(ac.artifact_count, 0) AS artifact_count,
               CASE
                   WHEN tr.started_at IS NULL THEN NULL
                   ELSE CAST(MAX(0, (julianday(COALESCE(tr.ended_at, ?)) - julianday(tr.started_at)) * 86400) AS INTEGER)
               END AS duration_seconds
        FROM task_runs tr
        JOIN projects p ON p.id = tr.project_id
        LEFT JOIN (
            SELECT project_id, task_run_id, COUNT(*) AS artifact_count
            FROM artifacts
            GROUP BY project_id, task_run_id
        ) ac ON ac.project_id = tr.project_id AND ac.task_run_id = tr.id
        WHERE tr.error_type = 'artifact_write_failed'
        ORDER BY COALESCE(tr.ended_at, tr.started_at, tr.id) DESC, tr.project_id, tr.id
        LIMIT 5
        """,
        (now,),
    ).fetchall()
    declared_count, declared_by_kind = _declared_artifact_counts(conn)
    return MonitoringArtifactSummary(
        total_count=row["total_count"] or 0,
        total_size=row["total_size"] or 0,
        raw_output_count=row["raw_output_count"] or 0,
        raw_output_size=row["raw_output_size"] or 0,
        largest_raw_output_artifact_id=largest_raw_output["id"] if largest_raw_output is not None else None,
        largest_raw_output_project_id=largest_raw_output["project_id"] if largest_raw_output is not None else None,
        largest_raw_output_task_run_id=largest_raw_output["task_run_id"] if largest_raw_output is not None else None,
        largest_raw_output_size=largest_raw_output["size"] if largest_raw_output is not None else 0,
        evidence_count=row["evidence_count"] or 0,
        deliverable_count=row["deliverable_count"] or 0,
        declared_count=declared_count,
        declared_by_kind=declared_by_kind,
        latest_artifact_id=latest["id"] if latest is not None else None,
        latest_created_at=latest["created_at"] if latest is not None else None,
        latest_uri=latest["uri"] if latest is not None else None,
        write_failed_task_count=failed["count"] or 0,
        recent_write_failures=[_task_run_summary(row) for row in recent_failure_rows],
    )


def _declared_artifact_counts(conn) -> tuple[int, dict[str, int]]:
    rows = conn.execute("SELECT kind, metadata_json FROM artifacts ORDER BY kind").fetchall()
    count = 0
    by_kind: dict[str, int] = {}
    for row in rows:
        try:
            metadata = loads(row["metadata_json"] or "{}")
        except (JSONDecodeError, TypeError):
            metadata = {}
        # 只有 worker 声明并由 dispatcher 成功读取写入的 artifact 才计入 declared。
        if not isinstance(metadata, dict) or metadata.get("declared_by_worker") is not True:
            continue
        kind = row["kind"]
        count += 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return count, dict(sorted(by_kind.items()))
