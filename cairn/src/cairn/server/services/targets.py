"""Target 校验、规范化和转换服务。"""

from __future__ import annotations

from .common import *


def get_target_or_404(
    conn: sqlite3.Connection, project_id: str, target_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM targets WHERE id = ? AND project_id = ?",
        (target_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Target not found")
    return row


def normalize_target_value(target_type: str, value: str) -> str:
    text = value.strip()
    try:
        # target 规范化是去重基础：域名小写、默认端口省略、CIDR 标准化。
        if target_type == "ip":
            return str(ip_address(text))
        if target_type == "cidr":
            return str(ip_network(text, strict=False))
        if target_type == "domain":
            return _normalize_domain_target(text)
        if target_type == "url":
            return _normalize_url_target(text)
    except ValueError as exc:
        raise HTTPException(422, f"Invalid {target_type} target") from exc
    raise HTTPException(422, f"Unsupported target type {target_type}")


def derive_seed_targets_from_origin(origin: str) -> list[tuple[str, str]]:
    candidates: list[tuple[int, str, str]] = []
    seen: set[tuple[str, str]] = set()
    occupied_spans: list[tuple[int, int]] = []

    # 先匹配 URL，再匹配 IP/CIDR 和域名，避免 URL 中的 host 被重复提取。
    for match in URL_TARGET_RE.finditer(origin):
        value = _strip_target_punctuation(match.group(0))
        _append_normalized_target(candidates, seen, match.start(), "url", value)
        occupied_spans.append(match.span())

    for match in IPV4_TARGET_RE.finditer(origin):
        if _span_overlaps(match.span(), occupied_spans):
            continue
        value = _strip_target_punctuation(match.group(0))
        target_type = "cidr" if "/" in value else "ip"
        _append_normalized_target(candidates, seen, match.start(), target_type, value)
        occupied_spans.append(match.span())

    for match in DOMAIN_TARGET_RE.finditer(origin):
        if _span_overlaps(match.span(), occupied_spans):
            continue
        value = _strip_target_punctuation(match.group(0))
        _append_normalized_target(candidates, seen, match.start(), "domain", value)
        occupied_spans.append(match.span())

    return [(target_type, value) for _position, target_type, value in sorted(candidates)]


def validate_target_not_duplicate(
    conn: sqlite3.Connection,
    project_id: str,
    target_type: str,
    value: str,
) -> None:
    row = conn.execute(
        "SELECT id FROM targets WHERE project_id = ? AND type = ? AND value = ?",
        (project_id, target_type, value),
    ).fetchone()
    if row is not None:
        raise HTTPException(409, f"Target already exists: {row['id']}")


def target_exists(
    conn: sqlite3.Connection,
    project_id: str,
    target_type: str,
    value: str,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM targets WHERE project_id = ? AND type = ? AND value = ?",
        (project_id, target_type, value),
    ).fetchone()
    return row is not None


def target_to_model(row: sqlite3.Row) -> Target:
    return Target(
        id=row["id"],
        value=row["value"],
        type=row["type"],
        status=row["status"],
        source_fact_id=row["source_fact_id"],
        discovered_by_run_id=row["discovered_by_run_id"],
        created_at=row["created_at"],
    )


def build_targets(conn: sqlite3.Connection, project_id: str) -> list[Target]:
    rows = conn.execute(
        "SELECT * FROM targets WHERE project_id = ? ORDER BY created_at, id",
        (project_id,),
    ).fetchall()
    return [target_to_model(row) for row in rows]


def _append_normalized_target(
    candidates: list[tuple[int, str, str]],
    seen: set[tuple[str, str]],
    position: int,
    target_type: str,
    value: str,
) -> None:
    try:
        normalized = normalize_target_value(target_type, value)
    except HTTPException:
        return
    key = (target_type, normalized)
    if key in seen:
        return
    seen.add(key)
    candidates.append((position, target_type, normalized))


def _span_overlaps(span: tuple[int, int], occupied_spans: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < occupied_end and occupied_start < end for occupied_start, occupied_end in occupied_spans)


def _strip_target_punctuation(value: str) -> str:
    return value.strip().strip("<>()[]{}\"'").rstrip(".,;:!?")


def _normalize_domain_target(value: str) -> str:
    domain = value.rstrip(".").lower()
    if not domain or "://" in domain or "/" in domain or any(ch.isspace() for ch in domain):
        raise ValueError("invalid domain target")
    return domain


def _normalize_url_target(value: str) -> str:
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc or parts.hostname is None:
        raise ValueError("url target must include scheme and host")
    scheme = parts.scheme.lower()
    hostname = parts.hostname.lower().rstrip(".")
    port = parts.port
    if (scheme, port) in (("http", 80), ("https", 443)):
        port = None
    if ":" in hostname and not hostname.startswith("["):
        host = f"[{hostname}]"
    else:
        host = hostname
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))
