<template>
  <TabsRoot
    class="ui-tabs"
    :model-value="modelValue"
    :orientation="orientation"
    :activation-mode="activationMode"
    :unmount-on-hide="unmountOnHide"
    @update:model-value="handleModelValueChange"
  >
    <TabsList class="ui-tabs__list" :class="`ui-tabs__list--${variant}`" :loop="loop">
      <TabsTrigger
        v-for="option in options"
        :key="option.value"
        class="ui-tabs__trigger"
        :value="option.value"
        :disabled="option.disabled"
      >
        <slot name="label" :option="option">{{ option.label }}</slot>
        <span v-if="option.count !== undefined" class="ui-tabs__count">
          {{ option.count }}
        </span>
      </TabsTrigger>
    </TabsList>
    <div v-if="$slots.actions" class="ui-tabs__actions">
      <slot name="actions" />
    </div>

    <TabsContent
      v-for="option in options"
      :key="`content:${option.value}`"
      class="ui-tabs__content"
      :value="option.value"
    >
      <slot name="panel" :option="option" />
    </TabsContent>
  </TabsRoot>
</template>

<script setup lang="ts">
import { TabsContent, TabsList, TabsRoot, TabsTrigger } from 'reka-ui'

export type TabsOrientation = 'horizontal' | 'vertical'
export type TabsActivationMode = 'automatic' | 'manual'
export type TabsVariant = 'line' | 'pill'

export interface TabsOption {
  value: string
  label: string
  disabled?: boolean
  count?: number | string
}

withDefaults(
  defineProps<{
    modelValue: string
    options: readonly TabsOption[]
    orientation?: TabsOrientation
    activationMode?: TabsActivationMode
    variant?: TabsVariant
    loop?: boolean
    unmountOnHide?: boolean
  }>(),
  {
    orientation: 'horizontal',
    activationMode: 'automatic',
    variant: 'line',
    loop: true,
    unmountOnHide: true,
  },
)

const emit = defineEmits<{
  (event: 'update:modelValue', value: string): void
}>()

function handleModelValueChange(value: string | number) {
  if (typeof value === 'string') emit('update:modelValue', value)
}

defineOptions({ name: 'UiTabs' })
</script>

<style scoped>
.ui-tabs {
  min-width: 0;
}

.ui-tabs__list {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  overflow-x: auto;
  scrollbar-width: thin;
}

.ui-tabs[data-orientation='vertical'] {
  display: grid;
  grid-template-columns: minmax(160px, max-content) minmax(0, 1fr);
  gap: var(--space-4);
}

.ui-tabs[data-orientation='vertical'] .ui-tabs__list {
  flex-direction: column;
  align-items: stretch;
  overflow: visible;
}

.ui-tabs__list--line {
  border-bottom: calc(var(--space-1) / 4) solid var(--border);
}

.ui-tabs__list--pill {
  width: fit-content;
  border-radius: var(--radius-lg);
  padding: var(--space-1);
  background: var(--bg-inset);
}

.ui-tabs__trigger {
  min-height: var(--control-height);
  border: 0;
  border-radius: var(--radius-sm);
  padding: var(--space-2) var(--space-3);
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  flex: none;
  background: transparent;
  color: var(--text-muted);
  font-size: var(--text-md);
  font-weight: var(--font-medium);
  white-space: nowrap;
  cursor: pointer;
  transition:
    background-color var(--dur-fast) var(--ease),
    color var(--dur-fast) var(--ease);
}

.ui-tabs__trigger:hover:not(:disabled) {
  color: var(--text);
}

.ui-tabs__list--line .ui-tabs__trigger[data-state='active'] {
  color: var(--accent);
}

.ui-tabs__list--line .ui-tabs__trigger[data-state='active']::after {
  content: '';
  height: calc(var(--space-1) / 2);
  position: absolute;
  right: var(--space-2);
  bottom: calc(var(--space-1) / -4);
  left: var(--space-2);
  border-radius: var(--radius-full);
  background: var(--accent);
}

.ui-tabs__list--pill .ui-tabs__trigger[data-state='active'] {
  background: var(--bg-elevated);
  box-shadow: var(--shadow-xs);
  color: var(--text);
}

.ui-tabs__trigger:disabled {
  color: var(--text-disabled);
  cursor: not-allowed;
}

.ui-tabs__count {
  min-width: var(--space-4);
  border-radius: var(--radius-full);
  padding: 0 var(--space-1);
  background: var(--surface-selected);
  color: var(--text-faint);
  font-size: var(--text-xs);
  line-height: var(--leading-snug);
}

.ui-tabs__content {
  min-width: 0;
  padding-top: var(--space-4);
  outline: none;
}

.ui-tabs__content:focus-visible {
  box-shadow: var(--ring);
}

.ui-tabs[data-orientation='vertical'] .ui-tabs__content {
  padding-top: 0;
}

@media (max-width: 720px) {
  .ui-tabs[data-orientation='vertical'] {
    display: block;
  }

  .ui-tabs[data-orientation='vertical'] .ui-tabs__list {
    flex-direction: row;
    overflow-x: auto;
  }

  .ui-tabs[data-orientation='vertical'] .ui-tabs__content {
    padding-top: var(--space-4);
  }
}
</style>
