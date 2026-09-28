<template>
  <label
    class="ui-checkbox"
    :class="[$attrs.class, { 'ui-checkbox--disabled': disabled }]"
    :style="$attrs.style"
  >
    <input
      ref="inputElement"
      v-bind="inputAttrs"
      class="ui-checkbox__control"
      type="checkbox"
      :checked="modelValue"
      :disabled="disabled"
      :required="required"
      :aria-checked="indeterminate ? 'mixed' : undefined"
      @change="onChange"
    />
    <span v-if="label" class="ui-checkbox__label">{{ label }}</span>
  </label>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, useAttrs, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue?: boolean
    label?: string
    disabled?: boolean
    required?: boolean
    indeterminate?: boolean
  }>(),
  {
    modelValue: false,
    label: undefined,
    disabled: false,
    required: false,
    indeterminate: false,
  },
)

const emit = defineEmits<{
  (event: 'update:modelValue', value: boolean): void
}>()

const attrs = useAttrs()
const inputAttrs = computed(() =>
  Object.fromEntries(Object.entries(attrs).filter(([key]) => key !== 'class' && key !== 'style')),
)
const inputElement = ref<HTMLInputElement | null>(null)

function syncIndeterminate() {
  if (inputElement.value) inputElement.value.indeterminate = props.indeterminate
}

onMounted(syncIndeterminate)
watch(() => props.indeterminate, syncIndeterminate, { flush: 'post' })

function onChange(event: Event) {
  emit('update:modelValue', (event.target as HTMLInputElement).checked)
  // 原生点击会清除 indeterminate；受控 prop 未变化时立即恢复，避免视觉状态与 ARIA 分裂。
  syncIndeterminate()
}

function focus(options?: FocusOptions) {
  inputElement.value?.focus(options)
}

defineExpose({ focus })
defineOptions({ name: 'UiCheckbox', inheritAttrs: false })
</script>

<style scoped>
.ui-checkbox {
  width: fit-content;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--text);
  cursor: pointer;
  font-size: var(--text-sm);
}

.ui-checkbox__control {
  width: 1rem;
  height: 1rem;
  flex: 0 0 auto;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  accent-color: var(--accent);
  cursor: inherit;
}

.ui-checkbox__control:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.ui-checkbox--disabled {
  color: var(--text-disabled);
  cursor: not-allowed;
}
</style>
