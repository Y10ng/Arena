# Dispatcher

> 本文保留了历史执行器与旧协议设计，不代表各段均已与当前实现对齐。`proj_030` 的历史只读查证、定向实施范围及本次实施结果见下节；不要据旧 driver、失败处理或示例输出重建当前系统。

## proj_030：结果交付契约与规划依赖纠偏（代码已实施；真实任务待验收）

### 历史查证状态与文档归属

- 下列历史取证由“分析、设计与实施交接”会话增量记录；该次会话**仅完成只读查证和设计，未修改业务代码、运行测试、构建部署或操作项目**。本次实施结果另见本节末尾，不将历史记录追溯解释为已修复或已验收。
- `docs/analysis-design-handoff.md` 指向的 `docs/specs/goal-driven-execution-design.md` 在本轮工作区不存在。本轮使用现存的 Dispatcher 设计承载对应改动，不声称已阅读或更新缺失的原方案，不新建同义交接副本。
- 用户只报告“创建的 proj_030 好像有问题”，尚未提供具体页面症状。本节确认该项目的实际执行失败，不声称已解释用户所有使用问题。
- 核查分支为 `chore/arena-initial`；既有未跟踪的 `docs/analysis-design-handoff.md` 不属于本轮新增或修改。

### 实际实例与历史证据

只读快照时间：主机本地 `2026-09-28 22:32:36 -04:00`，对应 `2026-09-29 02:32:36 UTC`。以下进程和端点只用于本轮定位，实施前须重新核对。

- 精确 `projects.id = 'proj_030'` 命中，场景为 `goal-driven-v1`，状态为 `paused`，不是项目创建失败。`tr001/bootstrap`、`tr002/reason` 已成功。
- 实际 Server PID `2823736`、Dispatcher PID `2823875` 的 cwd 均为 `/root/桌面/Arena`；Server 使用 `8001`。实际数据库为 `127.0.0.1:55433/cairn_isolated`，容器 `cairny-isolated-postgres-142ac29cc3e9`；**不是**默认 `55432` 的另一数据库。不得因容器名含 isolated 就将本次用户实例当作可丢弃的测试副本。
- 项目 Worker 容器为 `cairn-pi-dispatch-1498abdc625d6723-proj_030`，镜像名 `arena-worker:local`；快照时已退出，退出码 `143`。退出码不能证明是谁暂停，也不能单独证明故障首因或加载的代码版本。
- `tr003/i002`、`tr004/i003`、`tr007/reason` 为 `provider_error`；本轮未确定上游具体原因。日志第一处明确任务失败为本地 `21:34:06` 的 `i003`，不能把后来的业务拒绝倒写为第一次模型失败的原因。
- 三次 Explore 的最终拒绝为 `business_acceptance / phase_contract_invalid`，已有候选在本地通过校验：

| Run / Intent | 已本地校验的候选 | 仅字段存在性取证 | Dispatcher 最终拒绝 |
| --- | --- | --- | --- |
| `tr005 / i002` | `cnd_f0e37bc026fb45d69ff4174162d19d4c` | `finding`；证据、variants、observations 非空，baseline 缺失 | `finding requires assessment_record baseline and variants` |
| `tr006 / i003` | `cnd_2cbd2ea3ff24410c9c7b858c34d1aa92` | `finding`；证据、baseline、variants 非空，observations 缺失 | `finding requires assessment_record observations` |
| `tr009 / i004` | `cnd_fe2f0275e0d04da48e380550fbae382c` | `finding`；证据、observations 非空，baseline、variants 缺失 | `finding requires assessment_record baseline and variants` |

- 以上依据为 `task_runs`、`task_run_candidates`、`task_run_candidate_outcomes`，以及 `runtime/logs/dispatcher.log:42,46,69`。`tr006`、`tr009` 另有独立的 `submission_invalid` 候选，不能与表中的 `locally_validated` 候选混为一谈。
- 三次 Explore 的冻结记录保存相同的 `result_schema_sha256`，但本轮未取得历史 Schema 正文，**不能声称历史 Schema 与当前生成内容已经逐字比对**。当前源码缺口和当次本地通过、后置拒绝的记录分别成立。
- `tr008/reason` 候选本地通过，计划两条 Intent：两条 `from` 都为存在的 `f001`；第一条无依赖，成功创建 `i004`；第二条 `depends_on` 为自拟语义名称，而非实际 Intent ID 或 `@N`。项目实际只有 `i001`–`i004`，该引用不存在。日志 `:61–64` 为第二条写入 `404`、`created_intents=1/2`，最终 `business_acceptance`。这是另一条失败链，不是缺少 assessment_record。
- `i001` 已完成，`i002/i003/i004` 已失败。相关 `candidate recovery is unresolved` 警告后仍有任务派发，三次失败 Explore 的恢复记录最终为 `stopped` 且 `container_spool_recovered=true`；不能据该警告认定持续恢复死锁。
- 已登记 25 个 artifacts，宿主材料位于 `runtime/data/artifacts/projects/proj_030/runs/`。材料和候选留存不等于成果已接受，更不等于漏洞已确认；本轮未审核完整目标证据，不复制候选正文、目标内容、凭据或 session 到方案。

复核时使用实际实例的 PostgreSQL 只读连接与 `REPEATABLE READ READ ONLY` 事务，确认 `transaction_read_only=on`，按精确项目 ID 查询白名单字段后 `ROLLBACK`。不导入业务应用触发初始化，不转储进程环境或完整 JSON，不调用可能推进生命周期的接口。

### 当前实现判断与设计纠正

1. **Explore 的确定性字段要求被拆成不同规范。** `assessment_contracts.py:159–183` 的 ExperimentRecord 将 baseline、variants、observations 等设为可缺省；`dispatcher/worker_protocol/submission.py:395–407` 在 goal-driven Schema 中只补强 outcome 和 evidence_refs。`container/pi-agent/src/runner.ts:195–220,262–269` 按该 Schema 本地校验，不会看到 `dispatcher/contracts.py:777–834` 的 outcome 条件。后者要求 finding/tested_no_finding 同时有 baseline、variants、observations；Server 的 `services/coverage_slots.py:620–762` 又单独实现类似条件。仅补 Prompt 或只改一处验收不能消除这个不一致。
2. **不机械把旧后置规则全变成更严的工具 Schema。** 按 `AGENT.md`，实验结构应服务具体研究判断，不是所有成立发现或未发现结论都必须采用同一对照模板。本轮推荐在 `goal-driven-v1` 中取消 baseline/variants 的无条件必填；保留这些字段供需要对照的研究原样记录，不移除内容、不自动补造基线。finding/tested_no_finding 仍须有实际 observations 和证据；是否足以支持主张由证据及验证决定，不能以字段存在证明漏洞成立或目标安全。
3. **Reason 的依赖协议没有在本地表达完整。** `worker_protocol/submission.py:196–202` 允许任意非空 depends_on；`tasks/reason_writes.py:382–406` 只预检 `@N` 的前向或不存在位置，对其他字符串原样提交。`server/routers/intents.py:139–144` 要求其是同项目已有 Intent，因而出现先写一条、第二条才 404。不是 Server 应当接受任意名称，也不应删除依赖来让任务立即跑。
4. **失败处理放大了交付成本，但不应靠扩大重试白名单绕过。** `server/services/assessment_attempts.py:633–751` 不将 business_acceptance 视为可重试；`:824–922` 可将 Explore attempt 与 Intent 结为失败。已有本地修正环和原会话交付能力只处理其适用错误；不能假定后置业务拒绝会自动回传模型修正。先让本轮确定性错误进入已有本地修正闭环，不将所有业务拒绝改为 fresh 重跑或自动成功。

### 本轮最小完整实施范围

目标：正常研究材料不再因为隐藏的固定字段规则或无效依赖名称，在本地提交成功后才整次交付失败；减少重复探索和费用。**本轮是交付可靠性及实验表达纠偏，不宣称已经增强或验证真实漏洞发现能力。**

1. **统一 goal-driven 实验记录的确定性规则。** 复用现有 ExperimentRecord 和 Schema 生成路径，用一处小而直接的规范提供 Worker 本地 Schema、Dispatcher 及 Server 对记录字段的判定；删掉被替代的重复条件，不新增验证平台、第二份实验对象或并行状态。保持 legacy phased/Coverage 的行为边界，不借机改造旧引擎。
   - finding/tested_no_finding：保留非空 hypothesis、observations、evidence_refs；baseline/variants/controls 不作为通用准入门槛，原值保留，对照在研究语义需要时使用。
   - blocked/limitation/observation：对齐当前需要保留的 outcome 条件，使真正缺少必要内容时在 `submit_result` 就给出字段级修正信息；不得要求这些结果伪造对照，不把 provider/runtime 失败改成目标限制。
   - 不仅修改 Dispatcher：贯通 Server 的 `validate_assessment_experiment`。Server 继续负责当前 Run / 项目证据归属、已解析 Artifact、接受事务等依赖真实状态的判定；不把这些动态权威检查移到 Worker 假装完成。
   - 保留 finding 的 vulnerability identity、复现材料要求、候选与确认区分及独立复核。字段完备仍不自动等于 Server 接受或 confirmed。不得复制 fact.description 自动填 observations，也不为通过校验伪造 baseline/variants。
