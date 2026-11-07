# Arena

Arena 是面向本地和内部使用的自主安全探索平台：用 **Origin** 描述背景、目标系统和授权范围，用 **Goal** 说明要完成的研究目标，查看执行过程、漏洞证据并按需生成报告。

应用运行在 Linux 宿主机，研究与流量采集使用 Docker，数据存入 PostgreSQL；不是全栈 Docker Compose 部署。

## 安装

推荐使用 [v0.2.4 部署包](https://github.com/Y10ng/Arena/releases/tag/v0.2.4) 和 [v0.2.3 Worker 最小应用层包](https://github.com/Y10ng/Arena/releases/tag/v0.2.3)，在 v0.2.1 公开研究镜像上离线构建小应用层，网关镜像保持不变；无需本机构建前端、安装 npm 或重新安装工具层。当前配套镜像仅支持 **Linux x86_64（amd64）**。

先安装 **Python 3.11+、[uv](https://docs.astral.sh/uv/getting-started/installation/)、[Docker 与 Compose v2](https://docs.docker.com/engine/install/)**，并确保 `curl`、`unzip`、`sha256sum`、`flock` 可用。默认 PostgreSQL 由启动脚本准备。

### 1. 下载并安装应用

以下命令在新目录下载、校验、解压；任一步失败就停止后续步骤。使用 Release 附件中的部署 ZIP，不是 GitHub 自动生成的 Source code ZIP。

```bash
RELEASE_URL='https://github.com/Y10ng/Arena/releases/download/v0.2.4'
ASSET='arena-0.2.4-linux-code-only.zip'
DOWNLOAD_DIR="$(mktemp -d "$HOME/arena-download.XXXXXX")" &&
  curl -fL "$RELEASE_URL/$ASSET" -o "$DOWNLOAD_DIR/$ASSET" &&
  curl -fL "$RELEASE_URL/$ASSET.sha256" -o "$DOWNLOAD_DIR/$ASSET.sha256" &&
  (cd "$DOWNLOAD_DIR" && sha256sum -c "$ASSET.sha256") &&
  UNPACK_DIR="$(mktemp -d "$HOME/arena-install.XXXXXX")" &&
  unzip "$DOWNLOAD_DIR/$ASSET" -d "$UNPACK_DIR" &&
  cd "$UNPACK_DIR/arena-0.2.4-linux-code-only" &&
  ./deploy/install.sh
```

后续命令在解压后的应用目录执行。`install.sh` 安装 Python 依赖并生成缺失配置，不启动服务。

### 2. 准备并配置配套镜像

下载 v0.2.3 Release 的 Worker 应用层包，校验后在固定公开基础上离线构建；v0.2.4 未修改 Worker，继续使用这一配套应用层。以下命令使用独立的 Worker 下载地址：

```bash
WORKER_RELEASE_URL='https://github.com/Y10ng/Arena/releases/download/v0.2.3'
WORKER_ASSET='arena-0.2.3-worker-application.zip'
WORKER_DOWNLOAD_DIR="$(mktemp -d "$HOME/arena-worker-download.XXXXXX")" &&
  curl -fL "$WORKER_RELEASE_URL/$WORKER_ASSET" -o "$WORKER_DOWNLOAD_DIR/$WORKER_ASSET" &&
  curl -fL "$WORKER_RELEASE_URL/$WORKER_ASSET.sha256" -o "$WORKER_DOWNLOAD_DIR/$WORKER_ASSET.sha256" &&
  (cd "$WORKER_DOWNLOAD_DIR" && sha256sum -c "$WORKER_ASSET.sha256") &&
  WORKER_UNPACK_DIR="$(mktemp -d "$HOME/arena-worker-app.XXXXXX")" &&
  unzip "$WORKER_DOWNLOAD_DIR/$WORKER_ASSET" -d "$WORKER_UNPACK_DIR" &&
  docker pull 'yong29/arena-worker@sha256:b4babe19a719acf73733b3d6e093c490a2a741b95562682fd16b5c227df57d06' &&
  docker build --pull=false --network=none -t arena-worker:0.2.3 "$WORKER_UNPACK_DIR/arena-0.2.3-worker-application" &&
  docker pull 'yong29/arena-traffic-gateway@sha256:a2734dde304c5bdc7a3c22e867e21bbe22a4860fc112c54f0feab128b202da8a'
```

编辑 `runtime/dispatch.yaml`，只修改 `container` 下的两个字段，保留其他配置：

```yaml
container:
  image: "arena-worker:0.2.3"
  traffic_gateway_image: "yong29/arena-traffic-gateway@sha256:a2734dde304c5bdc7a3c22e867e21bbe22a4860fc112c54f0feab128b202da8a"
```

公开基础镜像无需登录，Docker 复用已存在的相同层；小应用层构建不下载工具或 npm 依赖。不要保留默认 `:local` 引用，否则更新器仍可能触发完整配方的缓存构建。已有显式镜像映射也须更新实际引用；详见[完整安装指南](docs/deployment-linux-zh.md#路径-a使用已有-worker-镜像)和[代理排障](docs/deployment-linux-zh.md#排障)。

### 3. 启动并登录

需要从其他电脑访问时，首次启动前在 `runtime/arena.env` 设置 `CAIRN_TRUSTED_ORIGINS=http://实际主机地址:8000`。改端口、HTTPS 或外部数据库见[配置说明](docs/deployment-linux-zh.md#修改端口和常用配置)。

```bash
./deploy/start.sh && ./deploy/status.sh
```

- 本机打开 **http://127.0.0.1:8000**，远程使用已配置的主机地址；管理员账号为 **`admin`**。
- 首次随机密码在终端显示；非交互启动时保存在 `runtime/secrets/initial-admin-password`。登录后修改密码，不分享该文件。

需要修改源码时使用[源码安装方式](docs/deployment-linux-zh.md#路径-b从源码完整安装)，不要将任意新源码与本次发布镜像混用。

## 更新

### 部署包安装的更新

**v0.2.3 → v0.2.4：** 只更新宿主应用；无新增数据库迁移，继续复用 `arena-worker:0.2.3` 和原固定网关，无需重建 Worker。**v0.2.1／v0.2.2 → v0.2.4：** 同时更新应用包和 v0.2.3 Worker 应用层，不能直接沿用未经更新的旧 Worker。默认 `:local` 配置仍会触发完整配方的缓存构建，不用 `--server-only` 绕过准备。所有更新都须在旧任务退出后的维护窗口进行；存量容器不自动升级，已有工作区冲突和材料签名不匹配须保留现场处理。

1. 从 [v0.2.4 Release](https://github.com/Y10ng/Arena/releases/tag/v0.2.4) 下载应用 ZIP 及校验文件；需要更新 Worker 时再从 v0.2.3 Release 下载对应应用层 ZIP 和校验文件。校验后解压到**旧安装之外的新目录**，按上方步骤准备配套镜像；不在新目录另行安装或启动实例。
2. 在界面暂停或结束任务并确认执行已退出，备份数据库、`runtime`、资料与外置存储。跨版本更新先查看[迁移限制](docs/deployment-linux-zh.md#本次跨版本升级注意)：有历史会话待迁移的安装不能直接套用更新器。
3. 停止旧应用：

   ```bash
   OLD_APP='/原安装绝对路径'
   "$OLD_APP/deploy/stop.sh"
   ```

4. 从 v0.2.3 更新时保留现有配套镜像配置；从更早版本更新时，按上方说明准备 `arena-worker:0.2.3`，并编辑 **旧安装**的 `runtime/dispatch.yaml`，同步研究、网关及自定义映射的实际引用。不要在旧任务仍运行时切换镜像。使用自定义 `CAIRN_STATIC_DIR` 时，须在维护窗口同步实际目录，或改为指向旧安装更新后的 `cairn/src/cairn/server/static`；详见[部署包更新](docs/deployment-linux-zh.md#部署包更新)。
5. 用**新包中的更新器**更新旧目录：

   ```bash
   NEW_APP='/新包解压后的应用绝对路径'
   "$NEW_APP/deploy/update.sh" --target "$OLD_APP"
   ```

更新器会替换程序、同步依赖、检查镜像并启动服务。完成后运行 `"$OLD_APP/deploy/status.sh"`，浏览器强制刷新，再自行验收目标流程。缺少的远端镜像需提前拉取；它不会自动获取最新版，也不会替换已有任务容器。失败时按错误处理后重跑完整更新命令，不删除业务数据绕过迁移。

### Git 源码安装的更新

不要用部署 ZIP 覆盖 Git 工作区。先保留本地修改，再按[源码更新步骤](docs/deployment-linux-zh.md#源码更新)取得目标代码、安装依赖、构建正式前端和配套镜像，最后启动。

## 使用

### 1. 配置模型

1. 进入 **系统管理 → 模型与 Worker**，在 **模型目录**点击 **新增模型**。
2. 填写名称、Base URL、模型 ID，选择服务支持的协议格式，在 **认证与自定义 Headers** 中填写 API Key，然后点击 **保存模型**。Base URL 填服务基础地址，默认接口路径由协议补齐；特殊路径使用 **自定义 endpoint**。
3. 在同页的 **Worker 分配**中，为 **初始化（bootstrap）、决策（reason）、探索（explore）**分别选择模型，保持模型和 Worker 启用，并逐行点击 **保存**。三个 Worker 可以使用同一个模型，不需要手动创建 Worker。

可在模型编辑窗口的 **连接测试（可选）**中发送测试；新增模型不会自动分配给 Worker。

### 2. 创建任务

1. 进入 **项目**，点击 **新建项目**。
2. 填写 **项目名称**，选择探索场景，并填写：
   - **Origin（已知信息）**：目标系统、已有资料、授权范围和排除项。
   - **Goal（目标）**：本次希望完成的研究目标。
3. 按需上传资料、选择资料库或设置代理。普通项目会自动关联已配置的三个 Worker，无需再次选择。
4. 启动方式选 **立即**，点击 **创建并启动**；需要预约或周期执行时，选择 **定时、每日或每周**并保存计划。

创建后在项目内查看执行进展、漏洞和证据，按需生成报告。**仅对已获授权的目标开展研究；模型测试和任务执行可能产生 API 费用。**

详细配置、日常启停与排障见[完整安装指南](docs/deployment-linux-zh.md)，执行环境见[容器说明](container/README.md)。不要将私有配置、密钥或研究材料提交到 Git。

## 开发与来源

项目源自 CairnY / cairn，部分内部标识沿用 `cairn`。开发遵循 [CLAUDE.md](CLAUDE.md) 与 [AGENT.md](AGENT.md)。项目级开源许可证尚未确定，第三方依赖和工具遵循各自许可证。
