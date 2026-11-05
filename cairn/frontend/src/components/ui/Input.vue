<template>
  <input
    ref="inputElement"
    v-bind="$attrs"
    class="ui-input"
    :class="{ 'ui-input--invalid': invalid }"
    :type="type"
    :value="modelValue ?? ''"
    :aria-invalid="invalid ? 'true' : undefined"
    @input="onInput"
  />
</template>

<script setup lang="ts">
import { ref } from 'vue'
import type { InputHTMLAttributes } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue: string | number | null
    type?: InputHTMLAttributes['type']
    invalid?: boolean
  }>(),
  {
    type: 'text',
    invalid: false,
  },
)

const emit = defineEmits<{
  (event: 'update:modelValue', value: string | number | null): void
}>()

const inputElement = ref<HTMLInputElement | null>(null)

function onInput(event: Event) {
  const target = event.target as HTMLInputElement
  if (props.type !== 'number') {
    emit('update:modelValue', target.value)
    return
  }

  emit(
    'update:modelValue',
    target.value === '' || Number.isNaN(target.valueAsNumber) ? null : target.valueAsNumber,
  )
}

function focus(options?: FocusOptions) {
  inputElement.value?.focus(options)
}

defineExpose({ focus })
defineOptions({ name: 'UiInput', inheritAttrs: false })
</script>

<style scoped>
.ui-input {
  width: 100%;
  min-width: 0;
  min-height: var(--field-control-height, var(--control-height-form));
  border: calc(var(--space-1) / 4) solid var(--color-control-border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text);
  padding: calc(var(--space-1) * 1.5) var(--space-3);
  font-family: var(--font-sans);
  font-size: var(--field-font-size, var(--text-body));
  line-height: var(--leading-snug);
  transition:
    background-color var(--dur-fast) var(--ease),
    border-color var(--dur-fast) var(--ease),
    box-shadow var(--dur-fast) var(--ease);
}

.ui-input:hover:not(:disabled):not([readonly]) {
  border-color: var(--border-strong);
}

.ui-input:focus {
  border-color: var(--accent);
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.ui-input--invalid,
.ui-input[aria-invalid='true'] {
  border-color: var(--danger);
}

.ui-input--invalid:focus,
.ui-input[aria-invalid='true']:focus {
  border-color: var(--danger);
  outline-color: var(--danger);
}

.ui-input[readonly] {
  background: var(--bg-inset);
  cursor: text;
}

.ui-input:disabled {
  background: var(--surface-disabled);
  color: var(--text-disabled);
}
</style>
