<template>
  <TransitionGroup tag="div" name="toast" class="toast-stack" role="region" aria-label="通知">
    <div
      v-for="toast in uiStore.toasts"
      :key="toast.id"
      class="toast-card"
      :class="`toast-card--${toast.type}`"
      :role="liveRole(toast.type)"
      :aria-live="livePoliteness(toast.type)"
      aria-atomic="true"
      @mouseenter="uiStore.pauseToast(toast.id, 'hover')"
      @mouseleave="uiStore.resumeToast(toast.id, 'hover')"
      @focusin="uiStore.pauseToast(toast.id, 'focus')"
      @focusout="resumeAfterFocus(toast.id, $event)"
    >
      <span class="toast-icon"><component :is="toastIcon(toast.type)" :size="18" aria-hidden="true" /></span>
      <span class="toast-text">{{ toast.text }}</span>
      <button
        class="toast-close"
        type="button"
        aria-label="关闭通知"
        @click="uiStore.removeToast(toast.id)"
      >
        <X :size="16" aria-hidden="true" />
      </button>
    </div>
  </TransitionGroup>
</template>

<script setup lang="ts">
import { onBeforeUnmount } from 'vue'
import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from '@lucide/vue'
import type { ToastMessage } from '@/stores/ui'
import { useUIStore } from '@/stores/ui'

const uiStore = useUIStore()

function resumeAfterFocus(id: string, event: FocusEvent) {
  if (event.relatedTarget instanceof Node && (event.currentTarget as HTMLElement).contains(event.relatedTarget)) return
  uiStore.resumeToast(id, 'focus')
}

onBeforeUnmount(() => {
  // 宿主卸载不应让悬停／焦点暂停永久留在全局 store。
  for (const toast of uiStore.toasts) {
    uiStore.resumeToast(toast.id, 'hover')
    uiStore.resumeToast(toast.id, 'focus')
  }
})

const toastIcon = (type: ToastMessage['type']) => {
  if (type === 'danger') return CircleAlert
  if (type === 'warn') return TriangleAlert
  if (type === 'success') return CircleCheck
  return Info
}

const liveRole = (type: ToastMessage['type']): 'alert' | 'status' =>
  type === 'danger' || type === 'warn' ? 'alert' : 'status'

const livePoliteness = (type: ToastMessage['type']): 'assertive' | 'polite' =>
  type === 'danger' || type === 'warn' ? 'assertive' : 'polite'
</script>

<style scoped>
.toast-stack {
  position: fixed;
  inset-block-start: max(calc(var(--layout-topbar-height) + var(--space-3)), env(safe-area-inset-top));
  inset-inline-end: max(var(--space-4), env(safe-area-inset-right));
  z-index: var(--z-toast);
  width: min(var(--overlay-toast-width), calc(100 * var(--ui-vw) - var(--space-8)));
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: calc(100 * var(--ui-dvh) - var(--layout-topbar-height) - var(--space-6));
  overflow-y: auto;
  pointer-events: none;
}

.toast-card {
  --toast-tone: var(--color-info);

  width: 100%;
  min-width: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  max-height: min(calc(40 * var(--ui-dvh)), 360px);
  overflow-y: auto;
  overscroll-behavior: contain;
  pointer-events: auto;
  background: var(--bg-elevated);
  box-shadow: var(--shadow-sm);
  color: var(--text);
}

.toast-card--success {
  --toast-tone: var(--color-success);
}

.toast-card--warn {
  --toast-tone: var(--color-warning);
}

.toast-card--danger {
  --toast-tone: var(--color-danger);
}

.toast-icon {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: var(--control-height-sm);
  height: var(--control-height-sm);
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--toast-tone) 10%, transparent);
  color: var(--toast-tone);
}

.toast-text {
  min-width: 0;
  flex: 1 1 auto;
  align-self: center;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  color: var(--text);
  font-size: var(--text-md);
  font-weight: var(--font-normal);
  line-height: var(--leading-normal);
}

.toast-close {
  width: var(--control-height-sm);
  height: var(--control-height-sm);
  flex: 0 0 auto;
  border-radius: var(--radius-sm);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-0);
  cursor: pointer;
  background: transparent;
  color: var(--text-faint);
  font-size: var(--text-xl);
  line-height: var(--leading-tight);
  transition:
    background-color var(--dur-fast) var(--ease),
    color var(--dur-fast) var(--ease);
}

.toast-close:hover {
  background: var(--surface-hover);
  color: var(--text);
}

.toast-enter-active {
  transition:
    opacity var(--dur-slow) var(--ease-enter),
    transform var(--dur-slow) var(--ease-enter);
}

.toast-leave-active {
  transition:
    opacity var(--dur) var(--ease-exit),
    transform var(--dur) var(--ease-exit);
}

.toast-move {
  transition: transform var(--dur) var(--ease);
}

.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(calc(-1 * var(--space-1)));
}

@media (max-width: 640px) {
  .toast-stack {
    inset-inline: max(var(--space-3), env(safe-area-inset-left))
      max(var(--space-3), env(safe-area-inset-right));
    width: auto;
  }

  .toast-card {
    padding: var(--space-3);
  }
}

@media (prefers-reduced-motion: reduce) {
  .toast-enter-active,
  .toast-leave-active,
  .toast-move {
    transition: none;
  }

  .toast-enter-from,
  .toast-leave-to {
    transform: none;
  }
}
</style>
