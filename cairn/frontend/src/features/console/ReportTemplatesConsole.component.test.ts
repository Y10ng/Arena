import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia } from 'pinia'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import type { ReportTemplate, UpdateReportTemplateRequest } from '@/api/types'
import { ConfirmAction } from '@/components/patterns'
import ReportTemplatesConsole from './ReportTemplatesConsole.vue'

const mocks = vi.hoisted(() => ({
  createReportTemplate: vi.fn(),
  deleteReportTemplate: vi.fn(),
  getReportTemplate: vi.fn(),
  listReportTemplates: vi.fn(),
  updateReportTemplate: vi.fn(),
}))
vi.mock('@/api/client', () => ({
  ...mocks,
  ApiError: class ApiError extends Error {
    readonly status: number
    readonly data: unknown
    constructor(message: string, status: number, data: unknown) {
      super(message)
      this.status = status
      this.data = data
    }
  },
}))
vi.mock('@/components/MonacoEditor.vue', () => ({
  __esModule: true,
  default: defineComponent({
    props: { modelValue: { type: String, default: '' }, disabled: Boolean },
    emits: ['update:modelValue'],
    template:
      '<textarea aria-label="Markdown 模板" :disabled="disabled" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  }),
}))

const wrappers: VueWrapper[] = []
const clients: QueryClient[] = []
let templates: ReportTemplate[]
const template = (
  key = 'security-report',
  markdown: string | null = '# 安全评估报告',
): ReportTemplate => ({
  key,
  is_default: false,
  supports_single_vulnerability: true,
  validation_error: null,
  name: key === 'security-report' ? '安全评估报告' : '另一模板',
  description: '默认模板',
  markdown_content: markdown,
  digest_sha256: markdown === null ? null : 'a'.repeat(64),
  compatibility_profiles: markdown === null ? [] : ['assessment-report-packet-v1'],
  created_at: '2026-08-29T00:00:00Z',
  updated_at: '2026-08-29T00:00:00Z',
})
const deferred = <T>() => {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((accept, fail) => {
    resolve = accept
    reject = fail
  })
  return { promise, resolve, reject }
}
const button = (wrapper: VueWrapper, text: string) => {
  const target = wrapper.findAll('button').find((item) => item.text().trim() === text || (item.find('strong').exists() && item.find('strong').text() === `${text}.md`))
  if (!target) throw new Error(`找不到按钮：${text}`)
  return target
}
const editor = (wrapper: VueWrapper) => wrapper.get('textarea[aria-label="Markdown 模板"]')
const nameInput = (wrapper: VueWrapper) => wrapper.get('input[placeholder="安全评估报告"]')
const descriptionInput = (wrapper: VueWrapper) =>
  wrapper.get('.report-template-description textarea')

async function mountConsole() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/console/report-templates', component: ReportTemplatesConsole },
      { path: '/other', component: defineComponent({ template: '<p>其他页面</p>' }) },
    ],
  })
  await router.push('/console/report-templates')
  await router.isReady()
  const wrapper = mount(
    defineComponent({ components: { RouterView }, template: '<RouterView />' }),
    {
      attachTo: document.body,
      global: { plugins: [createPinia(), [VueQueryPlugin, { queryClient }], router] },
    },
  )
  wrappers.push(wrapper)
  clients.push(queryClient)
  await flushPromises()
  const edit = wrapper.findAll('button').find((item) => item.text() === '编辑')
  if (edit) { await edit.trigger('click'); await flushPromises() }
  return { wrapper, router, queryClient }
}

