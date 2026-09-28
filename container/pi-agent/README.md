# Cairn pi Agent

独立的 TypeScript runner，生产执行使用实际发布的 `@mariozechner/pi-coding-agent@0.73.0` SDK。`pi-ai`、`pi-agent-core`、`pi-tui` 同时锁定到 0.73.0，不调用旧 Agent CLI，不提供假 SDK 或文本 JSON fallback。

## 本地验证

需要 Node 24；镜像使用 Node 24.16.0。

```bash
cd container/pi-agent
npm ci --no-audit --no-fund
npm run build
npm test
node dist/cli.js --version
node dist/cli.js --self-check
```

测试使用 Node 内置 test runner、临时目录和仅监听 `127.0.0.1` 的 HTTP provider fixtures。三种协议均经过真实 SDK 的 HTTP/SSE、工具执行和会话持久化路径。不会调用真实模型、启动部署服务、读取现有运行目录或凭据。`--version` 和 `--self-check` 均为离线操作；readiness 只使用 `--self-check`，不进入普通请求、provider 或会话路径。

## 唯一执行入口

```text
cairn-agent --request FILE
```

请求必须为私有普通文件（推荐 `0600`，拒绝 symlink、组/其他用户可读文件，最大 8 MiB）；调用者负责清理。prompt 和模型凭据只在文件中，不传 argv。

请求协议为 `cairn-agent-request-v1`：

- `project_id`、`task_run_id`、`phase`、`prompt`。
- `session`：`pi:<UUIDv4>`。Python 预生成引用；第一次执行 `resume:false`，后续明确 `resume:true`。
- `model`：`protocol`、`model_name`、`inference_endpoint`、`base_url`、`auth_header`、`api_key`、`custom_headers:[{name,value}]`。
- `result_schema`：Python 生成的完整 `submit_result` 参数 JSON Schema，包含 accepted/data 或 accepted/reason，但不包含外层 protocol/kind。唯一来源及导出快照是 `cairn.dispatcher.worker_protocol.submission` 与 `contracts/submission-v1.json`。
- `binding`：可为 null。`slot_key/surface_key/test_family` 仅注入已经存在的 `data.coverage_result`；`verification_observation_id` 仅注入已经存在的 `data.verification_result.observation_id`；`review_input_digest` 仅注入已经存在的 `data.depth_review.input_digest`。`max_intents/allowed_fact_ids` 供 Python 生成 Schema，不注入结果，不凭空创建语义对象。
- `timeout_seconds`：本次总截止，覆盖执行、provider 重试、结果修正和终态整理。
- `max_result_repairs`：0–2，默认 2。`conclude_only`：布尔值。
- 可选 `cwd`，默认 `/home/kali/workspace`；可选 `session_root`，默认 `<cwd>/.cairn/pi`。本地测试显式指定临时目录。

允许的 phase 为 `bootstrap_execute`、`bootstrap_conclude`、`explore_execute`、`explore_conclude`、`verifier_execute`、`verifier_conclude`、`reason_execute`、`report_execute`；`healthcheck`、其他未知 phase、旧 session、旧协议直接拒绝。

## 模型协议与 endpoint

| Cairn 协议 | SDK API | inference_endpoint 必需后缀 |
| --- | --- | --- |
| anthropic-messages | anthropic-messages | `/v1/messages` |
| openai-chat-completions | openai-completions | `/chat/completions` |
| openai-responses | openai-responses | `/responses` |

`inference_endpoint` 是权威完整 URL。runner 仅从明确后缀推导 SDK baseUrl，保留其之前的路径前缀；不会根据 `base_url` 猜测或覆盖 endpoint。非标准后缀、query、fragment、URL 内凭据明确返回 `unsupported_model_config`，不创建兼容代理。

`auth_header` 只允许 `authorization-bearer` 或 `x-api-key`。真实 HTTP 测试验证只发送所选认证头，不同时发送 SDK 默认认证头。保留自定义 headers，但拒绝重复、换行、Host/Content-Length 等传输头及认证头冲突。凭据通过内存配置进入 SDK，不发现本机 auth.json、不执行命令形式配置、不使用环境中的其他 provider key。

当前未提供模型能力发现或按模型自适应：代码静态声明 `contextWindow=128000`、`maxTokens=16384`，不是从 provider 探测得到的能力；thinking 与自动压缩关闭。真实模型的 token 上限和长会话兼容性必须另行验证，不能把本地 HTTP fixture 通过视为真实模型能力证明。SDK provider 重试最多一次，Agent 自动重试关闭，全部服从同一截止。

