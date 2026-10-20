<template>
  <div class="monaco-reader" :class="{ 'monaco-reader--packet': variant !== 'default', 'monaco-reader--http': variant === 'http' }">
    <div v-if="readingState?.searchOpen || reserveSearchSpace" class="monaco-reader__search" :class="{ 'monaco-reader__search--placeholder': !readingState?.searchOpen }" :aria-hidden="!readingState?.searchOpen" :inert="!readingState?.searchOpen" role="search" :aria-label="`${ariaLabel}搜索已载入文本`">
      <input ref="searchInput" :value="readingState?.query" :aria-label="`${ariaLabel}搜索已载入文本`" placeholder="搜索已载入文本" @input="setQuery" @keydown.enter.prevent="moveMatch($event.shiftKey ? -1 : 1)" @keydown.esc.prevent="closeSearch" />
      <span class="monaco-reader__matches" role="status">{{ matches.length ? `${(readingState?.matchIndex ?? 0) + 1} / ${matches.length === 10000 ? '≥' : ''}${matches.length}` : readingState?.query ? '无匹配' : '输入关键词' }}</span>
      <Button variant="ghost" size="sm" :disabled="!matches.length" :aria-label="`${ariaLabel}上一处匹配`" title="上一处（Shift+Enter）" @click="moveMatch(-1)"><ChevronUp :size="14" /></Button>
      <Button variant="ghost" size="sm" :disabled="!matches.length" :aria-label="`${ariaLabel}下一处匹配`" title="下一处（Enter）" @click="moveMatch(1)"><ChevronDown :size="14" /></Button>
      <Button variant="ghost" size="sm" :aria-label="`关闭${ariaLabel}搜索`" @click="closeSearch"><X :size="14" /></Button>
    </div>
    <div ref="container" class="monaco-editor-host" :style="hostStyle" />
  </div>
</template>

<script lang="ts">
import type { editor as MonacoTypes } from 'monaco-editor/esm/vs/editor/editor.api.js'
export interface MonacoReadingState {
  views: Record<string, MonacoTypes.ICodeEditorViewState | null>
  searchOpen: boolean
  query: string
  matchIndex: number
}
</script>

<script setup lang="ts">
import '@/lib/monacoEnvironment'
import 'monaco-editor/min/vs/editor/editor.main.css'
import { computed, nextTick, onBeforeUnmount, onMounted, shallowRef, watch } from 'vue'
import { ChevronDown, ChevronUp, X } from '@lucide/vue'
import { Button } from '@/components/ui'
import '@/lib/monacoHttp'
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api.js'
import 'monaco-editor/esm/vs/basic-languages/cpp/cpp.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/csharp/csharp.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/dockerfile/dockerfile.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/go/go.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/ini/ini.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/java/java.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/javascript/javascript.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/lua/lua.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/php/php.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/python/python.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/ruby/ruby.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/rust/rust.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/shell/shell.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/sql/sql.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/typescript/typescript.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/xml/xml.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution.js'
import 'monaco-editor/esm/vs/language/css/monaco.contribution.js'
import 'monaco-editor/esm/vs/language/html/monaco.contribution.js'
import 'monaco-editor/esm/vs/language/json/monaco.contribution.js'
import 'monaco-editor/esm/vs/language/typescript/monaco.contribution.js'
import { useUIStore } from '@/stores/ui'

