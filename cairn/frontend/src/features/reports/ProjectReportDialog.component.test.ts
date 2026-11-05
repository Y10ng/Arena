import { flushPromises, mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import { ApiError } from '@/api/client'
import { useUIStore } from '@/stores/ui'
import { Select } from '@/components/ui'
import type {
  AssessmentReportFinalization,
  ReportCapability,
  ReportInventoryPage,
  ReportTemplateCatalogEntry,
} from '@/api/types'
import ProjectReportDialog from './ProjectReportDialog.vue'

const mocks = vi.hoisted(() => ({
  getAssessmentReportFinalization: vi.fn(),
  listReports: vi.fn(),
  listReportTemplateCatalog: vi.fn(),
  requestAssessmentReportFinalization: vi.fn(),
}))

vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  getAssessmentReportFinalization: mocks.getAssessmentReportFinalization,
  listReports: mocks.listReports,
  listReportTemplateCatalog: mocks.listReportTemplateCatalog,
  requestAssessmentReportFinalization: mocks.requestAssessmentReportFinalization,
}))

const wrappers: VueWrapper[] = []
const queryClients: QueryClient[] = []
const capability: ReportCapability = {
  supported: true,
  policy_version: 1,
  template_profile: 'assessment-report-packet-v1',
  reason: null,
}
const catalog: ReportTemplateCatalogEntry[] = [
  {
    key: 'security-report',
    is_default: true,
    supports_single_vulnerability: true,
    validation_error: null,
    name: '安全评估报告',
    description: '默认模板',
    digest_sha256: 'a'.repeat(64),
    compatibility_profiles: ['assessment-report-packet-v1'],
  },
  {
    key: 'management-report',
    is_default: false,
    supports_single_vulnerability: false,
    validation_error: null,
    name: '管理摘要报告',
    description: '管理层模板',
    digest_sha256: 'b'.repeat(64),
    compatibility_profiles: [],
  },
  {
    key: 'other-report',
    is_default: false,
    supports_single_vulnerability: true,
    validation_error: null,
    name: '自定义报告',
    description: '其他 profile 不影响正文有效性',
    digest_sha256: 'c'.repeat(64),
    compatibility_profiles: ['other-profile-v1'],
  },
]

const inventory: ReportInventoryPage = {
  items: [
    {
      project_id: 'project-a',
      project_title: '项目甲',
      source_status: 'available',
      task_run_id: 'run-old',
      artifact_id: 'report-artifact-a',
      artifact_uri: 'artifact://reports/历史报告.md',
      artifact_sha256: 'a'.repeat(64),
      artifact_size: 1024,
      created_at: '2026-08-20T00:00:00Z',
      template_key: 'security-report',
      template_version: 3,
      template_digest_sha256: 'a'.repeat(64),
      report_policy_version: 1,
      report_execution_plan_sha256: 'b'.repeat(64),
      report_prompt_digest_sha256: 'c'.repeat(64),
      input_snapshot_artifact_id: 'snapshot-old',
      input_snapshot_sha256: 'd'.repeat(64),
      completeness_rule_version: 1,
    },
  ],
  total: 1,
  page: 1,
  page_size: 1,
}

function finalization(
  overrides: Partial<AssessmentReportFinalization> = {},
): AssessmentReportFinalization {
  return {
    project_id: 'project-a',
    version: 1,
    status: 'pending',
    attempt_count: 1,
    claimed_by: null,
    lease_expires_at: null,
    next_attempt_at: null,
    artifact_id: null,
    last_error: '',
    generation_started_at: '2026-08-21T00:00:00Z',
    generation_token: 'generation-a',
    claim_token: null,
    requested_at: '2026-08-21T00:00:00Z',
    requested_by: 'user-a',
    template_key: 'security-report',
    template_version: 3,
    template_digest_sha256: 'a'.repeat(64),
    created_at: '2026-08-21T00:00:00Z',
    updated_at: '2026-08-21T00:00:00Z',
    completed_at: null,
    ...overrides,
  }
}

async function mountDialog(open = true, overrides: Record<string, unknown> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/reports', name: 'Reports', component: { template: '<div />' } },
      {
        path: '/projects/:id/artifacts',
        name: 'WorkspaceArtifacts',
        component: { template: '<div />' },
      },
    ],
  })
  await router.push('/projects/project-a/artifacts')
  await router.isReady()

  const wrapper = mount(ProjectReportDialog, {
    attachTo: document.body,
    props: {
      open,
      projectId: 'project-a',
      projectStatus: 'completed',
      capability,
      ...overrides,
    },
    global: {
      plugins: [createPinia(), [VueQueryPlugin, { queryClient }], router],
    },
  })
  wrappers.push(wrapper)
  queryClients.push(queryClient)
  return { queryClient, router, wrapper }
}

