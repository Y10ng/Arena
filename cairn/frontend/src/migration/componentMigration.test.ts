import { readFileSync, statSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import {
  componentMigrationRecords,
  migrationStatuses,
  routePageInventory,
  verificationStatuses,
} from './componentMigration'

const unique = (values: readonly string[]) => new Set(values).size === values.length
const frontendRoot = fileURLToPath(new URL('../../', import.meta.url))

describe('frontend component migration inventory', () => {
  it('keeps route names, files and migration record ids unique', () => {
    expect(unique(routePageInventory.map((page) => page.routeName))).toBe(true)
    expect(unique(routePageInventory.map((page) => `${page.routeName}:${page.file}`))).toBe(true)
    expect(unique(componentMigrationRecords.map((record) => record.id))).toBe(true)
  })

  it('keeps migration record pages aligned with the route inventory', () => {
    const inventoryPages = new Set<string>(routePageInventory.map((page) => page.routeName))
    const coveredPages = new Set(componentMigrationRecords.flatMap((record) => record.pages))

    expect(routePageInventory.filter((page) => !coveredPages.has(page.routeName))).toEqual([])
    expect([...coveredPages].filter((routeName) => !inventoryPages.has(routeName))).toEqual([])
  })

  it('only inventories route page files that exist', () => {
    expect(
      routePageInventory.filter((page) => {
        try {
          return !statSync(resolve(frontendRoot, page.file)).isFile()
        } catch {
          return true
        }
      }),
    ).toEqual([])
  })

  it('uses only declared migration and verification states', () => {
    const knownMigrationStates = new Set(migrationStatuses)
    const knownVerificationStates = new Set(verificationStatuses)

    componentMigrationRecords.forEach((record) => {
      expect(knownMigrationStates.has(record.migrationStatus)).toBe(true)
      record.verification.forEach((status) => {
        expect(knownVerificationStates.has(status)).toBe(true)
      })
    })
  })

  it('does not mark legacy code removed before end-to-end verification', () => {
    const unverifiedRemoval = componentMigrationRecords.filter(
      (record) =>
        record.migrationStatus === 'legacy-removed' && !record.verification.includes('e2e'),
    )

    expect(unverifiedRemoval).toEqual([])
  })

  it('tracks semantic status tokens and removed legacy control compatibility styles', () => {
    const record = componentMigrationRecords.find((candidate) => candidate.id === 'design-tokens')
    const tokens = readFileSync(resolve(frontendRoot, 'src/design-system/tokens.css'), 'utf8')
    const primitives = readFileSync(
      resolve(frontendRoot, 'src/design-system/primitives.css'),
      'utf8',
    )
    const vulnerabilities = readFileSync(
      resolve(frontendRoot, 'src/features/vulnerabilities/VulnerabilitiesPage.vue'),
      'utf8',
    )

    expect(record?.migrationStatus).toBe('migrating')
    expect(record?.notes).toContain(
      '基础 token、Light/Dark 与 reduced-motion 合约已建立；新增 success/info/warning/danger soft 与 border 语义别名，并将 Console、Monitoring 与 Vulnerabilities 的重复状态背景和边框值收口到 token。',
    )
    expect(tokens).toContain('--success-border:')
    expect(tokens).toContain('--danger-soft:')
    expect(primitives).not.toMatch(/\.btn-(?:primary|secondary|success|danger|warn)/)
    expect(primitives).not.toMatch(/\.form-(?:input|select|textarea)/)
    expect(primitives).not.toMatch(/\.dialog-(?:backdrop|card)/)
    expect(vulnerabilities).toContain('class="vuln-confidence-input"')
    expect(vulnerabilities).not.toContain('class="form-input"')
  })

  it('tracks the implemented Forbidden and NotFound route contract', () => {
    const record = componentMigrationRecords.find((candidate) => candidate.id === 'status-pages')

    expect(record).toMatchObject({
      pages: ['Forbidden', 'NotFound'],
      migrationStatus: 'migrated',
      verification: ['unit', 'component', 'e2e'],
      replacement: {
        files: ['src/features/status/ForbiddenPage.vue', 'src/features/status/NotFoundPage.vue'],
      },
    })
    expect(record?.notes).toContain(
      'Forbidden 与 NotFound 已作为认证后的 FrontLayout 子路由接入；普通用户访问 super-admin 页面不再静默返回仪表盘，未知 URL 不再渲染空白页，同时保留匿名 redirect 与 mustChangePassword 优先级。',
    )
  })

  it('tracks verified dialog primitives and migrated overlays', () => {
    const record = componentMigrationRecords.find(
      (candidate) => candidate.id === 'dialogs-and-confirmations',
    )

    expect(record).toMatchObject({
      migrationStatus: 'migrated',
      verification: ['component'],
      replacement: {
        files: [
          'src/components/ui/Dialog.vue',
          'src/components/ui/AlertDialog.vue',
          'src/components/patterns/ConfirmAction.vue',
        ],
      },
    })
    expect(record?.notes).toContain(
      'Dialog、AlertDialog 与 ConfirmAction primitive 已完成组件验证；Users 已迁移账户管理 Overlay，TenantWorkers 已迁移删除确认，TenantModels 已迁移 Model 与 API Key 删除确认，ProjectWorkspace 已迁移 Worker 绑定、辅助资料、重新开启与删除确认，Projects 已迁移重命名、移动分组与删除确认，FrontLayout 已迁移项目组创建、删除确认与 Command Palette，Console TextEditor 已迁移 fullscreen Dialog，Graph retry 与 project hint 已迁移 inline Dialog 并保留 payload、快捷键、提交状态及 fullscreen Escape 契约，WorkspaceVulnerabilities 已迁移创建 Dialog 并保留初始焦点、触发器焦点恢复、pending 关闭保护、精确八字段 payload 与 query invalidation，ScenariosConsole 已迁移删除 ConfirmAction 并保留完整列表 PUT normalization、pending 防重复和关闭保护、失败原地重试、成功 refetch、双 query invalidation、Toast 与剩余场景选中；Command Palette 保留 Ctrl/Cmd+K、搜索排序、上下键、Enter、鼠标执行、项目上下文与 Tenant/角色可见性，补齐 portal、focus trap、初始焦点和精确焦点恢复，并将 Monitoring 可见性对齐 super-admin route；ProjectWorkspace、Projects、FrontLayout、TextEditor、Graph hint、Vulnerabilities create、Scenarios delete 与 Command Palette 的旧简单 Overlay selector 已移除；Runs command stdout Overlay 因当前 API 不提供 command stdout 而移除，不虚构不可用的数据查看器；声明范围内的手写 Overlay 已完成迁移，本次未执行 E2E、axe 或视觉验证。',
    )
    expect(record?.notes).toContain(
      'WorkerTemplatesPanel 已将行内删除确认迁移到 ConfirmAction，补齐 pending 防重复、失败原地保留、成功 query invalidation 与触发器焦点恢复。',
    )
  })

  it('registers the verified Select primitive and Workers migration', () => {
    const record = componentMigrationRecords.find(
      (candidate) => candidate.id === 'buttons-and-form-controls',
    )

    expect(record?.replacement.files).toContain('src/components/ui/Select.vue')
    expect(record?.migrationStatus).toBe('migrating')
    expect(record?.verification).toEqual(['component'])
    expect(record?.notes).toContain(
      'Login、ChangePassword、ConsoleTimeout 与 Overview 已迁移 Button；TenantWorkers 已迁移 Model Select；TenantModels 已迁移 protocol、auth header 与 discovery Select；WorkspaceVulnerabilities 创建弹窗已迁移 Button、FormField、Input、Select 与 Textarea，confidence 因 Cairn Input 的 nullable numeric 契约暂保留原生 number input；Findings 已迁移带可见标签的严重度/状态 Select 与 refresh Button；WorkspaceArtifacts 已迁移带可见标签的 kind Select、刷新、预览、Run 与复制操作；Traffic 已迁移全局项目 FormField/Select、刷新及 Run/evidence/复制 Button，工作区模式继续隐藏项目选择器；WorkspaceHints 已迁移 Button、FormField、Select 与 Textarea，保留项目级/Fact/Intent 锚点、Intent 优先和 Ctrl/Cmd+Enter 合同；其余清单页面控件仍待迁移。',
    )
    expect(record?.notes).toContain(
      'ScenariosConsole 已迁移 Button、Input、Textarea、FormField、Select、Checkbox、Alert、Spinner 与 EmptyState；场景侧栏保留领域专用选择按钮，并保持完整列表 PUT normalization、preflight、dirty draft ownership、pending 防重复与 query invalidation 合同。',
    )
    expect(record?.notes).toContain(
      'WorkersConsole 已迁移 Button、IconButton、Input、Select、FormField、Checkbox、Alert 与 Spinner；保留表单/YAML Tabs、dirty confirmation、表单序列化、只读状态与保存禁用合同。',
    )
    expect(record?.notes).toContain(
      'KnowledgeConsole 与 PromptGroupsConsole 已迁移外层 Button、IconButton、Input、Select 与 FormField，保留资料库/目录/文件及模板组领域选择按钮、隐藏 file input、路由 focus、dirty confirmation 与异步 selection ownership；WorkerTemplatesPanel 已迁移语义行选择、Button 与 IconButton。',
    )
    expect(record?.notes).toContain(
      '全仓 Vue 模板已无 .btn-primary/.btn-secondary/.btn-success/.btn-danger、.form-input/.form-select/.form-textarea 引用，对应 design-system 兼容层及 Console、Projects、ProjectWorkspace、Monitoring 页面重复 selector 已删除；原生专用控件使用领域 class，不冒充共享 Input。',
    )
  })

  it('tracks the component-verified DataTable, FilterBar, and Findings migrations', () => {
    const record = componentMigrationRecords.find(
      (candidate) => candidate.id === 'tables-and-lists',
    )

    expect(record).toMatchObject({
      migrationStatus: 'migrating',
      verification: ['component'],
      replacement: {
        label: 'DataTable 与 FilterBar；SearchField、Pagination 后续补充',
        files: ['src/components/patterns/DataTable.vue', 'src/components/patterns/FilterBar.vue'],
      },
    })
    expect(record?.notes).toContain(
      'DataTable 与 FilterBar 已完成组件验证；Findings 已升级为成果中心：漏洞管理使用跨项目扁平表格、服务端严重度/状态筛选、本地搜索/项目筛选、KPI 与复合 project/vulnerability Drawer selection；报告管理使用服务端筛选分页、复合 project/artifact 深链和 MasterDetail 安全预览；WorkspaceArtifacts 已迁移 category/kind 筛选、trim 后大小写不敏感的 basename 文件名搜索、最新优先列表、artifactId 深链 reconcile、独立预览/Run 操作及无分页、轮询合同，并修复嵌套交互行；Traffic 已迁移 Runs 与 Hook evidence 双 DataTable、全局项目 FilterBar、稳定 evidence identity 和独立详情操作，保留无搜索、分页、用户排序与 Traffic polling 合同；WorkspaceHints 已迁移被动 FilterBar 与 DataTable，保留稳定最新优先、完整正文、父级轮询以及无搜索、分页和用户排序合同；相关页面均补齐可访问表格语义；SearchField 与通用 Pagination 留待后续列表切片；本次未执行 E2E、axe、360px 实机或视觉验证。',
    )
  })

  it('tracks Findings cards, badges, and async feedback adoption', () => {
    const cardRecord = componentMigrationRecords.find(
      (candidate) => candidate.id === 'cards-badges-and-status',
    )
    const asyncRecord = componentMigrationRecords.find(
      (candidate) => candidate.id === 'async-feedback',
    )

    expect(cardRecord?.notes).toContain(
      'ConsoleTimeout 已迁移 Card，Overview 已迁移 Card、Badge 与 StatusBadge；Findings 成果中心已迁移 KPI、漏洞表格与报告审阅 Card，并使用漏洞严重度/可信度/状态领域 Badge，避免扩大全局 status 语义；Traffic 已迁移 Runs/evidence Card、Run 生命周期 StatusBadge 与 Bash 退出码领域 Badge，退出码不复用 HTTP status 语义；WorkspaceHints 已迁移创建 Card 与 creator/anchor 领域 Badge，保留原始 creator 和 Intent 优先展示；其余清单页面仍待迁移。',
    )
    expect(asyncRecord?.notes).toContain(
      'Skeleton 与 ErrorState 已完成组件验证；Login、ChangePassword 已迁移内联认证错误 Alert，ConsoleTimeout 已迁移加载 Spinner 与错误 Alert，Overview 已迁移加载 Alert/Spinner 与空态 EmptyState；Findings 已迁移 Skeleton、EmptyState、ErrorState/retry，并在后台刷新或失败时保留 stale rows；WorkspaceArtifacts 已区分列表初次/stale 状态、项目/筛选空态与 preview retry，并为项目访问、head/tail preview、截图错误和过期 Toast 增加 identity guard；Traffic 已区分 Projects/Runs/Traffic 三层初次、空态、stale error 与刷新状态，并为 project/run/evidence reconcile 和 clipboard Toast 增加 identity guard；WorkspaceHints 已迁移 EmptyState，并通过 pending token、project visit generation、POST response merge 和 stale Toast/reload 抑制隔离跨项目 mutation；其余异步状态仍待迁移。',
    )
    expect(asyncRecord?.notes).toContain(
      'ScenariosConsole、WorkersConsole、KnowledgeConsole 与 PromptGroupsConsole 已迁移加载/错误 Alert、Spinner 或编辑区 EmptyState；WorkerTemplatesPanel 已区分加载、失败与真实空态，并为删除增加 pending guard。',
    )
  })

  it('registers verified menu and popover primitives with the Workspace migration', () => {
    const record = componentMigrationRecords.find(
      (candidate) => candidate.id === 'menus-popovers-and-drawers',
    )

    expect(record).toMatchObject({
      migrationStatus: 'migrated',
      verification: ['component'],
      replacement: {
        files: [
          'src/components/ui/DropdownMenu.vue',
          'src/components/ui/Popover.vue',
          'src/components/ui/Tooltip.vue',
          'src/components/ui/Drawer.vue',
        ],
      },
    })
    expect(record?.notes).toContain(
      'Tooltip、Drawer、DropdownMenu 与 Popover primitive 已完成组件验证；ProjectWorkspace 已迁移项目工具 DropdownMenu，Projects 已迁移项目行操作 DropdownMenu，FrontLayout 已迁移账户 Popover 与项目组 DropdownMenu；VulnerabilityDrawer 已迁移 Cairn Drawer，保留详情证据、状态更新与 artifact 预览行为，并补齐加载、失败重试和 mutation 关闭保护；GraphDetailsDrawer 已迁移为单一 Cairn Drawer，保留节点、边、Evidence、Run、Artifact 与 Fact 重试行为，使用 inline Portal 兼容 Graph fullscreen，并移除旧 panel、header、close 与 body shell selector；声明的 legacy shell 已完成迁移，本次未执行 E2E、axe 或视觉验证。',
    )
  })

  it('registers the verified Tabs primitive and Dispatcher migration', () => {
    const record = componentMigrationRecords.find(
      (candidate) => candidate.id === 'tabs-and-local-navigation',
    )

    expect(record).toMatchObject({
      migrationStatus: 'migrated',
      verification: ['component'],
      replacement: {
        files: ['src/components/ui/Tabs.vue'],
      },
      pages: [
        'Findings',
        'ConsoleDispatcher',
        'ConsolePrompts',
        'WorkspaceRuns',
        'WorkspaceArtifacts',
      ],
    })
    expect(record?.notes).toContain(
      'Tabs primitive 已完成组件验证；ConsoleDispatcher 已迁移表单/YAML 模式并保留 dirty confirmation 与序列化行为；PromptGroups 已迁移文件 Tabs，保留未保存确认，并仅在异步读取成功后提交 selection；Runs 已迁移运行详情 Tabs，并完成扁平事件合同、轮询选择保留、project/run/artifact stale response guard、stdout/stderr/diagnostic 选择及 preview/content 下载语义的组件验证；WorkspaceArtifacts 已迁移交付产物/诊断日志本地 Tabs，并保留 category、kind 与 artifactId 深链合同；Findings 已迁移漏洞管理/报告管理 URL Tabs，报告审阅区使用渲染/原文子 Tabs；跨路由 ProjectWorkspace tabs 不属于本地状态 Tabs。',
    )
    expect(record?.notes).toContain('本次未执行 E2E、axe 或视觉验证。')
  })

  it('tracks the migrated typed application shell', () => {
    const record = componentMigrationRecords.find(
      (candidate) => candidate.id === 'application-shell',
    )

    expect(record).toMatchObject({
      migrationStatus: 'migrated',
      verification: ['unit', 'component', 'a11y', 'e2e'],
      replacement: {
        files: ['src/router/navigationRegistry.ts', 'src/layouts/AppSidebar.vue'],
      },
    })
    expect(record?.notes).toContain(
      'Playwright 已在 Desktop Chrome 与 Pixel 7 验证认证后 Shell、Command Palette 初始焦点/Escape/触发器焦点恢复及移动侧栏关闭与重新打开；登录页、Shell 与 404 页面通过 axe serious/critical 检查。',
    )
  })

  it('tracks the component-verified Graph and Runs migration', () => {
    const record = componentMigrationRecords.find((candidate) => candidate.id === 'graph-and-runs')

    expect(record).toMatchObject({
      migrationStatus: 'migrated',
      verification: ['component'],
      pages: ['WorkspaceGraph', 'WorkspaceRuns'],
    })
    expect(record?.replacement.files).toEqual([
      'src/features/graph/GraphPage.vue',
      'src/features/graph/GraphToolbar.vue',
      'src/features/graph/GraphPlaybackControls.vue',
      'src/features/graph/GraphDetailsDrawer.vue',
      'src/features/runs/RunsPage.vue',
    ])
    expect(record?.notes).toContain(
      'Runs 已修正真实 API DTO、详情请求归属、轮询终端选择、日志截断提示与 /content 下载；Graph 已拆分 Toolbar、Viewport、Playback 与 Details Drawer，迁移共享 Input、Checkbox、Button、IconButton、Badge、Tooltip、Drawer 与 inline Dialog，并保留专用 range、节点/边领域交互、fullscreen、retry、project hint、selection 与 playback 合同。',
    )
    expect(record?.notes).toContain(
      'Graph Dagre 布局、稳定定位、分支折叠、边路由、节点拖动、视口缩放平移与播放算法均未改动；本次未执行 E2E、axe 或视觉验证。',
    )
  })

  it('tracks the component-verified TextEditor fullscreen migration', () => {
    const record = componentMigrationRecords.find(
      (candidate) => candidate.id === 'master-detail-and-editor',
    )

    expect(record).toMatchObject({
      migrationStatus: 'migrated',
      verification: ['component'],
    })
    expect(record?.notes).toContain(
      'Console 私有 MasterDetail 与 TextEditor 已提升到 shared patterns，旧文件及旧导入已移除；TextEditor fullscreen 已迁移 Cairn Dialog，props、v-model、source/preview/split、双 Monaco、Markdown 与调用方 dirty/save 行为保持不变。',
    )
    expect(record?.notes).toContain('本次未执行 E2E、axe 或视觉验证。')
  })

  it('tracks the WorkspaceAudit shared components and async ownership migration', () => {
    const notesByRecord = Object.fromEntries(
      componentMigrationRecords.map((record) => [record.id, record.notes ?? []]),
    )

    expect(notesByRecord['buttons-and-form-controls']).toContain(
      'WorkspaceAudit 已迁移带可见标签的事件类型 FormField/Select，以及 Run、Fact、Artifact 和完整 payload 操作 Button；保留既有 deep-link query 合同。',
    )
    expect(notesByRecord['cards-badges-and-status']).toContain(
      'WorkspaceAudit 已迁移 KPI、事件、evidence 与完整 payload Card，并使用 event kind、unknown bucket、evidence validity 领域 Badge，未扩大全局状态语义。',
    )
    expect(notesByRecord['tables-and-lists']).toContain(
      'WorkspaceAudit 已迁移事件与 Fact/evidence 双 DataTable、事件 FilterBar、完整 payload 独立详情区域和 one-row-per-binding evidence model；保留稳定最新优先、四类 event filter、无搜索、分页、用户排序和页面 timer 合同。',
    )
    expect(notesByRecord['async-feedback']).toContain(
      'WorkspaceAudit 已区分 Runs、result events、aggregate evidence 三层初次加载、初次错误、真实空态、筛选空态、部分失败、后台刷新与 stale-data 错误；使用 project visit、parent detail refresh 与 immutable request snapshot 隔离跨项目和重叠 evidence response。',
    )
  })

  it('tracks the WorkspaceVulnerabilities list and request-ownership migration', () => {
    const notesByRecord = Object.fromEntries(
      componentMigrationRecords.map((record) => [record.id, record.notes ?? []]),
    )

    expect(notesByRecord['buttons-and-form-controls']).toContain(
      'WorkspaceVulnerabilities 已迁移 FilterBar、带可见标签的严重度/状态 Select 与 refresh/create Button；创建 confidence 继续保留原生 number input。',
    )
    expect(notesByRecord['cards-badges-and-status']).toContain(
      'WorkspaceVulnerabilities 已迁移列表 Card 与严重度、可信度、状态 Badge，并复用漏洞领域元数据；Drawer 私有徽标保持现有合同。',
    )
    expect(notesByRecord['tables-and-lists']).toContain(
      'WorkspaceVulnerabilities 已迁移 server-filtered DataTable 与本地 Drawer selection，保留 backend 顺序、10 秒轮询、完整正文和无 search、pagination、用户排序、vulnId route query 合同。',
    )
    expect(notesByRecord['async-feedback']).toContain(
      'WorkspaceVulnerabilities 已区分初次加载/错误、项目/筛选空态、后台刷新与 stale-data 错误；通过 project visit、immutable query snapshot、AbortSignal 和 captured mutation identity 隔离跨项目响应，并同步 workspace list、badge 与 global Findings。',
    )
  })
})
