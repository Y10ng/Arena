# 项目 Agent 会话、能力扩展与平台集成设计

> 更新日期：2026-10-30。**当前验收结论：同题新实例的目标绑定符合第 22 节限定实施预期；证据阅读本地交付已收尾，真实复测仍未执行。** 静态核对未发现目标绑定路径的确定性阻断，定向受控测试与部署结果见第 22.6–22.7 节；不能将这些证据称为模型实际执行通过，也没有上游 API 拦截已解除的证据。此前复测组件的 7 项失败已逐项归因并仅修正测试，完整文件 40 项通过；既有报告只读页面验收、覆盖边界及未验证项见第 23.5 节，本轮未构建部署。此前第 20 节携带附件创建暂停项目的真实闭环保持已通过；不重做回执恢复或合成项目创建。
>
> 工作模式遵循 [分析、设计与实施交接](../analysis-design-handoff.md)：分析会话查证并收敛范围、按用户要求更新规范，业务代码留给新实施会话。**第 15.8 节为历史有限验收；完整产品目标见第 16 节，恢复实施见第 19.6–19.10 节，第 20.1–20.4 节保留验收前交接，第 20.5–20.8 节记录创建项目真实业务验收，第 21 节复测仅完成前置检查；同题更换实例地址的实现与受控验证见第 22 节，最新复核和交付收尾见第 23 节。** 不把路线图当作当前分析会话运行研究、创建复测或修改业务代码的授权。
>
> 历史 R2a 的只读工具与连续会话曾在 localhost 入口通过，真实复测／综合报告模型链仍未验收。用户本轮的独立 Agent 需求不能被旧 R2a／R2b 标签、七个现有工具或“不做附件”的历史范围排除；先按第 16 节实现独立交互与业务能力，不默认加入主动接管研究 Worker 或外部平台发布。长期视觉规则已同步纠正到 `AGENT.md` 与 `docs/ui-design-standard.md`：共享维护不冻结旧风格，最大化窗口为主，不要求缩小窄窗验收。

## 1. 产品目标与方案归属

### 1.1 用户明确提出的目标

- 能在项目／任务工作环境与 Worker Agent 自然对话，而不只是提交固定表单或观看日志。
- 可以说“当前进展如何”“帮我复测某个漏洞”“生成漏洞报告”，Agent 按项目真实上下文选择工具、完成工作并解释结果。
- 后续能向其他平台发布发现，每个漏洞有稳定编号，并关联外部编号及后续复测。
- 能力应便于增加和适配；允许替换当前不适合的执行模式，不以维护旧架构为前提。
- 自主探索、界面操作、对话和外部调用复用同一执行内核及业务能力，不再分别实现报告／复测。

这不是聊天装饰，也不只是给旧函数换 tool 名称。核心改变是：**从主要围绕固定 phase 运行并提交一次结果的 Worker，演进为支持持续交互、可调用业务能力、可恢复工作的项目 Agent。** 实际漏洞发现能力仍是核心；不为聊天框新增层级指挥 Agent，不让所有确定性操作都付费经过模型。

### 1.2 与旧方案的关系

本文件是该新产品主线的唯一方案。此前没有覆盖持久项目对话、能力调用和跨平台发布的独立方案；因此不把它继续追加为漏洞阅读器局部修复的一轮，也不复制已有方案。

- [漏洞发现、关键证据与独立复测重设方案](vulnerability-evidence-retest-redesign.md) 保留发现／证据／复测／报告的事实归属和历史实施记录。其 15.27.7 中“扩展属于未来、本轮不做”的表述仅属于截图那一轮；本次具体需求已成立，不再据此排除会话和能力改造。
- [项目详情展示方案](project-workspace-ui-design.md) 的旧五标签取舍不禁止新增本次明确要求的 Agent 入口；既有页面和深链接不因新增对话而删除。
- [Dispatcher 方案](dispatcher-design.md) 与 [Server 协议](server-protocol.md) 只在实现落定后更新实际合同，不提前将本设计写成已支持能力。
- 全局标准继续以 `AGENT.md`、`CLAUDE.md` 和 [UI 设计规范](../ui-design-standard.md) 为准，不修改全局规范来扩大本轮权限。

## 2. 已确认事实、现场基线与未知

### 2.1 当前实现

以下为 2026-10-28 当前工作区查证；行号是定位线索，实施前核对最新源码。源码支持与实际运行验证分开。

| 已确认事实 | 依据 | 设计影响 |
| --- | --- | --- |
| CLI 接收一个私有 request 文件，调用 runner 后输出结果并退出 | `container/pi-agent/src/cli.ts:27–36` | 没有可直接使用的用户多轮输入通道 |
| runner 使用 SDK session、prompt、事件订阅；内部已使用 steer 发截止提示 | `container/pi-agent/src/runner.ts:267,448–456,526,630–680` | 可以复用 SDK，不需要新 Agent Loop；用户消息／干预协议仍需实现 |
| 没有合法业务候选会被判为失败，最后 dispose session | `container/pi-agent/src/runner.ts:665–680` | 普通聊天回答需要独立的完成语义，不能伪造漏洞提交才能结束一轮 |
| session 有单写者锁、持久记录及恢复校验，绑定 phase family | `container/pi-agent/src/session.ts:51–112` | 保留归属和单写者，不能把各种旧 phase session 直接当通用聊天恢复 |
| skills/extensions loader 返回空集合 | `container/pi-agent/src/runner.ts:132–142` | 放 SKILL.md 或装 npm 包不等于已启用能力 |
| 生产工具按 capability/phase 显式注册 | `container/pi-agent/src/runner.ts:278–435`、`container/pi-agent/src/protocol.ts` | 应收敛装配位置，不继续在多处添加相同业务分支 |
| 容器按 project_id 命名和归属，多次执行与项目环境关联 | `cairn/src/cairn/dispatcher/runtime/containers.py:631` | 用户说的“任务容器”不能直接当作长期会话身份 |
| 发现已有说明、步骤、建议、证据的结构化产出；单漏洞直接呈现和导出 | `dispatcher/worker_protocol/submission.py:60–94`、`server/services/vulnerability_reports.py:220–293,328`（均位于 `cairn/src/cairn/`） | 不再建第二份权威漏洞正文 |
| 独立复测已复用 runner、专用指引和独立结果接受路径 | `cairn/src/cairn/dispatcher/tasks/external_verification.py`、`dispatcher/prompts/external-verification/agent.md` | 抽出能力入口，保留本轮证据与历史输入区别 |
| 外部集成目前面向复测，已有 integration principal 和 external_id | `cairn/src/cairn/server/routers/verification_requests.py`、`server/services/verification_requests.py` | 不等于通用出站漏洞同步已经存在 |
| 漏洞 vNNN 在项目内计数 | `cairn/src/cairn/server/services/ids.py:13–32,51–52` | 对外不得仅使用 v001 识别漏洞 |
| 全局指引要求复测 v3，专用指引及实际新结果为 v4 | `container/AGENTS.md:10`、`cairn/src/cairn/dispatcher/prompts/external-verification/agent.md:15` | 指令来源需收敛，协议字段以实际 Schema 为准 |

现有自然对话路由未在本轮检查的 router／runner 路径发现；不据此宣称 SDK 本身不支持。SDK 具体资源装载和取消 API 以已锁定版本为准，不引用未核验的新版本示例。

### 2.2 proj_060 的实际读取基线

本会话在用户明确启用分析设计模式之前，已完成以下只读运行诊断；启用模式后不重复运行 profile、测试或业务动作。

- 实际入口为当时的本地 `127.0.0.1:8001`；从运行进程配置核对活动数据库为 PostgreSQL，端口 55433、数据库 `cairn_isolated`。这些是当时定位信息，不是固定部署参数；不记录连接串或凭据。
- `GET /projects/proj_060/vulnerabilities/v001` 返回 200，耗时 52.882 秒。
- 实际数据库只读事务中的同函数 profile 耗时 50.532 秒，其中 `_artifact_allowed` 26 次累计 48.895 秒；`artifact_is_internal_file` 被调用 136,415 次。
- 项目有 21,009 条 Artifact、10,487 条 flow；Artifact 内容共 5,314,026 字节。其中 10,487 条 Artifact 内容为空且相同；另有两组相同内容分别超过 5,000 条。
- `_artifact_allowed` 按 project/sha256/size 获取所有相同内容记录并逐一解析 metadata，放大了重复空请求体的读取成本。详情中的 `_key_evidence → freeze_report_flows` 重复调用该检查。
- `tr013`、`tr014`、`tr015` 都以 `report_packet_read_failed` 失败，错误为“权威 Report Packet 读取失败：read_timeout”。代码在启动模型之前读取 Packet，失败即返回。
- `build_assessment_report_packet` 复用相同材料检查、收集全项目 Artifact，并在构建开始取得项目写锁。该代码解释了潜在的放大机制；**本轮没有重新执行完整 Packet 生成，也没有据此证明所有历史写超时／死锁都由这一处引起**。

相关代码：`server/services/verification_requests.py:300–329`、`vulnerability_reports.py:125–249`、`flow_evidence.py:483–518`、`assessment_reports.py:648–651,1966–2120`，以及 `dispatcher/tasks/report.py:1463–1471`（均位于 `cairn/src/cairn/`）。

首因已经足够明确，不再要求靠新建付费项目来发现相同问题。增加模型预算、超时和重试，或取消内部材料保护，都不是修复方案。

### 2.3 尚未确定的事项

- 第一个外部平台、其认证／字段／附件／幂等能力及是否需要回写尚未指定；不影响 R1，不凭空实现连接器。
- 原生 pi 扩展装载的完整版本细节、繁忙 session 的 follow-up/steer 边界及取消对子进程的实际效果，需要实施时局部核对；已有 steer 调用不证明完整用户干预可用。
- 本轮没有验证真实模型自由对话、自动选工具、实时干预或容器重建后的交互恢复；这些是待实施目标。
- 当前项目综合报告仍失败；性能修正的效果、完整 Packet 行为及新报告质量分别验收，不用单漏洞快了代替总报告成功。

## 3. 推荐架构与职责归属

### 3.1 项目、会话、执行、容器

| 概念 | 职责与权威来源 | 不承担什么 |
| --- | --- | --- |
| Project | 背景、Goal、真实范围、共享事实和成果 | 不把聊天文本变成第二套项目状态 |
| Conversation | 用户可见的持续对话、所属项目与 Worker 选择、输入顺序 | 不与容器 ID 或单个业务阶段同义 |
| TaskRun／执行 | 一次实际工作、资源与费用、调用来源、完成／失败／取消事实 | 不因每次工具读取都新建一套子任务 |
| SDK session | 模型交互及恢复所需的执行记录 | 不独立编辑项目事实、报告或复测结论 |
| Container | 可分配、回收和重建的执行环境 | 不成为会话、漏洞或历史成果的永久身份 |

会话属于一个明确项目，多个会话可以共享该项目事实；没有必要将所有原始日志塞入每个会话。平台根据既有对象的访问权限提供内容，不能仅凭知道编号访问其他项目／复测调用方材料。

进程按需启动、在一次执行期间驻留，轮次结束可以释放；会话和成果必须持久。无需为了连续对话让模型或每个容器永久运行。容器重建只能恢复已持久化的环境信息，不声称恢复丢失的浏览器登录、内存变量或临时后台进程；确实缺前提时明确说明。

### 3.2 同一个执行内核，不让所有入口都经过模型

推荐保留 Python 侧业务服务和调度、TypeScript pi 执行核心；不切语言、不引入新 Agent 引擎或第三层指挥 Agent。

- runner 的共同部分负责模型/session、工具执行、事件、取消、材料留存和用量。
- 批处理任务仍有自己的业务结果消费者；聊天轮次允许正常自然语言结束，不强制调用 `submit_result`。
- 对话中发布发现／完成复测／生成报告，仍调用相应业务接受路径，不能把自然语言回答自动当成已持久化成果。
- 同一会话在业务工具返回后可以继续交流，不能让一次发现提交无条件终结整段会话。
- 页面按钮和外部 API 可直接调用确定性业务入口；聊天由模型选择同样入口。后台调度不必先给聊天 Agent 发一句自然语言。
- 普通探索、独立复测、总报告保留实际需要的操作边界和结果校验，不把固定 phase 名当作所有能力必须共享的控制骨架。

R1 允许两个调用入口（现有任务入口与交互轮次入口），但必须落在同一 SDK 执行核心，不能复制第二份 Agent Loop 后长期维护。

### 3.3 持久消息与断线恢复

只增加真实交互所需的持久内容：会话归属、客户端请求身份、消息顺序、已接受输入、对外回答／动作引用及对应执行。优先复用 TaskRun、事件和 Artifact，不建立另一个 Job/Operation 状态系统。

- 输入由服务端受理后分配顺序，并以客户端消息 ID 去重；重连或重发不能重复计费执行。
- 用户可见消息是服务端可查询的对话记录；SDK transcript 是关联该轮输入和执行的恢复日志，不是另一套允许独立修改的聊天事实。必要的显示投影可重建，不复制业务结果正文及状态作为新权威。
- delta 可以流式传输；不要求每个 token 单独落库。最终回答、已接受输入及动作回执必须可恢复。内部 prompt、密钥、私有推理和原始工具日志不直接作为聊天事件公开。
- 消息受理、开始执行、回答完成、业务结果接受相互区别；前端不能把发消息成功显示成任务完成。
- 页面断线不代表任务取消。重连先补历史／事件游标，不能重放整个有副作用的用户请求。
- 不知道一次外部动作是否完成时核对其业务回执；不能以恢复对话为由自动重复复测、发布或目标写操作。

传输推荐普通鉴权 HTTP 写入＋同源 SSE 输出（断线后按消息／事件游标补读）。是否复用已有事件接口由当前实现决定，不为聊天引入 WebSocket 依赖、独立消息中间件或给每个容器开放公网端口。服务端只消费 runner 明确发布的事件，不轮询猜测私有日志尾部。

## 4. 能力与扩展的最小组织方式

### 4.1 Tool、Skill 与能力模块

- **Tool**：明确输入和返回值的动作。读取、同步导出可直接返回；耗时业务返回现有执行／请求引用，不伪称完成。
- **Skill／Markdown**：方法、写作规范与示例，按实际任务装载，不代替持久化、授权和结果接受。
- **能力模块／extension**：组合相关工具、可信指引和必要资源，通过当前 pi SDK 可用接点加载。只维护实际装配需要的声明，不建设插件市场、服务发现或通用安装平台。

首批以仓库内可信模块实现，先跑通内置能力的真实注册和消费。允许的模块由实际 Worker／执行选择明确决定，不能自动将上传资料中的 AGENTS.md、SKILL.md 或源码当扩展执行。这里保护指令来源，不是给正常研究增加固定工具白名单或逐次审批。

工具参数 Schema、确定性检查及业务实现只有一处。全局指引不再手写可能过期的所有协议版本；能力方法保留一份来源，阶段只保留必要差异。更新对后续轮次生效，正在执行的轮次记录其实际能力版本，不热换在途行为；不因旧 session binding 将未来配置永久冻结。

### 4.2 第一批实际调用方

以下是能力语义，不预先冻结新 API 名称；具体名称沿现有代码风格。

| 能力 | 输入／输出要点 | 复用边界 |
| --- | --- | --- |
| 查询项目进度 | 真实工作、已保存成果、阻塞和数据时间 | 调用现有读模型，不让模型按旧聊天猜状态 |
| 查找／读取漏洞 | project + vulnerability，可显式选 Observation；返回正文和按需证据引用 | 复用 Fact/Observation，不全量扫日志 |
| 导出单漏洞报告 | 选定发现、既有模板及格式；返回真实成品引用或明确错误 | 确定性导出，不再次调用模型整理 |
| 生成项目综合报告 | 模板、材料范围及报告执行引用 | 复用报告业务，模型负责必要综合，不重写全部漏洞 |
| 复现／复测 | 所选原主张、本轮条件与真实范围；返回复测请求／结果引用 | 复用 VerificationRequest 与本轮证据接受，不另造复测库 |
| 后续工作／执行控制 | 指定会话或具体运行，保存输入／请求；返回受理与实际执行状态 | 复用调度与 TaskRun，不直接把 UI 状态改成停止 |
| 向平台发布 | 具体连接器、漏洞及版本／材料范围；返回外部回执 | R3 才实现第一个真实连接器 |

业务请求中的项目身份、用户身份、实际 TaskRun 等由运行上下文绑定，模型只选择业务对象，不手抄或伪造执行归属。外部 integration 的权限仍独立核对，聊天入口不自动把它升级成用户权限。

### 4.3 报告、发现和复测的内容所有权

- 发现说明／步骤／建议在发现时形成，继续归 Fact/Observation；可及时留存观察和中间成果，不强制每个工具调用写长报告。
- 单漏洞报告呈现该次发现；已有材料不因为模型不可用而不可读。不同 Observation 不补成一次不存在的实验。
- 综合报告是选定成果的派生交付物；冻结实际引用的内容版本，模型处理总结和建议优先级。材料索引不能默认携带所有无关 Artifact。
- 独立复测有自己的执行与证据归属。聊天可以发起和跟踪它，但不能把聊天历史或原成功报文注册为本轮验证；需要独立 session 时复用当前机制，不为每个漏洞新建一个层级 Agent 组织。
- 不把原始输入裁剪、摘要或改写为新的权威事实。分页和展示投影保留完整原件入口与来源；内部秘密和不可交付数据在访问／输出边界阻断。

## 5. 工作进行时的对话与资源边界

推荐区分行为而非要求用户选择复杂模式：

1. **查询**：从项目／运行读模型回答，不中断工作。会话忙时可以先展示确定性状态，正常模型回复遵循消息队列；不承诺同一个 SDK session 能同时推理两条消息。
2. **后续要求**：按顺序保存，在当前轮次结束或适当边界消费；界面能看见待处理消息，不静默丢弃或抢占。
3. **改变当前工作**：对明确的执行发送控制输入，实施 R2 时核对 SDK 可用 API，并显示“已受理／已应用／未能应用”。不能把进入队列冒称 Agent 已改变方向。
4. **停止**：向执行器发取消，观察当前模型与工具／子进程实际退出；无法确认则保留未确认状态。停止不能撤销已经发出的目标写请求，清理行为只针对本次拥有的资源。

同一 session 只有一个写入者；不同 session／TaskRun 可在现有资源边界内并行。不能因一个聊天窗口忙就全局锁住项目。对可能互相破坏的同一工作目录、浏览器会话或文件采取具体局部协调，不一律串行化研究。

一次报告操作只读取既有证据，不顺手访问目标；下一轮明确的复测可以在真实授权范围内使用研究工具。交互会话不获得任意跨项目访问或无限费用授权，也不因旧报告只读 phase 而永远不能执行新工作。既有配置和必要预算按当前执行生效；跨范围、外发或额外费用需要具体授权，正常内部步骤不重复审批。

## 6. 漏洞编号与外部平台集成

### 6.1 身份不等于展示编号

机器识别必须包含稳定实例／项目／漏洞身份，不能只发送项目内 `v001`。R1 在聊天里使用明确的项目内编号及链接；遇到歧义提供实际候选，不猜另一个项目。

R3 优先复用现有主键组合并加稳定实例身份，不为好看先重编号全库。确需用户友好编号时，它只是唯一展示别名；报告版本、Observation、复测结果各自关联同一漏洞，而非每次生成报告创建一个新漏洞。

### 6.2 一个真实连接器，而非通用同步平台

Agent 选择对象与平台，连接器负责字段映射、认证、附件、创建／更新和外部回执。凭据留在服务端／受控执行配置，不送入模型。先明确平台和实际发布字段，再实现最小调用链，不预建无消费方的 webhook 总线、映射 DSL 或连接器市场。

保存必要关联：Arena 漏洞身份、连接器身份、外部编号／链接、本次发布内容版本／摘要及实际结果。技术发现由 Arena 负责，外部工单分派和修复流程由对方负责；复测结论不能被外部“已修复”状态直接覆盖。

支持对方提供的幂等键／查询关联。响应丢失时可能已创建成功，应查询回执或关联后决定是否重试；不支持可靠查询时明确“结果未确认”，不能自动重复创建，也不承诺普遍的 exactly-once。默认先做显式单向发布；只有用户明确需要时才接回写和修复后复测，并防止自身更新互相触发。

首次确定目的平台和材料范围；后续按已保存且明确的发布规则执行，不逐附件反复审批。新的外发范围或目的地需确认。不把全部原始证据、内部日志或凭据随报告默认上传。

## 7. UI 与实际使用路径

- 项目工作区提供明确的 Agent 入口。现有页签、会话选择器、布局和卡片不是新设计的兼容目标，可以在本轮范围内重做；保留图谱、运行、漏洞、产物等主要业务能力及必要访问／深链接，不把删除页面入口当作简化功能。共享视觉能力可调整而非永久冻结，不在单页另造主题，也不扩展为无关全站改版。
- 默认展示当前会话、输入区、简洁执行状态和成果链接。原始诊断按需展开，不把工具日志当聊天正文，也不显示私有推理／系统 prompt。
- 漏洞／运行页可将明确对象带入对话。选择具体任务时显示来源，不能静默将任意旧 phase 的 transcript 改成聊天。
- 用户发送普通问句，模型可以正常回答；查实际进度、漏洞和报告时有真实工具调用，不用关键词 if/else 冒充 Agent 理解。
- 耗时操作返回可点击的执行状态，聊天可继续查看；后台失败有明确原因，不靠永久 loading 或模型自报成功掩盖。
- R1 只暴露已经贯通的能力。询问“帮我复测”时如尚未接入，应明确说明并指向已有业务入口，不能返回虚构任务；这仅是分轮交付边界，不作为最终功能设计。

## 8. 演进与切换策略

保留有效的数据来源、业务接受、已有材料和必要可靠性；替换造成当前需求阻碍的执行封装。不是全仓重写，也不是永远维护两个同义系统。

- 新会话显式创建新的交互 session。旧任务 session 仍按其真实用途读取／恢复，不自动改 phase family，不伪造 continuation。
- 现有批处理和对话共用 runner 核心及能力实现；修改共同内核时贯通当前消费者，不新增静默 fallback 或旧协议猜测分支。
- 只新增真实持久会话、消息受理和关联所需 PostgreSQL 结构。Schema 版本以实施时实际数据库为准，不硬编码历史 migration 序号；不补 SQLite 支持。
- 已接受发现、复测、报告和材料不批量改写。需要明确数据迁移时直接迁移并删除被替代的新写路径，不长时间双写同一事实。
- 不默认迁移或中断活动 TaskRun。实施前检查真实实例；若部署会影响正在运行的工作，说明影响并取得针对性授权，不利用旧“当时空闲”记录直接重启。
- Worker 只有应用／指引变化时复用未变工具及依赖镜像层，不重新下载工具链。

## 9. 分轮范围与下一步

### R1：持久项目对话与只读成果工具闭环（已有实施记录，真实模型待验收）

**用户可见结果：**项目中能持续聊天；Agent 能查询真实进展、查找／读取漏洞并导出已有单漏洞报告，刷新后保留消息和成果关联。对话不是固定命令解析器，也不是假工具演示。

完整范围：

1. 贯通最小会话／消息持久化、认证 API、交互轮次与流式／补读消息、项目 Agent 页面；消息去重和同 session 排队直接解决重连重复执行。
2. 在现有 runner 提取共同 SDK 执行核心，允许聊天自然结束；保留现有批处理业务结果接受语义，贯通实际 adapter／request／事件消费者。不复制一个 chat-only Agent Loop。
3. 以可信能力模块接入项目状态、漏洞查询／读取、单漏洞报告导出，调用已有服务。工具 Schema 和方法指引单一维护；只处理本轮真实装载与消费者，不预建任意第三方包安装系统。
4. 同时修正这些工具使用的重复材料检查：不再为每个证据获取全部同内容 Artifact 并反复解析。按实际实现选择查询存在性／批量来源判断和请求内复用，保持受保护材料副本判定、凭据边界、摘要／来源校验；不使用跨请求秘密缓存或直接关闭保护。
5. 阅读正文与按需证据的职责明确。优先利用上一步直接消除已知慢点；如果同步完整证据仍阻塞正文，再贯通必要的按需读取 API／UI，而非无收益地预先增加整套缓存系统。
6. 导出返回用户可下载的实际成品或已有合法下载引用；不由模型编造链接、不强制再跑报告模型、不扩大现有下载权限。

本轮不做：目标访问／新探索／复测发起工具、对活动研究的实时 steer、外部发布连接器、全量报告 Packet 重设、全站布局调整、任意插件市场。未实现能力不假称可用；但代码组织不能依赖“永远只读”而阻止 R2 扩展。共享材料优化可能改善 Packet，只能按已验证范围报告收益。

R1 是第一段实际交互路径，不是全目标结项。总报告重处理／长锁与实时执行控制继续归 R2，不能只因单漏洞变快就声称 `proj_060` 三次报告失败已解决。

### R2：耗时业务能力、执行控制及综合报告

在核对 R1 后，用新实施会话贯通：

- 对话发起已有复测业务、返回请求引用、观察结果并关联原漏洞；本轮证据／历史输入严格分开。
- 对话触发综合报告，按选定成果版本收集材料；移除非必要全项目聚合，将短事务确定身份／引用与耗时内容准备分开，保持一致输入及真实接受回执。
- 同一执行内补充要求、排队、取消与必要 steer；实际工具／子进程停止和不确定状态有可观察证据。
- 自主探索与交互共用工具和执行内核，用户给新方向时更新实际工作，不仅回复“好的”；保留基于反馈选择研究方法的自由。
- 如需生成进行中项目的阶段报告，明确其未结束状态和实际材料范围，不为了出报告强行结束项目，也不把阶段成果叫最终完成。具体行为在 R2 开始前按用户使用反馈收敛。

2026-10-28 已按用户要求收敛下一轮：**R2a 先完成复测／总报告发起、状态／成果跟踪及相关 Agent 界面重做，见第 13 节；R2b 再贯通活动研究的用户输入、steer 与执行控制。** R2a 不依赖先做实时干预，不自动展开 R2b。R1 真实模型验收仍保留未知，不把它变成要求用户先付费重跑旧项目的前置门槛。

### R3：第一个外部平台闭环

用户确定具体平台及调用范围后，按第 6 节实现一个连接器、编号关联、幂等／结果未确认处理及工具／页面共同入口。是否双向回写和修复后自动复测，按实际需求决定。没有具体平台前不阻塞 R1/R2，也不生成“已接入平台”的占位实现。

## 10. 验证与验收边界

以下是行为目标，不要求每条另写测试或跑整套套餐；实施者选最直接的现有验证路径，并区分真实与受控执行。

### 10.1 R1 必须能说明的行为

- 普通对话可以正常结束；查询状态、选定漏洞和导出报告经真实工具／业务服务返回，不调用目标。
- 消息重复提交、页面断线后重连不创建第二次相同执行；消息历史、成果链接和未完成状态可找回。
- 同 session 两条输入按顺序消费，不并发破坏 transcript；不因此全局锁项目。
- 仅在现有来源和权限范围内读取，普通对话不公开 prompt／密钥／内部日志；模拟敏感标记验证输出边界，不复制真实秘密到测试。
- 对 proj_060 已确认慢点做一次针对性比较，核对材料读取不再对每个空体重复装载上万条同内容记录；总耗时、查询／解析次数按实测报告，不仅加一个索引就宣称解决。
- 导出文件／下载引用确实对应选定漏洞与 Observation，原件不变；真实页面可读消息、滚动、打开成果，状态不假报成功。
- 共同 runner 改动用一个代表性现有任务合同作针对性非付费检查，防止将聊天结束语义错误套到必须提交业务结果的任务；不默认复跑所有阶段或全测试目录。

### 10.2 执行权限与交付

分析会话只更新文档，不运行测试、构建、启停、创建业务请求或模型。

新实施会话可采用现有非付费受控入口验证确定性链路；受控 provider 只能证明协议和消费，不能证明真实自然语言选工具质量。真实模型对话、复测、报告生成、目标访问与外部推送须具有当轮明确目标和费用／外发授权；没有授权就如实标为待验收，不借受控结果宣称通过。

运行代码改动由实施会话按现有部署方式构建并启动完整系统。前端、后端、Dispatcher 及依赖可用；改了容器内代码再更新对应镜像并核对后续执行使用。活动任务可能受影响时先处理具体部署边界，不擅自中断／重跑。新依赖先批准；不默认全量测试／lint／浏览器矩阵，不新增通用验收平台。

## 11. R1 实施提示词（历史，已执行，不整轮重跑）

```text
这是新开的实施会话，只负责项目 Agent 主线的 R1，不接续其他历史待办。
遵循 CLAUDE.md、AGENT.md；你可以修改本轮业务代码，不继承分析会话只读角色。

先读 docs/specs/project-agent-capabilities-design.md，重点第 2–5、7–10 节和 9.R1；
其他轮次只是方向，不是本轮实施授权。UI 遵循 docs/ui-design-standard.md。
旧漏洞方案 15.18/15.27 仅用于核对已有发现、报告、复测与证据归属，不重跑旧提示词。

先执行 git status --short 和 git branch --show-current。分析时为 feat/update-worker-cache，
以现场为准，工作区有大量已有改动；不要整文件覆盖、回退、提交或混入别人的修改。
核对当前代码和实际实例，文档旧 PID、端口、行号及实施记录都不是永久事实。
用几句话说明本轮用户可见闭环与不改范围，然后在本轮范围内实现。

目标：项目里有可持续对话，能查询真实进展、查找/读取漏洞、导出已有单漏洞报告；
刷新/断线后消息及成果可找回，普通对话无需伪造 submit_result 才能结束。
不是只加聊天 UI，也不另造一套 Agent Loop、任务系统或漏洞正文。

实现范围：
1. 最小持久会话/消息、鉴权 API、消息去重与顺序、交互执行和事件传输/补读、项目 Agent UI。
   优先 HTTP 输入 + SSE 输出，不给每个容器开放新外部端口，不引消息中间件。
2. 复用 pi SDK 与现有 runner 核心；区分普通回答结束和业务结果接受。
   新交互 session 显式创建，不把旧 phase session 直接改用途；保留单写者及恢复归属。
   现有任务入口与交互入口共用核心，不复制 chat-only loop；贯通 Python adapter 和 TS 协议。
3. 可信能力模块注册项目状态、漏洞查询/读取、单漏洞报告导出，真实调用既有服务。
   页面/工具共用业务实现，Schema 与指引只有一个维护来源；不自动装载资料里的 SKILL.md。
4. 修正同链的已确认材料读取放大：proj_060/v001 实测 GET 52.882s，
   只读 profile 50.532s，其中 _artifact_allowed 26 次约48.895s，
   artifact_is_internal_file 被调用136415次；项目有10487条相同空内容Artifact。
   定位 verification_requests.py:_artifact_allowed 与漏洞正文的 freeze_report_flows 调用，
   不再每项获取所有同内容记录重复解析；保留保护来源、凭据、摘要与证据归属检查。
   不通过关闭保护、加超时/重试或跨请求缓存秘密解决；按实际需要贯通正文/证据按需读取。
5. 单漏洞导出返回真实文件或合法下载引用，复用已有模板/格式，不另跑模型重新写漏洞。

不做：复测/探索执行工具、活动研究的实时 steer、外部平台连接器、
全量Report Packet重设、任意插件安装市场、全站改版、SQLite支持、无关工具链重建。
这些属于后续轮次，UI不冒充已经实现；本轮也不能设计成永远无法扩展的固定命令机器人。

关键依据：
- container/pi-agent/src/cli.ts、runner.ts、session.ts、protocol.ts；
- cairn/src/cairn/dispatcher/workers/adapters/pi.py 与已有任务/事件消费者；
- cairn/src/cairn/server/services/vulnerabilities.py、vulnerability_reports.py、
  verification_requests.py、flow_evidence.py；
- cairn/frontend/src/features/projects/ProjectWorkspace.vue、现有路由/API/阅读器。
本次报告 tr013–tr015 失败发生在 Packet 读取、未进入模型；
不要将 R1 的详情优化误报为所有综合报告问题已经修复。

验证选最直接的局部路径：持久对话/真实工具服务/导出闭环，重复消息和断线恢复、
一个代表性现有任务接受语义、已确认详情慢点的针对性比较和受影响实际页面。
优先现有非付费受控验证，不默认整套测试/lint/浏览器矩阵，不新建验收平台。
若增加依赖先取得批准。真实模型对话、目标访问、复测、生成新综合报告和外部推送，
没有当轮明确范围和费用授权不得自动执行；未验证的如实记录。

代码改动后按实际部署方式构建并启动完整系统；核对活动任务，可能中断时先说明影响。
改了容器应用才更新Worker镜像，复用未变工具/依赖层，不重下载工具，不重建旧项目来验收。
不清库、改写历史材料、自动重跑项目、commit或push。

回填本方案第12节：代码实现、非付费验证、部署加载、真实模型验收分开记录，
写明本轮修改文件、实际检查结果与剩余事项，不把既有工作区改动算成本轮。
完成R1后停止，额外机会只记录，用户会带回分析会话再确定R2。
```

