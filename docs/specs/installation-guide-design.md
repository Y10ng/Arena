# 简洁完整安装部署指南：分析与实施方案

## 1. 本轮目标与状态

用户希望有一份简洁但能完整走通安装的中文文档，覆盖使用镜像和从源码安装。本任务按 `docs/analysis-design-handoff.md` 分工：分析会话只读查证并维护本方案；另开的实施会话修改本轮用户文档。

- 2026-10-08：分析会话已完成相关源码及文档的静态查证；本轮文档实施与检查结果见第 6 节。没有运行安装、测试、构建、服务或项目，也没有验证远端发布资产。
- 查证时分支：`chore/arena-initial`。工作区有其他任务的大量未提交修改，实施前须重新检查并保护。
- 未找到专属安装部署文档原方案，因此建立本文件作为本任务唯一方案，后续实施和纠偏增量更新这里。不得借用正在修改的 Dispatcher 或前端工作区方案。
- 本轮收益是降低安装与首次配置负担，不声称增强或验证了漏洞发现能力。

## 2. 已确认依据与需要纠正的理解

以下行号是本次定位线索，实施时以当前文件为准。

| 事实 | 依据 |
| --- | --- |
| 当前是宿主机 Server / Dispatcher + Docker Worker，Compose 只负责 PostgreSQL，不是全栈应用镜像部署 | `deploy/start.sh:153-157,234-240`；`deploy/postgres.compose.yaml:1-21` |
| 宿主安装要求 Linux、Python >=3.11、uv；使用 frozen lock 安装，不启动服务、不构建前端或镜像 | `deploy/install.sh:9-19,49-61` |
| 首次 `start.sh` 会在必要文件缺失时调用安装；默认数据库模式需要可用 Docker 与 Compose v2 | `deploy/common.sh:55-82,122-129`；`deploy/start.sh:49-88` |
| 默认 Worker 名为 `arena-worker:local`；仅缺失该默认镜像才自动构建，自定义镜像缺失直接报错，已有同名镜像不自动重建 | `deploy/common.sh:261-304`；`deploy/dispatch.yaml.example:27-29` |
| 启动脚本检查 pi 1.0.0 / SDK 0.73.0 标签；通过标签检查并不能证明镜像与全部当前代码匹配或任务可用 | `deploy/common.sh:274-284`；`container/Dockerfile:58-61` |
| 未显式填写 images 注册时，默认注册引用 container.image；已有显式注册需核对画像引用链 | `cairn/src/cairn/dispatcher/config.py:430-460`；`deploy/dispatch.yaml.example:37-63` |
| 正式前端由 `build:server` 完成类型检查并构建，由 Server 提供，不要求同时运行 Vite 开发服务 | `cairn/frontend/package.json:16-19`；`cairn/frontend/vite.config.ts`；`README.md:9,24-37` |
| 代码部署 ZIP 带服务端、静态前端、部署脚本、Worker 构建输入和现有部署指南，不带完整前端源码、已安装依赖或 Docker 镜像 | `deploy/package_release.py:20-36,115-156,228-241` |
| 首次管理员随机密码在交互终端显示一次，非交互时留在受限文件；后续启动不重置已有管理员密码 | `deploy/common.sh:527-536`；`deploy/start.sh:103-123,279-283` |
| 默认监听 0.0.0.0:8000；远程浏览器访问须设置实际 Origin。运行配置来自 runtime/arena.env，不应只依赖终端 export | `deploy/common.sh:322-362`；`deploy/start.sh:53-56`；`deploy/arena.env.example:1-17` |
| running 主要判断进程与 HTTP，heartbeat 单独显示；stop 不停止 PostgreSQL，也不证明所有 Worker 已退出 | `deploy/status.sh:13-39,41-75`；`deploy/stop.sh:6-13` |
| 无 Git 更新器有真实入口，但不是源码同步工具，不刷新已有 Worker 镜像 | `deploy/update.sh:8-18,299-361` |

现有 `docs/deployment-linux-zh.md` 已有完整部署骨架，README 重复部分源码步骤；问题不是完全缺文档，而是安装方式不清、预构建 Worker 镜像的使用步骤缺失、首次配置与运维信息分散。

**不能伪造的前提：** 本地未找到发布 workflow；现有指南也明确没有提供已发布 GHCR 地址。这只能证明本次本地查证没有可靠发布依据，不能推断远端绝无镜像。实际镜像仓库、tag/digest、版本配套、发布架构、现成 ZIP 下载地址均未确认。