const props = withDefaults(
  defineProps<{
    modelValue: string
    language?: string
    readOnly?: boolean
    disabled?: boolean
    height?: number | string
    minHeight?: number
    autoHeight?: boolean
    maxHeight?: number | string
    wordWrap?: 'on' | 'off'
    ariaLabel?: string
    autoFocus?: boolean
    fontSize?: number
    variant?: 'default' | 'packet' | 'http'
    reserveSearchSpace?: boolean
    readingState?: MonacoReadingState
    readingView?: string
  }>(),
  {
    language: 'plaintext',
    readOnly: false,
    disabled: false,
    minHeight: 420,
    autoHeight: false,
    wordWrap: 'on',
    ariaLabel: '文本编辑器',
    autoFocus: false,
    fontSize: 14,
    variant: 'default',
    readingView: 'raw',
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  overflowChange: [overflow: boolean]
  contentHeightChange: [height: number]
}>()

const container = shallowRef<HTMLDivElement | null>(null)
const editorInstance = shallowRef<monaco.editor.IStandaloneCodeEditor | null>(null)
let changeDisposable: monaco.IDisposable | null = null
let contentSizeDisposable: monaco.IDisposable | null = null
let layoutDisposable: monaco.IDisposable | null = null
let applyingExternalValue = false
const renderedContentHeight = shallowRef(0)

const uiStore = useUIStore()

const isReadOnly = computed(() => props.readOnly || props.disabled)
const searchInput = shallowRef<HTMLInputElement | null>(null)
const matches = shallowRef<monaco.editor.FindMatch[]>([])
let searchDecorations: monaco.editor.IEditorDecorationsCollection | undefined
const readingActions: monaco.IDisposable[] = []
let currentView = props.readingView

function highlightMatch(reveal = false) {
  const state = props.readingState
  const editor = editorInstance.value
  if (!state || !editor || !searchDecorations) return
  state.matchIndex = Math.min(Math.max(0, state.matchIndex), Math.max(0, matches.value.length - 1))
  searchDecorations.set(state.searchOpen ? matches.value.map((match, index) => ({
    range: match.range,
    options: { inlineClassName: index === state.matchIndex ? 'packet-search-match--current' : 'packet-search-match' },
  })) : [])
  const match = matches.value[state.matchIndex]
  if (reveal && match) editor.revealRangeInCenterIfOutsideViewport(match.range)
}

function search(reveal = false) {
  const state = props.readingState
  // 使用 Monaco 的文本模型搜索，范围始终是当前已载入视图，不读取原件或远程内容。
  matches.value = state?.searchOpen && state.query
    ? editorInstance.value?.getModel()?.findMatches(state.query, false, false, false, null, false, 10000) || []
    : []
  highlightMatch(reveal)
}

function setQuery(event: Event) {
  if (!props.readingState) return
  props.readingState.query = (event.target as HTMLInputElement).value
  props.readingState.matchIndex = 0
  search(true)
}

function moveMatch(direction: number) {
  const state = props.readingState
  if (!state || !matches.value.length) return
  state.matchIndex = (state.matchIndex + direction + matches.value.length) % matches.value.length
  highlightMatch(true)
}

function closeSearch() {
  if (props.readingState) props.readingState.searchOpen = false
  search()
  editorInstance.value?.focus()
}

function saveReadingView() {
  if (props.readingState) props.readingState.views[currentView] = editorInstance.value?.saveViewState() || null
}

watch(() => props.readingState?.searchOpen, async (open) => {
  search()
  if (open) { await nextTick(); searchInput.value?.focus() }
})

const hostStyle = computed(() => {
  const height = props.autoHeight
    ? `${Math.max(props.minHeight, renderedContentHeight.value)}px`
    : typeof props.height === 'number' ? `${props.height}px` : props.height || `${props.minHeight}px`
  return {
    height,
    minHeight: `${props.minHeight}px`,
    maxHeight: props.autoHeight ? typeof props.maxHeight === 'number' ? `${props.maxHeight}px` : props.maxHeight : undefined,
  }
})

function measureContent() {
  const editor = editorInstance.value
  if ((!props.autoHeight && props.variant !== 'http') || !editor) return
  // 窄态隐藏侧没有可用行宽，不让其临时软换行高度撑大整对正文。
  if (props.variant === 'http' && !container.value?.clientWidth) return
  // getContentHeight 包含真实软换行但不以当前视窗封底，可用于整对高度协调。
  const height = editor.getContentHeight()
  if (height !== renderedContentHeight.value) renderedContentHeight.value = height
  // 原文更新可清空父级协调值，即使内容高度相同也需重新回报。
  emit('contentHeightChange', height)
  const overflow = renderedContentHeight.value > editor.getLayoutInfo().height + 1
  emit('overflowChange', overflow)
}

const defineTheme = () => {
  // Monaco 不继承 CSS 变量，按当前主题读取共享色板，避免维护第二套深浅配色。
  const styles = getComputedStyle(document.documentElement)
  const color = (token: string) => styles.getPropertyValue(`--color-${token}`).trim()
  const foreground = (token: string) => color(token).slice(1)
  monaco.editor.defineTheme(monacoThemeName(uiStore.theme), {
    base: uiStore.theme === 'light' ? 'vs' : 'vs-dark',
    inherit: true,
    rules: [
      { token: 'comment', foreground: foreground('text-tertiary') },
      { token: 'string', foreground: foreground('success') },
      { token: 'number', foreground: foreground('warning') },
      { token: 'keyword', foreground: foreground('accent') },
    ],
    colors: {
      'editor.background': color('surface-inset'),
      'editor.foreground': color('text'),
      'editorLineNumber.foreground': color('text-tertiary'),
      'editorLineNumber.activeForeground': color('text-secondary'),
      'editor.selectionBackground': `${color('accent')}38`,
      'editor.inactiveSelectionBackground': `${color('accent')}20`,
      'editor.lineHighlightBackground': color('surface-hover'),
      'editorCursor.foreground': color('accent'),
      'editorWidget.background': color('surface-raised'),
      'editorWidget.border': color('border-strong'),
      'editorSuggestWidget.background': color('surface-raised'),
      'editorSuggestWidget.border': color('border-strong'),
      'editorSuggestWidget.selectedBackground': color('surface-selected'),
      'input.background': color('surface-inset'),
      'input.foreground': color('text'),
      'focusBorder': color('accent'),
      'scrollbarSlider.background': `${color('border-strong')}66`,
      'scrollbarSlider.hoverBackground': `${color('text-tertiary')}80`,
    },
  })
}

const monacoThemeName = (mode: string) => (mode === 'light' ? 'cairn-light' : 'cairn-dark')

const createEditor = () => {
  if (!container.value || editorInstance.value) return
  defineTheme()
  const editor = monaco.editor.create(container.value, {
    value: props.modelValue,
    language: props.language,
    theme: monacoThemeName(uiStore.theme),
    readOnly: isReadOnly.value,
    ariaLabel: props.ariaLabel,
    automaticLayout: true,
    minimap: { enabled: false },
    scrollBeyondLastLine: false,
    wordWrap: props.wordWrap,
    fontSize: props.fontSize,
    lineHeight: props.variant === 'http' ? 20 : Math.round(props.fontSize * 1.65),
    fontFamily: getComputedStyle(document.documentElement).getPropertyValue('--font-mono').trim(),
    tabSize: 2,
    insertSpaces: true,
    lineNumbersMinChars: 3,
    overviewRulerBorder: false,
    renderLineHighlight: 'gutter',
    renderWhitespace: 'selection',
    padding: { top: 10, bottom: 10 },
    scrollbar: {
      horizontalScrollbarSize: 10,
      verticalScrollbarSize: 10,
      alwaysConsumeMouseWheel: !props.autoHeight,
    },
    // 装饰槽分为数字右留白 7px、分隔线 1px 和正文左留白 12px；数字宽度仍由 Monaco 随位数计算。
    ...(props.variant === 'http' ? { lineNumbersMinChars: 4, lineDecorationsWidth: 20, overviewRulerLanes: 0 } : {}),
    ...(props.variant !== 'default' ? {
      domReadOnly: true,
      folding: false,
      glyphMargin: false,
      contextmenu: false,
      links: false,
      renderLineHighlight: 'none' as const,
      renderValidationDecorations: 'off' as const,
      quickSuggestions: false,
      suggestOnTriggerCharacters: false,
      wordBasedSuggestions: 'off' as const,
      stickyScroll: { enabled: false },
      guides: { indentation: false },
      occurrencesHighlight: 'off' as const,
      selectionHighlight: false,
    } : {}),
  })
  editorInstance.value = editor
  if (props.readingState) {
    searchDecorations = editor.createDecorationsCollection()
    const view = props.readingState.views[currentView]
    if (view) editor.restoreViewState(view)
    search()
    // addAction 自动限定 editorId；卸载时释放，避免快捷键打开其他交互或侧别的搜索。
    readingActions.push(editor.addAction({
      id: 'packet.find',
      label: '搜索已载入文本',
      keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyF],
      keybindingContext: 'editorTextFocus',
      run: async () => { props.readingState!.searchOpen = true; await nextTick(); searchInput.value?.focus() },
    }), editor.addAction({
      id: 'packet.closeFind',
      label: '关闭报文搜索',
      keybindings: [monaco.KeyCode.Escape],
      keybindingContext: 'editorTextFocus',
      run: closeSearch,
    }))
  }
  changeDisposable = editor.onDidChangeModelContent(() => {
    if (!applyingExternalValue) emit('update:modelValue', editor.getValue())
  })
  if (props.autoHeight || props.variant === 'http') {
    contentSizeDisposable = editor.onDidContentSizeChange(measureContent)
    layoutDisposable = editor.onDidLayoutChange(measureContent)
    measureContent()
  }
  if (props.autoFocus) editor.focus()
}

