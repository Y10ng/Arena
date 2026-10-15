"""默认数据库配置与种子数据。"""

from __future__ import annotations

from pathlib import Path

FACT_PROVENANCE_BACKFILL_COUNTER = "migration_fact_provenance_backfilled"
KNOWLEDGE_BASE_ENV_VAR = "CAIRN_KNOWLEDGE_ROOT"
DEFAULT_KNOWLEDGE_ROOT = Path("datas/cairn/knowledge")

DEFAULT_COPY_V2_ORIGIN_TEMPLATE = """已获得授权，对以下系统开展渗透测试。

目标系统 URL：

普通用户测试账号（没有可留空）：
- 用户名：
- 密码：

管理员测试账号（没有可留空）：
- 用户名：
- 密码：

补充已知信息（可留空）：
- 登录入口、技术栈、环境限制或其他已知事实："""

DEFAULT_REPORT_TEMPLATES: tuple[dict[str, str], ...] = (
    {
        "key": "standard-pentest",
        "name": "标准渗透测试报告",
        "description": "适用于常规安全测试交付，兼顾管理摘要与技术复现。",
        "markdown": """# {{ report_title }}

## 1. 执行摘要

## 2. 测试范围与方法

## 3. 完成依据与覆盖情况

## 4. 漏洞成果汇总

## 5. 漏洞详情

每个漏洞必须包含漏洞 ID、风险等级、影响、复现步骤、验证状态、证据引用和修复建议。

## 6. 整体修复建议

## 7. 测试限制与剩余风险

## 附录：证据索引
""",
    },
    {
        "key": "management-summary",
        "name": "管理汇报版",
        "description": "突出总体风险、业务影响和修复优先级，保留必要技术依据。",
        "markdown": """# {{ report_title }}

## 1. 管理层摘要

## 2. 测试范围与总体结论

## 3. 风险分布与漏洞汇总

## 4. 重点漏洞与业务影响

每个重点漏洞必须保留漏洞 ID、验证结论和证据定位。

## 5. 修复优先级与治理建议

## 6. 测试限制与剩余风险

## 附录：技术成果索引
""",
    },
    {
        "key": "full-technical",
        "name": "完整技术版",
        "description": "包含完整复现、Observation、Verifier、Coverage 与证据索引。",
        "markdown": """# {{ report_title }}

## 1. 执行摘要

## 2. 授权范围、目标与测试方法

## 3. Surface Manifest 与 Coverage 完成依据

## 4. 漏洞成果汇总

## 5. 漏洞详细复现与影响

每个漏洞必须包含漏洞 ID、前置条件、请求或命令步骤、预期与实际结果、影响、Observation、Verifier 状态、证据引用和修复建议。

## 6. 漏洞链候选

候选关系不得描述为已确认攻击链。

## 7. 已测试未发现、证伪与受阻项

## 8. 剩余风险与修复验证建议

## 附录 A：Evidence 索引

## 附录 B：TaskRun 与验证链索引
""",
    },
)


DEFAULT_COPY_V2_GOAL_TEMPLATE = """在授权范围内对目标系统完成系统化渗透测试，并形成可复现结论。

测试要求：
1. 枚举公开攻击面；在对应凭据可用时，分别覆盖未登录、普通用户和管理员身份可见的功能。
2. 验证认证、授权、会话、输入处理、业务流程、文件处理和服务配置等关键风险。
3. 对发现的问题使用最小必要操作复现，记录关键请求、响应、受影响对象和前置条件。
4. 区分已确认漏洞、未证实线索和受阻项，避免将猜测或无实际影响的问题作为漏洞结论。

完成条件：
- 已识别的关键攻击面均完成至少一轮适用测试；
- 已确认漏洞具备可复现证据、影响说明和修复建议；
- 输出剩余风险、覆盖缺口与受阻原因。"""

DEFAULT_SCENARIOS: tuple[dict[str, object], ...] = (
    {
        "key": "goal-driven-v1",
        "label": "目标驱动探索",
        "is_default": True,
        "profile_id": "default",
        "image": "",
        "prompt_group": "goal-driven-v1",
        "knowledge": None,
        "cap_add": ["NET_RAW"],
        "features": {"sandbox": True},
    },
)
