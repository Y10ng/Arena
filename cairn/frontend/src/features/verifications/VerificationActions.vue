<template>
  <div class="verification-record-actions">
    <div class="verification-record-actions__buttons">
      <Button size="sm" variant="ghost" :disabled="!request.can_delete" :loading="deleting" :aria-describedby="request.delete_block_reason ? reasonId : undefined" @click="requestDelete"><template #icon><Trash2 :size="16" /></template>{{ purging ? '重试清理' : '删除复测记录' }}</Button>
      <DropdownMenu :items="items" @select="select">
        <template #trigger><IconButton :label="`复测操作：${request.title}`" :data-verification-actions="request.id" :loading="exporting || deleting"><MoreHorizontal :size="18" /></IconButton></template>
        <template #icon="{ item }"><RotateCcw v-if="item.value === 'rerun'" :size="16" /><Download v-else :size="16" /></template>
      </DropdownMenu>
    </div>
    <p v-if="request.delete_block_reason" :id="reasonId" class="verification-record-actions__reason">{{ request.delete_block_reason }}</p>
  </div>
  <ConfirmAction :open="confirmOpen" title="永久删除此轮复测" description="删除此轮复测及其产生的文件，不影响原漏洞和其他已受理复测；删除后无法恢复。" confirm-label="永久删除" :loading="deleting" @update:open="confirmOpen = $event" @confirm="remove">
    <Alert v-if="deleteError" tone="danger" title="未确认删除成功">{{ deleteError }}</Alert>
  </ConfirmAction>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import { useQueryClient } from '@tanstack/vue-query'
import { Download, MoreHorizontal, RotateCcw, Trash2 } from '@lucide/vue'
import { ApiError, fetchBlob } from '@/api/client'
import { deleteVerificationRequest, getVerificationRequest, verificationArtifactUrl, verificationReportUrl, type VerificationSummary } from '@/api/verificationRequests'
import { createReportExport, downloadReportBlob, fetchReportMarkdown } from '@/features/reports/reportExport'
import { loadReportImages, type ReportImageData } from '@/features/reports/reportImages'
import { Alert, Button, DropdownMenu, IconButton } from '@/components/ui'
import type { DropdownMenuOption } from '@/components/ui/DropdownMenu.vue'
import { ConfirmAction } from '@/components/patterns'
import { errorMessage } from './verificationPresentation'
const props = withDefaults(defineProps<{ request: VerificationSummary; context?: 'list' | 'detail' }>(), { context: 'list' })
const emit = defineEmits<{ (event: 'rerun'): void; (event: 'deleted', id: string): void; (event: 'error', message: string): void }>()
const queryClient = useQueryClient()
const reasonId = useId()
const confirmOpen = ref(false)
const deleting = ref(false)
const deleteError = ref('')
const exporting = ref(false)
const purging = computed(() => props.request.purge_state === 'pending')
const items = computed<DropdownMenuOption[]>(() => [
  ...(props.context === 'list' ? [{ value: 'rerun', label: '再次复测', disabled: purging.value, description: purging.value ? '此轮清理未完成，请从原漏洞新建复测' : undefined }] : []),
  { value: 'export', label: '导出报告（.md）', disabled: purging.value || !props.request.result, description: purging.value ? '此轮清理未完成，不能导出' : !props.request.result ? '尚无结果' : undefined },
  { value: 'export-package', label: 'Markdown 证据包（.tar）', disabled: purging.value || !props.request.result },
])
function select(value: string) {
  if (value === 'rerun' && !purging.value) emit('rerun')
  else if (value === 'export' || value === 'export-package') void exportReport(value === 'export-package' ? 'tar' : 'md')
}
function requestDelete() {
  if (!props.request.can_delete || deleting.value) return
  deleteError.value = ''; confirmOpen.value = true
}
let exportController: AbortController | undefined
function cancelExport() { exportController?.abort(); exporting.value = false }
watch(() => props.request.id, cancelExport, { flush: 'sync' })
onBeforeUnmount(cancelExport)
async function exportReport(format: 'md' | 'tar') {
  if (exporting.value || purging.value || !props.request.result) return
  const id = props.request.id
  exportController = new AbortController()
  const signal = exportController.signal
  exporting.value = true
  try {
    let markdown = await fetchReportMarkdown(verificationReportUrl(id), signal)
    let images: ReportImageData[] = []
    if (format === 'tar') {
      try {
        const detail = await getVerificationRequest(id, signal)
        if (detail.id !== id) throw new Error('图片清单身份不匹配。')
        images = await loadReportImages(detail.display_images || [], (image, imageSignal) => fetchBlob(verificationArtifactUrl(id, image.artifact_id), imageSignal), signal)
      } catch {
        if (signal.aborted) return
        markdown += '\n\n图片清单读取失败；正文与原文证据仍已保留。'
      }
    }
    if (signal.aborted) return
    const blob = await createReportExport(markdown, format, `复测报告-${id}`, images)
    if (!signal.aborted) downloadReportBlob(blob, `复测报告-${id}`, format)
  } catch (error) { if (!signal.aborted) emit('error', `导出报告失败：${errorMessage(error)}`) }
  finally { if (!signal.aborted) exporting.value = false }
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
</script>

<style scoped>
.verification-record-actions { min-width: 0; display: flex; flex-direction: column; align-items: flex-end; gap: var(--space-1); }
.verification-record-actions__buttons { display: flex; align-items: center; gap: var(--space-1); }
.verification-record-actions__reason { margin: 0; max-width: 28ch; white-space: normal; overflow-wrap: anywhere; text-align: left; color: var(--text-muted); font-size: var(--text-sm); line-height: 1.6; }
</style>