beforeEach(() => {
  Object.values(mocks).forEach((mock) => mock.mockReset())
  templates = [template()]
  mocks.listReportTemplates.mockImplementation(async () => [...templates])
  mocks.getReportTemplate.mockImplementation(async (key: string) =>
    templates.find((item) => item.key === key),
  )
  mocks.updateReportTemplate.mockImplementation(
    async (key: string, body: UpdateReportTemplateRequest) => {
      const saved = { ...template(key), ...body }
      templates = templates.map((item) => (item.key === key ? saved : item))
      return saved
    },
  )
  mocks.createReportTemplate.mockImplementation(async (body: UpdateReportTemplateRequest) => {
    const saved = { ...template('generated-key'), ...body }
    templates.push(saved)
    return saved
  })
  mocks.deleteReportTemplate.mockImplementation(async (key: string) => {
    templates = templates.filter((item) => item.key !== key)
  })
})
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  clients.splice(0).forEach((client) => client.clear())
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('ReportTemplatesConsole current CRUD', () => {
  it('starts plain Markdown without requiring a default and keeps both workspace panes', async () => {
    templates = [{ ...template('system-default', null), is_default: true, validation_error: '正文缺失' }]
    const { wrapper } = await mountConsole()
    await button(wrapper, '新建').trigger('click')
    expect((editor(wrapper).element as HTMLTextAreaElement).value).toBe('')
    expect(wrapper.find('.report-template-directory').exists()).toBe(true)
    expect(wrapper.find('.report-template-detail').exists()).toBe(true)
    await wrapper.get('[data-template-key="system-default"]').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('button').some((item) => item.text() === '删除模板')).toBe(false)
  })

  it('safely previews and saves the current name, description and exact Markdown together', async () => {
    const { wrapper } = await mountConsole()
    expect(mocks.getReportTemplate).toHaveBeenCalledWith('security-report', expect.any(AbortSignal))
    const markdown =
      '\n# 当前模板\n\n**排版内容**\n\n![远程图](https://example.invalid/a.png)\n\n<script>bad()</script>\n'
    await nameInput(wrapper).setValue('当前名称')
    await descriptionInput(wrapper).setValue('当前说明')
    await editor(wrapper).setValue(markdown)
    await button(wrapper, '预览').trigger('click')
    await flushPromises()
    expect(wrapper.get('.safe-report-markdown h1').text()).toBe('当前模板')
    expect(wrapper.get('.safe-report-markdown strong').text()).toBe('排版内容')
    expect(wrapper.find('.safe-report-markdown img').exists()).toBe(false)
    expect(wrapper.find('.safe-report-markdown script').exists()).toBe(false)
    await button(wrapper, '编辑').trigger('click')
    expect((editor(wrapper).element as HTMLTextAreaElement).value).toBe(markdown)
    await button(wrapper, '保存').trigger('click')
    await flushPromises()
    expect(mocks.updateReportTemplate).toHaveBeenCalledWith('security-report', {
      name: '当前名称',
      description: '当前说明',
      markdown_content: markdown,
      compatibility_profiles: ['assessment-report-packet-v1'],
    })
    expect(wrapper.get('.report-template-dirty').text()).toBe('')
    expect(wrapper.text()).not.toMatch(/发布新版本|归档模板|版本历史/)
  })

  it('creates a complete template in one request without asking the user for a key', async () => {
    templates[0]!.is_default = true
    const { wrapper } = await mountConsole()
    await button(wrapper, '新建').trigger('click')
    await nameInput(wrapper).setValue('新模板')
    await descriptionInput(wrapper).setValue('说明')
    await editor(wrapper).setValue('# 首次正文')
    await button(wrapper, '保存').trigger('click')
    await flushPromises()
    expect(mocks.createReportTemplate).toHaveBeenCalledWith({
      name: '新模板',
      description: '说明',
      markdown_content: '# 首次正文',
    })
    expect(wrapper.get('.report-template-item.active strong').text()).toBe('新模板.md')
    expect((editor(wrapper).element as HTMLTextAreaElement).value).toBe('# 首次正文')
    expect(button(wrapper, '保存').attributes('disabled')).toBeDefined()
  })

  it('allows an old empty-content record to receive a body and omits empty profiles', async () => {
    templates = [template('security-report', null)]
    const { wrapper } = await mountConsole()
    expect(wrapper.text()).toContain('安全评估报告.md')
    await editor(wrapper).setValue('# 补全正文')
    await button(wrapper, '保存').trigger('click')
    await flushPromises()
    expect(mocks.updateReportTemplate).toHaveBeenCalledWith('security-report', {
      name: '安全评估报告',
      description: '默认模板',
      markdown_content: '# 补全正文',
    })
  })

  it('awaits discard decisions on switch, create, back, reset and route leave', async () => {
    templates.push(template('other'))
    const { wrapper, router } = await mountConsole()
    const discard = () => wrapper.findAllComponents(ConfirmAction).find((item) => item.props('title') === '放弃模板草稿？')!
    await nameInput(wrapper).setValue('未保存名称')
    for (const label of ['另一模板', '新建', '重置']) {
      await button(wrapper, label).trigger('click')
      await flushPromises()
      expect(discard().props('open')).toBe(true)
      expect((nameInput(wrapper).element as HTMLInputElement).value).toBe('未保存名称')
      discard().vm.$emit('update:open', false)
      await flushPromises()
      expect((nameInput(wrapper).element as HTMLInputElement).value).toBe('未保存名称')
    }
    const leaving = router.push('/other')
    await flushPromises()
    expect(discard().props('open')).toBe(true)
    expect(router.currentRoute.value.path).toBe('/console/report-templates')
    discard().vm.$emit('update:open', false)
    await leaving
    expect(router.currentRoute.value.path).toBe('/console/report-templates')
    expect(wrapper.get('.report-template-item.active strong').text()).toBe('安全评估报告.md')
    await button(wrapper, '重置').trigger('click')
    discard().vm.$emit('confirm')
    await flushPromises()
    expect((nameInput(wrapper).element as HTMLInputElement).value).toBe('安全评估报告')
    expect(mocks.updateReportTemplate).not.toHaveBeenCalled()
    expect(mocks.createReportTemplate).not.toHaveBeenCalled()
  })

  it('locks editing and route changes through save and the following directory refresh', async () => {
    const saving = deferred<ReportTemplate>()
    const refresh = deferred<ReportTemplate[]>()
    const { wrapper, router } = await mountConsole()
    await editor(wrapper).setValue('# 保存中')
    mocks.updateReportTemplate.mockReturnValueOnce(saving.promise)
    mocks.listReportTemplates.mockReturnValueOnce(refresh.promise)
    await button(wrapper, '保存').trigger('click')
    expect(editor(wrapper).attributes('disabled')).toBeDefined()
    expect(nameInput(wrapper).attributes('disabled')).toBeDefined()
    expect(button(wrapper, '新建').attributes('disabled')).toBeDefined()
    await router.push('/other')
    expect(router.currentRoute.value.path).toBe('/console/report-templates')
    saving.resolve(template('security-report', '# 已保存'))
    await flushPromises()
    expect(editor(wrapper).attributes('disabled')).toBeDefined()
    refresh.resolve([template('security-report', '# 已保存')])
    await flushPromises()
    expect(editor(wrapper).attributes('disabled')).toBeUndefined()
    expect((editor(wrapper).element as HTMLTextAreaElement).value).toBe('# 已保存')
  })

  it('rejects a late old read after switching to another template', async () => {
    templates.push(template('other', '# 另一正文'))
    const reading = deferred<ReportTemplate>()
    mocks.getReportTemplate.mockReturnValueOnce(reading.promise)
    const { wrapper } = await mountConsole()
    await button(wrapper, '另一模板').trigger('click')
    await flushPromises()
    await button(wrapper, '编辑').trigger('click')
    await editor(wrapper).setValue('# 新草稿')
    reading.resolve(template('security-report', '# 迟到正文'))
    await flushPromises()
    expect((editor(wrapper).element as HTMLTextAreaElement).value).toBe('# 新草稿')
  })

  it('rejects a late old read after starting a new template', async () => {
    templates[0]!.is_default = true
    const reading = deferred<ReportTemplate>()
    mocks.getReportTemplate.mockReturnValueOnce(reading.promise)
    const { wrapper } = await mountConsole()
    await button(wrapper, '新建').trigger('click')
    await flushPromises()
    await editor(wrapper).setValue('# 新建草稿')
    reading.resolve(template('security-report', '# 迟到正文'))
    await flushPromises()
    expect((editor(wrapper).element as HTMLTextAreaElement).value).toBe('# 新建草稿')
    expect(button(wrapper, '保存').exists()).toBe(true)
  })

  it.each(['read failure', 'identity mismatch'])(
    'blocks saving an unavailable detail: %s',
    async (kind) => {
      if (kind === 'read failure')
        mocks.getReportTemplate.mockRejectedValueOnce(new Error('受控读取失败'))
      else mocks.getReportTemplate.mockResolvedValueOnce(template('wrong-key', '# 错误正文'))
      const { wrapper } = await mountConsole()
      expect(wrapper.text()).toContain(kind === 'read failure' ? '受控读取失败' : '身份不匹配')
      expect(wrapper.find('textarea[aria-label="Markdown 模板"]').exists()).toBe(false)
      expect(button(wrapper, '保存').attributes('disabled')).toBeDefined()
    },
  )

  it('retains denied and unconfirmed save input without automatically resending', async () => {
    const { wrapper } = await mountConsole()
    await editor(wrapper).setValue('# 保留草稿')
    mocks.updateReportTemplate.mockRejectedValueOnce(new TypeError('网络中断'))
    await button(wrapper, '保存').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('保存模板结果未确认')
    expect((editor(wrapper).element as HTMLTextAreaElement).value).toBe('# 保留草稿')
    expect(mocks.updateReportTemplate).toHaveBeenCalledTimes(1)
    mocks.updateReportTemplate.mockRejectedValueOnce(new ApiError('无权编辑', 403, null))
    await button(wrapper, '保存').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('保存模板失败：无权编辑')
    expect((editor(wrapper).element as HTMLTextAreaElement).value).toBe('# 保留草稿')
  })

  it('truly deletes a built-in or custom template without affecting accepted reports', async () => {
    const { wrapper } = await mountConsole()
    expect(wrapper.find('button[aria-label="模板更多操作"]').exists()).toBe(true)
    wrapper.findComponent({ name: 'UiDropdownMenu' }).vm.$emit('select', 'delete')
    await flushPromises()
    const confirmation = wrapper.findAllComponents(ConfirmAction).find((item) => item.props('title') === '删除报告模板')!
    expect(confirmation.props('description')).toContain('已生成的报告和已受理任务不受影响')
    confirmation.vm.$emit('confirm')
    await flushPromises()
    expect(mocks.deleteReportTemplate).toHaveBeenCalledWith('security-report')
    expect(wrapper.find('.report-template-item').exists()).toBe(false)
    expect(wrapper.text()).toContain('暂无报告模板')
  })

  it('does not overwrite a dirty draft when the directory refreshes', async () => {
    const { wrapper, queryClient } = await mountConsole()
    await editor(wrapper).setValue('# 本地未保存')
    queryClient.setQueryData(['reportTemplates'], [template('security-report', '# 后台新正文')])
    await flushPromises()
    expect((editor(wrapper).element as HTMLTextAreaElement).value).toBe('# 本地未保存')
  })

  it('ignores a save that resolves after the workspace unmounts', async () => {
    const saving = deferred<ReportTemplate>()
    const { wrapper } = await mountConsole()
    await editor(wrapper).setValue('# 卸载前提交')
    mocks.updateReportTemplate.mockReturnValueOnce(saving.promise)
    await button(wrapper, '保存').trigger('click')
    const reads = mocks.listReportTemplates.mock.calls.length
    wrapper.unmount()
    wrappers.splice(wrappers.indexOf(wrapper), 1)
    saving.resolve(template())
    await flushPromises()
    expect(mocks.listReportTemplates).toHaveBeenCalledTimes(reads)
  })
})
