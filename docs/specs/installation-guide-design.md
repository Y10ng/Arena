# 简洁完整安装部署指南：分析与实施方案

## 1. 本轮目标与状态

**最新轮次（2026-10-20）：** `update.sh --target` 配套更新与 Worker 缓存复用的分析及实施交接见第 8 节；本次实施与验证边界见第 8.8 节。第 1—6 节的“本轮”指原安装文档整理，第 7 节是尚未确定发布目标的历史分发讨论；其文档-only 范围和发布建议不代替第 8 节的新范围，也不授权当前分析会话操作运行系统。

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

## 8. 更新器配套更新与 Worker 增量复用（2026-10-20）

### 8.1 用户目标与本轮边界

用户采用新部署包中的 `update.sh --target /原Arena安装目录` 更新其他机器，希望一次操作完成应用、数据库与 Worker 的配套更新，未变化的工具和包不要重复下载。目标是减少升级遗漏、下载失败与重复等待，不改渗透工具集合、执行协议或研究能力。

本分析轮基于 `feat/retest-v3-cyber` 的 `e62f6d5` 只读核对，仅维护本方案；没有运行测试、安装、构建、打包、镜像、服务、数据库或项目操作。此前 Git push 已完成，不构成本轮提交、推送或镜像发布授权。其他机器的旧镜像身份、构建缓存、实际配置和迁移条件均未知。

**纠正理解：数据库并非不更新。** 更新器最终调用 `start.sh`，启动路径已有 PostgreSQL schema 自动迁移；缺口是已有 Worker 镜像不会刷新。本轮沿用该迁移链，不另造数据库更新器，不升级 PostgreSQL 大版本，不增加 SQLite 支持。

### 8.2 已确认依据与首因

| 事实 | 当前源码依据 |
| --- | --- |
| 更新器暂存新包、停宿主服务、替换受管文件、install、start、status；成功后删除临时旧程序备份 | `deploy/update.sh:138-361` |
| Worker 仅默认镜像缺失时构建；已有镜像只检查固定 pi / SDK 标签，不能说明 runner 已更新 | `deploy/common.sh:261-304` |
| start 在数据库初始化前检查 Worker；随后 `auth bootstrap-runtime → db.configure → run_migrations` 自动迁移 PostgreSQL；`--server-only` 跳过 Worker | `deploy/start.sh:59-116`；`cairn/src/cairn/cli.py:184-190`；`cairn/src/cairn/server/db.py:192-194,1213-1223` |
| 部署 helper 只读取 `container.image`，实际显式 `images` / execution profile 可以引用其他镜像，不能另写简化 YAML 解析或只更新无关默认 tag | `deploy/common.sh:261-271`；`cairn/src/cairn/dispatcher/config.py:419-445,508-509` |
| builder 已按清单 / lock → npm ci → 源码 → 编译、自检、prune 排列，普通源码变更可复用依赖安装层 | `container/Dockerfile:25-31` |
| runtime 复制应用后才安装 MCP、应用 Playwright 补丁并混合执行用户初始化；应用变化使这些后置步骤重跑 | `container/Dockerfile:130-154` |
| AGENTS 已在末尾复制；任意构建上下文变化不等于所有层失效 | `container/Dockerfile:160-162`；`container/.dockerignore` |
| 部署包有 Dockerfile、runner 源码 / lock、AGENTS、工具补丁和资产校验清单，但不带镜像、已安装依赖或多阶段构建缓存 | `deploy/package_release.py:20-44,115-156,195-207` |

`e62f6d5` 将工具下载的三处 `--max-time 300` 改成 `3600`，未改变这些工具的版本，但改变了 `Dockerfile:78` 起 RUN 的缓存键：该下载层及后续顺序层可能重跑。更前的 apt / CLI 层及独立 builder / web-assets 不因此全部失效。加 `--cache-from` 不能使不同缓存键命中，不应继续靠延长超时或换下载源处理应用更新。

### 8.3 最小实现：保留一个更新入口