2. **补全 Reason 依赖的同一份约定。** 现有项目 Intent ID 从本次权威项目快照取得；同批依赖继续使用现有 `@N`，且只能引用前面的条目。将合法引用范围与位置规则用于工具 Schema/本地修正和 Dispatcher 写入前检查，普通语义名称不能通过后再逐项碰 404；合法 `@1` 在第一条被接受后解析到真实 ID。
   - 优先复用本次调用的 binding 和冻结交付记录；如果需要传递 `allowed_intent_ids`，它只服务于本次确定性引用校验，不另建数据库状态。必须贯通 `reason_context.py`、Schema/请求边界、Server `assessment_attempts.py:119–198` 的 allowed_binding 与 goal-driven 精确键集合、冻结及续交付消费者；不能只给 binding 增加字段却被 Server 拒绝。
   - 合法 ID 的范围不能误用 `open_intent_ids` 来替代全部可引用的既有 Intent；保留当前依赖调度语义，不把“前序已结束”新增解释为“研究前提已证实”。
   - 保留逐项幂等回执、已接受条目及真实 partial 结果。动态竞争导致的后置拒绝仍如实处理，不吞错、不回滚已接受成果、不按 description 猜测映射名称、不自动抹掉依赖、不新建批量事务 API。
3. **原有修正通道可实际消费反馈。** 针对本轮新表达的必要字段和依赖范围，核对 pi runner 的 Schema 编译、错误定位与有界修正路径；避免只在 Python 检查中有效。无需延长 Prompt、增加修正次数或另造 Server–模型往返协议。必要的简短提示应从实际契约出发，不要求用户学习这些内部格式。

主要源码定位：`cairn/src/cairn/assessment_contracts.py`、`dispatcher/worker_protocol/submission.py`、`dispatcher/contracts.py`、`server/services/coverage_slots.py`；Reason 的 `dispatcher/tasks/reason_context.py`、`reason.py`、`reason_writes.py`、`server/services/assessment_attempts.py`；必要的 Worker 边界在 `container/pi-agent/src/protocol.ts`、`runner.ts`、`submission.ts`。路径均相对对应源码根，行号只是本轮线索。实施前查看当前实现及已有改动，不机械改满文件清单。

### 直接验证、历史项目与交付边界

- 使用**合成的非敏感材料、生产 Schema 生成器和实际本地 runner 校验路径**做一次定向验证；不重放 `proj_030` 的目标请求，不调用付费模型。复用现有用例位置，如 `container/pi-agent/tests/contracts.test.mjs`、`cairn/tests/test_goal_driven_reason.py` 及实际相关服务端用例，按本轮行为选择少量用例，不默认跑整个文件或套件，不新建验收框架。
- Explore：对应三条记录的缺字段形状验证——有 observations/真实本地证据的样例不再仅因缺 baseline/variants 被拒；缺 observations 的 finding 在本地即被拒并可在同一有限修正环补充**已有**材料。完成修正后，经 Dispatcher 和真实 Server 验收函数对字段语义应一致；缺证据、无效归属或缺 finding 身份/复现材料仍拒绝。blocked/observation 不被误强制填写对照。这里的合成通过不代表历史候选已验真。
- Reason：自拟名称依赖在写入前被本地拒绝；合法既有 ID 和第二条引用 `@1` 的情况可用；前向/不存在的 `@N` 被拒绝；修正后两条实际写入的依赖正确，既有幂等/partial 行为不被破坏。使用受控本地数据或现有测试接缝，不操作用户项目。
- 改动冻结 binding 时，验证新请求可被实际 Server 接受，不仅是 Schema 单独通过；历史冻结快照保持原样，不改哈希、不追溯补字段、不静默使用新契约冒充旧契约。不可续交付的旧快照如实报告，不加猜测性兼容层。
- 默认不扩大 provider_error 调查、不更换模型、不调整预算/timeout/重试、不重写调度、候选恢复、前端、权限或报告系统。日志中无关的原文输出问题不作为本轮顺手重构授权。
- **`proj_030` 不自动恢复。** 它已有失败 Intent 和 Reason 记录；部署新代码不等于这些记录自动变成可续跑。保留 candidate、artifacts、失败、已接受的 `i004` 和所有回执，不重新解释或批量导入历史候选。实施者只读说明现有正常恢复路径的实际限制；若恢复需要另行修复产品动作、操作项目或费用授权，交回用户决定，不要求用户手改数据库或默认新建付费项目。
- 运行相关改动后按 `docs/deployment-linux-zh.md` 及实际入口构建并交付完整系统，先重新确认 `8001` 与真实数据库归属，不能切到默认数据库或用隔离副本 ready 代替用户实例。涉及容器运行代码才重建对应 Worker 镜像，并说明已停止的旧容器是否仍持有旧版本；不能仅凭镜像 tag 更新声称该容器已加载修复。不要为部署私自删除/重建 `proj_030` 容器；如其他在途任务会受影响，先说明并取得所需操作许可。
- 不自动恢复/重跑项目、访问目标、调用付费模型、改历史授权、清库、commit 或 push。实施完成后在本节追加“代码实现、本地验证、部署生效、真实任务验收”四种不同状态及剩余限制；完成本轮即停止。

### 本次实施结果（2026-09-29）

- **代码实现：已完成本次约定范围。** Goal-driven Explore 的生产 `submit_result` Schema、Dispatcher 与 Server 复用同一静态字段规则：finding/tested_no_finding 仍要求非空 hypothesis、observations 和 evidence_refs，但不再统一强制 baseline、variants、controls；其他 outcome 的必要字段同步到本地规则。Server 继续检查证据归属、漏洞身份与复现，legacy phased/Coverage 未改。Goal-driven Reason 从权威项目快照绑定全部既有 Intent ID，按位置仅允许前项 `@N`；新字段进入请求边界、Schema、写前验证、Server 精确键注册和冻结/续交付，不猜测自拟名称、不删除依赖或改动逐项 partial/幂等回执。
- **本地验证：已通过定向合成验证，非目标验收。** Python 定向用例 186 passed；依赖写入、partial/幂等回执选定用例 10 passed（133 项中其余 123 项未选）；Worker `npm run build` 与实际 runner/生产 Schema 的 Node 用例 43 passed。合成 SQLite Server 边界验证了缺对照的记录可接受、缺观察/证据/归属/漏洞身份/复现会拒绝，Reason 新 binding 携实际 Schema hash 可注册；runner 在原有限修正环中补充已有合成观察和合法依赖。前端 `build:server` 的类型检查与打包、Worker 镜像构建及镜像内 `self-check` 均通过；最初并行构建与一次单独前端重试曾因内存不足失败，随后串行构建成功，临时 swap 已关闭并删除。未访问被测目标、调用付费模型或用 `proj_030` 做写入验证。
- **部署生效：新建服务与未来 Worker 已就绪，不等于旧容器更新。** 按实际 `ARENA_SERVER_PORT=8001`、`127.0.0.1:55433/cairn_isolated` 安装并重启 Server/Dispatcher；Server 进程配置及只读数据库身份核对一致。`deploy/status.sh` 显示 running、HTTP ready、Dispatcher idle heartbeat；`/health` 和 UI 根路径均为 HTTP 200。新 `arena-worker:local` 镜像 ID 为 `sha256:9b9aa6dd56452f101bf8baa82b12ea7540ae901912f1408c514ad81f61c23cde`，仅未来新建容器会取用新 tag；保留的 `proj_030` 已停止容器仍指向旧镜像 `sha256:9d06091d829e329e69ec0a2e32ce3d2e911513d43e392a7b8a0cc41d09cd63ca`，未删除或替换。部署前确认无在途 TaskRun；未切换或清空数据库。
- **真实任务验收：未进行。** `proj_030` 仍为 paused；未恢复或重跑、未重新解释历史 candidate、未更改失败 Intent、冻结快照、回执或授权，不能声称历史漏洞已确认或本次代码能使其原执行成功。既有 failed Intent 不会因正常 resume 自动重开，paused 状态也不能直接新增 Intent；历史冻结交付不自动获得新 binding。原 `provider_error` 仍未查明。若需继续真实项目，应另行决定合法恢复/新任务路径及费用授权，不要求手工改库，也不默认新建付费项目。

