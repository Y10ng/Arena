<template>
  <section class="ui-empty-state" :aria-labelledby="hasTitle ? titleId : undefined">
    <div v-if="$slots.icon" class="ui-empty-state__icon" aria-hidden="true">
      <slot name="icon" />
    </div>
    <h2 v-if="hasTitle" :id="titleId" class="ui-empty-state__title">
      <slot name="title">{{ title }}</slot>
    </h2>
    <div v-if="hasDescription" class="ui-empty-state__description">
      <slot name="description">{{ description }}</slot>
    </div>
    <div v-if="$slots.default" class="ui-empty-state__content">
      <slot />
    </div>
    <div v-if="$slots.actions" class="ui-empty-state__actions">
      <slot name="actions" />
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, useId, useSlots } from 'vue'

const props = withDefaults(
  defineProps<{
    title?: string
    description?: string
  }>(),
  {
    title: '',
    description: '',
  },
)

const slots = useSlots()
const titleId = `empty-state-${useId()}-title`
const hasTitle = computed(() => Boolean(props.title || slots.title))
const hasDescription = computed(() => Boolean(props.description || slots.description))
</script>

<style scoped>
.ui-empty-state {
  width: 100%;
  border: calc(var(--space-1) / 4) dashed var(--border);
  border-radius: var(--radius-lg);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-6) var(--space-5);
  color: var(--text-muted);
  text-align: center;
}

.ui-empty-state__icon {
  display: inline-flex;
  color: var(--text-faint);
}

.ui-empty-state__title {
  margin: var(--space-0);
  color: var(--text);
  font-size: var(--text-lg);
  font-weight: var(--font-semibold);
  line-height: var(--leading-tight);
}

.ui-empty-state__description,
.ui-empty-state__content {
  color: var(--text-muted);
  line-height: var(--leading-normal);
}

.ui-empty-state__actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: var(--space-2);
  margin-block-start: var(--space-2);
}
</style>
