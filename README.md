# Arena

Arena 是面向本地和内部使用的自主安全探索平台。通过 **Origin** 描述背景与已知资料，通过 **Goal** 表达目标，平台使用 pi Agent、共享事实和证据，在明确授权范围内安排探索、验证线索并交付成果。

当前唯一内置场景为 **`goal-driven-v1`（目标驱动探索）**，也是新建项目的默认场景。支持按需编辑或复制提示词、配置模型与 Worker，不要求使用固定的漏洞流程或先填写复杂清单。

## 系统组成

- **Server / UI**：Python 服务端提供 API 和构建后的 Vue 界面。
- **Dispatcher**：原生 Linux 进程，负责调度、容器执行和结果回收。
- **Worker**：Docker 镜像 `arena-worker:local`，内含 pi runner、浏览器及常用 Web 测试工具。
- **PostgreSQL**：保存账号、配置、项目与执行记录；可使用项目自带的数据库 Compose，也可连接已有 PostgreSQL。

Compose 只负责数据库，不是全栈容器部署。内部 Python 包、CLI 与部分协议标识沿用 `cairn`，不影响产品名称 Arena。

## 从源码安装

需要 Linux、Python 3.11+、[uv](https://docs.astral.sh/uv/)、Node.js 22.12+（建议 24 LTS）、npm、Docker daemon；使用自带数据库还需要 Docker Compose v2。软件源和容器仓库须可访问。

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

默认通过 `http://127.0.0.1:8000` 访问。默认服务监听 `0.0.0.0`；只需本机访问时使用 `./deploy/start.sh --host 127.0.0.1`。

首次启动自动初始化数据库、模型凭据密钥、Dispatcher 身份和管理员。交互终端会显示一次随机管理员密码；非交互启动将其保留在仅当前用户可读的 `runtime/secrets/initial-admin-password`。登录后修改密码并删除该交付文件，不要将其提交到 Git。

> `install.sh` 只准备 Python 环境和配置；前端、Worker 镜像的构建是独立步骤。`start.sh` 会构建缺失的默认 Worker 镜像，但**不会重建已经存在的同名镜像**。

## 配置与首次使用

1. 在界面的模型管理中填写协议、完整推理地址、Model ID 和 API Key。
2. 在 Worker 管理中创建并启用绑定该模型的 pi Worker。
3. 确认 Dispatcher 心跳新鲜，再创建项目并选择 Worker。
4. 填写 Origin、Goal 和实际授权范围；资料库和项目上传资料都是可选项。
5. 查看项目运行状态、事实与证据、漏洞及验证结果，按需要生成报告。

**创建项目可能立即开始执行并产生模型费用。** 模型连通性测试也可能调用付费 API；普通构建和离线 self-check 不调用模型。服务能启动不等于具体模型、目标或漏洞发现效果已经验证。

主要配置位置：

| 位置 | 用途 |
| --- | --- |
| `runtime/arena.env` | 监听端口、外部数据库、存储路径等本机设置 |
| `runtime/dispatch.yaml` | 镜像、并发、执行画像、任务超时 |
| `runtime/postgres.env` | 自带数据库的密码和端口 |
| `runtime/secrets/` | 模型凭据 keyring、Dispatcher token 等私有文件 |
| 界面中的模型 / Worker 管理 | 数据库存储的模型凭据和 Worker 绑定 |

模型和 Worker 配置不需要复制到 YAML，保存后由 Dispatcher 读取。配置、密钥、上传资料、执行记录不属于源码，不应进入新仓库或发布包。

## 日常管理与更新

```bash
./deploy/start.sh
./deploy/status.sh
./deploy/logs.sh
./deploy/stop.sh
```

`stop.sh` 停止应用，不删除数据或数据库卷。代码更新使用明确的停止、依赖刷新、构建和启动流程；不要把再次运行 `start.sh` 当成代码热更新，也不要用删除数据库卷解决升级问题。

完整步骤见 **[Linux 安装、配置与更新指南](docs/deployment-linux-zh.md)**，包括：

- 从源码和部署包安装；镜像的构建与更新；
- 自带或外部 PostgreSQL、监听地址与代理设置；
- 模型、Worker、并发、浏览器和存储配置；
- 更新、日志、数据清理与常见故障。

Worker 环境见 [容器说明](container/README.md)，pi 模型协议限制见 [runner 说明](container/pi-agent/README.md)。

## 开发与来源

开发遵循 [CLAUDE.md](CLAUDE.md) 与 [AGENT.md](AGENT.md)。本项目整理自原 CairnY / cairn 工作区，保留现有实现和必要来源说明；新仓库不包含此前的运行数据或 Git 历史。Cairn 相关设计思想的参考来源见 `AGENT.md`。

按改动选择必要验证，而不是每次运行全套测试。例如：

```bash
uv run --project cairn pytest cairn/tests/test_scenarios.py
npm --prefix cairn/frontend run test:component -- src/features/projects/NewProject.component.test.ts
```

前端正式构建已经包含类型检查。真实模型、Docker 集成和外部目标测试按各自条件显式执行，不默认发起。

项目级开源许可证尚未确定，不据此声称采用 MIT 或 Apache 许可；第三方依赖和镜像工具继续受各自许可证约束。
