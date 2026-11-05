import { expect, test, type Page } from '@playwright/test'

const resourceConfig = {
  kinds: ['note', 'script', 'file'],
  limits: {
    max_active_count: 200,
    max_file_bytes: 10485760,
    max_active_total_bytes: 104857600,
    max_zip_upload_bytes: 52428800,
    max_zip_entries: 400,
    max_compression_ratio: 200,
    max_open_batches_per_user: 4,
    max_staged_bytes_per_user: 209715200,
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

async function mockCreation(page: Page, createStatus = 422) {
  const requests: string[] = []
  // 仅页面资源来自被测前端；所有 API 在浏览器内拦截，避免创建、上传或模型费用。
  await page.route('**/*', async (route) => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()
    if (path === '/' || path.startsWith('/static/') || path === '/favicon.ico') {
      await route.continue()
      return
    }
    requests.push(`${method} ${path}`)
    const responses: Record<string, unknown> = {
      '/auth/session': {
        user_id: 'ui-check',
        tenant_id: 'ui-check',
        role: 'user',
        must_change_password: false,
        csrf_token: 'ui-check',
      },
      '/auth/csrf': {
        user_id: 'ui-check',
        tenant_id: 'ui-check',
        role: 'user',
        must_change_password: false,
        csrf_token: 'ui-check',
      },
      '/projects': [],
      '/scenarios': [{ key: 'goal-driven-v1', label: '目标驱动探索' }],
      '/scenario-catalog': [
        {
          key: 'goal-driven-v1',
          label: '目标驱动探索',
          is_default: true,
          origin_template: '已知业务背景',
          goal_template: '验证授权范围内的问题',
        },
      ],
      '/library-catalog': [
        { key: 'reference-check', name: '接口参考资料', latest_version: 1, file_count: 2 },
      ],
      '/library-catalog/reference-check/manifest': {
        library_key: 'reference-check', library_name: '接口参考资料', version: 1,
        digest_sha256: 'a'.repeat(64), file_count: 2, selection_proof: 'controlled-proof',
        files: ['guide.md', 'docs/intro.md'].map((path) => ({ path, size_bytes: 1, sha256: 'b'.repeat(64) })),
      },
      '/project-resource-config': resourceConfig,
      '/monitoring/summary': {
        project_count: 0,
        active_project_count: 0,
        queued_tasks: [],
        running_tasks: [],
        recent_failures: [],
        vulnerabilities: { total: 0, by_severity: {}, by_status: {}, per_project: [] },
      },
    }
    if (path === '/projects' && method === 'POST') {
      if (createStatus === 0) await route.abort('connectionfailed')
      else await route.fulfill({ status: createStatus, json: { detail: '受控字段拒绝' } })
      return
    }
    if (path in responses && (method === 'GET' || path === '/auth/csrf')) {
      await route.fulfill({ json: responses[path] })
      return
    }
    await route.fulfill({ status: 503, json: { detail: `未配置的受控接口：${method} ${path}` } })
  })
  return requests
}

async function refreshCreationQueries(page: Page) {
  // 模拟页面再次可见，驱动真实 QueryClient 刷新，不重挂载当前草稿。
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    window.dispatchEvent(new Event('visibilitychange'))
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    window.dispatchEvent(new Event('visibilitychange'))
  })
}

async function discardCreation(page: Page) {
  await page
    .getByRole('dialog', { name: '新建项目', exact: true })
    .getByRole('button', { name: '取消', exact: true })
    .click()
  const confirm = page.getByRole('alertdialog', { name: '放弃本次填写？' })
  if (await confirm.isVisible()) await confirm.getByRole('button', { name: '放弃并关闭' }).click()
  await expect(page.getByRole('dialog', { name: '新建项目', exact: true })).not.toBeVisible()
}

test('creation HTTP-equivalent randomUUID absence and optional cache failure still send one Origin request per mode', async ({ page }) => {
  await mockCreation(page)
  await page.addInitScript(() => {
    // 等效普通 HTTP 缺失 Secure Context UUID API；随机源仍使用浏览器 getRandomValues。
    Object.defineProperty(crypto, 'randomUUID', { configurable: true, value: undefined })
    const setItem = Storage.prototype.setItem
    Storage.prototype.setItem = function (key, value) {
      if (key === 'arena.creation-request') throw new DOMException('受控缓存拒绝', 'QuotaExceededError')
      return setItem.call(this, key, value)
    }
  })
  const submitted: Array<{ path: string; payload: Record<string, unknown> }> = []
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST' && (path === '/projects' || path === '/project-schedules'))
      submitted.push({ path, payload: request.postDataJSON() })
  })
  await page.route('**/project-schedules', (route) => route.request().method() === 'POST'
    ? route.fulfill({ status: 422, json: { detail: '受控 Origin 计划拒绝' } })
    : route.fulfill({ json: [] }))
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.goto('/#/projects')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  const dialog = page.getByRole('dialog', { name: '新建项目', exact: true })
  await page.locator('#np-title').fill('受控 Origin 提交')
  await page.locator('#np-origin-input').fill('  仅核验已有资料\n排除任何外部访问  ')
  await page.locator('#np-goal-input').fill('保留用户目标')
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeEnabled()
  await expect(dialog.getByLabel('连接方式', { exact: true })).toContainText('直接连接')
  expect(await page.evaluate(() => typeof crypto.randomUUID)).toBe('undefined')
  await dialog.getByRole('button', { name: '创建并启动' }).click()
  await expect(dialog.getByText('受控字段拒绝', { exact: true })).toBeVisible()
  await expect(dialog.getByText(/跨刷新查询入口未保存/)).toBeVisible()
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeEnabled()
  expect(submitted).toHaveLength(1)
  for (const [index, mode, label] of [[1, 'once', '单次定时'], [2, 'daily', '每日重复'], [3, 'weekly', '每周重复']] as const) {
    await dialog.locator(`.schedule-mode-options label:has(input[value="${mode}"])`).click()
    await expect(dialog.getByRole('radio', { name: label, exact: true })).toBeChecked()
    await page.locator('#schedule-timezone').fill('Asia/Shanghai')
    await page.locator('#schedule-start').fill('2099-06-01T09:00')
    await expect(dialog.locator('#np-targets')).toHaveCount(0)
    await expect(dialog.getByLabel('连接方式', { exact: true })).toContainText('直接连接')
    await dialog.getByRole('button', { name: mode === 'once' ? '保存单次预约' : '保存重复计划', exact: true }).click()
    await expect(dialog.getByText('受控 Origin 计划拒绝', { exact: true })).toBeVisible()
    expect(submitted).toHaveLength(index + 1)
    expect(submitted[index]!.payload.mode).toBe(mode)
    await expect(dialog.getByRole('button', { name: mode === 'once' ? '保存单次预约' : '保存重复计划', exact: true })).toBeEnabled()
  }
  for (const { path, payload } of submitted) {
    const project = path === '/projects' ? payload : payload.project
    expect(project).not.toHaveProperty('targets')
    expect(project).not.toHaveProperty('tenant_worker_ids')
    expect(project).toMatchObject({ origin: '  仅核验已有资料\n排除任何外部访问  ', goal: '保留用户目标' })
    expect(payload.request_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  }
  expect(new Set(submitted.map(({ payload }) => payload.request_id)).size).toBe(4)
  await page.screenshot({ path: test.info().outputPath('origin-creation-cache-warning.png'), fullPage: true })
  expect(pageErrors).toEqual([])
})

