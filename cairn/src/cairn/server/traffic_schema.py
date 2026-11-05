"""HTTP 采集事实及接受回执的 PostgreSQL 结构。"""

import base64
import re
from urllib.parse import urlsplit


def _authority_host(authority: str) -> str | None:
    if not authority or any(char.isspace() or ord(char) < 32 for char in authority):
        return None
    if any(char in authority for char in "/?#@\\"):
        return None
    try:
        parsed = urlsplit("//" + authority)
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return None
        return parsed.hostname
    except ValueError:
        return None


def traffic_request_address(request: dict) -> dict:
    """按请求形式派生地址；冲突明确呈现，连接目标不冒充请求域名。"""
    def text(value: str) -> str:
        return base64.b64decode(value).decode("utf-8", errors="backslashreplace")

    target = text(request["target_base64"])
    authority = text(request["authority_base64"])
    hosts = [text(value).strip(" \t") for key, value in request["headers_base64"]
             if base64.b64decode(key).lower() == b"host"]
    issue = "multiple_host_headers" if len(hosts) > 1 else None
    absolute = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)", target)
    connect = request["method"].upper() == "CONNECT"
    if absolute:
        selected = absolute.group(2)
        url = target
        path = target[absolute.end():].split("?", 1)[0].split("#", 1)[0] or "/"
    elif connect:
        selected = target
        url, path = f"{request['scheme']}://{target}", target
    else:
        selected = authority or (hosts[0] if len(hosts) == 1 else "")
        # 无有效 authority/Host 时只展示实际请求目标，不用连接 IP 猜测 URL。
        url = f"{request['scheme']}://{selected}{target}" if selected else target
        path = target.split("?", 1)[0].split("#", 1)[0]
    if selected and any(value.lower() != selected.lower() for value in hosts):
        issue = issue or "host_authority_conflict"
    if absolute and authority and authority.lower() != selected.lower():
        issue = issue or "authority_target_conflict"
    host = _authority_host(selected)
    if not host:
        issue = issue or ("invalid_request_authority" if selected else "request_authority_missing")
        host, url = "", target
    # 展示目标保留原查询编码与特殊请求形式，不复用已去掉查询串的检索 path。
    display_target = target[absolute.end():] if absolute else target
    if absolute and (not display_target or display_target.startswith("?")):
        display_target = "/" + display_target
    display_scheme = absolute.group(1) if absolute else request["scheme"]
    display_host = f"{display_scheme}://{selected}" if host else ""
    # PostgreSQL TEXT 不接受 NUL；只有可重建派生列做可见转义，原件不变。
    return {key: value.replace("\x00", "\\x00") if isinstance(value, str) else value
            for key, value in {
                "host": host, "request_url": url, "request_path": path,
                "display_host": display_host, "display_target": display_target,
                "connection_host": request["host"], "address_issue": issue,
            }.items()}


def traffic_request_location(request: dict) -> tuple[str, str]:
    address = traffic_request_address(request)
    return address["request_url"], address["request_path"]


POSTGRES_HTTP_TRAFFIC_V65_SCHEMA = """
-- 正文流式存储不应被旧 Artifact 的 32 位长度字段限制在 2 GiB 内。
ALTER TABLE artifacts ALTER COLUMN size TYPE BIGINT;

CREATE TABLE http_capture_scopes (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_run_id TEXT NOT NULL,
    generation BIGINT NOT NULL CHECK (generation >= 0),
    container_id TEXT NOT NULL,
    dispatcher_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id),
    FOREIGN KEY (task_run_id, project_id) REFERENCES task_runs(id, project_id) ON DELETE CASCADE
);
CREATE INDEX idx_http_capture_scopes_run ON http_capture_scopes(project_id, task_run_id);

CREATE TABLE http_flows (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    capture_scope_id TEXT NOT NULL,
    revision BIGINT NOT NULL CHECK (revision > 0),
    record_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('receiving', 'complete', 'partial', 'failed')),
    method TEXT NOT NULL,
    host TEXT NOT NULL,
    status_code INTEGER,
    created_cursor BIGINT NOT NULL,
    updated_cursor BIGINT NOT NULL,
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id),
    FOREIGN KEY (capture_scope_id, project_id) REFERENCES http_capture_scopes(id, project_id) ON DELETE CASCADE
);
CREATE INDEX idx_http_flows_created ON http_flows(project_id, created_cursor);
CREATE INDEX idx_http_flows_updated ON http_flows(project_id, updated_cursor);
CREATE INDEX idx_http_flows_scope ON http_flows(project_id, capture_scope_id, created_cursor);
CREATE INDEX idx_http_flows_metadata ON http_flows(project_id, state, method, host);

CREATE TABLE http_flow_receipts (
    project_id TEXT NOT NULL,
    flow_id TEXT NOT NULL,
    revision BIGINT NOT NULL CHECK (revision > 0),
    record_sha256 TEXT NOT NULL,
    PRIMARY KEY (project_id, flow_id, revision),
    FOREIGN KEY (flow_id, project_id) REFERENCES http_flows(id, project_id) ON DELETE CASCADE
);

CREATE TABLE http_flow_bodies (
    project_id TEXT NOT NULL,
    flow_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('request', 'response')),
    revision BIGINT NOT NULL CHECK (revision > 0),
    artifact_id TEXT NOT NULL,
    size BIGINT NOT NULL CHECK (size >= 0),
    sha256 TEXT NOT NULL,
    PRIMARY KEY (project_id, flow_id, side, revision),
    FOREIGN KEY (flow_id, project_id) REFERENCES http_flows(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (artifact_id, project_id) REFERENCES artifacts(id, project_id) ON DELETE CASCADE
);
"""

POSTGRES_HTTP_TRAFFIC_QUERY_V67_SCHEMA = """
ALTER TABLE http_flows ADD COLUMN request_url TEXT NOT NULL DEFAULT '';
ALTER TABLE http_flows ADD COLUMN request_path TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_http_flows_received ON http_flows(received_at DESC, project_id DESC, id DESC);
CREATE INDEX idx_http_flows_project_received ON http_flows(project_id, received_at DESC, id DESC);
CREATE INDEX idx_http_observations_evidence ON vulnerability_observations USING gin ((evidence_items_json::jsonb));
"""


POSTGRES_HTTP_TRAFFIC_STREAMING_V66_SCHEMA = """
ALTER TABLE http_capture_scopes ADD COLUMN status_json TEXT;
ALTER TABLE http_capture_scopes ADD COLUMN status_updated_at TEXT;

CREATE TABLE http_flow_body_streams (
    project_id TEXT NOT NULL,
    flow_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('request', 'response')),
    accepted_bytes BIGINT NOT NULL DEFAULT 0 CHECK (accepted_bytes >= 0),
    PRIMARY KEY (project_id, flow_id, side),
    FOREIGN KEY (flow_id, project_id) REFERENCES http_flows(id, project_id) ON DELETE CASCADE
);

CREATE TABLE http_flow_body_chunks (
    project_id TEXT NOT NULL,
    flow_id TEXT NOT NULL,
    side TEXT NOT NULL,
    byte_offset BIGINT NOT NULL CHECK (byte_offset >= 0),
    size INTEGER NOT NULL CHECK (size > 0 AND size <= 1048576),
    sha256 TEXT NOT NULL,
    PRIMARY KEY (project_id, flow_id, side, byte_offset),
    FOREIGN KEY (project_id, flow_id, side)
        REFERENCES http_flow_body_streams(project_id, flow_id, side) ON DELETE CASCADE
);
"""
