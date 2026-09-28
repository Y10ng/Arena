from __future__ import annotations

import pytest

from cairn.dispatcher.tasks.report import _report_completeness_error


def complete_report(*ids: str) -> dict[str, object]:
    return {
        "executive_summary": "仅解释冻结证据，建议修复权限边界。",
        "findings": [
            {
                "vulnerability_id": vulnerability_id,
                "impact": "已有证据支持的业务影响。",
                "root_cause": "黑盒推断，未验证源码。",
                "repro_steps": "依据冻结证据包说明前提与已有操作。",
                "remediation": "建议在服务端检查对象授权。",
                "regression_steps": "修复后使用不同身份和正常对照验证。",
                "limitations": "回归方案尚未执行。",
            }
            for vulnerability_id in ids
        ],
        "limitations": "报告不是新增测试证据。",
        "artifacts": [],
    }


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
