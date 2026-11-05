<template>
  <section class="schedule-timing" aria-label="启动安排">
    <FormField v-if="showMode" label="启动安排" for="schedule-mode">
      <Select
        id="schedule-mode"
        :model-value="modelValue.mode"
        :options="modeOptions"
        :disabled="lockedMode && modelValue.mode === 'once'"
        @update:model-value="update('mode', $event as ScheduleDraft['mode'])"
      />
    </FormField>
    <p v-if="showMode && modelValue.mode === 'immediate'" class="timing-hint">
      资料绑定成功后进入执行调度，不代表模型已启动。
    </p>
    <template v-if="modelValue.mode !== 'immediate'">
      <section class="timing-section" aria-label="启动时间与时区">
        <div class="timing-section-heading">
          <h3>{{ modelValue.mode === 'once' ? '启动时间' : '首次启动边界' }}</h3>
        </div>
        <div class="timing-grid">
          <FormField label="时区（IANA）" for="schedule-timezone">
            <Input
              id="schedule-timezone"
              :model-value="modelValue.timezone"
              placeholder="Asia/Shanghai"
              @update:model-value="update('timezone', String($event ?? ''))"
            />
          </FormField>
          <FormField
            :label="modelValue.mode === 'once' ? '启动日期与本地时间' : '起始边界（含该时刻）'"
            for="schedule-start"
            :error="timeError?.field === 'start' ? timeError.message : ''"
          >
            <Input
              id="schedule-start"
              type="datetime-local"
              :model-value="modelValue.startLocal"
              @update:model-value="update('startLocal', String($event ?? ''))"
            />
          </FormField>
          <FormField
            v-if="candidates.length > 1"
            label="该时刻出现两次，请选择偏移"
            for="schedule-offset"
          >
            <Select
              id="schedule-offset"
              :model-value="selectedOffset"
              :options="candidates"
              placeholder="请选择 UTC 偏移"
              @update:model-value="update('startAt', $event)"
            />
          </FormField>
        </div>
      </section>
      <section v-if="modelValue.mode !== 'once'" class="timing-section" aria-label="重复规则">
        <div class="timing-section-heading">
          <h3>重复规则</h3>
          <p class="timing-hint">
            每轮可能产生模型费用；上轮未结束则跳过，错过与暂停期间不补跑。编辑仅影响未来项目。
          </p>
        </div>
        <div class="timing-grid">
          <FormField label="每轮本地时刻" for="schedule-local-time">
            <Input
              id="schedule-local-time"
              type="time"
              :model-value="modelValue.localTime"
              @update:model-value="update('localTime', String($event ?? ''))"
            />
          </FormField>
          <FormField v-if="modelValue.mode === 'weekly'" label="每周执行日">
            <div class="weekday-options">
              <label v-for="(label, day) in weekdayLabels" :key="day">
                <Checkbox
                  :model-value="modelValue.weekdays.includes(day)"
                  @update:model-value="toggleDay(day, $event)"
                />{{ label }}
              </label>
            </div>
          </FormField>
        </div>
      </section>
      <section
        class="timing-section"
        :aria-label="modelValue.mode === 'once' ? '执行停止边界' : '计划结束边界'"
      >
        <div class="timing-section-heading">
          <h3>{{ modelValue.mode === 'once' ? '执行停止边界' : '计划结束边界' }}</h3>
        </div>
        <div v-if="modelValue.mode === 'once'" class="timing-grid">
          <FormField label="停止时间" for="schedule-stop-mode">
            <Select
              id="schedule-stop-mode"
              :model-value="modelValue.stopMode"
              :options="stopModeOptions"
              @update:model-value="update('stopMode', $event as ScheduleDraft['stopMode'])"
            />
          </FormField>
          <FormField
            v-if="modelValue.stopMode === 'specified'"
            label="停止日期与本地时间"
            for="schedule-stop"
            required
            :error="timeError?.field === 'stop' ? timeError.message : ''"
          >
            <Input
              id="schedule-stop"
              type="datetime-local"
              :model-value="modelValue.stopLocal"
              @update:model-value="update('stopLocal', String($event ?? ''))"
            />
          </FormField>
          <FormField
            v-if="modelValue.stopMode === 'specified' && stopCandidates.length > 1"
            label="停止时刻出现两次，请选择偏移"
            for="schedule-stop-offset"
          >
            <Select
              id="schedule-stop-offset"
              :model-value="selectedStopOffset"
              :options="stopCandidates"
              placeholder="请选择 UTC 偏移"
              @update:model-value="update('stopAt', $event)"
            />
          </FormField>
        </div>
        <div v-if="modelValue.mode !== 'once'" class="timing-grid">
          <FormField
            label="结束日期（可选）"
            for="schedule-end"
            hint="按所选时区，该日结束后不再启动。"
          >
            <Input
              id="schedule-end"
              type="date"
              :model-value="modelValue.endDate"
              @update:model-value="update('endDate', String($event ?? ''))"
            />
          </FormField>
          <FormField
            label="成功生成次数上限（可选）"
            for="schedule-max"
            hint="跳过、错过、受阻不扣次数。留空不限制。"
          >
            <Input
              id="schedule-max"
              type="number"
              min="1"
              step="1"
              :model-value="modelValue.maxRuns"
              @update:model-value="update('maxRuns', String($event ?? ''))"
            />
          </FormField>
        </div>
        <p v-if="boundaryHint" class="timing-hint timing-boundary-note">{{ boundaryHint }}</p>
      </section>
      <section class="timing-preview-section" aria-label="服务端时间预览">
        <div class="timing-preview-heading">
          <h3>未来启动时间</h3>
          <Button variant="secondary" :loading="previewBusy" @click="previewTimes"
            >预览未来时间</Button
          >
        </div>
        <Alert
          v-if="timeError?.field === 'rules' || previewError"
          tone="danger"
          title="时间安排需处理"
          >{{ previewError || timeError?.message }}</Alert
        >
        <div v-if="preview" class="timing-preview" aria-live="polite">
          <ol v-if="preview.times.length">
            <li v-for="time in preview.times" :key="time.at">
              <span class="timing-preview-local">{{ time.local }}</span>
              <span class="timing-hint">{{ modelValue.timezone }} · {{ time.at }}</span>
              <span v-if="time.note" class="timing-hint">{{ time.note }}</span>
            </li>
          </ol>
          <p v-else>所选规则下没有未来执行时刻。</p>
          <p v-if="preview.boundary" class="timing-hint">{{ preview.boundary }}</p>
        </div>
      </section>
    </template>
  </section>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Alert, Button, Checkbox, FormField, Input, Select } from '@/components/ui'
