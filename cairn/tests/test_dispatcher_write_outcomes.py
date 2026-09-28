"""Dispatcher 写结果分类必须区分同请求重放与重新执行模型。"""

from __future__ import annotations

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.tasks.write_outcomes import (
    classify_write_response,
    response_detail_code,
)


@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_success_is_accepted_without_replay(status):
    disposition = classify_write_response(ApiResult(status))

    assert disposition.disposition == "accepted"
    assert disposition.error_type is None
    assert disposition.retry_same_write is False


@pytest.mark.parametrize("status", [0, 408, 429, 500, 503, 599])
def test_only_transient_transport_or_server_failures_allow_same_write_replay(status):
    disposition = classify_write_response(ApiResult(status))

    assert disposition.disposition == "transient"
    assert disposition.error_type == "server_write_failed"
    assert disposition.retry_same_write is True


@pytest.mark.parametrize("status", [401, 403])
def test_auth_rejection_is_policy_denied(status):
    disposition = classify_write_response(ApiResult(status))

    assert disposition.disposition == "policy_denied"
    assert disposition.error_type == "policy_denied"
    assert disposition.retry_same_write is False


@pytest.mark.parametrize("status", [400, 404, 405, 409, 412, 422])
def test_deterministic_client_rejection_uses_call_site_classification(status):
    default = classify_write_response(ApiResult(status))
    contract = classify_write_response(ApiResult(status), client_error="request_contract")

    assert default.error_type == "business_acceptance"
    assert contract.error_type == "request_contract"
    assert default.retry_same_write is False
    assert contract.retry_same_write is False


@pytest.mark.parametrize("detail", [
    "execution_generation_mismatch: expected current generation",
    {"code": "lease_mismatch", "message": "private"},
])
def test_allowlisted_generation_or_claim_conflict_is_stale_business_state(detail):
    disposition = classify_write_response(ApiResult(409, data={"detail": detail}))

    assert disposition.disposition == "stale_business_state"
    assert disposition.error_type == "stale_business_state"
    assert disposition.retry_same_write is False


def test_allowlisted_missing_authoritative_resource_is_stale_business_state():
    disposition = classify_write_response(
        ApiResult(404, data={"detail": "vulnerability_observation_not_found"})
    )

    assert disposition.disposition == "stale_business_state"
    assert disposition.error_type == "stale_business_state"
    assert disposition.retry_same_write is False


@pytest.mark.parametrize("detail", [
    "execution_event_receipt_conflict",
    {"code": "submission_conflict"},
])
def test_allowlisted_digest_conflict_is_receipt_conflict(detail):
    disposition = classify_write_response(ApiResult(409, data={"detail": detail}))

    assert disposition.disposition == "receipt_conflict"
    assert disposition.error_type == "receipt_conflict"
    assert disposition.retry_same_write is False


@pytest.mark.parametrize("detail", [
    "private message with spaces",
    "x" * 129,
    {"message": "private"},
    ["not", "a", "code"],
])
def test_uncontrolled_server_detail_is_not_exposed_as_safe_code(detail):
    response = ApiResult(409, data={"detail": detail}, text="PRIVATE_SERVER_CANARY")

    assert response_detail_code(response) is None
    assert classify_write_response(response).safe_code is None
