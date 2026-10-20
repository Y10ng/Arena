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
  min-height: var(--control-height);
  border: calc(var(--space-1) / 4) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-inset);
  color: var(--text);
  padding: calc(var(--space-1) * 1.5) var(--space-3);
  font-size: var(--text-md);
  line-height: var(--leading-normal);
  transition:
    background-color var(--dur-fast) var(--ease),
    border-color var(--dur-fast) var(--ease),
    box-shadow var(--dur-fast) var(--ease);
}

.ui-input:hover:not(:disabled) {
  border-color: var(--border-strong);
}

.ui-input:focus {
  border-color: var(--accent);
  box-shadow: var(--ring);
}

.ui-input--invalid {
  border-color: var(--danger);
}

.ui-input--invalid:focus {
  border-color: var(--danger);
}

.ui-input:disabled {
  background: var(--surface-disabled);
  color: var(--text-disabled);
}
</style>
