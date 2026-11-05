# 环境介绍
* 当前环境是用于 Web 渗透测试的精简 Kali 容器，工具范围见下文；不假定 Kali 全套工具、AD/云/移动工具或额外 MCP 已安装。
* 当前目录是工作空间，可以用于保存一些命令执行日志，较大的扫描结果等

# pi SDK 执行与结果提交
* 所有生产阶段使用同一 pi SDK Agent；本说明由 runner 显式加载，不依赖旧 CLI 或自动发现配置。
* 调用当前阶段的 `submit_result` 工具，参数为 `{accepted:true,data:{...}}`，无法提交有效结果时使用 `{accepted:false,reason:"明确原因"}`。以工具提供的 Schema 为准，不用末尾 raw JSON 或 Markdown 信封代替工具调用。
* runtime 注入 protocol、结果 kind、项目/TaskRun、绑定 Cell、Observation 和 depth review digest；不要手抄这些权威身份。Reason 的来源 Fact、所选 Cell/策略和证据引用仍需明确选择，程序不会猜测。
* HTTP 由测试网络自动记录，目标请求仍按需使用 Bash/curl、脚本或浏览器。需要引用时用 `list_http_flows` 查询实际 flow，再用 `read_http_flow` 按需读取；不为取证重发请求，不按最近工具或时间窗口猜归属，未采到不得补造。默认查询本项目当前 TaskRun；`historical:true` 显式读取同项目已有流量，保留其源执行。其他新证据先保存为工作空间已有文件，再调用 `register_artifact`，提供绝对 `path` 和 `kind`，使用返回 handle 引用。不要手写 `data.artifacts`、猜 handle 或将远端路径冒充本地文件；登记不等于上传、持久化或任务成功。
* 新发现按工具 Schema 使用 `schema_version:2`，独立外部复测结果使用 `schema_version:3`；两者使用有序 `evidence_items`，保留 request/response 同条关联及真实缺侧。新 HTTP 条目只填真实 `flow_id`，不同时填写 request/response 引用或 inline 报文；本次实际流量使用 `source:capture`，同项目以前 TaskRun 的材料使用 `source:historical`，历史成功响应不代表本次复现。旧 Artifact 和非 HTTP 证据仍用 `artifact_handle` 或 `artifact_id` 之一。有漏洞的 Fact 和外部复测不另填 `evidence_refs`。复测的中文判断依据 `key_evidence` 和实际步骤 `test_steps` 通过 `evidence_item_ids` 引用本结果条目的原始 ID，不能用 handle 代替，不再填写 `notes`。
* 发现直接供读者阅读：`fact.description` 先写缺陷、必要条件、实际结果和影响边界，区分已证实与未证实范围；写入／覆盖／截断等副作用须在复现前显著说明，优先使用获授权的可丢弃样本。`vulnerability.reproduction_steps` 只保留必要主链及对照，以 `action/observation/evidence_item_ids` 保留参数来源、步骤依赖、实际观察和本发现证据条目 ID（不是 handle）；研究旁支留在既有事实／证据材料，相关限制不可省略。发现过程中的验证不称为平台独立复测。`vulnerability.remediation` 针对实际缺陷，不虚构代码位置或已修复状态。不设字数、步数或每步 HTTP 门槛，仍使用发现 `schema_version:2`。
* `list_http_flows` 每页最多 50 条摘要，不返回全部头或正文；`read_http_flow` 的 `side:metadata` 返回来源和同步状态，`side:request/response` 按 `offset/limit` 返回最多 32768 UTF-8 字节的服务端 `http-evidence-v1` 投影。receiving 只返回明确标注的 `body-preview-base64` 已同步 body 前缀，偏移是 body 字节位置，`excerpt_eligible:false`，不作为稳定投影选段。终态投影是结构化重建而非原始网络字节；保留 missing/receiving/partial/failed，不把分页片段当作全包。flow 选段使用返回的 `side/pointer/representation/sha256/revision`，不可使用 Pretty/Hex 屏幕坐标或把二进制占位当正文证据；引用与步骤仍使用证据条目 ID。按需读取，不规定每次请求后的固定留证动作。报告只读冻结 Packet，不查询 live flow。
* 短采集包无需 `excerpts` 即可完整显示；长包只选与结论相关的关键原文。以下 `read_evidence` 偏移规则用于历史 Artifact／文件，不代替 flow 投影身份。`read_evidence` 按 UTF-8 字节 `offset/length` 读取，每次返回 `text/byte_start/bytes_read/next_offset/total_bytes/eof/pointer`；`match` 可用非空、精确原文定位，返回完整匹配文本与工具计算的 `utf8-bytes=START:END` 半开范围，无需手算字符或字节边界。无匹配或重复匹配会明确失败，可用更完整原文或 `offset` 限定查找起点；只查指定文件，匹配文件限 32 MiB，文本回执不超过 `length`（默认／最多 32768 字节）。读取已登记原件的返回 `path`，在对应 `evidence_items` 上添加 `excerpts` 列表，每项仅填实际一侧 `side`（`request`、`response` 或 `artifact`）与工具返回的 `pointer`；原件引用仍使用原登记引用，不复制 SHA 或文本。多个不连续片段分别保留，不拼成完整包；保留尾部结果与反证，不自动取前 N 字节或加载所有附件，不替换原件。
* 显式 conclude、仅交付恢复和报告只读取已有证据、登记已有文件和提交，不重新测试目标；正常 execute 的格式修正不改变原工具权限。有效候选固定后结束执行，取消或硬截止后不恢复；运行失败、未执行不能伪造覆盖阴性或验证结果。
* 项目总报告以普通 Markdown 为写作范例，直接形成自由 Markdown 正文，不执行模板槽位。用 `save_report_segment({key,content})` 保存自然内容块，content 为 `{kind:"markdown",markdown:"正文",sources:[{vulnerability_id,observation_ids:[]}]}`。正文包含每条冻结漏洞的完整 number 与真实状态，多个 Observation 保留来源差异。真实原文使用 `[证据](evidence://ObservationID/EvidenceItemID)` 局部引用，由程序校验并插入；不自行编造 HTTP 或图片。最终 `submit_result` 只交 `data:{schema_version:5,segment_refs:[{artifact_handle:...}]}`，不重发整篇、不手填 artifacts。保存不等于接受；同 key 内容不可变，截断只在原期限内缩小未完成单元后有界恢复。只读冻结材料，不访问目标、不补测、不回写发现；样例事实不得变成本项目事实。旧冻结任务按自身 Schema 恢复，不修改历史输入。

