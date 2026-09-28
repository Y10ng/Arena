"""持久化项目容器激活与暂停确认，不以 TaskRun 缺席推定容器已停止。"""

from __future__ import annotations

from uuid import uuid4

from fastapi import HTTPException

from cairn.server.db import get_conn

from .ids import get_project_or_404
from .time_utils import utcnow


def _locked_project(conn, project_id: str):
    conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
    return get_project_or_404(conn, project_id)


def _activation(conn, project_id: str, dispatcher_id: str):
    return conn.execute(
        "SELECT * FROM project_container_activations WHERE project_id = ? AND dispatcher_id = ?",
        (project_id, dispatcher_id),
    ).fetchone()


def container_activation(project_id: str, dispatcher_id: str) -> dict | None:
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = _activation(conn, project_id, dispatcher_id)
        return dict(row) if row else None


def register_container_activation(
    project_id: str, dispatcher_id: str, generation: int, docker_daemon_id: str,
) -> dict:
    with get_conn() as conn:
        project = _locked_project(conn, project_id)
        if project["status"] != "active" or project["execution_generation"] != generation or project["coverage_stop_reason"] == "user_stopped":
            raise HTTPException(409, "stale_execution_generation")
        if not docker_daemon_id:
            raise HTTPException(409, "docker_daemon_identity_missing")
        # 旧项目的来源未查明前，不允许其他 owner 在新 daemon 启动容器来覆盖现场。
        if conn.execute(
            "SELECT 1 FROM project_container_activations WHERE project_id = ? AND state = 'inventory' LIMIT 1",
            (project_id,),
        ).fetchone() is not None:
            raise HTTPException(409, "container_inventory_unresolved")
        row = _activation(conn, project_id, dispatcher_id)
        if row is not None:
            if row["state"] in {"activating", "stopping"}:
                raise HTTPException(409, "container_activation_pending")
            if row["docker_daemon_id"] is not None and row["docker_daemon_id"] != docker_daemon_id:
                raise HTTPException(409, "docker_daemon_changed")
        token = uuid4().hex
        conn.execute(
            """INSERT INTO project_container_activations
                   (project_id, dispatcher_id, activation_generation, activation_token,
                    container_id, docker_daemon_id, state, pause_generation, stop_token, updated_at)
               VALUES (?, ?, ?, ?, NULL, ?, 'activating', NULL, NULL, ?)
               ON CONFLICT (project_id, dispatcher_id) DO UPDATE SET
                   activation_generation = excluded.activation_generation,
                   activation_token = excluded.activation_token,
                   container_id = NULL, docker_daemon_id = excluded.docker_daemon_id,
                   state = 'activating', pause_generation = NULL, stop_token = NULL,
                   updated_at = excluded.updated_at""",
            (project_id, dispatcher_id, generation, token, docker_daemon_id, utcnow()),
        )
        return {"activation_token": token, "activation_generation": generation}


def finish_container_activation(
    project_id: str, dispatcher_id: str, token: str, container_id: str | None,
) -> dict:
    with get_conn() as conn:
        _locked_project(conn, project_id)
        row = _activation(conn, project_id, dispatcher_id)
        if row is None or row["activation_token"] != token or row["state"] != "activating":
            raise HTTPException(409, "container_activation_stale")
        conn.execute(
            """UPDATE project_container_activations
               SET container_id = ?, state = 'running', updated_at = ?
               WHERE project_id = ? AND dispatcher_id = ?""",
            (container_id, utcnow(), project_id, dispatcher_id),
        )
        return {"state": "running"}


