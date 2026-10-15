# Cairn 协作探索协议

---

## 本质

衍迹（Cairn）将目标导向的探索过程建模为一张有向无环图：从已知事实出发，不断探索产出新事实，直到某些事实共同满足终点。名称取自登山者用石头垒起的路标——每个 Fact 是一块石头，Agent 沿着前人的 cairn 继续探索，同时垒新的 cairn 给后来者。

这套 API 管理这张图的生长过程。多个消费者（人或 Agent）并发读取完整图，各自声明探索意图，各自产出新事实，最终由某个消费者判断终点已达成。对于一些很简单的问题，消费者也可以在项目最初阶段直接围绕起点和终点做一次快速推进，而不必先拆成多轮显式规划；这种“起步即做”的过程仍通过 Intent/Fact 正常落图。

图仍然是知识与因果关系的唯一表达；但协议也允许少量**项目级协调状态**，用于消费者之间做互斥和可见性协作。这类状态不是事实，不属于图，不参与因果推理。当前唯一内置的项目级协调状态是 `Project.reason`，用于标识“某个消费者正在对整个项目做一次 `reason` 判断”。

系统不做任何推理和决策，只负责图的一致性维护。

### 设计渊源

这套协议是**黑板架构**（Blackboard Architecture）的现代化重构。1970 年代 CMU 的 Hearsay-II 系统提出了同样的范式：多个专家围绕一块共享黑板工作，各自读取当前状态，各自贡献新知识，没有中央调度。Fact 对应黑板上的内容，Intent 对应专家的行动记录，Hint 对应旁注。区别在于：经典黑板只有"当前状态"，本协议通过 Intent 保留了完整的因果链，使得图同时是知识库和推理路径的审计日志。

协调机制上，这套设计等价于蚁群的**信息素机制**（stigmergy）——个体之间不直接通信，而是通过改变共享环境间接协调。Agent 读图、写 Fact，就是在环境上留下信息素，其他 Agent 据此决策。

每个消费者的工作循环（读图→判断→声明意图→执行→产出事实）构成一个独立的 **OODA 循环**（Observe-Orient-Decide-Act），多个循环通过共享图间接同步。这与军事指挥中的**任务式指挥**（Mission Command）同构——上级只下达目标（goal），各单位根据态势自主决策，Hint 扮演"指挥官意图"的角色。核心优势：决策速度不受中心调度瓶颈限制。

### 状态变化的表达

Fact 只增不改，状态变化通过追加新 Fact 表达。例如在多层网络渗透中：

- `f003`: "获得 host-A 的 root shell"
- `f007`: "通过 host-A 跳板发现内网 10.0.2.0/24"
- `f025`: "host-A shell 已断开"

"shell 断了"本身就是一个新的客观事实。Agent 读图时看到 f003 和 f025，自行判断当前可达性，从 f025 出发声明新的探索意图（重建 shell、换跳板）。不需要修改历史 Fact，不需要标记失效——事实的时序本身就携带了状态变化的信息。

---

## 核心概念

### Project

一个有明确起点和终点的问题实例，包含完整的图数据。

项目状态：

- `pending`：单次预约已保存但尚未启动；原始输入与资料已属于项目，不接受探索写操作，不能用普通状态切换或 `resume` 绕过计划激活
- `active`：进行中，接受探索写操作
- `paused`：暂停研究，保留项目和已有成果；恢复仍走项目自身的授权与执行准入
- `stopped`：硬停止，不接受探索写操作，可恢复为 `active`
- `completed`：当前已完成，不接受探索写操作；若外部确认之前的完成判断有误，可显式调用 `reopen` 撤销完成态并继续

计划状态独立于项目状态，参见下方 `Project schedules`。暂停或停用周期计划只控制以后轮次，不等于暂停、停止或删除已经生成的项目。

### Fact

图中的节点，代表一个已确认的客观事实。只有描述文本，没有状态标记。只增不改，永久保留。

描述文本应是探索结论的提炼，而非原始数据。当原始输出较大时（如扫描结果），描述应包含关键洞见和原始数据的文件引用，例如："nmap 全端口扫描发现 22/80/443/8080 开放，80 和 8080 均为 HTTP 服务，详细结果见 /tmp/scans/nmap_192.168.1.10.xml"。这保证图保持轻量，同时原始数据仍可追溯。

每个 Project 有两个特殊 Fact，在 Project 创建时写入：

- `origin`：起点
- `goal`：终点

普通 Fact 由系统生成 id，如 `f001`。

### Intent

图中的边，代表从一个或多个 Fact 出发的探索过程。有三种状态：

- **进行中**：Intent 已被某个消费者认领，`worker` 不为 null
- **未认领**：Intent 尚无结论，且当前 `worker` 为 null，等待消费者接手
- **已结论**：`to` 不为 null，探索已完成，产出了结论 Fact

未结论的 Intent（`to=null`）表示这个探索方向已声明但尚未有结论。声明时可只写下方向而不认领，也可同时认领。已结论的 Intent 不可修改。

协议本身不区分 Intent 的“类型”，所有 Intent 在服务端都一视同仁；但消费者可以约定某些**保留语义**。例如可约定一条：

- `from = ["origin"]`
- `description = "bootstrap"`

的 open Intent 表示“项目最初阶段的一次直接推进尝试”。这类保留 intent 仍然走同样的 heartbeat / release / conclude 生命周期，不需要额外接口；区别只在于消费者会把它解释为“先自己分析并执行，先把关键证明性事实落图；若该事实已足以满足 goal，再继续声明 complete”，而不是普通的细分探索方向。

这个约定适合那些可能很快完成、或至少很快得到首个关键突破的问题。这样项目在前端和审计记录中仍然是可见、可理解的：读者能明确看到系统先做过一次 `bootstrap` 尝试，产出了什么关键事实；像 flag、shell、权限证明这类对后续系统有价值的结果，不会因为直接完成项目而绕开事实图。

**超边语义：** `from` 允许多个 Fact id。`from` 包含多个 id 时，这条 Intent 在图中等价于一条超边——多个源节点共同指向同一个目标节点，共享同一个 description、creator 和 worker：

```
f002 ──┐
       ├──(intent)──→ f006
f004 ──┘
```

这完整保留了"多个已知事实共同支撑一次探索"的因果关系，而不是被迫选一个主 Fact 丢失其余上下文。

### Hint

图外输入，策略建议或补充说明。不属于事实图，不影响因果关系，供消费者读图时参考。

Hint 代表外部补充的高纬输入，不属于探索执行本身。因此项目处于 `stopped` 或 `completed` 时，仍允许继续补充 Hint。`stopped` 项目恢复为 `active` 后，后续消费者会在下一次读图时看到这些新增提示；`completed` 项目上的新 Hint 会被保留，若之后需要继续处理该项目，仍应通过 `reopen` 回到 `active`。

除了人工补充的策略提示，Hint 也适合用于消费者之间传递**态势评估**。随着图增长，大量已排除的方向会稀释有效信息。消费者在完成一轮探索后，可将对当前局势的判断写为 Hint（如"SSH 和 SQL 注入方向均已排除，攻击面集中在文件上传功能"），帮助后续消费者快速定位重点，降低每轮读图的认知成本。

---

## 数据结构

### Project

```
id
title           # 项目名称
status          # "pending" | "active" | "paused" | "stopped" | "completed"
schedule_id     # 关联启动计划；普通项目为 null
bootstrap_enabled  # 是否允许消费者在初始态运行 bootstrap，默认 true
created_at
reason          # 当前项目级 reason lease，null 表示当前无人执行 reason
```

### Project.reason

```
worker             # 当前执行 reason 的消费者
trigger            # 本次 reason 的触发原因，由消费者定义
started_at         # 本次 reason 开始时间
last_heartbeat_at  # 最近一次 reason 心跳时间
```

### Fact

```
id              # "origin" | "goal" | 系统生成（如 f001）
description     # 客观事实描述
```

### Intent

```
id
from            # 出发 Fact 的 id 数组，至少一个
to              # 结论 Fact 的 id，null 表示尚无结论
description     # 意图描述
creator         # 声明意图的消费者，创建时写入，不可变
worker          # 执行者标识，语义随状态变化（见下方说明）
last_heartbeat_at  # 最后一次心跳时间，仅尚无结论时有意义，永久保留，不清空
created_at
concluded_at    # 结论时间，null 表示尚无结论
```

`creator` 在创建时写入，标识谁洞察局势提出了这个探索方向，不可变。`worker` 可为空；若创建时同时认领，则 `worker` 必须等于 `creator`，并写入首个 `last_heartbeat_at`。

