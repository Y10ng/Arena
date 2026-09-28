import { flushPromises, mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { AssessmentReportFinalization, ReportCapability } from '@/api/types'
import ProjectReportDialog from './ProjectReportDialog.vue'

const mocks = vi.hoisted(() => ({
  getAssessmentReportFinalization: vi.fn(),
  listReportTemplateCatalog: vi.fn(),
  requestAssessmentReportFinalization: vi.fn(),
}))

vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  getAssessmentReportFinalization: mocks.getAssessmentReportFinalization,
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
  return { queryClient, wrapper }
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
  mocks.listReportTemplateCatalog.mockResolvedValue([
    {
      key: 'security-report',
      name: '安全评估报告',
      description: '默认模板',
      version: 3,
      digest_sha256: 'a'.repeat(64),
      compatibility_profiles: ['assessment-report-packet-v1'],
    },
    {
      key: 'management-report',
      name: '管理摘要报告',
      description: '管理层模板',
      version: 2,
      digest_sha256: 'b'.repeat(64),
      compatibility_profiles: ['assessment-report-packet-v1'],
    },
    {
      key: 'other-report',
      name: '不兼容模板',
      description: '其他 profile',
      version: 1,
      digest_sha256: 'c'.repeat(64),
      compatibility_profiles: ['other-profile-v1'],
    },
  ])
  mocks.requestAssessmentReportFinalization.mockResolvedValue(finalization())
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  queryClients.splice(0).forEach((queryClient) => queryClient.clear())
  document.body.innerHTML = ''
})

