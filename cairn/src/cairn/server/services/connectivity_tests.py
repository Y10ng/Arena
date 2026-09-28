"""Server 侧 Provider/Model/Worker 联通性测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import ipaddress
import json
import os
import re
import socket
import sqlite3
from threading import BoundedSemaphore, Lock
import time
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
import uuid

from fastapi import HTTPException
import requests

from cairn.dispatcher.config import resolve_credential
from cairn.server import db
from cairn.server.schemas.monitoring_dispatcher import DispatcherConnectivityTest
from .dispatcher_config_store import DispatcherConfigStore
from .time_utils import utcnow


ConnectivityTarget = Literal["provider", "model", "worker"]
ConnectivityCategory = Literal[
    "dns",
    "connect",
    "tls",
    "auth",
    "not_found",
    "rate_limited",
    "model",
    "protocol",
    "timeout",
    "ok",
]

_MAX_DETAIL_LENGTH = 320
_SECRET_PATTERN = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+\S+|x-api-key\s*:\s*\S+|"
    r"[\"']?(?:api[_-]?key|token|secret|password)[\"']?\s*[=:]\s*[\"']?[^\s,}\"']+|"
    r"sk-[A-Za-z0-9_-]{8,})"
)
_QUERY_VALUE_PATTERN = re.compile(r"([?&][^=\s&#]+)=([^&#\s]+)")
_GLOBAL_LIMIT = max(1, min(int(os.environ.get("CAIRN_CONNECTIVITY_TEST_MAX_CONCURRENCY", "4")), 16))
_EXECUTOR = ThreadPoolExecutor(max_workers=_GLOBAL_LIMIT, thread_name_prefix="cairn-connectivity")
_CAPACITY = BoundedSemaphore(_GLOBAL_LIMIT)
_ACTIVE_TARGETS: set[str] = set()
_ACTIVE_LOCK = Lock()


class ConnectivityTargetSpec:
    def __init__(
        self,
        *,
        target_type: ConnectivityTarget,
        target_id: str,
        provider_id: str,
        base_url: str,
        credential: str,
        credential_fingerprint: str,
        config_revision: str,
        model_id: str | None,
        upstream_model: str | None,
        scope: str,
    ) -> None:
        self.target_type = target_type
        self.target_id = target_id
        self.provider_id = provider_id
        self.base_url = base_url
        self.credential = credential
        self.credential_fingerprint = credential_fingerprint
        self.config_revision = config_revision
        self.model_id = model_id
        self.upstream_model = upstream_model
        self.scope = scope

    @property
    def target_key(self) -> str:
        return f"{self.target_type}:{self.target_id}"


class ConnectivityOutcome:
    def __init__(
        self,
        category: ConnectivityCategory,
        detail: str,
        *,
        http_status: int | None = None,
        latency_ms: int | None = None,
    ) -> None:
        self.category = category
        self.detail = detail
        self.http_status = http_status
        self.latency_ms = latency_ms


def start_connectivity_test(
    conn: sqlite3.Connection,
    store: DispatcherConfigStore,
    target_type: ConnectivityTarget,
    target_id: str,
    *,
    timeout_seconds: int,
) -> DispatcherConnectivityTest:
    spec = resolve_connectivity_target(store, target_type, target_id)
    if not _CAPACITY.acquire(blocking=False):
        raise HTTPException(429, "Connectivity test global concurrency limit reached")
    with _ACTIVE_LOCK:
        if spec.target_key in _ACTIVE_TARGETS:
            _CAPACITY.release()
            raise HTTPException(409, f"Connectivity test already running for {spec.target_key}")
        _ACTIVE_TARGETS.add(spec.target_key)

    test_id = f"ct_{uuid.uuid4().hex}"
    created_at = utcnow()
    try:
        conn.execute(
            """
            INSERT INTO dispatcher_connectivity_tests
                (id, target_type, target_id, target_key, provider_id, model_id, scope,
                 status, config_revision, credential_fingerprint, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (
                test_id,
                spec.target_type,
                spec.target_id,
                spec.target_key,
                spec.provider_id,
                spec.model_id,
                spec.scope,
                spec.config_revision,
                spec.credential_fingerprint,
                created_at,
            ),
        )
        # 后台线程需要立即看到任务记录，因此在提交前完成这一次短事务。
        conn.commit()
        _EXECUTOR.submit(_run_connectivity_test, test_id, spec, timeout_seconds)
    except Exception:
        with _ACTIVE_LOCK:
            _ACTIVE_TARGETS.discard(spec.target_key)
        _CAPACITY.release()
        raise
    return get_connectivity_test(conn, store, test_id)