test('creation uses fixed tenant Workers without selection and preserves independent proxy input in every mode', async ({ page }) => {
  const requests = await mockCreation(page)
  const immediate: Array<Record<string, unknown>> = []
  const schedules: Array<Record<string, unknown>> = []
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/projects' && request.method() === 'POST')
      immediate.push(request.postDataJSON())
  })
  await page.route('**/project-schedules', async (route) => {
    if (route.request().method() === 'POST') {
      schedules.push(route.request().postDataJSON())
      await route.fulfill({ status: 422, json: { detail: '受控计划拒绝' } })
    } else await route.fulfill({ json: [] })
  })
  await page.goto('/#/projects')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  const dialog = page.getByRole('dialog', { name: '新建项目', exact: true })
  await expect(dialog.locator('#np-worker-template')).toHaveCount(0)
  await expect(dialog.getByRole('checkbox', { name: /选择 Worker/ })).toHaveCount(0)
  await expect(dialog.getByLabel('连接方式', { exact: true })).toBeVisible()
  await expect(dialog.getByLabel('连接方式', { exact: true })).toContainText('直接连接')
  await expect(dialog.getByRole('textbox', { name: '代理地址', exact: true })).toHaveCount(0)
  await page.locator('#np-title').fill('受控固定 Worker 创建')
  await dialog.getByLabel('连接方式', { exact: true }).click()
  await page.getByRole('option', { name: 'HTTP 代理', exact: true }).click()
  await dialog.getByRole('textbox', { name: '代理地址', exact: true }).fill('proxy.example.test')
  await dialog.getByRole('spinbutton', { name: '代理端口', exact: true }).fill('8080')
  await refreshCreationQueries(page)
  await expect(dialog.getByRole('textbox', { name: '代理地址', exact: true })).toHaveValue('proxy.example.test')
  await expect(dialog.getByLabel('连接方式', { exact: true })).toContainText('HTTP 代理')
  await dialog.getByRole('button', { name: '创建并启动' }).click()
  await expect(dialog.getByText('受控字段拒绝', { exact: true })).toBeVisible()
  expect(immediate).toHaveLength(1)
  expect(immediate[0]).not.toHaveProperty('tenant_worker_ids')
  expect(immediate[0]).toMatchObject({ test_egress: { mode: 'http', host: 'proxy.example.test', port: 8080 } })
  for (const [mode, label] of [
    ['once', '单次定时'], ['daily', '每日重复'], ['weekly', '每周重复'],
  ]) {
    await dialog.locator(`.schedule-mode-options label:has(input[value="${mode}"])`).click()
    await expect(dialog.getByRole('radio', { name: label, exact: true })).toBeChecked()
    await page.locator('#schedule-timezone').fill('Asia/Shanghai')
    await page.locator('#schedule-start').fill('2099-06-01T09:00')
    if (mode !== 'once') {
      await page.locator('#schedule-end').fill('2099-06-30')
      await page.locator('#schedule-max').fill('2')
    }
    await dialog.getByRole('button', { name: mode === 'once' ? '保存单次预约' : '保存重复计划', exact: true }).click()
    await expect.poll(() => schedules.length).toBe(mode === 'once' ? 1 : mode === 'daily' ? 2 : 3)
    expect(schedules.at(-1)).toMatchObject({ mode, project: { test_egress: { mode: 'http', host: 'proxy.example.test', port: 8080 } } })
    expect(schedules.at(-1)!.project).not.toHaveProperty('tenant_worker_ids')
    await expect(dialog.getByText('受控计划拒绝', { exact: true })).toBeVisible()
  }
  expect(requests.some((request) => /\/(tenant-workers|worker-templates)/.test(request))).toBe(false)
})

