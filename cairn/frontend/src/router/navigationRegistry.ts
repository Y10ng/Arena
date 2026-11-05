import type { RouteLocationRaw, RouteRecordNameGeneric } from 'vue-router'
import type { CommandItem } from '@/lib/commandPalette'

export const productionRouteNames = [
  'Login',
  'ChangePassword',
  'Overview',
  'Agent',
  'Findings',
  'Traffic',
  'Reports',
  'Projects',
  'ProjectSchedules',
  'ProjectSchedule',
  'Verifications',
  'NewVerification',
  'VerificationDetail',
  'VerificationIntegrations',
  'VerificationConfiguration',
  'WorkspaceOverview',
  'WorkspaceGraph',
  'WorkspaceAgent',
  'WorkspaceRuns',
  'WorkspaceHints',
  'WorkspaceArtifacts',
  'WorkspaceVulnerabilities',
  'WorkspaceTraffic',
  'Monitoring',
  'ConsoleWorkers',
  'ConsoleUsers',
  'ConsoleScenarios',
  'ConsolePrompts',
  'ConsoleKnowledge',
  'ConsoleReportTemplates',
  'Forbidden',
  'NotFound',
] as const

export type ProductionRouteName = (typeof productionRouteNames)[number]
export type NavigationIcon =
  | 'dashboard'
  | 'finding'
  | 'projects'
  | 'add'
  | 'graph'
  | 'runs'
  | 'hints'
  | 'artifacts'
  | 'vulnerabilities'
  | 'monitoring'
  | 'workers'
  | 'users'
  | 'scenarios'
  | 'prompts'
  | 'knowledge'
  | 'status'
  | 'auth'
  | 'schedules'

export type NavigationRole = 'authenticated' | 'super-admin'
export type SidebarPlacement = 'primary' | 'results' | 'support' | 'system'

interface CommandMetadata {
  kindLabel: string
  title: string
  subtitle: string
  badge: string
  keywords: readonly string[]
}

export interface NavigationRecord {
  routeName: ProductionRouteName
  label: string
  icon: NavigationIcon
  role: NavigationRole
  breadcrumbParent?: ProductionRouteName | 'workspace-project' | 'console-root'
  workspace?: { order: number }
  sidebar?: {
    placement: SidebarPlacement
    order: number
  }
  command?: CommandMetadata
}

const record = (value: NavigationRecord): NavigationRecord => value

