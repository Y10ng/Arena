from __future__ import annotations

import pytest

from cairn.dispatcher.tasks.report import _report_completeness_error
from test_report_frozen_rendering import _data as complete_report


def test_report_completeness_accepts_all_authoritative_vulnerabilities() -> None:
    assert _report_completeness_error(
        complete_report("v1", "v2"), id_to_title={"v1": "IDOR", "v2": "SQL 注入"},
    ) is None


def test_report_completeness_rejects_missing_vulnerability_before_writes() -> None:
    assert _report_completeness_error(
        complete_report("v1"), id_to_title={"v1": "IDOR", "v2": "SQL 注入"},
    ) == "report_vulnerability_missing:v2"


def test_zero_findings_does_not_require_model_to_generate_authoritative_statement() -> None:
    assert _report_completeness_error(complete_report(), id_to_title={}) is None


@pytest.mark.parametrize(("ids", "error"), [
    (("v1", "v1"), "report_vulnerability_duplicate:v1"),
    (("IDOR",), "report_vulnerability_unknown:IDOR"),
    (("unknown",), "report_vulnerability_unknown:unknown"),
])
def test_report_completeness_uses_exact_ids_not_titles(ids, error: str) -> None:
    assert _report_completeness_error(complete_report(*ids), id_to_title={"v1": "IDOR"}) == error


def test_legacy_markdown_with_all_keywords_is_not_a_valid_report() -> None:
    markdown = "# 执行摘要 测试范围 覆盖 漏洞 修复 剩余风险 证据 v1 " * 100
    assert _report_completeness_error(markdown, id_to_title={"v1": "IDOR"}) == "report_data_invalid"