请求体里的文本字段（如 `title`、`content`、`description`、`creator`、`worker`、`trigger`）以及 `from` 中的 Fact id，服务端都会先做首尾空白裁剪；裁剪后若为空，则返回 `422`。

如果消费者采用 `bootstrap` 约定，常见做法是使用固定 `creator`，例如 `dispatcher.bootstrap`，以便在图上与普通探索 intent 清晰区分；这只是消费者约定，不是服务端约束。

**worker 字段语义：**

| Intent 状态           | worker 含义                      |
| --------------------- | -------------------------------- |
| 尚无结论，worker=null | 当前无人处理，等待认领           |
| 尚无结论，worker 有值 | 有消费者正在处理                 |
| 尚无结论，worker 超时 | worker 清空为 null，等待重新认领 |
| 已结论                | 产出结论的消费者，永久保留       |

超时清空 worker 只针对尚无结论的 Intent。已结论的 Intent 不参与超时逻辑，worker 永久保留为产出结论的消费者标识。

### Hint

```
id
content
creator         # 提示的作者
created_at
```

### Settings

```
intent_timeout  # 单位秒，超过此时间无心跳则 worker 清空为 null
reason_timeout  # 单位秒，超过此时间无心跳则 Project.reason 清空为 null
```

---

## 接口列表

### 全局设置

#### GET /settings

返回全局配置。

响应：

```json
{
  "intent_timeout": 5,
  "reason_timeout": 5
}
```

---

#### PUT /settings

更新全局配置。

Body：

```json
{
  "intent_timeout": 5,
  "reason_timeout": 5
}
```

---

### Tenant Worker 模板

#### GET /worker-templates

返回当前认证 Tenant 的模板列表。每项包含 `id`、`name`、`owner`、有序 `tenant_worker_ids`、历史只读 `worker_names`、`is_default`、`created_at`。`owner` 仅作创建者归因，不参与访问控制；其他 Tenant 的模板不可访问。同 Tenant 至多一个 `is_default: true`，允许没有默认；旧模板迁移为 `false`，不按名称或顺序猜测。

#### POST /worker-templates
#### PUT /worker-templates/{template_id}

创建提供 `name`、完整 `tenant_worker_ids`，可选 `is_default`（默认 `false`）；更新可提供 `name`、`tenant_worker_ids`、`is_default`，省略或 `null` 的更新字段保持原值。创建返回 `201` 和模板，更新返回模板。

设置或修改默认模板时，成员须同 Tenant 且配置可用，`task_types` 并集覆盖 `bootstrap`、`reason`、`explore`；缺角色返回 `422`，一个多角色 Worker 也可合法。普通非默认模板不强制三角色。`is_default: true` 在同一事务取消本 Tenant 原默认并设置当前模板；Tenant 写锁与部分唯一索引保证切换一致，失败全部回滚。`is_default: false` 可明确取消默认，不选替代项。可用性不以临时容量或执行器在线性判断，不在线探测模型。

模板只保存 Worker ID，不复制模型、角色、并发或 revision。新建弹窗执行设置中的“保存为默认组合”复用上述接口：选中模板时设该模板为默认；手工组合更新已有默认，无默认时创建。普通勾选／提交项目不会隐式保存默认。保存默认本身不创建项目或计划、不启动执行；只影响以后新建时的首次初始化，不改当前草稿、已存项目绑定、计划输入或未知创建请求。

#### DELETE /worker-templates/{template_id}

删除本 Tenant 模板，成功返回 `204`；删除默认不会自动提升其他模板。已存项目／计划仍使用自身显式绑定；已打开草稿保留原 IDs，页面具体提示所选模板已删除，须主动选择本次自定义或恢复默认。

### Projects

#### GET /projects

返回所有项目列表，含元信息和统计摘要，不含图数据。

响应：

```json
[
  {
    "id": "proj_001",
    "title": "xx渗透测试",
    "status": "active",
    "bootstrap_enabled": true,
    "created_at": "2026-03-21T10:00:00Z",
    "reason": {
      "worker": "dispatcher-worker-A",
      "trigger": "new_facts",
      "started_at": "2026-03-21T10:06:00Z",
      "last_heartbeat_at": "2026-03-21T10:06:03Z"
    },
    "fact_count": 8,
    "intent_count": 5,
    "working_intent_count": 2,
    "unclaimed_intent_count": 1,
    "hint_count": 3
  }
]
```

---

#### POST /projects

创建新项目。`origin` 和 `goal` 写入 `facts` 作为特殊 Fact，`hints` 可选。必须显式提供同 Tenant 的项目组 `group_id` 和完整 `tenant_worker_ids`；所选 Worker 均须配置可用，其 `task_types` 并集须覆盖 `bootstrap`、`reason`、`explore`，缺少角色返回 `422`。一个 Worker 可以支持多个角色，不要求恰好三个 Worker；临时满载或执行器离线不等于配置失效，创建不要求手工 connectivity 验证。新建页面可以从默认模板初始化，但本接口不读取默认模板，也不回退到首项或 Tenant 全池。当前创建请求拒绝额外字段（422），旧客户端不得继续发送 `explore_budget`、`exploration_limit` 或 `verification_reserve` 等已移除额度参数，也不再使用旧的 `preferred_workers`／`bootstrap_enabled` 创建参数。

当前目标驱动执行没有项目级固定 Explore 次数上限或复核预留；discovery 与 verifier 都保留正常准入、证据接受和必要实质复核。`explore_deadline` 是唯一可选项目执行停止时间，省略或 `null` 表示不限期，非空值必须是含时区的未来绝对时间。到期停止新研究并按已有路径有序停止和收口，不代表 Goal 达成，也不只是停止新增 Explore。当前没有运行中编辑或自动延期 API，重新授权不延长这个执行停止时间；尚未触发的单次 `pending` 项目可通过计划编辑显式修改或取消该期限，见 `PUT /project-schedules/{schedule_id}`。

`authorization_expires_at` 在 API 请求中必须显式携带：未来绝对时间或 `null`，不能省略该键。创建页的“授权到期时间”可选、默认留空且可直接编辑，无需额外勾选无限期确认即可创建；留空表示不设到期时间，payload 明确发送 `authorization_expires_at: null`。如实际授权有期限，请填写；非空值沿用本地时间到含时区绝对时间的转换和未来时间校验，非法输入不能静默转为 `null`，清空后恢复提交 `null`。这是请求字段的显式性，不是要求使用者逐次手动确认，也不改变已有项目或授权 grant。授权到期时间与 `explore_deadline` 独立，设置执行停止时间不会生成授权期限，二者不能互相推导。不限项目执行期限不取消授权范围、用户停止、单 Run timeout、有界 retry／补交／截断续行、并发及静止／物理停止确认。累计 Attempt、Run、证据和历史记录继续保留；旧预算字段仅作为历史材料留在数据库或冻结原件中，不再控制当前运行，也不会自动恢复旧停止项目。

可选 `request_id` 是 UUID，用于创建结果未知时查询和安全重试；省略时不提供请求去重保证。相同 Tenant、用户、操作和请求 ID 的相同内容返回已经创建的项目，不再次消费资料批次；改变内容返回 `409`。请求身份及业务结果同事务保存，查询协议见 `GET /creation-requests/{request_id}`。页面以单一 `crypto.getRandomValues` UUID 实现支持普通 HTTP 入口；随机源等请求前本地准备失败显示“请求尚未发出”，不记为未知。未知恢复保留内存中的原请求键和原载荷。

新建立即／单次／每日／每周统一以 Origin 表达研究对象、已知资料、授权动作、范围和排除项，Goal 表达期望成果；Origin 不要求固定 URL，也不需要另填或确认 targets。`origin`／`goal` 校验非空但保留原文（含前后空白），新建页面省略 `targets`。API 的 `targets` 缺省或 `null` 选择 Origin 来源，不再从正文正则抽取地址写为 seed；显式数组（包括 `[]`）仍选择原有 Target 来源，不与 Origin 合并。

Origin 来源的不可变 grant 使用 scope descriptor v4：`version: 4`、`project_id`、`tenant_id`、`scope_source: "origin"`、`origin_fact_id: "origin"`、`origin_sha256`（已存 Fact 原始 UTF-8 描述的 SHA-256）。正文原件仍只由 Origin Fact 持有；授权投影重读该 Fact，并校验来源身份、摘要和范围 digest。项目摘要／详情新增只读 `authorization_scope_descriptor`，透出当前 epoch grant 原 descriptor，不由当前 Target 数量推测来源。已有 grant／项目不批量改写，显式 Target 来源继续按原 v1／v2／v3 及 seed／approved 集合校验；旧空数组不重新解释为 Origin 授权。