test('Worker configuration rejection preserves creation input and historical schedules no longer bind Workers', async ({ page }) => {
  const requests = await mockCreation(page)
  await page.route('**/projects', (route) => route.request().method() === 'POST'
    ? route.fulfill({ status: 422, json: { detail: 'Worker explore (worker-explore) 不可用：model_unconfigured' } })
    : route.fulfill({ json: [] }))
  const updates: Array<Record<string, unknown>> = []
  const historical = {
    id: 'workers-history',
    tenant_id: 'ui-check',
    creator_id: 'ui-check',
    mode: 'daily',
    timezone: 'Asia/Shanghai',
    start_at: '2099-06-01T09:00:00+08:00',
    local_time: '09:00',
    weekdays: [],
    end_date: null,
    max_runs: null,
    status: 'active',
    version: 1,
    next_run_at: '2099-06-01T09:00:00+08:00',
    project_id: null,
    last_project_id: null,
    generated_count: 0,
    resources: [],
    library_bindings: [],
    last_result: null,
    project: {
      title: '历史 Worker 计划',
      origin: '历史背景',
      goal: '历史目标',
      scenario: 'goal-driven-v1',
      targets: [],
      explore_deadline: null,
      authorization_expires_at: null,
    },
  }
  await page.route('**/project-schedules**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (route.request().method() === 'PUT' && path === '/project-schedules/workers-history') {
      updates.push(route.request().postDataJSON())
      await route.fulfill({ status: 422, json: { detail: '受控历史组合拒绝' } })
    } else if (path.endsWith('/history')) await route.fulfill({ json: { items: [], total: 0 } })
    else if (path === '/project-schedules/workers-history')
      await route.fulfill({ json: historical })
    else await route.fulfill({ status: 503, json: { detail: '禁止真实历史计划操作' } })
  })
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/#/projects')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  const dialog = page.getByRole('dialog', { name: '新建项目', exact: true })
  await page.locator('#np-title').fill('保留固定 Worker 拒绝草稿')
  await page.locator('#np-origin-input').fill('不丢失用户原始背景')
  await dialog.getByRole('button', { name: '创建并启动' }).click()
  await expect(dialog.getByRole('alert')).toContainText('Worker explore (worker-explore) 不可用：model_unconfigured')
  const configuration = dialog.getByRole('link', { name: '前往 Worker 配置' })
  await expect(configuration).toHaveAttribute('href', '#/console/workers')
  await expect(configuration).toHaveAttribute('target', '_blank')
  await expect(page.locator('#np-title')).toHaveValue('保留固定 Worker 拒绝草稿')
  await expect(page.locator('#np-origin-input')).toHaveValue('不丢失用户原始背景')
  await discardCreation(page)
  await page.goto('/#/project-schedules/workers-history')
  await page.getByRole('button', { name: '编辑未来配置' }).click()
  const editor = page.getByRole('dialog', { name: '编辑未来配置', exact: true })
  await expect(editor.locator('#np-worker-template')).toHaveCount(0)
  await expect(editor.getByRole('checkbox', { name: /选择 Worker/ })).toHaveCount(0)
  await expect(editor.getByLabel('连接方式', { exact: true })).toContainText('直接连接')
  await editor.getByRole('button', { name: '保存未来配置' }).click()
  await expect.poll(() => updates.length).toBe(1)
  expect(updates[0]).toMatchObject({ version: 1, project: { origin: '历史背景', targets: [] } })
  expect(updates[0]!.project).not.toHaveProperty('tenant_worker_ids')
  expect(requests.some((request) => /\/(tenant-workers|worker-templates)/.test(request))).toBe(false)
  await expect(editor.getByText('受控历史组合拒绝', { exact: true })).toBeVisible()
})

