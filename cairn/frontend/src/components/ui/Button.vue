<template>
  <button
    v-bind="$attrs"
    class="ui-button"
    :class="[
      `ui-button--${variant}`,
      `ui-button--${size}`,
      { 'ui-button--block': block, 'ui-button--loading': loading },
    ]"
    :type="type"
    :disabled="disabled || loading"
    :aria-busy="loading ? 'true' : undefined"
  >
    <Spinner v-if="loading" class="ui-button__spinner" size="sm" decorative />
    <span v-if="$slots.icon" class="ui-button__icon" aria-hidden="true">
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
  position: relative;
  min-width: 0;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  cursor: pointer;
  font-family: var(--font-sans);
  font-weight: var(--font-medium);
  line-height: var(--leading-tight);
  text-decoration: none;
  transition:
    background-color var(--dur-fast) var(--ease),
    border-color var(--dur-fast) var(--ease),
    color var(--dur-fast) var(--ease),
    box-shadow var(--dur-fast) var(--ease),
    transform var(--dur-fast) var(--ease);
}

.ui-button--sm {
  min-height: var(--button-control-height, var(--control-height-sm));
  padding-inline: var(--space-2);
  font-size: var(--text-body);
}

.ui-button--md {
  min-height: var(--button-control-height, var(--control-height));
  padding-inline: var(--space-3);
  font-size: var(--text-control);
}

.ui-button--lg {
  min-height: var(--button-control-height, var(--control-height-lg));
  padding-inline: var(--space-4);
  font-size: var(--text-control);
}

.ui-button--block {
  width: 100%;
}

.ui-button--primary {
  background: linear-gradient(135deg, var(--color-primary), var(--color-primary-end));
  color: var(--color-primary-contrast);
  box-shadow: inset 0 1px 0 rgb(255 255 255 / 20%), 0 2px 5px color-mix(in srgb, var(--color-primary) 18%, transparent);
}

.ui-button--primary:hover:not(:disabled) {
  background: linear-gradient(135deg, var(--color-primary-hover), var(--color-primary-end));
  box-shadow: inset 0 1px 0 rgb(255 255 255 / 28%), 0 4px 12px color-mix(in srgb, var(--color-primary) 26%, transparent);
}

.ui-button--secondary {
  border-color: var(--color-control-border);
  background: var(--bg-elevated);
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

/* 轻微按压只用于主动作，不改变控件布局。 */
.ui-button:active:not(:disabled) {
  background: var(--color-surface-pressed);
}

.ui-button--primary:active:not(:disabled) {
  background: var(--color-primary-pressed);
  transform: translateY(1px);
  box-shadow: none;
}

.ui-button--success:hover:not(:disabled),
.ui-button--success:active:not(:disabled) {
  background: color-mix(in srgb, var(--success) 88%, var(--text));
}

.ui-button--danger:hover:not(:disabled),
.ui-button--danger:active:not(:disabled) {
  background: color-mix(in srgb, var(--danger) 88%, var(--text));
}

.ui-button--warning:hover:not(:disabled),
.ui-button--warning:active:not(:disabled) {
  background: color-mix(in srgb, var(--warning) 88%, var(--text));
}

.ui-button:disabled {
  cursor: not-allowed;
  border-color: var(--border);
  background: var(--surface-disabled);
  color: var(--text-disabled);
  box-shadow: none;
}

.ui-button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.ui-button__spinner {
  position: absolute;
  inset: 0;
  margin: auto;
}

/* 保留原内容占位及可访问名称，加载不增宽、不重复触发。 */
.ui-button--loading .ui-button__content,
.ui-button--loading .ui-button__icon {
  opacity: 0;
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
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
}
.ui-button :deep(svg) {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
}
@media (prefers-reduced-motion: reduce) {
  .ui-button { transition: none; }
  .ui-button--primary:active:not(:disabled) { transform: none; }
}
</style>
