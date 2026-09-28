<template>
  <div class="monitoring-container">
    <div class="section-header section-header--actions">
      <Badge :tone="refreshStatusTone" :title="refreshStatusTitle" size="sm" dot>
        {{ refreshStatusLabel }}
      </Badge>
    </div>

    <div v-if="isLoading" class="skeleton-grid" role="status" aria-label="正在加载监控数据">
      <Skeleton height="7rem" :decorative="false" label="正在加载监控数据" />
      <div class="skeleton-cards-row">
        <Skeleton v-for="index in 3" :key="index" height="9rem" />
      </div>
    </div>

    <ErrorState
      v-else-if="isError && !monitoringData"
      title="监控数据加载失败"
      :description="errorMessage(error)"
      @retry="refetch()"
    />

    <div v-else class="dashboard-panels-layout">
      <Alert v-if="isError" tone="warning" title="实时刷新失败，保留上次监控快照">
        {{ errorMessage(error) }}
      </Alert>

      <Card class="panel-section dispatcher-panel" padding="none">
        <div class="panel-card-header">
          <h3>调度器状态 (Dispatcher Status)</h3>
          <Badge :tone="dispatcherStatusTone" size="sm" dot>
            {{ dispatcherStatusLabel }}
          </Badge>
        </div>
        <div class="dispatcher-details-grid">
          <div class="detail-item">
            <span class="item-lbl">上次循环耗时</span>
            <span class="item-val mono">{{ dispatcher?.last_loop_duration_ms || 0 }} ms</span>
          </div>
          <div class="detail-item">
            <span class="item-lbl">活动项目总数</span>
            <span class="item-val mono">{{ dispatcher?.active_project_count || 0 }}</span>
          </div>
          <div class="detail-item">
            <span class="item-lbl">当前运行任务</span>
            <span class="item-val mono text-info">{{ dispatcher?.running_task_count || 0 }}</span>
          </div>
          <div class="detail-item">
            <span class="item-lbl">调度器启动时间</span>
            <span class="item-val mono">{{ formatDateTime(dispatcher?.started_at) }}</span>
          </div>
          <div class="detail-item">
            <span class="item-lbl">配置版本 (Config Hash)</span>
            <span class="item-val mono text-truncate" :title="dispatcher?.config_hash || ''">
              {{ dispatcher?.config_hash ? dispatcher.config_hash.slice(0, 12) : '—' }}
            </span>
          </div>
        </div>
        <Alert
          v-if="dispatcher?.last_error_message"
          class="dispatcher-error-block"
          tone="danger"
          title="异常日志"
        >
          {{ dispatcher.last_error_message }}
        </Alert>
      </Card>

      <Card class="panel-section" padding="none">
        <div class="panel-card-header">
          <h3>Worker 健康与任务统计</h3>
          <div class="panel-heading-actions">
            <Badge tone="neutral" size="sm">{{ workerSummaryBadge }}</Badge>
            <Checkbox
              v-if="historicalWorkerCount > 0"
              v-model="showHistoricalWorkers"
              :label="`显示历史 (${historicalWorkerCount})`"
              title="改完配置后被移除的 worker 会以历史记录保留，可在此查看"
            />
          </div>
        </div>
        <div v-if="visibleWorkers.length > 0" class="workers-grid">
          <Card
            v-for="worker in visibleWorkers"
            :key="worker.worker_name"
            class="worker-card"
            padding="sm"
          >
            <div class="card-top">
              <span class="w-name mono">{{ worker.worker_name }}</span>
              <StatusBadge
                :status="worker.health"
                size="sm"
                :title="workerHealthTitle(worker.health)"
              />
            </div>
            <div class="card-middle mono">
              <div class="w-stat">
                <span class="num">{{ worker.running_count }}/{{ worker.max_running || 2 }}</span>
                <span class="lbl">并发度</span>
              </div>
              <div class="w-stat text-success">
                <span class="num">{{ worker.succeeded_count }}</span
                ><span class="lbl">成功</span>
              </div>
              <div class="w-stat text-danger">
                <span class="num">{{ worker.failed_count }}</span
                ><span class="lbl">失败</span>
              </div>
            </div>
            <div class="card-footer">
              <div class="worker-meta-row">
                <span class="text-faint">类型: {{ worker.worker_type }}</span>
                <Badge :tone="worker.configured ? 'success' : 'neutral'" size="sm">
                  {{ worker.configured ? '已配置' : '历史记录' }}
                </Badge>
              </div>
              <span class="text-faint"
                >最近任务开始: {{ formatDateTime(worker.last_started_at) }}</span
              >
              <span v-if="worker.latest_healthcheck_phase" class="w-hcheck text-faint">
                最近 HC: {{ formatDateTime(worker.latest_healthcheck_at) }} ·
                {{ worker.latest_healthcheck_phase }} ·
                {{ worker.latest_healthcheck_duration_seconds }}s
              </span>
            </div>
          </Card>
        </div>
        <EmptyState
          v-else
          title="暂无 Worker"
          description="当前没有可展示的 Worker 配置或历史记录。"
        />
      </Card>

      <section class="grid-2cols">
        <Card class="sub-panel" padding="none">
          <div class="panel-card-header"><h3>任务拒绝日志 (Executor Rejections)</h3></div>
          <div class="sub-body overflow-auto">
            <div v-if="rejections.length > 0" class="rejections-list">
              <div
                v-for="(rejection, index) in rejections"
                :key="rejectionKey(rejection, index)"
                class="rejection-row mono"
              >
                <span class="text-warn">{{ rejection.worker_name }}</span>
                <span class="text-faint">拒绝原因: {{ rejectionReason(rejection) }}</span>
              </div>
            </div>
            <EmptyState v-else title="暂无任务拒绝" description="当前无被执行器拒绝的任务。" />
          </div>
        </Card>
        <Card class="sub-panel" padding="none">
          <div class="panel-card-header"><h3>冷却中的 Worker (Cooldowns)</h3></div>
          <div class="sub-body overflow-auto">
            <div v-if="cooldowns.length > 0" class="cooldowns-list">
              <div
                v-for="(cooldown, index) in cooldowns"
                :key="cooldownKey(cooldown, index)"
                class="cooldown-row mono"
              >
                <span class="text-danger">{{ cooldown.worker_name }}</span>
                <span class="text-faint">冷却倒计时: {{ cooldownTimerText(cooldown) }}</span>
              </div>
            </div>
            <EmptyState v-else title="无已搁置冷却" description="所有执行节点均处于活跃状态。" />
          </div>
        </Card>
      </section>

      <Card class="panel-section" padding="none">
        <div class="panel-card-header">
          <h3>活跃任务调度表 (Active Runs Queue)</h3>
          <Spinner v-if="isFetching" size="sm" label="正在实时刷新活跃任务" />
        </div>
        <DataTable
          v-if="allActiveTasks.length > 0"
          :rows="allActiveTasks"
          :columns="taskColumns"
          :row-key="monitoringTaskKey"
          caption="活跃任务调度队列"
          density="compact"
        >
          <template #cell-id="{ row }">
            <Button variant="link" size="sm" class="mono" @click="goToRun(row.project_id, row.id)"
              >#{{ row.id.slice(0, 8) }}</Button
            >
          </template>
          <template #cell-project="{ row }">{{ row.project_title }}</template>
          <template #cell-type="{ row }"
            ><span class="mono text-info">{{ row.task_type }}</span></template
          >
          <template #cell-status="{ row }">
            <div class="task-status-cell">
              <StatusBadge :status="row.status" size="sm" />
              <Badge v-if="row.is_long_running" tone="warning" size="sm">长时间运行</Badge>
            </div>
          </template>
          <template #cell-worker="{ row }"
            ><span class="mono text-muted">{{ row.worker_name }}</span></template
          >
          <template #cell-duration="{ row }">
            <span class="mono text-faint">{{
              row.started_at ? `运行中 ${row.duration_seconds}s` : `排队 ${row.queue_wait_seconds}s`
            }}</span>
          </template>
          <template #cell-artifacts="{ row }">{{ row.artifact_count }} 个证据产物</template>
        </DataTable>
        <EmptyState
          v-else
          title="活跃队列为空"
          description="当前调度队列无排队或运行中的活跃任务。"
        />
      </Card>

      <section class="grid-2cols">
        <Card class="sub-panel" padding="none">
          <div class="panel-card-header"><h3>错误聚合 (Error Aggregation)</h3></div>
          <div class="sub-body overflow-auto">
            <div v-if="errorCounts.length > 0" class="error-aggregates">
              <div v-for="item in errorCounts" :key="item.error_type" class="error-aggregate-row">
                <div class="error-title-column">
                  <span class="error-type mono text-danger">{{ item.error_type }}</span>
                  <Button
                    v-if="item.latest_task_run"
                    variant="link"
                    size="sm"
                    @click="goToRun(item.latest_task_run.project_id, item.latest_task_run.id)"
                  >
                    最新发生于项目「{{ item.latest_task_run.project_title }}」
                  </Button>
                </div>
                <Badge tone="danger" size="sm">{{ item.count }} 次</Badge>
              </div>
            </div>
            <EmptyState v-else title="暂无错误聚合" description="近期没有执行异常记录。" />
          </div>
        </Card>

        <Card class="sub-panel" padding="none">
          <div class="panel-card-header"><h3>资产产物审计 (Artifacts Audit Summary)</h3></div>
          <div class="sub-body overflow-auto">
            <div v-if="artifacts" class="artifacts-summary">
              <div class="meta-row">
                <span class="meta-label">证据 (Evidence) 绑定数量:</span
                ><span class="meta-value mono">{{ artifacts.evidence_count }} 项</span>
              </div>
              <div class="meta-row">
                <span class="meta-label">产物 (Deliverable) 生成数量:</span
                ><span class="meta-value mono">{{ artifacts.deliverable_count }} 项</span>
              </div>
              <div class="meta-row">
                <span class="meta-label">全局字节 (Bytes) 占用:</span
                ><span class="meta-value mono">{{ formatBytes(artifacts.total_size) }}</span>
              </div>
              <div class="meta-row">
                <span class="meta-label">写磁盘失败任务数:</span
                ><span
                  class="meta-value mono"
                  :class="{ 'text-danger': artifacts.write_failed_task_count > 0 }"
                  >{{ artifacts.write_failed_task_count }}</span
                >
              </div>
            </div>
            <EmptyState v-else title="暂无产物摘要" description="当前监控快照未返回产物统计。" />
          </div>
        </Card>
      </section>

      <Card class="panel-section" padding="none">
        <div class="panel-card-header"><h3>高风险动作钩子审计 (Hook Operations Timeline)</h3></div>
        <div class="sub-body">
          <div v-if="hookOps" class="hook-stats-grid">
            <div class="stat-box">
              <span class="num mono">{{ hookOps.total_count }}</span
              ><span class="lbl text-faint">近 {{ agentEventLimit }} 条内调用</span>
            </div>
            <div class="stat-box text-danger">
              <span class="num mono">{{ hookOps.failed_count }}</span
              ><span class="lbl text-faint">操作失败</span>
            </div>
            <div class="stat-box text-warn">
              <span class="num mono">{{ hookOps.high_risk_count }}</span
              ><span class="lbl text-faint">高风险 Hook</span>
            </div>
            <div class="stat-box text-success">
              <span class="num mono">{{ hookOps.knowledge_hit_count }}</span
              ><span class="lbl text-faint">知识库命中</span>
            </div>
          </div>
          <EmptyState
            v-else
            title="暂无 Hook 摘要"
            description="当前监控快照未返回 Hook 操作统计。"
          />
          <Alert tone="neutral">
            网络流状态诊断: 当前只读兼容模式 · 最近网络流监测已挂载 · 流 ID 自动指派审计链条 ·
            累计标准错误由错误聚合展示。
          </Alert>
        </div>
      </Card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { api } from '@/api/client'