---

## 本质

Dispatcher 是 Cairn 的客户端执行器。它负责：

1. 协调项目容器生命周期
2. 给 Agent 下发明确任务
3. 代 Agent 调用 Cairn API 写回图

Agent 不直接认领 Intent，不直接 heartbeat，不直接调用 Cairn API。Agent 只接收 Dispatcher 下发的任务，返回结构化结果；Dispatcher 再决定如何请求 Server。

---

## 设计要点

1. Agent 的输出任务收敛成三类：`bootstrap`、`reason` 和 `explore`；`bootstrap` 只在项目初始态运行，让 Agent 直接尝试解决整个问题；主阶段只有在已解决时才返回，且必须同时给出关键 Fact 和 `complete`；若主阶段超时，再由 `bootstrap_conclude` 收尾产出 Fact；`reason` 负责读图判断是否完成或是否需要提出一个新 intent；`explore` 只负责执行一个已认领 intent 并产出一个 Fact 结论。
2. Dispatcher 是唯一的协议写入者和控制面；Agent 不 claim、不 heartbeat、不直接调用 Cairn API。
3. 超时策略按任务类型定义；`bootstrap` 和 `explore` 都支持“第一阶段执行 + timeout / parse-fail 后用同一 session 进入 conclude 收尾”的双阶段模式。
4. Prompt 以 markdown 文件形式随代码分发；支持按 prompt 组切换；Worker 行为由 `claudecode`、`codex`、`mock` 等 driver 实现；`dispatch.yaml` 只描述运行期参数。
5. 调度上，项目初始态按 `project.bootstrap_enabled` 和 Worker 能力决定优先 `bootstrap` 或直接 `reason`；非初始态出现新 Fact / Hint 等新态势时优先 `reason`，否则优先消费可认领的 `explore` intent；`reason` 的并发约束通过服务端的项目级 `project.reason` lease 表达为“单项目最多一个”，`bootstrap` 的并发约束是“单项目最多一个保留 bootstrap intent 且最多一个 bootstrap 任务”，跨项目允许并行；`runtime.interval` 被刻意复用为主循环节拍和带 claim 任务的 heartbeat 周期。
6. Worker 按独立的 LLM 并发配额单元建模；同一个 key 不拆成多个 Worker，因此并发控制使用 `workers[].max_running` 即可。
7. 运行日志按“状态变化优先”设计：稳定轮询、正常 heartbeat、重复 skip 原则上不刷屏；容器创建、任务派发、离线 readiness、容器内新进程启动、超时、收尾、释放 intent、worker 进入短暂不可选窗口等事件必须可见。
8. 项目容器收尾不应阻塞主调度循环；多个已完成项目的容器 cleanup 可以并行进行。
9. 项目切到非 `active` 后，Dispatcher 必须把它视为硬停止：不再派发新任务；对本地仍在运行的 `bootstrap`、`explore`、`reason` 任务立即发出取消；对已取消任务不再进入 conclude fallback；并在后续轮询中停止该项目容器，杀掉容器内仍在运行的 Agent 进程。
10. 当前实现按“单 Dispatcher 实例”设计和测试；不支持多个 Dispatcher 同时连接同一服务端共同调度。
11. 若项目曾 `completed` 后又被服务端显式 `reopen` 为 `active`，Dispatcher 不做特殊分支：它会把这视为普通 active 项目继续调度；同时若该项目容器仍处于已排队 cleanup 状态，Dispatcher 会先等待 cleanup 完成，避免与旧的 completed/stopped cleanup 竞态。
12. 已知限制：当前协议只记录当前 claim 持有者，不保留 Intent 的 worker 历史；因此项目被 `stopped` 后，随着 open intent 的 `worker` 被服务端清空，Dispatcher/UI/API 都无法直接展示“停止前最后是谁在推进这个 intent”。后续若要补这部分可观测性，较合理的方向是在 Intent 上增加类似 `worker_history` 的历史字段，而不是改变当前 claim 语义。

补充：

- 项目被删除后，Dispatcher 会把它视为 `deleted`。这和 `stopped` 一样会先取消本地运行中的任务，但容器收尾语义不同：`deleted` 对应的 orphan 容器会被直接删除，而不是仅停止后保留。

---

## 架构概览

这个项目在架构上可以分成 4 个部分：

1. Cairn Server
2. Dispatcher
3. 项目容器
4. Worker / Agent CLI

### 1. Cairn Server

Server 是协议真相源。

它负责：

- 保存 Project / Fact / Intent / Hint
- 提供协议接口
- 维护 Intent 的认领、心跳、结论状态
- 维护项目级 `reason` lease 的认领、心跳与释放状态

### 2. Dispatcher

Dispatcher 是这个工程要实现的核心。

它负责：

- 拉取项目图状态
- 决定当前该派发哪一种任务
- 选择哪个 Worker 来执行
- 管理项目容器和 Worker 进程
- 维护 session、超时、确定性离线 readiness、收尾
- 把结果写回 Cairn Server

### 3. 项目容器

每个项目对应一个运行容器。

这个容器是该项目的执行环境，通常负责：

- 提供工具链
- 提供网络环境
- 承载该项目下的 Worker 进程

### 4. Worker / Agent CLI

Worker 不是协议参与者本身，而是 Dispatcher 管理下的执行单元。

例如：

- Claude Code CLI
- Codex CLI

它们负责：

- 接收 Dispatcher 渲染好的 prompt
- 在当前容器内执行任务
- 输出结构化 JSON

### 组件关系

```text
                         +----------------------+
                         |     Cairn Server     |
                         |----------------------|
                         | Projects / Facts     |
                         | Intents / Hints      |
                         | Protocol API         |
                         +----------^-----------+
                                    |
                           read / write API
                                    |
+-----------------------------------------------------------+
|                         Dispatcher                        |
|-----------------------------------------------------------|
| Scheduling / Task Dispatch / Session / Timeout / Health   |
| Container Lifecycle / Protocol Writeback                  |
+----------------------+----------------------+-------------+
                       |                      |
             manage container        manage container
                       |                      |
          +------------v-----------+  +------v-------------+
          |   Project Container A  |  | Project Container B|
          |------------------------|  |--------------------|
          | Worker / Agent CLI     |  | Worker / Agent CLI |
          | - Claude Code          |  | - Codex            |
          | - Codex                |  | - ...              |
          +------------------------+  +--------------------+
```

### 执行主链路

Dispatcher 会同时读取两类数据：

- 结构化接口：用于调度、状态判断、intent 选择、协议写回
- `GET /projects/{project_id}/export?format=yaml`：仅用于构造 prompt 所需的图快照

1. Dispatcher 从 Server 读取项目图
2. Dispatcher 依据调度规则选择任务类型和 Worker，并物化该项目的 durable Worker binding
3. 对 fresh `bootstrap`、`reason`、`explore`，Dispatcher 在 claim/admission 前执行固定的离线 readiness；原交付 `delivery_retry` 跳过此步骤
4. readiness 成功后，如果是 `explore`，Dispatcher 通过 Server admission 原子认领 intent 并创建 attempt；如果是 `reason`，则通过 `POST /projects/{project_id}/reason/claim` 认领项目级 reason lease
5. Dispatcher 渲染 prompt，在项目容器内登记 delivery 并启动 Worker 业务进程
6. Worker 输出结构化 JSON
7. Dispatcher 解析结果，并调用 `POST /projects/{project_id}/complete`、`POST /projects/{project_id}/intents`、`POST /projects/{project_id}/intents/{intent_id}/conclude`、`POST /projects/{project_id}/intents/{intent_id}/release` 或 `POST /projects/{project_id}/reason/release`

项目若被切到 `stopped`，这条主链路会在下一轮短路：Dispatcher 不再把该项目纳入 active 调度集合，会先取消本地仍在运行的任务，再转入容器 cleanup 流程并停止项目容器。对 `bootstrap` / `explore` 来说，被 `stopped` 取消后不会再进入 conclude fallback，因此不会再额外落 Fact。项目恢复为 `active` 后，Dispatcher 会重新读取图状态，再决定是继续 `explore`、进入 `reason`，还是在初始态依据 `project.bootstrap_enabled` 和 Worker 能力重新选择 `bootstrap` 或 `reason`。项目若在 `completed` 后被服务端 `reopen`，对 Dispatcher 来说也等价于“重新变成 active 且图上多了一个新 fact”；下一轮会按普通 active 项目继续调度。

Worker 选择规则：

