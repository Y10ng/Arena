# Worker 容器

统一执行入口为 `cairn-agent --request FILE`，使用独立的 [pi SDK 子项目](pi-agent/README.md)。旧 Claude Code、Codex、全局 pi CLI 不再安装或注入专属配置。默认镜像聚焦 Web 发现、深测与取证，保留 Playwright CLI/Chromium；Playwright MCP 和 Chrome DevTools MCP 预装备选但不默认启动，不安装 Kali 全套工具元包。

默认本地镜像为 `arena-worker:local`，从仓库根目录构建，context 为 `container/`：

```bash
docker build -t arena-worker:local container
```

镜像名可自定义，在 `runtime/dispatch.yaml` 中设置 `container.image`；默认 `images: []` 会引用该镜像。已有显式 `images[]` 时，实际执行由画像的 `image_ref` 指向注册项的 `image`，按使用的映射修改即可。

完整应用安装、使用已有镜像、端口配置与更新统一见[部署指南](../docs/deployment-linux-zh.md)。`start.sh` 仅在默认镜像不存在时自动构建，不会因源码变化重建已有同名镜像；源码更新后须显式重新构建。**新部署包的 `deploy/update.sh --target OLD` 会在 install 后、start 及数据库迁移前，用新版代码带缓存构建配置中引用的默认本地镜像**，候选通过标签和无网络应用自检后才更新 tag。自定义引用保持不变并提示自行配套更新，缺失或无效引用明确失败；`-- --server-only` 明确跳过 Worker，不代表配套升级完成。部署 ZIP 仍不含 Worker 镜像或多阶段构建缓存。

Dockerfile 从固定 Node 24.16.0 分发层提供二进制。pi builder 与最终运行镜像继承相同的 Kali/Node 基础层，builder 内执行 `npm ci`、TypeScript 编译和离线 SDK 自检，再复制生产依赖和 `dist`；不复制宿主 `node_modules`。与应用无关的工具 / MCP 安装、补丁和用户初始化均位于应用复制前，`node_modules` 在 `dist` 前，应用复制后仍执行自检。基础镜像 / 架构、安装输入一致且缓存有效时，源码 / 指引变化不会触发未变工具的安装，lock 不变则复用 `npm ci`；首次跨版、层重排、历史 timeout 变化或缺缓存仍可能下载，不保证旧机器首次零下载。`.dockerignore` 排除宿主依赖、构建输出、运行目录与旧 CLI 配置。`AGENTS.md` 靠后复制到 `/opt/cairn/AGENTS.md`，由 runner 显式加载，不依赖 CLAUDE.md 自动发现。

离线自检命令为 `cairn-agent --version`、`cairn-agent --self-check`。Dispatcher readiness 只执行 `--self-check`，不创建普通请求、session 或候选结果，也不调用 provider；模型可用性由实际获准任务的正常执行结果体现。

### Web 核心工具与资产

