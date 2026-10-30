"""项目组分类退场的协议守卫；数据保留由隔离 PostgreSQL 迁移测试覆盖。"""

import pytest
from pydantic import ValidationError

from cairn.server.models import CreateProjectRequest, ProjectMeta
from cairn.server.routers.projects import router
from cairn.server.routers.tenant_workers import router as worker_router


def test_project_creation_has_no_group_and_rejects_retired_input() -> None:
    payload = {
        "title": "无组项目",
        "origin": "隔离测试授权背景",
        "goal": "只检查协议，不执行目标调用",
        "scenario": "goal-driven-v1",
        "authorization_expires_at": None,
    }
    assert "group_id" not in CreateProjectRequest.model_validate(payload).model_dump()
    assert "tenant_worker_ids" not in CreateProjectRequest.model_fields
    with pytest.raises(ValidationError) as exc:
        CreateProjectRequest.model_validate(payload | {"tenant_worker_ids": ["worker-test"]})
    assert any(error["loc"] == ("tenant_worker_ids",) and error["type"] == "extra_forbidden" for error in exc.value.errors())
    for value in (None, "group-old"):
        with pytest.raises(ValidationError) as exc:
            CreateProjectRequest.model_validate(payload | {"group_id": value})
        assert any(error["type"] == "extra_forbidden" for error in exc.value.errors())


def test_project_metadata_retires_groups_and_worker_rebinding() -> None:
    assert not {"group_id", "group_name"} & ProjectMeta.model_fields.keys()
    assert not any(route.path.endswith("/group") for route in router.routes)
    assert not any(route.path.endswith("/management-options") or route.path.endswith("/workers") for route in router.routes)
    assert not any(route.path.endswith("/options") or route.methods & {"POST", "DELETE"} for route in worker_router.routes)
