# Dispatcher

> 本文保留了历史执行器与旧协议设计，不代表各段均已与当前实现对齐。当前定向修复见“proj_038：Reason 提交结构与依赖同时有效”；`proj_037` 的截断续行、`proj_036` 的完成拒绝后新决策、`proj_035` 的可靠交付及更早记录保留在后文，以各节实际实施记录为准，不自动成为本轮待办。不要据旧 driver、失败处理或示例输出重建当前系统。

## proj_038：Reason 提交结构与依赖同时有效（2026-10-12 UTC；代码、定向本地接受验证与完整宿主部署完成，真实任务未验收）

### 已确认问题与批准范围

本轮仅解决研究被内部提交协议矛盾打断，不重新审计历史待办，不扩展自主研究反馈利用，也不宣称增强自主选题或真实漏洞发现能力。proj_037 只提供上下文，已生效的截断续行及原提交修正边界保持不变。

- **现场与本轮快照分开。** 用户已确认 tr017 的两个后续 Intent 带 `controls: []`，本地通过后遭 Dispatcher 拒绝。本轮重新核对当前进程与实际 PostgreSQL `127.0.0.1:55433/cairn_isolated`，02:21–02:24 UTC 的 `REPEATABLE READ READ ONLY`／ROLLBACK 快照确认 proj_038 为 active／generation 1，共17个Run；最新 tr017 为 failed／result_schema，验证码 phase_shape_invalid，无接受 receipt、delivery_retry、continuation 或受控完成拒绝边界。Dispatcher 新鲜投影仍为 execution_retry_blocked／reason／generation 1。全库无 queued／running，终局Run均有同代 termination／cleanup 确认。旧PID、时间及状态不是永久事实，启停前重新核查。
- **当前生产首因。** `submission.py` 给 Reason intents 添加的 prefixItems 仅声明 depends_on。JSON Schema 的 items 不再校验被 prefixItems 覆盖的位置，而 maxItems 又使合法批次全部落在前缀内，因而完整 Intent 的额外字段、必填和类型限制被绕过。Runner 实际使用 TypeBox Compile，不是 Ajv；gateway 的 validate_phase_data 仍严格拒绝非法结构。历史冻结 Schema 原件未找到，本轮不把当前生产复现冒称逐字验证历史冻结文件。
- **用户确认的方案。** 每个 prefix 项以 allOf 复用原 items 的完整 Intent Schema，并叠加既有当前位置的依赖限制；贯通生产 Schema → 既有 Worker 本地诊断与同会话修正 → 未放宽的 Dispatcher／Server 接受。只改必要生产者与已有验证用例，不新增 controls、不忽略额外字段，不扩大 result_schema／business_acceptance 自动重试，不增加次数、超时、提示词、状态、依赖或模型变更。

### 代码实现与本地接受验证

实施分支 `fix/proj-038-reason-schema`，从干净的 `docs/installation-guide`／`cd1a09b` 创建；不 commit／push。

- **唯一生产代码改动。** `submission.py` 的 prefixItems 每项为 `allOf: [原 items, 原 depends_on 片段]`，同时保留唯一完整 Intent 定义及冻结 binding、来源限制、maxItems、已有依赖与同批前序 @N；修正原“items 仍检查每项”的错误注释。不复制字段模型、不加 controls 拒绝特例，也不在依赖片段加入 additionalProperties:false 或 depends_on 必填。Runner、gateway、Server、截断续行和重试策略均未修改。
- **生产本地校验节点：1 passed。** 扩展 `container/pi-agent/tests/contracts.test.mjs` 的原生产 Reason Schema 用例，以 TypeBox Compile 验证三项批次：各前缀位置的 controls、缺 from／description、非法嵌套 from、未知来源 Fact、错误 description／Intent 类型均拒绝；self／forward／unknown 依赖拒绝；省略／null、已有ID、合法前序 @1／@2 通过。原 loopback HTTP 的依赖诊断与同会话修正仍通过。
- **完整接受链路：6 passed。** 扩展 `test_pi_submission_receipts.py::test_goal_reason_structure_repairs_in_session_and_server_accepts_intent_receipts`，参数为非法字段在首／中／末项，及有／无已有依赖。生产上下文与冻结交付登记 → 实际 Pi 请求 Schema → 真实 Runner 首次本地拒绝 controls（additionalProperties、`/data/intents/*`）→ 同 Run／session 仅删除该字段，原合法内容与 @1／@2 不变 → candidate capture／outcome → 原 gateway → 原 writer → 隔离生产 Server router 实际接受三项 Intent。核对 provider／冻结／candidate Schema 摘要一致，安全修正工具范围、候选 parent、来源顺序、实际ID依赖、索引0／1／2的接受receipt；重复写回未增加 Intent／来源，冻结恢复及篡改 inputs 拒绝检查仍通过。不是只断言拒绝或 Runner status，也未手换最终 payload 绕过 Runner。
- **验证边界。** 全部使用本地合成 provider 响应和隔离 SQLite，未访问目标、执行项目保存脚本或调用付费模型；没有写实际项目库。只跑上述定向节点，未跑全套、lint或浏览器矩阵，未安装依赖或另建验证框架。完整系统 `build:server` 成功（包含 vue-tsc 与 Vite），仅有插件耗时与大chunk提示；没有UI业务改动。增量代码差异与 git diff --check 已通过。

### 部署、正常恢复与剩余限制

**完整宿主部署完成，原项目容器保留。** 本次生产改动仅在宿主 Schema：PiDriver 随新请求发送它，原 Runner 动态编译；未重建 Worker 镜像或替换项目容器。单独运行 start 可能复用旧进程，因此按批准范围重载完整宿主系统，不以健康检查冒称真实研究接受。

- **启停前门禁。** 02:52:17 UTC 的只读快照及紧接启停的第二次核查确认：唯一 active 项目为 proj_038／generation 1、无 Reason claim；全库 queued／running=0、未结算 candidate=0，物理 termination／cleanup 均确认，155个历史Run的 candidate_recovery 均为 stopped。最新 Reason 仍为 tr017／failed／result_schema，Dispatcher idle／running_task_count=0，同代 execution_retry_blocked。项目容器无 ExecIDs，进程仅 docker-init／sleep；未发现会中断在途工作或开启新模型调用的条件，未执行项目生命周期操作。
- **实际重载与烟测。** 执行现有 `deploy/stop.sh`，再执行 `deploy/start.sh --host 0.0.0.0 --port 8001`；新 Server PID `1817480`、Dispatcher PID `1817654`，均与旧PID不同，已核对 `/proc` 启动身份、PID元数据的 proc_start_time 及 cwd。`/health` 为200／ok，`/`及入口引用的10项 `/static/` 资源均200，bytes与本次发布产物相同。两个PostgreSQL容器healthy。烟测脚本首次错误使用元数据键 starttime 而出现 KeyError；修正为 proc_start_time 后进程检查通过。静态资源检查最初错误筛选 `/assets/`、实际收集0项，因此该次不算资源验证；改用 `/static/` 并强制非空后，10项全部复验通过。以上为检查脚本错误，不是应用故障，不隐藏成全程一次通过。
- **保留与部署后状态。** 原项目容器仍为 `341b742fbf83fda36f9b0087d57ebfdab6d750123797d7bb4deb60079cb2be59`，image仍为 `sha256:6bf2537efe9953204ed4642a0794130ce6c7d20a6ad13fe7527e09c08a1a16cc`，启动时间 `2026-10-10T09:43:23.807947236Z`、宿主PID `3978514`未变，原可写层与材料未替换。02:55:43 UTC 从新Server实际环境定位同一 `cairn_isolated`，显式 `REPEATABLE READ READ ONLY`／ROLLBACK 核对：proj_038仍active／generation 1、未claim，最新Reason仍tr017／failed／result_schema；Run17、Fact11、Intent10、candidate15、outcome15、Artifact116，与重载前一致。全库在途／未结算candidate仍为0，Dispatcher新鲜决策为02:55:42的 execution_retry_blocked／reason／generation 1，running_task_count=0。部署没有恢复研究或新增Run；新Schema可供后续新请求使用，不回写历史冻结交付或自动重投失败候选。

**保留材料的正常恢复路径，仅说明、不执行。** 当前 active 上的 resume 是幂等noop，不能清除同代 result_schema 阻断；reopen 只适用于 completed，当前不可用。另行授权后可以普通 pause → 等待物理停止／ACK → resume：generation 1升至2，resume不再增代；不删事实、Intent、证据、历史Run或费用／预算。pause 使用 Docker stop 而非remove，保留原容器可写层及session材料；即使无在途Run，也须当前 activation 的 stopped／pause_generation 和其他恢复前置条件确认。新代 fresh Reason 使用新session及已接受共享事实／证据，不是续作失败tr017；未接受候选／旧会话正文不会自动升级成成果，也不保证自动消费全部原材料。resume开放自动研究，可能产生新模型费用。修复Schema或重启本身不清除旧失败，不将其加入同代fresh特例，不手改DB。

本轮不 pause／resume／reopen／重跑 proj_038，不改历史记录、候选、证据或费用，不访问目标、不发起付费调用。真实项目接受、研究效果与发现能力均未验收，不能用合成接受或健康检查代替。i002→i003、i007→i008 的研究反馈／前提利用不纳入本轮；实施结束即停止。

## proj_037：输出截断与研究连续性（2026-10-10；代码、定向验证、宿主部署与新镜像完成，旧容器保留，真实任务未验收）

### 评价结论与本轮取舍

**本次不符合实际研究预期：Goal“拿到flag”尚未实现，多数核心实验没有执行，项目仍显示active却已被Reason准入阻断。** 不是因为零漏洞就判失败，而是已有有效线索、原执行窗口仍充足时，Harness把单次模型输出截断误当提交修正，主动禁止了后续研究；随后又把本地合法的无动作Reason结果作为终局业务错误。10/12个Run成功、7/8个Intent完成不能代替有效实验和用户目标。

第一性原理上，**模型的一条响应结束、一次研究结束、结果提交和业务接受是不同边界**。维护研究连续性需要在相同事实／执行上下文与真实资源边界内继续尚未完成的工作，而不是反复创建Intent购买同一段准备。必要的只读交付、授权、真实截止和证据保障应保留；错误在于进入它们的条件不区分响应截断与实际提交。本轮仅贯通“输出截断后合理续行 → 决策在本地按冻结输入修正 → 下游接受”这一条路径，不新增Agent组织、恢复平台或通用重试状态机。

### 已确认运行、成果与上轮修复边界

- **实例。** 07:30–07:35 UTC的只读快照：实际Server PID `3448019`、Dispatcher `3448152`，cwd均为本仓库，入口`8001`，数据库`127.0.0.1:55433/cairn_isolated`；使用显式`REPEATABLE READ READ ONLY`查询并ROLLBACK，不导入应用或调用业务GET。项目容器`3760988adc2c`、宿主PID `3463450`，运行镜像仍为`sha256:be208a94654d80a82637aec22c11b02f464204a476274f3195236a5991345f88`。实际容器内`dist/runner.js`、`dist/model.js`及相关SDK provider/agent-loop与本地安装产物逐字节相同。PID及路径仅对应此次观察，后续须重新定位。
- **目标与状态。** 精确项目`proj_037`于06:25:57创建，Goal等值确认“拿到flag”；07:35仍为active、generation 0，无在途Run和开放Intent。12个Run为10成功、2失败；4个Reason中3成功、最后`tr012`失败，7个Explore中6成功、`tr004`失败，另1个Bootstrap成功。9个Fact包含origin/goal，8个Intent为7完成、1失败。7/40个逻辑Attempt全部各执行1次、charged=1，verification=0，不是预算耗尽。结果为5个temporarily_blocked、1个tested_no_finding、1个policy_denied；没有漏洞、Observation或Verification记录。11个候选/outcome均locally_validated，不等于每个都被业务接受。正式报告及其finish receipt为0，报告仍是显式请求，不能因此认定漏调度。
- **材料与研究价值。** 主会话核对全部54个Artifact（384999 bytes）的实际大小/SHA256，均与DB一致，其中22个显式登记原件的固定版本元数据也一致；没有出现上一轮材料变化/容量/identity故障。22份登记材料对应9组不同bytes，多个受阻任务引用相同先前源码／响应；复用原件本身正确，但重新登记不代表新实验。关键原件支持源码暴露、普通HTTP响应及有限路径排除，本地复刻为后续选题提供线索；尚无支持Goal的目标响应。研究从源码提出不同可检验假设，有事实复用和一条实测无发现分支，不能说完全没有研究；但5个核心Explore明确承认未完成所分配的关键payload实验，不能据它们的completed认定已测试或安全。
- **不作跨靶场能力排名。** 本次材料对应PHP反序列化挑战，不能把`proj_036`的已知CNEXT成果作为相同条件对照。12个Run冻结模型均为`wangsu-ds-flash-latest`、`anthropic-messages`；没有Hint，未见origin/goal之外人工创建的Fact/Intent，但不证明完全没有人工操作。未重新访问目标、执行保存脚本或提交靶场判分；可靠货币费用与后续模型效果未知，不以预算次数或本地model.cost=0推算费用。
- **上轮新边界并未被本项目触发。** 当前源码的受控completion_rejection持久化、共享digest、route/claim/admission和fresh边界主线已静态核对贯通；本项目没有complete protocol_write、completion_rejection或reason_decision_boundary。最后失败发生在Dispatcher语义校验，不是Server完成409。因此`proj_037`既不能证明上轮动态完成恢复已真实验收，也不能证明那条修复失败；不扩大它来吞下新错误。

### 首因：把模型输出截断误当研究进入交付

1. **真实结束原因已核对，不采用模型“被中断”的自述作根因。** 只读解析实际容器内12个session的结构元数据，不输出Prompt、思考或会话正文。`tr001/002/003/004/005/007/009/010/011/012`在首次repair前一条assistant消息均为`stopReason=length`、`usage.output=16384`，仅有thinking块，无toolCall、无submit_result。对应事件均为`result_repair: submission_missing, submission_attempts=0, repair_rounds=1`。`tr006/008`没有该截断/修正。可复核位置为各Run的`pi-events.jsonl`与既有session；例如`tr003` session第11行/repair第12行、`tr012`第21行/repair第22行，只取结构和计量，不复制思考内容。
2. **不是20分钟到期。** 5个受阻Explore的冻结`execution_delivery.deadline_at`都是启动后20分钟；它们进入repair仅约100–125秒，连最终结束也比deadline提前16–17分钟。事件没有exploration_deadline。例如`tr003`于06:35:23进入repair、06:37:01结束，deadline为06:53:13；`tr010`07:08:20进入repair、07:09:34结束，deadline为07:26:18。项目预算亦未耗尽。
3. **执行器确有确定性混淆。** `container/pi-agent/src/model.ts:28–37`无条件声明`reasoning:false`、`maxTokens:16384`；`runner.ts:388–395`设thinkingLevel=off。实际收到thinking说明这些本地声明不是供应商已经关闭推理的证明。安装SDK的Anthropic转换仅在`model.reasoning`为真时发送thinking设置，见`node_modules/@mariozechner/pi-ai/dist/providers/anthropic.js:700–729`；供应商原始请求/服务端配置未取得，不猜测其私有参数。本轮核心问题即使调整token上限仍会存在：输出截断不等于交付开始。
4. **SDK与Harness连接点。** 实际SDK `pi-coding-agent/node_modules/@mariozechner/pi-agent-core/dist/agent-loop.js:103–145`遇到无toolCall的length消息会结束本次prompt。`runner.ts:469–508`只在toolCall循环内检查不正常stopReason，外层仅单独处理error；随后`:542–548`将无候选统一送入readonly修正，调用narrow并禁止shell/目标请求。于是模型只能交付已做准备和受限说明。`tr004`在该修正后再次请求bash，产生`pi_submission/tool_permission → policy_denied`，不是已证明的供应商安全拒绝、目标拒绝或真实授权范围不足。不要取消整个工具边界来消除这个结果。
5. **投入放大。** Reason根据blocked事实重新安排实验，但后续Explore重复经历相同截断与只读收尾。技术上事实已积累，行为上关键假设仍未实测；增加任务数、timeout、修正次数、模型输出上限或Prompt长度都不能代替正确区分执行与交付。

### 第二断点：冻结输入相关的Reason规则未进入本地修正