- 基础运行：固定 Node、Python 3、bash、coreutils、进程工具、sudo、tmux、curl、OpenSSL、git、jq、rg/fd。Python 显式提供 requests、bs4、yaml，浏览器 helper 仍只依赖标准库。
- Web：DNS 工具、nmap、ncat、ffuf、dirsearch、sqlmap，固定 katana 1.6.1、nuclei 3.7.1、dalfox 2.12.0；JWT 入口 `jwt_tool` 使用独立 Python venv，不覆盖系统 Python 包。构建时精确修正固定 JWT 源码的三个 input 默认值及两处 playbook 内置字典路径，保持用户相对文件参数的工作目录语义；不预生成配置或签名密钥。配置写入器仅使用 `=` 分隔符，避免 Python 3.14 拒绝上游无值注释键中的冒号；普通配置读取及 URL/代理值不变。首次运行沿用上游行为：生成当前用户配置后退出 1，检查初始化提示及配置后再执行所需命令，不能泛化忽略所有非零退出。
- 浏览器：仅系统 Chromium，Playwright CLI 0.1.15 / core 1.62.0-alpha-2026-06-29；保留 `/opt/google/chrome/chrome` 链接。CLI 实际读取 `PLAYWRIGHT_MCP_BROWSER=chromium`，并通过 `PLAYWRIGHT_MCP_EXECUTABLE_PATH=/usr/bin/chromium` 显式使用系统浏览器，避免寻找未安装的 Chrome for Testing。这些变量也供 CLI 使用，与备用 MCP 服务是否启动无关。
- `/opt/cairn/wordlists/web/{common,parameters,xss,sqli}.txt` 来自 SecLists `8f4c1846cdb02a7024bd09decce1afc1ef5df46b`，通过 GitHub 官方 Contents API 下载同一 revision 的原始文件，并校验固定 SHA-256；仅复制四份字典、来源与许可，不下载完整字典库。API 未认证下载受 GitHub 限流，遇到 403 / 429 应按响应等待，不跳过校验。
- nuclei 模板保留完整依赖资产与许可，来源 `2df013c4fc9e8a7b5cbb8a52c77f8870f61590b9`，路径 `/home/kali/.local/nuclei-templates`。JWT 工具来源 `3bc7407cf2222d6a821dcc19c776e5a1b1cb9a9b`，路径 `/opt/cairn/jwt_tool`；两者各有 `CAIRN_REVISION`。
- 固定模板和字典由 `web-assets.sha256` 验证；rg 15.1.0 / fd 10.4.2 使用 GitHub 官方 Release API 的固定资产 ID，并按架构校验 SHA-256。升级搜索工具时同步 Dockerfile 中版本、资产 ID 和摘要。下载缓存与 `.git` 不进入最终镜像。Kali rolling 包、JWT 的上游 requirements 和其他发布二进制并非完整供应链锁定，不能宣称位级可重现构建。

移除 AD/横向、云、移动、源码秘密审计工具，专项 Java/JDWP 工具、pwntools/pymongo、未使用的 jshook MCP，以及整库知识/PoC/靶场预装。重复的 nikto/naabu 不再常驻；缺少专项能力要报告限制，不当成测试阴性。资料仍可通过现有项目只读 advisory 输入提供，不另建工具市场。

工具按备用工具箱提供，由同一 pi 按需直接调用，不为每个工具开发专属执行脚本或自动扫描流程。日常浏览器操作使用 Playwright CLI；另外固定安装 `@playwright/mcp@0.0.81` 和 `chrome-devtools-mcp@1.9.0` 备用，不默认启动服务、不下载第二套浏览器。已安装 MCP 不等于已经注册成 pi 的结构化工具；确需 MCP 调用时仍要建立连接并明确会话归属与收尾，不宣称 CLI 与 DevTools 的全部调试功能等价。镜像验收测试仅在交付检查时显式运行，不是每个渗透任务的强制步骤。

### 工具取舍原则

镜像大小和工具数量都不是漏洞发现质量指标。保留标准是：Worker 的明确运行依赖、能增加有效攻击面线索的能力、可重放与对照的验证能力，以及必要的证据与会话管理；功能重复或当前范围外的工具不因“渗透镜像惯例”而保留。固定版本用于约束实际接口和生命周期行为，不等于版本越旧越可靠；升级须验证当前依赖和实际用途，不能只改成 `latest`。

端点发现优先利用登录后的正常业务请求，再结合 HTML/表单、实际加载的 JS、已知接口文档及有依据的路径探测；不要仅堆目录扫描器。区分静态线索、已观察请求与已正常重放的接口，保留方法、参数、身份、对象和状态前提。发现更多 URL 不等于完成更多有效测试，统一错误页、登录跳转和 HTTP 200 不能单独证明接口存在。具体原则在 runner 加载的 `AGENTS.md` 中统一说明，不增加每工具执行器。

### 独立镜像验收

