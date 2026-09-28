<template>
  <span
    v-bind="$attrs"
    class="ui-skeleton"
    :class="`ui-skeleton--${shape}`"
    :role="decorative ? undefined : 'status'"
    :aria-label="decorative ? undefined : label"
    :aria-hidden="decorative ? 'true' : undefined"
  />
</template>

<script setup lang="ts">
export type SkeletonShape = 'text' | 'rect' | 'circle'

withDefaults(
  defineProps<{
    shape?: SkeletonShape
    decorative?: boolean
    label?: string
  }>(),
  {
    shape: 'rect',
    decorative: true,
    label: '加载中',
  },
)

defineOptions({ name: 'UiSkeleton', inheritAttrs: false })
</script>

<style scoped>
.ui-skeleton {
  display: block;
  overflow: hidden;
  position: relative;
  background: var(--surface-disabled);
  color: transparent;
}

.ui-skeleton::after {
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg, transparent, var(--accent-dim), transparent);
  content: '';
  transform: translateX(-100%);
  animation: ui-skeleton-shimmer calc(var(--dur-slow) * 7) var(--ease) infinite;
}

.ui-skeleton--text {
  width: 100%;
  height: var(--text-md);
  border-radius: var(--radius-sm);
}

.ui-skeleton--rect {
  width: 100%;
  min-height: var(--control-height-lg);
  border-radius: var(--radius-md);
}

.ui-skeleton--circle {
  width: var(--control-height);
  height: var(--control-height);
  border-radius: var(--radius-full);
}

@keyframes ui-skeleton-shimmer {
  to {
    transform: translateX(100%);
  }
}

@media (prefers-reduced-motion: reduce) {
  .ui-skeleton::after {
    animation: none;
  }
}
</style>