def adopt_container_pause(
    project_id: str, dispatcher_id: str, generation: int,
    container_id: str | None, docker_daemon_id: str,
) -> dict:
    """只收口已有可信 daemon 身份的旧 owner；未知来源不能以当前盘点代证。"""
    with get_conn() as conn:
        project = _locked_project(conn, project_id)
        if project["status"] != "paused" or project["execution_generation"] != generation:
            raise HTTPException(409, "project_pause_stale")
        if not docker_daemon_id:
            raise HTTPException(409, "docker_daemon_identity_missing")
        row = _activation(conn, project_id, dispatcher_id)
        if row is None:
            if container_id is None:
                raise HTTPException(409, "container_inventory_missing")
            conn.execute(
                """INSERT INTO project_container_activations
                   (project_id, dispatcher_id, activation_generation, activation_token,
                    container_id, docker_daemon_id, state, pause_generation, stop_token, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'running', NULL, NULL, ?)""",
                (project_id, dispatcher_id, generation - 1, uuid4().hex, container_id, docker_daemon_id, utcnow()),
            )
        elif row["state"] == "inventory":
            # 当前 daemon 上未发现容器，不能证明升级前未知 daemon 的容器已停止。
            if row["docker_daemon_id"] != docker_daemon_id:
                raise HTTPException(409, "legacy_container_origin_unknown")
            conn.execute(
                """UPDATE project_container_activations
                   SET container_id = ?, state = 'running', updated_at = ?
                   WHERE project_id = ? AND dispatcher_id = ?""",
                (container_id, utcnow(), project_id, dispatcher_id),
            )
        elif row["container_id"] != container_id or row["state"] not in {"running", "stopping"} or row["docker_daemon_id"] != docker_daemon_id:
            raise HTTPException(409, "container_activation_conflict")
        return {"container_id": container_id}


def claim_container_pause(
    project_id: str, dispatcher_id: str, generation: int, docker_daemon_id: str,
) -> dict:
    with get_conn() as conn:
        project = _locked_project(conn, project_id)
        row = _activation(conn, project_id, dispatcher_id)
        if project["status"] != "paused" or project["execution_generation"] != generation or row is None:
            raise HTTPException(409, "project_pause_stale")
        if not docker_daemon_id or row["docker_daemon_id"] != docker_daemon_id:
            raise HTTPException(409, "docker_daemon_changed")
        if row["state"] == "stopped" and row["pause_generation"] == generation:
            return {"state": "stopped", "container_id": row["container_id"]}
        if row["state"] in {"inventory", "activating"} or row["activation_generation"] >= generation:
            raise HTTPException(409, "container_activation_pending")
        token = row["stop_token"] if row["state"] == "stopping" else uuid4().hex
        conn.execute(
            """UPDATE project_container_activations
               SET state = 'stopping', pause_generation = ?, stop_token = ?, updated_at = ?
               WHERE project_id = ? AND dispatcher_id = ?""",
            (generation, token, utcnow(), project_id, dispatcher_id),
        )
        return {"state": "stopping", "stop_token": token, "container_id": row["container_id"]}


def confirm_container_pause(
    project_id: str, dispatcher_id: str, generation: int, token: str,
    container_id: str | None, docker_daemon_id: str,
) -> dict:
    with get_conn() as conn:
        project = _locked_project(conn, project_id)
        row = _activation(conn, project_id, dispatcher_id)
        if (
            project["status"] != "paused"
            or project["execution_generation"] != generation
            or row is None
            or not docker_daemon_id
            or row["docker_daemon_id"] != docker_daemon_id
            or row["state"] != "stopping"
            or row["pause_generation"] != generation
            or row["stop_token"] != token
            or row["container_id"] != container_id
        ):
            raise HTTPException(409, "project_pause_stale")
        conn.execute(
            """UPDATE project_container_activations SET state = 'stopped', updated_at = ?
               WHERE project_id = ? AND dispatcher_id = ?""",
            (utcnow(), project_id, dispatcher_id),
        )
        return {"state": "stopped", "container_id": container_id}


def project_container_pause_draining(conn, project) -> bool:
    if project["status"] != "paused":
        return False
    return conn.execute(
        """SELECT 1 FROM project_container_activations
           WHERE project_id = ? AND activation_generation < ?
             AND (state != 'stopped' OR pause_generation IS NULL OR pause_generation != ?)
           LIMIT 1""",
        (project["id"], project["execution_generation"], project["execution_generation"]),
    ).fetchone() is not None
