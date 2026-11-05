import { expect, test } from '@playwright/test'

const now = '2026-10-30T08:00:00Z'
const project = {
  id: 'agent-layout', title: 'Agent 受控呈现样本 · 非业务数据', status: 'stopped',
  created_at: now, scenario_key: 'default', tenant_id: 'synthetic', tenant_name: '受控样本',
  tenant_workers: [], fact_count: 0, intent_count: 0, working_intent_count: 0,
  unclaimed_intent_count: 0, hint_count: 0,
  report_capability: { supported: false, policy_version: 0, template_profile: null, reason: null },
}
const conversation = {
  id: 'c1', project_id: project.id, model_id: 'sample', model_display_name: '呈现样本（不调用模型）',
  model_name: 'sample', available: true, unavailable_reason: null, created_at: now,
}
const messages = Array.from({ length: 6 }, (_, index) => ({
  id: index + 1, client_message_id: `sample-${index}`, text: `受控问题 ${index + 1}：请整理已有记录，不执行任何工作。`,
  created_at: now, task_run_id: null, status: 'succeeded', error_code: null, attachments: [],
  answer: '## 已有记录与边界\n\n' + '这是仅用于呈现检查的中文长段落，不代表真实研究结果。需要保留来源、状态和未决事项，不能把受理当作完成。'.repeat(6)
    + '\n\n- 第一项：已有材料\n- 第二项：尚未确认的范围\n\n```text\n' + 'long_identifier_'.repeat(25)
    + '\n```\n\n| 条目 | 状态 | 说明 |\n| --- | --- | --- |\n| 受控材料 | 待确认 | 不代表业务结果 |',
  actions: index === 5 ? [{ id: 'work-1', kind: 'report', title: '受控工作回执' }] : [],
  artifacts: index === 5 ? [{ id: 'a1', title: '受控成果（不下载）', download_url: '/sample-artifact' }] : [],
}))

