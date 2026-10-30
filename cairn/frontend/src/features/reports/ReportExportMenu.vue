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
        <p v-if="loadingCatalog" role="status">正在读取报告模板…</p>
        <template v-else>
          <div class="report-export__options">
            <FormField v-if="availableTemplates.length" label="报告模板" :for="selectId">
              <Select
                :id="selectId"
                v-model="selectedTemplate"
                :options="
                  availableTemplates.map((item) => ({
                    value: item.key,
                    label: item.name + (item.is_default ? '（系统默认）' : ''),
                  }))
                "
                :disabled="exporting"
              />
            </FormField>
            <FormField label="导出格式" :for="`${selectId}-format`">
              <Select
                :id="`${selectId}-format`"
                :model-value="selectedFormat"
                :options="reportExportFormats"
                :disabled="exporting"
                @update:model-value="selectedFormat = $event as ReportExportFormat"
              />
            </FormField>
          </div>
          <p v-if="!availableTemplates.length && !error" role="alert">
            没有适用于单漏洞的报告模板。
          </p>
          <p v-else-if="availableTemplates.length && !selectedTemplate" role="status">
            请选择报告模板。
          </p>
          <p v-if="excludedTemplates.length" class="report-export__status">
            仅显示适用于单漏洞的模板。
          </p>
        </template>
        <slot name="preparation" :template-key="selectedTemplate" />
        <section v-if="selectedTemplate" class="report-export__preview" aria-label="报告预览">
          <div class="report-export__preview-head">
            <h3>报告预览</h3>
            <Button
              variant="ghost"
              size="sm"
              :loading="previewLoading"
              :disabled="exporting"
              @click="loadPreview"
              >刷新预览</Button
            >
          </div>
          <p v-if="previewLoading" role="status">正在读取预览…</p>
          <details v-else-if="previewError" class="report-export__error" open>
            <summary>预览读取失败</summary>
            <p>{{ previewError }}</p>
          </details>
          <SafeReportMarkdown v-else-if="previewMarkdown" :content="previewMarkdown" />
        </section>
        <details v-if="error" class="report-export__error" open role="alert">
          <summary>导出失败</summary>
          <p>{{ error }}</p>
        </details>
      </div>
      <template #footer>
        <Button variant="ghost" @click="closeDialog(false)">{{
          exporting ? '取消导出' : '关闭'
        }}</Button>
        <Button
          v-if="error && !availableTemplates.length"
          variant="secondary"
          :loading="loadingCatalog"
          @click="loadCatalog"
          >重试</Button
        >
        <Button
          :loading="exporting"
          :disabled="loadingCatalog || !selectedTemplate"
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
import { listReportTemplateCatalog } from '@/api/client'
import type { ReportTemplateCatalogEntry } from '@/api/types'
import { Button, Dialog, DropdownMenu, FormField, IconButton, Select } from '@/components/ui'
import type { ButtonSize } from '@/components/ui/Button.vue'
import SafeReportMarkdown from './SafeReportMarkdown.vue'
import { loadReportImages, type ReportDisplayImage, type ReportImageData, type ReportImageLoader } from './reportImages'
import {
  createReportExport,
  downloadReportBlob,
  reportExportFormats,
  type ReportExportFormat,
} from './reportExport'

