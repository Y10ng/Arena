import { flushPromises, mount } from '@vue/test-utils'
import { reactive, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { ArtifactPreview } from '@/api/client'
import type { ReportInventoryPage } from '@/api/types'
import ReportManagement from './ReportManagement.vue'

const mocks = vi.hoisted(() => ({
  fetchArtifactPreview: vi.fn(),
  listReports: vi.fn(),
  query: {} as Record<string, string | undefined>,
  replace: vi.fn(),
  useQuery: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: mocks.query }),
  useRouter: () => ({ replace: mocks.replace }),
}))
vi.mock('@tanstack/vue-query', () => ({ useQuery: mocks.useQuery }))
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    fetchArtifactPreview: mocks.fetchArtifactPreview,
    listReports: mocks.listReports,
  }
})

const initialReportPage: ReportInventoryPage = {
  items: [
    {
      project_id: 'project / one',
      project_title: '项目一',
      task_run_id: 'run / one',
      artifact_id: 'report / one',
      artifact_uri: 'artifact://reports/Final%20Report.md',
      artifact_sha256: 'a'.repeat(64),
      artifact_size: 4096,
      created_at: '2026-08-21T00:00:00Z',
      template_key: 'durable',
      template_version: 2,
      template_digest_sha256: 'b'.repeat(64),
      report_policy_version: 3,
      report_execution_plan_sha256: 'c'.repeat(64),
      report_prompt_digest_sha256: 'd'.repeat(64),
      input_snapshot_artifact_id: 'snapshot / one',
      input_snapshot_sha256: 'e'.repeat(64),
      completeness_rule_version: 4,
    },
  ],
  total: 21,
  page: 1,
  page_size: 20,
}
const reportPage = ref<ReportInventoryPage>(initialReportPage)
const preview = ref<ArtifactPreview | undefined>()
const wrappers: VueWrapper[] = []

