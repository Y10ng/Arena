import { expect, test, type Locator } from '@playwright/test'

const projectId = 'poll-ui'
const now = '2026-09-23T10:00:00Z'
const project = {
  id: projectId,
  title: '轮询稳定性合成项目',
  status: 'active',
  created_at: now,
  tenant_id: 'synthetic',
  tenant_name: '合成 Tenant',
  tenant_workers: [],
  fact_count: 0,
  intent_count: 0,
  working_intent_count: 0,
  unclaimed_intent_count: 0,
  hint_count: 0,
  report_capability: { supported: false, policy_version: 0, template_profile: null, reason: null },
}

const run = (id: string, workerName: string) => ({
  id,
  project_id: projectId,
  task_type: 'explore',
  worker_name: workerName,
  worker_type: 'pi',
  status: 'running',
  started_at: now,
  session_id: null,
})

const geometry = async (locators: Locator[]) =>
  Promise.all(
    locators.map((locator) =>
      locator.evaluate((element) => {
        const rect = element.getBoundingClientRect()
        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
      }),
    ),
  )

test('active run updates twice without moving title, tabs or terminal, and keeps errors retryable', async ({
  page,
}) => {
  let listVersion = 0
  let detailVersion = 0
  let failDetails = false
  const unexpected: string[] = []
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
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
    if (path === '/auth/session' || path === '/auth/csrf') {
      json = {
        user_id: 'synthetic',
        tenant_id: 'synthetic',
        role: 'user',
        must_change_password: false,
        csrf_token: 'synthetic-only',
      }
    } else if (path === '/projects') json = [project]
    else if (
      path === '/project-groups' ||
      path === '/scenarios' ||
      path === `/projects/${projectId}/vulnerabilities`
    ) json = []
    else if (path === `/projects/${projectId}`) {
      json = { project, facts: [], intents: [], hints: [], targets: [], resources: [], library_bindings: [] }
    } else if (path === `/projects/${projectId}/runtime`) {
      json = {
        schema_version: 1,
        project_id: projectId,
        observed_at: now,
        execution_generation: 1,
        project_status: 'active',
        state: 'running',
        code: 'process_started',
        message: '合成执行中',
        dispatcher: { status: 'online', freshness: 'fresh', updated_at: now },
        scheduling: { code: 'dispatched', observed_at: now, retry_at: null },
        active_runs: [],
        last_activity: null,
        last_error: null,
        actions: {
          pause: { allowed: false, reason: 'synthetic' },
          resume: { allowed: false, reason: 'synthetic' },
          finish: { allowed: false, reason: 'synthetic' },
        },
      }
    } else if (path === `/projects/${projectId}/task-runs`) {
      listVersion += 1
      json = [run('run-a', `worker-${listVersion}`), run('run-b', 'worker-b')]
    } else if (path === `/projects/${projectId}/task-runs/command-events`) {
      detailVersion += 1
      await new Promise((resolve) => setTimeout(resolve, 450))
      if (failDetails) {
        await route.fulfill({ status: 503, json: { detail: 'synthetic unavailable' } })
        return
      }
      json = [{ task_run_id: url.searchParams.get('task_run_id'), events: [] }]
    } else if (path === `/projects/${projectId}/artifacts` && url.searchParams.has('task_run_id')) {
      const runId = url.searchParams.get('task_run_id')
      json = [
        {
          id: runId === 'run-a' ? 'output-a' : 'output-b',
          project_id: projectId,
          task_run_id: runId,
          kind: 'raw_output',
          category: 'raw_output',
          uri: 'artifact://runs/explore_execute-stdout.log',
          size: 12000,
          created_at: now,
          metadata: { phase: 'explore_execute', stream: 'stdout', returncode: 0 },
        },
      ]
    } else if (path.endsWith('/preview-tail')) {
      const artifactId = path.split('/').at(-2)
      json = {
        artifact_id: artifactId,
        project_id: projectId,
        task_run_id: artifactId === 'output-a' ? 'run-a' : 'run-b',
        kind: 'raw_output',
        content: `${artifactId} version ${detailVersion}\n` + '合成日志\n'.repeat(180),
        size: 12000,
        bytes_read: 12000,
        truncated: false,
      }
    } else {
      unexpected.push(`${request.method()} ${path}`)
      await route.abort('blockedbyclient')
      return
    }
    await route.fulfill({ json })
  })

  await page.goto(`/#/projects/${projectId}/runs?runId=run-a`)
  const title = page.locator('.sidebar-title-row')
  const tabs = page.locator('.runs-detail-tabs .ui-tabs__list')
  const terminal = page.locator('.terminal-screen')
  const output = page.locator('.terminal-content')
  await expect(output).toContainText('output-a version 1')
  await page.getByRole('checkbox', { name: '自动滚动' }).uncheck()
  await terminal.evaluate((element) => (element.scrollTop = 90))
  const anchors = [title, tabs, terminal]
  const initial = await geometry(anchors)
  const scrollTop = await terminal.evaluate((element) => element.scrollTop)
  expect(scrollTop).toBeGreaterThan(0)

  for (const version of [2, 3]) {
    await expect(page.locator('.run-row').first()).toContainText(`worker-${version}`, {
      timeout: 7000,
    })
    await expect.poll(() => detailVersion, { timeout: 7000, intervals: [50] }).toBe(version)
    await expect(output).toContainText(`output-a version ${version - 1}`)
    expect(await geometry(anchors)).toEqual(initial)
    expect(await terminal.evaluate((element) => element.scrollTop)).toBe(scrollTop)
    await expect(title.locator('[role="status"]')).toHaveCount(0)
    await expect(page.locator('.run-detail-loading [role="status"]')).toHaveCount(0)
    await expect(page.locator('.run-detail-state')).toHaveCount(0)

    await expect(output).toContainText(`output-a version ${version}`)
    expect(await geometry(anchors)).toEqual(initial)
    expect(await terminal.evaluate((element) => element.scrollTop)).toBe(scrollTop)
  }

  await page.locator('.run-row').filter({ hasText: 'worker-b' }).click()
  await expect(page.locator('.run-detail-loading [role="status"]')).toBeVisible()
  await expect(output).not.toContainText('output-a version')
  await expect(output).toContainText('output-b version')
  await page.locator('.run-row').filter({ hasText: 'worker-' }).first().click()
  await expect(output).toContainText('output-a version')

  const refresh = page.getByRole('button', { name: '刷新', exact: true }).last()
  await refresh.click()
  await expect(page.locator('.run-detail-loading [role="status"]')).toBeVisible()
  await expect(page.locator('.run-detail-loading [role="status"]')).toHaveCount(0)
  failDetails = true
  await refresh.click()
  await expect(page.locator('.run-detail-state')).toContainText('synthetic unavailable')
  const error = page.locator('.run-detail-state')
  const failedVersion = detailVersion
  await expect.poll(() => detailVersion, { timeout: 7000 }).toBeGreaterThan(failedVersion)
  await expect(error).toContainText('synthetic unavailable')
  failDetails = false
  await error.getByRole('button', { name: '重试' }).click()
  await expect(error).toHaveCount(0)
  expect(unexpected).toEqual([])
  expect(pageErrors).toEqual([])
})
