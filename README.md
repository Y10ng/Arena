# Arena

Arena 是面向本地和内部使用的自主安全探索平台：用 **Origin** 描述背景、目标系统和授权范围，用 **Goal** 说明要完成的研究目标，查看执行过程、漏洞证据并按需生成报告。

应用运行在 Linux 宿主机，研究与流量采集使用 Docker，数据存入 PostgreSQL；不是全栈 Docker Compose 部署。

## 安装

本指南对应 **0.2.9** 配套部署。从 [最新 Release 页面](https://github.com/Y10ng/Arena/releases/latest) 确认版本为 **0.2.9** 再下载部署包；尚未出现该版本时不要把旧包当作本版安装。配套 Worker `yong29/arena-worker:0.2.7` 已上传，下面按固定 digest 使用预构建 Worker 与复用的网关镜像，不再下载 v0.2.3 Worker 应用层包或本地构建小应用层；无需本机构建前端、安装 npm 或重装工具层。当前配套镜像仅支持 **Linux x86_64（amd64）**。

先安装 **Python 3.11+、[uv](https://docs.astral.sh/uv/getting-started/installation/)、[Docker 与 Compose v2](https://docs.docker.com/engine/install/)**，并确保 `curl`、`unzip`、`sha256sum`、`flock` 可用。默认 PostgreSQL 由启动脚本准备。

### 1. 下载并安装应用

确认最新 Release 已发布 0.2.9 后执行以下命令，在新目录下载、校验、解压；任一步失败就停止后续步骤。使用 Release 附件中的部署 ZIP，不是 GitHub 自动生成的 Source code ZIP。

```bash
RELEASE_URL='https://github.com/Y10ng/Arena/releases/latest/download'
ASSET='arena-0.2.9-linux-code-only.zip'
DOWNLOAD_DIR="$(mktemp -d "$HOME/arena-download.XXXXXX")" &&
  curl -fL "$RELEASE_URL/$ASSET" -o "$DOWNLOAD_DIR/$ASSET" &&
  curl -fL "$RELEASE_URL/$ASSET.sha256" -o "$DOWNLOAD_DIR/$ASSET.sha256" &&
  (cd "$DOWNLOAD_DIR" && sha256sum -c "$ASSET.sha256") &&
  UNPACK_DIR="$(mktemp -d "$HOME/arena-install.XXXXXX")" &&
  unzip "$DOWNLOAD_DIR/$ASSET" -d "$UNPACK_DIR" &&
  cd "$UNPACK_DIR/arena-0.2.9-linux-code-only" &&
  ./deploy/install.sh
```

后续命令在解压后的应用目录执行。`install.sh` 安装 Python 依赖并生成缺失配置，不启动服务。

### 2. 准备并配置配套镜像

按固定远端 manifest digest 拉取两张预构建镜像；Worker 使用新版本，网关复用既有镜像。任一步失败时先处理，不继续启动：

```bash
docker pull 'yong29/arena-worker@sha256:62c3e3b9a19b1306b77361e98ab1169fa2e41529c31bb7100c3bb23849895d16' &&
  docker pull 'yong29/arena-traffic-gateway@sha256:c1ae1c054cc3b12b0648d06476cc7af86a13208b3f366d3ef0c1a3f9c3b41e10'
```

编辑 `runtime/dispatch.yaml`，只修改 `container` 下的两个字段，保留其他配置：

```yaml
container:
  image: "yong29/arena-worker@sha256:62c3e3b9a19b1306b77361e98ab1169fa2e41529c31bb7100c3bb23849895d16"
  traffic_gateway_image: "yong29/arena-traffic-gateway@sha256:c1ae1c054cc3b12b0648d06476cc7af86a13208b3f366d3ef0c1a3f9c3b41e10"
```

公开镜像无需登录，Docker 复用已存在的相同层。新安装显式使用上述 digest，避免缺少默认 `:local` 镜像时触发源码构建；已有显式镜像映射也须对应实际引用。旧安装由新包更新器统一迁移官方引用，不必手工改成新 digest；详见[完整安装指南](docs/deployment-linux-zh.md#路径-a使用已有-worker-镜像)和[代理排障](docs/deployment-linux-zh.md#排障)。

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

**升级目标为 0.2.9，操作前确认 Release / 渠道版本。** 本次修复有效更正模型配置后的认证重试阻断、Bootstrap 新协议结果误报，并补齐现场核验与材料上传的可导出诊断。只更新宿主 Server／Dispatcher，复用 0.2.7 的 Worker 与上述网关，无新增数据库迁移、依赖或前端变化，也保留 0.2.8 的安装目录改名修复。所有更新都须在旧任务退出后的维护窗口进行；更新不自动恢复或重跑项目，诊断增强不表示历史现场丢失、上传和启动故障均已解决。已有工作区冲突和材料签名不匹配须保留现场处理。

- **v0.2.7／v0.2.8 ZIP 安装：** 管理端「设置 → 系统更新」按 **检查 → 准备 → 应用** 更新，确认目标为 **0.2.9**；也可使用下面的新包手动更新入口。
- **v0.2.6 ZIP 安装：** 管理端「设置 → 系统更新」可按 **检查 → 准备 → 应用** 更新。旧版准备阶段可能仍只准备网关并提前改写同仓库网关引用；完整两镜像准备与配置迁移由新包的 `update.sh` 在应用阶段执行，不能把旧页面“准备完成”当作新 Worker 已验收。旧页面在同镜像存在多个 digest 时可能误报网关 digest 不匹配；若准备失败，直接改用下面的新包手动更新入口，不反复 pull 或删镜像，不保证所有旧页面首次升级都成功。
- **v0.2.5 及以前的 ZIP 安装：** 直接使用 0.2.9 新包的 `update.sh`，不必先升到 v0.2.6；v0.2.6 也可采用此手动路径。

手动更新步骤：

1. 从 [最新 Release 页面](https://github.com/Y10ng/Arena/releases/latest) 确认 **0.2.9**，下载应用 ZIP 及校验文件，校验后解压到**旧安装之外的新空目录**；不在新目录另行安装或启动实例。
2. 在界面暂停或结束任务并确认执行已退出，备份数据库、`runtime`、完整 keyring / token、资料与外置存储。先查看[迁移限制](docs/deployment-linux-zh.md#本次跨版本升级注意)：有历史会话待迁移的安装不能直接套用更新器。新更新器自动同步外置 `CAIRN_STATIC_DIR`，停服前预检冲突，保留旧 hash 和未知文件，最后原子切换 `index.html`；非入口同名异内容会阻止升级，参见[部署包更新](docs/deployment-linux-zh.md#部署包更新)。
3. 用**新包中的更新器**更新旧目录，无需预先手工停止宿主应用或修改官方镜像引用：

   ```bash
   OLD_APP='/原安装绝对路径'
   NEW_APP='/新包解压后的应用绝对路径'
   "$NEW_APP/deploy/update.sh" --target "$OLD_APP"
   ```

新更新器在**停服前**校验包内 manifest、按固定 digest 准备并离线验收 Worker 与网关；缺少官方镜像时拉取并复用已有层。**停服后**按配置 revision 原子迁移官方引用，覆盖 `container.image`、`container.traffic_gateway_image`、`container.allowed_images` 和 `images` 注册表中的相关项，再替换程序、同步依赖并启动。`arena-worker:local`、本地版本 tag（如 `arena-worker:0.2.3`）视为官方引用；`arena-worker:custom` 等真正自定义引用不覆盖，缺失或不兼容会在停服前拒绝。

完成后运行 `"$OLD_APP/deploy/status.sh"`，浏览器强制刷新，再自行验收目标流程。更新不替换已有容器，不自动恢复或重跑项目；服务可用不等于存量项目已验证。失败时按错误处理后重跑完整更新命令，不删除业务数据绕过迁移；更新器的程序备份不等于数据库、配置、依赖与镜像的整体回滚。

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
