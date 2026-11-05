<template>
  <div class="schedule-detail">
    <header class="schedule-heading">
      <router-link class="schedule-back" to="/projects?view=schedules"
        ><ArrowLeft :size="16" aria-hidden="true" />返回计划</router-link
      >
      <div class="schedule-identity">
        <h1>
          {{
            schedule?.project?.title || (schedule?.mode === 'once' ? '单次启动预约' : '计划详情')
          }}
        </h1>
        <Badge v-if="schedule" :tone="schedule.status === 'blocked' ? 'warning' : 'neutral'">{{
          scheduleStatusLabels[schedule.status]
        }}</Badge>
      </div>
      <Button variant="secondary" :loading="isFetching" @click="refresh">刷新</Button>
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
      <section class="schedule-overview" aria-label="下一次启动与计划操作">
        <div class="schedule-next">
          <p class="schedule-label">下一次启动</p>
          <p class="schedule-next-time">
            {{ scheduleLocalTime(schedule.next_run_at, schedule.timezone) }}
          </p>
          <dl class="schedule-rule">
            <div>
              <dt>启动规则</dt>
              <dd>{{ scheduleRuleLabel(schedule) }}</dd>
            </div>
            <div>
              <dt>时区</dt>
              <dd>{{ schedule.timezone }}</dd>
            </div>
          </dl>
          <Alert v-if="schedule.block_reason" tone="warning" title="当前阻塞">{{
            schedule.block_reason
          }}</Alert>
          <router-link
            v-if="schedule.project_id || schedule.last_project_id"
            class="schedule-project-link"
            :to="`/projects/${schedule.mode === 'once' ? schedule.project_id || schedule.last_project_id : schedule.last_project_id || schedule.project_id}`"
            >{{ schedule.mode === 'once' ? '进入关联项目' : '进入最近生成项目'
            }}<ArrowUpRight :size="16" aria-hidden="true"
          /></router-link>
          <p v-else class="schedule-muted">无可进入的关联项目</p>
        </div>
        <aside class="schedule-controls" aria-label="计划操作">
          <div class="schedule-actions">
            <template v-if="schedule.mode === 'once' && !onceStarted">
              <Button
                variant="secondary"
                size="sm"
                :disabled="operationBusy || operationUnknown"
                @click="openReschedule"
                >改期与代理设置</Button
              >
              <Button
                size="sm"
                :disabled="operationBusy || operationUnknown"
                @click="confirmAction = 'start_now'"
                >立即启动</Button
              >
              <div v-if="schedule.status !== 'cancelled'" class="schedule-destructive">
                <Button
                  variant="warning"
                  size="sm"
                  :disabled="operationBusy || operationUnknown"
                  @click="confirmAction = 'cancel'"
                  >取消预约</Button
                >
              </div>
            </template>
            <template
              v-else-if="
                schedule.mode !== 'once' &&
                ['active', 'paused', 'blocked'].includes(schedule.status)
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
              <div class="schedule-destructive">
                <Button
                  variant="danger"
                  size="sm"
                  :disabled="operationBusy || operationUnknown"
                  @click="confirmAction = 'disable'"
                  >停用计划</Button
                >
              </div>
            </template>
          </div>
          <Button
            v-if="editSnapshot && !editOpen"
            class="schedule-retained"
            variant="secondary"
            size="sm"
            @click="editOpen = true"
            >查看保留的编辑请求</Button
          >
          <p class="schedule-muted schedule-action-note">
            {{
              schedule.mode === 'once'
                ? '取消预约保留项目与输入，可重新改期或明确立即启动；预约激活仅代表等待调度。授权与执行停止边界由关联项目管理。'
                : '每轮创建独立项目，上轮未结束则跳过；错过与暂停期间不补跑。编辑、暂停或停用不改变已生成项目。'
            }}
          </p>
        </aside>
      </section>
      <section class="schedule-boundaries" aria-labelledby="schedule-boundaries-title">
        <h2 id="schedule-boundaries-title">执行边界</h2>
        <dl class="schedule-summary">
          <div>
            <dt>起始边界</dt>
            <dd>{{ scheduleLocalTime(schedule.start_at, schedule.timezone) }}</dd>
          </div>
          <template v-if="schedule.mode === 'once'">
            <div>
              <dt>执行停止</dt>
              <dd>
                {{
                  schedule.explore_deadline
                    ? scheduleLocalTime(schedule.explore_deadline, schedule.timezone)
                    : '不限制停止时间'
                }}
              </dd>
            </div>
            <div>
              <dt>授权到期</dt>
              <dd>
                {{
                  schedule.authorization_expires_at
                    ? scheduleLocalTime(schedule.authorization_expires_at, schedule.timezone)
                    : '不设授权到期'
                }}
              </dd>
            </div>
          </template>
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
      </section>
      <details v-if="schedule.project || schedule.mode === 'once'" class="schedule-inputs">
        <summary>
          <span>{{ schedule.mode === 'once' ? '关联项目资料' : '未来输入与已保存资料' }}</span><ChevronDown :size="16" aria-hidden="true" />
        </summary>
        <div class="schedule-input-body">
          <template v-if="schedule.project">
            <dl class="schedule-summary">
              <div>
                <dt>场景</dt>
                <dd>{{ schedule.project.scenario }}</dd>
              </div>
              <div>
                <dt>未来项目授权到期</dt>
                <dd>{{ schedule.project.authorization_expires_at || '不设授权到期' }}</dd>
              </div>
              <div>
                <dt>未来项目执行停止</dt>
                <dd>{{ schedule.project.explore_deadline || '不限制停止时间' }}</dd>
              </div>
            </dl>
            <div class="schedule-reading">
              <h3>Origin</h3>
              <pre>{{ schedule.project.origin }}</pre>
            </div>
            <div class="schedule-reading">
              <h3>Goal</h3>
              <pre>{{ schedule.project.goal }}</pre>
            </div>
            <section v-if="schedule.project.targets != null" class="schedule-reading">
              <h3>既有显式授权范围（只读）</h3>
              <ul v-if="schedule.project.targets.length">
                <li v-for="(target, index) in schedule.project.targets" :key="index">
                  {{ target.type }}：{{ target.value }}
                </li>
              </ul>
              <p v-else>既有显式范围为空；编辑未来配置不会将其改为 Origin 授权。</p>
            </section>
          </template>
          <section class="schedule-materials" :aria-label="schedule.mode === 'once' ? '关联项目资料' : '固定资料'">
            <h3>{{ schedule.mode === 'once' ? '关联项目资料' : '固定资料' }}</h3>
            <p v-if="schedule.mode === 'once' && !schedule.project_id" class="schedule-muted">
              关联项目已不可用，无法查看其资料。
            </p>
            <p v-else-if="schedule.mode === 'once' && schedule.once_project_materials_available !== true" class="schedule-muted">
              当前服务未提供关联项目的资料信息，请进入项目查看。
            </p>
            <template v-else>
              <ul v-if="schedule.resources.length" class="schedule-material-list">
                <li v-for="resource in schedule.resources" :key="resource.id">
                  <div>
                    <span>{{ resource.path }}</span
                    ><span class="schedule-muted"
                      >{{ resource.kind }} · {{ bytes(resource.size_bytes) }}</span
                    >
                  </div>
                  <details>
                    <summary>原件摘要</summary>
                    <span class="mono">{{ resource.digest_sha256 }}</span>
                  </details>
                </li>
              </ul>
              <ul v-if="schedule.library_bindings.length" class="schedule-material-list">
                <li v-for="binding in schedule.library_bindings" :key="binding.library_key">
                  <div>
                    <span>{{ binding.library_name }}</span
                    ><span class="schedule-muted"
                      >已保存 · {{ binding.file_count }} 文件</span
                    >
                  </div>
                </li>
              </ul>
              <p
                v-if="!schedule.resources.length && !schedule.library_bindings.length"
                class="schedule-muted"
              >
                未绑定固定资料
              </p>
            </template>
          </section>
        </div>
      </details>
      <section class="schedule-history" aria-label="计划运行历史">
        <div class="schedule-section-heading">
          <h2>运行历史</h2>
          <span v-if="history && !historyError" class="schedule-muted"
            >{{ history.total }} 条记录</span
          >
        </div>
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
        <div v-else class="history-table-scroll">
          <table class="history-table" aria-label="计划触发记录">
            <thead>
              <tr>
                <th scope="col">计划时刻</th>
                <th scope="col">结果</th>
                <th scope="col">配置版本</th>
                <th scope="col">生成项目</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in history.items" :key="item.id">
                <td>
                  {{ scheduleLocalTime(item.scheduled_at, schedule.timezone) }}
                  <div class="schedule-muted">{{ schedule.timezone }}</div>
                </td>
                <td>
                  <Badge :tone="item.status === 'blocked' ? 'warning' : 'neutral'" size="sm">{{
                    occurrenceStatusLabels[item.status]
                  }}</Badge>
                  <details>
                    <summary>{{ item.reason ? '查看原因与处理时间' : '查看处理时间' }}</summary>
                    <p v-if="item.reason">{{ item.reason }}</p>
                    <p>实际处理：{{ scheduleLocalTime(item.processed_at, schedule.timezone) }}</p>
                  </details>
                </td>
                <td>v{{ item.config_version }}</td>
                <td>
                  <router-link v-if="item.project_id" :to="`/projects/${item.project_id}`"
                    >进入项目</router-link
                  ><span v-else class="schedule-muted">无可进入的关联项目</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="history && history.total > 20" class="schedule-pagination">
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
      <Dialog
        :open="rescheduleOpen"
        title="调整单次启动时间与代理设置"
        description="不替换项目输入；改期不会取消或延长已有授权期限。"
        size="lg"
        :dismissible="!operationBusy"
        :close-on-outside="false"
        @update:open="!$event && closeReschedule()"
      >
        <fieldset :disabled="operationBusy" class="reschedule-fields">
          <ScheduleTiming ref="rescheduleTiming" v-model="timingDraft" locked-mode editing />
          <TestEgressFields
            v-model="rescheduleEgress"
            :auth-configured="rescheduleEgressPublic?.auth_configured"
            :disabled="operationBusy || operationUnknown"
            :error="rescheduleEgressError"
            @update:model-value="rescheduleEgressDirty = true"
          />
        </fieldset>
        <p class="schedule-muted">
          已有授权到期：{{
            rescheduleAuthorization
              ? `${scheduleLocalTime(rescheduleAuthorization, schedule.timezone)} ${schedule.timezone}`
              : '不设到期'
          }}。 仅预览启动规则，保存仍须通过关联项目的授权与执行条件校验；重新授权请进入关联项目。
        </p>
        <Alert
          v-if="operationError"
          :tone="operationUnknown ? 'warning' : 'danger'"
          :title="operationUnknown ? '操作结果未确认' : '改期未完成'"
        >
          {{ operationError }}
          <p v-if="operationUnknown">请求可能已提交，请先刷新确认最新状态；不要重复触发。</p>
          <template v-if="operationUnknown" #actions
            ><Button variant="secondary" :loading="isFetching" @click="refresh"
              >刷新核对</Button
            ></template
          >
        </Alert>
        <template #footer
          ><Button variant="secondary" :disabled="operationBusy" @click="closeReschedule"
            >取消</Button
          ><Button :loading="operationBusy" :disabled="operationUnknown" @click="saveReschedule"
            >保存改期与代理设置</Button
          ></template
        >
      </Dialog>
      <ConfirmAction
        :open="Boolean(discardResolver)"
        title="放弃未保存的代理设置？"
        description="取消将继续保留当前改期表单；放弃后不会保存本次代理修改。"
        tone="warning"
        confirm-label="放弃修改"
        cancel-label="继续编辑"
        @update:open="!$event && resolveDiscard(false)"
        @confirm="resolveDiscard(true)"
      />
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
import { computed, onBeforeUnmount, ref, shallowRef, toRaw, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute } from 'vue-router'
import type { TestEgressPublic } from '@/api/testEgress'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { ArrowLeft, ArrowUpRight, ChevronDown } from '@lucide/vue'
import { Alert, Badge, Button, Dialog, EmptyState, Skeleton } from '@/components/ui'
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
import TestEgressFields from '@/components/patterns/TestEgressFields.vue'
import { testEgressDraft, testEgressError, testEgressInput } from './testEgressForm'
import { newScheduleDraft, draftFromSchedule } from './scheduleTiming'
const route = useRoute()
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
const rescheduleEgress = ref(testEgressDraft())
const rescheduleEgressPublic = ref<TestEgressPublic>()
const rescheduleEgressDirty = ref(false)
const rescheduleEgressError = ref('')
const discardResolver = shallowRef<((allow: boolean) => void) | null>(null)
let discardPending: Promise<boolean> | null = null
function canLeaveReschedule(): boolean | Promise<boolean> {
  if (!rescheduleOpen.value) return true
  if (operationBusy.value) return false
  if (!rescheduleEgressDirty.value) return true
  // 路由守卫与浮层关闭等待同一个决定；取消不能继续导航或清空草稿。
  if (!discardPending)
    discardPending = new Promise<boolean>((resolve) => {
      discardResolver.value = resolve
    })
  return discardPending
}
function resolveDiscard(allow: boolean) {
  discardResolver.value?.(allow)
  discardResolver.value = null
  discardPending = null
}
async function closeReschedule() {
  if (!(await canLeaveReschedule())) return
  rescheduleOpen.value = false
  rescheduleEgress.value = testEgressDraft()
  rescheduleEgressDirty.value = false
}
onBeforeRouteLeave(canLeaveReschedule)
onBeforeRouteUpdate(canLeaveReschedule)
function beforeUnload(event: BeforeUnloadEvent) {
  if (rescheduleOpen.value && (operationBusy.value || rescheduleEgressDirty.value)) {
    event.preventDefault()
    event.returnValue = ''
  }
}
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', beforeUnload)
  resolveDiscard(false)
})
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
  rescheduleEgressPublic.value = schedule.value.test_egress
  rescheduleEgress.value = testEgressDraft(schedule.value.test_egress)
  rescheduleEgressDirty.value = false
  rescheduleEgressError.value = ''
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
  rescheduleEgressError.value = testEgressError(rescheduleEgress.value)
  if (!timing || rescheduleEgressError.value) return
  const { rules, deadline } = timing
  if (
    rescheduleAuthorization.value &&
    Date.parse(rules.start_at) >= Date.parse(rescheduleAuthorization.value)
  ) {
    operationError.value =
      '启动时间必须早于已有授权到期时间；改期不会自动续期，请在关联项目中重新授权。'
    return
  }
  operationBusy.value = true
  operationError.value = ''
  try {
    applySchedule(
      await updateProjectSchedule(id.value, {
        ...rules,
        ...(rescheduleEgressDirty.value
          ? { test_egress: testEgressInput(rescheduleEgress.value) }
          : {}),
        ...(deadline !== originalDeadline.value ? { explore_deadline: deadline } : {}),
        version: rescheduleVersion.value,
      }),
    )
    rescheduleOpen.value = false
    rescheduleEgress.value = testEgressDraft()
    rescheduleEgressDirty.value = false
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
  display: grid;
  gap: var(--space-5);
}
.schedule-detail {
  padding: var(--layout-content-padding);
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  font-size: var(--text-md);
}
.schedule-back {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--text-muted);
  font-size: var(--text-control);
  flex: none;
}
.schedule-heading,
.schedule-identity,
.schedule-section-heading {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.schedule-heading {
  justify-content: space-between;
  flex-wrap: wrap;
  padding-bottom: var(--space-4);
}
.schedule-identity {
  flex: 1;
  flex-wrap: wrap;
  min-width: 0;
}
.schedule-heading h1 {
  font-size: var(--text-object);
  font-weight: var(--font-semibold);
  line-height: var(--leading-snug);
  letter-spacing: var(--tracking-tight);
  overflow-wrap: anywhere;
  margin: 0;
}
.schedule-detail h2 {
  font-size: var(--text-section);
  font-weight: var(--font-semibold);
  margin: 0;
}
.schedule-detail h3 {
  font-size: var(--text-section);
  font-weight: var(--font-semibold);
  margin: 0 0 var(--space-3);
}
.schedule-overview {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(320px, 0.45fr);
  gap: var(--space-6);
  padding: var(--space-5) 0 var(--space-6);
  border-block: 1px solid var(--border);
}
.schedule-next {
  min-width: 0;
  display: grid;
  align-content: start;
  gap: var(--space-4);
}
.schedule-label {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--text-sm);
}
.schedule-next-time {
  font-size: var(--text-2xl);
  font-weight: var(--font-medium);
  font-variant-numeric: tabular-nums;
  line-height: var(--leading-snug);
  margin: 0;
  overflow-wrap: anywhere;
}
.schedule-rule {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4) var(--space-6);
  margin: 0;
}
.schedule-rule div {
  display: flex;
  gap: var(--space-3);
}
.schedule-rule dt {
  color: var(--text-muted);
}
.schedule-rule dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.schedule-project-link {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  justify-self: start;
}
.schedule-controls {
  border-left: 1px solid var(--border);
  padding-left: var(--space-5);
  min-width: 0;
}
.schedule-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}
.schedule-actions :deep(.ui-button) {
  flex: 1 0 auto;
}
.schedule-destructive {
  flex-basis: 100%;
  padding-top: var(--space-3);
  margin-top: var(--space-1);
  border-top: 1px solid var(--border);
}
.schedule-retained {
  margin-top: var(--space-3);
}
.schedule-action-note {
  margin: var(--space-4) 0 0;
  line-height: var(--leading-relaxed);
}
.schedule-boundaries {
  padding-block: var(--space-5);
}
.schedule-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--space-5);
  margin: var(--space-4) 0 0;
}
.schedule-summary dt,
.schedule-muted {
  color: var(--text-muted);
  font-size: var(--text-sm);
}
.schedule-summary dd {
  margin: var(--space-2) 0 0;
  overflow-wrap: anywhere;
  line-height: var(--leading-normal);
}
.schedule-inputs {
  border-block: 1px solid var(--border);
}
.schedule-inputs > summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding-block: var(--space-4);
  font-weight: var(--font-medium);
  list-style: none;
}
.schedule-inputs > summary::-webkit-details-marker {
  display: none;
}
.schedule-inputs[open] > summary svg {
  transform: rotate(180deg);
}
.schedule-input-body {
  padding: 0 0 var(--space-5);
  display: grid;
  gap: var(--space-5);
}
.schedule-input-body .schedule-summary {
  margin-top: 0;
}
.schedule-reading {
  max-width: var(--layout-content-narrow);
  min-width: 0;
}
.schedule-reading pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
  font-size: var(--text-body);
  line-height: var(--leading-reading);
  margin: 0;
}
.schedule-material-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.schedule-material-list li {
  padding-block: var(--space-3);
  border-bottom: 1px solid var(--border);
  overflow-wrap: anywhere;
}
.schedule-material-list li > div {
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--space-2) var(--space-4);
}
.schedule-history {
  padding-top: var(--space-5);
}
.schedule-section-heading {
  justify-content: space-between;
  margin-bottom: var(--space-4);
}
.history-table-scroll {
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
}
.history-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
}
.history-table th {
  padding: var(--space-3) var(--space-4);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  color: var(--text-muted);
  background: var(--bg-inset);
}
.history-table td {
  padding: var(--space-4);
  border-top: 1px solid var(--border);
  vertical-align: top;
  min-width: 100px;
  overflow-wrap: anywhere;
}
.history-table td:first-child {
  width: 30%;
  font-variant-numeric: tabular-nums;
}
.history-table td:nth-child(2) {
  width: 40%;
}
.history-table .schedule-muted {
  margin-top: var(--space-1);
}
.history-table details,
.schedule-material-list details {
  margin-top: var(--space-2);
  font-size: var(--text-sm);
}
.history-table details p {
  white-space: pre-wrap;
}
.schedule-detail summary {
  cursor: pointer;
}
.schedule-detail summary:focus-visible,
.schedule-detail a:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
  border-radius: var(--radius-sm);
}
.schedule-pagination {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: var(--space-3);
  padding-top: var(--space-4);
  color: var(--text-muted);
  font-size: var(--text-sm);
}
@media (max-width: 700px) {
  .schedule-detail {
    padding: var(--space-5) var(--space-4);
  }
  .schedule-overview,
  .schedule-summary {
    grid-template-columns: 1fr;
  }
  .schedule-controls {
    border-left: 0;
    border-top: 1px solid var(--border);
    padding: var(--space-4) 0 0;
  }
}
</style>