export const navigationRegistry = [
  record({
    routeName: 'Agent', label: 'ChatBot', icon: 'hints', role: 'authenticated',
    sidebar: { placement: 'primary', order: 30 },
    command: { kindLabel: '导航', title: '打开 ChatBot', subtitle: '持续对话、读取授权材料与下载已有成果', badge: 'ChatBot', keywords: ['agent', 'chatbot', '对话', '会话'] },
  }),
  record({ routeName: 'Login', label: '登录', icon: 'auth', role: 'authenticated' }),
  record({ routeName: 'ChangePassword', label: '修改密码', icon: 'auth', role: 'authenticated' }),
  record({
    routeName: 'Overview',
    label: '仪表盘',
    icon: 'dashboard',
    role: 'authenticated',
    sidebar: { placement: 'primary', order: 20 },
    command: {
      kindLabel: '导航',
      title: '转到仪表盘',
      subtitle: '查看调度状态、近期失败与活动流',
      badge: '仪表盘',
      keywords: ['dashboard', 'home', 'main', '仪表盘'],
    },
  }),
  record({
    routeName: 'Findings',
    label: '漏洞',
    icon: 'finding',
    role: 'authenticated',
    sidebar: { placement: 'results', order: 10 },
    command: {
      kindLabel: '导航',
      title: '转到漏洞',
      subtitle: '查看跨项目漏洞、证据与复测',
      badge: '漏洞',
      keywords: ['vulnerability', 'vuln', '漏洞', 'finding'],
    },
  }),
  record({
    routeName: 'Traffic', label: '流量', icon: 'monitoring', role: 'authenticated',
    sidebar: { placement: 'results', order: 40 },
    command: {
      kindLabel: '导航', title: '转到流量', subtitle: '查看授权项目的 HTTP 流量',
      badge: '流量', keywords: ['traffic', 'http', '流量'],
    },
  }),
  record({
    routeName: 'Reports',
    label: '报告',
    icon: 'artifacts',
    role: 'authenticated',
    sidebar: { placement: 'results', order: 30 },
    command: {
      kindLabel: '导航',
      title: '转到报告',
      subtitle: '阅读与下载已完成的项目评估报告',
      badge: '报告',
      keywords: ['report', 'markdown', '报告'],
    },
  }),
  record({
    routeName: 'Projects',
    label: '项目',
    icon: 'projects',
    role: 'authenticated',
    sidebar: { placement: 'primary', order: 10 },
    command: {
      kindLabel: '导航',
      title: '转到项目',
      subtitle: '列出并搜索所有安全探测任务',
      badge: '项目',
      keywords: ['list', 'detail', 'project'],
    },
  }),
  record({
    routeName: 'ProjectSchedules', label: '计划', icon: 'schedules', role: 'authenticated',
    sidebar: { placement: 'support', order: 30 },
    command: { kindLabel: '导航', title: '转到计划', subtitle: '查看项目启动安排与执行记录', badge: '计划', keywords: ['schedule', '计划', '预约'] },
  }),
  record({ routeName: 'ProjectSchedule', label: '计划详情', icon: 'schedules', role: 'authenticated', breadcrumbParent: 'ProjectSchedules' }),
  record({
    routeName: 'Verifications',
    label: '复测',
    icon: 'vulnerabilities',
    role: 'authenticated',
    sidebar: { placement: 'results', order: 20 },
    command: {
      kindLabel: '导航', title: '转到复测',
      subtitle: '提交漏洞资料并查看独立复测、证据与历史',
      badge: '复测', keywords: ['复现', '复测', 'verification', 'retest'],
    },
  }),
  record({
    routeName: 'NewVerification', label: '新建复测', icon: 'add',
    role: 'authenticated', breadcrumbParent: 'Verifications',
  }),
  record({
    routeName: 'VerificationDetail', label: '复测详情', icon: 'vulnerabilities',
    role: 'authenticated', breadcrumbParent: 'Verifications',
  }),
  record({
    routeName: 'VerificationIntegrations', label: '漏洞复测配置', icon: 'auth',
    role: 'authenticated', breadcrumbParent: 'console-root',
  }),
  record({
    routeName: 'VerificationConfiguration', label: '漏洞复测配置', icon: 'auth',
    role: 'authenticated', breadcrumbParent: 'console-root',
    sidebar: { placement: 'system', order: 25 },
    command: {
      kindLabel: '系统管理', title: '漏洞复测配置',
      subtitle: '配置个人复测模型、名称与启停',
      badge: '复测', keywords: ['模型', '执行', '复测', 'verification', '配置'],
    },
  }),
  record({
    routeName: 'WorkspaceOverview', label: '总览', icon: 'dashboard', role: 'authenticated',
    breadcrumbParent: 'workspace-project', workspace: { order: 0 },
    command: { kindLabel: '当前项目', title: '查看项目总览', subtitle: '查看原始目标、资料与累计 Token', badge: '总览', keywords: ['overview', '资料', '目标'] },
  }),
  // 仅登记已有地址的全局重定向，不生成项目 tab 或命令。
  record({ routeName: 'WorkspaceAgent', label: 'ChatBot', icon: 'hints', role: 'authenticated' }),
  record({
    routeName: 'WorkspaceGraph',
    label: '图谱',
    icon: 'graph',
    role: 'authenticated',
    breadcrumbParent: 'workspace-project',
    workspace: { order: 40 },
    command: {
      kindLabel: '当前项目',
      title: '查看探索图谱',
      subtitle: '查看 Fact / Intent 拓扑图',
      badge: 'Graph',
      keywords: ['topology', 'nodes'],
    },
  }),
  record({
    routeName: 'WorkspaceRuns',
    label: '运行日志',
    icon: 'runs',
    role: 'authenticated',
    breadcrumbParent: 'workspace-project',
    workspace: { order: 20 },
    command: {
      kindLabel: '当前项目',
      title: '查看运行终端',
      subtitle: '观察任务执行与实时 transcript SSE 输出',
      badge: 'Runs',
      keywords: ['terminal', 'logs'],
    },
  }),
  record({
    routeName: 'WorkspaceHints',
    label: '人工提示',
    icon: 'hints',
    role: 'authenticated',
    breadcrumbParent: 'workspace-project',
    workspace: { order: 60 },
    command: {
      kindLabel: '当前项目',
      title: '查看并添加提示',
      subtitle: '给 Agent 补充操作指南',
      badge: 'Hints',
      keywords: ['tips', 'guidance'],
    },
  }),
  record({
    routeName: 'WorkspaceVulnerabilities',
    label: '漏洞成果',
    icon: 'vulnerabilities',
    role: 'authenticated',
    breadcrumbParent: 'workspace-project',
    workspace: { order: 10 },
    command: {
      kindLabel: '当前项目',
      title: '查看漏洞成果',
      subtitle: '阅读项目漏洞与来源证据',
      badge: 'Vulnerabilities',
      keywords: ['漏洞', 'vulnerability', 'packet'],
    },
  }),
  record({
    routeName: 'WorkspaceTraffic', label: '流量', icon: 'monitoring', role: 'authenticated',
    breadcrumbParent: 'workspace-project', workspace: { order: 50 },
    command: {
      kindLabel: '当前项目', title: '查看项目流量', subtitle: '阅读采集交互与关联漏洞',
      badge: '流量', keywords: ['traffic', 'http', '流量'],
    },
  }),
  record({
    routeName: 'WorkspaceArtifacts',
    label: '文件',
    icon: 'artifacts',
    role: 'authenticated',
    breadcrumbParent: 'workspace-project',
    workspace: { order: 30 },
    command: {
      kindLabel: '当前项目',
      title: '浏览研究文件',
      subtitle: '只读浏览研究目录与已保存原件',
      badge: '文件',
      keywords: ['evidence', 'files', '文件', '目录'],
    },
  }),
  record({
    routeName: 'Monitoring',
    label: '运行监控',
    sidebar: { placement: 'system', order: 50 },
    icon: 'monitoring',
    role: 'super-admin',
    command: {
      kindLabel: '导航',
      title: '转到全局监控大屏',
      subtitle: '查看Worker健康、任务队列及错误分析',
      badge: 'Monitoring',
      keywords: ['workers', 'charts', 'scheduler'],
    },
  }),
  record({
    routeName: 'ConsoleWorkers',
    label: '模型与 Worker',
    icon: 'workers',
    role: 'authenticated',
    breadcrumbParent: 'console-root',
    sidebar: { placement: 'system', order: 20 },
    command: {
      kindLabel: '系统管理',
      title: '模型与 Worker',
      subtitle: '管理个人模型、Worker 与运行设置',
      badge: 'Workers',
      keywords: ['worker', 'model', 'pi', '模型', '运行设置'],
    },
  }),
  record({
    routeName: 'ConsoleUsers',
    label: '用户管理',
    icon: 'users',
    role: 'super-admin',
    breadcrumbParent: 'console-root',
    sidebar: { placement: 'system', order: 30 },
    command: {
      kindLabel: '系统管理',
      title: '管理用户',
      subtitle: '创建、禁用账户并管理密码与会话',
      badge: 'Users',
      keywords: ['user', 'account', 'session', '用户'],
    },
  }),
  record({
    routeName: 'ConsoleScenarios',
    label: '探索场景',
    icon: 'scenarios',
    role: 'super-admin',
    breadcrumbParent: 'console-root',
    sidebar: { placement: 'support', order: 20 },
    command: {
      kindLabel: '系统管理',
      title: '管理探索场景',
      subtitle: '配置新建项目使用的 Scenario',
      badge: 'Scenarios',
      keywords: ['scenario'],
    },
  }),
  record({
    routeName: 'ConsolePrompts',
    label: '提示词',
    icon: 'prompts',
    role: 'super-admin',
    breadcrumbParent: 'console-root',
    sidebar: { placement: 'system', order: 40 },
    command: {
      kindLabel: '系统管理',
      title: '管理 Prompt Groups',
      subtitle: '编辑 Agent 使用的提示词模板文件',
      badge: 'Prompts',
      keywords: ['prompt', 'template'],
    },
  }),
  record({
    routeName: 'ConsoleKnowledge',
    label: '资料库',
    icon: 'knowledge',
    role: 'super-admin',
    breadcrumbParent: 'console-root',
    sidebar: { placement: 'support', order: 10 },
    command: {
      kindLabel: '系统管理',
      title: '管理资料库',
      subtitle: '维护可复用的笔记、脚本与系统文档',
      badge: 'Library',
      keywords: ['library', 'knowledge', 'files', '资料库'],
    },
  }),
  record({
    routeName: 'ConsoleReportTemplates',
    label: '报告模板',
    icon: 'artifacts',
    role: 'super-admin',
    breadcrumbParent: 'console-root',
    sidebar: { placement: 'system', order: 45 },
    command: {
      kindLabel: '系统管理',
      title: '管理报告模板',
      subtitle: '创建不可变报告模板版本并管理模板归档',
      badge: 'Report Templates',
      keywords: ['report', 'template', 'markdown', '报告模板'],
    },
  }),
  record({ routeName: 'Forbidden', label: '无权访问', icon: 'status', role: 'authenticated' }),
  record({ routeName: 'NotFound', label: '页面不存在', icon: 'status', role: 'authenticated' }),
] as const satisfies readonly NavigationRecord[]