## 提交与证据

- 模型仅调用 `submit_result` 提交业务参数。原始工具参数先严格校验，禁止 SDK 类型转换与 partial JSON parser 把错误输入变成成功。
- runner 注入 `cairn-assessment-v2`、phase 对应 kind、权威绑定和已登记 Artifact。`submission_id` 是递归键排序 JSON 的 SHA256，固定 64 位小写 hex；Python 可将其作为 opaque 标识，不能假定不同语言数值序列化天然相同。
- `register_artifact({path,kind})` 只读取工作区已有普通文件；kind 为 log/file/report/screenshot/patch/raw_output。返回 `{handle,kind,path,sha256,size_bytes}`；最终 `data.artifacts` 仅包含 `{handle,kind,path}`。摘要和大小进入审计元数据，文件未上传前不视为 Server Artifact。
- SDK bash 的溢出输出复制到 `<cwd>/.cairn/artifacts/<session UUID>/` 并登记；大正文不进入 stderr 审计。其他证据由模型显式登记，提交前再次验证内容未变化。
- 已有 Server Artifact ID 仅保留引用；实际存在性、项目归属和业务条件继续由 Python Gateway/Server 校验。
- 收尾、report、修正只开放 `read_evidence`、`register_artifact`、`submit_result`。`read_evidence` 是有界本地文件读取，不运行 shell、网络或可执行文件。第一次尝试 submit 即进入收尾门禁，同一 SDK tool batch 后续的 bash 等动作也被拒绝。
- 没有提交、Schema 或证据引用错误最多两轮同 session 修正；不会抽取自然语言中的 JSON，不重新探索目标。重复同内容提交幂等，不同内容冲突失败。
- 候选结果以 `0600` 保存在 session 目录的 `candidate-*.json`，明确 `server_accepted:false`。失败不删除已生成证据；本地工具成功不等于 Server 已接受。

## 会话、取消与输出

会话目录有版本化项目 manifest、SDK session ID、转录文件 SHA256 和单写者 exclusive lock。恢复时严格检查版本、项目/cwd 归属、完整 JSONL、entry 链及摘要，然后才调用 SDK open；不使用 SDK 的损坏会话自动新建/迁移行为。新的 session 目录 exclusive 创建，绝不覆盖；没有初始化 assistant turn 的中断会话不能被静默恢复。

取消与截止调用真实 SDK abort；停止后不启动修正，晚到提交不能成为 succeeded。SIGKILL 等非协作终止可能留下 lock 或不匹配摘要，下一次明确失败，需要人工处理；不自动解锁或修补。Node 取消不能替代 Python/Docker 的进程硬截止与精确资源清理。

stdout 恰好一个程序生成信封：`protocol=cairn-agent-result-v1`、`engine=pi`、项目/TaskRun/phase/session、`status`、规范 result 或 null、submission_id、usage 或 null、error 或 null。退出码分别为成功 0、失败 1、取消 130。未知 token 计量返回 null，不使用 SDK 默认零值冒充已计量费用。

stderr 为 NDJSON：`protocol=cairn-agent-audit-v1`、timestamp、项目/TaskRun/phase/session、event。真实 SDK `tool_execution_start/end` 含 tool_name/tool_call_id、input_bytes/output_bytes、is_error 等有界元数据；不记录参数、正文、请求配置或密钥。调用者应流式保存 stderr，不将全部日志复制入内存。

## 边界

本 runner 是协议、会话与收尾可靠性边界，不是完整 OS 沙箱或通用 ToolBroker。正常 execute 的 shell 工具仍由容器授权范围约束；Server 的证据归属、状态冲突、原子接受和停止语义仍是最终权威。历史项目/Worker/Artifact/报告可读，但旧引擎绑定与旧冻结快照不能因此执行，旧 session 也不转换。

报告与容器归属还有外层限制：Dispatcher 在 `tasks.report.timeout > 1500` 时于 claim 前返回 `report_timeout_exceeds_lease`（执行 timeout 加 300 秒收尾不能超过 1800 秒租约，尚无续租）；容器 owner 身份随 Dispatcher/Server 身份改变时拒绝热更并保留上一份有效配置，必须由用户自行显式重启 Dispatcher，不由 runner 自动处理。

临时 SQLite、本机 PostgreSQL 18 的迁移/并发四项测试已通过；这些数据库回归与本地 SDK/HTTP fixtures 都不证明漏洞发现率、模型召回率或真实 provider 兼容性。整镜像构建、部署和真实模型能力尚未验证，本说明不构成整体验收完成或部署授权。