1. 保留部署包更新主线，不要求用户另外手工执行 `docker build` 或数据库迁移。推荐顺序为：现有暂存与停止 → 替换受管文件 → install → **准备本次 Worker** → start（含既有数据库迁移）→ status。Worker 准备失败必须非零退出，不继续迁移、启动新应用并静默沿用旧 Worker。
2. 新 Worker 准备在目标安装目录的**新子进程**执行，加载新 `common.sh`、新 `.venv` 和该安装的运行环境；不能调用更新器当前 shell 已加载的旧 helper。沿用现有锁与参数转发方式，避免新 helper 重入更新锁造成死锁；保留 `--server-only` 的明确跳过语义。
3. 用现有 `DispatchConfig` 解析实际镜像引用。默认本地管理的 `arena-worker:local` 即使存在也走带缓存的本次构建，不以三个固定版本标签判断无须更新。必要的镜像解析由 update / start 共用，修正本轮会影响实际更新目标的引用不一致，不扩大为全部画像系统重构。
4. 构建先产出候选镜像，通过既有镜像 / 应用自检后才更新实际使用的本地 tag；失败不覆盖原可用引用，不删除旧镜像或清缓存。构建通过只说明新镜像已准备，不说明旧容器或真实任务已升级。无需新增发布 manifest、版本审批或通用镜像管理平台。
5. 自定义镜像名、远端 tag/digest、显式 images / profile 不静默改成默认值，不把本地 runner 覆盖到任意用户镜像。本轮自动构建保证针对实际使用的默认本地镜像；用户维护的其他引用保留，清楚报告“未自动更新，需要配套镜像”。现有兼容检查不是版本配套证明，不能把这类结果显示为“全部 Worker 已升级”；若实际配置无法供启动使用，应明确失败，不回退默认镜像。第 7 节未确定的 registry 不作为本轮前提。
6. 失败提示和修复后续命令也必须覆盖 Worker 准备步骤，不能引导用户绕过它直接 start。应用、依赖、Worker、数据库初始化和服务状态的完成范围分别如实报告，不新增需要用户手工维护的重复状态文件。

### 8.4 最小分层调整与缓存承诺

沿用当前多阶段 Dockerfile，不新建独立工具镜像发行体系，不变更现有工具版本、安装来源、依赖集合或补丁行为：

- 将与 runner 无关的 MCP 安装、Playwright 补丁、CLI 初始化、目录 / 配置 / git 初始化移到应用 COPY 之前。保持 root / kali 用户、权限、浏览器路径和禁用自动更新等运行语义；从混合 RUN 中拆出依赖应用的自检，仍放在应用复制之后执行。
- runtime 中依赖文件及 `node_modules` COPY 放在 `dist` COPY 前；保留 builder 的“清单 / lock 在源码前”顺序，不把整个 context 提前 COPY。无需为了此次重排新建另一套 npm 依赖安装流程。
- 与应用版本有关的 LABEL 后置，避免仅应用身份改变使工具安装失效；AGENTS 保留靠后。保留必要编译和应用自检，不能以跳过检查换取快构建。
- 默认使用有效的本地 Docker 缓存，不使用 `--no-cache`、全局 prune 或每次强制刷新 base 的方式更新应用。不改变工具层输入时，不重复执行其安装；lock 真正变化时必须安装所需依赖，不能直接保留不兼容 node_modules。

**保证范围必须明确：** 在基础镜像、架构和安装输入一致且有效缓存保留的同机更新中，源码 / AGENTS 变更不应触发未变工具的联网安装，lock 不变时复用 npm ci 层。这不等于“每次构建都只运行一条 COPY”，编译、自检等本地步骤可以重跑。

**首次准备与后续更新分开：** 本次重排、已提交 timeout 变化、基础镜像变化、缓存被清理，都可能造成安装层重跑；只有 `docker load` 导入的最终镜像，也不等于具备 builder / web-assets 缓存。不能承诺任意旧机器首次升级完全零下载，但这不是下述用户澄清后的验收要求。

**用户进一步澄清（2026-10-20）：首次下载镜像耗时可以接受，重点是后续涉及 Worker 的更新，不要再次下载或安装整个工具环境。** 因此不再把“首次零下载”列为本轮必须解决的阻断；验收聚焦基础环境与依赖未变时，连续应用更新确实复用已有层。不自动从未知旧镜像做 overlay，不增加旧镜像兼容猜测或首次免下载专项机制。后文实施记录中“首次零下载不保证”是技术边界，不表示用户目标因此未满足。