## 3. 本轮推荐设计

### 用户补充：方便安装与自由配置优先（2026-10-08）

用户明确要求：安装部署不要太多限制、校验机制，要方便更改端口等配置。此前方案中的证据核对是文档作者的责任，不应转化为安装者必须完成的一套检查流程。

- 主线只保留真正需要的安装、配置、启动步骤；不新增预检脚本、manifest/hash 校验门槛、版本审批、镜像白名单、端口白名单或配置锁定，也不要求安装者重复执行启动脚本已有的检查。
- 默认值只是开箱即用的起点，不是必须遵守的部署方式。端口、监听地址、镜像名、数据库和数据目录允许按需配置，优先复用现有入口，不另造配置层。默认镜像名也只是减少配置的便利选项，不要求所有人改名。
- 在安装主线附近增加“改端口和常用配置”短节，用具体示例说明临时参数与持久配置：首次启动可用 `./deploy/start.sh --host 0.0.0.0 --port 8080`；持久设置只编辑 `runtime/arena.env` 的 `ARENA_SERVER_HOST`、`ARENA_SERVER_PORT`，命令行优先。示例里的 8080 不是唯一允许端口，不要求改源码或重建镜像。
- 已运行实例更改地址、端口或环境配置时，示例使用 `stop.sh` 后 `start.sh`，让 Server 与 Dispatcher 一起加载新配置；提醒会中断服务即可，不另加审批或强制检查清单。临时参数不会写回配置文件，下次未传参数时恢复文件值。
- 本机访问在 Origin 未自定义时会随端口自动生成 localhost/127.0.0.1 Origin，不要求重复填写。局域网/域名访问给一个具体 `CAIRN_TRUSTED_ORIGINS=http://192.168.1.10:8080` 示例并注明替换实际地址；已经自定义 Origin 时端口变化须同步修改。把现有行为说清，不扩展为公网安全加固教程，也不借本轮删掉现有认证或 Origin 检查。
- 常用配置用一张小表写清“在哪里改、如何生效”；外部数据库、存储和镜像映射细节按需查阅，不塞进每个人的首次安装流程。兼容性/标签诊断放排障，发布来源未确认用一句话说明，不让限制说明淹没安装步骤。

依据：`deploy/start.sh:9-18,52-56,133-145,208-239`；`deploy/common.sh:322-362`。这是文档设计纠偏，不是授权实施会话顺手改部署逻辑。若发现现有逻辑确实妨碍常用配置，单独记录最小问题，再决定是否开下一轮代码改动。

### 单一指南，两条明确路径

继续使用 `docs/deployment-linux-zh.md` 作为唯一完整安装入口，README 保留短介绍与醒目链接。不要另建 quickstart、安装报告或同义指南。正文先给最短安装主线，低频配置放后面；简洁靠复用共同步骤，不靠删除必要前提。

开头用小表解释选择：

1. **使用已有 Worker 镜像安装（有条件）**：需要与应用版本及主机架构匹配的可信镜像，以及应用程序文件。优先描述“应用部署包 + 已有 Worker 镜像”，明确仍需宿主 Python/uv、Docker 和数据库；若只有源码没有部署包，还需构建前端。不能称作纯镜像、全栈 Compose 或完全离线安装。
2. **从源码完整安装（当前可自构建路径）**：获取源码、安装 Python 依赖、构建正式前端、构建 Worker、启动完整系统。

没有可信预构建镜像时明确引导第二条，不把本地 build 偷换成“官方镜像安装”。本轮不新增全栈容器化、镜像发布流水线或下载渠道；若用户后续确实要求无需源码/宿主 Python 的全镜像产品，那是独立实现需求。

### 必须写全，但不展开为运维手册

