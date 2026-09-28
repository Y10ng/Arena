<template>
  <AlertDialogRoot :open="open" @update:open="handleOpenChange">
    <AlertDialogTrigger v-if="$slots.trigger" as-child>
      <slot name="trigger" />
    </AlertDialogTrigger>

    <AlertDialogPortal>
      <AlertDialogOverlay class="ui-alert-dialog__overlay" />
      <AlertDialogContent
        class="ui-alert-dialog__content"
        :class="`ui-alert-dialog__content--${tone}`"
        @escape-key-down="handleEscapeKeyDown"
      >
        <header class="ui-alert-dialog__header">
          <AlertDialogTitle class="ui-alert-dialog__title">
            <slot name="title">{{ title }}</slot>
          </AlertDialogTitle>
          <AlertDialogDescription class="ui-alert-dialog__description">
            <slot name="description">{{ description }}</slot>
          </AlertDialogDescription>
        </header>

        <div v-if="$slots.default" class="ui-alert-dialog__body">
          <slot />
        </div>

        <footer class="ui-alert-dialog__footer">
          <AlertDialogCancel as-child>
            <slot name="cancel">
              <Button variant="secondary">取消</Button>
            </slot>
          </AlertDialogCancel>
          <slot name="action" />
        </footer>
      </AlertDialogContent>
    </AlertDialogPortal>
  </AlertDialogRoot>
</template>

<script setup lang="ts">
import {
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogOverlay,
  AlertDialogPortal,
  AlertDialogRoot,
  AlertDialogTitle,
  AlertDialogTrigger,
} from 'reka-ui'
import Button from './Button.vue'

export type AlertDialogTone = 'neutral' | 'warning' | 'danger'

const props = withDefaults(
  defineProps<{
    open: boolean
    title: string
    description: string
    tone?: AlertDialogTone
    dismissible?: boolean
  }>(),
  {
    tone: 'neutral',
    dismissible: true,
  },
)

const emit = defineEmits<{
  (event: 'update:open', value: boolean): void
}>()

const handleOpenChange = (value: boolean) => {
  if (!value && !props.dismissible) return
  emit('update:open', value)
}

const handleEscapeKeyDown = (event: KeyboardEvent) => {
  if (!props.dismissible) event.preventDefault()
}

defineOptions({ name: 'UiAlertDialog' })
</script>

<style scoped>
.ui-alert-dialog__overlay {
  position: fixed;
  z-index: var(--z-modal);
  inset: 0;
  background: var(--color-overlay);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
}

.ui-alert-dialog__content {
  position: fixed;
  z-index: var(--z-modal);
  top: 50%;
  left: 50%;
  width: calc(100vw - var(--space-6));
  max-width: var(--overlay-dialog-sm);
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

.ui-alert-dialog__content--warning {
  border-color: color-mix(in srgb, var(--warning) 42%, var(--border-strong));
}

.ui-alert-dialog__content--danger {
  border-color: color-mix(in srgb, var(--danger) 42%, var(--border-strong));
}

.ui-alert-dialog__header {
  padding: var(--space-5) var(--space-5) var(--space-3);
}

.ui-alert-dialog__title {
  margin: 0;
  color: var(--text);
  font-size: var(--text-xl);
  font-weight: var(--font-semibold);
  line-height: var(--leading-tight);
}

.ui-alert-dialog__content--warning .ui-alert-dialog__title {
  color: var(--warning);
}

.ui-alert-dialog__content--danger .ui-alert-dialog__title {
  color: var(--danger);
}

.ui-alert-dialog__description {
  margin: var(--space-2) 0 0;
  color: var(--text-muted);
  font-size: var(--text-md);
  line-height: var(--leading-normal);
}

.ui-alert-dialog__body {
  min-height: 0;
  padding: var(--space-3) var(--space-5);
  overflow-y: auto;
}

.ui-alert-dialog__footer {
  padding: var(--space-3) var(--space-5) var(--space-5);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-2);
}

.ui-alert-dialog__overlay[data-state='open'] {
  animation: ui-alert-dialog-overlay-enter var(--dur) var(--ease-enter);
}

.ui-alert-dialog__content[data-state='open'] {
  animation: ui-alert-dialog-content-enter var(--dur) var(--ease-enter);
}

@keyframes ui-alert-dialog-overlay-enter {
  from {
    opacity: 0;
  }
}

@keyframes ui-alert-dialog-content-enter {
  from {
    opacity: 0;
    transform: translate(-50%, calc(-50% + var(--space-2))) scale(0.98);
  }
}

@media (max-width: 640px) {
  .ui-alert-dialog__content {
    width: calc(100vw - var(--space-4));
    max-height: calc(100dvh - var(--space-4));
  }

  .ui-alert-dialog__header,
  .ui-alert-dialog__body,
  .ui-alert-dialog__footer {
    padding-inline: var(--space-4);
  }
}

@media (prefers-reduced-motion: reduce) {
  .ui-alert-dialog__overlay,
  .ui-alert-dialog__content {
    animation: none;
  }
}
</style>