test('project dialog preserves edits, nests the library picker, contains focus and scrolls on desktop', async ({
  page,
}) => {
  await mockCreation(page)
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.setViewportSize({ width: 1920, height: 1080 })
  await page.goto('/#/projects')
  const trigger = page.getByRole('button', { name: '新建项目', exact: true }).first()
  await trigger.click()
  const dialog = page.getByRole('dialog', { name: '新建项目', exact: true })
  await expect(dialog).toBeVisible()
  await expect(page.locator('#np-title')).toBeFocused()
  await expect(dialog.getByRole('button', { name: '关闭', exact: true })).toBeVisible()
  await expect(dialog.getByLabel('连接方式', { exact: true })).toContainText('直接连接')
  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
  await expect(trigger).toBeFocused()

  await trigger.click()
  await page.locator('#np-title').fill('受控弹窗检查')
  await page.locator('#np-origin-input').fill('保留用户改写的背景与资料说明')
  await expect(dialog.locator('#np-proxy-settings')).toBeVisible()
  await expect(dialog.locator('#np-timing-settings')).toBeVisible()
  await expect(dialog.getByRole('textbox', { name: '代理地址', exact: true })).toHaveCount(0)
  await page.screenshot({ path: test.info().outputPath('project-dialog-default.png') })
  await expect(dialog.getByText('安全约束文件的资料库和路径尚未确认，无法加入。请明确关闭安全限制后继续。')).toBeVisible()
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeDisabled()
  await dialog.getByRole('switch', { name: '安全限制' }).uncheck()
  await dialog.getByRole('button', { name: '从资料库选择', exact: true }).click()
  const picker = page.getByRole('dialog', { name: '从资料库选择', exact: true })
  const library = picker.getByRole('checkbox', { name: '选择资料库 接口参考资料 (reference-check)' })
  await library.check()
  await picker.getByRole('button', { name: '展开 接口参考资料' }).click()
  await picker.getByRole('checkbox', { name: '选择文件 reference-check guide.md' }).uncheck()
  await expect(library).toHaveJSProperty('indeterminate', true)
  await page.screenshot({ path: test.info().outputPath('project-library-subset.png') })
  await page.keyboard.press('Escape')
  await expect(picker).not.toBeVisible()
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('button', { name: '移除 reference-check v1 docs/intro.md' })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '移除 reference-check v1 guide.md' })).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: '从资料库选择', exact: true })).toBeFocused()

  await page.keyboard.press('Escape')
  const discard = page.getByRole('alertdialog', { name: '放弃本次填写？' })
  await expect(discard).toBeVisible()
  await discard.getByRole('button', { name: '继续填写' }).click()
  await expect(page.locator('#np-title')).toHaveValue('受控弹窗检查')
  await page.locator('.ui-dialog__overlay').click({ position: { x: 4, y: 4 }, force: true })
  await expect(dialog).toBeVisible()

  await dialog.locator('.schedule-mode-options label:has(input[value="once"])').click()
  await expect(dialog.getByRole('radio', { name: '单次定时' })).toBeChecked()
  await expect(dialog.locator('#schedule-timezone')).toBeVisible()
  await dialog.getByLabel('连接方式', { exact: true }).click()
  await page.getByRole('option', { name: 'HTTP 代理', exact: true }).click()
  await dialog.getByRole('textbox', { name: '代理地址', exact: true }).fill('proxy.example.test')
  await dialog.getByRole('spinbutton', { name: '代理端口', exact: true }).fill('8080')
  const geometry = await dialog.evaluate((element) => {
    const body = element.querySelector<HTMLElement>('.ui-dialog__body')!
    const footer = element.querySelector<HTMLElement>('.ui-dialog__footer')!
    const header = element.querySelector<HTMLElement>('.ui-dialog__header')!
    const main = element.querySelector<HTMLElement>('.creation-main')!.getBoundingClientRect()
    const settings = element.querySelector<HTMLElement>('.creation-settings')!.getBoundingClientRect()
    const unobstructed = (region: HTMLElement) => {
      const rect = region.getBoundingClientRect()
      return element.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2))
    }
    body.scrollTop = 0
    return {
      viewport: [innerWidth, innerHeight],
      scale: getComputedStyle(document.documentElement).getPropertyValue('--ui-scale').trim(),
      mainWidth: main.width,
      settingsWidth: settings.width,
      horizontalOverflow: element.scrollWidth > element.clientWidth,
      scrollable: body.scrollHeight > body.clientHeight,
      footerBottom: footer.getBoundingClientRect().bottom,
      headerUnobstructed: unobstructed(header),
      footerUnobstructed: unobstructed(footer),
    }
  })
  expect(geometry.viewport).toEqual([1920, 1080])
  expect(Number(geometry.scale)).toBe(0.9)
  expect(geometry.mainWidth).toBeGreaterThan(geometry.settingsWidth)
  expect(geometry.horizontalOverflow).toBe(false)
  expect(geometry.scrollable).toBe(true)
  expect(geometry.footerBottom).toBeLessThanOrEqual(1080)
  expect(geometry.headerUnobstructed).toBe(true)
  expect(geometry.footerUnobstructed).toBe(true)
  await page.screenshot({ path: test.info().outputPath('project-dialog-timing-proxy.png') })
  await dialog.locator('.ui-dialog__body').evaluate((body) => { body.scrollTop = body.scrollHeight })
  await expect(dialog.getByRole('button', { name: '保存单次预约' })).toBeVisible()
  for (let i = 0; i < 20; i++) {
    await page.keyboard.press('Tab')
    expect(await dialog.evaluate((element) => element.contains(document.activeElement))).toBe(true)
  }
  await dialog.getByRole('radio', { name: '单次定时' }).focus()
  await page.keyboard.press('ArrowLeft')
  await expect(dialog.getByRole('radio', { name: '立即启动' })).toBeChecked()
  const payloads: Array<Record<string, unknown>> = []
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/projects' && request.method() === 'POST')
      payloads.push(request.postDataJSON())
  })
  await expect(page.locator('#np-explore-deadline')).toHaveCount(0)
  await expect(page.locator('#np-authorization-expires-at')).toHaveCount(0)
  await dialog.getByRole('button', { name: '创建并启动' }).click()
  expect(payloads).toHaveLength(1)
  expect(payloads[0]).toMatchObject({
    origin: '保留用户改写的背景与资料说明',
    scenario: 'goal-driven-v1',
    library_selections: [{ library_key: 'reference-check', version: 1, paths: ['docs/intro.md'], proof: 'controlled-proof' }],
    explore_deadline: null,
    authorization_expires_at: null,
  })
  expect(payloads[0]).not.toHaveProperty('targets')
  expect(payloads[0]).not.toHaveProperty('tenant_worker_ids')
  const rejection = dialog.getByRole('alert').filter({ hasText: '受控字段拒绝' })
  await expect(rejection).toBeVisible()
  await expect(rejection.getByRole('button', { name: '按原请求重试' })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeDisabled()
  await rejection.getByRole('button', { name: '修改内容并使用新请求' }).click()
  await expect(page.locator('#np-title')).toHaveValue('受控弹窗检查')
  await expect(dialog.getByRole('button', { name: '移除 reference-check v1 docs/intro.md' })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeEnabled()
  expect(pageErrors).toEqual([])
})

