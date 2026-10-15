<template>
  <div class="schedule-detail">
    <header class="schedule-heading">
      <Button variant="secondary" size="sm" @click="router.push('/projects?view=schedules')"
        ><template #icon><ArrowLeft :size="14" /></template>计划任务</Button
      >
      <h2>
        {{ schedule?.project?.title || (schedule?.mode === 'once' ? '单次启动预约' : '计划详情') }}
      </h2>
      <Button variant="secondary" size="sm" :loading="isFetching" @click="refresh">刷新</Button>
    </header>
    <Skeleton v-if="isLoading" />
    <ErrorState
      v-else-if="isError && !schedule"
      title="计划详情加载失败"
      description="请确认计划仍存在且有访问权限。"
      @retry="refresh"
    />
    <template v-else-if="schedule">
      <Alert v-if="isError" tone="warning" title="计划刷新失败"
        >当前显示最近取得的配置，管理操作由服务端校验版本。</Alert
      >
      <Alert
        v-if="operationError"
        :tone="operationUnknown ? 'warning' : 'danger'"
        :title="operationUnknown ? '操作结果未确认' : '计划操作失败'"
        >{{ operationError }}
        <p v-if="operationUnknown">请求可能已提交，请先刷新确认最新状态；不要重复触发。</p></Alert
      >
      <div class="schedule-actions">
        <Badge :tone="schedule.status === 'blocked' ? 'warning' : 'neutral'">{{
          scheduleStatusLabels[schedule.status]
        }}</Badge>
        <template v-if="schedule.mode === 'once' && !onceStarted">
          <Button
            variant="secondary"
            size="sm"
            :disabled="operationBusy || operationUnknown"
            @click="openReschedule"
            >改期</Button
          >
          <Button
            size="sm"
            :disabled="operationBusy || operationUnknown"
            @click="confirmAction = 'start_now'"
            >立即启动</Button
          >
          <Button
            v-if="schedule.status !== 'cancelled'"
            variant="warning"
            size="sm"
            :disabled="operationBusy || operationUnknown"
            @click="confirmAction = 'cancel'"
            >取消预约</Button
          >
        </template>
        <template
          v-else-if="
            schedule.mode !== 'once' && ['active', 'paused', 'blocked'].includes(schedule.status)
          "
        >
          <Button
            variant="secondary"
            size="sm"
            :disabled="operationBusy || operationUnknown"
            @click="openEdit($event)"
            >编辑未来配置</Button
          >
          <Button
            v-if="schedule.status === 'active'"
            variant="secondary"
            size="sm"
            :disabled="operationBusy || operationUnknown"
            @click="performAction('pause')"
            >暂停计划</Button
          >
          <Button
            v-if="['paused', 'blocked'].includes(schedule.status)"
            size="sm"
            :disabled="operationBusy || operationUnknown"
            @click="performAction('resume')"
            >恢复未来运行</Button
          >
          <Button
            variant="danger"
            size="sm"
            :disabled="operationBusy || operationUnknown"
            @click="confirmAction = 'disable'"
            >停用计划</Button
          >
        </template>
        <Button
          v-if="schedule.project_id || schedule.last_project_id"
          variant="secondary"
          size="sm"
          @click="router.push(`/projects/${schedule.project_id || schedule.last_project_id}`)"
          >{{ schedule.mode === 'once' ? '进入关联项目' : '进入最近项目' }}</Button
        >
      </div>
      <Alert v-if="schedule.block_reason" tone="warning" title="需要处理">{{
        schedule.block_reason
      }}</Alert>
      <p class="schedule-muted">
        {{
          schedule.mode === 'once'
            ? '预约激活只代表等待执行调度；取消预约保留项目和输入，之后可明确立即启动或重新改期。授权与执行停止边界在关联项目中管理。'
            : '每轮创建独立项目，上轮未结束则跳过；停机错过与暂停期间不补跑。暂停、停用和编辑不停止或改写已经生成的项目。'
        }}
      </p>
      <dl class="schedule-summary">
        <div>
          <dt>启动规则</dt>
          <dd>{{ scheduleRuleLabel(schedule) }} · {{ schedule.timezone }}</dd>
        </div>
        <div>
          <dt>起始边界</dt>
          <dd>{{ scheduleLocalTime(schedule.start_at, schedule.timezone) }}</dd>
        </div>
        <div v-if="schedule.mode === 'once'">
          <dt>执行停止 / 授权到期</dt>
          <dd>{{ schedule.explore_deadline ? scheduleLocalTime(schedule.explore_deadline, schedule.timezone) : '不限制停止时间' }} / {{ schedule.authorization_expires_at ? scheduleLocalTime(schedule.authorization_expires_at, schedule.timezone) : '不设授权到期' }}</dd>
        </div>
        <div>
          <dt>下一次</dt>
          <dd>{{ scheduleLocalTime(schedule.next_run_at, schedule.timezone) }}</dd>
        </div>
        <div>
          <dt>结束边界</dt>
          <dd>
            {{ schedule.end_date ? `${schedule.end_date} 该日结束后不再启动` : '无日期限制' }} ·
            {{
              schedule.max_runs ? `成功生成 ${schedule.max_runs} 个项目后结束` : '无成功次数限制'
            }}
          </dd>
        </div>
        <div>
          <dt>已生成 / 配置版本</dt>
          <dd>{{ schedule.generated_count }} 个 · v{{ schedule.version }}</dd>
        </div>
        <div>
          <dt>计划 ID</dt>
          <dd class="mono">{{ schedule.id }}</dd>
        </div>
      </dl>
      <details v-if="schedule.project" class="schedule-inputs">
        <summary>未来输入与固定资料版本</summary>
        <h3>Origin</h3>
        <pre>{{ schedule.project.origin }}</pre>
        <h3>Goal</h3>
        <pre>{{ schedule.project.goal }}</pre>
        <p>场景：{{ schedule.project.scenario }} · 项目组：{{ schedule.project.group_id }}</p>
        <p>Worker：{{ schedule.project.tenant_worker_ids.join('、') }}</p>
        <p>
          授权到期：{{ schedule.project.authorization_expires_at || '不限' }} · 执行停止：{{
            schedule.project.explore_deadline || '不限'
          }}
        </p>
        <template v-if="schedule.project.targets != null">
          <h3>既有显式授权范围（只读）</h3>
          <ul v-if="schedule.project.targets.length">
            <li v-for="(target, index) in schedule.project.targets" :key="index">{{ target.type }}：{{ target.value }}</li>
          </ul>
          <p v-else>既有显式范围为空；编辑未来配置不会将其改为 Origin 授权。</p>
        </template>
        <ul>
          <li v-for="resource in schedule.resources" :key="resource.id">
            {{ resource.path }} · {{ resource.kind }} · {{ bytes(resource.size_bytes) }}
            <details>
              <summary>原件摘要</summary>
              <span class="mono">{{ resource.digest_sha256 }}</span>
            </details>
          </li>
        </ul>
        <ul>
          <li v-for="binding in schedule.library_bindings" :key="binding.library_key">
            {{ binding.library_name }} · 固定 v{{ binding.version }} · {{ binding.file_count }} 文件
          </li>
        </ul>
      </details>
      <section class="schedule-history" aria-label="计划运行历史">
        <h3>运行历史</h3>
        <Skeleton v-if="historyLoading" />
        <ErrorState
          v-else-if="historyError"
          title="运行历史加载失败"
          description="请重试获取历史。"
          @retry="refetchHistory()"
        />
        <EmptyState
          v-else-if="!history?.items.length"
          title="尚无触发记录"
          description="保存计划不代表已经运行。后台处理后会在此记录启动、跳过、错过或受阻。"
        />
        <ol v-else class="history-list">
          <li v-for="item in history.items" :key="item.id">
            <div>
              <Badge :tone="item.status === 'blocked' ? 'warning' : 'neutral'" size="sm">{{
                occurrenceStatusLabels[item.status]
              }}</Badge>
              {{ scheduleLocalTime(item.scheduled_at, schedule.timezone) }} · 配置 v{{
                item.config_version
              }}
              <router-link v-if="item.project_id" :to="`/projects/${item.project_id}`"
                >进入项目</router-link
              >
            </div>
            <details>
              <summary>{{ item.reason ? '查看原因与处理时间' : '查看处理时间' }}</summary>
              <p v-if="item.reason">{{ item.reason }}</p>
              <p>实际处理：{{ scheduleLocalTime(item.processed_at, schedule.timezone) }}</p>
            </details>
          </li>
        </ol>
        <div v-if="history && history.total > 20" class="schedule-actions">
          <Button variant="secondary" size="sm" :disabled="offset === 0" @click="offset -= 20"
            >上一页</Button
          >
          <span
            >{{ offset + 1 }}–{{ Math.min(offset + 20, history.total) }} / {{ history.total }}</span
          >
          <Button
            variant="secondary"
            size="sm"
            :disabled="offset + 20 >= history.total"
            @click="offset += 20"
            >下一页</Button
          >
        </div>
      </section>
      <NewProject
        v-if="editSnapshot"
        :schedule="editSnapshot"
        :open="editOpen"
        :return-focus="editTrigger"
        @update:open="editOpen = $event"
        @discarded="discardEdit"
        @saved="onSaved"
      />
      <Button
        v-if="editSnapshot && !editOpen"
        variant="secondary"
        size="sm"
        @click="editOpen = true"
        >查看保留的编辑请求</Button
      >
      <Dialog
        :open="rescheduleOpen"
        title="调整单次启动时间"
        description="不替换项目输入；改期不会取消或延长已有授权期限。"
        :dismissible="!operationBusy"
        @update:open="rescheduleOpen = $event"
      >
        <fieldset :disabled="operationBusy" class="reschedule-fields">
          <ScheduleTiming ref="rescheduleTiming" v-model="timingDraft" locked-mode editing />
        </fieldset>
        <p class="schedule-muted">
          已有授权到期：{{ rescheduleAuthorization ? `${scheduleLocalTime(rescheduleAuthorization, schedule.timezone)} ${schedule.timezone}` : '不设到期' }}。
          仅预览启动规则，保存仍须通过关联项目的授权与执行条件校验；重新授权请进入关联项目。
        </p>
        <Alert v-if="operationError" tone="danger" title="改期未完成">{{ operationError }}</Alert>
        <template #footer
          ><Button variant="secondary" :disabled="operationBusy" @click="rescheduleOpen = false"
            >取消</Button
          ><Button :loading="operationBusy" :disabled="operationUnknown" @click="saveReschedule"
            >保存改期</Button
          ></template
        >
      </Dialog>
      <ConfirmAction
        :open="Boolean(confirmAction)"
        :title="actionTitle"
        :description="actionDescription"
        :confirm-label="actionTitle"
        :loading="operationBusy"
        @update:open="
          (value) => {
            if (!value && !operationBusy) confirmAction = null
          }
        "
        @confirm="confirmManagementAction"
      />
    </template>
  </div>
