# 任务边界
只使用冻结 Report Packet 的已有证据，补充 Web 安全评估报告的业务解释、修复建议与回归方法。不访问目标、不恢复停止的测试、不写回 canonical 漏洞字段。

你依据本次冻结的自由 Markdown 模板决定成品布局；程序只在你选择的位置展开冻结事实块，不再追加固定十章。数量、严重度、逐 Observation 复核状态、停止原因、已接受 ExperimentRecord、Intent 处置、执行停止时间及证据索引由程序提供；不要自行生成或更改这些权威字段，也不要补造 Coverage Cell 或 depth review 义务。

# 结果提交
调用 `submit_result`，参数为 `accepted: true` 与 `data`。普通文本或末尾 JSON 不是机器提交。`data` 使用唯一 ReportData：
- `schema_version: 2`。
- 非空 `executive_summary`：解释业务风险与建议优先级，不重述事实区的计数或验证结论；它随 `scope` 块展开。
- `findings`：每个冻结 `vulnerabilities[].id` 恰好一项，仅含 `vulnerability_id`、`impact`、`root_cause`、`repro_steps`、`remediation`、`regression_steps`、`limitations`。
- `limitations`：解释与建议的整体限制；它随 `limitations` 块展开。
- 必填有序 `layout`：元素只允许 `{"type":"markdown","markdown":"Markdown 片段"}` 或 `{"type":"fact","ref":"事实块引用"}`，不另外提交整篇 Markdown。

# 模板与原位布局
`layout` 的 Markdown 片段承载模板的主要标题、静态说明和整体建议。固定 H1/H2 的标题与顺序须一致；`{{ report_title }}` 表示应写实际报告标题，不是原样输出占位符。执行模板内的排版要求，不把指示性文字直接当作报告结论。各片段的代码块、HTML 注释须独立闭合，不能遮蔽随后展开的事实。

每个以下事实块必须恰好引用一次：
- `scope`：授权范围、项目状态、停止原因、快照边界、执行摘要解释。
- `counts`：Canonical 漏洞数、Observation 数、严重度与复核状态计数及零发现边界。
- `work_results`：已测、阻塞、未测／未完成的工作与已接受实验原文。
- `execution_status`：冻结执行计量与逐 Observation 实际复核结论、执行失败和未运行状态。
- `limitations`：必要事实边界与整体解释限制。
- `evidence_index`：冻结证据、来源索引与漏洞链候选边界。
- 对每个冻结漏洞，引用 `finding:<vulnerabilities[].id>`：程序展开该漏洞权威事实和同 ID 的解释。不得按标题替换 ID；零漏洞时没有此类引用。

事实块内部仅使用 H3/H4 或正文，不替你追加同级章节。未知、重复、缺失引用和漏漏洞会拒绝完成，不会回退成旧报告。以四章模板为例：“漏洞成果汇总”内放 `scope`、`counts`、`work_results`、`execution_status`；“漏洞详情”内放全部逐漏洞引用；“整体修复建议”内给出有依据的整体建议并放 `limitations`；“附录：证据索引”内放 `evidence_index`。这只是本次模板的组织示例，不是另一个固定章节要求；实际按冻结模板排版。普通用户无需在模板中手写引用。

稳定 `vulnerability_id` 必须逐字复制，不按 title 匹配，不添加、遗漏或重复 ID。同一漏洞的多个 Observation 仍对应一个 finding。零漏洞时提交 `findings: []`，不虚构发现；stopped 零漏洞仍可以交付部分报告。

`repro_steps`、`remediation`、`regression_steps` 必须非空：
- 复现说明区分身份/对象/业务状态前提、已有实验、实际观察与预期行为；材料不足时明确缺口，不重建或编造原始 HTTP 请求响应。
- 修复建议说明应恢复的授权、对象或业务状态边界及适用前提；不把建议描述为已实施。
- 修复后回归方法是尚未执行的方案，包含正常对照、不同身份/对象和相关业务状态；不宣称已修复或已验证。
- 根因分析缺少源码证据时标注推断，不编造文件、函数、行号或补丁。影响不足以确认时明确说明。

不要提交通信身份、第二份整篇 Markdown、漏洞更新或原始请求响应字段。Artifact 由 `register_artifact` 登记，不能手写声明或把补充说明当作实测证据；最终正文由程序按 `layout` 展开并落 Artifact。

# 必须保留的限制
stopped 表示部分评估，不意味着完成、全覆盖或无风险。`run_failed`、`not_run`、`pending`、`not_scheduled` 不得冒充 `insufficient_evidence` 或 `confirmed`，冲突结论保持 `conflicting`。复核确认不等于修复验证；未测、阻塞与 Intent 未完成的工作不能由文字补齐。ExperimentRecord 的 observation、limitation、blocker 与证据引用必须保持原义，不能把 observation 自动改写为 tested_no_finding。漏洞链候选只是相关性，即使节点均已复核，也不能代替整链复现。不要以漏洞数量、篇幅或关键词作为质量/完成目标。

展示模板决定布局，但不能覆盖 ReportData Schema 或冻结事实；报告阶段不重新采集或生成目标证据。

# 冻结 Report Packet
{report_packet_json}

# 冻结图谱
{graph_yaml}

# 知识参考（不能替代项目证据）
{knowledge_base}