- `tr012`的冻结`execution_delivery.inputs.open_intent_ids=[]`。其候选`data`只有rationale，说明还需继续关键实验，并未建议complete；本地候选有效、进程exit 0。07:14:27的`ev447`却为`status=invalid/outcome=business_acceptance`，TaskRun受控失败为`origin=dispatcher_validation/code=phase_contract_invalid`，错误为`intents is required when open_intents is empty`；07:14:30终结。
- `dispatcher/worker_protocol/submission.py:188–200,457–469`让Reason动作全部可选，accepted:true只要求data存在；`dispatcher/contracts.py:1176–1195`在冻结开放工作为空、无abandon等合法动作时必拒空/缺intents。这是本地可修正边界与下游规则不一致，不是模型尚未发现漏洞应受到业务惩罚。`worker_result_gateway.py:286–300`分类后，`tasks/reason.py:252–276`直接返回，尚未进入reason_writes和Server complete。
- 不能把所有rationale-only禁掉：冻结开放Intent非空时，已有合法noop/等待语义，见`contracts.py:1189–1195`、`reason_writes.py:183–201`。也不能将无工作、无动作结果悄悄记为成功，让项目空转，或强迫编造新研究/Goal完成。
- **当前阻断有实时证据。** 07:34:59.863735的Dispatcher正常完成循环、running_task_count=0、active_project_count=1，精确项目决策为`execution_retry_blocked / reason / generation=0`。全部12个Run已有termination/cleanup双确认和candidate_recovery stopped；tr012于07:14:37恢复确认。不是旧恢复警告或Worker仍忙，也不是用户已经暂停。已有普通终局失败链阻断这次失败符合其当前规则，不能把business_acceptance整体加入fresh白名单。

### 本轮最小完整实施范围

**A. 在现有Runner区分响应截断与提交修正。** 在`message_end`/prompt返回的既有控制路径保留SDK结束原因；对本次明确的length、无提交/无部分工具调用、原探索窗口未到的执行，保持同一Run/session及已有材料，在原授权、deadline与已有资源边界内有界续行，不立即narrow或消耗结果修正轮次。不从自然语言猜“继续”，不把同一工作拆成新Intent/新Run重新探索，不重放已执行工具。正常最终文本却未提交仍遵守既有正式提交规则；已尝试submit、conclude_only、软截止、取消、provider失败和部分工具参数的处理不能被该分支放开。连续截断或预算不足必须有可解释的有限终点，可保留材料并如实受限，不能无限续行，也不能将截断当成功。复用原执行预算，不增加max_result_repairs、timeout或另一套恢复状态/配置平台；确需额外有限控制时说明原边界为何不足，不用任意大数兜底。不硬编码当前模型名、关闭全部推理或猜供应商参数；静态模型能力与实际推理表现差异作为事实记录，不以调整输出长度冒充根因修复。

**B. 让既有Reason动作规则由冻结输入同源地产生和消费。** 对goal-driven无开放工作时必须有合法决策的现有要求，贯通生产Schema、本地修正和下游语义；复用现有模型/Schema及冻结上下文，不增加另一套手写验收器或重复状态。保留有开放工作的合法noop、accepted:false、合法complete/intents/abandon等行为，不为通过校验强制产生研究或伪造完成。开放工作信息当前只在`reason.py:127–130`的`execution_delivery.inputs`，Schema构造只收phase/binding；必须同时更新实际请求与冻结身份消费者：`submission.py:475–484`、`tasks/delivery.py:38–41`和`workers/adapters/pi.py:145`，不能只在本地临时附加规则而使冻结Schema hash与Runner使用的不同。只前移本次已证的规则及必要上下游，不审计重写所有阶段Schema。

**不做：** 泛化放宽Reason终局重试或上轮fresh资格；只改错误名为stale；取消只读交付/静止/证据门禁；将blocked记为tested_no_finding；增加角色、全局等待、去重或报告重构；模型/SDK升级或切换；全局规范、UI、授权策略修改；改旧Run为success、清除失败、回填历史边界、自动恢复/重跑项目。A修正研究为什么早停，B修正可交付决策为什么出本地后被确定性拒绝，二者完成仍不证明当前靶场一定可解或平台整体发现能力达标。

### 直接验证、部署与历史项目边界

- 分析会话未运行任何测试。实施复用`container/pi-agent/tests/fixtures.mjs`与`runner.test.mjs`现有回环假provider，支持合成`max_tokens/length`结束事件：thinking-only截断 → 原session有界继续且正常执行工具仍可用 → 形成合成证据 → submit正常接受。核对deadline不重置、之前工具不重复、截断不计submission repair；连续length仍有界退出，真正提交后的修正/软截止/取消不能恢复目标工具。必要时修改旧“无提交一律readonly”的过宽断言，但正常最终文本不作为JSON fallback或业务接受。不得使用真实目标、模型或本项目脚本验证。
- 用同一生产Reason Schema与既有接受fixture核对：冻结无开放工作、rationale-only在本地得到可用字段诊断，同Run补合法决策后通过原语义接受；有开放工作时合法noop继续可用。同时核对冻结result_schema_sha256、实际请求Schema和candidate authority一致，不只测静态字符串；不通过放宽下游验收换绿。只跑这两条必要接缝及捕获具体风险的少量负例，不默认全套、lint、浏览器矩阵或新验证框架/依赖。
- 本轮涉及容器内Runner，实施后需构建并交付新Worker镜像，以及必要宿主代码的完整系统。启停/镜像应用前重新查实例与在途任务；**本次快照proj_037仍active**，无在途Run不等于没有自动派发风险。需要中断或可能触发付费研究时先说明并取得许可，不能用当前分析授权操作项目。只改镜像tag也不会让现存容器自动加载新Runner；明确后续执行与旧容器的生效范围，不删旧session/材料，不无依据保证正常resume即可修复。
- 当前tr012既无受控完成拒绝，也无新事实后的合法fresh边界；本轮新逻辑不自动回溯修复该历史失败。只读说明现有正常生命周期动作能否保留事实并开启新决策、如何应用新镜像及潜在费用，由用户决定；不调用可能写入的投影/GET，不手改DB、不擅自pause/resume/reopen，不要求再建proj_038作为确定性问题的验证手段。不存在可用正常路径时如实说明，不自动新增恢复功能。
- 更新本节实施记录，区分代码实现、定向本地验证、实际部署及真实任务效果。不要继承后文其他待办；不commit/push。此前分析分支`docs/installation-guide`，当时仅增量更新本方案，保护既有修改；完成原件/元数据/源码只读查证，未修改业务代码、测试、配置或DB，未运行测试/构建/启停/项目操作/目标请求/模型调用。后续本轮实施结果如下，不把分析时快照当作当前部署状态。

### 实施记录（2026-10-10）

**代码实现。** 在`docs/installation-guide`增量修改现有Runner、生产Schema与必要消费者，保留会话开始已有未提交修改；未新增依赖、配置项、数据库表／migration、手写验收器或恢复平台。未调整模型、SDK、maxTokens、timeout、max_result_repairs或completion_rejection／fresh资格。

- **A：响应结束不再直接等于交付开始。** `runner.ts`在assistant `message_end`保留SDK的length语义，仅当无完整／部分工具调用、未尝试提交、没有候选／capture失败／provider失败／冲突、尚未readonly且原探索窗口仍有效时，在同一Run/session追加短续行提示。既有上下文与工具结果继续保留，不重新发送原任务或重放已执行工具；仍使用原授权、执行／软截止timer与资源限制，不计入submission attempts或result repair。
- **有限终点。** 原deadline只能限制时长，不能限制短时间连续截断导致的请求数，因此每个Run最多提供两次正常执行续行，不借用结果修正额度。两次后仍length且无提交时以既有`submission_missing`终结，错误正文明确续行次数与截断上限，事件为`output_truncation_limit`；不是虚假成功或供应商拒绝。正常最终文本无提交仍进入原提交修正规则。submit后、conclude_only、软截止、取消、provider失败与不完整工具参数均不能借length恢复执行工具；不完整工具流在turn_end停止SDK自动续轮，避免已识别provider_error后又发一次请求。
- **B：冻结输入只进既有Schema。** goal-driven Reason以现有Server验证的`decision_input_digest`识别，`build_submit_result_schema(..., inputs=...)`使用原`execution_delivery.inputs.open_intent_ids`；缺少冻结list明确报错，不猜空列表或改用实时图谱。无开放工作时，accepted:true的data增加合法非空intents／complete／非空abandon的`anyOf`，只拒绝本轮已证的无工作且无动作提交；有开放工作仍保留rationale-only noop，accepted:false信封分支不变。不新增动作互斥或修复其他动作组合问题。
- **身份贯通。** `register_delivery`及原交付恢复摘要、Server交付登记重算、Reason首次／retry的Pi请求与candidate authority均由同一生产Schema和原冻结inputs产生；不在TS另写业务验收器，也不把inputs变成模型可填写的业务字段。旧delivery的Schema摘要若与当前规则不同，仍由既有`DeliveryUnavailable`明确拒绝，不回写历史hash或增加兼容fallback。下游Reason语义及writer未被放宽。

**本地定向验证。** 复用现有loopback假provider和隔离SQLite／接受fixture，只使用合成材料，不访问目标、执行项目保存脚本或调用付费模型。下述Server接受均调用隔离fixture中的生产router／service并取得实际业务记录，不写实际项目库，也不冒称已部署实例的真实任务验收。

- A的Runner定向用例覆盖thinking-only length后保留原session、之前bash不重放、随后bash生成合成证据／register／submit；连续length在初次加两次续行后准确失败且repair=0；原deadline、取消、submit后、conclude_only、软截止、不完整工具参数及既有provider 503边界。初次不完整工具流负例曾失败：预期1次请求，实际2次；修正turn_end停止SDK续轮后，该节点与provider 503复验为**2 passed**。新增length软截止节点为**1 passed**，未将首次失败隐藏成全程全绿。
- `test_pi_submission_receipts.py::test_explore_thinking_length_continues_to_synthetic_evidence_and_server_conclude`为**1 passed**：实际生产Explore Schema → Anthropic `max_tokens` → 同session正常bash一次生成合成证据 → register／submit → production gateway → 原Artifact持久化与conclude writer → 真实Server `conclude`接受Fact，Intent来源闭合。不是只断言Runner status或文件存在。
- B新增节点为**5 passed**：生产Reason context → 冻结交付登记 → 实际Pi工具Schema → 真Runner的本地拒绝／同session修正 → candidate spool capture／outcome → 原gateway／writer → 真实Server Intent与来源边、接受receipt及幂等重放。分别覆盖missing／空数组／null intents的无动作拒绝、accepted:false合法且下游为rejected、有开放工作的合法noop且图谱不写入。核对实际provider Schema、冻结result_schema_sha256、candidate authority完全一致；原冻结snapshot恢复、篡改inputs但不改hash被拒，缺inputs明确ValueError。
- 受新inputs参数影响的既有Server登记valid／missing节点和Reason原交付恢复节点为**3 passed**；既有生产Reason Schema本地依赖修正节点为**1 passed**。TypeScript构建成功，增量diff及`git diff --check`通过。未跑全套、lint或浏览器矩阵；复用已有系统pytest和venv依赖，未安装验证依赖。

**完整宿主部署与新镜像构建完成，旧项目容器未升级。** 用户另行允许完整宿主重载及新Worker镜像构建，同时要求保留所有现存容器，不恢复或重跑proj_037，不进行真实任务／付费验证。

- 正式`docker build -t arena-worker:local container`成功，沿用原Dockerfile、SDK `0.73.0`、Node `24.16.0`及既有依赖，build内的离线self-check通过。新image ID为`sha256:6bf2537efe9953204ed4642a0794130ce6c7d20a6ad13fe7527e09c08a1a16cc`；另用一次性`--network none`／readonly容器核对`/opt/cairn/pi-agent/dist/runner.js`，与本地最终产物SHA256均为`084d24f2abf624c985a9e473886668ecbf46bce3a553cce8a13532f9ba4d444b`。只清理该自建临时容器，不碰项目容器。新镜像仅对后续新建容器生效。
- `npm --prefix cairn/frontend run build:server`成功，包含必要vue-tsc和Vite，只有构建性能／大chunk提示，无UI业务改动。启停前最后快照为08:08:21 UTC：实际库`127.0.0.1:55433/cairn_isolated`、schema 47，全库queued／running=0、未结算candidate=0；proj_037有12个原Run，物理termination／cleanup均确认，仍active／generation 0／authorization epoch 1／7 of 40 Attempts，tr012普通business_acceptance失败链无新决策例外。原容器无ExecIDs，仅docker-init／sleep。只读SQL均显式`REPEATABLE READ READ ONLY`并ROLLBACK，不调用带凭据的业务GET。
- 预检两次在启停前停止：首次Docker top的comm-only格式不兼容，修正为pid,comm；第二次读到Dispatcher的0／空decisions投影。只读源码确认每轮先发summaries=[]、loop_completed=False的前置心跳；该投影不是Server项目状态或每tick都应保留的阻断门禁。随后以权威项目／持久失败链及真实无在途、物理容器状态重新核对，不把空投影当门禁解除，也未因此操作项目或发起模型调用。
- 经现有`deploy/stop.sh`及`deploy/start.sh --host 0.0.0.0 --port 8001`完整重载。08:09:28快照Server PID为`3736125`、Dispatcher PID为`3736254`，starttime／cwd已核对，实际DB和Dispatcher identity未变。真实socket的`/health`为200／ok，`/`及入口引用的10项静态资源均200且bytes与发布产物相同；无凭据访问受影响task-runs路由为401。Dispatcher心跳idle、running_task_count=0，既有pause/runtime能力仍在，两个DB容器healthy。这只是部署烟测，不替代本地接受或真实研究验收。
- 原项目容器精确ID仍为`3760988adc2c56f849c854e450b7dae148816f6345bbbc1d638ad94017a2e1d7`，image ID仍为`sha256:be208a94654d80a82637aec22c11b02f464204a476274f3195236a5991345f88`，原启动时间、宿主PID、空mounts、session／材料所在可写层均保留；不热替换、删除或重建。重载后proj_037仍active／generation 0／12个Run／7 of 40 Attempts，tr012仍failed／business_acceptance，无pause interruption、completion_rejection或decision boundary。全库在途和未结算candidate仍为0，未恢复或增加研究。

**历史项目与真实任务。** 本轮未pause／resume／reopen／重跑proj_037，未修历史Run、回填completion_rejection、手改DB或删除／覆盖session和材料。当前普通business_acceptance终局失败不因重启或新镜像自动清除；active项目也不符合reopen前提。现有pause会将generation 0提升到1，resume不再增加generation，正常准入可能开启新代fresh Reason并产生新推理费用；这是新决策，不是续作failed tr012，由用户另行决定。新tag不会更新原精确容器，runtime_signature使用镜像引用字符串，旧容器仍可能复用旧Runner；更换profile／tag还可能触发runtime mismatch。未找到保留原精确container ID/session的正常无损升级路径，本轮不热覆盖Runner或删除重建容器，也不保证pause/resume即可应用新Runner。真实供应商续行、模型效果、费用及Goal仍未验收；健康检查与合成接受不能替代真实漏洞发现能力证明，不要求新建proj_038查确定性协议问题。

## proj_036：完成建议拒绝后的连续决策（2026-10-10；代码、定向验证与完整部署已完成，真实任务待验收）

### 评价结论与第一性原理

**部分符合预期：本项目“拿到flag”有实质原件支持，上轮材料交付修正在真实任务中生效；但项目没有完成业务收尾，研究投入还存在重复，不能据此宣布平台整体发现能力达标。** 判断对象分别是目标证据、研究质量、工程接受和使用闭环，不以 confirmed 数量、候选有效或 Run 成功率代替它们。

从第一性原理看，平台应把不确定的研究建议转化为可验证、可积累的事实，并在真实条件满足后合理结束。模型过早建议完成是可以发生的局部判断错误；Server 拒绝尚有开放工作的完成请求是必要保障。**拒绝旧建议不应永久剥夺平台在新事实到来后重新决策的能力。** 本轮推荐修正这个职责冲突，而不是放松完成检查、禁止并行研究或继续加 Prompt／重试次数。收益是保住已经取得的成果并正常收尾，不声称这一修复本身增强未知漏洞发现能力。

### 已确认运行与原件证据