import type {
  DispatcherWorkerCooldown,
  DispatcherWorkerRejection,
  MonitoringSummary,
} from '@/api/types'
import { DataTable, ErrorState, type DataTableColumn } from '@/components/patterns'
import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  EmptyState,
  Skeleton,
  Spinner,
  StatusBadge,
  type BadgeTone,
} from '@/components/ui'
import { dateTime } from '@/lib/format'
import {
  activeMonitoringTasks,
  dispatcherEffectiveStatus,
  formatMonitoringBytes,
  monitoringTaskKey,
  type ActiveMonitoringTaskRun,
} from './monitoringModel'

const router = useRouter()
const showHistoricalWorkers = ref(false)

const {
  data: monitoringData,
  error,
  isLoading,
  isError,
  isFetching,
  refetch,
} = useQuery<MonitoringSummary>({
  queryKey: ['monitoringSummary', 'monitoring'],
  queryFn: () => api<MonitoringSummary>('GET', '/admin/monitoring?profile=monitoring'),
  refetchInterval: 8000,
})

const dispatcher = computed(() => monitoringData.value?.dispatcher || null)
const workers = computed(() => monitoringData.value?.workers || [])
const configuredWorkerCount = computed(
  () => workers.value.filter((worker) => worker.configured).length,
)
const historicalWorkerCount = computed(() =>
  Math.max(0, workers.value.length - configuredWorkerCount.value),
)
const visibleWorkers = computed(() =>
  showHistoricalWorkers.value ? workers.value : workers.value.filter((worker) => worker.configured),
)
const errorCounts = computed(() => monitoringData.value?.error_counts || [])
const artifacts = computed(() => monitoringData.value?.artifacts || null)
const hookOps = computed(() => monitoringData.value?.agent_events?.hook_operation || null)
const agentEventLimit = computed(
  () => monitoringData.value?.agent_events?.recent_event_limit || 200,
)
const rejections = computed(() => dispatcher.value?.worker_rejections || [])
const cooldowns = computed(() => dispatcher.value?.worker_cooldowns || [])