Bootstrap、Reason、Explore（含复核及收口）直接消费同一 Origin 原文和授权来源。Origin 已明确授予的对象／动作可自主研究；参考／排除地址、附件、重定向、历史发现和模型输出不扩权。无地址的资料任务可开始，结构化空列表不等于任意联网许可；仅对范围不清的拟进行外部动作提出具体缺失信息，不增加逐步审批。此合同不是 OS 网络硬隔离。Prompt 必须含 `{authorization_scope}` 和 `{origin}`，Target 索引按来源解释；受管自定义 Prompt 不被自动覆盖，缺少必要上下文时需明确更新后才能使用。模板占位符只替换一次，不二次改写 Origin 内的同名片段。

`resource_batch_id` 消费当前用户、当前 Tenant 的一个临时资料批次；非空 `resources` 与批次不能同时提交。`library_keys` 在创建时固定到不可变资料库版本。定时和周期创建另走 `POST /project-schedules`，不能向本接口发送计划时间字段。

Body：

```json
{
  "title": "xx渗透测试",
  "origin": "目标 http://192.168.1.10",
  "goal": "拿到 flag",
  "group_id": "group_001",
  "tenant_worker_ids": ["worker_001"],
  "explore_deadline": null,
  "authorization_expires_at": null,
  "hints": [
    { "content": "优先看 web 服务" },
    { "content": "注意 80 端口" }
  ]
}
```

---

#### GET /projects/{project_id}

返回项目完整数据，包含 facts、intents、hints。

响应：

```json
{
  "project": {
    "id": "proj_001",
    "title": "xx渗透测试",
    "status": "active",
    "bootstrap_enabled": true,
    "created_at": "2026-03-21T10:00:00Z",
    "reason": {
      "worker": "dispatcher-worker-A",
      "trigger": "new_facts",
      "started_at": "2026-03-21T10:06:00Z",
      "last_heartbeat_at": "2026-03-21T10:06:03Z"
    }
  },
  "facts": [
    { "id": "origin", "description": "目标 http://192.168.1.10" },
    { "id": "goal",   "description": "拿到 flag" },
    { "id": "f001",   "description": "发现开放端口 22/80/443" }
  ],
  "intents": [
    {
      "id": "i001",
      "from": ["origin"],
      "to": "f001",
      "description": "nmap 全端口扫描",
      "creator": "agent-A",
      "worker": "agent-A",
      "last_heartbeat_at": "2026-03-21T10:02:00Z",
      "created_at": "2026-03-21T10:01:00Z",
      "concluded_at": "2026-03-21T10:02:00Z"
    },
    {
      "id": "i002",
      "from": ["f001"],
      "to": null,
      "description": "http 服务探测与目录扫描",
      "creator": "agent-A",
      "worker": "agent-B",
      "last_heartbeat_at": "2026-03-21T10:04:00Z",
      "created_at": "2026-03-21T10:03:00Z",
      "concluded_at": null
    }
  ],
  "hints": [
    { "id": "h001", "content": "优先看 web 服务", "creator": "human", "created_at": "2026-03-21T10:00:00Z" },
    { "id": "h002", "content": "注意 80 端口", "creator": "human", "created_at": "2026-03-21T10:00:00Z" }
  ]
}
```

---

#### DELETE /projects/{project_id}

删除项目及其所有数据。

如果某个 Dispatcher 仍持有该项目对应的运行容器，容器不由 Server 直接管理；Dispatcher 在后续轮询中发现项目已不存在后，会把该项目视为 `deleted`，取消本地仍在运行的任务，并删除对应的 orphan 容器。

---

#### PUT /projects/{project_id}/title

修改项目标题。该操作不改变项目图，也不影响 `reason` lease、open intent claim 或项目状态；无论项目当前是 `active`、`stopped` 还是 `completed`，都允许修改标题。

Body：

```json
{
  "title": "xx渗透测试（复盘）"
}
```

响应：

```json
{
  "id": "proj_001",
  "title": "xx渗透测试（复盘）",
  "status": "completed",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": null
}
```

---

#### PUT /projects/{project_id}/status

更新项目状态。仅允许 `active` 和 `stopped` 之间切换。`completed` 项目不可通过该接口再次变更状态；若需要撤销完成态，必须调用专用的 `reopen` 接口。该接口属于项目管理操作，不属于探索写操作。

当项目切到 `stopped` 时，Server 会立即把所有尚无结论的 Intent 的 `worker` 清空为 `null`，并把 `project.reason` 清空，使这些 claim 立刻失效。这样项目恢复后可以马上重新认领，不必等待超时。`stopped` 的语义是硬停止：Server 负责拒绝后续探索写操作并清空 open intent claim / reason lease；消费者拿到这个信号后应立刻取消本地仍在运行的任务，并停止对应项目容器。

已知问题：当前协议里，Intent 级只保存“当前 claim 持有者”这一份信息，也就是 `intent.worker`。因此项目一旦被切到 `stopped`，这些 open intent 的 `worker` 会被立即清空；停止后从项目详情里将无法直接看出“该 intent 在停止前最后是由哪个 worker 在推进”。后续可以考虑增加类似 `worker_history` 的 Intent 级历史字段来保留这部分可见性，但当前版本尚未实现。

Body：

```json
{
  "status": "stopped"
}
```

响应：

```json
{
  "id": "proj_001",
  "title": "xx渗透测试",
  "status": "stopped",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": null
}
```

---

#### POST /projects/{project_id}/reason/claim

认领项目级 `reason` lease。它不属于图，不产生 Intent/Fact，只表示“当前有人正在对整个项目做一次 `reason` 判断”。单个项目同一时刻最多只能有一个 `reason` lease。

若当前 `project.reason` 为空，则 claim 成功并写入 `worker`、`trigger`、`started_at`、`last_heartbeat_at`。若当前已被其他消费者占用且未超时，返回 409。若当前已被同一 `worker` 占用，则幂等返回当前状态。该接口属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "worker": "dispatcher-worker-A",
  "trigger": "new_facts"
}
```

响应：

```json
{
  "id": "proj_001",
  "title": "xx渗透测试",
  "status": "active",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": {
    "worker": "dispatcher-worker-A",
    "trigger": "new_facts",
    "started_at": "2026-03-21T10:06:00Z",
    "last_heartbeat_at": "2026-03-21T10:06:00Z"
  }
}
```

---

#### POST /projects/{project_id}/reason/heartbeat

当前 `worker` 对项目级 `reason` lease 续约，更新 `last_heartbeat_at`。仅当前持有该 lease 的 `worker` 可以续约；若当前无人持有，返回 409；若被其他消费者占用且未超时，返回 409。该接口属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "worker": "dispatcher-worker-A"
}
```

---

#### POST /projects/{project_id}/reason/release

