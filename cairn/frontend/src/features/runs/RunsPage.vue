<template>
  <div class="runs-container">
    <Alert v-if="chatRunNotice" class="runs-stale-error" tone="info">
      此记录属于 Agent 对话，请在项目 Agent 中查看。
      <template #actions>
        <Button variant="secondary" size="sm" @click="openProjectAgent">打开项目 Agent</Button>
      </template>
    </Alert>
    <Alert
      v-if="runsStaleError"
      class="runs-stale-error"
      tone="warning"
      title="刷新失败，保留上次运行列表"
    >
      {{ errorMessage(runsError) }}
      <template #actions>
        <Button variant="secondary" size="sm" @click="refetchRuns">重试</Button>
      </template>
    </Alert>

    <FilterBar class="runs-toolbar" label="工作索引筛选">
      <Input
        v-model="searchQuery"
        class="runs-search"
        placeholder="搜索工作元数据或结果…"
        aria-label="搜索工作元数据或结果"
      />
      <Select v-model="statusFilter" :options="statusFilterOptions" aria-label="执行记录状态" />
      <template #actions>
        <Button v-if="hasNewProgress" variant="ghost" @click="backToLatest">
          有新进展 · 回到最新
        </Button>
        <Button variant="secondary" @click="refreshTimeline"> <RefreshCw :size="16" />刷新 </Button>
      </template>
    </FilterBar>

    <div class="runs-workspace">
      <aside class="runs-index" aria-label="工作索引">
        <h2 class="sr-only">工作索引</h2>
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
            style="block-size: 5.25rem"
            :decorative="false"
            label="正在载入运行列表"
          />
        </div>
        <template v-else>
          <div
            v-if="workTimeline.length"
            ref="timelineScroll"
            class="runs-timeline-scroll"
            @scroll="onTimelineScroll"
          >
            <ol class="work-timeline" aria-label="工作索引">
              <li
                v-for="work in workTimeline"
                :key="work.key"
                class="work-node"
                :class="{ 'work-node--selected': selectedWork?.key === work.key }"
                :data-work-key="work.key"
              >
                <button
                  type="button"
                  class="work-select"
                  :aria-current="selectedWork?.key === work.key ? 'true' : undefined"
                  @click="selectWork(work)"
                >
                  <span class="work-title" :title="workTitle(work)">{{ workTitle(work) }}</span>
                  <span class="work-meta">
                    <StatusBadge class="work-status" :status="work.status" size="sm" />
                    <time v-if="work.startedAt" :datetime="work.startedAt" :title="work.startedAt">
                      {{ formatRelativeTime(work.startedAt) }}
                    </time>
                    <span v-else>时间未记录</span>
                  </span>
                </button>
              </li>
            </ol>
          </div>
          <EmptyState
            v-else
            class="runs-list-state"
            :title="filtersActive ? '当前筛选条件没有匹配记录' : '没有运行记录'"
          />
        </template>
      </aside>

      <main v-if="selectedRun && selectedWork" class="run-details-panel" aria-label="执行阅读器">
        <header class="reader-heading">
          <h2>{{ workTitle(selectedWork) }}</h2>
          <span class="run-detail-loading">
            <Spinner v-if="isLoadingRunDetails" size="sm" label="正在载入运行详情" />
          </span>
        </header>

        <Tabs
          class="runs-primary-tabs"
          :model-value="activeWorkView"
          :options="workViewTabs"
          activation-mode="manual"
          :unmount-on-hide="false"
          variant="line"
          @update:model-value="activeWorkView = $event"
        >
          <template #panel="{ option }">
            <div v-if="option.value === 'results'" class="results-view">
              <Alert
                v-if="reportsError"
                tone="warning"
                :title="reportsLoaded ? '报告刷新失败，保留上次读取结果' : '报告结果尚未读到'"
              >
                {{ reportsError }}
                <template #actions>
                  <Button variant="secondary" size="sm" @click="loadReports()">重读报告结果</Button>
                </template>
              </Alert>
              <Alert v-if="resultReadState" tone="warning" aria-label="工作结果读取状态">
                {{ resultReadState }}
              </Alert>
              <div v-if="selectedWork.results.length" class="work-results">
                <article
                  v-for="result in selectedWork.results"
                  :key="result.key"
                  class="work-result"
                >
                  <header class="work-result-source">
                    <span>{{ result.label }}</span>
                    <Button
                      variant="ghost"
                      size="sm"
                      :title="result.runId"
                      @click="selectRun(result.runId)"
                    >
                      来源 Run
                    </Button>
                  </header>
                  <Button
                    v-if="result.artifactId"
                    class="work-result-artifact"
                    variant="link"
                    @click="previewArtifact(result.artifactId)"
                  >
                    {{ result.text }}
                  </Button>
                  <p v-else class="work-result-content">{{ result.text }}</p>
                </article>
              </div>
              <EmptyState v-else title="尚无已接受结果" />
            </div>

            <div v-else :key="selectedRun.id" class="execution-view">
              <div class="run-context">
                <div class="run-selector-row">
                  <Select
                    class="run-selector"
                    :model-value="selectedRun.id"
                    :options="runOptions"
                    aria-label="选择执行记录"
                    @update:model-value="selectRun"
                  />
                  <StatusBadge :status="selectedRun.status" size="sm" />
                  <Popover>
                    <template #trigger>
                      <Button variant="secondary" size="sm">运行信息</Button>
                    </template>
                    <dl class="run-technical-information">
                      <div>
                        <dt>Run</dt>
                        <dd class="mono">{{ selectedRun.id }}</dd>
                      </div>
                      <div>
                        <dt>Session</dt>
                        <dd class="mono">{{ selectedRun.session_id || '—' }}</dd>
                      </div>
                      <div>
                        <dt>职责</dt>
                        <dd>
                          {{ runRoleLabel(selectedRun) }}
                          <template v-if="runPriorityLabel(selectedRun)">
                            · {{ runPriorityLabel(selectedRun) }}
                          </template>
                        </dd>
                      </div>
                      <div>
                        <dt>执行 Worker</dt>
                        <dd>{{ selectedRun.worker_name }} ({{ selectedRun.worker_type }})</dd>
                      </div>
                      <div>
                        <dt>任务类型</dt>
                        <dd>{{ selectedRun.task_type }}</dd>
                      </div>
                      <div>
                        <dt>Profile</dt>
                        <dd>{{ selectedRun.execution_profile || 'default' }}</dd>
                      </div>
                    </dl>
                  </Popover>
                </div>

                <div v-if="hasRunContextStatus" class="run-context-statuses">
                  <details
                    v-if="selectedRun.error_message || selectedRun.error_type"
                    class="run-error-disclosure"
                    aria-label="运行错误"
                  >
                    <summary class="run-error-summary">
                      <ChevronRight class="run-error-chevron" :size="16" aria-hidden="true" />
                      <CircleAlert :size="16" aria-hidden="true" />
                      <span>运行错误 · {{ selectedRun.error_type || '未知类型' }}</span>
                    </summary>
                    <pre
                      class="error-msg"
                      v-text="selectedRun.error_message || selectedRun.error_type"
                    ></pre>
                  </details>

                  <div
                    v-if="showRunTrafficStatus"
                    class="run-inline-state run-traffic-state"
                    :class="{
                      'run-inline-state--warning':
                        trafficStatusError || runTrafficDisplayMessage.includes('失败'),
                    }"
                    role="status"
                    aria-label="本次运行流量状态"
                  >
                    <span class="run-state-dot" aria-hidden="true"></span>
                    <span class="run-inline-state-text">{{ runTrafficDisplayMessage }}</span>
                    <Button
                      v-if="trafficStatusError"
                      class="run-inline-retry"
                      variant="ghost"
                      size="sm"
                      @click="refetchTrafficScopes"
                      >重试</Button
                    >
                  </div>

                  <div
                    v-if="selectedIntentOutcomeText"
                    class="run-inline-state intent-outcome-state"
                    tabindex="0"
                    role="note"
                    aria-label="工作结论"
                  >
                    <TriangleAlert :size="16" aria-hidden="true" />
                    <span class="run-inline-state-text">{{ selectedIntentOutcomeText }}</span>
                  </div>
                </div>
              </div>

              <Tabs
                class="runs-detail-tabs"
                :model-value="activeDetailTab"
                :options="detailTabs"
                activation-mode="manual"
                :unmount-on-hide="false"
                variant="line"
                @update:model-value="activeDetailTab = $event"
              >
                <template #actions>
                  <div v-if="activeDetailTab === 'terminal'" class="term-actions">
                    <a
                      v-if="stdoutDownloadUrl"
                      class="btn-term-action"
                      :href="stdoutDownloadUrl"
                      download
                      :title="stdoutDownloadTitle"
                    >
                      {{ stdoutDownloadLabel }}
                    </a>
                    <Button
                      variant="secondary"
                      size="sm"
                      title="重新读取日志"
                      :disabled="
                        isLoadingTerminalPreview || (!stdoutArtifactId && isLoadingArtifacts)
                      "
                      @click="refetchTerminal"
                    >
                      刷新
                    </Button>
                    <Checkbox v-model="autoScrollTerminal" label="自动滚动" />
                  </div>
                </template>
                <template #panel="{ option: detailOption }">
                  <div class="tab-viewport">
                    <!-- 终端日志保留原始内容与截断来源提示。 -->
                    <div v-if="detailOption.value === 'terminal'" class="terminal-view">
                      <Alert
                        v-if="artifactsError"
                        tone="warning"
                        aria-label="日志来源读取状态"
                        :title="
                          artifactsLoaded
                            ? '资产清单刷新失败，显示上次读取来源'
                            : '日志来源尚未读到'
                        "
                      >
                        {{ artifactsError }}
                        <template #actions>
                          <Button
                            variant="secondary"
                            size="sm"
                            :disabled="isLoadingArtifacts"
                            @click="refetchArtifacts"
                          >
                            重试资产清单
                          </Button>
                        </template>
                      </Alert>
                      <Alert
                        v-if="terminalPreviewError"
                        tone="warning"
                        aria-label="日志读取状态"
                        :title="
                          terminalPreviewLoaded ? '刷新失败，显示上次读取内容' : '日志读取失败'
                        "
                      >
                        {{ terminalPreviewError }}
                        <template #actions>
                          <Button
                            variant="secondary"
                            size="sm"
                            :disabled="isLoadingTerminalPreview"
                            @click="refetchTerminal"
                          >
                            重试日志
                          </Button>
                        </template>
                      </Alert>
                      <div v-if="terminalOutputs.length > 1" class="terminal-output-nav">
                        <Button
                          v-for="output in terminalOutputs"
                          :key="output.id"
                          variant="secondary"
                          size="sm"
                          class="terminal-output-option"
                          :class="{ active: stdoutArtifactId === output.id }"
                          :aria-pressed="stdoutArtifactId === output.id"
                          @click="selectTerminalOutput(output.id)"
                        >
                          {{ output.label }}
                          <Badge v-if="output.timedOut" tone="warning" size="sm">超时</Badge>
                          <Badge v-else-if="output.successful" tone="success" size="sm">成功</Badge>
                        </Button>
                      </div>
                      <div v-if="terminalRecovered" class="terminal-recovery-notice">
                        主执行超时后有 conclude
                        阶段输出；退出码不代表结果已接受，可切换查看此前记录。
                      </div>
                      <div v-if="previewTruncationMessage" class="terminal-preview-notice">
                        {{ previewTruncationMessage }}
                      </div>
                      <div v-if="captureTruncationMessage" class="terminal-capture-warning">
                        {{ captureTruncationMessage }}
                      </div>

                      <div
                        :ref="setTerminalScreen"
                        class="terminal-screen"
                        tabindex="0"
                        role="region"
                        aria-label="日志正文"
                      >
                        <pre v-if="stdoutContent" class="terminal-content text-wrap mono">{{
                          stdoutContent
                        }}</pre>
                        <div v-else-if="terminalPreviewLoaded" class="terminal-empty">
                          <span class="text-faint">日志内容为空</span>
                        </div>
                        <div v-else-if="isLoadingTerminalPreview" class="terminal-empty">
                          <span class="text-faint">正在载入终端日志...</span>
                        </div>
                        <div v-else-if="terminalPreviewError" class="terminal-empty">
                          <span class="text-faint">尚未读取到日志内容</span>
                        </div>
                        <div v-else-if="!artifactsLoaded" class="terminal-empty">
                          <span class="text-faint">
                            {{ isLoadingArtifacts ? '正在读取日志来源…' : '尚未读取到日志来源' }}
                          </span>
                        </div>
                        <div v-else-if="artifactsError" class="terminal-empty">
                          <span class="text-faint">上次读取未找到日志产物，当前清单尚未更新</span>
                        </div>
                        <div v-else class="terminal-empty">
                          <span class="text-faint">暂无标准输出、标准错误或诊断日志产物</span>
                        </div>
                      </div>
                    </div>

                    <div v-else-if="detailOption.value === 'tools'" class="tools-view">
                      <Alert
                        v-if="hookReadError(selectedRun.id)"
                        tone="warning"
                        aria-label="工具读取状态"
                      >
                        {{ hookReadError(selectedRun.id) }}
                        <template #actions>
                          <Button
                            variant="secondary"
                            size="sm"
                            @click="refetchHooks(selectedRun.id)"
                          >
                            重试工具记录
                          </Button>
                        </template>
                      </Alert>
                      <Alert
                        v-if="hookObservationIncomplete(selectedRun.id)"
                        tone="warning"
                        aria-label="工具观测状态"
                      >
                        工具观测不完整，不能据此判断全部调用状态。
                      </Alert>
                      <Spinner
                        v-if="selectedHookRead?.loading && !selectedHookRead.loaded"
                        size="sm"
                        label="正在读取工具记录"
                      />
                      <EmptyState
                        v-else-if="selectedHookRead?.loaded && !selectedToolCalls.length"
                        title="暂无已记录工具调用"
                      />
                      <EmptyState v-else-if="!selectedHookRead?.loaded" title="工具记录尚未读到" />
                      <ol v-if="selectedToolCalls.length" class="tool-calls" aria-label="工具调用">
                        <li v-for="call in selectedToolCalls" :key="call.key">
                          <details class="tool-call">
                            <summary>
                              <ChevronRight :size="16" aria-hidden="true" />
                              <span class="tool-name">{{ call.name }}</span>
                              <span class="tool-status">{{ call.status }}</span>
                              <time
                                class="tool-time"
                                :title="call.startedAt || call.endedAt || undefined"
                              >
                                {{
                                  call.startedAt || call.endedAt
                                    ? formatTimeShort(call.startedAt || call.endedAt || '')
                                    : '时间未记录'
                                }}
                              </time>
                              <span v-if="call.durationMs !== null" class="tool-duration">
                                {{ (call.durationMs / 1000).toFixed(1) }} 秒
                              </span>
                            </summary>
                            <div class="tool-call-detail">
                              <dl class="tool-call-fields">
                                <div v-if="call.command">
                                  <dt>命令</dt>
                                  <dd>
                                    <pre>{{ call.command }}</pre>
                                  </dd>
                                </div>
                                <div v-if="call.filePath">
                                  <dt>路径</dt>
                                  <dd>
                                    <pre>{{ call.filePath }}</pre>
                                  </dd>
                                </div>
                                <div v-if="call.query">
                                  <dt>查询</dt>
                                  <dd>
                                    <pre>{{ call.query }}</pre>
                                  </dd>
                                </div>
                                <div v-if="call.url">
                                  <dt>URL</dt>
                                  <dd>
                                    <pre>{{ call.url }}</pre>
                                  </dd>
                                </div>
                                <div v-if="call.summary">
                                  <dt>摘要</dt>
                                  <dd class="tool-summary">{{ call.summary }}</dd>
                                </div>
                                <div v-if="call.exitCode !== null">
                                  <dt>退出码</dt>
                                  <dd class="mono">{{ call.exitCode }}</dd>
                                </div>
                              </dl>
                              <section v-if="hasToolBody(call.toolInput)" class="tool-body">
                                <h4>tool_input</h4>
                                <pre>{{ formatToolBody(call.toolInput) }}</pre>
                              </section>
                              <section v-if="hasToolBody(call.toolResponse)" class="tool-body">
                                <h4>tool_response</h4>
                                <pre>{{ formatToolBody(call.toolResponse) }}</pre>
                              </section>
                              <p
                                v-if="
                                  !hasToolBody(call.toolInput) && !hasToolBody(call.toolResponse)
                                "
                                class="tool-body-missing"
                              >
                                该来源未记录工具输入／响应正文
                              </p>
                              <Button
                                v-if="call.artifactId"
                                variant="secondary"
                                size="sm"
                                @click="previewArtifact(call.artifactId)"
                              >
                                打开已保存原件
                              </Button>
                            </div>
                          </details>
                        </li>
                      </ol>
                    </div>

                    <div v-else-if="detailOption.value === 'commands'" class="commands-view">
                      <Alert
                        v-if="commandsError"
                        tone="warning"
                        aria-label="命令审计读取状态"
                        :title="commandsLoaded ? '刷新失败，显示上次读取内容' : '命令审计读取失败'"
                      >
                        {{ commandsError }}
                        <template #actions>
                          <Button
                            variant="secondary"
                            size="sm"
                            :disabled="isLoadingCommands"
                            @click="refetchCommands"
                          >
                            重试命令审计
                          </Button>
                        </template>
                      </Alert>
                      <Spinner v-if="isLoadingCommands" size="sm" label="正在读取命令审计" />
                      <DataTable
                        v-if="commandEvents.length > 0"
                        :rows="commandEvents"
                        :columns="commandColumns"
                        :row-key="(command) => command.id"
                        caption="Shell 命令审计"
                        density="compact"
                      >
                        <template #cell-time="{ row }">
                          <span class="mono text-faint">{{ formatTimeShort(row.ts) }}</span>
                        </template>
                        <template #cell-phase="{ row }">
                          <span class="mono">{{ row.phase || '—' }}</span>
                        </template>
                        <template #cell-kind="{ row }">
                          <span class="mono command-cell" :title="row.command_kind">
                            {{ row.command_kind || '—' }}
                          </span>
                        </template>
                        <template #cell-status="{ row }">
                          <Badge :tone="commandStatusTone(row)" size="sm">
                            {{ row.kind === 'command_started' ? '已启动' : statusText(row.status) }}
                            <template v-if="row.kind === 'command_finished'">
                              · Exit {{ row.exit_code ?? row.returncode ?? '—' }}
                            </template>
                          </Badge>
                        </template>
                        <template #cell-duration="{ row }">
                          <span class="mono">
                            {{ row.duration_seconds === null ? '—' : `${row.duration_seconds}s` }}
                          </span>
                        </template>
                        <template #cell-diagnostics="{ row }">
                          <div class="command-diagnostics">
                            <Badge v-if="row.timed_out" tone="warning" size="sm">超时</Badge>
                            <Badge v-if="row.cancelled" tone="warning" size="sm">已取消</Badge>
                            <Badge v-if="row.oom_killed" tone="danger" size="sm">OOM</Badge>
                            <span
                              v-if="!row.timed_out && !row.cancelled && !row.oom_killed"
                              class="text-faint"
                            >
                              —
                            </span>
                          </div>
                        </template>
                      </DataTable>
                      <EmptyState
                        v-else-if="commandsLoaded && !commandsError"
                        title="暂无 Shell 命令"
                      />
                    </div>

                    <div v-else-if="detailOption.value === 'artifacts'" class="artifacts-view">
                      <Alert
                        v-if="artifactsError"
                        tone="warning"
                        aria-label="关联资产读取状态"
                        :title="artifactsLoaded ? '刷新失败，显示上次读取内容' : '关联资产读取失败'"
                      >
                        {{ artifactsError }}
                        <template #actions>
                          <Button
                            variant="secondary"
                            size="sm"
                            :disabled="isLoadingArtifacts"
                            @click="refetchArtifacts"
                          >
                            重试资产清单
                          </Button>
                        </template>
                      </Alert>
                      <Spinner v-if="isLoadingArtifacts" size="sm" label="正在读取关联资产" />
                      <ul
                        v-if="artifacts.length > 0"
                        class="run-artifacts-list"
                        aria-label="本次运行关联资产"
                      >
                        <li v-for="art in artifacts" :key="art.id">
                          <Button
                            variant="ghost"
                            class="run-artifact"
                            @click="previewArtifact(art.id)"
                          >
                            <FileText :size="18" />
                            <span class="art-info">
                              <span class="art-title">{{ art.uri.split('/').pop() }}</span>
                              <span class="art-meta">{{ art.size }} 字节 · {{ art.category }}</span>
                              <span class="art-source mono">{{ art.uri }}</span>
                            </span>
                            <ArrowUpRight :size="16" />
                          </Button>
                        </li>
                      </ul>
                      <EmptyState v-else-if="artifactsLoaded && !artifactsError" title="暂无文件" />
                    </div>
                  </div>
                </template>
              </Tabs>
            </div>
          </template>
        </Tabs>
      </main>
      <main v-else class="run-details-panel run-details-empty" aria-label="执行阅读器">
        <EmptyState :title="readerEmptyTitle" />
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch, type ComponentPublicInstance } from 'vue'
import {
  ArrowUpRight,
  ChevronRight,
  CircleAlert,
  FileText,
  RefreshCw,
  TriangleAlert,
} from '@lucide/vue'
import { useQuery } from '@tanstack/vue-query'
import { getTrafficScopes } from '@/api/traffic'
import { trafficScopeLabel } from '@/features/traffic/trafficStatus'
import { DataTable, ErrorState, FilterBar, type DataTableColumn } from '@/components/patterns'
import {
  Alert,
  Badge,
  Button,
  Checkbox,
  EmptyState,
  Input,
  Popover,
  Select,
  Skeleton,
  Spinner,
  StatusBadge,
  Tabs,
  type BadgeTone,
  type SelectOption,
} from '@/components/ui'
import type { TaskRun, TaskRunCommandEvent } from './runTypes'
import { useRunsPage } from './useRunsPage'

