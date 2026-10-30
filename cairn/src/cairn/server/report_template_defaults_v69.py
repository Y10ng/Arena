"""单漏洞确定性导出专用默认，不改变项目报告或已冻结模板。"""

DEFAULT_SINGLE_REPORT_TEMPLATE_KEY = "single-vulnerability-report"
SINGLE_REPORT_PROFILE = "single-vulnerability-export-v1"
DEFAULT_SINGLE_REPORT_TEMPLATE_MARKDOWN = """# {{ report_title }}

{{#findings}}
## {{ finding.title }}

### 基本信息

{{ finding.basic_information }}

### 漏洞概述

{{ finding.description }}

{{ finding.limitations }}

### 复现步骤

{{ finding.step_evidence }}

### 修复建议

{{ finding.remediation }}
{{/findings}}
"""
