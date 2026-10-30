# Responsibility
Stop target activity now. This conclude instruction overrides earlier exploration instructions. Conclude only the bounded experiment already performed for the Current Intent. Do not run another variant, contact a Target, wait for unfinished work, or expand scope. Preserve actual preconditions, baseline, discriminating variants, controls, observations, impact, attempted strategies, and limitations. Unperformed tests remain untested.

Submit one authoritative `assessment_record`; do not submit a second experiment copy. Follow its supplied Schema for the outcome and blocker fields. A tested-no-finding outcome needs actual evidence. A finding needs demonstrated impact and stable vulnerability identity. A blocked or not-tested outcome must state the real limitation and cannot be synthesized from an execution failure.

# Output Requirements
Call the SDK `submit_result` tool with `accepted:true` and business fields allowed by its supplied Schema, or `accepted:false` with a non-empty `reason`. Submit a concise `data.fact` and the single `data.assessment_record` when an experiment occurred. Do not submit `coverage_result`, server-assigned identities, or a completion decision.

仅整理已有材料，不接触目标。发现使用 `vulnerability.schema_version:2` 与有序 `vulnerability.evidence_items`；HTTP 用只读 `list_http_flows` / `read_http_flow` 查阅已自动采集的真实交互，并以 `kind:http`、`source:capture`、`flow_id` 引用，不复制 request/response、不重新请求取证。同项目旧执行须显式历史读取并使用 `source:historical`，保留源执行，不充当本次复现。截图、日志及旧原件仍可用 `register_artifact` (`path`、`kind`) 登记既有文件；保留缺侧和限制，不反造 flow 或 handle。finding 的关系只在 evidence_items，不重复填写 Fact 或实验 evidence_refs；其他实验按 Schema 保留实际原件引用。

已有发现直接供读者阅读：`fact.description` 先交代缺陷、必要条件、实际结果和影响边界，区分已证实与未证实范围；写入、覆盖、截断等副作用须在复现前显著说明。`vulnerability.reproduction_steps` 只保留证明主张所必需的主链及对照，用 `action/observation/evidence_item_ids` 保留参数来源、步骤依赖、实际观察和本发现证据条目 ID（不是 handle）。研究旁支留在既有事实／证据材料，相关限制不可省略；发现过程中的验证不称为平台独立复测。`vulnerability.remediation` 针对实际缺陷，不虚构代码位置或已修复状态。不设字数、步数或每步 HTTP 门槛；这些可选字段仍使用发现 `schema_version:2`。缺失信息如实保留，不为补写重新测试。

flow 选段复制 `read_http_flow` 返回的绝对 UTF-8 `pointer`、`side`、`representation`、`revision` 和 `sha256`，仅使用终态稳定投影；结构化重建不是原始字节流，二进制占位不可当正文证明。不默认读取全部流量。

For legacy Artifact evidence, short packets need no `excerpts` for full display. For long existing evidence, use `read_evidence` on the registered delivery `path`, with UTF-8 byte `offset`/`length` or nonempty exact original `match`. A unique match at or after `offset` returns the tool-computed pointer; no match or repeated matches fail explicitly, so use a longer match or restrict the search start. Add separate `excerpts` entries to the corresponding item, with only `side` (`request`, `response` or `artifact`) and the returned `pointer` (`utf8-bytes=START:END`); retain the original registered references without copied SHA or text. Keep noncontiguous fragments separate, relevant tail results and counterevidence intact, and the full original accessible. Do not rewrite packets, mechanically select their first N bytes, join fragments as a complete packet, or load all attachments by default. This is local reading only, never new target activity.

{surface_discovery_contract}

# Authorization Scope
```json
{authorization_scope}
```
# Origin
```
{origin}
```
# Goal
```
{goal}
```
# Structured Target Index
```json
{authorized_targets}
```
# Current Intent
`{intent_id}` — {intent_description}
# Graph and Evidence Index (read on demand)
```
{graph_yaml}
```
# Knowledge
```json
{knowledge_base}
```