function buttonByText(text: string): HTMLButtonElement {
  const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === text,
  )
  if (!button) throw new Error(`找不到按钮：${text}`)
  return button
}

async function selectOption(label: string) {
  const trigger = document.body.querySelector<HTMLButtonElement>('[role="combobox"]')
  trigger?.dispatchEvent(
    new PointerEvent('pointerdown', { bubbles: true, button: 0, pointerType: 'mouse' }),
  )
  trigger?.click()
  await flushPromises()
  const option = [...document.body.querySelectorAll<HTMLElement>('[role="option"]')].find(
    (candidate) => candidate.textContent?.trim() === label,
  )
  if (!option) throw new Error(`找不到下拉选项：${label}`)
  option.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
  option.click()
  await flushPromises()
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

beforeEach(() => {
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
  vi.clearAllMocks()
  mocks.getAssessmentReportFinalization.mockResolvedValue(null)
  mocks.listReports.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 1 })
  mocks.listReportTemplateCatalog.mockResolvedValue(catalog)
  mocks.requestAssessmentReportFinalization.mockResolvedValue(finalization())
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  queryClients.splice(0).forEach((queryClient) => queryClient.clear())
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('ProjectReportDialog', () => {
  it.each(['deleted', 'unknown'] as const)('项目历史档案来源状态%s不影响既有阅读导出', async (status) => {
    mocks.listReports.mockResolvedValue({ ...inventory, items: inventory.items.map((item) => ({ ...item, source_status: status })) })
    mocks.getAssessmentReportFinalization.mockResolvedValue(finalization({ status: 'ready', source_status: 'deleted', stale: true }))
    await mountDialog()
    await flushPromises()
    const available = document.body.querySelector('[aria-label="已有报告"]')!
    expect(available.textContent).toContain(status === 'deleted' ? '历史档案 · 来源已删除' : '历史档案 · 来源状态未知')
    expect(available.querySelector('.report-read-link')?.getAttribute('href')).toContain('reportId=report-artifact-a')
    expect(available.textContent).toContain('导出报告')
    expect(document.body.querySelector('[aria-label="本次生成"]')?.textContent).toContain('历史档案 · 来源已删除')
  })

  it('preserves existing reports and marks an unconfirmed state when finalization cannot be loaded', async () => {
    mocks.listReports.mockResolvedValue(inventory)
    mocks.getAssessmentReportFinalization.mockRejectedValueOnce(new Error('报告状态暂不可读'))
    await mountDialog()
    await flushPromises()
    expect(document.body.textContent).toContain('状态未确认')
    expect(document.body.querySelector('.report-read-link')).not.toBeNull()
    expect(document.body.querySelector('[aria-label="本次生成"]')?.textContent).toContain(
      '报告状态暂不可读',
    )
    buttonByText('刷新状态').click()
    await flushPromises()
    expect(document.body.textContent).not.toContain('状态未确认')
    expect(mocks.getAssessmentReportFinalization).toHaveBeenCalledTimes(2)
  })

  it('keeps inventory reports independently readable when the latest generation fails at its output limit', async () => {
    mocks.listReports.mockResolvedValue(inventory)
    mocks.getAssessmentReportFinalization.mockResolvedValue(
      finalization({
        status: 'failed',
        artifact_id: 'unaccepted-current-artifact',
        last_error_code: 'model_output_limit',
        last_error: 'SDK stopped at the configured output budget',
        next_attempt_at: new Date(Date.now() - 60_000).toISOString(),
      }),
    )
    await mountDialog()
    await flushPromises()

    expect(mocks.listReports).toHaveBeenCalledWith(
      { project_id: 'project-a', page: 1, page_size: 1 },
      expect.any(AbortSignal),
    )
    const available = document.body.querySelector('[aria-label="已有报告"]')!
    const generation = document.body.querySelector('[aria-label="本次生成"]')!
    expect(available.textContent).toContain('历史报告.md')
    expect(available.textContent).not.toContain('生成失败')
    expect(available.querySelector('.report-read-link')?.getAttribute('href')).toContain(
      'reportId=report-artifact-a',
    )
    expect(available.textContent).toContain('导出报告')
    expect(generation.textContent).toContain('输出达到上限，当前段未完成')
    expect(generation.textContent).not.toContain('下次重试')
    const details = generation.querySelector<HTMLDetailsElement>('details')!
    expect(details.open).toBe(false)
    expect(details.textContent).toContain('SDK stopped at the configured output budget')
    expect(generation.querySelectorAll('.report-read-link')).toHaveLength(0)
    expect(buttonByText('重试生成').disabled).toBe(false)
  })

  it.each(['failed', 'cancelled', 'ready'] as const)(
    'stops polling terminal %s records with no retry plan',
    async (status) => {
      vi.useFakeTimers({
        toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'],
      })
      mocks.getAssessmentReportFinalization.mockResolvedValue(finalization({ status }))
      await mountDialog()
      await flushPromises()
      await vi.advanceTimersByTimeAsync(30_000)
      expect(mocks.getAssessmentReportFinalization).toHaveBeenCalledOnce()
    },
  )

  it('shows only future scheduled retries, checks once when due, then stops if no new plan exists', async () => {
    vi.useFakeTimers({
      toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'],
    })
    mocks.getAssessmentReportFinalization.mockResolvedValue(
      finalization({
        status: 'failed',
        next_attempt_at: new Date(Date.now() + 5_000).toISOString(),
      }),
    )
    await mountDialog()
    await flushPromises()
    expect(document.body.textContent).toContain('下次重试')
    expect(buttonByText('等待重试').disabled).toBe(true)
    await vi.advanceTimersByTimeAsync(5_200)
    await flushPromises()
    expect(mocks.getAssessmentReportFinalization).toHaveBeenCalledTimes(2)
    expect(document.body.textContent).not.toContain('下次重试')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(mocks.getAssessmentReportFinalization).toHaveBeenCalledTimes(2)
  })

  it('上次模板正文无效时选择明确默认模板而非目录首项，保留历史模板详情', async () => {
    mocks.listReportTemplateCatalog.mockResolvedValue([
      catalog[1],
      catalog[0],
      {
        ...catalog[2]!,
        key: 'old-free-text',
        validation_error: '模板正文为空',
      },
    ])
    mocks.getAssessmentReportFinalization.mockResolvedValue(
      finalization({ status: 'failed', template_key: 'old-free-text' }),
    )
    const { wrapper } = await mountDialog()
    await flushPromises()
    expect(document.body.querySelector('[role="combobox"]')?.textContent).toContain('安全评估报告')
    expect(document.body.querySelector('.report-technical-details')?.textContent).toContain('old-free-text')
    expect(wrapper.getComponent(Select).props('options')).toEqual([
      { value: 'management-report', label: '管理摘要报告' },
      { value: 'security-report', label: '安全评估报告' },
    ])
    buttonByText('重试生成').click()
    await flushPromises()
    expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledWith('project-a', {
      template_key: 'security-report',
    })
  })

  it('defers report queries until open and exposes the shared accessible Dialog contract', async () => {
    const { wrapper } = await mountDialog(false)
    await flushPromises()

    expect(mocks.getAssessmentReportFinalization).not.toHaveBeenCalled()
    expect(mocks.listReportTemplateCatalog).not.toHaveBeenCalled()

    await wrapper.setProps({ open: true })
    await flushPromises()

    const dialog = document.body.querySelector('[role="dialog"]')
    const titleId = dialog?.getAttribute('aria-labelledby')
    expect(titleId).toBeTruthy()
    expect(document.getElementById(titleId!)?.textContent).toBe('项目报告')
    expect(dialog?.hasAttribute('aria-describedby')).toBe(false)
    expect(dialog?.textContent).toContain('已有报告')
    expect(dialog?.textContent).toContain('本次生成')
    expect(document.body.querySelector('button[aria-label="关闭项目报告"]')).not.toBeNull()
    expect(mocks.getAssessmentReportFinalization).toHaveBeenCalledWith(
      'project-a',
      expect.any(AbortSignal),
    )
    expect(mocks.listReportTemplateCatalog).toHaveBeenCalledWith(expect.any(AbortSignal))
  })

  it.each([
    ['管理摘要报告', 'management-report'],
    ['自定义报告', 'other-report'],
  ])('显式选择%s并仅提交key，不依赖模板profiles或项目template_profile', async (label, key) => {
    const { queryClient } = await mountDialog(true, {
      capability: { ...capability, template_profile: null },
    })
    await flushPromises()

    expect(document.body.textContent).toContain('安全评估报告')
    await selectOption(label)
    expect(document.body.querySelector('[role="combobox"]')?.textContent).toContain(label)
    expect(mocks.requestAssessmentReportFinalization).not.toHaveBeenCalled()

    buttonByText('生成报告').click()
    await flushPromises()

    expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledWith('project-a', {
      template_key: key,
    })
    expect(
      queryClient.getQueryData<AssessmentReportFinalization>([
        'workspaceReportFinalization',
        'project-a',
      ])?.status,
    ).toBe('pending')
  })

  it('preserves a new retry template when failed-report polling returns the pinned template', async () => {
    const failed = finalization({ status: 'failed', last_error: 'generation failed' })
    mocks.getAssessmentReportFinalization.mockResolvedValue(failed)
    const { queryClient } = await mountDialog()
    await flushPromises()

    expect(document.body.querySelector('[role="combobox"]')?.textContent).toContain('安全评估报告')
    await selectOption('管理摘要报告')

    queryClient.setQueryData(['workspaceReportFinalization', 'project-a'], {
      ...failed,
      updated_at: '2026-08-21T00:01:00Z',
    })
    await flushPromises()

    expect(document.body.querySelector('[role="combobox"]')?.textContent).toContain('管理摘要报告')
    buttonByText('重试生成').click()
    await flushPromises()
    expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledWith('project-a', {
      template_key: 'management-report',
    })
  })

  it('requires explicit reselection when the selected template disappears instead of falling back', async () => {
    const { queryClient } = await mountDialog()
    await flushPromises()
    await selectOption('管理摘要报告')

    queryClient.setQueryData(
      ['reportTemplateCatalog'],
      catalog.filter((template) => template.key !== 'management-report'),
    )
    await flushPromises()

    expect(document.body.textContent).toContain('所选模板已删除或正文无效，请重新选择。')
    expect(document.body.querySelector('[role="combobox"]')?.textContent).toContain('管理摘要报告')
    expect(document.body.querySelector('[role="combobox"]')?.textContent).not.toContain(
      '安全评估报告',
    )
    expect(buttonByText('生成报告').disabled).toBe(true)
    buttonByText('生成报告').click()
    await flushPromises()
    expect(mocks.requestAssessmentReportFinalization).not.toHaveBeenCalled()

    await selectOption('安全评估报告')
    expect(document.body.textContent).not.toContain('所选模板已删除或正文无效')
    expect(buttonByText('生成报告').disabled).toBe(false)
    buttonByText('生成报告').click()
    await flushPromises()
    expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledWith('project-a', {
      template_key: 'security-report',
    })
  })

  it('blocks duplicate requests and dismissal while the request is in flight', async () => {
    const pending = deferred<AssessmentReportFinalization>()
    mocks.requestAssessmentReportFinalization.mockReturnValueOnce(pending.promise)
    const { wrapper } = await mountDialog()
    await flushPromises()

    buttonByText('生成报告').click()
    buttonByText('生成报告').click()
    await flushPromises()

    expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledTimes(1)
    expect(buttonByText('生成报告').disabled).toBe(true)
    expect(document.body.querySelector<HTMLButtonElement>('[role="combobox"]')?.disabled).toBe(true)
    expect(document.body.querySelector('button[aria-label="关闭项目报告"]')).toBeNull()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
    expect(wrapper.emitted('update:open')).toBeUndefined()

    pending.resolve(finalization())
    await flushPromises()
  })

  it.each(['active', 'paused', 'completed', 'stopped'] as const)(
    '%s 项目打开弹窗只读取状态和模板，用户显式确认后才生成',
    async (projectStatus) => {
      await mountDialog(true, { projectStatus })
      await flushPromises()

      expect(mocks.getAssessmentReportFinalization).toHaveBeenCalledWith(
        'project-a',
        expect.any(AbortSignal),
      )
      expect(mocks.listReportTemplateCatalog).toHaveBeenCalledWith(expect.any(AbortSignal))
      expect(document.body.querySelector('[role="combobox"]')).not.toBeNull()
      expect(buttonByText('生成报告').disabled).toBe(false)
      expect(mocks.requestAssessmentReportFinalization).not.toHaveBeenCalled()
      buttonByText('生成报告').click()
      await flushPromises()
      expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledExactlyOnceWith('project-a', {
        template_key: 'security-report',
      })
    },
  )

  it('暂停项目保留已有报告阅读入口，并允许显式再次生成', async () => {
    mocks.listReports.mockResolvedValue(inventory)
    mocks.getAssessmentReportFinalization.mockResolvedValue(
      finalization({ status: 'ready', artifact_id: 'report-artifact-a' }),
    )
    await mountDialog(true, { projectStatus: 'paused' })
    await flushPromises()

    const artifact = document.body.querySelector<HTMLAnchorElement>('.report-read-link')
    expect(artifact?.textContent).toBe('阅读报告')
    expect(artifact?.closest('details')).toBeNull()
    expect(artifact?.getAttribute('href')).toBe(
      '/reports?reportProject=project-a&reportProjectId=project-a&reportId=report-artifact-a',
    )
    expect(document.body.querySelector<HTMLDetailsElement>('.report-technical-details')?.open).toBe(
      false,
    )
    expect(mocks.listReportTemplateCatalog).toHaveBeenCalledWith(expect.any(AbortSignal))
    expect(mocks.requestAssessmentReportFinalization).not.toHaveBeenCalled()
    expect(buttonByText('再次生成').disabled).toBe(false)
    await selectOption('管理摘要报告')
    buttonByText('再次生成').click()
    await flushPromises()
    expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledExactlyOnceWith('project-a', {
      template_key: 'management-report',
    })
  })

  it.each([null, 3])(
    'keeps historical template version %s in collapsed details with a separate read action',
    async (version) => {
      mocks.listReports.mockResolvedValue(inventory)
      mocks.getAssessmentReportFinalization.mockResolvedValue(
        finalization({
          status: 'ready',
          artifact_id: 'report-artifact-a',
          template_version: version,
          report_execution_plan_sha256: 'c'.repeat(64),
          input_snapshot_artifact_id: 'snapshot-a',
        }),
      )
      const { router, wrapper } = await mountDialog()
      await flushPromises()

      const details = document.body.querySelector<HTMLDetailsElement>('.report-technical-details')!
      expect(details.open).toBe(false)
      expect(details.querySelector('summary')?.textContent).toBe('生成详情')
      expect(details.textContent).toContain('报告尝试')
      expect(details.textContent).toContain('请求时间')
      expect(details.textContent).toContain('user-a')
      expect(details.textContent).toContain('c'.repeat(64))
      expect(details.querySelector('strong')?.textContent).toBe(
        version == null ? 'security-report' : 'security-report · v3',
      )
      expect(details.textContent).not.toMatch(/v(?:null|undefined)/)
      details.querySelector('summary')!.click()
      await flushPromises()
      expect(details.open).toBe(true)
      expect(details.querySelector('a[href$="artifactId=snapshot-a"]')).not.toBeNull()

      const readLink = document.body.querySelector<HTMLAnchorElement>('.report-read-link')!
      expect(readLink.textContent).toBe('阅读报告')
      expect(readLink.closest('details')).toBeNull()
      expect(buttonByText('再次生成').disabled).toBe(false)
      readLink.click()
      await flushPromises()
      expect(router.currentRoute.value.query).toEqual({
        reportProject: 'project-a',
        reportProjectId: 'project-a',
        reportId: 'report-artifact-a',
      })
      expect(wrapper.emitted('update:open')).toEqual([[false]])
      expect(mocks.requestAssessmentReportFinalization).not.toHaveBeenCalled()
    },
  )

  it('暂停项目保留生成错误，并允许显式重试', async () => {
    mocks.getAssessmentReportFinalization.mockResolvedValue(
      finalization({ status: 'failed', last_error: 'generation failed' }),
    )
    await mountDialog(true, { projectStatus: 'paused' })
    await flushPromises()

    expect(document.body.textContent).toContain('报告生成错误')
    expect(document.body.querySelector('.report-technical-details')?.textContent).toContain('generation failed')
    expect(mocks.listReportTemplateCatalog).toHaveBeenCalledWith(expect.any(AbortSignal))
    expect(mocks.requestAssessmentReportFinalization).not.toHaveBeenCalled()
    expect(buttonByText('重试生成').disabled).toBe(false)
    buttonByText('重试生成').click()
    await flushPromises()
    expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledExactlyOnceWith('project-a', {
      template_key: 'security-report',
    })
  })

  it('defers paused report reads until the dialog opens', async () => {
    const { wrapper } = await mountDialog(false, { projectStatus: 'paused' })
    await flushPromises()

    expect(mocks.getAssessmentReportFinalization).not.toHaveBeenCalled()
    expect(mocks.listReportTemplateCatalog).not.toHaveBeenCalled()

    await wrapper.setProps({ open: true })
    await flushPromises()

    expect(mocks.getAssessmentReportFinalization).toHaveBeenCalledWith(
      'project-a',
      expect.any(AbortSignal),
    )
    expect(mocks.listReportTemplateCatalog).toHaveBeenCalledWith(expect.any(AbortSignal))
    expect(buttonByText('生成报告').disabled).toBe(false)
    expect(mocks.requestAssessmentReportFinalization).not.toHaveBeenCalled()
  })

  it.each([
    ['report_template_not_found', '所选模板已不存在，请刷新目录后重新选择。'],
    ['assessment_report_template_content_missing', '所选模板尚无正文，请联系管理员补全后再生成。'],
    ['assessment_report_template_invalid', '所选模板正文无效，请检查非空内容和大小限制。'],
  ])(
    '模板请求失败%s展示明确错误且允许修正后重试',
    async (code, message) => {
      mocks.requestAssessmentReportFinalization.mockRejectedValueOnce(
        new ApiError(code, 409, { detail: code }),
      )
      await mountDialog(true, { projectStatus: 'stopped' })
      await flushPromises()

      buttonByText('生成报告').click()
      await flushPromises()

      expect(document.body.textContent).toContain(message)
      expect(document.body.textContent).toContain('报告请求失败')
      expect(document.body.textContent).not.toContain('报告请求结果未确认')
      expect(buttonByText('生成报告').disabled).toBe(false)
    },
  )

  it.each([
    ['network', new Error('连接已断开')],
    ['server', new ApiError('HTTP 503', 503, null)],
  ])(
    'does not repeat a %s-uncertain POST and only unlocks after an explicit successful state refresh',
    async (_kind, error) => {
      mocks.requestAssessmentReportFinalization.mockRejectedValueOnce(error)
      await mountDialog()
      await flushPromises()
      const showToast = vi.spyOn(useUIStore(), 'showToast')

      buttonByText('生成报告').click()
      await flushPromises()
      expect(showToast).toHaveBeenCalledWith(expect.stringContaining('报告请求结果未确认'), 'warn')
      expect(document.body.querySelector('.ui-alert--warning')?.textContent).toContain(
        '报告请求结果未确认',
      )
      expect(document.body.textContent).toContain(
        '服务端可能已受理，请先刷新报告状态，不要重复提交',
      )
      expect(document.body.textContent).not.toContain('报告请求失败')
      expect(buttonByText('生成报告').disabled).toBe(true)
      buttonByText('生成报告').click()
      await flushPromises()
      expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledOnce()
      expect(mocks.getAssessmentReportFinalization).toHaveBeenCalledOnce()

      const refresh = deferred<AssessmentReportFinalization | null>()
      mocks.getAssessmentReportFinalization
        .mockRejectedValueOnce(new Error('刷新连接失败'))
        .mockReturnValueOnce(refresh.promise)
      buttonByText('刷新报告状态').click()
      await flushPromises()
      expect(document.body.textContent).toContain('报告请求结果未确认')
      expect(buttonByText('生成报告').disabled).toBe(true)
      expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledOnce()

      buttonByText('刷新报告状态').click()
      await flushPromises()
      expect(buttonByText('生成报告').disabled).toBe(true)
      expect(mocks.getAssessmentReportFinalization).toHaveBeenCalledTimes(3)
      expect(mocks.getAssessmentReportFinalization).toHaveBeenLastCalledWith(
        'project-a',
        expect.any(AbortSignal),
      )
      refresh.resolve(null)
      await flushPromises()
      expect(document.body.textContent).not.toContain('报告请求结果未确认')
      expect(document.body.textContent).not.toContain('服务端可能已受理')
      expect(buttonByText('生成报告').disabled).toBe(false)
      expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledOnce()

      buttonByText('生成报告').click()
      await flushPromises()
      expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledTimes(2)
    },
  )

  it('resets an uncertain request on project switch without replaying the POST', async () => {
    mocks.requestAssessmentReportFinalization.mockRejectedValueOnce(new Error('连接已断开'))
    const { wrapper } = await mountDialog()
    await flushPromises()
    buttonByText('生成报告').click()
    await flushPromises()
    expect(document.body.textContent).toContain('报告请求结果未确认')

    await wrapper.setProps({ projectId: 'project-b' })
    await flushPromises()
    expect(document.body.textContent).not.toContain('报告请求结果未确认')
    expect(mocks.getAssessmentReportFinalization).toHaveBeenLastCalledWith(
      'project-b',
      expect.any(AbortSignal),
    )
    expect(buttonByText('生成报告').disabled).toBe(false)
    expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledOnce()
  })
})
