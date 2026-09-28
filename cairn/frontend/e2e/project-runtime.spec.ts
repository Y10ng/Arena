import { expect, test, type Locator, type Page } from '@playwright/test'

const now = '2026-09-17T12:00:00Z'
const originalMessage = '执行进程已启动；活动记录与模型进展需分别确认'
const longError =
  '请求契约失败：binding.review_input_digest。' +
  '完整诊断证据需要保留，不代表模型正在产出。'.repeat(35)
const project = {
  id: 'runtime-ui',
  title: '合成项目名称'.repeat(18),
  status: 'active',
  created_at: now,
  scenario_key: 'default',
  tenant_id: 'synthetic',
  tenant_name: '合成 Tenant',
  tenant_workers: [],
  assessment_protocol: 'web-assessment-v1',
  fact_count: 0,
  intent_count: 0,
  working_intent_count: 0,
  unclaimed_intent_count: 0,
  hint_count: 0,
  report_capability: { supported: false, policy_version: 0, template_profile: null, reason: null },
}
const runtime = {
  schema_version: 1,
  project_id: project.id,
  observed_at: now,
  execution_generation: 1,
  project_status: 'active',
  state: 'running',
  code: 'process_started',
  message: originalMessage,
  dispatcher: { status: 'unknown', freshness: 'stale', updated_at: now },
  scheduling: { code: 'dispatched', observed_at: now, retry_at: null },
  active_runs: [],
  last_activity: null,
  last_error: {
    code: 'submission_invalid',
    message: longError,
    task_run_id: 'run-00',
    recorded_at: now,
    submission: {
      code: 'submission_invalid',
      issue: { keyword: 'type', path: '/data/fact/description' },
      submission_attempts: 3,
      repair_rounds: 2,
      max_repair_rounds: 2,
    },
  },
  actions: {
    pause: { allowed: true, reason: null as string | null },
    resume: { allowed: false, reason: 'project_status_invalid' as string | null },
    reauthorize: { allowed: false, reason: 'project_status_invalid' as string | null },
    reopen: { allowed: false, reason: 'project_status_invalid' as string | null },
    finish: { allowed: true, reason: null as string | null },
  },
}

async function mockRuntimeApp(
  page: Page,
  options: { unavailable?: boolean; empty?: boolean; paused?: boolean; resumeReady?: boolean; rejectResume?: boolean } = {},
) {
  const metadata = { ...project, status: options.paused ? 'paused' : 'active' }
  const snapshot: typeof runtime = {
    ...runtime,
    project_status: metadata.status,
    ...(options.paused
      ? {
          state: 'pausing',
          code: 'project_pause_draining',
          message: '正在暂停，等待旧执行停止确认。',
          actions: {
            ...runtime.actions,
            pause: { allowed: false, reason: 'project_not_active' },
            resume: { allowed: false, reason: 'project_pause_draining' },
          },
        }
      : {}),
  }
  const confirmPause = () => {
    snapshot.state = 'paused'
    snapshot.code = 'project_paused'
    snapshot.message = '评估已暂停，已保存的进度和证据保留'
    snapshot.dispatcher = { status: 'idle', freshness: 'fresh', updated_at: now }
    snapshot.actions.resume = { allowed: true, reason: null }
  }
  if (options.resumeReady) confirmPause()
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('requestfailed', (request) =>
    console.error('request failed', request.url(), request.failure()?.errorText),
  )
  const unexpected: string[] = []
  const posts: string[] = []
  await page.clock.setFixedTime(new Date(now))
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (
      url.origin === new URL(test.info().project.use.baseURL!).origin &&
      (url.pathname.startsWith('/static/') ||
        ['document', 'script', 'stylesheet', 'font', 'image'].includes(request.resourceType()))
    ) {
      await route.continue()
      return
    }
    const path = url.pathname
    let json: unknown
    if (path === '/auth/session' || path === '/auth/csrf')
      json = {
        user_id: 'synthetic',
        tenant_id: 'synthetic',
        role: 'user',
        must_change_password: false,
        csrf_token: 'synthetic-only',
      }
    else if (path === '/projects') json = [metadata]
    else if (path === '/projects/runtime') json = { items: [snapshot] }
    else if (
      path === '/project-groups' ||
      path === '/scenarios' ||
      path === `/projects/${project.id}/vulnerabilities`
    )
      json = []
    else if (path === `/projects/${project.id}`)
      json = {
        project: metadata,
        facts: [],
        intents: [],
        hints: [],
        targets: [],
        resources: [],
        library_bindings: [],
      }
    else if (path === `/projects/${project.id}/runtime`) {
      await route.fulfill({
        status: options.unavailable ? 503 : 200,
        json: options.unavailable ? { detail: 'runtime_unavailable' } : snapshot,
      })
      return
    } else if (path === `/projects/${project.id}/pause` && request.method() === 'POST') {
      posts.push(path)
      metadata.status = 'paused'
      snapshot.project_status = 'paused'
      snapshot.state = 'pausing'
      snapshot.code = 'project_pause_draining'
      snapshot.message = '正在暂停，等待旧执行停止确认。'
      snapshot.actions.pause = { allowed: false, reason: 'project_not_active' }
      snapshot.actions.resume = { allowed: false, reason: 'project_pause_draining' }
      json = { ...metadata }
    } else if (path === `/projects/${project.id}/resume` && request.method() === 'POST') {
      posts.push(path)
      if (options.rejectResume) {
        await route.fulfill({ status: 409, json: { detail: 'project_pause_draining' } })
        return
      }
      metadata.status = 'active'
      snapshot.project_status = 'active'
      snapshot.state = 'waiting'
      snapshot.code = 'waiting_dispatch'
      snapshot.message = '等待 Dispatcher 派发。'
      snapshot.actions.pause = { allowed: true, reason: null }
      snapshot.actions.resume = { allowed: false, reason: 'project_status_invalid' }
      json = { ...metadata }
    } else if (path === `/projects/${project.id}/task-runs`)
      json = options.empty
        ? []
        : Array.from({ length: 35 }, (_, i) => ({
            id: `run-${String(i).padStart(2, '0')}`,
            project_id: project.id,
            task_type: 'reason',
            worker_name: `合成 Worker ${i}`,
            worker_type: 'pi',
            status: 'completed',
            created_at: now,
            started_at: now,
            ended_at: now,
          }))
    else if (
      /^\/projects\/runtime-ui\/task-runs\/(command-events|hook-events|prompt-events|run-\d+\/agent-events)$/.test(
        path,
      ) ||
      path === `/projects/${project.id}/artifacts`
    )
      json = []
    else {
      unexpected.push(`${request.method()} ${request.url()}`)
      await route.abort('blockedbyclient')
      return
    }
    await route.fulfill({ json })
  })
  return { unexpected, posts, pageErrors, confirmPause }
}

