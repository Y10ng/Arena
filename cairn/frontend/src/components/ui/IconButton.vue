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
  border: calc(var(--space-1) / 4) solid transparent;
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
  background: var(--accent);
  color: var(--color-accent-contrast);
}

.ui-icon-button--primary:hover:not(:disabled) {
  background: var(--accent-hover);
}

.ui-icon-button--secondary {
  border-color: var(--border);
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
  box-shadow: var(--ring);
}

.ui-icon-button:disabled {
  cursor: not-allowed;
  background: var(--surface-disabled);
  color: var(--text-disabled);
}

.ui-icon-button__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
</style>
