"""项目 Agent 默认模型配置；仅引用既有租户模型，不创建 Worker。"""

from fastapi import HTTPException

from cairn.server.auth import write_security_audit
from .model_execution import model_availability, resolve_model_execution_binding
from .time_utils import utcnow


def get_configuration(conn, tenant_id):
    row = conn.execute("SELECT model_id FROM project_agent_configurations WHERE tenant_id = ?", (tenant_id,)).fetchone()
    return model_availability(conn, tenant_id=tenant_id, model_id=row["model_id"] if row else None)


def update_configuration(conn, model_id, principal):
    if model_id is not None:
        resolve_model_execution_binding(conn, tenant_id=principal.tenant_id, model_id=model_id)
    conn.execute(
        "INSERT INTO project_agent_configurations(tenant_id, model_id, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(tenant_id) DO UPDATE SET model_id = excluded.model_id, updated_at = excluded.updated_at",
        (principal.tenant_id, model_id, utcnow()),
    )
    write_security_audit(
        conn, actor_type=principal.kind, actor_id=principal.subject_id, tenant_id=principal.tenant_id,
        action="project_agent.configure", resource_type="project_agent_configuration", resource_id=principal.tenant_id,
        outcome="success", metadata={"model_id": model_id},
    )
    return get_configuration(conn, principal.tenant_id)


def default_model_id(conn, tenant_id):
    configuration = get_configuration(conn, tenant_id)
    if configuration["model_id"] is None:
        raise HTTPException(422, {
            "code": "agent_model_unconfigured", "message": "请先配置项目 Agent 默认模型，新建会话无需选择研究 Worker。",
        })
    return configuration["model_id"]