- **镜像版本替换不等于全量传输或重装。** 每次仍可得到完整、不可变的新镜像；Docker 复用未变的内容层，只生成或下载缺少的层。粒度是层，不是层内逐文件 / 逐字节补丁，因此应用有变化时整个应用层可能重新传输，但不应带着稳定工具层重来。
- **当前 code-only 包路径：** `update.sh` 内部执行带缓存构建，首次建立有效缓存后，后续仅源码 / 指引变化复用工具安装层，lock 不变复用 npm ci；保留缓存、稳定基础镜像与安装输入。最终仍是新版完整镜像，不在运行中容器内用覆盖文件或 `docker commit` 热补丁。
- **以后若采用镜像仓库分发：** 发布端以稳定分层构建配套版本，安装端 pull 时复用本地已有的相同层，通常只下载变化的应用层；安装端不需要 builder 缓存，但发布端必须保持基础层内容稳定。完整 tar 的反复分发不等于网络增量下载。当前没有已确认仓库与发布授权，本澄清不新增 registry、上传或切换分发路径的实施任务。

### 8.5 数据保护、失败与存量容器

- 只更新代码和必要镜像；保留 runtime 配置、完整 keyring 历史版本、机器 token、项目资料、Artifact、外置存储与 PostgreSQL 数据。不清库、不改历史迁移 checksum，不自动取消报告或恢复 / 重跑项目。
- 旧程序临时备份不是数据库 / runtime 备份。install 会同步依赖，也可能迁移 legacy secret 配置；因此相关失败消息不得绝对声称“运行数据未被修改”。文件替换可恢复不等于依赖、镜像和数据库可整体回滚。
- 当前 v58 对归档模板、pending / running 报告及符合其条件的可重试 failed generation 有迁移阻断。沿既有迁移报错给出诊断，不能通过删除项目、归档材料或改 SQL 来绕过；保留旧程序备份也不能承诺直接切旧代码恢复数据库。
- stop 只停止宿主 Server / Dispatcher，不证明所有 Worker 已退出。更新 tag 只影响后续新建容器，不替换已有项目容器、覆盖其工作区或修改历史任务绑定；不能宣称“所有旧项目即刻获得新 runner”。如存量执行与升级不兼容，应如实说明并由用户决定处理，不自动杀容器。
- 继续使用 code-only 包的静态前端，不为本轮强制安装宿主 Node 或重建前端。保留已有外置静态目录和自定义路径；不拿更新器覆盖 Git 开发工作区的本地修改。

### 8.6 实施范围与直接验证

新实施会话允许修改 `deploy/update.sh`、必要的 `deploy/common.sh` / `deploy/start.sh`、`container/Dockerfile`、直接相关定向测试及已有部署 / Worker 文档，并回填本节。若确需一个部署 helper，保持单一入口与职责，确认 `package_release.py` 已收录其构建 / 运行输入；不要为了形式拆一套部署框架。默认不改工具源码、依赖锁文件、业务数据库 schema、前端、项目容器生命周期或全局规范。

按风险选择最小直接验证，不把下面事项转换成全仓测试套餐：

1. 在临时安装夹具中用受控命令替身验证调用顺序：已有默认镜像仍准备新版本，失败时无 start / DB 入口；成功后才启动；保留参数、锁、外部配置和 `--server-only` 语义。自定义映射不被覆盖、不假报配套完成，失败后的提示命令不绕过准备。此检查不能代替真实 Docker 缓存证据。
2. 在获准构建的环境，以同一 base / 架构、未清理缓存验证连续应用更新：首次完整下载和构建可以接受；随后分别仅改变 runner 源码、仅改变 AGENTS，实际完整构建日志中的工具安装及 npm ci 层均须命中缓存。记录哪些步骤实际执行、哪些缓存命中，不用耗时、Dockerfile 排列、静态字符串断言或模拟测试代替。不要为制造缺缓存场景删除用户缓存，也不无理由多次全量下载。
3. 数据库沿当前 PostgreSQL 迁移入口，不重新创建迁移机制；若需要失败路径验证，用隔离夹具 / 定向用例，不直接拿真实业务库试迁移或做回滚实验。只对本轮调用顺序作验证，不默认跑全部历史迁移测试。
4. 代码完成后的构建与完整系统启动按 `AGENT.md` 和该实施会话实际运行授权执行；有真实在途任务、迁移阻断或备份条件不具备时说明障碍，不擅自清理解除。确认实际新建容器的镜像引用，但不自动运行项目、访问目标或调用模型。把代码实现、缓存实测、系统部署、其他旧机升级及真实任务验收分开报告。