## 12. 实施与分析回填

### 2026-10-28：分析设计落文

- 已建立本方案，明确总体架构、能力边界、会话／执行关系、外部身份与分轮范围。
- 已在原漏洞重设方案加入本主线入口，旧截图轮的“未来扩展”限制不再适用于本次新需求。
- 本轮只改设计文档；没有修改业务代码、测试、配置、Prompt、依赖或数据库，没有构建、启停、模型调用或外部发布。
- 先前详情接口及 profile 的诊断结果见 2.2；不是本轮重跑。R1、R2、R3 均未实施，真实对话／复测／报告／平台发布均未验收。
- 当前分支 `feat/update-worker-cache`；无 commit、无 push。后续实施在此节追加真实记录，不把本提示词存在当作已执行。

### 2026-10-28：R1 实施、受控验证与本地加载

#### 实际实现与取舍

- **持久输入与公开消息**：实际 PostgreSQL 增加 migration 72（`project-agent-conversations`），保存会话、独立 pi session、输入 UUID、消息次序、最终回答及 Artifact 引用。TaskRun 仍是执行状态权威来源，不另建 job 状态机。会话行锁保证同 session 顺序 claim；重复 UUID／相同正文返回原消息，不同正文返回冲突。
- **鉴权与传输**：用户使用既有 cookie、CSRF 和项目权限；Dispatcher 私有 claim／heartbeat／tools／finish 接口核对项目、轮次、TaskRun 和机器 owner。HTTP 受理输入，SSE 发送状态、最终消息和成果引用；消息 id 与事件 revision 分离，断线后全量补读旧消息状态。最终正文完整检查后才公开，本轮没有逐 token 发布旁白或原始工具日志。
- **同一执行内核**：`chat_turn` 复用现有 `runRequest`、pi SDK、session 锁、PiDriver、进程执行器、线程池和 RunningTask；普通最终文本自然成功，不要求 `submit_result`。旧批处理继续要求业务提交。chat 是独立 session family，不能把旧探索 session 直接改为聊天。
- **真实工具与成果**：可信 TypeScript 模块单处维护工具 Schema／指引，私有 Unix socket 绑定当前项目和轮次，Dispatcher 持有机器认证。四个工具实际调用已有进度、漏洞列表／详情及单漏洞报告服务；导出选择已有 Observation、生成 Markdown、真实保存 Artifact，返回同源鉴权下载。不会重新调用报告模型，不从回答正文推断下载链接。导出入口仅先确定 Observation，完整材料由报告服务读取一次。
- **材料读取优化**：同内容 Artifact 改为投影、去重来源路径，不再逐条装载和解析全部副本；`_key_evidence → freeze_report_flows` 在当前只读调用链复用来源判断。受保护副本、当前身份、实际字节、Prompt 摘要、执行凭据和 session 绑定仍各自检查；没有跨请求秘密缓存，没有修改历史原件或放宽交付权限。
- **项目页面**：增加 Agent 页签、绑定 Worker 新建会话、会话 URL 恢复、消息排队、失败同 UUID 重试、状态与成果补读、执行详情入口及成品下载。回复作为纯文本显示。连接稳定至少 30 秒才复位连续失败预算，避免服务端每 60 秒正常轮换最终耗尽重连次数；快速断连仍有界停止。
- **运行隔离**：沿用 ContainerManager 的同一创建核心，为项目建立独立 `-chat` 控制容器。聊天不启动研究 collector／gateway，不装载目标操作工具，不随研究 stopped／completed 自动取消；仍计入全局、项目和 Worker 执行容量。私有 workspace／control／prompts 位于现有 traffic root 旁的 `*-chat/<project>`，session 可随容器重建保留。项目删除的 orphan 清理会移除聊天容器，宿主私有目录沿用历史材料保留策略，不自动删除。
- **不确定状态**：复用真实 `TaskCancellation.execution_lifecycle()` 判断终止，不把 future 返回等同于 Docker 执行已停止。终止未知不发送 finish、不释放本地会话占位；heartbeat 失联后页面直接显示“执行状态待确认”。已完成执行的 finish 最多有界重送原回执，绝不为回执失败再跑模型；不可恢复或重试耗尽时保留服务端未确认事实。Dispatcher 重启不自动重跑未确认轮次。
- **公开边界**：平台凭据、Worker 敏感 literal、私有 socket 和完整 chat system prompt 命中时拒绝整段输出；thinking、工具前旁白和原始执行事件不进入公开聊天。不通过删字、替换或裁剪权威材料制造可交付答案。

#### 非付费验证结果

| 检查 | 实际结果与范围 |
| --- | --- |
| `cairn/tests/test_agent_conversations.py` | 一次性 PostgreSQL，1 项集中行为测试通过，最终运行 12.87 秒。真实登录／CSRF、跨项目与机器 owner、并发 UUID 幂等、顺序 claim、实际四工具、Markdown Artifact 与下载、秘密拒绝、finish 幂等、同 session 下一轮和 revision 补读。另验证实际 ASGI SSE 帧及 `Last-Event-ID`；不是生产浏览器长连接验收。 |
| `container/pi-agent/tests/chat-turn.test.mjs` | 7 项受控 Provider 检查通过：普通对话自然完成、持久 chat session 恢复、真实 Unix socket 工具调用、thinking／旁白不公开、模拟敏感 literal 整段拒绝。没有真实模型费用或目标访问。 |
| 代表性旧批处理合同 | 1 项通过，未将聊天自然结束语义套到要求 `submit_result` 的旧任务。未扩展到全部阶段。 |
| `cairn/tests/test_agent_chat_execution.py` | 9 项通过：PiDriver／原进程消费者／Unix 工具桥、一次执行与有界 finish、stopped 隔离、私有持久挂载，以及返回／启动异常／通信异常三种终止未确认路径。此处容器管理验证为受控测试，不冒充生产 Docker 模型端到端。 |
| 前端局部组件与格式测试 | Agent 原 5 项及补充 2 项通过；任务类型标签 1 项通过。补充覆盖五次正常 SSE 轮换仍可连接、快速失败耗尽后不再重试、执行待确认提示及恢复。 |
| 材料边界局部测试 | `test_external_verification_evidence.py` 定向 6 项通过、17 项未选；覆盖来源去重、请求间重判、内部副本、凭据／Prompt 摘要／session 及原件变化保护。 |
| 正式前端构建 | `npm --prefix cairn/frontend run build:server -- --emptyOutDir false` 通过，含 `vue-tsc -b`。首次构建发现测试参数属性不兼容 `erasableSyntaxOnly` 和 Dialog 缺 description，修正后通过。保留既有静态文件，不清空用户已有生成产物。 |
| Worker 构建 | TypeScript build 及新镜像内离线 `cairn-agent --self-check` 通过。没有安装新依赖或重下载工具。 |

系统 venv 没有 pytest，复用了本机已安装 pytest 与项目 venv（`PYTHONPATH=/usr/lib/python3/dist-packages:cairn/src`），没有为测试安装依赖。没有运行全量测试、lint 或浏览器矩阵。

**材料慢点实测**：在同一个活动 PostgreSQL 的 `READ ONLY, REPEATABLE READ` 事务内，对原算法和新实现调用 `get_vulnerability_detail(proj_060, v001)`，两次完整结果相等：

| 指标 | 原算法 | 新实现 |
| --- | ---: | ---: |
| 详情函数总耗时 | 52.7961 秒 | 6.5057 秒 |
| `_artifact_allowed` 累计耗时 | 51.3922 秒 | 5.7561 秒 |
| 同内容来源查询次数 | 26 | 14 |
| 查询返回行数 | 136,389 | 18 |
| `artifact_is_internal_file` 调用数 | 136,415 | 44 |

本次函数总耗时下降约 87.7%。这是只读函数 profile，**不是部署后 HTTP GET 基准**；首个不同内容仍有来源查询成本。未重跑综合报告 Packet，不能据此声称 `tr013–tr015` 全部修复。

#### 完整系统部署与实际页面

- 重启前重新核对活动库：queued／running TaskRun 为 0，项目为 3 stopped、3 completed；没有中断、恢复或重跑项目。
- 原 Dockerfile 的禁网构建未命中基础 apt 层，在禁网条件下失败；没有放开网络重装工具。随后核对现有镜像应用 package 元数据／依赖完全一致，以既有镜像为基础，只复制本次编译的应用和指引，离线构建成功。核对新镜像 RootFS 以旧镜像全部层为完整前缀，工具／依赖层全部保留。
- 已将 `arena-worker:local` 加载为 `sha256:313645302bf11af1f2a8cd1dcac627440ea8e5f238f2fe3d815de10003153e2e`，镜像内最终 runner／project-tools 与本地编译产物摘要一致。未删除／重建旧项目容器；后续新聊天容器使用此镜像。
- 按 `deploy/stop.sh` 与 `deploy/start.sh --host 0.0.0.0 --port 8001` 重启完整系统。实际 PostgreSQL 从 schema 71 加载到 72；Server、Dispatcher、HTTP API 正常，Dispatcher 心跳 idle。现场 PID 为 Server `1735489`、Dispatcher `1735716`，仅作本次事实记录，不作为后续定位依据。
- 首次真实页面检查发现活动实例设置了 `CAIRN_STATIC_DIR=runtime/static`，未使用源码构建目录，故仍显示旧路由。已将同一次正式构建增量同步到实际目录并最后替换入口；未清理旧文件、未更改运行配置。随后真实浏览器验收通过。
- 已用本地授权账号登录，实际查看 `proj_060` 的 Agent 页签、会话列表／空态和绑定 Worker 新建对话框，并查看 1440×960 截图；没有脚本错误，操作和布局正常。未点击创建或发送，生产会话数仍为 0、活动 TaskRun 为 0。没有将模拟答案或测试成果写入业务库。

#### 本轮修改文件

以下仅列本轮增量涉及的文件；其中不少文件开始时已有用户修改，这些既有修改不属于本轮。初始工作区有 595 个变更条目，均未回退。

- Server／API／迁移：
  - `cairn/src/cairn/server/app.py`
  - `cairn/src/cairn/server/migrations.py`
  - `cairn/src/cairn/server/schemas/common.py`
  - `cairn/src/cairn/server/routers/agent_conversations.py`（新增）
  - `cairn/src/cairn/server/services/agent_conversations.py`（新增）
  - `cairn/src/cairn/server/services/audit_task_runs.py`
  - `cairn/src/cairn/server/services/tenant_workers.py`
  - `cairn/tests/test_agent_conversations.py`（新增）
- 材料读取：
  - `cairn/src/cairn/server/services/verification_requests.py`
  - `cairn/src/cairn/server/services/flow_evidence.py`
  - `cairn/src/cairn/server/services/vulnerability_reports.py`
  - `cairn/tests/test_external_verification_evidence.py`
- pi runner：
  - `container/pi-agent/src/{protocol,session,candidate-spool,runner}.ts`
  - `container/pi-agent/src/project-tools.ts`（新增）
  - `container/pi-agent/tests/chat-turn.test.mjs`（新增）
- Dispatcher：
  - `cairn/src/cairn/dispatcher/models.py`
  - `cairn/src/cairn/dispatcher/protocol/client.py`
  - `cairn/src/cairn/dispatcher/runtime/{containers,traffic_reader}.py`
  - `cairn/src/cairn/dispatcher/runtime/project_tools.py`（新增）
  - `cairn/src/cairn/dispatcher/tasks/process_runner.py`
  - `cairn/src/cairn/dispatcher/tasks/agent_chat.py`（新增）
  - `cairn/src/cairn/dispatcher/workers/base.py`
  - `cairn/src/cairn/dispatcher/workers/adapters/pi.py`
  - `cairn/src/cairn/dispatcher/scheduler/{loop,dispatch_cycle,task_lifecycle,stale_reconcile,cleanup}.py`
  - `cairn/src/cairn/dispatcher/scheduler/agent_chat.py`（新增）
  - `cairn/tests/test_agent_chat_execution.py`（新增）
- 前端：
  - `cairn/frontend/src/api/projectAgent.ts`（新增）
  - `cairn/frontend/src/features/projects/{ProjectAgent.vue,useProjectAgent.ts,ProjectAgent.component.test.ts}`（新增）
  - `cairn/frontend/src/router/{index,navigationRegistry}.ts`
  - `cairn/frontend/src/lib/{format.ts,format.test.ts}`
- 文档与生成产物：本方案第 12 节；正式前端构建更新 `cairn/src/cairn/server/static/index.html` 并新增相应 hash 资源，同步到实例实际使用的 `runtime/static`。未手工修改生成 JS。

#### 未验收与剩余问题

1. **真实模型未验收**：没有真实模型对话、自然语言自主选工具质量或模型费用验证；受控 Provider 仅证明协议／消费者。没有目标访问、新探索、复测、新综合报告或外部发布。
2. **真实页面的消息长列表滚动、断网重连及成品点击下载尚未进行生产端到端验收**：已由局部组件、实际 SSE 帧与临时 PostgreSQL 的真 Artifact／下载链路验证，但不把这些说成生产真实对话验收。生产页面仅验证入口、空态和 Worker 选择。
3. **实际 Docker→真实 Provider→业务服务整条运行链尚未执行**：已完成代码接通、受控消费者、镜像编译／离线自检及正式加载；没有以真实付费调用越过授权边界。
4. 执行终止或完成回执未确认时，同会话不会自动重跑；需要核实真实进程／服务端状态后人工收口或后续执行控制能力，不把超时视为已停止。首轮尚未建立有效 session 即失败时，不会自动创建新 session 隐瞒失败。
5. 已有聊天容器的后续镜像升级需要在空闲时受控重建并保留挂载目录；本轮不自动替换运行中容器。项目删除后的宿主私有 session 目录保留，不属于本轮自动清理范围。
6. 单漏洞 Artifact 生成与消息关联是既有 Artifact 服务及消息事务两步；中间失败可能留下已保存但未关联消息的成品，不返回虚假成功。没有为此另造通用作业系统。
7. 材料查询仍有首读成本；R2 的综合报告聚合／长锁、实时 steer／取消及 R3 外部发布均未实施。不会把本轮收益扩大为综合报告全链路或漏洞发现能力已验收。

已审阅本轮差异；当前分支 `feat/update-worker-cache`，**无 commit、无 push**。R1 已实现并完成上述受控验证和本地加载，真实业务验收按以上边界保留；到此停止，不自动进入 R2/R3。

## 13. 下一轮 R2a：发起耗时业务、跟踪成果与 Agent 交互重做

> 2026-10-28 分析交接。用户要求提供下一轮提示词，并明确长期适用的复杂度、UI 和 UX／DX／AX 标准。规则已更新到 `AGENT.md`、`docs/ui-design-standard.md` 和 `docs/analysis-design-handoff.md`，本节只记录本轮具体取舍。R2a 尚未实施；R1 的测试、profile 和部署来自第 12 节实施会话记录，不是本分析轮重新验证。

### 13.1 用户可见目标与本轮边界

用户在项目中说“复测 v001”或“生成项目综合报告”，得到真实业务请求；随后能继续聊天、询问进度、查看结果和下载实际成果，刷新页面仍可找回。普通聊天轮次结束与后台工作完成是两件事，不能让模型自报“完成”替代业务回执。

本轮新增两类业务发起和对应状态／结果读取，贯通现有模型工具、业务服务、执行调度和前端。相关 Agent 区域按新的 UI 标准重做，而不是继续给旧聊天页面叠说明和标签。R1 的项目查询、漏洞阅读、Markdown 导出、会话恢复等主要能力保留。

明确不做：活动研究的实时 steer／改方向、新探索、外部平台发布、独立复测结论自动覆盖原漏洞状态、活动项目阶段报告、整站改版、通用插件／任务／审批框架。原有报告或复测页面的取消等主要功能不因新入口被移除；聊天内的新执行控制归 R2b。

### 13.2 查证依据与必要改动理由

本轮定向源码查证包括委派的只读调查；没有数据库操作、运行测试、模型调用或目标访问。以下行号仅用于定位。

| 当前事实 | 依据 | 对下一轮的直接影响 |
| --- | --- | --- |
| 四工具和可信指引在 TS 注册，服务端再次解析参数并调用已有服务；指引明确不支持复测／探索 | `container/pi-agent/src/project-tools.ts:15–81`；`cairn/src/cairn/server/services/agent_conversations.py:214–274` | 新工具、参数、指引和服务调用必须一起贯通，不能仅添一个按钮或改一份 Markdown |
| 工具桥绑定项目／会话／消息／TaskRun，Dispatcher 使用机器身份；当前非成功响应统一成 `project_tool_failed` | `cairn/src/cairn/dispatcher/runtime/project_tools.py:10–34` | 保留服务端绑定；传递可公开的业务错误和实际补充动作，不让模型猜失败原因或暴露内部响应 |
| 独立复测接受 user/integration caller，而平台漏洞来源要求 user；当前消息受理没有保存消息发送者身份，只记录会话创建者 | `server/services/verification_requests.py:67–71,668–820`；`verification_sources.py:61–75`；`agent_conversations.py:50–56,113–144`（服务路径均位于 `cairn/src/cairn/`） | 要保存并使用本次真实消息提交者的必要身份，不能把机器身份直接用于复测，也不能默认为所有消息都由会话创建者发送 |
| 自动内部 verifier 按 Observation 去重，重复调度会返回原 Intent | `cairn/src/cairn/server/services/vulnerability_verifications.py:185–255` | 不把这一函数包装成“再复测一次”；用户主动复测采用现有独立请求语义 |
| 独立复测已有平台漏洞来源、请求去重、后台执行、查询和报告 | `cairn/src/cairn/server/schemas/verification_requests.py:39–85`；`server/services/verification_requests.py:668–820,879–1007`；`dispatcher/tasks/external_verification.py` | 复用这条业务链，不另起直接执行目标的聊天实现 |
| 报告 request、claim、Packet 与 stopped 分派把 queued/running 的非 report 任务视为忙；包含当前 running 的 agent_chat | `cairn/src/cairn/server/services/assessment_convergence.py:441–445`；`assessment_reports.py:654–737,1966–1981`；`dispatcher/scheduler/dispatch_cycle.py:167–171` | 其他前置通过后，聊天发起报告会确定性地被自身挡住。必须在报告相关生产者／消费者统一区分聊天与改变研究材料的执行，不是猜测竞态 |
| 新一代报告失败可以保留旧 accepted Artifact | `cairn/src/cairn/server/services/assessment_reports.py:1529–1536` | 跟踪必须包含 generation_token，不能拿上一代文件宣称本次成功 |
| 当前页面只有回答、聊天状态和 Artifact 列表，没有耗时业务引用；常驻能力声明和多处重复状态说明 | `cairn/frontend/src/api/projectAgent.ts:10–20`；`features/projects/ProjectAgent.vue:18–81` | 需要真实业务关联与结果呈现；可重排会话／工作／成果关系，不能只输出一段不可追踪的 ID 文本 |

上述身份绑定和报告分类是当前接入会直接遇到的问题，不是为假想公网攻击或极端并发新增框架。业务创建回执丢失后重发同一工具调用，可能重复付费执行；发生率没有统计，不能给出百分比。采用已有请求去重和最小动作关联即可，不因此引入通用 outbox、事件总线或第二套作业状态机。

### 13.3 复测工具：复用独立请求，明确归属

- 当前项目内解析漏洞编号。未指定 Observation 时沿用既有来源选择规则，并在结果中返回实际选中的 Observation；不跨项目猜编号或让模型重写一份漏洞报告作为来源。
- 使用现有 `VerificationRequest`，`mode="retest"`、`report.type="platform_vulnerability"`，传入绑定项目、漏洞及所选 Observation。已有来源服务生成历史输入，本轮证据仍由复测执行产生。
- 从受认证消息保存必要的操作人身份，由服务端恢复真实调用上下文并按现有规则检查。模型不填写 tenant／caller／机器 owner；已有历史消息缺少动作身份时不能猜成会话创建者，不自动为历史对话补发新业务。
- 复测沿用专用配置。聊天 Worker 与复测 Worker 是不同用途；配置缺失时给出实际配置入口，不静默借用聊天 Worker，不新建固定审批步骤。
- 返回并关联服务端 `vr_*`、来源项目／漏洞／Observation。客户端 `request_id` 是 UUID 去重身份；返回 `project_id` 是独立复测执行项目，不得与来源项目混淆。
- 结果、报告和查询权限继续归原复测业务；聊天按实际用户权限消费。来源关联不等于把复测改成项目内 `vv_*` 核验，不自动创建原项目新 Observation，也不自动标记原漏洞已修复。

### 13.4 综合报告：修正真实阻断，保持确定的材料来源

- 复用现有 finalization request、调度、冻结输入、结果接受和下载；返回 `project_id + generation_token`。本轮先支持已有资格下的 completed／stopped 项目，不通过自动暂停／停止研究来制造报告资格。
- 在报告相关资格检查及调度消费中，区分普通聊天与改变研究材料的实际研究工作，贯通 request、claim、Packet 和 stopped 分派。不要只排除发起消息的 TaskRun，否则后续聊天查询仍可能挡住报告；也不要全局删除研究收敛规则。
- 明确总报告的实际输入：项目概况、研究范围、发现／核验和所引用材料。聊天记录、控制执行和仅供下载的导出副本不自动成为新的研究事实；不能继续全项目无差别聚合再要求所有聊天静止。保留报告实际需要的内容及来源，原件不删、不改写。
- 针对已确认的 Packet 长路径，复用 R1 材料优化，限制集合到实际输入，缩短持项目锁准备材料的阶段。短事务确定本次身份与实际内容版本，耗时材料准备读取已确定内容，在现有绑定／接受点核对该 generation 仍有效；不能简单释放锁后重新读取任意 live 内容。优先复用已有快照、来源身份和事务能力，不加全库版本系统、通用锁框架或跨请求秘密缓存。
- 用直接非付费路径核对 Packet 能在实际客户端条件下完成，并确认普通聊天不会使报告自我阻塞；记录实际耗时和仍有的瓶颈。不能仅增大 timeout／重试或凭 R1 详情 profile 宣称总报告已恢复。
- 当前生成状态、错误和 accepted_generation 分开读取。可以保留旧报告下载，但旧文件必须明确是旧成果，不显示成本轮新生成成功。

### 13.5 消息、动作与结果的最小关联

工具发起后尽快返回业务受理结果，聊天执行不占着进程等待另一个模型结束。后续工具查询或页面读取直接取业务记录，不能用模型反复轮询维持所谓实时感。

只保存当前消息与实际业务身份的关联：复测 `vr_*` 或报告 `project_id + generation_token`，以及定位源对象／发起人的必要字段；不复制业务状态和报告正文成为第二来源。SDK 工具调用 ID 当前没有被桥接使用，本轮贯通为服务端绑定动作身份的一部分，重送同一动作返回原请求；用户明确另发“再复测一次”则是新动作，不以漏洞 ID 永久去重。业务创建与关联尽量利用同一现有事务，不能依靠模型最终回答成功后才保存关联。

页面重连时能从关联补读真实状态和成果；不同业务结果沿用各自权限，拥有项目入口不自动意味着可读另一调用方的独立复测。只在具体业务需要的位置处理权限，不新增通用跨平台授权框架。

### 13.6 Agent 界面：可以重做，不给旧布局继续贴说明

- 围绕“当前会话／输入 → 这次实际工作 → 结果与文件”组织界面，可替换现有选择器、消息排版及工作状态布局，不要求保留当前表单式聊天外观。保留主要功能、合理的导航与对象深链接；不强制另加固定侧栏、卡片墙或全站 Shell 改造。
- 删除常驻“只读成果工具”能力声明、正常状态下重复的排队／完成解释和无助于当前动作的标签。状态用一个相关位置表达；真实失败、结果未确认、必要费用信息与资料限制按实际动作准确呈现，不能为清爽隐去关键事实。
- 工具成功以实际业务引用和成果表达，不把所有机器 ID、状态字段或内部协议铺在消息旁。模型回答不是另一份权威状态；历史回复与当前业务状态可以区分。
- 新增结果能力不强迫用户重复填写已有项目、漏洞、模板和身份字段。已有默认配置合理时直接使用；仅真实歧义或缺少执行前提时补充选择，不能把工具变成逐次审批表单。
- 本轮实际界面重做前，可选少量近期仍维护的高质量公开 Agent／工作台项目作定向参考，按 UI 规范 1.1 记录实际链接、观察时点和借鉴理由；不以截图替代交互查证，不复制私有素材，不为研究参考上传本项目资料。查不到或网络受限就如实说明，不杜撰“最新参考”，不因此阻塞已明确的业务工作。本分析轮未进行新的外部调研。

### 13.7 直接验证与交付

选能证明本次链路的最短非付费验证，不把下面内容逐条转成新测试平台或套餐：

- 受控工具调用真实业务服务，确认创建／返回／消息关联／后续查询／结果下载，重送同一动作不重复创建，明确再次请求可以新建。普通阅读和 R1 导出继续可用。
- 覆盖本次确定性接入差异：真实消息提交者与会话创建者不同的归属、聊天 running 时符合条件的报告受理及后续调度、旧 accepted 报告不会冒充本代成功。无需为更多理论排列组合展开全套并发测试。
- 针对实际 Packet 路径做必要的材料／事务验证，不以详情加速或模型 stub 直接返回成功跳过材料构建。
- 在实际页面检查会话、输入、工作状态、结果跳转／下载与刷新恢复。删除多余解释后仍可理解和操作；用非敏感受控材料验证布局，不为了截图发起真实目标任务。
- 运行改动后按项目规则构建加载完整系统；Worker 应用更新复用原工具／依赖层。已有聊天容器使用旧应用时，在空闲确认后针对性更新并保留持久挂载；不得只改镜像 tag 就宣称现有容器已加载，不中断在途执行或删会话原件。

R1 的真实模型端到端仍未验收，可以在本轮获得明确模型／费用授权后合并验证，不强迫用户先付费重跑 R1 或新建探索项目。真实复测会访问目标，真实综合报告会消耗模型费用；本提示词只授权实施，不自动授权这些业务执行、外发、清库或提交。未获授权时交付受控证据与明确待验收项。

### 13.8 可复制的新实施会话提示词

```text
这是新开的实施会话，只实施项目 Agent 的 R2a，不重跑 R1、不接续历史待办。

先读 CLAUDE.md、AGENT.md、docs/ui-design-standard.md 第1节，以及
docs/specs/project-agent-capabilities-design.md 第12节R1实施记录和第13节完整R2a方案。
第11节R1提示词已执行，不再执行；R2b实时干预和R3外部集成都不在本轮。
本会话可以修改本轮业务代码，不继承分析会话只读角色。

先检查 git status --short、git branch --show-current，保护大量既有改动；
以现场源码和实例为准，不照抄旧PID、端口、迁移序号或镜像已加载声明。
用几句话说明本轮实际目标、依据与不做范围，然后推进。

必须遵循最新长期标准：
- 额外竞态/安全/恢复机制先说明真实触发路径、后果、发生可能性及依据。
  无统计就说未知，不编概率；仅理论可能、不值得的情况不加复杂度和限制。
  不因此删除必要授权、数据保护或事实归属，不建立新的风险/审批框架。
- 不为兼容现有UI或旧方案设计。相关Agent界面可重做，可查近期高质量公开项目，
  按UI规范记录真实参考链接/时点和借鉴理由；不虚构调研、不复制私有内容。
- 删掉多余标语、说明和次要标签；删完看不懂就调整布局和交互，不能把说明换个地方堆。
  同时保证UX、DX、AX，保留主要功能、关键事实、可访问名称和真实结果入口。

目标：用户说“复测v001”“生成项目综合报告”能发起真实业务，
随后继续聊天、查进度、读结果、下载成果，刷新后仍能找回；
不是仅返回文本ID，不让聊天轮次等待后台模型执行结束。

范围：
1. 贯通TS能力工具/指引、Unix工具桥、可信身份、服务端业务调用和消息动作关联。
   业务动作使用本次受认证消息提交者，不默认为会话创建者，也不让模型提供caller。
   同一次工具动作重送复用已有请求，用户明确再次发起可创建新请求；不新建作业状态机。
2. “复测v001”复用现有VerificationRequest(mode=retest, report.type=platform_vulnerability)，
   返回vr_*及准确来源Observation；使用已有复测专用配置、调度和结果接受。
   不包装按Observation去重的内部verifier冒充新一轮复测，不自动覆盖原漏洞状态。
3. 总报告复用finalization与generation_token，先支持已有资格的completed/stopped项目。
   当前agent_chat被非report活跃任务检查挡住是确定性问题：贯通request、claim、Packet、
   stopped调度对研究工作/聊天的区分；不要求停止聊天，不自动暂停项目凑资格。
   处理本轮必要的Packet集合及长锁路径，复用已有冻结/材料身份，不改成读不确定live内容，
   不靠增大timeout或全局删除研究静止检查解决；旧accepted报告不冒充新一代成功。
4. 查询真实业务状态、结果和合法成果入口；消息只保存业务引用，不复制第二份状态/正文。
   返回可公开的实际业务错误和必要补充动作，不让模型根据project_tool_failed猜原因。
5. 按第13.6节重做受影响的Agent交互，不保留不合理旧布局；会话恢复、四个R1能力、
   已有报告/复测主要操作和必要深链接不丢失，不扩展为整站改版。

不做：实时steer/改研究方向、新探索、活动项目阶段报告、外部平台发布、
自动回写原漏洞状态、通用插件/权限/任务框架、SQLite、无关工具链重建。

验证按第13.7节选最直接的非付费链路和实际页面，覆盖本次已确认接入差异；
不默认全量测试/lint/浏览器矩阵，不新建验收平台。若依赖确需增加先批准。
本轮不自动调用真实模型、访问目标或发起真实复测/总报告；有当轮明确范围与费用授权才做。
R1真实模型未验收如实保留，不以它为理由要求先重做旧项目或停止本轮正常实施。

运行代码改动后按实际部署方式构建并启动完整系统；检查在途任务，不擅自中断。
容器应用变化才更新Worker，复用未变工具/依赖层，核对实际消费容器加载，保留session挂载。
不清库、不改写历史材料、不自动恢复/重跑项目、不commit、不push。

在原方案第13节后追加实施记录，分别报告代码、受控验证、部署加载、真实业务验收及未知。
最终报告当前分支、修改文件、验证结果、提交情况、剩余事项。完成R2a后停止，
结果带回分析会话后再确定R2b；不要继续执行旧提示词或自行扩大范围。
```

