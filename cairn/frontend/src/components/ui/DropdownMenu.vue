<template>
  <DropdownMenuRoot :open="open" :modal="modal" @update:open="emit('update:open', $event)">
    <DropdownMenuTrigger as-child>
      <slot name="trigger" />
    </DropdownMenuTrigger>

    <DropdownMenuPortal>
      <DropdownMenuContent
        class="ui-dropdown-menu__content"
        :side="side"
        :align="align"
        :side-offset="sideOffset"
        :collision-padding="collisionPadding"
      >
        <DropdownMenuLabel v-if="label" class="ui-dropdown-menu__label">
          {{ label }}
        </DropdownMenuLabel>
        <template v-for="item in items" :key="item.value">
          <DropdownMenuSeparator v-if="item.separatorBefore" class="ui-dropdown-menu__separator" />
          <DropdownMenuItem
            class="ui-dropdown-menu__item"
            :class="{ 'ui-dropdown-menu__item--danger': item.tone === 'danger' }"
            :disabled="item.disabled"
            :text-value="item.label"
            @select="emit('select', item.value)"
          >
            <span v-if="$slots.icon" class="ui-dropdown-menu__icon" aria-hidden="true">
              <slot name="icon" :item="item" />
            </span>
            <span class="ui-dropdown-menu__item-label">{{ item.label }}</span>
            <span v-if="item.shortcut" class="ui-dropdown-menu__shortcut" aria-hidden="true">
              {{ item.shortcut }}
            </span>
          </DropdownMenuItem>
        </template>
        <slot />
      </DropdownMenuContent>
    </DropdownMenuPortal>
  </DropdownMenuRoot>
</template>

<script setup lang="ts">
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuPortal,
  DropdownMenuRoot,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from 'reka-ui'
import type { DropdownMenuContentProps } from 'reka-ui'

export type DropdownMenuSide = NonNullable<DropdownMenuContentProps['side']>
export type DropdownMenuAlign = NonNullable<DropdownMenuContentProps['align']>
export type DropdownMenuItemTone = 'default' | 'danger'

export interface DropdownMenuOption {
  value: string
  label: string
  disabled?: boolean
  shortcut?: string
  separatorBefore?: boolean
  tone?: DropdownMenuItemTone
}

withDefaults(
  defineProps<{
    open?: boolean
    items: readonly DropdownMenuOption[]
    label?: string
    side?: DropdownMenuSide
    align?: DropdownMenuAlign
    sideOffset?: number
    collisionPadding?: number
    modal?: boolean
  }>(),
  {
    open: undefined,
    label: '',
    side: 'bottom',
    align: 'end',
    sideOffset: 6,
    collisionPadding: 8,
    modal: true,
  },
)

const emit = defineEmits<{
  (event: 'update:open', value: boolean): void
  (event: 'select', value: string): void
}>()

defineOptions({ name: 'UiDropdownMenu' })
</script>

<!-- DropdownMenu 通过 Portal 挂载到 body，浮层样式使用组件专属类名全局生效。 -->
<style>
.ui-dropdown-menu__content {
  min-width: 180px;
  max-width: min(320px, calc(100vw - var(--space-4)));
  border: calc(var(--space-1) / 4) solid var(--border-strong);
  border-radius: var(--radius-md);
  padding: var(--space-1);
  z-index: var(--z-dropdown);
  overflow: hidden;
  background: var(--bg-elevated);
  box-shadow: var(--shadow-pop);
  color: var(--text);
  transform-origin: var(--reka-dropdown-menu-content-transform-origin);
}

.ui-dropdown-menu__content[data-state='open'] {
  animation: ui-dropdown-menu-enter var(--dur-fast) var(--ease-enter);
}

.ui-dropdown-menu__label {
  padding: var(--space-2) var(--space-3);
  color: var(--text-faint);
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
}

.ui-dropdown-menu__item {
  min-height: var(--control-height-sm);
  border-radius: var(--radius-sm);
  padding: var(--space-2) var(--space-3);
  display: flex;
  align-items: center;
  gap: var(--space-2);
  outline: none;
  color: var(--text);
  font-size: var(--text-sm);
  line-height: var(--leading-snug);
  cursor: default;
  user-select: none;
}

.ui-dropdown-menu__item[data-highlighted] {
  background: var(--surface-selected);
}

.ui-dropdown-menu__item[data-disabled] {
  color: var(--text-disabled);
  pointer-events: none;
}

.ui-dropdown-menu__item--danger {
  color: var(--danger);
}

.ui-dropdown-menu__item--danger[data-highlighted] {
  background: var(--color-danger-soft);
}

.ui-dropdown-menu__icon {
  width: var(--space-4);
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.ui-dropdown-menu__item-label {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ui-dropdown-menu__shortcut {
  margin-left: var(--space-3);
  color: var(--text-faint);
  font: var(--text-xs)/1 var(--font-mono);
}

.ui-dropdown-menu__separator {
  height: calc(var(--space-1) / 4);
  margin: var(--space-1) calc(var(--space-1) * -1);
  background: var(--border);
}

@keyframes ui-dropdown-menu-enter {
  from {
    opacity: 0;
    transform: scale(0.98);
  }
}

@media (prefers-reduced-motion: reduce) {
  .ui-dropdown-menu__content {
    animation: none;
  }
}
</style>
