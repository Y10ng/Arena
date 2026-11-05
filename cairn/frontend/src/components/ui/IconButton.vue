<template>
  <button
    v-bind="$attrs"
    class="ui-icon-button"
    :class="[`ui-icon-button--${variant}`, `ui-icon-button--${size}`]"
    :type="type"
    :disabled="disabled || loading"
    :aria-label="label"
    :aria-busy="loading ? 'true' : undefined"
  >
    <Spinner v-if="loading" :size="spinnerSize" decorative />
    <span v-else class="ui-icon-button__icon" aria-hidden="true">
      <slot />
    </span>
  </button>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { PropType } from 'vue'
import type { ButtonSize, ButtonVariant } from './Button.vue'
import Spinner from './Spinner.vue'

const props = defineProps({
  label: {
    type: String,
    required: true,
    validator: (value: string) => value.trim().length > 0,
  },
  variant: {
    type: String as PropType<ButtonVariant>,
    default: 'ghost',
  },
  size: {
    type: String as PropType<ButtonSize>,
    default: 'md',
  },
  type: {
    type: String as PropType<'button' | 'submit' | 'reset'>,
    default: 'button',
  },
  loading: {
    type: Boolean,
    default: false,
  },
  disabled: {
    type: Boolean,
    default: false,
  },
})

const spinnerSize = computed(() => (props.size === 'lg' ? 'md' : 'sm'))

defineOptions({ name: 'UiIconButton', inheritAttrs: false })
</script>

<style scoped>
.ui-icon-button {
  min-width: 0;
  padding: 0;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  cursor: pointer;
  transition:
    background-color var(--dur-fast) var(--ease),
    border-color var(--dur-fast) var(--ease),
    color var(--dur-fast) var(--ease),
    box-shadow var(--dur-fast) var(--ease);
}

.ui-icon-button--sm {
  width: var(--control-height-sm);
  height: var(--control-height-sm);
}

.ui-icon-button--md {
  width: var(--control-height);
  height: var(--control-height);
}

.ui-icon-button--lg {
  width: var(--control-height-lg);
  height: var(--control-height-lg);
}

.ui-icon-button--primary {
  background: var(--color-primary);
  color: var(--color-primary-contrast);
}

.ui-icon-button--primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.ui-icon-button--secondary {
  border-color: var(--color-control-border);
  background: var(--bg-surface);
  color: var(--text);
}

.ui-icon-button--secondary:hover:not(:disabled),
.ui-icon-button--ghost:hover:not(:disabled) {
  background: var(--surface-hover);
}

.ui-icon-button--success {
  background: var(--success);
  color: var(--color-text-inverse);
}

.ui-icon-button--danger {
  background: var(--danger);
  color: var(--color-text-inverse);
}

.ui-icon-button--warning {
  background: var(--warning);
  color: var(--color-text-inverse);
}

.ui-icon-button--ghost {
  background: transparent;
  color: var(--text-muted);
}

.ui-icon-button--link {
  background: transparent;
  color: var(--link);
}

.ui-icon-button--link:hover:not(:disabled) {
  color: var(--link-hover);
}

.ui-icon-button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* 按压不改尺寸或位移，连续操作时保持目标稳定。 */
.ui-icon-button:active:not(:disabled) {
  background: var(--color-surface-pressed);
}

.ui-icon-button--primary:active:not(:disabled) {
  background: var(--color-primary-pressed);
}

.ui-icon-button--success:hover:not(:disabled),
.ui-icon-button--success:active:not(:disabled) {
  background: color-mix(in srgb, var(--success) 88%, var(--text));
}

.ui-icon-button--danger:hover:not(:disabled),
.ui-icon-button--danger:active:not(:disabled) {
  background: color-mix(in srgb, var(--danger) 88%, var(--text));
}

.ui-icon-button--warning:hover:not(:disabled),
.ui-icon-button--warning:active:not(:disabled) {
  background: color-mix(in srgb, var(--warning) 88%, var(--text));
}

.ui-icon-button:disabled {
  cursor: not-allowed;
  border-color: var(--border);
  background: var(--surface-disabled);
  color: var(--text-disabled);
}

.ui-icon-button__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
</style>
