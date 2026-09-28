import { describe, expect, it } from 'vitest'
import { routePageInventory } from '@/migration/componentMigration'
import router from './index'

const namedRouteNames = () =>
  router
    .getRoutes()
    .map((route) => route.name)
    .filter((name): name is string => typeof name === 'string')
    .sort()

const inventoryRouteNames = () => routePageInventory.map((page) => page.routeName).sort()

async function routeComponentFile(routeName: string): Promise<string | undefined> {
  const route = router.getRoutes().find((candidate) => candidate.name === routeName)
  const source = route?.components?.default
  if (!source) return undefined

  const loaded =
    typeof source === 'function' ? await (source as () => unknown | Promise<unknown>)() : source
  const component =
    loaded && typeof loaded === 'object' && 'default' in loaded ? loaded.default : loaded

  return component && (typeof component === 'object' || typeof component === 'function')
    ? (component as { __file?: string }).__file
    : undefined
}

describe('frontend route contracts', () => {
  it('keeps named production routes aligned with the migration inventory', () => {
    expect(namedRouteNames()).toEqual(inventoryRouteNames())
  })

  it('keeps each inventory file aligned with its production route component', async () => {
    Object.defineProperty(document, 'queryCommandSupported', {
      configurable: true,
      value: () => false,
    })

    await Promise.all(
      routePageInventory.map(async (page) => {
        const componentFile = (await routeComponentFile(page.routeName))?.replaceAll('\\', '/')
        expect(componentFile, page.routeName).toBeTruthy()
        expect(componentFile?.endsWith(page.file), page.routeName).toBe(true)
      }),
    )
  }, 30_000)

  it('resolves authenticated status pages inside FrontLayout without privileged meta', () => {
    const forbidden = router.resolve('/forbidden')
    const notFound = router.resolve('/missing/deep/path?source=test')

    expect(forbidden.name).toBe('Forbidden')
    expect(notFound.name).toBe('NotFound')

    for (const resolved of [forbidden, notFound]) {
      expect(resolved.matched).toHaveLength(2)
      expect(resolved.matched[0]?.path).toBe('/')
      expect(resolved.matched.some((record) => record.meta.public === true)).toBe(false)
      expect(resolved.matched.some((record) => record.meta.superAdmin === true)).toBe(false)
    }
  })

  it('does not let the catch-all consume existing auth or workspace routes', () => {
    expect(router.resolve('/login').name).toBe('Login')
    expect(router.resolve('/change-password').name).toBe('ChangePassword')
    expect(router.resolve('/projects/project-a/vulnerabilities').name).toBe(
      'WorkspaceVulnerabilities',
    )
  })
})
