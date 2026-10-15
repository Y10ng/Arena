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
      '/project-groups': [{ id: 'group-check', name: '受控项目组' }],
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
      '/tenant-workers/options': [
        {
          id: 'worker-check',
          display_name: '受控 Worker',
          model_id: 'model-check',
          engine: 'pi',
          available: true,
          task_types: ['bootstrap', 'reason', 'explore'],
        },
      ],
      '/worker-templates': [
        {
          id: 'template-check',
          name: '受控默认组合',
          is_default: true,
          tenant_worker_ids: ['worker-check'],
        },
      ],
      '/library-catalog': [
        { key: 'reference-check', name: '接口参考资料', latest_version: 1, file_count: 2 },
      ],
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

async function selectGroup(page: Page) {
  await page.locator('#np-group-select').click()
  await page.getByRole('option', { name: '受控项目组' }).click()
}

const completeWorkerIds = ['worker-bootstrap', 'worker-reason', 'worker-explore']
const specialistWorkers = completeWorkerIds.map((id, index) => ({
  id,
  display_name: ['准备 Worker', '推理 Worker', '探索 Worker'][index],
  model_id: 'model-check',
  engine: 'pi',
  available: true,
  task_types: [['bootstrap'], ['reason'], ['explore']][index],
  max_running: index === 2 ? 3 : 1,
  executor_available: true,
}))

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
  let releaseDefault!: () => void
  const defaultReady = new Promise<void>((resolve) => { releaseDefault = resolve })
  await page.route('**/worker-templates', async (route) => {
    await defaultReady
    await route.fulfill({ json: [{ id: 'late-default', name: '延迟默认', is_default: true, tenant_worker_ids: ['worker-check'] }] })
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
  await selectGroup(page)
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeDisabled()
  releaseDefault()
  await expect(page.locator('#np-execution-trigger')).toContainText('默认组合 · 受控 Worker')
  expect(await page.evaluate(() => typeof crypto.randomUUID)).toBe('undefined')
  await dialog.getByRole('button', { name: '创建并启动' }).click()
  await expect(dialog.getByText('受控字段拒绝', { exact: true })).toBeVisible()
  await expect(dialog.getByText(/跨刷新查询入口未保存/)).toBeVisible()
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeEnabled()
  expect(submitted).toHaveLength(1)
  for (const [index, mode, label] of [[1, 'once', '单次定时'], [2, 'daily', '每日重复'], [3, 'weekly', '每周重复']] as const) {
    await page.locator('#schedule-mode').click()
    await page.getByRole('option', { name: label, exact: true }).click()
    await page.locator('#schedule-timezone').fill('Asia/Shanghai')
    await page.locator('#schedule-start').fill('2099-06-01T09:00')
    await dialog.getByRole('button', { name: '完成设置', exact: true }).click()
    await expect(dialog.locator('#np-targets')).toHaveCount(0)
    await expect(page.locator('#np-execution-trigger')).not.toContainText('触发将受阻')
    await dialog.getByRole('button', { name: mode === 'once' ? '保存单次预约' : '保存重复计划', exact: true }).click()
    await expect(dialog.getByText('受控 Origin 计划拒绝', { exact: true })).toBeVisible()
    expect(submitted).toHaveLength(index + 1)
    expect(submitted[index]!.payload.mode).toBe(mode)
    await expect(dialog.getByRole('button', { name: mode === 'once' ? '保存单次预约' : '保存重复计划', exact: true })).toBeEnabled()
  }
  for (const { path, payload } of submitted) {
    const project = path === '/projects' ? payload : payload.project
    expect(project).not.toHaveProperty('targets')
    expect(project).toMatchObject({ origin: '  仅核验已有资料\n排除任何外部访问  ', goal: '保留用户目标', tenant_worker_ids: ['worker-check'] })
    expect(payload.request_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  }
  expect(new Set(submitted.map(({ payload }) => payload.request_id)).size).toBe(4)
  await page.screenshot({ path: test.info().outputPath('origin-creation-cache-warning.png'), fullPage: true })
  expect(pageErrors).toEqual([])
})

test('execution defaults save a manual complete combination, reopen it and submit explicit IDs in every mode without refresh overwrites', async ({
  page,
}) => {
  await mockCreation(page)
  let templates = [
    {
      id: 'template-other',
      name: '非默认模板',
      tenant_worker_ids: ['worker-reason'],
      is_default: false,
    },
  ]
  let templateGets = 0
  const savedDefaults: Array<{ method: string; path: string; body: Record<string, unknown> }> = []
  const immediate: Array<Record<string, unknown>> = []
  const schedules: Array<Record<string, unknown>> = []
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/projects' && request.method() === 'POST')
      immediate.push(request.postDataJSON())
  })
  await page.route('**/tenant-workers/options', (route) =>
    route.fulfill({ json: specialistWorkers }),
  )
  await page.route('**/worker-templates**', async (route) => {
    const method = route.request().method(),
      path = new URL(route.request().url()).pathname
    if (method === 'GET' && path === '/worker-templates') {
      templateGets++
      await route.fulfill({ json: templates })
    } else if (method === 'POST' && path === '/worker-templates') {
      const body = route.request().postDataJSON()
      savedDefaults.push({ method, path, body })
      const saved = { id: 'template-saved', ...body }
      templates = [...templates.map((template) => ({ ...template, is_default: false })), saved]
      await route.fulfill({ json: saved })
    } else if (method === 'PUT' && path === '/worker-templates/template-saved') {
      const body = route.request().postDataJSON()
      savedDefaults.push({ method, path, body })
      const saved = { id: 'template-saved', name: '默认 Worker 组合', ...body }
      templates = templates.map((template) =>
        template.id === saved.id ? saved : { ...template, is_default: false },
      )
      await route.fulfill({ json: saved })
    } else await route.fulfill({ status: 503, json: { detail: '禁止真实模板操作' } })
  })
  await page.route('**/project-schedules', async (route) => {
    if (route.request().method() === 'POST') {
      schedules.push(route.request().postDataJSON())
      await route.fulfill({ status: 422, json: { detail: '受控组合计划拒绝' } })
    } else await route.fulfill({ json: [] })
  })
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/#/projects')
  const trigger = page.getByRole('button', { name: '新建项目', exact: true }).first()
  await trigger.click()
  const dialog = page.getByRole('dialog', { name: '新建项目', exact: true })
  await expect(dialog.getByText('执行组合需要处理', { exact: true })).toBeVisible()
  await expect(page.locator('#np-execution-trigger')).toContainText('尚未设置默认组合')
  for (const worker of specialistWorkers)
    await expect(
      dialog.getByRole('checkbox', { name: `选择 Worker ${worker.display_name}` }),
    ).not.toBeChecked()
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeDisabled()
  for (const worker of specialistWorkers)
    await dialog.getByRole('checkbox', { name: `选择 Worker ${worker.display_name}` }).check()
  await dialog.getByRole('button', { name: '保存为默认组合' }).click()
  await expect(dialog.getByText('默认组合已保存', { exact: true })).toBeVisible()
  expect(savedDefaults).toEqual([
    {
      method: 'POST',
      path: '/worker-templates',
      body: { name: '默认 Worker 组合', is_default: true, tenant_worker_ids: completeWorkerIds },
    },
  ])
  await discardCreation(page)
  await trigger.click()
  await expect(page.locator('#np-execution-trigger')).toContainText(
    '默认组合 · 准备 Worker、推理 Worker、探索 Worker',
  )
  await page.locator('#np-execution-trigger').click()
  for (const worker of specialistWorkers)
    await expect(
      dialog.getByRole('checkbox', { name: `选择 Worker ${worker.display_name}` }),
    ).toBeChecked()
  await page.locator('#np-worker-template').click()
  await page.getByRole('option', { name: '本次自定义', exact: true }).click()
  for (const worker of specialistWorkers)
    await expect(
      dialog.getByRole('checkbox', { name: `选择 Worker ${worker.display_name}` }),
    ).toBeChecked()
  await dialog.getByRole('button', { name: '保存为默认组合' }).click()
  await expect.poll(() => savedDefaults.length).toBe(2)
  expect(savedDefaults[1]).toEqual({
    method: 'PUT',
    path: '/worker-templates/template-saved',
    body: { is_default: true, tenant_worker_ids: completeWorkerIds },
  })
  await page.locator('#np-worker-template').click()
  await page.getByRole('option', { name: '本次自定义', exact: true }).click()
  const beforeRefresh = templateGets
  templates = templates.map((template) =>
    template.id === 'template-saved'
      ? { ...template, tenant_worker_ids: ['worker-reason'] }
      : template,
  )
  await refreshCreationQueries(page)
  await expect.poll(() => templateGets).toBeGreaterThan(beforeRefresh)
  await expect(page.locator('#np-worker-template')).toContainText('本次自定义')
  for (const worker of specialistWorkers)
    await expect(
      dialog.getByRole('checkbox', { name: `选择 Worker ${worker.display_name}` }),
    ).toBeChecked()
  await page.screenshot({
    path: test.info().outputPath('worker-default-complete.png'),
    fullPage: true,
  })
  await page.locator('#np-title').fill('受控完整组合')
  await selectGroup(page)
  await dialog.getByRole('button', { name: '完成设置', exact: true }).click()
  await dialog.getByRole('button', { name: '创建并启动' }).click()
  await expect(dialog.getByText('受控字段拒绝', { exact: true })).toBeVisible()
  expect(immediate).toHaveLength(1)
  expect(immediate[0]).toMatchObject({ tenant_worker_ids: completeWorkerIds })
  for (const [mode, label] of [
    ['once', '单次定时'],
    ['daily', '每日重复'],
    ['weekly', '每周重复'],
  ]) {
    await page.locator('#schedule-mode').click()
    await page.getByRole('option', { name: label, exact: true }).click()
    await page.locator('#schedule-timezone').fill('Asia/Shanghai')
    await page.locator('#schedule-start').fill('2099-06-01T09:00')
    if (mode !== 'once') {
      await page.locator('#schedule-end').fill('2099-06-30')
      await page.locator('#schedule-max').fill('2')
    }
    await dialog.getByRole('button', { name: '完成设置', exact: true }).click()
    await dialog
      .getByRole('button', { name: mode === 'once' ? '保存单次预约' : '保存重复计划', exact: true })
      .click()
    await expect.poll(() => schedules.length).toBe(mode === 'once' ? 1 : mode === 'daily' ? 2 : 3)
    expect(schedules.at(-1)).toMatchObject({
      mode,
      project: { tenant_worker_ids: completeWorkerIds },
    })
    await expect(dialog.getByText('受控组合计划拒绝', { exact: true })).toBeVisible()
  }
  expect(pageErrors).toEqual([])
})