### 13.9 本分析轮记录

- 已更新长期规范、UI 标准、分析／实施交接模板，以及本主方案的当前状态和 R2a 范围；没有修改业务代码、测试、依赖、Prompt 资源或数据库。
- 只读核对本轮调用路径并采纳委派调查的具体源码依据；未重跑 R1 测试、profile、构建／部署，未执行模型、目标或外部发布，也未做新的公开项目调研。
- 已审阅本轮规范与方案新增差异；主要功能是否保留、R2a 是否可用仍待实施与相应验收，不能由文档判定通过。
- 当前分支 `feat/update-worker-cache`；无 commit、无 push。

## 14. R2a 实施记录（2026-10-28）

### 14.1 本轮代码与范围

仅实施第 13 节 R2a，不重跑第 11 节历史提示词，不开展 R2b／R3。沿用当前 PostgreSQL、独立复测和报告 finalization 权威链；不把旧 UI 或历史 SQLite 实现作为兼容目标。已有未提交修改保留，无新增依赖。

- **工具到业务**：保留四个 R1 工具，增加 `request_vulnerability_retest`、`request_project_report`、`query_agent_work`。SDK `tool_call_id` 经 Unix socket 和 Dispatcher 传给服务端；指引要求明确请求才发起，受理后结束聊天，不循环等待后台模型。
- **身份与关联**：PostgreSQL migration 73 为消息保存受认证提交者／租户，新增 `agent_actions`，仅存动作身份与原业务引用，不复制业务状态／正文。动作读取当前有效用户及项目权限；历史无身份消息不推断成会话创建者。
- **同动作重送**：以消息与工具动作 ID 唯一标识，已受理动作返回原引用，参数冲突明确拒绝；不同动作可以发起新的独立复测。复测创建与动作引用共用原有 managed transaction。此保护针对真实 HTTP 重送／返回丢失路径，不增加通用任务状态机；没有发生率统计。
- **独立复测**：使用现有 `VerificationRequest(mode="retest", report.type="platform_vulnerability")`，明确来源项目、漏洞、Observation 与独立执行项目，沿用专用配置、来源材料冻结、调度、结果接受及报告下载；不自动改原漏洞状态。
- **综合报告**：沿用 finalization／generation_token，request、claim、Packet、stopped 调度区分研究执行与 `agent_chat`。真实研究仍需静止，不自动暂停项目；仅 accepted_generation_token 匹配本动作代次时提供本轮成果，不把旧 accepted 文件标成新一轮成功。
- **Packet**：短写事务整理停止态，再在 PostgreSQL `REPEATABLE READ READ ONLY` 快照构建材料，慢材料不持项目写锁。排除 chat/report 执行及无研究引用的导出副本，按显式研究引用收集 Artifact，并在同次快照共享来源检查。未增大客户端 timeout，未跨请求缓存权限／准出结论。
- **界面**：按用户消息、Agent 回复、本轮工作／成果排列；删除能力口号、正常连接标签与已完成聊天标签。保留会话、Worker 选择、输入恢复、失败诊断、R1 导出与业务入口。后台工作独立补读，不依赖聊天 SSE revision；在途工作每 5 秒读取，终态停止，连续读取失败保留现有内容并提供手动恢复，不禁聊天输入。刷新后从消息持久引用找回。

### 14.2 受控验证

以下不调用真实模型、不访问目标，也不以模型 stub 直接跳过业务创建／Packet／结果接受。

| 验证 | 结果及证据边界 |
| --- | --- |
| `test_agent_business_actions.py`，一次性 PostgreSQL，`CAIRN_TEST_POSTGRES_BOOTSTRAP=1` | 1 passed，11.00s。真实登录：管理员创建会话、普通项目所有者提交消息；独立复测创建、同动作去重、不同动作新建、Observation、finish 前持久引用、后续轮次查询／刷新、无身份历史消息拒绝；报告 running-chat request／claim／Packet；旧 accepted 与本代 failed 隔离；复测 claim／本轮原件登记／结果接受／Markdown 下载；R1 导出及外租户拒绝。Packet 的 TestClient 耗时 0.146s 仅是受控 ASGI 结果，不代表实例 socket。 |
| `test_agent_report_packet.py` + `test_http_evidence_render.py`，一次性 PostgreSQL | 最终 11 passed，3.77s。验证聊天不挡报告、研究执行仍挡报告、显式材料集合、stopped 调度；慢材料期间另一连接以 500ms lock_timeout 更新项目／事实成功，Packet 仍读取原快照；同源检查复用后仍拒绝含秘密的原件。 |
| TS 与 Python 工具桥定向测试 | Node `chat naturally\|R2a tools` 2 passed；Python `claim_runs_through_driver or project_tool_bridge` 2 passed、8 deselected。动作 ID 贯通、七工具可用与结构业务错误透传。 |
| `ProjectAgent.component.test.ts` | 最终逻辑版本 10 passed（6.04s），覆盖工作状态更新／刷新／失败恢复、旧报告代次下载隔离及原交互；后续仅调整链接颜色 token。 |
| 正式前端与 Worker 应用构建 | `build:server -- --emptyOutDir false`（含 vue-tsc）通过，Worker TypeScript build 通过；保留现有大 chunk 提示，不扩展本轮打包重构。 |

没有跑全量测试、lint、浏览器矩阵，也没有新增验收平台。集中测试中错误 fixture 与不可达的跨租户管理员复测样本已修正／移除；没有为测试伪造共享个人租户权限模型，不能声称所有权限排列均已覆盖。

### 14.3 部署加载与实际页面

- 现场先查在途 TaskRun 为 0，项目均为 stopped／completed；按现有 `deploy/stop.sh`、`deploy/start.sh` 重启完整 Server／Dispatcher，数据库迁移已加载至 73，API ready、Dispatcher heartbeat idle。没有启动／恢复项目或中断任务，没有清库或改写历史原件。
- 正式静态产物同步至实例实际 `runtime/static`，保留历史静态文件。不能只以源码构建成功代替实例加载。
- Worker 应用以原本地镜像为基础离线追加 `dist`／指引，包定义哈希与原镜像一致，工具和依赖层未重装。新镜像 `sha256:3e2b3d49d3cc4cdd4652049254c4d8c632d86fdbe0a1a438885d7c672700d657`；一次性无网络容器内 self-check 通过，`project-tools.js` 与 `AGENTS.md` 哈希和宿主一致。首次 FROM 直接用镜像 ID 被 BuildKit 当仓库名解析、随后构建上下文排除 dist，均未成功更新 tag；最终使用固定本地标签及只含构建产物的临时上下文完成。未重新下载工具／依赖。
- 当前没有既有聊天消费容器，无会话原件需搬迁；现有已停止的研究容器及持久挂载未删除。新镜像的离线启动／哈希不等于实际聊天执行已验收，也不宣称旧研究容器已更新。
- Chrome DevTools MCP 连接超时，改用项目已有 Playwright 与系统 Chromium，不安装新依赖。真实登录实例 `http://127.0.0.1:8001/#/…` 后检查 Agent 空态与新建会话弹窗，未提交生产会话／消息。
- 通过浏览器局部受控响应展示后台 running／完成工作，验证继续输入、手动刷新、整页刷新恢复与结果入口；截图仅包含 Agent 区域的合成内容，并实际查看。受控 SSE 立即结束，因此截图展示断连提示，不冒充真实长连接验收。成果入口实际只读下载一份既有 accepted 报告（114466 bytes）并跳转原报告页面；没有生成新报告。最终 page errors 为空。
- 浏览器首次误用 history 路径导致 404，修正为项目现有 hash 路由；精确标签未含 required 标记导致定位超时，改用现有 autocomplete 控件语义。合成下载拦截未覆盖浏览器下载请求，返回 404 的文件没有计为成功，改为验证既有合法成果下载。

#### 实例 Packet 慢路径复验

首次部署后，真实 socket 在原 `requests timeout=10` 下：`proj_059` 为 200／2.425s，`proj_060` 仍在 10.015s 超时，未将受控测试通过当成该问题已解决。只读剖析实际 `proj_060`（不执行初始化／迁移／停止态写入）发现总耗时 82.519s，其中同内容来源检查 SQL 74 次占 79.654s；可选图片材料 `freeze_render_sources → prepare_render_input` 没有接入本轮 Packet 的来源检查复用。

据此只给 `http_evidence_render.py` 两个现有函数透传同一只读快照内的 `source_checks`，不缓存平台秘密或最终准出、不省略原件检查。相同来源 SQL 降至 22 次；无剖析器只读构建为 5.306s／5.021s。空闲确认后再次重启完整系统，最终真实 socket 仍用 **10 秒 timeout**：

| 实例材料样本 | HTTP | 总耗时 | 响应字节 | 规模 |
| --- | --- | --- | --- | --- |
| `proj_060` | 200 | 4.850s | 983428 | 7 facts、2 vulnerabilities、74 artifacts、12 研究 task_runs |
| `proj_059` | 200 | 1.639s | 11359173 | 68 facts、6 vulnerabilities、385 artifacts、113 研究 task_runs |

这些是现存材料的读取，不是重新发起报告或模型执行。最终再次核对 migration 73、在途任务 0、生产 Agent 会话 0、HTTP 首页与本次构建 index 相同，Server／Dispatcher 均 running、heartbeat idle。两个样本证明已观察到的慢路径改善，不是任意规模性能保证。

### 14.4 真实业务验收与剩余事项

- 本轮未获得真实模型、目标访问、真实复测／综合报告的范围与费用授权，因此**未进行真实模型端到端或新业务执行验收**；R1 原有真实模型待验收状态不变，未要求付费重跑 R1 作为实施前置。
- 受控结果接受与下载证明业务集成，不证明模型一定选择正确工具、真实目标复测质量或真实总报告质量。自然语言发起、受理即结束、后台完成后继续聊天的完整真实模型链仍待当轮明确授权。
- 报告引用遵循现有 finalization 权威投影；更新代次替换后显示已有更新请求，不另建历史报告状态库，不能声称保留了任意历史代次的完整状态快照。
- 同项目可见但不同 caller 的不可达个人租户样本未扩造；实际已验证提交者／创建者不同及外租户拒绝，其他权限组合不作通过声明。
- 本轮修改文件集中在 Agent 工具／桥／API／服务／前端及定向测试，报告 Packet／收口／调度相关服务与本实施记录；构建产物随正式构建更新。当前分支 `feat/update-worker-cache`；无 commit、无 push，不自动进入 R2b／R3。

### 14.5 本轮改动文件

以下仅列本轮触及文件，不将工作区其他既有修改算成本轮成果；其中 R1 文件和若干报告文件在本轮开始前已未跟踪／已修改，不能用相对 HEAD 的完整 diff 代替本轮增量归属。

- 工具／桥：`container/pi-agent/src/project-tools.ts`、`cairn/src/cairn/dispatcher/runtime/project_tools.py`。
- 服务端：`cairn/src/cairn/server/migrations.py`、`cairn/src/cairn/server/routers/agent_conversations.py`、`cairn/src/cairn/server/services/agent_conversations.py`、`cairn/src/cairn/server/services/verification_requests.py`。
- 报告／调度：`cairn/src/cairn/server/services/assessment_reports.py`、`cairn/src/cairn/server/services/assessment_convergence.py`、`cairn/src/cairn/server/services/http_evidence_render.py`、`cairn/src/cairn/server/routers/projects.py`、`cairn/src/cairn/dispatcher/scheduler/dispatch_cycle.py`。
- 前端：`cairn/frontend/src/api/projectAgent.ts`、`cairn/frontend/src/features/projects/useProjectAgent.ts`、`cairn/frontend/src/features/projects/ProjectAgent.vue`。
- 定向测试：`container/pi-agent/tests/chat-turn.test.mjs`、`cairn/tests/test_agent_chat_execution.py`、`cairn/tests/test_agent_conversations.py`、`cairn/tests/test_agent_business_actions.py`、`cairn/tests/test_agent_report_packet.py`、`cairn/tests/test_http_evidence_render.py`、`cairn/frontend/src/features/projects/ProjectAgent.component.test.ts`。
- 文档与生成物：本文件第 14 节；前端构建更新 `cairn/src/cairn/server/static/index.html` 及内容哈希资源，并同步实际 `runtime/static`；Worker 应用构建更新本地 `container/pi-agent/dist` 和本地镜像。

已审阅本轮相关 diff／未跟踪文件内容，`git diff --check` 通过。未 commit、未 push；本轮到此停止，真实业务验收和下一轮范围交回分析会话决定。

### 14.6 2026-10-28：实施后核对与真实入口轻量验收未通过

**授权与范围**：用户本轮明确允许适量使用平台已配置模型做测试，不做大任务。沿实际 `127.0.0.1:8001` 入口登录，仅创建一个轻量验收会话并发送一条消息：查询 `proj_060` 状态和漏洞列表，限制简短回答，不读取完整证据／流量，不访问目标、不启动探索、复测或新综合报告。失败后没有再次发送，不通过直接调用 provider 绕过平台阻断。旧第 14.4 节“当时未获模型授权”的历史记录不再代表本轮权限。

**已观察事实**：

- `deploy/status.sh` 返回 Server／Dispatcher running、HTTP API ready、heartbeat idle；浏览器能打开 Agent、新建会话、提交消息并显示失败，已实际查看截图。服务就绪不等于聊天可用。
- 新建会话 `conv_61cdfb21501445ac83afb1be384bf1e9`，首条消息关联 `proj_060/tr016`，最终 `failed / chat_execution_failed`，没有公开回答、成果或业务动作。平台执行绑定为既有 `pi-ds-1`，未修改模型配置。
- Dispatcher 日志记录创建 `…-proj_060-chat` 后立即失败，没有进入 `starting container exec`。执行记录 `process_started=false`、`usage=null`。这次是平台真实调用尝试，但在模型启动前被阻断，不能当成模型返回失败，也不能宣称已通过真实模型验收。
- Docker 只读检查确认聊天容器 running，使用第 14.3 节记录的新镜像。针对实际容器直接调用现有只读归属判断，`_owns_container=False`，`_assert_owned` 抛出 `ContainerOwnershipError: container is not owned by this pi dispatcher instance`。未重启服务、重建镜像、运行测试套件、清理业务材料或修改业务代码。

**确认问题与下一轮最小范围**：

1. **阻断：聊天容器命名与归属判断冲突。** `dispatcher/runtime/containers.py:665–670` 创建 `container_name(project_id) + "-chat"`，但 `:1545–1558` 只接受研究容器 exact name。创建后的 `:819–823 → :1499–1504 → :1567–1574` 因此拒绝刚创建的合法聊天容器；下一次复用也会被 `_get_container` 拒绝。不能通过重试、换模型、删容器或放宽所有归属检查解决。让研究／聊天容器身份在现有命名、创建、读取与归属边界一致，保留 instance、engine、project、runtime 等实际所有权约束，不接受任意同前缀容器，不混用研究与聊天挂载。补一个能捕获真实归属判断的局部用例，不再以模拟 ContainerManager 的测试代替该边界。
2. **报告待重试被误当终态。** `server/services/assessment_reports.py:1514–1546` 可保存 `failed + next_attempt_at`，调度器仍会按现有策略重试同一代次；`server/services/agent_conversations.py:257–259` 却直接投影 `failed`，`frontend/src/features/projects/useProjectAgent.ts:67–69` 随即停止轮询。正常退避期间打开页面即可触发；后台后续成功仍可能停留在失败卡片，直至手动刷新。基于原 finalization 的权威重试信息投影非终态，复用现有补读，不复制报告状态机、不无限轮询真正的终态。此项为静态调用链核实，未真实发起报告复现。
3. **复测对话读取缺少结构化结论。** `server/services/agent_conversations.py:243–249,394–402` 仅向 `query_agent_work` 透传复测 `summary`，未包含原结果独立维护的 `outcome / tested_scope / limitations`。聊天仅有既有七个项目工具，不能仅凭下载链接读取这些字段；摘要也不保证重复全部范围和限制。直接复用原结果中的必要字段，使 Agent 能基于权威信息回答“是否复现／哪些没测”；不建立第二份正文。这是聊天能力缺口，不是原结果丢失或详情下载失效，尚未观察到真实模型误判。

下一实施会话只修复以上已查明范围，保护现有未提交修改和此次失败记录；保留 R2a 已打通的提交者归属、动作幂等及报告代次隔离。不扩展 R2b／R3，不升级工具层或增加依赖。运行代码修正后按既有方式构建启动完整系统；在用户本轮授权内用少量、短消息验证首轮项目查询与下一轮会话恢复，必要时验证单漏洞既有材料导出。不要为验收触发大项目探索、真实目标复测或大体量综合报告。报告退避与结构化结果字段优先用一个针对性非付费验证覆盖。

**本轮交付边界**：结论为“实现方向符合 R2a，但真实入口验收未通过”，不是“整体完成可用”。未验证真实自然语言工具选择、连续对话、后台业务完成后的真实模型追问或总报告质量。保留一条失败聊天记录及其已创建容器，不删除用户材料；没有新探索／复测／报告任务。当前分支 `feat/update-worker-cache`，本轮仓库仅更新本方案；无 commit、无 push。

### 14.7 2026-10-28：R2a 验收修复与两轮真实只读对话

#### 范围与实际修复

本实施会话仅处理第 14.6 节三项已确认问题，不继承上一分析会话只读角色，也不执行旧整轮提示词。目标是让已有项目能正常开始并继续聊天、报告自动重试期间不会被误判为最终失败、Agent 查询复测时能取得权威结论与范围。未改模型／供应商／Worker 配置，不访问被测目标，不启动探索、真实复测或新综合报告，不开展 R2b／R3。分支现场核对为 `feat/update-worker-cache`；原有大量未提交修改全部保留，下列仅记本轮增量。

- **容器归属**：在原 `ContainerManager` 集中 `chat_container_name`，调度声明与创建使用同一命名。`_owns_container` 根据原 `cairn.scenario_key=project-chat` 选择聊天 exact name，其他场景仍使用研究 exact name；继续验证完整 instance、engine、project 标签，创建后检查及复用的 runtime signature 比较未放宽。不按同前缀或任意 `-chat` 后缀放行，不关闭检查、不改成研究容器名、不重建容器规避问题。
- **实际生命周期**：核对创建后重读、按名称／ID 读取、状态 reload、启动已停止容器、文件读写／移除、exec 构造，以及 managed 列举、项目反查与 orphan 调用方，均经原归属方法。研究 stopped／completed 收尾仍只处理研究容器，不因项目已经停止而停止聊天或混入聊天挂载；聊天 workspace、control/session、prompts 保持原独立持久目录。没有删除上一轮创建的聊天容器或业务材料。
- **报告重试投影**：仅在当前动作代次与 finalization 匹配，且原状态 `failed`、权威 `next_attempt_at` 非空时，投影为已有 `queued`，同时保留原错误与重试时间；到期但未 claim 仍不是终态。页面显示“等待重试”和下次尝试时间，复用原 5 秒补读与既有读取失败边界；成功／最终失败停止补读。`query_agent_work` 和页面共用 `_action_work`，不复制报告调度策略，不另加状态机。旧 accepted 代次仍不能成为本次下载成果。
- **复测结果消费**：`result` 直接取原结果的 `outcome / summary / tested_scope / limitations / reason_code`；前端类型复用已有 `VerificationResult` 的必要字段。未完成时仍为 `null`，已接受结果按原 outcome 区分，不从摘要猜测或继承来源漏洞结论。页面仅显示原摘要，保留详情／下载入口，不铺开内部字段或复制正文；工具桥本来透传业务响应，故无需修改 Worker 应用或指引。

#### 本轮局部非付费验证

以下为本实施会话实际执行（其中结果消费测试由本会话委派子任务执行），不引用第 14.2 节旧成绩作为本轮成绩。未运行全量测试、独立全量 lint 或浏览器矩阵，未新增依赖。

| 验证 | 本轮结果与边界 |
| --- | --- |
| `test_pi_container_ownership.py -k 'chat_container or creation_uses_separate_owner_namespaces or same_identity_reuses_only_owned_container'` | **9 passed，169 deselected，8.68s**。使用真实 ContainerManager，仅替换 Docker 传输；覆盖聊天创建后原归属检查、运行／停止态复用、按 ID 读取、文件 IO、exec 构造、独立持久挂载、研究收尾不影响聊天，以及错误 instance／engine／project／scenario／exact name／runtime 被拒绝。不是 mock ContainerManager 的成功路径。 |
| `test_agent_chat_execution.py -k existing_pool_claims_bound_worker` | **1 passed，9 deselected，3.34s**。更新调度测试的命名接口，核对原调度池接线。 |
| `test_agent_business_actions.py`，`CAIRN_TEST_POSTGRES_BOOTSTRAP=1` | **1 passed，43.47s**。既有一次性 PostgreSQL：真实报告 finish 产生退避、到期再 claim、不可重试终态；页面／工具投影一致、保留原错误和旧代隔离；真实受控复测结果接受后必要字段与后续轮次完整透传，未完成请求无本次结论。成功／被替代报告分支用合成权威记录检查投影，不调用模型生成报告。 |
| `ProjectAgent.component.test.ts -t '报告退避\|会话恢复后独立读取业务状态\|新业务引用立即补读'` | **4 passed，8 skipped**。退避继续补读，经 running 到成功／最终失败后停止；保留错误、重试时间、旧代下载隔离；复测结构结果仍显示原摘要。 |
| 既有真实聊天容器的只读检查 | 修复后的真实 `_owns_container=True`、`inspect_state=running`，按原容器 ID 读取成功，反查仍为 `proj_060`。未执行容器删除、目标请求或模型调用。 |

实例 venv 没有 pytest，首次 `python -m pytest` 因缺少该模块未运行；随后复用系统 pytest 与实例已有 site-packages，`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`，没有安装测试依赖。只读归属检查首次因临时构造 `ContainerConfig` 漏填必需的 `completed_action` 未执行，补齐该参数后通过，未改业务配置。

#### 完整系统构建、部署与加载

- 以 `runtime/arena.env` 指向的实际 PostgreSQL 实例为准；重启前只读确认在途 TaskRun、待领取聊天消息、待执行／退避报告和未终态独立复测均为 **0**，项目均 stopped／completed。未中断其他执行，未清库。
- 正式运行 `npm --prefix cairn/frontend run build:server -- --emptyOutDir false`，构建内的 `vue-tsc -b` 与 Vite 均通过（Vite 34.79s）；保留已有大 chunk 警告，不扩大打包重构，不额外重复类型检查。
- 正式静态资源同步到实际 `runtime/static`，保留已有静态文件；通过 `deploy/stop.sh`、`deploy/start.sh` 启动完整 Server／Dispatcher 及所需依赖。`deploy/status.sh` 为 Server／Dispatcher running、HTTP API ready、heartbeat idle；实际 HTTP 首页与本轮构建 index、runtime index 一致。
- 本轮仅修改宿主 Python 和前端，**未重建 Worker 镜像**、未重装工具或依赖。原聊天容器 ID 仍以 `dadbafb2f0d1` 开头，镜像仍为第 14.3 节的 `sha256:3e2b3d49…`，原持久挂载保留。没有用重建环境代替修复。

#### 少量真实模型验收

使用已有 Playwright／系统 Chromium，从正常 `http://127.0.0.1:8001/#/projects/proj_060/agent` 页面登录、选择既有 **pi-ds-1**，新建会话 `conv_647056ef726f4da4b81d056b6ee3fcfc`。两轮均由页面提交、平台调度和工具桥执行，不直接调用 provider；新会话复用上一轮留下的同一个聊天容器，旧失败会话及 `tr016` 原样保留。

| 轮次 | 实际执行与平台接受 | 平台记录的 token 用量 |
| --- | --- | --- |
| 消息 2／`tr017` | 仅要求读取既有项目状态和漏洞列表、简短回答并记住合成验收标记。`process_started=true`，session 原件记录 `query_project_progress`、`list_vulnerabilities` 两次工具调用均 `is_error=false`；平台保存 `succeeded` 与公开回复，答复 stopped、2 条既有漏洞。没有新业务动作或导出成果。 | input **2924**，output **167**，cache read **1280**，cache write **null**。 |
| 消息 3／`tr018` | 同一 session 续聊，不重复提供标记，要求只沿用上一轮信息。`process_started=true`，准确恢复合成标记、漏洞数和项目状态；本轮没有工具调用。平台保存 `succeeded` 与公开回复。第二条已受理后立即刷新仍保留消息；完成后再次整页刷新，2 条消息及2份回复均恢复，浏览器 page errors 为空，已实际查看截图。 | input **165**，output **114**，cache read **2816**，cache write **null**。 |

合计记录 input **3089**、output **281**、cache read **4096**；保留平台各字段口径，不将 null 写成零，也不编造费用。SDK 原件的 cost 为零占位，不能据此声称免费或实际账单为零。没有必要追加第三条导出消息，故本轮没有重新验证单漏洞导出。

首次浏览器驱动未正确等到消息终态，把仍为 `queued` 的读取误作失败而提前退出；只读核对发现原消息随后正常成为 `tr017/succeeded`。修正为等待页面结果后恢复**同一已受理消息**继续验收，没有重送首条或新增模型重试；该驱动问题不记为平台或模型失败。

验收后在途 TaskRun、待终态独立复测及这两条消息的业务动作均为 0；Server／Dispatcher 仍 running、heartbeat idle。旧 `tr016` 仍是 `failed / chat_execution_failed`、`process_started=false`、`usage=null`，没有改写失败证据。

#### 本轮文件、增量审阅与剩余边界

- 容器与调度：`cairn/src/cairn/dispatcher/runtime/containers.py`、`cairn/src/cairn/dispatcher/scheduler/agent_chat.py`。
- 服务与前端：`cairn/src/cairn/server/services/agent_conversations.py`、`cairn/frontend/src/api/projectAgent.ts`、`cairn/frontend/src/features/projects/useProjectAgent.ts`、`cairn/frontend/src/features/projects/ProjectAgent.vue`。
- 定向测试：`cairn/tests/test_pi_container_ownership.py`、`cairn/tests/test_agent_chat_execution.py`、`cairn/tests/test_agent_business_actions.py`、`cairn/frontend/src/features/projects/ProjectAgent.component.test.ts`。
- 文档／生成物：本文件文首和第 14.7 节；正式构建更新 `cairn/src/cairn/server/static/index.html` 及本次内容哈希资源，并同步实例静态目录。没有把原有未跟踪文件的全部内容或相对 HEAD 的其他差异计为本轮成果。

已审阅本轮精确增量、相关 `git diff`，定向差异空白检查通过。当前分支 **`feat/update-worker-cache`**，**无 commit、无 push**。

**未覆盖／TODO**：这两条短消息只证明本次只读工具与连续会话／持久化链，不证明真实复测、综合报告生成、后台业务完成后的真实模型追问或漏洞发现质量。报告重试和复测字段以本轮非付费受控验证为准，没有在生产库制造合成任务。未续发上一轮模型启动前失败的旧会话，不声称缺失 session 的恢复已经验收；未做多浏览器／尺寸矩阵。保留现有构建大 chunk 提示。本轮到此结束，不自动进入下一阶段。

## 15. 2026-10-28：Chat 界面重做与聊天执行退出运行日志（2026-10-29 已实施并有限验收）

### 15.1 用户反馈、已核对事实与范围

用户明确反馈当前 Chat UI 太丑，要求检索 GitHub／开源组件的近期设计，不再沿用现有聊天外观；聊天执行不要在运行日志里展示。本轮不是继续增加 Agent 能力，而是把已跑通的对话做成真正适合阅读、输入和跟踪成果的工作界面。

本分析会话实际打开第 14.7 节的会话，查看页面和截图，并通过正常已登录接口只读确认消息 2／`tr017`、消息 3／`tr018` 均为 `succeeded`、有持久化回复、无业务动作。没有重新调用模型。第 14.7 节的测试、构建和原始模型执行属于上一实施会话证据，本轮未重跑；不再把第 14.6 节的三个已修复问题列为下一轮工作。

当前视觉问题不是颜色单一，而是信息结构仍像管理表单：会话标签与 Select 占一行、重复“你／Agent 回复”、正文缺乏合适阅读宽度、输入框和发送行分离、每轮都有跳往运行日志的“查看执行”。本轮允许替换这些结构；不要求兼容旧 Chat 外观，也不借此重做无关页面。

### 15.2 公开设计参考与观察边界

观察日期为 **2026-10-28**。已进行公开搜索并实际打开以下官方文档、仓库或演示；未向外站发送 Arena 源码、截图、日志、凭据或业务数据，未向演示发送模型消息、安装依赖或执行外站脚本指令。

