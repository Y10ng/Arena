<template>
  <section class="graph-toolbar" aria-label="图谱工具栏">
    <div class="toolbar-left">
      <div class="search-box">
        <Search :size="14" :stroke-width="2" class="search-icon" aria-hidden="true" />
        <Input
          :model-value="searchQuery"
          aria-label="搜索节点内容"
          placeholder="搜索节点内容..."
          class="toolbar-input"
          @update:model-value="updateSearchQuery"
          @keyup.enter="$emit('runSearch')"
        />
        <Badge v-if="searchMatchCount > 0" class="search-matches" tone="info" size="sm">
          {{ searchMatchCount }} 个匹配
        </Badge>
      </div>

      <div class="filter-group" role="group" aria-label="节点状态过滤">
        <span class="filter-lbl">过滤:</span>
        <div class="segmented-control">
          <Button
            v-for="filter in statusFilters"
            :key="filter.value"
            class="segment-btn"
            :class="{ active: activeStatusFilter === filter.value }"
            :variant="activeStatusFilter === filter.value ? 'secondary' : 'ghost'"
            size="sm"
            :aria-pressed="activeStatusFilter === filter.value"
            @click="$emit('update:activeStatusFilter', filter.value)"
          >
            {{ filter.label }}
          </Button>
        </div>
      </div>
    </div>

    <div class="toolbar-right">
      <Tooltip content="为项目添加全局提示，下一轮 reason 自动注入">
        <Button class="graph-tool-button" variant="secondary" size="sm" @click="$emit('addHint')">
          <template #icon><Sparkles :size="14" :stroke-width="2" /></template>
          添加提示
        </Button>
      </Tooltip>

      <Tooltip content="折叠未开始分析的分支">
        <Checkbox
          :model-value="isBranchCollapseEnabled"
          label="自动收拢"
          class="graph-collapse-checkbox"
          @update:model-value="$emit('update:isBranchCollapseEnabled', $event)"
        />
      </Tooltip>

      <Tooltip content="清除当前项目中手动拖动的节点位置">
        <Button
          class="graph-tool-button"
          variant="secondary"
          size="sm"
          @click="$emit('resetNodePositions')"
        >
          <template #icon><RotateCcw :size="14" :stroke-width="2" /></template>
          重置节点
        </Button>
      </Tooltip>

      <Tooltip content="复位视口 (Fit)">
        <Button
          class="graph-tool-button"
          variant="secondary"
          size="sm"
          @click="$emit('resetViewport')"
        >
          <template #icon><Scan :size="14" :stroke-width="2" /></template>
          适应视口
        </Button>
      </Tooltip>

      <Tooltip :content="isFullscreen ? '退出全屏 (Esc)' : '全屏展示图谱'">
        <Button
          class="graph-tool-button"
          variant="secondary"
          size="sm"
          @click="$emit('toggleFullscreen')"
        >
          <template #icon>
            <Minimize2 v-if="isFullscreen" :size="14" :stroke-width="2" />
            <Maximize2 v-else :size="14" :stroke-width="2" />
          </template>
          {{ isFullscreen ? '退出全屏' : '全屏' }}
        </Button>
      </Tooltip>
    </div>
  </section>
</template>

<script setup lang="ts">
import { Maximize2, Minimize2, RotateCcw, Scan, Search, Sparkles } from '@lucide/vue'
import { Badge, Button, Checkbox, Input, Tooltip } from '@/components/ui'

defineProps<{
  searchQuery: string
  searchMatchCount: number
  activeStatusFilter: string
  statusFilters: { value: string; label: string }[]
  isBranchCollapseEnabled: boolean
  isFullscreen: boolean
}>()

const emit = defineEmits<{
  (event: 'update:searchQuery', value: string): void
  (event: 'update:activeStatusFilter', value: string): void
  (event: 'update:isBranchCollapseEnabled', value: boolean): void
  (event: 'runSearch'): void
  (event: 'resetViewport'): void
  (event: 'resetNodePositions'): void
  (event: 'toggleFullscreen'): void
  (event: 'addHint'): void
}>()

const updateSearchQuery = (value: string | number | null) => {
  emit('update:searchQuery', String(value ?? ''))
}
</script>