const dispatcherStatusLabel = computed(() => {
  const status = dispatcherEffectiveStatus(dispatcher.value)
  if (status === 'running') return '活跃循环'
  if (status === 'starting') return '启动中'
  if (status === 'stopping') return '停止中'
  if (status === 'idle') return '空闲'
  if (status === 'stale') return '心跳过期'
  if (status === 'error') return '异常'
  return '未连接'
})
const dispatcherStatusTone = computed<BadgeTone>(() => {
  const status = dispatcherEffectiveStatus(dispatcher.value)
  if (status === 'running') return 'success'
  if (status === 'starting') return 'info'
  if (status === 'stopping') return 'warning'
  if (status === 'stale' || status === 'error') return 'danger'
  return 'neutral'
})
const refreshStatusLabel = computed(() => (isError.value ? '刷新异常' : '实时刷新'))
const refreshStatusTone = computed<BadgeTone>(() => {
  if (isError.value) return 'danger'
  if (isFetching.value) return 'info'
  const status = dispatcherEffectiveStatus(dispatcher.value)
  if (status === 'running') return 'success'
  if (status === 'stale' || status === 'error') return 'danger'
  return 'neutral'
})
const refreshStatusTitle = computed(() => {
  if (isError.value) return '实时刷新失败'
  const age = dispatcher.value?.heartbeat_age_seconds
  return age == null ? '实时刷新' : `实时刷新 · 调度器心跳 ${age}s 前`
})
const workerSummaryBadge = computed(() =>
  historicalWorkerCount.value > 0
    ? `${configuredWorkerCount.value} 个已配置 · ${historicalWorkerCount.value} 个历史`
    : `${configuredWorkerCount.value} 个已配置`,
)
const allActiveTasks = computed(() =>
  activeMonitoringTasks({
    running: monitoringData.value?.running_tasks || [],
    longRunning: monitoringData.value?.long_running_tasks || [],
    queued: monitoringData.value?.queued_tasks || [],
  }),
)

