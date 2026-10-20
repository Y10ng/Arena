<template>
  <div class="text-editor">
    <div class="text-editor-toolbar">
      <div class="text-editor-heading">
        <span class="text-editor-label">{{ label }}</span>
        <span class="text-editor-language">{{ languageLabel }}</span>
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
    <div class="resize-handle" title="拖拽调整高度" @pointerdown.prevent="startResize" />
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
        <span>{{ label }}</span>
        <span class="text-editor-language">{{ languageLabel }}</span>
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
}

const resize = (event: PointerEvent) => {
  const delta = event.clientY - startY
  height.value = Math.max(props.minHeight, startHeight + delta)
}

const startResize = (event: PointerEvent) => {
  startY = event.clientY
  startHeight = height.value
  window.addEventListener('pointermove', resize)
  window.addEventListener('pointerup', stopResize)
}

onUnmounted(stopResize)
</script>

<style scoped>
.text-editor {
  min-width: 0;
  display: flex;
  flex-direction: column;
  background-color: var(--bg-inset);
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
  background-color: var(--bg-surface);
}

.text-editor-heading,
.text-editor-actions {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.text-editor-label {
  color: var(--text-muted);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  overflow-wrap: anywhere;
}

.text-editor-language {
  color: var(--text-faint);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 1px 5px;
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
  background-color: var(--bg-inset);
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
  background:
    linear-gradient(90deg, transparent, var(--border-strong), transparent) center / 64px 1px
      no-repeat,
    var(--bg-surface);
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