本次只读查证对应实际入口 `8001`：Server PID `2919781`、Dispatcher PID `2919944`，cwd 均为本仓库，实际 PostgreSQL 为 `127.0.0.1:55433/cairn_isolated`、migration v47，不是 `55432` 的另一实例。项目容器实际 image ID 为上轮最终 `sha256:be208a94654d80a82637aec22c11b02f464204a476274f3195236a5991345f88`。上述身份只对应此次观察，实施前须重新核对。数据库查询使用显式只读事务并回滚，不导入应用初始化，不调用会推进业务的项目 GET，不访问目标或执行保存的脚本。

- **项目与预算。** 精确项目 `proj_036`，Goal 为“拿到flag”；2026-10-10 02:59:44 UTC 创建，22 个 Run 为15成功、7失败。11个逻辑 Attempt 包含7 discovery和4 verifier，预算为11/100，不是11再加4；assessment实际执行15次包含重试，另有bootstrap/reason。7个discovery产生4个finding、3个tested_no_finding，4个verifier均记录confirmed。14个Fact含origin/goal，12个Intent均已completed。最后Run于04:33:48结束，05:39–05:47的只读快照项目为paused、generation 2、无在途任务；容器05:34:14.541退出，归属记录05:34:15变为stopped。暂停操作者与精确触发原因未查明，不能称为自动暂停或用户主动暂停。
- **目标不只是模型自报。** 按Artifact URI读取30个不同的关键原件，实际大小和SHA256均与登记一致；其中24个主核查原件还与固定内容登记元数据相符。`tr015/016/018/019/020/021` 的10个正例原件得到同一个CTF token，10个相应基线／反证原件不含它。`tr019/a123` 触发前为0 bytes，`a124` 触发后为200 bytes，`a125`原响应中的Base64与解码结果一致；`tr020/a141,a142` 两条回读通道逐字节一致。`tr021/a164` 触发前为0 bytes，`a161` 触发后为124 bytes，包含执行身份与本次唯一标记；`a162` 的HTTP 200接收数据区含同一token，发送区不含。`a163/a167/a151` 保存的触发请求／命令／脚本不含该token；去掉相关溢出过滤器的对照请求 `a171` 返回HTTP 200，但对照输出文件 `a169` 返回404且无token。这里只保留比较结论，不收录目标地址、flag值、请求或脚本正文。未重新访问目标、提交靶场判分，也未对全部材料作独立实质审计。
- **上轮B/C存在真实正证。** `tr019` 的18项、`tr021` 的17项身份集合已捕获、持久化且Run成功，超过旧16项限制；不是只看配置32。`tr020` 首候选finding的vulnerability缺identity，本地以`submission_invalid`、`/data/fact/vulnerability`、`anyOf`拒绝；同Run第二候选以首候选为parent、补齐identity后成功接受为`f011`。本次未再出现已记录的`artifact_changed`、候选身份超限或`candidate_integrity`终态422；没有触发某失败分支不等于该分支已经得到新的运行验收。固定副本的源码与上述原件相符，但没有把“本项目未报变化错误”当作每种并发写入都已覆盖。
- **外部失败与交付失败分开。** 7个失败Run中`tr001=provider_auth`，`tr007/009/011/013/017=provider_error`，`tr022=business_acceptance`。前6条之后均有相应后续成功运行，旧失败记录未变成功。provider底层原因及可靠货币费用未核实，不将预算次数当金额；不把这些错误与最后完成拒绝混为一因。
- **实际研究存在积累，也有重复。** 已有`f003/f004/f005 → i006 → f007 → i010 → f010`和`f006 → i008 → f011 → i012 → f012`等链，研究从文件读取深入已知CNEXT问题并取得执行与Goal证据，复核有实质对照，不只是扫描计数。Hints为0，未见初始输入之外人工创建的研究图节点，但不能据此断言完全无人操作。材料中使用了公开writeup／已知漏洞路线，证明的是本次自主利用与验证能力，不是原创或通用未知漏洞发现能力。
- **4条confirmed不等于4个独立根因。** `v001/v003` 为同资产同路由的文件读取，identity的类别语言、parameter及条件描述不同；`v002/v004` 为同资产、同路由、同parameter、同RCE类别，主要差异在object_type/source_role/state/variant。尤其`i008`于03:53:39创建，原意就是依赖`i006`的独立复现／复核，却是discovery，后来再次触发系统verifier；`i006`也已触发自己的verifier。这些后续证据并非全无价值，但不能将它们计作四条独立新发现。当前指纹会哈希上述全部身份字段（`vulnerability_identity.py:31–82`），相同指纹才归入同一漏洞（`vulnerabilities.py:792–848`）；每个新Observation又有独立复核调度（`vulnerability_verifications.py:169–239`）。这是下一轮可另行收敛的研究效率问题，本轮不擅自合并历史记录或取消独立验证。
- **报告不是本次首因。** 175个Artifact中140 evidence、35 deliverable；正式报告生成／接受记录为0。报告是显式请求行为，不能把普通deliverable称为正式报告，也不能仅据没有定稿记录认定漏调度。本轮不修改报告生命周期。

### 完成拒绝与实施前源码断点

1. **过早建议，正确拒绝。** `tr022`于04:21:53开始，其冻结`execution_delivery.inputs.open_intent_ids=['i012']`，binding已有`decision_input_digest`；并不是输入未告知仍有开放工作。04:23:06的`agent_events/ev1436`显示本地有效的complete，`ev1437`写回HTTP409、细码`goal_driven_completion_open_intents`，`ev1438`记录写入失败。最后复核`i012/tr021`当时仍在运行，04:33:46才接受结论、04:33:48才终结。证据位置为本项目事件及`runtime/logs/dispatcher.log:218–223`。`project_completion.py:82–168`的最新输入和静止检查应保留；Reason Prompt第8行已经说明无未决工作才建议完成，继续加长提示词不能承担运行保障。
2. **不能误判候选恢复卡死。** 日志04:23:09和04:33:47确有`candidate recovery is unresolved`，但`tr022`在04:23:16、`tr021`在04:33:54已经持久化`stopped/recorded_terminal_confirmed`、physical proof v2、spool recovered及termination/cleanup confirmed。当前22/22 Run恢复均为stopped。旧日志只证明当时暂缓，不能证明后续持续卡死；不修改恢复机制来“解卡”。
3. **已确认的静态矛盾在决策轮次与失败重试的归属。** `write_outcomes.py:36–47,79–94`没有将该409识别为可重新决策的业务条件变化，`reason_writes.py:310–354`返回`business_acceptance`。`assessment_attempts.py:482–543`只在Reason succeeded之后切断同代失败链，按generation/type/intent/phase取链，不看新Fact或决策输入摘要；`:690–711,735–796`使`business_acceptance`和`stale_business_state`都成为`execution_retry_blocked`。`:415–439`将其传给Dispatcher拒绝fresh；`scheduler/task_lifecycle.py:17–49,77–88`消费该路由，`project_reason.py:53–61`的claim和`audit_task_runs.py:214–216`的admission还会再次检查。**只换错误分类或只在Dispatcher忽略拒绝都不解决。**
4. **并行规划不是应删除的根因。** `dispatch_cycle.py:393–457`允许仍有Explore运行时依据新事实开展Reason，`:576–622`在新Fact或开放Intent归零后触发新Reason；这符合及时调节研究的目标。但相同generation/phase内，旧`tr022`失败仍会让Server拒绝该新轮次，即使`f012`已接受。这里应区分“重放旧complete”与“依据新状态作新决定”，不能靠全局等待或成功状态伪装修复。
5. **运行因果边界。** 实际没有后续Reason／项目完成记录；上述拒绝路径由当前源码与该Run状态确定，但没有取得04:33–05:34期间完整调度历史。05:47:16的`dispatcher_status`已为idle、active=0、`project_decisions=[]`，不能冒称查到了历史`execution_retry_blocked`心跳，也不能把整段停顿或后来的暂停归为单一原因。

### 本轮实施范围：新输入允许新决策，不重放旧建议

推荐只修复**goal-driven完成建议因业务条件未满足而被拒后，已有工作正常结算仍不能再次决策**这一完整路径。不是开放全部Reason失败自动重试，也不是重做研究引擎。

1. **让受控拒绝原因可由权威准入消费。** 沿现有完成响应与TaskRun结果持久化路径保留明确的操作／拒绝细码，与已有冻结`decision_input_digest`关联；优先复用现有结果字段和运行上下文，不新建恢复表、决策台账、影子状态或独立错误白名单。新增必要的受控结果信息仅为区分“完成建议业务条件暂不满足”与契约错误、越权、收据冲突等终局错误，不让模型自报决定准入。当前`tr022`没有`worker_failure`字段；`decision_result`审计虽有safe code，但`protocol_records.py:130–140`是写失败仅告警的best-effort路径，不能扫描它或解析日志／候选自然语言作正确性依据。结果信息的持久化必须真实可依赖，不能写入后又随409事务回滚而宣称已经保存。
2. **以现有权威业务输入划分后续轮次。** 在Server当前Reason链与统一准入处实现窄边界：只对明确记录的goal-driven完成动态拒绝，旧Run已终态、无已接受结果／未决写入，且物理终止与清理已确认时，比较当前权威`goal_driven_decision_input_digest`与原冻结输入。有效业务输入变化后，后续是新fresh Reason；同输入仍不自动反复调用模型。业务摘要应复用`assessment_contracts.py:15–129`，排除心跳、租约、日志和仅时间变化；权威输入读取不能调用带副作用的项目GET或复制另一套摘要语义。至少覆盖本次`open_intents`拒绝，相关输入过时情形按同一窄语义处理，不把所有409或所有`goal_driven_completion_*`无差别放行。
3. **贯通route、claim、admission及执行上下文。** `assessment_attempts.py`中路由投影、当前链选择、`execution_retry_eligibility`和新Run准入共用同一边界；claim／admission在各自事务里仍检查当前授权、generation、lease、在途执行与已有接受结果。新轮次不继承旧失败链的deadline/session/补交来源，后续真正的provider或交付失败仍沿自己的有限链处理。Dispatcher正常消费fresh路由，不另造重试器。保留原失败Run、候选、回执和费用；不能改旧Run为success、清空旧期限、提高generation或删除候选来获得fresh。
4. **新建议仍需真实完成接受。** `i012`完成并不自动等于Goal完成；新Reason使用含`f012`的最新输入作决定，Server照常检查开放工作、在途Run、Attempt、candidate及静止状态后接受。不重放tr022的旧complete，不重做Explore，不恢复旧session；新的Reason如调用模型仍有新推理成本，不宣称零费用。重复tick／Dispatcher重启不能重复创建同一决策或绕过lease与幂等接受。

**明确不做：** 上调timeout／repair／retry额度；把business_acceptance或stale_business_state整体加入可重试白名单；任意Fact变化清空所有失败链；禁止Reason与Explore并行；自动abandon仍有价值的复核；放宽完成／证据／身份校验；provider／模型／SDK改造；去重与研究角色重构；自动生成报告；全局规范和UI改版。额外静态可达问题不自动加入本轮。

### 最直接验证与交付边界

分析会话未执行下列验证。实施会话在现有隔离Server／HTTP调度fixture内扩展一条定向序列，使用合成材料，不访问目标、不调用付费模型，不让用户新建`proj_037`替确定性协议查错：

- 复用`test_goal_driven_completion.py:120–194`的门禁、`test_delivery_retry.py:125–158`的同代新Reason、`test_dispatcher_retry_wire.py:15–78`的实际HTTP与重建Dispatcher接缝，优先串成一个必要用例，而非把所有文件整套运行。序列为：开放且运行的verifier → complete正确409 → 旧Run失败、拒绝原因和候选保留 → 同输入重复tick／重启不创建新Run → verifier接受Fact并完成必要终态／cleanup → route/claim/admission一致允许fresh Reason → 最新输入完成并取得真实ACK。Fact出现但其他执行未清理不能被当作已静止；已有生命周期接缝见`test_goal_driven_lifecycle.py:1371–1433`。
- 核对新Reason未复用旧session／旧交付输入，旧失败与候选未被重写，Explore次数、attempt charged和历史预算不增加。补最少的针对性拒绝断言：输入未变、非本次动态拒绝、已有接受结果或未决交付不能被新规则放行；新Fact不能清空provider／delivery失败链。不能只断言错误字符串变成stale或Schema多了字段。
- 构建启动前重新核对实际实例和在途工作。只对必要宿主代码生效完整部署；未改容器内代码不重建Worker镜像。依现有方式交付Server/UI/Dispatcher及所需依赖，但不自动恢复／重跑项目、启用Worker、访问目标或制造模型费用；启停会中断工作或触发派发时先说明并取得许可。
- `proj_036`现有成果应保留。它当前paused，旧tr022也没有新受控拒绝信息；不能声称代码修复会自动将其completed或补写历史元数据。实施后只读说明当前正常恢复路径及授权／Worker条件，由用户决定是否恢复；不要求手改数据库，不自动迁移历史失败，不以新建项目作为唯一出路。
- 更新本节实施记录，分别写明实现、本地验证、部署生效与真实任务验收。保护现有未提交修改，不commit、不push。完成本轮即停止；研究重复和更广发现能力仍属后续评估，不自动开展。

此前只读分析声明：分支`docs/installation-guide`，当时仅增量更新本方案；未修改业务代码、测试、配置或业务数据，未运行测试、构建、启停、项目动作或目标请求，未创建提交。原件只读核对支持本次Goal；整体发现能力、费用和暂停归因仍未验收。后续实施与部署结果如下，不将分析时快照当作当前进程身份。

### 实施记录（2026-10-10）

**代码实现。** 本轮仅增量修改`project_completion.py`、`assessment_attempts.py`、`audit_task_runs.py`、`test_delivery_retry.py`及本节，保护已有未提交改动；无新依赖、表或migration，无Dispatcher／Worker／模型／Prompt／UI改动。

- `project_completion.py`仅在原有`input_stale`、`open_intents`动态完成门禁拒绝时，向原TaskRun写入Server-owned的`completion_rejection={code, decision_input_digest}`，关联原冻结binding。HTTP complete自有事务在抛出409前可靠提交该拒绝，仍不接受完成业务；复用调用方连接的`with_conn`路径不暗中提交其其他业务写入。其他409及契约、授权、收据冲突不生成此标记，不依赖模型自报、自然语言、日志或best-effort审计。
- `assessment_attempts.py`共用窄资格判定：当前Reason链以明确受控完成拒绝终结，无已接受结果、Fact或未结算候选，无原会话续交付／continuation；相关终态执行及已产Fact的执行须有本Run代次的termination／cleanup确认。未产Fact且仍运行的Explore不因此被全局禁止与Reason并行。
- 当前业务输入直接在调用方事务内读取，复用`goal_driven_decision_input_digest`，不调用带租约清理副作用的项目GET。摘要相同时继续阻断；业务输入变化且必要清理完成时，route、claim、admission共用同一规则允许fresh。新Run准入持久化Server-owned的`reason_decision_boundary={source_task_run_id, decision_input_digest}`，使新失败仅属于新链；即使新Run不慎消费旧快照，也不能对已经准入的同一权威输入循环创建Reason。
- `audit_task_runs.py`禁止客户端创建／覆盖上述两个权威字段，并在项目写锁内阻止重复Reason在途准入。fresh不继承旧session、冻结输入、deadline或补交来源，旧session不得重用；普通provider／delivery失败仍受原有有限链约束，没有增加重试额度、改旧Run成功、提高generation或删除候选。新Reason的complete仍经过全部原Server门禁并取得真实ACK。

**本地定向验证。** 使用现有隔离SQLite／真实HTTP fixture与生产Dispatcher调度接缝，4个既有完成门禁、7个新增节点及2个相邻交付回归共**13个唯一节点通过**，未跑全套、lint或浏览器矩阵。

