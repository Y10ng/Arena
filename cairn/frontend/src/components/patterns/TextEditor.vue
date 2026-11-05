<template>
  <div class="text-editor">
    <div class="text-editor-toolbar">
      <div class="text-editor-heading">
        <slot name="heading">
          <span class="text-editor-label">{{ label }}</span>
          <span class="text-editor-language">{{ languageLabel }}</span>
        </slot>
      </div>
      <div class="text-editor-actions">
        <template v-if="isMarkdown">
          <button
            class="editor-tool-btn"
            :class="{ active: editorMode === 'source' }"
            type="button"
            title="源码模式"
            aria-label="源码模式"
            :aria-pressed="editorMode === 'source'"
            @click="editorMode = 'source'"
          >
            <Code2 :size="14" aria-hidden="true" />
          </button>
          <button
            class="editor-tool-btn"
            :class="{ active: editorMode === 'preview' }"
            type="button"
            title="预览模式"
            aria-label="预览模式"
            :aria-pressed="editorMode === 'preview'"
            @click="editorMode = 'preview'"
          >
            <Eye :size="14" aria-hidden="true" />
          </button>
          <button
            class="editor-tool-btn"
            :class="{ active: editorMode === 'split' }"
            type="button"
            title="分屏模式"
            aria-label="分屏模式"
            :aria-pressed="editorMode === 'split'"
            @click="editorMode = 'split'"
          >
            <Columns2 :size="14" aria-hidden="true" />
          </button>
        </template>
        <button
          class="editor-tool-btn"
          type="button"
          title="全屏编辑"
          aria-label="全屏编辑"
          @click="isFullscreen = true"
        >
          <Maximize2 :size="14" aria-hidden="true" />
        </button>
      </div>
    </div>
    <div
      class="text-editor-body"
      :class="{ split: editorMode === 'split' }"
      :style="{ height: `${height}px`, minHeight: `${minHeight}px` }"
    >
      <MonacoEditor
        v-if="editorMode !== 'preview'"
        v-model="value"
        :language="resolvedLanguage"
        :disabled="disabled"
        :height="editorPaneHeight"
        :min-height="0"
        :aria-label="label"
      />
      <span
        v-if="editorMode !== 'preview' && !value && placeholder"
        class="editor-placeholder-overlay"
        >{{ placeholder }}</span
      >
      <MarkdownPreview
        v-if="isMarkdown && editorMode !== 'source'"
        class="editor-markdown-pane"
        :content="value"
      />
    </div>
    <div
      class="resize-handle"
      role="separator"
      tabindex="0"
      aria-label="调整编辑器高度"
      aria-orientation="horizontal"
      :aria-valuemin="minHeight"
      :aria-valuenow="height"
      @pointerdown.prevent="startResize"
      @keydown="resizeWithKeyboard"
    />
  </div>

  <Dialog
    :open="isFullscreen"
    :title="fullscreenTitle"
    :description="fullscreenDescription"
    size="fullscreen"
    :close-on-outside="false"
    :show-close="false"
    @update:open="handleFullscreenOpenChange"
    @open-auto-focus="handleFullscreenOpenAutoFocus"
  >
    <template #title>
      <div class="text-editor-heading">
        <slot name="heading">
          <span>{{ label }}</span>
          <span class="text-editor-language">{{ languageLabel }}</span>
        </slot>
      </div>
    </template>

    <template #header-actions>
      <div class="text-editor-actions">
        <template v-if="isMarkdown">
          <button
            class="editor-tool-btn"
            :class="{ active: editorMode === 'source' }"
            type="button"
            title="源码模式"
            aria-label="源码模式"
            :aria-pressed="editorMode === 'source'"
            @click="editorMode = 'source'"
          >
            <Code2 :size="14" aria-hidden="true" />
          </button>
          <button
            class="editor-tool-btn"
            :class="{ active: editorMode === 'preview' }"
            type="button"
            title="预览模式"
            aria-label="预览模式"
            :aria-pressed="editorMode === 'preview'"
            @click="editorMode = 'preview'"
          >
            <Eye :size="14" aria-hidden="true" />
          </button>
          <button
            class="editor-tool-btn"
            :class="{ active: editorMode === 'split' }"
            type="button"
            title="分屏模式"
            aria-label="分屏模式"
            :aria-pressed="editorMode === 'split'"
            @click="editorMode = 'split'"
          >
            <Columns2 :size="14" aria-hidden="true" />
          </button>
        </template>
        <button
          class="editor-tool-btn"
          type="button"
          title="退出全屏"
          aria-label="退出全屏"
          @click="isFullscreen = false"
        >
          <Minimize2 :size="15" aria-hidden="true" />
        </button>
      </div>
    </template>

    <div class="editor-fullscreen-body" :class="{ split: editorMode === 'split' }">
      <MonacoEditor
        v-if="editorMode !== 'preview'"
        v-model="value"
        :language="resolvedLanguage"
        :disabled="disabled"
        height="100%"
        :min-height="0"
        :aria-label="label"
        auto-focus
      />
      <span
        v-if="editorMode !== 'preview' && !value && placeholder"
        class="editor-placeholder-overlay fullscreen"
      >
        {{ placeholder }}
      </span>
      <MarkdownPreview
        v-if="isMarkdown && editorMode !== 'source'"
        class="editor-markdown-pane"
        :content="value"
      />
    </div>
  </Dialog>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, onUnmounted, ref, watch } from 'vue'