test('the project list opens the dialog and a lost creation response cannot be blindly repeated or discarded', async ({
  page,
}) => {
  const requests = await mockCreation(page, 0)
  await page.goto('/#/projects')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  const dialog = page.getByRole('dialog', { name: '新建项目', exact: true })
  await expect(dialog).toBeVisible()
  await expect(page).toHaveURL(/#\/projects$/)
  await dialog.getByRole('switch', { name: '安全限制' }).uncheck()
  await page.locator('#np-title').fill('未知创建结果')
  await dialog.getByRole('button', { name: '创建并启动' }).click()
  await expect(dialog.getByText('保存结果未确认', { exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeDisabled()
  await page.keyboard.press('Control+Enter')
  await dialog.getByRole('button', { name: '查看列表，保留内容' }).click()
  await expect(dialog).not.toBeVisible()
  await page.getByRole('button', { name: '查看保留内容' }).click()
  await expect(page.locator('#np-title')).toHaveValue('未知创建结果')
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeDisabled()
  expect(requests.filter((request) => request === 'POST /projects')).toHaveLength(1)
  expect(requests.some((request) => request.startsWith('DELETE '))).toBe(false)
})

test('scheduled creation preserves edited content, exposes inline settings and keeps timing payloads', async ({
  page,
}) => {
  const requests = await mockCreation(page)
  const scheduleRequests: string[] = []
  const previews: Array<Record<string, unknown>> = []
  const submissions: Array<Record<string, unknown>> = []
  const schedule = {
    id: 'schedule-check',
    tenant_id: 'ui-check',
    creator_id: 'ui-check',
    mode: 'daily',
    timezone: 'Asia/Shanghai',
    start_at: '2099-06-01T09:00:00+08:00',
    local_time: '14:30',
    weekdays: [],
    end_date: '2099-06-30',
    max_runs: 2,
    status: 'active',
    version: 1,
    next_run_at: '2099-06-01T14:30:00+08:00',
    project_id: null,
    last_project_id: null,
    generated_count: 0,
    block_reason: null,
    created_at: '2026-10-13T00:00:00Z',
    updated_at: '2026-10-13T00:00:00Z',
    resources: [],
    library_bindings: [],
    last_result: null,
    project: {
      title: '受控每日计划',
      origin: '仅验证界面，不执行任务',
      goal: '查看未来配置',
      scenario: 'goal-driven-v1',
      targets: [],
      authorization_expires_at: null,
    },
  }
  await page.route('**/scenario-catalog', (route) =>
    route.fulfill({
      json: [
        {
          key: 'goal-driven-v1',
          label: '目标驱动探索',
          description: '用于明确问题并逐步验证授权范围内的假设。',
          is_default: true,
          origin_template: '已知业务背景',
          goal_template: '验证授权范围内的问题',
        },
        {
          key: 'document-check',
          label: '资料核验场景',
          description: '用于核对已有资料中的线索与结论。',
          is_default: false,
          origin_template: '资料核验背景建议',
          goal_template: '资料核验目标建议',
        },
      ],
    }),
  )
  // 预览仅验证界面展示与请求形状，不证明后端时间计算或事务行为。
  await page.route('**/project-schedules**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()
    scheduleRequests.push(`${method} ${path}`)
    if (path === '/project-schedules/preview' && method === 'POST') {
      const payload = route.request().postDataJSON() as Record<string, unknown>
      previews.push(payload)
      await route.fulfill({
        json: {
          times: [
            {
              at: '2099-06-01T14:30:00+08:00',
              local: '2099-06-01 14:30',
              note: `受控${payload.mode}预览说明`,
            },
          ],
          boundary: '受控边界：该日结束后不再启动',
        },
      })
    } else if (path === '/project-schedules' && method === 'POST') {
      submissions.push(route.request().postDataJSON())
      await route.fulfill({ status: 422, json: { detail: '受控计划拒绝' } })
    } else if (path === '/project-schedules' && method === 'GET')
      await route.fulfill({ json: [schedule] })
    else if (path === '/project-schedules/schedule-check' && method === 'GET')
      await route.fulfill({ json: schedule })
    else if (path === '/project-schedules/schedule-check/history' && method === 'GET')
      await route.fulfill({ json: { items: [], total: 0 } })
    else await route.fulfill({ status: 503, json: { detail: '禁止真实计划操作' } })
  })
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/#/projects')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  const dialog = page.getByRole('dialog', { name: '新建项目', exact: true })
  await expect(dialog.locator('#np-scenario')).toBeVisible()
  await page.locator('#np-title').fill('受控计划界面检查')
  await page.locator('#np-origin-input').fill('用户自行改写的背景')
  await page.locator('#np-goal-input').fill('用户自行改写的目标')
  await dialog.locator('#np-scenario').click()
  await page.getByRole('option', { name: '资料核验场景', exact: true }).click()
  await expect(page.locator('#np-origin-input')).toHaveValue('用户自行改写的背景')
  await expect(page.locator('#np-goal-input')).toHaveValue('用户自行改写的目标')
  await expect(dialog.locator('#np-scenario')).toContainText('资料核验场景')
  await page.screenshot({
    path: test.info().outputPath('project-dialog-scenario.png'),
    fullPage: true,
  })

  for (const [mode, label] of [
    ['once', '单次定时'],
    ['daily', '每日重复'],
    ['weekly', '每周重复'],
  ]) {
    await dialog.locator(`.schedule-mode-options label:has(input[value="${mode}"])`).click()
    await expect(dialog.getByRole('radio', { name: label, exact: true })).toBeChecked()
    await page.locator('#schedule-timezone').fill('Asia/Shanghai')
    await page.locator('#schedule-start').fill('2099-06-01T09:00')
    if (mode !== 'once') {
      await page.locator('#schedule-local-time').fill('14:30')
      await page.locator('#schedule-end').fill('2099-06-30')
      await page.locator('#schedule-max').fill('2')
    }
    if (mode === 'weekly') await dialog.getByRole('checkbox', { name: '周二', exact: true }).check()
    await dialog.getByRole('button', { name: '预览未来时间' }).click()
    await expect(dialog.getByText(`受控${mode}预览说明`, { exact: false })).toBeVisible()
    await expect(dialog.getByText('受控边界：该日结束后不再启动')).toBeVisible()
    expect(previews.at(-1)).toMatchObject({
      mode,
      timezone: 'Asia/Shanghai',
      start_at: '2099-06-01T09:00:00+08:00',
      project: { origin: '用户自行改写的背景', goal: '用户自行改写的目标' },
    })
    expect(previews.at(-1)!.project).not.toHaveProperty('targets')
    expect(previews.at(-1)!.project).not.toHaveProperty('tenant_worker_ids')
    await expect(page.locator('#schedule-timezone')).toHaveValue('Asia/Shanghai')
    await expect(page.locator(mode === 'once' ? '#schedule-start' : '#schedule-local-time')).toHaveValue(
      mode === 'once' ? '2099-06-01T09:00' : '14:30',
    )
    await expect(dialog.getByLabel('连接方式', { exact: true })).toContainText('直接连接')
  }
  expect(previews.at(-1)).toMatchObject({ local_time: '14:30', weekdays: [0, 1], max_runs: 2 })
  await page.screenshot({ path: test.info().outputPath('schedule-timing.png'), fullPage: true })
  await dialog.locator('.timing-preview').scrollIntoViewIfNeeded()
  await page.screenshot({ path: test.info().outputPath('schedule-preview.png'), fullPage: true })
  await expect(dialog).toBeVisible()
  await expect(dialog.locator('#np-targets')).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: '添加授权目标' })).toHaveCount(0)
  await dialog.getByRole('button', { name: '保存重复计划', exact: true }).click()
  await expect(dialog.getByText('受控计划拒绝', { exact: true })).toBeVisible()
  expect(submissions).toHaveLength(1)
  expect(submissions[0]).toMatchObject({
    mode: 'weekly',
    timezone: 'Asia/Shanghai',
    start_at: '2099-06-01T09:00:00+08:00',
    local_time: '14:30',
    weekdays: [0, 1],
    max_runs: 2,
    end_date: '2099-06-30',
    project: {
      scenario: 'document-check',
      origin: '用户自行改写的背景',
      goal: '用户自行改写的目标',
      authorization_expires_at: null,
    },
  })
  expect(submissions[0]!.project).not.toHaveProperty('targets')
  expect(submissions[0]!.project).not.toHaveProperty('tenant_worker_ids')
  expect(submissions[0].request_id).toBeTruthy()
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await page
    .getByRole('alertdialog', { name: '放弃本次填写？' })
    .getByRole('button', { name: '放弃并关闭' })
    .click()
  await expect(dialog).not.toBeVisible()
  await page.getByRole('navigation', { name: '项目与计划视图' }).getByRole('link', { name: '启动计划', exact: true }).click()
  const entry = page.getByRole('link', { name: '受控每日计划', exact: true })
  await expect(entry).toBeVisible()
  await expect(entry).toHaveAttribute('href', '#/project-schedules/schedule-check')
  await expect(page.getByText('无可进入的关联项目', { exact: true })).toBeVisible()
  await page.screenshot({ path: test.info().outputPath('schedule-list.png'), fullPage: true })
  await entry.click()
  await expect(page.getByRole('heading', { name: '受控每日计划', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '编辑未来配置' })).toBeVisible()
  expect(pageErrors).toEqual([])
  expect(
    requests.some((request) => /^(POST|PUT|DELETE) \/(projects|project-resource)/.test(request)),
  ).toBe(false)
  expect(scheduleRequests.filter((request) => request.startsWith('POST '))).toEqual([
    'POST /project-schedules/preview',
    'POST /project-schedules/preview',
    'POST /project-schedules/preview',
    'POST /project-schedules',
  ])
})