- 主线串通运行中的verifier → complete返回409且拒绝记录可在HTTP回滚后读到 → 原Reason失败、候选原文及结果保留 → 同输入重复tick和重建Dispatcher不新增Run → verifier通过真实conclude接受Fact → Fact生产者仍运行、终态未cleanup、旧Reason未cleanup分别阻断 → 双确认后fresh调度 → 当前事实图／export进入生产`build_reason_prompt_context`、`register_delivery`、`PiDriver.build_execute` → 新session、非resume／非conclude_only请求 → 最新complete得到200，exact replay保持同一Intent身份／来源和记录计数。
- 核对旧失败、候选bytes、Explore Run、Attempt及charged／execution_count、历史预算、授权和generation不被重写或增加。普通终局错误、provider／delivery链、已有接受结果、未结算候选不能因新Fact放行；owned字段伪造、旧session和在途重复admission被拒。另验证`with_conn`拒绝不提交调用方pending写入，以及fresh边界上的旧快照`input_stale`拒绝不会再次消费同一权威输入。
- 相邻回归为`test_http_admission_derives_same_session_delivery_from_persisted_failure`及`test_http_delivery_transient_failure_keeps_original_chain[provider_error]`。另一个既有`test_dispatcher_retry_wire.py::test_released_reason_cannot_restart_before_previous_finish_is_persisted`**失败**：历史fixture在初始claim即返回409／`assessment_protocol_historical`，未进入本轮边界；未放松协议或顺手修改该fixture，不称整体全绿。
- 运行venv缺pytest，系统pytest单独使用又缺docker；最终复用系统pytest及已有venv包（`PYTHONPATH=cairn/src:cairn/.venv/lib/python3.13/site-packages python3 -m pytest ...`），未安装依赖。fixture为共享内存SQLite连接、合成容器元数据与lifecycle ACK；接受回执负例用历史DB种子。未运行真实runner／容器／模型，也未验收独立连接并发或真实PostgreSQL写入序列。首次complete的`conclusion=None`与replay的`conclusion='completed'`展示差异仍存在；同一Intent和幂等记录已验证，本轮未改该展示差异。

**完整部署已生效。** `npm --prefix cairn/frontend run build:server`成功，包含必要`vue-tsc`与Vite发布构建；仅有构建性能／大chunk提示。随后以现有`deploy/stop.sh`、`deploy/start.sh --host 0.0.0.0 --port 8001`交付Server、静态UI、Dispatcher和已有PostgreSQL依赖，没有重建Worker镜像。

- 启停前核对PID／starttime／cwd和实际DSN；用`REPEATABLE READ READ ONLY`事务查询并ROLLBACK，确认active项目、queued／running Run、待处理报告均为0，两个实际outbox仅有锁文件，没有在途研究被此次重载中断。宿主Worker列表为空；数据库原有3个enabled Worker未修改，不以该记录推定有运行中的Worker。
- 部署后Server PID为`3448019`、Dispatcher PID为`3448152`，cwd仍为本仓库；DSN fingerprint不变，仍是`127.0.0.1:55433/cairn_isolated`。真实socket访问`/health`、`/`和入口引用的9项静态资源均200，入口及资源bytes与本次发布产物一致；未带凭据的受影响task-runs路由返回401，鉴权仍在。Dispatcher心跳为idle、running_task_count=0，支持`project_pause_v1`／`project_runtime_v1`，两个DB容器healthy，无新Worker容器，启动日志无Traceback。此烟测证明部署可用，不冒充业务接受或真实任务验收。
- Worker镜像保持`sha256:be208a94654d80a82637aec22c11b02f464204a476274f3195236a5991345f88`。未恢复／重跑项目、启用Worker、手改业务数据库或回填历史拒绝。`proj_036`仍paused／generation 2、14 Fact、12 Intent、22 Run、11 Attempt；预算计数仍11/100（含4 verifier），22/22 Run清理确认，`tr022`仍failed／business_acceptance／generation 1，没有新拒绝或决策边界元数据。

**真实任务验收与正常恢复边界。** 本轮未访问目标、执行保存脚本、调用付费模型或创建真实项目；`proj_036`未completed，整体发现能力、真实新Reason语义判断、费用和暂停原因仍不作验收声明。

部署后只读调用现有权威生命周期／授权／准入投影，当前`resume.allowed=True`、授权无阻断、当前generation Reason准入允许。原暂停已形成generation 2，旧`tr022`在generation 1；**正常resume不新增generation，也不迁移旧失败**。如用户决定继续，应由有该项目访问权限的浏览器用户／超级管理员执行正常“恢复”动作（`POST /projects/proj_036/resume`，遵循现有session／CSRF），无需新建项目或手改DB。届时仍须授权范围有效、物理清理完成、Dispatcher就绪；实际推理还取决于已绑定Worker和模型可用性，当前准入允许不证明模型就绪。新Reason应读现有最新Fact再决定，不能重放旧complete或保证自动完成，**会有潜在的新推理费用**；是否恢复由用户决定，本会话不执行。

分支仍为`docs/installation-guide`，未commit、未push。以上层次分别是代码实现、本地确定性验证、部署烟测和未开展的真实任务验收；本轮到此结束，不接续研究去重、provider、报告或其他历史待办。

## proj_035：已有研究材料的可靠交付（代码与定向接受验证已实施；部署状态见本节记录）

本次评价区分“本项目 Goal 是否有证据支撑”“上一轮修正是否实际生效”和“平台是否已具备稳定的自主发现能力”。推荐实施仍仅优化已有研究材料的交付与失败结算，不展开研究方向、自主选题或 Hint 影响分析，也不将这些列为本轮验收前置条件。

### 目标与第一性原理取舍

本轮服务于用户的自主漏洞研究目标：已经完成的实验应能真实交付、被后续研究利用，不因内部登记、候选捕获或终态协议问题被迫重新付费探索。不是消灭所有错误，也不是让所有任务都变绿。

必要保障是材料内容及引用一致、执行归属真实、有限资源和准确结算。现有登记集合、捕获时机、固定数量与错误分类都是实现选择，不因已写入代码或旧方案就不可调整。尤其不要把上一轮为控制范围而保留的“全部登记项一并校验／提交”，升级为永久产品标准；调整仍需讲清实际用途、材料影响并贯通消费者，不靠删材料、静默换引用或伪造成功绕过问题。

### proj_035 实际证据与未知

以下运行发生于 2026-10-09，时间均为 UTC；只读分析没有恢复项目或发起额外模型／目标请求。

- 项目 09:16:14 创建，11:24:09 接受末轮 Reason `tr035` 的完成决定，引用 `f014/f015`，11:24:14 Run 成功。35 个 Run 中 21 成功、14 失败；18 个逻辑 Attempt 包含 14 discovery 和 4 verifier，不是 18 再加 4。预算计数 18/40，不是预算耗尽停止。
- 15 个产出 Fact 包含 1 bootstrap、10 experiment（4 finding、6 tested_no_finding）及 4 verifier；4 个漏洞记录均 confirmed，复核均使用不同 Run/session。存在 `f009/f012/f013 → i017 → f014 → i019 → f015 → goal` 的事实与复核链。**本次回查确认 Goal 原文为“拿到flag”，并核对了关键响应原件，详见下节；此前“未核验目标及原件”的范围限制已被这次定向核查推进，但不是全部四条漏洞的独立实质审计。**
- **`i010/tr017` 的修正进入，但同一日志再次变化。** 10:22:55.583 首次提交被拒；随后成功读取一次、重新登记一次；10:23:39.863 第二次仍拒绝；此后没有再次读取／登记，10:23:58.876 第三次仍拒绝。三候选均为 `artifact_changed`，各有 14 个登记项。原 session 的第 77、85、89 行工具回执表明：首个旧 handle 与后两次新 handle 对应**同一 path／kind=log**，不是另一登记项依次暴露；第 75、83 行登记时大小从 629 变为 737 bytes，回查原工作文件为 1048 bytes。第 46 行已有向该日志输出的后台 Shell 启动记录；写入者的完整进程生命周期未复核。最终 3 次提交、2 轮修正耗尽，无接受 Fact。此证据更正此前“变化项未知”，不能再归因为模型完全没有修正或只差一次重试。
- **`tr018/tr034` 的实际超限维度已定位为 Artifact 身份数。** 各 1 次提交、0 轮修正、0 入库候选。固定诊断均为 `Candidate artifact identity count exceeds the limit`，见 `runtime/data/artifacts/projects/proj_035/runs/tr018/a094-explore_execute-pi-events.jsonl:111` 与 `tr034/a206-explore_execute-pi-events.jsonl:109`。两个 session 首次提交前分别已有 20、17 个不同 path／kind 的显式成功登记，尚未计入 Harness 自动登记；原候选参数约 11–12 KiB，不是证据正文把 spool 撑满。源码先捕获全部登记身份与原候选，捕获成功才进入提交校验；捕获失败阻断修正。注册器允许的正常材料集合与捕获端 16 项上限确实不协调，已不再只是可达假设。0 入库候选不等于工作区/session 全部丢失。
- **这两个 Run 的失败终态又被 422 拒绝。** `runtime/logs/dispatcher.log:185、360` 记录 finish rejected/response_status=422；Dispatcher 的 `STANDARD_ERROR_TYPES` 包含并发送 `candidate_integrity`，Server 的 `TaskRunErrorType` 不接受它。最终数据库为 failed/delivery_unavailable。这里的二次 `business_acceptance` 分类是终态请求拒绝，不是又一次研究结论不成立。
- **`i015/tr029` 的具体拒绝已查明。** `tr028` 三次 `submission_invalid` 分别为两次 `strict_json` 和一次 `/data/fact/vulnerability` 的 `additionalProperties`；末次候选含 `identity`，但还含当前 Finding Schema 不接受的 `evidence_refs`。补交 `tr029` 从两次 `artifact_unknown` 修正到 `locally_validated`，其三候选均不再含 `identity`。TaskRun 的 `worker_failure.message` 明确为 `Worker 结果未通过阶段业务验收：vulnerability identity is required`；这是 Dispatcher 语义拒绝，不是 Server 已接受 Fact 后又撤销。当前 `worker_protocol/submission.py:75–85` 将 Finding.identity 定为可选，`contracts.py:1483–1488` 却在该协议强制要求，确定性规则没有进入可修正的本地交付边界。`tr028/tr029` 原候选已含与后续核验一致的 flag 文本（比较时仅忽略响应末尾换行，不输出值），但没有形成已接受 Fact；不能把材料存在写成业务已接受，也不能说全部成果已丢失。
- 有 report/deliverable Artifact，但没有最终报告定稿记录。**当前报告是显式请求行为**：`assessment_reports.py:563–577` 的 `request_assessment_report_generation()` 冻结模板并建立请求，`project_completion.py:410–447` 的完成事务不自动请求报告。没有定稿记录本身不能认定漏调度；不能把普通 report Artifact 当最终报告。报告生命周期不进入本轮修改。

### 本次回查：是否符合预期（2026-10-09；只读证据评估）

**结论：本项目狭义 Goal 有实质证据支持；上一轮局部修正已在新镜像运行中生效，但可靠交付仍不达标，整体自主发现能力不能凭本项目一次通过验收。** 三者不应合并成“完全成功”或“上一轮全部无效”。

- 实际入口为 `8001`，Server／Dispatcher 的 cwd 均为本仓库，活动数据库为 `127.0.0.1:55433/cairn_isolated`，不是另一套 `55432`。以独立连接执行 `REPEATABLE READ READ ONLY`，确认只读后按精确 `proj_035` 查询，结束 `ROLLBACK`；未导入应用、调用会推进业务的接口或访问目标。`tr017/tr029/tr031/tr033` 运行上下文均记录上一轮新 image ID `sha256:23f0af3c7bb6da9a931d26738d71a40a7434b0ae42c3d74a248c85d38ef7efdf`，且 tr017 的原回执实际包含新版修正反馈及两轮修正，不能用“可能仍是旧镜像”代替解释。
- 定向核对 `tr031` 的 `a179/a184–a187` 与 `tr033` 的 `a194/a196/a197/a199/a200/a203/a205`，共 12 个关键原件，大小与 SHA256 均符合数据库登记。**不是只读取模型 summary**：两次运行的原始响应相对不存在文件对照，直接读取分支多出相同的 46-byte 内容，Base64 分支多出 64 bytes，解码等于直接读取结果；两个 Run 的所得内容一致，符合 CTF token 形态，与各自结果记录一致。该内容不在基线、不存在文件对照或保存的请求 URL 中；正对照也有对应文件内容。这里只输出比较结论，不输出目标地址、路径细节或 flag 值。
- 因而“拿到flag”不是单凭 `completed`／`confirmed` 自报，有发现、独立 session 复核及响应对照支持；未重新访问目标、提交靶场判分或审计全部发现。Server 的完成检查主要保证新鲜输入和真实静止，见 `project_completion.py:82–168`；漏洞确认主要保证 Run/session/证据归属并接受复核结果，见 `vulnerability_verifications.py:319–423`，两者都不自动证明模型的语义结论。这次肯定 Goal 依据的是上述内容核对，而不是这些状态名。
- 从第一性原理看，值得保留的行为是：事实被后续 Intent 引用、有反证／无发现结果、有发现后复核、局部失败后仍能继续达到目标；完成不需耗尽 40 次预算，也不需所有探索方向成功。不要为消除失败状态而无限延长项目，或强制做完整测试矩阵。
- 仍不符合可靠研究预期的是：14 个 discovery 中 4 个以交付错误终结，没有接受 Fact；其中已有目标相关材料。35 个 Run 的 14 次失败中 9 次为 `provider_error`，另外 5 次为上述交付／契约失败；不同原因不能混并，9 次 provider 的底层原因未在本轮重查。`h001` 是中途外部 Hint，影响未评估，不能宣称这是无人工介入的能力证明。4 条 confirmed 记录也不等于 4 个已审计的独立根因，更不等于通用或未知漏洞发现能力已经达标。
- 首要改进不是增加 Agent、阶段、Prompt 长度或重试次数，而是让**有价值的现存研究内容稳定绑定到证据版本，按一致的有限容量经过同一套接受规则**。本轮收益限定为研究成果保全与可信交付；不据此声称整体发现能力已提升。

### 实施顺序与范围

**A. 先消除已确认的终态协议矛盾。** 核对 `dispatcher/scheduler/common.py`、`task_run_finish.py` 与 `server/schemas/common.py`、`tasks.py`，使真实候选失败能被 Server 正确接受和结算，保留原始细码及错误归属。优先对齐已有权威定义，不再增加另一份错误白名单，不把它随意降级为 unknown。修复终态只能解决准确落账，不能宣称已恢复研究成果。

**B. 将材料登记变为可交付的内容版本，并统一捕获容量。** 本次已定位变化项和超限分支，不再把“先调查是哪一种”留作实施的主要交付。开始前定向核对当前代码／回执是否有新变化即可；数据库仍只读，不重跑研究补证据。

1. **优先在现有 ArtifactRegistry 的登记边界固定所登记的实际 bytes。** `artifacts.ts:63–101` 当前把可继续写的工作文件路径加登记时摘要当作交付物，之后逐项要求源文件仍完全相同。tr017 已证明同一路径重新登记也可能继续增长，要求模型不断追赶摘要不是可靠方案。复用现有受管 Artifact 存储，登记返回的 handle、path、size、hash 应指向同一次捕获的内容版本，Dispatcher 实际收集相同 bytes；后续工作文件变化不能改变旧 handle 的内容。保留来源关联与原文件，明确这是登记时观察到的内容，不假装后台活动或整份日志已完成。登记过程要有界，不能追随无限增长的文件；摘要来自实际保存的 bytes，不分两遍分别读取源文件制造新的竞态。重新登记产生新版本，不能静默更新旧身份；交付副本被改动、缺失或引用错误仍应拒绝。此为局部材料固定，不新增快照服务、版本数据库、状态机或清理旧证据的后台。
2. **消除“允许登记，首次捕获才因第 17 项致命失败”的不一致。** 不能只把 Node 的 16 改大：同时核对 `candidate-spool.ts:300–307,378–434`、`protocol.ts`、Python `workers/base.py`、`tasks/candidate_intake.py:142–144` 和 `server/schemas/candidates.py:154–172` 的数量／manifest／总字节限制。用一套可解释的有界容量贯通登记、候选帧、传输和持久化，允许本次正常的 17／20 项材料及 Harness 自动登记通过；不要漏算每个身份占用的 manifest 字节。保留必要原文、manifest 和总存储上限，容量不足应在可理解、可修正的边界反馈，不是研究无效，也不以截断身份／静默丢文件换成功。具体上限依实际编码预算确定，不凭任意大数永久兜底；不增加第二套影子限额或所有业务通用配额平台。
3. **先用上述直接路径解决已证问题，不额外重构交付集合。** 保留合理的现有登记／交付行为不是将“全部登记项”升为永久标准；若当前实现允许直接固定内容并贯通现有集合，就不同时新增让模型维护另一份文件选择清单。仅在必要消费者确有冲突时调整集合语义，并说明实际引用与历史材料如何保留。不得靠删掉正在变化或超限的登记项回避本次事实。

