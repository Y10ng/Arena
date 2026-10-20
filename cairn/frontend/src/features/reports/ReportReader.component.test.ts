import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ReportReader from './ReportReader.vue'

const wrappers: VueWrapper[] = []
const originalUrl = window.location.href

function mountReader(content: string, identity = 'project-a:report-a') {
  const wrapper = mount(ReportReader, {
    attachTo: document.body,
    props: { content, identity },
    slots: { title: '<h1>当前报告</h1>', state: '正在读取' },
  })
  wrappers.push(wrapper)
  return wrapper
}

function modeButton(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((candidate) => candidate.text() === text)
  if (!button) throw new Error(`找不到模式按钮：${text}`)
  return button
}

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  vi.restoreAllMocks()
  window.history.replaceState({}, '', originalUrl)
})

describe('ReportReader', () => {
  it('uses the rendered tokens for unique headings and excludes fenced code', async () => {
    const wrapper = mountReader(
      [
        '# 概览',
        '```markdown\n## 围栏内不是章节\n```',
        '## **证据**与 `ID`',
        '## **证据**与 `ID`',
        '![远程图片](https://tracker.example/image.png)',
      ].join('\n\n'),
    )
    await flushPromises()

    const headings = wrapper.findAll('.safe-report-markdown :is(h1, h2)')
    const directory = wrapper.findAll('nav[aria-label="当前报告章节目录"] button')
    expect(directory.map((button) => button.text())).toEqual(['概览', '证据与 ID', '证据与 ID'])
    expect(new Set(headings.map((heading) => heading.attributes('id'))).size).toBe(3)
    expect(headings.every((heading) => heading.attributes('tabindex') === '-1')).toBe(true)
    expect(wrapper.get('pre code').text()).toContain('## 围栏内不是章节')
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.html()).not.toContain('tracker.example')
  })

  it('moves only the owned body scroll pane without changing the hash route', async () => {
    window.history.replaceState({}, '', '#/reports?reportId=report-a')
    const wrapper = mountReader('# 开始\n\n## 最后一章\n\n末行内容')
    await flushPromises()
    const pane = wrapper.get('.report-reader-scroll').element as HTMLElement
    const heading = wrapper.get('.safe-report-markdown h2').element as HTMLElement
    const windowScroll = vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)
    vi.spyOn(pane, 'getBoundingClientRect').mockReturnValue({ top: 40 } as DOMRect)
    vi.spyOn(heading, 'getBoundingClientRect').mockReturnValue({ top: 240 } as DOMRect)
    pane.scrollTop = 100
    const originalHash = window.location.hash

    await wrapper.findAll('.report-reader-directory button')[1]!.trigger('click')

    expect(pane.scrollTop).toBe(284)
    expect(document.activeElement).toBe(heading)
    expect(windowScroll).not.toHaveBeenCalled()
    expect(window.location.hash).toBe(originalHash)
    expect(wrapper.get('.report-reader-directory button[aria-current="location"]').text()).toBe(
      '最后一章',
    )
  })

  it('does not invent a directory and hides it in raw mode', async () => {
    const wrapper = mountReader('普通正文，没有标题。')
    await flushPromises()
    expect(wrapper.find('nav').exists()).toBe(false)

    await wrapper.setProps({ content: '# 实际章节\n\n<script>unsafe()</script>' })
    await flushPromises()
    expect(wrapper.find('nav').exists()).toBe(true)
    await modeButton(wrapper, '原文').trigger('click')
    await flushPromises()
    expect(wrapper.find('nav').exists()).toBe(false)
    expect(wrapper.get('.report-reader-raw').text()).toContain('<script>unsafe()</script>')
    expect(wrapper.find('script').exists()).toBe(false)
  })

  it('retains per-mode scroll and resets both modes when report identity changes', async () => {
    const wrapper = mountReader('# 报告 A\n\n正文')
    await flushPromises()
    const pane = wrapper.get('.report-reader-scroll').element as HTMLElement
    pane.scrollTop = 240
    await wrapper.get('.report-reader-scroll').trigger('scroll')
    await modeButton(wrapper, '原文').trigger('click')
    await flushPromises()
    expect(pane.scrollTop).toBe(0)
    pane.scrollTop = 180
    await wrapper.get('.report-reader-scroll').trigger('scroll')
    await modeButton(wrapper, '排版').trigger('click')
    await flushPromises()
    expect(pane.scrollTop).toBe(240)

    await modeButton(wrapper, '原文').trigger('click')
    await flushPromises()
    expect(pane.scrollTop).toBe(180)
    await wrapper.setProps({ identity: 'project-b:report-b', content: '# 报告 B\n\n新正文' })
    await flushPromises()
    expect(wrapper.get('.report-reader-directory').text()).toContain('报告 B')
    expect(wrapper.get('.report-reader-directory').text()).not.toContain('报告 A')
    expect(pane.scrollTop).toBe(0)
    await modeButton(wrapper, '原文').trigger('click')
    await flushPromises()
    expect(pane.scrollTop).toBe(0)
  })

  it('does not expose a stale directory while the next report is loading', async () => {
    const wrapper = mountReader('# 旧报告')
    await flushPromises()
    await wrapper.setProps({ identity: 'new', content: '', ready: false })
    expect(wrapper.find('nav').exists()).toBe(false)
    expect(wrapper.get('.report-reader-state').text()).toBe('正在读取')
    await wrapper.setProps({ content: '新正文，无标题。', ready: true })
    await flushPromises()
    expect(wrapper.find('nav').exists()).toBe(false)
    expect(wrapper.get('.safe-report-markdown').text()).toBe('新正文，无标题。')
  })

  it('prevents Markdown fragment links from replacing the application hash', async () => {
    window.history.replaceState({}, '', '#/projects/project-a/artifacts?artifactId=report-a')
    const wrapper = mountReader('# 概览\n\n[原有章节链接](#old-section)')
    await flushPromises()
    const originalHash = window.location.hash
    const link = wrapper.get('.safe-report-markdown a').element as HTMLAnchorElement
    const click = new MouseEvent('click', { bubbles: true, cancelable: true })
    link.dispatchEvent(click)
    expect(click.defaultPrevented).toBe(true)
    expect(window.location.hash).toBe(originalHash)
  })
})
