<template>
  <div class="runs-container">
    <!-- Left Master Column: Runs List -->
    <aside class="runs-sidebar">
      <div class="sidebar-header">
        <div class="sidebar-title-row">
          <h3>任务运行审计</h3>
        </div>
        <FilterBar label="运行列表筛选">
          <Input
            v-model="searchQuery"
            placeholder="搜索工作目的、角色、优先级、Worker 或结果..."
            aria-label="搜索运行"
          />
          <Select v-model="statusFilter" :options="statusFilterOptions" aria-label="运行状态" />
        </FilterBar>
      </div>

      <ErrorState
        v-if="runsInitialError"
        class="runs-list-state"
        title="运行列表加载失败"
        :description="errorMessage(runsError)"
        @retry="refetchRuns"
      />
      <div v-else-if="isLoadingRuns" class="runs-list-skeleton" aria-label="正在载入运行列表">
        <Skeleton
          v-for="index in 5"
          :key="index"
          height="4.25rem"
          :decorative="false"
          label="正在载入运行列表"
        />
      </div>
      <template v-else>
        <Alert
          v-if="runsStaleError"
          class="runs-stale-error"
          tone="warning"
          title="刷新失败，保留上次运行列表"
        >
          {{ errorMessage(runsError) }}
        </Alert>
        <div class="runs-list" v-if="filteredRuns.length > 0">
          <Button
            v-for="run in filteredRuns"
            :key="run.id"
            variant="ghost"
            size="sm"
            class="run-row"
            :class="{ active: selectedRunId === run.id }"
            @click="selectRun(run.id)"
          >
            <div class="row-top">
              <span class="run-business-title" :title="runBusinessTitle(run)">
                {{ runBusinessTitle(run) }}
              </span>
              <StatusBadge :status="run.status" size="sm" />
            </div>
            <div class="row-middle row-business-meta">
              <span class="run-badges">
                <Badge tone="info" size="sm">{{ runRoleLabel(run) }}</Badge>
                <Badge
                  v-if="runPriorityLabel(run)"
                  :tone="runIntent(run)?.priority === 2 ? 'warning' : 'neutral'"
                  size="sm"
                >
                  {{ runPriorityLabel(run) }}
                </Badge>
              </span>
              <span class="worker-name" :title="run.worker_name">{{ run.worker_name }}</span>
            </div>
            <p v-if="runCardDiagnostic(run)" class="row-diagnostic" :title="runCardDiagnostic(run)">
              {{ runCardDiagnostic(run) }}
            </p>
            <div class="row-bottom">
              <span class="row-technical mono text-faint">
                {{ run.task_type }} · #{{ run.id.slice(0, 8) }}
              </span>
              <span class="time text-faint">
                {{ runDurationText(run) }} · {{ formatRelativeTime(runReferenceTime(run)) }}
              </span>
            </div>
          </Button>
        </div>
        <EmptyState
          v-else
          class="runs-list-state"
          :title="filtersActive ? '当前筛选条件没有匹配记录' : '没有运行记录'"
          :description="
            filtersActive
              ? '清除筛选条件可查看全部历史任务。'
              : '项目状态请查看工作空间顶部。'
          "
        />
      </template>
    </aside>

    <!-- Right Detail Column: Selected Run Details -->
    <main class="run-details-panel" v-if="selectedRun">
      <!-- Detail Header -->
      <header class="detail-header">
        <div class="header-left-info">
          <div class="title-row">
            <StatusBadge :status="selectedRun.status" size="sm" />
            <h2 :title="runBusinessTitle(selectedRun)">{{ runBusinessTitle(selectedRun) }}</h2>
            <span class="run-detail-loading">
              <Spinner v-if="isLoadingRunDetails" size="sm" label="正在载入运行详情" />
            </span>
          </div>
          <p class="subtitle text-faint">
            {{ runRoleLabel(selectedRun) }}
            <template v-if="runPriorityLabel(selectedRun)">
              · 优先级：{{ runPriorityLabel(selectedRun) }}
            </template>
            <span class="mono">
              · Run #{{ selectedRun.id }} · Session: {{ selectedRun.session_id || '—' }}
            </span>
          </p>
        </div>

        <!-- Claimed Executor Details -->
        <Card class="executor-details" padding="sm" title="执行 Worker">
          <span class="exec-label text-faint">执行 Worker</span>
          <span class="exec-val mono"
            >{{ selectedRun.worker_name }} ({{ selectedRun.worker_type }})</span
          >
        </Card>
      </header>

      <section class="run-business-summary" aria-label="本次运行结果" tabindex="0">
        <span class="business-summary-label">本次运行结果</span>
        <strong>{{ runResultText(selectedRun) }}</strong>
        <span class="business-summary-technical mono text-faint">
          {{ selectedRun.task_type }} · Profile: {{ selectedRun.execution_profile || 'default' }}
        </span>
      </section>

      <Alert
        v-if="selectedRun.error_message || selectedRun.error_type"
        class="error-banner"
        tabindex="0"
        aria-label="运行错误"
        tone="danger"
        :title="`运行错误 (${selectedRun.error_type || 'Error'})`"
      >
        <p class="error-msg pre-wrap">{{ selectedRun.error_message || selectedRun.error_type }}</p>
        <div v-if="selectedRun.error_type === 'worker_rejected'" class="rejection-details">
          拒绝原因: {{ selectedRun.error_message }}
        </div>
      </Alert>

      <Alert
        v-if="linkedIntentOutcomeText(selectedRun)"
        class="intent-outcome-banner"
        tabindex="0"
        aria-label="关联 Intent 最终结论"
        tone="warning"
        title="关联 Intent 最终结论"
      >
        {{ linkedIntentOutcomeText(selectedRun) }}
      </Alert>

      <Alert v-if="runDetailsError" class="run-detail-state" tone="warning">
        {{ runDetailsError }}
        <template #actions>
          <Button variant="secondary" size="sm" @click="refetchTerminal">重试</Button>
        </template>
      </Alert>

      <Tabs
        class="runs-detail-tabs"
        :model-value="activeDetailTab"
        :options="detailTabs"
        activation-mode="manual"
        variant="line"
        @update:model-value="activeDetailTab = $event"
      >
        <template #panel="{ option }">
          <div class="tab-viewport">
            <!-- 1. Terminal View -->
            <div class="terminal-view" v-if="option.value === 'terminal'">
              <div class="terminal-toolbar">
                <span class="term-title">终端视图</span>
                <div class="term-actions">
                  <a
                    v-if="stdoutDownloadUrl"
                    class="btn-term-action"
                    :href="stdoutDownloadUrl"
                    download
                    :title="stdoutDownloadTitle"
                  >
                    {{ stdoutDownloadLabel }}
                  </a>
                  <Button variant="secondary" size="sm" title="重新加载流" @click="refetchTerminal">
                    刷新
                  </Button>
                  <Checkbox v-model="autoScrollTerminal" label="自动滚动" />
                </div>
              </div>

              <div class="terminal-output-nav" v-if="terminalOutputs.length > 1">
                <Button
                  v-for="output in terminalOutputs"
                  :key="output.id"
                  variant="secondary"
                  size="sm"
                  class="terminal-output-option"
                  :class="{
                    active: stdoutArtifactId === output.id,
                    success: output.successful,
                    timeout: output.timedOut,
                  }"
                  :aria-pressed="stdoutArtifactId === output.id"
                  @click="selectTerminalOutput(output.id)"
                >
                  {{ output.label }}
                </Button>
              </div>
              <div class="terminal-recovery-notice" v-if="terminalRecovered">
                主执行超时后已通过 conclude 恢复；默认展示最终成功输出，可切换查看原始执行记录。
              </div>
              <div class="terminal-preview-notice" v-if="previewTruncationMessage">
                {{ previewTruncationMessage }}
              </div>
              <div class="terminal-capture-warning" v-if="captureTruncationMessage">
                {{ captureTruncationMessage }}
              </div>

              <div class="terminal-screen" ref="terminalScreen">
                <div class="terminal-empty" v-if="isLoadingTerminalPreview && !stdoutContent">
                  <span class="text-faint">正在载入终端日志...</span>
                </div>
                <div class="terminal-empty terminal-error" v-else-if="terminalPreviewError">
                  <span>{{ terminalPreviewError }}</span>
                  <Button variant="secondary" size="sm" @click="refetchTerminal">重试</Button>
                </div>
                <pre class="terminal-content text-wrap mono" v-else-if="stdoutContent">{{
                  stdoutContent
                }}</pre>
                <div class="terminal-empty" v-else>
                  <span class="text-faint">暂无标准输出、标准错误或诊断日志产物</span>
                </div>
              </div>
            </div>

            <!-- Shell 命令 -->
            <div class="commands-view" v-if="option.value === 'commands'">
              <DataTable
                v-if="commandEvents.length > 0"
                :rows="commandEvents"
                :columns="commandColumns"
                :row-key="(command) => command.id"
                caption="Shell 命令审计"
                density="compact"
              >
                <template #cell-time="{ row }"
                  ><span class="mono text-faint">{{ formatTimeShort(row.ts) }}</span></template
                >
                <template #cell-phase="{ row }"
                  ><span class="mono">{{ row.phase || '—' }}</span></template
                >
                <template #cell-kind="{ row }"
                  ><span class="mono command-cell" :title="row.command_kind">{{
                    row.command_kind || '—'
                  }}</span></template
                >
                <template #cell-status="{ row }">
                  <Badge :tone="commandStatusTone(row)" size="sm">
                    {{ row.kind === 'command_started' ? '已启动' : statusText(row.status) }}
                    <template v-if="row.kind === 'command_finished'">
                      · Exit {{ row.exit_code ?? row.returncode ?? '—' }}</template
                    >
                  </Badge>
                </template>
                <template #cell-duration="{ row }"
                  ><span class="mono">{{
                    row.duration_seconds === null ? '—' : `${row.duration_seconds}s`
                  }}</span></template
                >
                <template #cell-diagnostics="{ row }">
                  <div class="command-diagnostics">
                    <Badge v-if="row.timed_out" tone="warning" size="sm">超时</Badge>
                    <Badge v-if="row.cancelled" tone="warning" size="sm">已取消</Badge>
                    <Badge v-if="row.oom_killed" tone="danger" size="sm">OOM</Badge>
                    <span
                      v-if="!row.timed_out && !row.cancelled && !row.oom_killed"
                      class="text-faint"
                      >—</span
                    >
                  </div>
                </template>
              </DataTable>
              <EmptyState
                v-else
                title="暂无 Shell 命令"
                description="本轮执行中没有调用的 Shell 命令。"
              />
            </div>

            <!-- 产物证据 -->
            <div class="artifacts-view" v-if="option.value === 'artifacts'">
              <div class="artifacts-grid" v-if="artifacts.length > 0">
                <Card v-for="art in artifacts" :key="art.id" class="artifact-card" padding="sm">
                  <Button variant="ghost" size="sm" @click="previewArtifact(art.id)">
                    <div class="art-icon">
                      <svg
                        width="24"
                        height="24"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="2"
                        class="text-info"
                      >
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                        <polyline points="14 2 14 8 20 8"></polyline>
                        <line x1="16" y1="13" x2="8" y2="13"></line>
                        <line x1="16" y1="17" x2="8" y2="17"></line>
                        <polyline points="10 9 9 9 8 9"></polyline>
                      </svg>
                    </div>
                    <div class="art-info">
                      <span class="art-title text-truncate" :title="art.uri">{{
                        art.uri.split('/').pop()
                      }}</span>
                      <span class="art-meta mono text-faint">
                        Size: {{ art.size }} 字节 · Category: {{ art.category }}
                      </span>
                    </div>
                  </Button>
                </Card>
              </div>
              <EmptyState
                v-else
                title="暂无产物证据"
                description="本轮运行中没有写入任何产物证据资产。"
              />
            </div>
          </div>
        </template>
      </Tabs>
    </main>
    <div class="runs-empty-detail" v-else>
      <svg
        width="48"
        height="48"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1"
        class="text-faint"
      >
        <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
        <line x1="9" y1="3" x2="9" y2="21"></line>
        <line x1="15" y1="9" x2="21" y2="9"></line>
        <line x1="15" y1="15" x2="21" y2="15"></line>
      </svg>
      <h3>
        {{ filteredRuns.length ? '请在左侧选择一个运行记录' : '暂无运行记录' }}
      </h3>
      <p>
        {{
          filteredRuns.length
            ? '观察该任务的实时终端日志及审计详情。'
            : '项目状态请查看工作空间顶部。'
        }}
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { DataTable, ErrorState, FilterBar, type DataTableColumn } from '@/components/patterns'
import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  EmptyState,
  Input,
  Select,
  Skeleton,
  Spinner,
  StatusBadge,
  Tabs,
  type BadgeTone,
  type SelectOption,
} from '@/components/ui'
import type { TaskRunCommandEvent } from './runTypes'
import { useRunsPage } from './useRunsPage'

