import { expect, test } from '@playwright/test'

test('knowledge tree searches collapsed libraries without replacing the reader', async ({ page }) => {
  const unexpected: string[] = []
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.setViewportSize({ width: 1920, height: 1080 })
  // 页面资源来自隔离的前端服务；所有 API 都在浏览器拦截，不访问实际资料或执行写入。
  await page.route('**/*', async (route) => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()
    if (path === '/' || path.startsWith('/static/') || path === '/favicon.ico') {
      await route.continue()
      return
    }
    const session = { user_id: 'ui-check', tenant_id: 'ui-check', role: 'super_admin', must_change_password: false, csrf_token: 'ui-check' }
    const files = {
      'library-a': [{ name: 'first.md', path: 'first.md', size: 12, warnings: [] }],
      'library-b': [{ name: 'hidden.md', path: 'docs/deep/hidden.md', size: 12, warnings: [] }],
    }
    const sets = Object.entries(files).map(([key, entries]) => ({ key, path: `library://${key}`, builtin: false, file_count: entries.length, referenced_by: [] }))
    const catalog = sets.map((set) => ({ key: set.key, name: set.key === 'library-a' ? '资料 A' : '资料 B', latest_version: 1, file_count: 1 }))
    let json: unknown
    if (path === '/auth/session' || path === '/auth/csrf') json = session
    else if (method === 'GET' && path === '/knowledge') json = sets
    else if (method === 'GET' && path === '/library-catalog') json = catalog
    else if (method === 'GET' && path === '/knowledge/library-a/files') json = files['library-a']
    else if (method === 'GET' && path === '/knowledge/library-b/files') json = files['library-b']
    else if (method === 'GET' && path === '/knowledge/library-a/files/first.md') json = { ...files['library-a'][0], content: '# 可见正文\n\n这是受控样例。' }
    else if (method === 'GET' && /^\/libraries\/library-[ab]\/versions$/.test(path)) json = []
    else {
      unexpected.push(`${method} ${path}`)
      await route.fulfill({ status: 503, json: { detail: '未配置的受控接口' } })
      return
    }
    await route.fulfill({ json })
  })

  await page.goto('/#/console/knowledge')
  const tree = page.getByRole('navigation', { name: '资料库文件树' })
  const reader = page.getByRole('region', { name: '资料阅读' })
  await expect(tree).toBeVisible()
  await page.getByRole('button', { name: '展开资料库 资料 A' }).click()
  await tree.getByRole('button', { name: 'first.md', exact: true }).click()
  await expect(reader.getByRole('heading', { name: '可见正文' })).toBeVisible()
  await page.getByRole('button', { name: '展开资料库 资料 B' }).click()
  await expect(reader.getByRole('heading', { name: '可见正文' })).toBeVisible()
  await page.getByRole('button', { name: '收起资料库 资料 B' }).click()
  await page.getByRole('textbox', { name: '搜索资料库、文件名或路径' }).fill('hidden.md')
  await expect(tree.getByRole('button', { name: 'hidden.md', exact: true })).toBeVisible()
  await expect(tree.getByRole('button', { name: 'docs', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '收起目录 docs', exact: true }).click()
  await expect(tree.getByRole('button', { name: 'hidden.md', exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '展开目录 docs', exact: true }).click()
  await expect(tree.getByRole('button', { name: 'hidden.md', exact: true })).toBeVisible()
  await expect(reader.getByRole('heading', { name: '可见正文' })).toBeVisible()
  const geometry = await page.evaluate(() => {
    const tree = document.querySelector('.knowledge-navigation')!.getBoundingClientRect()
    const reader = document.querySelector('.knowledge-reader')!.getBoundingClientRect()
    return { viewport: [innerWidth, innerHeight], scale: getComputedStyle(document.documentElement).getPropertyValue('--ui-scale').trim(), treeRight: tree.right, readerLeft: reader.left }
  })
  expect(geometry.viewport).toEqual([1920, 1080])
  expect(Number(geometry.scale)).toBe(0.9)
  expect(geometry.treeRight).toBeLessThanOrEqual(geometry.readerLeft)
  await page.screenshot({ path: test.info().outputPath('knowledge-search.png') })
  expect(pageErrors).toEqual([])
  expect(unexpected).toEqual([])
})
