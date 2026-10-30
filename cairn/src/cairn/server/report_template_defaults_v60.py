"""migration 60 的固定模板输入；后续默认变更须新增版本，不改写本模块。"""

from __future__ import annotations


DEFAULT_REPORT_TEMPLATE_KEY_V60 = "vulnerability-report"
DEFAULT_REPORT_TEMPLATE_NAME_V60 = "漏洞报告"
DEFAULT_REPORT_TEMPLATE_DESCRIPTION_V60 = "逐条呈现漏洞说明、复现步骤与修复建议，适用于单漏洞和整体报告。"
DEFAULT_REPORT_TEMPLATE_MARKDOWN_V60 = """# {{ report_title }}

{{#findings}}
## {{ finding.title }}

### 漏洞说明

{{ finding.description }}

风险等级：{{ finding.severity }}
影响目标：{{ finding.target }}

{{ finding.limitations }}

### 复现步骤

{{ finding.recorded_steps }}

{{ finding.evidence_note }}

{{#finding.evidence}}
{{ evidence.content }}
{{/finding.evidence}}

### 修复建议

{{ finding.remediation }}
{{/findings}}
"""