const statusFilterOptions: readonly SelectOption[] = [
  { label: '所有状态', value: 'all' },
  { label: '运行中', value: 'running' },
  { label: '成功', value: 'succeeded' },
  { label: '失败', value: 'failed' },
  { label: '超时', value: 'timed_out' },
  { label: '已取消', value: 'cancelled' },
  { label: '排队中', value: 'queued' },
]

type CommandColumnKey = 'time' | 'phase' | 'kind' | 'status' | 'duration' | 'diagnostics'

const commandColumns = [
  { key: 'time', label: '时间' },
  { key: 'phase', label: '阶段' },
  { key: 'kind', label: '命令类型' },
  { key: 'status', label: '状态' },
  { key: 'duration', label: '耗时' },
  { key: 'diagnostics', label: '诊断' },
] satisfies readonly DataTableColumn<TaskRunCommandEvent, CommandColumnKey>[]

const commandStatusTone = (command: TaskRunCommandEvent): BadgeTone => {
  if (command.status === 'failed') return 'danger'
  if (command.status === 'timed_out' || command.timed_out) return 'warning'
  if (command.status === 'cancelled' || command.cancelled) return 'warning'
  if (
    command.kind === 'command_started' ||
    ['started', 'running', 'queued'].includes(command.status)
  ) {
    return 'info'
  }
  if ((command.exit_code ?? command.returncode) !== 0) return 'danger'
  if (['success', 'succeeded'].includes(command.status)) return 'success'
  return 'neutral'
}