不通过加修正轮次、延长 deadline、换模型、删登记项、静默刷新旧摘要或无限扩大额度掩盖问题。修正仍基于既有材料，不重做目标实验；不扩大 Shell／目标工具权限，不改后台进程生命周期来伪造静止，历史失败不自动重开，旧费用和期限不清零。

**C. 将已证的 finding／identity 规则前移到同一可修正提交边界。** `tr029` 的首因已明确，属于本轮必要交付上下游。沿当前协议的权威业务模型／Schema 构造处表达 finding 必须具备合法 vulnerability identity 的现有要求，并让生产 Runner 使用它；Dispatcher／Server 继续负责真实接受，避免本地一套可选字段、下游另一套强制字段。复用已有条件 Schema／验证机制，不再手写另一份仅此用例专属验收器，不根据标题、目标字符串猜造 identity；不能靠取消 identity 要求、接受空壳或降级 finding 放行。保留 `tr028` 的正确额外字段拒绝，把字段问题定位在本地修正，不能要求模型靠更多重试碰运气。仅贯通本次 finding/identity 必需规则及其消费者，不顺带审计、放松或重写所有阶段 Schema。

**不扩大范围。** provider、报告/reopen、研究角色、前端重构、SDK 升级和全局规范不在本轮。A、B、C 共同服务“已有材料可靠交付”；只完成 A 时必须明确只是准确落账，不能宣布本轮目标全部完成。原始材料不可用或当前源码改变时记录具体差异，不用旧文档替代事实。

### 直接验证、部署与交付

- 对 A 选择一个现有接缝，把 Dispatcher 实际生成的候选失败终态请求送到真实 Server 校验／更新路径，验证接受、错误归属和结算；仅断言两个字符串集合相等不够。
- 对 B 复用现有真实 SDK＋回环假 provider 及业务接受 fixture，使用非敏感合成材料直接覆盖：登记后工作源继续增长，交付仍对应登记时固定的原 bytes；交付副本若被修改仍拒绝；20 项显式材料加 Harness 自动登记可经过真实捕获、解码和 Server 持久化／接受。固定内容的新语义若替换旧行为，应更新相应旧断言，不为保住“源文件变化必失败”的测试扭曲设计。保留实际资源上限的必要拒绝断言，不扩成边界矩阵。
- 对 C 用生产生成的 Schema 与既有同 session 修正机制：finding 漏 identity 在本地被定位，基于合成既有证据补齐真实所需字段后，由同一生产 Dispatcher 语义校验和隔离 Server 接受；不重做目标活动、不仅断言 Schema 含 required，也不 mock 掉要验证的业务规则。可与 B 的既有接缝复用材料，不默认再全跑历史测试。
- 候选捕获验证要到本次改动影响的持久化接缝，保持请求、冻结 binding/schema 和 Server 状态一致；别把只解码 spool 说成候选已被 Server 保存。优先扩展现有 fixture，不为追求“完整覆盖”新建验收平台。
- 不依赖真实业务数据库写入、收费模型或被测目标来验证确定性协议。已有材料不足就报告不足，不默认新增付费项目；不新增依赖。核对修正成功、不重复目标活动、失败准确落账到了哪一层。
- 按实际改动构建和部署受影响组件。容器内改动需更新 Worker 镜像，不能把旧容器标签相同当作已升级；宿主代码改动需确认实际服务加载。未改且兼容的前端可复用。启停前核对当前实例、在途工作与自动派发影响，涉及中断或费用须取得相应许可；不自动暂停/恢复/重跑项目，不删旧容器/session/证据，不擅自改系统资源。
- 开始及编辑前核对工作树和分支，保护现有未提交改动；不因新会话重复实现上一轮修复。在本节追加实际原因、取舍、代码实现、本地验证、部署生效和真实任务验收范围。未做的层次明确写未做；报告分支、文件、测试结果、提交情况及风险，不 commit、不 push。完成本轮后停止，不自动展开其他历史待办。

前述为本轮分析交接时的事实与范围，实际实施状态以下面的记录为准，不改写历史失败或将旧行号当当前源码。

### 实施记录（2026-10-09）

#### 代码实现与必要消费者对齐

- **A：准确失败结算。** `TaskRunErrorType` 补齐 `candidate_integrity`，Dispatcher 的标准错误分类直接引用由该权威 Literal 派生的集合，删除独立同义白名单。一起对齐原发送端已有的五个 `readiness_*` 与 `container_runtime_mismatch` 分类，避免集合统一后把既有合法错误降为 `unknown`；未修改 readiness/provider 的执行逻辑或重试政策。候选细码、origin、phase、message、exit_code 继续保存于原失败记录。
- **B：登记固定内容，不追赶源文件。** `ArtifactRegistry.register()` 通过打开源文件时已有的长度、有界分块读取及同一次写入/摘要，保存至现有 `.cairn/artifacts/registered`。成功回执的 `handle/path/sha256/size_bytes` 指向这个内容副本，`source_path` 保留来源，原文件不删除；重新登记创建新版本，不替换旧身份。提前 EOF 或超过预算不登记部分内容。源后续增长不改变旧版本；副本篡改、缺失与错误引用继续拒绝。副本默认只读，但其所有者仍可修改权限，真实性依赖实际摘要核对，不能将文件权限冒充完整性证明。
- 容量贯通为每集合/候选 **32 个内容版本（含 Harness 自动输出）、32 MiB 登记内容合计、16 KiB manifest**。32 个最长合法 handle 与最长执行身份的 canonical 编码验证在 manifest 预算内。原候选 raw **2 MiB**、每 Run **16 候选**、每候选 **64 outcomes**、spool/Server 持久化合计 **34 MiB** 保持有界；不截断身份，不删除材料，也不加通用配额平台。登记工具在容量不足时明确拒绝，已有成功版本保留。
- Dispatcher 声明结构复用权威 `ArtifactDeclaration`，携带内容身份和来源；收集容器文件后核对实际 size/SHA，seek 后上传同一临时文件。Server 对 Worker 声明的 JSON/base64 与 raw 流式上传都再次核对实际 bytes，拒绝 `registered_content_version_mismatch`。Artifact 元数据保留注册 handle、path、SHA、大小和原始来源。
- **实际持久化暴露的旧约束已纳入本轮。** 首次联合接受验证发现 SQLite 仍限制 `artifact_count <= 16`，candidate/outcome manifest 仍为 4 KiB；不是捕获已通过就等于保存成功。新增双后端 v47 容量迁移，不修改 v41 checksum：PostgreSQL 只替换相关 CHECK，SQLite 在原迁移事务内原样复制 BLOB/TEXT 并恢复关系、索引和不可变触发器。其他 raw/outcome 上限保留，历史候选及失败/费用/授权不重写。
- **C：本地可修正的 finding identity。** 删除 Worker 重复 identity 模型，`Finding.identity` 使用 Server 的权威 `VulnerabilityIdentityInput` 且必填。非空类别、host、合法 port、至少一个非空 route/locus 同时进入模型与生成 Schema。goal-driven finding 要求非 null vulnerability；非 finding 仍允许省略或 null，但不接受非 null 漏洞声明。Runner 使用生产 Schema 和原同 session 修正流程，不猜造 identity、不增加次数/deadline或目标工具权限。
- **兼容作用范围：** 新 Artifact 声明必须携带固定内容身份与 `source_path`，identity 模型不再忽略额外字段或转换字符串 port。静态提交 contract 已重新导出；冻结旧 Schema 的历史执行不偷偷改 hash、不加 fallback，旧容器不会因镜像 tag 相同自动获得新声明。历史读取与原始内容保留，不自动补交或恢复项目。前端未改。

#### 本地验证：定向合成接受，不是发现能力验收

- TypeScript build 已通过；最终固定副本/容量/候选失败/原 deadline 五个 Node 节点 **5 passed、0 failed**，使用真实已安装 SDK 与回环假 provider。篡改副本的测试显式修改权限后再写，避免仅在 root 身份下通过；源增长测试无新增修正轮次。
- 扩展现有 `test_goal_driven_lifecycle.py::test_fixed_material_versions_and_identity_repair_reach_server`，最终 **1 passed in 2.81s**：真实 Runner 注册 20 项合成材料及 1 项真实 SDK overflow 输出；源增长后副本仍为登记 bytes；缺 identity 在本地反馈定位到 vulnerability 的合法 object 分支所需 identity，第三次 provider 调用只做原材料修正，没有目标活动。两候选及 outcomes 经生产 spool 解码、冻结 binding/Schema/Run 校验与真实 Server 持久化，保存 21 身份和原文，exact replay 不增记录。
- 同一实际产物继续经过生产 Dispatcher 严格语义校验、21 个 Artifact 的真实 Server 落盘、证据 handle 映射及 `intents.conclude`。核对 Fact、实验、canonical vulnerability 的 identity/fingerprint 与 observation/repro 原文；漏洞仍为 **new**，未冒称 confirmed。副本在上传前被篡改继续拒绝；ACK/replay 返回同 Fact、同 observation，同 Intent 不能重新探索，原 charged/execution_count/预算计数及授权/generation 不变；接受 Fact 可正常派生后续 Intent。
- 中途真实失败均保留解释：初版测试误把 `anyOf.alternatives.required` 当 leaf `required`；按实际安全诊断结构修正断言，未放松规则。之后数据库旧 16 项约束及 Dispatcher 旧三字段声明校验分别阻断真实链路，已修复必要消费者，而不是只跳过检查让测试变绿。
- Dispatcher 实际 `_reap_futures → finish → CairnClient.update_task_run → UpdateTaskRunRequest → Server 事务` 的失败结算节点 **7 passed**；只替换 HTTP 边界，真实生成 JSON/ACK/DB 的分类及细码一致，Attempt/Intent 失败结算，无 Fact、重复准入或二次计费。
- 生产 goal-driven Explore Schema 节点 **1 passed**，核对两个 Explore phase 的合法 identity、缺失/null/空位置和非 finding 规则；相关旧声明传输 fixture **2 passed**。
- 既有 candidate_store 的三个定向节点均通过：实际 ASGI POST/SQLite 保存 32 个最长 handle 与合计 32 MiB 的身份声明；33 项/32 MiB+1/manifest 超 16 KiB 拒绝。真实持久化预算测试在**局部缩小的测试额度**下验证 candidate/outcome 新帧 413、exact replay 不重复计量，按 UTF-8 bytes 加每帧 48 bytes 计算；不是实际填满默认 34 MiB。
- raw Artifact 复用既有隔离 Server/TestClient 与真实机器鉴权，匹配身份返回 201并核对 DB/磁盘 bytes/来源，SHA或大小不匹配422且无记录/临时上传残留，**3 passed in 2.67s**。这是 ASGI HTTP 入口验证，不冒称实际网络分块或 CairnClient→Server socket 端到端传输已测。
- 未默认运行全套测试、lint、前端构建；未安装依赖、升级 SDK、访问目标或调用付费模型。隔离 fixture 清理本次临时材料，不触业务原件。

- 双后端迁移定向验证最终 **5 passed in 4.28s**，使用现有 fixture 的隔离 PostgreSQL 18 临时集群与 SQLite，实际运行 v46→v47、DML、回滚和重复迁移；原 raw（含 NUL/非法 UTF-8）、manifest（中文/CRLF/空白/键序）、lineage/outcome、费用与授权历史逐值保留，升级后21/32身份及16KiB可保存，33/16385仍拒绝。SQLite/Postgres 各46条历史 version/name/checksum与HEAD逐项相同。原v41三个迁移节点也已通过。中途 Pg 不可变触发器实际抛 `RaiseException`，修正测试的异常类型期望后通过，未改触发器行为。SQLite大历史库迁移有临时空间与事务写锁成本；当前部署后端是PostgreSQL，只替换CHECK。

#### 部署生效

- 启停前从实际 Server 进程核对配置，`runtime/arena.env` 的数据库 URL 与原进程及 PID fingerprint 一致，实际仍为 `127.0.0.1:55433/cairn_isolated`，未切换到默认 `55432`。最终只读快照的主机实际 UTC 为 **2026-10-10T02:39:40Z**：DB v46，queued/running、active 项目、未完成报告/有效租约、运行 Worker、orphan、过期上传均0；两个 outbox 各只有 `.lock`。这不是维护锁，结论只覆盖观察窗口。
- 按现有 `docker build -f container/Dockerfile -t arena-worker:local container` 构建，未改 Dockerfile、SDK 或依赖。首次构建后发现新增总 size 校验早于 individual identity 校验会让 null 输入先产生 TypeError，已调整顺序；宿主重新 build 及 capability/golden/invalid identity 三个 Node 节点 **3 passed**，之后第二次构建最终镜像成功。最终 ID：`sha256:be208a94654d80a82637aec22c11b02f464204a476274f3195236a5991345f88`。
- 最终镜像以默认非 root Worker 用户、无网络/只读 rootfs 实际运行相关测试，使用镜像内 dist，不挂宿主 dist/node_modules：**8 passed、1 failed，进程 exit 1**。失败节点 `goal-driven Explore production Schema requires finding identity and matching outcome` 的辅助函数在镜像内调用宿主 Python 包，报 `ModuleNotFoundError: No module named 'cairn'`；纯 Worker 镜像本来只消费宿主提供的 Schema，不为消除这个环境不适用的失败增加 Cairn/依赖。宿主该生产 Schema 节点此前通过。随后把宿主生成的 goal-driven 生产 Schema 经 stdin 交给镜像内 TypeBox：合法 identity 接受、缺 identity 拒绝；同时核对 artifacts/candidate-spool/protocol/runner/submission 五个 JS 模块 SHA 与已验证宿主 dist 一致。这个补充检查不把原9节点结果改称全绿。
- 已执行 `deploy/stop.sh`、`deploy/start.sh --host 0.0.0.0 --port 8001`，完整重载宿主 Server/UI 与 Dispatcher。Server PID 从110711变为 **2919781**，Dispatcher 从110833变为 **2919944**；核对实际 cwd、PID start time及部署配置。`deploy/status.sh` 返回 running、Dispatcher ownership=tracked、API ready、heartbeat=idle。
- 重载后 **2026-10-10T02:50:57Z～02:50:58Z** 的 PostgreSQL `REPEATABLE READ READ ONLY` 快照确认实际 DB 已应用 **v47**，三个相关 CHECK 分别为32项和两个16384-byte manifest约束。queued/running、active、待报告/租约、运行 Worker、orphan、过期上传继续为0，outbox仅两把 `.lock`。六个 proj_030～035 原容器均保留且 exited，ID基线与当前 Dispatcher归属均一致；未删除容器/session/证据，未恢复暂停项目。
- 实际 socket 交互确认 `/health`、Vue UI入口及其10个静态资源均HTTP200；机器鉴权后的 `GET /candidates/capabilities` 实际返回32项/16KiB，并保留原2MiB raw、16候选、64 outcomes和34MiB spool额度。首次 UI烟测误断言 React `id=root` 而失败，读取真实页面确认是Vue `id=app`，修正烟测断言后通过；未改前端、服务或资源文件。前端复用既有兼容构建，此处不是浏览器交互或业务研究验收。
- 配置的 Worker image 已核对解析为上述最终 ID及原pi/SDK标签。当前没有 enabled workers，Dispatcher日志明确跳过启动时Worker healthcheck；本轮未为此启用Worker或创建真实研究任务。最终镜像已有隔离执行验证，**旧六个容器不会随同名tag自动升级**，未来新建容器才使用新镜像；不能把配置、ready或镜像测试冒称历史session已完成新版本交付。

#### 真实任务验收与停止边界

本轮不恢复或重跑 proj_035，不重开历史失败、不迁移旧 session、不修改旧费用、期限或授权；**真实任务未运行**。已有研究可靠交付的确定性链路已到隔离 Server 业务接受，但真实模型依据反馈修正的效果、实际目标研究及平台整体漏洞发现能力均未验收。当前分支 `docs/installation-guide`，未 commit、未 push。provider、报告/reopen、研究角色与前端等额外问题不在本轮展开。

## 证据变化后的局部修正与真实接受（2026-10-09；待实施）

### 用户目标与本轮改善

