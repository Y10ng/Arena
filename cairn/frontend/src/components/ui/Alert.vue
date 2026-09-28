<template>
  <section class="ui-alert" :class="`ui-alert--${tone}`" :role="defaultRole">
    <div v-if="$slots.icon" class="ui-alert__icon" aria-hidden="true">
      <slot name="icon" />
    </div>
    <div class="ui-alert__body">
      <div v-if="title || $slots.title" class="ui-alert__title">
        <slot name="title">{{ title }}</slot>
      </div>
      <div class="ui-alert__content">
        <slot />
      </div>
      <div v-if="$slots.actions" class="ui-alert__actions">
        <slot name="actions" />
      </div>
    </div>
    <button
      v-if="dismissible"
      class="ui-alert__dismiss"
      type="button"
      aria-label="关闭提示"
      @click="emit('dismiss')"
    >
      ×
    </button>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'

defineOptions({ name: 'UiAlert' })

export type AlertTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger'

const props = withDefaults(
  defineProps<{
    tone?: AlertTone
    title?: string
    dismissible?: boolean
  }>(),
  {
    tone: 'info',
    title: '',
    dismissible: false,
  },
)

const emit = defineEmits<{
  (event: 'dismiss'): void
}>()

const defaultRole = computed(() =>
  props.tone === 'danger' || props.tone === 'warning' ? 'alert' : 'status',
)
</script>

<style scoped>
.ui-alert {
  border: calc(var(--space-1) / 4) solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-3);
  background: var(--bg-inset);
  color: var(--text-muted);
}

.ui-alert--info {
  border-color: var(--info);
  background: var(--color-info-soft);
  color: var(--info);
}

.ui-alert--success {
  border-color: var(--success);
  background: var(--color-success-soft);
  color: var(--success);
}

.ui-alert--warning {
  border-color: var(--warning);
  background: var(--color-warning-soft);
  color: var(--warning);
}

.ui-alert--danger {
  border-color: var(--danger);
  background: var(--color-danger-soft);
  color: var(--danger);
}

.ui-alert__icon,
.ui-alert__dismiss {
  flex: 0 0 auto;
}

.ui-alert__icon {
  display: inline-flex;
}

.ui-alert__body {
  min-width: 0;
  flex: 1 1 auto;
}

.ui-alert__title {
  color: currentColor;
  font-weight: var(--font-semibold);
}

.ui-alert__content {
  color: var(--text-muted);
  line-height: var(--leading-normal);
}

.ui-alert__title + .ui-alert__content,
.ui-alert__actions {
  margin-block-start: var(--space-1);
}

.ui-alert__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.ui-alert__dismiss {
  width: var(--control-height-xs);
  height: var(--control-height-xs);
  border-radius: var(--radius-sm);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  background: transparent;
  color: currentColor;
  font-size: var(--text-xl);
  line-height: var(--leading-tight);
}

.ui-alert__dismiss:hover {
  background: var(--surface-hover);
}
</style>
