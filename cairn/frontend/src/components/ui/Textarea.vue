<template>
  <textarea
    ref="textareaElement"
    v-bind="$attrs"
    class="ui-textarea"
    :class="[`ui-textarea--resize-${resize}`, { 'ui-textarea--invalid': invalid }]"
    :value="modelValue"
    :rows="rows"
    :aria-invalid="invalid ? 'true' : undefined"
    @input="onInput"
  />
</template>

<script setup lang="ts">
import { ref } from 'vue'

export type TextareaResize = 'none' | 'vertical' | 'horizontal' | 'both'

withDefaults(
  defineProps<{
    modelValue: string
    invalid?: boolean
    resize?: TextareaResize
    rows?: number
  }>(),
  {
    invalid: false,
    resize: 'vertical',
    rows: 4,
  },
)

const emit = defineEmits<{
  (event: 'update:modelValue', value: string): void
}>()

const textareaElement = ref<HTMLTextAreaElement | null>(null)

function onInput(event: Event) {
  emit('update:modelValue', (event.target as HTMLTextAreaElement).value)
}

function focus(options?: FocusOptions) {
  textareaElement.value?.focus(options)
}

defineExpose({ focus })
defineOptions({ name: 'UiTextarea', inheritAttrs: false })
</script>

<style scoped>
.ui-textarea {
  width: 100%;
  min-width: 0;
  min-height: 120px;
  border: calc(var(--space-1) / 4) solid var(--color-control-border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text);
  padding: var(--space-2) var(--space-3);
  font-family: var(--font-sans);
  font-size: var(--text-md);
  line-height: var(--leading-relaxed);
  transition:
    background-color var(--dur-fast) var(--ease),
    border-color var(--dur-fast) var(--ease),
    box-shadow var(--dur-fast) var(--ease);
}

.ui-textarea:hover:not(:disabled):not([readonly]) {
  border-color: var(--border-strong);
}

.ui-textarea:focus {
  border-color: var(--accent);
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.ui-textarea--invalid,
.ui-textarea--invalid:focus,
.ui-textarea[aria-invalid='true'] {
  border-color: var(--danger);
  outline-color: var(--danger);
}

.ui-textarea[readonly] {
  background: var(--bg-inset);
  cursor: text;
}

.ui-textarea:disabled {
  background: var(--surface-disabled);
  color: var(--text-disabled);
}

.ui-textarea--resize-none {
  resize: none;
}

.ui-textarea--resize-vertical {
  resize: vertical;
}

.ui-textarea--resize-horizontal {
  resize: horizontal;
}

.ui-textarea--resize-both {
  resize: both;
}
</style>
