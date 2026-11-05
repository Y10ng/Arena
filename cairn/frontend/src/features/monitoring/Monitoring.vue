<template>
  <div class="monitoring-container">
    <header class="monitoring-header">
      <h1 class="sr-only">运行监控</h1>
      <div class="monitoring-refresh">
        <span v-if="monitoringData" class="monitoring-snapshot"
          >快照时间
          <time :datetime="monitoringData.generated_at">{{
            formatDateTime(monitoringData.generated_at)
          }}</time></span
        >
        <Badge :tone="refreshStatusTone" size="sm">{{ refreshStatusLabel }}</Badge>
        <Button variant="secondary" :disabled="isFetching" @click="refetch()">
          <template #icon><RefreshCw :size="16" /></template>{{ isFetching ? '更新中' : '刷新' }}
        </Button>
      </div>
    </header>

    <div
      v-if="isLoading && !monitoringData"
      class="monitoring-loading"
      role="status"
      aria-label="正在加载监控数据"
    >
      <Skeleton height="7rem" :decorative="false" label="正在加载监控数据" />
      <Skeleton height="15rem" />
    </div>
    <ErrorState
      v-else-if="isError && !monitoringData"
      title="监控数据加载失败"
      :description="errorMessage(error)"
      @retry="refetch()"
    />

    <div v-else-if="monitoringData" class="monitoring-content">
      <Alert v-if="isError" tone="warning" title="实时刷新失败，保留上次监控快照">{{
        errorMessage(error)
      }}</Alert>

      <section
        class="monitoring-section monitoring-dispatcher"
        aria-labelledby="monitoring-dispatcher-title"
      >
        <div class="monitoring-section-heading">
          <div class="monitoring-heading-label">
            <h2 id="monitoring-dispatcher-title">Dispatcher</h2>
            <Badge :tone="dispatcherStatusTone" size="sm" dot>{{ dispatcherStatusLabel }}</Badge>
          </div>
          <span class="monitoring-secondary"
            >心跳
            {{
              dispatcher?.heartbeat_age_seconds == null
                ? '未知'
                : `${dispatcher.heartbeat_age_seconds} 秒前`
            }}</span
          >
        </div>
        <dl class="monitoring-metadata">
          <div>
            <dt>上次循环耗时</dt>
            <dd>
              {{
                dispatcher?.last_loop_duration_ms == null
                  ? '未知'
                  : `${dispatcher.last_loop_duration_ms} ms`
              }}
            </dd>
          </div>
          <div>
            <dt>活动项目</dt>
            <dd>{{ dispatcher?.active_project_count ?? '未知' }}</dd>
          </div>
          <div>
            <dt>运行任务</dt>
            <dd>{{ dispatcher?.running_task_count ?? '未知' }}</dd>
          </div>
          <div>
            <dt>启动时间</dt>
            <dd>{{ formatDateTime(dispatcher?.started_at) }}</dd>
          </div>
          <div class="monitoring-config">
            <dt>配置版本</dt>
            <dd>
              <code>{{ dispatcher?.config_hash || '未知' }}</code>
            </dd>
          </div>
        </dl>
        <Alert
          v-if="dispatcher?.last_error_message"
          tone="danger"
          title="调度异常"
          class="monitoring-error"
          >{{ dispatcher.last_error_message }}</Alert
        >
      </section>

      <section class="monitoring-section" aria-labelledby="monitoring-workers-title">
        <div class="monitoring-section-heading">
          <div class="monitoring-heading-label">
            <h2 id="monitoring-workers-title">Worker</h2>
            <span class="monitoring-secondary">{{ workerSummaryBadge }}</span>
          </div>
          <Checkbox
            v-if="historicalWorkerCount > 0"
            v-model="showHistoricalWorkers"
            :label="`显示历史 (${historicalWorkerCount})`"
          />
        </div>
        <div v-if="visibleWorkers.length" class="monitoring-table-scroll">
          <table class="monitoring-worker-table">
            <caption class="monitoring-sr-only">
              Worker 健康与任务统计；执行成功数量不代表漏洞确认数量
            </caption>
            <colgroup>
              <col class="monitoring-worker-name-col" />
              <col class="monitoring-health-col" />
              <col class="monitoring-capacity-col" />
              <col class="monitoring-results-col" />
              <col class="monitoring-source-col" />
              <col class="monitoring-recent-col" />
            </colgroup>
            <thead>
              <tr>
                <th scope="col">名称／类型</th>
                <th scope="col">健康</th>
                <th scope="col">当前／上限</th>
                <th scope="col">执行成功／失败</th>
                <th scope="col">来源</th>
                <th scope="col">最近任务／健康检查</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="worker in visibleWorkers" :key="worker.worker_name">
                <td>
                  <details class="monitoring-worker-identity">
                    <summary>
                      <strong class="monitoring-worker-name" :title="worker.worker_name">{{ worker.worker_name }}</strong>
                      <span class="monitoring-cell-detail">{{ worker.worker_type }}</span>
                      <ChevronDown :size="14" aria-hidden="true" />
                    </summary>
                    <dl class="monitoring-worker-details">
                      <div><dt>名称</dt><dd>{{ worker.worker_name }}</dd></div>
                      <div><dt>类型</dt><dd>{{ worker.worker_type }}</dd></div>
                      <div><dt>最近任务</dt><dd>{{ formatDateTime(worker.last_started_at) }}</dd></div>
                      <div><dt>健康检查</dt><dd>{{ formatDateTime(worker.latest_healthcheck_at) }}</dd></div>
                      <div v-if="worker.latest_healthcheck_phase"><dt>检查阶段</dt><dd>{{ worker.latest_healthcheck_phase }}</dd></div>
                      <div v-if="worker.latest_healthcheck_duration_seconds != null"><dt>检查耗时</dt><dd>{{ worker.latest_healthcheck_duration_seconds }}s</dd></div>
                    </dl>
                  </details>
                </td>
                <td>
                  <StatusBadge
                    :status="worker.health"
                    size="sm"
                    :title="workerHealthTitle(worker.health)"
                  />
                </td>
                <td class="monitoring-numeric">
                  {{ worker.running_count }} / {{ worker.max_running ?? '未知' }}
                </td>
                <td class="monitoring-numeric">
                  {{ worker.succeeded_count }} / {{ worker.failed_count }}
                </td>
                <td>
                  <span>{{ worker.configured ? '已配置' : '历史记录' }}</span>
                </td>
                <td>
                  <div class="monitoring-worker-recent">
                    <span :title="formatDateTime(worker.last_started_at)">{{ formatDateTime(worker.last_started_at) }}</span>
                    <span class="monitoring-cell-detail"
                      >健康检查：{{ formatDateTime(worker.latest_healthcheck_at)
                      }}<template v-if="worker.latest_healthcheck_phase">
                        · {{ worker.latest_healthcheck_phase }}</template
                      ><template v-if="worker.latest_healthcheck_duration_seconds != null">
                        · {{ worker.latest_healthcheck_duration_seconds }}s</template
                      ></span
                    >
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <EmptyState
          v-else
          title="暂无 Worker"
          :description="
            historicalWorkerCount > 0
              ? '当前筛选下没有 Worker，可勾选“显示历史”查看已有记录。'
              : undefined
          "
        />
      </section>

      <section class="monitoring-section" aria-labelledby="monitoring-tasks-title">
        <div class="monitoring-section-heading">
          <h2 id="monitoring-tasks-title">活跃任务</h2>
          <span class="monitoring-secondary">{{ allActiveTasks.length }} 项</span>
        </div>
        <DataTable
          v-if="allActiveTasks.length"
          :rows="allActiveTasks"
          :columns="taskColumns"
          :row-key="monitoringTaskKey"
          caption="活跃任务调度队列"
        >
          <template #cell-id="{ row }"
            ><Button
              variant="link"
              size="sm"
              class="monitoring-run-link"
              @click="goToRun(row.project_id, row.id)"
              >#{{ row.id.slice(0, 8) }}</Button
            ></template
          >
          <template #cell-project="{ row }">{{ row.project_title }}</template>
          <template #cell-type="{ row }"
            ><code>{{ row.task_type }}</code></template
          >
          <template #cell-status="{ row }"
            ><div class="monitoring-task-status">
              <StatusBadge :status="row.status" size="sm" /><Badge
                v-if="row.is_long_running"
                tone="warning"
                size="sm"
                >长时间运行</Badge
              >
            </div></template
          >
          <template #cell-worker="{ row }"
            ><span>{{ row.worker_name }}</span></template
          >
          <template #cell-duration="{ row }"
            ><span class="monitoring-numeric">{{
              row.started_at
                ? `运行中 ${row.duration_seconds == null ? '未知' : `${row.duration_seconds}s`}`
                : `排队 ${row.queue_wait_seconds == null ? '未知' : `${row.queue_wait_seconds}s`}`
            }}</span></template
          >
          <template #cell-artifacts="{ row }">{{ row.artifact_count }} 项</template>
        </DataTable>
        <EmptyState v-else title="活跃队列为空" />
      </section>

      <section class="monitoring-section" aria-labelledby="monitoring-diagnostics-title">
        <div class="monitoring-section-heading">
          <h2 id="monitoring-diagnostics-title">诊断与审计</h2>
        </div>
        <details class="monitoring-disclosure">
          <summary>
            <span>任务拒绝</span
            ><span class="monitoring-secondary">{{ rejections.length }} 项</span>
          </summary>
          <div class="monitoring-disclosure-body">
            <ul v-if="rejections.length" class="monitoring-diagnostic-list">
              <li v-for="(rejection, index) in rejections" :key="rejectionKey(rejection, index)">
                <strong>{{ rejection.worker_name }}</strong
                ><code>{{ rejectionReason(rejection) }}</code
                ><span>项目 {{ rejection.project_id }} · {{ rejection.task_type }}</span
                ><span
                  >{{
                    rejection.retry_after_seconds > 0
                      ? `${rejection.retry_after_seconds}s 后可重试`
                      : '无自动重试窗口'
                  }}<template v-if="rejection.retry_until_at">
                    · {{ formatDateTime(rejection.retry_until_at) }}</template
                  ></span
                >
              </li>
            </ul>
            <p v-else class="monitoring-empty-note">当前快照没有任务拒绝记录。</p>
          </div>
        </details>
        <details class="monitoring-disclosure">
          <summary>
            <span>Worker 冷却</span
            ><span class="monitoring-secondary">{{ cooldowns.length }} 项</span>
          </summary>
          <div class="monitoring-disclosure-body">
            <ul v-if="cooldowns.length" class="monitoring-diagnostic-list">
              <li v-for="(cooldown, index) in cooldowns" :key="cooldownKey(cooldown, index)">
                <strong>{{ cooldown.worker_name }}</strong
                ><code>{{ cooldown.error_type }}</code
                ><span
                  >{{ cooldownTimerText(cooldown)
                  }}<template v-if="cooldown.retry_until_at">
                    · {{ formatDateTime(cooldown.retry_until_at) }}</template
                  ></span
                >
              </li>
            </ul>
            <p v-else class="monitoring-empty-note">当前快照没有冷却记录。</p>
          </div>
        </details>
        <details class="monitoring-disclosure">
          <summary>
            <span>错误聚合</span
            ><span class="monitoring-secondary">{{ errorCounts.length }} 类</span>
          </summary>
          <div class="monitoring-disclosure-body">
            <ul v-if="errorCounts.length" class="monitoring-diagnostic-list">
              <li v-for="item in errorCounts" :key="item.error_type">
                <code>{{ item.error_type }}</code
                ><span>{{ item.count }} 次</span
                ><Button
                  v-if="item.latest_task_run"
                  variant="link"
                  size="sm"
                  @click="goToRun(item.latest_task_run.project_id, item.latest_task_run.id)"
                  >最新发生于项目「{{ item.latest_task_run.project_title }}」</Button
                >
              </li>
            </ul>
            <p v-else class="monitoring-empty-note">当前快照没有错误聚合记录。</p>
          </div>
        </details>
        <details class="monitoring-disclosure">
          <summary>
            <span>产物审计</span
            ><span class="monitoring-secondary">{{
              artifacts
                ? `${artifacts.evidence_count} 项证据 · ${artifacts.deliverable_count} 项交付物`
                : '未返回摘要'
            }}</span>
          </summary>
          <div class="monitoring-disclosure-body">
            <dl v-if="artifacts" class="monitoring-audit-metadata">
              <div>
                <dt>证据绑定</dt>
                <dd>{{ artifacts.evidence_count }} 项</dd>
              </div>
              <div>
                <dt>交付物生成</dt>
                <dd>{{ artifacts.deliverable_count }} 项</dd>
              </div>
              <div>
                <dt>总占用</dt>
                <dd>{{ formatBytes(artifacts.total_size) }}</dd>
              </div>
              <div>
                <dt>写入失败任务</dt>
                <dd>{{ artifacts.write_failed_task_count }}</dd>
              </div>
            </dl>
            <p v-else class="monitoring-empty-note">当前监控快照未返回产物统计。</p>
          </div>
        </details>
        <details class="monitoring-disclosure">
          <summary>
            <span>Hook 操作审计</span
            ><span class="monitoring-secondary">{{
              hookOps ? `${hookOps.total_count} 次调用` : '未返回摘要'
            }}</span>
          </summary>
          <div class="monitoring-disclosure-body">
            <dl v-if="hookOps" class="monitoring-audit-metadata">
              <div>
                <dt>近 {{ agentEventLimit }} 条内调用</dt>
                <dd>{{ hookOps.total_count }}</dd>
              </div>
              <div>
                <dt>操作失败</dt>
                <dd>{{ hookOps.failed_count }}</dd>
              </div>
              <div>
                <dt>高风险 Hook</dt>
                <dd>{{ hookOps.high_risk_count }}</dd>
              </div>
              <div>
                <dt>知识库命中</dt>
                <dd>{{ hookOps.knowledge_hit_count }}</dd>
              </div>
            </dl>
            <p v-else class="monitoring-empty-note">当前监控快照未返回 Hook 操作统计。</p>
          </div>
        </details>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ChevronDown, RefreshCw } from '@lucide/vue'
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
  Checkbox,
  EmptyState,
  Skeleton,
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
  () => monitoringData.value?.agent_events?.recent_event_limit ?? '未知',
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
const refreshStatusLabel = computed(() =>
  isError.value
    ? '刷新异常'
    : isFetching.value
      ? '正在更新'
      : monitoringData.value
        ? '每 8 秒刷新'
        : '等待快照',
)
const refreshStatusTone = computed<BadgeTone>(() => (isError.value ? 'danger' : 'neutral'))
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
  { key: 'id', label: '任务 ID', width: 130 },
  { key: 'project', label: '所属项目' },
  { key: 'type', label: '类别', width: 150 },
  { key: 'status', label: '状态', width: 200 },
  { key: 'worker', label: 'Worker', width: 190 },
  { key: 'duration', label: '排队／执行时间', width: 160 },
  { key: 'artifacts', label: '证据／产物', width: 120 },
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
