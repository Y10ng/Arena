<template>
  <section class="graph-toolbar" aria-label="图谱工具栏">
    <div class="search-box">
      <Search :size="16" :stroke-width="2" class="search-icon" aria-hidden="true" />
      <Input
        :model-value="searchQuery"
        aria-label="搜索节点内容"
        placeholder="搜索节点内容…"
        class="toolbar-input"
        @update:model-value="updateSearchQuery"
        @keyup.enter="$emit('runSearch')"
      />
      <span v-if="searchQuery.trim() || searchMatchCount > 0" class="search-matches" role="status">
        {{ searchMatchCount }} 个匹配
      </span>
    </div>
    <div class="graph-toolbar-views">
      <div class="graph-status-views" role="group" aria-label="节点状态过滤">
        <Button
          v-for="filter in statusFilters"
          :key="filter.value"
          class="segment-btn"
          :class="{ active: activeStatusFilter === filter.value }"
          variant="ghost"
          size="sm"
          :aria-pressed="activeStatusFilter === filter.value"
          @click="$emit('update:activeStatusFilter', filter.value)"
        >
          {{ filter.label }}
        </Button>
      </div>
      <Tooltip content="折叠未开始分析的分支">
        <Checkbox
          :model-value="isBranchCollapseEnabled"
          label="自动收拢"
          class="graph-collapse-checkbox"
          @update:model-value="$emit('update:isBranchCollapseEnabled', $event)"
        />
      </Tooltip>
    </div>
    <div class="graph-view-actions" role="group" aria-label="图谱视图操作">
      <Tooltip content="清除当前项目中手动拖动的节点位置">
        <Button
          class="graph-tool-button"
          variant="ghost"
          size="sm"
          @click="$emit('resetNodePositions')"
        >
          <template #icon><RotateCcw :size="16" /></template>
          重置节点
        </Button>
      </Tooltip>
      <Button
        class="graph-tool-button"
        variant="secondary"
        size="sm"
        @click="$emit('resetViewport')"
      >
        <template #icon><Scan :size="16" /></template>
        适应视口
      </Button>
      <Button
        class="graph-tool-button"
        variant="secondary"
        size="sm"
        @click="$emit('toggleFullscreen')"
      >
        <template #icon>
          <Minimize2 v-if="isFullscreen" :size="16" />
          <Maximize2 v-else :size="16" />
        </template>
        {{ isFullscreen ? '退出全屏' : '全屏' }}
      </Button>
      <Button class="graph-tool-button" variant="secondary" size="sm" @click="$emit('addHint')">
        <template #icon><MessageSquarePlus :size="16" /></template>
        添加提示
      </Button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { Maximize2, MessageSquarePlus, Minimize2, RotateCcw, Scan, Search } from '@lucide/vue'
import { Button, Checkbox, Input, Tooltip } from '@/components/ui'

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