const errorMessage = (error: unknown): string =>
  error instanceof Error && error.message ? error.message : String(error || '未知错误')

const {
  activeDetailTab,
  artifacts,
  autoScrollTerminal,
  captureTruncationMessage,
  commandEvents,
  detailTabs,
  filteredRuns,
  filtersActive,
  formatRelativeTime,
  formatTimeShort,
  isLoadingRunDetails,
  isLoadingRuns,
  isLoadingTerminalPreview,
  linkedIntentOutcomeText,
  previewArtifact,
  previewTruncationMessage,
  refetchRuns,
  refetchTerminal,
  runBusinessTitle,
  runCardDiagnostic,
  runDetailsError,
  runDurationText,
  runIntent,
  runPriorityLabel,
  runReferenceTime,
  runResultText,
  runRoleLabel,
  runsError,
  runsInitialError,
  runsStaleError,
  searchQuery,
  selectedRun,
  selectedRunId,
  selectRun,
  selectTerminalOutput,
  statusFilter,
  statusText,
  stdoutArtifactId,
  stdoutContent,
  stdoutDownloadLabel,
  stdoutDownloadTitle,
  stdoutDownloadUrl,
  terminalOutputs,
  terminalPreviewError,
  terminalRecovered,
  terminalScreen,
} = useRunsPage()

void terminalScreen
</script>

<style src="./RunsPage.css" scoped></style>