实施者同步 `docs/deployment-linux-zh.md` 的 ZIP 更新说明：一条入口做了什么、默认与自定义镜像边界、首次缓存限制、旧容器范围、备份和迁移失败处理。不要发布尚不存在的 ZIP / 镜像地址，不 commit、不 push、不上传镜像。完成后在本原方案追加真实记录；其他旧机未验证及首次缓存限制如实保留为技术边界，不再把首次零下载列为本轮待解决事项。

### 8.7 实施前的分析交付状态（历史记录）

已完成当前脚本、Dockerfile 和打包输入的静态核对并更新本方案；仅文档改动。尚未实施 Worker 更新入口或分层调整，未访问其他机器、查询实际镜像缓存、运行测试 / 构建 / 服务 / 数据库，也未新建提交。新实施会话以第 8 节为本轮范围，不执行第 7 节历史发布建议，不重复原安装文档整理或报告 / UI 的其他待办。

### 8.8 2026-10-20：更新入口与缓存分层实施记录

**范围与工作区：** 编辑前检查分支和工作区，从 `feat/retest-v3-cyber` 创建 `feat/update-worker-cache`。保留本方案已有未提交内容，只增量更新第 8 节实施状态与记录，不接续报告、UI 或第 7 节历史发布。未增加依赖，未创建提交、push 或上传镜像。

**代码实现：**

- `update.sh` 在 install 成功后，以新 Bash 子进程加载目标目录新 `common.sh`、运行环境和 `.venv`，验证继承的 FD 9 操作锁，再准备 Worker；失败保留退出码并阻断 start / 数据库启动迁移。`--server-only` 明确跳过，不假报配套完成。install、Worker、start 失败后的修复提示均回到完整更新入口，并用 shell 转义保留原参数边界。
- start 与 update 复用 common 中同一个 `DispatchConfig.load` 解析路径，覆盖启动自检基线、允许选择及显式注册镜像；画像引用无效即失败，不另写 YAML 解析或回落默认值。默认本地引用在 update 中始终带缓存构建；自定义 tag/digest 保留并提示未自动更新。候选以 `--iidfile` 接收，复用现有 `verify_image` 的平台、版本与无网络自检，通过后才 tag 到默认引用，失败不覆盖旧引用，不删镜像或缓存。
- Dockerfile 保留多阶段结构、工具版本、安装来源、依赖与补丁。MCP 安装、Playwright 补丁和用户初始化均前移到应用 COPY 前，生产 `node_modules` 在 `dist` 前，应用自检仍在应用复制之后，版本 LABEL 后置，AGENTS 保持靠后。
- 同步现有部署及 Worker 指南：单一更新入口、默认 / 自定义边界、缓存前提、首次限制、存量容器和失败恢复范围。未改业务迁移、前端或项目容器生命周期；现有 `package_release.py` 的 deploy / container tree 已覆盖本轮运行输入，无需扩大发布 allowlist。

**验证（按完成范围分列）：**

- 脚本语法：`bash -n deploy/common.sh deploy/update.sh` 通过。
- 镜像准备定向夹具：`test_deployment_verification.py` 中标签 / 候选检查、实际配置引用、构建或验收失败不 tag、自定义缺失中断、纯自定义不构建或改 tag，共 20 个用例通过；未运行该文件其他用例或全套测试。
- 测试环境 `.venv` 未安装 pytest，首次常规调用报 `No module named pytest`；后续在保留 `.venv` 业务依赖优先级的前提下复用系统现有 pytest，关闭插件自动发现，未安装新依赖。
- 更新生命周期临时安装夹具：原文件 12 个用例通过，覆盖新 helper / `.venv` / env、调用顺序、锁继承、失败阻断、原参数及可执行的重试提示、server-only、配置 / 完整凭据 / 资料 / 外置存储保留；另补 2 个 host / port 值恰为 `--server-only` 时不能误跳过 Worker 的用例，均通过。合计 34 个定向用例通过，没有运行全套测试。
- 真实 Docker 连续完整构建：基线、仅源码变化、仅 AGENTS 变化三次均 exit 0。后两次的工具安装与 `npm ci` 等未变依赖层实际显示 `CACHED`，两个候选均通过既有 `verify_image` 的无网络版本 / 应用自检；具体输入、步骤和日志见下文，不以 mock 或静态层顺序代替实测。
- **部署生效未验证：** 只读核对实际运行 PostgreSQL 时发现 1 个 active 项目、1 个 running TaskRun（schema 59），且项目 Worker 容器仍运行。因此没有停止 / 重启当前 Server 或 Dispatcher，没有迁移或替换业务容器，更没有恢复任务、调用模型或访问目标。既有系统 `/health` 与首页返回 HTTP 200 仅证明旧运行系统可访问，不代表本轮代码已部署。

