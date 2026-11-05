<template>
  <span class="ui-badge" :class="[`ui-badge--${normalizedTone}`, `ui-badge--${size}`]">
    <span v-if="dot" class="ui-badge__dot" aria-hidden="true" />
    <slot />
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

defineOptions({ name: 'UiBadge' })

export type BadgeTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger' | 'reviewed'
export type BadgeToneInput = BadgeTone | 'warn'
export type BadgeSize = 'sm' | 'md' | 'lg'

const props = withDefaults(
  defineProps<{
    tone?: BadgeToneInput
    size?: BadgeSize
    dot?: boolean
  }>(),
  {
    tone: 'neutral',
    size: 'md',
    dot: false,
  },
)

const normalizedTone = computed<BadgeTone>(() => (props.tone === 'warn' ? 'warning' : props.tone))
</script>

<style scoped>
.ui-badge {
  width: fit-content;
  max-width: 100%;
  border: calc(var(--space-1) / 4) solid transparent;
  border-radius: var(--radius-sm);
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  color: var(--text-muted);
  background: var(--bg-inset);
  font-variant-numeric: tabular-nums;
  font-weight: var(--font-medium);
  line-height: var(--leading-normal);
  overflow-wrap: anywhere;
}

.ui-badge--sm {
  min-height: 27.5px;
  padding-inline: 6px;
  font-size: var(--text-xs);
}

.ui-badge--md {
  min-height: var(--control-height-xs);
  padding-inline: var(--space-2);
  font-size: var(--text-xs);
}

.ui-badge--lg {
  min-height: var(--control-height-sm);
  padding-inline: var(--space-3);
  font-size: var(--text-sm);
}

.ui-badge--info {
  border-color: var(--info-border);
  background: var(--color-info-soft);
  color: var(--info);
}

.ui-badge--reviewed {
  border-color: color-mix(in srgb, var(--color-reviewed) 32%, transparent);
  background: var(--color-reviewed-soft);
  color: var(--color-reviewed);
}

.ui-badge--success {
  border-color: var(--success-border);
  background: var(--color-success-soft);
  color: var(--success);
}

.ui-badge--warning {
  border-color: var(--warning-border);
  background: var(--color-warning-soft);
  color: var(--warning);
}

.ui-badge--danger {
  border-color: var(--danger-border);
  background: var(--color-danger-soft);
  color: var(--danger);
}

.ui-badge__dot {
  width: 7.5px;
  height: 7.5px;
  border-radius: var(--radius-full);
  flex: 0 0 auto;
  background: currentColor;
}
</style>