test('execution defaults retain invalid IDs, explain missing roles and never replace historical plan Workers', async ({
  page,
}) => {
  await mockCreation(page)
  let workers = [
    ...specialistWorkers,
    {
      ...specialistWorkers[0]!,
      id: 'worker-disabled',
      display_name: '失效 Worker',
      available: false,
      unavailable_reason: 'Model 已停用',
    },
  ]
  let templateGets = 0
  await page.route('**/tenant-workers/options', (route) => route.fulfill({ json: workers }))
  await page.route('**/worker-templates', (route) => {
    templateGets++
    return route.fulfill({
      json: [
        {
          id: 'template-invalid',
          name: '待修复默认',
          is_default: true,
          tenant_worker_ids: ['worker-bootstrap', 'worker-disabled', 'worker-deleted'],
        },
      ],
    })
  })
  const updates: Array<Record<string, unknown>> = []
  const historicalIds = ['worker-history']
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
      group_id: 'group-check',
      tenant_worker_ids: historicalIds,
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
  await expect(dialog.getByText('失效 Worker · Model 已停用', { exact: true })).toBeVisible()
  await expect(
    dialog.getByText('worker-deleted · Worker 已删除或不属于当前 Tenant', { exact: true }),
  ).toBeVisible()
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeDisabled()
  await expect(dialog.getByRole('button', { name: '保存为默认组合' })).toBeDisabled()
  await page.locator('#np-worker-template').click()
  await page.getByRole('option', { name: '本次自定义', exact: true }).click()
  const beforeRefresh = templateGets
  workers = workers.filter((worker) => worker.id !== 'worker-disabled')
  await refreshCreationQueries(page)
  await expect.poll(() => templateGets).toBeGreaterThan(beforeRefresh)
  await expect(
    dialog.getByText('worker-disabled · Worker 已删除或不属于当前 Tenant', { exact: true }),
  ).toBeVisible()
  await expect(dialog.getByRole('button', { name: '移除失效绑定', exact: true })).toHaveCount(2)
  await page.setViewportSize({ width: 480, height: 900 })
  await page.screenshot({
    path: test.info().outputPath('worker-invalid-narrow.png'),
    fullPage: true,
  })
  await dialog.getByRole('button', { name: '移除失效绑定', exact: true }).first().click()
  await dialog.getByRole('button', { name: '移除失效绑定', exact: true }).click()
  await expect(page.locator('#np-execution-trigger')).toContainText(
    '执行组合缺少角色：reason、explore',
  )
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeDisabled()
  await dialog.getByRole('checkbox', { name: '选择 Worker 推理 Worker' }).check()
  await dialog.getByRole('checkbox', { name: '选择 Worker 探索 Worker' }).check()
  await expect(dialog.getByRole('button', { name: '保存为默认组合' })).toBeEnabled()
  await discardCreation(page)
  workers = [
    ...specialistWorkers,
    {
      ...specialistWorkers[0]!,
      id: 'worker-history',
      display_name: '历史 Worker',
      task_types: ['bootstrap', 'reason', 'explore'],
      executor_available: false,
    },
  ]
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/#/project-schedules/workers-history')
  await page.getByRole('button', { name: '编辑未来配置' }).click()
  const editor = page.getByRole('dialog', { name: '编辑未来配置', exact: true })
  await expect(page.locator('#np-execution-trigger')).toContainText('本次组合 · 历史 Worker')
  if ((await page.locator('#np-execution-trigger').getAttribute('aria-expanded')) !== 'true')
    await page.locator('#np-execution-trigger').click()
  await expect(editor.getByRole('checkbox', { name: '选择 Worker 历史 Worker' })).toBeChecked()
  for (const worker of specialistWorkers)
    await expect(
      editor.getByRole('checkbox', { name: `选择 Worker ${worker.display_name}` }),
    ).not.toBeChecked()
  await editor.getByRole('button', { name: '保存未来配置' }).click()
  await expect.poll(() => updates.length).toBe(1)
  expect(updates[0]).toMatchObject({ version: 1, project: { tenant_worker_ids: historicalIds } })
  await expect(editor.getByText('受控历史组合拒绝', { exact: true })).toBeVisible()
})