1. 先按任务类型筛选
2. 再过滤掉已达到 `max_running` 的 Worker
3. 再过滤掉处于短暂不可选窗口内的不健康 Worker
4. 在剩余 Worker 中，优先选择 `priority` 更小的
5. 如果 `priority` 相同，则优先选择当前运行中任务数更少的
6. 如果仍然相同，则随机选择
7. fresh `bootstrap`、`reason`、`explore` 在 claim/admission 前对选中的 Worker 执行离线 readiness；失败时不创建 TaskRun 或 AssessmentAttempt、不认领 Intent，也不增加 Explore 逻辑计费，并让该 Worker 进入 readiness 专属的短暂不可选窗口
8. readiness 通过后再由 Server 原子执行 claim/admission；`reason` claim 成功后由 `POST /projects/{project_id}/reason/heartbeat` 维持 lease；`delivery_retry` 继续使用 Server 指定的原 Worker、container、session、binding 和绝对 deadline，不重复 readiness

---

## 配置模型

Dispatcher 使用一个运行期配置文件：

- `dispatch.yaml`

也就是说：

- 使用者只需要提供 `dispatch.yaml`
- 探索任务 Prompt 以 markdown 文件形式随代码分发，场景未指定时通过 `runtime.prompt_group` 选择兼容回退目录；默认回退为 `default`
- Worker 的业务命令、session 处理和二阶段收尾能力由对应 driver 实现；离线 readiness 由独立 Module 统一实现，不属于 driver 接口

代码目录可以采用类似组织：

```text
dispatcher/
  models.py
  prompting.py
  output_parser.py
  contracts.py
  prompts/
    default/
      bootstrap.md
      bootstrap_conclude.md
      reason.md
      explore.md
      explore_conclude.md
    mock/
      bootstrap.md
      bootstrap_conclude.md
      reason.md
      explore.md
      explore_conclude.md
  workers/
    base.py
    registry.py
    adapters/
      claudecode.py
      codex.py
      mock.py
```

本文档附录给出：

- `dispatch.example.yaml` 的示例内容
- 上述 markdown prompt 的示例内容

---

## 任务模型

### 三类任务一览

| 任务 | 触发条件 | 输入 | 输出 | 超时策略 |
| --- | --- | --- | --- | --- |
| `bootstrap` | 项目 `active`；`project.bootstrap_enabled=true`，且配置中存在支持 `bootstrap` 的 Worker 或项目已经存在保留 bootstrap intent；facts 只有 `origin` 和 `goal`；当前没有普通 intent；允许不存在 bootstrap intent，或只存在保留的 open `bootstrap` intent | `{origin}`、`{goal}`、`{hints}` | 主阶段成功时固定返回 `fact + complete`；收尾阶段只返回 `fact` | 双阶段：`timeout` 后可进入 `conclude_timeout` 收尾；两阶段都失败则 release 保留 intent，下轮仍按新项目重试 |
| `reason` | 项目 `active`；当前项目无未认领 intent；当前项目内无其他 `reason`；首次触发或满足“新态势”重触发条件 | `{graph_yaml}`、`{fact_ids}`、`{open_intents}` | `complete` 对象；或 `intent` 对象；或空 `data` | 仅 `timeout`；超时或非法结果直接作废，不写图 |
| `explore` | 项目 `active`；存在一个当前可认领的未结论 intent | `{graph_yaml}`、`{intent_id}`、`{intent_description}` | 一个 Fact 结论描述 | 单阶段：超时直接作废；双阶段：超时或输出解析失败时可进入 `conclude` 收尾 |

### `bootstrap`

#### 触发条件

- 当前项目仍然是 `active`
- `project.bootstrap_enabled = true`，且配置中存在支持 `bootstrap` 的 Worker 或项目已经存在保留 bootstrap intent
- facts 恰好只有 `origin` 和 `goal`
- intents 为空，或只存在保留的 open `bootstrap` intent
- 保留 `bootstrap` intent 的约定固定为：
  - `description = "bootstrap"`
  - `creator = "dispatcher.bootstrap"`
  - `from = ["origin"]`

#### 输入

- `{origin}`
- `{goal}`
- `{hints}`

其中：

- `{hints}` 是 JSON 数组文本，便于 Worker 在项目起始阶段快速吸收策略信息
- `bootstrap` 不读取图 YAML，不依赖普通 intent 图结构

#### 输出契约

`bootstrap` 使用独立输出契约：

- 主阶段只有在已经解决问题时才返回
- 主阶段返回时必须同时包含 `fact` 和 `complete`
- 如果主阶段没有在超时前解决问题，就不会返回合法结果；Dispatcher 会终止它并进入 `bootstrap_conclude`
- `bootstrap_conclude` 只负责收尾总结，因此只返回 `fact`

```json
{
  "accepted": true,
  "data": {
    "fact": {
      "description": "拿到两个 flag，分别为 flag{...} 与 flag{...}；同时获得管理员 shell，权限证明见 /tmp/proofs/root.txt"
    },
    "complete": {
      "description": "已拿到并验证完成 Goal 所需的全部关键结果，Goal 达成"
    }
  }
}
```

约束：

- 主阶段返回时，`data.fact.description` 和 `data.complete.description` 都必须存在
- 主阶段不允许只返回 `fact`
- `bootstrap_conclude` 只允许返回 `fact`，不允许返回 `complete`

#### 接口映射

`bootstrap` 复用普通 intent 协议，但使用保留 intent：

| 接口 | 用途 | 何时使用 |
| --- | --- | --- |
| `POST /projects/{project_id}/intents` | 创建保留 `bootstrap` intent | 初始态项目首次进入时，如尚不存在该 intent |
| `POST /projects/{project_id}/intents/{intent_id}/heartbeat` | claim 并维持 `bootstrap` intent | 派发前先 claim；执行中按 `interval` 周期发送 |
| `POST /projects/{project_id}/intents/{intent_id}/conclude` | 将 `bootstrap` 产出的关键结果写成 Fact | `bootstrap` 或 `bootstrap_conclude` 返回合法 `fact` 后调用 |
| `POST /projects/{project_id}/complete` | 基于刚写入的 bootstrap fact 直接完成项目 | 仅当 `bootstrap` 主阶段成功返回 `fact + complete` 时调用 |
| `POST /projects/{project_id}/intents/{intent_id}/release` | 放弃本次 bootstrap 尝试 | 两阶段都失败，或命令直接失败时调用 |

#### 超时与失败

- `bootstrap` 第一阶段使用 `timeout`
- `bootstrap_conclude` 第二阶段使用 `conclude_timeout`
- 主阶段如果在 `timeout` 内解决问题，就返回 `fact + complete`
- 第一阶段超时、输出解析失败或返回了不满足契约的结果时，如果 Worker 支持 session / conclude，则进入 `bootstrap_conclude`
- `bootstrap_conclude` 的 prompt 必须明确要求“不继续推进，不等待未完成任务，只总结当前最关键事实”，因此它只产出 `fact`
- 如果 `bootstrap` 主阶段成功返回合法 JSON，则 Dispatcher 会先 conclude 写入 fact，再立即 complete
- 如果 `bootstrap_conclude` 成功返回合法 JSON 且 conclude 写回成功，则保留 intent 被结论落定，项目不再视为初始态
- 如果主阶段的 `complete` 写回失败，已写入的 fact 仍然保留，后续可由下一轮 `reason` 再完成项目
- 如果两阶段都失败，或 conclude 写回失败，则 release 当前 `bootstrap` intent，不写 Fact；项目下轮仍然按新项目处理

### `reason`

#### 触发条件

- 当前项目仍然是 `active`
- 当前项目没有未认领 intent
- 当前项目的 `project.reason` 为空，也就是当前没有其他 `reason` lease 正在运行
- 首次触发只发生在“当前没有任何 open intent”时
- 之后只有出现新的态势才重新触发；这里的“新态势”限定为：
  - Fact 数量增加
  - Hint 数量增加
  - 项目从“存在 open intents”进入“没有 open intents”
- 单次 `explore` 失败、掉心跳、释放但 intent 仍保持 open，不构成新的态势，不应触发新的 `reason`

#### 输入

- `{graph_yaml}`
- `{fact_ids}`
- `{open_intents}`

其中：

- `{fact_ids}` 是 JSON 数组文本，用于显式列出当前合法的 Fact id
- `{open_intents}` 是 JSON 数组文本，用于显式列出当前所有未结论的 intent；因此即使有别的 intent 正在 `explore`，只要出现了新的 Fact / Hint，`reason` 仍可能再次被触发
- 这两个占位符都是 prompt 层辅助，不替代 server 的最终校验

#### 输出契约

已完成：

```json
{
  "accepted": true,
  "data": {
    "complete": {
      "from": ["f008"],
      "description": "flag{abc} 满足 goal 要求"
    }
  }
}
```

未完成，提出新 intent：

```json
{
  "accepted": true,
  "data": {
    "intent": {
      "from": ["f003"],
      "description": "尝试 SQL 注入"
    }
  }
}
```

