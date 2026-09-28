import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import MarkdownPreview from './MarkdownPreview.vue'

describe('MarkdownPreview', () => {
  it('renders accepted sensitive content as complete escaped selectable text', () => {
    const longTail = 'x'.repeat(20_000)
    const content = [
      '-----BEGIN OPENSSH PRIVATE KEY-----',
      'Authorization: Bearer full-token-value',
      '<script>globalThis.__markdownExecuted = true</script>',
      '<img src=x onerror="globalThis.__markdownHandler = true">',
      '[exploit](javascript:globalThis.__markdownProtocol=true)',
      longTail,
    ].join('\n')

    const wrapper = mount(MarkdownPreview, { props: { content } })
    const preview = wrapper.get('.markdown-preview__content')

    expect(preview.element.textContent).toBe(content)
    expect(preview.attributes('style')).toBeUndefined()
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.html()).toContain('&lt;script&gt;')
    expect(wrapper.html()).toContain('&lt;img')
  })

  it('does not lazily execute script-shaped content after interaction', async () => {
    const execute = vi.fn()
    Object.assign(globalThis, { __markdownExecuted: execute })
    const wrapper = mount(MarkdownPreview, {
      props: {
        content:
          '<button onclick="globalThis.__markdownExecuted()">run</button>\n[jump](javascript:globalThis.__markdownExecuted())',
      },
    })

    await wrapper.trigger('click')

    expect(execute).not.toHaveBeenCalled()
    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.find('a').exists()).toBe(false)
    delete (globalThis as { __markdownExecuted?: unknown }).__markdownExecuted
  })
})