当前 `worker` 主动释放项目级 `reason` lease，使其立刻回到无人执行 `reason` 的状态。仅当前持有者本人可以释放；若当前已为空，则幂等返回当前状态；若被其他消费者占用且未超时，返回 409。该接口属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "worker": "dispatcher-worker-A"
}
```

---

### Project schedules

启动计划管理接口使用用户会话，写请求沿用 Cookie／CSRF 保护；普通用户只访问所属 Tenant 的计划。保存与未来输入编辑复用项目组、场景、Worker 和资料批次的归属校验，不能以计划绕过这些约束。尚未生成项目的周期输入同样持有场景引用，普通配置目录不能删除被计划引用的场景。到期消费只允许 Dispatcher 机器身份。

#### 时间、输入与状态契约

| 字段 | 语义 |
| --- | --- |
| `mode` | `once` 单次预约，`daily` 每日，`weekly` 每周 |
| `timezone` | 有效的 IANA 时区，预览与消费使用同一规则 |
| `start_at` | 含时区的绝对时间；创建时必须在未来。周期中是首次允许启动的边界，不替代 `local_time` |
| `local_time` | 周期必填的本地 `HH:MM`；单次省略或为 `null` |
| `weekdays` | 每周必填，`0` 为周一、`6` 为周日；每日和单次使用空数组 |
| `end_date` | 周期可选的本地结束日期，包含整日；单次不设置 |
| `max_runs` | 周期可选的正整数，限制实际生成项目的总次数；跳过与错过不计入生成次数；单次不设置 |

单次 `start_at` 必须带所选时区该本地时刻的有效偏移；夏令时不存在的本地时刻被拒绝，重复时刻由调用方用偏移明确选择。周期遇到不存在的本地时刻跳过，重复时刻固定取较早一次，预览以 `note` 提示。未来启动时刻必须早于计划结束、项目授权到期和执行停止时间中最早的有效边界；授权期限和执行停止时间互不推导。

计划的 `project` 复用 `CreateProjectRequest` 及相同来源语义：`targets` 可省略／为 `null`，采用 Origin 来源；显式数组仍采用原 Target 来源。单次 pending 项目固定 Origin Fact 和 grant；周期 `project_json` 保存原始 Origin／Goal 和来源选择，每轮只复制该输入，不从资料、发现或正文重新抽取授权地址。新 Origin 来源不因结构化 targets 为空、Origin 无网络地址或没有硬隔离证明而统一受阻；旧显式 `targets: []` 的计划仍按原空集合受阻，不自动转换或放宽。嵌套 `project.request_id` 不得为非空值，否则返回 `422`；创建身份放在计划顶层。

计划状态为 `active`、`paused`、`blocked`、`cancelled`、`disabled`、`completed`。这里的 `active` 表示计划等待到期消费，不表示已经有运行项目。单次预约取消不删除其 `pending` 项目；周期暂停／停用不修改已生成项目，也不删除计划原件或历史资料。

#### POST /project-schedules/preview

Body 为上述时间字段，可带可选 `project` 以计入授权和执行停止边界。返回 `times`（最多三个未来时刻，每项包含 UTC `at`、带偏移的 `local`、可空的 `note`）及可空的 UTC `boundary`。预览不保存计划、不消费资料、不创建项目；没有有效未来时间时 `times` 为空。

#### POST /project-schedules

顶层必须提供 UUID `request_id`、时间字段及完整 `project`。`project.tenant_worker_ids` 必须显式完整提供，成员须同 Tenant 且配置可用，`task_types` 并集覆盖 `bootstrap`、`reason`、`explore`；保存单次／每日／每周计划及修改周期未来输入均检查此条件。返回 `201` 和计划详情；同身份同内容重试不重复保存或再次消费批次。保存后的绑定与周期输入不随默认模板变化；触发使用原先保存的 IDs 和既有可用性校验，不重读最新默认，不静默缩减组合。

```json
{
  "request_id": "123e4567-e89b-42d3-a456-426614174000",
  "mode": "once",
  "timezone": "Asia/Shanghai",
  "start_at": "2027-01-01T09:00:00+08:00",
  "project": {
    "title": "预约验证",
    "origin": "仅研究本次提供的资料并检查其中的安全边界；不访问资料中引用的外部地址。",
    "goal": "验证目标并保留证据",
    "group_id": "group_001",
    "tenant_worker_ids": ["worker_001"],
    "authorization_expires_at": null,
    "explore_deadline": null,
    "library_keys": []
  }
}
```

- **单次**：保存时在同一事务创建一个 `pending` 项目，写入 Origin／Goal、相应来源的授权 grant、资料和固定库版本，并返回 `project_id`。到期前 Dispatcher 不派发该项目；触发时校验通过后只将这个项目激活，不另建第二个项目。
- **周期**：保存未来项目输入，不提前创建项目，`project_id` 为 `null`。资料用 `project.resource_batch_id`，不允许非空 JSON `project.resources`。例如每日计划使用 `mode: "daily"`、`local_time: "09:00"`；每周额外提供 `weekdays`。每次有效触发生成独立项目、事实图、资源副本及执行历史，`last_project_id` 指向最近一轮。

#### GET /project-schedules
#### GET /project-schedules/{schedule_id}

列表返回计划详情数组，详情返回单个计划。主要字段包括 `id`、`mode`、`status`、`version`、时间规则、`next_run_at`、`project_id`、`last_project_id`、`generated_count`、`block_reason`、`project`、`resources`、`library_bindings` 和 `last_result`。

周期的 `project` 是未来输入；`resources` 项包含 `id/path/kind/size_bytes/digest_sha256/source_name`，库绑定包含 `library_key/library_name/version/digest_sha256/file_count`。单次输入和资料以关联项目为准，计划的 `project` 为 `null`、计划资料列表为空，另以顶层只读 `explore_deadline`、`authorization_expires_at` 展示关联项目的执行停止时间与授权到期时间，直接读取关联项目，不另存计划副本；完整资料通过项目详情读取。`last_result` 为最近的触发记录或 `null`。

#### PUT /project-schedules/{schedule_id}

提交当前 `version` 和完整时间规则。版本不匹配返回 `409`，不能静默覆盖别人刚完成的编辑；成功后版本递增。已完成／停用计划不能编辑，单次与周期不能互相转换，每日与每周可以互改。

**单次**只编辑尚未触发的 `pending` 项目预约，不重复提交 `project`、资料 ID 或刷新库版本。顶层可选 `explore_deadline` 的更新语义为：

- 省略该键：保留项目现有执行停止时间。
- 显式 `null`：取消执行停止时间，不取消或延长授权期限。
- 非空：设为含时区的未来绝对时间，且新启动时间必须早于更新后的执行停止边界。

期限更新与改期在同一事务；先应用显式期限，再校验新预约，校验失败全部回滚。因此旧执行期限已过期时可显式改成未来值或 `null` 后重新排期。授权过期仍须通过项目重新授权接口显式处理，重新授权保留 `pending`，不会自行启动。

```json
{
  "version": 1,
  "mode": "once",
  "timezone": "Asia/Shanghai",
  "start_at": "2027-01-02T09:00:00+08:00",
  "explore_deadline": null
}
```

**周期**必须提供完整未来 `project`，只影响以后实例。Origin 来源可继续省略／使用 `null` targets；旧显式 Target 来源须保留原数组（包括空数组），无关字段编辑不删除该范围，也不能以省略／`null` 静默切换为 Origin 授权，否则返回 `422`。管理详情只读展示旧范围，新建不恢复重复填写。执行停止时间来自 `project.explore_deadline`，不是单次专用的顶层字段。可同时设置：

- `resource_ids` 省略或为 `null`：保留所有计划资料；`[]`：移除全部；非空数组：保留指定资料 ID，其他移除。重复 ID 或不属于此计划的 ID 被拒绝。
- `project.resource_batch_id`：追加本次新批次。先校验保留资料与新成员的最终路径冲突、类型及文件数量／字节配额；可在同一次编辑中移除旧资料并用新批次替换同路径资料。
- `project.library_keys`：最终选择的库集合。同 key 默认保留已固定 revision，新增 key 才解析当前版本；从集合移除即解除未来绑定。
- `refresh_library_keys`：显式将这些已选 key 更新到当前不可变 revision，未选择的 key 不能刷新。更新不追改历史项目。

成功编辑重新计算未来时间和受阻原因；有有效未来时间时清除旧原因，暂停中的周期计划仍保持暂停。达到生成次数上限或因结束日期已无下一有效时刻时完成；若限制来自授权或执行停止边界，边界前已无后续有效启动时间则变为 `blocked`，要求显式更新未来配置后恢复，不提前宣称尚未到达的边界已经过期。

#### POST /project-schedules/{schedule_id}/actions

Body 为 `{"version": 1, "action": "pause"}`，仍执行版本检查。

| 动作 | 适用范围与结果 |
| --- | --- |
| `cancel` | 尚未触发的单次预约；变为 `cancelled`，清除下一次时间，保留 pending 项目 |
| `start_now` | 尚未触发、受阻或已取消的单次预约；使用相同 pending 项目，仍执行用户、授权、Worker、范围和资料校验 |
| `pause` | active／blocked 周期计划；变为 `paused`，停止未来消费，不暂停当前项目 |
| `resume` | paused／blocked 周期计划；校验发起用户和未来输入后，从当前时间重新计算未来轮次，不补跑暂停期间历史轮次 |
| `disable` | active／paused／blocked 周期计划；变为 `disabled`，停止未来轮次，保留资料和历史 |

已触发项目要停止时使用项目自身动作，不用计划取消代替。合法 `start_now` 请求若在触发校验中受阻，返回更新后的 `blocked` 计划及原因，不把请求返回冒充项目已经启动。

#### GET /project-schedules/{schedule_id}/history

查询参数 `offset` 默认为 `0`，`limit` 默认为 `20`、范围 `1..100`。返回包含 `items` 数组和 `total` 的对象，空列表为 `{"items": [], "total": 0}`；记录按 `scheduled_at` 和 ID 倒序，包含 `id/schedule_id/occurrence_key/scheduled_at/processed_at/project_id/config_version/status/reason`。结果状态为 `started`、`skipped`、`missed`、`blocked`。

单次使用固定逻辑键 `once`，校验受阻后修正并首次启动仍使用同一条触发身份，保留先前受阻原因。周期使用原定 UTC 时刻为逻辑键；计划与逻辑键唯一，轮询重试或多个 Dispatcher 不重复启动／生成。触发记录保留使用的配置版本，不能用当前配置覆盖历史解释。

#### 资料持久化与触发边界

临时 batch 只能消费给项目或计划其中一个所有者。`GET /project-resource-batches/{batch_id}` 返回 `state`、`consumed_at`、`consumed_project_id`、`consumed_schedule_id` 和成员；消费后再次消费、修改成员或取消批次返回 `409`。普通项目批次和存量归属保留，不将 schedule ID 伪装成 project ID。

周期消费上传批次一次，将原始成员保存在资源根目录按 schedule 隔离的目录；未来每轮复制并校验文件大小／SHA-256，生成普通项目资源记录，继续使用既有 advisory bundle／只读挂载。临时批次 TTL 只清理 batch 记录和暂存，不删除计划持久原件。计划停用和已生成项目删除都不删除计划输入；修改计划资料也不影响已有项目副本。不使用隐藏模板项目，不将私有附件发布为公共库。

资料库在保存／显式编辑计划时固定不可变版本及摘要，每轮绑定同一版本，不静默换成 latest；归档和数据库删除约束包含计划引用。触发前校验库文件字节和摘要，资料缺失、不可读或摘要不符时，不提交缺资料的 active 项目。

资料复制和 DB 事务结果通过回调关联：复制失败或外层 rollback 仅清理本次新副本，不删除源 batch 或已提交资料；commit 后才清理 batch 暂存和此次移除的旧计划资料。普通项目初始 JSON 资料和 batch 消费也遵循外层 rollback 清理。

#### POST /dispatcher/project-schedules/consume

仅使用 Dispatcher 的机器 Bearer 身份，无请求体；服务端决定当前时间及本次处理数量，不从客户端 Body 接收时间或消费额度。返回 `{"processed": 0, "errors": 0}`，`processed` 包含受阻、错过、跳过和有效处理，不是已启动项目数。

每个计划独立事务完成触发和资料准备，再提交项目的 active 状态。发起用户失去访问权、组／场景／Worker 不可用、授权或执行期限失效、原显式 Target 来源的空集合、资料缺失／摘要错误均阻止启动，记录原因并停止该计划自动消费；其他计划不因这一业务拒绝被一并回滚。

单次错过原定时刻后，在校验通过时尽快启动同一 pending 项目并记录延迟。单次验证使用项目已经冻结的实际执行引用，不用后来修改的场景引用替代；无效引用仍明确受阻。周期超过 60 秒的历史轮次记为 `missed` 并前进到当前有效窗口／下一次；若同一事务后续失败，回滚后持久化的受阻原因仍包含错过范围，不补建历史项目；上一轮仍 active、paused、pending 或物理清理未确认时记为 `skipped`，不生成重叠项目。到达结束边界或成功生成次数上限时计划完成；若下一有效时刻被授权或执行停止边界截断，则计划变为 `blocked` 并显示“边界前已无后续有效启动时间”原因，不静默越过边界，也不把无后续轮次误报为正常完成。

#### GET /creation-requests/{request_id}?operation=project|schedule

使用当前用户和 Tenant 查询 UUID 请求身份；`operation` 必填，`project` 对应立即创建，`schedule` 对应计划保存。返回形状为：

```json
{
  "request_id": "123e4567-e89b-42d3-a456-426614174000",
  "operation": "schedule",
  "status": "committed",
  "project_id": null,
  "schedule_id": "123e4567-e89b-42d3-a456-426614174001"
}
```

`committed` 表示创建请求已提交，周期结果只有 schedule ID，单次结果同时有 project ID；`unknown` 表示当前未查到已提交身份，两个 ID 均为 `null`，不等于确认请求失败。关联项目后来被删除时，请求仍为 `committed` 且 `project_id` 可为 `null`；若已无关联业务结果，相同请求 ID 重试返回 `409`，不能把删除后的原请求重放成新创建。

API 已发出但结果未确认时继续查询，或使用相同请求 ID、相同内容重试；不要自动换请求 ID 再创建，也不要在未确认前清掉待消费资料。已知成功但导航失败，只重试进入已知对象，不撤销成功事实。页面的可选 `sessionStorage` 仅缓存不透明 ID／operation，不保存 Origin、Goal、凭据或完整载荷；读取、损坏值、写入或清理失败均显示短提示，不阻断提交、不锁定按钮、不把已确认成功改成未知。缓存不可用时仍保留内存恢复依据，但跨刷新查询入口可能未保存，且不承诺刷新后恢复原载荷／File。此身份机制覆盖立即创建和计划保存，不扩展为上传／ZIP 或所有管理动作的通用去重机制。

---

### Hints

#### POST /projects/{project_id}/hints

向项目补充一条策略提示。Hint 属于图外输入写操作，`active`、`stopped` 和 `completed` 项目均允许。

Body：

```json
{
  "content": "尝试文件上传绕过",
  "creator": "agent-A"
}
```

---

### Intents

#### POST /projects/{project_id}/intents

从某个 Fact 出发声明探索意图，产生一条尚无结论的 Intent。可选择只声明，或声明并同时认领。Intent 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "from": ["f001"],
  "description": "http 服务探测与目录扫描",
  "creator": "agent-A",
  "worker": null
}
```