import { previewProjectSchedule, weekdayLabels, type SchedulePreview } from '@/api/projectSchedules'
import {
  localTimeCandidates,
  scheduleModeOptions,
  selectedTimeCandidate,
  scheduleTimesFromDraft,
  ScheduleTimeError,
  type ScheduleDraft,
} from './scheduleTiming'
import type { CreateProjectRequest } from '@/api/types'
const props = withDefaults(
  defineProps<{
    modelValue: ScheduleDraft
    lockedMode?: boolean
    editing?: boolean
    boundaryHint?: string
    project?: CreateProjectRequest
    showMode?: boolean
  }>(),
  { showMode: true },
)
const emit = defineEmits<{ 'update:modelValue': [value: ScheduleDraft] }>()
const modeOptions = computed(() => scheduleModeOptions(props.modelValue.mode, props.lockedMode))
const stopModeOptions = [
  { value: 'unlimited', label: '不限制停止时间' },
  { value: 'specified', label: '指定停止时间' },
]
const timeError = ref<ScheduleTimeError | null>(null)
const previewError = ref('')
const previewBusy = ref(false)
const preview = ref<SchedulePreview | null>(null)
const candidates = computed(() => {
  try {
    return localTimeCandidates(props.modelValue.startLocal, props.modelValue.timezone)
  } catch (error) {
    if (error instanceof RangeError) return []
    throw error
  }
})
const selectedOffset = computed(
  () => selectedTimeCandidate(candidates.value, props.modelValue.startAt)?.value || '',
)
const stopCandidates = computed(() => {
  if (props.modelValue.mode !== 'once' || props.modelValue.stopMode !== 'specified') return []
  try {
    return localTimeCandidates(props.modelValue.stopLocal, props.modelValue.timezone)
  } catch (error) {
    if (error instanceof RangeError) return []
    throw error
  }
})
const selectedStopOffset = computed(
  () => selectedTimeCandidate(stopCandidates.value, props.modelValue.stopAt)?.value || '',
)
function update<K extends keyof ScheduleDraft>(key: K, value: ScheduleDraft[K]) {
  emit('update:modelValue', {
    ...props.modelValue,
    [key]: value,
    ...(key === 'startLocal' || key === 'timezone' ? { startAt: '' } : {}),
    ...(key === 'stopLocal' || key === 'timezone' ? { stopAt: '' } : {}),
  })
}
function toggleDay(day: number, checked: boolean) {
  update(
    'weekdays',
    checked
      ? [...props.modelValue.weekdays, day]
      : props.modelValue.weekdays.filter((item) => item !== day),
  )
}
let revision = 0
watch(
  () => [props.modelValue, props.project],
  () => {
    revision++
    preview.value = null
    timeError.value = null
    previewError.value = ''
  },
  { deep: true },
)
function validate() {
  timeError.value = null
  try {
    return scheduleTimesFromDraft(
      props.modelValue,
      props.editing && props.modelValue.mode !== 'once',
    )
  } catch (error) {
    timeError.value =
      error instanceof ScheduleTimeError
        ? error
        : new ScheduleTimeError(error instanceof Error ? error.message : '时间无效', 'rules')
    return null
  }
}
defineExpose({ validate })
async function previewTimes() {
  if (previewBusy.value) return
  previewError.value = ''
  const timing = validate()
  if (!timing) return
  const { rules, deadline } = timing
  previewBusy.value = true
  const currentRevision = revision
  try {
    const result = await previewProjectSchedule({
      ...rules,
      ...(props.project
        ? {
            project: {
              ...props.project,
              ...(props.modelValue.mode === 'once' ? { explore_deadline: deadline } : {}),
            },
          }
        : {}),
    })
    if (currentRevision === revision) preview.value = result
  } catch (error) {
    if (currentRevision === revision)
      previewError.value = error instanceof Error ? error.message : '预览失败'
  } finally {
    previewBusy.value = false
  }
}
</script>
<style scoped>
.schedule-timing {
  display: grid;
  gap: var(--space-5);
  min-width: 0;
}
.timing-section {
  min-width: 0;
}
.timing-section + .timing-section,
.timing-preview-section {
  border-top: 1px solid var(--border);
  padding-top: var(--space-5);
}
.timing-section-heading {
  display: grid;
  gap: var(--space-2);
  margin-bottom: var(--space-4);
}
.schedule-timing h3 {
  font-size: var(--text-section);
  font-weight: var(--font-semibold);
  margin: 0;
}
.timing-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-4);
  align-items: start;
}
.timing-hint {
  color: var(--text-muted);
  font-size: var(--text-sm);
  line-height: var(--leading-relaxed);
  margin: 0;
  overflow-wrap: anywhere;
}
.timing-boundary-note {
  margin-top: var(--space-3);
}
.weekday-options {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.weekday-options label {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2);
  min-height: var(--control-height-form);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  cursor: pointer;
  font-size: var(--text-sm);
}
.weekday-options label:has([data-state='checked']) {
  background: var(--surface-selected);
  border-color: var(--accent);
}
.timing-preview-section {
  display: grid;
  gap: var(--space-4);
}
.timing-preview-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--space-3);
}
.timing-preview {
  overflow-wrap: anywhere;
}
.timing-preview ol {
  list-style: none;
  padding: 0;
  margin: 0;
  border-top: 1px solid var(--border);
}
.timing-preview li {
  display: grid;
  gap: var(--space-1);
  padding-block: var(--space-3);
  border-bottom: 1px solid var(--border);
}
.timing-preview-local {
  font-size: var(--text-md);
  font-variant-numeric: tabular-nums;
}
.timing-preview > p {
  margin: var(--space-3) 0 0;
}
@container (max-width: 440px) {
  .timing-grid {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 700px) {
  .timing-grid {
    grid-template-columns: 1fr;
  }
}
</style>