watch(
  () => [props.modelValue, props.readingView] as const,
  ([next, view]) => {
    const editor = editorInstance.value
    if (!editor) return
    saveReadingView()
    const state = props.readingState ? props.readingState.views[view] : editor.saveViewState()
    currentView = view
    if (editor.getValue() !== next) {
      applyingExternalValue = true
      editor.setValue(next)
      applyingExternalValue = false
    }
    if (state) editor.restoreViewState(state)
    else if (props.readingState) { editor.setPosition({ lineNumber: 1, column: 1 }); editor.setScrollPosition({ scrollTop: 0, scrollLeft: 0 }) }
    search()
    measureContent()
  },
)

watch(
  () => props.language,
  (language) => {
    const model = editorInstance.value?.getModel()
    if (model) monaco.editor.setModelLanguage(model, language || 'plaintext')
  },
)

watch(isReadOnly, (readOnly) => {
  editorInstance.value?.updateOptions({ readOnly })
})

watch(
  () => props.wordWrap,
  (wordWrap) => {
    editorInstance.value?.updateOptions({ wordWrap })
  },
)

// 全局主题切换时联动 Monaco。monaco.editor.setTheme 是全局生效，多个编辑器实例重复调用幂等。
watch(
  () => uiStore.theme,
  (mode) => {
    defineTheme()
    monaco.editor.setTheme(monacoThemeName(mode))
  },
)

