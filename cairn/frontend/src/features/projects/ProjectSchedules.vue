<template>
  <section class="schedule-list" aria-label="计划任务">
    <div class="schedule-toolbar">
      <FormField label="搜索计划" for="schedule-search"
        ><Input id="schedule-search" v-model="search" placeholder="搜索名称或计划 ID"
      /></FormField>
      <FormField label="计划状态" for="schedule-status"
        ><Select id="schedule-status" v-model="status" :options="statusOptions"
      /></FormField>
      <Button variant="secondary" size="sm" :loading="isFetching" @click="refetch()">刷新</Button>
    </div>
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
      title="暂无匹配计划"
      description="可在新建项目中选择单次定时、每日或每周重复运行。"
    />
    <div v-else class="schedule-table-scroll">
      <table class="schedule-table">
        <thead>
          <tr>
            <th>计划</th>
            <th>启动规则 / 时区</th>
            <th>下一次时间</th>
            <th>状态 / 上次结果</th>
            <th>关联项目</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="schedule in filtered" :key="schedule.id">
            <td>
              <router-link :to="`/project-schedules/${schedule.id}`">{{
                schedule.project?.title || `单次预约 · ${schedule.id}`
              }}</router-link>
              <div class="schedule-muted">v{{ schedule.version }}</div>
            </td>
            <td>
              {{ scheduleRuleLabel(schedule) }}
              <div class="schedule-muted">{{ schedule.timezone }}</div>
            </td>
            <td>{{ scheduleLocalTime(schedule.next_run_at, schedule.timezone) }}</td>
            <td>
              <Badge :tone="schedule.status === 'blocked' ? 'warning' : 'neutral'" size="sm">{{
                scheduleStatusLabels[schedule.status]
              }}</Badge>
              <div v-if="schedule.last_result" class="schedule-muted">
                {{ occurrenceStatusLabels[schedule.last_result.status] }}
              </div>
              <details v-if="schedule.block_reason || schedule.last_result?.reason">
                <summary>查看原因</summary>
                {{ schedule.block_reason || schedule.last_result?.reason }}
              </details>
            </td>
            <td>
              <router-link
                v-if="schedule.last_project_id || schedule.project_id"
                :to="`/projects/${schedule.last_project_id || schedule.project_id}`"
                >{{
                  schedule.mode === 'once' ? '待启动 / 已启动项目' : '最近生成项目'
                }}</router-link
              ><span v-else class="schedule-muted">尚未生成项目</span>
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
import {
  Alert,
  Badge,
  Button,
  EmptyState,
  FormField,
  Input,
  Select,
  Skeleton,
} from '@/components/ui'
import { ErrorState } from '@/components/patterns'
import {
  listProjectSchedules,
  occurrenceStatusLabels,
  scheduleLocalTime,
  scheduleRuleLabel,
  scheduleStatusLabels,
} from '@/api/projectSchedules'
const search = ref('')
const status = ref('all')
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
  padding: var(--space-3);
}
.schedule-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  align-items: end;
  margin-bottom: var(--space-4);
}
.schedule-table-scroll {
  overflow-x: auto;
}
.schedule-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}
.schedule-table th {
  text-align: left;
  color: var(--text-muted);
  font-weight: 500;
}
.schedule-table td,
.schedule-table th {
  padding: var(--space-3);
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
.schedule-table td {
  min-width: 120px;
  overflow-wrap: anywhere;
}
.schedule-muted {
  color: var(--text-muted);
  font-size: var(--text-xs);
  margin-top: var(--space-1);
}
.schedule-table summary {
  cursor: pointer;
  color: var(--text-muted);
}
</style>
