export async function copyText(content: string, container: HTMLElement = document.body): Promise<void> {
  if (typeof navigator.clipboard?.writeText === 'function') {
    // 明确拒绝权限时不绕过浏览器决定，调用方显示真实失败。
    await navigator.clipboard.writeText(content)
    return
  }
  // LAN HTTP 没有 Clipboard API；在用户点击期间提供原文并检查浏览器回执。
  const focused = document.activeElement instanceof HTMLElement ? document.activeElement : null
  const selection = window.getSelection()
  const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index).cloneRange()) : []
  const inputSelection = focused instanceof HTMLInputElement || focused instanceof HTMLTextAreaElement
    ? { start: focused.selectionStart, end: focused.selectionEnd, direction: focused.selectionDirection } : null
  const temporary = document.createElement('textarea')
  temporary.value = content
  temporary.readOnly = true
  temporary.tabIndex = -1
  temporary.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;pointer-events:none;'
  let suppliedOriginal = false
  temporary.addEventListener('copy', (event) => {
    if (!event.clipboardData) return
    // textarea 会规范化 CRLF；copy 事件直接传递原文，不复制 DOM 或行号。
    event.clipboardData.setData('text/plain', content)
    event.preventDefault()
    suppliedOriginal = true
  })
  container.appendChild(temporary)
  try {
    temporary.focus({ preventScroll: true })
    temporary.select()
    if (!document.execCommand('copy') || !suppliedOriginal) throw new Error('copy_not_accepted')
  } finally {
    temporary.remove()
    focused?.focus({ preventScroll: true })
    selection?.removeAllRanges()
    ranges.forEach(range => selection?.addRange(range))
    if (inputSelection && inputSelection.start !== null && inputSelection.end !== null && (focused instanceof HTMLInputElement || focused instanceof HTMLTextAreaElement)) {
      focused.setSelectionRange(inputSelection.start, inputSelection.end, inputSelection.direction || undefined)
    }
  }
}