</template>
<script setup lang="ts">
import { computed, ref, shallowRef, toRaw, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { ArrowLeft } from '@lucide/vue'
import {
  Alert,
  Badge,
  Button,
  Dialog,
  EmptyState,
  Skeleton,
} from '@/components/ui'
import { ConfirmAction, ErrorState } from '@/components/patterns'
import { ApiError } from '@/api/client'
import {
  getProjectSchedule,
  getProjectScheduleHistory,
  updateProjectSchedule,
  actOnProjectSchedule,
  occurrenceStatusLabels,
  scheduleLocalTime,
  scheduleRuleLabel,
  scheduleStatusLabels,
  type ProjectSchedule,
  type ScheduleAction,
} from '@/api/projectSchedules'
import { bytes } from '@/lib/format'
import NewProject from './NewProject.vue'
import ScheduleTiming from './ScheduleTiming.vue'
import { newScheduleDraft, draftFromSchedule } from './scheduleTiming'
const route = useRoute()
const router = useRouter()
const client = useQueryClient()
const id = computed(() => String(route.params.id))
const {
  data: schedule,
  isLoading,
  isFetching,
  isError,
  refetch,
} = useQuery({
  queryKey: computed(() => ['project-schedule', id.value]),
  queryFn: () => getProjectSchedule(id.value),
  refetchInterval: 8000,
  retry: false,
})
const offset = ref(0)
const {
  data: history,
  isLoading: historyLoading,
  isError: historyError,
  refetch: refetchHistory,
} = useQuery({
  queryKey: computed(() => ['project-schedule-history', id.value, offset.value]),
  queryFn: () => getProjectScheduleHistory(id.value, offset.value),
  refetchInterval: 8000,
  retry: false,
})
const operationBusy = ref(false)
const operationError = ref('')
const operationUnknown = ref(false)
const confirmAction = ref<ScheduleAction | null>(null)
const editSnapshot = ref<ProjectSchedule | null>(null)
const editOpen = ref(false)
const editTrigger = shallowRef<HTMLElement | null>(null)
const rescheduleOpen = ref(false)
const rescheduleVersion = ref(0)
const originalDeadline = ref<string | null>(null)
const rescheduleAuthorization = ref<string | null>(null)
const rescheduleTiming = ref<InstanceType<typeof ScheduleTiming> | null>(null)
const timingDraft = ref(newScheduleDraft())
const onceStarted = computed(() =>
  Boolean(
    schedule.value?.generated_count ||
    schedule.value?.last_result?.status === 'started' ||
    schedule.value?.status === 'completed',
  ),
)
const actionTitle = computed(() =>
  confirmAction.value === 'start_now'
    ? '立即启动项目'
    : confirmAction.value === 'cancel'
      ? '取消预约'
      : '停用计划',
)
const actionDescription = computed(() =>
  confirmAction.value === 'start_now'
    ? '将待启动项目进入执行调度，可能产生模型费用；此操作仍由服务端校验授权与执行条件。'
    : confirmAction.value === 'cancel'
      ? '仅撤销自动启动，保留待启动项目及原计划关联，不删除输入。'
      : '停止未来生成，保留计划、资料及历史项目，不停止已经生成的项目。',
)
watch(id, () => {
  offset.value = 0
  editSnapshot.value = null
  editOpen.value = false
  rescheduleOpen.value = false
  operationError.value = ''
  operationUnknown.value = false
})
async function refresh() {
  const result = await refetch()
  if (result.isSuccess) {
    operationUnknown.value = false
    operationError.value = ''
  }
  await refetchHistory()
}
function applySchedule(value: ProjectSchedule) {
  client.setQueryData(['project-schedule', value.id], value)
  void client.invalidateQueries({ queryKey: ['project-schedules'] })
  void client.invalidateQueries({ queryKey: ['project-schedule-history', value.id] })
  void client.invalidateQueries({ queryKey: ['projects'] })
}
function reportError(error: unknown) {
  operationUnknown.value = !(
    error instanceof ApiError &&
    error.status >= 400 &&
    error.status < 500 &&
    error.status !== 408
  )
  operationError.value = error instanceof Error ? error.message : '未取得操作结果'
}
async function performAction(action: ScheduleAction) {
  if (!schedule.value || operationBusy.value || operationUnknown.value) return
  operationBusy.value = true
  operationError.value = ''
  try {
    applySchedule(await actOnProjectSchedule(id.value, schedule.value.version, action))
    confirmAction.value = null
  } catch (error) {
    reportError(error)
    confirmAction.value = null
  } finally {
    operationBusy.value = false
  }
}
async function confirmManagementAction() {
  if (confirmAction.value) await performAction(confirmAction.value)
}
function openEdit(event: Event) {
  if (!schedule.value) return
  if (!editSnapshot.value) editSnapshot.value = structuredClone(toRaw(schedule.value))
  editTrigger.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  editOpen.value = true
}
function discardEdit() {
  editSnapshot.value = null
  editOpen.value = false
}
function onSaved(value: ProjectSchedule) {
  applySchedule(value)
  editOpen.value = false
  editSnapshot.value = null
}
function openReschedule() {
  if (!schedule.value) return
  timingDraft.value = draftFromSchedule(schedule.value)
  rescheduleVersion.value = schedule.value.version
  originalDeadline.value = schedule.value.explore_deadline || null
  rescheduleAuthorization.value = schedule.value.authorization_expires_at || null
  operationError.value = ''
  rescheduleOpen.value = true
}
async function saveReschedule() {
  if (operationBusy.value || operationUnknown.value) return
  operationError.value = ''
  const timing = rescheduleTiming.value!.validate()
  if (!timing) return
  const { rules, deadline } = timing
  if (rescheduleAuthorization.value && Date.parse(rules.start_at) >= Date.parse(rescheduleAuthorization.value)) {
    operationError.value = '启动时间必须早于已有授权到期时间；改期不会自动续期，请在关联项目中重新授权。'
    return
  }
  operationBusy.value = true
  operationError.value = ''
  try {
    applySchedule(
      await updateProjectSchedule(id.value, {
        ...rules,
        ...(deadline !== originalDeadline.value ? { explore_deadline: deadline } : {}),
        version: rescheduleVersion.value,
      }),
    )
    rescheduleOpen.value = false
  } catch (error) {
    reportError(error)
  } finally {
    operationBusy.value = false
  }
}
</script>
<style scoped>
.reschedule-fields {
  border: 0;
  padding: 0;
  margin: 0;
  min-width: 0;
}
.schedule-detail {
  padding: var(--space-4);
  min-width: 0;
  overflow-y: auto;
}
.schedule-heading,
.schedule-actions {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
  margin-bottom: var(--space-4);
}
.schedule-heading h2 {
  flex: 1;
  font-size: var(--text-lg);
  margin: 0;
}
.schedule-summary {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-4);
  padding-block: var(--space-4);
  border-block: 1px solid var(--border);
}
.schedule-summary dt,
.schedule-muted {
  color: var(--text-muted);
  font-size: var(--text-xs);
}
.schedule-summary dd {
  margin: var(--space-1) 0 0;
  overflow-wrap: anywhere;
}
.schedule-inputs,
.schedule-history {
  margin-block: var(--space-4);
}
.schedule-inputs summary,
.history-list summary {
  cursor: pointer;
}
.schedule-inputs pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
}
.history-list {
  list-style: none;
  padding: 0;
}
.history-list li {
  padding-block: var(--space-3);
  border-bottom: 1px solid var(--border);
}
.history-list a {
  margin-left: var(--space-3);
}
@media (max-width: 700px) {
  .schedule-summary {
    grid-template-columns: 1fr;
  }
}
</style>
