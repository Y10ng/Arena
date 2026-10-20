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

  it('keeps five workspace destinations and sends retired URLs to NotFound', () => {
    expect(
      navigationRegistry
        .filter((item) => item.workspace)
        .sort((a, b) => a.workspace!.order - b.workspace!.order)
        .map((item) => item.routeName),
    ).toEqual([
      'WorkspaceGraph',
      'WorkspaceRuns',
      'WorkspaceVulnerabilities',
      'WorkspaceArtifacts',
      'WorkspaceHints',
    ])
    for (const path of ['targets', 'provenance', 'coverage', 'payloads']) {
      expect(router.resolve(`/projects/project-a/${path}`).name).toBe('NotFound')
    }
    expect(router.resolve('/projects/project-a').name).toBe('WorkspaceGraph')
    expect(router.resolve('/projects/project-a/runs?runId=run-a').name).toBe('WorkspaceRuns')
    expect(router.resolve('/projects/project-a/vulnerabilities?vulnId=vuln-a').name).toBe(
      'WorkspaceVulnerabilities',
    )
    expect(router.resolve('/projects/project-a/artifacts?artifactId=artifact-a').name).toBe(
      'WorkspaceArtifacts',
    )
    expect(router.resolve('/projects/project-a/hints').name).toBe('WorkspaceHints')
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
      'ConsoleModels',
      'ConsoleWorkers',
      'VerificationConfiguration',
    ])
    expect(sidebarNavigation('scenario', false)).toEqual([])
    expect(sidebarNavigation('standalone', false)).toEqual([])

    const tenantCommands = commandItemsFromRegistry(false)
    expect(tenantCommands.some((item) => item.id === 'go:Monitoring')).toBe(false)
    expect(tenantCommands.some((item) => item.id === 'go:ConsoleUsers')).toBe(false)
    expect(tenantCommands.some((item) => item.id === 'go:ConsoleModels')).toBe(true)

    const adminCommands = commandItemsFromRegistry(true)
    expect(adminCommands.some((item) => item.id === 'go:Monitoring')).toBe(true)
    expect(adminCommands.some((item) => item.id === 'go:ConsoleUsers')).toBe(true)
  })

  it('exposes vulnerabilities and reports as independent authenticated primary routes', async () => {
    for (const isAdmin of [false, true]) {
      const primary = sidebarNavigation('primary', isAdmin)
      expect(
        primary
          .filter((item) => ['Findings', 'Reports'].includes(item.routeName))
          .map((item) => item.label),
      ).toEqual(['漏洞', '报告'])
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
    expect(
      router.resolve('/projects/new').matched.some((route) => Boolean(route.redirect)),
    ).toBe(false)
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
    expect(commandItemsFromRegistry(true).some((item) => item.id === 'go:WorkspaceRuns')).toBe(
      false,
    )

    const commands = commandItemsFromRegistry(true, 'project-a')
    const runs = commands.find((item) => item.id === 'go:WorkspaceRuns')
    expect(runs?.to).toEqual({ name: 'WorkspaceRuns', params: { id: 'project-a' } })
  })

  it('builds semantic breadcrumb ancestors for front, workspace, console, and status routes', () => {
    expect(breadcrumbsFromRegistry('Findings').map((crumb) => crumb.text)).toEqual([
      'Arena',
      '漏洞',
    ])
    expect(
      breadcrumbsFromRegistry('WorkspaceRuns', 'project-a', 'Project A').map((crumb) => crumb.text),
    ).toEqual(['Arena', '任务管理', 'Project A', '运行日志'])
    expect(
      breadcrumbsFromRegistry('WorkspaceGraph', 'project-a').map((crumb) => crumb.text),
    ).toEqual(['Arena', '任务管理', 'project-a'])
    expect(breadcrumbsFromRegistry('ConsoleDispatcher').map((crumb) => crumb.text)).toEqual([
      'Arena',
      '系统管理',
      'Dispatcher',
    ])
    expect(breadcrumbsFromRegistry('Monitoring').map((crumb) => crumb.text)).toEqual([
      'Arena',
      '监控',
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
