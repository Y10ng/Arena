# Arena：Linux 安装、配置与更新

本文是完整安装入口，两条路径共用后面的配置、登录和维护说明。

| 安装路径 | 适合情况 | 需要准备 |
| --- | --- | --- |
| [部署包与预构建镜像（推荐）](#路径-a使用已有-worker-镜像) | 安装已发布版本，省去前端和工具层构建 | 应用 ZIP、按固定 digest 拉取的 Worker 与网关镜像；仍需安装宿主 Python 依赖 |
| [从源码完整安装](#路径-b从源码完整安装) | 需要自行修改、构建，或没有适配主机架构的发布镜像 | Git、Node.js/npm，以及构建时的网络和资源 |

当前是**宿主机 Server / Dispatcher + Docker Worker**，Server 同时提供 API 和正式前端；Compose 只负责 PostgreSQL，不是全栈 Docker 部署。本指南对应 **0.2.8** 配套部署，实际发布版本以 Release 和渠道文档为准。配套 `yong29/arena-worker:0.2.7` 已上传，网关复用既有镜像，两者均按固定 digest 拉取；不再要求 v0.2.3 Worker 最小应用层包或本地小应用层构建。宿主机无需 npm，Docker 复用未变化的镜像层。

默认端口、镜像名、数据库和安装位置只是起点，可按需要选择；常见调整见 [修改端口和常用配置](#修改端口和常用配置)。

## 已发布版本与下载

已发布的 [v0.2.6](https://github.com/Y10ng/Arena/releases/tag/v0.2.6) 提供 ZIP 管理端「系统更新」页面与 `release.json` 更新渠道。安装本指南对应的 **0.2.8** 时，先在 [最新 Release 页面](https://github.com/Y10ng/Arena/releases/latest) 或渠道检查版本为 **0.2.8**，再使用下面的包名；当前 latest 若仍是旧版，不能视作本版已经发布。完整 revision、附件大小及 SHA256 以实际发布说明和校验文件为准。

| 资产 | 下载或版本引用 |
| --- | --- |
| 0.2.8 应用部署包 | [最新 Release 页面](https://github.com/Y10ng/Arena/releases/latest)，选择 `arena-0.2.8-linux-code-only.zip` |
| 更新渠道文档 | [release.json](https://github.com/Y10ng/Arena/releases/latest/download/release.json)，检查 `version` 为 `0.2.8` |
| ZIP 校验文件 | 同一 Release 中对应的 `.zip.sha256` |
| 配套预构建 Worker（已上传） | [yong29/arena-worker](https://hub.docker.com/r/yong29/arena-worker)：`0.2.7`，manifest digest `sha256:62c3e3b9a19b1306b77361e98ab1169fa2e41529c31bb7100c3bb23849895d16` |
| 复用的流量采集镜像 | [yong29/arena-traffic-gateway](https://hub.docker.com/r/yong29/arena-traffic-gateway)：digest `sha256:c1ae1c054cc3b12b0648d06476cc7af86a13208b3f366d3ef0c1a3f9c3b41e10` |

本次修复安装目录改名后更新器的 `cairn` 导入失败，复用 0.2.7 的 Worker 与网关，无新增数据库迁移；Worker 远端 config 已核对对应本地通过 Docker 集成验证的 candidate，但这不等于已验收所有存量项目恢复或正式模型流程。网关复用上述 digest。**v0.2.6 ZIP 安装可检查 → 准备 → 应用；v0.2.5 及以前直接使用新包 `update.sh` 升级，不必先升到 v0.2.6。** 两镜像仅支持 `linux/amd64`，公开镜像无需登录。不要沿用未经更新的旧 Worker，也不要将本版镜像与任意更新后的 `main` 混用。

已发布 ZIP 内的文档保留打包时内容，下载地址与镜像引用以本指南和 Release 说明为准；不会为了补充文档覆盖同版本 ZIP 或改变其校验值。v0.2.1 的原部署包、校验值和镜像保持不变，历史发布记录见 `CHANGELOG.md`。

## 前置条件

两条路径都需要 Linux、Python 3.11+、[uv](https://docs.astral.sh/uv/getting-started/installation/)、可用的 [Docker daemon](https://docs.docker.com/engine/install/)，以及 `bash`、`curl`、`flock`（通常由 util-linux 提供）。默认使用脚本管理的 PostgreSQL，需要 [Docker Compose v2](https://docs.docker.com/compose/install/)；也可以连接已有 PostgreSQL。

- 路径 A 解压 ZIP 需要 `unzip`，校验使用 `sha256sum`（通常由 coreutils 提供）；已有正式前端和配套镜像时，宿主机不需要 Node/npm。
- 路径 B 需要 Git、npm，建议 [Node.js 24 LTS](https://nodejs.org/en/download) **24.15.0 或更新的 24.x**；若选 Node 22，需 22.22.2+，以满足当前 `npm ci` 的完整依赖要求。
- Python 软件源须可访问；默认数据库首次使用还会下载 PostgreSQL 镜像。从源码构建另需访问 npm、容器仓库、Kali 软件源及 GitHub 工具下载地址，并预留构建时间和磁盘空间。资源有限时串行构建。

已有镜像应与应用版本及主机架构匹配；本次预构建镜像用于 x86_64 / `linux/amd64` 主机。Dockerfile 有 amd64 / arm64 分支，跨架构构建需相应 builder，不代表已发布或验证 `arm64`。导入镜像 tar 也不等于整个安装已离线。

## 安装：选择一条路径

### 路径 A：使用已有 Worker 镜像

**1. 下载、校验并解压应用部署包，安装 Python 依赖。** 使用 Release 附件中的 code-only ZIP，不是 GitHub 自动生成的 Source code ZIP。它包含服务端、正式前端静态资源、部署脚本及镜像构建输入，不含已安装依赖、完整前端源码、运行数据或 Docker 镜像。

确认最新 Release 已发布 **0.2.8** 后执行以下命令，校验成功后才解压到新空目录并安装；任一步失败即停止后续步骤。包内顶层为 `arena-0.2.8-linux-code-only/`：

```bash
RELEASE_URL='https://github.com/Y10ng/Arena/releases/latest/download'
ASSET='arena-0.2.8-linux-code-only.zip'
DOWNLOAD_DIR="$(mktemp -d "$HOME/arena-download.XXXXXX")" &&
  curl -fL "$RELEASE_URL/$ASSET" -o "$DOWNLOAD_DIR/$ASSET" &&
  curl -fL "$RELEASE_URL/$ASSET.sha256" -o "$DOWNLOAD_DIR/$ASSET.sha256" &&
  (cd "$DOWNLOAD_DIR" && sha256sum -c "$ASSET.sha256") &&
  UNPACK_DIR="$(mktemp -d "$HOME/arena-install.XXXXXX")" &&
  unzip "$DOWNLOAD_DIR/$ASSET" -d "$UNPACK_DIR" &&
  cd "$UNPACK_DIR/arena-0.2.8-linux-code-only" &&
  ./deploy/install.sh
```

后续 `./deploy/...` 命令均在这个应用根目录执行。安装位置可自行选择，不必使用示例目录。已有下载文件时，先在同目录执行 `sha256sum -c`，再解压到新空目录。旧安装不要执行这里的新安装流程覆盖原目录，请转到[部署包更新](#部署包更新)。

**2. 准备本版研究镜像与配套网关。** Worker 和 executor 共用研究镜像；网关、collector 和网络 helper 使用独立采集镜像。运行容器、挂载和任务停止边界不合并。

直接按固定远端 manifest digest 拉取两张预构建镜像；不再下载旧 Worker 应用层包，也不执行小应用层构建。Docker 复用本机已有相同层；任一步失败时先处理，不继续启动：

```bash
docker pull 'yong29/arena-worker@sha256:62c3e3b9a19b1306b77361e98ab1169fa2e41529c31bb7100c3bb23849895d16' &&
  docker pull 'yong29/arena-traffic-gateway@sha256:c1ae1c054cc3b12b0648d06476cc7af86a13208b3f366d3ef0c1a3f9c3b41e10'
```

Worker 的公开 tag 为 `yong29/arena-worker:0.2.7`，安装固定上述 digest，避免浮动 tag 改变实际内容。也可导入可信分发方准备的配套镜像 tar（`docker load --input /绝对路径/配套镜像.tar`），但旧 Worker tar 不适用；导入后须核对实际镜像引用，不假定 tar 保留 registry digest。导入最终镜像不等于拥有多阶段构建缓存。

**3. 在 `runtime/dispatch.yaml` 中设置实际镜像名。** 默认 `images: []` 自动注册主 Worker，不必再填一套映射。网关不是 pi Worker，不要把它加入 Worker 注册列表。以下只展示待修改字段，不要覆盖完整文件：

```yaml
container:
  image: "yong29/arena-worker@sha256:62c3e3b9a19b1306b77361e98ab1169fa2e41529c31bb7100c3bb23849895d16"
  traffic_gateway_image: "yong29/arena-traffic-gateway@sha256:c1ae1c054cc3b12b0648d06476cc7af86a13208b3f366d3ef0c1a3f9c3b41e10"
```

上面的 YAML 对应按 digest 拉取的配套两镜像。使用 `docker load` 时改填实际导入的 tag，并自行保证版本配套。新安装不要保留缺失的默认 `:local` 引用，以免启动时触发源码构建。`start.sh` 不负责拉取新版；新包 `update.sh` 则会准备发布 manifest 指定的镜像，并统一迁移官方引用（包括本地 `:local` 与版本 tag）。已有显式 `images`／profiles／允许镜像列表也需对应实际选图，见后面的按需说明。

完成后进入 [修改端口和常用配置](#修改端口和常用配置)，默认值合适时直接 [启动并登录](#启动并登录)。如果只有源码而没有部署包，按路径 B 准备应用与前端；仅在源码 revision 与发布镜像确实配套时，才可用预构建镜像代替源码构建。

### 路径 B：从源码完整安装

在准备存放源码的位置执行；`git clone` 使用新目录，不覆盖已有工作区。以下示例获取 `main` 并完整构建该 revision 的镜像；如需固定发布版，可在构建前检出 Release 页面实际存在的 tag。按 tag 检出时处于 detached HEAD 是正常现象，不把任意源码直接配到上方发布镜像：

```bash
git clone --branch main --single-branch https://github.com/Y10ng/Arena.git
cd Arena
./deploy/install.sh
npm --prefix cairn/frontend ci
npm --prefix cairn/frontend run build:server
docker build -t arena-worker:local container/ &&
  docker build -t arena-traffic-gateway:local container/traffic-gateway/
```

- `install.sh` 用 `uv sync --frozen --no-dev` 安装 Python 依赖，只创建缺失配置，不启动服务、不构建前端或镜像。
- `build:server` 包含类型检查，生成由 Server 提供的正式前端；更新 `cairn/src/cairn/server/static/index.html` 并保留旧 hash 资源以支持已打开页面的延迟加载，不要在静态目录中存放私有文件。无需启动 Vite 开发服务，`build:verify` 不能代替正式构建。
- 两个构建上下文为 `container/`、`container/traffic-gateway/`。研究镜像已包含执行服务和浏览器证书工具，不再构建独立 executor 镜像。使用自定义 tag 时修改 `container.image`、`container.traffic_gateway_image` 及已有显式研究镜像映射。

`start.sh` 自动构建缺失的默认研究镜像和网关镜像，并分别做无网络检查；已有同名镜像只检查、不自动刷新。旧 Worker 缺少执行服务时会明确报错，须先重建。自定义镜像需先自行 build、pull 或 load，脚本不静默覆盖，也不在失败后回落默认镜像。完成后使用下面的共用配置与启动步骤。

## 修改端口和常用配置

`install.sh` 创建 `runtime/arena.env` 和 `runtime/dispatch.yaml`，保留已有文件。默认监听 `0.0.0.0:8000`，本机访问 `http://127.0.0.1:8000`。默认数据库会在首次启动时自动准备，不必手工填写密码、连接 URL、keyring 或 token。

### 临时改端口

从应用根目录启动，例如本次使用 8080：

```bash
./deploy/start.sh --host 0.0.0.0 --port 8080
```

仅本机使用时可将 `--host` 改为 `127.0.0.1`；8080 也可换成其他可用端口。参数**不会写回文件**，下次不传参数时恢复文件值。

### 持久设置与重启

编辑 `runtime/arena.env` 中已有字段：

```dotenv
ARENA_SERVER_HOST=0.0.0.0
ARENA_SERVER_PORT=8080
```

监听参数优先级为 **命令行 `--host/--port` > 环境文件中的 `ARENA_SERVER_HOST/ARENA_SERVER_PORT` > 脚本默认值 `0.0.0.0:8000`**。启动脚本先读取环境文件，其中的赋值会覆盖同名终端 `export`；持久配置应写入文件。

**已运行系统**修改端口、地址或环境配置后，从同一应用目录执行：

```bash
./deploy/stop.sh
./deploy/start.sh
```

要临时覆盖时，将第二行替换为带 `--host/--port` 的命令。此操作会中断应用服务，让 Server 和 Dispatcher 一起加载新配置；再次运行 `start.sh` 可能复用旧进程，不能当作通用重启。

**改端口不需要改源码、重建前端或镜像，也不需要修改 YAML 的 `server` 字段**：启动脚本会传入 Dispatcher 实际连接地址。本机 Origin 未自定义（留空或保留模板默认值）时，会随端口自动生成 `localhost` / `127.0.0.1` Origin，不必重复填写。

### 局域网或域名访问

若主机地址为 `192.168.1.10`，浏览器使用 `http://192.168.1.10:8080`，在首次远程登录前将 `runtime/arena.env` 改为：

```dotenv
CAIRN_TRUSTED_ORIGINS=http://192.168.1.10:8080
```

替换为浏览器实际使用的 **scheme、host、port**，不带页面路径；域名访问同理。多个 Origin 用逗号分隔，需要同时本机访问时可追加本机 Origin。已经自定义此项后，改变访问端口或域名时也要同步修改，并按上面的方法重启。HTTPS 反向代理时设置 `CAIRN_SESSION_COOKIE_SECURE=1`，直接 HTTP 保持 `0`。监听地址不是浏览器 Origin，不填写 `http://0.0.0.0:8080`。

### 常用配置在哪里改

| 配置 | 修改位置 | 生效方式 |
| --- | --- | --- |
| 监听地址、端口、浏览器 Origin | `runtime/arena.env`：`ARENA_SERVER_HOST`、`ARENA_SERVER_PORT`、`CAIRN_TRUSTED_ORIGINS` | stop/start；临时监听参数可在 start 时传入 |
| 外部数据库、持久存储路径 | `runtime/arena.env`：`CAIRN_DATABASE_URL`、下文的数据目录变量 | stop/start；换目录不自动搬迁已有数据 |
| Worker 镜像、并发、任务超时 | `runtime/dispatch.yaml`：`container.image`、`runtime.max_workers` 等、`tasks.*.timeout` | 一般配置由调度循环重载；不自动升级已建容器或改写在途执行 |
| 自带数据库端口、密码 | 首次启动自动生成的 `runtime/postgres.env`：`CAIRN_POSTGRES_PORT`、`CAIRN_POSTGRES_PASSWORD` | 修改后 stop/start，脚本同步数据库密码和连接地址 |
| 模型凭据、Worker 启用与模型绑定 | 登录后的 Models / Workers 界面 | 保存后供调度读取，无需复制到 YAML；不代表在途任务已经换配置 |

只修改需要的字段，不用整份模板覆盖配置。部署脚本参数使用 `ARENA_*`，Python 服务部分设置仍叫 `CAIRN_*`，这是现有接口，不需要全局改名。

## 启动并登录

两条安装路径完成后，在应用根目录按默认配置启动；需要持久设置或远程访问时先完成上一节：

```bash
./deploy/start.sh
./deploy/status.sh
```

临时覆盖时，用带 `--host/--port` 的启动命令替换第一行；已经启动则只执行 `status.sh`。启动脚本会准备数据库 schema、模型凭据 keyring、Dispatcher 身份和首次管理员，然后启动 Server / UI 与 Dispatcher。无需另外执行安装预检。

1. 浏览器打开 `http://127.0.0.1:8000`，或前面设置的实际地址。默认管理员登录名为 **`admin`**。
2. 首次随机密码在交互终端显示一次；非交互启动保留在仅当前用户可读的 `runtime/secrets/initial-admin-password`。登录后改密，若密码交付文件仍在则删除；后续启动不重置已有密码。不要将密码或文件内容粘贴到 Git、日志或问题报告。
3. 在 **Models** 中填写协议、完整推理地址、Provider Model ID 和 API Key，并启用模型；在 **Workers** 中创建并启用绑定该模型的 pi Worker。
4. 在 `status.sh` 输出中查看 Dispatcher heartbeat。它与 `running` 分开显示：`running` 只说明进程与 HTTP 基本可用，不证明模型或真实任务可用。

普通账号由管理员在 Users 界面创建，初始密码为 `Arena@2026`，首次登录必须更改；已有账号可通过界面重置密码。

准备开始探索时创建项目、选择 Worker，用 Origin / Goal 填写背景、目标和实际授权范围；内置 `goal-driven-v1` 是默认场景。**创建项目可能立即调度，模型连通性测试和探索可能产生模型费用**，不是安装时必须执行的验收步骤。

## 按需配置

默认值可用时跳过本节。数据库、数据目录、模型与 Worker 都可按实际需要选择，不必为了首次使用配置所有参数。

<details>
<summary>使用已有 / 外部 PostgreSQL，或调整自带数据库</summary>

在 `runtime/arena.env` 填入实际连接 URL，例如：

```dotenv
CAIRN_DATABASE_URL=postgresql://arena_user:URL_ENCODED_PASSWORD@db.example:5432/arena_db
```

这是占位示例。数据库和账号需预先存在，账号应能应用 schema 迁移；密码中的 `@`、`:`、`/`、`%` 等字符需 percent-encode。外部模式只连接该库，不启动或修改自带数据库，失败也不会回退到另一套空库。启动脚本会清除终端中的 `CAIRN_DATABASE_URL` 再读取文件，因此不要只用 `export` 配置。

不设置外部 URL 时，自带数据库默认使用 `postgres:16-alpine`，绑定 `127.0.0.1:55432`，数据库 / role 为 `cairn`，Compose project 为 `arena-db`，service 为 `arena-postgres`，默认实际卷名为 `arena-db_arena_postgres_data`。`runtime/postgres.env` 的密码首次自动生成；之后改端口或密码只编辑这一份文件，再按共用重启方法生效，不必复制连接 URL。

自带脚本的数据库名 / role 固定为 `cairn`；需要其他名称或实例时使用外部 URL，不必修改源码。排查自带数据库时，从应用根目录执行：

```bash
docker compose -p arena-db --env-file runtime/postgres.env \
  -f deploy/postgres.compose.yaml logs --tail=100 arena-postgres
```

数据库卷不是应用文件。不要用 `docker compose down -v` 重启或升级，也不要为目录改名更换原来的数据库。

</details>

<details>
<summary>显式 Worker 镜像映射、执行画像与并发</summary>

默认 `images: []` 根据 `container.image` 生成注册项。已有显式 `images` 时，实际执行按以下链路选择镜像：

```text
场景 profile_id → execution_profiles[].image_ref → images[].image
```

`image_ref` 是注册 ID，不是 Docker tag；修改使用的注册项，保留画像中已有网络、capabilities、内存、swap、pids 设置。启动和更新通过同一个 `DispatchConfig` 加载器检查基线 `container.image` 以及注册 / 允许选择的镜像；显式画像中的引用也必须有效，不会因基线镜像可用就忽略其他引用。无需为了默认安装新建显式列表。

并发由 `runtime.max_workers`、`max_running_projects`、`max_project_workers` 等设置；任务超时在 `tasks` 中。修改后由调度循环读取，加载失败会保留上一份有效配置，原因见 Dispatcher 日志。Server 地址或执行身份变更需要显式重启；镜像和画像修改不会自动重建存量容器。

`container.dns: []` 使用 Docker 默认 resolver；确有解析问题时才填写可用的 DNS IP。代理和下载源按所在网络配置，不需要禁用 TLS 校验。`runtime.playwright_session_policy: task` 在同一 TaskRun 的执行与收尾间复用浏览器，完成后定向关闭，不代表可以批量结束其他浏览器进程。

</details>

<details>
<summary>数据目录与凭据保留</summary>

| 默认位置 | 内容 / 可选覆盖变量 |
| --- | --- |
| `runtime/data/artifacts/` | 任务产物；`CAIRN_ARTIFACT_ROOT` |
| `datas/cairn/project-resources/` | 项目上传与暂存；`CAIRN_PROJECT_RESOURCE_ROOT` |
| `datas/cairn/advisory-library-content/` | 全局资料库正文；`CAIRN_ADVISORY_LIBRARY_ROOT` |
| `runtime/data/prompts/`、`runtime/data/knowledge/` | 受管 Prompt 与知识资料 |
| `runtime/secrets/` | 完整模型凭据 keyring、Dispatcher token 等 |

持久数据覆盖变量写入 `runtime/arena.env`，建议使用绝对路径，stop/start 生效。更换路径前按需搬迁原材料，脚本不会代办。上传配额等低频参数参见源码中的 `deploy/arena.env.example`。脚本配置目录固定在所选应用目录下的 `runtime/`，不是所有运行目录都支持环境变量重定向。

保留模型凭据需要数据库及匹配的完整 `model-credential-keyring.json`，包括历史 key versions；Dispatcher token 与数据库机器身份匹配，不要重新生成文件覆盖已有配置。外置 keyring 可用 `CAIRN_MODEL_CREDENTIAL_KEYRING_FILE` 指定，并保留文件属主和 `0600` 权限。

PostgreSQL 备份不包含这些文件；资料库定义在数据库、正文在目录中，应一起保存。私有配置、上传和执行材料不要提交 Git、打入源码包或交给静态文件服务器。

</details>

## 日常管理与更新

从应用根目录按需执行，以下是独立操作，不是需要顺序全部运行的清单：

| 命令 | 用途 |
| --- | --- |
| `./deploy/start.sh` | 启动 Server / UI 和 Dispatcher |
| `./deploy/status.sh` | 查看进程、HTTP 与单独的 Dispatcher heartbeat |
| `./deploy/logs.sh` | 显示最近 80 行 Server / Dispatcher 日志，不持续跟随 |
| `./deploy/stop.sh` | 停止应用，不停止 PostgreSQL、不删除数据库卷 |

`stop.sh` 不等于所有 Worker 执行都已退出。更新或改变执行环境前，先结束或暂停相关任务并确认执行退出。`--server-only` 仅供维护，不启动新的 Dispatcher，也不会停止已运行的 Dispatcher；需要仅运行 Server 时先停应用，再执行 `./deploy/start.sh --server-only`。不要从两个安装目录同时启动连接同一数据库、使用同一身份的 Dispatcher。

### 读取暂停或结束后的执行材料

在应用根目录使用本机运维 CLI；它只依赖当前 Docker daemon，不连接业务数据库、不调用模型，也不经过 Tenant Web API 鉴权。执行人须有本机 Docker 权限；读取 bind/volume 文件还须能读取其宿主源路径。不要把这个入口当作不可信租户的文件沙箱或开放为远程下载接口。

```bash
# 先取得精确项目下的完整 container_id，包括已保留的研究、独立报告和测试 executor。
cairn/.venv/bin/cairn materials containers --project '实际项目ID'

# 省略 --path 只返回支持的材料入口，不下载 HOME/TMPDIR 整棵目录。
cairn/.venv/bin/cairn materials list --project '实际项目ID' --container '完整64位容器ID'

# 选择已知材料子目录做有界索引，或直接查某个文件。
cairn/.venv/bin/cairn materials list --project '实际项目ID' --container '完整64位容器ID' \
  --path /home/kali/workspace/research

# 保存单个普通文件的原始字节；输出文件必须尚不存在，父目录须已存在。
cairn/.venv/bin/cairn materials save --project '实际项目ID' --container '完整64位容器ID' \
  --path /tmp/c1-control-layer --output /安全目录/新的材料文件.bin
```

- `list` / `save` 只读取 `paused`、`exited`、`created`、`dead` 的静止现场，**不会 exec、start、unpause、停止或删除容器**。活动现场会明确拒绝；不要为了下载材料擅自暂停真实任务。容器名称、短 ID、名称前缀和其他项目的 ID 都不能替代完整 ID 与归属标签核验。
- 支持标准 Worker 的 `/home/kali`（含 workspace、浏览器 profile）、`/tmp`、`/var/tmp` 下的材料。`/`、其他 Linux 系统目录不开放；HOME、`/tmp`、`/var/tmp` 只作为入口，不能整根归档，应选择具体文件或材料子目录。根入口清单不是文件存在性证明。
- `.cairn`、pi 私有配置、prompts、执行 gate/socket、capture CA、egress private、未知 `.config` 子目录及私有挂载别名不开放；浏览器 `.config/chromium`、`.config/google-chrome`、`.config/ms-playwright` 可作为研究材料读取。研究/报告的 writable-layer 导出要求当前私有 control 与 prompts 已分别挂载，旧的未隔离现场会明确拒绝，不通过启动或复制控制配置补齐。
- 目录索引来自 Docker tar 流，最多读取 8 MiB / 200 条记录；遇私有子树也会停止。`truncated: true` 与 `limit_reason` 表示不完整，不能当作全部材料清单；请缩小到已知子目录，不会后台下载整盘。索引不导出文件正文，`blocked_special` 的链接、socket 等特殊项不能作为普通文件保存。
- 保存会逐父路径检查 symlink，拒绝 tar 中的 symlink / hardlink、目录冒充与额外文件。bind/volume 文件另经宿主 `O_NOFOLLOW` / `fstat.nlink` 核验；**Docker archive stat 不提供 writable-layer inode 的 nlink，因此 layer 只验证 tar 可观测的链接类型，不宣称证明 layer inode 没有 hardlink**。结果的 `hardlink_check` 区分 `host_nlink_and_tar` 与 `tar_only`。同一文件系统仍可能被其他宿主进程或共享挂载写入，CLI 不提供并发写入的一致性快照。
- 保存只在私有临时文件完整写入后原子无覆盖发布，失败删除临时字节，不覆盖已有文件或输出 symlink。材料不是可执行输入；下载后先审阅，不直接运行其中脚本。此入口读取保留的容器材料，不替代 Server 中已接受 Artifact 的现有下载，也不读取已删除的容器或业务材料。

### 源码更新

当前没有从 GitHub 获取代码并完成整套升级的源码一键更新器。先在原应用根目录执行 `git status --short` 和 `git branch --show-current`，保留并处理本地修改；不要用 `reset --hard` 或清理工作区代替处理。以下示例适用于**当前已在 `main`、工作区无待保护改动，且 `origin` 指向预期仓库**的安装；按 Release tag 安装的 detached HEAD 和开发分支不适用，不自动切换到 main。更新到其他源码 revision 时应使用该版本配套镜像或重新构建，不能把 `v0.2.1` 镜像视为任意 main 的最新版。

先阅读下方[本次跨版本升级注意](#本次跨版本升级注意)，确认迁移条件、原件目录与一致备份齐备，相关任务已退出且处于获准维护窗口。**存在待迁移历史会话的安装不在本轮完整升级支持范围内**，不要直接套用。先 fetch 并确认可以快进，再停服务；任一步失败即停止，不继续启动半套环境：

```bash
(
  set -e
  test -z "$(git status --porcelain)" || { printf '请先处理本地修改\n' >&2; exit 1; }
  test "$(git branch --show-current)" = main
  git fetch origin main
  git merge-base --is-ancestor HEAD origin/main
  ./deploy/stop.sh
  git merge --ff-only origin/main
  ./deploy/install.sh
  npm --prefix cairn/frontend ci
  npm --prefix cairn/frontend run build:server
  # 自定义镜像需使用实际研究镜像与网关 tag，保留原配置。
  docker build -t arena-worker:local container/
  docker build -t arena-traffic-gateway:local container/traffic-gateway/
  ./deploy/start.sh
  ./deploy/status.sh
)
```

这是可复制的更新步骤，不是已实现的自动更新脚本。Docker 会复用可用构建缓存；不要添加 `--no-cache` 或清理缓存来完成普通更新。已有同名镜像不会被 `start.sh` 自动刷新。重建或拉取同名 tag 也不会改变已经创建的容器；不要将删容器作为默认升级步骤，运行边界不匹配时见排障，先保留项目工作区。

### 部署包更新

**升级目标为 0.2.8，操作前确认 Release / 渠道版本。** 本次修复更新器导入失败，复用 0.2.7 的 Worker 与固定 digest 的网关，无新增数据库迁移。安装目录改名后无需改回原名；新包显式加载自身源码，只复用旧 Python 环境中的第三方依赖，运行配置中显式填写的其他绝对路径仍须指向实际位置。**v0.2.6 ZIP 安装**可在管理端检查 → 准备 → 应用，也可采用下面的手动入口；**v0.2.5 及以前**直接运行新包 `update.sh`，不必先升到 v0.2.6。旧 v0.2.6 页面准备阶段不具备完整两镜像逻辑，其兼容边界及失败处理见[系统一键更新](#系统一键更新)。

更新前先暂停或结束任务，确认真实执行已退出，在维护窗口保留数据库、`runtime`、完整 keyring / token、资料和外置路径的一致备份。不用 `--server-only` 绕过执行环境准备；不把“没有新增数据库迁移”理解为历史迁移阻断已经解除。

**存量项目限制：** 更新镜像 tag 或宿主应用不会自动替换已有容器、批量补齐所有旧工作区或重跑任务。公共研究准备会在符合原容器复用条件时准备输入；材料增补改变 bundle digest 时仍可能触发既有签名不匹配。已有 `inputs/` 必须与本次来源清单完全一致才能复用；用户目录、修改或不完整副本均保留并报冲突，不删除业务材料自愈。不以已完成隔离验证宣称存量恢复和正式模型流程均已验收。

**自定义静态目录：** 新包 `update.sh` 沿用原安装的环境加载规则读取 `CAIRN_STATIC_DIR`，自动同步外置静态内容，不改写配置路径。支持绝对路径、相对原安装根目录的路径，以及显式配置的根目录 symlink；待同步路径内部的 symlink 会拒绝，不沿链接覆盖其他用户内容。停服前先预检冲突，安装与镜像准备完成后再次检查并增量补齐新资源，最后原子替换 `index.html`。**旧 hash 与未知文件保留；除 `index.html` 外，任何同名异内容（包括无 hash 的定制资源）都会阻止升级**，不覆盖或清空用户目录。外置路径不能指向临时新包目录或其他待整体替换的程序目录。若不再需要外置部署，可在维护窗口停服后改为原安装的 `cairn/src/cairn/server/static` 真实绝对路径；无论哪种路径，同步成功仍需在更新后刷新浏览器检查实际入口。

取得配套新包，校验后像路径 A 一样解压到**原安装目录之外的新空目录**，不在新目录运行 `install.sh` 或 `start.sh`。先核对[跨版本升级条件](#本次跨版本升级注意)；有待迁移历史会话时，不要把包更新器当作文件／session 迁移入口。新更新器拒绝目标中的 `.git` 目录或 worktree 文件，Git 安装必须走[源码更新](#源码更新)，不能调用 ZIP `update.sh` 覆盖工作区。

确认任务退出、备份和维护窗口后运行**新包中的**更新器，替换下例两个绝对路径；不必预先手工停止宿主应用，也不必手工迁移官方镜像引用：

```bash
/新包解压目录/arena-0.2.8-linux-code-only/deploy/update.sh --target /现有应用根目录
```

一条入口依次完成：**停服前校验发布 manifest → 准备并离线验收 Worker 与网关 → 核对配置 revision 与镜像身份 → 停止宿主应用 → 按 revision 原子迁移官方镜像引用 → 替换受管程序和静态资源 → install 同步依赖 → start → 检查服务状态**。镜像准备使用新包中的检查器和原安装的 Python 环境；缺少官方镜像时按包内固定 digest 拉取，不再为官方 `:local` 运行完整源码构建。manifest、镜像或配置检查失败时在停服前中止；配置变化不静默覆盖，须重新准备。沿用现有 PostgreSQL，本次无新增数据库迁移，也不升级 PostgreSQL 大版本。

- **官方引用统一迁移：** `yong29/arena-worker` / `yong29/arena-traffic-gateway` 的引用，以及本地默认名、`:local`、`:latest` 和版本 tag（如 `arena-worker:0.2.3`）切换到包内对应 digest。覆盖 `container.image`、`container.traffic_gateway_image`、`container.allowed_images` 和 `images[].image` 中的官方项；注册 ID、profile 绑定和其他配置保留，不只改基线镜像。准备阶段不写回新引用，停服后通过配置 revision 校验原子写入。
- **真正自定义引用保留：** 如 `arena-worker:custom` 或私有仓库镜像，不自动覆盖、拉取或重建。升级前自行准备与新应用配套的镜像，新更新器在停服前离线验收；缺失或不兼容明确拒绝，不回退官方镜像，也不靠伪造标签绕过。离线通过仍不证明自定义能力的真实任务兼容。
- `-- --server-only` 明确跳过研究与网关镜像准备和引用迁移，仅启动 Server，不表示执行环境已更新；仅用于有意选择的维护模式，不是准备失败后的绕过办法。
- **复用未变化的镜像层：** Docker 按固定 digest 准备预构建镜像，已存在的相同层不重复下载，不清理旧镜像或工具缓存。源码安装继续使用其配套源码构建路径，缓存边界见[增量下载的实际边界](#增量下载的实际边界)。

更新保留 `runtime`、完整 keyring / token、数据库卷、项目资料、Artifact、外置存储与 Python 环境位置，依赖仍会同步。tag 更新只影响后续新建容器，**不替换已有项目容器、不恢复或重跑任务**；宿主服务停止也不证明 Worker 已退出。更新前应结束或暂停相关任务并确认执行退出。它不是源码同步器，不用于需要保留开发源码或受管目录本地改动的 Git 工作区。临时监听参数可在末尾通过 `-- --host 0.0.0.0 --port 8080` 原样传入；持久配置仍修改原安装的 `runtime/arena.env`，已有外置静态资源路径不会被改写。

失败时按输出检查，修复后**重新运行新包的完整 `update.sh --target ...` 命令并保留原参数**，不要直接 start 而漏掉两镜像的刷新准备。迁移阻断按原错误处理，不删除项目、报告、归档材料或改 SQL 绕过。旧程序临时备份只覆盖程序文件，不含数据库 / runtime，也不保证依赖、配置、镜像或数据库整体回滚；install 可能已改变依赖或 legacy 私有配置，start 可能已执行迁移。升级前按需备份数据库、数据目录、私有配置、完整 keyring（含历史版本）和 token，外置文件一起保留。

如需自行生成部署包，在完整源码根目录安装好依赖并完成 `build:server` 后执行；CLI 必须同时提供 **Worker ref + digest、gateway ref + digest 四参数**，例如本次配套镜像：

```bash
python3 deploy/package_release.py \
  --worker-reuse-version \
  --worker-image-ref yong29/arena-worker:0.2.7 \
  --worker-digest sha256:62c3e3b9a19b1306b77361e98ab1169fa2e41529c31bb7100c3bb23849895d16 \
  --gateway-image-ref yong29/arena-traffic-gateway:latest \
  --gateway-digest sha256:c1ae1c054cc3b12b0648d06476cc7af86a13208b3f366d3ef0c1a3f9c3b41e10
```

版本取自源码项目版本，打包前须确认与目标发布版本一致。默认产物为 `dist/arena-<version>-linux-code-only.zip`、校验文件与 `dist/release.json`；两镜像的固定来源同时写入包内 manifest 和渠道文档。ZIP 不包含 Docker 镜像；打包成功也不代表 Git tag / Release 已创建。这是可选分发操作，不是使用已有包的前置步骤。当前仓库没有随 push 自动打包、发布镜像的 workflow；本地 `update.sh` 不下载新应用包，也不默认跟随 `main`，缺少的官方配套镜像则会按 manifest 拉取。

### 系统一键更新

管理端「设置 → 系统更新」提供 **检查 → 准备 → 应用**，仅对 ZIP 安装开放。v0.2.6 ZIP 已有该入口；Git 源码安装使用[源码更新](#源码更新)的依赖、正式前端与镜像构建步骤，**不能用 `deploy/update.sh` 覆盖 Git 工作区**。v0.2.5 及以前无该管理入口，直接用新包 `update.sh`，不要求中转 v0.2.6。

- 更新渠道：默认读取 `https://github.com/Y10ng/Arena/releases/latest/download/release.json`，可通过环境变量 `CAIRN_UPDATE_CHANNEL_URL` 覆盖为自建地址；`CAIRN_UPDATE_PROXY` 可选设置代理，不默认任何地址。升级前核对 `version` 为目标版本；渠道显示旧版时不能把旧包当作本版。
- `release.json` 约定（`arena-release-channel/v1`）：包含 `version`、`notes_url`、`app.asset/sha256/size`、`worker.reuse_version`；新包还明确记录 `images.worker.image/digest` 和 `images.traffic_gateway.image/digest`，与包内 manifest 的两镜像来源一致。渠道附件自 v0.2.6 起提供；`worker.reuse_version` 不能代替 Worker 固定来源。
- 新版流程：检查版本 → 下载 ZIP 并校验 SHA256 → 安全解压并验证 manifest → 按发布来源准备、离线验收两镜像（不改运行配置）→ 应用时调用**新包** `update.sh` 再做停服前检查 → 停服后按 revision 原子迁移官方镜像引用 → 替换并重启。真正自定义引用保留并提前验收。
- **旧 v0.2.6 页面首次升级边界：** 旧 prepare 可能仍只拉取、校验网关，并提前改写同仓库网关引用；它使用 `RepoDigests[0]`，同一镜像已有多个 digest 时可能误报网关 digest 不匹配。新包无法修复尚未调用其更新器之前的旧 Server 行为，因此不保证所有旧页面首次准备都成功。若旧页面准备失败，按[部署包更新](#部署包更新)下载并校验新包，直接运行新包 `update.sh --target ...`；不要反复 pull、删镜像或清理缓存。旧页面准备成功也不代表新 Worker 已验收，完整新逻辑在新包 apply 阶段才执行。
- 运行边界：先暂停或结束项目并确认执行已退出，再在维护窗口应用；宿主服务停止不等于所有 Worker 已退出。更新不替换已有容器，也不自动恢复或重跑项目。
- 状态恢复：若更新器进程死亡超过 30 秒，状态自动标记为 failed；服务重启导致 preparing 中断时标记为 interrupted，可重新执行检查更新。状态恢复不代表业务或数据库回滚。
- 失败排查：更新日志位于 `runtime/logs/update-apply.log`；apply 失败后按日志修复，再运行**新包中的**完整 `deploy/update.sh --target /现有应用根目录` 并保留原参数，不直接启动半套环境。

### 增量下载的实际边界

- **拉取预构建镜像：** Docker 按内容寻址的镜像层下载，本机已有的相同层会复用。它不是按单个文件打补丁；某层变化，需要下载新的整层。把稳定工具层放在应用层之前、发布时保留构建缓存，才能让只改应用的版本继续共享大工具层。
- **本地构建：** 主 Worker 已把工具安装放在应用和指引 COPY 之前；仅应用或指引变化、基础镜像及安装输入不变且缓存仍有效时，可复用已有工具层。工具版本、依赖输入、基础镜像或架构变化，以及缓存缺失，仍会重新下载相应层；新增工具也不保证只下载新增那个文件。
- **研究镜像已合并：** 执行服务直接复制到 Worker 镜像，`libnss3-tools` 与字体位于应用、执行服务和指引之前，删除独立 executor Dockerfile。Node 与 Kali 基础引用固定 digest，普通应用改动不会因浮动 `latest` 悄悄更换基础；工具或基础升级另行显式处理。网关保持自己的 Python、mitmproxy 与 pproxy 依赖层。
- 不默认运行 Docker prune，不删除旧工具缓存，不把最终镜像的 `load` 当成导入了完整多阶段构建缓存。首次安装或缓存不存在时仍需完整准备相关依赖。

### 本次跨版本升级注意

**升级到 0.2.8 无新增数据库迁移，Worker 复用 0.2.7 版本，网关复用上述固定 digest。v0.2.6 ZIP 可从管理端检查 → 准备 → 应用；v0.2.5 及以前直接用新包 `update.sh`，无需先升到 v0.2.6。** 新包会准备并离线验收两镜像，迁移官方配置引用，不会替换既有容器或自动恢复、重跑项目。下述 v76–v83 等要求针对更早版本进入 v0.2.1 的历史升级链，不表示本版又新增这些迁移；此前已遗留的迁移阻断不会自动解除。

`install.sh` 会一次性转换旧标准 `container.task_executor_image: arena-task-executor:local`：删除旧字段，executor 改用实际选中的研究镜像；旧值已与 `container.image` 相同也可直接删除。其他自定义 executor 值会在写配置和凭据前明确阻断，不静默丢弃。应先将定制能力并入对应研究镜像（包含执行服务、证书工具与 pi runner），确认实际 `images/profile` 映射后移除旧字段，再重新安装；无需保留旧键作为 fallback。没有旧键的配置不需要转换。默认镜像重建只影响新建容器，旧项目的控制容器仍保留；镜像引用变化触发既有复用签名检查，不能以重打同名 tag 或重启应用宣称所有历史容器已升级。

v0.2.1 发布链新增 **PostgreSQL v76–v83**：独立会话归属、漏洞人工处置、物理执行控制、会话研究与回收、暂停时钟、资料选择、当前资料正文、模型响应级用量与价格快照。Server、Dispatcher、正式前端与两镜像应配套升级。更早的迁移条件仍有效：v61 要求没有 pending / running 报告；v71 拒绝无法明确映射到 bootstrap / reason / explore 的普通 Worker、重复角色和异常 owner 关系，补建 Worker 后仍需配置模型；旧模板键冲突等按实际错误处理，不清库、不改已应用迁移 checksum。

**历史会话：自动 schema 迁移不是无损续聊迁移。** v76 遇到没有对应消息的 chat run 会拒绝升级；有运行历史的会话可能留下 `session_verification_required`，旧附件／产物可能留下 `conversation_file_migration_required`。当前 `update.sh`、`start.sh` 和 CLI **没有调用旧会话文件／SDK session 的显式迁移函数，本轮不提供通用运维入口**。有这些数据的旧安装应先保留原环境与一致备份，另行安排基于原件的迁移，不直接执行上述整套升级后期待续聊。新安装或无此类历史数据的安装仍需满足其他 schema 前置条件。

源码中的 `migrate_conversation_files` 和 `migrate_local_conversation_session` 不是可直接复制执行的升级命令。前者需托管事务、原 Artifact 根目录及摘要匹配的原件；后者还需原机器的真实 Dispatcher 身份、原 Server 地址／挂载、仍存在且为 exited/created 的原聊天容器、最后写入 run 和可验证的 SDK manifest/session。缺失归属、原容器／原件、遗留锁或 SDK 证据时只保留阻断，不能猜目录、改状态假解锁或启动历史容器。文件迁移普通事务回滚清理本次副本；session 迁移失败可能保留副本并在重试时严格比对，不承诺任意中断后自动恢复。

**v82 在首次数据库初始化时读取原资料正文和旧知识目录。** 升级前在原安装 `runtime/arena.env` 明确保留 `CAIRN_ADVISORY_LIBRARY_ROOT`（默认相对应用根目录的 `datas/cairn/advisory-library-content`）、`CAIRN_KNOWLEDGE_ROOT`（脚本默认 `runtime/data/knowledge`）和 `CAIRN_ARTIFACT_ROOT`，外置路径建议为绝对路径。本版 `start.sh` 在 bootstrap 之前设置这些运行路径，保留显式知识根，不等到 Server 启动后才设置。资料库最新版本（含归档库）的文件必须存在、字节数／SHA256／UTF-8 正确；历史版本正文之后仍会读取原目录，不能在迁移后删除。

旧知识根缺失可能被当成没有材料，非 UTF-8、symlink 或非法路径可能跳过；**v82 成功不证明全部旧知识已导入**。必须在首次迁移前核对原目录；已经在错误目录下完成 v82 的安装、仅恢复数据库未恢复原文件／身份的安装，不在本轮自动修复范围内，补回目录后重启不会重做已完成迁移。不把 `db migrate --dry-run` 当作无副作用的资料检查，也不修改迁移记录强迫重跑。

升级后 ChatBot 是独立会话入口，项目作为可选上下文；创建／计划使用 `library_selections`，旧 `library_keys` 与资料库版本 CRUD 退出，流量汇总改为分页。新复测结果使用 v4；报告数据为 v6、分段输出合同仍为 v5，历史已接受材料保留，不承诺旧冻结执行直接续跑。完整变化见根目录 `CHANGELOG.md`。

## 排障

| 现象 | 处理方向 |
| --- | --- |
| Python / npm / Docker 构建下载失败 | 按失败地址检查网络、代理和对应软件源；源码 `npm ci` 注意前面的 Node 版本要求 |
| `curl` 经代理可连 Docker Hub，但 `docker login/pull` 仍超时 | 拉取由 Docker daemon 发起，仅在终端设置 `HTTP_PROXY`／`HTTPS_PROXY` 不保证生效。按 [Docker daemon 代理文档](https://docs.docker.com/engine/daemon/proxy/) 配置实际 daemon（远程 context 配远端）；需要重启时先安排维护窗口，可能影响运行容器。不要关闭 TLS 校验或把代理密码写进仓库；公开镜像无需为拉取额外提供发布者凭据 |
| 构建 exit 137 / OOM | 串行构建，按本机需要增加内存或 swap，不跳过正式前端类型检查 |
| Docker 或 Compose 不可用 | 按报错检查 daemon、当前用户权限或 Compose v2；可用 `docker info` 定位，不需要每次启动前再查一遍 |
| 数据库失败 / 端口占用 | 区分自带与外部库，查看对应日志；自带端口改 `runtime/postgres.env`，不要临时切到空库 |
| 配置改了未生效 | 确认改的是本安装的文件，检查命令行覆盖，按共用 stop/start 加载；YAML 重载失败看 Dispatcher 日志 |
| UI 仍旧或静态资源 404 | 源码安装重新执行 `build:server`，部署包使用带完整静态资源的配套包；检查 `CAIRN_STATIC_DIR` 是否指向旧目录 |
| `Untrusted request origin` / 登录 Cookie 问题 | 按浏览器实际地址修改 `CAIRN_TRUSTED_ORIGINS` 并重启；HTTP / HTTPS 对应检查 Cookie 设置 |
| 自定义镜像不存在 / pi 标签不匹配 | 先 pull、load 或 build 对应镜像；脚本目前要求 pi 1.0.0 / SDK 0.73.0 标签。更换配套镜像或显式重建，不靠手工伪造标签绕过；标签通过不等于真实任务已验证 |
| 模型可连但项目不运行 | 看 Models / Workers 的启用、绑定及可用原因，项目 Worker 选择、Dispatcher heartbeat 与 `runtime/logs/dispatcher.log` |
| `unsupported_model_config` | 根据错误检查完整推理地址、协议对应的 endpoint 后缀和请求头；当前 runner 不支持任意 endpoint，界面连接测试不等于 Worker 兼容 |
| 历史 Worker 引擎 / 不兼容的旧执行快照 | 使用当前 pi Worker；旧执行不自动转换，保留历史材料，按错误说明处理 |
| 改端口后旧执行无法续跑，或 runtime / container identity mismatch | 容器归属包含 Dispatcher 身份与 Server 地址，端口变化会改变归属；新身份不自动接管旧容器。先停止相关执行、保留工作区，再按日志处理，不强行认领或全局清理 |
| 模型凭据解密失败 | 恢复与数据库匹配的完整 keyring，不重新生成密钥覆盖旧文件 |

在界面删除项目与清理运行日志、历史容器或暂存文件不是同一操作。不要通过删整个 `runtime`、重置整库、删除数据库卷或全局 `docker system prune` 清理项目历史；保留需要继续使用的资料和凭据。

服务状态、镜像标签和离线 `cairn-agent --self-check` 都不能代替真实模型与任务验证。