- **前置条件按路径拆分**：共同宿主条件、源码额外 Node/npm/Git、ZIP 解压工具、Worker 本地构建的网络与资源条件。建议主线使用 Node 24 LTS；不要编造测试过的最低硬件规格或架构兼容承诺。对系统依赖给明确官方安装入口或一个限定发行版的示例，不写适用于所有 Linux 的万能安装命令。
- **应用文件来源**：源码地址沿用仓库已有来源；部署包是 `package_release.py` 生成的 `arena-<version>-linux-code-only.zip`，不是 GitHub 自动 Source code ZIP。说明从可信分发方取得匹配包；没有已核实下载地址就明确标注，不捏造 Release。包的生成命令移到可选说明，不要求安装者先自行构建才能理解镜像路径。
- **已有镜像**：分别给 `docker pull` 和可信离线 tar 的 `docker load` 用法，明确地址/文件路径为待替换参数，不是已发布资产。导入后使用实际镜像名；新安装可选标为默认 `arena-worker:local` 以减少 YAML 操作，不强制改名、不盲目覆盖既有 tag。保留自定义引用时说明修改完整配置中的 container.image；已有显式 images/profile 映射的情况放进阶说明。不把手动标签、摘要、架构逐项校验设为启动前置步骤，实际兼容错误放排障。
- **步骤顺序**：获取应用 → 准备依赖/配置 → 准备匹配镜像 → 配置必要访问地址/数据库 → 启动 → 登录与基础状态检查。每个代码块标明工作目录、先决条件和变量替换，区分可复制命令和二选一分支。解压放入独立新目录，不覆盖现有安装。
- **默认主线**：使用脚本管理的 PostgreSQL，毋须手工重复生成数据库密码、keyring、token 或 schema。外部 PostgreSQL 和局域网访问放短可选小节；远程 Origin 配置要在首次远程登录前可见，不能只写“开放 8000 端口”。
- **共同首次使用**：默认 URL、admin 账号、两种随机密码交付方式、改密；按当前界面配置 Models/Workers、启用并绑定模型、检查 Dispatcher 心跳。模型凭据在界面保存，不让新用户同时填写旧 providers/workers YAML。创建项目、模型测试可能产生费用，不作为安装自动验收命令。
- **状态与运维**：列 start/status/logs/stop，解释 running、heartbeat、server-only 的边界；源码与部署包更新分开，说明修改运行配置后按需 stop/start、Worker 同名 tag 与存量容器不会自动刷新。保留数据库、数据目录、runtime 配置、keyring/token 的保护要求，不给清库或全局 prune 命令。
- **简短排障**：保留依赖/网络下载、数据库、静态资源、Origin、镜像不匹配、Worker/心跳和 keyring 的直接定位，不掺入发现算法、浏览器协议细节或大段发布治理。

## 4. 修改范围与不做事项

实施会话允许修改：

1. `docs/deployment-linux-zh.md`：重组为上述单一指南，保留必要配置与数据保护信息。
2. `README.md`：收敛重复安装步骤，明确两条路径与限制，指向主指南；不改无关介绍和开发约束。
3. `container/README.md`：仅在与新入口冲突或重复时，收敛应用安装内容并链接主指南，保留 Worker 专属说明。
4. 本方案：追加真实实施记录与未验证项，不复制一份新交接文件。

不得修改业务代码、脚本、Dockerfile、示例配置、依赖、全局规范或其他方案；不得新增依赖或发布基础设施。源码障碍如确实影响文档命令，记录具体证据并停在范围边界，不为了让文档成立顺手改功能。不要凭本轮授权 commit/push。

## 5. 直接核对与完成标准

本轮是纯文档工作：只读核对命令对应的入口、参数、配置生效顺序、文件来源、链接和最终 diff。不运行自动化测试、安装、构建、打包、拉取/加载镜像、启动/停止服务、模型调用或项目操作；也不将 status.sh 当作绝对只读工具直接执行，因为它会加载并确保运行配置存在。

完成标准：

- 新用户能选择一条路径，沿文档获得应用文件、准备依赖与镜像、启动、登录、完成必要模型/Worker 配置；哪些步骤有前提清楚可见。
- 镜像是 Worker 依赖、ZIP 是应用分发形式、源码安装包含前端构建，三者没有混淆。
- 不存在伪造的发布 URL/tag、假定预置 runtime/镜像/静态资源、用 Vite 代替完整系统或漏写首次远程 Origin 的步骤。
- 不要求读另一份聊天、历史方案或内部协议才会安装；少量深入参考不能成为主线缺步骤的借口。
- 仅宣称“文档与当前源码静态核对完成”，不宣称“全新机器部署通过”“镜像可下载”或“真实任务已验收”。实际安装可用性留待另行明确授权验证。

## 6. 实施记录

### 2026-10-08：本轮文档整理

实施分支：`docs/installation-guide`，从 `chore/arena-initial` 创建。编辑前检查了分支与工作区；保留其他任务的已有修改，并在这份已有未跟踪方案上增量更新，不修改其他方案。

**修改内容：**