用户需要本地／内部使用的自主漏洞研究平台：通过 Origin／Goal 自由表达背景与目标，自主选题和验证，复用已有事实、证据与失败上下文，局部失败后继续研究并可信交付。执行可靠性服务于这个目标，不以任务状态变绿、字段齐全或测试数量代替真实发现能力。

**本轮只打通一个已发生的断点：证据文件登记后变化，能在原 Runner 执行内重新登记、更新引用并重提交，最终由 Server 接受，而不是重复探索。** 原则是修正互相矛盾的恢复语义，不取消证据完整性要求，不增加一套恢复平台。本节是另一实施会话的任务依据；分析会话仅修改文档，以下代码与验证均尚未实施。

### 已确认依据与未知

- 历史 `proj_032 / tr040、tr051` 的底层错误均为 `artifact_changed`，origin 为 `pi_submission`、phase 为 `explore_execute`，提交尝试数为 1。数据库有限字段与错误记录证明此错误实际发生过；**文件具体为何变化仍未知**，不能猜测某个工具持续写入，或通过禁止正常研究写文件来掩盖问题。
- `container/pi-agent/src/artifacts.ts` 的 `ArtifactRegistry.register()`（交接时约 63–73 行）允许对同一 path／kind 重新登记，替换旧登记并返回新 handle；`inject()`（约 75–98 行）检查全部已登记项的真实摘要，变化则抛出 `artifact_changed`，错误定义要求重新登记。
- `container/pi-agent/src/submission.ts` 的 `SubmissionState.repairable()`（约 193 行）未包含 `artifact_changed`。`runner.ts` 的提交入口（约 269 行）、catch（约 300–308 行）及修正循环／最终判断（约 542–554 行）共同将它变为不可修正状态。工具反馈主要保留错误码，原 Artifact 恢复文案没有完整传递。**Registry 支持重新登记，不等于 Runner 允许修正成功。**
- 工作区已有成功 Reason 截断旧链、原补交中 provider／transport 失败保留交付身份的修改，见后文实施记录。本轮复用、定向核验，不重新实现或覆盖这些修改。
- 已有真实 SDK＋回环 SSE 假 provider：`container/pi-agent/tests/fixtures.mjs` 的 `fixture(t, protocol, script)`。脚本步骤可确定性修改合成证据；`artifacts.test.mjs` 已验证 Registry 的重新登记，`runner.test.mjs` 已验证同会话只读修正和耗尽，但尚未覆盖本次 `artifact_changed` 的 Runner 恢复闭环。
- 最近 `proj_033` 的 Reason 曾发生 `phase_shape_invalid`，后续 Reason 成功；两次 Explore 的 `provider_error` 均尚未尝试提交。具体 Schema 字段及这两次 provider 底层原因仍未确认，不与本轮 Artifact 问题合并。后文更早 SDK 错误的证据不能自动套用到这些运行。
- `2026-10-09 07:00:51Z` 的只读调查确认应用未运行、数据库正常，实际项目记录对应 55433。这是历史快照，不能覆盖后续状态；实施前重新核对实例、在途工作与实际镜像，不把旧 PID、端口或标签当作生效证明。

### 最小完整实施范围

1. **在现有同次执行修正机制内恢复 `artifact_changed`。** 定向核对 `SubmissionState` 与 Runner 的入口、catch、工具收缩和结束条件，复用当前修正轮数、候选记录和绝对 deadline。不另建状态、计数器或恢复后台；不通过增加次数、延长期限、更换模型来代替修复。
2. **让反馈足以完成修正。** 经已有工具回执／修正提示说明：重新登记发生变化的既有证据，用返回的新 handle 更新相关引用，再提交。需要定位时优先使用已登记 handle／kind 等安全标识；不传播任意文件系统异常、证据正文或凭据。只补齐当前操作必需的局部反馈，不为此新增通用诊断协议或长研究脚本。
3. **保持证据接受条件。** 重新登记后仍实际校验摘要、引用与 Schema；不能把旧 handle 静默指向新内容、自动刷新摘要后放行，也不能通过删除登记项或跳过“未显式引用的已登记文件”规避现有完整性语义。陈旧引用、未重新登记和持续变化仍拒绝；不能在原有界限内完成时如实失败。
4. **修正成功后正常进入接受路径。** 优先复用现有 candidate 成功条件，不预设新增 success 状态或清空错误码；当前代码确实存在遗留状态阻断时才作必要调整。前次失败候选／回执保留，不改成成功。修正期间仍只能读取既有材料、登记和提交，不恢复 Shell／目标活动，不重复实验。
5. **核验生产者到消费者。** 用同一组非敏感合成材料，把实际修正后的 Runner 产物送过现有 Dispatcher 解码、证据登记和 Server 业务接受接缝。优先复用 `decode_pi_result()`、已有 Explore 接受路径与隔离 fixture，确认接受的是新登记对应的内容、已接受结果不触发重复执行、相关后续工作仍可正常安排。不要手工 PATCH `succeeded` 或 mock 掉需要证明的接受校验。

**作用范围区别：本节增加失败最终上报前的局部修正机会，不放宽 Server 对最终 `artifact_error` 的跨 Run 终局／重试政策。** 后文“provider 后 Artifact 拒绝不应变成自动 fresh”的边界仍有效。历史 `tr040/tr051` 不自动复活，旧期限、费用、failed Intent 不被清零或重写。

现有三个服务文件是必要时核对的消费者，不是本轮预设修改清单。只有这条直接路径暴露具体上下游缺口时才调整，并说明关联；不要重新审计或清理全部历史测试。如果当前源码已经解决问题，或与方案依据冲突，说明差异并复用已有实现，不机械执行旧补丁。

### 直接验证与交付边界

复用 `container/pi-agent/tests/artifacts.test.mjs`、`runner.test.mjs` 和 `fixtures.mjs`，补充或扩展少量定向用例：

- **成功修正**：真实 SDK 注册合成文件 → 测试侧确定性改变文件 → 提交被拒 → 原会话只读修正，重新登记并更新引用 → 提交成功。核验新旧 handle、修正期间工具范围和最终材料身份，不只断言 `repairable()` 返回 True。
- **有界失败**：发生变化后始终没有正确重新登记，或材料持续变化 → 原有修正轮次耗尽 → 无成功候选，错误与材料保留。必要的陈旧引用拒绝断言放在相关用例中，不扩展成边界矩阵。
- 随后用上述实际修正产物完成一条隔离业务接受验证。可复用 `cairn/tests/test_pi_result_protocol.py`、`test_goal_driven_lifecycle.py` 等已有接缝；只选相关节点，不为跨端验证另建通用框架。既有 delivery 修改按这条路径的实际需要核验，不再次全套重测。

这些 Node 用例使用真实 SDK 和本地假 provider，证明确定性协议行为，不证明真实模型一定能依据反馈完成修正。用例从 `dist` 导入，先执行已有 `npm --prefix container/pi-agent run build`，再用 `node --test --test-name-pattern=...` 精确运行实际相关测试名，不默认 `npm test` 全跑。无关 lint、前端检查或浏览器矩阵不追加；本轮不在业务数据库写入测试，不访问被测目标、不调用收费模型。未贯通的步骤明确列出，不能把组件测试或本地 `accepted=true` 称为业务接受或真实研究验收。

### 构建、部署与原项目连续性

- 本轮涉及容器内 Runner，实施后需要构建对应 Worker 镜像并确认版本生效，不能只重启宿主服务。相同镜像标签不证明旧容器使用了新内容；既有运行／会话容器不能随意删除重建，按项目现有部署方式说明更新作用范围。
- 未改且兼容的前端产物可复用，沿用后文构建纠偏，不把无关前端全量重编译作为门槛。资源不足时报告实际瓶颈，不反复执行同一 OOM 构建，不擅自加 swap、改系统配置或停止其他会话。
- 启动完整系统前检查实际实例的 active 项目、在途任务及待派发工作。启动 Dispatcher 本身可能触发模型费用；有这种影响时先说明并取得相应操作许可，不自行暂停项目、改数据库，也不以“未调用 resume”为无副作用的证明。没有业务执行风险时按项目现有授权完成必要部署启动，并报告实际加载情况。
- 保留原证据、session、失败和费用。代码修复不等于旧 deadline 延长、历史失败 Intent 重开或暂停项目恢复；原会话能否使用新版 Runner 需按实际容器状态说明，不为展示成功强行迁移或重做研究。

### 文件范围、后续事项与记录要求

优先改动 `container/pi-agent/src/submission.ts`、`runner.ts`；仅在局部反馈确需时调整 `artifacts.ts`，不重写 Registry。同步少量相关 Node 测试及必要的既有 Python 接受用例。`worker_result_gateway.py` 与既有服务文件是核对入口，不预设必须修改。

本轮不改报告／reopen 生命周期、不重写研究角色或实验粒度、不修改 `AGENT.md`／全局会话模式、不升级 SDK 或新增依赖，也不猜测修复 provider 故障。此前分析中的报告失败阻断 reopen、stopped 后连续研究、规范取舍等保留为后续方向，不自动扩大本轮。

实施前核对分支和最新工作树，保护当前已有三个服务文件、两个测试文件及两份设计文档的修改。只在实际修改重叠／依据冲突时协调，不整文件覆盖，不为换会话重复造分支或复制方案。实施后在本节追加**代码实现、本地验证、部署生效、真实任务验收**的实际结果，最后一项未进行就明确说明；报告分支、修改文件、测试结果、提交情况和剩余限制，不 commit、不 push。

本次分析交付：分支 `docs/installation-guide`，仅增补本文并保留用户既有改动；未修改业务代码，未运行测试／构建、未启停服务、未操作项目或模型，未创建提交。以上为待实施方案，不是修复完成声明。

### 实施记录（2026-10-09）

以下记录更新本节前述“待实施”的交接状态，不将其他历史章节转为本轮待办。

**代码实现**

- `SubmissionState.repairable()` 增加 `artifact_changed`，Runner 复用已有修正循环和 candidate 成功条件。未增加成功状态、修正次数、跨 Run 重试或恢复服务，未清空错误码来冒充成功。
- `ArtifactRegistry.inject()` 保持全部登记项的真实 SHA256／size 校验；错误反馈只使用 Runner 生成的 handle 和已校验 kind，要求读取既有文件、按相同 path／kind 重新登记、用新 handle 替换引用（包括显式 `data.artifacts`）后提交。Runner 仅传递这一固定安全文案，不泛化透传任意文件系统异常、路径或证据正文。
- 修正仍只开放 `read_evidence`、`register_artifact`、`submit_result`，沿用原绝对 deadline 和最多两轮额度；旧引用不静默刷新，持续变化仍拒绝，失败候选与回执保留。未改 Dispatcher／Server 生产代码，最终 `artifact_error` 的跨 Run 终局政策未放宽。
- 本轮实际增量为三个 Runner 源码文件、三个既有 Node 测试文件、`cairn/tests/test_goal_driven_lifecycle.py` 与本文，共八个文件。三个服务文件、`test_delivery_retry.py`、安装设计文档及重叠文件中的用户既有修改均保留；未新增依赖或升级 SDK。

**本地验证**

- 已执行必要的 `npm --prefix container/pi-agent run build`，TypeScript 编译通过。最终源码定向复跑七个 Node 节点，**7 passed、0 failed**：同 session／Run 的重新登记与正确提交、故意保留旧 handle 后拒绝再修正、未重新登记时原额度耗尽及原始候选保留、持续变化拒绝且登记摘要不自动刷新、原 deadline 取消修正，以及相关只读工具／严格参数回归。未默认运行全套测试。
- 新增一个隔离接缝节点 `test_goal_driven_changed_artifact_same_session_repair_reaches_conclude`：真实 SDK＋回环假 provider 使用生产提交 Schema，自然产生 `artifact_changed → read_evidence → 同 path／kind 新 handle → runRequest succeeded`。实际信封经过 `decode_pi_result` 严格解码，真实材料经过 `record_declared_artifacts` 和服务端 Artifact 落盘，生产引用映射后调用真实 `intents.conclude`；仅替代 provider、容器 IO 和隔离连接，未 mock 接受校验、未手工构造成功信封或 SQL 写入 `succeeded`。
- 比较重新登记回执、实际文件 bytes 和服务端 Artifact 的 SHA256／size／归属，核验 Fact／实验／证据／Run 引用；使用生产 `decode_candidate_spool` 验证两候选、旧 rejected／新 locally_validated 回执与父子关系。原 attempt 的 charged、execution_count、已消耗探索预算均保持 1，授权与 generation 不变；真实终态 ACK 后重发返回同 Fact，原 Intent 再准入拒绝，由接受 Fact 派生的新 Intent 正常准入，没有重复原实验。
- 上述接缝与既有 `test_goal_driven_artifact_rejection_after_provider_retry_remains_terminal` 最终一并复跑，**2 passed in 5.50s**。使用现有系统 pytest 与项目依赖的混合环境，未安装包：

```bash
node --test --test-name-pattern='artifact_changed repairs|artifact_changed repair never extends|registry rejects symlinks and changed artifacts|artifact injection failure preserves|text is not JSON fallback|strict tool arguments reject coercion|deadline aborts without' container/pi-agent/tests/runner.test.mjs container/pi-agent/tests/artifacts.test.mjs container/pi-agent/tests/candidate-capture.test.mjs
PYTHONPATH=cairn/src:cairn/.venv/lib/python3.13/site-packages uv run --no-sync --project cairn pytest cairn/tests/test_goal_driven_lifecycle.py::test_goal_driven_changed_artifact_same_session_repair_reaches_conclude cairn/tests/test_goal_driven_lifecycle.py::test_goal_driven_artifact_rejection_after_provider_retry_remains_terminal -q
```

- 验证过程中 `.venv` 缺少 pytest；单独使用系统环境又缺少 docker 依赖，改用已有混合环境，不变更依赖。初版合成提交使用 `data.description` 被生产 Schema 如实拒绝为 `submission_invalid`，夹具改为当前契约的 `data.fact.description` 后通过，未放松 Schema。最终接缝清理临时工作区／spool；未写真实业务数据库、访问真实目标或调用收费模型。

**部署生效**

- 按现有方式执行 `docker build -f container/Dockerfile -t arena-worker:local container`，**构建 exit 0**，包含现有编译和 self-check。新 image ID 为 `sha256:23f0af3c7bb6da9a931d26738d71a40a7434b0ae42c3d74a248c85d38ef7efdf`。既有 Kali rolling 基础镜像变化使系统依赖层重新下载，耗时较长；未修改 Dockerfile、SDK、系统资源或停止其他服务。
- 以该 image ID 而非标签启动新临时容器，使用 `--network none --read-only --cap-drop ALL --security-opt no-new-privileges`、临时 `/tmp` 和只读测试目录，无真实凭据／业务工作区挂载。镜像内直接导入 `/opt/cairn/pi-agent/dist`，上述七个 Node 节点 **7 passed、0 failed**，没有挂载宿主 dist／node_modules；`--rm` 仅清理此次临时容器。
- 三个编译文件的镜像内 SHA256 与已验证宿主 dist 完全一致：`artifacts.js` 为 `8e3ce8dcdea577aa5b1473958882af20dfd255c4f7eab303969fd289e3dfb464`，`submission.js` 为 `a1877620430521e7c29d1a46a1d45bf230b71424261e3963d8dd94aadb17b5f3`，`runner.js` 为 `ac086b91732699d99faad6859f021c2d6f4ec5b76215e5ec5093673e67b43410`。这证明构建镜像确含本轮 Runner，不只依赖 labels 或相同 tag。
- 实施前重新核对实际实例，`08:36 UTC` 快照中 Server／UI HTTP 200，Dispatcher 正常持锁且 idle，active 项目、在途 Run、可自动派发与待报告工作均为 0；暂停项目的开放 Intent 不能因此恢复。现有宿主完整系统未启停，兼容前端产物复用，未触发任务或费用。`09:00:53 UTC` 再次核验 Server／UI HTTP 200，四个既有 pi 项目容器的 ID／image ID 未变，仍 exited、无 ExecIDs，原容器／session／材料全部保留。
- **生效范围**：`arena-worker:local` 已指向新镜像，后续新建 Worker 容器可使用它；既有容器的 runtime signature 比较镜像引用字符串，重建同 tag 不会自动升级旧容器。`proj_031/032/033` 仍使用 `sha256:9b9aa6dd56452f101bf8baa82b12ea7540ae901912f1408c514ad81f61c23cde`，`proj_030` 仍使用 `sha256:9d06091d829e329e69ec0a2e32ce3d2e911513d43e392a7b8a0cc41d09cd63ca`。旧 session 未迁移，新镜像验证不能说历史项目已加载新版或真实任务已完成；升级原会话需另行明确兼容与操作授权，不在本轮强行重建。

