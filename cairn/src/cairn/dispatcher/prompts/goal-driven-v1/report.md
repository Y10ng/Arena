# 任务边界
只整理冻结 Report Packet 的已有材料。不访问目标、不补测、不写回 Fact、Observation 或 canonical 漏洞。冻结模板决定章节；模型负责有依据的解释和综合，程序负责身份、证据校验、搬运与排版。项目总报告不依赖单漏洞模型任务。

# 分段保存与最终提交
不要在最后一次工具调用输出全项目正文。使用 `save_report_segment` 逐段保存，每次只提交一个有界的内容单元，成功后只保留返回的 `key` 与 `artifact_handle` 用于最终引用；不要在下一段重新输出已保存正文。

工具参数为 `key`（本次执行内唯一）和 `content`：
- 项目综合段：`{kind: "executive_summary" | "overall_remediation" | "limitations", text: "..."}`。综合结论和整体建议各至少一段；长内容可分自然段分别保存。
- 漏洞段：`{kind: "finding", vulnerability_id, observation_id, description?, steps?, remediation?, limitations?, reuse_source?}`。一个段可以包含几个短字段，也可以只写一组自然段或几个完整步骤。同一漏洞的各段必须使用同一个 Observation，程序按最终清单顺序连接文字段落、追加步骤。
- 已完整的内容使用 `reuse_source: ["description", "steps", "remediation"]` 明确复用，也可只选部分字段；复用字段不重复提交正文。description 来自所选 Observation 的 source_fact_id 指向的冻结 Fact，steps 原样使用该 Observation 的 reproduction_steps，remediation 来自其冻结 remediation；程序解析步骤证据、已有选段及短原件，无需为搬运内容重算字节。没有 Observation 时仅可复用漏洞自身说明／建议，缺失来源不能猜补。
- 每条冻结漏洞最终须有非空 description 和 remediation，并明确提供或复用 steps（材料不足可显式 []，同时说明限制）。limitations 保留必要边界；长原件无选段时由程序明确缺口和定位，不充当已有关键证明。
- 同一 key 重放必须内容相同；有冲突时不能覆盖旧段。工具保存成功不等于报告已接受。保存失败或截断的当前段不计为完成，不能伪造引用。

全部内容保存后，`submit_result` 只提交 `accepted: true` 与 `data: {schema_version: 5, segment_refs: [{artifact_handle: "工具返回的身份"}, ...]}`。不要携带 findings、整篇 Markdown、大文件内容、原件或手抄 Artifact 元数据。清单顺序决定各字段内部段落／步骤顺序。每个引用只能出现一次；缺项、错误归属及不完整材料都会拒绝。

不是每条漏洞强制一次模型调用。已有清楚内容可明确复用；较长漏洞按已有内容结构拆段，不机械裁剪操作或报文。输出达到上限时，只能在原期限内缩小尚未完成的当前单元后有界恢复，不重做全部漏洞，不拼接或补全半截 JSON。

# 内容和证据
- description 解释哪个功能／对象、什么条件、发生什么问题与证据支持的实际影响。有依据才解释根因，不复制运行日志、内部身份或验证 notes，不把可能影响写成已验证。
- 需要改写主链时，steps 每项为 `operation`、`observed`、`evidence`；保留必要参数来源、依赖和实际观察，不新增执行或混入研究旁支。每个 evidence 为 `evidence_item_id` 和非空 selections；selection 仅含 `side`（request/response/artifact）、`artifact_id`、`byte_start`、`byte_end`，为原件 UTF-8 字节半开区间。仅在阅读确有需要时选择新选段，原件 SHA 和大小仍由程序解析。
- remediation 提供针对性整改及必要验证建议，明确尚未实施／验证。limitations 保留缺侧、反证、冲突及尚不能确认的事项，不能因只选关键包忽略 supporting 中的相反证据。

vulnerability_id、observation_id 与证据条目身份逐字引用。每个冻结漏洞恰好形成一份最终表达，不跨 Observation 拼接交互。仅重放切换前冻结任务时，accepted_readable_findings 中的历史成果由程序按原合同复用，不重复提交；零漏洞仅保存综合结论与建议，不能声称不存在漏洞。

# 冻结材料读取
先用 read_evidence 读取默认冻结工作索引，选择相关交互，再沿 artifact_reads 路径按需读取原件；完整冻结索引由 frozen_materials_path 提供。其他相关材料和图谱仍来自同一冻结代际，不读取可变实时项目。offset/length 与选段均为原件字节，可从已知范围、尾部和反证读取，不能只看开头就猜结论，不把全部附件目录和历史执行塞入上下文。冻结只读输入不重新 register_artifact，也不制作交付文件。

程序校验每条证据属于所选 Observation、side 对应同一次交互、项目归属、摘要、越界、UTF-8 边界及交付权限，再从冻结原件取实际片段。不把不连续内容拼成完整 HTTP 包，不补缺侧，不回退全文附录。

解释、操作／观察、建议和限制可使用短段落、列表和强调，不另造大标题、封面、链接、HTML 或代码块；关键代码块由程序提取原件后生成，模板示例不是事实。

# 必须保留的边界
stopped 是部分评估，不代表完成、全覆盖或无风险。未测、受阻、失败、未执行与待确认不能改写为已确认或阴性；冲突仍是冲突。已有材料不能支持的根因、源码位置和关联链不得补造。报告完成不代表重新验证漏洞或验证修复。

# 冻结 Report Packet 索引
{report_packet_json}

# 冻结图谱
{graph_yaml}

# 知识参考（不能替代项目证据）
{knowledge_base}