# 浏览器自动化（Playwright CLI）
* 使用 `playwright-cli` 操作无头浏览器，适合登录、JavaScript 页面、截图取证和表单操作；不需要 DISPLAY 环境变量。
* Playwright MCP 和 Chrome DevTools MCP 已安装备用，但没有默认启动或自动注册为 pi 工具；仅使用当前会话实际提供的接口，不假定旧 MCP 配置仍然生效。普通命令直接按需调用，不要求为每个工具创建专属脚本或执行一整套扫描。


## Playwright CLI 会话生命周期
* 当平台注入 `PLAYWRIGHT_CLI_SESSION=cairn-task-...` 时，普通临时浏览器直接使用 `playwright-cli open ...`；同一 TaskRun 的执行与总结阶段共用完整页面状态，完整收尾并确认退出后由平台定向关闭。
* `cairn-task-` 是平台保留前缀，不得复制给其他任务或跨任务 tmux 服务。需要长期保留的浏览器必须显式使用非保留名称，例如 `playwright-cli -s=shared-review open ...`，后续命令沿用该名称，并说明使用者与手工关闭责任。
* 已配置的 `PLAYWRIGHT_CLI_SESSION` 不由平台覆盖或自动纳管。显式 `-s`、清除变量、旧 tmux 环境或自行更换 CLI 可能绕过托管；不要用 `close-all`、`kill-all` 或批量杀 Node/Chrome 代替单会话收尾。

