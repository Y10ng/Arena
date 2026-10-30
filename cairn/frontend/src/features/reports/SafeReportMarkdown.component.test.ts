import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import SafeReportMarkdown from './SafeReportMarkdown.vue'

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('SafeReportMarkdown', () => {
  it('授权图片按需读取、失败只显示说明，切换取消旧读取并释放 Blob', async () => {
    const create = vi.fn().mockReturnValue('blob:controlled-image')
    const revoke = vi.fn()
    vi.stubGlobal('URL', class extends URL { static createObjectURL = create; static revokeObjectURL = revoke })
    vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue({ close: vi.fn() }))
    const image = { artifact_id: 'image-a', caption: '受控图片', content_type: 'image/png' }
    const load = vi.fn().mockResolvedValue(new Blob(['controlled'], { type: 'image/png' }))
    const wrapper = mount(SafeReportMarkdown, { props: { content: '**正文继续**\n\n![任意图](https://example.test/x.png)', identity: 'a', images: [image], loadImage: load } })
    expect(load).not.toHaveBeenCalled()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('img').attributes('src')).toBe('blob:controlled-image')
    expect(load).toHaveBeenCalledTimes(1)
    expect(wrapper.html()).not.toContain('https://example.test/x.png')
    const firstSignal = load.mock.calls[0]![1] as AbortSignal
    await wrapper.setProps({ identity: 'b', images: [{ ...image, artifact_id: 'image-b' }] })
    expect(firstSignal.aborted).toBe(true)
    expect(revoke).toHaveBeenCalledWith('blob:controlled-image')
    expect(wrapper.find('img').exists()).toBe(false)
    load.mockRejectedValueOnce(new Error('图片不可用'))
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('图片不可用')
    expect(wrapper.get('strong').text()).toBe('正文继续')
    expect(wrapper.find('img').exists()).toBe(false)
    wrapper.unmount()
  })

  it('renders report markdown without changing MarkdownPreview behavior', () => {
    const wrapper = mount(SafeReportMarkdown, {
      props: {
        content:
          '# Summary\n\n- **Critical** finding\n\n| Area | Result |\n| --- | --- |\n| API | Pass |',
      },
    })

    expect(wrapper.get('h1').text()).toBe('Summary')
    expect(wrapper.get('strong').text()).toBe('Critical')
    expect(wrapper.get('table').text()).toContain('API')
  })

  it('escapes HTML and rejects executable or unsupported link protocols', () => {
    const wrapper = mount(SafeReportMarkdown, {
      props: {
        content: [
          '<script>globalThis.__reportExecuted = true</script>',
          '<iframe src="https://tracker.example/frame"></iframe>',
          '[script](javascript:alert(1))',
          '[data](data:text/html,unsafe)',
          '[file](file:///etc/passwd)',
        ].join('\n\n'),
      },
    })

    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('iframe').exists()).toBe(false)
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.text()).toContain('<script>')
    expect(wrapper.text()).toContain('<iframe')
  })

  it('never renders markdown images or their remote sources', () => {
    const wrapper = mount(SafeReportMarkdown, {
      props: {
        content: [
          '![remote evidence](https://tracker.example/pixel.png)',
          '![embedded evidence](data:image/svg+xml,<svg onload=alert(1)></svg>)',
        ].join('\n\n'),
      },
    })

    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.html()).not.toContain('tracker.example')
    expect(wrapper.text()).toContain('remote evidence')
    expect(wrapper.text()).toContain('embedded evidence')
  })

  it('hardens external links while leaving relative report links in-page', () => {
    const wrapper = mount(SafeReportMarkdown, {
      props: {
        content:
          '[external](https://example.com/evidence) [email](mailto:security@example.com) [section](#details) [artifact](/artifacts/42)',
      },
    })
    const links = wrapper.findAll('a')

    expect(links).toHaveLength(4)
    expect(links[0]?.attributes()).toMatchObject({
      href: 'https://example.com/evidence',
      rel: 'noopener noreferrer',
      target: '_blank',
    })
    expect(links[1]?.attributes()).toMatchObject({ href: 'mailto:security@example.com' })
    expect(links[1]?.attributes('target')).toBeUndefined()
    expect(links[2]?.attributes()).toMatchObject({ href: '#details' })
    expect(links[2]?.attributes('target')).toBeUndefined()
    expect(links[3]?.attributes()).toMatchObject({ href: '/artifacts/42' })
    expect(links[3]?.attributes('target')).toBeUndefined()
  })

  it('wraps long prose and gives wide report content bounded scrolling hooks', () => {
    const wrapper = mount(SafeReportMarkdown, {
      props: {
        content: `${'long-unbroken-value'.repeat(100)}\n\n\`\`\`text\n${'x'.repeat(500)}\n\`\`\``,
      },
    })

    expect(wrapper.get('article').classes()).toContain('safe-report-markdown')
    expect(wrapper.get('pre').element).toBeInstanceOf(HTMLPreElement)
  })
})
