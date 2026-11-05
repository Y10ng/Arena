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

    <section
      v-show="!selectedRun || showRunList"
      ref="runList"
      class="runs-index"
      aria-label="工作进展时间线"
    >
      <h2 ref="listHeading" class="sr-only" tabindex="-1">工作进展</h2>
      <Alert
        v-if="reportsError"
        tone="warning"
        :title="reportsLoaded ? '报告刷新失败，保留上次读取结果' : '报告结果尚未读到'"
      >
        {{ reportsError }}
        <template #actions
          ><Button variant="secondary" size="sm" @click="loadReports()"
            >重读报告结果</Button
          ></template
        >
      </Alert>
      <Alert v-if="resultReadState" tone="warning" aria-label="工作结果读取状态">{{
        resultReadState
      }}</Alert>
      <FilterBar label="工作进展筛选">
        <Input
          v-model="searchQuery"
          class="runs-search"
          placeholder="搜索目的、角色、Worker 或结果…"
          aria-label="搜索运行元数据"
          aria-describedby="runs-search-scope"
        />
        <Select v-model="statusFilter" :options="statusFilterOptions" aria-label="执行记录状态" />
        <template #status
          ><span id="runs-search-scope">检索元数据与已接受结果，不搜索日志正文</span></template
        >
        <template #actions>
          <Button v-if="selectedRun" variant="ghost" @click="resumeReading">返回当前记录</Button>
          <Button v-if="hasNewProgress" variant="ghost" @click="backToLatest"
            >有新进展 · 回到最新</Button
          >
          <Button variant="secondary" @click="refreshTimeline"
            ><RefreshCw :size="16" />刷新进展</Button
          >
        </template>
      </FilterBar>
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
        <div
          v-if="workTimeline.length"
          ref="timelineScroll"
          class="runs-timeline-scroll"
          @scroll="onTimelineScroll"
        >
          <ol class="work-timeline" aria-label="工作进展">
            <li
              v-for="work in workTimeline"
              :key="work.key"
              class="work-node"
              :data-work-key="work.key"
            >
              <div class="work-time">
                <time v-if="work.startedAt" :datetime="work.startedAt" :title="work.startedAt">{{
                  formatRelativeTime(work.startedAt)
                }}</time
                ><span v-else>时间未记录</span>
              </div>
              <div class="work-body">
                <header class="work-heading">
                  <h3>{{ workTitle(work) }}</h3>
                  <span
                    class="work-status"
                    :class="{
                      'work-status--active': work.status === '执行中',
                      'work-status--uncertain': work.status.includes('未'),
                    }"
                    >{{ work.status }}</span
                  >
                </header>
                <ul
                  v-if="workActivity(work).length"
                  class="work-activity"
                  aria-label="当前工具活动"
                >
                  <li v-for="call in workActivity(work)" :key="call.key">
                    <Wrench :size="14" aria-hidden="true" />
                    <span
                      >{{
                        call.active
                          ? '正在执行'
                          : call.status === '已完成'
                            ? '最近完成'
                            : '最近调用未完成'
                      }}
                      · {{ call.name }}</span
                    >
                    <time v-if="call.startedAt || call.endedAt">{{
                      formatTimeShort(call.startedAt || call.endedAt || '')
                    }}</time>
                  </li>
                </ul>
                <p v-else-if="work.runs.some(isActiveRun)" class="work-observation-empty">
                  {{ work.status
                  }}<span v-if="work.runs.some((run) => hookReads[run.id]?.loaded)">
                    · 暂无已记录工具活动</span
                  >
                </p>
                <div v-for="run in work.runs" :key="`read:${run.id}`" class="work-read-state">
                  <p v-if="hookReadError(run.id)" role="status">
                    {{ hookReadError(run.id) }}
                    <Button variant="ghost" size="sm" @click="refetchHooks(run.id)"
                      >重读工具过程</Button
                    >
                  </p>
                  <p v-if="hookObservationIncomplete(run.id)" role="status">
                    工具观测不完整，不能据此判断全部调用状态。<Button
                      variant="ghost"
                      size="sm"
                      @click="openRun(run.id, $event)"
                      >查看执行详情</Button
                    >
                  </p>
                </div>
                <div v-if="work.results.length" class="work-results">
                  <section v-for="result in work.results" :key="result.key" class="work-result">
                    <div class="work-result-source">
                      <span>{{ result.label }}</span
                      ><Button
                        variant="ghost"
                        size="sm"
                        :title="result.runId"
                        @click="openRun(result.runId, $event)"
                        >来源执行</Button
                      >
                    </div>
                    <Button
                      v-if="result.artifactId"
                      variant="link"
                      @click="previewArtifact(result.artifactId)"
                      >{{ result.text }}</Button
                    >
                    <details
                      v-else-if="result.text.length > 240 || result.text.split('\n').length > 3"
                    >
                      <summary>查看结果全文</summary>
                      <p>{{ result.text }}</p>
                    </details>
                    <p v-else>{{ result.text }}</p>
                  </section>
                </div>
                <p v-else-if="!work.runs.some(isActiveRun)" class="work-observation-empty">
                  执行已结束，尚无可确认归属的接受结果。
                </p>
                <div class="work-actions">
                  <Button
                    variant="ghost"
                    size="sm"
                    :aria-expanded="expandedTools.has(work.key)"
                    :aria-controls="`tools-${work.key}`"
                    @click="toggleTools(work)"
                    ><ChevronDown
                      :size="14"
                      :class="{ 'is-expanded': expandedTools.has(work.key) }"
                    />工具过程</Button
                  >
                  <Button
                    class="work-detail"
                    variant="ghost"
                    size="sm"
                    :aria-current="
                      work.runs.some((run) => selectedRunId === run.id) ? 'true' : undefined
                    "
                    @click="openRun(work.matchedRuns[0]!.id, $event)"
                    >查看详情</Button
                  >
                </div>
                <div
                  v-if="expandedTools.has(work.key)"
                  :id="`tools-${work.key}`"
                  class="work-tools"
                >
                  <section
                    v-for="run in work.runs"
                    :key="run.id"
                    :aria-label="`执行 ${run.id} 的工具过程`"
                  >
                    <div class="work-tool-source">
                      <Button variant="ghost" size="sm" @click="openRun(run.id, $event)"
                        >执行 {{ run.id }}</Button
                      ><StatusBadge :status="run.status" size="sm" />
                    </div>
                    <p v-if="!hookReads[run.id]?.loaded">
                      {{ hookReads[run.id]?.loading ? '正在读取工具过程…' : '工具过程尚未读到' }}
                    </p>
                    <p v-else-if="!toolCalls(run).length">暂无已记录工具调用</p>
                    <ol v-else class="tool-calls">
                      <li v-for="call in toolCalls(run)" :key="call.key">
                        <time :title="call.startedAt || call.endedAt || undefined">{{
                          call.startedAt || call.endedAt
                            ? formatTimeShort(call.startedAt || call.endedAt || '')
                            : '时间未记录'
                        }}</time>
                        <span class="tool-name">{{ call.name }}</span
                        ><span>{{ call.status }}</span
                        ><span v-if="call.durationMs !== null"
                          >{{ (call.durationMs / 1000).toFixed(1) }} 秒</span
                        >
                        <Button
                          v-if="call.artifactId"
                          variant="ghost"
                          size="sm"
                          @click="previewArtifact(call.artifactId)"
                          >已保存原件</Button
                        >
                      </li>
                    </ol>
                  </section>
                </div>
                <details class="work-attempts">
                  <summary>
                    执行记录 · {{ work.runs.length
                    }}<span v-if="filtersActive"> · 命中 {{ work.matchedRuns.length }} 次</span>
                  </summary>
                  <ul>
                    <li
                      v-for="run in work.runs"
                      :key="run.id"
                      :class="{
                        'attempt-match':
                          filtersActive && work.matchedRuns.some((match) => match.id === run.id),
                      }"
                    >
                      <StatusBadge :status="run.status" size="sm" /><time>{{
                        formatRelativeTime(runReferenceTime(run))
                      }}</time
                      ><span>{{ runDurationText(run) }}</span>
                      <span
                        v-if="
                          filtersActive && work.matchedRuns.some((match) => match.id === run.id)
                        "
                        >命中筛选</span
                      >
                      <Button variant="ghost" size="sm" @click="openRun(run.id, $event)"
                        >查看执行 {{ run.id }}</Button
                      >
                    </li>
                  </ul>
                </details>
              </div>
            </li>
          </ol>
        </div>
        <EmptyState
          v-else
          class="runs-list-state"
          :title="filtersActive ? '当前筛选条件没有匹配记录' : '没有运行记录'"
        />
      </template>
    </section>

    <main
      v-if="selectedRun"
      v-show="!showRunList"
      class="run-details-panel"
      aria-label="运行记录阅读"
    >
      <div class="run-reader-nav">
        <Button class="run-back" variant="ghost" @click="returnToList">
          <ArrowLeft :size="16" />返回工作进展
        </Button>
        <span class="run-reader-id mono">Run {{ selectedRun.id }}</span>
      </div>
      <header class="detail-header">
        <div class="header-left-info">
          <div class="title-row">
            <StatusBadge :status="selectedRun.status" size="sm" />
            <h2 ref="detailHeading" tabindex="-1">{{ runBusinessTitle(selectedRun) }}</h2>
            <span class="run-detail-loading">
              <Spinner v-if="isLoadingRunDetails" size="sm" label="正在载入运行详情" />
            </span>
          </div>
          <p class="subtitle text-faint">
            {{ runRoleLabel(selectedRun) }} · {{ selectedRun.worker_name }} ·
            {{ runDurationText(selectedRun) }}
            <time
              :datetime="runReferenceTime(selectedRun) || undefined"
              :title="runReferenceTime(selectedRun) || undefined"
            >
              · {{ formatRelativeTime(runReferenceTime(selectedRun)) }}
            </time>
            <template v-if="runPriorityLabel(selectedRun)">
              · 优先级：{{ runPriorityLabel(selectedRun) }}
            </template>
          </p>
        </div>
        <Popover>
          <template #trigger><Button variant="secondary" size="sm">运行信息</Button></template>
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
      </header>

      <section
        v-if="!resultDuplicatesError"
        :key="selectedRun.id"
        class="run-business-summary"
        aria-label="本次运行结果"
      >
        <details v-if="resultIsLong">
          <summary>本次运行结果（展开全文）</summary>
          <p class="run-result-content">{{ resultText }}</p>
        </details>
        <template v-else
          ><span class="business-summary-label">本次运行结果</span
          ><strong>{{ resultText }}</strong></template
        >
      </section>

      <details
        v-if="selectedRun.error_message || selectedRun.error_type"
        :key="`error:${selectedRun.id}`"
        class="error-banner"
        aria-label="运行错误"
        :open="!errorIsLong"
      >
        <summary>运行错误 · {{ selectedRun.error_type || '未知类型' }}（查看原文）</summary>
        <p class="error-msg pre-wrap">{{ selectedRun.error_message || selectedRun.error_type }}</p>
      </details>

      <Alert
        v-if="runTrafficMessage || trafficStatusError"
        class="run-detail-state"
        :tone="trafficStatusError ? 'warning' : 'info'"
        aria-label="本次运行流量状态"
      >
        {{ runTrafficMessage
        }}<template v-if="trafficStatusError">{{
          runTrafficMessage ? ' · 状态刷新失败，显示上次观测' : '流量状态查询失败，当前状态未知'
        }}</template>
        <template v-if="trafficStatusError" #actions>
          <Button variant="secondary" size="sm" @click="refetchTrafficScopes">重试</Button>
        </template>
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

      <Tabs
        class="runs-detail-tabs"
        :model-value="activeDetailTab"
        :options="detailTabs"
        activation-mode="manual"
        :unmount-on-hide="false"
        variant="line"
        @update:model-value="activeDetailTab = $event"
      >
        <template #label="{ option }">
          {{
            option.value === 'terminal'
              ? '运行日志'
              : option.value === 'commands'
                ? '命令审计'
                : '关联资产'
          }}
        </template>
        <template #actions>
          <div v-if="activeDetailTab === 'terminal'" class="term-actions">
            <a
              v-if="stdoutDownloadUrl"
              class="btn-term-action"
              :href="stdoutDownloadUrl"
              download
              :title="stdoutDownloadTitle"
              >{{ stdoutDownloadLabel }}</a
            >
            <Button
              variant="secondary"
              size="sm"
              title="重新读取日志"
              :disabled="isLoadingTerminalPreview || (!stdoutArtifactId && isLoadingArtifacts)"
              @click="refetchTerminal"
              >刷新</Button
            >
            <Checkbox v-model="autoScrollTerminal" label="自动滚动" />
          </div>
        </template>
        <template #panel="{ option }">
          <div class="tab-viewport">
            <!-- 终端日志保留原始内容与截断来源提示。 -->
            <div class="terminal-view" v-if="option.value === 'terminal'">
              <Alert
                v-if="artifactsError"
                tone="warning"
                aria-label="日志来源读取状态"
                :title="artifactsLoaded ? '资产清单刷新失败，显示上次读取来源' : '日志来源尚未读到'"
              >
                {{ artifactsError }}
                <template #actions>
                  <Button
                    variant="secondary"
                    size="sm"
                    :disabled="isLoadingArtifacts"
                    @click="refetchArtifacts"
                    >重试资产清单</Button
                  >
                </template>
              </Alert>
              <Alert
                v-if="terminalPreviewError"
                tone="warning"
                aria-label="日志读取状态"
                :title="terminalPreviewLoaded ? '刷新失败，显示上次读取内容' : '日志读取失败'"
              >
                {{ terminalPreviewError }}
                <template #actions>
                  <Button
                    variant="secondary"
                    size="sm"
                    :disabled="isLoadingTerminalPreview"
                    @click="refetchTerminal"
                    >重试日志</Button
                  >
                </template>
              </Alert>
              <div class="terminal-output-nav" v-if="terminalOutputs.length > 1">
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
              <div class="terminal-recovery-notice" v-if="terminalRecovered">
                主执行超时后有 conclude 阶段输出；退出码不代表结果已接受，可切换查看此前记录。
              </div>
              <div class="terminal-preview-notice" v-if="previewTruncationMessage">
                {{ previewTruncationMessage }}
              </div>
              <div class="terminal-capture-warning" v-if="captureTruncationMessage">
                {{ captureTruncationMessage }}
              </div>

              <div
                class="terminal-screen"
                ref="terminalScreen"
                tabindex="0"
                role="region"
                aria-label="日志正文"
              >
                <pre class="terminal-content text-wrap mono" v-if="stdoutContent">{{
                  stdoutContent
                }}</pre>
                <div class="terminal-empty" v-else-if="terminalPreviewLoaded">
                  <span class="text-faint">日志内容为空</span>
                </div>
                <div class="terminal-empty" v-else-if="isLoadingTerminalPreview">
                  <span class="text-faint">正在载入终端日志...</span>
                </div>
                <div class="terminal-empty" v-else-if="terminalPreviewError">
                  <span class="text-faint">尚未读取到日志内容</span>
                </div>
                <div class="terminal-empty" v-else-if="!artifactsLoaded">
                  <span class="text-faint">{{
                    isLoadingArtifacts ? '正在读取日志来源…' : '尚未读取到日志来源'
                  }}</span>
                </div>
                <div class="terminal-empty" v-else-if="artifactsError">
                  <span class="text-faint">上次读取未找到日志产物，当前清单尚未更新</span>
                </div>
                <div class="terminal-empty" v-else>
                  <span class="text-faint">暂无标准输出、标准错误或诊断日志产物</span>
                </div>
              </div>
            </div>

            <div class="commands-view" v-if="option.value === 'commands'">
              <p class="commands-reading-note">历史命令审计 · 只读记录</p>
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
                    >重试命令审计</Button
                  >
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
                v-else-if="commandsLoaded && !commandsError"
                title="暂无 Shell 命令"
              />
            </div>

            <div class="artifacts-view" v-if="option.value === 'artifacts'">
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
                    >重试资产清单</Button
                  >
                </template>
              </Alert>
              <Spinner v-if="isLoadingArtifacts" size="sm" label="正在读取关联资产" />
              <ul
                class="run-artifacts-list"
                v-if="artifacts.length > 0"
                aria-label="本次运行关联资产"
              >
                <li v-for="art in artifacts" :key="art.id">
                  <Button variant="ghost" class="run-artifact" @click="previewArtifact(art.id)">
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
              <EmptyState
                v-else-if="artifactsLoaded && !artifactsError"
                title="暂无产物证据"
                description="本轮运行中没有写入任何产物证据资产。"
              />
            </div>
          </div>
        </template>
      </Tabs>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ArrowLeft, ArrowUpRight, ChevronDown, FileText, RefreshCw, Wrench } from '@lucide/vue'