- `docs/deployment-linux-zh.md` 保留为唯一完整入口：补全“应用部署包 + 已有 Worker 镜像”的 pull / load 路径，与源码完整安装共用配置、启动、登录和维护说明；写明 ZIP 的顶层目录，示例在新空目录解压，不编造发布地址。
- 将改端口、持久环境文件、参数优先级、stop/start、本机 Origin 自动派生和具体局域网 Origin 放在首次启动前，顶部也提供直达链接。常用配置用位置 / 生效方式小表，外部数据库、显式镜像映射和数据保留按需展开。
- `README.md` 收敛重复的安装命令、配置表和启停步骤，链接主指南与端口配置小节；保留产品介绍、简要使用流程及开发说明。
- `container/README.md` 仅收敛重复应用安装内容，纠正自定义镜像名与注册 ID 必须相同的误导，保留 Worker 专属说明。
- 本方案更新状态与本节记录；没有新增校验脚本、审批、白名单、配置锁定或第二份指南，也没有修改认证、业务代码、脚本、Dockerfile 或配置模板。

**静态核对依据：**

| 内容 | 本轮核对的当前入口 |
| --- | --- |
| 安装与正式前端 | `deploy/install.sh:9-61`；`cairn/frontend/package.json:16-18`；`cairn/frontend/vite.config.ts:17-27` |
| Node 版本建议 | `cairn/frontend/package-lock.json:2997-3027` 的 jsdom 30.0.1 engines：`^22.22.2 || ^24.15.0 || >=26.0.0`；原来的 22.12+ 不足以覆盖完整 `npm ci` 依赖，指南推荐 24.15.0+ 的 24 LTS |
| 临时 / 持久配置、Origin 与 Dispatcher 地址 | `deploy/common.sh:322-362`；`deploy/start.sh:9-18,52-56,133-145,208-239`；`cairn/src/cairn/dispatcher/config.py:418-439` |
| 镜像准备、注册与重载 | `deploy/common.sh:261-304`；`deploy/dispatch.yaml.example:27-61`；`cairn/src/cairn/dispatcher/config.py:430-460`；`cairn/src/cairn/dispatcher/scheduler/config_runtime.py:48-132` |
| 部署 ZIP 与更新 | `deploy/package_release.py:20-44,115-156,223-242`；`deploy/update.sh:4-63,228-361`，核对包名、顶层目录、内容与参数转发，不执行打包或更新 |
| 登录、状态、日志和停止边界 | `deploy/common.sh:527-536`；`deploy/start.sh:103-123,279-283`；`cairn/src/cairn/server/auth.py:27`；`deploy/status.sh:6-75`；`deploy/logs.sh:6-20`；`deploy/stop.sh:6-13` |
| 界面与存储 | Models / Workers 当前组件及导航；`deploy/start.sh:126-131`；`server/services/project_resources.py:32-44`、`server/services/libraries.py:26-36`（相对 `cairn/src/cairn/`） |
| 兼容错误与旧工作区 | `container/pi-agent/src/model.ts:6-25`；`cairn/src/cairn/dispatcher/runtime/containers.py:88-101,183-188,641-715`；现有 Worker 专属说明 |

**检查结果与未执行动作：**

- 审阅文档 diff；`git diff --check` 无空白错误。方案原本未跟踪，单独用 no-index diff 检查，未暂存文件。
- 本地 Markdown 链接及标题锚点静态核对共 13 个，均存在；代码围栏及 `details` 标签成对。外部安装链接仅核对写法，未访问远端，也未验证镜像仓库或部署包下载资产。
- 命令与源码入口、工作目录、ZIP 目录结构、配置生成时机和生效方式静态一致；没有执行文档中的安装、启动、状态、镜像或更新命令。
- 未运行测试、安装、构建、打包、镜像操作、服务启停、模型调用或项目操作；未 commit、push。这里只能确认文档静态核对结果，不能宣称新机器部署或真实任务通过。

**现有配置障碍与剩余事项（仅记录，不改代码）：**

