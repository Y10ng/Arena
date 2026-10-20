<template>
  <section ref="reader" class="evidence-reader" :class="{ 'evidence-reader--step': presentation === 'step' }" :aria-label="presentation === 'step' ? '步骤证据' : '关键证据'">
    <header v-if="!isHttp && presentation === 'standalone'" class="evidence-reader__header">
      <h3>关键证据</h3>
      <span v-if="source" class="evidence-reader__source">{{ sourceLabels[source] }}</span>
    </header>
    <nav v-if="!isHttp && (presentation === 'step' ? items.length > 1 : primary.length > 1 || (primary.length === 1 && supporting.length > 0))" class="evidence-reader__navigation" aria-label="关键交互">
      <Button v-for="(item, index) in presentation === 'step' ? items : primary" :key="item.id" size="sm" :variant="selectedId === item.id ? 'secondary' : 'ghost'" :aria-pressed="selectedId === item.id" @click="selectItem(item.id)">{{ item.title || `交互 ${index + 1}` }}</Button>
    </nav>
    <details v-if="!isHttp && presentation === 'standalone' && supporting.length" class="evidence-reader__supporting">
      <summary>补充证据 {{ supporting.length }}</summary>
      <nav class="evidence-reader__navigation" aria-label="补充证据"><Button v-for="(item, index) in supporting" :key="item.id" size="sm" :variant="selectedId === item.id ? 'secondary' : 'ghost'" :aria-pressed="selectedId === item.id" @click="selectItem(item.id)">{{ item.title || `补充 ${index + 1}` }}</Button></nav>
    </details>
    <template v-if="selected">
      <div v-if="!isHttp && presentation === 'standalone'" class="evidence-reader__identity"><strong v-if="selected.title">{{ selected.title }}</strong><span>{{ sourceLabels[selected.source] }}</span></div>
      <div class="evidence-reader__packets" :class="{ 'evidence-reader__packets--http': isHttp }">
        <header v-if="isHttp" class="evidence-reader__summary">
          <h3>HTTP 证据</h3>
          <div v-if="items.length > 1" class="evidence-reader__interaction"><Select :model-value="selectedId" :options="interactionOptions" aria-label="选择证据交互" @update:model-value="selectItem($event)" /></div>
          <span class="evidence-reader__target" :title="requestTarget || selected.title">{{ requestTarget || (presentation === 'standalone' && items.length === 1 ? selected.title : '') }}</span>
          <span class="evidence-reader__source">{{ sourceLabels[selected.source] }}</span>
          <Button v-if="pairOverflows || pairExpanded" class="evidence-reader__icon" variant="ghost" size="sm" :aria-label="pairExpanded ? '收起成对报文' : '放大成对报文'" :title="pairExpanded ? '收起成对报文' : '放大成对报文'" :aria-expanded="pairExpanded" @click="pairExpanded = !pairExpanded"><Minimize2 v-if="pairExpanded" :size="15" /><Maximize2 v-else :size="15" /></Button>
        </header>
        <div v-if="isHttp" class="evidence-reader__side-switch" role="group" aria-label="报文视图">
          <Button v-for="panel in panels" :key="panel.key" size="sm" :variant="activeSide === panel.key ? 'secondary' : 'ghost'" :aria-pressed="activeSide === panel.key" @click="selectSide(panel.key)">{{ panel.label }}<span v-if="!panel.reference && panel.inline === null"> · 未记录</span></Button>
        </div>
        <div class="evidence-reader__panels" :class="{ 'evidence-reader__panels--http': isHttp }">
          <article v-for="panel in displayedPanels" :key="`${entityKey}:${selected.id}:${panel.key}`" class="evidence-reader__panel" :class="{ 'evidence-reader__panel--selected': activeSide === panel.key }" :aria-label="`${panel.label}阅读器`">
            <header class="evidence-reader__toolbar">
              <h4>{{ panel.label }}</h4>
              <span v-if="isHttp && panel.summary.marker" class="evidence-reader__protocol" :title="panel.summary.marker">{{ panel.summary.marker }}</span>
              <template v-if="panel.content !== null">
                <div class="evidence-reader__modes" role="group" :aria-label="`${panel.label}显示模式`">
                  <Button variant="ghost" size="sm" :aria-pressed="panel.state.mode === 'raw'" @click="panel.state.mode = 'raw'">Raw</Button>
                  <Button v-if="panel.view.pretty !== null" variant="ghost" size="sm" :aria-pressed="panel.state.mode === 'pretty'" title="仅格式化正文，不改原文" @click="panel.state.mode = 'pretty'">Pretty</Button>
                </div>
                <div class="evidence-reader__tools">
                  <Button class="evidence-reader__icon" variant="ghost" size="sm" :aria-label="`${panel.label}搜索`" :aria-pressed="panel.state.searchOpen" title="搜索已载入文本" @click="panel.state.searchOpen = !panel.state.searchOpen"><Search :size="15" /></Button>
                  <Button class="evidence-reader__icon" variant="ghost" size="sm" :aria-label="`${panel.label}${isTruncated(panel) ? '复制已载入片段' : '复制原文'}`" :title="isTruncated(panel) ? '复制已载入片段' : '复制原文'" @click="copy(panel)"><Copy :size="15" /></Button>
                  <Button v-if="!isHttp && (overflowPanels[panel.key] || panel.state.expanded)" class="evidence-reader__icon" variant="ghost" size="sm" :aria-label="`${panel.label}${panel.state.expanded ? '收起报文' : '放大报文'}`" :title="panel.state.expanded ? '收起报文' : '放大报文'" :aria-expanded="panel.state.expanded" @click="panel.state.expanded = !panel.state.expanded"><Minimize2 v-if="panel.state.expanded" :size="15" /><Maximize2 v-else :size="15" /></Button>
                </div>
              </template>
              <DropdownMenu v-if="panel.content !== null || (panel.reference && originalUrl(panel.reference))" :items="panel.content !== null ? [{ value: 'wrap', label: panel.state.wrap ? '关闭自动换行' : '开启自动换行' }] : []" :label="`${panel.label}选项`" @select="panel.state.wrap = !panel.state.wrap">
                <template #trigger><Button class="evidence-reader__icon" variant="ghost" size="sm" :aria-label="`${panel.label}更多`" title="更多"><Ellipsis :size="16" /></Button></template>
                <DropdownMenuItem v-if="panel.reference && originalUrl(panel.reference)" as-child class="ui-dropdown-menu__item"><a :href="originalUrl(panel.reference) || undefined" target="_blank" rel="noopener" download>下载原件</a></DropdownMenuItem>
                <p v-if="panel.view.reason" class="evidence-reader__menu-hint">{{ panel.view.reason }}</p>
              </DropdownMenu>
            </header>
            <div class="evidence-reader__prelude">
              <div v-if="copyFeedback[panel.key]" class="evidence-reader__copy-feedback" :class="{ 'evidence-reader__copy-feedback--error': copyFeedback[panel.key]?.status === 'error' }">
                <span role="status" aria-live="polite">{{ copyFeedback[panel.key]?.message }}</span>
                <Button v-if="copyFeedback[panel.key]?.status === 'error' && panel.content !== null" variant="link" size="sm" @click="selectRaw(panel)">选择 Raw 手工复制</Button>
              </div>
              <p v-if="isTruncated(panel)" class="evidence-reader__notice evidence-reader__notice--warning">已载入片段（{{ previews[panel.key]?.preview?.bytes_read }} / {{ previews[panel.key]?.preview?.size }} 字节），复制仅含此片段；请下载原件。</p>
              <p v-if="panel.content !== null && panel.view.limitation && !isTruncated(panel)" class="evidence-reader__notice evidence-reader__notice--warning">{{ panel.view.reason }}</p>
              <p v-else-if="panel.content !== null && panel.state.mode === 'pretty'" class="evidence-reader__hint">格式化视图 · 仅调整 JSON 空白，复制仍为原文。</p>
            </div>
            <div class="evidence-reader__content">
              <template v-if="panel.reference && imageUrl?.(panel.reference)"><p v-if="imageErrors[panel.key]" class="evidence-reader__notice" role="alert">图像读取失败，请读取原件核对。</p><img v-else class="evidence-reader__image" :src="imageUrl(panel.reference) || undefined" :alt="selected.title || '本轮观察图像'" @error="imageErrors[panel.key] = true" /></template>
              <p v-else-if="!panel.reference && panel.inline === null" class="evidence-reader__notice">{{ isHttp ? `未记录${panel.label}` : '未记录证据内容' }}</p>
              <p v-else-if="previews[panel.key]?.loading" class="evidence-reader__notice" role="status">正在读取{{ panel.label }}…</p>
              <div v-else-if="previews[panel.key]?.error" class="evidence-reader__notice" role="alert">{{ previews[panel.key]?.error }}<Button size="sm" variant="ghost" @click="loadPanel(panel)">重试</Button></div>
              <MonacoEditor v-else-if="panel.content !== null" :ref="(editor) => registerEditor(panel.key, editor)" :key="`${entityKey}:${selected.id}:${panel.key}`" :model-value="panel.state.mode === 'pretty' ? panel.view.pretty ?? panel.content : panel.content" :language="panel.view.language" :variant="isHttp ? 'http' : 'packet'" :reading-state="panel.state" :reading-view="panel.state.mode" :reserve-search-space="isHttp && anySearchOpen" read-only :word-wrap="panel.state.wrap ? 'on' : 'off'" :auto-height="!isHttp" :height="isHttp ? packetHeight : undefined" :max-height="panel.state.expanded ? '80dvh' : 'min(48dvh, 520px)'" :min-height="64" :font-size="isHttp ? 13 : 14" :aria-label="panel.label" @content-height-change="contentHeights[panel.key] = $event" @overflow-change="overflowPanels[panel.key] = $event" />
              <p v-else-if="previews[panel.key]?.preview" class="evidence-reader__notice">二进制内容不作文本预览，请读取原件。</p>
            </div>
          </article>
        </div>
      </div>
    </template>
    <p v-else class="evidence-reader__notice">{{ items.length ? '选择补充证据查看' : emptyLabel }}</p>
  </section>
