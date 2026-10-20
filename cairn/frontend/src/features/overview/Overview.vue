<template>
  <div class="overview-container console-page">
    <!-- Empty State -->
    <div v-if="!isLoading && projects.length === 0" class="empty-state-container">
      <EmptyState
        class="overview-empty-state"
        title="欢迎使用 Arena"
        description="这是一个基于事实图谱的安全自治探索控制台。您当前还没有创建任何项目。"
      >
        <template #icon>
          <svg
            width="48"
            height="48"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
          </svg>
        </template>
        <template #actions>
          <Button @click="$router.push('/projects')">前往任务管理</Button>
        </template>
      </EmptyState>
    </div>

    <div v-else-if="isLoading" class="loading-state-container">
      <Alert class="loading-state" tone="neutral">
        <Spinner size="sm" decorative />
        正在加载概览数据...
      </Alert>
    </div>

    <!-- Loaded Dashboard -->
    <div v-else class="dashboard-grid">
      <!-- 概览只展示服务可用性，不暴露管理员全局 dispatcher 诊断字段。 -->
      <Alert v-if="dispatcherLiveness" class="dispatcher-strip" :tone="dispatcherAlertTone">
        <span class="ds-label">任务服务</span>
        <Badge :tone="dispatcherAlertTone" size="sm" dot>
          <strong class="ds-status">{{ dispatcherStatusLabel }}</strong>
        </Badge>
        <span v-if="dispatcherLiveness.updated_at" class="ds-meta mono">
          · 更新于 {{ dispatcherUpdatedTimeText }}
        </span>
      </Alert>

      <!-- KPI Row -->
      <section class="kpi-row">
        <!-- Stat Tile 1: Active Projects -->
        <Card as="button" type="button" class="stat-tile" @click="$router.push('/projects')">
          <div class="stat-header">
            <span class="stat-label">活跃项目数</span>
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              class="tile-icon"
            >
              <path
                d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"
              ></path>
            </svg>
          </div>
          <div class="stat-value">{{ activeProjectCount }}</div>
          <div class="stat-sub">总项目 {{ projects.length }} 个</div>
        </Card>

        <!-- Stat Tile 2: Running Tasks -->
        <Card as="button" type="button" class="stat-tile" @click="$router.push('/projects')">
          <div class="stat-header">
            <span class="stat-label">运行中任务</span>
            <div class="live-indicator-container">
              <span class="live-dot live-success animate-pulse-live"></span>
            </div>
          </div>
          <div class="stat-value">{{ runningTaskCount }}</div>
          <div class="stat-sub">执行列表繁忙度</div>
        </Card>

        <!-- Stat Tile 3: Queued Tasks -->
        <Card as="button" type="button" class="stat-tile" @click="$router.push('/projects')">
          <div class="stat-header">
            <span class="stat-label">等待队列</span>
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              class="tile-icon"
            >
              <path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" />
            </svg>
          </div>
          <div class="stat-value">{{ queuedTaskCount }}</div>
          <div class="stat-sub">等待执行的 Intent 数</div>
        </Card>

        <!-- Stat Tile 4: Failure Rate -->
        <Card as="div" class="stat-tile">
          <div class="stat-header">
            <span class="stat-label">最近失败次数</span>
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              class="tile-icon text-danger"
            >
              <polygon
                points="7.86 2 16.14 2 22 7.86 22 16.14 16.14 22 7.86 22 2 16.14 2 7.86 7.86 2"
              ></polygon>
              <line x1="12" y1="8" x2="12" y2="12"></line>
              <line x1="12" y1="16" x2="12.01" y2="16"></line>
            </svg>
          </div>
          <div class="stat-value" :class="{ 'text-danger': recentFailuresCount > 0 }">
            {{ recentFailuresCount }}
          </div>
          <div class="stat-sub">
            <svg width="40" height="12" class="mini-sparkline">
              <polyline
                fill="none"
                stroke="var(--danger)"
                stroke-width="1.5"
                points="0,10 8,8 16,10 24,4 32,10 40,2"
              ></polyline>
            </svg>
            <span>近期执行轨迹波动</span>
          </div>
        </Card>

        <!-- Stat Tile 5: Vulnerabilities -->
        <Card as="button" type="button" class="stat-tile" @click="$router.push('/projects')">
          <div class="stat-header">
            <span class="stat-label">漏洞总数</span>
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              class="tile-icon text-danger"
            >
              <path
                d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"
              ></path>
              <line x1="12" y1="9" x2="12" y2="13"></line>
              <line x1="12" y1="17" x2="12.01" y2="17"></line>
            </svg>
          </div>
          <div class="stat-value" :class="{ 'text-danger': vulnElevatedCount > 0 }">
            {{ vulnTotalCount }}
          </div>
          <div class="stat-sub">
            <span v-if="vulnElevatedCount > 0" class="text-danger"
              >严重/高危 {{ vulnElevatedCount }} ·
            </span>
            <span>严重度分布</span>
          </div>
        </Card>
      </section>

      <!-- Vulnerability Severity Distribution -->
      <Card padding="none" class="panel-card vuln-dist-card">
        <div class="card-header">
          <h3>漏洞严重度分布</h3>
          <Badge :tone="vulnTotalCount > 0 ? 'danger' : 'neutral'" size="sm">
            共 {{ vulnTotalCount }} 个
          </Badge>
        </div>
        <EmptyState
          v-if="vulnSeverityChart.total === 0"
          class="vuln-dist-empty"
          title="暂无漏洞记录"
        />
        <figure v-else class="vuln-chart" @keydown.esc.stop="clearSeveritySelection">
          <div class="vuln-chart-graphic" @mouseleave="clearHoveredSeverity">
            <svg
              class="vuln-donut"
              :class="{ 'has-active-segment': activeSeveritySegment }"
              viewBox="0 0 120 120"
              role="group"
              aria-labelledby="vuln-chart-title vuln-chart-desc"
            >
              <title id="vuln-chart-title">漏洞严重度分布</title>
              <desc id="vuln-chart-desc">{{ vulnChartDescription }}</desc>
              <defs aria-hidden="true">
                <pattern
                  id="vuln-pattern-critical"
                  width="6"
                  height="6"
                  patternUnits="userSpaceOnUse"
                >
                  <path class="vuln-pattern-line" d="M0 0 6 6 M6 0 0 6" />
                </pattern>
                <pattern id="vuln-pattern-high" width="6" height="6" patternUnits="userSpaceOnUse">
                  <path class="vuln-pattern-line" d="M-1 1 1 -1 M0 6 6 0 M5 7 7 5" />
                </pattern>
                <pattern
                  id="vuln-pattern-medium"
                  width="6"
                  height="6"
                  patternUnits="userSpaceOnUse"
                >
                  <path class="vuln-pattern-line" d="M-1 5 1 7 M0 0 6 6 M5 -1 7 1" />
                </pattern>
                <pattern id="vuln-pattern-low" width="6" height="6" patternUnits="userSpaceOnUse">
                  <path class="vuln-pattern-line" d="M0 2 6 2" />
                </pattern>
                <pattern id="vuln-pattern-info" width="6" height="6" patternUnits="userSpaceOnUse">
                  <circle class="vuln-pattern-dot" cx="3" cy="3" r="1" />
                </pattern>
              </defs>
              <path
                v-for="segment in vulnSeverityChart.segments"
                :key="segment.key"
                :d="segment.path"
                :class="[
                  `vuln-segment vuln-segment-${segment.key}`,
                  { 'is-active': activeSeveritySegment?.key === segment.key },
                ]"
                :stroke-width="vulnSegmentStrokeWidth"
                :style="severitySegmentStyle(segment)"
                vector-effect="non-scaling-stroke"
                role="button"
                tabindex="0"
                :aria-label="severitySegmentLabel(segment)"
                :aria-pressed="pinnedSeverityKey === segment.key"
                :aria-describedby="
                  activeSeveritySegment?.key === segment.key ? 'vuln-chart-tooltip' : undefined
                "
                @mouseenter="setHoveredSeverity(segment.key)"
                @focus="setFocusedSeverity(segment.key)"
                @blur="clearFocusedSeverity(segment.key)"
                @click="togglePinnedSeverity(segment.key)"
                @keydown.enter.prevent="togglePinnedSeverity(segment.key)"
                @keydown.space.prevent="togglePinnedSeverity(segment.key)"
              />
            </svg>
            <div class="vuln-chart-center" aria-live="polite" aria-atomic="true">
              <strong>{{ activeSeveritySegment?.count ?? vulnSeverityChart.total }}</strong>
              <span>{{ activeSeveritySegment?.label ?? '漏洞总数' }}</span>
            </div>
            <div
              v-if="activeSeveritySegment"
              id="vuln-chart-tooltip"
              class="vuln-chart-tooltip"
              role="tooltip"
              :style="activeSeverityTooltipStyle"
            >
              <strong>{{ activeSeveritySegment.count }} 个</strong>
              <span
                >{{ activeSeveritySegment.label }} · {{ activeSeveritySegment.percentLabel }}</span
              >
            </div>
          </div>
          <figcaption class="vuln-chart-legend" aria-label="漏洞严重度明细">
            <button
              v-for="segment in vulnSeverityChart.segments"
              :key="segment.key"
              type="button"
              class="vuln-legend-row"
              :class="{ 'is-active': activeSeveritySegment?.key === segment.key }"
              :aria-pressed="pinnedSeverityKey === segment.key"
              @mouseenter="setHoveredSeverity(segment.key)"
              @mouseleave="clearHoveredSeverity"
              @focus="setFocusedSeverity(segment.key)"
              @blur="clearFocusedSeverity(segment.key)"
              @click="togglePinnedSeverity(segment.key)"
            >
              <span
                class="vuln-legend-swatch"
                :class="`vuln-swatch-${segment.key}`"
                aria-hidden="true"
              ></span>
              <span class="vuln-legend-label">{{ segment.label }}</span>
              <strong class="vuln-legend-count">{{ segment.count }}</strong>
              <span class="vuln-legend-percent">{{ segment.percentLabel }}</span>
            </button>
          </figcaption>
        </figure>
      </Card>

      <!-- 普通用户查看 Tenant 任务，超级管理员查看全局任务；两者都不展示底层诊断。 -->
      <section>
        <!-- Active Runs Activity Log -->
        <Card padding="none" class="panel-card flex-col">
          <div class="card-header">
            <h3>近期执行日志 (TaskRuns)</h3>
            <Button variant="link" size="sm" @click="$router.push('/projects')">全部项目</Button>
          </div>
          <div class="card-body overflow-auto flex-grow">
            <div class="activity-stream" v-if="recentRuns.length > 0">
              <button
                v-for="run in recentRuns"
                :key="monitoringTaskKey(run)"
                type="button"
                class="activity-row"
                @click="goToRun(run.project_id, run.id)"
              >
                <div class="run-project-info">
                  <span class="project-title-label">{{ run.project_title }}</span>
                  <Badge class="task-type-badge mono" tone="info" size="sm">
                    {{ run.task_type }}
                  </Badge>
                </div>
                <div class="run-execution-info">
                  <span v-if="run.worker_name" class="worker-name-label mono">{{
                    run.worker_name
                  }}</span>
                  <span class="duration-label mono">{{
                    run.duration_seconds ? run.duration_seconds + 's' : '—'
                  }}</span>
                </div>
                <div class="run-status">
                  <StatusBadge :status="run.status" size="sm">
                    {{ runStatusText(run.status) }}
                  </StatusBadge>
                </div>
              </button>
            </div>
            <EmptyState v-else class="empty-placeholder" title="暂无近期任务执行日志" />
          </div>
        </Card>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { CSSProperties } from 'vue'
import { useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { api } from '@/api/client'
import type {
  MonitoringSummary,
  MonitoringTaskRun,
  ProjectSummary,
  TenantMonitoringSummary,
  TenantMonitoringTaskRun,
} from '@/api/types'
import { Alert, Badge, Button, Card, EmptyState, Spinner, StatusBadge } from '@/components/ui'
import { monitoringTaskKey } from '@/features/monitoring/monitoringModel'
import { useAuthStore } from '@/stores/auth'
import {
  buildSeverityChart,
  severitySegmentAnchor,
  severitySegmentOffset,
  type SeverityChartSegment,
  type SeverityKey,
} from './overviewSeverityModel'

type OverviewMonitoringTask = TenantMonitoringTaskRun | MonitoringTaskRun

type OverviewVulnerabilitySummary = NonNullable<TenantMonitoringSummary['vulnerabilities']>

interface OverviewMonitoringData {
  dispatcher: {
    status?: string | null
    stale: boolean
    updated_at?: string | null
  }
  queued_tasks: OverviewMonitoringTask[]
  running_tasks: OverviewMonitoringTask[]
  long_running_tasks: OverviewMonitoringTask[]
  recent_failures: OverviewMonitoringTask[]
  vulnerabilities?: OverviewVulnerabilitySummary
}

defineOptions({ name: 'OverviewPage' })

const router = useRouter()
const authStore = useAuthStore()

// Query Projects list
const { data: projectsData, isLoading: isProjectsLoading } = useQuery<ProjectSummary[]>({
  queryKey: ['projects'],
  queryFn: () => api<ProjectSummary[]>('GET', '/projects'),
})
const projects = computed(() => projectsData.value || [])

// 管理员概览读取全局汇总；普通用户继续使用 Tenant 隔离的轻量协议。
const { data: rawMonitoringData, isLoading: isSummaryLoading } = useQuery<
  TenantMonitoringSummary | MonitoringSummary
>({
  queryKey: computed(() => [
    'monitoringSummary',
    'overview',
    authStore.isSuperAdmin ? 'admin' : 'tenant',
  ]),
  queryFn: () =>
    authStore.isSuperAdmin
      ? api<MonitoringSummary>('GET', '/admin/monitoring?profile=overview')
      : api<TenantMonitoringSummary>('GET', '/monitoring/summary'),
  refetchInterval: 10000,
})

const monitoringData = computed<OverviewMonitoringData | null>(() => {
  const summary = rawMonitoringData.value
  if (!summary) return null

  const dispatcher = summary.dispatcher
  return {
    dispatcher:
      'heartbeat_stale' in dispatcher
        ? {
            status: dispatcher.effective_status,
            stale: dispatcher.heartbeat_stale,
            updated_at: dispatcher.updated_at,
          }
        : dispatcher,
    queued_tasks: summary.queued_tasks,
    running_tasks: summary.running_tasks,
    long_running_tasks: summary.long_running_tasks,
    recent_failures: summary.recent_failures,
    vulnerabilities: summary.vulnerabilities,
  }
})

const isLoading = computed(() => isProjectsLoading.value && isSummaryLoading.value)

const activeProjectCount = computed(() => {
  return projects.value.filter((p) => p.status === 'active').length
})

const dispatcherLiveness = computed(() => monitoringData.value?.dispatcher || null)
const runningTaskCount = computed(() => monitoringData.value?.running_tasks.length || 0)
const queuedTaskCount = computed(() => monitoringData.value?.queued_tasks.length || 0)

const recentFailures = computed(() => monitoringData.value?.recent_failures || [])
const recentFailuresCount = computed(() => recentFailures.value.length)

// 漏洞统计（所有场景通用，来自 monitoring summary 的 vulnerabilities_summary 字段）
const vulnSummary = computed(() => monitoringData.value?.vulnerabilities || null)
const vulnSeverityChart = computed(() => buildSeverityChart(vulnSummary.value?.by_severity))
const hoveredSeverityKey = ref<SeverityKey | null>(null)
const focusedSeverityKey = ref<SeverityKey | null>(null)
const pinnedSeverityKey = ref<SeverityKey | null>(null)
const activeSeverityKey = computed(
  () => hoveredSeverityKey.value || focusedSeverityKey.value || pinnedSeverityKey.value,
)
const activeSeveritySegment = computed(
  () =>
    vulnSeverityChart.value.segments.find((segment) => segment.key === activeSeverityKey.value) ||
    null,
)
const activeSeverityTooltipStyle = computed<CSSProperties>(() => {
  if (!activeSeveritySegment.value) return {}
  const anchor = severitySegmentAnchor(activeSeveritySegment.value)
  const horizontalOffset = anchor.x > 80 ? '-100%' : anchor.x < 40 ? '0' : '-50%'
  return {
    left: `${(anchor.x / 120) * 100}%`,
    top: `${(anchor.y / 120) * 100}%`,
    transform: `translate(${horizontalOffset}, -50%)`,
  }
})
const vulnSegmentStrokeWidth = computed(() =>
  vulnSeverityChart.value.segments.some((segment) => segment.endAngle - segment.startAngle < 1)
    ? 0.25
    : 2.5,
)
const vulnTotalCount = computed(() => vulnSeverityChart.value.total)
const vulnElevatedCount = computed(
  () =>
    (vulnSummary.value?.by_severity?.critical || 0) + (vulnSummary.value?.by_severity?.high || 0),
)
const vulnChartDescription = computed(() => {
  const details = vulnSeverityChart.value.segments
    .map((segment) => `${segment.label} ${segment.count} 个，占 ${segment.percentLabel}`)
    .join('；')
  return `共 ${vulnSeverityChart.value.total} 个漏洞。${details}`
})

const severitySegmentLabel = (segment: SeverityChartSegment) =>
  `${segment.label}：${segment.count} 个，占 ${segment.percentLabel}`

const severitySegmentStyle = (segment: SeverityChartSegment): CSSProperties => {
  if (activeSeveritySegment.value?.key !== segment.key) return {}
  const offset = severitySegmentOffset(segment)
  return { transform: `translate(${offset.x}px, ${offset.y}px) scale(1.025)` }
}

const setHoveredSeverity = (key: SeverityKey) => {
  hoveredSeverityKey.value = key
}

const clearHoveredSeverity = () => {
  hoveredSeverityKey.value = null
}

const setFocusedSeverity = (key: SeverityKey) => {
  focusedSeverityKey.value = key
}

const clearFocusedSeverity = (key: SeverityKey) => {
  if (focusedSeverityKey.value === key) focusedSeverityKey.value = null
}

const togglePinnedSeverity = (key: SeverityKey) => {
  pinnedSeverityKey.value = pinnedSeverityKey.value === key ? null : key
}

const clearSeveritySelection = () => {
  hoveredSeverityKey.value = null
  focusedSeverityKey.value = null
  pinnedSeverityKey.value = null
}

// 数据刷新后若某个严重度消失，避免保留不可见的固定选择。
watch(vulnSeverityChart, (chart) => {
  const visibleKeys = new Set(chart.segments.map((segment) => segment.key))
  if (hoveredSeverityKey.value && !visibleKeys.has(hoveredSeverityKey.value)) {
    hoveredSeverityKey.value = null
  }
  if (focusedSeverityKey.value && !visibleKeys.has(focusedSeverityKey.value)) {
    focusedSeverityKey.value = null
  }
  if (pinnedSeverityKey.value && !visibleKeys.has(pinnedSeverityKey.value)) {
    pinnedSeverityKey.value = null
  }
})

// Aggregate task runs from all running & long running & failures
const recentRuns = computed(() => {
  const list = [
    ...(monitoringData.value?.running_tasks || []),
    ...(monitoringData.value?.long_running_tasks || []),
    ...(monitoringData.value?.recent_failures || []),
  ]
  const map = new Map<string, (typeof list)[number]>()
  list.forEach((item) => map.set(monitoringTaskKey(item), item))
  // Sort by started_at or queued_at descending
  return [...map.values()]
    .sort((a, b) => {
      const timeA = a.started_at || a.queued_at || ''
      const timeB = b.started_at || b.queued_at || ''
      return timeB.localeCompare(timeA)
    })
    .slice(0, 10)
})

const dispatcherEffectiveStatus = computed(() => {
  if (dispatcherLiveness.value?.stale) return 'stale'
  return dispatcherLiveness.value?.status || 'unknown'
})

const dispatcherStatusLabel = computed(() => {
  if (dispatcherEffectiveStatus.value === 'running') return '可用'
  if (dispatcherEffectiveStatus.value === 'idle') return '空闲'
  if (dispatcherEffectiveStatus.value === 'stale') return '状态过期'
  if (dispatcherEffectiveStatus.value === 'error') return '暂不可用'
  return '状态未知'
})

const dispatcherAlertTone = computed<'success' | 'danger' | 'neutral'>(() => {
  if (dispatcherEffectiveStatus.value === 'running') return 'success'
  if (dispatcherEffectiveStatus.value === 'stale' || dispatcherEffectiveStatus.value === 'error')
    return 'danger'
  return 'neutral'
})

const dispatcherUpdatedTimeText = computed(() => {
  const dateStr = dispatcherLiveness.value?.updated_at
  if (!dateStr) return '未知'
  const timestamp = Date.parse(dateStr)
  if (Number.isNaN(timestamp)) return '未知'
  return new Date(timestamp).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
})

const runStatusText = (status: string) => {
  if (status === 'running') return '运行中'
  if (status === 'queued') return '排队中'
  if (status === 'completed') return '已完成'
  if (status === 'failed') return '失败'
  if (status === 'cancelled') return '已取消'
  return status
}

const goToRun = (projectId: string, runId: string) => {
  router.push(`/projects/${projectId}/runs?runId=${runId}`)
}
</script>

<style src="./Overview.css" scoped></style>
