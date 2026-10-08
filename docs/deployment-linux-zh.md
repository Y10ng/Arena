# Arena：Linux 安装、配置与更新

本文是完整安装入口，两条路径共用后面的配置、登录和维护说明。

| 安装路径 | 适合情况 | 需要准备 |
| --- | --- | --- |
| [使用已有 Worker 镜像](#路径-a使用已有-worker-镜像) | 已有配套应用部署包和 Worker 镜像，省去本机构建 | 应用 ZIP、可拉取的镜像引用或镜像 tar |
| [从源码完整安装](#路径-b从源码完整安装) | 没有配套发布资产，或需要自行修改、构建 | Git、Node.js/npm，以及构建时的网络和资源 |

当前是**宿主机 Server / Dispatcher + Docker Worker**，Server 同时提供 API 和正式前端；Compose 只负责 PostgreSQL，不是全栈 Docker 部署。目前没有已核实的镜像或部署包下载地址，路径 A 的资产须从可信分发方取得，否则选择路径 B。

默认端口、镜像名、数据库和安装位置只是起点，可按需要选择；常见调整见 [修改端口和常用配置](#修改端口和常用配置)。

## 前置条件

两条路径都需要 Linux、Python 3.11+、[uv](https://docs.astral.sh/uv/getting-started/installation/)、可用的 [Docker daemon](https://docs.docker.com/engine/install/)，以及 `bash`、`curl`、`flock`（通常由 util-linux 提供）。默认使用脚本管理的 PostgreSQL，需要 [Docker Compose v2](https://docs.docker.com/compose/install/)；也可以连接已有 PostgreSQL。

- 路径 A 解压 ZIP 需要 `unzip`；已有正式前端和 Worker 镜像时，宿主机不需要 Node/npm。
- 路径 B 需要 Git、npm，建议 [Node.js 24 LTS](https://nodejs.org/en/download) **24.15.0 或更新的 24.x**；若选 Node 22，需 22.22.2+，以满足当前 `npm ci` 的完整依赖要求。
- Python 软件源须可访问；默认数据库首次使用还会下载 PostgreSQL 镜像。从源码构建另需访问 npm、容器仓库、Kali 软件源及 GitHub 工具下载地址，并预留构建时间和磁盘空间。资源有限时串行构建。

已有 Worker 镜像应与应用版本及主机架构匹配。Dockerfile 有 amd64 / arm64 分支，跨架构构建需相应 builder，不代表所有平台已经验证。导入镜像 tar 也不等于整个安装已离线。

## 安装：选择一条路径

### 路径 A：使用已有 Worker 镜像

**1. 解压应用部署包并安装 Python 依赖。** 使用 `arena-<version>-linux-code-only.zip`，不是 GitHub 自动生成的 Source code ZIP。它包含服务端、正式前端静态资源、部署脚本及 Worker 构建输入，不含已安装依赖、完整前端源码、运行数据或 Worker 镜像。

将下例 `APP_ZIP` 换为实际包的绝对路径。解压到新空目录，不覆盖已有安装；ZIP 自带一层 `arena-<version>-linux-code-only/` 目录：

```bash
APP_ZIP='/绝对路径/arena-实际版本-linux-code-only.zip'
UNPACK_DIR="$(mktemp -d "$HOME/arena-install.XXXXXX")"
unzip "$APP_ZIP" -d "$UNPACK_DIR"
cd "$UNPACK_DIR"/arena-*-linux-code-only
./deploy/install.sh
```

后续 `./deploy/...` 命令均在这个应用根目录执行。安装位置可自行选择，不必使用示例目录。

**2. 准备 Worker 镜像，以下二选一。** 镜像引用和 tar 路径都是待替换参数，不是已发布地址。

从实际仓库拉取：

```bash
WORKER_IMAGE='替换为实际仓库/镜像名:配套版本'
docker pull "$WORKER_IMAGE"
```

或导入可信镜像 tar：

```bash
WORKER_TAR='/绝对路径/worker-image.tar'
docker load --input "$WORKER_TAR"
```

**3. 在 `runtime/dispatch.yaml` 中修改现有 `container.image` 为拉取或导入后的实际镜像名。** 默认 `images: []` 会自动引用它，不必再填一套映射。以下只展示待修改字段，不要覆盖完整文件：

```yaml
container:
  image: "替换为实际镜像名:版本"
```

也可在新安装中将镜像标为默认名以省去 YAML 修改：`docker tag "$WORKER_IMAGE" arena-worker:local`；导入 tar 后先将 `WORKER_IMAGE` 设为其实际镜像名。改名不是必需步骤，已有默认 tag 时不要无意覆盖。已有显式镜像注册的配置见后面的按需说明。

完成后进入 [修改端口和常用配置](#修改端口和常用配置)，默认值合适时直接 [启动并登录](#启动并登录)。如果只有源码而没有部署包，按路径 B 准备应用与前端，Worker 构建这一步可改用上面的已有镜像。

### 路径 B：从源码完整安装

在准备存放源码的位置执行；`git clone` 使用新目录，不覆盖已有工作区：

```bash
git clone https://github.com/Y10ng/Arena.git
cd Arena
./deploy/install.sh
npm --prefix cairn/frontend ci
npm --prefix cairn/frontend run build:server
docker build -t arena-worker:local container/
```

- `install.sh` 用 `uv sync --frozen --no-dev` 安装 Python 依赖，只创建缺失配置，不启动服务、不构建前端或镜像。
- `build:server` 包含类型检查，生成由 Server 提供的正式前端；会清空 `cairn/src/cairn/server/static/`，不要在其中存放私有文件。无需启动 Vite 开发服务，`build:verify` 不能代替正式构建。
- Docker 构建上下文为 `container/`。`arena-worker:local` 是建议名；使用其他 tag 时同步修改 `runtime/dispatch.yaml` 的 `container.image`。

`start.sh` 也会构建缺失的默认镜像，但**不会重建已经存在的同名镜像**；自定义镜像需先自行 build、pull 或 load。完成后使用下面的共用配置与启动步骤。

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

`image_ref` 是注册 ID，不是 Docker tag；修改使用的注册项，保留画像中已有网络、capabilities、内存、swap、pids 设置。当前启动脚本还会检查 `container.image`，仅修改显式注册项不会改变它；保留它指向一个可用的配套镜像。无需为了默认安装新建显式列表。

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

### 源码更新

保留本地修改，按自己的分支更新源码；以下 `git pull --ff-only` 适用于已配置远端跟踪的工作区，从原应用根目录执行：

```bash
./deploy/stop.sh
git pull --ff-only
./deploy/install.sh
npm --prefix cairn/frontend ci
npm --prefix cairn/frontend run build:server
# container/ 有变化时执行；若使用自定义镜像名，替换构建 tag。
docker build -t arena-worker:local container/
./deploy/start.sh
./deploy/status.sh
```

已有同名镜像不会被 `start.sh` 自动刷新。重建或拉取同名 tag 也不会改变已经创建的容器；不要将删容器作为默认升级步骤，运行边界不匹配时见排障，先保留项目工作区。

### 部署包更新

取得配套新包，像路径 A 一样解压到**原安装目录之外的新空目录**。运行新包的更新器，替换下例两个绝对路径：

```bash
/新包解压目录/arena-实际版本-linux-code-only/deploy/update.sh --target /现有应用根目录
```

它会停止应用、替换受管程序和静态资源、刷新依赖并启动，保留 `runtime`、数据库卷、运行数据和 Python 环境位置。它不是源码同步器，不用于需要保留开发源码或受管目录本地改动的 Git 工作区，也不会刷新已有 Worker 镜像。临时监听参数可在末尾通过 `-- --host 0.0.0.0 --port 8080` 传入；持久设置仍修改原安装的 `runtime/arena.env`。

更新失败按输出处理，不能假定回退代码会恢复已经迁移的数据库。备份时同时保留数据库、数据目录、私有配置、完整 keyring 和 token。

如需自行生成部署包，在完整源码根目录安装好依赖并完成 `build:server` 后执行 `python3 deploy/package_release.py`，默认产物为 `dist/arena-<version>-linux-code-only.zip`。这是可选分发操作，不是使用已有包的前置步骤，也不包含 Worker 镜像。

## 排障

| 现象 | 处理方向 |
| --- | --- |
| Python / npm / Docker 构建下载失败 | 按失败地址检查网络、代理和对应软件源；源码 `npm ci` 注意前面的 Node 版本要求 |
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
