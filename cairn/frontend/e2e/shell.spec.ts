import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

const session = {
  user_id: 'e2e-user',
  tenant_id: 'tenant-e2e',
  role: 'user' as const,
  must_change_password: false,
  csrf_token: 'e2e-csrf-token',
}

const monitoringSummary = {
  generated_at: '2026-08-22T00:00:00Z',
  long_running_threshold_seconds: 900,
  project_count: 0,
  active_project_count: 0,
  dispatcher: { status: 'idle', stale: false, updated_at: '2026-08-22T00:00:00Z' },
  queued_tasks: [],
  running_tasks: [],
  long_running_tasks: [],
  recent_failures: [],
  vulnerabilities: { total: 0, by_severity: {}, by_status: {}, per_project: [] },
}

const vulnerabilityDashboardSummary = {
  ...monitoringSummary,
  project_count: 1,
  active_project_count: 1,
  vulnerabilities: {
    total: 10,
    by_severity: { critical: 4, high: 3, medium: 2, info: 1 },
    by_status: {},
    per_project: [],
  },
}

const projectResourceConfig = {
  kinds: ['note', 'script', 'file'],
  limits: {
    max_active_count: 200,
    max_file_bytes: 10 * 1024 * 1024,
    max_active_total_bytes: 100 * 1024 * 1024,
    max_zip_upload_bytes: 50 * 1024 * 1024,
    max_zip_entries: 400,
    max_compression_ratio: 200,
    max_open_batches_per_user: 4,
    max_staged_bytes_per_user: 200 * 1024 * 1024,
    max_path_bytes: 1024,
    max_path_segment_bytes: 255,
  },
  reserved_names: ['manifest.json'],
  batch_ttl_seconds: 86400,
  zip_policy: {
    allowed_compression_methods: ['stored', 'deflated'],
    nested_archives: 'opaque',
    preserve_relative_directories: true,
  },
}

async function mockAuthenticatedShell(page: Page) {
  await page.route('**/auth/session', (route) => route.fulfill({ json: session }))
  await page.route('**/auth/csrf', (route) => route.fulfill({ json: session }))
  await page.route('**/projects', (route) => route.fulfill({ json: [] }))
  await page.route('**/monitoring/summary', (route) => route.fulfill({ json: monitoringSummary }))
  await page.route('**/project-resource-config', (route) =>
    route.fulfill({ json: projectResourceConfig }),
  )
}

async function mockVulnerabilityDashboard(page: Page) {
  await page.route('**/auth/session', (route) => route.fulfill({ json: session }))
  await page.route('**/auth/csrf', (route) => route.fulfill({ json: session }))
  await page.route('**/projects', (route) =>
    route.fulfill({ json: [{ id: 'project-e2e', title: 'E2E 项目', status: 'active' }] }),
  )
  await page.route('**/monitoring/summary', (route) =>
    route.fulfill({ json: vulnerabilityDashboardSummary }),
  )
}

async function expectNoSeriousAxeViolations(page: Page) {
  const results = await new AxeBuilder({ page }).analyze()
  const seriousViolations = results.violations.filter((violation) =>
    ['serious', 'critical'].includes(violation.impact || ''),
  )
  expect(seriousViolations).toEqual([])
}

test('redirects an anonymous protected visit to the login page @a11y', async ({ page }) => {
  await page.route('**/auth/session', (route) =>
    route.fulfill({ status: 401, json: { detail: 'Not authenticated' } }),
  )

  await page.goto('/#/projects')

  await expect(page).toHaveURL(/#\/login/)
  await expect(page).toHaveTitle('Arena')
  await expect(page.getByText('Arena', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '登录' })).toBeVisible()
  await expect(page.getByRole('textbox', { name: '登录名' })).toBeVisible()
  await expect(page.getByRole('textbox', { name: '密码' })).toBeVisible()
  await expectNoSeriousAxeViolations(page)
})