`from` 为数组，至少包含一个 Fact id。`worker` 可为 `null` 或等于 `creator`；`null` 表示只声明不认领，等于 `creator` 表示声明并立即认领。多个 Fact 共同驱动同一次探索时，全部列入 `from`，完整保留因果关系。例如：

`from` 不能包含 `goal`。

服务端不校验 `description` 的业务语义，因此像 `description = "bootstrap"` 这样的保留 intent 也只是普通 Intent。是否把它解释为“项目最初阶段的直接推进尝试”，由消费者自己约定。

```json
{
  "from": ["f002", "f004"],
  "description": "用 f004 获取的凭据登录 f002 发现的后台",
  "creator": "agent-A",
  "worker": "agent-A"
}
```

响应：新创建的 Intent 对象。

---

#### POST /projects/{project_id}/intents/{intent_id}/heartbeat

消费者上报存活，更新 `worker` 和 `last_heartbeat_at`。仅对尚无结论的 Intent 有效。该接口用于续约，或在某条 Intent 已超时变为未认领后重新接手。若当前 `worker` 为 `null`，任意消费者可通过 heartbeat 认领；若当前 `worker` 已被其他消费者占用且未超时，返回 409。Heartbeat 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

消费者通常会按固定心跳周期继续 heartbeat 或轮询项目；项目进入 `stopped` 或 `completed` 后，继续 heartbeat 会返回 403，而轮询读取会看到项目 `status` 变化。由于 Server 在 `stopped` 时会立即清空 open intent 的 `worker`，消费者应把它当作硬停止信号，尽快终止本地推进，而不是继续依赖旧 claim。

Body：

```json
{
  "worker": "agent-B"
}
```

---

#### POST /projects/{project_id}/intents/{intent_id}/release