for (const theme of ['light', 'dark'] as const) {
  test(`Agent U1b controlled layout ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 2554, height: 1182 })
    const errors: string[] = []
    const unexpected: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.addInitScript(mode => {
      localStorage.setItem('cairn.theme', mode)
      localStorage.setItem('cairn:csrf-token', 'synthetic-only')
      // 仅此隔离浏览器上下文使用合成事件；不连接业务 SSE，不持久化消息。
      class SampleEvents extends EventTarget {
        onopen: (() => void) | null = null
        onerror: (() => void) | null = null
        constructor() {
          super()
          window.addEventListener('agent-layout-update', event => {
            this.dispatchEvent(new MessageEvent('messages', { data: JSON.stringify((event as CustomEvent).detail), lastEventId: '1' }))
          })
        }
        close() {}
      }
      Object.defineProperty(window, 'EventSource', { value: SampleEvents })
    }, theme)
    const base = `/projects/${project.id}`
    const responses: Record<string, unknown> = {
      '/auth/session': { user_id: 'synthetic', tenant_id: 'synthetic', role: 'user', must_change_password: false, csrf_token: 'synthetic' },
      '/projects': [project], '/projects/runtime': { items: [] }, '/project-groups': [], '/scenarios': [],
      [base]: { project, facts: [], intents: [], hints: [], targets: [], resources: [], library_bindings: [] },
      [`${base}/vulnerabilities`]: [],
      [`${base}/runtime`]: { schema_version: 1, project_id: project.id, observed_at: now, project_status: 'stopped', state: 'stopped', code: 'stopped', message: '', active_runs: [], actions: {} },
      '/project-agent/configuration': { ...conversation },
      [`${base}/agent-conversations`]: [conversation, { ...conversation, id: 'c2' }],
      [`${base}/agent-conversations/c1/messages`]: messages,
      [`${base}/agent-conversations/c1/attachments`]: [],
      [`${base}/agent-conversations/c1/attachment-limits`]: { max_file_bytes: 10485760, max_count: 200, max_total_bytes: 104857600 },
      [`${base}/agent-conversations/c1/actions`]: [{ id: 'work-1', kind: 'report', title: '受控工作回执', status: 'failed', error: '受控失败说明：不能冒充已完成。', url: '', download_url: null, result: null, next_attempt_at: null }],
    }
    // 只放行本地静态资源；包括意外写入在内的所有其他请求一律拦截。
    await page.route('**/*', async route => {
      const request = route.request()
      const url = new URL(request.url())
      if (url.origin === new URL(test.info().project.use.baseURL!).origin && request.method() === 'GET'
        && (url.pathname === '/' || url.pathname.startsWith('/static/'))) {
        await route.continue()
      } else if (request.method() === 'GET' && url.pathname in responses) {
        await route.fulfill({ json: responses[url.pathname] })
      } else {
        unexpected.push(`${request.method()} ${url.pathname}`)
        await route.fulfill({ status: 503, json: { detail: '未配置的受控接口' } })
      }
    })
    await page.goto(`/#${base}/agent?conversation=c1`)
    const input = page.getByRole('textbox', { name: '消息', exact: true })
    await expect(input).toBeVisible()
    await expect(page.locator('.agent-turn')).toHaveCount(6)
    await expect(page.locator('.agent-sidebar')).toHaveCount(0)
    await expect(page.locator('.agent-deliverables')).toHaveCount(0)
    const geometry = await page.evaluate(() => {
      const box = (selector: string) => document.querySelector(selector)!.getBoundingClientRect()
      const read = box('.agent-reading-column'); const composer = box('.agent-composer')
      return { axis: Math.abs((read.x + read.width / 2) - (composer.x + composer.width / 2)), width: composer.width,
        inputHeight: box('.agent-input-surface').height, bodyOverflow: document.documentElement.scrollWidth > innerWidth }
    })
    expect(geometry.axis).toBeLessThanOrEqual(8)
    expect(geometry.width).toBeLessThanOrEqual(1040)
    expect(geometry.inputHeight).toBeLessThanOrEqual(128)
    expect(geometry.bodyOverflow).toBe(false)
    await input.fill('保留中文草稿\n第二行')
    await input.press('Enter')
    await page.screenshot({ path: `/tmp/arena-u1b-agent-sample-${theme}.png` })
    await page.getByRole('button', { name: '会话历史', exact: true }).click()
    await expect(page.locator('.agent-sidebar')).toBeVisible()
    await page.getByRole('button', { name: '工作与成果', exact: true }).click()
    await expect(page.locator('.agent-sidebar')).toHaveCount(0)
    await expect(page.locator('.agent-deliverables')).toContainText('受控失败说明')
    await expect(input).toHaveValue('保留中文草稿\n第二行\n')
    await page.screenshot({ path: `/tmp/arena-u1b-agent-work-${theme}.png` })
    await page.locator('.agent-messages').evaluate(el => { el.scrollTop = 200; el.dispatchEvent(new Event('scroll')) })
    const oldTop = await page.locator('.agent-messages').evaluate(el => el.scrollTop)
    await page.evaluate(rows => window.dispatchEvent(new CustomEvent('agent-layout-update', { detail: rows })), [{ ...messages[5], id: 7, client_message_id: 'sample-7' }])
    await expect(page.locator('.agent-turn')).toHaveCount(7)
    expect(await page.locator('.agent-messages').evaluate(el => el.scrollTop)).toBe(oldTop)
    await page.getByRole('button', { name: '回到最新', exact: true }).click()
    await expect(page.getByRole('button', { name: '回到最新', exact: true })).toHaveCount(0)
    await input.fill('中文长草稿\n'.repeat(40))
    expect(await input.evaluate(el => el.getBoundingClientRect().height)).toBe(240)
    await input.fill('')
    expect(await input.evaluate(el => el.getBoundingClientRect().height)).toBeLessThanOrEqual(68)
    expect(errors).toEqual([])
    expect(unexpected).toEqual([])
  })
}
