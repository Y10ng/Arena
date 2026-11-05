import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Select } from '@/components/ui'
import ReportExportMenu from './ReportExportMenu.vue'
import VulnerabilityReportExport from '@/features/vulnerabilities/VulnerabilityReportExport.vue'

const mocks = vi.hoisted(() => ({
  catalog: vi.fn(),
  fetchText: vi.fn(),
  create: vi.fn(),
  download: vi.fn(),
  finalization: vi.fn(),
  request: vi.fn(),
  vulnerability: vi.fn(),
}))
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  listReportTemplateCatalog: mocks.catalog,
  fetchText: mocks.fetchText,
  getAssessmentReportFinalization: mocks.finalization,
  requestAssessmentReportFinalization: mocks.request,
  getVulnerability: mocks.vulnerability,
}))
vi.mock('./reportExport', async (original) => ({
  ...(await original<typeof import('./reportExport')>()),
  createReportExport: mocks.create,
  downloadReportBlob: mocks.download,
}))
const wrappers: VueWrapper[] = []
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}
async function chooseFormat(wrapper: VueWrapper, label = 'Markdown（.md）') {
  await wrapper.get('button').trigger('click')
  await flushPromises()
  const items = [...document.body.querySelectorAll<HTMLElement>('[role="menuitem"]')]
  expect(items.map((item) => item.textContent?.trim())).toEqual([
    'Markdown（.md）',
    'Markdown 证据包（.tar）',
    'Word（.docx）',
    'HTML（.html）',
  ])
  items.find((item) => item.textContent?.trim() === label)!.click()
  await flushPromises()
}
function confirmButton() {
  return [...document.body.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')].find(
    (button) => button.textContent?.startsWith('导出 '),
  )!
}
beforeEach(() => {
  vi.clearAllMocks()
  mocks.catalog.mockResolvedValue([])
  mocks.fetchText.mockResolvedValue('# 完整正文\n\n末尾\n')
  mocks.finalization.mockResolvedValue(null)
  mocks.vulnerability.mockResolvedValue({ project_id: 'p', id: 'v', observation_id: 'obs-latest' })
  mocks.create.mockResolvedValue(new Blob(['受控导出']))
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
})
afterEach(() => {
  vi.useRealTimers()
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('共享报告导出菜单', () => {
  it('单漏洞固定Observation且不选择模板，目录和原文可读，刷新后导出当前预览', async () => {
    const wrapper = mount(VulnerabilityReportExport, {
      attachTo: document.body,
      props: {
        projectId: 'project-a',
        vulnerabilityId: 'vuln-a',
        title: '受控漏洞',
        observationId: 'obs-selected',
      },
    })
    wrappers.push(wrapper)
    await chooseFormat(wrapper)
    expect(mocks.catalog).not.toHaveBeenCalled()
    expect(mocks.create).not.toHaveBeenCalled()
    expect(wrapper.findAllComponents(Select)).toHaveLength(1)
    expect(wrapper.getComponent(Select).props('modelValue')).toBe('md')
    expect(document.body.textContent).not.toContain('报告模板')
    expect(document.querySelector('[aria-label="当前报告章节目录"]')).toBeNull()
    const directoryToggle = document.querySelector<HTMLButtonElement>('.report-reader-directory-toggle')!
    directoryToggle.click()
    await flushPromises()
    expect(directoryToggle.getAttribute('aria-expanded')).toBe('true')
    const directory = document.querySelector<HTMLElement>('[aria-label="当前报告章节目录"]')
    expect(directory?.textContent).toContain('完整正文')
    const hash = window.location.hash
    directory?.querySelector('button')?.click()
    await flushPromises()
    expect(window.location.hash).toBe(hash)
    expect(document.querySelector('[aria-label="当前报告章节目录"]')).toBeNull()
    expect(directoryToggle.getAttribute('aria-expanded')).toBe('false')
    const raw = [...document.querySelectorAll<HTMLButtonElement>('.report-reader-actions button')].find((button) => button.textContent?.trim() === '原文')!
    raw.click()
    await flushPromises()
    expect(document.querySelector('.report-reader-raw')?.textContent).toBe('# 完整正文\n\n末尾\n')
    expect(mocks.fetchText).toHaveBeenCalledTimes(1)
    mocks.fetchText.mockResolvedValueOnce('# 刷新后的完整正文\n\n新末尾\n')
    const refresh = [...document.querySelectorAll<HTMLButtonElement>('.report-reader-actions button')].find((button) => button.textContent?.trim() === '刷新预览')!
    refresh.click()
    await flushPromises()
    expect(document.querySelector('.report-reader-raw')?.textContent).toBe('# 刷新后的完整正文\n\n新末尾\n')
    confirmButton().click()
    await flushPromises()
    expect(mocks.fetchText).toHaveBeenCalledTimes(2)
    expect(mocks.fetchText).toHaveBeenLastCalledWith(
      '/projects/project-a/vulnerabilities/vuln-a/report?observation_id=obs-selected',
      expect.any(AbortSignal),
      'no-store',
    )
    expect(mocks.create).toHaveBeenCalledWith('# 刷新后的完整正文\n\n新末尾\n', 'md', '受控漏洞', [])
    expect(mocks.download).toHaveBeenCalledWith(expect.any(Blob), '受控漏洞', 'md')
    expect(document.body.textContent).toContain('已开始下载')
  })

  it.each(['md', 'html', 'docx'] as const)('%s 导出复用当前预览正文，切换格式不重新读取或生成报告', async (format) => {
    const markdown = '\n# 同一份正文\n\n原始证据与末尾\n'
    const loadMarkdown = vi.fn().mockResolvedValue(markdown)
    const wrapper = mount(ReportExportMenu, {
      attachTo: document.body,
      props: { identity: 'report-a', filename: '受控报告', preview: true, loadMarkdown },
    })
    wrappers.push(wrapper)
    await chooseFormat(wrapper)
    expect(mocks.create).not.toHaveBeenCalled()
    expect(mocks.download).not.toHaveBeenCalled()
    expect(wrapper.findAllComponents(Select)).toHaveLength(1)
    wrapper.getComponent(Select).vm.$emit('update:modelValue', format)
    await flushPromises()
    confirmButton().click()
    await flushPromises()
    expect(loadMarkdown).toHaveBeenCalledExactlyOnceWith(expect.any(AbortSignal))
    expect(mocks.create).toHaveBeenCalledExactlyOnceWith(markdown, format, '受控报告', [])
    expect(mocks.download).toHaveBeenCalledExactlyOnceWith(expect.any(Blob), '受控报告', format)
    expect(mocks.catalog).not.toHaveBeenCalled()
    expect(mocks.request).not.toHaveBeenCalled()
  })

  it('整体历史成品直接读取全文，不读取当前模板；键盘可打开菜单', async () => {
    const markdown = '\n# 历史四章\n' + '原始内容\n'.repeat(14000) + '尾部\n'
    const loadMarkdown = vi.fn().mockResolvedValue(markdown)
    const wrapper = mount(ReportExportMenu, {
      attachTo: document.body,
      props: { identity: 'artifact-old', filename: '历史成品', loadMarkdown },
    })
    wrappers.push(wrapper)
    const trigger = wrapper.get('button')
    await trigger.trigger('keydown', { key: 'ArrowDown' })
    await flushPromises()
    const item = [...document.body.querySelectorAll<HTMLElement>('[role="menuitem"]')].find(
      (element) => element.textContent === 'Word（.docx）',
    )!
    expect(item).toBeTruthy()
    item.click()
    await flushPromises()
    expect(mocks.catalog).not.toHaveBeenCalled()
    expect(document.body.querySelector('.report-export-dialog')).toBeNull()
    expect(loadMarkdown).toHaveBeenCalledExactlyOnceWith(expect.any(AbortSignal))
    expect(mocks.create).toHaveBeenCalledWith(markdown, 'docx', '历史成品', [])
    expect(mocks.download).toHaveBeenCalledWith(expect.any(Blob), '历史成品', 'docx')
  })

  it('读取时切项与转换时切项都中止，A到B到A的迟到结果不能下载', async () => {
    const reading = deferred<string>()
    const converting = deferred<Blob>()
    const loadMarkdown = vi.fn().mockReturnValueOnce(reading.promise).mockResolvedValue('# B正文')
    const wrapper = mount(ReportExportMenu, {
      attachTo: document.body,
      props: { identity: 'A', filename: 'A', loadMarkdown },
    })
    wrappers.push(wrapper)
    await chooseFormat(wrapper)
    const firstSignal = loadMarkdown.mock.calls[0]![0] as AbortSignal
    await wrapper.setProps({ identity: 'B', filename: 'B' })
    expect(firstSignal.aborted).toBe(true)
    reading.resolve('A的迟到正文')
    await flushPromises()
    expect(mocks.create).not.toHaveBeenCalled()
    mocks.create.mockReturnValueOnce(converting.promise)
    await chooseFormat(wrapper, 'HTML（.html）')
    expect(mocks.create).toHaveBeenCalledWith('# B正文', 'html', 'B', [])
    await wrapper.setProps({ identity: 'A', filename: 'A' })
    converting.resolve(new Blob(['B的迟到转换']))
    await flushPromises()
    expect(mocks.download).not.toHaveBeenCalled()
  })

  it('切换Observation中止预览，取消转换拒绝迟到下载，失败保留明确错误', async () => {
    const reading = deferred<string>()
    mocks.fetchText.mockReturnValueOnce(reading.promise)
    const wrapper = mount(VulnerabilityReportExport, {
      attachTo: document.body,
      props: { projectId: 'p', vulnerabilityId: 'v', title: '受控漏洞', observationId: 'obs-a' },
    })
    wrappers.push(wrapper)
    await chooseFormat(wrapper)
    expect(confirmButton().disabled).toBe(true)
    const signal = mocks.fetchText.mock.calls.at(-1)![1] as AbortSignal
    await wrapper.setProps({ observationId: 'obs-b' })
    expect(signal.aborted).toBe(true)
    reading.resolve('obs-a的迟到正文')
    await flushPromises()
    expect(mocks.create).not.toHaveBeenCalled()
    expect(mocks.download).not.toHaveBeenCalled()
    const canceled = deferred<Blob>()
    mocks.fetchText.mockResolvedValueOnce('# 新发现预览')
    mocks.create.mockReturnValueOnce(canceled.promise)
    await chooseFormat(wrapper)
    const canceledSignal = mocks.fetchText.mock.calls.at(-1)![1] as AbortSignal
    confirmButton().click()
    await flushPromises()
    expect(mocks.create).toHaveBeenCalledWith('# 新发现预览', 'md', '受控漏洞', [])
    const cancelPending = [
      ...document.body.querySelectorAll<HTMLButtonElement>('[role="dialog"] button'),
    ].find((button) => button.textContent === '取消导出')!
    cancelPending.click()
    await flushPromises()
    expect(canceledSignal.aborted).toBe(true)
    canceled.resolve(new Blob(['已取消的转换结果']))
    await flushPromises()
    expect(mocks.download).not.toHaveBeenCalled()
    mocks.fetchText.mockResolvedValueOnce('# 原文预览')
    mocks.create.mockRejectedValueOnce(new Error('报告转换失败：受控错误'))
    await chooseFormat(wrapper)
    confirmButton().click()
    await flushPromises()
    expect(document.body.querySelector('[role="alert"]')?.textContent).toContain(
      '报告转换失败：受控错误',
    )
    expect(document.body.textContent).not.toContain('已开始下载')
    expect(mocks.download).not.toHaveBeenCalled()
    const cancel = [
      ...document.body.querySelectorAll<HTMLButtonElement>('[role="dialog"] button'),
    ].find((button) => button.textContent === '关闭')!
    cancel.click()
    await flushPromises()
    expect(document.body.querySelector('.ui-dialog__content')).toBeNull()
    expect(document.activeElement).toBe(wrapper.get('button').element)
    expect(document.body.querySelector('.report-export__feedback')?.textContent).toContain(
      '报告转换失败：受控错误',
    )
  })

  it('列表入口先固定发现，预览、切格式和下载都不隐式整理或读取模板', async () => {
    const wrapper = mount(VulnerabilityReportExport, {
      attachTo: document.body,
      props: { projectId: 'p', vulnerabilityId: 'v', title: '非敏感合成漏洞' },
    })
    wrappers.push(wrapper)
    await chooseFormat(wrapper)
    expect(mocks.vulnerability).toHaveBeenCalledTimes(1)
    expect(document.body.textContent).toContain('完整正文')
    for (const label of ['整理报告', '重新整理', '取消整理', '刷新状态', '产生费用']) {
      expect(document.body.textContent).not.toContain(label)
    }
    expect(wrapper.findAllComponents(Select)).toHaveLength(1)
    wrapper.getComponent(Select).vm.$emit('update:modelValue', 'html')
    await flushPromises()
    confirmButton().click()
    await flushPromises()
    expect(mocks.create).toHaveBeenLastCalledWith('# 完整正文\n\n末尾\n', 'html', '非敏感合成漏洞', [])
    expect(mocks.fetchText).toHaveBeenCalledExactlyOnceWith(
      '/projects/p/vulnerabilities/v/report?observation_id=obs-latest',
      expect.any(AbortSignal),
      'no-store',
    )
    expect(mocks.download).toHaveBeenCalledWith(expect.any(Blob), '非敏感合成漏洞', 'html')
    expect(mocks.catalog).not.toHaveBeenCalled()
    expect(mocks.request).not.toHaveBeenCalled()
    expect(mocks.finalization).not.toHaveBeenCalled()
    expect(mocks.vulnerability).toHaveBeenCalledTimes(2)
    expect(mocks.vulnerability).toHaveBeenLastCalledWith('p', 'v', expect.any(AbortSignal), 'obs-latest', true)
  })

  it('列表定位拒绝另一漏洞身份，预览失败仍保留已保存发现且不启动整理', async () => {
    mocks.vulnerability.mockResolvedValue({ project_id: 'p', id: 'another-vulnerability', observation_id: 'obs-other' })
    const wrapper = mount(VulnerabilityReportExport, {
      attachTo: document.body,
      props: { projectId: 'p', vulnerabilityId: 'v', title: '受控漏洞' },
    })
    wrappers.push(wrapper)
    await chooseFormat(wrapper)
    expect(document.body.textContent).toContain('发现记录与当前漏洞身份不匹配')
    expect(confirmButton().disabled).toBe(true)
    expect(mocks.fetchText).not.toHaveBeenCalled()
    expect(mocks.create).not.toHaveBeenCalled()
    expect(mocks.download).not.toHaveBeenCalled()
    expect(mocks.request).not.toHaveBeenCalled()
    expect(mocks.finalization).not.toHaveBeenCalled()
  })

  it('单漏洞导出关闭弹层后回焦不吞掉可视成功提示，提示保留约3.5秒', async () => {
    const loadMarkdown = vi.fn().mockResolvedValue('# 合成报告')
    const wrapper = mount(ReportExportMenu, {
      attachTo: document.body,
      props: {
        identity: 'single-vulnerability',
        filename: '合成报告',
        preview: true,
        loadMarkdown,
      },
    })
    wrappers.push(wrapper)
    await chooseFormat(wrapper)
    const confirm = confirmButton()
    confirm.focus()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    confirm.click()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(100)
    await flushPromises()
    expect(document.body.querySelector('.ui-dialog__content')).toBeNull()
    expect(document.activeElement).toBe(wrapper.get('button').element)
    expect(document.body.querySelector('.report-export__feedback')?.textContent).toContain(
      '已开始下载',
    )
    expect(wrapper.get('[role="status"]').text()).toBe('已开始下载')
    await vi.advanceTimersByTimeAsync(3300)
    expect(document.body.querySelector('.report-export__feedback')).not.toBeNull()
    await vi.advanceTimersByTimeAsync(101)
    expect(document.body.querySelector('.report-export__feedback')).toBeNull()
    expect(wrapper.get('[role="status"]').text()).toBe('')
  })

  it('成功短时锚定反馈与长错误可展开，不在按钮下增加占流文字', async () => {
    const loadMarkdown = vi.fn().mockResolvedValue('# 合成报告')
    const wrapper = mount(ReportExportMenu, {
      attachTo: document.body,
      props: { identity: 'a', filename: '合成报告', loadMarkdown },
    })
    wrappers.push(wrapper)
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    await chooseFormat(wrapper)
    expect(wrapper.findAll('p')).toHaveLength(0)
    expect(document.body.querySelector('.report-export__feedback')?.textContent).toContain(
      '已开始下载',
    )
    expect(wrapper.get('[role="status"]').text()).toBe('已开始下载')
    await vi.advanceTimersByTimeAsync(3501)
    expect(document.body.querySelector('.report-export__feedback')).toBeNull()
    loadMarkdown.mockRejectedValueOnce(new Error('受控错误\n' + '详细原因。'.repeat(120)))
    await chooseFormat(wrapper)
    const details = document.body.querySelector<HTMLDetailsElement>(
      '.report-export__feedback details',
    )!
    expect(details.open).toBe(false)
    expect(details.textContent).toContain('详细原因。'.repeat(120))
    details.open = true
    expect(details.open).toBe(true)
    expect(wrapper.findAll('p')).toHaveLength(0)
    expect(wrapper.get('[role="status"]').text()).toContain('导出失败')
    document.body.querySelector<HTMLButtonElement>('[aria-label="关闭导出提示"]')!.click()
    await flushPromises()
    expect(document.body.querySelector('.report-export__feedback')).toBeNull()
  })
})