构建共享宿主 CPU、内存和磁盘，资源有限时串行执行。`FROM` 旧大镜像后删除文件无法缩小继承层，因此正式配方从干净基础构建；缓存仅在层和元数据匹配时有效，不保证首轮提速。不对业务容器修改后 `docker commit`。同名镜像重新构建不会更新已经创建的容器；须先确认旧执行退出并按精确容器身份清理，再创建新容器，不能把重新打标签当成运行时升级。

需要验证镜像工具或浏览器生命周期时，可显式指定本地镜像后运行对应的隔离用例；这不是日常构建或启动的自动测试门槛（从完整源码仓库根目录，使用测试环境 Python）：

```bash
export CAIRN_TEST_BROWSER_IMAGE='arena-worker:local'
PYTHONPATH=cairn/src CAIRN_TEST_WEB_IMAGE_INTEGRATION=1 python -m pytest cairn/tests/test_web_worker_image_integration.py -q
PYTHONPATH=cairn/src CAIRN_TEST_BROWSER_INTEGRATION=1 python -m pytest cairn/tests/test_browser_session_integration.py -q
```

两类验收都要求显式镜像，仅解析本地 image ID，不自动 pull，不回落旧 tag；容器无网络、无业务挂载，仅测试容器内 loopback 合成页面。Web 烟测检查工具真实运行与证据落盘。浏览器测试分别运行 `single-browser-workflow` 和 `concurrent-lifecycle`：前者检查单会话登录、请求正文取证、动态请求、JS 下载、截图与定向回收；后者保留三浏览器并存、五轮任务收尾及旁路浏览器/tmux 存活检查。单会话通过不能替代并发验收，资源不足时应明确记录未完成项。离线工具烟测不是漏洞召回率或真实模型评测。

固定 CLI 可用 `requests --filter=...` 定位请求，再用 `request N`、`request-body N`、`response-body N` 分别读取详情与正文；`request N` 本身不包含正文。隔离合成诊断确认：页面仅接收 fetch 响应头和数据、未消费正文时，可能没有请求完成事件，原 CLI 会一直等待完成才读取正文。构建期 `patch-playwright-response.py` 仅修正主 CLI 的固定 core：请求尚未完成时立即返回 `response_body_incomplete`，不阻塞后续浏览器操作；已完成请求保持原有正文读取路径。版本、源码片段或 JavaScript 语法不匹配时构建失败，不修改两个备用 MCP 或浏览器生命周期。

`response_body_incomplete` 是取证限制，不是空正文、无响应或测试阴性。保留已有请求/响应头，仅在观察到请求完成后按预算重试；不要修改真实目标页面强行消费响应，也不能把 curl/Python 重放冒充原浏览器抓包。集成负例只在合成页中保留并消费同一个 Response，验证报错后会话可继续使用且原请求正文恢复可读；不通过第二次请求掩盖问题。这不是通用抓包完整性保证，修正后的实际验收状态仍以实施记录为准。

### 执行限制与验证边界

- 历史数据可读不等于旧执行可用：旧引擎绑定、旧 session、旧冻结执行快照拒绝执行；不转换为 pi，也不回退旧 CLI。
- SDK 无法表达的 `inference_endpoint` 明确返回 `unsupported_model_config`。模型静态声明 `contextWindow=128000`、`maxTokens=16384`，thinking 与自动压缩关闭；无自动能力发现，不能保证任意模型或长会话兼容。
- Dispatcher 为报告申请执行 timeout 加 300 秒收尾的租约，最多 1800 秒；`tasks.report.timeout > 1500` 在 claim 前拒绝，不截短或续租。
- 容器 owner 由 Dispatcher/Server 身份确定；热更若改变该身份会被拒绝并保留上一份有效配置，须由用户自行显式重启 Dispatcher 应用。不认领旧容器、不自动重启服务。

部署须同时核对实际 images/profile 引用、`runtime.playwright_session_policy: task` 和正式前端；需要先暂停的故障项目若无法通过正常认证接口暂停，只启动 Server，不启动 Dispatcher 隐式恢复执行。镜像离线自检及数据库测试通过仍不能代替真实 provider、项目端到端和渗透效果验证。