**真实 Docker 缓存证据：**

在同机 `linux/amd64`、Docker 默认 builder 上串行执行原 Dockerfile 的完整构建，没有使用 `--no-cache`、清缓存、换源、修改工具版本或跳过编译 / 自检。首次构建允许完整下载；其中 katana / nuclei / dalfox 下载层耗时 3346.1 秒，已有 curl 重试处理一次 HTTP/2 reset 后成功，此耗时不作为后续缓存命中证据。

隔离副本仅复制 Dockerfile 实际使用的 20 个输入文件，不带业务 runtime、凭据或宿主依赖。通过 SHA-256 对比确认：基线到第二次仅 `pi-agent/src/cli.ts` 追加中文验证注释；第二次到第三次仅 `AGENTS.md` 追加验证注释，源码、Dockerfile、lock、工具补丁与固定资产清单均不变；仓库中的 runner 源码和 AGENTS 未因验证被修改。第二次确实重新执行了源码 COPY、TypeScript 编译及应用自检，产生不同的 dist 与镜像，而非全缓存的空验证。

| 实际步骤 | 仅源码变化（source-change.log） | 仅 AGENTS 变化（guidance-change.log） |
| --- | --- | --- |
| runtime apt 工具 / Chromium 安装 | `#30 CACHED` | `#15 CACHED` |
| Playwright CLI 安装 | `#22 CACHED` | `#23 CACHED` |
| katana / nuclei / dalfox 下载与安装 | `#32 CACHED` | `#20 CACHED` |
| rg / fd 下载与校验 | `#33 CACHED` | `#13 CACHED` |
| JWT 补丁、venv 与 pip 安装 | `#25 CACHED` | `#37 CACHED` |
| MCP 安装 | `#24 CACHED` | `#31 CACHED` |
| Playwright 补丁 / 用户初始化 | `#23 / #36 CACHED` | `#9 / #34 CACHED` |
| 固定 Web 资产下载与校验 | `#20 CACHED` | `#38 CACHED` |
| builder `npm ci` | `#9 CACHED` | `#18 CACHED` |
| runtime 生产 `node_modules` COPY | `#37 CACHED` | `#21 CACHED` |
| builder 编译 / SDK 自检 / 既有 npm prune 合并步骤 | `#18 DONE 12.7s` | `#19 CACHED` |
| runtime `dist` COPY / 应用自检 | `#38 DONE 0.1s` / `#40 DONE 3.1s` | `#16 / #40 CACHED` |
| AGENTS COPY | `#41 DONE 0.1s`（前置应用层变化） | `#41 DONE 0.1s`（指引本身变化） |

三次实际镜像 ID 分别为：

- 基线：`sha256:50ba4e38dd02d94b07565e1b3c6acc2215a547ec0d23975d593b1f384a8e53cf`，使用独立验证 tag `arena-worker:update-cache-check`。
- 仅源码变化：`sha256:b374dd4ecb38d3941496e3adbac334fe9589b37b9e0f1e26b3f1b257249adc28`，通过独立 `--iidfile` 接收。
- 仅 AGENTS 变化：`sha256:87d5c8da58c8b6f45290936fd04bc62cf0c286112aee56f7d30d652148d02eb4`，通过独立 `--iidfile` 接收。