当前 `worker` 主动释放一条尚无结论的 Intent，使其回到未认领状态，供其他消费者立即接手。仅当前 `worker` 本人可以释放；若被其他消费者占用且未超时，返回 409。若该 Intent 当前已未认领，则幂等返回当前状态。`last_heartbeat_at` 保留，不清空。Release 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "worker": "agent-B"
}
```

响应：更新后的 Intent 对象。

---

#### POST /projects/{project_id}/intents/{intent_id}/conclude

探索结束，产出新 Fact 并将 Intent 结论落定。原子操作。仅对尚无结论的 Intent 有效。claim 规则与 heartbeat 相同：未认领可直接 conclude；由当前 `worker` conclude；若被其他消费者占用且未超时，返回 409。Conclude 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

`worker` 写入后永久保留，标识本次结论由谁产出。

Body：

```json
{
  "worker": "agent-B",
  "description": "80端口运行 nginx 1.18，存在目录遍历"
}
```

响应：

```json
{
  "fact": {
    "id": "f002",
    "description": "80端口运行 nginx 1.18，存在目录遍历"
  },
  "intent": {
    "id": "i002",
    "from": ["f001"],
    "to": "f002",
    "description": "http 服务探测与目录扫描",
    "creator": "agent-A",
    "worker": "agent-B",
    "last_heartbeat_at": "2026-03-21T10:05:00Z",
    "created_at": "2026-03-21T10:03:00Z",
    "concluded_at": "2026-03-21T10:05:00Z"
  }
}
```

---

#### POST /projects/{project_id}/complete

消费者判断某些 Facts 共同满足 goal 后调用，声明项目完成。原子操作。Complete 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

创建一条 `from` 指向所列 Facts、`to` 指向 `goal` 的已结论 Intent。完成声明无探索过程，服务端将 `creator` 和 `worker` 均设为请求中的 `worker` 值。Project 状态变为 `completed`，并立即清空当前 `project.reason`。

如果后续外部验证发现这次完成判断有误，可再调用 `POST /projects/{project_id}/reopen` 撤销这条完成边，并把纠错信息写成新的 Fact 后继续探索。

`from` 不能包含 `goal`。

Body：

```json
{
  "from": ["f007", "f012", "f015"],
  "description": "三个 shell 均已获取，满足 goal 要求",
  "worker": "agent-A"
}
```

---

#### POST /projects/{project_id}/reopen

外部确认项目其实尚未完成时，显式撤销当前完成态。该接口属于项目管理操作，不属于探索写操作；仅 `completed` 项目允许调用。

服务端会：

- 找到当前唯一一条 `to=goal` 的完成边
- 读取它原本的 `from`
- 删除这条完成边，不保留“曾完成过一次”的图内历史
- 新建一个普通 Fact，内容为调用者提供的客观纠错说明
- 新建一条已结论 Intent，`description` 固定为 `external_feedback`，其 `from` 继承原完成边的 `from`，`to` 指向这个新 Fact
- 将 `creator` 和 `worker` 都写为请求中的 `creator`
- 将项目状态改回 `active`
- 清空当前 `project.reason`

这适合诸如“提交的 flag 是错的，需要继续寻找正确 flag”这类图外反馈。反馈本身被记成图中的新 Fact，而 `reopen` 只是触发这次改写的控制动作。

Body：

```json
{
  "description": "flag FLAG{fake} 是错误的，需要继续寻找正确 flag",
  "creator": "judge"
}
```

响应：

```json
{
  "project": {
    "id": "proj_001",
    "title": "xx渗透测试",
    "status": "active",
    "created_at": "2026-03-21T10:00:00Z",
    "reason": null
  },
  "fact": {
    "id": "f016",
    "description": "flag FLAG{fake} 是错误的，需要继续寻找正确 flag"
  },
  "intent": {
    "id": "i021",
    "from": ["f008"],
    "to": "f016",
    "description": "external_feedback",
    "creator": "judge",
    "worker": "judge",
    "last_heartbeat_at": "2026-03-21T10:10:00Z",
    "created_at": "2026-03-21T10:10:00Z",
    "concluded_at": "2026-03-21T10:10:00Z"
  }
}
```

---

### 导出

#### GET /projects/{project_id}/export?format=yaml

返回项目图的 YAML 结构化快照，供消费者读取。不含 `last_heartbeat_at`。尚无结论的 Intent（`to=null`）出现在 YAML 中，`worker` 字段直接反映当前是否有消费者在处理。`project.reason` 不在导出中出现，因为它是项目级协调状态，不属于事实图。读取时服务端仍会执行超时清理，因此导出结果反映的是清理后的当前状态。`created_at` 和 `concluded_at` 按服务端当前时区格式化为 `YYYY-MM-DD HH:mm:ss`。

```yaml
project:
  title: "xx渗透测试"
  origin: "目标 http://192.168.1.10"
  goal: "拿到 flag"
  bootstrap_enabled: true

hints:
  - content: "优先看 web 服务"
    creator: "human"
    created_at: "2026-03-21 18:00:00"
  - content: "注意 80 端口"
    creator: "human"
    created_at: "2026-03-21 18:00:00"

facts:
  - id: origin
    description: "目标 http://192.168.1.10"
  - id: goal
    description: "拿到 flag"
  - id: f001
    description: "发现开放端口 22/80/443"
  - id: f002
    description: "80端口运行 nginx 1.18，存在目录遍历"
  - id: f003
    description: "遍历发现 /backup/db.sql，包含用户表"
  - id: f004
    description: "获得凭据 admin/passwd123"
  - id: f005
    description: "ssh 登录失败，凭据不通用"
  - id: f006
    description: "web后台登录成功，发现文件上传功能"
  - id: f007
    description: "上传 webshell 成功，获得 www-data shell"
  - id: f008
    description: "在 /var/www/html/flag.txt 找到 flag{abc123}"

intents:
  - from: [origin]
    to: f001
    description: "nmap 全端口扫描"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:01:00"
    concluded_at: "2026-03-21 18:02:00"

  - from: [f001]
    to: f002
    description: "http 服务探测与目录扫描"
    creator: "agent-A"
    worker: "agent-B"
    created_at: "2026-03-21 18:02:00"
    concluded_at: "2026-03-21 18:04:00"

  - from: [f002]
    to: f003
    description: "利用目录遍历下载 /backup/db.sql"
    creator: "agent-B"
    worker: "agent-B"
    created_at: "2026-03-21 18:04:00"
    concluded_at: "2026-03-21 18:05:00"

  - from: [f003]
    to: f004
    description: "解析 db.sql 提取用户凭据"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:05:00"
    concluded_at: "2026-03-21 18:05:30"

  - from: [f004]
    to: f005
    description: "尝试 ssh 登录"
    creator: "agent-A"
    worker: "agent-C"
    created_at: "2026-03-21 18:05:30"
    concluded_at: "2026-03-21 18:06:00"

  - from: [f002, f004]
    to: f006
    description: "用 f004 获取的凭据登录 f002 发现的后台"
    creator: "agent-A"
    worker: "agent-B"
    created_at: "2026-03-21 18:05:30"
    concluded_at: "2026-03-21 18:06:00"

  - from: [f006]
    to: f007
    description: "上传 php webshell 绕过扩展名检测"
    creator: "agent-B"
    worker: "agent-B"
    created_at: "2026-03-21 18:06:00"
    concluded_at: "2026-03-21 18:07:00"

  - from: [f007]
    to: f008
    description: "在 shell 中搜索 flag 文件"
    creator: "agent-B"
    worker: "agent-A"
    created_at: "2026-03-21 18:07:00"
    concluded_at: "2026-03-21 18:08:00"

  - from: [f008]
    to: goal
    description: "flag{abc123} 满足 goal 要求"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:08:00"
    concluded_at: "2026-03-21 18:08:01"
```

尚无结论的 Intent 示例（`worker` 不为 null 表示有消费者正在处理）：

```yaml
  - from: [f003]
    to: null
    description: "尝试利用目录遍历读取敏感文件"
    creator: "agent-A"
    worker: "agent-C"
    created_at: "2026-03-21 18:09:00"
    concluded_at: null
