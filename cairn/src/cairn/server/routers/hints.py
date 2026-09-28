"""项目提示接口，允许用户向仍可写的项目补充人工线索。"""

from fastapi import APIRouter, Depends, HTTPException

from cairn.server.auth import RequestPrincipal, require_user_tenant_project
from cairn.server.db import get_conn
from cairn.server.models import CreateHintRequest, Hint
from cairn.server.services import check_project_hint_writable, next_hint_id, utcnow

router = APIRouter(tags=["hints"])


@router.post(
    "/projects/{project_id}/hints",
    response_model=Hint,
    status_code=201,
)
def create_hint(
    project_id: str,
    body: CreateHintRequest,
    principal: RequestPrincipal = Depends(require_user_tenant_project),
):
    with get_conn() as conn:
        # completed 项目只读，stopped/active 项目仍允许补充 hints 供恢复或后续调度使用。
        check_project_hint_writable(conn, project_id)
        # 指定 fact_id 时校验该 fact 存在于当前项目，避免留下悬空定向提示。
        if body.fact_id:
            fact_row = conn.execute(
                "SELECT id FROM facts WHERE id = ? AND project_id = ?",
                (body.fact_id, project_id),
            ).fetchone()
            if fact_row is None:
                raise HTTPException(422, f"Fact {body.fact_id} not found in project {project_id}")
        # 指定 intent_id 时校验该 intent 存在；intent 锚点比 fact 更精确地引导探索方向。
        if body.intent_id:
            intent_row = conn.execute(
                "SELECT id FROM intents WHERE id = ? AND project_id = ?",
                (body.intent_id, project_id),
            ).fetchone()
            if intent_row is None:
                raise HTTPException(422, f"Intent {body.intent_id} not found in project {project_id}")

        creator = principal.subject_id if isinstance(principal, RequestPrincipal) else body.creator
        if creator is None:
            raise HTTPException(422, "creator is required")
        now = utcnow()
        hid = next_hint_id(conn, project_id)
        conn.execute(
            "INSERT INTO hints (id, project_id, fact_id, intent_id, content, creator, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (hid, project_id, body.fact_id, body.intent_id, body.content, creator, now),
        )
        return Hint(
            id=hid,
            content=body.content,
            creator=creator,
            fact_id=body.fact_id,
            intent_id=body.intent_id,
            created_at=now,
        )