const statusFilterOptions: readonly SelectOption[] = [
  { label: '执行记录状态：全部', value: 'all' },
  { label: '运行中', value: 'running' },
  { label: '成功', value: 'succeeded' },
  { label: '失败', value: 'failed' },
  { label: '超时', value: 'timed_out' },
  { label: '已取消', value: 'cancelled' },
  { label: '排队中', value: 'queued' },
]

const workViewTabs: readonly SelectOption[] = [
  { label: '结果', value: 'results' },
  { label: '执行记录', value: 'execution' },
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
  activeWorkView,
  activeDetailTab,
  artifacts,
  autoScrollTerminal,
  captureTruncationMessage,
  chatRunNotice,
  commandEvents,
  detailTabs,
  filtersActive,
  formatRelativeTime,
  formatTimeShort,
  isLoadingRunDetails,
  isLoadingRuns,
  isLoadingTerminalPreview,
  linkedIntentOutcomeText,
  openProjectAgent,
  previewArtifact,
  previewTruncationMessage,
  refetchRuns,
  refetchTerminal,
  workTimeline,
  workTitle,
  selectedWork,
  selectWork,
  toolCalls,
  hookReads,
  hookReadError,
  hookObservationIncomplete,
  refetchHooks,
  refreshTimeline,
  reportsLoaded,
  resultReadState,
  reportsError,
  loadReports,
  commandsLoaded,
  commandsError,
  isLoadingCommands,
  artifactsLoaded,
  artifactsError,
  isLoadingArtifacts,
  refetchCommands,
  refetchArtifacts,
  terminalPreviewLoaded,
  runDurationText,
  runPriorityLabel,
  runReferenceTime: referenceTime,
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

const setTerminalScreen = (element: Element | ComponentPublicInstance | null) => {
  terminalScreen.value = element instanceof HTMLElement ? element : null
}

const {
  data: trafficScopes,
  isError: trafficStatusError,
  refetch: refetchTrafficScopes,
} = useQuery({
  queryKey: computed(() => [
    'runTrafficScopes',
    selectedRun.value?.project_id,
    selectedRun.value?.id,
  ]),
  queryFn: ({ signal }) =>
    getTrafficScopes(selectedRun.value!.project_id, signal, selectedRun.value!.id),
  enabled: computed(() => Boolean(selectedRun.value?.project_id)),
  refetchInterval: (query) => {
    if (query.state.status === 'error') return false
    const scope = query.state.data?.items.find((item) => item.task_run_id === selectedRun.value?.id)
    if (!scope)
      return ['queued', 'running'].includes(selectedRun.value?.status ?? '') ? 3000 : false
    return scope.status?.upload_complete === true &&
      scope.status.control_stopped === true &&
      scope.status.test_stopped === true &&
      scope.status.collector_stopped === true
      ? false
      : 3000
  },
})
const runTrafficScope = computed(() =>
  trafficScopes.value?.items.find((scope) => scope.task_run_id === selectedRun.value?.id),
)
const runTrafficMessage = computed(() =>
  runTrafficScope.value ? trafficScopeLabel(runTrafficScope.value) : '',
)
const runTrafficDisplayMessage = computed(() => {
  if (!trafficStatusError.value) return runTrafficMessage.value
  return runTrafficMessage.value
    ? `${runTrafficMessage.value} · 状态刷新失败，显示上次观测`
    : '流量状态查询失败，当前状态未知'
})
// 正常同步完成无需常驻占位；刷新失败仍须展示上次观测与重试入口。
const showRunTrafficStatus = computed(
  () =>
    trafficStatusError.value ||
    Boolean(runTrafficMessage.value && runTrafficMessage.value !== '同步完成'),
)
const selectedIntentOutcomeText = computed(() =>
  selectedRun.value ? linkedIntentOutcomeText(selectedRun.value) : '',
)
const hasRunContextStatus = computed(() =>
  Boolean(
    selectedRun.value &&
      (selectedRun.value.error_message ||
        selectedRun.value.error_type ||
        showRunTrafficStatus.value ||
        selectedIntentOutcomeText.value),
  ),
)

const runOptions = computed<SelectOption[]>(() =>
  (selectedWork.value?.runs || []).map((run: TaskRun) => {
    const time = referenceTime(run)
    return {
      value: run.id,
      label: `${statusText(run.status)} · ${time ? formatRelativeTime(time) : '时间未记录'} · ${runDurationText(run)}`,
    }
  }),
)
const selectedToolCalls = computed(() => (selectedRun.value ? toolCalls(selectedRun.value) : []))
const selectedHookRead = computed(() =>
  selectedRun.value ? hookReads.value[selectedRun.value.id] : undefined,
)
const readerEmptyTitle = computed(() => {
  if (isLoadingRuns.value) return '正在载入执行记录'
  if (selectedRunId.value && !selectedRun.value) return '未找到该执行记录'
  if (!workTimeline.value.length) return '没有可选择的工作'
  return '选择一项工作查看执行记录'
})

const hasToolBody = (body: Record<string, unknown> | null): boolean =>
  Boolean(body && Object.keys(body).length)
const formatToolBody = (body: Record<string, unknown> | null): string =>
  body ? JSON.stringify(body, null, 2) : ''

const timelineScroll = ref<HTMLElement | null>(null)
const hasNewProgress = ref(false)
const onTimelineScroll = () => {
  if ((timelineScroll.value?.scrollTop || 0) < 24) hasNewProgress.value = false
}
const backToLatest = () => {
  timelineScroll.value?.scrollTo({ top: 0 })
  hasNewProgress.value = false
}
// 只固定当前读到的工作锚点；新工作或状态更新不能把读者推回顶部。
watch(
  () =>
    JSON.stringify([
      workTimeline.value.map((work) => [
        work.key,
        work.status,
        work.results,
        work.runs.map((run) => [run.id, run.status]),
      ]),
      Object.entries(hookReads.value).map(([id, read]) => [
        id,
        read.events.map((event) => event.id),
      ]),
    ]),
  async () => {
    const viewport = timelineScroll.value
    if (!viewport || viewport.scrollTop < 24) return
    const anchor = [...viewport.querySelectorAll<HTMLElement>('.work-node')].find(
      (item) => item.getBoundingClientRect().bottom > viewport.getBoundingClientRect().top,
    )
    const before = anchor?.getBoundingClientRect().top
    hasNewProgress.value = true
    await nextTick()
    if (anchor?.isConnected && before !== undefined) {
      // DOMRect 已包含应用缩放，scrollTop 仍是布局像素，须换回同一坐标系。
      const scale = viewport.offsetHeight
        ? viewport.getBoundingClientRect().height / viewport.offsetHeight
        : 1
      viewport.scrollTop += (anchor.getBoundingClientRect().top - before) / (scale || 1)
    }
  },
)
watch([statusFilter, searchQuery], () => {
  hasNewProgress.value = false
})
</script>

<style src="./RunsPage.css" scoped></style>