export const navigationByRouteName = new Map<ProductionRouteName, NavigationRecord>(
  navigationRegistry.map((item) => [item.routeName, item]),
)

export function isProductionRouteName(
  name: RouteRecordNameGeneric | null | undefined,
): name is ProductionRouteName {
  return typeof name === 'string' && navigationByRouteName.has(name as ProductionRouteName)
}

export function navigationVisibleToRole(item: NavigationRecord, isSuperAdmin: boolean): boolean {
  return item.role !== 'super-admin' || isSuperAdmin
}

export function sidebarNavigation(
  placement: SidebarPlacement,
  isSuperAdmin: boolean,
): NavigationRecord[] {
  return navigationRegistry
    .filter(
      (item) =>
        item.sidebar?.placement === placement && navigationVisibleToRole(item, isSuperAdmin),
    )
    .sort((left, right) => (left.sidebar?.order || 0) - (right.sidebar?.order || 0))
}

export function routeLocation(
  routeName: ProductionRouteName,
  projectId?: string,
): RouteLocationRaw {
  return projectId ? { name: routeName, params: { id: projectId } } : { name: routeName }
}

export function commandItemsFromRegistry(
  isSuperAdmin: boolean,
  projectId?: string,
): Array<CommandItem & { to: RouteLocationRaw }> {
  return navigationRegistry
    .filter((item) => item.command && navigationVisibleToRole(item, isSuperAdmin))
    .filter((item) => !item.workspace || Boolean(projectId))
    .sort((left, right) => Number(Boolean(left.workspace)) - Number(Boolean(right.workspace)))
    .map((item) => ({
      id: `go:${item.routeName}`,
      kindLabel: item.command?.kindLabel,
      title: item.command?.title,
      subtitle: item.command?.subtitle,
      badge: item.command?.badge,
      keywords: item.command ? [...item.command.keywords] : [],
      superAdminOnly: item.role === 'super-admin',
      to: routeLocation(item.routeName, item.workspace ? projectId : undefined),
    }))
}