test('preserves shell navigation, command palette focus, 403, and 404 contracts @a11y', async ({
  page,
}) => {
  await mockAuthenticatedShell(page)
  await page.goto('/#/')

  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await expect(page).toHaveTitle('Arena')
  await expect(page.locator('nav[aria-label="应用导航"]')).toBeVisible()
  await expect(page.locator('aside[aria-label="主导航"]').getByText('Arena')).toBeVisible()
  await expect(page.getByText('未分组', { exact: true })).toHaveCount(0)
  await expect(page.locator('a[href*="group=ungrouped"]')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '欢迎使用 Arena' })).toBeVisible()

  const commandTrigger = page.getByRole('button', { name: /搜索/ })
  await commandTrigger.focus()
  await page.keyboard.press('ControlOrMeta+K')
  const commandDialog = page.getByRole('dialog', { name: '命令面板' })
  const commandInput = page.getByPlaceholder('搜索功能、项目、运行...')
  await expect(commandDialog).toBeVisible()
  await expect(commandInput).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(commandDialog).toBeHidden()
  await expect(commandTrigger).toBeFocused()

  await page.goto('/#/monitoring')
  await expect(page).toHaveURL(/#\/forbidden/)
  await expect(page.getByRole('heading', { name: '无权访问此页面' })).toBeVisible()

  await page.goto('/#/missing-e2e-route')
  await expect(page.getByRole('heading', { name: '页面不存在' })).toBeVisible()
  await expectNoSeriousAxeViolations(page)
})

test('preserves an explicit dark theme and persists theme changes @a11y', async ({ page }) => {
  await page.addInitScript(() => {
    if (localStorage.getItem('cairn.theme') === null) localStorage.setItem('cairn.theme', 'dark')
  })
  await mockAuthenticatedShell(page)

  await page.goto('/#/')

  const root = page.locator('html')
  await expect(root).toHaveAttribute('data-theme', 'dark')
  await expect(page.getByRole('button', { name: '切换到浅色主题' })).toBeVisible()
  await expectNoSeriousAxeViolations(page)

  await page.reload()
  await expect(root).toHaveAttribute('data-theme', 'dark')

  await page.getByRole('button', { name: '切换到浅色主题' }).click()
  await expect(root).toHaveAttribute('data-theme', 'light')
  await expect.poll(() => page.evaluate(() => localStorage.getItem('cairn.theme'))).toBe('light')

  await page.reload()
  await expect(root).toHaveAttribute('data-theme', 'light')
})

test('animates and exposes the vulnerability severity distribution @a11y', async ({
  page,
}, testInfo) => {
  await mockVulnerabilityDashboard(page)
  await page.goto('/#/')
  if (testInfo.project.name === 'mobile-chromium') {
    await page.getByRole('button', { name: '关闭主导航' }).click()
  }

  const criticalSegment = page.getByRole('button', { name: '严重：4 个，占 40.0%' })
  const criticalLegend = page.locator('.vuln-legend-row').filter({ hasText: '严重' })
  await criticalLegend.hover()
  await expect(criticalSegment).toHaveClass(/is-active/)
  await expect(criticalSegment).not.toHaveCSS('transform', 'none')
  await expect(page.getByRole('tooltip')).toContainText('4 个严重 · 40.0%')
  await expect(page.locator('.vuln-chart-center')).toContainText('4严重')
  await page.mouse.move(0, 0)

  const highLegend = page.locator('.vuln-legend-row').filter({ hasText: '高危' })
  await highLegend.focus()
  await page.keyboard.press('Enter')
  await expect(highLegend).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('tooltip')).toContainText('3 个高危 · 30.0%')

  await page.keyboard.press('Escape')
  await expect(page.getByRole('tooltip')).toHaveCount(0)
  await expect(page.locator('.vuln-chart-center')).toContainText('10漏洞总数')
  await expectNoSeriousAxeViolations(page)
})

test('disables severity segment displacement when reduced motion is requested', async ({
  page,
}, testInfo) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await mockVulnerabilityDashboard(page)
  await page.goto('/#/')
  if (testInfo.project.name === 'mobile-chromium') {
    await page.getByRole('button', { name: '关闭主导航' }).click()
  }

  const criticalSegment = page.getByRole('button', { name: '严重：4 个，占 40.0%' })
  const criticalLegend = page.locator('.vuln-legend-row').filter({ hasText: '严重' })
  await criticalLegend.hover()
  await expect(criticalSegment).toHaveClass(/is-active/)
  await expect(criticalSegment).toHaveCSS('transform', 'none')
})

