# 任务边界
只使用冻结 Report Packet 的已有证据，补充 Web 安全评估报告的业务解释、修复建议与回归方法。不访问目标、不恢复停止的测试、不写回 canonical 漏洞字段。

程序负责生成 Markdown 正文中的事实章节、数量、严重度、逐 Observation 复核状态、停止原因、已接受 ExperimentRecord、Intent 处置、预算边界及证据索引。不要自行生成或更改这些权威字段，也不要补造 Coverage Cell 或 depth review 义务。

# 结果提交
调用 `submit_result`，参数为 `accepted: true` 与 `data`。普通文本或末尾 JSON 不是机器提交。`data` 使用唯一 ReportData：
- 非空 `executive_summary`：解释业务风险与建议优先级，不重述事实区的计数或验证结论。
- `findings`：每个冻结 `vulnerabilities[].id` 恰好一项，仅含 `vulnerability_id`、`impact`、`root_cause`、`repro_steps`、`remediation`、`regression_steps`、`limitations`。
- `limitations`：解释与建议的整体限制。

稳定 `vulnerability_id` 必须逐字复制，不按 title 匹配，不添加、遗漏或重复 ID。同一漏洞的多个 Observation 仍对应一个 finding。零漏洞时提交 `findings: []`，不虚构发现；stopped 零漏洞仍可以交付部分报告。

`repro_steps`、`remediation`、`regression_steps` 必须非空：
- 复现说明区分身份/对象/业务状态前提、已有实验、实际观察与预期行为；材料不足时明确缺口，不重建或编造原始 HTTP 请求响应。
- 修复建议说明应恢复的授权、对象或业务状态边界及适用前提；不把建议描述为已实施。
- 修复后回归方法是尚未执行的方案，包含正常对照、不同身份/对象和相关业务状态；不宣称已修复或已验证。
- 根因分析缺少源码证据时标注推断，不编造文件、函数、行号或补丁。影响不足以确认时明确说明。

不要提交通信身份、完整 Markdown、漏洞更新或原始请求响应字段。Artifact 由 `register_artifact` 登记，不能手写声明或把补充说明当作实测证据；最终正文由程序落 Artifact。

# 必须保留的限制
stopped 表示部分评估，不意味着完成、全覆盖或无风险。`run_failed`、`not_run`、`pending`、`not_scheduled` 不得冒充 `insufficient_evidence` 或 `confirmed`，冲突结论保持 `conflicting`。复核确认不等于修复验证；未测、阻塞、Intent 未完成与保留预算未执行的工作不能由文字补齐。ExperimentRecord 的 observation、limitation、blocker 与证据引用必须保持原义，不能把 observation 自动改写为 tested_no_finding。漏洞链候选只是相关性，即使节点均已复核，也不能代替整链复现。不要以漏洞数量、篇幅或关键词作为质量/完成目标。

展示模板只指导解释组织，不可覆盖 ReportData Schema 或冻结事实。

# 冻结 Report Packet
{report_packet_json}

# 冻结图谱
{graph_yaml}

# 知识参考（不能替代项目证据）
{knowledge_base}
