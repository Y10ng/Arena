<template>
  <div ref="root" class="report-export" @click.stop @keydown.stop>
    <PopoverRoot :open="feedbackOpen && !dialogOpen" @update:open="feedbackOpen = $event">
      <PopoverAnchor as-child>
        <div class="report-export__anchor">
          <DropdownMenu v-model:open="menuOpen" :items="reportExportFormats" @select="chooseFormat">
            <template #trigger>
              <IconButton
                v-if="iconOnly"
                label="导出报告"
                :size="size"
                :loading="exporting"
                :disabled="disabled"
              >
                <Download :size="16" />
              </IconButton>
              <Button
                v-else
                variant="secondary"
                size="sm"
                :loading="exporting"
                :disabled="disabled"
              >
                <template #icon><Download :size="14" /></template>导出报告
              </Button>
            </template>
          </DropdownMenu>
        </div>
      </PopoverAnchor>
      <PopoverPortal>
        <!-- 弹层关闭后的回焦不应提前吞掉状态提示，仍可点击外部、按 Esc 或显式关闭。 -->
        <PopoverContent
          class="report-export__feedback"
          side="bottom"
          align="end"
          :side-offset="6"
          :collision-padding="8"
          @open-auto-focus="$event.preventDefault()"
          @close-auto-focus="$event.preventDefault()"
          @focus-outside="$event.preventDefault()"
        >
          <details v-if="error" class="report-export__error">
            <summary>导出失败，查看原因</summary>
            <p>{{ error }}</p>
          </details>
          <span v-else aria-hidden="true">{{ message }}</span>
          <IconButton label="关闭导出提示" size="sm" @click="feedbackOpen = false"
            ><X :size="14"
          /></IconButton>
        </PopoverContent>
      </PopoverPortal>
    </PopoverRoot>
    <span class="report-export__live" role="status" aria-live="polite" aria-atomic="true">{{
      error ? '导出失败，可在导出操作旁查看完整原因。' : message
    }}</span>
    <Dialog
      :open="dialogOpen"
      title="导出漏洞报告"
      :description="filename"
      size="lg"
      content-class="report-export-dialog"
      @update:open="closeDialog"
      @close-auto-focus="restoreFocus"
    >
      <div class="report-export__body">
        <slot name="preparation" />
        <ReportReader embedded class="report-export__preview" :identity="JSON.stringify([identity, previewRevision])" :content="previewMarkdown" :ready="!previewLoading && !previewError && Boolean(previewMarkdown)">
          <template #title><h2>报告预览</h2></template>
          <template #actions><Button variant="ghost" size="sm" :loading="previewLoading" :disabled="exporting" @click="loadPreview">刷新预览</Button></template>
          <template #state>
            <p v-if="previewLoading" role="status">正在读取预览…</p>
            <details v-else-if="previewError" class="report-export__error" open><summary>预览读取失败</summary><p>{{ previewError }}</p></details>
            <p v-else>尚未读取到报告正文。</p>
          </template>
        </ReportReader>
        <details v-if="error" class="report-export__error" open role="alert">
          <summary>导出失败</summary>
          <p>{{ error }}</p>
        </details>
      </div>
      <template #footer>
        <div class="report-export__format">
          <label :for="`${selectId}-format`">导出格式</label>
          <Select :id="`${selectId}-format`" class="report-export__format-select" :model-value="selectedFormat" :options="reportExportFormats" :disabled="exporting" @update:model-value="selectedFormat = $event as ReportExportFormat" />
        </div>
        <Button variant="secondary" @click="closeDialog(false)">{{
          exporting ? '取消导出' : '关闭'
        }}</Button>
        <Button
          :loading="exporting"
          :disabled="previewLoading || Boolean(previewError) || !previewMarkdown"
          @click="confirmExport"
          >导出 {{ selectedFormatLabel }}</Button
        >
      </template>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import { Download, X } from '@lucide/vue'
