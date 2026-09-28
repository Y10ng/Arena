<template>
  <SelectRoot
    :model-value="resolvedModelValue"
    :open="open"
    :disabled="disabled"
    :required="required"
    :name="name"
    @update:model-value="handleModelValueChange"
    @update:open="emit('update:open', $event)"
  >
    <SelectTrigger
      v-bind="$attrs"
      class="ui-select__trigger"
      :class="{ 'ui-select__trigger--invalid': invalid }"
      :aria-invalid="invalid ? 'true' : undefined"
      :aria-label="ariaLabel || undefined"
    >
      <SelectValue :placeholder="placeholder" />
      <SelectIcon class="ui-select__icon" aria-hidden="true">⌄</SelectIcon>
    </SelectTrigger>

    <SelectPortal>
      <SelectContent class="ui-select__content" position="popper" align="start" :side-offset="4">
        <SelectScrollUpButton class="ui-select__scroll-button" aria-label="向上滚动">
          ↑
        </SelectScrollUpButton>
        <SelectViewport class="ui-select__viewport">
          <SelectItem
            v-for="option in options"
            :key="option.value"
            class="ui-select__item"
            :value="option.value"
            :disabled="option.disabled"
            :text-value="option.label"
          >
            <SelectItemIndicator class="ui-select__indicator" aria-hidden="true">
              ✓
            </SelectItemIndicator>
            <SelectItemText>{{ option.label }}</SelectItemText>
          </SelectItem>
        </SelectViewport>
        <SelectScrollDownButton class="ui-select__scroll-button" aria-label="向下滚动">
          ↓
        </SelectScrollDownButton>
      </SelectContent>
    </SelectPortal>
  </SelectRoot>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  SelectContent,
  SelectIcon,
  SelectItem,
  SelectItemIndicator,
  SelectItemText,
  SelectPortal,
  SelectRoot,
  SelectScrollDownButton,
  SelectScrollUpButton,
  SelectTrigger,
  SelectValue,
  SelectViewport,
} from 'reka-ui'

export interface SelectOption {
  value: string
  label: string
  disabled?: boolean
}

const props = withDefaults(
  defineProps<{
    modelValue: string
    options: readonly SelectOption[]
    open?: boolean
    placeholder?: string
    disabled?: boolean
    required?: boolean
    name?: string
    invalid?: boolean
    ariaLabel?: string
  }>(),
  {
    open: undefined,
    placeholder: '请选择',
    disabled: false,
    required: false,
    name: undefined,
    invalid: false,
    ariaLabel: undefined,
  },
)

const emit = defineEmits<{
  (event: 'update:modelValue', value: string): void
  (event: 'update:open', value: boolean): void
}>()

const resolvedModelValue = computed(() => props.modelValue || undefined)

function handleModelValueChange(value: unknown) {
  if (typeof value === 'string') emit('update:modelValue', value)
}

defineOptions({ name: 'UiSelect', inheritAttrs: false })
</script>

<style scoped>
.ui-select__trigger {
  width: 100%;
  min-height: var(--control-height);
  border: calc(var(--space-1) / 4) solid var(--border);
  border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  overflow: hidden;
  background: var(--bg-surface);
  color: var(--text);
  text-align: left;
  transition:
    background-color var(--dur-fast) var(--ease),
    border-color var(--dur-fast) var(--ease),
    box-shadow var(--dur-fast) var(--ease);
}

.ui-select__trigger:hover:not(:disabled) {
  border-color: var(--border-strong);
}

.ui-select__trigger:focus-visible,
.ui-select__trigger[data-state='open'] {
  border-color: var(--border-strong);
  box-shadow: var(--ring);
}

.ui-select__trigger--invalid {
  border-color: var(--danger);
}

.ui-select__trigger:disabled {
  background: var(--surface-disabled);
  color: var(--text-disabled);
}

.ui-select__trigger :deep([data-placeholder]) {
  color: var(--text-faint);
}

.ui-select__trigger :deep(span:first-child) {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ui-select__icon {
  flex: none;
  color: var(--text-muted);
  font-size: var(--text-sm);
}
</style>

<!-- SelectContent 通过 Portal 挂载到 body，弹层样式不能依赖组件 scoped 属性。 -->
<style>
.ui-select__content {
  min-width: var(--reka-select-trigger-width);
  max-width: min(520px, calc(100vw - var(--space-4)));
  max-height: min(var(--reka-select-content-available-height), 320px);
  border: calc(var(--space-1) / 4) solid var(--border-strong);
  border-radius: var(--radius-md);
  z-index: var(--z-overlay-child);
  overflow: hidden;
  background: var(--bg-elevated);
  box-shadow: var(--shadow-pop);
  color: var(--text);
  transform-origin: var(--reka-select-content-transform-origin);
}

.ui-select__content[data-state='open'] {
  animation: ui-select-enter var(--dur-fast) var(--ease-enter);
}

.ui-select__viewport {
  padding: var(--space-1);
}

.ui-select__item {
  min-height: var(--control-height-sm);
  border-radius: var(--radius-sm);
  padding: var(--space-2) var(--space-3) var(--space-2) calc(var(--space-3) + var(--space-4));
  position: relative;
  display: flex;
  align-items: center;
  outline: none;
  color: var(--text);
  font-size: var(--text-sm);
  line-height: var(--leading-snug);
  cursor: default;
  user-select: none;
}

.ui-select__item[data-highlighted] {
  background: var(--surface-selected);
}

.ui-select__item[data-disabled] {
  color: var(--text-disabled);
  pointer-events: none;
}

.ui-select__indicator {
  width: var(--space-4);
  position: absolute;
  left: var(--space-2);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--accent);
}

.ui-select__scroll-button {
  min-height: var(--control-height-xs);
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-elevated);
  color: var(--text-muted);
}

@keyframes ui-select-enter {
  from {
    opacity: 0;
    transform: scale(0.98);
  }
}

@media (prefers-reduced-motion: reduce) {
  .ui-select__content {
    animation: none;
  }
}
</style>
