"""不可变、版本化报告模板服务。"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from re import fullmatch
import sqlite3

from fastapi import HTTPException

from cairn.server.schemas.report_templates import (
    ReportTemplate,
    ReportTemplateCatalogEntry,
    ReportTemplateVersion,
    ReportTemplateVersionDetail,
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


def _template_model(row: sqlite3.Row) -> ReportTemplate:
    return ReportTemplate(**dict(row))


def _version_profiles(
    conn: sqlite3.Connection,
    template_key: str,
    version: int,
) -> list[str]:
    rows = conn.execute(
        """SELECT profile_id FROM report_template_version_profiles
           WHERE template_key = ? AND template_version = ? ORDER BY profile_id""",
        (template_key, version),
    ).fetchall()
    return [str(row["profile_id"]) for row in rows]


def _version_model(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> ReportTemplateVersion:
    values = dict(row)
    values["compatibility_profiles"] = _version_profiles(
        conn,
        str(row["template_key"]),
        int(row["version"]),
    )
    return ReportTemplateVersion(**values)


def list_report_templates(
    conn: sqlite3.Connection,
    *,
    include_archived: bool = False,
) -> list[ReportTemplate]:
    where = "" if include_archived else "WHERE template.archived_at IS NULL"
    rows = conn.execute(
        f"""
        SELECT template.key, template.name, template.description,
               template.created_at, template.archived_at,
               (SELECT MAX(version.version)
                FROM report_template_versions AS version
                WHERE version.template_key = template.key) AS latest_version
        FROM report_templates AS template
        {where}
        ORDER BY template.key
        """
    ).fetchall()
    return [_template_model(row) for row in rows]


def get_report_template(
    conn: sqlite3.Connection,
    key: str,
    *,
    include_archived: bool = False,
) -> ReportTemplate:
    normalized = validate_report_template_key(key)
    for item in list_report_templates(conn, include_archived=include_archived):
        if item.key == normalized:
            return item
    raise HTTPException(404, f"Report template {normalized} not found")


def create_report_template(
    conn: sqlite3.Connection,
    key: str,
    name: str,
    description: str = "",
) -> ReportTemplate:
    normalized = validate_report_template_key(key)
    if conn.execute(
        "SELECT 1 FROM report_templates WHERE key = ?", (normalized,)
    ).fetchone() is not None:
        raise HTTPException(409, f"Report template {normalized} already exists")
    conn.execute(
        """INSERT INTO report_templates (key, name, description, created_at)
           VALUES (?, ?, ?, ?)""",
        (normalized, name.strip(), description.strip(), _utcnow()),
    )
    return get_report_template(conn, normalized)


def archive_report_template(conn: sqlite3.Connection, key: str) -> None:
    template = get_report_template(conn, key, include_archived=True)
    if template.archived_at is None:
        conn.execute(
            "UPDATE report_templates SET archived_at = ? WHERE key = ?",
            (_utcnow(), template.key),
        )


def list_report_template_versions(
    conn: sqlite3.Connection,
    key: str,
) -> list[ReportTemplateVersion]:
    normalized = get_report_template(conn, key, include_archived=True).key
    rows = conn.execute(
        """SELECT template_key, version, digest_sha256, message, created_by, created_at
           FROM report_template_versions
           WHERE template_key = ? ORDER BY version DESC""",
        (normalized,),
    ).fetchall()
    return [_version_model(conn, row) for row in rows]


def get_report_template_version(
    conn: sqlite3.Connection,
    key: str,
    version: int,
    *,
    allow_archived: bool = True,
) -> ReportTemplateVersionDetail:
    normalized = get_report_template(
        conn, key, include_archived=allow_archived
    ).key
    row = conn.execute(
        """SELECT template_key, version, digest_sha256, markdown_content,
                  message, created_by, created_at
           FROM report_template_versions
           WHERE template_key = ? AND version = ?""",
        (normalized, version),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"Report template {normalized} version {version} not found")
    values = dict(row)
    values["compatibility_profiles"] = _version_profiles(
        conn,
        normalized,
        version,
    )
    item = ReportTemplateVersionDetail(**values)
    if report_template_digest(item.markdown_content) != item.digest_sha256:
        raise RuntimeError(
            f"Report template digest mismatch: {normalized}@{version}"
        )
    return item


def create_report_template_version(
    conn: sqlite3.Connection,
    key: str,
    markdown_content: str,
    *,
    message: str | None = None,
    created_by: str | None = None,
    compatibility_profiles: list[str] | None = None,
) -> ReportTemplateVersionDetail:
    template = get_report_template(conn, key)
    content = markdown_content.strip()
    digest = report_template_digest(content)
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
        "FROM report_template_versions WHERE template_key = ?",
        (template.key,),
    ).fetchone()
    version = int(row["next_version"])
    created_at = _utcnow()
    conn.execute(
        """INSERT INTO report_template_versions
           (template_key, version, digest_sha256, markdown_content,
            message, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            template.key,
            version,
            digest,
            content,
            message,
            created_by,
            created_at,
        ),
    )
    profiles = compatibility_profiles or ["assessment-report-packet-v1"]
    for profile in profiles:
        conn.execute(
            """INSERT INTO report_template_version_profiles
               (template_key, template_version, profile_id, created_at)
               VALUES (?, ?, ?, ?)""",
            (template.key, version, profile, created_at),
        )
    return get_report_template_version(conn, template.key, version)


def list_report_template_catalog(
    conn: sqlite3.Connection,
) -> list[ReportTemplateCatalogEntry]:
    rows = conn.execute(
        """SELECT template.key, template.name, template.description,
                  version.version, version.digest_sha256
           FROM report_templates AS template
           JOIN report_template_versions AS version
             ON version.template_key = template.key
            AND version.version = (
                SELECT MAX(candidate.version)
                FROM report_template_versions AS candidate
                WHERE candidate.template_key = template.key
            )
           WHERE template.archived_at IS NULL
           ORDER BY template.key"""
    ).fetchall()
    catalog: list[ReportTemplateCatalogEntry] = []
    for row in rows:
        values = dict(row)
        values["compatibility_profiles"] = _version_profiles(
            conn,
            str(row["key"]),
            int(row["version"]),
        )
        catalog.append(ReportTemplateCatalogEntry(**values))
    return catalog


def require_report_template_profile(
    conn: sqlite3.Connection,
    template_key: str,
    version: int,
    profile_id: str,
) -> ReportTemplateVersionDetail:
    template = get_report_template_version(
        conn,
        template_key,
        version,
        allow_archived=False,
    )
    if profile_id not in template.compatibility_profiles:
        raise HTTPException(409, "assessment_report_template_incompatible")
    return template


__all__ = [name for name in globals() if not name.startswith("__")]