未完成，不提新 intent：

```json
{
  "accepted": true,
  "data": {}
}
```

约束：

- `data.complete` 存在时，它必须是对象，且 `complete.from` 和 `complete.description` 都必须存在
- `data.complete` 存在时，不应再带 `intent`
- 如果 `intent` 存在，则 `intent.from` 和 `intent.description` 都必须存在
- 如果 `{open_intents}` 为空，说明当前图里没有任何进行中的探索；此时若没有 `data.complete`，则必须返回 `intent`
- 如果 `{open_intents}` 非空，且没有 `data.complete`，则允许不返回 `intent`

#### 接口映射

`reason` 启动前，Dispatcher 必须先 claim 项目级 reason lease，并在执行期间持续 heartbeat；这一状态会直接出现在 `GET /projects` 和 `GET /projects/{project_id}` 的 `project.reason` 字段中，供前端和其他消费者观察。

| `reason` 输出 | Dispatcher 动作 | 备注 |
| --- | --- | --- |
| `data.complete` 存在 | 调用 `POST /projects/{project_id}/complete` | `worker` 使用当前执行该任务的 `workers[].name` |
| `data.complete` 不存在，且带 `intent` | 调用 `POST /projects/{project_id}/intents` | `creator` 使用当前 Worker 名；`worker` 固定写 `null` |
| `data` 为空对象 | 不写图 | 不写 Fact / Intent / Complete |

写回失败的日志语义：

- 如果 `POST /projects/{project_id}/complete` 或 `POST /projects/{project_id}/intents` 返回 `403`，通常表示项目已不再是 `active`，本次任务直接作废，记 `info`
- 其他写入失败也直接作废，不做立即重试，只记日志
- 无论本轮是否写图，只要项目仍是 `active` 且 reason lease 仍在自己手里，Dispatcher 都会在收尾时调用 `POST /projects/{project_id}/reason/release`；如果项目已 `completed` 或 `stopped`，则由服务端直接清空该 lease

#### 超时与失败

- `reason` 只使用 `timeout`
- 超时直接作废
- `accepted: false` 直接作废，记 `warn`
- 其他执行错误也直接作废，例如：
  - 命令退出码非 `0`
  - 输出不是合法 JSON
  - JSON 缺少必要字段
- 以上情况都不写 Fact / Intent / Complete，只记日志
- 失败（非取消）后进入一个短暂的 per-project 退避（默认 5s）：该窗口内不再对本项目触发 `reason`，但 `explore` 不受影响；`reason` 成功后立即清除该退避。目的是避免同一态势在 parse/timeout 类失败上每个调度循环立即重试，浪费 LLM 调用

### `explore`

#### 触发条件

- 当前项目仍然是 `active`
- 存在一个当前可认领的、尚无结论的 intent

#### 输入

- `{graph_yaml}`
- `{intent_id}`
- `{intent_description}`

#### 输出契约

正常返回：

```json
{
  "accepted": true,
  "data": {
    "description": "发现 /search 参数存在报错注入"
  }
}
```

即使没有打出漏洞，也应返回一个客观探索结论，而不是空响应。例如：

```json
{
  "accepted": true,
  "data": {
    "description": "对 /search 参数测试常见 SQL 注入 payload，未发现可利用注入迹象"
  }
}
```

约束：

- `data.description` 必须存在，且必须是客观事实结论
- 不允许输出“我拒绝帮助渗透”“这不安全”等文本作为 `description`

#### 接口映射

`explore` 会涉及三类协议接口：

| 接口 | 用途 | 何时使用 |
| --- | --- | --- |
| `POST /projects/{project_id}/intents/{intent_id}/heartbeat` | claim 并维持持有 | 派发前先 claim；执行中按 `interval` 周期发送 |
| `POST /projects/{project_id}/intents/{intent_id}/conclude` | 产出 Fact 并结论落定 Intent | `execute` 或 `conclude` 返回合法结论后调用 |
| `POST /projects/{project_id}/intents/{intent_id}/release` | 放弃当前尝试 | 失败路径使用 |

派发顺序要求固定为：

1. Dispatcher 先选中一个可认领 intent
2. Dispatcher 先调用一次 `POST /projects/{project_id}/intents/{intent_id}/heartbeat` 作为 claim
3. 只有 heartbeat 成功后，才真正启动 `explore` 对应的 Worker

#### 超时与失败

`explore` 要兼容两种模式：

1. 单阶段模式：只有 `execute`
2. 双阶段模式：`execute + session + conclude`

其中 `conclude` 是附加收尾阶段，不是主流程必经阶段。

第一阶段使用 `timeout`。
如果是双阶段模式，第二阶段使用 `conclude_timeout`。

正常完成：

- 如果 `execute` 在 `timeout` 内正常返回合法 JSON，且 `accepted: true`
- Dispatcher 直接调用 `POST /projects/{project_id}/intents/{intent_id}/conclude`
- `POST /projects/{project_id}/intents/{intent_id}/conclude` 成功即完成结论落定，无需额外 `release`
- 如果 `POST /projects/{project_id}/intents/{intent_id}/conclude` 写入失败，本次任务直接作废，不做立即重试，释放当前 intent，只记日志

可进入二阶段收尾的异常：

- 这类异常只在“双阶段 Worker”上进入 `conclude`
- 适用异常只有两种：
  - 执行超时
  - Dispatcher 无法从第一阶段输出里正确提取并解析结果，例如：
    - 输出不是合法 JSON
    - JSON 缺少必要字段
    - `accepted: true` 但 `data` 结构不符合当前任务要求
- 这类异常在“单阶段 Worker”上不进入 `conclude`

双阶段收尾流程固定为：

1. Dispatcher 杀掉当前进程
2. 保留这次任务对应的 session id
3. 在保持 heartbeat 的前提下，用同一个 session 直接进入 `conclude`
4. `conclude` 的 prompt 必须明确要求“不要继续探索，只总结截至目前已经完成的探索与结论”
5. 如果 `conclude` 在 `conclude_timeout` 内返回合法 JSON，且 `accepted: true`：
   - Dispatcher 调用 `POST /projects/{project_id}/intents/{intent_id}/conclude`
   - 成功则结束
6. 如果 `conclude` 再次超时，或输出不合法，或返回 `accepted: false`，或 `POST /projects/{project_id}/intents/{intent_id}/conclude` 写入失败：
   - 整次探索作废
   - 不写任何图数据
   - 释放当前 intent
   - 只记日志

单阶段 Worker 的异常处理：

- 如果当前 Worker 不支持 `session` 或 `conclude`，则它属于单阶段模式
- 这时第一阶段一旦出现“超时”或“输出解析 / 结构校验失败”，直接按失败处理
- 处理方式是：杀进程、整次探索作废、不写任何图数据、释放当前 intent、记 `warn`

直接失败，不进入 `conclude`：

- 第一阶段返回 `accepted: false`
- 命令退出码非 `0`
- Worker 进程根本没有产生可读取结果
- Dispatcher 在进入结果解析前就已经确定本次执行失败

以上情况都：

- 不进入 `conclude`
- 不写任何图数据
- 释放当前 intent
- 清理本地任务状态
- 只记日志

---

## 调度策略

### 全局调度

核心规则：

1. 已运行项目优先，但只优先可立即派发的任务
2. 如果某个运行中项目处于初始态且可执行 `bootstrap`，优先继续它
3. 否则如果某个运行中项目存在可执行的 `explore`，优先继续探索它
4. 如果所有运行中项目都暂时没有可派发任务，且 `runtime.max_running_projects` 还有余量，就启动一个未开始的新项目

`runtime.interval` 的设计约定：

- `runtime.interval` 不只是一个普通轮询间隔
- 它被刻意复用为两个地方的统一节拍：
  - Dispatcher 主循环间隔
  - 带 claim 任务（`bootstrap` / `explore`）的 heartbeat 周期
- 这样做的目标是减少额外时序参数，先保持实现简单
- 这是一项明确设计决策，不是偶然耦合

调度伪代码可以保持成下面这种粒度：

```text
for project in running_projects_round_robin:
  if has_dispatchable_bootstrap(project):
    dispatch_bootstrap(project)
    continue
  if has_dispatchable_reason(project):
    dispatch_reason(project)
    continue
  if has_dispatchable_explore(project):
    dispatch_explore(project)
    continue

if running_project_count < runtime.max_running_projects:
  maybe_start_one_new_project()
```

### 项目内调度

对于单个项目，Dispatcher 读完整项目状态后，按下面顺序调度：

