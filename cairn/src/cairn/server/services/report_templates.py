"""报告模板当前内容的读取、保存与真实删除。"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from re import fullmatch
from uuid import uuid4

from fastapi import HTTPException

from cairn.server.db import PostgresConnection
from cairn.server.report_template_defaults_v60 import DEFAULT_REPORT_TEMPLATE_KEY_V60
from cairn.server.report_template_defaults_v69 import DEFAULT_SINGLE_REPORT_TEMPLATE_KEY, SINGLE_REPORT_PROFILE
from cairn.server.schemas.report_templates import (
    ReportTemplate,
    ReportTemplateCatalogEntry,
    ReportTemplateUpdate,
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_report_template_key(value: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not fullmatch(r"[a-z0-9_-]{1,128}", text):
        raise HTTPException(422, "Invalid report template key")
    return text


def report_template_digest(markdown_content: str) -> str:
    return sha256(markdown_content.encode("utf-8")).hexdigest()


def _template_model(conn: PostgresConnection, row) -> ReportTemplate:
    profiles = conn.execute(
        "SELECT profile_id FROM report_template_profiles WHERE template_key = ? ORDER BY profile_id",
        (row["key"],),
    ).fetchall()
    content = row["markdown_content"] or ""
    validation_error = None if content.strip() else "模板正文不能为空"
    if len(content.encode("utf-8")) > 500_000:
        validation_error = "模板正文不能超过 500000 个 UTF-8 字节"
    item = ReportTemplate(
        **dict(row),
        compatibility_profiles=[str(profile["profile_id"]) for profile in profiles],
        is_default=row["key"] == DEFAULT_REPORT_TEMPLATE_KEY_V60,
        supports_single_vulnerability=False,
        validation_error=validation_error,
    )
    if item.markdown_content is not None and report_template_digest(item.markdown_content) != item.digest_sha256:
        raise RuntimeError(f"Report template digest mismatch: {item.key}")
    return item


def default_single_report_template_key(conn: PostgresConnection) -> str:
    # 已自定义的旧默认不会生成专用种子；继续尊重该用户模板，不按正文偷偷替换。
    row = conn.execute("SELECT key FROM report_templates WHERE key = ?", (DEFAULT_SINGLE_REPORT_TEMPLATE_KEY,)).fetchone()
    return DEFAULT_SINGLE_REPORT_TEMPLATE_KEY if row is not None else DEFAULT_REPORT_TEMPLATE_KEY_V60


def list_report_templates(conn: PostgresConnection) -> list[ReportTemplate]:
    rows = conn.execute("SELECT * FROM report_templates ORDER BY key").fetchall()
    return [_template_model(conn, row) for row in rows]


def _template_row(conn: PostgresConnection, key: str, *, lock: str = ""):
    normalized = validate_report_template_key(key)
    row = conn.execute(
        f"SELECT * FROM report_templates WHERE key = ? {lock}", (normalized,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"Report template {normalized} not found")
    return row


def get_report_template(conn: PostgresConnection, key: str) -> ReportTemplate:
    return _template_model(conn, _template_row(conn, key))


def _replace_profiles(conn: PostgresConnection, key: str, profiles: list[str]) -> None:
    # 修改关系之前持有模板行锁，受理方可一次冻结同一份正文与 profile。
    conn.execute("DELETE FROM report_template_profiles WHERE template_key = ?", (key,))
    for profile in profiles:
        conn.execute(
            "INSERT INTO report_template_profiles (template_key, profile_id) VALUES (?, ?)",
            (key, profile),
        )


def create_report_template(
    conn: PostgresConnection,
    key: str | None,
    name: str,
    description: str,
    markdown_content: str,
    *,
    compatibility_profiles: list[str] | None = None,
) -> ReportTemplate:
    body = ReportTemplateUpdate(
        name=name,
        description=description,
        markdown_content=markdown_content,
        **({"compatibility_profiles": compatibility_profiles} if compatibility_profiles is not None else {}),
    )
    normalized = validate_report_template_key(key) if key is not None else f"report-{uuid4().hex}"
    now = _utcnow()
    inserted = conn.execute(
        """INSERT INTO report_templates
           (key, name, description, markdown_content, digest_sha256, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO NOTHING""",
        (normalized, body.name, body.description, body.markdown_content,
         report_template_digest(body.markdown_content), now, now),
    )
    if inserted.rowcount != 1:
        raise HTTPException(409, f"Report template {normalized} already exists")
    _replace_profiles(conn, normalized, body.compatibility_profiles)
    return get_report_template(conn, normalized)


def update_report_template(
    conn: PostgresConnection,
    key: str,
    name: str,
    description: str,
    markdown_content: str,
    *,
    compatibility_profiles: list[str] | None = None,
) -> ReportTemplate:
    body = ReportTemplateUpdate(
        name=name,
        description=description,
        markdown_content=markdown_content,
        **({"compatibility_profiles": compatibility_profiles} if compatibility_profiles is not None else {}),
    )
    row = _template_row(conn, key, lock="FOR UPDATE")
    conn.execute(
        """UPDATE report_templates SET name = ?, description = ?, markdown_content = ?,
           digest_sha256 = ?, updated_at = ? WHERE key = ?""",
        (body.name, body.description, body.markdown_content,
         report_template_digest(body.markdown_content), _utcnow(), row["key"]),
    )
    _replace_profiles(conn, row["key"], body.compatibility_profiles)
    return get_report_template(conn, row["key"])


def delete_report_template(conn: PostgresConnection, key: str) -> None:
    row = _template_row(conn, key, lock="FOR UPDATE")
    if row["key"] == DEFAULT_REPORT_TEMPLATE_KEY_V60:
        raise HTTPException(409, "report_template_default_delete_forbidden")
    # finalization 自持冻结输入，不以外键绑定当前模板，也不随删除撤销任务。
    conn.execute("DELETE FROM report_templates WHERE key = ?", (row["key"],))


def list_report_template_catalog(conn: PostgresConnection) -> list[ReportTemplateCatalogEntry]:
    return [
        ReportTemplateCatalogEntry(
            key=item.key,
            name=item.name,
            description=item.description,
            digest_sha256=item.digest_sha256,
            compatibility_profiles=item.compatibility_profiles,
            is_default=item.is_default,
            supports_single_vulnerability=item.supports_single_vulnerability,
            validation_error=item.validation_error,
        )
        for item in list_report_templates(conn)
        if item.markdown_content is not None and item.markdown_content.strip()
    ]


def require_report_template_profile(
    conn: PostgresConnection,
    template_key: str,
    profile_id: str,
) -> ReportTemplate:
    # 行锁保持到受理事务结束，禁止编辑或删除穿过正文／profile 冻结的边界。
    template = _template_model(conn, _template_row(conn, template_key, lock="FOR SHARE"))
    if template.markdown_content is None or not template.markdown_content.strip():
        raise HTTPException(409, "assessment_report_template_content_missing")
    if template.validation_error is not None:
        raise HTTPException(409, {"code": "assessment_report_template_invalid", "message": template.validation_error})
    return template


__all__ = [name for name in globals() if not name.startswith("__")]
