<template>
  <button
    v-bind="$attrs"
    class="ui-button"
    :class="[`ui-button--${variant}`, `ui-button--${size}`, { 'ui-button--block': block }]"
    :type="type"
    :disabled="disabled || loading"
    :aria-busy="loading ? 'true' : undefined"
  >
    <Spinner v-if="loading" class="ui-button__spinner" size="sm" decorative />
    <span v-else-if="$slots.icon" class="ui-button__icon" aria-hidden="true">
      <slot name="icon" />
    </span>
    <span class="ui-button__content"><slot /></span>
  </button>
</template>

<script setup lang="ts">
import Spinner from './Spinner.vue'

export type ButtonVariant =
  'primary' | 'secondary' | 'success' | 'danger' | 'warning' | 'ghost' | 'link'
export type ButtonSize = 'sm' | 'md' | 'lg'

withDefaults(
  defineProps<{
    variant?: ButtonVariant
    size?: ButtonSize
    type?: 'button' | 'submit' | 'reset'
    loading?: boolean
    block?: boolean
    disabled?: boolean
  }>(),
  {
    variant: 'primary',
    size: 'md',
    type: 'button',
    loading: false,
    block: false,
    disabled: false,
  },
)

defineOptions({ name: 'UiButton', inheritAttrs: false })
</script>

<style scoped>
.ui-button {
  min-width: 0;
  border: calc(var(--space-1) / 4) solid transparent;
  border-radius: var(--radius-md);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  cursor: pointer;
  font-weight: var(--font-semibold);
  line-height: var(--leading-tight);
  text-decoration: none;
  transition:
    background-color var(--dur-fast) var(--ease),
    border-color var(--dur-fast) var(--ease),
    color var(--dur-fast) var(--ease),
    box-shadow var(--dur-fast) var(--ease);
}

.ui-button--sm {
  min-height: var(--control-height-sm);
  padding-inline: var(--space-2);
  font-size: var(--text-sm);
}

.ui-button--md {
  min-height: var(--control-height);
  padding-inline: var(--space-3);
  font-size: var(--text-md);
}

.ui-button--lg {
  min-height: var(--control-height-lg);
  padding-inline: var(--space-4);
  font-size: var(--text-lg);
}

.ui-button--block {
  width: 100%;
}

.ui-button--primary {
  background: var(--accent);
  color: var(--color-accent-contrast);
}

.ui-button--primary:hover:not(:disabled) {
  background: var(--accent-hover);
}

.ui-button--secondary {
  border-color: var(--border);
  background: var(--bg-surface);
  box-shadow: var(--shadow-xs);
  color: var(--text);
}

.ui-button--secondary:hover:not(:disabled) {
  border-color: var(--border-strong);
  background: var(--surface-hover);
}

.ui-button--ghost:hover:not(:disabled) {
  background: var(--surface-hover);
}

.ui-button--success {
  background: var(--success);
  color: var(--color-text-inverse);
}

.ui-button--danger {
  background: var(--danger);
  color: var(--color-text-inverse);
}

.ui-button--warning {
  background: var(--warning);
  color: var(--color-text-inverse);
}

.ui-button--ghost {
  background: transparent;
  color: var(--text-muted);
}

.ui-button--link {
  min-height: auto;
  padding: var(--space-0);
  background: transparent;
  color: var(--link);
  text-decoration: underline;
  text-underline-offset: var(--space-1);
}

.ui-button--link:hover:not(:disabled) {
  color: var(--link-hover);
}

.ui-button:disabled {
  cursor: not-allowed;
  background: var(--surface-disabled);
  color: var(--text-disabled);
}

.ui-button__icon,
.ui-button__spinner {
  flex: 0 0 auto;
}

.ui-button__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.ui-button__content {
  min-width: 0;
}
</style>
