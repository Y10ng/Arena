"""版本化 Worker 结果协议的公共入口。"""

from .envelope import (
    ASSESSMENT_PROTOCOL_ID,
    PHASE_RESULT_KINDS,
    WorkerProtocolError,
    canonical_result_example,
    protocol_json_schema,
    protocol_schema_digest,
    protocol_transport_json_schema,
    validate_result_envelope,
)

__all__ = [
    "ASSESSMENT_PROTOCOL_ID",
    "PHASE_RESULT_KINDS",
    "WorkerProtocolError",
    "canonical_result_example",
    "protocol_json_schema",
    "protocol_schema_digest",
    "protocol_transport_json_schema",
    "validate_result_envelope",
]
