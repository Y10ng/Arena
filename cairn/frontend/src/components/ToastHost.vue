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
    >
      <span class="toast-text">{{ toast.text }}</span>
      <button
        class="toast-close"
        type="button"
        aria-label="关闭通知"
        @click="uiStore.removeToast(toast.id)"
      >
        <span aria-hidden="true">&times;</span>
      </button>
    </div>
  </TransitionGroup>
</template>

<script setup lang="ts">
import type { ToastMessage } from '@/stores/ui'
import { useUIStore } from '@/stores/ui'

const uiStore = useUIStore()

const liveRole = (type: ToastMessage['type']): 'alert' | 'status' =>
  type === 'danger' || type === 'warn' ? 'alert' : 'status'

const livePoliteness = (type: ToastMessage['type']): 'assertive' | 'polite' =>
  type === 'danger' || type === 'warn' ? 'assertive' : 'polite'
</script>

<style scoped>
.toast-stack {
  position: fixed;
  inset-block-end: max(var(--space-4), env(safe-area-inset-bottom));
  inset-inline-end: max(var(--space-4), env(safe-area-inset-right));
  z-index: var(--z-toast);
  width: min(var(--overlay-toast-width), calc(100vw - var(--space-8)));
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  pointer-events: none;
}

.toast-card {
  --toast-tone: var(--color-info);
  --toast-tone-soft: var(--color-info-soft);

  width: 100%;
  min-width: 0;
  border: 1px solid var(--border-strong);
  border-inline-start: var(--space-1) solid var(--toast-tone);
  border-radius: var(--radius-md);
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  overflow: hidden;
  pointer-events: auto;
  background:
    linear-gradient(90deg, var(--toast-tone-soft), transparent var(--space-8)), var(--bg-elevated);
  box-shadow: var(--shadow-pop);
  color: var(--text);
}

.toast-card--success {
  --toast-tone: var(--color-success);
  --toast-tone-soft: var(--color-success-soft);
}

.toast-card--warn {
  --toast-tone: var(--color-warning);
  --toast-tone-soft: var(--color-warning-soft);
}

.toast-card--danger {
  --toast-tone: var(--color-danger);
  --toast-tone-soft: var(--color-danger-soft);
}

.toast-text {
  min-width: 0;
  flex: 1 1 auto;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  color: var(--text);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  line-height: var(--leading-normal);
}

.toast-close {
  width: var(--control-height-xs);
  height: var(--control-height-xs);
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
  transform: translateY(var(--space-2));
}

@media (max-width: 640px) {
  .toast-stack {
    inset-inline: max(var(--space-3), env(safe-area-inset-left))
      max(var(--space-3), env(safe-area-inset-right));
    inset-block-end: max(var(--space-3), env(safe-area-inset-bottom));
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