1. 如果项目仍处于初始态，先按 `project.bootstrap_enabled` 和 Worker 能力决定路径：未开启或没有支持 `bootstrap` 的 Worker 时直接 `reason`，否则执行 `bootstrap`；若已经存在保留 bootstrap intent，则继续该阶段
2. 如果满足“新态势”重触发条件，优先派发 `reason`
3. 否则如果存在未认领 intent，派发 `explore`
4. `reason` 的去重按“态势”做，而不是按总图变化做：首次只有在当前没有任何 open intent 时才触发；之后只有当前 Fact / Hint 数量增加，或项目从“存在 open intents”进入“没有 open intents”时，才重新触发
5. 如果 `reason` 返回 `data.complete`，Dispatcher 调用 `POST /projects/{project_id}/complete`
6. 如果 `reason` 没有返回 `data.complete` 且带 `intent`，Dispatcher 调用 `POST /projects/{project_id}/intents`
7. 如果 `reason` 既没有返回 `data.complete`，也没有返回 `intent`，则本轮不写图

另外：

- 初始态项目里，如果选择了 `bootstrap` 路径且 bootstrap intent 已被 claim，则这一轮不再派发 `reason` 或普通 `explore`
- 即使同一项目里已经有进行中的 `explore`，也允许继续派发一个 `reason` 任务
- 但前提不是“刚新增了 intent”，而是“确实出现了新的 Fact / Hint 等新态势”；仅仅因为上一个 `reason` 刚创建了新的 intent，不应该立刻再次 `reason`
- 仍然要求当前没有未认领 intent、当前项目内没有其他 `reason` 任务在运行、且没有超过 `runtime.max_project_workers`

`reason` 的去重规则建议保持简单：

- 记录该项目上次成功完成 `reason` 时的 Fact 数量、Hint 数量，以及当时是否仍存在 open intents
- Dispatcher 对“当前已观察到、且已有 open intents、但尚无 checkpoint 的 active 项目”建立基线 checkpoint；启动时已有项目和运行中晚到项目都适用，不应吞掉运行过程中第一次新增的 Fact / Hint
- 首次没有历史记录且当前没有 open intents 时，直接触发
- 之后只有 Fact / Hint 数量增加，或项目从“有 open intents”变为“无 open intents”时，才再次触发 `reason`
- 不把“总 intent 数量增加”当作重触发条件；因为这通常只是上一次 `reason` 刚创建了新 intent，并不代表出现了新的态势
- `explore` 的执行失败、掉心跳、临时 release 只会让 intent 重新等待被探索，不会额外触发 `reason`

### Worker 配置提示与容量诊断

- Worker 配置页建议准备至少 3 个独立、启用的 `claudecode` Worker，以提升并行能力和故障切换空间；该数量只用于运行提示。
- Worker 数量和手工 Connectivity 结果不会阻止配置保存、项目创建、Dispatcher 启动或任务调度。
- Connectivity 只验证 Server 到 Provider 的最小 API 请求，不替代 Dispatcher 对容器和 CLI 的确定性离线 readiness。
- 项目设置 `preferred_workers` 时，Dispatcher 仍按 Worker `name` 做严格过滤；名单无匹配项时不会回退到全局 Worker 池。
- 容量诊断应分开说明 project admission (`max_running_projects`)、global slots (`max_workers`)、per-project slots (`max_project_workers`) 和每个 Worker 的运行槽位，避免把建议 Worker 数量与实际并发容量混为一谈。

### 并发约束

- 当前设计下，只支持一个 Dispatcher 实例连接同一服务端执行调度
- 如果同时运行多个 Dispatcher，本地维护的 admission、Worker 健康状态、并发计数、容器清理和 bootstrap 去重都不会跨进程协调，因此不属于支持场景
- 单个项目内，同一时刻最多只能有一个 `bootstrap` 任务在运行
- Dispatcher 会尽力让单个项目在初始态时只保留一个 open `bootstrap` intent
- 单个项目内，同一时刻最多只能有一个 `reason` 任务在运行
- 跨项目允许并行运行多个 `reason`
- `reason` 也计入对应项目的 `runtime.max_project_workers`
- `runtime.max_workers`：Dispatcher 同时运行中的任务总数上限
- `runtime.max_running_projects`：当前 dispatcher 运行期内已接手且仍为 `active` 的项目 admission 上限；项目即使暂时没有可派发任务，只要仍为 `active`，也继续占用该名额，直到其退出 active
- `runtime.max_project_workers`：单个项目内同时运行的任务上限，统一计入 `bootstrap`、`reason` 和 `explore`
- `workers[].max_running`：单个 Worker 自身的并发上限；达到上限后，这个 Worker 暂时不再参与派发

---

## Worker 配置

### 字段定义

Dispatcher 固定从 `stdout` 取全文作为模型正文输出。

`dispatch.yaml` 中与 Worker 相关的运行期字段如下：

| 字段 | 含义 | 说明 |
| --- | --- | --- |
| `id` / `name` | Worker 静态标识 | `name` 仍用于项目 `preferred_workers`、协议写回和调度审计；新结构化配置通常令两者相同 |
| `engine` / `type` | Worker driver 名 | 支持 `claudecode`、`codex`、`pi`、`mock`；两组字段为新旧格式兼容别名 |
| `model_id` | Model 引用 | 新结构化配置通过 Model → Provider 派生模型、Base URL 和 credential |
| `task_types` | 支持的任务类型 | `bootstrap`、`reason`、`explore` |
| `concurrency` / `max_running` | Worker 并发上限 | 达到上限后暂不派发；两组字段为兼容别名 |
| `priority` | 选择优先级 | 数字越小越优先 |
| `enabled` | 是否参与调度 | 关闭后不再成为新任务候选 |
| `env` | 运行时环境变量 | legacy/direct-env 配置继续支持；`mock` 的 phase 耗时和结果概率也通过这里配置 |

系统提供四类 Worker driver：

- `claudecode`
- `codex`
- `pi`
- `mock`

也就是说：

- `dispatch.yaml` 负责声明“用哪个 driver、能跑什么任务、并发多少、环境变量是什么”；如果是 `mock`，各 phase 的模拟分布也放在 `env`
- 业务命令、session 提取和 `conclude` 恢复由 driver 代码负责；readiness 固定为 `cairn-agent --self-check`，由 Dispatcher 的独立 Module 负责

### Driver 接口

每个 Agent / CLI 工具在代码里对应一个独立 driver 文件，并实现统一接口。

统一接口至少应覆盖这些能力：

- `prepare_session()`：需要时预先生成 session id
- `build_execute(worker, prompt, session)`：构造第一阶段执行命令
- `extract_session(session, stderr)`：需要时从 `stderr` 提取 session id，或继续使用预生成 session
- `build_conclude(worker, prompt, session)`：在双阶段 `explore` 中恢复同一 session 做收尾
- `supports_conclude()`：声明该 driver 是否支持双阶段 `explore`

readiness 不通过 driver 扩展：所有 Worker 都执行相同的 `cairn-agent --self-check`，避免把 provider/model 请求重新引入业务 admission 之前。

这些 driver 的能力约定是：

- `claudecode` 支持双阶段 `explore`
- `codex` 支持双阶段 `explore`
- `mock` 支持双阶段 `explore`

并发建模约定：

- 一个 Worker 应代表一个独立的 LLM 并发配额单元
- 不考虑“多个 Worker 共用同一个 key”的情况
- 并发控制使用 `workers[].max_running`

### 确定性离线 readiness

readiness 由 Dispatcher 的独立 Module 统一实现，固定运行：

```bash
cairn-agent --self-check
```

该命令使用空环境，只验证本地 Agent SDK、协议结构和声明能力，不读取 provider/model 凭据，不发起模型请求，也不创建业务 request file、Pi session、candidate、repair、checkpoint spool 或 BrowserSession。

执行规则：

- Dispatcher 启动检查与 fresh 任务前检查复用同一 Module 和同一严格响应解析器。
- fresh `bootstrap`、`reason`、`explore` 在 Worker 选择和 durable binding 物化之后、任何 claim/admission、TaskRun、AssessmentAttempt 或 Explore 逻辑计费之前执行 readiness。
- `delivery_retry` 不重复 readiness，继续使用 Server 指定的原 Worker、container、session、binding 和绝对 deadline。
- 只有退出码为 `0`，且唯一 JSON 对象满足 `status == "ok"`、`engine == "pi"`、`offline is true` 和合法无重复的 `capabilities` 数组时才接受。
- 失败固定归类为 `readiness_timeout`、`readiness_transport`、`readiness_oom`、`readiness_invalid` 或 `readiness_process`；正向 OOM 证据优先于 exit 137 的 timeout 分类。
- readiness 失败只进入 readiness 专属 Worker cooldown；成功只清除 readiness 自己的退避历史，不清除 provider/auth/business transport 历史。
- probe 返回后仍由 Server admission 原子重校验 generation、项目状态、deadline、budget 和 Intent claim，Dispatcher 不复制这些业务策略。

