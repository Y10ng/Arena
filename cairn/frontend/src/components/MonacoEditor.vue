<template>
  <div ref="container" class="monaco-editor-host" :style="hostStyle" />
</template>

<script setup lang="ts">
import '@/lib/monacoEnvironment'
import 'monaco-editor/min/vs/editor/editor.main.css'
import { computed, onBeforeUnmount, onMounted, shallowRef, watch } from 'vue'
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
    wordWrap?: 'on' | 'off'
    ariaLabel?: string
    autoFocus?: boolean
  }>(),
  {
    language: 'plaintext',
    readOnly: false,
    disabled: false,
    minHeight: 420,
    wordWrap: 'on',
    ariaLabel: '文本编辑器',
    autoFocus: false,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const container = shallowRef<HTMLDivElement | null>(null)
const editorInstance = shallowRef<monaco.editor.IStandaloneCodeEditor | null>(null)
let changeDisposable: monaco.IDisposable | null = null
let applyingExternalValue = false
let themeReady = false

const uiStore = useUIStore()

const isReadOnly = computed(() => props.readOnly || props.disabled)

const hostStyle = computed(() => {
  const height =
    typeof props.height === 'number' ? `${props.height}px` : props.height || `${props.minHeight}px`
  return {
    height,
    minHeight: `${props.minHeight}px`,
  }
})

const defineTheme = () => {
  if (themeReady) return
  monaco.editor.defineTheme('cairn-dark', {
    base: 'vs-dark',
    inherit: true,
    rules: [
      { token: 'comment', foreground: '707681' },
      { token: 'string', foreground: '71B991' },
      { token: 'number', foreground: 'C8A96A' },
      { token: 'keyword', foreground: '78A9C7' },
    ],
    colors: {
      'editor.background': '#08090B',
      'editor.foreground': '#F2F3F5',
      'editorLineNumber.foreground': '#707681',
      'editorLineNumber.activeForeground': '#A8ADB7',
      'editor.selectionBackground': '#E7E9ED2B',
      'editor.inactiveSelectionBackground': '#E7E9ED18',
      'editor.lineHighlightBackground': '#17191E99',
      'editorCursor.foreground': '#E7E9ED',
      'editorWidget.background': '#17191E',
      'editorWidget.border': '#3B4049',
      'scrollbarSlider.background': '#3B404966',
      'scrollbarSlider.hoverBackground': '#70768180',
    },
  })
  // 浅色主题：背景与 tokens.css 浅色底色对齐，语法色加深以保证对比度。
  monaco.editor.defineTheme('cairn-light', {
    base: 'vs',
    inherit: true,
    rules: [
      { token: 'comment', foreground: '858981' },
      { token: 'string', foreground: '397556' },
      { token: 'number', foreground: '846725' },
      { token: 'keyword', foreground: '426F8A' },
    ],
    colors: {
      'editor.background': '#FFFFFF',
      'editor.foreground': '#171A1F',
      'editorLineNumber.foreground': '#6B7280',
      'editorLineNumber.activeForeground': '#4B5563',
      'editor.selectionBackground': '#1F293724',
      'editor.inactiveSelectionBackground': '#1F293714',
      'editor.lineHighlightBackground': '#F2F4F799',
      'editorCursor.foreground': '#1F2937',
      'editorWidget.background': '#FFFFFF',
      'editorWidget.border': '#C8CED6',
      'scrollbarSlider.background': '#C8CED666',
      'scrollbarSlider.hoverBackground': '#6B728080',
    },
  })
  themeReady = true
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
    fontSize: 12,
    lineHeight: 19,
    fontFamily:
      'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Noto Sans Mono CJK SC", monospace',
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
    },
  })
  editorInstance.value = editor
  changeDisposable = editor.onDidChangeModelContent(() => {
    if (!applyingExternalValue) emit('update:modelValue', editor.getValue())
  })
  if (props.autoFocus) editor.focus()
}

watch(
  () => props.modelValue,
  (next) => {
    const editor = editorInstance.value
    if (!editor || editor.getValue() === next) return
    const state = editor.saveViewState()
    applyingExternalValue = true
    editor.setValue(next)
    applyingExternalValue = false
    if (state) editor.restoreViewState(state)
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
    if (!themeReady) defineTheme()
    monaco.editor.setTheme(monacoThemeName(mode))
  },
)

onMounted(createEditor)

onBeforeUnmount(() => {
  changeDisposable?.dispose()
  editorInstance.value?.dispose()
})
</script>

<style scoped>
.monaco-editor-host {
  width: 100%;
  min-width: 0;
  overflow: hidden;
}
</style>