test('project dialog preserves edits, nests the library picker, contains focus and scrolls at desktop and narrow widths', async ({
  page,
}) => {
  await mockCreation(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/projects')
  const trigger = page.getByRole('button', { name: '新建项目', exact: true }).first()
  await trigger.click()
  const dialog = page.getByRole('dialog', { name: '新建项目', exact: true })
  await expect(dialog).toBeVisible()
  await expect(page.locator('#np-title')).toBeFocused()
  await expect(dialog.getByRole('button', { name: '关闭', exact: true })).toBeVisible()
  await expect(page.locator('#np-execution-trigger')).toHaveAttribute('aria-expanded', 'false')
  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
  await expect(trigger).toBeFocused()

  await trigger.click()
  await page.locator('#np-title').fill('受控弹窗检查')
  await selectGroup(page)
  await page.locator('#np-origin-input').fill('保留用户改写的背景与资料说明')
  await expect(dialog.locator('#np-execution-settings')).not.toBeVisible()
  await expect(dialog.locator('#np-timing-settings')).not.toBeVisible()
  await page.screenshot({
    path: test.info().outputPath('project-dialog-compact.png'),
    fullPage: true,
  })
  await dialog.locator('.project-materials > summary').click()
  await page.getByRole('button', { name: '从资料库选择', exact: true }).click()
  const picker = page.getByRole('dialog', { name: '从资料库选择', exact: true })
  await picker.getByRole('button', { name: /接口参考资料/ }).click()
  await page.keyboard.press('Escape')
  await expect(picker).not.toBeVisible()
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('资料库 · 接口参考资料')).toBeVisible()
  await expect(dialog.getByRole('button', { name: '从资料库选择', exact: true })).toBeFocused()
  await dialog.getByRole('button', { name: '取消关联' }).click()
  await expect(dialog.getByText('资料库 · 接口参考资料')).not.toBeVisible()

  await page.keyboard.press('Escape')
  const discard = page.getByRole('alertdialog', { name: '放弃本次填写？' })
  await expect(discard).toBeVisible()
  await discard.getByRole('button', { name: '继续填写' }).click()
  await expect(page.locator('#np-title')).toHaveValue('受控弹窗检查')
  await page.locator('.ui-dialog__overlay').click({ position: { x: 4, y: 4 }, force: true })
  await expect(dialog).toBeVisible()

  for (const width of [1440, 480]) {
    await page.setViewportSize({ width, height: 900 })
    if ((await page.locator('#np-execution-trigger').getAttribute('aria-expanded')) === 'false')
      await page.locator('#np-execution-trigger').click()
    const geometry = await dialog.evaluate((element) => {
      const body = element.querySelector<HTMLElement>('.ui-dialog__body')!
      const footer = element.querySelector<HTMLElement>('.ui-dialog__footer')!
      const header = element.querySelector<HTMLElement>('.ui-dialog__header')!
      const unobstructed = (region: HTMLElement) => {
        const rect = region.getBoundingClientRect()
        return element.contains(
          document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2),
        )
      }
      body.scrollTop = 0
      return {
        horizontalOverflow: element.scrollWidth > element.clientWidth,
        scrollable: body.scrollHeight > body.clientHeight,
        footerBottom: footer.getBoundingClientRect().bottom,
        viewportHeight: innerHeight,
        headerUnobstructed: unobstructed(header),
        footerUnobstructed: unobstructed(footer),
      }
    })
    expect(geometry.horizontalOverflow).toBe(false)
    expect(geometry.scrollable).toBe(true)
    expect(geometry.footerBottom).toBeLessThanOrEqual(geometry.viewportHeight)
    expect(geometry.headerUnobstructed).toBe(true)
    expect(geometry.footerUnobstructed).toBe(true)
    await page.screenshot({
      path: test.info().outputPath(`project-dialog-${width}-top.png`),
      fullPage: true,
    })
    await dialog.locator('.ui-dialog__body').evaluate((body) => {
      body.scrollTop = body.scrollHeight
    })
    await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeVisible()
    await page.screenshot({
      path: test.info().outputPath(`project-dialog-${width}.png`),
      fullPage: true,
    })
    for (let i = 0; i < 20; i++) {
      await page.keyboard.press('Tab')
      expect(await dialog.evaluate((element) => element.contains(document.activeElement))).toBe(
        true,
      )
    }
  }
  const payloads: Array<Record<string, unknown>> = []
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/projects' && request.method() === 'POST')
      payloads.push(request.postDataJSON())
  })
  await expect(page.locator('#np-explore-deadline')).toHaveCount(0)
  await expect(page.locator('#np-authorization-expires-at')).toHaveCount(0)
  await page.keyboard.press('Escape')
  await expect(dialog.locator('#np-execution-settings')).not.toBeVisible()
  await expect(page.locator('#np-execution-trigger')).toBeFocused()
  await dialog.getByRole('button', { name: '创建并启动' }).click()
  expect(payloads).toHaveLength(1)
  expect(payloads[0]).toMatchObject({
    origin: '保留用户改写的背景与资料说明',
    scenario: 'goal-driven-v1',
    group_id: 'group-check',
    tenant_worker_ids: ['worker-check'],
    library_keys: [],
    explore_deadline: null,
    authorization_expires_at: null,
  })
  expect(payloads[0]).not.toHaveProperty('targets')
  await expect(dialog.getByText('受控字段拒绝', { exact: true })).toBeVisible()
  await expect(page.locator('#np-title')).toHaveValue('受控弹窗检查')
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeEnabled()
})