Provider Connectivity 是独立的配置诊断，不属于 readiness，也不参与业务任务前的健康判定。

### CLI 接入约定

#### `claudecode` driver

依赖环境变量：

- `ANTHROPIC_MODEL`
- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_AUTH_TOKEN`

该 driver 的 provider 配置只用于业务执行；readiness 不读取这些变量，也不发送在线探活请求。

已知行为：

- driver 预先生成 session id
- 首轮可以预先指定 session id
- 如果该 id 已存在，命令会报错，不会复用旧会话
- Dispatcher 固定从 `stdout` 取全文作为结果正文

第一阶段执行：

```bash
claude --session-id "{session}" --dangerously-skip-permissions -p -- "{prompt}"
```

二阶段收尾：

```bash
claude -r "{session}" --dangerously-skip-permissions -p -- "{prompt}"
```

#### `codex` driver

依赖环境变量：

- `CODEX_MODEL`
- `CODEX_BASE_URL`
- `OPENAI_API_KEY`

该 driver 的 provider 配置只用于业务执行；readiness 不读取这些变量，也不发送在线探活请求。

已知行为：

- 首轮 session id 会打印在 `stderr`
- 可以用正则 `session id:\s*([0-9a-fA-F-]+)` 提取
- Dispatcher 固定从 `stdout` 取全文作为结果正文

第一阶段执行：

```bash
codex exec --dangerously-bypass-approvals-and-sandbox --model "{env.CODEX_MODEL}" \
  -c 'model_provider="cairn"' \
  -c 'model_providers.cairn.name="cairn"' \
  -c 'model_providers.cairn.wire_api="responses"' \
  -c 'model_reasoning_effort="high"' \
  -c 'model_providers.cairn.base_url="{env.CODEX_BASE_URL}"' \
  -c 'model_providers.cairn.env_key="OPENAI_API_KEY"' \
  -- "{prompt}"
```

二阶段收尾：

```bash
codex exec resume "{session}" --dangerously-bypass-approvals-and-sandbox --model "{env.CODEX_MODEL}" \
  -c 'model_provider="cairn"' \
  -c 'model_providers.cairn.name="cairn"' \
  -c 'model_providers.cairn.wire_api="responses"' \
  -c 'model_reasoning_effort="high"' \
  -c 'model_providers.cairn.base_url="{env.CODEX_BASE_URL}"' \
  -c 'model_providers.cairn.env_key="OPENAI_API_KEY"' \
  -- "{prompt}"
```

#### `mock` driver

`mock` driver 用于本地观察 dispatcher 的成功、失败和超时路径。

行为约定：

- `runtime.prompt_group: "mock"` 时，prompt 本身是结构化 JSON，不再依赖自然语言说明
- `reason` prompt 最少包含 `phase`、`fact_ids`、`open_intents`
- `explore` / `conclude` prompt 最少包含 `phase`、`intent_id`
- `bootstrap` / `bootstrap_conclude` prompt 最少包含 `phase`、`origin`、`goal`、`hints`
- driver 会先解析 prompt 里的 `phase` 字段，再读取对应的 `MOCK_<PHASE>` JSON 环境变量选择当前 phase 的模拟结果
- 每个 phase 都在自己的 JSON 里配置 `delay: [min, max]`；单位是秒，支持小数；随机耗时超过 Dispatcher 外层 timeout 时，就会自然表现为超时
- `reason.noop` 只会在 `open_intents` 非空时参与抽样；mock 会自动避开当前上下文下不合法的结果

`mock` 支持五个业务 phase：

- `bootstrap`
- `bootstrap_conclude`
- `reason`
- `explore_execute`
- `explore_conclude`

支持的结果如下：

- `bootstrap.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `bootstrap_conclude.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `reason.outcomes`: `complete`、`intent`、`noop`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `explore_execute.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `explore_conclude.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`

命名约定：

- 每个 phase 一个变量：`MOCK_<PHASE>`，例如 `MOCK_REASON`
- 变量值必须是 JSON 对象，结构为：`{"delay":[min,max],"outcomes":{...}}`
- `delay` 必须是两个非负数字，单位是秒
- 每个 phase 的所有结果概率都使用 `0` 到 `1` 的小数，并且总和必须严格等于 `1.0`

### 配置校验规则

#### 加载时校验

启动 Dispatcher 时，应完成静态校验：

- `dispatch.yaml` 必须存在且可读取
- `runtime.max_workers` 必须存在
- `runtime.max_running_projects` 必须存在
- `runtime.max_project_workers` 必须存在
- `runtime.interval` 必须存在
- `runtime.healthcheck_timeout` 必须存在且为正整数；它只限制离线 readiness
- `runtime.prompt_group` 必须存在
- `tasks.bootstrap.timeout` 必须存在
- `tasks.bootstrap.conclude_timeout` 必须存在
- `tasks.reason.timeout` 必须存在
- `tasks.explore.timeout` 必须存在
- `tasks.explore.conclude_timeout` 必须存在
- 每个 Worker 都必须有 `type`
- 每个 Worker 都必须有 `max_running`
- `task_types` 只允许 `bootstrap`、`reason`、`explore`
- `type` 只允许 `claudecode`、`codex`、`mock`
- `max_running` 必须是正整数
- `claudecode`、`codex`、`mock` 都支持双阶段 `explore`
- `runtime.prompt_group` 对应的 prompt 目录必须存在
- 代码工程中的 prompt 资源必须存在
- 默认 prompt 组下，`reason.md` 必须至少覆盖 `{graph_yaml}`、`{fact_ids}`、`{open_intents}`
- 默认 prompt 组下，`explore.md` 必须至少覆盖 `{graph_yaml}`、`{intent_id}`、`{intent_description}`
- 默认 prompt 组下，`bootstrap.md` 和 `bootstrap_conclude.md` 必须至少覆盖 `{origin}`、`{goal}`、`{hints}`
- `mock` prompt 组下，`reason.md` 必须至少覆盖 `{fact_ids}`、`{open_intents}`
- `mock` prompt 组下，`explore.md` 和 `explore_conclude.md` 必须至少覆盖 `{intent_id}`
- `mock` prompt 组下，`bootstrap.md` 和 `bootstrap_conclude.md` 必须至少覆盖 `{origin}`、`{goal}`、`{hints}`
- `mock` worker 的 `MOCK_*` 变量名只能使用系统支持的 phase
- `mock` worker 每个 phase 的概率都必须在 `0` 到 `1` 之间，且总和必须严格等于 `1.0`

#### 运行时校验

任务真正派发时，还需要做运行时校验：

- driver 必须存在且支持该任务类型
- fresh `bootstrap`、`reason`、`explore` 必须在 claim/admission 前通过固定离线 readiness；不能仅凭退出码接受，还必须验证严格的 self-check JSON
- `delivery_retry`、业务 execute/conclude 和历史结果解码都不得再次触发 readiness
- 只有支持当前任务类型的 Worker 才能被选中
- 只有当前运行中任务数小于 `max_running` 的 Worker 才能被选中
- 处于本地 `retry_after` 窗口内的不健康 Worker 不参与派发
- readiness 通过后，`bootstrap` 和 `explore` 都必须完成 Server claim/admission，成功后才真正启动任务线程
- 如果要走双阶段 `explore`，则第一阶段必须成功拿到 session id，才能进入 `conclude`
- 如果要走双阶段 `bootstrap`，则第一阶段必须成功拿到 session id，才能进入 `bootstrap_conclude`
- Dispatcher 必须对 `stdout` 全文做 JSON 解析和任务级结构校验
- `accepted: false`、JSON 非法、字段缺失、接口写回失败等情况都必须记日志，且不做立即重试

---

## 配置字段速查

### `dispatch.yaml`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `server` | 是 | Cairn Server 的 base URL |

### `runtime.*`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `runtime.max_workers` | 是 | Dispatcher 同时运行中的任务总数上限 |
| `runtime.max_running_projects` | 是 | 当前 dispatcher 运行期内已接手且仍为 `active` 的项目上限，不受历史遗留容器影响 |
| `runtime.max_project_workers` | 是 | 单个项目内同时运行的任务上限，统一计入 `bootstrap`、`reason` 和 `explore` |
| `runtime.interval` | 是 | 统一节拍配置；既是 Dispatcher 主循环间隔，也是带 claim 任务的 heartbeat 周期 |
| `runtime.healthcheck_timeout` | 是 | `cairn-agent --self-check` 离线 readiness 的统一外层 watchdog 超时 |
| `runtime.prompt_group` | 是 | 场景未指定探索 Prompt 时使用的兼容回退目录名，默认 `default`；durable 报告由冻结的场景策略选择报告 Prompt |

