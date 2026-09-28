<template>
  <section class="filter-bar" role="region" :aria-label="label">
    <div class="filter-bar__fields">
      <slot />
    </div>
    <div v-if="$slots.status" class="filter-bar__status" role="status" aria-live="polite">
      <slot name="status" />
    </div>
    <div v-if="$slots.actions" class="filter-bar__actions">
      <slot name="actions" />
    </div>
  </section>
</template>

<script setup lang="ts">
defineProps<{
  label: string
}>()

defineSlots<{
  default?: () => unknown
  status?: () => unknown
  actions?: () => unknown
}>()

defineOptions({ name: 'FilterBar' })
</script>

<style scoped>
.filter-bar {
  width: 100%;
  min-width: 0;
  display: flex;
  align-items: flex-end;
  gap: var(--space-3);
}

.filter-bar__fields {
  min-width: 0;
  display: flex;
  flex: 1 1 auto;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: var(--space-3);
}

.filter-bar__status {
  flex: 0 1 auto;
  min-inline-size: var(--filter-bar-status-min-inline-size, 0);
  align-self: center;
  color: var(--text-muted);
  font-size: var(--text-sm);
  text-align: var(--filter-bar-status-text-align, start);
  white-space: var(--filter-bar-status-white-space, normal);
}

.filter-bar__actions {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}

@media (max-width: 640px) {
  .filter-bar,
  .filter-bar__fields,
  .filter-bar__actions {
    align-items: stretch;
    flex-direction: column;
  }

  .filter-bar__fields,
  .filter-bar__actions,
  .filter-bar__fields :deep(> *),
  .filter-bar__actions :deep(> *) {
    width: 100%;
  }

  .filter-bar__status {
    width: 100%;
    min-inline-size: 0;
    align-self: stretch;
    text-align: start;
    white-space: normal;
  }
}
</style>
