# Arena

Arena 是面向本地和内部使用的自主安全探索平台。通过 **Origin** 描述背景与已知资料，通过 **Goal** 表达目标，平台使用 pi Agent、共享事实和证据，在明确授权范围内安排探索、验证线索并交付成果。

当前唯一内置场景为 **`goal-driven-v1`（目标驱动探索）**，也是新建项目的默认场景。支持按需编辑或复制提示词、配置模型与 Worker，不要求使用固定的漏洞流程或先填写复杂清单。

## 系统组成

- **Server / UI**：Python 服务端提供 API 和构建后的 Vue 界面。
- **Dispatcher**：原生 Linux 进程，负责调度、容器执行和结果回收。
- **Worker**：Docker 镜像，内含 pi runner、浏览器及常用 Web 测试工具；默认名为 `arena-worker:local`，也可使用自定义镜像名。
- **PostgreSQL**：保存账号、配置、项目与执行记录；可使用项目自带的数据库 Compose，也可连接已有 PostgreSQL。

Compose 只负责数据库，不是全栈容器部署。内部 Python 包、CLI 与部分协议标识沿用 `cairn`，不影响产品名称 Arena。

## 安装与配置

完整步骤统一见 **[Linux 安装、配置与更新指南](docs/deployment-linux-zh.md)**：

- **使用已有 Worker 镜像**：取得配套应用部署包，拉取或导入 Worker 镜像，在宿主机安装并启动应用；仍需要 Python、uv 和 Docker。
- **从源码完整安装**：获取源码、安装 Python 依赖、构建正式前端与 Worker 镜像，再启动完整系统。

当前没有已核实的预构建镜像或部署包下载地址；没有配套资产时选择源码路径。两条路径共用登录、配置和维护说明，不是全栈 Docker 部署。

默认本机地址为 `http://127.0.0.1:8000`；端口、监听地址、镜像、数据库和存储位置可按需选择。临时参数、持久配置、局域网访问与重启方法见 **[修改端口和常用配置](docs/deployment-linux-zh.md#修改端口和常用配置)**。

## 使用流程

1. 登录后在 Models / Workers 界面配置模型凭据、启用 pi Worker 并绑定模型，无需再复制到 YAML。
2. 创建项目、选择 Worker，用 Origin、Goal 描述背景、目标与实际授权范围；资料库和上传资料按需使用。
3. 查看运行状态、事实与证据、漏洞及验证结果，按需要生成报告。

**创建项目可能立即执行并产生模型费用，模型连通性测试也可能调用付费 API。** 服务能启动不等于模型、真实任务或漏洞发现效果已经验证。

日常启停、日志、源码与部署包更新、数据保留及排障均见主指南。私有配置、密钥和运行资料不应进入 Git 或发布包。

Worker 专属环境见 [容器说明](container/README.md)，pi 模型协议限制见 [runner 说明](container/pi-agent/README.md)。

## 开发与来源

开发遵循 [CLAUDE.md](CLAUDE.md) 与 [AGENT.md](AGENT.md)。本项目整理自原 CairnY / cairn 工作区，保留现有实现和必要来源说明；新仓库不包含此前的运行数据或 Git 历史。Cairn 相关设计思想的参考来源见 `AGENT.md`。

按改动选择必要验证，而不是每次运行全套测试。例如：

```bash
uv run --project cairn pytest cairn/tests/test_scenarios.py
npm --prefix cairn/frontend run test:component -- src/features/projects/NewProject.component.test.ts
```

前端正式构建已经包含类型检查。真实模型、Docker 集成和外部目标测试按各自条件显式执行，不默认发起。

项目级开源许可证尚未确定，不据此声称采用 MIT 或 Apache 许可；第三方依赖和镜像工具继续受各自许可证约束。
