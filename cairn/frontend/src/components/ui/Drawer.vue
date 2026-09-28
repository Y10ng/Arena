<template>
  <DrawerRoot
    :open="open"
    :modal="modal"
    :swipe-direction="swipeDirection"
    @update:open="handleOpenChange"
    @update:open-complete="emit('openComplete', $event)"
  >
    <DrawerTrigger v-if="$slots.trigger" as-child>
      <slot name="trigger" />
    </DrawerTrigger>

    <DrawerPortal :disabled="portalDisabled">
      <DrawerOverlay class="ui-drawer__overlay" />
      <DrawerContent
        class="ui-drawer__content"
        :class="[`ui-drawer__content--${side}`, `ui-drawer__content--${size}`]"
        :initial-focus="initialFocus"
        :final-focus="finalFocus"
        @escape-key-down="handleEscapeKeyDown"
        @pointer-down-outside="handleDismissEvent"
      >
        <header class="ui-drawer__header">
          <div class="ui-drawer__heading">
            <DrawerTitle class="ui-drawer__title">
              <slot name="title">{{ title }}</slot>
            </DrawerTitle>
            <DrawerDescription class="ui-drawer__description">
              <slot name="description">{{ description }}</slot>
            </DrawerDescription>
          </div>

          <DrawerClose v-if="showClose && dismissible" as-child>
            <IconButton class="ui-drawer__close" :label="closeLabel" size="sm">
              <span aria-hidden="true">&times;</span>
            </IconButton>
          </DrawerClose>
        </header>

        <div class="ui-drawer__body">
          <slot />
        </div>

        <footer v-if="$slots.footer" class="ui-drawer__footer">
          <slot name="footer" />
        </footer>
      </DrawerContent>
    </DrawerPortal>
  </DrawerRoot>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  DrawerClose,
  DrawerContent,
  DrawerDescription,
  DrawerOverlay,
  DrawerPortal,
  DrawerRoot,
  DrawerTitle,
  DrawerTrigger,
} from 'reka-ui'
import type { DrawerModal, DrawerOpenChangeDetails } from 'reka-ui'
import IconButton from './IconButton.vue'

export type { DrawerModal, DrawerOpenChangeDetails } from 'reka-ui'
export type DrawerSide = 'left' | 'right' | 'top' | 'bottom'
export type DrawerSize = 'sm' | 'md' | 'lg'

const props = withDefaults(
  defineProps<{
    open: boolean
    title: string
    description: string
    side?: DrawerSide
    size?: DrawerSize
    modal?: DrawerModal
    dismissible?: boolean
    portalDisabled?: boolean
    showClose?: boolean
    closeLabel?: string
    initialFocus?: boolean | HTMLElement | null
    finalFocus?: boolean | HTMLElement | null
  }>(),
  {
    side: 'right',
    size: 'md',
    modal: true,
    dismissible: true,
    portalDisabled: false,
    showClose: true,
    closeLabel: '关闭',
    initialFocus: true,
    finalFocus: true,
  },
)

const emit = defineEmits<{
  (event: 'update:open', value: boolean, details?: DrawerOpenChangeDetails): void
  (event: 'openComplete', value: boolean): void
  (event: 'escapeKeyDown', value: KeyboardEvent): void
}>()

const swipeDirection = computed(() => {
  const directions: Record<DrawerSide, 'up' | 'down' | 'left' | 'right'> = {
    left: 'left',
    right: 'right',
    top: 'up',
    bottom: 'down',
  }
  return directions[props.side]
})

const handleOpenChange = (value: boolean, details?: DrawerOpenChangeDetails) => {
  if (!value && !props.dismissible) return
  emit('update:open', value, details)
}

const handleDismissEvent = (event: Event) => {
  if (!props.dismissible) event.preventDefault()
}

const handleEscapeKeyDown = (event: KeyboardEvent) => {
  handleDismissEvent(event)
  emit('escapeKeyDown', event)
}

defineOptions({ name: 'UiDrawer' })
</script>

<!-- Drawer 通过 Portal 挂载到 body，浮层样式使用组件专属类名全局生效。 -->
<style>
.ui-drawer__overlay {
  position: fixed;
  z-index: var(--z-modal);
  inset: 0;
  background: var(--color-overlay);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
}

.ui-drawer__content {
  position: fixed;
  z-index: var(--z-modal);
  border: 1px solid var(--border-strong);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-elevated);
  box-shadow: var(--shadow-modal);
  color: var(--text);
}