test('legacy entry opens the same dialog and a lost creation response cannot be blindly repeated or discarded', async ({
  page,
}) => {
  const requests = await mockCreation(page, 0)
  await page.goto('/#/projects/new')
  const dialog = page.getByRole('dialog', { name: '新建项目', exact: true })
  await expect(dialog).toBeVisible()
  await expect(page).toHaveURL(/#\/projects$/)
  await page.locator('#np-title').fill('未知创建结果')
  await selectGroup(page)
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

test('scheduled creation preserves edited content, exposes settings on demand and keeps timing payloads', async ({
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
      group_id: 'group-check',
      tenant_worker_ids: ['worker-check'],
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
  await expect(dialog.locator('.scenario-settings')).toHaveAttribute('open')
  await expect(dialog.locator('.scenario-purpose')).toHaveCount(0)
  await expect(dialog.locator('.scenario-desc')).toHaveCount(0)
  await page.locator('#np-title').fill('受控计划界面检查')
  await selectGroup(page)
  await page.locator('#np-origin-input').fill('用户自行改写的背景')
  await page.locator('#np-goal-input').fill('用户自行改写的目标')
  await dialog.getByRole('button', { name: /资料核验场景/ }).click()
  await expect(page.locator('#np-origin-input')).toHaveValue('用户自行改写的背景')
  await expect(page.locator('#np-goal-input')).toHaveValue('用户自行改写的目标')
  await expect(dialog.locator('.scenario-settings')).toHaveAttribute('open')
  await dialog.locator('.scenario-settings > summary').click()
  await expect(dialog.locator('.scenario-settings')).not.toHaveAttribute('open')
  await dialog.locator('.scenario-settings > summary').click()
  await expect(dialog.getByText('查看场景填写建议', { exact: true })).toHaveCount(0)
  await page.screenshot({
    path: test.info().outputPath('project-dialog-scenario.png'),
    fullPage: true,
  })

  for (const [mode, label] of [
    ['once', '单次定时'],
    ['daily', '每日重复'],
    ['weekly', '每周重复'],
  ]) {
    await page.locator('#schedule-mode').click()
    await page.getByRole('option', { name: label, exact: true }).click()
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
    await dialog.getByRole('button', { name: '完成设置', exact: true }).click()
    await expect(page.locator('#np-timing-trigger')).toContainText('Asia/Shanghai')
    await expect(page.locator('#np-timing-trigger')).toContainText(
      mode === 'once' ? '2099-06-01 09:00' : '14:30',
    )
    await expect(page.locator('#np-execution-trigger')).not.toContainText('触发将受阻')
  }
  expect(previews.at(-1)).toMatchObject({ local_time: '14:30', weekdays: [0, 1], max_runs: 2 })
  await page.screenshot({ path: test.info().outputPath('schedule-timing.png'), fullPage: true })
  await page.locator('#np-timing-trigger').click()
  await dialog.locator('.timing-preview').scrollIntoViewIfNeeded()
  await page.screenshot({ path: test.info().outputPath('schedule-preview.png'), fullPage: true })
  await page.keyboard.press('Escape')
  await expect(page.locator('#np-timing-trigger')).toBeFocused()
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
  expect(submissions[0].request_id).toBeTruthy()
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await page
    .getByRole('alertdialog', { name: '放弃本次填写？' })
    .getByRole('button', { name: '放弃并关闭' })
    .click()
  await expect(dialog).not.toBeVisible()
  await page.getByRole('button', { name: '计划任务', exact: true }).click()
  const entry = page.getByRole('link', { name: '受控每日计划', exact: true })
  await expect(entry).toBeVisible()
  await expect(entry).toHaveAttribute('href', '#/project-schedules/schedule-check')
  await expect(page.getByText('尚未生成项目', { exact: true })).toBeVisible()
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
  const scene = dialog.locator('.scenario-settings')
  await expect(scene).toHaveAttribute('open')
  await scene.locator('summary').click()
  await page.locator('#np-title').fill('单次可选停止验证')
  await selectGroup(page)
  await expect(scene).not.toHaveAttribute('open')
  await scene.locator('summary').click()
  await dialog.getByRole('button', { name: /目标驱动探索/ }).click()
  await expect(scene).toHaveAttribute('open')
  await dialog.locator('.project-materials > summary').click()
  await page.locator('#np-execution-trigger').click()
  for (const text of ['场景 ·', '自由描述已有信息', '自由描述希望达成的目标', '文件先加入临时批次', '文件先进入临时批次', '普通上传按原始字节保存', '项目只会使用这里选择的 Tenant Worker', '探索场景 (Scenario)', '执行边界'])
    await expect(dialog.getByText(text, { exact: false })).toHaveCount(0)
  await expect(page.locator('#np-authorization-expires-at')).toHaveCount(0)
  await page.screenshot({ path: test.info().outputPath('creation-trimmed.png'), fullPage: true })
  async function mode(label: string) {
    await page.locator('#schedule-mode').click()
    await page.getByRole('option', { name: label, exact: true }).click()
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
  await dialog.getByRole('button', { name: '完成设置', exact: true }).click()
  await dialog.getByRole('button', { name: '保存单次预约' }).click()
  await expect(dialog.locator('#np-origin').getByText('已知信息不能为空')).toBeVisible()
  await expect(page.locator('#np-timing-settings')).not.toBeVisible()
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
  await page.locator('#np-timing-trigger').click()
  await expect(page.locator('#np-timing-trigger')).toContainText('不限制停止时间')
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
      group_id: 'group-check', tenant_worker_ids: ['worker-check'], targets: [],
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
  await page.getByRole('button', { name: '改期', exact: true }).click()
  const reschedule = page.getByRole('dialog', { name: '调整单次启动时间' })
  await expect(reschedule.getByText(/已有授权到期：2099\/6\/3 10:30:37 Asia\/Shanghai/)).toBeVisible()
  await expect(page.locator('#schedule-start')).toHaveValue('2099-06-01T09:00')
  await expect(page.locator('#schedule-stop')).toHaveValue('2099-06-01T10:30')
  await page.setViewportSize({ width: 480, height: 900 })
  expect(await reschedule.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(false)
  await page.screenshot({ path: test.info().outputPath('historical-once-narrow.png'), fullPage: true })
  await page.setViewportSize({ width: 1440, height: 900 })
  await reschedule.getByRole('button', { name: '保存改期' }).click()
  await expect.poll(() => updates.length).toBe(1)
  expect(updates[0]).toMatchObject({ start_at: base.start_at, version: 1 })
  expect(updates[0]).not.toHaveProperty('explore_deadline')
  expect(updates[0]).not.toHaveProperty('authorization_expires_at')
  await page.locator('#schedule-start').fill('2099-06-01T10:31')
  await reschedule.getByRole('button', { name: '保存改期' }).click()
  await expect(reschedule.getByText('停止时间必须严格晚于启动时间。')).toBeVisible()
  expect(updates).toHaveLength(1)
  await page.locator('#schedule-start').fill('2099-06-01T09:30')
  await page.locator('#schedule-stop').fill('2099-06-01T11:30')
  await reschedule.getByRole('button', { name: '保存改期' }).click()
  await expect.poll(() => updates.length).toBe(2)
  expect(updates[1]).toMatchObject({ start_at: '2099-06-01T09:30:00+08:00', explore_deadline: '2099-06-01T11:30:00+08:00' })
  await page.locator('#schedule-stop-mode').click()
  await page.getByRole('option', { name: '不限制停止时间', exact: true }).click()
  await page.locator('#schedule-start').fill('2099-06-04T09:00')
  await reschedule.getByRole('button', { name: '保存改期' }).click()
  await expect(reschedule.getByText(/启动时间必须早于已有授权到期时间/)).toBeVisible()
  expect(updates).toHaveLength(2)
  await page.locator('#schedule-start').fill('2099-06-01T09:30')
  await reschedule.getByRole('button', { name: '保存改期' }).click()
  await expect.poll(() => updates.length).toBe(3)
  expect(updates[2]).toMatchObject({ explore_deadline: null })
  expect(updates[2]).not.toHaveProperty('authorization_expires_at')
  await reschedule.getByRole('button', { name: '取消', exact: true }).click()
  await page.goto('/#/project-schedules/recurring-history')
  await page.getByRole('button', { name: '编辑未来配置' }).click()
  const editor = page.getByRole('dialog', { name: '编辑未来配置', exact: true })
  await expect(editor.locator('#np-execution-trigger')).toContainText('受控 Worker')
  if (await page.locator('#np-execution-trigger').getAttribute('aria-expanded') !== 'true')
    await page.locator('#np-execution-trigger').click()
  await expect(editor.getByRole('heading', { name: '既有期限' })).toBeVisible()
  await editor.getByRole('button', { name: '保存未来配置' }).click()
  await expect.poll(() => updates.length).toBe(4)
  expect(updates[3]).toMatchObject({ project: { explore_deadline: deadline, authorization_expires_at: authorization }, version: 1 })
  await page.locator('#np-explore-deadline').fill('')
  await page.locator('#np-authorization-expires-at').fill('')
  await editor.getByRole('button', { name: '保存未来配置' }).click()
  await expect.poll(() => updates.length).toBe(5)
  expect(updates[4]).toMatchObject({ project: { explore_deadline: null, authorization_expires_at: null } })
  expect(pageErrors).toEqual([])
})

test('resource session survives collapsing during upload, failure, unsaved paths and unknown results', async ({
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
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/projects')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  const dialog = page.getByRole('dialog', { name: '新建项目', exact: true })
  await page.locator('#np-title').fill('受控资料会话检查')
  await selectGroup(page)
  const materials = dialog.locator('.project-materials')
  const summary = materials.locator(':scope > summary')
  await summary.click()
  const uploader = materials.locator('.resource-batch-uploader')
  await uploader
    .locator('input[type=file]')
    .first()
    .setInputFiles([
      { name: 'first.txt', mimeType: 'text/plain', buffer: Buffer.from('one') },
      { name: 'failed.txt', mimeType: 'text/plain', buffer: Buffer.from('two') },
      { name: 'unknown.txt', mimeType: 'text/plain', buffer: Buffer.from('end') },
    ])
  await expect.poll(() => Boolean(releaseFailure)).toBe(true)
  await summary.click()
  await expect(summary).toContainText('上传中')
  await expect(summary).toContainText('本地文件 3')
  releaseFailure!()
  await expect(summary).toContainText('失败 1 项')
  await expect(summary).toContainText('待上传 1 项')
  await summary.click()
  await uploader.getByRole('button', { name: '补传', exact: true }).click()
  await expect(summary).not.toContainText('失败 1 项')
  await expect(uploader.locator('.resource-batch-member')).toHaveCount(2)
  await uploader.locator('.resource-member-details > summary').first().click()
  const pathInput = uploader.getByRole('textbox', { name: '资源相对路径' }).first()
  // 构造尚未触发 blur 自动保存的草稿，单独验证折叠不会卸载上传器。
  await pathInput.evaluate((element) => {
    const input = element as HTMLInputElement
    input.value = 'unsaved/first.txt'
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await summary.click()
  await expect(summary).toContainText('未保存路径 1 项')
  await summary.click()
  await expect(pathInput).toHaveValue('unsaved/first.txt')
  await page.keyboard.press('Control+Enter')
  await expect(materials).toHaveAttribute('open')
  await expect(dialog.getByText('仍有未完成的资料，请补传、核对或明确移除后再创建。')).toBeVisible()
  await pathInput.fill('first.txt')
  await uploader.getByRole('button', { name: '上传', exact: true }).click()
  await expect(uploader.getByText('资料结果未确认', { exact: true })).toBeVisible()
  await summary.click()
  await expect(summary).toContainText('结果未确认')
  await expect(dialog.getByRole('button', { name: '创建并启动' })).toBeDisabled()
  await page.setViewportSize({ width: 480, height: 900 })
  await summary.scrollIntoViewIfNeeded()
  await page.screenshot({
    path: test.info().outputPath('resource-session-collapsed.png'),
    fullPage: true,
  })
  await summary.click()
  await expect(uploader.locator('.resource-pending-item')).toContainText('unknown.txt')
  await expect(uploader.locator('.resource-batch-member')).toHaveCount(2)
  expect(filesReceived).toEqual(['first.txt', 'failed.txt', 'failed.txt', 'unknown.txt'])
  expect(requests.filter((request) => request === 'POST /projects')).toEqual([])
})
