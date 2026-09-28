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
  min-height: calc(var(--control-height) * 2);
  border: calc(var(--space-1) / 4) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text);
  padding: var(--space-2) var(--space-3);
  line-height: var(--leading-normal);
  transition:
    background-color var(--dur-fast) var(--ease),
    border-color var(--dur-fast) var(--ease),
    box-shadow var(--dur-fast) var(--ease);
}

.ui-textarea:hover:not(:disabled) {
  border-color: var(--border-strong);
}

.ui-textarea:focus {
  border-color: var(--border-strong);
  box-shadow: var(--ring);
}

.ui-textarea--invalid,
.ui-textarea--invalid:focus {
  border-color: var(--danger);
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
