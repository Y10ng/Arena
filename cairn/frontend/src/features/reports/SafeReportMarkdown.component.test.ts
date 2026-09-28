import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import SafeReportMarkdown from './SafeReportMarkdown.vue'

describe('SafeReportMarkdown', () => {
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
