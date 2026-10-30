"""请求地址来自原请求形式，不由连接 IP 或 DNS 猜测。"""

import base64
from copy import deepcopy

import pytest

from cairn.server.traffic_schema import traffic_request_address


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def request(*, target="/a%2Fb?q=x%20y", host="example.invalid:8080", authority="", method="GET"):
    return {"target_base64": b64(target), "authority_base64": b64(authority),
            "headers_base64": [[b64("Host"), b64(host)]] if host is not None else [],
            "method": method, "scheme": "http", "host": "192.0.2.1", "port": 8080}


@pytest.mark.parametrize("changes,expected", [
    ({}, ("example.invalid", "http://example.invalid:8080/a%2Fb?q=x%20y", "/a%2Fb", None)),
    ({"host": "[2001:db8::1]:8443"}, ("2001:db8::1", "http://[2001:db8::1]:8443/a%2Fb?q=x%20y", "/a%2Fb", None)),
    ({"host": None, "authority": "h2.invalid:81"}, ("h2.invalid", "http://h2.invalid:81/a%2Fb?q=x%20y", "/a%2Fb", None)),
    ({"host": "other.invalid", "target": "https://absolute.invalid/A%2fB?q=1"},
     ("absolute.invalid", "https://absolute.invalid/A%2fB?q=1", "/A%2fB", "host_authority_conflict")),
    ({"method": "CONNECT", "host": "[2001:db8::2]:443", "target": "[2001:db8::2]:443"},
     ("2001:db8::2", "http://[2001:db8::2]:443", "[2001:db8::2]:443", None)),
    ({"host": None}, ("", "/a%2Fb?q=x%20y", "/a%2Fb", "request_authority_missing")),
    ({"host": "bad.invalid:xxx"}, ("", "/a%2Fb?q=x%20y", "/a%2Fb", "invalid_request_authority")),
])
def test_address_forms_preserve_original(changes, expected):
    value = request(**changes)
    original = deepcopy(value)
    result = traffic_request_address(value)
    assert tuple(result[key] for key in ("host", "request_url", "request_path", "address_issue")) == expected
    assert result["connection_host"] == "192.0.2.1"
    assert value == original


def test_conflicting_host_headers_do_not_silently_choose_a_domain():
    value = request()
    value["headers_base64"].append([b64("hOsT"), b64("other.invalid")])
    result = traffic_request_address(value)
    assert result["host"] == ""
    assert result["request_url"] == "/a%2Fb?q=x%20y"
    assert result["address_issue"] == "multiple_host_headers"