.ui-drawer__content--left,
.ui-drawer__content--right {
  top: 0;
  bottom: 0;
  width: calc(100vw - var(--space-5));
  max-height: 100dvh;
}

.ui-drawer__content--left {
  left: 0;
  border-radius: 0 var(--radius-lg) var(--radius-lg) 0;
}

.ui-drawer__content--right {
  right: 0;
  border-radius: var(--radius-lg) 0 0 var(--radius-lg);
}

.ui-drawer__content--left.ui-drawer__content--sm,
.ui-drawer__content--right.ui-drawer__content--sm {
  max-width: var(--overlay-drawer-sm);
}

.ui-drawer__content--left.ui-drawer__content--md,
.ui-drawer__content--right.ui-drawer__content--md {
  max-width: var(--overlay-drawer-md);
}

.ui-drawer__content--left.ui-drawer__content--lg,
.ui-drawer__content--right.ui-drawer__content--lg {
  max-width: var(--overlay-drawer-lg);
}

.ui-drawer__content--top,
.ui-drawer__content--bottom {
  right: 0;
  left: 0;
  width: 100vw;
  max-height: calc(100dvh - var(--space-5));
}

.ui-drawer__content--top {
  top: 0;
  border-radius: 0 0 var(--radius-lg) var(--radius-lg);
}

.ui-drawer__content--bottom {
  bottom: 0;
  border-radius: var(--radius-lg) var(--radius-lg) 0 0;
}

.ui-drawer__content--top.ui-drawer__content--sm,
.ui-drawer__content--bottom.ui-drawer__content--sm {
  max-height: var(--overlay-drawer-sm);
}

.ui-drawer__content--top.ui-drawer__content--md,
.ui-drawer__content--bottom.ui-drawer__content--md {
  max-height: var(--overlay-drawer-md);
}

.ui-drawer__content--top.ui-drawer__content--lg,
.ui-drawer__content--bottom.ui-drawer__content--lg {
  max-height: var(--overlay-drawer-lg);
}

.ui-drawer__header {
  padding: var(--space-5) var(--space-5) var(--space-3);
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
}

.ui-drawer__heading {
  min-width: 0;
}

.ui-drawer__title {
  margin: 0;
  color: var(--text);
  font-size: var(--text-xl);
  font-weight: var(--font-semibold);
  line-height: var(--leading-tight);
}

.ui-drawer__description {
  margin: var(--space-2) 0 0;
  color: var(--text-muted);
  font-size: var(--text-md);
  line-height: var(--leading-normal);
}

.ui-drawer__close {
  margin: calc(var(--space-1) * -1);
}

.ui-drawer__body {
  min-height: 0;
  flex: 1;
  padding: var(--space-3) var(--space-5) var(--space-5);
  overflow-y: auto;
}

.ui-drawer__footer {
  padding: var(--space-3) var(--space-5) var(--space-5);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-2);
}

.ui-drawer__overlay[data-state='open'] {
  animation: ui-drawer-overlay-enter var(--dur) var(--ease-enter);
}

.ui-drawer__content[data-state='open'] {
  animation-duration: var(--dur-slow);
  animation-timing-function: var(--ease-enter);
}

.ui-drawer__content--left[data-state='open'] {
  animation-name: ui-drawer-left-enter;
}

.ui-drawer__content--right[data-state='open'] {
  animation-name: ui-drawer-right-enter;
}

.ui-drawer__content--top[data-state='open'] {
  animation-name: ui-drawer-top-enter;
}

.ui-drawer__content--bottom[data-state='open'] {
  animation-name: ui-drawer-bottom-enter;
}

@keyframes ui-drawer-overlay-enter {
  from {
    opacity: 0;
  }
}

@keyframes ui-drawer-left-enter {
  from {
    transform: translateX(-100%);
  }
}

@keyframes ui-drawer-right-enter {
  from {
    transform: translateX(100%);
  }
}

@keyframes ui-drawer-top-enter {
  from {
    transform: translateY(-100%);
  }
}

@keyframes ui-drawer-bottom-enter {
  from {
    transform: translateY(100%);
  }
}

@media (max-width: 640px) {
  .ui-drawer__content--left,
  .ui-drawer__content--right {
    width: calc(100vw - var(--space-4));
  }

  .ui-drawer__header,
  .ui-drawer__body,
  .ui-drawer__footer {
    padding-inline: var(--space-4);
  }
}

@media (prefers-reduced-motion: reduce) {
  .ui-drawer__overlay,
  .ui-drawer__content {
    animation: none;
  }
}
</style>
