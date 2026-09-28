<template>
  <span
    v-bind="$attrs"
    class="ui-spinner"
    :class="`ui-spinner--${size}`"
    :role="decorative ? undefined : 'status'"
    :aria-label="decorative ? undefined : label"
    :aria-hidden="decorative ? 'true' : undefined"
  />
</template>

<script setup lang="ts">
export type SpinnerSize = 'sm' | 'md' | 'lg'

withDefaults(
  defineProps<{
    size?: SpinnerSize
    label?: string
    decorative?: boolean
  }>(),
  {
    size: 'md',
    label: '加载中',
    decorative: false,
  },
)

defineOptions({ name: 'UiSpinner', inheritAttrs: false })
</script>

<style scoped>
.ui-spinner {
  border: calc(var(--space-1) / 2) solid currentColor;
  border-right-color: transparent;
  border-radius: var(--radius-full);
  display: inline-block;
  flex: 0 0 auto;
  animation: ui-spinner-rotate calc(var(--dur-slow) * 4) linear infinite;
}

.ui-spinner--sm {
  width: var(--space-4);
  height: var(--space-4);
}

.ui-spinner--md {
  width: var(--space-5);
  height: var(--space-5);
}

.ui-spinner--lg {
  width: var(--space-6);
  height: var(--space-6);
}

@keyframes ui-spinner-rotate {
  to {
    transform: rotate(360deg);
  }
}
</style>