```

---

#### GET /projects/{project_id}/export?format=timeline

返回项目的时间线纯文本，按事件发生时间排序。YAML 快照展示图的结构拓扑，timeline 展示事件的先后顺序和因果链，两者互补。事件类型包括 `PROJECT CREATED`、`HINT`、`INTENT DECLARED`、`INTENT CONCLUDED`、`PROJECT COMPLETED`。若项目曾经完成后又被 `reopen`，由于旧的 `goal` 边已被撤销，timeline 中不会保留之前那次 `PROJECT COMPLETED` 事件。

---

## 外部漏洞复现／复测 v1

复测使用独立记录和独立执行上下文，不进入普通 Bootstrap／Reason／自动探索／自动报告流程。外部报告是待验证资料，不是内部 Observation 或已确认漏洞。相关产品与交付范围见 [漏洞复测方案](vulnerability-retest-design.md)。

### 身份与集成配置

浏览器沿用 Cookie 会话及写操作的 `X-Cairn-CSRF-Token`。外部平台使用专门的租户集成 Bearer 凭据，不能使用或获得 Dispatcher machine token。集成只能访问自己创建的复测及关联资料／证据；存在 Authorization 时不回退 Cookie。集成停用阻止后续调用，不改写旧记录或自动取消已受理任务。

“漏洞复测配置”仅使用现有超级管理员权限管理，所有管理接口必须显式提供目标租户 `?tenant_id=...`，integration 及普通用户均无管理权限；浏览器写操作继续校验 CSRF。

- `GET /api/v1/verification-readiness`：用户／integration 只读取认证所属租户的安全摘要 `{configured, ready, worker_name, model_name, model_display_name, enabled, reason, message}`，不能通过 query 切换租户，不返回凭据。`reason` 区分未配置、Worker／模型不可用、执行器离线、独立容量为零或未知。
- `GET /api/v1/verification-configuration?tenant_id=...`：读取该租户唯一专用配置，返回 `{tenant_id, tenant_worker_id, worker_name, display_name, engine, model_id, model_name, model_display_name, max_running, priority, enabled, revision, ready, reason, message, updated_at}`；未配置时执行资源字段为 `null`。
- `PUT /api/v1/verification-configuration?tenant_id=...`：直接提交 `{model_id, display_name?, engine?, max_running?, priority?, enabled?}`。默认名称为“漏洞复测专用 Worker”，引擎只接受 `pi`，并发默认 1（1–64），优先级默认 100（−10000–10000），默认启用。服务端在显式目标租户创建或维护一个稳定身份的 `purpose=verification` Worker；不接受项目组、普通 Worker ID 或模板。保存与首次 claim 共用租户锁，影响尚未首次 claim 的请求，不热改已开始的执行。允许保存停用或尚未就绪的配置，并通过返回的真实原因说明不能受理／执行。
- `GET /api/v1/verification-configuration/options?tenant_id=...`：返回 `{models, engines, defaults}`；模型仅含 `id/display_name/model_name/protocol/enabled/credential_configured`，不返回组、普通 Worker 或密钥。
- `/api/v1/verification-integrations?tenant_id=...`：列表、创建、改名／启停、停用和轮换；创建仅提交 `{name}`，修改仅接受 `name/enabled`。集成不再配置生效的 Worker 模板或项目组。创建与轮换返回 `{integration, token}`，明文仅返回一次且禁止缓存；稳定身份、归属及历史不因轮换而重建。按 ID 管理的路径同样必须提供 `tenant_id`。

每次新受理只绑定该租户专用 Worker 的稳定身份，内部 Project 的 `group_id=NULL`，不造默认组、不要求三角色。`purpose` 由服务端维护，存量普通 Worker 为 `general`；普通列表、按 ID CRUD、模板及普通项目／计划绑定均排除专用用途，不能通过普通 Schema 覆盖用途。普通任务的组、三角色要求、配置及调度语义不变。v52 增量迁移保留 v50／v51 checksum、普通 Worker、历史请求／任务／资料／结果及集成身份；旧配置解除对普通 Worker／组的引用，管理员需明确保存新的专用配置，历史旧执行不静默换绑。

### 资料批次

- `POST /api/v1/verification-resource-batches`：创建临时上传批次；可传 `{source_request_id}`，从有权访问的旧复测复制原件用于新请求，不复制旧执行证据或把旧结论作为本次事实。
- `GET /api/v1/verification-resource-batches/{id}`：查询批次与成员。
- `POST /api/v1/verification-resource-batches/{id}/files?path=相对路径&kind=资料类型`：`application/octet-stream` 原始字节上传，返回成员 ID、路径、大小和 SHA256；不是 multipart 表单。
- `DELETE /api/v1/verification-resource-batches/{id}/members/{member_id}`：移除尚未消费批次中的指定成员。
- `DELETE /api/v1/verification-resource-batches/{id}`：取消尚未消费的批次，不删除已受理档案。

批次归属包含真实 user/integration 调用者，不因同租户而允许另一集成消费。路径、大小、数量及摘要校验复用原资料服务；保持 MD 附件相对路径，不静默覆盖。内联 MD／文本按原 UTF-8 正文保存。DOCX 原件保留，标准库提取的正文、表格和 PNG／JPEG 图片独立标明派生来源；不执行内容或自动抓取外链。截图必须经实际视觉读取，上传／挂载本身不代表理解。

### 受理与查询

`POST /api/v1/verification-requests` 接收：

```json
{
  "schema_version": 1,
  "request_id": "0f5aa5d4-80ac-4d33-82cd-5dd3ce4c0e35",
  "external_id": "ISSUE-123",
  "mode": "retest",
  "title": "原报告问题修复后复测",
  "origin": "仅在已明确授权的测试环境复测本报告描述的问题，不扩展到其他系统。",
  "report": {"type": "markdown", "content": "# 待复测问题\n\n填写原条件、步骤、观察和修复变化。"},
  "authorization_expires_at": null
}
```

示例只说明协议，不代表目标或费用授权，并假定管理员已为认证所属租户保存可用的专用 Worker 与模型；无需项目组或外部令牌。外部和浏览器创建均不能指定执行资源，`group_id`、`worker_template_id`、`tenant_worker_id`、`tenant_worker_ids`、模型／Prompt 等额外字段明确返回 `422`，不静默忽略。未配置、资源失效、没有独立容量或容量未知时明确说明原因；配置有容量而暂时满载则排队。可选 `execution_deadline` 使用绝对时间；`previous_request_id` 只建立历史关系，不自动继承授权、Worker revision 或模型，再次复测在首次 claim 时采用专用配置。

`report` 判别联合：`markdown/text` 使用 `content`；`file` 使用 `batch_member_id` 并要求 `resource_batch_id`；`platform_vulnerability` 使用真实 `project_id/vulnerability_id`，仅允许有来源访问权且来源租户与当前租户一致的用户，integration 明确拒绝。MD、TXT、DOCX、PNG、JPEG 可作手工主资料；附件可与内联正文或平台来源同时提交。主资料在受理时绑定为持久 ProjectResource，批次过期不影响已受理档案。

平台来源由服务端读取漏洞原文字段、关联 Fact／Observation、适用复核说明及直接引用的合法 Artifact，保存原文 JSON 快照、来源清单和历史证据原字节为本次输入 ProjectResource。清单保留来源、摘要和不可携带的缺项，不复制整包 provenance、运行日志、Prompt、session 或配置，不伪造 Observation，也不将历史输入登记成本轮新 Artifact。execution input 与专用 Prompt 提供只读挂载索引；历史资料不是本轮观察，不扩大本次授权，不自动改写原漏洞状态。

- `GET /vulnerabilities?current_tenant_only=true`：复测候选只读取认证用户当前租户的漏洞，不接受客户端指定租户。该参数默认 `false`，超级管理员普通成果查看仍为全局，普通用户始终按所属租户隔离；复测入口及关联历史同样限定当前租户。
- `GET /api/v1/verification-sources/projects/{project_id}/vulnerabilities/{vulnerability_id}/preview`：仅合法用户预览当前原文、相关材料及缺项，`Cache-Control: no-store`；受理时服务端重新读取，不信任客户端预览快照。
- 同路径 `/history`：仅合法用户按 `limit/offset` 查询该来源关联的独立复测历史。
- 已受理请求以 `input_source` 返回来源项目／漏洞及 `input_mode=current_vulnerability/copied_snapshot`；来源 ID 存在请求行一处，不因源记录修改／删除级联删除档案。再次复测可复制旧输入批次，必须保留原始主资料、清单和历史证据的完整映射；或显式选择当前漏洞重新留档，两者不能混淆。复制旧平台 JSON 主资料仅在核验过的旧输入路径允许，普通文件上传不会因此放宽类型。

幂等键为租户＋真实调用者类型／ID＋`request_id`。新受理返回 `202` 和 Location；同键同输入返回 `200` 的原记录，先查重放再读取当前配置或批次，不重新选择 Worker、不再消费批次、不重复派发；同键不同输入返回 `409`。新 request_id 才是新的一轮复测。上下文、请求与资料绑定在同一事务中完成。指纹继续将旧协议缺省资源字段按 `group_id:null`、`worker_template_id:null` 计算，保证历史未显式选资源的等价输入仍可重放；旧客户端仍携带已删除字段时应修正请求，而不是自动换 ID 重建任务。

创建响应丢失后，客户端必须保留原 `request_id` 与原正文，后续鉴权失败或网关错误不能证明此前未受理。仅携带 `resource_batch_id` 的创建请求收到明确 `410 {"detail":"Project resource batch expired"}` 时，可据同租户事务锁及同键查找先于批次校验，确认该键尚未受理并更换过期批次；不能将任意 `410` 当成此证明。恢复编辑保留正文与授权，原过期文件须重新上传或复制。

- `GET /api/v1/verification-requests`：`{items,total}` 分页列表；支持 `limit/offset/external_id/mode/execution_status/outcome/source_project_id/source_vulnerability_id` 筛选，integration 仍只能查自己的记录。
- `GET /api/v1/verification-requests/{id}`：本轮输入定位、实际配置、执行状态、结果／终止原因、过程、原件、允许交付的证据、历史及回收情况。`tenant_workers:[{id,display_name,revision}]` 未 claim 时 revision 为 `null`；`execution_binding` 未 claim 为 `null`，执行后只返回当次 `{worker_id,display_name,worker_revision,model_id,model_name,model_revision}` 安全历史摘要，不用当前 Worker 的模型冒充。`worker_template_id` 仅为历史字段，新受理为 `null`。`execution_message` 说明开始时采用专用配置、授权／截止阻塞或旧绑定不能自动替换，不新增持久业务状态。
- `POST /api/v1/verification-requests/{id}/cancel`：请求停止；接收停止请求不是执行已经退出，重复取消不重复执行动作，不能覆盖已接受结果。
- `GET /api/v1/verification-requests/{id}/report`：从唯一已接受结果生成 Markdown，不调用模型；结果未就绪返回明确错误。
- `GET /api/v1/verification-requests/{id}/materials/{resource_id}`：受控下载原件或派生资料。
- `GET /api/v1/verification-requests/{id}/artifacts/{artifact_id}`：受控下载本轮允许交付证据，不开放任意 Project 下载。详情与下载复用持久终结结果的 `evidence_refs` 作为接受事实，仍核对本请求 Project／TaskRun 归属及原大小／SHA256；已接受证据不因模型当前 Header 改变而撤回。未接受材料和新交付继续检查当次凭据及内容边界，不保存历史明文秘密。来源与交付按材料类别、来源路径及明确内部文件身份排除内部资料，不因文件名含 `session`／`prompt` 等漏洞主题词误排合法证据。

### 执行状态、业务结果与回收

`execution_status` 集中投影为 `queued/running/cancelling/completed/failed/cancelled`。`completed` 要求本轮结果及引用证据已持久化，且执行确认结束；TaskRun 退出 0 不足以认定完成。停止未确认时仍显示正在取消／运行，不将失联当已停止。平台运行、解析或交付故障以稳定 `error.code/message` 交付，`result` 为空，不冒充阴性结果。

`result` 字段：`outcome`、`summary`、`tested_scope`、`notes`、`limitations`、`reason_code`、`evidence_refs`。四态含义：

- `confirmed`：本轮证据支持报告主张，说明成立条件与影响。
- `not_reproduced`：已实际测试的说明范围内未复现，不自动判定安全或已修复。
- `environment_changed`：有本轮观察支持的环境／条件变化，不用来掩盖平台故障。
- `insufficient_evidence`：明确缺口及未测试事项，可无执行证据。

前三态必须引用已落盘且属于本请求当前执行的 Artifact。外部调用方无权提交、修改业务结果；内部回报绑定 Request、Project、有效 TaskRun、execution generation 及认领凭据，同内容重复交付幂等，冲突或迟到回报不能覆盖唯一终结。内部 Verifier 的 Observation／发现 session／独立性限制保持。

外部执行使用专用 `external_verify` TaskRun 和 `external_verify_execute` phase／`external_verification_result` 结构化提交，Worker 能力仍映射已有 `explore`。直接创建 pi SDK session，不经过 Bootstrap／Reason／独立复核／自动报告。Dispatcher 显式读取随包发布的 `dispatcher/prompts/external-verification/agent.md`，通过现有 Prompt 审计记录指引与执行 Prompt 的 hash；指引缺失、为空或不可读时以 `verification_instructions_unavailable` 明确失败，不启动模型、不回退普通探索。上传材料中的同名文件不获得指令权限，不改变普通 runner loader。一次 session 允许多轮模型／工具交互，不代表一次模型调用。

首次 `POST /api/v1/verification-requests/{id}/claim` 只允许认证 Dispatcher，原子刷新尚未开始请求的专用绑定并确定 Worker revision、模型 revision 和凭据版本。回执 `{request_id,project_id,intent_id,claim_token,execution_generation,replayed,execution_binding,task_run,input}` 带 `Cache-Control: no-store`；首次 `execution_binding` 为仅内存消费的完整执行配置，Dispatcher 必须用它构造实际 Worker，不能复用 claim 前 materialize 的缓存。TaskRun 仅持久化无明文密钥的执行 provenance（含 header hash），后续更新不能覆盖这些来源字段。重复 claim 返回原 TaskRun／认领凭据，`replayed=true` 且 `execution_binding=null`，不是第二次启动许可；已 claim 禁止从通用 resolver 重新解析当前专用配置。历史展示和新输出检查依据当次 provenance／密文凭据版本，旧 key 轮换后仍检查当次 key；实际秘密来源不可读取时明确拒绝新交付，不能以当前模型冒充或改写旧档案。

普通活动任务和普通报告先派发，原 `runtime.max_workers` 与 `max_running_projects` 仍留给普通任务；复测只使用执行器 `hard_cap - runtime.max_workers` 中额外容量，每 Dispatcher 最多并发一条，并受专用 Worker 自身容量约束。心跳 `runtime` 上报 `hard_cap` 和 `verification_capacity=min(1,max(0,hard_cap-max_workers))`；readiness 用有效心跳区分独立容量为零、未知和执行器离线，不把临时满载当永久不可用。不降低普通额度、不抢占或停止普通任务；共用宿主机、网络及模型限额仍存在资源竞争，不构成物理资源隔离。

网络故障只补交固定结果／证据，不重新执行目标操作。执行与交付排空后按精确容器归属真正 remove；普通项目的回收策略不变。回收失败单独显示，已接受结果仍可查；原件、报告和服务端证据不依赖容器存活，不通过删除 Project 清理环境。

## 消费者典型使用流程

```
1. 读取结构化项目数据
   用于调度、状态判断、intent 选择与协议写回

