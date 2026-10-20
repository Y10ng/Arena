<template>
  <DropdownMenu :items="items" @select="select">
    <template #trigger><IconButton :label="`复测操作：${request.title}`" :data-verification-actions="request.id" :loading="exporting || deleting"><MoreHorizontal :size="18" /></IconButton></template>
    <template #icon="{ item }"><RotateCcw v-if="item.value === 'rerun'" :size="16" /><Download v-else-if="item.value === 'export'" :size="16" /><Trash2 v-else :size="16" /></template>
  </DropdownMenu>
  <ConfirmAction :open="confirmOpen" title="永久删除此轮复测" description="删除此轮复测及其产生的文件，不影响原漏洞和其他已受理复测；删除后无法恢复。" confirm-label="永久删除" :loading="deleting" @update:open="confirmOpen = $event" @confirm="remove">
    <Alert v-if="deleteError" tone="danger" title="未确认删除成功">{{ deleteError }}</Alert>
  </ConfirmAction>
</template>
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useQueryClient } from '@tanstack/vue-query'
import { Download, MoreHorizontal, RotateCcw, Trash2 } from '@lucide/vue'
import { ApiError } from '@/api/client'
import { deleteVerificationRequest, downloadVerificationReport, type VerificationSummary } from '@/api/verificationRequests'
import { Alert, DropdownMenu, IconButton } from '@/components/ui'
import type { DropdownMenuOption } from '@/components/ui/DropdownMenu.vue'
import { ConfirmAction } from '@/components/patterns'
import { errorMessage } from './verificationPresentation'
const props = withDefaults(defineProps<{ request: VerificationSummary; context?: 'list' | 'detail' }>(), { context: 'list' })
const emit = defineEmits<{ (event: 'rerun'): void; (event: 'deleted', id: string): void; (event: 'error', message: string): void }>()
const queryClient = useQueryClient()
const confirmOpen = ref(false)
const deleting = ref(false)
const deleteError = ref('')
const exporting = ref(false)
const purging = computed(() => props.request.purge_state === 'pending')
const items = computed<DropdownMenuOption[]>(() => [
  ...(props.context === 'list' ? [{ value: 'rerun', label: '再次复测', disabled: purging.value, description: purging.value ? '此轮清理未完成，请从原漏洞新建复测' : undefined }] : []),
  { value: 'export', label: '导出报告', disabled: purging.value || !props.request.result, description: purging.value ? '此轮清理未完成，不能导出' : !props.request.result ? '尚无结果' : undefined },
  { value: 'delete', label: purging.value ? '重试清理' : '删除', separatorBefore: true, tone: 'danger', disabled: !props.request.can_delete, description: props.request.delete_block_reason || undefined },
])
function select(value: string) {
  if (value === 'rerun' && !purging.value) emit('rerun')
  else if (value === 'export') void exportReport()
  else if (value === 'delete') { deleteError.value = ''; confirmOpen.value = true }
}
async function exportReport() {
  if (exporting.value || purging.value || !props.request.result) return
  exporting.value = true
  try { await downloadVerificationReport(props.request.id) }
  catch (error) { emit('error', `导出报告失败：${errorMessage(error)}`) }
  finally { exporting.value = false }
}
async function remove() {
  if (deleting.value || !props.request.can_delete) return
  const id = props.request.id
  deleting.value = true; deleteError.value = ''
  try {
    await deleteVerificationRequest(id)
    confirmOpen.value = false
    // 先通知页面，再刷新会卸载当前行的缓存；列表由页面重读并校正分页。
    void queryClient.invalidateQueries({ queryKey: ['verification-requests'], refetchType: 'none' })
    emit('deleted', id)
    queryClient.removeQueries({ queryKey: ['verification-request', id], exact: true })
    // 其他轮的上一轮链接及来源历史也从服务端重读，不在本地猜测删除影响。
    void queryClient.invalidateQueries({ queryKey: ['verification-request'] })
    void queryClient.invalidateQueries({ queryKey: ['verification-source-history'] })
  } catch (error) {
    deleteError.value = error instanceof ApiError
      ? `服务端未确认删除成功，请核对或重试。${errorMessage(error)}`
      : `删除响应未确认，请刷新核对或重试删除。${errorMessage(error)}`
    // 文件清理可能已开始，失败时重读最小投影，不把旧档案继续当作可导出来源。
    void queryClient.invalidateQueries({ queryKey: ['verification-requests'] })
    void queryClient.invalidateQueries({ queryKey: ['verification-request', id], exact: true })
  } finally { deleting.value = false }
}
defineExpose({ retryCleanup: () => { deleteError.value = ''; confirmOpen.value = true } })
</script>
