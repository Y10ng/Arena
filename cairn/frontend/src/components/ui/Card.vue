<template>
  <component :is="as" v-bind="$attrs" class="ui-card" :class="`ui-card--padding-${padding}`">
    <slot />
  </component>
</template>

<script setup lang="ts">
import type { Component } from 'vue'

export type CardPadding = 'none' | 'sm' | 'md' | 'lg'

withDefaults(
  defineProps<{
    as?: string | Component
    padding?: CardPadding
  }>(),
  {
    as: 'section',
    padding: 'md',
  },
)

defineOptions({ name: 'UiCard', inheritAttrs: false })
</script>

<style scoped>
.ui-card {
  min-width: 0;
  border: calc(var(--space-1) / 4) solid var(--border);
  border-radius: var(--radius-lg);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  background: var(--bg-surface);
  box-shadow: var(--shadow-xs);
}

.ui-card--padding-none {
  padding: var(--space-0);
}

.ui-card--padding-sm {
  padding: var(--space-3);
}

.ui-card--padding-md {
  padding: var(--space-4);
}

.ui-card--padding-lg {
  padding: var(--space-5);
}
</style>
