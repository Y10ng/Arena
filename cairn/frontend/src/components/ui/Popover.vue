<template>
  <PopoverRoot :open="open" :modal="modal" @update:open="handleOpenChange">
    <PopoverTrigger as-child>
      <slot name="trigger" />
    </PopoverTrigger>

    <PopoverPortal>
      <PopoverContent
        class="ui-popover__content"
        :class="`ui-popover__content--${size}`"
        :side="side"
        :align="align"
        :side-offset="sideOffset"
        :collision-padding="collisionPadding"
        @escape-key-down="handleDismiss"
        @pointer-down-outside="handleDismiss"
        @focus-outside="handleDismiss"
        @open-auto-focus="emit('openAutoFocus', $event)"
        @close-auto-focus="emit('closeAutoFocus', $event)"
      >
        <slot />
      </PopoverContent>
    </PopoverPortal>
  </PopoverRoot>
</template>

<script setup lang="ts">
import { PopoverContent, PopoverPortal, PopoverRoot, PopoverTrigger } from 'reka-ui'
import type { PopoverContentProps } from 'reka-ui'

export type PopoverSide = NonNullable<PopoverContentProps['side']>
export type PopoverAlign = NonNullable<PopoverContentProps['align']>
export type PopoverSize = 'sm' | 'md' | 'lg'

const props = withDefaults(
  defineProps<{
    open?: boolean
    side?: PopoverSide
    align?: PopoverAlign
    sideOffset?: number
    collisionPadding?: number
    size?: PopoverSize
    modal?: boolean
    dismissible?: boolean
  }>(),
  {
    open: undefined,
    side: 'bottom',
    align: 'center',
    sideOffset: 6,
    collisionPadding: 8,
    size: 'md',
    modal: false,
    dismissible: true,
  },
)

const emit = defineEmits<{
  (event: 'update:open', value: boolean): void
  (event: 'openAutoFocus', value: Event): void
  (event: 'closeAutoFocus', value: Event): void
}>()

function handleOpenChange(value: boolean) {
  if (!value && !props.dismissible) return
  emit('update:open', value)
}

function handleDismiss(event: Event) {
  if (!props.dismissible) event.preventDefault()
}

defineOptions({ name: 'UiPopover' })
</script>

<!-- Popover 通过 Portal 挂载到 body，浮层样式使用组件专属类名全局生效。 -->
<style>
.ui-popover__content {
  max-width: calc(100vw - var(--space-4));
  max-height: min(var(--reka-popover-content-available-height), 70dvh);
  border: calc(var(--space-1) / 4) solid var(--border-strong);
  border-radius: var(--radius-lg);
  padding: var(--space-3);
  z-index: var(--z-popover);
  overflow: auto;
  background: var(--bg-elevated);
  box-shadow: var(--shadow-pop);
  color: var(--text);
  transform-origin: var(--reka-popover-content-transform-origin);
}

.ui-popover__content--sm {
  width: min(240px, calc(100vw - var(--space-4)));
}

.ui-popover__content--md {
  width: min(360px, calc(100vw - var(--space-4)));
}

.ui-popover__content--lg {
  width: min(520px, calc(100vw - var(--space-4)));
}

.ui-popover__content[data-state='open'] {
  animation: ui-popover-enter var(--dur-fast) var(--ease-enter);
}

@keyframes ui-popover-enter {
  from {
    opacity: 0;
    transform: scale(0.98);
  }
}

@media (prefers-reduced-motion: reduce) {
  .ui-popover__content {
    animation: none;
  }
}
</style>
