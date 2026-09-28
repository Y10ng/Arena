# 环境介绍
* 当前环境是用于 Web 渗透测试的精简 Kali 容器，工具范围见下文；不假定 Kali 全套工具、AD/云/移动工具或额外 MCP 已安装。
* 当前目录是工作空间，可以用于保存一些命令执行日志，较大的扫描结果等

# pi SDK 执行与结果提交
* 所有生产阶段使用同一 pi SDK Agent；本说明由 runner 显式加载，不依赖旧 CLI 或自动发现配置。
* 调用当前阶段的 `submit_result` 工具，参数为 `{accepted:true,data:{...}}`，无法提交有效结果时使用 `{accepted:false,reason:"明确原因"}`。以工具提供的 Schema 为准，不用末尾 raw JSON 或 Markdown 信封代替工具调用。
* runtime 注入 protocol、结果 kind、项目/TaskRun、绑定 Cell、Observation 和 depth review digest；不要手抄这些权威身份。Reason 的来源 Fact、所选 Cell/策略和证据引用仍需明确选择，程序不会猜测。
* 新证据先保存为当前工作空间已有的本地文件，再调用 `register_artifact`，提供绝对 `path` 和 `kind`，使用返回 handle 引用证据。不要手写 `data.artifacts`、猜 handle 或将远端路径冒充本地文件；登记不等于上传、持久化或任务成功。
* conclude/结果修正阶段停止目标测试与等待，只读取已有证据、登记已有文件和重新提交。取消或硬截止后不恢复执行；运行失败、预算未执行不能伪造覆盖阴性或验证结果。

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
* HTTP/脚本：`curl`、`openssl`、Python 3（requests、bs4、yaml）、Node、`jq`、`rg`、`fd`。请求、响应和截图保存在工作空间，再登记 Artifact；截图或部分网络日志不等于完整 HTTP 报文。
* 发现与深测：`dig`/`host`、`nmap`、`ffuf`、`dirsearch`、`katana`、`nuclei`、`sqlmap`、`dalfox`、`jwt_tool`。按当前假设选择工具与有界参数；扫描器告警不是已确认漏洞，正常对照、身份和业务状态验证不能省略。
* Web 小型字典位于 `/opt/cairn/wordlists/web/`：`common.txt`（目录/文件）、`parameters.txt`（参数）、`xss.txt`、`sqli.txt`。它们不是全面覆盖清单；默认工具字典不适用时显式指定所需文件。
* nuclei 模板位于 `/home/kali/.local/nuclei-templates`，离线使用已安装模板，不假设执行时能自动更新。根据协议、技术与假设选择模板，避免无边界全扫。
* `jwt_tool` 首次运行生成当前用户配置后退出 1，须核对初始化提示及 `~/.jwt_tool/jwtconf.ini` 后再执行所需操作；部分成功 action 同样退出 1，必须结合具体成功输出和证据判断，不能只看退出码或泛化忽略失败。
* 缺少专项工具、字典、身份、网络或目标前提时，记录限制/阻塞；命令不存在或执行失败不等于测试阴性。不假定 AD/云/移动工具、整库 PoC 或正在运行的 MCP 服务存在。

# 回调与长期服务
* OOB/回调只能使用项目实际提供且验证可达的地址；容器内监听不代表端口自动向目标开放。没有可达地址时如实记录限制，不编造对外 IP。
* 可用 Python HTTP 服务、`ncat` 和 `tmux` 运行必要的受控接收服务。需要跨阶段保留时使用明确命名的 tmux 会话，并记录用途、端口、归属和停止方法；不清理其他任务的服务。
