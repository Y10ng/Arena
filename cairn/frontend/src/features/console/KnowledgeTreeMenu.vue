<template>
  <ContextMenuRoot @update:open="contextOpen = $event">
    <ContextMenuTrigger as-child>
      <div ref="row" v-bind="$attrs" @contextmenu.stop="captureContext" @keydown="openFromKeyboard">
        <slot />
        <DropdownMenu v-if="!hideTrigger" :items="menuItems" @update:open="onDropdownOpen" @select="select">
          <template #trigger>
            <IconButton :label="`更多操作：${label}`" :data-console-action="`knowledge-menu:${target.key}:${target.path}`"
              @pointerdown="capture" @keydown="capture" @click.stop="capture">
              <MoreHorizontal :size="16" />
            </IconButton>
          </template>
        </DropdownMenu>
      </div>
    </ContextMenuTrigger>
    <ContextMenuPortal>
      <ContextMenuContent class="ui-dropdown-menu__content knowledge-context-menu" :collision-padding="8"
        @close-auto-focus="restoreFocus">
        <template v-for="item in menuItems" :key="item.value">
          <ContextMenuSeparator v-if="item.separatorBefore" class="ui-dropdown-menu__separator" />
          <ContextMenuItem class="ui-dropdown-menu__item" :disabled="item.disabled" :text-value="item.label"
            :class="{ 'ui-dropdown-menu__item--danger': item.tone === 'danger' }" @select="select(item.value)">
            <span class="ui-dropdown-menu__item-text">
              <span>{{ item.label }}</span>
              <span v-if="item.description" class="ui-dropdown-menu__description">{{ item.description }}</span>
            </span>
          </ContextMenuItem>
        </template>
      </ContextMenuContent>
    </ContextMenuPortal>
  </ContextMenuRoot>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { MoreHorizontal } from '@lucide/vue'
import { ContextMenuRoot, ContextMenuTrigger, ContextMenuPortal, ContextMenuContent, ContextMenuItem, ContextMenuSeparator } from 'reka-ui'
import { DropdownMenu, IconButton } from '@/components/ui'
import type { DropdownMenuOption } from '@/components/ui/DropdownMenu.vue'

defineOptions({ inheritAttrs: false })
export interface KnowledgeMenuTarget { key: string; path: string; kind: 'library' | 'directory' | 'file' }
const props = defineProps<{ target: KnowledgeMenuTarget; items: DropdownMenuOption[]; label: string; hideTrigger?: boolean }>()
const emit = defineEmits<{ select: [action: string, target: KnowledgeMenuTarget, trigger: HTMLElement | null] }>()
const row = ref<HTMLElement | null>(null)
const contextOpen = ref(false)
const dropdownOpen = ref(false)
const captured = ref<KnowledgeMenuTarget | null>(null)
const capturedItems = ref<DropdownMenuOption[]>([])
let trigger: HTMLElement | null = null
const menuItems = computed(() => contextOpen.value || dropdownOpen.value ? capturedItems.value : props.items)
function capture(event?: Event) {
  if (contextOpen.value || dropdownOpen.value) return
  captured.value = { ...props.target }
  capturedItems.value = props.items.map((item) => ({ ...item }))
  trigger = event?.currentTarget instanceof HTMLElement ? event.currentTarget : row.value
}
function captureContext(event: MouseEvent) {
  capture()
  trigger = event.target instanceof HTMLElement
    ? event.target.closest<HTMLElement>('button') || row.value : row.value
}
function onDropdownOpen(open: boolean) {
  if (open && !captured.value) capture()
  dropdownOpen.value = open
}
function select(action: string) {
  // 两种入口消费打开时的同一目标，不读取当前阅读对象。
  if (captured.value) emit('select', action, { ...captured.value }, trigger)
}
function restoreFocus(event: Event) {
  event.preventDefault()
  if (trigger?.isConnected) {
    if (trigger === row.value) row.value?.querySelector<HTMLElement>('button')?.focus()
    else trigger.focus()
  }
}
function openFromKeyboard(event: KeyboardEvent) {
  if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return
  event.preventDefault()
  event.stopPropagation()
  const element = event.target as HTMLElement
  const rect = element.getBoundingClientRect()
  element.dispatchEvent(new MouseEvent('contextmenu', {
    bubbles: true, cancelable: true, clientX: rect.left + 12, clientY: rect.bottom,
  }))
}
</script>

<style>
.knowledge-context-menu {
  max-height: min(calc(var(--reka-context-menu-content-available-height) / var(--ui-scale)), calc(70 * var(--ui-dvh)));
}
</style>
