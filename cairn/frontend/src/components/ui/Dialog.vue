<template>
  <DialogRoot :open="open" :unmount-on-hide="unmountOnHide" @update:open="handleOpenChange">
    <DialogTrigger v-if="$slots.trigger" as-child>
      <slot name="trigger" />
    </DialogTrigger>

    <DialogPortal :disabled="portalDisabled">
      <DialogOverlay class="ui-dialog__overlay" :class="[`ui-dialog__overlay--${size}`, overlayClass]" />
      <DialogContent
        class="ui-dialog__content"
        :class="[`ui-dialog__content--${size}`, contentClass]"
        @open-auto-focus="handleOpenAutoFocus"
        @close-auto-focus="handleCloseAutoFocus"
        @escape-key-down="handleEscapeKeyDown"
        @pointer-down-outside="handlePointerDownOutside"
      >
        <header class="ui-dialog__header">
          <div class="ui-dialog__heading">
            <DialogTitle class="ui-dialog__title">
              <slot name="title">{{ title }}</slot>
            </DialogTitle>
            <DialogDescription class="ui-dialog__description">
              <slot name="description">{{ description }}</slot>
            </DialogDescription>
          </div>

          <div
            v-if="$slots['header-actions'] || (showClose && dismissible)"
            class="ui-dialog__header-actions"
          >
            <slot name="header-actions" />

            <DialogClose v-if="showClose && dismissible" as-child>
              <IconButton class="ui-dialog__close" :label="closeLabel" size="sm">
                <span aria-hidden="true">&times;</span>
              </IconButton>
            </DialogClose>
          </div>
        </header>

        <div class="ui-dialog__body">
          <slot />
        </div>

        <footer v-if="$slots.footer" class="ui-dialog__footer">
          <slot name="footer" />
        </footer>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>

<script setup lang="ts">
import {
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogPortal,
  DialogRoot,
  DialogTitle,
  DialogTrigger,
} from 'reka-ui'
import IconButton from './IconButton.vue'

export type DialogSize = 'sm' | 'md' | 'lg' | 'fullscreen'

const props = withDefaults(
  defineProps<{
    open: boolean
    title: string
    description: string
    size?: DialogSize
    dismissible?: boolean
    closeOnOutside?: boolean
    portalDisabled?: boolean
    showClose?: boolean
    closeLabel?: string
    contentClass?: string
    overlayClass?: string
    unmountOnHide?: boolean
  }>(),
  {
    size: 'md',
    dismissible: true,
    closeOnOutside: true,
    portalDisabled: false,
    showClose: true,
    closeLabel: '关闭',
    unmountOnHide: true,
  },
)

const emit = defineEmits<{
  (event: 'update:open', value: boolean): void
  (event: 'openAutoFocus', value: Event): void
  (event: 'closeAutoFocus', value: Event): void
  (event: 'escapeKeyDown', value: KeyboardEvent): void
}>()

const handleOpenChange = (value: boolean) => {
  if (!value && !props.dismissible) return
  emit('update:open', value)
}

const handleEscapeKeyDown = (event: KeyboardEvent) => {
  if (!props.dismissible) event.preventDefault()
  emit('escapeKeyDown', event)
}

const handlePointerDownOutside = (event: Event) => {
  if (!props.dismissible || !props.closeOnOutside) event.preventDefault()
}

const handleOpenAutoFocus = (event: Event) => emit('openAutoFocus', event)
const handleCloseAutoFocus = (event: Event) => emit('closeAutoFocus', event)

defineOptions({ name: 'UiDialog' })
</script>

<!-- Dialog 通过 Portal 挂载到 body，浮层样式使用组件专属类名全局生效。 -->
<style>
.ui-dialog__overlay {
  position: fixed;
  z-index: var(--z-modal);
  inset: 0;
  background: var(--color-overlay);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
}

.ui-dialog__content {
  position: fixed;
  z-index: var(--z-modal);
  top: 50%;
  left: 50%;
  width: calc(100vw - var(--space-6));
  max-height: calc(100dvh - var(--space-6));
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-elevated);
  box-shadow: var(--shadow-modal);
  color: var(--text);
  transform: translate(-50%, -50%);
}

.ui-dialog__content--sm {
  max-width: var(--overlay-dialog-sm);
}

.ui-dialog__content--md {
  max-width: var(--overlay-dialog-md);
}

.ui-dialog__content--lg {
  max-width: var(--overlay-dialog-lg);
}

.ui-dialog__overlay--fullscreen,
.ui-dialog__content--fullscreen {
  z-index: var(--z-fullscreen);
}

.ui-dialog__content--fullscreen {
  inset: var(--space-5);
  width: auto;
  max-width: none;
  max-height: none;
  transform: none;
}

.ui-dialog__content--fullscreen .ui-dialog__body {
  flex: 1;
  padding: 0;
  overflow: hidden;
}

.ui-dialog__header {
  padding: var(--space-5) var(--space-5) var(--space-3);
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
}

.ui-dialog__heading {
  min-width: 0;
  flex: 1;
}

.ui-dialog__header-actions {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.ui-dialog__title {
  margin: 0;
  color: var(--text);
  font-size: var(--text-xl);
  font-weight: var(--font-semibold);
  line-height: var(--leading-tight);
}

.ui-dialog__description {
  margin: var(--space-2) 0 0;
  color: var(--text-muted);
  font-size: var(--text-md);
  line-height: var(--leading-normal);
}

.ui-dialog__close {
  margin: calc(var(--space-1) * -1);
}

.ui-dialog__body {
  min-height: 0;
  padding: var(--space-3) var(--space-5) var(--space-5);
  overflow-y: auto;
}

.ui-dialog__footer {
  padding: var(--space-3) var(--space-5) var(--space-5);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-2);
}

.ui-dialog__overlay[data-state='open'] {
  animation: ui-dialog-overlay-enter var(--dur) var(--ease-enter);
}

.ui-dialog__content[data-state='open'] {
  animation: ui-dialog-content-enter var(--dur) var(--ease-enter);
}

.ui-dialog__content--fullscreen[data-state='open'] {
  animation-name: ui-dialog-fullscreen-enter;
}

@keyframes ui-dialog-overlay-enter {
  from {
    opacity: 0;
  }
}

@keyframes ui-dialog-content-enter {
  from {
    opacity: 0;
    transform: translate(-50%, calc(-50% + var(--space-2))) scale(0.98);
  }
}

@keyframes ui-dialog-fullscreen-enter {
  from {
    opacity: 0;
    transform: translateY(var(--space-2)) scale(0.99);
  }
}

@media (max-width: 640px) {
  .ui-dialog__content {
    width: calc(100vw - var(--space-4));
    max-height: calc(100dvh - var(--space-4));
  }

  .ui-dialog__content--fullscreen {
    inset: var(--space-3);
    width: auto;
    max-height: none;
  }

  .ui-dialog__header,
  .ui-dialog__body,
  .ui-dialog__footer {
    padding-inline: var(--space-4);
  }
}

@media (prefers-reduced-motion: reduce) {
  .ui-dialog__overlay,
  .ui-dialog__content {
    animation: none;
  }
}
</style>