import { Code2, Columns2, Eye, Maximize2, Minimize2 } from '@lucide/vue'
import MarkdownPreview from '@/components/MarkdownPreview.vue'
import { Dialog } from '@/components/ui'
import { isMarkdownFile, languageDisplayName, resolveFileLanguage } from '@/lib/fileLanguage'
import { toLogicalPixels } from '@/lib/uiScale'

type EditorMode = 'source' | 'preview' | 'split'

const MonacoEditor = defineAsyncComponent(() => import('@/components/MonacoEditor.vue'))

const props = withDefaults(
  defineProps<{
    modelValue: string
    disabled?: boolean
    minHeight?: number
    label?: string
    placeholder?: string
    fileName?: string
    language?: string
  }>(),
  {
    disabled: false,
    minHeight: 420,
    label: '内容',
    placeholder: '',
    fileName: '',
    language: '',
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const value = computed({
  get: () => props.modelValue,
  set: (next: string) => emit('update:modelValue', next),
})

const isFullscreen = ref(false)
const height = ref(props.minHeight)
const editorMode = ref<EditorMode>('source')
let startY = 0
let startHeight = 0

const resolvedLanguage = computed(() => props.language || resolveFileLanguage(props.fileName))
const languageLabel = computed(() => languageDisplayName(resolvedLanguage.value))
const isMarkdown = computed(
  () => isMarkdownFile(props.fileName) || resolvedLanguage.value === 'markdown',
)
const editorPaneHeight = computed(() => (editorMode.value === 'split' ? '100%' : height.value))
const fullscreenTitle = computed(() => `${props.label}全屏编辑`)
const fullscreenDescription = computed(
  () => `使用 ${languageLabel.value} 查看或编辑${props.label}。`,
)

watch(
  () => props.minHeight,
  (next) => {
    height.value = Math.max(height.value, next)
  },
)

watch(isMarkdown, (markdown) => {
  if (!markdown && editorMode.value !== 'source') {
    editorMode.value = 'source'
  }
})

const handleFullscreenOpenChange = (open: boolean) => {
  isFullscreen.value = open
}

const handleFullscreenOpenAutoFocus = (event: Event) => {
  event.preventDefault()
}

const stopResize = () => {
  window.removeEventListener('pointermove', resize)
  window.removeEventListener('pointerup', stopResize)
  window.removeEventListener('pointercancel', stopResize)
}

const resize = (event: PointerEvent) => {
  const delta = toLogicalPixels(event.clientY - startY)
  height.value = Math.max(props.minHeight, startHeight + delta)
}

const startResize = (event: PointerEvent) => {
  if (event.button !== 0) return
  ;(event.currentTarget as HTMLElement).focus({ preventScroll: true })
  startY = event.clientY
  startHeight = height.value
  window.addEventListener('pointermove', resize)
  window.addEventListener('pointerup', stopResize)
  window.addEventListener('pointercancel', stopResize)
}

function resizeWithKeyboard(event: KeyboardEvent) {
  if (!['ArrowUp', 'ArrowDown', 'Home'].includes(event.key)) return
  event.preventDefault()
  const step = (event.key === 'ArrowDown' ? 1 : -1) * (event.shiftKey ? 80 : 20)
  height.value = event.key === 'Home'
    ? props.minHeight
    : Math.max(props.minHeight, height.value + step)
}

onUnmounted(stopResize)
</script>

<style scoped>
.text-editor {
  min-width: 0;
  display: flex;
  flex-direction: column;
  background-color: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.text-editor-toolbar {
  min-height: var(--control-height-lg);
  padding: var(--space-1) var(--space-2) var(--space-1) var(--space-3);
  gap: var(--space-2);
  flex-wrap: wrap;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border);
  background-color: var(--bg-inset);
}

.text-editor-heading,
.text-editor-actions {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.text-editor-label {
  color: var(--text);
  font-size: var(--text-md);
  font-weight: var(--font-medium);
  overflow-wrap: anywhere;
}

.text-editor-language {
  color: var(--text-faint);
  padding-inline-start: var(--space-2);
  border-inline-start: 1px solid var(--border);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
}

.editor-tool-btn {
  width: var(--control-height-sm);
  height: var(--control-height-sm);
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  background-color: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition:
    background-color var(--dur-fast) var(--ease),
    color var(--dur-fast) var(--ease);
}

.editor-tool-btn:hover {
  color: var(--text);
  background-color: var(--surface-hover);
}

.editor-tool-btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.editor-tool-btn:active {
  background-color: var(--color-surface-pressed);
}

.editor-tool-btn.active {
  color: var(--accent);
  background-color: var(--surface-selected);
}

.text-editor-body,
.editor-fullscreen-body {
  position: relative;
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  background-color: var(--bg-surface);
  overflow: hidden;
}

.text-editor-body.split,
.editor-fullscreen-body.split {
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
}

.editor-markdown-pane {
  border-left: 1px solid var(--border);
}

.editor-placeholder-overlay {
  position: absolute;
  top: var(--space-3);
  left: 58px;
  z-index: 2;
  max-width: calc(100% - 76px);
  color: var(--text-faint);
  font-family: var(--font-mono);
  font-size: var(--text-md);
  line-height: var(--leading-relaxed);
  pointer-events: none;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.editor-placeholder-overlay.fullscreen {
  top: var(--space-4);
}

.resize-handle {
  height: 10px;
  cursor: ns-resize;
  border-top: 1px solid var(--border);
  position: relative;
  background: var(--bg-inset);
  touch-action: none;
}

.resize-handle::after {
  content: '';
  position: absolute;
  top: 4px;
  left: calc(50% - 24px);
  width: 48px;
  height: 2px;
  border-radius: var(--radius-full);
  background: var(--color-control-border);
}

.resize-handle:hover::after,
.resize-handle:focus-visible::after {
  background: var(--accent);
}

.resize-handle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.editor-fullscreen-body {
  height: 100%;
}

@media (max-width: 768px) {
  .text-editor-body.split,
  .editor-fullscreen-body.split {
    grid-template-columns: minmax(0, 1fr);
  }

  .editor-markdown-pane {
    border-left: 0;
    border-top: 1px solid var(--border);
  }
}
</style>