export interface BreadcrumbItem {
  key: string
  text: string
  to?: RouteLocationRaw
  current: boolean
}

export function breadcrumbsFromRegistry(
  routeName: RouteRecordNameGeneric | null | undefined,
  projectId?: string,
  projectTitle?: string,
): BreadcrumbItem[] {
  if (!isProductionRouteName(routeName)) return []
  const item = navigationByRouteName.get(routeName)
  if (!item || routeName === 'Login' || routeName === 'ChangePassword') return []

  const crumbs: Omit<BreadcrumbItem, 'current'>[] = [
    { key: 'root', text: 'Arena', to: { name: 'Projects' } },
  ]

  if (item.breadcrumbParent === 'ProjectSchedules') {
    crumbs.push({ key: 'schedules', text: navigationByRouteName.get('ProjectSchedules')!.label, to: { name: 'ProjectSchedules' } })
  } else if (item.breadcrumbParent === 'Projects') {
    crumbs.push({
      key: 'projects',
      text: navigationByRouteName.get('Projects')?.label || '项目',
      to: { name: 'Projects' },
    })
  } else if (item.breadcrumbParent === 'Verifications') {
    crumbs.push({ key: 'verifications', text: navigationByRouteName.get('Verifications')!.label, to: { name: 'Verifications' } })
  } else if (item.breadcrumbParent === 'workspace-project' && projectId) {
    crumbs.push({
      key: 'projects',
      text: navigationByRouteName.get('Projects')?.label || '项目',
      to: { name: 'Projects' },
    })
    crumbs.push({
      key: 'project',
      text: projectTitle || projectId,
      to: { path: `/projects/${projectId}` },
    })
  } else if (item.breadcrumbParent === 'console-root') {
    crumbs.push({ key: 'console', text: '系统管理', to: { name: 'ConsoleWorkers' } })
  }

  crumbs.push({ key: routeName, text: item.label })

  return crumbs.map((crumb, index) => ({ ...crumb, current: index === crumbs.length - 1 }))
}