2. GET /projects/{id}/export?format=yaml
   读取图快照（仅供 Dispatcher 渲染给 Agent）

3. 若项目仍处于最初阶段：
   典型判定是 facts 只有 `origin` 和 `goal`，且没有普通 intent；
   某些消费者会允许“没有任何 intent”，或“只存在保留 bootstrap intent”都视为初始态
   若 `project.bootstrap_enabled=false`，或消费者不具备 bootstrap 能力，则直接进入 reason；
   a. 若尚不存在保留 bootstrap intent：
      POST /projects/{id}/intents
        { from: ["origin"], description: "bootstrap", creator: "dispatcher.bootstrap", worker: null }
   b. POST /projects/{id}/intents/{intent_id}/heartbeat
        { worker: "dispatcher-worker-A" }
      认领成功后，直接做一轮 `bootstrap`
   c. 推进过程中持续 heartbeat
   d. 若拿到了关键证明性事实：
      POST /projects/{id}/intents/{intent_id}/conclude
        { worker: "dispatcher-worker-A", description: "发现登录页存在默认口令 admin:admin" }
      若该新 fact 已足以满足 goal，也可继续：
      POST /projects/{id}/complete
        { from: ["新写入的 fact id"], description: "为什么该事实已足以满足 goal", worker: "dispatcher-worker-A" }
   e. 若本轮 bootstrap 失败且需要放弃：
      POST /projects/{id}/intents/{intent_id}/release
        { worker: "dispatcher-worker-A" }
   f. 回到步骤 1

4. 若图中存在未认领的普通 Intent（worker=null）：
   a. 选择一条可认领 Intent
   b. POST /projects/{id}/intents/{intent_id}/heartbeat
        { worker: "dispatcher-worker-A" }
      认领成功后，派发一次 `explore`
   c. 探索过程中持续 heartbeat
   d. 探索成功时：
      POST /projects/{id}/intents/{intent_id}/conclude
        { worker: "dispatcher-worker-A", description: "发现 /search 参数存在报错注入" }
   e. 若探索失败且需要放弃：
      POST /projects/{id}/intents/{intent_id}/release
        { worker: "dispatcher-worker-A" }
   f. 回到步骤 1

5. 若图中没有未认领 Intent：
   a. POST /projects/{id}/reason/claim
        { worker: "dispatcher-worker-A", trigger: "new_facts" }
      认领成功后，派发一次 `reason`
   b. 推进过程中持续 POST /projects/{id}/reason/heartbeat
        { worker: "dispatcher-worker-A" }
   c. 若 `reason` 返回完成结论：
      POST /projects/{id}/complete
        { from: ["f008"], description: "flag{abc} 满足 goal 要求", worker: "dispatcher-worker-A" }
      流程结束
   d. 若 `reason` 返回新的 intent：
      POST /projects/{id}/intents
        { from: ["f003"], description: "尝试 SQL 注入", creator: "dispatcher-worker-A", worker: null }
      然后 POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
   e. 若 `reason` 未返回完成结论，也未返回新 intent：
      POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
      本轮不写图
   f. 若 `reason` 执行失败、超时或需要放弃：
      POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
   g. 回到步骤 1

6. 若项目已 `completed`，但图外验证确认之前的完成判断有误：
   a. POST /projects/{id}/reopen
        { description: "flag FLAG{fake} 是错误的，需要继续寻找正确 flag", creator: "judge" }
   b. 项目回到 `active`，图中新增一条 `external_feedback` 结论边和一个纠错 Fact
   c. 回到步骤 1
```
