import { computed, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { api } from '@/api/client'
import type { TimeoutSettings } from '@/api/types'
import { useUIStore } from '@/stores/ui'
import { clonePlain, formatError } from './consoleCommon'

export const readTimeoutValue = (value: unknown) => {
  if (value === '' || value === null || value === undefined) return Number.NaN
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : Number.NaN
}

export const useTimeoutConsole = () => {
  const uiStore = useUIStore()
  const isSavingTimeouts = ref(false)

  const {
    data: timeoutData,
    isLoading: isLoadingTimeouts,
    isError: isTimeoutsError,
    error: timeoutsError,
    refetch: refetchTimeouts,
  } = useQuery<TimeoutSettings>({
    queryKey: ['timeouts'],
    queryFn: () => api<TimeoutSettings>('GET', '/settings'),
  })

  const timeoutForm = ref<TimeoutSettings | null>(null)

  watch(timeoutData, (value) => {
    if (value) {
      timeoutForm.value = clonePlain(value)
    }
  }, { immediate: true })

  const timeoutErrors = computed(() => {
    const intent = readTimeoutValue(timeoutForm.value?.intent_timeout)
    const reason = readTimeoutValue(timeoutForm.value?.reason_timeout)
    return {
      intent: Number.isFinite(intent) ? (intent >= 5 ? '' : '不能小于 5 秒') : '请输入有效数字',
      reason: Number.isFinite(reason) ? (reason >= 5 ? '' : '不能小于 5 秒') : '请输入有效数字',
    }
  })

  const isTimeoutsValid = computed(() => !timeoutErrors.value.intent && !timeoutErrors.value.reason)

  const saveTimeouts = async () => {
    if (!timeoutForm.value || !isTimeoutsValid.value) return

    const payload: TimeoutSettings = {
      intent_timeout: readTimeoutValue(timeoutForm.value.intent_timeout),
      reason_timeout: readTimeoutValue(timeoutForm.value.reason_timeout),
    }

    try {
      isSavingTimeouts.value = true
      await api<TimeoutSettings>('PUT', '/settings', payload)
      uiStore.showToast('系统生命周期超时策略已成功修改', 'success')
      await refetchTimeouts()
    } catch (error: unknown) {
      uiStore.showToast(`修改超时策略失败: ${formatError(error)}`, 'danger')
    } finally {
      isSavingTimeouts.value = false
    }
  }

  return {
    formatError,
    isLoadingTimeouts,
    isSavingTimeouts,
    isTimeoutsError,
    isTimeoutsValid,
    readTimeoutValue,
    refetchTimeouts,
    saveTimeouts,
    timeoutData,
    timeoutErrors,
    timeoutForm,
    timeoutsError,
  }
}
