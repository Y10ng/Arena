<template>
  <TooltipProvider :delay-duration="delay">
    <TooltipRoot :delay-duration="delay" :disabled="disabled">
      <TooltipTrigger as-child>
        <slot />
      </TooltipTrigger>
      <TooltipPortal>
        <TooltipContent class="ui-tooltip" :side="side" :side-offset="sideOffset">
          <slot name="content">{{ content }}</slot>
        </TooltipContent>
      </TooltipPortal>
    </TooltipRoot>
  </TooltipProvider>
</template>

<script setup lang="ts">
import {
  TooltipContent,
  TooltipPortal,
  TooltipProvider,
  TooltipRoot,
  TooltipTrigger,
} from 'reka-ui'
import type { TooltipContentProps } from 'reka-ui'

export type TooltipSide = NonNullable<TooltipContentProps['side']>

withDefaults(
  defineProps<{
    content?: string
    side?: TooltipSide
    delay?: number
    disabled?: boolean
    sideOffset?: number
  }>(),
  {
    content: '',
    side: 'top',
    delay: 400,
    disabled: false,
    sideOffset: 6,
  },
)

defineOptions({ name: 'UiTooltip' })
</script>

<style>
.ui-tooltip {
  max-width: min(320px, calc(100 * var(--ui-vw) - var(--space-4)));
  border: calc(var(--space-1) / 4) solid var(--border);
  border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3);
  z-index: var(--z-overlay-child);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-pop);
  color: var(--text);
  font-family: var(--font-sans);
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
  line-height: var(--leading-snug);
  transform-origin: var(--reka-tooltip-content-transform-origin);
}

.ui-tooltip[data-state='delayed-open'],
.ui-tooltip[data-state='instant-open'] {
  animation: ui-tooltip-enter var(--dur-fast) var(--ease-enter);
}

@keyframes ui-tooltip-enter {
  from {
    opacity: 0;
    transform: translateY(var(--space-1));
  }
}

@media (prefers-reduced-motion: reduce) {
  .ui-tooltip {
    animation: none;
  }
}
</style>