1. 单独重跑 `start.sh` 可能重启已变地址的 Server，却复用旧 Dispatcher，后者仍保留启动时的 `CAIRN_SERVER_URL`；环境修改也不是全部热加载。指南统一给出 stop/start，避免只改端口后两端地址不一致。依据：`deploy/start.sh:133-145,208-239`。
2. Worker 容器 namespace 将 Server 地址（含端口）纳入实例身份；stop/start 能加载新端口，但不保证旧任务工作区无缝续跑，原容器不会由新身份自动接管。已放排障，保留工作区，不以删容器或取消归属校验作默认解决方式。依据：`cairn/src/cairn/dispatcher/runtime/containers.py:88-101,183-188,641-715`。若下一轮要改善“改 Web 端口后继续旧任务”，需单独设计身份与续跑行为。
3. 显式 `images` / profile 可选择与 `container.image` 不同的实际镜像，但启动脚本仍检查后者；只改显式注册项可能仍被旧默认镜像阻塞。默认 `images: []` 无此重复配置负担，指南将已有显式映射说明放进按需配置。依据：`deploy/common.sh:261-304`；`cairn/src/cairn/dispatcher/config.py:430-439`。
4. 自带数据库名 / role 与脚本 `runtime` 目录目前固定；自由选择数据库通过外部 URL、自由选择安装位置与支持的持久数据目录通过现有入口完成。没有虚构可改全部目录或内置数据库名称的环境变量。
5. 预构建 Worker 镜像、配套 ZIP 的真实分发地址与架构 / 版本配套仍未确认；全新机器安装、pull / load、重启生效、远程登录、更新和真实任务均待另行授权动态验证。

本轮实施到此停止。后续分析会话应结合实际 diff 和用户反馈验收，不以实施者的完成声明直接结案。

## 7. 后续诉求：Worker 镜像分发（待确定发布目标）

用户随后询问当前镜像如何安装、能否发布到 Docker。推荐先将现有 Worker 发布到 Docker Hub，让安装者 pull 代替本地构建，不先改全栈部署架构或增加发布平台。此处是分发方案，不是已发布声明；此前 Git push 授权不自动延伸为 Docker Hub 上传授权。

### 本次只读证据

- 当前源码提交为 `623603a`，已推送至 `Y10ng/Arena` 的 `docs/installation-guide` 分支，尚未合入 main。需要本轮代码时不能只给默认分支的 clone 命令。
- 本机存在 `arena-worker:local`，镜像 ID 为 `sha256:9b9aa6dd56452f101bf8baa82b12ea7540ae901912f1408c514ad81f61c23cde`，平台 `linux/amd64`，本地大小 1,900,507,939 bytes（约 1.90 GB，不是压缩下载体积）。标签声明 pi 1.0.0 / SDK 0.73.0，`RepoDigests` 为空。
- 上述元信息只能证明本机镜像存在，不能证明它包含 `623603a` 的全部 Worker 修改，也不能证明远端从未发布过同内容镜像。本次没有提取或运行容器内程序核对版本。
- 构建入口仍是 `docker build -t <镜像引用> container/`；`container/.dockerignore` 排除了运行目录、宿主依赖、私有配置等。首次正式分发宜从明确源码提交构建，不把有未知改动的业务容器 docker commit 成发布镜像。

### 推荐最短分发路径

1. 由用户确定 Docker Hub 用户名或组织、仓库公开或私有，并明确允许上传；建议仓库名 `arena-worker`。不能从 GitHub 用户名猜 Docker Hub 账号，不索取或记录密码/token。
2. 新实施会话在明确目标后，用当前指定提交的 `container/` 构建并推送 `docker.io/<namespace>/arena-worker:git-623603a`（这是建议 tag，尚未创建）；首次仅声明本次实际构建的架构，不因 Dockerfile 有分支就宣称已发布多架构镜像。用户需要稳定入口时再给同一产物附加 `latest`，不必引入 CI 或版本审批框架。
3. 登录由用户在会话终端自行执行 `! docker login` 等交互入口，认证材料不进入聊天、Git 或构建参数。实施会话做一次必要的镜像内容/安装自检即可，不增加安装者预检门槛，不调用模型、不操作已有项目或更新正在运行的业务容器。
4. 发布后记录实际 tag/digest/架构，用户安装时只需 pull 并设置 `runtime/dispatch.yaml` 的 `container.image`，或按需使用默认本地别名；由 Dispatcher 启动 Worker，不要求用户手工 docker run。
5. 应用仍需宿主 Python/uv、Server、Dispatcher 和 PostgreSQL；使用源码时仍需构建前端。若还希望省去前端构建，配套发布应用 ZIP 是另一个需要明确目标的上传动作，不假装 Docker Hub 镜像已经包含完整应用。
6. 成功后更新现有部署指南的实际拉取命令、来源说明和本方案；全栈应用容器化、自动发布流水线、跨架构构建与已有任务迁移不在这轮默认范围。

待用户提供 Docker Hub namespace、可见性和上传确认后，再填写本轮专用实施提示词；当前没有构建、登录、上传、运行测试或启停服务。