test('once stop options use the plan timezone, isolate mode drafts and keep the first error reachable', async ({ page }) => {
  await mockCreation(page)
  const submissions: Array<Record<string, unknown>> = []
  const immediate: Array<Record<string, unknown>> = []
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST' && path === '/projects') immediate.push(request.postDataJSON())
  })
  await page.route('**/project-schedules', async (route) => {
    if (route.request().method() === 'POST') {
      submissions.push(route.request().postDataJSON())
      await route.fulfill({ status: 422, json: { detail: '受控计划拒绝' } })
    } else await route.fulfill({ json: [] })
  })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/projects')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  const dialog = page.getByRole('dialog', { name: '新建项目', exact: true })
  await expect(dialog.locator('#np-scenario')).toContainText('目标驱动探索')
  await page.locator('#np-title').fill('单次可选停止验证')
  await expect(dialog.locator('.project-materials')).toBeVisible()
  await expect(dialog.getByLabel('连接方式', { exact: true })).toBeVisible()
  for (const text of ['场景 ·', '自由描述已有信息', '自由描述希望达成的目标', '文件先加入临时批次', '文件先进入临时批次', '普通上传按原始字节保存', '项目只会使用这里选择的 Tenant Worker', '探索场景 (Scenario)', '执行边界'])
    await expect(dialog.getByText(text, { exact: false })).toHaveCount(0)
  await expect(page.locator('#np-authorization-expires-at')).toHaveCount(0)
  await page.screenshot({ path: test.info().outputPath('creation-trimmed.png'), fullPage: true })
  async function mode(label: string) {
    const radio = dialog.getByRole('radio', { name: label, exact: true })
    await dialog.locator(`.schedule-mode-options label:has(input[aria-label="${label}"])`).click()
    await expect(radio).toBeChecked()
  }
  async function stop(label: string) {
    await page.locator('#schedule-stop-mode').click()
    await page.getByRole('option', { name: label, exact: true }).click()
  }
  await mode('每日重复')
  await page.locator('#schedule-timezone').fill('Asia/Shanghai')
  await page.locator('#schedule-start').fill('2099-06-01T09:00')
  await page.locator('#schedule-end').fill('2099-06-30')
  await page.locator('#schedule-max').fill('2')
  await mode('单次定时')
  await expect(page.locator('#schedule-stop-mode')).toContainText('不限制停止时间')
  await dialog.getByRole('button', { name: '保存单次预约' }).click()
  await expect.poll(() => submissions.length).toBe(1)
  expect(submissions[0]).toMatchObject({ end_date: null, max_runs: null, project: { explore_deadline: null, authorization_expires_at: null } })
  await stop('指定停止时间')
  await page.locator('#np-origin-input').fill('')
  await dialog.getByRole('button', { name: '保存单次预约' }).click()
  await expect(dialog.locator('#np-origin').getByText('已知信息不能为空')).toBeVisible()
  await expect(page.locator('#np-timing-settings')).toBeVisible()
  await expect.poll(() => dialog.locator('#np-origin').evaluate((field) => {
    const body = field.closest('.ui-dialog__body')!
    const rect = field.getBoundingClientRect(), view = body.getBoundingClientRect()
    return rect.top >= view.top - 1 && rect.top < view.bottom
  })).toBe(true)
  await page.locator('#np-origin-input').fill('保留用户填写的背景')
  await dialog.getByRole('button', { name: '保存单次预约' }).click()
  await expect(page.locator('#np-timing-settings')).toBeVisible()
  await expect(dialog.getByText('请填写指定停止时间。')).toBeVisible()
  for (const time of ['2099-06-01T08:59', '2099-06-01T09:00']) {
    await page.locator('#schedule-stop').fill(time)
    await dialog.getByRole('button', { name: '保存单次预约' }).click()
    await expect(dialog.getByText('停止时间必须严格晚于启动时间。')).toBeVisible()
  }
  await page.locator('#schedule-stop').fill('2099-06-01T10:30')
  expect(await page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone)).not.toBe('Asia/Shanghai')
  await page.screenshot({ path: test.info().outputPath('once-stop-settings.png'), fullPage: true })
  await dialog.getByRole('button', { name: '保存单次预约' }).click()
  await expect.poll(() => submissions.length).toBe(2)
  expect(submissions[1]).toMatchObject({ start_at: '2099-06-01T09:00:00+08:00', end_date: null, max_runs: null, project: { explore_deadline: '2099-06-01T10:30:00+08:00', authorization_expires_at: null } })
  await mode('立即启动')
  await dialog.getByRole('button', { name: '创建并启动' }).click()
  await expect.poll(() => immediate.length).toBe(1)
  expect(immediate[0]).toMatchObject({ explore_deadline: null, authorization_expires_at: null })
  expect(immediate[0]).not.toHaveProperty('start_at')
  await mode('每日重复')
  await dialog.getByRole('button', { name: '保存重复计划' }).click()
  await expect.poll(() => submissions.length).toBe(3)
  expect(submissions[2]).toMatchObject({ end_date: '2099-06-30', max_runs: 2, project: { explore_deadline: null, authorization_expires_at: null } })
  await mode('单次定时')
  await expect(page.locator('#schedule-stop')).toHaveValue('2099-06-01T10:30')
  await stop('不限制停止时间')
  await dialog.getByRole('button', { name: '保存单次预约' }).click()
  await expect.poll(() => submissions.length).toBe(4)
  expect(submissions[3]).toMatchObject({ end_date: null, max_runs: null, project: { explore_deadline: null } })
  await page.setViewportSize({ width: 480, height: 900 })
  await expect(page.locator('#schedule-stop-mode')).toContainText('不限制停止时间')
  expect(await dialog.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(false)
  await page.screenshot({ path: test.info().outputPath('once-stop-narrow.png'), fullPage: true })
})

