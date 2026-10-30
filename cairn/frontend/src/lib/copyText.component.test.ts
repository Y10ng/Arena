import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyText } from './copyText'

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
const originalCommand = Object.getOwnPropertyDescriptor(document, 'execCommand')
afterEach(() => {
  if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard)
  else Reflect.deleteProperty(navigator, 'clipboard')
  if (originalCommand) Object.defineProperty(document, 'execCommand', originalCommand)
  else Reflect.deleteProperty(document, 'execCommand')
  document.body.innerHTML = ''
  window.getSelection()?.removeAllRanges()
})

describe('原文复制', () => {
  it('LAN copy 事件传递 CRLF 原文并恢复焦点、输入选择和文档范围', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
    const input = document.createElement('textarea')
    input.value = '未发送输入'
    const paragraph = document.createElement('p')
    paragraph.textContent = '阅读中的范围'
    document.body.append(input, paragraph)
    const range = document.createRange()
    range.selectNodeContents(paragraph)
    window.getSelection()?.addRange(range)
    input.focus()
    input.setSelectionRange(1, 4, 'backward')
    const setData = vi.fn()
    Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn(() => {
      const event = new Event('copy', { bubbles: true, cancelable: true })
      Object.defineProperty(event, 'clipboardData', { value: { setData } })
      document.activeElement?.dispatchEvent(event)
      return true
    }) })
    const original = '# 标题\r\n```text\r\n第一行\n第二行\r\n```'
    await copyText(original)
    expect(setData).toHaveBeenCalledExactlyOnceWith('text/plain', original)
    expect(document.activeElement).toBe(input)
    expect([input.selectionStart, input.selectionEnd, input.selectionDirection]).toEqual([1, 4, 'backward'])
    expect(input.value).toBe('未发送输入')
    expect(document.querySelectorAll('textarea')).toHaveLength(1)
  })

  it.each([false, true])('命令返回 %s 但没有原文 copy 回执时拒绝成功', async accepted => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
    Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn(() => accepted) })
    await expect(copyText('原文')).rejects.toThrow('copy_not_accepted')
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('Clipboard 权限拒绝不偷偷降级执行复制命令', async () => {
    const denied = new DOMException('denied', 'NotAllowedError')
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockRejectedValue(denied) } })
    const command = vi.fn()
    Object.defineProperty(document, 'execCommand', { configurable: true, value: command })
    await expect(copyText('原文')).rejects.toBe(denied)
    expect(command).not.toHaveBeenCalled()
  })
})
