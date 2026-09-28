"""Tenant Model transport 的 URL、DNS 与 redirect 安全策略测试。"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from cairn.server.services import model_transport


def test_join_model_endpoint_avoids_duplicate_v1() -> None:
    assert model_transport.join_model_endpoint("https://api.example.test", "/v1/models") == "https://api.example.test/v1/models"
    assert model_transport.join_model_endpoint("https://api.example.test/v1", "/v1/models") == "https://api.example.test/v1/models"
    assert model_transport.join_model_endpoint("https://api.example.test/service", "/v1/models") == "https://api.example.test/service/v1/models"


def test_address_policy_allows_public_and_rfc1918_only() -> None:
    for value in ("8.8.8.8", "10.20.30.40", "172.16.1.2", "192.168.1.2"):
        assert model_transport._address_is_allowed(ipaddress.ip_address(value))
    for value in (
        "127.0.0.1",
        "169.254.169.254",
        "100.100.100.200",
        "100.64.0.1",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
    ):
        assert not model_transport._address_is_allowed(ipaddress.ip_address(value))


def test_dns_error_identifies_failed_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_resolution(*_args, **_kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "not found")

    monkeypatch.setattr(socket, "getaddrinfo", fail_resolution)

    with pytest.raises(model_transport.ModelTransportError) as exc:
        model_transport._resolve_safe_addresses("vendor-api.example.test", 443, "https")

    assert exc.value.category == "dns"
    assert exc.value.detail == "Model hostname could not be resolved: vendor-api.example.test"


def test_mixed_dns_results_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.30.40", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(model_transport.ModelTransportError) as exc:
        model_transport._resolve_safe_addresses("mixed.example.test", 443, "https")
    assert exc.value.category == "unsafe_destination"


def test_metadata_host_and_address_are_rejected() -> None:
    for host in ("metadata.google.internal", "169.254.169.254", "100.100.100.200"):
        with pytest.raises(model_transport.ModelTransportError) as exc:
            model_transport._resolve_safe_addresses(host, 80, "http")
        assert exc.value.category == "unsafe_destination"


def test_cross_origin_redirect_with_custom_headers_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            model_transport._InternalResponse(
                status_code=302,
                body=b"",
                latency_ms=1,
                url="https://api.example.test/v1/models",
                headers=(("location", "https://other.example.test/v1/models"),),
            )
        ]
    )
    monkeypatch.setattr(model_transport, "_request_once", lambda *args, **kwargs: next(responses))
    with pytest.raises(model_transport.ModelTransportError) as exc:
        model_transport.request_json(
            "GET",
            "https://api.example.test/v1/models",
            headers={"X-Gateway-Token": "secret"},
            payload=None,
            timeout_seconds=5,
        )
    assert exc.value.category == "unsafe_destination"


def test_same_origin_redirect_is_revalidated(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_request(method, url, **kwargs):
        seen.append(url)
        if len(seen) == 1:
            return model_transport._InternalResponse(
                status_code=307,
                body=b"",
                latency_ms=1,
                url=url,
                headers=(("location", "/v1/models-next"),),
            )
        return model_transport._InternalResponse(
            status_code=200,
            body=b"{}",
            latency_ms=2,
            url=url,
            headers=(),
        )

    monkeypatch.setattr(model_transport, "_request_once", fake_request)
    response = model_transport.request_json(
        "GET",
        "https://api.example.test/v1/models",
        headers={"authorization": "Bearer secret"},
        payload=None,
        timeout_seconds=5,
    )
    assert response.status_code == 200
    assert seen == [
        "https://api.example.test/v1/models",
        "https://api.example.test/v1/models-next",
    ]