test('historical once rescheduling and recurring edits preserve or explicitly clear existing deadlines', async ({ page }) => {
  await mockCreation(page)
  const deadline = '2099-06-01T10:30:23.123+08:00'
  const authorization = '2099-06-03T10:30:37.456+08:00'
  const base = {
    tenant_id: 'ui-check', creator_id: 'ui-check', timezone: 'Asia/Shanghai',
    start_at: '2099-06-01T09:00:17.890+08:00', local_time: null,
    weekdays: [], end_date: null, max_runs: null, status: 'active', version: 1,
    next_run_at: '2099-06-01T09:00:17.890+08:00', last_project_id: null,
    generated_count: 0, resources: [], library_bindings: [], last_result: null,
  }
  const once = { ...base, id: 'once-history', mode: 'once', project_id: 'pending-check', project: null,
    explore_deadline: deadline, authorization_expires_at: authorization }
  const recurring = { ...base, id: 'recurring-history', mode: 'daily', project_id: null,
    local_time: '09:00', project: {
      title: '历史重复计划', origin: '历史背景', goal: '历史目标', scenario: 'goal-driven-v1',
      targets: [],
      explore_deadline: deadline, authorization_expires_at: authorization,
    } }
  const updates: Array<Record<string, unknown>> = []
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.route('**/project-schedules**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (route.request().method() === 'PUT') {
      updates.push(route.request().postDataJSON())
      await route.fulfill({ status: 422, json: { detail: '受控历史改期拒绝' } })
    } else if (path.endsWith('/history')) await route.fulfill({ json: { items: [], total: 0 } })
    else if (path.endsWith('/once-history')) await route.fulfill({ json: once })
    else if (path.endsWith('/recurring-history')) await route.fulfill({ json: recurring })
    else await route.fulfill({ status: 503, json: { detail: '禁止真实计划操作' } })
  })
  await page.goto('/#/project-schedules/once-history')
  await page.getByRole('button', { name: '改期与代理设置', exact: true }).click()
  const reschedule = page.getByRole('dialog', { name: '调整单次启动时间与代理设置' })
  await expect(reschedule.getByText(/已有授权到期：2099\/6\/3 10:30:37 Asia\/Shanghai/)).toBeVisible()
  await expect(page.locator('#schedule-start')).toHaveValue('2099-06-01T09:00')
  await expect(page.locator('#schedule-stop')).toHaveValue('2099-06-01T10:30')
  await page.setViewportSize({ width: 480, height: 900 })
  expect(await reschedule.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(false)
  await page.screenshot({ path: test.info().outputPath('historical-once-narrow.png'), fullPage: true })
  await page.setViewportSize({ width: 1440, height: 900 })
  await reschedule.getByRole('button', { name: '保存改期与代理设置' }).click()
  await expect.poll(() => updates.length).toBe(1)
  expect(updates[0]).toMatchObject({ start_at: base.start_at, version: 1 })
  expect(updates[0]).not.toHaveProperty('explore_deadline')
  expect(updates[0]).not.toHaveProperty('authorization_expires_at')
  await page.locator('#schedule-start').fill('2099-06-01T10:31')
  await reschedule.getByRole('button', { name: '保存改期与代理设置' }).click()
  await expect(reschedule.getByText('停止时间必须严格晚于启动时间。')).toBeVisible()
  expect(updates).toHaveLength(1)
  await page.locator('#schedule-start').fill('2099-06-01T09:30')
  await page.locator('#schedule-stop').fill('2099-06-01T11:30')
  await reschedule.getByRole('button', { name: '保存改期与代理设置' }).click()
  await expect.poll(() => updates.length).toBe(2)
  expect(updates[1]).toMatchObject({ start_at: '2099-06-01T09:30:00+08:00', explore_deadline: '2099-06-01T11:30:00+08:00' })
  await page.locator('#schedule-stop-mode').click()
  await page.getByRole('option', { name: '不限制停止时间', exact: true }).click()
  await page.locator('#schedule-start').fill('2099-06-04T09:00')
  await reschedule.getByRole('button', { name: '保存改期与代理设置' }).click()
  await expect(reschedule.getByText(/启动时间必须早于已有授权到期时间/)).toBeVisible()
  expect(updates).toHaveLength(2)
  await page.locator('#schedule-start').fill('2099-06-01T09:30')
  await reschedule.getByRole('button', { name: '保存改期与代理设置' }).click()
  await expect.poll(() => updates.length).toBe(3)
  expect(updates[2]).toMatchObject({ explore_deadline: null })
  expect(updates[2]).not.toHaveProperty('authorization_expires_at')
  await reschedule.getByRole('button', { name: '取消', exact: true }).click()
  await page.goto('/#/project-schedules/recurring-history')
  await page.getByRole('button', { name: '编辑未来配置' }).click()
  const editor = page.getByRole('dialog', { name: '编辑未来配置', exact: true })
  await expect(editor.getByLabel('连接方式', { exact: true })).toContainText('直接连接')
  await expect(editor.locator('#np-timing-settings')).toBeVisible()
  await expect(editor.locator('#np-proxy-settings #np-explore-deadline')).toHaveCount(0)
  await expect(editor.getByRole('heading', { name: '既有期限' })).toBeVisible()
  await editor.getByRole('button', { name: '保存未来配置' }).click()
  await expect.poll(() => updates.length).toBe(4)
  expect(updates[3]).toMatchObject({ project: { explore_deadline: deadline, authorization_expires_at: authorization }, version: 1 })
  expect(updates[3]!.project).not.toHaveProperty('tenant_worker_ids')
  await page.locator('#np-explore-deadline').fill('')
  await page.locator('#np-authorization-expires-at').fill('')
  await editor.getByRole('button', { name: '保存未来配置' }).click()
  await expect.poll(() => updates.length).toBe(5)
  expect(updates[4]).toMatchObject({ project: { explore_deadline: null, authorization_expires_at: null } })
  expect(pageErrors).toEqual([])
})