function selectAll() {
  const editor = editorInstance.value
  const model = editor?.getModel()
  if (!editor || !model) return
  editor.focus()
  editor.setSelection(model.getFullModelRange())
}
defineExpose({ selectAll })

onMounted(createEditor)

onBeforeUnmount(() => {
  saveReadingView()
  searchDecorations?.clear()
  readingActions.forEach((action) => action.dispose())
  changeDisposable?.dispose()
  contentSizeDisposable?.dispose()
  layoutDisposable?.dispose()
  editorInstance.value?.dispose()
})
</script>

<style scoped>
.monaco-reader { min-width: 0; }
.monaco-reader--packet { background: var(--surface-inset); }
.monaco-reader--packet .monaco-editor-host { border: 0; border-radius: 0; }
.monaco-reader__search { display: flex; align-items: center; gap: var(--space-1); padding: var(--space-1) var(--space-2); border-bottom: 1px solid var(--border); }
.monaco-reader__search input { min-width: 0; flex: 1; padding: var(--space-1) var(--space-2); color: var(--text); background: var(--bg-elevated); border: 1px solid var(--border); border-radius: var(--radius-sm); font: inherit; font-size: var(--text-sm); }
.monaco-reader__search input:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.monaco-reader__matches { flex-shrink: 0; color: var(--text-muted); font-size: var(--text-sm); font-variant-numeric: tabular-nums; }
.monaco-reader__search--placeholder { visibility: hidden; }
.monaco-reader--http, .monaco-reader--http :deep(.monaco-editor), .monaco-reader--http :deep(.monaco-editor-background) { background: var(--color-surface); }
.monaco-reader--http { border-radius: inherit; overflow: clip; }
.monaco-reader--http .monaco-reader__search { min-height: 41px; background: var(--color-surface-inset); }
.monaco-reader--http .monaco-reader__search .ui-button { flex: none; width: 32px; min-height: 32px; padding: 0; }
.monaco-reader--packet :deep(.margin) { border-right: 1px solid var(--border); }
/* Monaco 主题全局共享；仅 HTTP 实例用局部表面与装饰槽留出正文内边距。 */
.monaco-reader--http :deep(.margin) { border-right: 0; background: linear-gradient(to right, var(--color-surface-inset) calc(100% - 12px), var(--color-surface) calc(100% - 12px)); }
.monaco-reader--http :deep(.margin)::after { content: ''; position: absolute; inset-block: 0; right: 12px; width: 1px; background: var(--border); }
.monaco-reader--packet :deep(.packet-search-match) { background: color-mix(in srgb, var(--warning) 18%, transparent); }
.monaco-reader--packet :deep(.packet-search-match--current) { background: color-mix(in srgb, var(--warning) 32%, transparent); outline: 1px solid var(--warning); }
.monaco-editor-host {
  width: 100%;
  min-width: 0;
  overflow: hidden;
}
</style>