def get_connectivity_test(
    conn: sqlite3.Connection,
    store: DispatcherConfigStore,
    test_id: str,
) -> DispatcherConnectivityTest:
    row = conn.execute(
        "SELECT * FROM dispatcher_connectivity_tests WHERE id = ?",
        (test_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"Connectivity test not found: {test_id}")
    stale = _connectivity_test_is_stale(store, row)
    return DispatcherConnectivityTest(
        id=row["id"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        provider_id=row["provider_id"],
        model_id=row["model_id"],
        scope=row["scope"],
        status=row["status"],
        category=row["category"],
        detail=row["detail"] or "",
        http_status=row["http_status"],
        latency_ms=row["latency_ms"],
        config_revision=row["config_revision"],
        credential_fingerprint=row["credential_fingerprint"],
        stale=stale,
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def resolve_connectivity_target(
    store: DispatcherConfigStore,
    target_type: ConnectivityTarget,
    target_id: str,
) -> ConnectivityTargetSpec:
    config, _content, revision = store.read()
    providers = _items_by_id(config.get("providers"))
    models = _items_by_id(config.get("models"))
    workers = _items_by_id(config.get("workers"))

    model_id: str | None = None
    if target_type == "provider":
        provider = providers.get(target_id)
        if provider is None:
            raise HTTPException(404, f"Provider not found: {target_id}")
        provider_models = sorted(
            (item for item in models.values() if item.get("provider_id") == target_id),
            key=lambda item: str(item.get("id") or ""),
        )
        model = provider_models[0] if provider_models else None
        model_id = str(model.get("id")) if model is not None else None
        scope = "messages_api" if model is not None else "transport_endpoint_only"
    elif target_type == "model":
        model = models.get(target_id)
        if model is None:
            raise HTTPException(404, f"Model not found: {target_id}")
        model_id = target_id
        provider = providers.get(str(model.get("provider_id") or ""))
        if provider is None:
            raise HTTPException(422, f"Model {target_id} references a missing Provider")
        scope = "messages_api"
    elif target_type == "worker":
        worker = workers.get(target_id)
        if worker is None:
            raise HTTPException(404, f"Worker not found: {target_id}")
        model_id = str(worker.get("model_id") or "")
        model = models.get(model_id)
        if model is None:
            raise HTTPException(422, f"Worker {target_id} references a missing Model")
        provider = providers.get(str(model.get("provider_id") or ""))
        if provider is None:
            raise HTTPException(422, f"Worker {target_id} references a missing Provider")
        scope = "messages_api"
    else:
        raise HTTPException(422, f"Unsupported connectivity target: {target_type}")

    provider_id = str(provider.get("id") or "")
    if provider.get("provider_api") != "anthropic-messages":
        raise HTTPException(422, f"Provider {provider_id} is not configured for anthropic-messages")
    base_url = _validated_base_url(str(provider.get("base_url") or ""))
    common_env = config.get("common_env") if isinstance(config.get("common_env"), dict) else {}
    credential_ref = str(provider.get("credential_ref") or "")
    credential = resolve_credential(credential_ref, common_env)
    if not credential:
        raise HTTPException(422, f"Provider {provider_id} credential is unavailable")
    upstream_model = str(model.get("upstream_model") or "") if model is not None else None
    return ConnectivityTargetSpec(
        target_type=target_type,
        target_id=target_id,
        provider_id=provider_id,
        base_url=base_url,
        credential=credential,
        credential_fingerprint=_credential_fingerprint(credential),
        config_revision=revision,
        model_id=model_id,
        upstream_model=upstream_model,
        scope=scope,
    )


def perform_connectivity_request(
    spec: ConnectivityTargetSpec,
    *,
    timeout_seconds: int,
) -> ConnectivityOutcome:
    endpoint = f"{spec.base_url}/v1/messages"
    headers = {
        "authorization": f"Bearer {spec.credential}",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "user-agent": "Arena-connectivity-test/1",
    }
    payload: dict[str, Any]
    if spec.upstream_model:
        payload = {
            "model": spec.upstream_model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }
    else:
        # 无 Model 的 Provider 测试故意发送不完整请求，只确认 transport、endpoint 与 auth 前置链路。
        payload = {}
    started = time.monotonic()
    destination_error = _validate_connectivity_destination(spec.base_url)
    if destination_error is not None:
        return ConnectivityOutcome(destination_error[0], destination_error[1])
    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=(min(3, timeout_seconds), timeout_seconds),
            stream=True,
            allow_redirects=False,
        )
        body = _read_limited_response(response, deadline=started + timeout_seconds)
    except requests.exceptions.SSLError as exc:
        return ConnectivityOutcome("tls", _sanitize_detail(str(exc), spec.credential))
    except requests.exceptions.Timeout:
        return ConnectivityOutcome("timeout", "Connectivity request timed out")
    except requests.exceptions.ConnectionError as exc:
        category: ConnectivityCategory = "dns" if _is_dns_error(exc) else "connect"
        return ConnectivityOutcome(category, _sanitize_detail(str(exc), spec.credential))
    except Exception as exc:
        return ConnectivityOutcome("protocol", _sanitize_detail(str(exc), spec.credential))

    latency_ms = int((time.monotonic() - started) * 1000)
    return classify_connectivity_response(
        response.status_code,
        body,
        scope=spec.scope,
        credential=spec.credential,
        latency_ms=latency_ms,
    )


def classify_connectivity_response(
    status_code: int,
    body: bytes,
    *,
    scope: str,
    credential: str,
    latency_ms: int,
) -> ConnectivityOutcome:
    payload = _safe_json_object(body)
    detail = _response_detail(payload, status_code, credential)
    if 200 <= status_code < 300:
        if not isinstance(payload, dict) or payload.get("type") != "message" or not isinstance(payload.get("content"), list):
            return ConnectivityOutcome("protocol", "Messages API returned an invalid success envelope", http_status=status_code, latency_ms=latency_ms)
        return ConnectivityOutcome("ok", "Minimal Messages API request succeeded", http_status=status_code, latency_ms=latency_ms)
    if status_code in {401, 403}:
        category: ConnectivityCategory = "auth"
    elif status_code == 404:
        category = "model" if scope == "messages_api" and _mentions_model(payload) else "not_found"
    elif status_code == 429:
        category = "rate_limited"
    elif status_code in {400, 422} and scope == "transport_endpoint_only" and _is_error_envelope(payload):
        return ConnectivityOutcome(
            "ok",
            "Messages endpoint reachable; no Model was available, so inference and model access were not tested",
            http_status=status_code,
            latency_ms=latency_ms,
        )
    elif status_code in {400, 422} and _mentions_model(payload):
        category = "model"
    else:
        category = "protocol"
    return ConnectivityOutcome(category, detail, http_status=status_code, latency_ms=latency_ms)


def _run_connectivity_test(test_id: str, spec: ConnectivityTargetSpec, timeout_seconds: int) -> None:
    try:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE dispatcher_connectivity_tests SET status = 'running', started_at = ? WHERE id = ?",
                (utcnow(), test_id),
            )
        outcome = perform_connectivity_request(spec, timeout_seconds=timeout_seconds)
        with db.get_conn() as conn:
            conn.execute(
                """
                UPDATE dispatcher_connectivity_tests
                SET status = 'completed', category = ?, detail = ?, http_status = ?,
                    latency_ms = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    outcome.category,
                    outcome.detail,
                    outcome.http_status,
                    outcome.latency_ms,
                    utcnow(),
                    test_id,
                ),
            )
    except Exception as exc:
        with db.get_conn() as conn:
            conn.execute(
                """
                UPDATE dispatcher_connectivity_tests
                SET status = 'completed', category = 'protocol', detail = ?, finished_at = ?
                WHERE id = ?
                """,
                (_sanitize_detail(str(exc), spec.credential), utcnow(), test_id),
            )
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_TARGETS.discard(spec.target_key)
        _CAPACITY.release()


def _connectivity_test_is_stale(store: DispatcherConfigStore, row: sqlite3.Row) -> bool:
    try:
        config, _content, revision = store.read()
        if revision != row["config_revision"]:
            return True
        providers = _items_by_id(config.get("providers"))
        provider = providers.get(row["provider_id"])
        if provider is None:
            return True
        common_env = config.get("common_env") if isinstance(config.get("common_env"), dict) else {}
        credential = resolve_credential(str(provider.get("credential_ref") or ""), common_env)
        return _credential_fingerprint(credential) != row["credential_fingerprint"]
    except Exception:
        return True


def _items_by_id(raw: object) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list):
        return {}
    items: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or item.get("name") or "")
        if item_id:
            items[item_id] = item
    return items


def _validated_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(422, "Provider base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(422, "Provider base_url must not contain credentials, query, or fragment")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _validate_connectivity_destination(base_url: str) -> tuple[ConnectivityCategory, str] | None:
    if os.environ.get("CAIRN_CONNECTIVITY_ALLOW_PRIVATE_NETWORKS", "").lower() in {"1", "true", "yes", "on"}:
        return None
    hostname = urlsplit(base_url).hostname
    if not hostname:
        return "protocol", "Provider base_url has no hostname"
    try:
        addresses = {
            ipaddress.ip_address(item[4][0].split("%", 1)[0])
            for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        }
    except socket.gaierror:
        return "dns", "Provider hostname could not be resolved"
    except ValueError:
        return "protocol", "Provider hostname resolved to an invalid address"
    blocked = [
        address
        for address in addresses
        if not address.is_global
    ]
    if blocked:
        return "connect", "Provider base_url resolves to a non-public address; set CAIRN_CONNECTIVITY_ALLOW_PRIVATE_NETWORKS only for trusted private endpoints"
    return None


def _credential_fingerprint(credential: str) -> str:
    return sha256(credential.encode("utf-8")).hexdigest()[:24]


def _read_limited_response(response: requests.Response, *, deadline: float) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=4096):
        if time.monotonic() > deadline:
            response.close()
            raise requests.exceptions.Timeout("wall-clock deadline exceeded")
        if not chunk:
            continue
        remaining = 16384 - size
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        size += min(len(chunk), remaining)
    response.close()
    if time.monotonic() > deadline:
        raise requests.exceptions.Timeout("wall-clock deadline exceeded")
    return b"".join(chunks)


def _safe_json_object(body: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _response_detail(payload: dict[str, Any] | None, status_code: int, credential: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            error_type = str(error.get("type") or "api_error")
            message = str(error.get("message") or "")
            return _sanitize_detail(f"{error_type}: {message}", credential)
    return f"Messages API returned HTTP {status_code}"


def _is_error_envelope(payload: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("type") == "error"
        and isinstance(payload.get("error"), dict)
    )


def _mentions_model(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    text = f"{error.get('type', '')} {error.get('message', '')}".lower()
    return "model" in text


def _is_dns_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, socket.gaierror):
            return True
        current = current.__cause__ or current.__context__
    text = str(exc).lower()
    return any(marker in text for marker in ("name resolution", "nodename nor servname", "temporary failure in name"))


def _sanitize_detail(value: str, credential: str) -> str:
    text = " ".join(value.replace("\x00", " ").split())
    if credential:
        text = text.replace(credential, "[REDACTED]")
    text = _QUERY_VALUE_PATTERN.sub(r"\1=[REDACTED]", text)
    text = _SECRET_PATTERN.sub("[REDACTED]", text)
    return text[:_MAX_DETAIL_LENGTH] or "Connectivity test failed"
