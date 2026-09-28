import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
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
vi.mock('@/features/reports/SafeReportMarkdown.vue', () => ({
  default: defineComponent({
    props: { content: { type: String, required: true } },
    template: '<article data-testid="safe-report-markdown">{{ content }}</article>',
  }),
}))

const reportPage = ref<ReportInventoryPage>({
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
})
const preview = ref<ArtifactPreview | undefined>()

beforeEach(() => {
  Object.keys(mocks.query).forEach((key) => delete mocks.query[key])
  mocks.query.view = 'reports'
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

function mountPage() {
  return mount(ReportManagement, {
    global: {
      stubs: {
        RouterLink: { props: ['to'], template: '<a :data-to="JSON.stringify(to)"><slot /></a>' },
      },
    },
  })
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

  it('uses composite deep links, validates preview identity, and shows rendered/raw tabs', async () => {
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
    const previewTabs = wrapper.get('.report-preview-tabs')
    expect(previewTabs.get('[data-testid="safe-report-markdown"]').text()).toContain('安全评估报告')
    expect(previewTabs.find('.ui-tabs__content').exists()).toBe(true)
    expect(wrapper.findAll('[role="tab"]').map((tab) => tab.text())).toEqual([
      '渲染视图',
      '原始 Markdown',
    ])
    expect(wrapper.text()).not.toContain('执行计划摘要')
    expect(wrapper.text()).not.toContain('输入快照 Artifact')
    expect(wrapper.findAll('.report-download-link')[0]?.attributes('data-to')).toContain(
      'WorkspaceVulnerabilities',
    )
  })

  it('blocks rendering when preview project or artifact identity does not match', async () => {
    mocks.query.reportProjectId = 'project / one'
    mocks.query.reportId = 'report / one'
    preview.value = { ...matchingPreview(), project_id: 'other-project' }
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('报告预览身份不匹配')
    expect(wrapper.find('[data-testid="safe-report-markdown"]').exists()).toBe(false)
  })

  it('keeps the review action visible without a template column', () => {
    const wrapper = mountPage()

    expect(wrapper.find('.master-detail--fill-height').exists()).toBe(false)
    expect(wrapper.findAll('.report-list-card th').map((cell) => cell.text())).toEqual([
      '报告',
      '生成时间',
      '审阅',
    ])
    expect(wrapper.get('.report-list-item strong').attributes('title')).toBe(
      'artifact://reports/Final%20Report.md',
    )
    expect(wrapper.get('.report-list-item span').attributes('title')).toBe('项目一')
    expect(
      wrapper
        .findAll('button')
        .some((button) => button.attributes('aria-label') === '审阅报告：Final Report.md'),
    ).toBe(true)
  })

  it('writes composite selection and pagination while preserving unrelated query values', async () => {
    mocks.query.keep = 'yes'
    const wrapper = mountPage()
    await wrapper
      .findAll('button')
      .find((button) => button.text().includes('审阅'))
      ?.trigger('click')
    expect(mocks.replace).toHaveBeenCalledWith({
      query: {
        view: 'reports',
        keep: 'yes',
        reportProjectId: 'project / one',
        reportId: 'report / one',
      },
    })

    mocks.replace.mockClear()
    await wrapper
      .findAll('button')
      .find((button) => button.text().includes('下一页'))
      ?.trigger('click')
    expect(mocks.replace).toHaveBeenCalledWith({
      query: {
        view: 'reports',
        keep: 'yes',
        page: '2',
        reportId: undefined,
        reportProjectId: undefined,
      },
    })
  })
})