</template>
<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { Copy, Ellipsis, Maximize2, Minimize2, Search } from '@lucide/vue'
import { DropdownMenuItem } from 'reka-ui'
import type { ArtifactPreview } from '@/api/client'
import type { EvidenceItem, EvidenceRef, EvidenceSource } from '@/api/types'
import type { MonacoReadingState } from '@/components/MonacoEditor.vue'
import { httpMessageSummary, httpMessageView } from '@/lib/httpMessageView'
import { Button, DropdownMenu, Select } from '@/components/ui'
const MonacoEditor = defineAsyncComponent(() => import('@/components/MonacoEditor.vue'))
const props = withDefaults(defineProps<{
  entityKey: string
  projectId: string
  items: EvidenceItem[]
  source?: EvidenceSource
  selectedItemId?: string
  loadPreview: (reference: EvidenceRef, signal: AbortSignal) => Promise<ArtifactPreview>
  originalUrl: (reference: EvidenceRef) => string | null
  imageUrl?: (reference: EvidenceRef) => string | null
  presentation?: 'standalone' | 'step'
  emptyLabel?: string
}>(), { presentation: 'standalone', emptyLabel: '未记录关键证据' })
const emit = defineEmits<{ 'update:selectedItemId': [id: string] }>()
const sourceLabels: Record<EvidenceSource, string> = { capture: '采集原件', manual: '人工记录', historical: '历史原文' }
const primary = computed(() => props.items.filter((item) => item.role === 'primary'))
const supporting = computed(() => props.items.filter((item) => item.role === 'supporting'))
const selectedId = ref('')
const selected = computed(() => props.items.find((item) => item.id === selectedId.value))
const isHttp = computed(() => selected.value?.kind === 'http')
const interactionOptions = computed(() => props.items.map((item, index) => ({ value: item.id, label: `${item.role === 'supporting' ? '补充 · ' : ''}${item.title || `交互 ${index + 1}`}` })))
const activeSide = ref('request')
const activeSides = new Map<string, string>()
function selectSide(side: string) { activeSide.value = side; activeSides.set(selectedId.value, side) }
const overflowPanels = ref<Record<string, boolean>>({})
const contentHeights = ref<Record<string, number>>({})
const expandedItems = reactive(new Set<string>())
const pairExpanded = computed({ get: () => expandedItems.has(selectedId.value), set: (value: boolean) => { if (value) expandedItems.add(selectedId.value); else expandedItems.delete(selectedId.value) } })
const pairOverflows = computed(() => Object.values(overflowPanels.value).some(Boolean))
// 使用真实内容测量值而非已协调的视窗高度，避免左右高度互相回写。
const packetHeight = computed(() => `min(${Math.max(64, ...Object.values(contentHeights.value))}px, ${pairExpanded.value ? '80dvh' : 'clamp(280px, 36dvh, 400px)'})`)
interface Panel { key: string; label: string; reference: EvidenceRef | null; inline: string | null }
interface PreviewState { loading: boolean; preview: ArtifactPreview | null; error: string }
interface ReadingState extends MonacoReadingState { mode: 'raw' | 'pretty'; wrap: boolean; expanded: boolean }
// 仅在当前阅读器生命周期内保留各交互的视图，不把证据或搜索词写入浏览器存储。
const readingStates = new Map<string, { identity: string | null; state: ReadingState }>()
function stateFor(panel: Panel) {
  const key = JSON.stringify([props.entityKey, selectedId.value, panel.key])
  const identity = panel.reference ? JSON.stringify(panel.reference) : panel.inline
  let saved = readingStates.get(key)
  if (!saved || saved.identity !== identity) {
    saved = { identity, state: reactive({ mode: 'raw', wrap: true, expanded: false, views: {}, searchOpen: false, query: '', matchIndex: 0 }) }
    readingStates.set(key, saved)
  }
  return saved.state
}
const panels = computed<Panel[]>(() => {
  const item = selected.value
  if (!item) return []
  if (item.kind === 'http') return [
    { key: 'request', label: '请求', reference: item.request ?? null, inline: item.request_text ?? null },
    { key: 'response', label: '响应', reference: item.response ?? null, inline: item.response_text ?? null },
  ]
  return [{ key: 'artifact', label: item.kind === 'text' ? '记录原文' : '证据原件', reference: item.artifact ?? null, inline: item.text ?? null }]
})
const previews = ref<Record<string, PreviewState>>({})
const displayedPanels = computed(() => panels.value.map((panel) => {
  const content = panelContent(panel)
  return { ...panel, content, view: httpMessageView(content || '', isTruncated(panel)), summary: httpMessageSummary(content || ''), state: stateFor(panel) }
}))
const requestTarget = computed(() => displayedPanels.value.find((panel) => panel.key === 'request')?.summary.target || '')
const anySearchOpen = computed(() => displayedPanels.value.some((panel) => panel.state.searchOpen))
const reader = ref<HTMLElement>()
const editorRefs = new Map<string, InstanceType<typeof MonacoEditor>>()
function registerEditor(key: string, editor: unknown) {
  if (editor) editorRefs.set(key, editor as InstanceType<typeof MonacoEditor>)
  else editorRefs.delete(key)
}
const copyFeedback = ref<Record<string, { status: 'copying' | 'success' | 'error'; message: string }>>({})
const copyOperations = new Map<string, symbol>()
const imageErrors = ref<Record<string, boolean>>({})
const initialSelection = () => props.items.find((item) => item.id === props.selectedItemId)?.id || primary.value[0]?.id || (props.presentation === 'step' ? props.items[0]?.id || '' : '')
function selectItem(id: string) { selectedId.value = id; emit('update:selectedItemId', id) }
const controllers = new Map<string, AbortController>()
let generation = 0
function cancel() { generation++; controllers.forEach((controller) => controller.abort()); controllers.clear() }
watch(() => props.entityKey, () => { cancel(); selectedId.value = initialSelection(); previews.value = {}; copyFeedback.value = {}; copyOperations.clear(); readingStates.clear(); activeSides.clear(); expandedItems.clear() }, { flush: 'sync' })
watch(() => props.items, () => {
  if (!props.items.some((item) => item.id === selectedId.value)) selectedId.value = initialSelection()
}, { immediate: true })
watch(() => props.selectedItemId, (id) => { if (props.items.some((item) => item.id === id)) selectedId.value = id! })
watch(() => JSON.stringify([props.entityKey, selected.value]), () => {
  cancel(); previews.value = {}; copyFeedback.value = {}; copyOperations.clear(); imageErrors.value = {}; overflowPanels.value = {}; contentHeights.value = {}
  activeSide.value = activeSides.get(selectedId.value) || panels.value.find((panel) => panel.reference || panel.inline !== null)?.key || 'request'
  for (const panel of panels.value) if (panel.reference && !props.imageUrl?.(panel.reference)) void loadPanel(panel)
}, { immediate: true })
async function loadPanel(panel: Panel) {
  if (!panel.reference) return
  controllers.get(panel.key)?.abort()
  const controller = new AbortController()
  controllers.set(panel.key, controller)
  const currentGeneration = generation
  const entity = props.entityKey
  const projectId = props.projectId
  const artifactId = panel.reference.artifact_id
  copyOperations.delete(panel.key)
  delete copyFeedback.value[panel.key]
  previews.value = { ...previews.value, [panel.key]: { loading: true, preview: null, error: '' } }
  const isCurrent = () => !controller.signal.aborted && currentGeneration === generation && entity === props.entityKey && controllers.get(panel.key) === controller
  try {
    const preview = await props.loadPreview(panel.reference, controller.signal)
    if (!isCurrent()) return
    if (preview.project_id !== projectId || preview.artifact_id !== artifactId) throw new Error('证据响应身份不匹配，已停止展示。')
    previews.value = { ...previews.value, [panel.key]: { loading: false, preview, error: '' } }
  } catch (error) {
    if (!isCurrent()) return
    previews.value = { ...previews.value, [panel.key]: { loading: false, preview: null, error: error instanceof Error ? error.message : '证据读取失败' } }
  }
}
function panelContent(panel: Panel): string | null { return panel.reference ? previews.value[panel.key]?.preview?.content ?? null : panel.inline }
function isTruncated(panel: Panel) { return Boolean(previews.value[panel.key]?.preview?.truncated) }
// 局域网 HTTP 入口没有 Clipboard API；仅在用户点击期间使用浏览器仍支持的复制命令。
function copySelectedText(content: string) {
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
    // textarea.value 会规范化 CRLF；复制事件直接提供已载入原文，不依赖选择文本的换行转换。
    event.clipboardData.setData('text/plain', content)
    event.preventDefault()
    suppliedOriginal = true
  })
  reader.value!.appendChild(temporary)
  try {
    temporary.focus({ preventScroll: true })
    temporary.select()
    return document.execCommand('copy') && suppliedOriginal
  } finally {
    temporary.remove()
    focused?.focus({ preventScroll: true })
    if (inputSelection && inputSelection.start !== null && inputSelection.end !== null && (focused instanceof HTMLInputElement || focused instanceof HTMLTextAreaElement)) {
      focused.setSelectionRange(inputSelection.start, inputSelection.end, inputSelection.direction || undefined)
    }
    selection?.removeAllRanges()
    ranges.forEach((range) => selection?.addRange(range))
  }
}
async function copy(panel: Panel) {
  const content = panelContent(panel)
  if (content === null) return
  const currentGeneration = generation
  const operation = Symbol()
  copyOperations.set(panel.key, operation)
  const isCurrent = () => currentGeneration === generation && copyOperations.get(panel.key) === operation
  const truncated = isTruncated(panel)
  const hasClipboardApi = typeof navigator.clipboard?.writeText === 'function'
  copyFeedback.value[panel.key] = { status: 'copying', message: '正在复制…' }
  try {
    if (hasClipboardApi) await navigator.clipboard.writeText(content)
    else if (!copySelectedText(content)) throw new Error('copy_not_accepted')
    if (isCurrent()) copyFeedback.value[panel.key] = { status: 'success', message: truncated ? '已复制已载入片段' : '已复制原文' }
  } catch (error) {
    if (!isCurrent()) return
    const denied = error instanceof DOMException && error.name === 'NotAllowedError'
    copyFeedback.value[panel.key] = { status: 'error', message: hasClipboardApi
      ? denied ? '浏览器未允许剪贴板写入。可检查本站权限，或选择 Raw 手工复制。' : '剪贴板写入未完成，请选择 Raw 手工复制。'
      : '此环境没有 Clipboard API，浏览器复制命令也未完成。请选择 Raw 手工复制。' }
  }
}
async function selectRaw(panel: Panel) {
  const currentGeneration = generation
  stateFor(panel).mode = 'raw'
  await nextTick()
  if (currentGeneration === generation) editorRefs.get(panel.key)?.selectAll()
}
onBeforeUnmount(cancel)
</script>
<style scoped>
.evidence-reader { container-type: inline-size; container-name: evidence-reader; min-width: 0; display: flex; flex-direction: column; gap: var(--space-2); }
.evidence-reader__header, .evidence-reader__identity, .evidence-reader__navigation, .evidence-reader__side-switch { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
.evidence-reader__header h3 { flex: 1; margin: 0; font-size: var(--text-md); font-weight: var(--font-semibold); }
.evidence-reader__source, .evidence-reader__identity { color: var(--text-muted); font-size: var(--text-sm); }
.evidence-reader__identity strong { color: var(--text); }
.evidence-reader__navigation .ui-button { max-width: 100%; height: auto; min-height: 32px; white-space: normal; overflow-wrap: anywhere; }
.evidence-reader__packets { min-width: 0; border: 1px solid var(--border); border-radius: var(--radius-xl); background: var(--color-surface); }
.evidence-reader__summary { display: flex; align-items: center; gap: var(--space-2); min-height: 40px; padding: var(--space-1) var(--space-2) var(--space-1) var(--space-3); border-bottom: 1px solid var(--border); border-radius: var(--radius-xl) var(--radius-xl) 0 0; background: var(--color-surface-inset); }
.evidence-reader__summary h3 { flex: none; margin: 0; font-size: var(--text-sm); font-weight: var(--font-semibold); }
.evidence-reader__interaction { min-width: 0; max-width: 240px; flex: 0 1 auto; }
.evidence-reader__interaction :deep(.ui-select__trigger) { min-height: 32px; }
.evidence-reader__target { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); font: var(--text-sm)/var(--leading-normal) var(--font-mono); }
.evidence-reader__summary .evidence-reader__source { flex: none; }
.evidence-reader__panels { display: grid; grid-template-columns: minmax(0, 1fr); align-items: start; }
.evidence-reader__panels--http { grid-template-columns: repeat(2, minmax(0, 1fr)); grid-template-rows: auto auto auto; align-items: stretch; }
.evidence-reader__panel { min-width: 0; }
.evidence-reader__panels--http .evidence-reader__panel { display: grid; grid-row: 1 / 4; grid-template-rows: subgrid; }
.evidence-reader__panels--http .evidence-reader__panel + .evidence-reader__panel { grid-column: 2; border-left: 1px solid var(--border); }
.evidence-reader__content { min-width: 0; background: var(--color-surface); }
.evidence-reader__panel:first-child .evidence-reader__content { border-bottom-left-radius: var(--radius-xl); }
.evidence-reader__panel:last-child .evidence-reader__content { border-bottom-right-radius: var(--radius-xl); }
.evidence-reader__side-switch { display: none; padding: var(--space-1) var(--space-2); border-bottom: 1px solid var(--border); background: var(--color-surface-inset); }
.evidence-reader__toolbar { display: flex; align-items: center; flex-wrap: nowrap; gap: var(--space-1); min-width: 0; min-height: 44px; padding: var(--space-1) var(--space-2); border-bottom: 1px solid var(--border); background: var(--color-surface-inset); }
.evidence-reader__toolbar h4 { flex: none; margin: 0; font-size: var(--text-sm); font-weight: var(--font-semibold); }
.evidence-reader__protocol { flex: 0 1 auto; max-width: 52px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); font: var(--text-xs)/var(--leading-normal) var(--font-mono); padding-inline: var(--space-1); }
.evidence-reader__modes, .evidence-reader__tools { display: flex; flex: none; align-items: center; gap: 0; }
.evidence-reader__tools { margin-left: auto; }
.evidence-reader__modes .ui-button { position: relative; min-height: 32px; padding-inline: var(--space-1); border-radius: 0; color: var(--text-muted); font-weight: var(--font-normal); }
.evidence-reader__modes .ui-button[aria-pressed="true"] { color: var(--text); background: transparent; }
.evidence-reader__modes .ui-button[aria-pressed="true"]::after { content: ''; position: absolute; height: 2px; bottom: 0; inset-inline: var(--space-1); background: var(--accent); }
.evidence-reader__icon.ui-button { flex: none; width: 32px; min-height: 32px; padding: 0; color: var(--text-muted); }
.evidence-reader__icon[aria-pressed="true"] { color: var(--text); background: var(--surface-selected); }
.evidence-reader__notice { margin: 0; padding: var(--space-2) var(--space-3); color: var(--text-muted); font-size: var(--text-sm); line-height: 1.7; overflow-wrap: anywhere; }
.evidence-reader__hint, .evidence-reader__menu-hint { margin: 0; padding: var(--space-1) var(--space-3); color: var(--text-muted); font-size: var(--text-sm); line-height: 1.5; }
.evidence-reader__menu-hint { max-width: 280px; padding-block: var(--space-2); }
.evidence-reader__notice--warning { color: var(--color-warning); }
.evidence-reader__copy-feedback { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-1) var(--space-2); padding: var(--space-1) var(--space-3); color: var(--text-muted); font-size: var(--text-sm); line-height: 1.5; }
.evidence-reader__copy-feedback--error { color: var(--color-warning); }
.evidence-reader__copy-feedback .ui-button { min-height: 28px; height: auto; padding: 0; white-space: normal; text-align: left; }
.evidence-reader__image { display: block; max-width: 100%; max-height: 60dvh; object-fit: contain; }
.evidence-reader__supporting summary { color: var(--text-muted); cursor: pointer; padding-block: var(--space-2); font-size: var(--text-sm); }
@container evidence-reader (max-width: 599px) {
  .evidence-reader__side-switch { display: flex; }
  .evidence-reader__summary { flex-wrap: wrap; }
  .evidence-reader__interaction { max-width: calc(100% - 100px); }
  .evidence-reader__target { flex-basis: 100%; order: 1; }
  .evidence-reader__target:empty { display: none; }
  .evidence-reader__summary .evidence-reader__source { margin-left: auto; }
  .evidence-reader__panels--http { grid-template-columns: minmax(0, 1fr); }
  .evidence-reader__panels--http .evidence-reader__panel { grid-column: 1; }
  .evidence-reader__panels--http .evidence-reader__panel + .evidence-reader__panel { grid-column: 1; border-left: 0; }
  .evidence-reader__panels--http .evidence-reader__panel:not(.evidence-reader__panel--selected) { display: none; }
}
</style>