beforeEach(() => {
  reportPage.value = {
    ...initialReportPage,
    items: initialReportPage.items.map((item) => ({ ...item })),
  }
  Object.keys(mocks.query).forEach((key) => delete mocks.query[key])
  mocks.query = reactive(mocks.query)
  mocks.replace
    .mockReset()
    .mockImplementation(({ query }: { query: Record<string, string | undefined> }) => {
      Object.keys(mocks.query).forEach((key) => delete mocks.query[key])
      Object.assign(mocks.query, query)
      return Promise.resolve()
    })
  mocks.listReports.mockReset().mockResolvedValue(reportPage.value)
  mocks.fetchArtifactPreview.mockReset()
  preview.value = undefined
  mocks.useQuery
    .mockReset()
    .mockReturnValueOnce({
      data: reportPage,
      error: ref(null),
      isError: ref(false),
      isFetching: ref(false),
      isLoading: ref(false),
      refetch: vi.fn(),
    })
    .mockReturnValueOnce({
      data: preview,
      error: ref(null),
      isError: ref(false),
      isLoading: ref(false),
      refetch: vi.fn(),
    })
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

function mountPage() {
  const wrapper = mount(ReportManagement, {
    attachTo: document.body,
    global: {
      stubs: {
        RouterLink: { props: ['to'], template: '<a :data-to="JSON.stringify(to)"><slot /></a>' },
      },
    },
  })
  wrappers.push(wrapper)
  return wrapper
}

const matchingPreview = (): ArtifactPreview => ({
  artifact_id: 'report / one',
  project_id: 'project / one',
  task_run_id: null,
  kind: 'report',
  kind_deprecated: false,
  deprecation_message: null,
  content: '# 安全评估报告',
  size: 4096,
  bytes_read: 1024,
  truncated: true,
  sha256: 'a'.repeat(64),
})

describe('ReportManagement', () => {
  it('uses final inventory filters, hierarchical keys, pagination, and AbortSignal', async () => {
    Object.assign(mocks.query, {
      reportQ: 'final report',
      reportProject: 'project / one',
      reportTemplate: 'durable',
      dateFrom: '2026-08-01',
      dateTo: '2026-08-29',
      page: '3',
      pageSize: '50',
    })
    mountPage()
    const options = mocks.useQuery.mock.calls[0]?.[0] as {
      queryKey: {
        value: readonly [string, string, string, string, string, string, string, number, number]
      }
      queryFn: (context: {
        queryKey: readonly [string, string, string, string, string, string, string, number, number]
        signal: AbortSignal
      }) => Promise<ReportInventoryPage>
    }
    const controller = new AbortController()

    expect(options.queryKey.value).toEqual([
      'reports',
      'list',
      'final report',
      'project / one',
      'durable',
      '2026-08-01T00:00:00Z',
      '2026-08-29T23:59:59Z',
      3,
      50,
    ])
    await options.queryFn({ queryKey: options.queryKey.value, signal: controller.signal })
    expect(mocks.listReports).toHaveBeenCalledWith(
      {
        q: 'final report',
        project_id: 'project / one',
        template_key: 'durable',
        date_from: '2026-08-01T00:00:00Z',
        date_to: '2026-08-29T23:59:59Z',
        page: 3,
        page_size: 50,
      },
      controller.signal,
    )
  })

  it('opens composite deep links in a reader mutually exclusive with the directory and filters', async () => {
    mocks.query.reportProjectId = 'project / one'
    mocks.query.reportId = 'report / one'
    preview.value = matchingPreview()
    const wrapper = mountPage()
    await flushPromises()

    const previewOptions = mocks.useQuery.mock.calls[1]?.[0] as {
      queryKey: { value: readonly string[] }
      enabled: { value: boolean }
    }
    expect(previewOptions.queryKey.value).toEqual([
      'reports',
      'preview',
      'project / one',
      'report / one',
    ])
    expect(previewOptions.enabled.value).toBe(true)
    expect(wrapper.get('.report-directory').isVisible()).toBe(false)
    expect(wrapper.get('.report-filters').isVisible()).toBe(false)
    const reader = wrapper.get('[aria-label="报告阅读"]')
    expect(reader.isVisible()).toBe(true)
    expect(reader.get('.safe-report-markdown h1').text()).toBe('安全评估报告')
    expect(reader.get('[aria-label="报告正文"]').attributes('tabindex')).toBe('0')
    expect(wrapper.find('[role="tab"]').exists()).toBe(false)
    expect(reader.text()).toContain('当前仅显示报告开头')
    expect(reader.text()).toContain('目录仅覆盖已载入部分')
    expect(wrapper.get('a[download]').attributes('href')).toBe(
      '/projects/project%20%2F%20one/artifacts/report%20%2F%20one/content',
    )
    expect(wrapper.text()).not.toContain('执行计划摘要')
    expect(wrapper.text()).not.toContain('输入快照 Artifact')

    const rawButton = reader.findAll('button').find((button) => button.text() === '原文')!
    expect(rawButton.attributes('aria-pressed')).toBe('false')
    await rawButton.trigger('click')
    expect(reader.get('[aria-label="报告原文"] pre').text()).toBe('# 安全评估报告')
    expect(reader.find('.safe-report-markdown').exists()).toBe(false)
    expect(rawButton.text()).toBe('排版')
    expect(rawButton.attributes('aria-pressed')).toBe('true')
    await rawButton.trigger('click')
    expect(reader.get('.safe-report-markdown h1').text()).toBe('安全评估报告')
  })

  it('builds the chapter directory from rendered tokens without treating fenced headings as chapters', async () => {
    mocks.query.reportProjectId = 'project / one'
    mocks.query.reportId = 'report / one'
    preview.value = {
      ...matchingPreview(),
      content: '# **安全**评估 `报告`\n\n## 证据\n\n```md\n# 不是章节\n```\n\n## 证据\n\n后续工作\n---',
    }
    const wrapper = mountPage()
    await flushPromises()

    const directory = wrapper.get('nav[aria-label="当前报告章节目录"]')
    const entries = directory.findAll('button')
    expect(entries.map((entry) => entry.text())).toEqual(['安全评估 报告', '证据', '证据', '后续工作'])
    const headings = wrapper.findAll('.safe-report-markdown h1, .safe-report-markdown h2')
    expect(headings.map((heading) => heading.text())).toEqual(entries.map((entry) => entry.text()))
    expect(new Set(headings.map((heading) => heading.attributes('id'))).size).toBe(4)
    expect(wrapper.get('.safe-report-markdown pre code').text()).toContain('# 不是章节')

    await entries[2]!.trigger('click')
    expect(entries[2]!.attributes('aria-current')).toBe('location')
    expect(document.activeElement).toBe(headings[2]!.element)
    expect(mocks.replace).not.toHaveBeenCalled()
    expect(mocks.query.reportId).toBe('report / one')
  })

  it.each([null, 2])('shows historical template version %s only when present in on-demand file information', async (version) => {
    reportPage.value.items[0]!.template_version = version
    mocks.query.reportProjectId = 'project / one'
    mocks.query.reportId = 'report / one'
    preview.value = matchingPreview()
    const wrapper = mountPage()
    await flushPromises()

    expect(document.body.querySelector('.report-file-info')).toBeNull()
    await wrapper.findAll('button').find((button) => button.text() === '更多')!.trigger('click')
    await flushPromises()

    const info = document.body.querySelector('.report-file-info')!
    expect(info.textContent).toContain(version == null ? 'durable' : 'durable · v2')
    if (version == null) expect(info.textContent).not.toMatch(/durable\s*·\s*v/)
    expect(info.textContent).not.toMatch(/v(?:null|undefined)/)
    expect(info.querySelector('a')?.getAttribute('data-to')).toContain('WorkspaceVulnerabilities')
  })

  it('captures preview identity from the request key and forwards cancellation', async () => {
    mocks.query.reportProjectId = 'project / one'
    mocks.query.reportId = 'report / one'
    mountPage()
    const options = mocks.useQuery.mock.calls[1]?.[0] as {
      queryFn: (context: {
        queryKey: readonly string[]
        signal: AbortSignal
      }) => Promise<ArtifactPreview>
    }
    const signal = new AbortController().signal
    await options.queryFn({
      queryKey: ['reports', 'preview', 'captured-project', 'captured-artifact'],
      signal,
    })

    expect(mocks.fetchArtifactPreview).toHaveBeenCalledWith(
      'captured-project',
      'captured-artifact',
      signal,
    )
  })

  it('returns to the directory with filters, selection, raw mode, scroll positions, and focus preserved', async () => {
    Object.assign(mocks.query, {
      reportProjectId: 'project / one',
      reportId: 'report / one',
      reportQ: 'final report',
      reportProject: 'project / one',
      reportTemplate: 'durable',
      dateFrom: '2026-08-01',
      page: '2',
      pageSize: '10',
      keep: 'yes',
    })
    const originalQuery = { ...mocks.query }
    preview.value = matchingPreview()
    const wrapper = mountPage()
    await flushPromises()
    const directory = wrapper.get<HTMLElement>('.report-directory')
    directory.element.scrollTop = 180
    await wrapper.findAll('button').find((button) => button.text() === '原文')!.trigger('click')
    const pane = wrapper.get<HTMLElement>('[aria-label="报告原文"]')
    pane.element.scrollTop = 120
    await pane.trigger('scroll')

    await wrapper.get('.report-directory-back').trigger('click')
    expect(wrapper.get('.report-workspace').classes()).not.toContain('report-workspace--reading')
    expect(directory.isVisible()).toBe(true)
    expect(wrapper.get('.report-filters').isVisible()).toBe(true)
    expect(wrapper.get('.report-reader').isVisible()).toBe(false)
    expect(directory.element.scrollTop).toBe(180)
    expect(document.activeElement).toBe(wrapper.get('.report-directory-item').element)
    expect(mocks.query).toEqual(originalQuery)
    expect(mocks.replace).not.toHaveBeenCalled()
    await wrapper.get('.report-directory-item').trigger('click')
    await flushPromises()
    expect(wrapper.get('.report-workspace').classes()).toContain('report-workspace--reading')
    expect(directory.isVisible()).toBe(false)
    expect(wrapper.get('.report-reader').isVisible()).toBe(true)
    expect(wrapper.get('.report-reader-raw').text()).toBe('# 安全评估报告')
    expect(wrapper.get<HTMLElement>('[aria-label="报告原文"]').element.scrollTop).toBe(120)
    expect(wrapper.get('.report-directory-item').attributes('aria-current')).toBe('true')
    expect(mocks.query).toEqual(originalQuery)
    expect(document.activeElement).toBe(wrapper.get('.report-preview-title h1').element)
  })

  it.each(['project_id', 'artifact_id'] as const)('blocks rendering when preview %s does not match', async (field) => {
    mocks.query.reportProjectId = 'project / one'
    mocks.query.reportId = 'report / one'
    preview.value = { ...matchingPreview(), [field]: 'other-identity', content: '# 不应显示' }
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('报告预览身份不匹配')
    expect(wrapper.find('.safe-report-markdown').exists()).toBe(false)
    expect(wrapper.find('.report-reader-raw').exists()).toBe(false)
    expect(wrapper.find('nav[aria-label="当前报告章节目录"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('不应显示')
  })

  it('uses compact accessible directory entries with filename, project, and secondary time', () => {
    const wrapper = mountPage()
    const item = wrapper.get('.report-directory-item')

    expect(wrapper.find('table').exists()).toBe(false)
    expect(item.attributes('aria-label')).toBe('审阅报告：Final Report.md')
    expect(item.get('strong').attributes('title')).toBe('Final Report.md')
    expect(item.get('.report-item-project').attributes('title')).toBe('项目一')
    expect(item.get('time').attributes('datetime')).toBe('2026-08-21T00:00:00Z')
    expect(wrapper.get('.report-pagination').attributes('aria-label')).toBe('报告分页')
  })

  it('writes composite selection and pagination while preserving unrelated query values', async () => {
    mocks.query.keep = 'yes'
    const wrapper = mountPage()
    await wrapper.get('.report-directory-item').trigger('click')
    expect(mocks.replace).toHaveBeenCalledWith({
      query: {
        keep: 'yes',
        reportProjectId: 'project / one',
        reportId: 'report / one',
      },
    })

    await flushPromises()
    expect(wrapper.get('.report-directory').isVisible()).toBe(false)
    await wrapper.get('.report-directory-back').trigger('click')
    mocks.replace.mockClear()
    await wrapper
      .findAll('button')
      .find((button) => button.text().includes('下一页'))!
      .trigger('click')
    expect(mocks.replace).toHaveBeenCalledWith({
      query: {
        keep: 'yes',
        page: '2',
        reportId: undefined,
        reportProjectId: undefined,
      },
    })
  })
})