# Web 端点发现
* 在预算内优先观察正常业务操作产生的真实请求，尤其是登录后与不同身份可见的操作；结合实际加载的 JS、已发现或提供的接口文档、表单 action/method/字段补充线索。Bootstrap 做有界映射并留下来源，Explore 按当前假设继续发现，不要求启动时递归爬完或每次运行全套工具。
* 区分仅静态线索、已观察请求、已完成正常重放；JS 字符串和猜测路径不等于接口存在。路径发现结合正常与不存在路径的响应对照，不能只凭 HTTP 200 把登录页或统一错误页计作新接口。
* 重要接口保留 method、URL、参数位置、Content-Type、body 结构、身份/角色、对象归属、前置状态、正常响应及来源证据；同路径不同方法不能混为一个操作。认证材料仅通过项目允许的证据与凭据途径使用，不写进普通摘要或报告；记录更新条件，不默认跨 TaskRun 继承登录态。
* 浏览器网络采集应在相关操作前准备，使用 `requests --filter=...` 定位编号，再用 `request N`、`request-body N`、`response-body N` 分别读取详情与正文，并关联触发操作。`request N` 不包含正文，请求列表、资源 URL 和截图不等于完整报文；缺少 headers/body 等字段时如实记录。`response_body_incomplete` 表示请求尚未完成，不是无响应或测试阴性；保留已获得的证据，仅在观察到完成后按预算重试，不循环等待、不修改目标页面强行消费响应。必要的 curl/Python 重放须核对身份、CSRF 与副作用，不能把重放伪称为原浏览器抓包。

# Web 工具与证据
* HTTP/脚本：已有 `curl`、`openssl`、Python 3（requests、bs4、yaml）、Node、`jq`、`rg`、`fd`，按当前问题自由选择。HTTP 自动采集不依赖专用 helper；以实际 flow 及其状态为准，不重复生成 trace→HTTP Artifact。截图或部分网络日志不等于完整 HTTP 报文，超时／失败不等于测试阴性；其他专项原件可保存到工作空间再登记。
* 发现与深测：`dig`/`host`、`nmap`、`ffuf`、`dirsearch`、`katana`、`nuclei`、`sqlmap`、`dalfox`、`jwt_tool`。按当前假设选择工具与有界参数；扫描器告警不是已确认漏洞，正常对照、身份和业务状态验证不能省略。
* Web 小型字典位于 `/opt/cairn/wordlists/web/`：`common.txt`（目录/文件）、`parameters.txt`（参数）、`xss.txt`、`sqli.txt`。它们不是全面覆盖清单；默认工具字典不适用时显式指定所需文件。
* nuclei 模板位于 `/home/kali/.local/nuclei-templates`，离线使用已安装模板，不假设执行时能自动更新。根据协议、技术与假设选择模板，避免无边界全扫。
* `jwt_tool` 首次运行生成当前用户配置后退出 1，须核对初始化提示及 `~/.jwt_tool/jwtconf.ini` 后再执行所需操作；部分成功 action 同样退出 1，必须结合具体成功输出和证据判断，不能只看退出码或泛化忽略失败。
* 缺少专项工具、字典、身份、网络或目标前提时，记录限制/阻塞；命令不存在或执行失败不等于测试阴性。不假定 AD/云/移动工具、整库 PoC 或正在运行的 MCP 服务存在。

# 回调与长期服务
* OOB/回调只能使用项目实际提供且验证可达的地址；容器内监听不代表端口自动向目标开放。没有可达地址时如实记录限制，不编造对外 IP。
* 可用 Python HTTP 服务、`ncat` 和 `tmux` 运行必要的受控接收服务。需要跨阶段保留时使用明确命名的 tmux 会话，并记录用途、端口、归属和停止方法；不清理其他任务的服务。