describe('ProjectReportDialog', () => {
  it('defers report queries until open and exposes the shared accessible Dialog contract', async () => {
    const { wrapper } = await mountDialog(false)
    await flushPromises()

    expect(mocks.getAssessmentReportFinalization).not.toHaveBeenCalled()
    expect(mocks.listReportTemplateCatalog).not.toHaveBeenCalled()

    await wrapper.setProps({ open: true })
    await flushPromises()

    const dialog = document.body.querySelector('[role="dialog"]')
    expect(dialog?.getAttribute('aria-labelledby')).toBeTruthy()
    expect(dialog?.getAttribute('aria-describedby')).toBeTruthy()
    expect(dialog?.textContent).toContain('选择兼容的不可变模板版本')
    expect(document.body.querySelector('button[aria-label="关闭生成报告"]')).not.toBeNull()
    expect(mocks.getAssessmentReportFinalization).toHaveBeenCalledWith(
      'project-a',
      expect.any(AbortSignal),
    )
    expect(mocks.listReportTemplateCatalog).toHaveBeenCalledWith(expect.any(AbortSignal))
  })

  it('selects a second compatible template and submits its immutable version', async () => {
    const { queryClient } = await mountDialog()
    await flushPromises()

    expect(document.body.textContent).toContain('安全评估报告 · v3')
    await selectOption('管理摘要报告 · v2')
    expect(document.body.querySelector('[role="combobox"]')?.textContent).toContain(
      '管理摘要报告 · v2',
    )
    expect(document.body.textContent).not.toContain('不兼容模板')

    buttonByText('生成报告').click()
    await flushPromises()

    expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledWith('project-a', {
      template_key: 'management-report',
      template_version: 2,
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

    expect(document.body.querySelector('[role="combobox"]')?.textContent).toContain(
      '安全评估报告 · v3',
    )
    await selectOption('管理摘要报告 · v2')

    queryClient.setQueryData(['workspaceReportFinalization', 'project-a'], {
      ...failed,
      updated_at: '2026-08-21T00:01:00Z',
    })
    await flushPromises()

    expect(document.body.querySelector('[role="combobox"]')?.textContent).toContain(
      '管理摘要报告 · v2',
    )
    buttonByText('重试生成').click()
    await flushPromises()
    expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledWith('project-a', {
      template_key: 'management-report',
      template_version: 2,
    })
  })

  it('blocks duplicate requests and dismissal while the request is in flight', async () => {
    const pending = deferred<AssessmentReportFinalization>()
    mocks.requestAssessmentReportFinalization.mockReturnValueOnce(pending.promise)
    await mountDialog()
    await flushPromises()

    buttonByText('生成报告').click()
    buttonByText('生成报告').click()
    await flushPromises()

    expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledTimes(1)
    expect(document.body.querySelector('button[aria-label="关闭生成报告"]')).toBeNull()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()

    pending.resolve(finalization())
    await flushPromises()
  })

  it('shows active-project guidance without querying report resources', async () => {
    await mountDialog(true, { projectStatus: 'active' })
    await flushPromises()

    expect(document.body.textContent).toContain('请先结束评估或等待项目完成')
    expect(mocks.getAssessmentReportFinalization).not.toHaveBeenCalled()
    expect(mocks.listReportTemplateCatalog).not.toHaveBeenCalled()
  })

  it('reads paused report state without exposing generation controls', async () => {
    await mountDialog(true, { projectStatus: 'paused' })
    await flushPromises()

    expect(document.body.textContent).toContain('暂停仅保留未完成工作')
    expect(mocks.getAssessmentReportFinalization).toHaveBeenCalledWith(
      'project-a',
      expect.any(AbortSignal),
    )
    expect(mocks.listReportTemplateCatalog).not.toHaveBeenCalled()
    expect(mocks.requestAssessmentReportFinalization).not.toHaveBeenCalled()
    expect(document.body.querySelector('[role="combobox"]')).toBeNull()
    expect(document.body.textContent).not.toContain('暂无可用报告模板')
    expect(
      [...document.body.querySelectorAll('button')].some((button) =>
        ['生成报告', '重试生成', '生成新版本'].includes(button.textContent?.trim() || ''),
      ),
    ).toBe(false)
  })

  it('shows an existing ready report while paused without allowing regeneration', async () => {
    mocks.getAssessmentReportFinalization.mockResolvedValue(
      finalization({ status: 'ready', artifact_id: 'report-artifact-a' }),
    )
    await mountDialog(true, { projectStatus: 'paused' })
    await flushPromises()

    const artifact = [...document.body.querySelectorAll<HTMLAnchorElement>('a')].find(
      (link) => link.textContent?.trim() === 'report-artifact-a',
    )
    expect(document.body.textContent).toContain('暂停仅保留未完成工作')
    expect(artifact?.getAttribute('href')).toBe(
      '/projects/project-a/artifacts?artifactId=report-artifact-a',
    )
    expect(mocks.listReportTemplateCatalog).not.toHaveBeenCalled()
    expect(mocks.requestAssessmentReportFinalization).not.toHaveBeenCalled()
    expect(document.body.querySelector('[role="combobox"]')).toBeNull()
  })

  it('shows an existing failed report while paused without allowing retry', async () => {
    mocks.getAssessmentReportFinalization.mockResolvedValue(
      finalization({ status: 'failed', last_error: 'generation failed' }),
    )
    await mountDialog(true, { projectStatus: 'paused' })
    await flushPromises()

    expect(document.body.textContent).toContain('报告生成错误')
    expect(document.body.textContent).toContain('generation failed')
    expect(mocks.listReportTemplateCatalog).not.toHaveBeenCalled()
    expect(mocks.requestAssessmentReportFinalization).not.toHaveBeenCalled()
    expect(document.body.querySelector('[role="combobox"]')).toBeNull()
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
    expect(mocks.listReportTemplateCatalog).not.toHaveBeenCalled()
  })

  it('allows stopped projects and explains the incomplete snapshot semantics', async () => {
    await mountDialog(true, { projectStatus: 'stopped' })
    await flushPromises()

    expect(document.body.textContent).toContain('将生成结束时点报告')
    expect(document.body.textContent).toContain('属于未完成评估报告')
    expect(mocks.getAssessmentReportFinalization).toHaveBeenCalledWith(
      'project-a',
      expect.any(AbortSignal),
    )

    buttonByText('生成报告').click()
    await flushPromises()
    expect(mocks.requestAssessmentReportFinalization).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['assessment_report_tasks_not_quiescent', '请等待 Worker 完全退出后重试'],
    ['assessment_report_stopped_tasks_not_quiescent', '请等待 Worker 完全退出后重试'],
    ['project_pause_draining', '请等待收尾完成后重试'],
  ])('maps stopped-project eligibility failure %s to actionable guidance', async (code, message) => {
    mocks.requestAssessmentReportFinalization.mockRejectedValueOnce(new Error(code))
    await mountDialog(true, { projectStatus: 'stopped' })
    await flushPromises()

    buttonByText('生成报告').click()
    await flushPromises()

    expect(document.body.textContent).toContain(message)
  })
})
