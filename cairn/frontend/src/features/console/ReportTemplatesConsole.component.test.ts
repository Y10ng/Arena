import { flushPromises, mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia } from 'pinia'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ReportTemplatesConsole from './ReportTemplatesConsole.vue'

const mocks = vi.hoisted(() => ({
  archiveReportTemplate: vi.fn(),
  createReportTemplate: vi.fn(),
  createReportTemplateVersion: vi.fn(),
  getReportTemplateVersion: vi.fn(),
  listReportTemplates: vi.fn(),
  listReportTemplateVersions: vi.fn(),
}))

vi.mock('@/api/client', () => mocks)
vi.mock('@/components/patterns/TextEditor.vue', () => ({
  default: defineComponent({
    props: { modelValue: { type: String, default: '' } },
    emits: ['update:modelValue'],
    template:
      '<textarea aria-label="Markdown 模板" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  }),
}))

const wrappers: Array<ReturnType<typeof mount>> = []
const clients: QueryClient[] = []

async function mountConsole() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/console/report-templates', component: ReportTemplatesConsole }],
  })
  await router.push('/console/report-templates')
  await router.isReady()
  const wrapper = mount(ReportTemplatesConsole, {
    global: { plugins: [createPinia(), [VueQueryPlugin, { queryClient }], router] },
  })
  wrappers.push(wrapper)
  clients.push(queryClient)
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.listReportTemplates.mockResolvedValue([
    {
      key: 'security-report',
      name: '安全评估报告',
      description: '默认模板',
      latest_version: 1,
      created_at: '2026-08-29T00:00:00Z',
      archived_at: null,
    },
  ])
  mocks.listReportTemplateVersions.mockResolvedValue([
    {
      template_key: 'security-report',
      version: 1,
      digest_sha256: 'a'.repeat(64),
      message: '初始模板',
      created_by: 'admin-a',
      created_at: '2026-08-29T00:00:00Z',
    },
  ])
  mocks.getReportTemplateVersion.mockResolvedValue({
    template_key: 'security-report',
    version: 1,
    digest_sha256: 'a'.repeat(64),
    message: '初始模板',
    created_by: 'admin-a',
    created_at: '2026-08-29T00:00:00Z',
    markdown_content: '# 安全评估报告',
  })
  mocks.createReportTemplateVersion.mockResolvedValue({
    template_key: 'security-report',
    version: 2,
    digest_sha256: 'b'.repeat(64),
    message: '新增摘要',
    created_by: 'admin-a',
    created_at: '2026-08-29T01:00:00Z',
    markdown_content: '# 新版本',
  })
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  clients.splice(0).forEach((client) => client.clear())
  vi.restoreAllMocks()
})

describe('ReportTemplatesConsole', () => {
  it('loads versions, previews Markdown, and publishes an immutable version', async () => {
    const wrapper = await mountConsole()
    await flushPromises()

    expect(wrapper.text()).toContain('安全评估报告')
    expect(wrapper.text()).toContain('v1')
    expect(mocks.getReportTemplateVersion).toHaveBeenCalledWith('security-report', 1)

    const editor = wrapper.get('textarea[aria-label="Markdown 模板"]')
    await editor.setValue('# 新版本')
    const message = wrapper
      .findAll('input')
      .find((input) => input.attributes('placeholder') === '说明本次模板调整')
    if (!message) throw new Error('找不到版本说明输入框')
    await message.setValue('新增摘要')
    const publish = wrapper
      .findAll('button')
      .find((button) => button.text().includes('发布不可变版本 v2'))
    if (!publish) throw new Error('找不到发布按钮')
    await publish.trigger('click')
    await flushPromises()

    expect(mocks.createReportTemplateVersion).toHaveBeenCalledWith('security-report', {
      markdown_content: '# 新版本',
      message: '新增摘要',
    })
  })

  it('protects dirty drafts before switching templates', async () => {
    mocks.listReportTemplates.mockResolvedValue([
      ...(await mocks.listReportTemplates()),
      {
        key: 'second-report',
        name: '第二模板',
        description: '',
        latest_version: null,
        created_at: '2026-08-29T00:00:00Z',
        archived_at: null,
      },
    ])
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const wrapper = await mountConsole()
    await flushPromises()

    await wrapper.get('textarea[aria-label="Markdown 模板"]').setValue('# 未发布草稿')
    const second = wrapper.findAll('button').find((button) => button.text().includes('第二模板'))
    if (!second) throw new Error('找不到第二模板')
    await second.trigger('click')

    expect(confirm).toHaveBeenCalledOnce()
    expect(wrapper.find('.knowledge-set-item.active').text()).toContain('security-report')
  })
})