**真实任务验收与交付边界**

- **真实任务未运行。** 本轮没有恢复暂停项目、重开历史失败 Intent、迁移旧 session、修改历史期限／费用或调用真实模型／目标。回环假 provider 证明确定性修正机制，隔离 Server 证明业务接受条件，不证明真实模型一定依据反馈修正，也不构成真实漏洞发现效果验收。
- 当前分支 `docs/installation-guide`；未创建提交、未 push。最后复核实际 diff 与空白检查；报告／reopen、未知 provider 根因、研究粒度与全局规范仍保留后续，不在本轮实施。

## proj_032：成功规划后的连续推进与原会话交付重试（代码已实施；构建与部署状态见实施记录）

### 分析交接时的状态与目标（2026-10-08；实施前快照）

- 用户报告：目标未达成但有时不再推进，暂停／恢复后才继续，另有“失败两次后不再尝试”。本轮要消除已确认的规划衔接与交付重试阻断，让已有事实、材料和失败上下文能够继续被使用；**不是承诺只要 Goal 未完成就无限重试，也不宣称真实漏洞发现能力已通过验收**。
- 本轮为“分析、设计与实施交接”：只读检查源码、实际进程元信息、数据库和相关日志，仅更新本方案；未修改业务代码、运行测试、构建、启停服务或操作项目。以下修正均为待实施，不是完成声明。
- 当前分支 `docs/installation-guide`。`docs/specs/installation-guide-design.md` 有用户既有修改，未触碰。交接文档指向的 `goal-driven-execution-design.md` 仍不存在，沿用本文承载调度问题，不新建平行方案。本节取代的是本轮涉及的判断，不自动恢复 `proj_030` 旧待办。

### 运行证据：不能把所有“两次失败”视为同一件事

实例定位：Server PID `958435` 提供 `8001`，Dispatcher PID `958627`，cwd 均为 `/root/桌面/Arena`。Server 进程的数据库指向 `127.0.0.1:55433/cairn_isolated`，不是另一个 `55432` 数据库；PID、端口仅用于本轮定位，实施前重新核对。数据库用只读事务按精确 `project_id = 'proj_032'` 查询，不导入应用触发初始化、不调用可能修改租约的项目 GET 接口。

下列运行时间为数据库原始 UTC（`Z`）；主机及 Dispatcher 文本日志为 `America/New_York`、UTC−04:00，不能直接把两者相减判定时钟异常。

| 证据范围 | 已确认事实 | 解释边界 |
| --- | --- | --- |
| 当前项目 | `active`、`execution_generation=4`、无 Reason 占用；29 条 Fact、32 条 Intent（27 completed、5 failed、0 开放），61 条 TaskRun；`explore_attempts_started=31/explore_budget=40`，`coverage_stop_reason=NULL` | 当前没有在途执行，也不是 Explore 预算已耗尽；active 不代表正在执行，队列耗尽不证明目标达成 |
| 成功的原会话规划续交付 | Reason `tr007` 的 `delivery_retry.source_task_run_id=tr006`；`tr056` 的来源为 `tr055`，两者均成功 | 已成功的交付链不应继续成为下一次独立 Reason 的待处理义务 |
| 最后一批探索与下一次规划 | `tr056` 后 `i030/i031/i032` 完成；最后的 `tr059` 于 `07:42:26Z` 结束，下一条 Run 是换到 generation 4 的 `tr060`，始于 `07:52:32Z`。`runtime/logs/dispatcher.log:1838` 记录 `execution_generation:3->4,facts:26->29,settled_intents:29->32` 的 Reason 触发 | 10 分 06 秒的 Run 间隔及跨代重触发是事实；不能仅凭间隔推断全部期间的容量或唯一阻断，也不能补造暂停点击的精确时间／操作者 |
| `tr060 / reason / generation 4` | `07:52:32Z` 至 `08:00:19Z`，`submission_missing`；执行内部 `repair_rounds=2/max_result_repairs=2`、`submission_attempts=0` | 两轮本地修正发生在同一个 TaskRun 内；未提交不等于 Server 拒收两次 |
| `tr061 / reason / generation 4` | `08:00:25Z` 至 `08:00:46Z`，`provider_error`，`worker_failure.origin=pi_sdk`；`delivery_retry.source_task_run_id=tr060`，无提交、无修正轮次 | 这是上一条 Run 的原会话补交，不是新的研究规划；冻结 phase 与失败 phase 仍为 `reason_execute`，不能虚构 `phase=delivery_retry`。原始 Run 与本次 Run 均为进程退出 1，不是 timeout/OOM/cancel |
| `i027 / tr050 -> tr051` | generation 3，先 `provider_error`，再 `artifact_error`，细码 `artifact_changed`；候选回执 `artifact_status=rejected`、`binding_status=not_evaluated`。`aa026.execution_count=2`，`max_runtime_retries=10` | 是材料一致性拒绝，不是用完 10 次额度；不能放宽材料校验、把候选改成已接受或直接盲重跑 |
| 当前调度观测 | `dispatcher_status` 在 `08:40:55Z` 的观测仍为 idle，`running_task_count=0`、`active_project_count=1`；项目决定为 `execution_delivery_unavailable`、generation 4、task_type reason，无 retry_at | 证明 Dispatcher 仍在循环但当前交付不可用；不能把源码推导的早期 `execution_retry_blocked` 写成查询到的历史观测 |

`tr060/tr061` 共享冻结期限 `2026-10-08T08:12:33.031612+00:00`：第二次失败结束时尚未到期，调查时已过期。两条 Run 均无候选记录，生命周期均已确认启动、终止和清理。`dispatcher.log:1851` 曾记录 `tr061` 候选恢复控制清理失败，但之后的持久化证明已完成，不能仅凭旧日志认定当前仍卡在清理。`tr055`（`07:23:59–07:30:49Z`，submission_missing）与 `tr056`（`07:31:00–07:31:39Z`，succeeded）共享原期限 `07:43:59.439739Z`；成功规划之后的新规划不应继续受这份旧期限约束。

证据来自 `projects`、`intents`、`task_runs` 的有限状态／关联字段、`assessment_attempts`、候选回执、`dispatcher_status` 和上述日志。未转储 Prompt、session、目标正文、凭据或候选正文。此次查到的 `execution_events` 无该项目记录；相关安全审计未提供完整 pause/resume 操作链。已有一次直接暂停证据：`dispatcher.log:1529–1530` 在 `06:11:36Z` 观察到 paused，`tr038/tr039` 随后为 pause_interrupted 的 cancelled，generation 3 的 `tr040` 于 `06:12:09Z` 启动；其余暂停／恢复精确时刻和操作者未知。早期 generation 0 还出现过 candidate recovery 未解决的日志，`tr013` 则本地校验通过、进程退出 0 后，在 `abandon_intent` 写入收到 409（`agent_events/ev596`、`dispatcher.log:1057`）而最终 business_acceptance；**不把该项目全部历史停顿归为同一个原因，也不把本地通过当业务接受**。上游 `provider_error` 的具体请求失败原因、为何模型始终未提交、`artifact_changed` 的具体文件变化原因以及该早期 409 的完整原因链仍未确定，不纳入本轮猜测修复。

### 当前实现判断：首因、放大因素与不应沿用的旧结论

1. **成功 Reason 没有结束旧逻辑交付链，是连续规划的确定性缺口。**
   - `cairn/src/cairn/server/services/assessment_attempts.py:479–498` 的 `_logical_executions` 仅按 generation、task_type、intent_id、phase 分组，Reason 的 intent_id 为空；没有按已成功的 Reason 切断历史。`:723–745` 只是跳过 succeeded，旧失败、旧交付标志仍参与后续裁决。
   - `delivery_retry_routes` 自己另行分组（`:386–435`）。最新 Run 即使 succeeded，只要还带 `delivery_retry`，仍进入交付路由，再由 `:426–427` 标为 `assessment_execution_not_quiescent`。`derive_delivery_retry_context` 的 `:603–624` 也会因同一成功来源上的交付标志拒绝新 admission。只改计数或 Dispatcher checkpoint 不会打通。
   - `tr007/tr056` 的成功续交付形状实际命中了这个代码前提；`tr056` 后新事实和已结算 Intent 已经产生。源码足以确认该阻断机制，但没有连续历史决策快照，不将全部等待时段的唯一根因冒称为运行已验证。
2. **“不许退回 fresh”被实现成了“原交付中发生其他错误就不许再续交付”。**
   - `assessment_attempts.py:729–739` 在交付链中遇到非 submission 错误，包括 `provider_error`，直接返回 `execution_retry_blocked`，早于额度判断。`:623–624` 在 admission 派生上下文时还有第二处硬拒绝。因此 `tr060 submission_missing -> tr061 原会话 provider_error` 在来源仍有效时也会被拒；调查时的实际观测已是 `execution_delivery_unavailable`，两种时刻和原因必须区分。
   - 防止原会话补交偷偷重新探索是必要边界；把可恢复的供应商故障连同原会话有限补交一并禁止，没有帮助研究连续性。不能只把 provider_error 加进一个白名单，却让 route/admission 仍拒绝或退回 fresh。
3. **暂停／恢复绕过旧代次，不代表修好了原链。** `project_records.py:647` 的 pause 增加 generation，`:712–713` 的 resume 只置 active。`dispatcher/scheduler/dispatch_cycle.py:583–607` 会因代次改变重新触发 Reason，而旧代次失败不再进入当前重试分组。已有 failed Intent 不因此重开，已有失败和费用也不应被清零。
4. **没有“所有任务失败两次就停”的统一额度。** 同 Run 的两轮本地修正位于 `container/pi-agent/src/runner.ts:542–554`，配置生产者为 `dispatcher/workers/adapters/pi.py:207`。跨 Run 默认是初次执行加两次重试，`assessment_attempts.py:749` 在失败数大于 2 后才耗尽；某些纯 provider/transport 故障另有现存扩展额度。`tr061` 的类别阻断和 `tr051` 的材料拒绝都不能诊断为单纯额度不足，本轮不调大这些数字。
5. **继续推进的触发已有实现，不另造无条件轮询。** `dispatch_cycle.py:381–457,576–622` 已消费可运行 Explore，并在事实增长、开放任务归零或全部任务结算后触发 Reason；成功 checkpoint 保留派发前计数（`scheduler/task_lifecycle.py:701–719`）。本轮重点是打通触发后的持久路由与 admission，不因目标未完成就每个 tick 重跑模型。
6. **运行诊断必须消费同一“当前链”。** `server/services/project_runtime_diagnostics.py:310–327,999–1006,1074–1076` 会投影当前代次历史失败 scope 的 eligibility，前端 `features/projects/workspaceStatus.ts` 直接展示服务端 message。它不能继续将已完成的旧 Reason 交付判为当前项目阻断；也不能反过来用宽泛“等待下一轮”遮蔽真实 deadline、材料或额度终点。这里是必要消费者对齐，不是新增一套前端调度逻辑。

### 本轮最小完整实施范围

**推荐方向：在 Server 的现有执行记录上明确“本次 Reason 已完成”与“仍在同一原交付链中”，复用当前事实／回执／来源关联，修正路由与故障类别的职责混淆。不新增逻辑执行表、第二计数器、自动修复后台或新审批流程。**

1. **成功 Reason 切断旧链。** 以已成功结算的 Reason 为明确边界，后续新 Reason 不继承该边界之前的失败额度、deadline、session 或 delivery route；保留全部历史 Run、candidate、artifacts、已接受 Fact／Intent 及回执。复用现有 `TaskRun.status` 和来源关联即可，不因普通图变化就任意重置尚未完成的失败链。
   - 统一 `_logical_executions`、`delivery_retry_routes` 自身分组、eligibility、admission 派生和诊断所用的“当前 Reason 链”定义，不能只在一个循环中跳过成功 Run。
   - 边界后的新失败继续计入新链；更早成功不能抹掉后来未交付的义务。partial／已接受部分写入不伪装为完整成功，也不能靠重开 fresh 重复写入。不要顺手改变 Explore attempt、报告 generation token 或暂停中断 continuation 的范围。
2. **原交付链内的瞬态故障允许有界原链重试。** 对当前已有稳定分类的 provider/transport 瞬态错误，模式仍为 `delivery_retry`，而不是 fresh。由同一个规范实现同时裁决 eligibility、来源路由和 `derive_delivery_retry_context`，按现有 `delivery_retry.source_task_run_id` 与冻结 `execution_delivery` 取得真实来源，禁止按描述猜测、重建缺失 session 或悄悄生成新绑定。
   - 继续使用原 owner、session、container、模型／Schema binding、inputs 和剩余 deadline；不重新登记新的完整 timeout，不享受 fresh 的扩展重试额度。保持当前有界额度与持久退避，同一原链所有失败累计，不通过错误类型切换刷新计数。
   - 贯通 `server/services/audit_task_runs.py:83–120` 的服务端 session／structured binding 派生，`dispatcher/scheduler/task_lifecycle.py:759–798` 的权威上下文转交，`dispatcher/tasks/reason.py:88–98,158–166` 的冻结上下文及 `resume/conclude_only`，以及 `dispatcher/tasks/delivery.py:56–100` 的剩余期限／原容器校验。
   - 共享代码影响 Explore／Bootstrap／Report 时，保留各自原绑定、attempt、冻结输入与来源语义，尤其不能重建报告输入快照。物理执行未确认结束、有在途 Run、原容器／session 不可用、deadline 到期、额度耗尽、确定性业务／材料拒绝时继续如实阻断；不要把 provider_auth、artifact_changed 或所有未知异常一并放宽。
3. **诊断与必要提示同步。** 成功的旧链不再使新规划显示重试阻断；真正的原链退避、过期和耗尽仍给出实际原因及已有 retry_at，不将暂停／恢复作为正常每轮推进的必要步骤。不新增项目状态、恢复按钮、前端重试计数或整页重构。

不做：调大 timeout／repair／retry 数字、延长 Prompt 代替逻辑修复、更换模型、自动重开全部失败 Intent、删除回执、取消 Artifact 校验、重写研究引擎／Goal 判定／候选恢复／权限／报告系统，或声称候选和字段完备等于漏洞已确认。`i027 artifact_changed` 记录为另一个明确失败，不在本轮顺手猜测修复文件变化原因。

### 直接验证与实施交付边界

分析会话未执行下列验证。实施会话使用合成的非敏感材料与现有测试接缝，选择一组最直接的定向用例，不访问目标、不调用付费模型、不用真实 `proj_032` 反复试错，也不默认全套测试、lint 或浏览器矩阵。

- **成功规划后的下一步**：`submission_missing -> 原交付 succeeded -> 本批 Explore 全部终态／新事实 -> 同代新 Reason`。通过实际 route、claim/admission 和 Dispatcher 选择链确认不需 pause/resume，旧 session 不被新 Reason 错用，且能够派发下一次规划；同时验证“旧 succeeded -> 后来的新失败”不会错误清掉新链。不能仅测试一个计数 helper 返回 True。
- **两次失败后仍有合法补交机会**：`submission_missing -> 原交付 provider_error -> 冷却结束 -> 仍有额度且未过期的原会话补交`。验明取得同一冻结 session/binding/container/deadline，仍为 conclude_only，不新增探索副作用。再验证原 deadline／额度终点以及未确认物理停止仍拒绝，不能以允许重试为由无限续命。
- **确定性拒绝边界**：`provider_error -> artifact_changed` 仍保持材料拒绝和失败语义，不变成自动 fresh。确认局部失败的记录／材料保留，已完成旧 Reason 不再污染当前诊断。
- 优先复用 `cairn/tests/test_delivery_retry.py`、`test_dispatcher_retry_decisions.py`、必要的 `test_pi_dispatch_cycle.py` 和 `test_project_runtime_diagnostics.py` 的少量实际相关用例。原 `test_delivery_chain_cannot_fall_back_to_fresh_provider_retry` 中伪造或缺失来源仍应拒绝；新加合法原链瞬态故障可补交的情形，不能为了旧断言一概阻断，也不能删掉“不许 fresh 回退”的有效检查。不新增验收平台、依赖或临时报告。

