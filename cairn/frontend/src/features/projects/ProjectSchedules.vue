<template>
  <section class="schedule-list" aria-label="计划任务">
    <div class="schedule-toolbar">
      <slot name="views" />
      <div class="schedule-actions">
        <Button variant="secondary" :loading="isFetching" @click="refetch()">
          <template #icon><RefreshCw :size="16" /></template>刷新
        </Button>
        <slot name="actions" />
      </div>
    </div>
    <FilterBar label="计划筛选">
      <div class="schedule-search">
        <Search :size="16" aria-hidden="true" />
        <Input
          id="schedule-search"
          v-model="search"
          aria-label="搜索计划"
          placeholder="搜索名称或计划 ID"
          @keydown.esc="search = ''"
        />
      </div>
      <SearchFilter v-model="status" label="计划状态" :options="statusOptions" clear-value="all" />
      <Button v-if="search || status !== 'all'" variant="ghost" @click="resetFilters">重置</Button>
    </FilterBar>
    <Alert v-if="isError && data" tone="warning" title="计划刷新失败"
      >当前显示上次成功的数据；请重试刷新。</Alert
    >
    <Skeleton v-if="isLoading" aria-label="正在加载计划任务" />
    <ErrorState
      v-else-if="isError && !data"
      title="计划任务加载失败"
      description="请检查后端连接并重试。"
      @retry="refetch()"
    />
    <EmptyState
      v-else-if="!filtered.length"
      :title="search || status !== 'all' ? '没有匹配的计划' : '暂无计划'"
    />
    <div v-else class="schedule-table-scroll">
      <table class="schedule-table" aria-label="计划任务列表">
        <thead>
          <tr>
            <th scope="col">计划</th>
            <th scope="col">启动规则</th>
            <th scope="col">下一次启动</th>
            <th scope="col">计划状态</th>
            <th scope="col">关联项目</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="schedule in filtered" :key="schedule.id">
            <td class="schedule-name-cell">
              <router-link class="schedule-name" :title="schedule.project?.title || schedule.id" :to="`/project-schedules/${schedule.id}`">{{
                schedule.project?.title || `单次预约 · ${schedule.id}`
              }}</router-link>
            </td>
            <td>
              <div class="schedule-rule">
                <span class="schedule-rule-label" :title="scheduleRuleLabel(schedule)">{{ scheduleRuleLabel(schedule) }}</span>
                <span class="schedule-muted">{{ schedule.timezone }}</span>
              </div>
            </td>
            <td class="schedule-time">
              {{ scheduleLocalTime(schedule.next_run_at, schedule.timezone) }}
            </td>
            <td>
              <div class="schedule-status">
                <Badge :tone="schedule.status === 'blocked' ? 'warning' : 'neutral'" size="sm">{{
                  scheduleStatusLabels[schedule.status] || schedule.status
                }}</Badge>
                <Badge v-if="schedule.last_result && schedule.last_result.status !== 'started'" tone="warning" size="sm">
                  上次{{ occurrenceStatusLabels[schedule.last_result.status] || schedule.last_result.status }}
                </Badge>
                <details
                  v-if="schedule.block_reason || schedule.last_result?.reason"
                  class="schedule-reason"
                >
                  <summary>{{ schedule.block_reason ? '阻塞原因' : '上次结果详情' }}</summary>
                  <p>{{ schedule.block_reason || schedule.last_result?.reason }}</p>
                </details>
              </div>
            </td>
            <td>
              <router-link
                v-if="schedule.last_project_id || schedule.project_id"
                :to="`/projects/${schedule.mode === 'once' ? schedule.project_id || schedule.last_project_id : schedule.last_project_id || schedule.project_id}`"
                >{{ schedule.mode === 'once' ? '进入关联项目' : '进入最近项目' }}</router-link
              >
              <span v-else class="schedule-muted">无可进入的关联项目</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { RefreshCw, Search } from '@lucide/vue'
import { Alert, Badge, Button, EmptyState, Input, Skeleton } from '@/components/ui'
import { ErrorState, FilterBar } from '@/components/patterns'
import SearchFilter from '@/components/patterns/SearchFilter.vue'
import {
  listProjectSchedules,
  occurrenceStatusLabels,
  scheduleLocalTime,
  scheduleRuleLabel,
  scheduleStatusLabels,
} from '@/api/projectSchedules'
const search = ref('')
const status = ref('all')
function resetFilters() {
  search.value = ''
  status.value = 'all'
}
const statusOptions = [
  { value: 'all', label: '全部' },
  ...Object.entries(scheduleStatusLabels).map(([value, label]) => ({ value, label })),
]
const { data, isLoading, isFetching, isError, refetch } = useQuery({
  queryKey: ['project-schedules'],
  queryFn: listProjectSchedules,
  refetchInterval: 8000,
  retry: false,
})
const filtered = computed(() =>
  (data.value || []).filter(
    (schedule) =>
      (status.value === 'all' || schedule.status === status.value) &&
      `${schedule.project?.title || ''} ${schedule.id}`
        .toLowerCase()
        .includes(search.value.toLowerCase()),
  ),
)
</script>
<style scoped>
.schedule-list {
  min-width: 0;
}
.schedule-toolbar, .schedule-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.schedule-toolbar { justify-content: space-between; gap: var(--space-3) var(--space-5); margin-bottom: var(--space-3); }
.schedule-actions { margin-left: auto; }
.schedule-search {
  position: relative;
  width: 300px;
  max-width: 100%;
}
.schedule-search > svg {
  position: absolute;
  top: 50%;
  left: var(--space-3);
  transform: translateY(-50%);
  color: var(--text-muted);
  pointer-events: none;
}
.schedule-search :deep(input) {
  padding-left: var(--space-7);
}
.schedule-table-scroll {
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
}
.schedule-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-md);
  line-height: var(--leading-normal);
}
.schedule-table th {
  height: 39px;
  padding: var(--space-2) var(--space-4);
  text-align: left;
  color: var(--text-muted);
  background: var(--bg-inset);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  white-space: nowrap;
}
.schedule-table tbody tr { height: var(--table-row-height-detail); }
.schedule-table td {
  padding: var(--space-2) var(--space-4);
  border-top: 1px solid var(--border);
  vertical-align: middle;
  min-width: 130px;
  overflow-wrap: anywhere;
}
.schedule-table tr:hover td {
  background: var(--surface-hover);
}
.schedule-name-cell {
  width: 26%;
}
.schedule-name {
  display: block;
  max-width: 22rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
  font-weight: var(--font-medium);
}
.schedule-name:hover {
  color: var(--link-hover);
}
.schedule-muted {
  color: var(--text-muted);
  font-size: var(--text-sm);
}
.schedule-rule,
.schedule-status {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.schedule-rule { display: grid; gap: 3px; white-space: nowrap; }
.schedule-rule-label {
  max-width: 18rem;
  overflow: hidden;
  text-overflow: ellipsis;
}
.schedule-status { flex-wrap: wrap; }
.schedule-time {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.schedule-reason {
  font-size: var(--text-sm);
}
.schedule-reason[open] { flex-basis: 100%; }
.schedule-reason summary {
  cursor: pointer;
  color: var(--text-muted);
}
.schedule-reason p {
  max-width: 36ch;
  margin: var(--space-2) 0 0;
  white-space: pre-wrap;
}
.schedule-list a:focus-visible,
.schedule-reason summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
  border-radius: var(--radius-sm);
}
</style>