import { PopoverAnchor, PopoverContent, PopoverPortal, PopoverRoot } from 'reka-ui'
import { Button, Dialog, DropdownMenu, IconButton, Select } from '@/components/ui'
import type { ButtonSize } from '@/components/ui/Button.vue'
import ReportReader from './ReportReader.vue'
import { loadReportImages, type ReportDisplayImage, type ReportImageData, type ReportImageLoader } from './reportImages'
import {
  createReportExport,
  downloadReportBlob,
  reportExportFormats,
  type ReportExportFormat,
} from './reportExport'

const props = withDefaults(
  defineProps<{
    loadMarkdown: (signal: AbortSignal) => Promise<string>
    images?: ReportDisplayImage[]
    loadImages?: (signal: AbortSignal) => Promise<ReportDisplayImage[]>
    loadImage?: ReportImageLoader
    filename: string
    identity: string
    disabled?: boolean
    iconOnly?: boolean
    size?: ButtonSize
    preview?: boolean
    previewRevision?: string
  }>(),
  { disabled: false, iconOnly: false, previewRevision: '' },
)
const emit = defineEmits<{ (event: 'open-change', open: boolean): void }>()
const root = ref<HTMLElement | null>(null)
const menuOpen = ref(false)
const dialogOpen = ref(false)
const exporting = ref(false)
const selectedFormat = ref<ReportExportFormat>('md')
const error = ref('')
const message = ref('')
const feedbackOpen = ref(false)
const previewMarkdown = ref('')
const previewLoading = ref(false)
const previewError = ref('')
const selectId = `report-export-template-${useId()}`
const selectedFormatLabel = computed(
  () => reportExportFormats.find((item) => item.value === selectedFormat.value)?.label,
)
let controller: AbortController | undefined
let previewController: AbortController | undefined
let generation = 0
let previewGeneration = 0
let feedbackTimer: ReturnType<typeof setTimeout> | undefined
let trigger: HTMLElement | null = null

function cancel() {
  generation += 1
  controller?.abort()
  exporting.value = false
}
function cancelPreview() {
  previewGeneration += 1
  previewController?.abort()
  previewLoading.value = false
}
function clearFeedback() {
  clearTimeout(feedbackTimer)
  feedbackOpen.value = false
  error.value = ''
  message.value = ''
}
function closeDialog(open: boolean) {
  if (open) return
  cancel()
  cancelPreview()
  dialogOpen.value = false
}
function restoreFocus(event: Event) {
  if (trigger?.isConnected) {
    event.preventDefault()
    trigger.focus()
  }
}
watch(dialogOpen, (open) => emit('open-change', open), { flush: 'sync' })
watch(
  () => props.identity,
  () => {
    closeDialog(false)
    menuOpen.value = false
    previewMarkdown.value = ''
    clearFeedback()
  },
  { flush: 'sync' },
)
watch(() => props.previewRevision, () => {
  if (exporting.value) cancel()
  cancelPreview()
  previewMarkdown.value = ''
  previewError.value = ''
  // 预览只读当前内容，不提交模型生成请求。
  if (dialogOpen.value) void loadPreview()
})
onBeforeUnmount(() => {
  cancel()
  cancelPreview()
  clearTimeout(feedbackTimer)
})

