# Arena：Linux 安装、配置与更新

Arena 面向本地和内部使用。应用通过现有 Shell 脚本管理，不需要 Kubernetes、外部密钥平台或额外发布审批系统。保留基本的账号认证、凭据保护和数据边界即可；如果要直接暴露到公网，再单独考虑反向代理、TLS 和访问控制。

## 1. 系统组成与前置条件

```text
浏览器 → Server（API + Vue 静态资源）→ PostgreSQL
                 ↑
             Dispatcher → Docker Worker（pi runner + 浏览器 / Web 工具）
```

Server 和 Dispatcher 运行在宿主机 Python 环境，Worker 运行在 Docker 中。`deploy/postgres.compose.yaml` **只运行数据库**。

需要：

- Linux、Python 3.11+、[uv](https://docs.astral.sh/uv/)、系统 `flock`；
- Docker CLI 和可用的 Docker daemon；使用自带数据库时需要 Compose v2；
- 从源码构建前端需要 Node.js 22.12+，建议 Node 24 LTS，以及 npm；
- 可访问 Python / npm 软件源、容器仓库、Kali 软件源及构建时下载工具的 GitHub 地址。

Worker Dockerfile 有 amd64 和 arm64 分支。跨架构构建需要相应 builder；未实际验证的平台不视为自动支持。首次构建需要下载浏览器与工具，请预留磁盘空间和时间。

前端类型检查与打包、Docker 构建都需要内存。约 4 GB 且无 swap 的主机可能被内核 OOM 终止（exit 137）；资源有限时串行构建，并按本机需要准备交换空间或增加内存，不要通过跳过类型检查解决。

## 2. 安装与镜像构建

### 从源码安装

```bash
git clone https://github.com/Y10ng/Arena.git
cd Arena
./deploy/install.sh
npm --prefix cairn/frontend ci
npm --prefix cairn/frontend run build:server
docker build -t arena-worker:local container/
./deploy/start.sh
./deploy/status.sh
```

各步骤的职责：

- `install.sh` 使用 `uv sync --frozen --no-dev` 安装 Python 依赖，只创建缺失配置，不启动服务。
- `build:server` 包含类型检查，清空并重新生成 `cairn/src/cairn/server/static/`。它不同于仅供检查的 `build:verify`。
- Docker 构建上下文必须是 `container/`，使用其中的 `.dockerignore`；不将宿主机依赖或私有运行配置带入镜像。
- `start.sh` 初始化运行依赖、数据库 schema 和首次账号，然后启动 Server / UI 和 Dispatcher。

默认镜像只有一个日常名字：**`arena-worker:local`**。版本化的 pi SDK、工具与协议标识仍有各自技术含义，不需要为每次本地修改制造日期或候选镜像名。

`start.sh` 在默认镜像缺失时自动构建，但**同名镜像已存在时不会自动重建**。改了 `container/` 必须显式执行上面的 Docker 构建命令。使用自定义镜像时，先自行 build 或 pull，然后修改配置；本文不提供尚未发布的 GHCR 镜像地址。

### 使用自行生成的部署包

已有源码和匹配的正式前端产物时，可运行：

```bash
python3 deploy/package_release.py
```

该 ZIP 是**部署包，不是完整源码包**：包含服务端、前端静态资源、部署脚本与 Worker 构建输入，不包含前端开发源码、完整测试、依赖、运行数据或 Docker 镜像。解压后使用 `deploy/start.sh`；缺少默认 Worker 镜像时仍需构建网络条件。

发布清单只服务于明确的发布包核对，不是日常启动、修改配置或本地构建的门槛。

## 3. 启动、登录与状态

```bash
./deploy/start.sh
./deploy/status.sh
./deploy/logs.sh
./deploy/stop.sh
```

默认监听 `0.0.0.0:8000`，本机访问 `http://127.0.0.1:8000`。临时覆盖：

```bash
./deploy/start.sh --host 127.0.0.1 --port 8080
```

首次启动在空数据库中创建超级管理员。随机密码在交互终端显示一次；非交互启动写入权限受限的 `runtime/secrets/initial-admin-password`。完成登录、改密后删除该文件。不要将密码或该文件的内容粘贴进日志、Git 或问题报告。

管理员在界面创建的普通账号使用初始密码 `Arena@2026`，首次登录必须更改；此设置不修改已有账号密码。已有账号可使用管理界面的密码重置功能。

随后在界面配置模型、API Key 和 pi Worker，确认 Worker 已启用并绑定正确模型。唯一内置场景 `goal-driven-v1` 会自动成为新建项目默认值。资料、模型和 Worker 的配置应按实际需要修改，不需要维护第二份静态清单。

注意：

- `status.sh` 的 `running` 表示进程与 HTTP 基本可用，**还要单独查看 Dispatcher heartbeat 是否新鲜**。
- `logs.sh` 显示最近的 Server / Dispatcher 日志，不是持续跟随。
- `stop.sh` 停止应用，不停止 PostgreSQL、不删除数据库卷，也不能代替核对所有 Worker 执行是否已退出。
- `--server-only` 仅供维护；它不是完整系统已经可用的证明。
- 不要从两个安装目录同时启动连接同一数据库、使用同一身份的 Dispatcher；各目录的锁文件不会互相协调。
- 新建项目可能立即调度；模型连通性测试和真实探索会产生模型费用。离线启动自检不验证真实模型调用。

## 4. 配置

### 文件与生效方式

首次安装生成的本机文件不进入 Git：

| 文件 | 内容 |
| --- | --- |
| `runtime/arena.env` | Web 监听、外部数据库、存储根、可信 Origin 等 |
| `runtime/dispatch.yaml` | 默认镜像、镜像注册、执行画像、并发与超时 |
| `runtime/postgres.env` | 自带 PostgreSQL 的密码和宿主端口 |
| `runtime/secrets/model-credential-keyring.json` | 解密数据库模型凭据所需的 keyring |
| `runtime/secrets/dispatcher.token` | 与数据库机器身份匹配的 Dispatcher token |

模型与 Tenant Worker 主要保存在数据库，通过界面维护，不要将 API Key 放入 Dispatcher YAML。模型和 Worker 保存后由调度器读取；程序代码、环境变量、监听或执行身份改变时，应明确停止并重启。不要用整份示例文件覆盖已有配置。

部署层环境变量使用 `ARENA_*`；底层 Python 服务仍使用 `CAIRN_*`，内部 CLI 仍叫 `cairn`。这是当前实现接口，不需要手工全局替换。

### 自带 PostgreSQL

`runtime/arena.env` 未配置外部数据库 URL 时，启动脚本使用自带数据库：

- Compose project：`arena-db`；service：`arena-postgres`；
- image：`postgres:16-alpine`；database / role：`cairn`；
- 默认绑定 `127.0.0.1:55432`；
- 卷键为 `arena_postgres_data`，默认实际卷名为 `arena-db_arena_postgres_data`。

`runtime/postgres.env` 中的密码首次自动生成。修改端口或密码只编辑这一份文件，再执行 `start.sh`；脚本会同步已有数据库 role 的密码，不删除数据，也不要求再复制一份连接 URL。

```bash
docker compose -p arena-db --env-file runtime/postgres.env \
  -f deploy/postgres.compose.yaml ps

docker compose -p arena-db --env-file runtime/postgres.env \
  -f deploy/postgres.compose.yaml logs --tail=100 arena-postgres
```

不要把 `docker compose down -v` 用于重启或更新；它会删除持久数据。若主机已有别的 PostgreSQL，确认实际端口与实例后再操作。

### 已有 / 外部 PostgreSQL

先运行 `install.sh` 创建本机文件，再在 `runtime/arena.env` 中填写：

```dotenv
CAIRN_DATABASE_URL=postgresql://cairn:URL_ENCODED_PASSWORD@db.example:5432/cairn
```

这是示例，不是真实凭据。密码中的 `@`、`:`、`/`、`%` 等字符需 percent-encode。

External 模式只连接该库，不启动或修改项目 Compose PostgreSQL；连接失败不会回退成另一套空数据库。`start.sh` 会先清除 shell 中的 `CAIRN_DATABASE_URL` 再读取环境文件，**只在终端 export 不会持久生效**。

目录或品牌改名不应改变既有数据库连接、数据库名、role 或 volume。保留账号和模型配置需要保留同一个数据库及匹配的 keyring，不是只复制 YAML。

### 端口与浏览器 Origin

在 `runtime/arena.env` 中持久设置：

```dotenv
ARENA_SERVER_HOST=0.0.0.0
ARENA_SERVER_PORT=8000
```

远程访问时将浏览器实际访问的完整 scheme、host、port 加入 `CAIRN_TRUSTED_ORIGINS`。HTTPS 反向代理下可设置 `CAIRN_SESSION_COOKIE_SECURE=1`。本地内部部署不需要为此引入额外认证服务。

### Worker 镜像、画像与并发

`runtime/dispatch.yaml` 的基本配置：

```yaml
runtime:
  prompt_group: goal-driven-v1
  max_workers: 8
  max_running_projects: 3
  max_project_workers: 4
  playwright_session_policy: task
container:
  image: arena-worker:local
  network_mode: host
  dns: []
```

这是字段说明，不要用这一小段覆盖完整文件。

若使用显式 `images` 列表，实际镜像由以下链路决定：

```text
场景 profile_id → execution_profiles[].image_ref → images[].image
```

只改 `container.image` 不一定会更新实际 Worker 镜像。`image_ref` 是注册 ID，不是 Docker tag。默认画像与 `restricted` 画像可按实际资源需要配置；修改名称时同步引用，不丢失已有网络、capabilities、内存、swap、pids 设置。

Docker host 网络仍使用容器内 resolver。`dns: []` 表示使用默认 resolver；确有解析问题时可填写实测可用的 DNS IP。代理和下载源按所在网络配置，不默认固定模型服务 IP或禁用 TLS 校验。

任务级浏览器策略使同一 TaskRun 的执行与收尾共享浏览器，完整收尾且执行确认退出后定向关闭；用户显式指定的长期会话仍需自行管理。它不代表可以批量杀掉所有浏览器进程。

### 数据路径

默认运行文件和存储：

- `runtime/data/artifacts/`：任务产物；
- `runtime/data/prompts/`：受管 Prompt；
- `runtime/data/knowledge/`：可选知识资料；
- `datas/cairn/project-resources/`：项目上传与暂存；
- `datas/cairn/advisory-library-content/`：版本化全局资料库正文。

可通过 `CAIRN_ARTIFACT_ROOT`、`CAIRN_PROJECT_RESOURCE_ROOT`、`CAIRN_ADVISORY_LIBRARY_ROOT` 指定其他持久目录。上传大小等参数可按实际需要设置，具体变量见 `server/services/project_resources.py`；不必为了首次使用改完所有配额。

这些目录可能含授权测试的私有材料，不能交给静态文件服务器或包含进源码包。资料库定义在数据库，正文在文件中，两者须一起保留。

## 5. 更新

### Git 源码安装

先结束或暂停在途任务，确认相关执行已退出。保留自己的未提交修改，再更新代码：

```bash
./deploy/stop.sh
git pull --ff-only
./deploy/install.sh
npm --prefix cairn/frontend ci
npm --prefix cairn/frontend run build:server
# container/ 有变化时重建；不确定时也可显式构建。
docker build -t arena-worker:local container/
./deploy/start.sh
./deploy/status.sh
```

普通依赖刷新不会覆盖已有 `runtime`。启动会应用数据库迁移，但不会清空项目历史。

**镜像更新与存量容器是两件事。** 同名 tag 重建不会改变已经创建的容器；运行配置签名变化也可能被明确拒绝而不是自动重建。确认项目已经停止、资料已经按需要保存后，定向删除该项目所属的旧 Worker 容器，让后续执行创建新容器。不要删除数据库容器、其他项目容器，或用全局 prune 代替归属检查。

### 无 Git 部署包更新

将匹配的新部署包解压到安装目录之外，从新包执行：

```bash
/tmp/arena-new/deploy/update.sh --target /opt/arena
```

更新器替换受管程序与静态文件，保留运行配置、数据、资料库和现有 Python 环境，再安装依赖并启动。它**不是源码同步器**，不能用于更新一份需要保留前端开发源码的 Git 工作区；也不会刷新已存在的同名 Worker 镜像，`container/` 改动仍需显式构建。

更新失败时按输出检查问题，不宣称仅回退代码就能恢复迁移后的数据库。需要备份时应同时保存 PostgreSQL、相关数据目录、私有配置、完整 keyring 和 token；系统不会为每次普通构建自动堆积备份。

## 6. 清理与排障

### 清历史但保留配置

在界面删除项目会删除其数据库关联和项目文件；清理运行日志、未消费上传、历史容器和 outbox 是另一个操作。删除前先停止相关执行，不能让 Dispatcher 同时继续写入或重放历史交付。

以下不属于可随手删除的历史垃圾：

- 数据库中的用户、租户、模型、加密凭据版本、Worker 和机器身份；
- `runtime/secrets/`、实际外置 keyring、私有环境文件；
- 保留的资料库正文及其数据库索引；
- PostgreSQL volume 和迁移记录。

不要通过重置整库、删除整个 `runtime`、全局重置 counters 或 `docker system prune` 实现项目历史清理。普通更新也不应自动执行清历史操作。

### 常见问题

| 现象 | 最直接的检查 |
| --- | --- |
| Docker 不可用 | `docker info`，确认 daemon、当前用户权限和 Compose 版本 |
| 数据库失败 / 端口占用 | 确认是自带还是外部库，再看对应端口和数据库日志；不要临时换到空库 |
| 修改配置后没生效 | 检查实际 `runtime/arena.env` / YAML，并按需要 stop/start；不要仅改终端 export |
| UI 仍旧或资源 404 | 重新执行 `build:server`，确认 `CAIRN_STATIC_DIR` 未指向旧安装 |
| `Untrusted request origin` | 配置浏览器实际使用的完整 Origin，HTTPS 时检查 Cookie 设置 |
| 模型可连但项目不运行 | Worker 启用与绑定、项目 Worker 选择、Dispatcher 心跳、镜像与容器状态 |
| 旧镜像 / runtime mismatch | 停止相关项目，确认镜像映射和存量容器；不要强行认领其他实例容器 |
| 模型凭据解密失败 | 检查是否保留了匹配的完整 keyring，不要重新生成密钥覆盖旧文件 |

`cairn-agent --self-check` 是离线安装检查，不会请求真实模型，也不证明漏洞发现能力。需要判断具体模型和任务效果时，再使用明确授权、范围和费用的真实案例。