两个变化后的候选均通过标签检查及既有 `verify_image`：`cairn-agent 1.0.0 (pi-coding-agent 0.73.0)`、`--self-check` 契约有效。检查容器使用 `--rm --network none`，没有业务挂载、凭据、模型请求或目标访问。检查前后默认 `arena-worker:local` 仍为原来的 `sha256:56369f4d4b95399070a992311401a7343a022b42ba319529b75292598f8ba691`，未晋升验证镜像。基线与源码候选的 RootFS 前 21 层相同，源码与指引候选前 24 层相同，作为实际层复用的补充证据。

原始本地证据保留于 `/tmp/arena-worker-cache.7at6Lu/`：`baseline.log`、`source-change.log`、`guidance-change.log`、`verification.log`，以及输入摘要 `input-hashes.json`；临时目录可能随系统清理，不是发布资产。此处实际验证的是 updater 所用 Dockerfile 的连续构建和候选离线检查；完整 update 的新子进程、锁与参数语义由上述临时安装夹具验证，未对当前业务安装执行更新。构建后再次以只读 PostgreSQL 连接复核，仍有 1 个 active 项目和 1 个 running TaskRun，因此继续阻断本次系统部署，不停止服务或处理业务任务。

**部署与兼容边界：** 其他旧机器未访问、未升级；首次跨版零下载不保证，但按用户澄清不再作为本轮待解决事项。旧最终镜像不能替代完整多阶段缓存；层重排、历史 timeout / base 变化与缺缓存都可能重新下载。旧程序备份仍不是数据库、runtime、依赖或整体回滚保障；迁移阻断保持原逻辑，不作清理或绕过。真实任务验收及存量项目获得新版 runner 均不在本轮已验证结果内。

### 8.9 2026-10-20：实施后的只读复核

- **代码已修改：** 当前 `feat/update-worker-cache` 工作区中，`deploy/update.sh:300-388` 已加入 install 后、start 前的 Worker 准备；`deploy/common.sh:299-345` 已实现默认已有镜像的缓存构建、候选离线检查后更新 tag、失败向上传递。`start.sh` 本身未改，复用新 helper；普通启动不等同于更新器的 refresh。限定源码复核未发现新增主流程的明显阻断。
- **分层与缓存有实际依据：** Dockerfile 中与应用无关的安装已前移，runtime 应用 COPY 后没有显式工具安装步骤。只读核对第 8.8 节指向的现存 `source-change.log`、`guidance-change.log`、`verification.log`，确认源码 / 指引变化后的工具层、npm ci 缓存命中及候选自检记录，不仅采信实施者声明。未重新执行构建或复算隔离输入摘要；不将缓存复用扩大为整个构建绝无网络请求。
- **测试与部署分开：** 34 个定向用例通过是第 8.8 节的实施记录，本分析轮未重跑测试。实施记录明确未晋升默认镜像、未更新业务安装；当时有在途项目 / TaskRun。当前运行态本轮未重新查询，不能断言这些任务现在仍运行，也不能把旧系统 HTTP 200 当成本轮部署成功。其他机器实际升级未验证。
- **交付状态：** 检查时 HEAD 仍为 `e62f6d5`，本轮 8 个文件的改动尚未提交，不能认为此前推送版本已含新更新器。其他机器需取得包含这些改动的新部署包并使用新包的更新入口；只换一个旧脚本或继续使用原包不等于获得此次能力。现有打包入口收录 deploy / container，但本分析轮没有生成或确认新的发布 ZIP。
- **非阻断收尾项：** install、Worker、start 失败均提供完整重试命令；最终 status 失败 / 状态不符分支目前只提示备份位置（`deploy/update.sh:367-387`），可在后续同范围收尾时补齐提示，不因此否定已经实现的更新顺序和缓存能力，不在本分析轮修改脚本。
- **本轮操作：** 仅更新本方案并审阅差异；未运行测试、安装、构建、Docker、服务、数据库或项目操作，未创建提交或推送。后续重点是获准条件下的业务安装部署及新包交付，不重做首次免下载、工具链或缓存平台。

**后续用户安排（2026-10-20）：** 用户明确要求先提交并推送当前代码，当前环境的部署等项目完成后再安排。本次推送仅补充 CHANGELOG、审阅并提交现有改动，不运行更新器、构建、启停服务、替换业务镜像、迁移数据库或操作项目；不把 Git 推送视为已部署，也不设置自动监控项目或自动部署。上述未提交 / 未推送描述是复核时的历史状态，实际提交与远端状态以本次 Git 结果为准。