async function loadPreview() {
  if (!dialogOpen.value) return
  cancelPreview()
  previewController = new AbortController()
  const signal = previewController.signal
  const request = ++previewGeneration
  previewLoading.value = true
  previewError.value = ''
  try {
    const markdown = await props.loadMarkdown(signal)
    if (request === previewGeneration && !signal.aborted) previewMarkdown.value = markdown
  } catch (cause) {
    if (request === previewGeneration && !signal.aborted)
      previewError.value = cause instanceof Error ? cause.message : '预览读取失败，请重试。'
  } finally {
    if (request === previewGeneration) previewLoading.value = false
  }
}
async function chooseFormat(value: string) {
  if (exporting.value || props.disabled) return
  const format = reportExportFormats.find((item) => item.value === value)?.value
  if (!format) return
  selectedFormat.value = format
  trigger = root.value?.querySelector<HTMLElement>('button') || null
  clearFeedback()
  if (props.preview) {
    // 菜单先收起再打开所属弹层，避免两个浮层争抢焦点。
    menuOpen.value = false
    dialogOpen.value = true
    await loadPreview()
  } else {
    await exportReport(format)
  }
}
function confirmExport() {
  return exportReport(selectedFormat.value)
}
async function exportReport(format: ReportExportFormat) {
  if (exporting.value) return
  cancel()
  controller = new AbortController()
  const signal = controller.signal
  const request = ++generation
  const { filename, loadMarkdown } = props
  exporting.value = true
  clearFeedback()
  try {
    let markdown = props.preview && previewMarkdown.value ? previewMarkdown.value : await loadMarkdown(signal)
    if (request !== generation || signal.aborted) return
    let imageData: ReportImageData[] = []
    try {
      const images = format !== 'md' && props.loadImages ? await props.loadImages(signal) : props.images || []
      imageData = format !== 'md' && props.loadImage ? await loadReportImages(images, props.loadImage, signal) : images.map((image) => ({ image, error: image.unavailable_reason || undefined }))
    } catch {
      if (signal.aborted) return
      markdown += '\n\n图片清单读取失败；正文与原文证据仍已保留。'
    }
    if (request !== generation || signal.aborted) return
    const blob = await createReportExport(markdown, format, filename, imageData)
    // 转换过程中也可能切换漏洞、Observation 或历史成品，旧结果不得再开始下载。
    if (request !== generation || signal.aborted) return
    downloadReportBlob(blob, filename, format)
    cancelPreview()
    dialogOpen.value = false
    message.value = '已开始下载'
    feedbackOpen.value = true
    feedbackTimer = setTimeout(clearFeedback, 3500)
  } catch (cause) {
    if (request === generation && !signal.aborted) {
      error.value = cause instanceof Error ? cause.message : '导出失败，请重试。'
      feedbackOpen.value = true
    }
  } finally {
    if (request === generation) exporting.value = false
  }
}
</script>

<style scoped>
.report-export,
.report-export__anchor {
  display: inline-flex;
  align-items: center;
  min-width: 0;
}
.report-export__live {
  position: absolute;
  width: 1px;
  height: 1px;
  clip-path: inset(50%);
  white-space: nowrap;
}
.report-export__body {
  display: grid;
  gap: var(--space-4);
  min-width: 0;
}
.report-export__options { min-width: 0; }
.report-export__format { --field-control-height: var(--control-height); display: flex; flex: 1; align-items: center; gap: var(--space-2); min-width: 0; }
.report-export__format > label { flex: none; color: var(--text-muted); font-size: var(--text-sm); }
.report-export__format-select { width: 180px; min-width: 0; }
.report-export__preview {
  min-width: 0;
  height: calc(46 * var(--ui-dvh));
  min-height: 260px;
  flex: none;
  border-top: 1px solid var(--border-subtle);
}
.report-export__error,
.report-export__status {
  margin: 0;
  overflow-wrap: anywhere;
}
.report-export__error {
  color: var(--danger);
}
.report-export__error summary {
  cursor: pointer;
}
.report-export__error p {
  margin: var(--space-2) 0 0;
  white-space: pre-wrap;
}
.report-export__status {
  color: var(--text-muted);
}
@media (max-width: 600px) {
  .report-export__format { flex-basis: 100%; }
}
</style>

<style>
.report-export__feedback {
  z-index: var(--z-overlay-child);
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  max-width: min(360px, calc(100 * var(--ui-vw) - var(--space-4)));
  max-height: min(calc(60 * var(--ui-dvh)), calc(var(--reka-popover-content-available-height) / var(--ui-scale)));
  overflow-y: auto;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-pop);
  color: var(--text);
  font-size: var(--text-md);
  line-height: var(--leading-normal);
}
.report-export__feedback details {
  min-width: 0;
  color: var(--danger);
}
.report-export__feedback summary {
  cursor: pointer;
}
.report-export__feedback p {
  margin: var(--space-2) 0 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.report-export__feedback > button {
  flex-shrink: 0;
}
</style>
