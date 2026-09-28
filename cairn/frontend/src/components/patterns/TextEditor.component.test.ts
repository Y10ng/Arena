import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'

vi.mock('@/components/MonacoEditor.vue', () => ({
  __esModule: true,
  __isTeleport: false,
  default: {
    name: 'MonacoEditor',
    props: ['modelValue', 'language', 'disabled', 'height', 'minHeight', 'ariaLabel', 'autoFocus'],
    emits: ['update:modelValue'],
    template: `
      <textarea
        data-testid="monaco-editor"
        :value="modelValue"
        :disabled="disabled"
        :aria-label="ariaLabel"
        :data-language="language"
        :data-height="height"
        :data-auto-focus="String(autoFocus === '' || Boolean(autoFocus))"
        @input="$emit('update:modelValue', $event.target.value)"
      />
    `,
  },
}))

vi.mock('@/components/MarkdownPreview.vue', () => ({
  default: {
    name: 'MarkdownPreview',
    props: ['content'],
    template: '<article data-testid="markdown-preview">{{ content }}</article>',
  },
}))

import TextEditor from './TextEditor.vue'

const wrappers: VueWrapper[] = []

function mountEditor(props: Partial<InstanceType<typeof TextEditor>['$props']> = {}) {
  const wrapper = mount(TextEditor, {
    attachTo: document.body,
    props: {
      modelValue: '# 项目资料',
      label: '项目资料',
      fileName: 'notes.md',
      placeholder: '输入项目资料',
      minHeight: 420,
      ...props,
    },
  })
  wrappers.push(wrapper)
  return wrapper
}

function buttonByLabel(label: string, root: ParentNode = document.body) {
  const button = root.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)
  if (!button) throw new Error(`找不到按钮：${label}`)
  return button
}

function pressEscape() {
  document.dispatchEvent(
    new KeyboardEvent('keydown', {
      key: 'Escape',
      bubbles: true,
    }),
  )
}

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('TextEditor fullscreen migration', () => {
  it('opens one named fullscreen Dialog with the existing editor contract', async () => {
    mountEditor({
      modelValue: '',
      disabled: true,
    })
    await flushPromises()

    expect(document.body.querySelectorAll('[data-testid="monaco-editor"]')).toHaveLength(1)

    buttonByLabel('全屏编辑').click()
    await flushPromises()

    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')
    expect(document.body.querySelectorAll('[role="dialog"]')).toHaveLength(1)
    expect(dialog?.classList.contains('ui-dialog__content--fullscreen')).toBe(true)
    expect(document.body.querySelector('.ui-dialog__overlay--fullscreen')).not.toBeNull()
    expect(dialog?.getAttribute('aria-labelledby')).toBeTruthy()
    expect(dialog?.getAttribute('aria-describedby')).toBeTruthy()
    expect(dialog?.textContent).toContain('项目资料')
    expect(dialog?.textContent).toContain('Markdown')
    expect(dialog?.textContent).toContain('使用 Markdown 查看或编辑项目资料。')
    expect(dialog?.textContent).toContain('输入项目资料')

    const editors = document.body.querySelectorAll<HTMLTextAreaElement>(
      '[data-testid="monaco-editor"]',
    )
    const fullscreenEditor = dialog?.querySelector<HTMLTextAreaElement>(
      '[data-testid="monaco-editor"]',
    )
    expect(editors).toHaveLength(2)
    expect(fullscreenEditor?.dataset.language).toBe('markdown')
    expect(fullscreenEditor?.dataset.height).toBe('100%')
    expect(fullscreenEditor?.dataset.autoFocus).toBe('true')
    expect(fullscreenEditor?.disabled).toBe(true)
  })

  it('shares source, preview, and split mode across inline and fullscreen views', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    buttonByLabel('预览模式').click()
    await flushPromises()
    expect(document.body.querySelectorAll('[data-testid="monaco-editor"]')).toHaveLength(0)
    expect(document.body.querySelectorAll('[data-testid="markdown-preview"]')).toHaveLength(1)

    buttonByLabel('全屏编辑').click()
    await flushPromises()
    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')
    expect(dialog).not.toBeNull()
    expect(document.body.querySelectorAll('[data-testid="markdown-preview"]')).toHaveLength(2)

    buttonByLabel('分屏模式', dialog ?? document.body).click()
    await flushPromises()
    expect(document.body.querySelectorAll('[data-testid="monaco-editor"]')).toHaveLength(2)
    expect(document.body.querySelectorAll('[data-testid="markdown-preview"]')).toHaveLength(2)
    expect(dialog?.querySelector('.editor-fullscreen-body')?.classList.contains('split')).toBe(true)

    await wrapper.setProps({ fileName: 'script.py' })
    await flushPromises()
    expect(document.body.querySelectorAll('[data-testid="markdown-preview"]')).toHaveLength(0)
    expect(document.body.querySelectorAll('[data-testid="monaco-editor"]')).toHaveLength(2)
    expect(dialog?.querySelector('button[aria-label="预览模式"]')).toBeNull()

    buttonByLabel('退出全屏', dialog ?? document.body).click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(document.body.querySelectorAll('[data-testid="monaco-editor"]')).toHaveLength(1)
  })

  it('emits fullscreen Monaco edits immediately with explicit language precedence', async () => {
    const wrapper = mountEditor({
      language: 'yaml',
      modelValue: 'name: before',
    })
    await flushPromises()

    buttonByLabel('全屏编辑').click()
    await flushPromises()

    const editors = document.body.querySelectorAll<HTMLTextAreaElement>(
      '[data-testid="monaco-editor"]',
    )
    const fullscreenEditor = editors[editors.length - 1]
    expect(fullscreenEditor?.dataset.language).toBe('yaml')

    if (!fullscreenEditor) throw new Error('找不到全屏 MonacoEditor')
    fullscreenEditor.value = 'name: after'
    fullscreenEditor.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()

    expect(wrapper.emitted('update:modelValue')).toContainEqual(['name: after'])
  })

  it('keeps outside interaction open and restores focus after Escape', async () => {
    mountEditor()
    await flushPromises()

    const opener = buttonByLabel('全屏编辑')
    opener.focus()
    opener.click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()

    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    document.body.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()

    pressEscape()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(document.activeElement).toBe(opener)
  })

  it('preserves inline resize limits and cleans up pointer listeners', async () => {
    const removeListener = vi.spyOn(window, 'removeEventListener')
    const wrapper = mountEditor({ minHeight: 420 })
    await flushPromises()

    const handle = wrapper.get('.resize-handle')
    handle.element.dispatchEvent(
      new PointerEvent('pointerdown', {
        bubbles: true,
        clientY: 100,
      }),
    )
    window.dispatchEvent(new PointerEvent('pointermove', { clientY: 180 }))
    await flushPromises()

    expect(wrapper.get('.text-editor-body').attributes('style')).toContain('height: 500px')

    wrapper.unmount()
    expect(removeListener).toHaveBeenCalledWith('pointermove', expect.any(Function))
    expect(removeListener).toHaveBeenCalledWith('pointerup', expect.any(Function))
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
  })
})