import { isActiveRun } from './runTimelineModel'
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
import type { TaskRunCommandEvent } from './runTypes'
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
  runBusinessTitle,
  workTimeline,
  workTitle,
  workActivity,
  toolCalls,
  hookReads,
  hookReadError,
  hookObservationIncomplete,
  expandedTools,
  toggleTools,
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

const showRunList = ref(false)
watch(selectedRunId, () => {
  showRunList.value = false
})
const runList = ref<HTMLElement | null>(null)
const listHeading = ref<HTMLElement | null>(null)
const detailHeading = ref<HTMLElement | null>(null)
const resumeReading = async () => {
  showRunList.value = false
  await nextTick()
  // 检索期间输出可能更新；只有开启自动滚动时才恢复到底部。
  if (autoScrollTerminal.value && terminalScreen.value) {
    terminalScreen.value.scrollTop = terminalScreen.value.scrollHeight
  }
  detailHeading.value?.focus({ preventScroll: true })
}
const lastRunTrigger = ref<HTMLElement | null>(null)
const timelineScroll = ref<HTMLElement | null>(null)
const hasNewProgress = ref(false)
const onTimelineScroll = () => {
  if ((timelineScroll.value?.scrollTop || 0) < 24) hasNewProgress.value = false
}
const backToLatest = () => {
  timelineScroll.value?.scrollTo({ top: 0 })
  hasNewProgress.value = false
}
// 只固定当前读到的工作锚点；新工作或工具更新不能把读者推回顶部。
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
const openRun = (id: string, event?: Event) => {
  lastRunTrigger.value = event?.currentTarget instanceof HTMLElement ? event.currentTarget : null
  selectRun(id)
  void resumeReading()
}
const returnToList = async () => {
  showRunList.value = true
  await nextTick()
  const currentRow = runList.value?.querySelector<HTMLElement>('.work-detail[aria-current="true"]')
  ;(lastRunTrigger.value?.isConnected
    ? lastRunTrigger.value
    : currentRow || listHeading.value
  )?.focus({ preventScroll: true })
}
const resultText = computed(() => (selectedRun.value ? runResultText(selectedRun.value) : ''))
const resultDuplicatesError = computed(
  () =>
    Boolean(selectedRun.value?.error_message || selectedRun.value?.error_type) &&
    resultText.value === (selectedRun.value?.error_message || selectedRun.value?.error_type),
)
const resultIsLong = computed(
  () => resultText.value.length > 240 || resultText.value.split('\n').length > 3,
)
const errorIsLong = computed(() => {
  const message = selectedRun.value?.error_message || ''
  return message.length > 240 || message.split('\n').length > 3
})
</script>

<style src="./RunsPage.css" scoped></style>