type TaskColumnKey = 'id' | 'project' | 'type' | 'status' | 'worker' | 'duration' | 'artifacts'
const taskColumns = [
  { key: 'id', label: '任务 ID' },
  { key: 'project', label: '项目标题' },
  { key: 'type', label: '类别' },
  { key: 'status', label: '状态' },
  { key: 'worker', label: '分配的 Worker' },
  { key: 'duration', label: '排队/执行时间' },
  { key: 'artifacts', label: '证据/产物' },
] satisfies readonly DataTableColumn<ActiveMonitoringTaskRun, TaskColumnKey>[]

const errorMessage = (value: unknown): string =>
  value instanceof Error && value.message
    ? value.message
    : String(value || '无法连接至平台审计监控服务，请重试。')
const formatDateTime = (value?: string | null): string => dateTime(value) || '—'
const workerHealthTitle = (health: string): string => {
  if (health === 'healthy') return '最近健康检查通过'
  if (health === 'running') return '当前有任务运行'
  if (health === 'unhealthy') return '最近健康检查异常'
  if (health === 'idle') return '当前没有任务运行'
  return '状态未知'
}
const goToRun = (projectId: string, runId: string) => {
  void router.push({
    name: 'WorkspaceRuns',
    params: { id: projectId },
    query: { runId },
  })
}
const rejectionReason = (rejection: DispatcherWorkerRejection): string =>
  rejection.error_type || (rejection.retry_after_seconds > 0 ? '并发限额' : '永久拒绝')
const rejectionKey = (rejection: DispatcherWorkerRejection, index: number): string =>
  `${rejection.project_id}/${rejection.task_type}/${rejection.worker_name}/${index}`
const cooldownTimerText = (cooldown: DispatcherWorkerCooldown): string =>
  cooldown.retry_after_seconds > 0 ? `${cooldown.retry_after_seconds}s` : '无自动重试窗口'
const cooldownKey = (cooldown: DispatcherWorkerCooldown, index: number): string =>
  `${cooldown.worker_name}/${cooldown.worker_type || 'unknown'}/${index}`
const formatBytes = formatMonitoringBytes
</script>

<style src="./Monitoring.css" scoped></style>
