"""项目与项目内作用域 ID 服务。"""

from __future__ import annotations

from .common import *

def next_project_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project'").fetchone()
    return f"proj_{row['value']:03d}"


def next_project_group_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project_group'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project_group'").fetchone()
    return f"pg_{row['value']:03d}"


def next_worker_template_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'worker_template'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'worker_template'").fetchone()
    return f"wt_{row['value']:03d}"


def _next_scoped_id(
    conn: sqlite3.Connection, kind: str, prefix: str, project_id: str
) -> str:
    # 项目内 ID 独立计数，导出的 fact/intent 序号更短也更容易人工阅读。
    conn.execute(
        "INSERT INTO scoped_counters (project_id, kind, value) VALUES (?, ?, 0) ON CONFLICT DO NOTHING",
        (project_id, kind),
    )
    conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    )
    row = conn.execute(
        "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    ).fetchone()
    assert row is not None
    return f"{prefix}{row['value']:03d}"


def next_fact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "fact", "f", project_id)


def next_intent_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "intent", "i", project_id)


def next_hint_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "hint", "h", project_id)


def next_target_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "target", "t", project_id)


def next_vulnerability_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "vulnerability", "v", project_id)


def next_task_run_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "task_run", "tr", project_id)


def next_assessment_attempt_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "assessment_attempt", "aa", project_id)


def next_agent_event_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "agent_event", "ev", project_id)


def next_artifact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "artifact", "a", project_id)


def next_project_resource_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "project_resource", "r", project_id)


def next_injection_payload_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "injection_payload", "p", project_id)


def get_project_or_404(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    tenant_id: str | None = None,
) -> sqlite3.Row:
    """按显式 Tenant context 读取项目；省略 context 仅供内部任务和直接 service 测试。"""
    if tenant_id is None:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND tenant_id = ?",
            (project_id, tenant_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row
