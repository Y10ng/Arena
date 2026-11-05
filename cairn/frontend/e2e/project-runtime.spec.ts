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
  options: { unavailable?: boolean; empty?: boolean; paused?: boolean; resumeReady?: boolean; rejectResume?: boolean; liveClock?: boolean } = {},
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
  if (!options.liveClock) await page.clock.setFixedTime(new Date(now))
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

for (const entry of ['workspace', 'list'] as const) {
  for (const clockOffset of [-120_000, 120_000]) {
    test(`runtime freshness uses selected local reads: ${entry}, clock ${clockOffset}`, async ({ page }) => {
      await page.setViewportSize({ width: 1920, height: 1080 })
      const mock = await mockRuntimeApp(page, { empty: true, liveClock: true })
      const browserTime = new Date(Date.parse(now) + clockOffset)
      await page.clock.install({ time: browserTime })
      await page.clock.pauseAt(browserTime)
      type Control = import('../src/api/types').ProjectExecutionControl
      const pending: Control = {
        request_id: 'freshness-pause', action: 'pause', state: 'requested', requested_at: now,
      }
      const confirmed: Control = { ...pending, state: 'paused', confirmed_at: now }
      let metadata = { ...project, status: 'paused', execution_control: pending }
      const confirmedSnapshot = {
        ...runtime, project_status: 'paused', state: 'paused', code: 'project_paused',
        execution_control: confirmed, last_error: null,
        dispatcher: { status: 'idle', freshness: 'fresh', updated_at: now },
        actions: { ...runtime.actions, resume: { allowed: true, reason: null as string | null } },
      }
      const pendingSnapshot = {
        ...confirmedSnapshot, state: 'pausing', code: 'execution_control_pending',
        execution_control: pending,
        actions: { ...runtime.actions, resume: { allowed: false, reason: 'execution_control_pending' } },
      }
      let reply = pendingSnapshot
      let failed = false
      let omitProject = false
      let singleReads = 0
      let batchReads = 0
      const posts: string[] = []
      await page.route('**/*', async (route) => {
        const request = route.request()
        const path = new URL(request.url()).pathname
        if (request.method() === 'POST' && path.startsWith('/projects')) {
          posts.push(path)
          return route.fulfill({ status: 409, json: { detail: 'project_authorization_expired' } })
        }
        if (path === '/projects') return route.fulfill({ json: [metadata] })
        if (path === `/projects/${project.id}`) return route.fulfill({ json: {
          project: metadata, facts: [], intents: [], hints: [], targets: [], resources: [], library_bindings: [],
        } })
        if (path === '/projects/runtime' || path === `/projects/${project.id}/runtime`) {
          if (path === '/projects/runtime') batchReads++
          else singleReads++
          return route.fulfill({
            status: failed ? 503 : 200,
            json: failed ? { detail: 'runtime_unavailable' } : path === '/projects/runtime'
              ? { items: omitProject ? [{ ...reply, project_id: 'other-project' }] : [reply] } : reply,
          })
        }
        return route.fallback()
      })
      await page.goto(entry === 'workspace' ? `/#/projects/${project.id}/runs` : '/#/projects')
      const statusBadge = page.getByLabel('项目状态', { exact: true })
      await expect(statusBadge).toHaveText('正在暂停')
      const expectResume = async (allowed: boolean) => {
        if (entry === 'list') await page.getByLabel('项目操作菜单').click()
        const resume = page.getByRole(entry === 'list' ? 'menuitem' : 'button', { name: '继续项目', exact: true })
        if (entry === 'list') {
          if (allowed) await expect(resume).not.toHaveAttribute('data-disabled', '')
          else await expect(resume).toHaveAttribute('data-disabled', '')
          await page.keyboard.press('Escape')
        } else if (allowed) await expect(resume).toBeEnabled()
        else await expect(resume).toBeDisabled()
      }
      const refresh = async () => {
        await page.getByLabel('运行详情', { exact: true }).click()
        const response = page.waitForResponse((r) => new URL(r.url()).pathname ===
          (entry === 'list' ? '/projects/runtime' : `/projects/${project.id}/runtime`))
        await page.getByRole('button', { name: '刷新观测', exact: true }).click()
        await response
        await page.keyboard.press('Escape')
      }
      // 从 requested 自动快读到确认，不提交任何项目动作；Server 时间固定，只有浏览器偏移。
      await expect.poll(() => singleReads).toBeGreaterThan(0)
      reply = confirmedSnapshot
      await page.clock.runFor(800)
      await expect(statusBadge).toHaveText('已暂停')
      await expectResume(true)
      await expect(page.getByText('观测已失效', { exact: true })).toHaveCount(0)
      await page.getByLabel('运行详情', { exact: true }).click()
      await expect(page.locator('.project-observation-details')).toContainText(now)
      await expect(page.locator('.project-observation-details')).not.toContainText('历史观测时间')
      await page.keyboard.press('Escape')

      // 普通查询／batch 的相同响应也是新读取；Query 结构共享不能让旧本地年龄一直保留。
      await page.clock.fastForward(40_000)
      await refresh()
      if (entry === 'list') expect(batchReads).toBeGreaterThan(1)
      metadata = { ...metadata, execution_control: confirmed }
      reply = pendingSnapshot
      await page.clock.fastForward(40_000)
      await refresh()
      await expect(statusBadge).toHaveText('已暂停')
      await expectResume(true)
      // 被丢弃的旧 requested、元数据确认，以及其他项目的 batch 成功均不能续期。
      if (entry === 'list') omitProject = true
      await page.clock.fastForward(24_001)
      await refresh()
      await expect(page.getByText('观测已失效', { exact: true }).first()).toBeVisible()
      await expectResume(false)
      await page.getByLabel('运行详情', { exact: true }).click()
      await expect(page.locator('.project-observation-details')).toContainText('历史观测时间')
      await expect(page.locator('.project-observation-details')).toContainText(now)
      await page.keyboard.press('Escape')

      omitProject = false
      reply = { ...confirmedSnapshot, actions: {
        ...confirmedSnapshot.actions, resume: { allowed: false, reason: 'project_authorization_expired' },
      } }
      await refresh()
      await expect(page.getByText('观测已失效', { exact: true })).toHaveCount(0)
      await expectResume(false)
      reply = confirmedSnapshot
      await page.clock.runFor(1)
      await refresh()
      await expectResume(true)
      failed = true
      await refresh()
      await expect(page.getByText('观测未更新', { exact: true }).first()).toBeVisible()
      await expectResume(false)
      expect(posts).toEqual([])
      expect(mock.posts).toEqual([])
      expect(mock.unexpected).toEqual([])
      expect(mock.pageErrors).toEqual([])
    })
  }

  test(`hot control feedback closes the delayed receipt seam: ${entry}`, async ({ page }) => {
    test.setTimeout(60_000)
    await page.setViewportSize({ width: 1920, height: 1080 })
    const mock = await mockRuntimeApp(page, { empty: true, liveClock: true })
    type Control = import('../src/api/types').ProjectExecutionControl
    let control: Control = {}
    let status = 'active'
    let staleMetadata = { ...project, execution_control: {} as Control }
    let physicalAt = 0
    let acceptedAt = 0
    let activeReads = 0
    let maxActiveReads = 0
    let reads = 0
    let releaseOld: (() => Promise<void>) | undefined
    let holdOld = false
    let resumeDenied = false
    const posts: string[] = []
    const times: Record<string, number | string>[] = []
    const snapshot = () => ({
      ...runtime, observed_at: new Date().toISOString(), project_status: status,
      execution_control: { ...control }, last_error: null,
      dispatcher: { status: 'running', freshness: 'fresh', updated_at: new Date().toISOString() },
      state: control.state === 'requested' ? (control.action === 'pause' ? 'pausing' : 'starting') : status === 'paused' ? 'paused' : 'running',
      code: control.state === 'requested' ? 'execution_control_pending' : 'process_started',
      actions: { ...runtime.actions, resume: {
        allowed: status === 'paused' && control.state === 'paused' && !resumeDenied,
        reason: resumeDenied ? 'project_authorization_expired' : null,
      } },
    })
    let pendingSnapshot: ReturnType<typeof snapshot> | undefined
    let returnPending = false
    await page.route('**/*', async (route) => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      if (path === '/project-schedules') return route.fulfill({ json: [] })
      if (path === '/projects') return route.fulfill({ json: [staleMetadata] })
      if (path === `/projects/${project.id}`) return route.fulfill({ json: {
        project: staleMetadata, facts: [], intents: [], hints: [], targets: [], resources: [], library_bindings: [],
      } })
      if (path === '/projects/runtime' || path === `/projects/${project.id}/runtime`) {
        reads++
        activeReads++
        maxActiveReads = Math.max(maxActiveReads, activeReads)
        let view = snapshot()
        if (control.state === 'requested') pendingSnapshot = view
        if (returnPending && pendingSnapshot) {
          // 同一请求的旧 requested 即使晚到，也不能撤回已确认的动作资格。
          view = { ...pendingSnapshot, observed_at: view.observed_at }
          returnPending = false
        }
        const body = path === '/projects/runtime' ? { items: [view] } : view
        const respond = async () => {
          activeReads--
          await route.fulfill({ json: body })
        }
        if (holdOld) {
          holdOld = false
          releaseOld = respond
          return
        }
        return respond()
      }
      if (request.method() === 'POST' && [
        `/projects/${project.id}/pause`, `/projects/${project.id}/resume`,
      ].includes(path)) {
        const action = path.endsWith('/pause') ? 'pause' : 'resume'
        posts.push(action)
        acceptedAt = performance.now()
        status = action === 'pause' ? 'paused' : 'active'
        control = { request_id: `request-${posts.length}`, action, state: 'requested', requested_at: new Date().toISOString(), confirmed_at: null }
        if (action === 'pause') staleMetadata = { ...project, status, execution_control: { ...control } }
        return route.fulfill({ json: { ...project, status, execution_control: control } })
      }
      return route.fallback()
    })
    await page.goto(entry === 'workspace' ? `/#/projects/${project.id}/runs` : '/#/projects')
    const statusBadge = page.getByLabel('项目状态', { exact: true })
    await expect(statusBadge).toHaveText('进行中')
    const act = async (label: string) => {
      if (entry === 'list') {
        await page.getByLabel('项目操作菜单').click()
        await page.getByRole('menuitem', { name: label, exact: true }).click()
      } else await page.getByRole('button', { name: label, exact: true }).click()
    }
    await act('暂停项目')
    await expect(statusBadge).toHaveText('正在暂停')
    times.push({ action: 'pause', accepted_ui_ms: performance.now() - acceptedAt })
    await expect(statusBadge).not.toHaveText('已暂停')
    // 让普通元数据读取先确认，runtime 仍是同请求 requested；不手动刷新或 invalidate。
    await new Promise((resolve) => setTimeout(resolve, 2200))
    staleMetadata = { ...project, status, execution_control: {
      ...control, state: 'paused', confirmed_at: new Date().toISOString(),
    } }
    await expect(statusBadge).toHaveText('已暂停', { timeout: 11_000 })
    await expect(page.getByText('动作资格观测未更新', { exact: true }).first()).toBeVisible()
    const expectResume = async (allowed: boolean) => {
      if (entry === 'list') await page.getByLabel('项目操作菜单').click()
      const resume = page.getByRole(entry === 'list' ? 'menuitem' : 'button', { name: '继续项目', exact: true })
      if (entry === 'list') {
        if (allowed) await expect(resume).not.toHaveAttribute('data-disabled', '', { timeout: 1200 })
        else await expect(resume).toHaveAttribute('data-disabled', '')
        await page.keyboard.press('Escape')
      } else if (allowed) await expect(resume).toBeEnabled({ timeout: 1200 })
      else await expect(resume).toBeDisabled()
    }
    await expectResume(false)
    physicalAt = performance.now()
    control = { ...staleMetadata.execution_control }
    await expectResume(true)
    times.push({ action: 'pause-metadata-first', runtime_ready_to_action_ms: performance.now() - physicalAt })
    await expect(page.getByText('动作资格观测未更新', { exact: true })).toHaveCount(0)
    expect(maxActiveReads).toBe(1)
    await page.screenshot({ path: test.info().outputPath('metadata-first-pause.png') })

    // 同一窗口内正常恢复应自动反馈，不依靠后面的超时手动刷新场景。
    await act('继续项目')
    await expect(statusBadge).toHaveText('正在恢复')
    times.push({ action: 'resume-auto', accepted_ui_ms: performance.now() - acceptedAt })
    await new Promise((resolve) => setTimeout(resolve, 2200))
    physicalAt = performance.now()
    control = { ...control, state: 'running', confirmed_at: new Date().toISOString() }
    await expect(statusBadge).toHaveText('进行中', { timeout: 1200 })
    times.push({ action: 'resume-auto', physical_ms: physicalAt - acceptedAt, physical_to_ui_ms: performance.now() - physicalAt })
    expect(posts).toEqual(['pause', 'resume'])
    await page.screenshot({ path: test.info().outputPath('automatic-resume.png') })

    // 保留 runtime 先确认顺序；合法拒绝也是完整资格，不能持续快读直到允许。
    await act('暂停项目')
    await expect(statusBadge).toHaveText('正在暂停')
    await new Promise((resolve) => setTimeout(resolve, 2200))
    physicalAt = performance.now()
    resumeDenied = true
    control = { ...control, state: 'paused', confirmed_at: new Date().toISOString() }
    await expect(statusBadge).toHaveText('已暂停', { timeout: 1200 })
    times.push({ action: 'pause-runtime-first', physical_ms: physicalAt - acceptedAt, physical_to_ui_ms: performance.now() - physicalAt })
    await expectResume(false)
    const afterDenied = reads
    await new Promise((resolve) => setTimeout(resolve, 1800))
    expect(reads - afterDenied).toBeLessThanOrEqual(1)
    await expectResume(false)
    resumeDenied = false
    await page.getByLabel('运行详情', { exact: true }).click()
    await page.getByRole('button', { name: '刷新观测', exact: true }).click()
    await page.keyboard.press('Escape')
    await expectResume(true)
    await page.screenshot({ path: test.info().outputPath('confirmed-pause.png') })
    returnPending = true
    await page.getByLabel('运行详情', { exact: true }).click()
    await page.getByRole('button', { name: '刷新观测', exact: true }).click()
    await expect.poll(() => returnPending).toBe(false)
    await expect(statusBadge).toHaveText('已暂停')
    await page.keyboard.press('Escape')
    // 下一次常规读取先取得旧 pause，再延迟到新 resume 已受理后返回。
    holdOld = true
    await expect.poll(() => Boolean(releaseOld), { timeout: 9000 }).toBe(true)
    await act('继续项目')
    await expect(statusBadge).toHaveText('正在恢复')
    times.push({ action: 'resume', accepted_ui_ms: performance.now() - acceptedAt })
    await releaseOld!()
    await expect(statusBadge).toHaveText('正在恢复')
    expect(posts).toEqual(['pause', 'resume', 'pause', 'resume'])
    await expect(page.getByText(/物理状态尚未确认，请刷新观测/).first()).toBeVisible({ timeout: 16_000 })
    const afterWindow = reads
    await new Promise((resolve) => setTimeout(resolve, 1100))
    // 快观察已退出；期间至多碰到一次常规 8 秒读取。
    expect(reads - afterWindow).toBeLessThanOrEqual(1)
    expect(posts).toEqual(['pause', 'resume', 'pause', 'resume'])
    await expect(statusBadge).toHaveText('正在恢复')
    await page.screenshot({ path: test.info().outputPath('unconfirmed-resume.png') })
    physicalAt = performance.now()
    control = { ...control, state: 'running', confirmed_at: new Date().toISOString() }
    await page.getByLabel('运行详情', { exact: true }).click()
    await page.getByRole('button', { name: '刷新观测', exact: true }).click()
    await expect(statusBadge).toHaveText('进行中')
    times.push({ action: 'resume', physical_ms: physicalAt - acceptedAt, manual_refresh_to_ui_ms: performance.now() - physicalAt })
    expect(posts).toEqual(['pause', 'resume', 'pause', 'resume'])
    await page.keyboard.press('Escape')
    // 带着尚未确认的请求离开 SPA 页面，核验作用域清理，而不是关闭浏览器来中断请求。
    await act('暂停项目')
    await expect(statusBadge).toHaveText('正在暂停')
    expect(posts).toEqual(['pause', 'resume', 'pause', 'resume', 'pause'])
    await page.evaluate(() => { location.hash = '#/feedback-exit-not-found' })
    await expect(statusBadge).toHaveCount(0)
    const afterExit = reads
    await new Promise((resolve) => setTimeout(resolve, 1100))
    expect(reads).toBe(afterExit)
    expect(mock.pageErrors).toEqual([])
    expect(mock.unexpected).toEqual([])
    console.log('CONTROL_FEEDBACK_TIMINGS', entry, JSON.stringify(times))
  })
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