const props = withDefaults(
  defineProps<{
    loadMarkdown: (signal: AbortSignal, templateKey?: string) => Promise<string>
    images?: ReportDisplayImage[]
    loadImages?: (signal: AbortSignal) => Promise<ReportDisplayImage[]>
    loadImage?: ReportImageLoader
    filename: string
    identity: string
    disabled?: boolean
    iconOnly?: boolean
    size?: ButtonSize
    templateMode?: 'single-vulnerability'
    previewRevision?: string
  }>(),
  { disabled: false, iconOnly: false, previewRevision: '' },
)
const emit = defineEmits<{ (event: 'open-change', open: boolean): void }>()
const root = ref<HTMLElement | null>(null)
const menuOpen = ref(false)
const dialogOpen = ref(false)
const exporting = ref(false)
const loadingCatalog = ref(false)
const catalog = ref<ReportTemplateCatalogEntry[]>([])
const selectedTemplate = ref('')
const selectedFormat = ref<ReportExportFormat>('md')
const error = ref('')
const message = ref('')
const feedbackOpen = ref(false)
const previewMarkdown = ref('')
const previewLoading = ref(false)
const previewError = ref('')
const selectId = `report-export-template-${useId()}`
const availableTemplates = computed(() =>
  catalog.value.filter((item) => item.supports_single_vulnerability),
)
const excludedTemplates = computed(() =>
  catalog.value.filter((item) => !item.supports_single_vulnerability),
)
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
  loadingCatalog.value = false
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
    catalog.value = []
    selectedTemplate.value = ''
    previewMarkdown.value = ''
    clearFeedback()
  },
  { flush: 'sync' },
)
watch([selectedTemplate, () => props.previewRevision], () => {
  if (exporting.value) cancel()
  cancelPreview()
  previewMarkdown.value = ''
  previewError.value = ''
  // 预览只读已保存正文；模板、格式和预览永远不提交整理请求。
  if (dialogOpen.value && selectedTemplate.value) void loadPreview()
})
onBeforeUnmount(() => {
  cancel()
  cancelPreview()
  clearTimeout(feedbackTimer)
})

async function loadCatalog() {
  cancel()
  const request = ++generation
  controller = new AbortController()
  loadingCatalog.value = true
  error.value = ''
  try {
    const result = await listReportTemplateCatalog(controller.signal)
    if (request !== generation) return
    catalog.value = result
    const current = selectedTemplate.value
    if (!availableTemplates.value.some((item) => item.key === current)) {
      selectedTemplate.value = availableTemplates.value.find(
        (item) => props.templateMode === 'single-vulnerability' && item.key === 'single-vulnerability-report',
      )?.key || availableTemplates.value.find((item) => item.is_default)?.key || ''
    } else if (selectedTemplate.value) {
      void loadPreview()
    }
  } catch (cause) {
    if (request === generation)
      error.value = cause instanceof Error ? cause.message : '模板读取失败，请重试。'
  } finally {
    if (request === generation) loadingCatalog.value = false
  }
}
async function loadPreview() {
  if (!selectedTemplate.value || !dialogOpen.value) return
  cancelPreview()
  previewController = new AbortController()
  const signal = previewController.signal
  const request = ++previewGeneration
  previewLoading.value = true
  previewError.value = ''
  try {
    const markdown = await props.loadMarkdown(signal, selectedTemplate.value)
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
  if (props.templateMode) {
    // 菜单先收起再打开所属弹层，避免两个浮层争抢焦点。
    menuOpen.value = false
    dialogOpen.value = true
    await loadCatalog()
  } else {
    await exportReport(format)
  }
}
function confirmExport() {
  if (!availableTemplates.value.some((item) => item.key === selectedTemplate.value)) return
  return exportReport(selectedFormat.value, selectedTemplate.value)
}
async function exportReport(format: ReportExportFormat, templateKey?: string) {
  if (exporting.value) return
  cancel()
  controller = new AbortController()
  const signal = controller.signal
  const request = ++generation
  const { filename, loadMarkdown } = props
  exporting.value = true
  clearFeedback()
  try {
    let markdown = await loadMarkdown(signal, templateKey)
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
.report-export__options {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
  gap: var(--space-3);
}
.report-export__preview {
  min-width: 0;
  border-top: 1px solid var(--border-subtle);
  padding-top: var(--space-4);
}
.report-export__preview-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}
.report-export__preview-head h3 {
  margin: 0;
  font-size: var(--text-lg);
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
  .report-export__options {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>

<style>
.report-export-dialog .ui-dialog__description,
.report-export-dialog .ui-dialog__title {
  overflow-wrap: anywhere;
  white-space: normal;
}
.report-export__feedback {
  z-index: var(--z-overlay-child);
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  max-width: min(360px, calc(100vw - var(--space-4)));
  max-height: min(60dvh, var(--reka-popover-content-available-height));
  overflow-y: auto;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
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
