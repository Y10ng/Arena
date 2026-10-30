"""migration 62 的固定默认正文：交付关键内容，完整原件仍由显式槽位读取。"""

from __future__ import annotations


DEFAULT_REPORT_TEMPLATE_MARKDOWN_V62 = """# {{ report_title }}

{{#findings}}
## {{ finding.title }}

### 漏洞说明

{{ finding.description }}

风险等级：{{ finding.severity }}
影响目标：{{ finding.target }}

{{ finding.limitations }}

### 复现步骤

{{ finding.recorded_steps }}

{{ finding.key_evidence }}

### 修复建议

{{ finding.remediation }}
{{/findings}}
"""
