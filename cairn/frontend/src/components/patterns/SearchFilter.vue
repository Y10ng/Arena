<template>
  <Popover v-model:open="open" align="start" size="sm">
    <template #trigger>
      <button type="button" class="search-filter" :class="{ 'is-selected': modelValue !== clearValue }">
        <Plus :size="14" aria-hidden="true" /><span>{{ label }}<template v-if="modelValue !== clearValue">：{{ selectedLabel }}</template></span><ChevronDown :size="13" aria-hidden="true" />
      </button>
    </template>
    <div class="search-filter-options">
      <Input v-model="query" :aria-label="`搜索${label}`" placeholder="搜索选项…" />
      <fieldset>
        <legend class="sr-only">{{ label }}</legend>
        <label v-for="option in filteredOptions" :key="option.value" class="search-filter-option">
          <input type="radio" :name="groupId" :value="option.value" :checked="modelValue === option.value" @change="select(option.value)" />
          <span>{{ option.label }}</span>
        </label>
        <p v-if="!filteredOptions.length" role="status">没有匹配选项</p>
      </fieldset>
      <Button v-if="modelValue !== clearValue" variant="ghost" size="sm" @click="select(clearValue)">清除筛选</Button>
    </div>
  </Popover>
</template>
<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { ChevronDown, Plus } from '@lucide/vue'
import { Button, Input, Popover } from '@/components/ui'
const props = withDefaults(defineProps<{ modelValue: string; label: string; options: readonly { value: string; label: string }[]; clearValue?: string }>(), { clearValue: '__all__' })
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const open = ref(false)
const query = ref('')
const groupId = useId()
const selectedLabel = computed(() => props.options.find(option => option.value === props.modelValue)?.label || props.modelValue)
const filteredOptions = computed(() => props.options.filter(option => option.label.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase())))
watch(open, () => { query.value = '' })
function select(value: string) { emit('update:modelValue', value); open.value = false }
</script>
<style scoped>
.search-filter { display: inline-flex; align-items: center; gap: 7px; height: var(--control-height-filter); padding: 0 11px; border: 1px dashed var(--color-control-border); border-radius: var(--radius-md); background: var(--color-canvas); color: var(--text-muted); font: inherit; font-size: var(--text-control); white-space: nowrap; cursor: pointer; }
.search-filter.is-selected { border-style: solid; background: var(--surface-selected); color: var(--text); }
.search-filter:hover { background: var(--surface-hover); }
.search-filter:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.search-filter-options { display: grid; gap: 8px; }
.search-filter-options fieldset { border: 0; padding: 0; margin: 0; max-height: 240px; overflow: auto; }
.search-filter-option { display: flex; align-items: center; gap: 9px; min-height: var(--control-height-filter); padding: 7px; border-radius: var(--radius-sm); font-size: var(--text-control); cursor: pointer; overflow-wrap: anywhere; }
.search-filter-option:hover, .search-filter-option:focus-within { background: var(--surface-hover); }
.search-filter-option input { accent-color: var(--accent); }
.search-filter-options p { color: var(--text-muted); font-size: var(--text-control); }
</style>
