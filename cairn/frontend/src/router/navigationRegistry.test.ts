// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { routePageInventory } from '@/migration/componentMigration'
import router from './index'
import {
  breadcrumbsFromRegistry,
  commandItemsFromRegistry,
  navigationRegistry,
  productionRouteNames,
  sidebarNavigation,
} from './navigationRegistry'

const sorted = (values: readonly string[]) => [...values].sort()

describe('navigation registry', () => {
  it('labels the research browser as 文件 while retaining Artifact deep links', () => {
    expect(navigationRegistry.find(item => item.routeName === 'WorkspaceArtifacts')?.label).toBe('文件')
    const route = router.resolve('/projects/project-a/artifacts?artifactId=original-a')
    expect(route.name).toBe('WorkspaceArtifacts')
    expect(route.query.artifactId).toBe('original-a')
  })

  it('covers production router and migration inventory names exactly once', () => {
    const routerNames = router
      .getRoutes()
      .map((route) => route.name)
      .filter((name): name is string => typeof name === 'string')
    const inventoryNames = routePageInventory.map((page) => page.routeName)
    const registryNames = navigationRegistry.map((item) => item.routeName)

    expect(new Set(registryNames).size).toBe(registryNames.length)
    expect(sorted(registryNames)).toEqual(sorted(productionRouteNames))
    expect(sorted(registryNames)).toEqual(sorted(routerNames))
    expect(sorted(registryNames)).toEqual(sorted(inventoryNames))
  })

  it('keeps explicit workspace deep links and redirects the project entry to overview', () => {
    expect(
      navigationRegistry
        .filter((item) => item.workspace)
        .sort((a, b) => a.workspace!.order - b.workspace!.order)
        .map((item) => item.routeName),
    ).toEqual([
      'WorkspaceOverview',
      'WorkspaceVulnerabilities',
      'WorkspaceRuns',
      'WorkspaceArtifacts',
      'WorkspaceGraph',
      'WorkspaceTraffic',
      'WorkspaceHints',
    ])
    for (const path of ['targets', 'provenance', 'coverage', 'payloads']) {
      expect(router.resolve(`/projects/project-a/${path}`).name).toBe('NotFound')
    }
    const entry = router.resolve('/projects/project-a?vulnId=vuln-a')
    const redirect = entry.matched.at(-1)?.redirect
    if (typeof redirect !== 'function') throw new Error('缺少项目入口重定向')
    const destination = router.resolve(redirect(entry, router.currentRoute.value))
    expect(destination.name).toBe('WorkspaceOverview')
    expect(destination.params.id).toBe('project-a')
    expect(destination.query).toEqual({ vulnId: 'vuln-a' })
    expect(router.resolve({ name: 'WorkspaceGraph', params: { id: 'project-a' } }).path).toBe('/projects/project-a/graph')
    expect(router.resolve('/projects/project-a/graph?factId=fact-a').name).toBe('WorkspaceGraph')
    expect(router.resolve('/projects/project-a/runs?runId=run-a').name).toBe('WorkspaceRuns')
    expect(router.resolve('/projects/project-a/vulnerabilities?vulnId=vuln-a').name).toBe(
      'WorkspaceVulnerabilities',
    )
    expect(router.resolve('/projects/project-a/artifacts?artifactId=artifact-a').name).toBe(
      'WorkspaceArtifacts',
    )
    expect(router.resolve('/projects/project-a/hints').name).toBe('WorkspaceHints')
  })

  it('retains only the old Agent redirect without a project tab or command', () => {
    const legacy = navigationRegistry.find(item => item.routeName === 'WorkspaceAgent')!
    expect(legacy.workspace).toBeUndefined()
    expect(legacy.command).toBeUndefined()
    const route = router.resolve('/projects/project-a/agent?conversation=conversation-a')
    const redirect = route.matched.at(-1)?.redirect
    if (typeof redirect !== 'function') throw new Error('缺少 Agent 重定向')
    expect(redirect(route, router.currentRoute.value)).toEqual({
      name: 'Agent', query: { project: 'project-a', conversation: 'conversation-a' },
    })
  })

  it.each<[string, string, string | undefined, Record<string, string>]>([
    ['/projects/project-a/agent?sessionId=session-a', 'WorkspaceAgent', 'project-a', { sessionId: 'session-a' }],
    ['/projects/project-a/traffic?runId=run-a', 'WorkspaceTraffic', 'project-a', { runId: 'run-a' }],
    ['/project-schedules?status=active', 'ProjectSchedules', undefined, { status: 'active' }],
    ['/project-schedules/schedule-a', 'ProjectSchedule', 'schedule-a', {}],
    ['/verifications?status=completed', 'Verifications', undefined, { status: 'completed' }],
    ['/verifications/new', 'NewVerification', undefined, {}],
    ['/verifications/verification-a?tab=evidence', 'VerificationDetail', 'verification-a', { tab: 'evidence' }],
    ['/traffic?projectId=project-a&runId=run-a', 'Traffic', undefined, { projectId: 'project-a', runId: 'run-a' }],
    ['/console/verification-configuration?profile=personal', 'VerificationConfiguration', undefined, { profile: 'personal' }],
  ])('preserves the destination and query of %s', (path, name, id, query) => {
    const route = router.resolve(path)
    expect(route.name).toBe(name)
    expect(route.params.id).toBe(id)
    expect(route.query).toEqual(query)
    expect(route.meta.superAdmin).not.toBe(true)
  })

  it('preserves queries on the retained verification configuration redirect', () => {
    const route = router.resolve('/console/verification-integrations?profile=personal')
    expect(route.name).toBe('VerificationIntegrations')
    const redirect = route.matched.at(-1)?.redirect
    expect(typeof redirect).toBe('function')
    if (typeof redirect !== 'function') throw new Error('缺少复测配置重定向')
    expect(redirect(route, router.currentRoute.value)).toEqual({
      name: 'VerificationConfiguration', query: { profile: 'personal' },
    })
    expect(routePageInventory.find((page) => page.routeName === route.name)?.file).toBe('src/router/index.ts')
  })

  it('sends retired global configuration and provenance URLs to NotFound', () => {
    for (const path of ['/provenance', '/console/models', '/console/dispatcher', '/console/timeout']) {
      expect(router.resolve(path).name).toBe('NotFound')
    }
    expect(router.getRoutes().find((route) => route.path === '/console')?.redirect).toBe('/console/workers')
  })

  it('keeps tenant and super-admin navigation visibility aligned with route meta', () => {
    const privilegedRegistryNames = navigationRegistry
      .filter((item) => item.role === 'super-admin')
      .map((item) => item.routeName)
      .sort()
    const privilegedRouterNames = router
      .getRoutes()
      .filter((route) => route.meta.superAdmin === true)
      .map((route) => String(route.name))
      .sort()
    expect(privilegedRegistryNames).toEqual(privilegedRouterNames)

    expect(sidebarNavigation('system', false).map((item) => item.routeName)).toEqual([
      'ConsoleWorkers',
      'VerificationConfiguration',
    ])
    expect(sidebarNavigation('support', false).map((item) => item.routeName)).toEqual([
      'ProjectSchedules',
    ])
    expect(sidebarNavigation('support', true).map((item) => item.routeName)).toEqual([
      'ConsoleKnowledge', 'ConsoleScenarios', 'ProjectSchedules',
    ])
    expect(sidebarNavigation('system', true).map((item) => item.routeName)).toEqual([
      'ConsoleWorkers', 'VerificationConfiguration', 'ConsoleUsers', 'ConsolePrompts',
      'ConsoleReportTemplates', 'Monitoring',
    ])
    for (const placement of ['primary', 'results', 'support', 'system'] as const) {
      expect(sidebarNavigation(placement, false).every((item) => item.role === 'authenticated')).toBe(true)
    }

    const tenantCommands = commandItemsFromRegistry(false)
    expect(tenantCommands.some((item) => item.id === 'go:Monitoring')).toBe(false)
    expect(tenantCommands.some((item) => item.id === 'go:ConsoleUsers')).toBe(false)
    expect(tenantCommands.some((item) => item.id === 'go:ConsoleWorkers')).toBe(true)

    const adminCommands = commandItemsFromRegistry(true)
    expect(adminCommands.some((item) => item.id === 'go:Monitoring')).toBe(true)
    expect(adminCommands.some((item) => item.id === 'go:ConsoleUsers')).toBe(true)
  })

  it('separates primary work from authenticated results without changing report access', async () => {
    for (const isAdmin of [false, true]) {
      expect(sidebarNavigation('primary', isAdmin).map((item) => [item.routeName, item.label])).toEqual([
        ['Projects', '项目'], ['Overview', '仪表盘'], ['Agent', 'ChatBot'],
      ])
      expect(sidebarNavigation('results', isAdmin).map((item) => [item.routeName, item.label])).toEqual([
        ['Findings', '漏洞'], ['Verifications', '复测'], ['Reports', '报告'], ['Traffic', '流量'],
      ])
      const commands = commandItemsFromRegistry(isAdmin)
      expect(commands.find((item) => item.id === 'go:Reports')?.to).toEqual({ name: 'Reports' })
      expect(commands.some((item) => item.id === 'go:ConsoleReportTemplates')).toBe(isAdmin)
    }
    const route = router.resolve('/reports?reportProjectId=project-a&reportId=artifact-a')
    expect(route.name).toBe('Reports')
    expect(route.meta.superAdmin).not.toBe(true)
    const record = router.getRoutes().find((item) => item.name === 'Reports')!
    const component = await (
      record.components!.default as () => Promise<{ default: { __file: string } }>
    )()
    expect(component.default.__file).toMatch(/features\/findings\/ReportManagement.vue$/)
    expect(routePageInventory.find((item) => item.routeName === 'Reports')?.file).toBe(
      'src/features/findings/ReportManagement.vue',
    )
    expect(breadcrumbsFromRegistry('Reports').map((crumb) => crumb.text)).toEqual(['Arena', '报告'])
    expect(router.resolve('/console/report-templates').meta.superAdmin).toBe(true)
  })

  it('keeps ordinary creation inside task management instead of global navigation or redirects', () => {
    expect(router.getRoutes().some((route) => route.name === 'NewProject')).toBe(false)
    expect(router.resolve('/projects/new').params.id).toBe('new')
    expect(router.getRoutes().some((route) => route.path === '/projects/new')).toBe(false)
    for (const isAdmin of [false, true]) {
      expect(
        sidebarNavigation('primary', isAdmin).some((item) => String(item.routeName) === 'NewProject'),
      ).toBe(false)
      expect(commandItemsFromRegistry(isAdmin).some((item) => item.id === 'go:NewProject')).toBe(false)
      expect(commandItemsFromRegistry(isAdmin).find((item) => item.id === 'go:Projects')?.to).toEqual({
        name: 'Projects',
      })
    }
  })

  it('adds workspace commands only with project context and uses named destinations', () => {
    for (const isAdmin of [false, true]) {
      const globalCommands = commandItemsFromRegistry(isAdmin)
      const projectCommands = commandItemsFromRegistry(isAdmin, 'project-a')
      for (const item of navigationRegistry.filter((record) => record.workspace)) {
        expect(globalCommands.some((command) => command.id === `go:${item.routeName}`)).toBe(false)
        const command = projectCommands.find((command) => command.id === `go:${item.routeName}`)
        expect(command?.to).toEqual({ name: item.routeName, params: { id: 'project-a' } })
        expect(router.resolve(command!.to).params.id).toBe('project-a')
      }
    }
  })

  it('R3 builds breadcrumbs with a project entry and an explicit graph location', () => {
    expect(breadcrumbsFromRegistry('Findings').map((crumb) => crumb.text)).toEqual([
      'Arena',
      '漏洞',
    ])
    expect(
      breadcrumbsFromRegistry('WorkspaceRuns', 'project-a', 'Project A').map((crumb) => crumb.text),
    ).toEqual(['Arena', '项目', 'Project A', '运行日志'])
    expect(
      breadcrumbsFromRegistry('WorkspaceGraph', 'project-a').map((crumb) => crumb.text),
    ).toEqual(['Arena', '项目', 'project-a', '图谱'])
    expect(breadcrumbsFromRegistry('WorkspaceRuns', 'project-a')[2]?.to).toEqual({ path: '/projects/project-a' })
    expect(breadcrumbsFromRegistry('WorkspaceVulnerabilities', 'project-a').at(-1)?.text).toBe('漏洞成果')
    expect(breadcrumbsFromRegistry('ConsoleWorkers').map((crumb) => crumb.text)).toEqual([
      'Arena',
      '系统管理',
      '模型与 Worker',
    ])
    expect(breadcrumbsFromRegistry('Monitoring').map((crumb) => crumb.text)).toEqual([
      'Arena',
      '运行监控',
    ])
    expect(breadcrumbsFromRegistry('Forbidden').map((crumb) => crumb.text)).toEqual([
      'Arena',
      '无权访问',
    ])
    expect(breadcrumbsFromRegistry('NotFound').map((crumb) => crumb.text)).toEqual([
      'Arena',
      '页面不存在',
    ])
  })
})