**真实项目与部署：**

- `proj_032` 仍是保留业务材料的现有项目。改好代码不等于它的历史冻结期限被延长、failed Intent 被重开或历史候选被接受；当前不可用的旧链不能静默变为成功。实施后只读说明此项目可用的正常恢复路径与限制，涉及执行／费用时由用户决定，不要求手改数据库或默认新建付费项目。
- 运行代码修改后按 `docs/deployment-linux-zh.md` 与实际入口构建、重载用户使用的完整系统，核实真实数据库归属，不切到默认库或以隔离副本 ready 代替。只修改 Server／Dispatcher 时不要无理由重建 Worker；确有容器代码变化才重建并确认后续执行使用对应版本。
- **部署前检查 active 项目的自动派发影响。** 本项目调查时为 active；重启 Dispatcher 或解除门控也可能触发真实调用，不能把“未调用 resume API”当作没有启动任务。若部署会释放待派发工作或中断在途执行，先说明具体影响并取得用户所需操作许可，不自行暂停项目、重跑、访问目标、调用付费模型、改历史授权或清库。
- 不 commit、不 push。实施会话完成本轮后，在本节追加“代码实现、本地验证、部署生效、真实任务验收”四种状态及剩余限制，报告分支、文件、验证与提交情况；之后停止，由分析会话结合实际结果核对，不自动展开下一轮。

### 本次实施结果（2026-10-08；部署后核验于 2026-10-09 UTC）

以下是本轮实施结果；前文保留分析交接时的 active／generation 4 快照、源码行号与待实施判断，不将其解释为实施后的当前状态。

#### 代码实现：已完成本轮范围

- `assessment_attempts.py` 在原 generation／task／intent／phase 分组内，以最近一次 `succeeded` Reason 作为旧链结束边界；路由、eligibility、admission 与运行诊断消费同一当前链。已完成旧链不再阻断下一次独立规划，之后的新失败仍参与裁决，partial／已接受前缀不算完整成功。保留历史失败、回执和材料；不新增逻辑执行表、计数器或恢复后台，也不增加“Goal 未完成就每轮调用”的触发。
- 原交付中的 `provider_error`、`provider_rate_limit`、`transport_error` 仅在真实 `delivery_retry` 来源关系成立时继续原链。前序必须是同一当前链内已失败的交付执行，owner、worker、session、attempt 与完整冻结 `execution_delivery` 一致；不允许缺失／伪造来源、切回 fresh 或借错误类别变化取得扩展额度。原初次执行加最多两次重试、持久退避及绝对期限未调大；请求继续 `resume=True`、`conclude_only=True`，保留原容器、模型／Schema、binding、inputs。
- `explore_attempts.py` 的原链交付沿用原 structured binding，不因服务故障重新消费实时 Cell；fresh 与暂停续作保留原准入。`assessment_reports.py` 识别瞬态失败仍处于原交付模式，保留原报告 generation token、输入快照 Artifact、hash 与来源校验。Explore 的同一 attempt／原计费不变；Bootstrap 不另设策略。
- 来源不可用、未确认物理终止、在途执行、过期、额度耗尽、确定性业务／材料拒绝仍阻断。未修改 Worker、模型、Prompt、Schema、授权、前端源码或依赖声明，未重开历史 failed Intent。

#### 本地验证：定向合成用例已通过，非真实目标验收

分批运行现有接缝中的相关用例，去重后共 **38 个用例最终通过**，不是一次全套运行：

| 验证范围 | 通过数 | 直接观察 |
| --- | ---: | --- |
| 同代下一次 Reason 与三类瞬态原链交付 | 4 | 实际 HTTP route／claim／TaskRun 结算及 Dispatcher 调度；下一次为无旧 session 的独立 Reason。实际 Reason 请求捕获确认 readonly resume、原冻结输入和剩余 timeout，不启动模型进程 |
| 原链阻断边界与诊断／partial | 9 | 过期、未确认终止、额度、材料拒绝、来源缺失、快照改变、在途执行均拒绝；旧成功不隐藏新失败，历史 error 与部分接受回执保留，诊断只读 |
| 既有成功边界、来源变更及原额度／冷却 | 12 | 当前链不受已完成旧 Reason 污染，原归属与两次重试限制保留 |
| Goal-driven Explore 归属及材料终止 | 4 | provider／transport 原链沿用同一 attempt、冻结输入与一次计费；provider 后的 artifact_error 仍结为失败，不能再次派发 |
| Report 冻结快照及持久冷却 | 2 | 第二次 submission_invalid／provider_error 后均保留原 token、Artifact 与 hash，继续同一原链，期限与额度终点仍生效 |
| 既有资源、fresh 策略及协议分类 | 7 | 容器替换／模型改变／过期拒绝、Bootstrap 原链、artifact_changed 分类、禁止 fresh 回退及独立 provider 重试政策未放宽 |

- 主回归命令：`PYTHONPATH=cairn/src:cairn/.venv/lib/python3.13/site-packages python -m pytest -q cairn/tests/test_delivery_retry.py -k 'successful_delivery_reason_releases or delivery_transient_failure_keeps'`，结果 `4 passed, 87 deselected`；其余表内用例按具体节点或过滤器另行运行，没有默认全套测试、lint 或浏览器矩阵。
- 修正前上述四例实际失败，修正后通过。验证过程中先遇到运行环境缺 pytest／docker，以及旧授权、Schema digest、清理 generation 与错误测试属性等夹具问题；改用已有测试环境和当前契约的合成夹具，没有放松生产校验。TestClient timeout 的既有弃用警告不是业务失败。
- 全部验证使用合成 SQLite 数据与现有接缝；未访问目标、调用付费模型或用真实项目写入试错。Goal-driven Explore 用例不独立证明全部历史 structured Cell 兼容路径；不因此展开历史协议改造。

#### 部署生效：后端与 Dispatcher 已重载，正式前端重建未完成

- 部署前重新核对实际 `8001` 与 `127.0.0.1:55433/cairn_isolated`，不切到默认库。只读快照已观察到 `proj_032=paused`、generation 5、无 active 项目、无 queued／running TaskRun、无待派发报告；停止前与启动前均复核无待派发或在途工作，运行中的 Docker 容器只有 PostgreSQL。暂停／代次变化不是本实施会话操作，操作者未查明；本会话未调用 pause／resume／重跑接口。
- 按现有方式执行 `deploy/stop.sh`、`deploy/install.sh`、前端依赖安装和 `build:server`，再执行 `deploy/start.sh --host 0.0.0.0 --port 8001`。依赖同步与服务启动成功。只涉及 Server 服务代码，既有 `arena-worker:local` 镜像未重建，镜像 ID 仍为 `sha256:9b9aa6dd56452f101bf8baa82b12ea7540ae901912f1408c514ad81f61c23cde`。
- **正式前端构建失败，不能记为完整新构建成功。** 首次 `npm ci` 被杀，exit 137；停止服务后依赖安装成功，但 `build:server` 在 `vue-tsc -b` 的 512 MiB 堆限制下内存耗尽，exit 134；640 MiB 的一次重试仍被杀，exit 137。均未进入 Vite 打包，未关闭类型检查、增设 swap、结束其他会话或批准额外 install scripts。现以原有未改静态资源恢复完整服务；前端源码未改，但本轮没有新前端构建通过的证明。
- 部署后于 `2026-10-09T01:53:26Z` 核验：新 Server／Dispatcher 进程 cwd 与原实例一致，Server live env 仍指向原数据库；`/health`、UI 根路径和实际 `/static/assets/` 入口 JS 均 HTTP 200。Dispatcher 心跳新鲜且 idle。只读事务确认 active=0、queued／running=0、TaskRun 总数仍为 85、报告 finalization 记录为 0，`proj_032` 保持 paused／generation 5、Explore 消耗 31／40。服务恢复和代码重载不等于模型正常或真实功能验收。

#### 真实任务验收：未进行；恢复由用户决定

- `proj_032` 当前已暂停。用户决定真实执行和费用后，可走现有正常 resume 动作（`POST /projects/{project_id}/resume`）；当前 paused→active 使用已有 generation 5，不再增加一次代次。正常 active→pause 会增加代次，暂停时仍须等待在途执行物理停止；不把 pause／resume 作为以后每轮 Reason 的必要操作。
- generation 4 的 `tr060/tr061` 原期限 `2026-10-08T08:12:33.031612+00:00` 已过期，且不是 pause_interrupted；本修正不使其自动成为 generation 5 的合法续交付，不延长旧授权／deadline、不重开 failed Intent、不清零消耗。新代次可根据现有成果进入新规划，但仍受授权、期限、Worker／模型可用性、容器状态和收敛条件约束；名义剩余 9 个 Explore 名额不是保证可新增 9 次发现探索。
- resume 可能自动派发真实工作并产生模型费用，本会话未执行。未验收同代真实连续规划、真实原会话补交或漏洞发现效果；provider 根因、未提交原因、历史 artifact_changed 文件变化及早期 409 原因仍未知，仅报告，不展开下一轮。
- 当前分支 `docs/installation-guide`。本轮涉及三个服务源码、两个测试文件与本文；`docs/specs/installation-guide-design.md` 的用户既有改动未触碰。没有 commit／push。实施会话当时将正式前端重建与真实任务验收列为剩余事项；前者作为本轮交付门槛的判断已由下节纠正，后者仍待用户决定。本轮至此停止。

### 4GB 主机的构建与交付纠偏（2026-10-09；分析会话只读复核）

- 本次资源快照：物理内存约 3.8 GiB，可用约 865 MiB，没有 swap；根磁盘剩余约 43 GiB，`/tmp` 是 tmpfs。内核日志明确记录 global OOM，包括 `2026-10-08 12:23:32`（主机本地时间）的 `npm ci` 被杀；另有 MainThread 被杀记录，但未将所有进程名擅自认定为同一个构建。当前会话及已检查父级 cgroup 的 memory.max／memory.swap.max 均为 max。因而内存不足有实际证据，不是仅凭“机器只有 4GB”猜测。
- 实施记录中的 512 MiB Node 堆耗尽和系统 OOM 是不同层面的限制。继续降低堆上限会更早触及 V8 限制；直接把堆设成 4 GiB 也不会创造物理内存，可能让系统更早杀进程。不要在资源条件不变时反复执行相同构建、重装依赖或同时运行多个检查。
- **本轮后端修复不需要强制重建未改前端。** 定向复核三份服务源码 diff：重试／准入行为确有改变，但响应结构、前端所需请求字段及状态码／错误枚举表达仍沿用既有契约；没有需要新版前端配套的变化。前端源码、依赖、构建配置及已跟踪 static 均无本轮差异，现有入口文件仍在。因此，在原有前端产物完整可用的前提下可以继续复用它，而不是把一次无关的前端重编译作为这轮后端交付的阻塞项。
- 这纠正了此前交接中“完整系统构建”容易被理解为“每轮所有组件都重新编译”的表述：完整系统须可用，未改变且兼容的产物可以复用。保留此次正式前端构建失败的事实，不写成新构建通过；也不把复用原前端解释为可以跳过以后真实前端变更所需的类型检查。实施会话记录的后端重载／入口可达仍只是其部署记录，本次没有重跑验证或把它当作真实任务验收。
- **以后确实修改前端、产物缺失或出现接口不兼容时再构建。** 4GB 主机可先由用户关闭不用的桌面程序或额外会话；经用户同意后，在本地磁盘启用约 4 GiB 临时 swap，再串行运行现有 `build:server`。Node old-space 可从 1536 MiB 作为尝试起点，不保证任何版本都能在此上限完成；根据真实 heap/OOM 证据再调整，不取消 `vue-tsc`，不新增依赖或改写构建脚本来制造成功。
- 临时 swap 不放在 tmpfs 的 `/tmp`，不未经同意修改 `/etc/fstab`，不覆盖已有文件或强杀其他会话／服务。撤销 swap 前需确认有足够内存，不在仍吃紧时强行 swapoff。长期经常构建可考虑增加物理内存，或在内存更充足的机器构建同版本产物；swap 是缓解峰值的办法，不保证速度或成功率。
- 本次只补充上述方案判断：未启用 swap、安装依赖、运行构建／测试、启停服务或操作项目；未完整验收业务修复，不新增“为证明复用而重跑全套检查”的任务。后续仍按用户授权核对实际连续推进效果。

### provider_error 原因追查（2026-10-09；只读取证，未修复）

本节补充前文“provider 根因未知”的范围：现已从保留的原会话记录取得两次 SDK 错误，但尚未定位全部底层原因，不将分类标签当成具体原因，也不将前轮重试修复说成上游故障已修复。

- 重新确认 `8001` 服务 cwd 为本仓库、数据库为 `127.0.0.1:55433/cairn_isolated`；只读查询时 `proj_032` 为 paused／generation 5。该项目持久化为 `provider_error` 的 TaskRun 恰有两条：历史 generation 3 的 `tr050` 与 generation 4 的 `tr061`；这是 TaskRun 分类计数，不代表全部 SDK 内部调用次数。
- **`tr050`：调用异常终止，具体来源仍未知。** 对应原 session JSONL 第 53 行于 `2026-10-08T07:03:48.530Z` 记录 `errorMessage=terminated`。没有 HTTP 状态或上游错误码；仅凭此词不能认定为余额不足、认证失败、用户取消、网络断开或本机 OOM。
- **`tr061`：SDK 的流式事件解析失败。** 对应原 session JSONL 第 25 行于 `2026-10-08T08:00:42.794Z` 记录 `stopReason=error`；错误涉及 Anthropic SSE 的 `content_block_delta`，具体异常为 `Cannot read properties of null (reading 'type')`，SDK 错误说明中事件 payload 为 null。记录没有 HTTP 状态。可以确认解析端遇到 null，但不能据此确认 null 必然是供应商线路上的原始数据，或判定问题一定来自供应商、代理转换或 SDK 的哪一层。
- **解析位置与留存边界已定位。** 历史运行容器镜像层内的 SDK `anthropic.js:258–269` 对 `sse.data` 执行 `parseJsonWithRepair` 后直接读取 `event.type`，没有先排除 null；`:509–518` 的 catch 仅将 `error.message` 保存为 `output.errorMessage`，没有保留 cause／stack。`agent-session.js:298–308` 随后完整保存消息。因此 `tr061` 的解析异常可以定位，而 `tr050` 的 `terminated` 尚无更深原因可据此判断。
- **原始错误仍在，不是全平台丢失。** 原件来自已退出 Worker 的已有 session 文件，只读解析，未启动容器、执行容器代码或拷贝完整 session。会话按各 TaskRun 的既有冻结归属定位；不将 Prompt、消息正文或凭据转储进方案。
- **没有证据将这两次错误归因于本机 4GB OOM。** 两次 `command_finished` 均为退出码 1，timed_out／cancelled／oom_killed 均为 false。容器后来在 `09:11:28Z` 以 143 退出、OOMKilled=false，不能用这个后续退出解释 `08:00Z` 的 SDK 错误；这些字段也不证明主机历史从未发生 OOM，须与前端构建的 OOM 证据分开。
- **面向排障的错误信息被中途泛化。** `container/pi-agent/src/runner.ts` 的分类函数先按认证／限流等文字判断，其余归为 `provider_error` 并生成固定说明；`tr061` 的 `a388-reason_execute-pi-events.jsonl:2` 只留下 `Provider stream failed or returned an error`。`cairn/src/cairn/dispatcher/worker_result_gateway.py` 又将其映射为固定中文提示。因此页面、审计摘要里的同一个标签掩盖了两种不同错误；它也不是“已经证明可恢复的服务端瞬态故障”的充分证据。
- 后续如实施，先沿现有错误生产者、传递路径与消费者补齐可追溯的具体原因，保持重试分类与诊断原因分离；遵守敏感信息访问／输出边界，不直接公开整段 SDK 响应、不改写权威 session，也不另建错误平台。流式解析修复须沿已定位的 SDK 路径进一步确认 null 的产生方式与正确处理语义，不能盲目吞掉 null 事件、假装成功、加大重试次数或未经批准升级依赖。`terminated` 没有进一步证据时保留未知。
- 本次只读查证并更新原方案，未修改业务代码、运行测试、调用模型、访问目标或操作项目。前轮连续推进修复的真实任务验收仍未完成；本节不构成下一轮业务实施或额外费用授权。

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