### `container.*`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `container.image` | 是 | 项目容器镜像 |
| `container.network_mode` | 是 | 项目容器网络模式 |
| `container.completed_action` | 是 | 项目 completed 后对容器的处理方式 |

`container.completed_action` 可选值：

- `remove`：项目 completed 后删除容器
- `stop`：项目 completed 后只停止容器，保留现场

实现约定：

- completed project 的容器 cleanup 可以异步并行进行，不要求阻塞主调度循环
- 如果项目已从 Server 删除，Dispatcher 会把找不到对应项目的 `cairn-dispatch-*` 容器视为 orphan，并执行 stop 清理

### `tasks.*`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `tasks.bootstrap.timeout` | 是 | `bootstrap` 第一阶段超时 |
| `tasks.bootstrap.conclude_timeout` | 是 | `bootstrap` 双阶段收尾超时 |
| `tasks.reason.timeout` | 是 | `reason` 的超时 |
| `tasks.explore.timeout` | 是 | `explore` 第一阶段超时 |
| `tasks.explore.conclude_timeout` | 是 | `explore` 双阶段收尾超时 |

### `workers.*`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `name` | 是 | Worker 静态标识；协议写回时使用这个值作为 `creator` 或 `worker` |
| `type` | 是 | Worker driver 名；支持 `claudecode`、`codex`、`mock` |
| `task_types` | 是 | 该 Worker 支持的任务类型列表 |
| `max_running` | 是 | 该 Worker 自身的并发上限 |
| `priority` | 是 | 当前任务类型的候选 Worker 中，数字越小优先级越高 |
| `env` | 是 | 该 Worker 的变量表；具体必需 key 由对应 driver 决定并在启动时校验 |

补充：

- Worker 选择顺序是：先过滤任务类型、`max_running` 和处于本地 `retry_after` 窗口内的 Worker，再按 `priority`，同优先级优先选当前运行数更少的，最后随机；fresh `bootstrap`、`reason`、`explore` 在 claim/admission 前执行离线 readiness，失败的 Worker 进入 readiness 专属短暂不可选窗口；`delivery_retry` 和 `bootstrap_conclude` / `explore_conclude` 不重复 readiness
- 离线 readiness 由独立 Module 统一实现；业务执行命令、session 提取和二阶段 `conclude` 由对应 driver 代码负责
- prompt 内容从代码工程里的 markdown 资源加载

---

## 附录：示例配置与 Prompt 内容

### `dispatch.example.yaml`

```yaml
server: "http://127.0.0.1:8000"

runtime:
  max_workers: 5  # total running tasks
  max_running_projects: 3  # total active projects admitted by this dispatcher runtime
  max_project_workers: 2  # per-project running tasks, including bootstrap + reason + explore
  interval: 3  # intentional shared cadence: scheduler loop interval + claim-task heartbeat interval, in seconds
  healthcheck_timeout: 15  # offline cairn-agent --self-check watchdog, in seconds
  prompt_group: "default"  # selects prompts/<group>/

tasks:
  bootstrap:
    timeout: 120
    conclude_timeout: 30
  reason:
    timeout: 45
  explore:
    timeout: 600
    conclude_timeout: 120

container:
  image: "tmp:latest"
  network_mode: "host"
  completed_action: "stop"  # options: "remove" | "stop"

common_env:
  TSEC_BASE_URL: "http://<SERVER_HOST>/api"
  TSEC_AGENT_TOKEN: "..."

workers:
  # 同一模型拆成多个 Worker 的原因，应是它们使用不同的 API key，
  # 从而拥有彼此独立的并发配额。
  - name: "claude-sonnet-thinker"
    type: "claudecode"
    task_types: [bootstrap, reason]
    max_running: 1
    priority: 0  # lower number wins; ties prefer fewer running tasks, then choose randomly
    env:
      ANTHROPIC_MODEL: "claude-sonnet-4-6"
      ANTHROPIC_BASE_URL: "https://api.example.com"
      ANTHROPIC_AUTH_TOKEN: "sk-ant-worker-a"

  - name: "claude-sonnet-doer"
    type: "claudecode"
    task_types: [bootstrap, explore]
    max_running: 1
    priority: 1
    env:
      ANTHROPIC_MODEL: "claude-sonnet-4-6"
      ANTHROPIC_BASE_URL: "https://api.example.com"
      ANTHROPIC_AUTH_TOKEN: "sk-ant-worker-b"

  - name: "codex-gpt54"
    type: "codex"
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 3
    env:
      CODEX_MODEL: "gpt-5.4"
      CODEX_BASE_URL: "https://api.example.com/v1"
      OPENAI_API_KEY: "sk-worker-c"

  - name: "codex-gpt54-alt"
    type: "codex"
    task_types: [bootstrap, explore]
    max_running: 1
    priority: 4
    env:
      CODEX_MODEL: "gpt-5.4"
      CODEX_BASE_URL: "https://api.example.com/v1"
      OPENAI_API_KEY: "sk-worker-d"

  - name: "mock-observer"
    type: "mock"
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 9
    env:
      MOCK_HEALTHCHECK: '{"delay":[0.05,0.15],"outcomes":{"ok":0.9,"fail":0.1}}'
      MOCK_BOOTSTRAP: '{"delay":[0.1,12.0],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_BOOTSTRAP_CONCLUDE: '{"delay":[0.1,2.2],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_REASON: '{"delay":[0.1,2.2],"outcomes":{"complete":0.1,"intent":0.3,"noop":0.1,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.2}}'
      MOCK_EXPLORE_EXECUTE: '{"delay":[0.1,12.0],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_EXPLORE_CONCLUDE: '{"delay":[0.1,2.2],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
```

补充：

- 这里只是用示例值表达配置结构
- `common_env` 会先并到每个 worker 的环境变量里，然后再被 `worker.env` 覆盖；即 `common_env < worker.env`
- Worker 按独立的 key / model 并发配额单元建模，不应让多个 Worker 共享同一个 key

下面 5 份 markdown 内容对应代码工程里的 prompt 文件。

### `bootstrap.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你需要直接解决这个问题，目标是完成 Goal。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "..."}}}
```
## 上下文
### Origin
{origin}
### Goal
{goal}
### Hints JSON 数组
{hints}
````

### `reason.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你当前只做 `reason`。
你要同时判断两件事：
1. 现有 facts 是否已经满足 goal。
2. 如果还未满足，当前是否需要提出一个新的 intent。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
拒绝任务时返回：
```json
{"accepted": false, "reason": "..."}
```
已满足 goal 时返回：
```json
{"accepted": true, "data": {"complete": {"from": ["f001"], "description": "..."}}}
```
未满足 goal，但需要提出新 intent 时返回：
```json
{"accepted": true, "data": {"intent": {"from": ["f001"], "description": "..."}}}
```
未满足 goal，且当前不需要提出新 intent 时返回：
```json
{"accepted": true, "data": {}}
```
## 规则
- 如果下面的 `open_intents` 为空，说明当前图里没有任何进行中的探索；此时若不返回 `data.complete`，则必须返回 `intent`。
- `intent.from` 只能从下面的合法 fact id 中选择。
## 上下文
### 图快照
{graph_yaml}
### 当前合法的 fact id JSON 数组
{fact_ids}
### 当前所有未结论的 intent JSON 数组
{open_intents}
````

### `explore.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你当前只做 `explore`。
你只处理当前这一条 intent，执行探索并给出最终事实结论。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
拒绝任务时返回：
```json
{"accepted": false, "reason": "policy_refusal"}
```
正常返回示例：
```json
{"accepted": true, "data": {"description": "..."}}
```
## 规则
- `description` 必须是客观探索结论，不要输出解释性废话。
## 上下文
### 图快照
{graph_yaml}
### 当前 intent id
{intent_id}
### 当前 intent 描述
{intent_description}
````

### `explore_conclude.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你当前正在对同一个 `explore` 做收尾总结。
- 不要继续探索。
- 只总结截至目前已经完成的探索和结论。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
拒绝任务时返回：
```json
{"accepted": false, "reason": "policy_refusal"}
```
正常返回示例：
```json
{"accepted": true, "data": {"description": "..."}}
```
## 规则
- `description` 必须是客观探索结论，不要输出解释性废话。
## 上下文
### 图快照
{graph_yaml}
### 当前 intent id
{intent_id}
### 当前 intent 描述
{intent_description}
````

### `bootstrap_conclude.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
- 不要继续推进。
- 不要等待未完成的任务。
- 只总结截至目前已经确认、且对达到 goal 最有帮助的关键事实。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```
## 上下文
### Origin
{origin}
### Goal
{goal}
### Hints JSON 数组
{hints}
````