test('creates an ordinary project without any group input', async ({ page }) => {
  let createPayload: Record<string, unknown> | null = null
  await page.route('**/auth/session', (route) => route.fulfill({ json: session }))
  await page.route('**/auth/csrf', (route) => route.fulfill({ json: session }))
  await page.route('**/projects', async (route) => {
    if (route.request().method() === 'POST') {
      createPayload = route.request().postDataJSON() as Record<string, unknown>
      await route.fulfill({ json: { project: { id: 'project-created-e2e' } } })
      return
    }
    await route.fulfill({ json: [] })
  })
  await page.route('**/monitoring/summary', (route) => route.fulfill({ json: monitoringSummary }))
  await page.route('**/project-resource-config', (route) =>
    route.fulfill({ json: projectResourceConfig }),
  )
  await page.route('**/scenario-catalog', (route) =>
    route.fulfill({
      json: [
        {
          key: 'default',
          label: '默认场景',
          is_default: true,
          origin_template: 'https://example.test',
          goal_template: '验证目标',
        },
      ],
    }),
  )
  await page.route('**/tenant-workers/options', (route) =>
    route.fulfill({
      json: [
        {
          id: 'worker-e2e',
          display_name: 'E2E Worker',
          model_id: 'model-e2e',
          model_display_name: 'Claude',
          task_types: ['reason'],
          available: true,
        },
      ],
    }),
  )
  await page.route('**/library-catalog', (route) => route.fulfill({ json: [] }))
  await page.route('**/worker-templates', (route) => route.fulfill({ json: [] }))
  await page.route('**/scenarios', (route) =>
    route.fulfill({ json: [{ key: 'default', label: '默认场景' }] }),
  )

  await page.goto('/#/projects')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  await expect(page.getByRole('dialog', { name: '新建项目', exact: true })).toBeVisible()
  await page.locator('#np-title').fill('分组创建合同')

  const createButton = page.getByRole('button', { name: '创建并启动' })
  await expect(page.locator('#np-group-select')).toHaveCount(0)
  await expect(createButton).toBeEnabled()
  await createButton.click()

  await expect.poll(() => createPayload).not.toBeNull()
  expect(createPayload).not.toHaveProperty('group_id')
})

test('renders shared Select portals with opaque light and dark surfaces', async ({ page }) => {
  await mockAuthenticatedShell(page)
  await page.route('**/scenario-catalog', (route) =>
    route.fulfill({
      json: [
        {
          key: 'default',
          label: '默认场景',
          is_default: true,
          origin_template: 'https://example.test',
          goal_template: '验证目标',
        },
      ],
    }),
  )
  await page.route('**/tenant-workers/options', (route) =>
    route.fulfill({
      json: [
        {
          id: 'worker-e2e',
          display_name: 'E2E Worker',
          model_id: 'model-e2e',
          model_display_name: 'Claude',
          task_types: ['reason'],
          available: true,
        },
      ],
    }),
  )
  await page.route('**/library-catalog', (route) => route.fulfill({ json: [] }))
  await page.route('**/worker-templates', (route) => route.fulfill({ json: [] }))
  await page.route('**/scenarios', (route) =>
    route.fulfill({ json: [{ key: 'default', label: '默认场景' }] }),
  )

  await page.goto('/#/projects')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  await page.locator('#np-execution-trigger').click()
  await page.locator('#np-worker-template').click()

  const content = page.locator('.ui-select__content')
  await expect(content).toBeVisible()
  await expect(content).toHaveCSS('background-color', 'rgb(255, 255, 255)')

  await page.keyboard.press('Escape')
  await page
    .getByRole('dialog', { name: '新建项目', exact: true })
    .getByRole('button', { name: '关闭', exact: true })
    .click()
  await page.getByRole('button', { name: '切换到深色主题' }).click()
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  await page.locator('#np-execution-trigger').click()
  await page.locator('#np-worker-template').click()
  await expect(content).toBeVisible()
  await expect(content).toHaveCSS('background-color', 'rgb(23, 25, 30)')
})

test('keeps the mobile sidebar operable at Pixel 7 width', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile-chromium', '仅验证移动端侧栏合同')
  await mockAuthenticatedShell(page)
  await page.goto('/#/')

  const sidebar = page.locator('aside[aria-label="主导航"]')
  await expect(sidebar).toBeVisible()
  await page.getByRole('button', { name: '关闭主导航' }).click()
  await expect(sidebar).toHaveCSS('transform', /matrix\(1, 0, 0, 1, -/)

  await page.getByRole('button', { name: /折叠\/展开侧栏/ }).click()
  await expect(sidebar).toHaveCSS('transform', 'matrix(1, 0, 0, 1, 0, 0)')
})
