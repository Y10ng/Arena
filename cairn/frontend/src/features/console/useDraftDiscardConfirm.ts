import { onScopeDispose, ref } from 'vue'

// 让所有离开入口等待同一份主题确认；取消和组件卸载都不能继续导航。
export function useDraftDiscardConfirm() {
  const discardConfirmationOpen = ref(false)
  let resolver: ((accepted: boolean) => void) | null = null
  function resolveDiscard(accepted: boolean) {
    discardConfirmationOpen.value = false
    const pending = resolver
    resolver = null
    pending?.(accepted)
  }
  function requestDiscard() {
    if (resolver) return Promise.resolve(false)
    discardConfirmationOpen.value = true
    return new Promise<boolean>((resolve) => {
      resolver = resolve
    })
  }
  onScopeDispose(() => resolveDiscard(false))
  return { discardConfirmationOpen, requestDiscard, resolveDiscard }
}