async function closeMobileNavigation(page: Page) {
  await expect(page.locator('.workspace-header')).toBeVisible()
  const closeNav = page.getByRole('button', { name: '关闭主导航', exact: true })
  if (
    (await closeNav.isVisible()) &&
    (await closeNav.evaluate((element) => {
      const rect = element.getBoundingClientRect()
      return rect.left >= 0 && rect.top >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight
    }))
  ) {
    await closeNav.click()
  }
}

async function expectHitTarget(locator: Locator) {
  await expect(locator).toBeVisible()
  expect(
    await locator.evaluate((element) => {
      const rect = element.getBoundingClientRect()
      const x = rect.x + rect.width / 2
      const y = rect.y + rect.height / 2
      for (let parent = element.parentElement; parent; parent = parent.parentElement) {
        const style = getComputedStyle(parent)
        const bounds = parent.getBoundingClientRect()
        if (
          /(auto|scroll|hidden|clip)/.test(style.overflowX) &&
          (rect.left < bounds.left - 1 || rect.right > bounds.right + 1)
        )
          return false
        if (
          /(auto|scroll|hidden|clip)/.test(style.overflowY) &&
          (rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1)
        )
          return false
      }
      return (
        rect.width > 0 &&
        rect.height > 0 &&
        rect.left >= 0 &&
        rect.top >= 0 &&
        rect.right <= innerWidth + 1 &&
        rect.bottom <= innerHeight + 1 &&
        element.contains(document.elementFromPoint(x, y))
      )
    }),
  ).toBe(true)
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1280, height: 720 },
  { width: 900, height: 700 },
  { width: 390, height: 667 },
]) {
  test(`runtime details do not consume run-list space ${viewport.width}x${viewport.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport)
    const mock = await mockRuntimeApp(page)
    await page.goto('/static/#/projects/runtime-ui/runs')
    await expect(page.locator('.run-row')).toHaveCount(35)
    const closeNav = page.getByRole('button', { name: '关闭主导航', exact: true })
    if (await closeNav.isVisible()) await closeNav.click()
    const list = page.locator('.runs-list')
    const listBox = await list.boundingBox()
    expect(listBox!.height).toBeGreaterThanOrEqual(150)
    expect(listBox!.y + listBox!.height).toBeLessThanOrEqual(viewport.height + 1)
    const header = page.locator('.workspace-header')
    await expect(header).toContainText('执行已领取')
    await expect(header).toContainText('Dispatcher 连接未及时更新')
    await expect(header).not.toContainText(longError)
    await expect(header.getByRole('button', { name: '运行诊断' })).toHaveCount(0)
    await expectHitTarget(page.getByRole('button', { name: '暂停项目', exact: true }))
    const titleBox = await page.locator('.header-left').boundingBox()
    const actionsBox = await page.locator('.workspace-header .header-right').boundingBox()
    expect(
      titleBox!.x + titleBox!.width <= actionsBox!.x + 1 ||
        titleBox!.y + titleBox!.height <= actionsBox!.y + 1,
    ).toBe(true)
    await list.evaluate((el) => {
      el.scrollTop = el.scrollHeight
    })
    await expectHitTarget(page.locator('.run-row').last())
    await page.locator('.run-row').last().click()
    await expect(page.locator('.run-row').last()).toHaveClass(/active/)
    expect(mock.unexpected).toEqual([])
    expect(mock.pageErrors).toEqual([])
  })

  test(`confirmed pause exposes usable resume action ${viewport.width}x${viewport.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport)
    const mock = await mockRuntimeApp(page, { paused: true, resumeReady: true, empty: true })
    await page.goto('/static/#/projects/runtime-ui/runs')
    const header = page.locator('.workspace-header')
    await expect(header).toContainText('已暂停')
    const closeNav = page.getByRole('button', { name: '关闭主导航', exact: true })
    if (await closeNav.isVisible()) await closeNav.click()
    await expect(header).toContainText('评估已暂停，已保存的进度和证据保留')
    await expect(header).not.toContainText('运行诊断')
    await expect(header).not.toContainText(longError)
    const resume = page.getByRole('button', { name: '继续项目', exact: true })
    await expect(resume).toBeEnabled()
    await expectHitTarget(resume)
    await resume.click()
    await expect.poll(() => mock.posts).toEqual([`/projects/${project.id}/resume`])
    expect(mock.unexpected).toEqual([])
    expect(mock.pageErrors).toEqual([])
  })

  test(`project list keeps status and menu targets independent ${viewport.width}x${viewport.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport)
    const mock = await mockRuntimeApp(page, { paused: true })
    await page.goto('/static/#/projects')
    await expect(page.locator('.project-row')).toHaveCount(1)
    const closeNav = page.getByRole('button', { name: '关闭主导航', exact: true })
    if (await closeNav.isVisible()) await closeNav.click()
    const row = page.locator('.project-row')
    await expect(row).toContainText('正在暂停')
    await expect(row).toContainText('正在暂停，等待旧执行停止确认。')
    await expect(row.getByRole('button', { name: '运行诊断' })).toHaveCount(0)
    expect(await row.locator('button button').count()).toBe(0)
    const menu = row.getByRole('button', { name: '项目操作菜单' })
    await menu.scrollIntoViewIfNeeded()
    await expectHitTarget(menu)
    await menu.click()
    const resume = page.getByRole('menuitem', { name: '继续项目', exact: true })
    await expect(resume).toBeVisible()
    await expect(resume).not.toHaveAttribute('data-disabled', '')
    await page.keyboard.press('Escape')
    expect(mock.unexpected).toEqual([])
    expect(mock.pageErrors).toEqual([])
  })
}

test('pause request remains pausing until cleanup confirms it, then resumes the same project', async ({ page }) => {
  const mock = await mockRuntimeApp(page, { empty: true })
  await page.goto('/static/#/projects/runtime-ui/runs')
  await closeMobileNavigation(page)
  await page.getByRole('button', { name: '暂停项目', exact: true }).click()
  const header = page.locator('.workspace-header')
  await expect(header).toContainText('正在暂停')
  await expect(header).not.toContainText('已暂停')
  await expect(page.getByRole('button', { name: '继续项目', exact: true })).toBeEnabled()
  expect(mock.posts).toEqual([`/projects/${project.id}/pause`])

  mock.confirmPause()
  await page.reload()
  await expect(header).toContainText('已暂停')
  await closeMobileNavigation(page)
  await page.getByRole('button', { name: '继续项目', exact: true }).click()
  await expect.poll(() => mock.posts).toEqual([
    `/projects/${project.id}/pause`,
    `/projects/${project.id}/resume`,
  ])
  expect(mock.unexpected).toEqual([])
  expect(mock.pageErrors).toEqual([])
})

test('paused project reaches the action API when status query fails and shows rejection', async ({ page }) => {
  const mock = await mockRuntimeApp(page, { paused: true, unavailable: true, empty: true, rejectResume: true })
  await page.goto('/static/#/projects/runtime-ui/runs')
  const header = page.locator('.workspace-header')
  await expect(header).toContainText('状态暂未更新')
  await closeMobileNavigation(page)
  await page.getByRole('button', { name: '继续项目', exact: true }).click()
  await expect.poll(() => mock.posts).toEqual([`/projects/${project.id}/resume`])
  await expect(page.getByText('项目操作失败').first()).toBeVisible()
  await expect(page.locator('.workspace-layout')).toContainText('旧执行仍在取消或清理中')
  await expect(header).not.toContainText('已暂停')
  expect(mock.unexpected).toEqual([])
  expect(mock.pageErrors).toEqual([])
})

test('active project can request pause when status query fails', async ({ page }) => {
  const mock = await mockRuntimeApp(page, { unavailable: true, empty: true })
  await page.goto('/static/#/projects/runtime-ui/runs')
  await expect(page.locator('.workspace-header')).toContainText('状态暂未更新')
  await closeMobileNavigation(page)
  await page.getByRole('button', { name: '暂停项目', exact: true }).click()
  await expect.poll(() => mock.posts.length).toBe(1)
  await expect(page.locator('.runs-sidebar')).toContainText('没有运行记录')
  await expect(page.locator('.runs-sidebar')).not.toContainText('状态暂未更新')
  expect(mock.unexpected).toEqual([])
})