| 参考 | 本轮实际核对 | 采用与不采用 |
| --- | --- | --- |
| [shadcn-vue AI Elements / Chat](https://www.shadcn-vue.com/docs/ai-elements/chat)、[官方仓库](https://github.com/unovue/shadcn-vue)、[Changelog](https://www.shadcn-vue.com/docs/changelog) | 已读 Chat 结构说明与示例，查看其空态截图；官方 Changelog 明确列出 **October 2026 – Trace, Code Block & AI Elements**，并关联 July 2026 的 Message／Message Scroller／Bubble 等聊天组件。未实测外站发送或流式输出。 | 主参考：对话阅读列、轻量用户气泡、独立 assistant 正文、一体化输入、用户离开底部时不强抢滚动。借鉴组合与交互，不运行 CLI 安装／迁移，不引入它的 AI SDK、Comark、Shiki 或 Trace 运行时。不能因示例有 reasoning／streaming 就伪造本平台能力。 |
| [assistant-ui](https://www.assistant-ui.com/)、[官方仓库](https://github.com/assistant-ui/assistant-ui) | 实际展开首页嵌入演示的 Full screen 并看图：窄历史栏、精简顶部、一体化居中输入表面、克制层级。观察的是空态，不是实际聊天完成效果；未核验其最新 release／commit 日期。 | 参考输入与阅读比例、减少重复边框和标签；Arena 已有全局侧栏，不再默认叠一根常驻历史栏。不采用其 React／云运行时、模型选择器、附件、语音等未在本轮支持的控件。 |
| [Nuxt UI Chat 教程](https://ui.nuxt.com/templates/chat)、[Vue Chat 仓库](https://github.com/nuxt-ui-templates/chat-vue) | 教程页面标示 **Dec 16, 2025**，只定向读了正文与链接；实际查看 Vue 仓库 README 和页面时间元素，其中显示 `2026-10-28T10:52:10.000Z` 的更新记录。这不等于核验了最新发行版或所有提交。 | 辅助参考：历史导航与对话输入组合、Vue 下的组件边界。不迁移 Nuxt／Nitro／数据库／鉴权／AI Gateway，不照搬完整模板，不把模板功能当作 Arena 已支持功能。 |

结论：以 **shadcn-vue 当前 AI Elements 的对话结构**为主、assistant-ui 的阅读和输入比例为辅，做 Arena 自己的 Vue 实现。可以准确称为参考了 2026 年 10 月公开更新的模式，不声称“全网最新”或已经复刻了外站全部交互。

### 15.3 唯一推荐布局与交互

采用 **轻量会话头 + 单列阅读区 + 底部一体化输入器**，不是旧表单换色：

1. **会话头**：当前会话标识／名称、紧凑历史切换入口、新建会话；历史默认通过已有浮层模式打开，不新增常驻第二侧栏。使用现有会话字段，不为美化自动调用模型命名、不新增命名协议。Worker 选择仍留在已有新建流程，当前绑定信息按需可读，不放一个实际不可切换的模型下拉框。
2. **阅读区**：正文置于有舒适最大宽度的居中阅读列，用户内容是右侧弱底色气泡，assistant 内容左对齐且以正文为主，不套一层大型回复卡片。时间和次要动作降低视觉权重，但保留可访问名称与实际状态。删除每轮重复标题、粗分隔和常驻“查看执行”；不是删除原始对话内容。
3. **正文呈现**：assistant 支持基础 Markdown 段落、列表、标题、行内代码、代码块和表格；用户输入保持原文。代码／长行局部横向滚动，不能撑宽页面。复制回复应复制原始文本，不把渲染后的摘要当原件。原始 HTML 不执行，外部图片不自动加载；模型正文中的链接不是平台动作或下载授权，正式成果入口仍只来自已验证的服务端字段。
4. **一体化输入**：输入内容、发送动作、必要的快捷键／费用提示在一个紧凑编辑表面内；去掉外置“消息”标签的视觉占位但保留可访问标签。输入随内容适度增高并设上限。保留 Ctrl／Cmd+Enter 发送、普通 Enter 换行；输入法组字不能误发送。待受理不确定时沿用同一 `client_message_id` 重试，排队时呈现真实“发送并排队”，不禁用已经支持的排队能力。费用提示一句短文即可，不把必要边界全部藏起来。
5. **空态**：短标题与少量已有能力的示例入口即可，不堆功能宣传。建议使用“查看项目进展”“列出已有漏洞”；点击只填入草稿，不自动提交或产生费用。
6. **滚动**：只有消息区承担纵向对话滚动，输入器稳定可见；阅读旧消息时后台更新不拉回底部，提供明确的“回到最新”入口。新建／切换会话、恢复历史和切换草稿不得混入另一个会话的数据。窄屏不被全局侧栏、表格或输入器挤出横向页面滚动。
7. **状态、工作与成果留在会话内**：排队／运行／失败／状态待确认就地呈现；低频错误码与 TaskRun ID 可在该轮诊断展开，不展示原始运行终端或私有执行轨迹。`execution_unconfirmed` 的警告必须可见且不声称已停止。业务动作放在所属回复附近的紧凑工作项中，保留等待重试、下次尝试、真实错误、复测摘要、业务详情和可信下载入口；旧报告不冒充本轮成果。没有动作就不渲染空工作区。

颜色、字号、间距与控件语义遵循共享设计规范；**共享基础不等于旧页面形状必须保留**。若确需调整相关共享默认值，应在共享层和规范中说明最小差异，不叠页面私有主题，也不借机改无关页面。深／浅主题均保持可读，使用简体中文与 Lucide；不做霓虹、渐变宣传区、虚构打字机输出或思考轨迹。

### 15.4 运行日志职责调整

**只将 `task_type=agent_chat` 从项目“运行日志”的页面展示集合排除，保留后台执行事实。**

- `useRunsPage.ts` 当前从 `GET /projects/{id}/task-runs` 取得完整数组，没有分页／总数协议。最直接实现是在该页面的基础 `runs` 投影排除 `agent_chat`，使列表、搜索筛选、默认选择、详情选择与刷新统一使用同一可见集合；不要只在模板藏行或仅改末端 `filteredRuns`。
- 原接口和数据库 TaskRun 保留，聊天状态、审计、调度并发和用量统计不变；不删除历史 `agent_chat`，不改成其他 task_type，不在 API 全局截断执行记录，也不另建聊天日志表／状态机。
- 删除 `ProjectAgent.vue` 每轮“查看执行”的运行日志跳转；将“请查看执行记录”改为准确的会话内状态说明与按需诊断。诊断仅使用已有可公开字段，不因移走链接把错误吞掉。
- 旧 `/runs?runId=<agent_chat>` 链接是当前界面实际已经生成过的入口：加载成功并确认该 ID 属于聊天后，不打开聊天详情，清除不可选中的 runId，给出简短提示和项目 Agent 入口；不自动选中另一轮冒充原目标。不为了反查具体 conversation 新增后端映射。网络失败不应误判为“这是聊天运行”。
- 只按实际 `task_type` 排除聊天轮次，不按“由对话发起”排除任务：真实 `report` 运行仍保留，`external_verify` 在其所属复测执行项目／详情内照常可达，复测和报告业务记录及成果不删除。
- `ProjectWorkspace.vue` 的运行标签计数当前来自 Worker 绑定的 active Intents，不是 TaskRun 总条数；不顺手改统计口径。全局监控、调度可观测性不是本轮删除范围，不做全站 `agent_chat` 隐藏。

### 15.5 实施位置与保留的工作逻辑

主要位置：

- `cairn/frontend/src/features/projects/ProjectAgent.vue`：页面组合、消息／工作呈现、输入与会话内诊断；必要时拆少量直接服务本页面的组件，不创建通用聊天平台。
- `cairn/frontend/src/features/projects/useProjectAgent.ts`：只调整本轮必要的显示／滚动／输入连接，不重写已工作的会话、幂等发送、草稿、SSE 有界重连、业务结果补读及退避逻辑。
- `cairn/frontend/src/features/runs/useRunsPage.ts`、`RunsPage.vue`：页面可见运行集合和实际旧链接行为。
- 现有依赖已有 `markdown-it`；`src/components/MarkdownPreview.vue` 当前只是 `<pre>` 原文，不是现成富文本渲染器。`src/features/reports/safeReportMarkdown.ts` 有禁用 HTML、链接和图片处理，可参考／提取确实共用的最小部分；不能直接假设其报告图片／相对链接策略适合聊天，也不为这一页重构整个报告系统。无需新增 Markdown、代码高亮或 UI 依赖。
- `ProjectWorkspace.vue`、共享 UI／tokens 仅在实际可用空间、浮层或相关基础样式确有阻碍时定向修改。按实际变化更新原有相关测试断言，不为保住旧 DOM 结构扭曲新布局。

不改 Server／Dispatcher／Worker 执行链、模型供应商／Worker 配置、业务动作合同、数据库后端；若实施中发现确实必须改变协议，先报告依据，不用新增后端设施完成本可由页面投影解决的事情。不进入 R2b／R3，不实现 token 流式协议、附件、语音、重生成、编辑历史或新的执行控制。

### 15.6 验收、交付与未覆盖项

优先验证真实页面，不把测试全绿当作视觉验收：

- 用第 14.7 节已有成功会话和旧失败会话确认读取、切换、刷新、错误诊断，不为看历史再调用模型。实际看桌面和一个窄视口截图，并操作历史入口、输入、复制和滚动，检查长正文／代码不会挤坏布局；必要的合成内容只放非生产受控验证，不向生产库写假消息／任务。
- 运行日志不展示既有 `tr016`／`tr017`／`tr018` 聊天轮次，旧聊天 runId 链接不打开详情；原始接口记录仍存在，真实非聊天运行仍能选择和查看。没有现成 report／external_verify 时用局部受控数据验证类型投影，不为此发起真实报告或复测。
- 需要验证发送交互时，最多 1–2 条短只读消息，复用平台已配置模型和正常 UI；用同一次发送观察排队／运行／接受状态，不追加无目的付费重试。不得启动探索、真实目标复测或新综合报告，不访问外部目标。
- 确认旧的幂等发送、按会话草稿、SSE 恢复与报告退避消费未被 UI 重做破坏。浏览器不能直接覆盖的实际分支，最多补跑对应局部用例；不默认跑整套测试、lint、类型检查组合，不重复构建内校验。
- 按现有部署路径构建并使完整系统可用；重启前看有无在途工作，不中断用户任务。仅前端变化不重建 Worker 镜像、不重装工具层或依赖。没有用户批准不 commit／push。
- 回填本节之后的实施记录：实际增量、部署、真实页面截图观察、已测／未测行为和剩余限制。用户审美认可仍需实际页面反馈，不能把参考网站效果说成 Arena 已实现。

本分析轮仅更新本文文首和第 15 节，未改业务代码、配置或 UI，未运行自动化测试、构建、重启、模型调用或提交。当前分支为 `feat/update-worker-cache`。第 14.7 节关于真实复测／报告模型链的未验收边界保持，不将其混入本次 UI 交付承诺。

### 15.7 新实施会话提示词

```text
本轮是实施会话。请直接按 docs/specs/project-agent-capabilities-design.md 第 15 节完成“Chat 界面重做与聊天执行退出运行日志”，不要重跑第 13 节旧提示词或第 14.6 节已完成修复，不进入 R2b／R3。

先读 CLAUDE.md、AGENT.md、docs/ui-design-standard.md、docs/analysis-design-handoff.md，以及原设计第 14.7／15 节。修改前执行 git status --short、git branch --show-current；保护当前大量未提交修改，只做本轮增量，工作在任务分支。

用户明确不接受当前 Chat 外观。主参考第 15.2 节已核对的 shadcn-vue 2026 年 10 月 AI Elements，辅以 assistant-ui 的阅读／输入比例。不是旧 Select＋表单＋回复卡片换色：实现轻量会话头和历史浮层、居中阅读列、用户气泡与 assistant 正文、一体化底部输入、真实空态与滚动反馈、所属轮次内的工作／成果和诊断。可重新打开参考核对必要细节，不无限调研，不上传本项目数据到外站，不安装模板或依赖。

保留现有会话持久化、Worker 绑定、会话草稿、同 client_message_id 重试、排队、SSE 有界重连、业务补读、报告等待重试／代次隔离、复测真实摘要与可信下载。基础 Markdown 用现有依赖和局部安全渲染，不执行模型 HTML、不自动加载外部图片、不把模型文字链接变成业务动作。不得伪造流式输出、工具轨迹或未支持的按钮。

项目运行日志的基础展示集合排除且只排除 task_type=agent_chat，统一覆盖列表、筛选、详情选择和刷新；处理既有聊天 runId 深链接。Chat 不再跳到运行日志，状态和必要诊断留在会话内。后台 TaskRun、原 API、调度／用量／审计不删不改；实际 report／external_verify 不因来源是 chat 而被隐藏。不要新增后端过滤框架或聊天日志系统，不改全局监控。

按第 15.6 节有限验收：优先操作真实页面并实际看桌面和一个窄视口截图，复用已有成功／失败会话；需要时最多 1–2 条平台现有模型的短只读消息，无大任务、真实复测、综合报告或探索。只补不能直接验证的局部用例，不全量跑测试套餐。按项目路径构建并启动完整系统，不中断在途工作；不重建未变化的 Worker 工具层，不新增依赖。

完成后审阅本轮 git diff，在原设计文档回填实际实施和验收，不新建平行方案。报告当前分支、本轮文件、验证与结果、是否创建 commit、剩余风险／TODO。未经批准不 commit／push。
```

### 15.8 2026-10-29 实施、部署与有限验收

本实施轮只完成第 15 节的 UI 与项目运行日志展示调整。在 `feat/update-worker-cache` 上工作，修改前核对状态并保护既有大量未提交内容；未创建 commit，未 push。以下是本轮增量，不把已有 R2a、流量诊断或其他工作区修改算成本轮成果。

#### 实际实施

- `ProjectAgent.vue` 改为轻量会话头、带时间和 Worker 信息的历史浮层、居中有限宽阅读列、右侧用户气泡、左侧 Agent 正文及一体化底部输入器。新建会话沿用原 Worker 绑定流程；空态建议只填草稿。没有常驻第二侧栏、模型选择假控件或虚构流式／工具轨迹。
- 消息区独立滚动，底部输入器稳定可见；阅读旧消息时不因更新被拉回，提供“回到最新”。输入自动增高并设上限，普通 Enter 换行、Ctrl／Cmd+Enter 发送，过滤输入法组字。回复复制原始 `answer`，不复制渲染 DOM；保留明确的复制失败反馈。
- 新增局部 `AgentMarkdown.vue`、`agentMarkdown.ts`，复用已有 `markdown-it`，支持段落、列表、标题、代码和表格。原始 HTML 不执行、图片不请求，模型链接仅呈现文字和目标地址；代码与表格局部横滚。正式成果仍通过原有服务端字段及 `agentDownloadUrl` 校验，不把正文链接当授权。
- 删除每轮运行日志跳转。错误码与 TaskRun ID 留在对应轮次的可展开诊断内；`execution_unconfirmed` 警告保持可见。原工作项的报告等待重试、下次尝试、错误、代次隔离、复测摘要与可信下载保留。
- `useProjectAgent.ts` 未改：会话持久化、按会话草稿、同 `client_message_id` 重试、排队、SSE 有界重连和业务状态补读继续使用原实现。未改共享主题、项目工作区布局、依赖或后端协议。
- `useRunsPage.ts` 在基础 `runs` 投影只排除 `agent_chat`，供列表、搜索、状态筛选、选择与刷新共同使用。仅成功取得真实类型后识别旧聊天深链接，清掉不可选择的 `runId`、不选其他运行代替目标；`RunsPage.vue` 提供简短说明和项目 Agent 入口。请求失败或未知 ID 不猜测类型。
- 后台 TaskRun、原 API、调度、用量、审计与全局监控不变；`report`／`external_verify` 不因来源是 Chat 而被过滤。没有新增后端过滤框架或聊天日志系统。

本轮源码／测试文件为：`features/projects/` 下的 `ProjectAgent.vue`、`ProjectAgent.component.test.ts`、新增 `AgentMarkdown.vue`、`agentMarkdown.ts`、`agentMarkdown.test.ts`，以及 `features/runs/` 下的 `useRunsPage.ts`、`RunsPage.vue` 和各自的 `.component.test.ts`（均位于 `cairn/frontend/src/`）。另更新本文与正式前端构建产物。

#### 真实页面与受控内容

- 使用本地正常登录和既有成功／失败会话，未新增会话或发送模型消息。成功会话两轮可读取；历史浮层切换失败会话可展开原错误码和 TaskRun ID，且没有运行日志链接。切换会话后草稿隔离并可恢复，清空草稿后整页刷新保持会话及历史。
- 操作空态建议确认仅填草稿；打开新建对话框核对既有 Worker，随后关闭，未提交。隔离浏览器中复制公开回复后，剪贴板内容与该次回复的原始 `answer` 一致。
- 实际查看 **1440×1000 桌面**及 **480×820 窄视口**截图，包括浅／深主题。窄屏通过正常按钮关闭移动主导航，检查输入器位于视口内、页面没有横向溢出；上滚后出现“回到最新”，点击可回底。根据首轮截图微调共享 token 的气泡底色及输入器对齐，再次构建发布，没有新增私有主题。
- 项目运行日志原接口仍返回 18 条记录，其中 3 条 `agent_chat`、3 条 `report`。页面只显示其余 15 条，搜索 `agent_chat` 无匹配；清空搜索恢复 15 条。旧聊天深链接清除 `runId` 并显示提示，可进入项目 Agent；真实报告深链接仍可选择、打开详情并在刷新后保持选中。最后隔离浏览器检查无 `pageerror`、无聊天 POST。
- 既有会话没有足够长的代码／表格和等待重试样本，因此只在隔离浏览器拦截特定 GET 响应，使用非敏感受控内容验证，不写生产库、不启动业务。480px 视口下页面宽仍为 480px，代码与表格在各自容器横滚；未产生 `a`／`img`／`script` 正文节点或样本外部请求，状态未确认、报告等待重试及“上次报告不是本轮成果”提示可见。这不是实际报告生成或复测验收。
- 截图仅保留在本机临时目录供查看，未上传外站或加入仓库。实际模型调用为 **0**；新增探索、真实复测、综合报告任务均为 **0**。

#### 局部测试与部署

| 检查 | 实际结果与范围 |
| --- | --- |
| Chat 组件限定用例 | **11 passed、3 skipped**。覆盖 URL 恢复、同 ID 受理重试、排队、SSE 恢复／重连预算、状态未确认、会话隔离、报告退避／代次隔离、业务补读与可信下载，并新增 IME 与不抢滚动检查；未跑全部组件套件。 |
| Markdown 局部单测 | **1 passed**。基础格式、HTML 转义、图片不加载、链接纯文本及表格容器。 |
| 运行日志限定组件用例 | **3 passed、27 skipped**。基础投影、筛选／选择／刷新、保留 report／external_verify、请求失败后成功确认旧链接以及提示入口。 |
| 正式前端构建 | 两次 `npm --prefix cairn/frontend run build:server -- --emptyOutDir false` 均通过，含 `vue-tsc -b` 和 Vite；第二次包含看图后的微调。仍有大 chunk 和插件耗时提示，本轮未做无关拆包。 |
| 静态发布和系统状态 | 构建至 `cairn/src/cairn/server/static`，将新增内容哈希资源发布到 `runtime/static`，原子替换入口并保留旧资源。`deploy/status.sh` 确认 Server、Dispatcher running，HTTP API ready。未重启后端、未中断在途工作、未重建 Worker 镜像／工具层、未安装依赖。 |

测试与工具过程中的失败没有计为通过：首次局部 Vitest 与构建并行时出现 `Failed to start forks worker / Timeout waiting for worker to respond`，没有执行用例；构建完成后串行重跑上述限定用例通过。DevTools 窄视口模拟遇到协议错误且初始 resize 未得到真实 480px，改用现有 Playwright／系统 Chromium 的隔离 context。临时驱动先后因移动导航遮挡和将运行列表误定位为 table 而超时，正常关闭导航、改用实际 `.run-row`／`.run-details-panel` 后完成检查；已通过的 Chat 段没有为运行日志定位修正而重复执行。

已审阅本轮精确增量及空白检查：对原有未跟踪 Chat 文件使用修改前临时基线区分本轮变化，运行日志文件不回退或认领既有流量诊断改动；未把全工作区相对 HEAD 的差异当成本轮。

#### 剩余边界

- 视觉结构和有限交互已实现并实际看图，**用户审美认可仍待真实页面反馈**，不是用户已确认通过。
- 本轮没有重跑真实模型发送；相关发送／恢复分支依靠保留实现与局部用例，不宣称重新验证了完整模型链。没有新建真实复测或报告，没有把受控内容当业务成功证据。
- `external_verify` 的保留通过局部类型投影用例验证，没有为页面验收启动真实复测。剪贴板权限失败有反馈分支，但本轮真实浏览器只验证了允许复制的环境。
- 第 14.7 节关于真实目标复测、综合报告生成、漏洞发现质量的未验收边界不变。不进入 R2b／R3；后续范围需用户另行指定。

## 16. 2026-10-29：验收未通过与独立项目 Agent 纠偏（设计，待实施）

### 16.1 当前结论与用户目标

用户反馈：视觉仍像旧界面、发送按钮无效；新建会话不应要求选择三个研究 Worker，研究运行时仍需独立对话；Agent 要读取当前项目容器内各研究会话状态，接收附件，单独执行用户任务，生成符合本次要求的可下载报告，并调用复测、任务创建等系统能力，不影响正在运行的 Worker。UI 以最大化窗口效果验收为主，不要求缩小窄窗。

**结论是未达到预期，而不是“上一轮测试通过所以已完成”。** 上一轮确实实施了气泡／Markdown／历史浮层等结构和运行日志分离；独立 Agent 并未实现，视觉也未获得用户认可。发送未经过本轮实际入口验证是验收缺口；历史成功不代表所有部署入口均可发送。本轮是产品定位纠正，不应再缩成“修按钮 + 改几个圆角”。

本分析轮实际复核：运行日志原接口仍有 18 条 TaskRun，其中 3 条聊天、3 条报告；页面 15 行且不含聊天，故该项保留，不回退后台执行记录。两条历史成功回复仍在。以下其他源码机制由定向只读查证确认，不冒称已进行研究／聊天同时运行实验。

### 16.2 发送失败：已复现的确定性入口问题

在同一实际实例的局域网 IP、HTTP 入口正常登录，打开既有成功会话，输入一条仅要求回复“收到”、禁止工具和任务的短消息，然后实际点击发送：

- 页面草稿不消失，也没有错误提示；控制台报 **`TypeError: crypto.randomUUID is not a function`**。
- 该页面 `isSecureContext=false`，`typeof crypto.randomUUID='undefined'`，但 `crypto.getRandomValues` 可用；对照 localhost 页面，`isSecureContext=true` 且 `randomUUID` 可用。
- 未发出聊天消息 POST；随后正常 GET 确认会话仍只有消息 2、3，没有产生新 TaskRun 或模型执行。只点击一次后停止；清除了本次验证草稿，没有改用户消息。
- 直接原因在 `cairn/frontend/src/features/projects/useProjectAgent.ts:217–227`：生成 `client_message_id` 调用 `crypto.randomUUID()`，发生在 try/catch 之外。它在普通局域网 HTTP 浏览器上下文不可用，所以事件异常在请求前发生，页面也未设置可见错误。
- 这证明当前部署存在与用户描述一致的真实故障路径；用户未给出其地址和控制台，不能声称已经证明其每次失败都由同一个原因引起。此前 localhost 验收无法覆盖该路径。登录前出现的 401 不是这次发送失败原因。

下一实施应使用支持实际部署入口的随机 ID 生成方式，复用现有能力；若没有公共实现，可用可用的 `crypto.getRandomValues` 生成满足现有合同的 UUID，不能退成时间戳／弱随机或每次重试重生成 ID。生成及发送失败都进入用户可见的错误边界，保持原草稿和幂等语义。不能只靠让用户改用 localhost、浏览器不安全例外或强制改部署来掩盖普通入口错误。是否统一 HTTPS 是另一部署决定，不是当前按钮修复的前提。

### 16.3 为什么反复变成旧风格：规范与上轮方案共同约束

本轮实际看了 **2554×1348 CSS px** 的完整桌面视口截图，尺寸等于当前显示环境可用屏幕面积；`devicePixelRatio=1`、`visualViewport.scale=1`。这是等效最大化视口，不冒称操作系统原生最大化状态。未安排窄窗测试。

页面仍使用原全局导航、面包屑、项目标题、Tabs 和旧浅蓝灰表面；Agent 区宽约 2306px，但阅读列固定 768px、输入外层 816px，正文 14px。整体观感仍由旧管理工作区支配，变化集中在中间一列。合理控制行长没有错，但“一个窄阅读列 + 大面积旧背景”本身不能兑现新工作台设计。

影响来自两个层面：

1. 规范虽然允许重做，却反复强调“使用已有 tokens／共享控件”“柔蓝、细线、小圆角、紧凑”“不动普通页面”，具体实现容易优先遵循这些旧值。共享样式的维护位置被误用成必须保持外观。
2. 本分析会话上轮设计明确保留 Worker 绑定、限制共享／外层改动，并排除了附件等能力。实施记录也明确“未改共享主题、项目工作区布局”。因此问题不全在实施者；上轮方案把产品与视觉空间限制得过窄，本节纠正这些限制。

已按用户要求更新 `AGENT.md` 和 UI 规范：共享维护不冻结颜色、字号、形状、导航占位或工作区外壳；明确的视觉重做可以修改相关外层和共享变体，并控制无关影响。最大化桌面为主要验收，窄窗／移动端不构成默认要求；旧历史截图与窄屏记录不删除，也不再作为本轮门槛。

下一版目标是**项目 Agent 工作台**而非“原项目页内嵌一列聊天”：

- Agent 视图合并冗余的对象标题／工具条层级，在紧凑头部保留项目身份、研究状态、历史和新会话；研究详情入口仍可达，但不让三层后台栏长期压着聊天。可定向修改 `ProjectWorkspace` 的 Agent 布局，其他标签业务不重做。
- 以最大化实际工作面积组织对话、输入和按需资料／成果查看；文字尺度与留白重新确定，不机械保持 14px、48rem 或旧蓝灰。普通短对话不为了填屏建立空面板，附件／报告查看时能使用旁侧空间，而不是挤在小气泡里。
- 输入器承载实际支持的附件与任务输入，历史、上传状态、成果下载属于同一工作流。没有接线的能力不先放假按钮。
- 继续参考第 15.2 节已经核验的公开模式，不重复无限调研；借鉴完整工作区比例与行为，不仅搬一个气泡或输入框。选择具体视觉方向后用完整最大化页面看图迭代，不能再次以“复用了规范”作为审美验收结论。

### 16.4 独立执行：复用内核，不复用研究 Worker 身份和槽位

**当前实际情况：**

- `server/routers/agent_conversations.py:29–39` 创建必须传 `worker_id`；`server/services/agent_conversations.py:50–57,115–128,131–171` 在创建、发送、认领时解析项目 Worker。`server/migrations.py:5084–5114` 的会话 Worker 非空外键表明这不是只改下拉框就能解决的问题。
- `server/services/tenant_workers.py:139–234` 经项目 Worker 绑定解析租户模型与凭据；聊天没有独立的默认模型配置。可复用模型与凭据解析，不应秘密选择第一个 Worker。
- `dispatcher/scheduler/agent_chat.py:17–66` 同时受研究全局／项目／Worker 并发约束；`scheduler/loop.py:99–103` 共用 executor，`worker_runtime.py:546–550` 占用计数包含聊天。聊天排在前面不等于有独立容量：研究满时聊天等，聊天运行也会占研究额度。
- **已有隔离应保留**：每项目一个独立 `-chat` 容器，workspace／control／prompts 独立；各 conversation 有自己的 pi session、排他锁和恢复检查。底层已有 `run_agent_turn → PiDriver → run_worker_process` 与项目工具桥。不需要另一套 Agent Loop、服务、消息总线，也不需要每会话新建一个容器。

**目标设计：**

1. 会话直接使用现有租户模型配置，由平台设置一个明确的项目 Agent 默认模型；新建只创建会话，不让用户先建／选研究 Worker。默认模型缺失时只引导配置模型，不引导创建 Worker；不增加第四个“聊天 Worker”伪装解耦。新会话可继承明确的 Agent 默认配置，不暗中随第一条 Worker 变化。
2. 模型执行参数和凭据复用已有解析及 runner 适配；会话身份、项目归属、真实提交人不变。旧会话从原 Worker 绑定迁移到对应模型引用，保留 ID、消息、session、动作与历史用量；不能删除重建，或永久维护两套聊天模式。迁移处理当前模型不可用时应明确提示，不把历史失败抹掉。
3. 在现有 Dispatcher 内设置小而有界的独立 Agent 执行容量，例如初始独立 1 槽；不占研究 Worker 的 `max_running` 或研究项目配额。executor 必须真正有可用槽位，仅修改一个计数仍会被研究线程占满。保留每会话单写者，不用停止／暂停研究来腾位置，也不凭无限并发实现“独立”。
4. “不影响 Worker”指不抢其逻辑槽位、不 resume／写其 session、不修改研究工作目录、不隐式停止／重启／注入提示。模型供应商速率、宿主 CPU／内存仍是物理共享资源，不能承诺绝对零资源影响。用户明确要求追加研究任务时，原研究调度有新工作是预期业务副作用，不是假称完全无影响。

以上路径相对 `cairn/src/cairn/`。具体字段与迁移遵循当前实际 PostgreSQL，不恢复 SQLite 支持。

### 16.5 上下文可读，不接管正在运行的研究会话

当前七个工具只有项目进度、漏洞列表／详情、单漏洞 Markdown 导出、漏洞复测、终态综合报告请求及动作查询（`container/pi-agent/src/project-tools.ts:15–25`）；`query_project_progress` 不等于读取全部研究会话。聊天 transcript 独立，不会自动看到研究容器里其他 session。

目标是让 Agent 能索引并按需读取**当前项目的所有可用研究执行／会话状态和相关材料**：

- 先列出 Worker、TaskRun、会话来源、运行／终态、更新时间，以及可用事实／Intent／证据索引；再按实际问题读取所需事件、材料和已有 session 的相关原文范围。提供来源和位置，分页／按范围继续读取，不能只返回固定摘要后声称已读全部，也不每轮把全部原始会话塞进模型。
- 复用 `audit_task_runs.py`、`audit_events.py`、runtime 诊断与事实／Intent 查询；现成项目详情服务会调用 `expire_workers()`，不能不加区分地把它包装成“纯只读”。使用无副作用查询，不触发研究清理、恢复、会话写入或资源同步。
- 对运行中 session 的读取沿现有持久记录只读访问，标明读取时点与可用范围；活动尾部尚未落盘、旧容器已清理或记录不可用如实返回，不拿旧摘要冒充实时原件。读取能力不要求研究先停机，也不把研究会话锁拿来写。
- 范围是当前授权项目，不是宿主机、其他租户或任意容器。凭据／认证通道不作为会话上下文输出；保留内部权威原件，仅在访问与输出边界决定可读字段和材料。

### 16.6 用户任务、附件和可下载交付物

这部分是用户本轮明确需求，不再沿用第 15 节“不做附件／新增能力”的排除项。需要形成“用户输入及附件 → 独立 Agent 执行 → 平台真实动作或成果 → 可追踪／下载”的完整路径，而非只给自然语言建议。

| 能力 | 当前事实与复用位置 | 本轮目标与边界 |
| --- | --- | --- |
| 漏洞复测 | 已有 `request_vulnerability_retest`，调用 `verification_requests.create_request`，具备真实操作人和幂等；不等于已验收真实复测模型链 | 保留当前漏洞复测，按需支持读取用户上传材料后走同一复测入口；返回真实业务 ID、状态、结果与链接，不复制复测引擎 |
| 附件 | `project_resources` 有上传／批次／存储基础，但导入研究项目要求 stopped／paused 且无在途 TaskRun，消息合同也没有附件 | 复用存储和校验，增加真实消息附件引用与持久归属；发送前有上传状态、失败重试／移除，刷新和续聊仍可读取。不能只引用会过期的临时批次，更不能要求暂停研究或把附件默认写入研究资源 |
| 自定义报告 | 现有单漏洞模板导出与 Artifact 下载可复用；终态综合报告限制 completed／stopped，当前工具没有用户要求参数 | 接受本次范围、侧重点、章节／表达要求，由独立 Agent 使用真实材料生成额外交付物并持久保存、下载。初始至少打通 Markdown；其他格式仅在已有转换链可复用时加入。自定义一次报告不等于要求 super-admin 修改全局模板 |
| 运行中阶段报告 | 不能直接删终态综合报告的检查 | 单独标明材料读取时点、覆盖范围与未完成事项，保存为本次用户交付物；不覆盖项目权威最终报告，不把运行中快照说成研究完成 |
| 新建项目任务 | `creation_requests.py`、项目创建 service 已有幂等与业务合同，聊天未接工具 | 将用户给出的 Origin／Goal／资料接入同一创建路径；明确“创建”与“启动”各自实际副作用，不能默认扩大目标或自动启动用户没要求的探索。用户已明确授权的正常动作不另造逐步审批 |
| 向当前项目追加研究工作 | 已有 intents 路由，要求活动项目及真实来源／依赖，聊天未接工具 | 与新建项目区分；复用原 Intent 业务事务，不让聊天伪造研究 TaskRun。用户明确安排工作才写入，纯查询不注入任务 |
| 独立完成用户任务 | 当前聊天主要是七个平台工具，不是完整通用工作区 | 在独立 Agent workspace 中复用现有运行器可用的文件／命令工具处理用户材料和形成交付物；依据用户任务与现有目标／费用授权执行，不局限为七个固定问答。不得由此写研究目录、读取凭据或任意扩大目标访问 |

实现仍使用现有项目工具桥与真实 `tool_call_id`、消息提交人和动作幂等；按实际新增业务类型扩展动作引用，不建立新的泛化工作流平台。长耗时业务返回“已受理”及权威引用，后续查询／页面继续跟踪，不能用文本“完成”代替结果接受。相同消息和工具调用重试不能重复创建任务或付费工作。

附件解析支持范围以现有可用工具为准，不新增依赖；不能读取的格式给出明确限制，不能把上传成功说成模型已经理解文件。模型输出文件必须进入现有可信 Artifact／鉴权下载路径，不能依赖模型正文中的虚构 URL。

### 16.7 推荐交付顺序与验收

在同一产品目标下分两轮实施，防止继续只做表面 UI，也避免把所有后端合同同时重写；**B 是明确待交付需求，不是被永久排除的“未来功能”**。

**A：先交付可用的独立项目 Agent 底座与最大化工作台。**

- 修复并验证局域网 HTTP 发送；按钮／快捷键使用同一发送函数，真实错误可见、幂等 ID 稳定。
- 完成模型与研究 Worker 解耦、旧会话迁移、独立调度槽位与既有 session／容器复用。
- 接入研究状态／会话索引及按需读取，保留已有复测／报告工具和运行日志分离。
- 按第 16.3 节重做 Agent 外层与整体视觉，不能只改中间聊天区域。为附件／成果阅读留下合理组合位置，但未完成能力不放假按钮。
- 用真实最大化页面操作新建、发送、续聊、历史和读取；允许少量短模型调用。研究占满时仍可派发聊天的隔离验证，优先使用局部受控执行而不是发起真实研究大任务；分别报告实际并发验证和真实模型验证，不能拼成一次未发生的端到端成功。

**B：基于 A 补全附件、独立用户任务、定制报告和任务创建闭环。**

- 完成消息附件全生命周期、独立 workspace 的材料处理和真实成果下载。
- 复用业务入口完成新建项目／追加 Intent／上传材料复测的实际工具调用和回执；不另造业务副本。
- 以无敏感合成附件、小型用户定制报告为最小演示；任务创建／启动与真实目标复测另按具体目标、费用和副作用授权验证。当前分析会话的轻量模型许可不等于批准启动这些业务。

两轮都按规范构建并使完整系统可用，不中断在途工作、不重下未变化的 Worker 工具层；Worker 应用确有变化时只重建必要应用层。仅用与当前行为对应的最小验证，默认不全量测试，不做窄屏验收。保留失败和历史材料，不新建平行方案，不擅自 commit／push。

A 完成后可以称“独立交互与读取可用”，不能称“完整独立 Agent 已交付”；B 的附件、定制报告与任务动作完成并经过相应验证后，才按实际范围声明能力。

### 16.8 下一实施会话提示词（A）

```text
本轮是实施会话。先读 CLAUDE.md、AGENT.md、docs/ui-design-standard.md、docs/analysis-design-handoff.md，以及 docs/specs/project-agent-capabilities-design.md 第 16 节。修改前检查 git status --short、git branch --show-current，保护所有现有修改，在任务分支工作。

只实施第 16.7 节 A，但必须按第 16.1–16.6 节的完整独立 Agent 目标设计，不能再次缩成按钮修补或旧 UI 换色。B 的附件、自定义报告和任务创建是明确后续交付，不要声称本轮已实现，也不要改为永久不支持。不要重跑第 13／15.7 节历史提示词。

已复现：同一实例局域网 HTTP 入口中 crypto.randomUUID 不可用，useProjectAgent.ts 在 try 外调用使点击发送无 POST、无提示。修复实际入口兼容、可见错误和同 ID 重试，不用换 localhost 或关闭浏览器安全替代。

创建会话不再选择研究 Worker，直接使用明确的 Agent 默认租户模型配置；复用模型凭据解析，迁移旧会话并保留 session／消息／历史用量。独立 Agent 槽位不能占研究 Worker／项目配额，executor 必须有实际容量；不以增加第四个 Worker 或提高优先级冒充独立。保留已有聊天容器、单会话写者、PiDriver 与工具桥，不新建 Agent Loop／服务／队列框架。

接入当前授权项目的研究执行／会话索引和按需原文读取，不接管、resume 或改写研究 session，不要求停止研究；来源、读取时点和不可读状态真实可见。保留已有七个工具、报告退避与代次隔离、业务动作幂等和可信下载。

视觉按第 16.3 节重做完整 Agent 工作台，必要的 ProjectWorkspace 外层和共享变体属于范围；不冻结旧蓝灰、14px、48rem 或三层后台栏。共享 tokens 是维护位置，不是必须像旧界面的理由。UI 以桌面最大化完整窗口的实际操作和看图验收，记录视口／缩放；窄窗／移动端不作要求。不安装新依赖、不上传私有数据到参考站，不显示未接线能力按钮。

优先最小直接验证；可适量使用现有平台模型的短消息，不启动大任务、真实目标复测或综合报告。按第 16.7 节分别证明独立调度和真实对话，不混淆受控模拟与真实执行。运行改动按既有部署方式使完整系统可用，避开在途工作，不重装未变化的工具层。

审阅本轮精确 diff，回填原文档实际改动、部署、验证和未覆盖项。报告当前分支、文件、测试／实际操作结果、是否 commit、剩余风险。未经批准不 commit／push。
```

### 16.9 本分析轮操作与剩余边界

- 修改仅为 `AGENT.md`、`docs/ui-design-standard.md`、本文；规范更新由用户明确要求。未改业务代码、配置、测试或数据库。
- 实际浏览器操作：正常本地及同实例局域网登录；大屏看图；局域网入口一次短消息点击复现，未发送聊天 POST、未调用模型；清空本次验证草稿；只读核对消息和运行日志。首次手写 GET 误加 `/api` 前缀返回 404，随即使用实际 `/projects/...` 路径取得 200；不是消息发送端点返回 404。
- 源码跨文件查证为只读，无自动化测试、构建、重启、任务创建或目标访问。本轮没有 commit／push，当前分支 `feat/update-worker-cache`。
- 发送故障尚未修复；视觉与独立 Agent 仍待实施。未声称用户实际地址已知、研究与聊天并发已运行验证、附件／定制报告／任务创建已支持。历史第 14／15 节证据保留其原范围。

## 17. A 轮实际实施与验证（2026-10-29）

本节记录第 16.7 节 A 的实际实施，不回写第 16.9 节分析当时的事实。当前分支 `feat/update-worker-cache`；保留接手时已有的大量工作区修改，按接手快照审阅本轮增量，未创建 commit、未 push、未新增依赖。**本轮交付独立交互与研究读取底座，不等于完整独立 Agent 已交付；B 仍是明确待交付需求。**

### 17.1 实际改动

| 范围 | 本轮实现与边界 |
| --- | --- |
| LAN HTTP 发送 | 使用 `crypto.getRandomValues` 生成 UUID v4，不依赖安全上下文限定的 `randomUUID`，不使用弱随机或时间戳。生成与发送均纳入错误处理；草稿和不确定请求的消息 ID 保留，重试复用同一 ID。按钮与 Ctrl／⌘+Enter 共用发送路径，保留 IME 保护。 |
| 默认模型与迁移 | 新增租户级项目 Agent 默认模型配置 GET／PUT `/project-agent/configuration`，复用现有模型及凭据管理；新会话 POST `{}`，不选择研究 Worker。会话保存 `model_id`，默认修改只影响新会话。PostgreSQL v74 从旧 Worker 原位迁移模型引用后删除会话 `worker_id`，不保留两套聊天模式。共用模型执行绑定解析，不创建聊天 Worker 数据行；runner 的 `WorkerConfig` 仅为现有执行接口适配。 |
| 模型错误 | 创建、发送、认领均校验当前绑定的模型；停用、缺配置、凭据失效等返回明确原因，历史仍可读。模型／凭据执行来源不记录认证明文。旧会话模型已删除或原来为空时保留历史，但本轮未提供显式换模型入口，修改默认不能修复这类旧会话。 |
| 独立调度 | Dispatcher 内新增独立 `ThreadPoolExecutor(max_workers=1)`，Agent 容量为 1；不占研究 executor、Worker 并发、研究项目／全局额度。保留单会话写者、代次隔离、取消与回执重试，执行仍沿 `run_agent_turn → PiDriver → run_worker_process`、独立聊天容器与持久 session。没有新增 Agent Loop、服务或通用队列。 |
| 研究只读上下文 | 保留原七工具，新增 `list_research_sessions`、`read_research_context`、`read_research_session`。按授权项目索引研究 TaskRun／session，分页读取 events、facts、intents、Artifact 证据及原生 HTTP flow；session 从真实持久文件或现存容器只读 archive 读取，不启动、resume、清理或写入研究。返回来源、读取时点、分页位置、缺失与不完整尾部，不把持久状态称为实时健康。 |
| 原件与认证边界 | session 要求明确项目／session ownership，持久路径逐层拒绝符号链接；缺少归属标记不猜测兼容。session 原文经 Server 按来源执行凭据审核后才交给模型。受保护记录在输出边界整条拒绝，不改写原件；HTTP flow 沿既有冻结证据与公开字段准入，认证材料不可读，未封存或材料缺失明确返回。 |
| 最大化工作台 | Agent 路由使用共享暖中性 tokens；项目标题／状态／操作并入 Shell 顶栏，以“研究视图”浮层替代 Agent 页原横排 Tabs。272px 历史栏、17px 正文、68rem 阅读区与常驻输入器；仅有真实动作／成果时显示成果栏。不放附件或其他未接线按钮，其他项目页面保留原布局，离开 Agent 移除路由主题。 |
| 原业务保留 | 复测、报告等待／重试与代次判断、动作幂等、真实提交人、可信成果下载继续使用既有业务。聊天 TaskRun 仍保留在后台审计及用量中，但不进入项目运行日志；report／external_verify 不因聊天发起而隐藏。 |

主要文件（相对仓库根目录；不把接手前修改归为本轮成果）：

- 前端：`cairn/frontend/src/api/projectAgent.ts`、`features/projects/{useProjectAgent.ts,ProjectAgent.vue,ProjectAgent.component.test.ts,AgentMarkdown.vue,ProjectWorkspace.vue,ProjectWorkspace.css}`、`design-system/tokens.css`、`layouts/{FrontLayout.vue,FrontLayout-shell.css}`；后两组及 features 路径均位于 `cairn/frontend/src/`。
- Server：`cairn/src/cairn/server/schemas/{models.py,tenant_workers.py}`、`services/{model_execution.py,project_agent_configuration.py,tenant_workers.py,agent_conversations.py,agent_research_context.py}`、`routers/agent_conversations.py`、`migrations.py`；services／routers 路径均位于同一 server 目录。
- Dispatcher：`cairn/src/cairn/dispatcher/config.py`、`scheduler/{agent_chat.py,loop.py,dispatch_cycle.py,worker_runtime.py,heartbeat_reporter.py}`、`tasks/agent_chat.py`、`runtime/{project_tools.py,research_sessions.py}`；后续路径均位于同一 dispatcher 目录。
- Worker 应用：`container/pi-agent/src/project-tools.ts`。
- 后端测试：`cairn/tests/{test_agent_chat_execution.py,test_agent_conversations.py,test_agent_business_actions.py,test_agent_research_context.py}`。
- 文档：本文第 17 节。前端构建同时更新生成的静态资源，不将 hashed assets 逐个列为独立功能。

### 17.2 构建、部署与历史保留

- 前端正式执行 `npm --prefix cairn/frontend run build:server`，包含 `vue-tsc -b` 和 Vite。首次失败是新增测试的 `Uint8Array<ArrayBufferLike>` 与 Web Crypto 参数类型不匹配；改为 `Uint8Array<ArrayBuffer>` 后正式构建成功。仍有 Vite 大 chunk／插件耗时提示，未为了消除提示扩大本轮范围。
- Worker 复用本地已有工具层，仅在临时构建上下文 COPY 应用 `dist`（以及原有未变指引），使用 `--pull=false --network=none`；没有下载工具或依赖。首次只用 `node cli.js --self-check` 未发现文件缺可执行权限，随后真实 `cairn-agent --self-check` 报 permission denied；修正构建上下文中的执行位并重建后，真实 CLI 离线自检通过。最终镜像为 `arena-worker:local`。
- 依既有 `deploy/stop.sh`／`deploy/start.sh` 发布 Server、Dispatcher 与静态资源；实际静态目录为 `runtime/static`，保留旧 hashed assets，最后原子替换入口。空闲聊天容器只更新应用 dist，未删除重建容器，保留 workspace／control／prompts 挂载与 session；没有重启研究容器。
- **部署检查中的实际错误：**此前独立检查已确认数据库无在途任务、无待认领消息，聊天容器进程只有 init／sleep；最终组合命令再次确认数据库为零后，`docker top -eo comm` 因缺 PID 字段失败，而组合 shell 未启用 fail-fast，仍继续 stop／start。不能将整条命令最终 exit 0 当作前置检查全部通过。后续同类命令须使用 `set -e` 或显式成功串联，且 `docker top` 包含 PID 字段。本次后验旧 TaskRun 全表哈希不变，没有发现用户任务被中断；不为补做检查再次重启。
- 实际 PostgreSQL 迁移前后：4 个旧会话的 ID／项目／session／创建人／创建时间摘要一致，全部迁移到非空模型；3 条旧消息、150 条旧 TaskRun（含历史用量）整行摘要一致，原业务动作表为空且保持。三轮真实验证后再核对，旧消息和旧 TaskRun 摘要仍一致；项目、91 条事实和 105 条 Intent 摘要也未改变。
- 最终 `deploy/status.sh`：Server、Dispatcher running，HTTP API ready，heartbeat idle。没有创建新的复测／报告动作或研究任务。

### 17.3 验证结果：受控调度与真实执行分别记录

**定向自动化／构建，不代表全量测试：**

| 验证 | 实际结果 |
| --- | --- |
| `test_agent_chat_execution.py` | 10 passed。新增受控用例真正阻塞容量为 1 的研究 executor，Agent 在另一容量为 1 的 executor 完成；两个 pending 只认领一个，研究各配额计数不含聊天。finish 首次网络结果未知、第二次成功时只执行一次模型替身，不取消研究。 |
| PostgreSQL 会话迁移、无研究 Worker 的会话／续聊／导出、业务动作与代次测试 | 3 passed；另有入队后停用模型专项 1 passed。使用一次性 PostgreSQL，不是操作真实库的破坏性测试。凭据解密失败的新错误分支未单独验收。 |
| `test_agent_research_context.py` | 4 passed，覆盖真实 PostgreSQL READ ONLY 事务、分页／跨项目拒绝／状态不变、活动 session 尾部、认证记录拒绝且原件不变、bridge 二次审核和私有方法阻断、HTTP flow 索引及未封存不可读。 |
| `ProjectAgent.component.test.ts` | 定向 16 例运行中 15 passed、1 条滚动用例超过 5 秒；该滚动用例独立重跑通过（174ms）。另在类型修正后 UUID 专项 1 passed、15 skipped。不是同一次完整 16 例全绿。 |
| 正式构建与 Worker 入口 | 前端 `build:server` 最终 exit 0；Worker 真实 executable 离线自检通过。 |

**实际 LAN HTTP 与少量真实模型：**

- 在同一实例真实局域网 HTTP 页面操作，实际 `isSecureContext=false`、`crypto.randomUUID` 为 undefined、`crypto.getRandomValues` 为 function。通过页面配置默认模型（PUT 200）、新建会话（POST 201，无 Worker 参数），实际点击发送得到消息 POST 200 和真实回复，不以 localhost 验证替代。
- 共做三轮真实模型交互：新会话短回复、旧成功会话 Ctrl+Enter 续聊并正确返回旧上下文标记、一次有界研究只读调用。旧会话沿用原 session，新执行来源为 `tenant_model_binding`，调度身份为 `project-agent`；不把工具轮次内部多次供应商交换说成仅三次 API 请求。
- 只读轮次实际持久工具记录为 4 次调用：研究索引前 2 条、facts 索引前 1 条、选中研究 session 前 2 行、该 fact 前 400 字符。session 返回可读、无缺失及不完整尾部，仍有后续行；facts 索引 `next_offset=1`，只是一页 1 条，不是项目只有 1 条事实。读取发生于 2026-10-29 10:00:10–10:00:14 UTC；没有读取其余材料。模型回答说明了来源和范围，但超过提示中的 150 字要求，不声称严格遵守字数。
- 上述真实模型验证期间没有运行真实研究大任务；“研究满载仍可执行 Agent”仅由前述受控 executor 用例证明，**没有完成真实研究与真实聊天同时调用模型的端到端实验**。
- 实际进入项目运行日志：后台 21 条 TaskRun 中页面显示 15 条，3 条原报告仍在，6 条聊天均不显示。没有发起新的报告或 external_verify；external_verify 的实际执行不在本轮验收范围。

### 17.4 最大化工作台实际操作与看图

- 当前浏览器实际最大化 CSS 视口为 **1914×740**，`devicePixelRatio=1`、`visualViewport.scale=1`；屏幕可用窗口为 1914×879。尝试改为旧基线视口时工具提示须先退出最大化，未强行改窗或冒称保持旧尺寸。旧图的 2554×1348 与本次不同，不作为同尺寸前后对照。
- 实际操作配置模型、新建、按钮发送、快捷键续聊、历史切换、研究视图浮层、运行日志、长消息滚动、“回到最新”及执行诊断展开。诊断显示真实 TaskRun；阅读位置保持 6.5 秒未跳动，点击回到最新后确实到底。输入器保持可达，没有 body 横向溢出；阅读列实测 1088px（68rem）、正文 17px。
- 完整页面截图 `/tmp/arena-agent-a-after-empty.png`、`/tmp/arena-agent-a-after-read.png` 已实际打开看图；它们是本地验收材料，未作为公共资源上传。相对旧界面，改动包括整体暖中性色、单层项目顶栏、独立历史导航、扩大正文与输入区域，而非仅聊天气泡换色。此处记录实际观察，不替用户作最终审美认可。
- 未做窄屏／移动端测试。本轮没有创建业务成果填充侧栏，因此成果栏的真实新业务结果展示未做端到端验收；原成果行为由定向组件测试覆盖，未摆放假成果。

### 17.5 剩余边界与后续 B

1. 旧会话绑定模型已删除或为空时历史可读、发送不可用；需要后续明确的重绑定产品入口，本轮不静默改用默认模型。配置变化不单独触发会话 SSE revision，页面可能等下一次约 60 秒轮换补读或主动刷新才显示新可用性。
2. 独立逻辑槽和线程不隔离宿主 CPU／内存或模型供应商限流；不能承诺绝对零资源影响。未知执行仍按既有规则占槽，不能因等待时间长就宣称已结束。
3. session 原件缺失、归属标记缺失、认证内容受保护或活动尾部未落盘时明确返回不可读／部分可读；不借摘要冒充原文。真实模型读过已有研究 session，但没有同时运行研究写入的真实端到端验收；活动尾部行为只做受控验证。
4. 原生 HTTP flow 已接入既有冻结证据路径，但本轮未做真实 sealed flow 的端到端验收；真实成果侧栏、新复测、综合报告也未通过额外业务操作验收，不能从构建／测试推断这些动作本轮真实执行成功。
5. **B 继续按第 16.6／16.7 节交付：**消息附件全生命周期、独立 workspace 用户材料处理、按用户要求生成并下载定制报告、创建项目及追加 Intent 的真实业务闭环。A 不替代这些需求，本轮未实现，也不将其改为永久不支持。后续验证继续按目标、费用和副作用授权控制，不默认启动探索或复测。

## 18. 2026-10-29：A 轮复核、日常体验收口与 B 轮能力闭环

### 18.1 当前判断与验证身份

**A 的核心方向已落地，不需要推倒重做；但不能宣布整体独立 Agent 已达预期。** 用户新增四项反馈：聊天字体偏大、连接断开提示影响体验、复制有问题、缺少删除会话。下一轮应在保留 A 的基础上进入原定 B，同时收口这四项，不再把 B 无限延期为未来功能。

本分析轮依据当前源码、实际 LAN HTTP 页面和持久接口查证，不重跑实施会话的测试或付费验证：

- 创建／发送／claim 使用模型引用，不再依赖研究 Worker；迁移、独立 executor、研究工具贯通见第 17 节及当前相应源码。受控满载隔离与真实短对话是两类证据，不能合称真实并行研究实验。
- 当前实际会话列表接口返回 200，字段为 `model_id` 而不是 `worker_id`；既有成功会话消息 2／3／5 均为 `succeeded` 且有持久回复，对应 tr017／tr018／tr020。没有为了复核再发送消息。
- `useProjectAgent.ts:18–27,229–255` 的发送确已改用 `getRandomValues` 并纳入错误边界。LAN 实际发送验收沿用第 17.3 节的实施记录；本轮没有重复执行该 POST。
- 实际打开并看图：完整工作台已改为暖中性表面、单层项目顶栏、会话侧栏及更宽阅读区，不再是旧界面局部换色。本轮采用 **1914×740 CSS 等效桌面视口、缩放 1**；不冒称原生窗口最大化，不要求窄窗验证。新的字号和交互反馈仍需按用户反馈修正。

### 18.2 四项体验问题：证据与明确取舍

#### 1. 字体再小一点，不重做主题

实际用户消息、Agent 正文及输入器均为 **17px**；正文行高约 29.75px。来源是 `frontend/src/design-system/tokens.css:118` 的 `--workbench-body-size`，Markdown 继承该字号（`AgentMarkdown.vue:14`）。

本轮将聊天正文与输入器以 **15px、约 1.65–1.75 行高**为起点，最大化看图后微调段间距和代码比例。保留目前外层、阅读区宽度和字体层级，不缩放整个页面，不全站缩字，不重新退回旧窄列或低对比小字。共享语义 token 继续作为维护位置。

#### 2. 连接提示不是模型必然停止：正常轮换被误报

- Server `routers/agent_conversations.py:146–164` 每连接循环 60 次，每次 sleep 1 秒，然后正常结束 SSE；期间仍核验登录和项目访问。
- 前端 `useProjectAgent.ts:204–212` 把每次 `onerror` 都立刻写成“实时连接已断开……最多自动重试 3 次”，即使这是服务端正常轮换。
- 在正常 LAN 页面连续观察约 90.8 秒：第 45.324 秒出现该横幅，第 46.525 秒自动消失，持续约 **1.2 秒**。期间没有发送消息或修改服务。结合源码，可以确认存在正常轮换也产生断线警告的路径；不能据此称后台模型已中断，也不能推断所有断线均无害。

修正前后端连接生命周期：正常轮换静默续接；短暂可恢复中断先后台补读／恢复，不立刻弹整块警告或挤动正文。需要区分正常关闭时，可在现有 SSE 上发送明确结束原因，不新增通用连接框架。保留有限重试和稳定连接复位；持续失败时用紧凑且可操作的“消息更新暂不可用／重试”，准确说明状态可能滞后。401／403／404、消息解析失败与真正执行失败不可静默隐藏。保留已显示消息、草稿、阅读位置、事件游标、补读和幂等；重连绝不重新 POST 用户消息。不能只删 Alert、取消鉴权或把连接改为无限重试。

#### 3. 复制故障已在实际入口复现

LAN 页面 `isSecureContext=false`、`navigator.clipboard=undefined`。实际点击第 5 轮短回复的复制按钮，立即出现“复制失败，请选择回复文本手动复制”。`ProjectAgent.vue:329–337` 仅调用 `navigator.clipboard.writeText`，所以该入口必失败；这与前一轮 UUID 故障同属安全上下文 API 差异，但不是发送回归。

优先复用当前 `components/patterns/EvidenceReader.vue:622–674` 已有的 LAN 复制方案：有 Clipboard API 时按真实完成回执显示成功；无 API 时在用户点击期间使用浏览器复制命令，并通过 copy 事件提供原始文本、检查命令结果，恢复焦点与选择。可以为这两个实际调用方提取一个小型共享函数，不做全站复制重构或泛化剪贴板框架。

复制对象必须仍是原始 `answer`，不是渲染 DOM；多行、Markdown／代码及换行语义不被改写。真正拒绝权限时明确失败；允许手动选择原文作为末级恢复，不能把“请手动复制”当作当前 LAN 缺陷的完成修复。正常成功反馈轻量且及时复位，不显示假成功，不影响输入草稿和滚动。

#### 4. 删除会话是缺失能力，不是入口藏得深

当前 `ProjectAgent.vue:10–18` 历史栏只有选择会话；前端 API 与 Server router 没有删除端点。下一轮增加历史项菜单及明确确认，按钮不可嵌套，兼顾键盘访问。

采用**服务端持久逻辑删除**作为本次产品语义：从普通聊天历史移除且不能继续发送，不是安全擦除全部历史。确认文案说明“不删除已生成报告／复测等业务成果”；消息、TaskRun、用量与必要审计引用保留，不级联清理研究记录、业务动作、session 或 Artifact。仅用 localStorage 隐藏不算交付。

- 列表、消息／动作访问、SSE、新消息准入及 pending／claim 必须一致处理已删除会话；既有成果从其业务页／项目产物入口继续可达，下载不能因粗暴复用会话可见性检查而意外失效。
- 正在排队、执行或执行状态待确认的会话暂不可删除，给出具体原因；不因点击删除偷偷取消付费任务。当前 submit／claim 已锁会话行，删除沿同一事务边界判定，不新建锁框架。只有后台业务如复测／报告尚在运行、聊天轮次已终结时，不应因此永久挡住会话整理；这些独立业务继续在原入口跟踪。
- 删除当前会话后选择现存最近会话；没有剩余时回到可新建空态，不自动创建新会话。修正 URL、终止该会话流与补读、清理其本地草稿；有未发送输入先确认，其他会话草稿不受影响。
- 删除必须后端确认后才完成；失败保留列表与草稿，重复删除有明确幂等结果。刷新、旧深链接和其他已打开标签不能把已删除会话复活或继续发送。不在用户现有会话上做删除验收。

#### 补充：执行完成回执的恢复缺口，不是上述 SSE 横幅的根因

对 A 底座的定向源码核对还发现：模型已结束且终止得到确认，但 `/finish` 连续三次瞬时失败、Server 尚未保存终态时，`dispatcher/scheduler/agent_chat.py:73–105` 会移除 future，从而丢失本地结果重送；某些确定性拒绝也会直接走此路径。runner 已停止 heartbeat，Server 数据库仍为 running，`server/services/agent_conversations.py:147–153,166–170` 因而不再认领同会话后续消息。消息读取超过 120 秒只显示 `execution_unconfirmed`，不完成业务收口；通用 pending-finish 路径被聊天分支跳过，启动 reconcile 亦跳过聊天（`scheduler/task_lifecycle.py:692–694`、`scheduler/stale_reconcile.py:56–58`）。

这是**源码可达的恢复风险，未查实当前实例发生，也无法归因于 A 新引入**；Server 已提交但响应丢失的情况不等于上述未提交场景。不要将其与正常 SSE 轮换混为一谈，或把用户观察到横幅解释为任务已经卡死。

本轮先补齐该局部收口：复用已有回执重送／持久执行结果机制，尚未确认接受时不能丢掉已确认结束的结果和待处理身份；明确可重试与确定性拒绝的终点。确定性内容审核拒绝不能放行原内容，应通过既有失败回执正确结束；不能重跑模型来补送结果，不能因超时猜测进程停止，也不能只无限占槽重试。用受控 finish 失败验证恢复，禁止在实际用户任务上制造故障。不借此引入通用工作流／消息总线。上一轮“代次隔离”等宽泛表述不视为所有入口逐一验收，本次没有运行项目代次切换实验。

### 18.3 下一轮主线：B，不是又一轮只调 UI

按第 16.6／16.7 节完整 B 范围推进，先处理四项日常问题，再打通一条小而完整的“上传 → 独立处理 → 定制成果下载”路径及真实任务工具。顺序是实施安排，不把后半程永久移出本轮。

1. **消息附件**：复用上传存储／校验，持久绑定会话／消息；发送前显示上传、失败重试和移除，刷新与续聊仍可读，发送幂等包含同一附件集合。不能只引用会过期临时批次，不能把文件默认导入研究资源或要求研究暂停。明确现有可解析格式与大小限制，上传成功不等于读懂内容。
2. **独立用户任务**：当前 runner 的 chat 分支仍只有平台工具（`container/pi-agent/src/runner.ts:155–167,283,460`），不能把 A 的三项只读工具当成已具备文件／命令工作区。复用现有执行内核在独立 workspace 处理用户材料；不新增 Agent Loop、服务或框架，不默认挂入研究可写目录、凭据／认证文件或其他租户资料，不扩大目标／费用边界，也不把能力永久锁成固定问答。
3. **定制报告与下载**：接受用户本次关注点、范围、章节与表达要求，至少形成真实 Markdown 文件，登记现有 Artifact 并提供鉴权下载，刷新仍可找回。运行中材料标注来源、读取时点、未完成与不可读范围，不覆盖最终报告，不改全局模板，不以模型正文中的路径／URL 冒充交付。
4. **真实平台动作**：新建项目、追加当前项目 Intent、带附件发起现有漏洞复测分别复用原业务服务。创建不等于启动；当前项目不满足追加条件时准确反馈，不静默恢复项目。复用真实提交人、`tool_call_id`、业务引用、动作幂等和状态／成果查询，不伪造 TaskRun 或在自然语言里自报成功。原报告代次隔离和有限退避保持。
5. **现有底座保持**：模型与 Worker 解耦、独立槽位、旧 session、研究只读边界和聊天退出项目运行日志保持；实际 report／external_verify 仍可查。旧会话模型重绑定仍是已知后续项，不静默切默认模型，也不借本轮扩展为全站模型改造。

### 18.4 验收与交付边界

- 真实 LAN HTTP 页复制短回复及多行原文后粘贴核对，不能只测试 localhost 或 mock Clipboard API；没有权限读取系统剪贴板时，用用户手势粘贴验证，不绕过浏览器权限。
- 正常页面停留跨过至少两次服务端轮换不再闪断线横幅；受控短暂断网和持续故障分别验证恢复与可见终点，不更改宿主／其他用户网络，不把模拟算真实网络故障。
- 删除仅用本轮新建、无用户材料的验证会话或隔离测试；覆盖取消确认、已删除刷新／旧链接、最后一个会话、草稿保护和在途拒绝，不删除现有用户历史。
- 只用小型无敏感合成附件验证持久引用、续聊读取、一次用户指定结构的短 Markdown 与实际下载；可适量用现有平台模型，不做大任务、不启动研究／真实目标复测或终态综合报告。项目／Intent／复测写入路径优先在隔离环境与受控执行中核对，缺少真实授权时如实说明未做真实目标验收。
- 最大化桌面完整看图和实际操作；不要求窄窗／移动端，不为了形式重跑全套测试。前后端／Worker 必要应用改动按既有部署路径使完整系统可用，避开在途工作、复用未变化的工具层，不新增依赖。
- 回填本方案实际完成项、迁移、部署和证据，区分受控验证与真实运行；若 B 未全部完成，逐项说明阻塞，不把剩余范围悄悄写成“后续可选”。未经批准不 commit／push。

### 18.5 本分析轮操作

本轮仓库仅更新本方案；未修改业务代码或全局规范，未运行自动化测试、构建、迁移、启停服务、模型调用或业务任务，未删除会话。实际操作为读取既有对话、调整等效桌面视口并看图、点击一次短回复复制及观察正常 SSE 轮换。浏览器临时观察器已移除。分支 `feat/update-worker-cache`，无 commit、无 push。第 17 节测试与部署仍归属上一实施会话，不能写成本轮重跑成功。

### 18.6 下一实施会话提示词

```text
这是新实施会话，完成“项目 Agent 日常体验收口 + 原定 B 轮能力闭环”。

先读 CLAUDE.md、AGENT.md、docs/ui-design-standard.md、docs/analysis-design-handoff.md，以及 docs/specs/project-agent-capabilities-design.md 第16节完整目标、第17节实施记录和第18节当前复核。以第18节为当前范围，不重跑历史提示词。编辑前检查 git status --short、git branch --show-current；保护所有既有修改，在任务分支工作。本会话可以改本轮业务代码，不沿用分析会话的只读权限。

A 的独立模型、独立执行槽、研究读取与工作台已实质落地，不推倒重做。下一轮本应进入 B；不能因四项体验反馈又只改 UI，把附件、独立用户任务、定制报告和任务工具永久延期。

先完成体验和局部可靠性收口：
1. 聊天正文／输入器当前实测17px，以15px为起点调小，协调行高和Markdown／代码比例。修改相应共享语义token，保留当前整体工作台及阅读宽度，不全站缩字、不缩放页面。
2. Server SSE约60秒主动结束，前端每次onerror立即弹“连接断开”。实际正常页面已观察到约1.2秒横幅闪现。正常轮换静默续接，短暂断网先后台恢复，持续失败才显示紧凑、可操作的状态；鉴权失败、解析失败和执行状态未知不能隐藏。保留有限重试、游标补读、草稿和滚动，不因重连重新发送消息，不只删除Alert。
3. LAN HTTP的navigator.clipboard为undefined，ProjectAgent.vue仅调用writeText，实际复制必失败。复用EvidenceReader.vue已具备的用户点击复制路径，必要时为两个调用方提取小型共享函数。复制原始answer，核对多行与Markdown原文；成功后才提示成功，失败真实反馈，恢复输入焦点和选择。必须在LAN HTTP实际复制粘贴验证，localhost成功不能替代。
4. 增加会话历史菜单、删除确认和服务端持久逻辑删除。普通历史／深链接／发送／SSE／pending与claim一致排除已删除会话，刷新不复活。保留消息审计、TaskRun、用量与既有业务成果，确认说明不取消或删除已发起复测／报告，不宣称安全擦除。聊天仍排队、执行或终止待确认时明确拒绝删除；只剩独立业务运行不应挡住整理。删除当前会话切最近现存会话，无剩余则显示空态，不自动创建；保护其他会话草稿，失败不假删。仅对新建验证会话或隔离数据验收，不删除用户历史。
5. 第18.2节补充已定位finish恢复缺口：模型已结束，但finish三次失败且未落库时scheduler会丢弃future，服务端running继续挡住同会话。这不是SSE横幅根因，也未查实本实例发生。复用已有回执／持久结果机制局部收口，不丢待确认结果；区分瞬时失败与确定性拒绝，不放行受保护内容，不重跑模型、不靠无限重试或超时伪造终态。

然后完成原定B，贯通前端、Server、Dispatcher和实际runner：
6. 消息附件的上传、失败重试／移除、持久归属、消息幂等及刷新／续聊读取。复用存储校验，不只引用临时批次，不默认导入研究目录，不要求暂停研究；只支持现有可解析格式，限制真实可见。
7. 在独立workspace复用现有文件／命令执行能力处理用户材料。当前chat仍只有平台工具，不能把新增三个只读工具当作通用任务已实现。不新建Agent Loop／服务／框架，不写研究session或扩大目标、费用、凭据访问范围。
8. 接受本次范围、侧重点与章节要求，生成至少Markdown的定制报告，登记Artifact并提供鉴权下载，刷新可找回。阶段材料标明来源、时点及未完成事项，不覆盖权威最终报告、不要求改全局模板、不把模型文字URL当作成果。
9. 接入新建项目、向当前活动项目追加Intent、带附件走现有漏洞复测入口。创建与启动分开，复用业务服务、真实提交人、tool_call_id和幂等，返回真实对象／状态／成果引用；纯查询不写任务，不伪造TaskRun、不静默恢复项目。

保留A的模型与Worker解耦、独立槽位、旧session、研究只读和聊天退出项目运行日志；保留报告代次／退避和可信下载。不要顺手重做全站或增加依赖；未接线功能不摆假按钮。

验证对准实际行为：LAN HTTP发送／复制，正常页面至少跨两次SSE轮换不闪警告，受控断网有恢复与失败终点，局部finish重送不重复执行，验证会话删除及刷新，小型合成附件→按要求处理→定制Markdown实际下载。可适量调用平台现有模型做短验证，不做大任务，不启动真实研究／目标复测／终态综合报告。新建项目、Intent及复测写入在隔离环境受控验证；未做真实业务验收要如实说明，不混称端到端通过。

UI仅以最大化桌面完整看图与操作验收，记录CSS视口／缩放，不要求窄窗或移动端，不默认全量测试。按既有部署路径构建并使完整系统可用，避开在途工作，复用未变化的Worker工具层。审阅本轮精确diff，回填原方案的实际实现、迁移、部署、验证和缺口；B若未完成逐项说明原因，不能改成永久不支持。最终报告分支、文件、验证结果、是否commit及风险。未经批准不commit／push。
```

### 18.7 2026-10-29 实施状态：代码贯通，真实完成回执仍阻塞

**本轮不能宣布完整验收通过。** 四项体验及 B 的前后端、执行端代码已接通；真实小型材料任务已生成、登记并可鉴权取得 Markdown，但最终消息未被 Server 接受。实际验收发现本轮新增持久回执的整数消息 ID 契约缺陷：真实 claim 返回整数，`_PendingAgentFinish.message_id` 要求字符串，构造失败被记录为 `chat_finish_storage_unavailable`。原测试使用字符串身份，遗漏了真实类型。

现已在 `dispatcher/scheduler/agent_chat.py` 构造回执处使用 `str(task.agent_message_id)`，定向测试改为整数身份并验证落盘 JSON、HTTP 参数及模型仅执行一次，同进程／重启两个分支通过。**该最后修复尚未加载到正在运行的 Dispatcher**：原结果仍在旧进程的 future 中，构造失败发生在首次持久落盘之前，不能直接重启丢掉它。

本轮只读核对了新建合成任务的聊天 session：保有最终 assistant 原文和各次真实 usage；聊天容器只剩 init／sleep。它们证明已有材料及执行进程现状，但不能代替已清理的 runner 原始结果信封及完整终止回执。因此没有凭 session 拼造成功 finish，没有重跑模型，也没有强改数据库终态或写研究 session。当前页面如实显示“执行状态待确认”，该聊天仍占独立槽位；研究记录未受修改。后续必须先安全取得并持久保存原 future 的真实结果，再沿原鉴权 finish 路径收口，才能重启加载修复及宣布聊天恢复。

### 18.8 本轮实现与迁移

- **字号／SSE／复制**：共享 workbench 正文字号改为 15px，保留宽阅读区并调整代码比例；Server 显式发送 `close: rotate`，客户端正常静默续接，短故障按 1／2／4 秒有限恢复，稳定连接后复位预算，鉴权／解析错误即时可见。补读与事件游标保留，不重发消息。`lib/copyText.ts` 为 ProjectAgent 和 EvidenceReader 复用原始文本复制；Clipboard API 按真实 Promise，LAN fallback 检查 copy 事件供文和 execCommand 结果，恢复焦点与选择。
- **逻辑删除**：会话菜单、确认、草稿提示、后端成功后清理和 URL／当前会话切换接通。删除与 submit／claim 共用会话行锁；排队／运行／结束待确认拒绝，独立业务运行不代替聊天状态。普通入口排除已删除会话，保留消息、TaskRun、用量、session、业务成果；成果下载不粗暴依赖会话可见性。
- **附件**：新增 `services/agent_attachments.py`，复用现有 Artifact 和文件校验，持久归属会话／消息；上传完成再次检查删除竞态。发送事务绑定真实上传者及附件集合，幂等冲突明确拒绝；未发送附件可恢复、移除，已发送附件可跨轮工具读取。现有限制由共享资源配额裁决，不再另设固定 20 个附件等冲突门槛。文本严格 UTF-8、分页并标记读取时点；二进制通过受控 bridge 放入 workspace，不向模型灌入 base64。
- **独立 workspace**：复用 PiDriver、原 runner 和 SDK read／write／edit；工具 cwd 使用当前会话子目录，runner 旧 cwd 保持不变，避免破坏旧 session。SDK 展开路径后在实际 operations 校验越界和软链接。bash 使用已有 Worker 镜像的短生命周期容器，仅挂当前会话目录、无网络、不挂研究／认证／控制 socket，有限时间与输出，结束清理；不新增执行服务或依赖。
- **定制 Markdown**：可从真实 workspace 文件发布，登记既有 Artifact；标题、来源、读取时点、范围和 tool_call_id 摘要持久保存，幂等冲突拒绝。原准出审核、鉴权下载继续生效，不覆盖权威报告。`audit_artifacts.create_artifact_record` 仅增加调用方事务支持，文件失败清理由本调用方负责。
- **真实业务工具**：新增 `agent_task_actions.py`，真实 actor、消息行锁、tool_call_id、参数摘要与业务引用贯通。创建项目复用原服务、初始为 paused，不启动研究；原 resume 可显式启动。追加 Intent 抽取原 router 的事务主体为共享 `intent_creation.py`，仅允许当前 active 项目，不自动恢复。复测复用原入口；附件转原资源批次与业务提交同事务，`project_resources.py` 仅增加调用方事务支持。动作提交成功不冒充后续研究成功。
- **finish 有界恢复**：复用原子 receipt 写入／fsync，保存结果及任务身份后才允许释放槽位，三次瞬时失败后由持久回执接管，总计六次；重启不清预算。确定性拒绝和耗尽明确保留；内容拒绝不放行原文，独立失败回执也有界。覆盖从首次持久落盘以后开始，不宣称覆盖落盘前崩溃。本次整数契约故障正发生在该边界之前，见 18.7。
- **数据库**：仅 PostgreSQL 新增迁移 **75 `project-agent-user-tasks`**：会话 `deleted_at`、持久 `agent_attachments` 及索引／关联约束、project／intent 动作业务引用和互斥约束。没有修改历史迁移 checksum，没有恢复 SQLite 支持。隔离库和实际实例均已应用。

### 18.9 验证证据：受控测试与真实 LAN 分开

受控测试记录：

| 范围 | 结果与边界 |
| --- | --- |
| Pi build 与 `chat-turn.test.mjs` | build 成功，最终 10 passed，包含实际 operations 路径展开边界 |
| `test_agent_chat_workspace.py` | 3 passed；不代表真实目标研究 |
| `test_agent_chat_execution.py` | 先前整文件最终 17 passed；曾有 7 项失败，追踪后未复现。真实验收暴露整数身份遗漏后补同进程／重启回执恢复，定向 2 passed |
| `test_agent_user_materials.py` | 附件／Markdown／复测转交／删除主链 1 passed；SSE rotate／显式鉴权错误定向 1 passed，使用隔离 PostgreSQL |
| `test_agent_task_actions.py` | 五项均有定向通过记录；paused 创建／恢复等最终子集 3 passed、2 deselected。没有启动实际研究或真实目标复测 |
| ProjectAgent 与共享复制 | 28 passed；EvidenceReader 复制专项 2 passed |
| 未全绿／未执行 | EvidenceReader 全文件 19 passed、3 failed（两个文本定位断言、一个大正文超时）；最后前端动作／删除／上传子集重跑遇 Vitest forks worker 启动超时，未执行测试，不算断言通过 |
| 前端正式构建／差异 | `build:server` 的 vue-tsc／Vite 成功；存在耗时和大 chunk 提示。`git diff --check` 通过；未跑全量套件 |

真实 LAN HTTP 操作仅涉及本轮新建验证会话和无敏感合成材料：

1. `isSecureContext=false`、Clipboard API 不存在。硬刷新加载新静态后，输入器实测 **15px**；完整截图已实际查看，等效桌面 CSS 视口 **2554×1209**、DPR 1、缩放 1。没有冒称原生最大化，没有要求移动端或窄窗。
2. 页面只读观察记录到 **5 次明确 rotate、0 次连接错误、0 次连接警告、0 次观察期间消息重 POST**。因此正常轮换静默续接已实证；不把它扩写为断网通过。
3. 小型 CSV 上传后硬刷新，待发送附件仍在，没有重传。仅发送一次合成任务：真实工具将原件放入当前独立目录，使用标准库处理，写出真 Markdown；报告含两个指定章节、总和 5、附件来源、UTC 读取时点及未访问目标／未评估项目安全的边界。首次发布使用错误相对路径而被拒，模型改用绝对路径后成功，未重跑任务。
4. 成果侧栏已显示真实 Artifact；刷新后仍可找回。实际点击下载，随后同源鉴权 GET 返回 **200、attachment 响应头及 412 字节 Markdown 原文**。未定位浏览器最终保存文件，不能称已核对浏览器落地文件。最终聊天回复因 18.7 阻塞，不能称整条任务端到端完成。
5. 在途验证会话删除实际返回 **409** 并显示具体原因，未取消任务。另建空验证会话，输入草稿后打开删除确认：出现草稿后果说明，取消保留原草稿；再次确认成功后切到最近现存会话并清理该草稿。数据库确认逻辑删除，messages／actions／attachments／events 均返回 **404**；旧深链接显示明确不可访问且发送禁用，没有复活。原有六个会话未删除；最后会话空态未在真实库破坏历史来验证。
6. 最后只读摘要对比：部署前已有 **6 个项目、91 条事实、105 条 Intent、8 条消息、155 条 TaskRun** 均保持原行内容。会话表因新增列未用整行摘要冒称字段级完全相同；实际原六会话仍可见，本轮仅新增一个保留中的材料会话和一个已逻辑删除的空验收会话。

尚未完成且仍属本轮目标：安全恢复原 finish 并部署最后修复；真实 LAN 短回复和多行 answer 复制后用户手势粘贴逐字符核对；真实后续轮次读取附件；受控页面短断网／持续断网恢复、终点及草稿／阅读位置核对。浏览器工具的 Offline 调用报 CDP 参数缺少 `offline`，没有成功施加网络故障，不计为断网验收。未为绕过这些阻塞发送新模型任务、修改宿主网络或动用户历史。

### 18.10 构建部署与交付边界

部署前确认无 queued／running TaskRun、无待认领消息，既有聊天容器仅 init／sleep；随后沿 `deploy/stop.sh`、`deploy/start.sh --host 0.0.0.0 --port 8001` 完整启动 Server 与 Dispatcher。前端静态发布保留旧 hash 资源、最后替换入口；实际 LAN 硬刷新已加载新页面。Worker 仅 COPY 最终应用 dist 到原本地镜像，离线 CLI 自检成功，未重新下载工具层；旧聊天容器更新应用文件而不重建挂载／session。

第一次临时 Dockerfile 将本地 image ID 写成 `FROM sha256:...`，BuildKit 错当远程镜像，metadata 请求被拒；该组合命令末尾输出导致表面 exit 0，已按真实错误判失败。改为 `FROM arena-worker:local`，明确串联 build／离线 self-check／tag 后成功。不能宣称第一次也成功或整个过程中从未发起 metadata 请求。

完整启动时 HTTP ready、Dispatcher heartbeat idle。其后真实合成任务触发 18.7，**当前服务仍运行，但聊天槽位被待确认任务占用，不是全功能可用／空闲状态**。未在原结果未安全持久保存前再次重启 Dispatcher；最后一行整数身份修复尚未进入运行进程。恢复必须如实区分人工恢复与正常自动恢复，禁止从 session 猜造执行终态。

本轮分支为 `feat/update-worker-cache`。保护了接手时大量未提交修改；本文仅记录本轮增量，不能将整个工作区 diff 归为本轮。未新增依赖、未 commit、未 push；没有运行研究、真实目标复测或终态综合报告。

## 19. 2026-10-29：B 轮复核——先恢复已完成执行的回执，再完成闭环验收

### 19.1 结论与本轮直接证据

**部分达到预期，但当前不能作为完整可用的 Agent 交付。** 这次不只是声明接入工具：合成附件已产出真实 Markdown，体验问题也有改善；主要阻断是“执行结果未被接受”，不是需要再增加功能。

本分析轮只读核对源码、正常 LAN 页面及既有成果，没有重跑模型、上传材料、创建或删除会话，也没有恢复／重启进程：

| 范围 | 本轮实际证据与边界 |
| --- | --- |
| 字号 | 实际输入器为 15px，共享 token 已更新；1914×740 CSS 等效桌面视口、缩放 1，已看完整工作台截图。 |
| 正常连接轮换 | 实际页面连续观察 **192.066 秒**，浏览器记录到 **3 条已结束 SSE 请求**（每条约 70–72 秒），没有出现连接警告；源码明确消费 `close: rotate`。不等于已验证故障断网。 |
| LAN 复制 | 实际 `isSecureContext=false`、Clipboard API 不存在。点击真实复制按钮后，用 Ctrl+V 粘贴到原本为空的输入器，与持久 `answer` 严格相等：短回复 **5 字符**，多行回复 **234 字符／5 行**。首次多行粘贴前工具清空未消除上一段草稿，得到原短回复＋新正文；用真实全选／Backspace清空再粘贴后完全匹配，不能误报复制多带字符。验证草稿已键盘清空，未发送。 |
| 删除会话 | 页面已有每会话菜单，代码具备服务端逻辑删除和在途拒绝；沿用第 18.9 节对专门验证会话的真实删除记录。本轮未删除用户会话，不把入口存在说成重做了删除验收。 |
| 附件／定制文件 | 既有材料消息 9 有 1 个附件和真实 Artifact `a21011`。本轮对既有下载端点只读 GET 返回 **200、attachment 头、412 字节 text/markdown**，包含 Markdown 章节、读取日期及统计值。说明成品确实存在，不代表最终聊天已完成，也不声称核对了浏览器本地落地文件。 |
| 消息完成与容量 | 既有 `conv_72dbd6c1faa34825a913da17075ab056` 的消息 **9／tr024** 仍为 `running`、`execution_unconfirmed`、`answer=null`；页面如实显示执行状态待确认。日志仍重复 `chat_finish_storage_unavailable`，本轮采样最近到 07:37:16。 |

源码对应：`frontend/src/lib/copyText.ts:1–39`，`features/projects/useProjectAgent.ts:313–397`；回执缺陷及最后一行修复见 `dispatcher/scheduler/agent_chat.py:30–43,191–222`。本轮没有重跑第 18.9 节测试；其中 EvidenceReader 的 3 项失败及最终子集未执行仍保留原身份，不能改称全绿。项目创建、追加 Intent、附件转交复测仍只有实施记录中的受控验证，本轮没有新增真实目标任务证明。

### 19.2 当前阻断：代码修正不等于运行恢复

- claim 的消息 ID 实际为整数；新持久回执 Schema 的 `message_id` 为字符串。旧进程把原整数直接构造 Schema，校验失败发生在首次保存 receipt **之前**，被 `except (OSError, ValueError)` 归成存储错误。不是已经证明磁盘满或权限错误，不应围绕磁盘反复试错。
- 当前文件已存在 `message_id=str(task.agent_message_id)`；定向用例现在使用整数 9，检查持久 JSON／HTTP 身份为字符串，并覆盖同进程／重启后重送。该用例仍是受控验证，不证明旧进程已加载修复或真实 Server 已接受本轮结果。
- 实施记录明确最后修复未加载；当前相同回执错误和未终结消息仍与这一记录一致。本分析轮没有调试器附加或读取进程内对象，**原 future 的内容及可恢复性仍须在恢复操作前核实**，不能仅凭文档保证一定可以提取。
- `_reap_agent_turn` 在构造／落盘失败时直接 return，保留 future；`AGENT_CAPACITY=1`，因此不是只影响一条消息：该实例的其他 Agent 消息也可能一直排队。不要为验收继续付费发送或新建会话尝试绕开。
- `tasks/agent_chat.py:116–124` 会停止 heartbeat 并清理 runner 临时结果。session 最终文本、usage 片段、文件存在和容器当前无模型进程，各自都是证据，但**不能拼造成原 future 的完成结果／终止证明**。未保全原结果直接重启可能永久失去唯一仍在内存中的回执。

该问题必须区分三步：**源代码修复 → 安全处理既有阻塞 → 新执行验证已加载修复**。仅完成第一步，不是部署成功；人工恢复原任务也不是自动恢复能力已经验收。

### 19.3 下一轮唯一范围：恢复与收尾，不新增能力

1. **先保护现状并核实恢复条件。** 精确绑定项目、会话、消息 9、tr024、当前 Dispatcher 身份和执行生命周期；只读取所需对象，不转储整进程内存、凭据或完整 session。检查是否已有可用原始 receipt／结果及受支持恢复入口；不能先 stop/start、杀进程、改运行代次、删除会话或重跑模型。
2. **保全真实结果后重送原回执。** 若原 future 结果及终止证据仍可安全取得，保存在既有私有 receipt 路径并核对身份／原值，沿原鉴权 finish 接受；保留真实答案、usage、session 和 Artifact。不得从 session 重组成功结果，不直接 UPDATE 数据库终态。若只能通过调试器附加、进程内执行或服务暂停取得，先说明最小操作、暂停影响和失败后果并请求明确批准；普通部署授权不自动包括侵入式恢复。若无法可靠取得，停在明确阻塞并向用户说明受控失败收口会损失什么，不能偷偷丢弃原结果、伪造成功或无限调查。
3. **收敛类型与错误定位。** 复核整数消息身份进入 claim→RunningTask→receipt→HTTP 的真实路径；在确定边界正确转换即可，不增加猜测性兼容层或重写回执框架。将身份／Schema失败与真正文件I/O错误区分为无敏感内容的诊断，不让确定性校验失败永久冒充存储故障并高速刷日志。现有有界重送／幂等和拒绝原文保护保持。
4. **安全加载与小型真实闭环。** 原结果已妥善收口且确认没有其他在途工作后，按既有部署路径加载最终修复。先核对原消息状态、原成果和槽位，再仅发一条短续聊读取原附件的小任务，验证有真实最终回复、刷新保留和后续可派发；如需验证新成果只做同一小型合成材料的明确任务，不重复大研究或重跑已完成的材料计算来冒充恢复。
5. **补完尚未覆盖的体验验证。** 受控短断网／持续故障、失败终点与草稿／滚动恢复；故障只作用于测试页或隔离测试，不改宿主网络。工具不支持时使用正确工具参数或局部受控验证并说明身份，不虚构断网成功。LAN 原文复制与正常轮换本分析轮已直接通过，相关代码未再改时不重复验收。共享复制变更相关的 EvidenceReader 3 项未通过只做定向归因，必要时修复相关问题，不扩展全套测试或顺手重构。

维持现有 A／B 范围、成果、授权、研究状态和大屏布局，不重新设计 UI，不新增外部平台发布，不把旧会话换模型等独立需求混入。真实项目启动、追加活动研究和目标复测不因本轮恢复自动获得授权；已有受控测试如实保留，恢复通过后再选择用户实际需要的一个业务场景验收。

### 19.4 下一实施会话提示词

```text
本轮是“项目 Agent 已完成任务回执恢复与 B 轮验收收尾”，不是继续加功能或重跑第18.6节整轮任务。

先读 CLAUDE.md、AGENT.md、docs/ui-design-standard.md、docs/analysis-design-handoff.md，以及 docs/specs/project-agent-capabilities-design.md 第18.7–18.10节和第19节。编辑前 git status --short、git branch --show-current，保护现有修改，在任务分支工作。

已确认：proj_060 的会话 conv_72dbd6c1faa34825a913da17075ab056，消息9／tr024仍 running + execution_unconfirmed，answer=null。小型CSV已产出真实Markdown Artifact a21011，鉴权下载200。不是模型需要重做，而是新receipt把整数消息ID交给字符串Schema，在首次落盘前失败，日志反复chat_finish_storage_unavailable，独立Agent槽位未释放。

源文件已加message_id=str(task.agent_message_id)，整数身份的受控同进程／重启用例已补，但最后修复尚未加载旧Dispatcher。原结果据实施记录仍在旧future；当前是否可恢复必须核实。不要先重启、杀进程、删除会话、强改数据库或重跑模型。

按第19.3节顺序执行：
1. 只检查本次身份、已有原始receipt／结果和受支持恢复入口。若能安全取得原future真实结果及执行终止证明，先在既有私有receipt路径完整保全，再走原鉴权finish；答案、usage、session、Artifact不伪造、不丢弃。不能从session文本或“当前没进程”拼造成功回执。
2. 如恢复必须附加调试器、在运行进程执行代码或暂停服务，先明确操作／影响并取得用户批准，不以部署授权代替。若无法安全恢复，说明具体阻塞及受控失败收口的损失，请用户决定，不私自放弃原结果，不无限摸索。
3. 定向复核真实整数消息身份从claim到receipt及HTTP的转换，并区分Schema校验错误与真实存储错误。保持实现直接，不加兼容层、通用恢复平台或更多无依据重试。保留已完成结果重送、不重复模型执行、真实失败和有限预算。
4. 原阻塞妥善处理且无其他在途工作后，按既有方式加载最终实现并保证完整系统可用。先确认原回执、成果及Agent容量，再用现有模型做一条短续聊，实际读取原合成附件、得到最终回复，刷新仍可见；如需新生成成果，保持小型明确任务，不将新任务成功冒称原任务恢复。
5. 定向补受控短断网／持续故障及草稿／滚动恢复。分析轮已实测LAN短回复与多行原文复制逐字符相等、3次正常SSE轮换无警告；相关代码未改不用重跑。第18.9节EvidenceReader仍有3项未通过，只针对共享复制影响定向查明，不默认全量测试。

不启动研究、真实目标复测或终态综合报告，不删除用户历史，不新增依赖，不重下载未变化Worker工具层。最大化桌面验收，不要求窄窗／移动端。不要改其他平台功能。

审阅精确diff并回填原方案，明确区分人工恢复、修复部署、受控自动重送和真实新对话。若结果无法安全保全，不得宣布恢复完成。最终报告分支、文件、验证结果、是否commit及剩余风险；未经批准不commit／push。
```

### 19.5 本分析轮工作声明

当前分支 `feat/update-worker-cache`。本轮仅更新本方案；只读接口、实际复制／粘贴、正常 SSE 观察及完整页面看图已执行，未运行自动化测试、构建、迁移、模型、业务任务或进程恢复。临时浏览器观察器已移除、复制验收草稿已清空；无 commit／push。当前真实回执阻塞仍然存在，不因本轮证明复制可用而宣布整体验收通过。

### 19.6 恢复实施轮：原结果人工收口（2026-10-29）

以下是第 19.5 节之后的实施结果，不改写此前阻塞、失败或未执行记录。范围仍是回执恢复与本轮验收收尾，不新增能力、不重新执行原材料任务。

1. **先核实、后恢复。** 重启前核对 `proj_060`／`conv_72dbd6c1faa34825a913da17075ab056`／消息 9／`tr024`、原 Dispatcher 身份和执行代次。PostgreSQL 只读查询确认原消息仍无答案、任务 running，且只有这一项在途；原私有 receipt 目录为空。源码未找到可读出活 future 的受支持控制入口，未将文档中的“结果仍在”直接当作事实。
2. **取得单独批准后只读提取。** 用户明确批准一次限定范围的只读进程内存检查。使用现有系统工具及 CPython 布局，只以只读方式访问原进程的目标对象；未附加调试器、暂停进程、注入代码、转储整进程或安装依赖。核对进程启动身份后，沿主线程中的 Dispatcher 实例定位唯一目标 future，连续两次核对结果和生命周期一致。
3. **真实完成与终止证明。** 目标 future 为 `FINISHED`、无异常，原结果为 `succeeded`，包含 239 字符答案和 usage。原 cancellation 中执行记录的 started／finished／completed 均为真，资源记录为空；终止证明来自这些冻结的执行记录，而不是 session 文本或“当前无模型进程”。
4. **先保全，再沿原 finish 提交。** 使用既有 `_PendingAgentFinish` 与原子保存／fsync 路径，把原结果逐值保存在原私有 receipt 目录，仅将消息身份 9 表示为字符串；回读验证身份和原值。另保留不含答案的最小私有恢复证明。通过原 Dispatcher 认证与原 `finish` 入口人工提交，返回 **HTTP 200**；PostgreSQL 只读复核 `tr024=succeeded`、answer／error_code／usage 与原结果逐值一致。成功后删除已消费待发送 receipt，保留恢复证明。
5. **损失与边界。** 未直接 UPDATE 数据库终态，未从 session 重组成功结果，未重跑原模型、删除会话或替换成果。原 session 绑定未改变，Artifact `a21011` 仍鉴权下载 **200、412 字节**。旧进程内槽位未通过写内存强行释放，随后以获批部署重启收口。

这是**人工恢复原任务**，不计作自动故障恢复或重启自动重送的真实环境验收。

### 19.7 本轮代码收敛与安全部署

- `dispatcher/models.py`：`RunningTask.agent_message_id` 标注为真实 claim 的 `int | None`；`protocol/client.py` 的 action 参数明确接受整数 claim 身份与字符串 receipt 身份，URL 构造不变。
- `scheduler/agent_chat.py`：保留接手时已有的 `message_id=str(task.agent_message_id)`，不将其冒称本轮新增；将确定性 Schema／身份／格式 `ValueError` 分类为 `chat_finish_receipt_invalid`，真正 `OSError` 仍为 `chat_finish_storage_unavailable`。复用现有 `_log_changed`／`_clear_log_state`，相同诊断不逐 tick 刷屏，恢复后清理诊断状态；不打印可能包含原文的异常正文。
- 首次 receipt 构造／保存失败仍保留 future 和原结果；现有终止证明检查、有限重送预算、拒绝原文保护与容量交接不变。没有引入通用恢复平台、猜测性兼容层或额外依赖。
- `test_agent_chat_execution.py`：claim 与相关任务 fixture 改用整数 9，并验证最终 HTTP 身份为字符串；新增 Schema／I/O 两类错误在 reap 和 load 两处的定向用例，覆盖保留结果、阻止错误认领、不泄露正文、日志去重和恢复后的状态清理。
- 原回执成功且再次确认无 queued／running TaskRun、无未认领 Agent 消息后，用户单独批准短暂停机部署。按既有 `deploy/stop.sh`／`deploy/start.sh --host 0.0.0.0 --port 8001` 加载最终 Python 实现。Server、Dispatcher、HTTP API 均恢复；复用既有环境、前端产物与 Worker 工具层，未安装依赖、重建 Worker 或重新下载工具。
- 最终现有监控入口报告 `idle`、心跳未过期、`running_task_count=0`、`agent_capacity=1`；全局 queued／running TaskRun 数为 0，近期 Dispatcher 日志未见新的回执错误。未再次侵入进程检查槽位。

### 19.8 唯一真实短续聊与刷新验证

部署后仅在原会话发送**一条**短续聊，要求用附件工具读取上一条消息的合成 CSV，只答列名和数据行数，不重新求和、生成文件、读取研究材料、访问网络或启动业务任务。

- 新身份为消息 **10／`tr025`**，最终 `succeeded`、`error_code=null`，得到 16 字符短答，内容为两列及两条数据行。
- 只定向核对该新轮工具记录：实际调用 `read_conversation_attachment`，参数精确匹配原附件；结果 `isError=false`、`readable=true`，从 offset 0 读到全部 17 个字符，`next_offset=null`。不是仅凭答案正确推断读取成功，也没有重建原轮结果。
- 硬刷新后 API 中消息 9 与 10 均保持成功，原答案 239 字符、新答案 16 字符；页面显示两条最终回复及原成果。原答案是 Markdown，不能用原 Markdown 字符串是否逐字符出现在渲染后 `textContent` 中判断丢失。
- 新任务可正常派发并终结，加上最终心跳运行任务数为 0，证明原 Agent 容量阻塞已解除。不再发第二条模型任务来“证明空闲”。
- 桌面等效视口 **1914×1000、DPR 1、页面 scale 1** 下实际查看完整截图，三栏、回复和输入器可用；不声称操作了原生窗口最大化。未启动研究、真实目标复测或终态综合报告。

### 19.9 页面局部故障与定向测试结果

Chrome MCP `emulate` 报 `Network.emulateNetworkConditionsByRule` 缺少 `offline` 参数，**未成功施加真实断网**。随后在实际已部署页面做局部受控验证：仅包装当前合成会话的原生 EventSource 和消息补读 fetch；主动关闭该连接并投递错误事件，指定期间仅对该会话 GET 补读返回网络错误。恢复时继续使用真实接口／原生 SSE，不模拟成功数据、不改宿主网络、不影响服务或其他页面。

| 检查 | 本轮实测结果与边界 |
| --- | --- |
| 持续故障 | 补读分别在故障后约 1／3／7 秒发生，对应 1／2／4 秒退避；3 次均失败后出现“消息更新暂不可用／状态可能滞后／重新连接”。终点后再观察 9 秒，补读次数仍为 3。 |
| 草稿与阅读位置 | 故障、失败终点、手动重连和短故障恢复期间，同一未发送合成草稿逐值保留，消息区 scrollTop 均为 80，没有跳到底部。 |
| 手动重连 | 解除注入后实际点击“重新连接”，真实 SSE 恢复，警告消失，草稿与位置不变。 |
| 短故障 | 再次局部注入，约第 1 秒补读失败，约第 1.4 秒解除注入，第 3 秒真实补读／SSE 恢复；全过程无终点警告。 |
| 无重复发送与清理 | 故障测试期间该会话非 GET fetch 次数为 0，没有点击发送；结束恢复原生 fetch／EventSource，移除观察器并通过输入事件清空测试草稿，发送按钮恢复禁用。控制台无 error／warn。 |

自动测试只执行直接相关子集：

| 命令／范围 | 结果 |
| --- | --- |
| `test_agent_chat_execution.py -q -k 'finish or real_executor_capacity'` | **16 passed、5 deselected**。覆盖整数身份、同进程／模拟重启后回执重送、原结果保留且不重跑模型、拒绝与预算，以及新增分类／去重。它们是受控测试，不冒称真实断网后的自动恢复。 |
| `ProjectAgent.component.test.ts -t '阅读旧消息\|断线补读\|明确正常轮换和短故障'`，threads／单 worker | **3 passed、23 skipped**。验证阅读位置、补读与事件版本、有限失败和手动重连保留草稿。 |
| `EvidenceReader.component.test.ts -t '公共头\|大正文默认\|复制原文\|异常字节'`，threads／单 worker | **5 passed、17 skipped**。本轮所选公共头、原文复制、异常字节及 10MiB 大正文用例通过；未修改共享复制／EvidenceReader 实现或测试。 |
| 修改的三个生产 Python 文件 `py_compile` | 通过。 |
| 精确 diff／空白检查 | 以接手时五个文件内容为基线审阅本轮增量；保护已有未提交／未跟踪内容，不把整份新文件或全仓差异算作本轮。 |

测试环境限制：首次直接使用项目 venv 执行 pytest 报 `No module named pytest`，未执行测试；随后用同一 venv Python 加载现有系统 pytest 路径执行上述限定用例，未安装包。EvidenceReader 历史记录为“两个文本定位断言失败、一个大正文超时”；本轮相关子集未复现，但缺少历史完整失败日志，不能据此确定旧失败根因或将其改写为已修复、全文件全绿。没有跑其余用例、全量套件或本轮前端正式构建。LAN 复制和正常 SSE 轮换沿用第 19.1 节已通过的真实验证，不再重复专门验收。

### 19.10 本轮交付边界与剩余项

- 当前分支仍为 `feat/update-worker-cache`。本轮只改四个 Python 生产／测试文件及本方案；不覆盖既有修改，**未 commit／push**，未修改 CHANGELOG 或混入其他平台需求。
- 原任务已人工无损收口、分类修正已部署、唯一真实附件续聊已成功、页面局部故障恢复已验证。人工恢复、自动重送受控测试、真实新对话三种证据分别成立，不互相替代。
- 未做真实网络层断连或生产进程崩溃演练；自动 receipt 重送的故障／重启证据仍是受控测试。首次 receipt 尚未落盘时若进程直接丢失，不能保证找回只在内存中的结果，本轮人工提取不是通用灾难恢复保证。
- EvidenceReader 历史三项失败保留为“相关子集本轮通过、历史原因未完全确定”；若后续再次复现，应保留完整日志定向归因，不据此扩展全量重构。
- 项目创建、追加 Intent、附件转交复测等独立业务范围仍只有原方案对应的受控证据；本轮没有新增真实业务验收，也不宣称提升漏洞发现能力。

## 20. 2026-10-29：恢复轮收尾，下一轮验收一个真实业务动作

### 20.1 最新复核与取舍

恢复轮可以按其限定范围收尾，不再把第 19.1 节的阻塞当作当前状态。本分析轮在既有 LAN 页面正常鉴权下，只读原会话消息接口返回 200：消息 9／tr024 和消息 10／tr025 均为 succeeded、error_code=null，答案分别为 239／16 字符；原消息仍关联一个附件和一个成果。源码已将回执 ValueError 与 OSError 分别诊断，并保留字符串化消息身份。这些是本轮直接核对结果。

人工恢复过程、实际附件工具调用、部署空闲快照和局部故障验证沿用第 19.6–19.9 节实施记录，不冒称本分析轮重做。真实网络层故障与崩溃恢复仍未验证，但不是普通业务验收的前置门槛；不为完善可靠性演练继续阻塞产品使用。

下一步推荐**只打通“对 Agent 说清目标、带已有材料创建新项目、打开真实项目确认”的路径**。该能力已有代码但尚缺真实模型到业务接受的闭环证据；不是新增功能。当前 `agent_task_actions.py` 已复用原创建服务，附带附件可转资源批次，初始状态为 paused，工作卡返回真实项目链接。相对直接复测或追加研究任务，该路径不需要启动研究、访问目标或购买大任务，可检验 Agent 是否真正调用平台能力。

当前追加 Intent 只面向会话所属项目，并受原服务 active 检查；不要为了把多个场景塞进一轮，恢复旧项目、伪造 active、移除检查或向刚创建的暂停项目强塞 Intent。复测真实目标、并行研究与终态报告留待具体目标／预算明确后再做，不在本轮串联启动。

### 20.2 下一轮范围与完成条件

- **一条轻量模型任务、至多一个合成暂停项目。** 复用原合成 CSV 和当前已成功的材料会话；使用清楚标识为验收的项目名称，Origin／Goal 明确仅合成材料、不授权访问任何目标、不启动研究。授权期限等必要字段按当前真实创建合同提供合理明确值，不猜目标或使用用户秘密材料。
- **业务接受为准。** 要求实际 `create_project` 调用，携带原附件身份。核对消息最终回复、动作的真实项目 ID、工作卡链接与项目记录一致；状态保持 paused，没有该项目的研究 TaskRun 或对目标的访问。创建成功不表示研究完成。
- **材料落到实际消费者。** 从新项目正常资源入口核对 CSV 可读取、内容与原合成材料一致、归属正确；不能只看 attachment_ids 参数存在就宣称材料转交成功。只核对小型合成数据，不转储其他材料。
- **用户入口顺畅。** 实际打开工作卡项目链接、刷新聊天和项目，确认结果仍在且没有新增重复项目；以等效最大化桌面看图和操作。验证后保留明确标识的暂停验收项目，不自动删除，也不自动启动。
- **只有发现缺陷才改代码。** 优先查真实工具参数→原业务服务→动作引用→UI消费者，不新增 Agent Loop、状态系统、工具框架或额外审批。确定性错误先本地修复验证，再决定是否需要额外一次模型调用，不能不断发消息试错或假定换 tool_call_id 仍会去重。若没有缺陷，交付验收记录即可，不为本轮制造代码改动。
- 相同消息／tool_call_id 幂等的异常重送仍可沿用现有受控测试；不在真实环境主动重复发创建指令来测试幂等。发现已有业务成功但回复丢失时先核对原动作，不立即重发创建。

本分析轮不执行创建。以下提示词交给新实施会话并执行时，才是本轮明确的小型业务操作范围；它不授予真实目标访问、研究启动或侵入进程权限。

### 20.3 新实施会话提示词

```text
本轮只做“项目 Agent 携带合成附件创建暂停项目”的真实业务验收与必要修复，不重新做回执恢复，也不新增功能框架。

遵循 CLAUDE.md、AGENT.md、docs/ui-design-standard.md；先读 docs/specs/project-agent-capabilities-design.md 第19.6–19.10节和第20节。编辑前检查git status --short、git branch --show-current，保护现有修改，在任务分支工作。

原消息9／tr024已人工恢复成功；新消息10／tr025已真实续聊成功，分析轮只读接口也确认两者succeeded，不再按旧阻塞方案操作。原材料会话为proj_060／conv_72dbd6c1faa34825a913da17075ab056，已有合成CSV和Markdown成果。先核实当前身份与状态，不重新读取进程内存或重跑原任务。

本轮允许用平台已配置模型做一条轻量对话，通过现有create_project工具，携带原合成CSV，创建至多一个明确标识为验收的暂停项目。项目仅处理合成材料，Origin／Goal说明不授权目标访问、不启动研究；按实际创建合同提供必要字段。不得恢复原项目、启动新项目、追加研究Intent、创建目标复测或生成终态报告。

按第20.2节核对真实闭环：实际工具调用→服务接受的项目ID→动作／工作卡→项目页面；附件必须从新项目资源入口实际可读且与原合成材料一致。核对新项目paused、无研究TaskRun；刷新后回复、链接和材料仍可用、不重复创建。不要只凭模型回答或测试全绿宣布完成。

先验收已有实现；遇到确定性缺陷才修直接相关上下游，不不断调用模型试错。业务已成功而回复失败时先查原动作，不重发创建。没有缺陷就只写验收记录，不强造代码改动。复用既有幂等受控证据，不在真实环境重复创建来测幂等。

UI按最大化或等效桌面实际看图／操作；不要求窄窗。验证后保留明确标识的暂停验收项目，不自动删除。若有运行代码变更，按既有部署路径加载，部署前核对在途工作，不打断用户任务；不重下载未变化的Worker工具层，不新增依赖，不默认全量测试。

回填同一方案，分别说明真实模型调用、业务接受、材料转交和页面证据；保留仍未验证的研究／复测边界。报告当前分支、文件、测试／实际操作结果、是否commit和剩余风险。不commit／push，完成本轮后停止。
```

### 20.4 本分析轮声明

分支 `feat/update-worker-cache`；仅更新本方案文首和第 20 节。只读核对最新实施记录、相关源码与两条既有消息状态；没有运行模型、自动化测试、构建、部署、恢复进程或创建／删除业务对象。原恢复许可不自动沿用到未来会话。无 commit／push。

### 20.5 真实业务验收：身份与唯一模型轮次（2026-10-29）

本节及后续是执行第 20.3 节限定任务的结果，不改写此前分析轮未操作声明。本轮先核对工作区和任务分支，保护已有修改；正常登录本地已部署 Arena，未重做回执恢复、读取进程内存或重跑旧任务。

- **起始身份核实：** 原项目 `proj_060` 为 `stopped`；原会话 `conv_72dbd6c1faa34825a913da17075ab056` 的消息 9／`tr024`、消息 10／`tr025` 均为 `succeeded`、`error_code=null`，答案分别为 239／16 字符。动作列表原为空，原附件与 `a21011` 成果关联仍在。
- **原件核实：** 消息 9 的 `att_57f5c63ce66a4522ac46ec4e966f81e4`，文件名 `合成数量.csv`。原附件正常鉴权下载 HTTP 200、29 字节；UTF-8 内容为 `名称,数量\r\n甲,2\r\n乙,3\r\n`，保留 CRLF。SHA-256 为 `98c0cd13ec97214fa0b6fb034112bcdef376ab8d2c6c0d9a537bf3b38d7ae37f`。
- **唯一新轮次：** 在原会话页面实际输入并点击发送一次，使用既有配置 `wangsu`／`wangsu-ds-flash-latest`。新消息 **11／`tr026`**，创建于 **2026-10-29T15:00:16Z**，任务类型 `agent_chat`，最终 `succeeded`、`error_code=null`，答案 276 字符。只发送这一条用户消息，不把工具前后模型推理轮次说成只有一次底层 HTTP 请求。
- **创建合同：** 名称 `合成附件暂停项目验收-20261029-01`；Origin／Goal 明确只使用原合成 CSV、不授权任何目标或外部网络访问、不启动研究。显式提供 `authorization_expires_at=2026-10-30T23:59:59Z`，仅为合成验收记录期限；省略 scenario 使用现有默认值。指定原附件 ID，不重新上传、不生成替代文件；要求失败或不确定时不重试创建。

### 20.6 实际工具调用与业务接受

不是仅凭模型回复判断创建成功。通过正常 TaskRun 接口取得本轮 session 绑定，只读该 session 中本轮用户消息之后的工具调用与结果，不提取进程内存、不重建旧轮回执。

| 层次 | 本轮直接证据 |
| --- | --- |
| 实际调用 | 本轮 session 只有 **1 次工具调用**：`create_project`，`tool_call_id=call_00_p9qsw91ch44uww0moavjssox`。title、origin、goal、期限及原 `attachment_ids` 与提交要求一致；没有 bash、网络访问、研究、复测或报告工具调用。 |
| 服务回执 | 同一 toolCallId 的结果 `isError=false`；返回 `action_id=act_239796a2ce994e6589abcdbe9eec7caf`、`project_id=proj_062`，work 为 `succeeded`、error 为 null，result 为 `project_status=paused`，url 为 `/projects/proj_062`。 |
| 持久动作 | 会话 `/actions` 正常鉴权 GET 200，仅返回该动作；消息 11 的 actions 引用同一 ID，项目详情 GET 200 确认同一 `proj_062`、验收名称与 Origin／Goal。 |
| 最终回复与卡片 | 回复中的项目 ID、暂停状态、项目路径、action_id 与真实回执一致；右侧工作卡显示“新建项目／已创建”，其“查看项目”链接为 `/#/projects/proj_062`。 |
| 边界 | 模型回复明确“附件已按 ID 提交，但工具回执未单列附件，未另行核验”，没有冒称已验证原件；附件落地由下一节直接验证，不把业务接受当作研究完成。 |

取证中曾对一个现存 PostgreSQL 容器发起只读查询，返回 `relation "agent_conversations" does not exist`，未得到业务证据、未写数据库，随即停止该路径；最终身份、动作与项目核对均使用当前服务正常接口，实际调用使用由 TaskRun 接口定位的原 session 记录。不据该失败推断当前业务缺陷，也未扫描或修改其他数据库。

### 20.7 附件转交、页面操作与刷新

1. **正常资源入口真正可读。** `/projects/proj_062/resources` GET 200 返回唯一资源 `r001`，归属 `proj_062`、路径 `合成数量.csv`、active=true、29 字节。通过既有 `/projects/proj_062/resources/r001` GET 200 取得原字节，与原会话附件逐字节比较相等，完整 SHA-256 与第 20.5 节一致；不是仅比较文件名或相信 attachment_ids 参数。
2. **实际打开工作卡。** 点击原聊天右侧“查看项目”，真实跳转到 `/#/projects/proj_062`，标题为验收名称、状态“已暂停”。通过“项目更多操作 → 资料 (1)”打开项目辅助资料弹窗，实际看到同一 CSV、29 B 和 hash 摘要；没有点击继续项目、结束生成报告、停用或上传。该弹窗本次验证的是资源条目，完整内容读取由上述正常鉴权资源 API 完成，不冒称 UI 已提供 CSV 正文预览。
3. **项目刷新。** 对项目页面执行忽略缓存刷新，再打开“资料 (1)”确认材料仍在；正常 API 再读 `r001` 仍为 200、29 字节且 hash 相同，项目仍 paused、Intent 数 0、TaskRun 列表为空。
4. **聊天刷新与去重观察。** 返回原会话并忽略缓存刷新；消息 9／10／11 均保持成功、error_code=null，原附件与成果关联保留，新回复、同一动作和同一工作卡链接仍在。当前用户可见项目由验收前 4 个变为 5 个，创建后的集合差仅 `proj_062`；最终同名验收项目仅一个、会话创建动作仅一个。没有再次发送创建指令或以真实重复创建测试幂等。
5. **桌面实看。** 使用 **1920×1080 CSS 视口、DPR 1、页面 scale 1** 的等效桌面窗口；实际查看完整聊天截图、项目资料弹窗截图及聊天硬刷新后的完整截图，并完成输入、发送、链接和菜单操作。三栏回复与工作卡、输入器、弹窗正文及底部操作可见可用；不声称原生窗口最大化或用户已确认视觉设计，不做窄窗矩阵。项目图谱自身显示 110% 是画布缩放，不是浏览器页面缩放。

### 20.8 本轮交付与保留边界

- **限定闭环通过，未发现需要定向修复的确定性缺陷。** 保留验收项目 `proj_062`，最终状态 paused；新项目 TaskRun 总数为 **0**，研究／复测尝试数均为 0、无 Intent；旧项目仍 stopped。本轮唯一 TaskRun 是原项目下的轻量 `agent_chat`。没有启动／恢复项目、访问目标、追加研究、创建复测或终态报告，也未删除验收项目。
- **改动仅本方案：** 文首更新当前结论，追加第 20.5–20.8 节；既有历史记录与未提交文件均保留。分支 `feat/update-worker-cache`，未修改运行代码、未新增依赖、未构建／部署／重启服务，未下载 Worker 工具层，**无 commit／push**。
- **验证方式：** 一条真实模型对话、单次真实业务创建、限定 session 工具证据、正常鉴权 API、原件字节比较、桌面页面操作和硬刷新；未运行自动化测试套件。纯记录更新只审阅本轮精确 diff 与空白，不以旧受控测试替代本轮业务证据。
- **仍未验证：** 真实目标研究、追加 Intent、目标复测、研究并行、终态综合报告及真实网络／崩溃恢复不在本轮范围。刷新后未重复创建不等于已做异常重送幂等演练；不因本次合成验收宣称这些能力已通过，也不自动开始下一轮。

## 21. 2026-10-29：创建业务收尾，转向一个有实际用途的漏洞复测场景

### 21.1 当前结论

第 20 节限定目标已达到，不再安排重复创建项目、恢复旧回执或修已通过的复制／字号／正常轮换。本分析轮通过既有 LAN 页面只读核对：消息 11／tr026 为 succeeded、error_code=null，答案 276 字符；动作 `act_239796a2ce994e6589abcdbe9eec7caf` 为 project／succeeded，链接 `/projects/proj_062`，业务状态 paused。原件逐字节核对、实际页面跳转及无研究 TaskRun 等沿用第 20.5–20.8 节实施证据，本轮不冒称重复执行。

基础聊天、附件处理、定制文件、跨轮读取和携带材料创建暂停项目已具备分段真实证据，可以进入日常使用；没有理由为每个剩余未知继续制造一轮代码改动。若继续推进原始能力目标，推荐选择**一个用户实际需要复核的已有漏洞，带相关补充材料，从聊天发起单次复测并查看真实结果与报告**。收益是验证 Agent 能把现有发现和材料交给真正的验证业务，而不只是创建一条记录。不宣称这能证明自主漏洞发现能力已提升。

### 21.2 实施前必须明确的实际范围

复测与第 20 节创建暂停项目有实质区别：`agent_conversations.py:349–357` 直接调用既有复测服务；`verification_requests.py:738–769,786–821` 固定来源快照、继承来源授权／出口并创建 Intent，正常创建后将专用项目置为 active，可能立即派发付费执行和访问目标。**不能先创建“看一下”再询问授权，也不能把“适量调用模型”当作目标访问许可。** 数据库里有历史授权字段也不等于本次分析已经确认可做真实复测。

本轮尚未指定复测漏洞、当前有效目标范围、补充材料及执行预算，因此这里是下一步建议，不是当前会话启动复测的批准。新会话先只读定位一个候选并说明已有证据；在首次创建前，由用户确认具体 project_id／vulnerability_id／observation_id、允许访问的目标与方法边界、必要账号以及预算／时间终点。已有明确且适用于本轮的授权无需重复索取；缺少的部分一次问清。没有合适案例就停止在建议，不伪造漏洞、不把 `proj_062` 合成 CSV 项目当成靶场，也不为验收另起广泛扫描。

### 21.3 获得具体授权后的单场景完成条件

- 使用漏洞所属项目的会话，实际读取选定 observation 和相关材料，再调用现有 `request_vulnerability_retest`。只发起一次业务请求；记录动作、复测请求与专用项目身份，不将聊天完成当作复测完成。
- 相关补充附件进入复测资源后，验证执行者确实读取或明确说明未使用，不能只凭传入 attachment_ids 认定已消费；无关合成 CSV 不作为复测材料凑覆盖。缺失材料由用户提供，不复制凭据到日志、方案或报告。
- 跟踪原有业务状态到真实终态，工作卡与复测详情一致。核对本轮观察、结论依据、未验证范围及下载报告；成立、不成立、受限和执行失败按实际合同区分，历史发现不自动作为本轮观察。
- 不启动／恢复来源项目，不追加其他研究，不顺带做全项目综合报告、并行 Worker 演练或自动连续复测。发现平台确定性阻断先本地定向修复，不通过反复真实模型／目标调用试错；请求已接受而聊天回执失败时先查原动作，不新建替代请求。
- 现有实现直接通过就只更新记录。只有具体缺陷才改相关上下游；没有新的证据不加恢复平台、审批系统或工具框架。普通能力边界与本次开发操作授权分开，用户已明确范围内的执行不逐步重复审批。

### 21.4 可复制的下一会话提示词

```text
本轮目标是“通过项目 Agent 对一个明确授权的已有漏洞发起带相关材料的单次复测，查看实际结果与下载报告”，不是继续重复合成创建验收。

先读 CLAUDE.md、AGENT.md、docs/specs/project-agent-capabilities-design.md 第20.5–20.8节和第21节。检查当前分支与工作区，保护已有修改，在任务分支工作。基础回执恢复及合成附件创建暂停项目已通过，不重做旧任务、不启动proj_062。

当前还没有选定实际复测对象或本次目标授权。先只读检查合适的已有漏洞及其observation，推荐一个最小案例，列明来源ID、目标、需要的相关材料、复测配置和现有预算／时间限制。不要全仓审计、扫描目标或创建复测请求。创建复测会使专用项目active并可能立即执行，不是只建草稿；必须在首次创建前让用户确认尚缺的具体目标／方法授权与预算。历史记录或轻量模型许可不能代替它。若已有本轮明确授权则直接遵循，不重复索取。

取得范围后，只使用一个所属项目会话和一次现有request_vulnerability_retest业务请求。贯通实际读取来源证据→相关附件转交且被消费→服务接受→专用执行→工作卡／详情→真实结果与下载报告。聊天succeeded不是复测完成，历史漏洞不等于本轮确认；受限或执行失败如实说明。

不启动来源研究、不添加其他Intent、不做全项目综合报告、不扩大目标或自动重试复测。若业务已接受但回复丢失，先查询原动作，不能重新创建。遇到确定性平台缺陷只修直接相关路径，先用最小本地验证，不不断付费试错；现有实现通过则不制造代码改动。

页面按最大化或等效桌面操作与看图。若改运行代码按既有方式安全加载，不打断在途任务、不重下载未变化工具层、不新增依赖、不默认全量测试。更新同一方案，分别记录业务接受、真实执行、结论质量和未验证范围。报告分支、修改文件、验证结果、提交情况与剩余事项；不commit／push，完成后停止。
```

### 21.5 本分析轮声明

当前分支 `feat/update-worker-cache`；仅更新本方案交接指向并追加第 21 节。实际执行了既有消息／动作只读查询和相关源码核对，未运行自动化测试、模型、构建、部署、目标访问或业务创建。差异按本轮基线审阅；无 commit／push。真实复测的具体对象与授权仍待用户确认，不把推荐写成已获准执行。

### 21.6 单次复测前置只读检查：候选与待确认边界（2026-10-29）

已阅读指定规范与第 20.5–20.8、21 节，核对任务分支并保护原有修改。正常登录本地 Arena，通过漏洞列表实际展开详情，并使用正常鉴权 GET 查询来源、历史复测、资料和运行配置；没有点击“复测此漏洞”，也没有向项目 Agent 发送消息或访问被测目标。

- **推荐候选（尚未授权）：** `proj_060`／`v001`，Observation `vo_0a672b7c7b5d49838dd991ea79b56771`，来源 Fact `f002`、发现 TaskRun `tr005`；已有主张为 `GET /?id` 未认证 UNION 注入及用户表口令读取。相较同项目写文件／命令执行主张及其他项目业务数据修改主张，可缩小为只读的单参数复核。来源项目实际为 `stopped`，当前无执行任务。
- **目标与适用性限制：** 漏洞目标是 `http://5bfdab74-953b-44fe-8c98-c898ea116a1f.challenge.ctf.show/`；Origin 写的是同域名 HTTPS。历史请求 `vr_343d83d564524c9db480941377a7c420` 为 completed／confirmed，但其限制明确写有 DNS 不可解析、改连历史 IP 并保留 Host、未取得 DNS 同源确认，HTTPS 也未验证。本轮未探测 DNS 或网络，不知道靶场是否仍有效；历史结论不是本轮确认，历史 IP 不自动纳入本次授权。必须由用户确认当前授权仍适用于该靶场；正常解析／同源条件不满足时应受限结束，不擅自沿用 IP 绕过。
- **真实相关补充材料：** 来源项目资源列表为空，但历史验证 `tr007` 已登记正常页 `a349`（`id_1.html`，1123 字节）、负向页 `a350`（`id_999.html`，1084 字节）和 UNION 常量回显 `a351`（`q_union123.html`，1081 字节）。三个正常鉴权 preview 均 HTTP 200、完整且未截断，正文分别包含正常内容、未找到页面及 `1-2`／`3` 回显。可在批准后将这些原件作为补充附件逐字节转交，用于当前响应对照；不能用历史原件代替本轮响应。不使用合成 CSV、不附带历史口令值；目前尚未下载、上传或形成附件 ID，也未验证执行者消费。
- **配置与实际限制：** 租户 readiness 为 ready；专用“漏洞复测专用 Worker”，引擎 `pi`，模型 `wangsu`／`wangsu-ds-flash-latest`，并发 1、配置 revision 1、enabled=true。正常 `/dispatcher/runtime-settings` 返回 loaded，`tasks.explore.timeout=1200` 秒、`conclude_timeout=600` 秒；后者受剩余执行时间约束，不是额外增加十分钟。来源授权到期与执行截止均为 null，出口为 direct；这些历史空值不代表本轮无限授权。
- **工具合同限制：** 现有 `request_vulnerability_retest` 只接受 `vulnerability_id`、可选 `observation_id` 和 `attachment_ids`，不接受金额／Token／目标请求次数硬预算或本轮执行截止字段；创建成功即进入专用自动执行流程。用户需要确认一次 Agent 请求加一次专用执行的资源范围；不能把“单次业务请求”说成只调用一次模型或一次 HTTP，也不能把附件中的次数约定冒称服务端硬上限。
- **待一次性确认：** 是否选择上述来源、该域名 HTTP/80 是否仍在用户当前授权内；建议仅正常页／负向对照／UNION 常量回显及不返回口令原文的读取能力验证，不写库、不读写文件、不执行命令、不枚举其他目标，不自动跳转或回退历史 IP。若仅批准这些动作，报告必须明确未重新验证口令原文及完整历史影响。建议本次专用执行沿用现有 1200 秒时限、一次业务创建、不自动重试，并另明确是否要求平台目前不具备的金额硬封顶；未确认前不创建。
- **实际验证与异常：** 已在 1920×1080 CSS、DPR 1、scale 1 的等效桌面窗口操作并看图，不冒称原生最大化。一次浏览器批量 GET 的工具返回发生 `Runtime.callFunctionOn timed out`；随后在同一页面读到已完成的配置与资料响应，并以独立 GET 取得运行超时和三份完整 preview。没有重发任何创建操作，也不足以据此认定业务平台存在确定性缺陷。
- **阶段结论：** 来源检查与材料可读性已验证；业务接受、专用执行、本轮结论、材料实际消费和本轮报告下载均未发生。只追加本节，不改运行代码、不构建／重启、不下载 Worker 工具层、不运行自动化测试，无 commit／push。停在首次业务创建之前，等待具体范围与预算确认。

## 22. 同题新实例：本次目标绑定的局部实施，不启动真实复测

### 22.1 用户补充与当前事实

用户确认原靶场地址失效，新地址是**同一道题的新实例**：`https://edb56d2a-49a6-40c5-b71f-0d7da98bb152.challenge.ctf.show/`。这解决了实例关系的未知，不证明新实例漏洞仍成立，也不改变旧证据的实际来源。第 21.6 节停在前置检查，不能写成真实复测完成。

用户提供的新会话错误为上游 API 400，正文说明内容被标记为 possible cybersecurity risk，并提示通过提供商的正式访问申请渠道处理。它不是目标站点的 HTTP 400，也不是 Arena 已产生的复测结果。本轮不重发被拦请求、不改写或转用其他通道绕过拦截；平台的本地数据建模与受控测试可以独立进行，不能据此宣布上游问题解除。

本轮核对 `CreateVerificationRequest`：已有 `origin`、来源 report、资源批次、授权期限、执行截止和出口字段，但没有明确的本次目标地址字段。Agent 现有复测工具只传来源漏洞／Observation／附件。不能据此断言全平台绝无可复用能力；实施前应定向追踪现有目标与授权构造路径，复用有效部分。之前第 21 节按原地址安排真实验收的计划，应让位于本节，不再尝试对失效旧地址执行。

### 22.2 下一轮最小完整范围

目标是让**历史来源与本次执行目标分别明确，且下游实际使用同一个本次目标**。本轮是普通平台功能实施，仅本地受控验证，不访问上述地址或其他被测目标、不调用模型、不创建实际会被 Dispatcher 领取的复测任务。

1. 定向检查请求 Schema、Agent 工具、创建服务、现有授权／出口构造、执行上下文、详情与报告消费者。若底层已有适用能力，补齐入口与透传即可；只有确有缺口才增加一个权威的本次目标表达，不新增平行来源快照、映射平台或通用迁移框架。
2. 原项目 Origin、漏洞目标、Observation、旧报告、原始请求／响应等保持不变；本次目标单独保存。不得全局字符串替换历史材料，不把旧证据“改成新地址”，不静默修改其他排队／运行任务。
3. 用户未指定新目标时维持原目标复测的正常语义；明确指定时，请求接受、执行上下文和现有范围判断必须一致使用本次目标。历史目标仅作为来源，不作为本轮默认回退；不自动切换协议、历史 IP 或其他域名。复用现有授权机制，不默认复制目标专属凭据或扩大授权，也不为本功能另加逐次审批系统。不能只靠追加一句 Prompt 解决相互冲突的目标来源。
4. Agent 入口能显式提交本次目标；原详情与报告能分别说明历史来源和本次目标，真实结果仍只由本轮证据支撑。仅改必要展示，不重做全站 UI，不扩展批量换址或其他任务管理。
5. 使用隔离 PostgreSQL／现有测试设施和模拟执行端，验证：原目标路径不退化；新目标贯通到实际执行输入及详情／报告；原件不变；同一幂等身份更改目标不能复用旧请求成功；旧地址不会被误当本次目标。只覆盖实际触及的路径，不启动完整研究、不增加新依赖、不默认全量测试。测试不能意外把真实库任务置为 active；无安全隔离条件则说明限制，不借真实环境试验。
6. 有运行改动按现有部署规范加载完整系统，部署前核对在途工作，不打断用户任务；不通过创建真实复测来证明部署。涉及 Worker 应用代码才更新相应应用层，复用未变化工具层。分开报告代码完成、受控验证、部署生效与真实复测未验收。

本轮不加入金额硬上限、故障恢复平台、全新 Agent Loop 或漏洞利用逻辑。上游拦截通过正式渠道处理；后续真实复测需要可用的合规执行渠道和明确的本次操作范围，不能由本轮本地测试自动授权。

### 22.3 下一实施会话提示词

```text
本轮只实施“同题新实例的复测目标绑定”，并做本地受控验证，不访问靶场、不调用模型、不启动真实复测。

先读 CLAUDE.md、AGENT.md、docs/ui-design-standard.md，以及 docs/specs/project-agent-capabilities-design.md 第21.6节和第22节。编辑前执行git status --short、git branch --show-current，保护已有修改，在任务分支工作。你是本轮实施会话，可修改本范围业务代码。

背景：用户已确认旧靶场失效，替换地址仍是同一道题的新实例。原复测只完成前置检查，尚未执行。现有Agent复测工具只传来源漏洞／Observation／附件；CreateVerificationRequest尚无明确的本次目标地址字段。新会话还遇到上游API安全拦截，本轮不得重发、改写或通过其他通道绕过它，亦不以本地修改声称拦截已解决。

目标：历史来源与本次执行目标明确分开，并让实际消费者使用一致的本次目标。先沿Schema→Agent工具→创建服务→授权／出口构造→执行上下文→详情／报告定向检查；有现成能力就复用，没有才做最小完整补齐，不另造框架。

原项目、漏洞、Observation、历史报告和原始证据不变，不用字符串替换篡改历史。明确指定的新目标单独持久化；未指定则保持原目标复测语义。目标不能只存在于聊天或补充附件中。请求、执行范围和结果展示必须一致，旧地址不是本轮回退目标；不自动换协议、历史IP或其他域名，不扩大授权或复制目标专属凭据。复用原有授权机制，不新增审批平台。

只补必要的Agent参数、后端权威来源和直接消费者；详情／报告区分来源与本次目标，保留真实证据要求。不要重做UI、增加批量换址、预算系统或漏洞利用功能。

采用现有隔离PostgreSQL和模拟执行设施做定向验证：原目标路径、新目标贯通、历史不变、目标变化与现有幂等身份不冲突、执行输入不误用旧目标。不访问真实目标，不创建会被真实Dispatcher领取的任务，不安装依赖，不默认全量测试。

有运行改动按现有规范安全部署完整系统，先确认在途工作，不能强停用户任务。Worker只有应用变更才更新对应层，不重下载未变化工具层。受控测试和部署不等于真实复测验收。

回填同一方案，列明具体改动、受控验证、部署状态及真实复测仍未执行。报告当前分支、修改文件、测试结果、提交情况和剩余风险。不commit／push，完成本轮后停止。
```

### 22.4 本分析轮声明

分支 `feat/update-worker-cache`；仅更新原方案交接指向并追加第 22 节。读取当前 Schema 和最新实施记录，没有访问目标、调用模型、运行自动化测试、修改业务代码或部署。`rg` 在本轮 shell 不可用，改用专用 Read 工具完成所需核对，未安装依赖。无 commit／push。

### 22.5 本次目标绑定实施记录（2026-10-29）

**代码完成范围。** 本实施轮在 `feat/update-worker-cache` 上保护既有未提交修改，仅补齐本节能力；第 22.4 节仍是上一分析轮的事实，不以本节回写历史。

- `CreateVerificationRequest.target_url` 与 Agent `request_vulnerability_retest.target_url` 接受用户明确的 HTTP(S) URL；拒绝缺协议、URL 内嵌账号密码、空白、反斜线、片段和非法端口，保留合法原文，不自动改协议、域名或路径。
- 本次目标作为新复测项目 Origin JSON 的独立 `target_url` 字段持久化，不替换来源漏洞目标、Origin、Observation、旧报告或报文字节。已有 Origin v4 授权摘要绑定整个执行上下文，受理后改目标会触发 `project_authorization_scope_changed`；未增加数据库列、迁移或审批平台。历史快照中的来源授权不是本轮授权。
- 明确目标参与业务请求摘要和 Agent 动作参数比较，同一幂等身份改目标返回 409。未指定字段时仍沿用旧请求摘要和原目标语义；通过上一轮资料批次再次复测时，省略覆盖值会保留上一轮已绑定目标，不从历史快照误回退旧地址。
- 共享目标投影贯通详情、claim、Dispatcher 任务数据、delivery inputs 与 Worker `build_execute` 输入；执行 Goal／专用指引说明历史地址仅是来源，不回退历史 IP、不换协议或其他域名。既有 `test_egress` 仍是出口代理设置，不被当作测试目标或目标身份。
- 明确新目标时，不自动纳入来源 Origin／Goal 关联的环境资料及其附件，来源索引标明未复用；漏洞／Observation 的历史证据保留原文。**这不是清洗历史证据中的身份信息，也不授权把旧请求中的凭据用于新实例。** 用户显式复制上一轮资料仍保留原件，不自动改写。新目标所需身份不足时应报告限制。
- 表单增加可选目标输入；复制表单保留绑定值，未知受理重试锁定原输入。详情与确定性 Markdown 报告分别展示“历史来源”和“本次目标”，不添加目标外链。真实结论及实际测试范围仍由本轮结果证据决定。
- 现有授权机制是 Origin 范围摘要校验，**不是逐个网络请求的 host／scheme ACL**；本轮没有新增网络拦截器，不把流量 scope 或指引冒称网络强制隔离。

**本轮修改文件。** 下列仅列本轮涉及文件，不把工作区原有大量改动归到本轮：

- 后端：`cairn/src/cairn/server/schemas/verification_requests.py`；`cairn/src/cairn/server/services/agent_conversations.py`、`verification_requests.py`、`verification_sources.py`、`project_records.py`。
- 执行端：`cairn/src/cairn/dispatcher/tasks/external_verification.py`；`cairn/src/cairn/dispatcher/prompts/external-verification/agent.md`；`container/pi-agent/src/project-tools.ts`。
- 前端：`cairn/frontend/src/api/verificationRequests.ts`；`cairn/frontend/src/features/verifications/VerificationForm.vue`、`VerificationDetail.vue`。
- 测试：`cairn/tests/test_verification_target_binding.py`（新增）、`test_agent_business_actions.py`、`test_external_verification_receipts.py`；`cairn/frontend/src/features/verifications/VerificationBoundaries.component.test.ts`；`container/pi-agent/tests/chat-turn.test.mjs`。
- 本方案及 `cairn/src/cairn/server/static/` 正式生成产物。未新增依赖，未修改真实项目／任务的业务记录。

### 22.6 受控验证与明确失败项

所有业务创建和模拟 claim 均在一次性 PostgreSQL 内完成；测试目标只用 `*.example.test`，真实 Dispatcher 不连接测试数据库，不访问靶场、不调用模型。

| 验证 | 命令／覆盖 | 最终结果 |
| --- | --- | --- |
| 后端定向集成 | `PYTHONPATH=/usr/lib/python3/dist-packages:cairn/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 CAIRN_TEST_POSTGRES_BOOTSTRAP=1 CAIRN_TEST_LOCAL_PG=1 cairn/.venv/bin/python -m pytest -q cairn/tests/test_verification_target_binding.py cairn/tests/test_agent_business_actions.py::test_agent_business_actions_use_real_actor_sources_and_generation cairn/tests/test_external_verification_evidence.py::test_request_without_additional_origin_preserves_source_in_claim` | **12 passed**。覆盖原目标、新目标、授权摘要、请求／工具幂等冲突、实际模拟执行输入、详情／报告、再次复制不回退、来源记录与证据字节不变、非法地址。模拟结果为 `insufficient_evidence`，明确没有执行实际复测。 |
| 前端目标绑定定向 | `npm --prefix cairn/frontend run test:component -- src/features/verifications/VerificationBoundaries.component.test.ts -t '本次目标独立提交\|复制复测保留已绑定目标\|仅展示三段结果'` | **4 passed，36 skipped**。覆盖新地址提交、未知受理锁定／重试、复制保留目标、v3／v4 详情区分来源且不生成目标链接。 |
| 同一组件文件完整运行 | `npm --prefix cairn/frontend run test:component -- src/features/verifications/VerificationBoundaries.component.test.ts` | **33 passed，7 failed**，不能称全绿。失败集中在本轮未修改的 EvidenceReader 交互：两项仍断言“选择 Raw 手工复制”，实际文案是“选择当前文本手工复制”；四项 flow 投影／预览模拟与当前 readable 路径不匹配；一项补充材料测试期待两次预览调用、实际为零。本轮不扩展修复这些测试／阅读器路径。 |
| Worker 工具透传 | `node --test --test-name-pattern='R2a tools preserve action identity' container/pi-agent/tests/chat-turn.test.mjs` | **1 passed**，通过本地 Unix socket 验证无目标与显式目标参数及 tool-call identity；没有执行模型请求。 |
| 正式前端 | `npm --prefix cairn/frontend run build:server` | **通过**，包含 `vue-tsc` 类型检查和 Vite 正式构建；有构建性能提示，不是构建失败。 |
| Worker 应用 | `npm --prefix container/pi-agent run build`、`run self-check`，候选镜像及既有空闲聊天容器内 `cairn-agent --self-check` | **通过**，仅离线 SDK／CLI 检查，不代表真实 provider 已验证。 |

测试过程曾因新 fixture 使用手工漏洞却引用 capture 证据、按数字索引读取 PostgreSQL 行、传错批次服务签名失败，均修正测试后重跑通过，未放宽生产证据校验。新增组件测试首次把 fieldset 继承禁用误读为 input 自身 disabled，并漏点显式复制按钮，修正测试后定向通过。虚拟环境没有 pytest 可执行文件，复用系统已有 pytest 模块运行，未安装依赖。上述 7 项组件失败保留为剩余验证缺口，未默认运行全量测试。

### 22.7 部署生效与真实执行边界

**部署前核对。** 只读查询真实 PostgreSQL：active 项目、queued／running TaskRun、未终结复测、待领取 Agent 消息、可执行计划及待生成／重试报告均为 0。部分既有聊天历史 TaskRun 的 lifecycle 仍标记 `termination_confirmed=false`／`cleanup_confirmed=false`，没有伪造修复这些记录；同时检查唯一运行的聊天容器进程，只有 init／sleep，无 Agent 执行。保留既有容器、工作区和 session，不强停用户任务。

**完整加载。** 正式前端构建后发布至实际 `CAIRN_STATIC_DIR=runtime/static`，保留旧 hash 资源、最后替换入口；按既有 `deploy/stop.sh` 与 `deploy/start.sh --host 0.0.0.0 --port 8001` 加载 Server、Dispatcher，保留原 PostgreSQL。Worker 确有应用修改：原 Dockerfile 的禁网构建未命中基础 apt 层并失败，没有放开网络重装；改用既有镜像应用层更新方式，确认 package 元数据哈希一致，只追加已编译 dist。新镜像 `sha256:3680a1b080090331195a6732008466757c722af2cbf2e413e7eccd467adb4c63` 的 RootFS 以旧镜像全部层为完整前缀，工具／生产依赖层全部复用，未重新下载。正式 `arena-worker:local` 指向该镜像；既有空闲聊天容器仅更新应用 dist，执行位、离线 CLI 自检及 `project-tools.js` 哈希均核对通过，未删除或重建其挂载。

**实际检查。** `deploy/status.sh` 确认 Server／Dispatcher running、ownership tracked、HTTP ready、心跳 idle。正常登录本地 UI，打开新建复测对话框看图，确认新增字段可见；没有填写真实地址、没有提交或发送 Agent 消息，随后取消关闭。一次初始 readiness 提示执行器不可用，重新只读核对后恢复，进程状态与最终心跳正常。正常鉴权 GET 旧复测详情返回 200，已输出 `target_url:null` 和历史 `source_target`，旧结果仍 completed。正式首页与 runtime 入口字节相同、10 个入口静态资源均 HTTP 200。尝试读取 `/openapi.json` 返回 404，未据此声称在线 Schema 验证成功；改以正常详情接口和正式页面确认服务生效。

部署前后对 `projects`、`facts`、`vulnerabilities`、`vulnerability_observations`、`verification_requests`、`task_runs`、`agent_messages` 全行摘要比对一致；实际运行库未新增／改写复测或执行任务。新目标双地址展示与模拟结果报告由隔离测试验证，未为了生产页面演示造一条真实复测。

**结论与停止点。** 代码已接通、上述定向受控验证通过、完整系统已加载；同文件 7 项其他组件断言失败仍未处理。**真实复测仍未执行，未访问新旧靶场、未调用模型、未重发或改写被拦请求，也未通过其他通道绕过上游拦截。** 本地功能完成不等于拦截已解除、实例仍可用或漏洞成立；后续真实执行仍需合规可用渠道和明确范围。已审阅本轮相关差异与格式检查，无 commit／push；本轮到此停止。

## 23. 目标绑定复核：限定目标达到，收尾证据阅读缺口，不扩建功能

### 23.1 本轮复核结论与证据身份

**符合第 22 节“实现＋受控验证、不启动真实复测”的限定预期。** 定向静态核对涵盖目标持久化、claim／delivery／Worker 输入、复制继承、幂等和历史原件，未发现会直接阻断该目标的确定性缺口。主要路径：`verification_requests.py:679–704,734–738,776–791,876–885,942–948`；`project_records.py:338–359`；`dispatcher/tasks/external_verification.py:67–79,198–224`；`verification_sources.py:145–148,272–274,312–345`。这些是代码核对，不是本轮执行了测试或访问目标。

前端当前 `VerificationForm.vue:67–69` 已有可选本次目标，`VerificationDetail.vue:23–26` 区分历史来源与本次目标。定向测试 12／4／1 项通过、正式构建与部署加载沿用第 22.6–22.7 节实施记录，本轮未重跑。尝试通过既有浏览器正常 GET 旧复测详情时，MCP 调用超过 120 秒未返回，随后停止该工具任务；没有取得新的在线结果，不能据此认定服务故障，也不能声称本轮独立复验部署成功。

现有 Origin 授权摘要保护本次输入不被悄悄改写，不等于逐请求 host／scheme ACL；代码记录已明确这个边界，不新增网络隔离框架来追求不存在的本轮承诺。上游 API 拦截是否解除仍未知，不转发被拒请求到其他通道继续执行。

### 23.2 下一轮收尾范围：7 项失败的真实归因与只读页面体验

不继续重写目标绑定，也不重跑合成创建。当前直接相关的交付缺口是第 22.6 节同组件文件的 **7 项失败**；不能断言全是历史测试问题，也不能据此说新目标功能整体失败。

已核对到两类明确差异：测试仍定位“选择 Raw 手工复制”（`VerificationBoundaries.component.test.ts:503,606`），组件实际为“选择当前文本手工复制”（`EvidenceReader.vue:88`）；flow 测试仍以 projection／preview 为主要模拟，而当前 flow 默认 `readable`（`:215`），正文通过 `getTrafficReadable` 读取（`:509–514`），显式投影才进入另一接口（`:517`）。这解释了为什么应先查测试与当前交互合同是否一致，不证明所有 7 项均已归因。补充材料零预览调用等仍需核对实际点击与懒加载路径。

下一轮只处理该文件已失败的对应场景，保留完整失败输出，分别区分旧断言／模拟失配和真实用户缺陷。测试失配就修测试，不把产品退回旧默认；真实缺陷则最小修复相关消费者。保留原件／当前视图的复制语义、UTF-8 字节定位、切换后迟到响应隔离和补充材料可达性，不通过删断言或制造假 preview 把测试刷绿。

选择既有报告／证据，只读验证正文、显式投影／Hex、复制反馈及补充材料切换，按实际材料支持的模式操作；不从历史证据请求目标、不启动复测。若既有材料不足以覆盖某模式，使用现有本地 fixture，并明确不能代替实际页面。只运行受影响定向用例，不扩成全站阅读器重做或全量测试。测试-only 修改不触发构建部署；生产改动才按既有规范安全加载并核对。

完成后即可结束本地功能交付，不再为未覆盖的所有故障制造下一轮。未来有合规可用的模型渠道与明确实际操作范围时，再做一条真实同题新实例复测；这不是本轮授权动作，也不要求先做全平台回归或崩溃演练。

### 23.3 下一实施会话提示词

```text
本轮只做复测证据阅读交付收尾，不扩展目标绑定，不访问靶场、不调用模型、不创建复测。

先读 CLAUDE.md、AGENT.md、docs/ui-design-standard.md，以及 docs/specs/project-agent-capabilities-design.md 第22.5–22.7节和第23节。编辑前检查git status --short、git branch --show-current，保护原有修改，在任务分支工作。

同题新实例target_url已贯通且限定受控验证通过；真实复测未执行，上游API访问问题未证明解除。不重新做目标绑定，不重发或转用其他通道执行被拦请求。

只针对 VerificationBoundaries.component.test.ts 实施记录中的7项失败，取得完整失败输出并定位当前实际交互。已有线索：复制恢复文案从“选择 Raw 手工复制”变为“选择当前文本手工复制”；flow默认正文走getTrafficReadable，而旧测试仍主要模拟projection／preview；补充材料测试预期调用未发生。逐项判断旧断言、模拟失配或真实用户缺陷，不预设都是旧测试。

测试过时就更新模拟和有效断言，不能为了测试把产品退回旧交互；真实缺陷只改直接相关实现。保留当前视图与原件区别、UTF-8定位、异步切换归属、懒加载和附件可读性，不删有效验证或伪造成功数据。

用已有报告和证据只读检查正文、显式投影／Hex、复制反馈、补充材料切换；按等效最大化桌面实际操作与看图。缺少真实材料的模式用本地fixture并明确身份，不访问报文里的目标。不必重复已通过的Agent复制／SSE或发送模型消息。

只运行对应定向用例，不跑全量套件。若仅测试修改，不构建部署；有运行代码修改才按既有方式安全加载，不打断在途工作，不新增依赖或下载未变化Worker工具层。

更新原方案，逐项说明7项失败的原因、处理和验证身份。报告分支、文件、测试／页面结果、提交情况、剩余风险。无具体生产缺陷就不强造业务改动。不commit／push，完成后停止；真实复测留待上游访问问题和实际执行范围明确后另行验收。
```

### 23.4 本分析轮声明

分支 `feat/update-worker-cache`；仅更新原方案文首、交接指向和第 23 节。完成相关源码静态核对，浏览器旧详情读取未取得结果；没有运行测试、模型、构建、部署或目标访问。部署／测试成功身份仍为实施记录，不冒称本轮复验。无 commit／push。

### 23.5 证据阅读交付收尾实施记录（2026-10-30）

**范围与结论。** 本轮为用户授权的限定实施会话，不沿用第 23.4 节分析会话的只读代码角色。开始前已执行 `git status --short`、`git branch --show-current`，在既有任务分支 `feat/update-worker-cache` 上保护原有大量未提交改动。本轮仅修改 `cairn/frontend/src/features/verifications/VerificationBoundaries.component.test.ts` 与本方案；后者原已为未跟踪文件，未覆盖其历史记录。没有修改生产代码、目标绑定、依赖或业务数据。

#### 七项失败逐项归因

首次完整运行确实得到 **33 passed、7 failed**，与第 22.6 节数量一致。完整 stdout／stderr（含七项错误、断言位置及退出结果）保留在本机 `/tmp/arena-verification-boundaries-before-20261030.log`，不以摘要替代原始失败输出。下表测试名按首次运行记录，当前名称按新阅读合同作必要调整。

| 失败用例 | 本轮确认的根因与分类 | 最小处理及仍保留的验证 |
| --- | --- | --- |
| 复制 API 拒绝按侧反馈，手工恢复选中 Raw 原文 | **过时文案断言**：定位“选择 Raw 手工复制”失败；实际为“选择当前文本手工复制”。非 flow 的 Pretty 复制仍使用原文，恢复仍回到 Raw，并非产品退化。 | 仅更新按钮文字；保留 Clipboard 拒绝、同侧错误／另一侧无反馈、不显示成功、`selectAll`、重复 JSON 键／大整数原文及 Raw 选中断言。 |
| flow 使用共享投影并按绝对 UTF-8 字节分页和定位 | **默认正文接口模拟失配**：挂载已走 `getTrafficReadable`，旧测试未 mock 此接口，却期待立即调用 projection。 | 增加具备完整身份字段的 `TrafficReadable` fixture；先断言正文接口、正文内容及不调用投影，再显式点击“投影”。补充正文视图／v1 投影片段各自的复制内容和反馈断言；保留原件入口、历史来源、缺侧、65536 字节分页及中文六字节定位成两个字符。 |
| flow 选段拒绝摘要变化和二进制占位 | **接口模拟失配＋过时提示断言**：未 mock readable 导致相对 URL 解析错误；旧测试还期待默认进入投影及旧占位提示。 | readable 明确模拟 `non-text`，显式切投影后断言当前“二进制正文 · 投影仅含头部与占位”；仍验证不能引用二进制占位、不能选中错误范围、摘要变化必须报错且不展示替代内容，原件下载入口保留。 |
| flow 尚未终结不读投影，切换后迟到投影不覆盖历史 Artifact | **懒加载交互及默认正文行为失配**：receiving 只先显示头部，不自动调用 preview；终态先回正文；大小未知的历史 Artifact 也不会预取。原测试在任何显式加载前就期待正文前缀。 | 先断言不预取，再点击“加载请求正文”；终态确认 readable 后显式切投影制造真实待决请求，切换后确认其 AbortSignal 已取消，再手动加载旧 Artifact；迟到投影仍不能覆盖当前原件。 |
| flow 接收进度不变不重复取正文，终态及 Hex | **懒加载／默认正文失配＋说明位置变化**：receiving 初始不调用 preview，终态走 readable；Hex 的正文原字节说明位于“更多”菜单，而非常驻正文。 | 显式加载接收前缀后验证未变化刷新不重读；终态只调用 readable 而非 projection；保留 Hex 实际字节／偏移、API 参数、复制标签、无 Pretty 及选段切回投影定位。打开真实菜单再验证 Hex 限制说明，不删除该断言。 |
| 复制失败浮层 ArrowDown／Escape 焦点恢复 | **过时文案断言**：恢复按钮名称改变。 | 仅更新文字；保留 Portal 归属、稳定 `aria-controls`、ArrowDown 进入恢复、Escape 返回同侧按钮及关闭后节点释放。 |
| 单条主证据从两条补充材料返回，迟到预览隔离 | **未知大小附件的懒加载合同失配，不是预览丢失**：测试直接 mount 时没有提供 `artifactSize`，`loadPanel(..., automatic=true)` 按当前规则延期，故预期 2 次实际 0 次。实际详情调用方会从 artifacts 提供大小；未知大小仍须手动读取。 | 不编造大小来强行自动加载：先断言 0 次和“已保存大小未知”，逐侧点击加载后才断言 2 次；日志、环境附件各在显式加载后读取，切换时取消日志请求，迟到日志不得覆盖环境内容；返回主证据再次显式读取，严格核对 2→3→4→6 次及两侧原件 URL。 |

依据为当前 `EvidenceReader.vue` 的默认模式、自动读取条件、readable／preview／projection 分流、异步身份检查和复制恢复实现，以及 `VerificationDetail.vue` → `EvidenceItemDisclosure.vue` 的附件大小透传。**在这七项场景中未确认需要修生产代码的证据阅读、复制或附件切换缺陷**；不是先假定七项全是旧测试，也不将有限验证外推为所有阅读场景均无缺陷。

#### 实际测试与输出

| 命令 | 真实结果与身份 |
| --- | --- |
| `npm --prefix cairn/frontend run test:component -- src/features/verifications/VerificationBoundaries.component.test.ts` | 修改前 **33 passed、7 failed，退出 1**。按当前合同调整后 **40 passed，退出 0**；最后补齐二进制 readable fixture 后再次完整运行仍 **40 passed，退出 0**。最终完整输出 `/tmp/arena-verification-boundaries-final-20261030.log`，中间通过记录 `/tmp/arena-verification-boundaries-after-20261030.log`。没有删除测试、跳过原失败项或改生产行为迎合测试。 |
| `npm --prefix cairn/frontend run test:component -- src/components/patterns/EvidenceReader.component.test.ts -t '小gzip默认可读\|大正文默认只有头部\|小原件自动探测膨胀\|未知或大Artifact不预取\|异常字节保留有效正文'` | **5 passed、17 skipped，退出 0**；选取已有用例核对本次调整所依赖的正文／投影、解压、未知大小懒加载、取消迟到响应及转义复制合同。17 项仅因 `-t` 未选中，不称该文件全量通过。完整输出 `/tmp/arena-evidence-reader-targeted-20261030.log`。 |

均为本地受控组件测试，没有访问靶场、调用模型或创建真实复测；未扩跑全站测试、lint、类型检查或构建。

#### 已运行实例的只读页面验收

正常访问已运行的 `http://127.0.0.1:8001`（HTTP 200），按既有本地登录授权正常登录，只打开列表中已有的 completed／v4 复测报告。正常鉴权详情 GET 返回 200，未为演示新增、重跑、修改报告或替换其证据响应。

- **桌面条件与看图：** 外窗 `1914×879`，与屏幕可用区域相等；按等效最大化桌面验收，不冒称另行核验了窗口管理器最大化标志。执行 `Ctrl+0`，最终 CSS 视口 `1914×740`、`devicePixelRatio=1`、`visualViewport.scale=1`；首次正文截图为 `1906×732`，其余为最终视口，未增加窄窗矩阵。实际打开并查看本机截图 `/tmp/arena-evidence-readable-20261030.png`、`/tmp/arena-evidence-copy-failure-20261030.png`、`/tmp/arena-evidence-projection-hex-20261030.png`、`/tmp/arena-evidence-supporting-20261030.png`、`/tmp/arena-evidence-excerpt-20261030.png`。完整窗口保留导航和报告上下文；成对报文、模式、复制浮层、分页及选段可见。截图仅作本机临时验收材料，不加入仓库。
- **懒挂与正文：** 报告八条交互初始全部折叠、阅读器数为 0；展开既有交互后仅挂载所选阅读器，请求／响应默认“正文”，实际请求本地 `/readable/request`、`/readable/response`，两侧编辑器出现且无读取错误。证据只作文本显示，没有请求报文中的目标。
- **显式投影／Hex：** 响应切“投影”后实际读取 projection，标签为“复制 v1 投影”，UTF-8 范围为 `0–1764 / 1764`；再切 Hex，显示真实正文偏移与十六进制／ASCII，范围 `0–1123 / 已同步 1123 字节`，复制标签为“复制当前 Hex 展示”。请求投影同步可读；没有把解码正文、v1 投影、正文原字节当成同一原件。
- **复制反馈：** 正常 Clipboard API 点击后出现“已复制正文视图”。另尝试独立 `clipboard.readText()` 回读时工具超过 120 秒，已停止该工具任务，**不声称剪贴板实际回读比对通过**。失败分支采用页面内单次 `NotAllowedError` 受控注入，点击后立即恢复原 Clipboard API；真实页面显示拒绝原因而非假成功，“选择当前文本手工复制”可用。恢复后焦点回到响应编辑器，出现文本选中区域，模式仍为“正文”，没有切成投影。该失败是浏览器 API 受控验收，不冒称现场真实权限曾被拒绝。
- **主证据／补充交互：** 当前报告结果含三条 primary 与五条 supporting，均为 HTTP flow。实际从补充基线展开、收起，进入主证据，再切另一补充交互，最终返回主证据；每次仅保留当前展开阅读器、相应请求／响应均出现且无错误。点击补充交互已保存选段后切回 v1 投影，范围 `600–1725 / 1725`，正文文本被选中，无范围／摘要错误。报告没有独立文本／artifact 类型补充 evidence item，故“两份非 HTTP 附件切换及迟到响应”只由上述本地 fixture 验证，未借输入附件或新造数据冒充真实页面覆盖。
- **其他未验证项：** 页面工具的键盘／合成滚轮尝试未确认编辑器内滚动，不据此宣称完整键盘滚动验收通过或确定存在生产缺陷；实际看图覆盖已载入区和选段。未制造服务断线、真实剪贴板权限拒绝、全部编码／大附件或跨浏览器故障。没有为这些未覆盖条件制造下一实施轮次。

浏览器已加载资源检查中没有异源 HTTP(S) 请求；没有点击“开始复测”、启动项目、发送 Agent 消息、调用模型或访问新旧靶场。上游 API 拦截是否解除仍未知，没有重发、改写、拆分或切换通道绕过。

**部署与停止点。** 本轮为测试＋文档变更，**没有构建、部署或重启**，没有中断任务、重建 Worker 或下载工具层。已审阅相关 `git diff` 并检查格式差异；无 commit／push。本轮限定的本地证据阅读交付到此结束；真实复测在合规可用渠道与实际操作范围明确后另行验收，不将未覆盖故障自动升级为新的实施任务。