test('resource session survives the library picker during upload, failure, unsaved paths and unknown results', async ({
  page,
}) => {
  const requests = await mockCreation(page)
  const filesReceived: string[] = []
  const members: Array<Record<string, unknown>> = []
  let releaseFailure: (() => void) | undefined
  let rejected = false
  const currentBatch = () => ({
    id: 'batch-ui',
    state: 'open',
    created_at: '2026-10-13T00:00:00Z',
    expires_at: '2099-06-01T00:00:00Z',
    resource_count: members.length,
    total_bytes: members.length * 3,
    members,
  })
  await page.route('**/project-resource-batches**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/project-resource-batches' && request.method() === 'POST') {
      await route.fulfill({ json: currentBatch() })
    } else if (path.endsWith('/files') && request.method() === 'POST') {
      const body = request.postDataBuffer()!.toString()
      const name = new URL(request.url()).searchParams.get('path')!
      filesReceived.push(name)
      expect(body).toContain(name === 'first.txt' ? 'one' : name === 'failed.txt' ? 'two' : 'end')
      if (name === 'failed.txt' && !rejected) {
        rejected = true
        await new Promise<void>((resolve) => {
          releaseFailure = resolve
        })
        await route.fulfill({ status: 422, json: { detail: '受控上传拒绝' } })
      } else if (name === 'unknown.txt') {
        await route.abort('connectionfailed')
      } else {
        const member = {
          id: name,
          kind: 'file',
          path: name,
          source: 'file',
          size_bytes: 3,
          sha256: 'a'.repeat(64),
        }
        members.push(member)
        await route.fulfill({ json: member })
      }
    } else if (path === '/project-resource-batches/batch-ui' && request.method() === 'GET') {
      await route.fulfill({ json: currentBatch() })
    } else await route.fulfill({ status: 503, json: { detail: '禁止真实资料操作' } })
  })
  await page.setViewportSize({ width: 1920, height: 1080 })
  await page.goto('/#/projects')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  const dialog = page.getByRole('dialog', { name: '新建项目', exact: true })
  await dialog.getByRole('switch', { name: '安全限制' }).uncheck()
  await page.locator('#np-title').fill('受控资料会话检查')
  const materials = dialog.locator('.project-materials')
  const summary = materials.locator('.materials-summary')
  const uploader = materials.locator('.resource-batch-uploader')
  async function openAndClosePicker() {
    await dialog.getByRole('button', { name: '从资料库选择', exact: true }).click()
    const picker = page.getByRole('dialog', { name: '从资料库选择', exact: true })
    await expect(picker).toBeVisible()
    await picker.getByRole('button', { name: '完成选择', exact: true }).click()
    await expect(picker).not.toBeVisible()
  }
  await uploader
    .locator('input[type=file]')
    .first()
    .setInputFiles([
      { name: 'first.txt', mimeType: 'text/plain', buffer: Buffer.from('one') },
      { name: 'failed.txt', mimeType: 'text/plain', buffer: Buffer.from('two') },
      { name: 'unknown.txt', mimeType: 'text/plain', buffer: Buffer.from('end') },
    ])
  await expect.poll(() => Boolean(releaseFailure)).toBe(true)
  await openAndClosePicker()
  await expect(summary).toContainText('上传中')
  await expect(summary).toContainText('本地文件 3')
  releaseFailure!()
  await expect(summary).toContainText('失败 1 项')
  await expect(summary).toContainText('待上传 1 项')
  await openAndClosePicker()
  await uploader.getByRole('button', { name: '补传', exact: true }).click()
  await expect(summary).not.toContainText('失败 1 项')
  await expect(uploader.locator('.resource-batch-member')).toHaveCount(2)
  await materials.scrollIntoViewIfNeeded()
  await page.screenshot({ path: test.info().outputPath('resource-file-tags.png'), fullPage: true })
  await uploader.locator('.resource-member-details > summary').first().click()
  const pathInput = uploader.getByRole('textbox', { name: '资源相对路径' }).first()
  // 构造尚未触发 blur 自动保存的草稿，验证打开选择器不会卸载上传器。
  await pathInput.evaluate((element) => {
    const input = element as HTMLInputElement
    input.value = 'unsaved/first.txt'
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await openAndClosePicker()
  await expect(summary).toContainText('未保存路径 1 项')
  await expect(pathInput).toHaveValue('unsaved/first.txt')
  await page.keyboard.press('Control+Enter')
  await expect(materials).toBeVisible()
  await expect(dialog.getByText('仍有未完成的资料，请补传、核对或明确移除后再创建。')).toBeVisible()
  await pathInput.fill('first.txt')
  await uploader.getByRole('button', { name: '上传', exact: true }).click()
  await expect(uploader.getByText('资料结果未确认', { exact: true })).toBeVisible()
  await openAndClosePicker()
  await expect(summary).toContainText('结果未确认')
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeDisabled()
  await summary.scrollIntoViewIfNeeded()
  await page.screenshot({
    path: test.info().outputPath('resource-session-unknown.png'),
    fullPage: true,
  })
  await expect(uploader.locator('.resource-pending-item')).toContainText('unknown.txt')
  await expect(uploader.locator('.resource-batch-member')).toHaveCount(2)
  expect(filesReceived).toEqual(['first.txt', 'failed.txt', 'failed.txt', 'unknown.txt'])
  expect(requests.filter((request) => request === 'POST /projects')).toEqual([])
})
