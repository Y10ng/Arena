"""Tenant Model 的 DNS 校验、IP 固定与有界 HTTP transport。"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
import json
import socket
import ssl
import time
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_RESPONSE_BYTES = 64 * 1024
MAX_REDIRECTS = 3
_METADATA_HOSTS = {
    "metadata.google.internal",
    "metadata.azure.internal",
    "instance-data.ec2.internal",
}
_METADATA_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
}
_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


class ModelTransportError(RuntimeError):
    """可安全映射为 operation category 的 transport 错误。"""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


@dataclass(frozen=True)
class ModelTransportResponse:
    status_code: int
    body: bytes
    latency_ms: int
    url: str


def join_model_endpoint(base_url: str, endpoint: str) -> str:
    """兼容 service root 与已包含 /v1 的协议默认路径。"""
    parsed = urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    suffix = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    if base_path.endswith("/v1") and suffix.startswith("/v1/"):
        suffix = suffix[3:]
    path = f"{base_path}{suffix}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def resolve_model_endpoint(base_url: str, override: str | None, default_endpoint: str) -> str:
    """解析协议默认路径或用户明确配置的相对/绝对 endpoint。"""
    if override is None:
        return join_model_endpoint(base_url, default_endpoint)
    if not override or any(ord(character) < 32 or ord(character) == 127 for character in override):
        raise ModelTransportError("unsafe_destination", "Model endpoint contains invalid characters")
    if override.startswith("//"):
        raise ModelTransportError("unsafe_destination", "Scheme-relative Model endpoints are not allowed")
    try:
        parsed = urlsplit(override)
        port = parsed.port
    except ValueError as exc:
        raise ModelTransportError("unsafe_destination", "Model endpoint URL is invalid") from exc
    if parsed.fragment or parsed.username is not None or parsed.password is not None:
        raise ModelTransportError("unsafe_destination", "Model endpoint URL is unsafe")
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ModelTransportError("unsafe_destination", "Absolute Model endpoint must use http or https")
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host if port is None else f"{host}:{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))

    base = urlsplit(base_url)
    base_path = base.path.rstrip("/")
    relative_path = parsed.path.lstrip("/")
    path = f"{base_path}/{relative_path}" if relative_path else (base_path or "/")
    return urlunsplit((base.scheme, base.netloc, path, parsed.query, ""))


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout_seconds: int,
) -> ModelTransportResponse:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request_headers = dict(headers)
    if body is not None:
        request_headers.setdefault("content-type", "application/json")
    started = time.monotonic()
    current_url = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        response = _request_once(
            method,
            current_url,
            headers=request_headers,
            body=body,
            timeout_seconds=timeout_seconds,
            deadline=started + timeout_seconds,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = _header_value(response, "location")
        if not location:
            raise ModelTransportError("protocol", "Upstream redirect did not include Location")
        if redirect_count >= MAX_REDIRECTS:
            raise ModelTransportError("protocol", "Upstream redirect limit exceeded")
        next_url = urljoin(current_url, location)
        if _origin(next_url) != _origin(current_url):
            raise ModelTransportError("unsafe_destination", "Cross-origin Model redirect was rejected")
        current_url = next_url
        if response.status_code == 303:
            method = "GET"
            body = None
            request_headers.pop("content-type", None)
    raise ModelTransportError("protocol", "Upstream redirect limit exceeded")


def _request_once(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: bytes | None,
    timeout_seconds: int,
    deadline: float,
) -> ModelTransportResponse:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ModelTransportError("unsafe_destination", "Model endpoint URL is invalid")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ModelTransportError("unsafe_destination", "Model endpoint URL is unsafe")
    hostname = parsed.hostname.rstrip(".").casefold()
    addresses = _resolve_safe_addresses(hostname, parsed.port, parsed.scheme)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ModelTransportError("timeout", "Model request timed out")
    timeout = min(float(timeout_seconds), remaining)
    connection: http.client.HTTPConnection
    if parsed.scheme == "https":
        connection = _PinnedHTTPSConnection(hostname, port, addresses[0], timeout)
    else:
        connection = _PinnedHTTPConnection(hostname, port, addresses[0], timeout)
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    started = time.monotonic()
    try:
        connection.request(method, path, body=body, headers=headers)
        upstream = connection.getresponse()
        response_body = _read_limited(upstream, deadline=deadline)
        response_headers = tuple((name.casefold(), value) for name, value in upstream.getheaders())
        return _InternalResponse(
            status_code=upstream.status,
            body=response_body,
            latency_ms=int((time.monotonic() - started) * 1000),
            url=url,
            headers=response_headers,
        )
    except (TimeoutError, socket.timeout) as exc:
        raise ModelTransportError("timeout", "Model request timed out") from exc
    except ssl.SSLError as exc:
        raise ModelTransportError("tls", "Model TLS negotiation failed") from exc
    except socket.gaierror as exc:
        raise ModelTransportError(
            "dns",
            f"Model hostname could not be resolved: {hostname}",
        ) from exc
    except (ConnectionError, OSError, http.client.HTTPException) as exc:
        raise ModelTransportError("connect", "Model endpoint could not be reached") from exc
    finally:
        connection.close()


@dataclass(frozen=True)
class _InternalResponse(ModelTransportResponse):
    headers: tuple[tuple[str, str], ...]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, port: int, address: ipaddress._BaseAddress, timeout: float) -> None:
        super().__init__(hostname, port=port, timeout=timeout)
        self._pinned_address = str(address)

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, port: int, address: ipaddress._BaseAddress, timeout: float) -> None:
        super().__init__(hostname, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = str(address)

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _resolve_safe_addresses(hostname: str, explicit_port: int | None, scheme: str) -> list[ipaddress._BaseAddress]:
    if hostname in _METADATA_HOSTS or hostname.endswith(".metadata.google.internal"):
        raise ModelTransportError("unsafe_destination", "Metadata destinations are not allowed")
    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
        addresses = {literal}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0].split("%", 1)[0])
                for item in socket.getaddrinfo(
                    hostname,
                    explicit_port or (443 if scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror as exc:
            raise ModelTransportError(
                "dns",
                f"Model hostname could not be resolved: {hostname}",
            ) from exc
    if not addresses:
        raise ModelTransportError("dns", "Model hostname did not resolve to an address")
    for address in addresses:
        if not _address_is_allowed(address):
            raise ModelTransportError("unsafe_destination", "Model hostname resolved to an unsafe address")
    return sorted(addresses, key=lambda item: (item.version, int(item)))


def _address_is_allowed(address: ipaddress._BaseAddress) -> bool:
    if address in _METADATA_ADDRESSES:
        return False
    if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return address.is_global or any(address in network for network in _RFC1918_NETWORKS)
    return address.is_global


def _read_limited(response: http.client.HTTPResponse, *, deadline: float) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        if time.monotonic() > deadline:
            raise ModelTransportError("timeout", "Model request timed out")
        chunk = response.read(min(4096, MAX_RESPONSE_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ModelTransportError("protocol", "Model response exceeded the size limit")
    return b"".join(chunks)


def _header_value(response: ModelTransportResponse, name: str) -> str | None:
    if not isinstance(response, _InternalResponse):
        return None
    expected = name.casefold()
    for header_name, value in response.headers:
        if header_name == expected:
            return value
    return None


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    if not parsed.hostname:
        raise ModelTransportError("unsafe_destination", "Redirect URL is invalid")
    return parsed.scheme, parsed.hostname.casefold(), parsed.port or (443 if parsed.scheme == "https" else 80)
