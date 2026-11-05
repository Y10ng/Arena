<template>
  <section class="runtime-settings-panel">
    <ErrorState
      v-if="query.error.value"
      title="运行设置读取失败"
      :description="errorText(query.error.value)"
      @retry="query.refetch()"
    />
    <Skeleton v-if="!draft && query.isPending.value" label="正在读取运行设置" />
    <form v-if="draft" class="managed-form" @submit.prevent="save">
      <fieldset :disabled="saving">
        <section>
          <h3>并发容量</h3>
          <div class="runtime-fields">
            <label v-for="field in capacityFields" :key="field.key" class="form-group">
              <span class="form-label"
                >{{ field.label
                }}<small class="runtime-unit">{{
                  field.key === 'max_running_projects' ? '个项目' : '个 Worker'
                }}</small></span
              >
              <Input
                :model-value="draft.runtime[field.key]"
                type="number"
                :min="1"
                step="1"
                :aria-label="field.label"
                @update:model-value="draft.runtime[field.key] = numberValue($event)"
              />
            </label>
          </div>
          <p v-if="query.data.value?.load_status.hard_cap" class="section-desc">
            当前 Dispatcher 总槽位上限
            {{ query.data.value.load_status.hard_cap }}，普通任务之外的剩余槽位供独立复测使用。
          </p>
        </section>
        <section>
          <h3>执行时限</h3>
          <div class="runtime-fields">
            <label v-for="field in taskFields" :key="field.key" class="form-group">
              <span class="form-label">{{ field.label }}（秒）</span>
              <Input
                :model-value="draft.tasks[field.key].timeout"
                type="number"
                :min="1"
                step="1"
                :aria-label="`${field.label}执行时限（秒）`"
                @update:model-value="draft.tasks[field.key].timeout = numberValue($event)"
              />
            </label>
          </div>
        </section>
        <details class="runtime-advanced">
          <summary>高级运行设置</summary>
          <div class="runtime-fields">
            <label v-for="field in concludeFields" :key="field.key" class="form-group">
              <span class="form-label">{{ field.label }}收尾时限（秒）</span>
              <Input
                :model-value="draft.tasks[field.key].conclude_timeout"
                type="number"
                :min="1"
                step="1"
                :aria-label="`${field.label}收尾时限（秒）`"
                @update:model-value="draft.tasks[field.key].conclude_timeout = numberValue($event)"
              />
            </label>
            <label class="form-group"
              ><span class="form-label">报告执行时限（秒）</span
              ><Input
                v-model="draft.tasks.report.timeout"
                type="number"
                :min="1"
                step="1"
                aria-label="报告执行时限（秒）"
            /></label>
            <label class="form-group"
              ><span class="form-label">调度间隔（秒）</span
              ><Input
                v-model="draft.runtime.interval"
                type="number"
                :min="1"
                step="1"
                aria-label="调度间隔（秒）"
            /></label>
            <label class="form-group"
              ><span class="form-label">健康检查时限（秒）</span
              ><Input
                v-model="draft.runtime.healthcheck_timeout"
                type="number"
                :min="1"
                step="1"
                aria-label="健康检查时限（秒）"
            /></label>
          </div>
          <p class="section-desc">收尾时限受当次剩余执行预算约束，不是额外保证的运行时间。</p>
        </details>
      </fieldset>
      <Alert v-if="validationError" tone="warning">{{ validationError }}</Alert>
      <Alert v-if="saveError" tone="danger">{{ saveError }}</Alert>
      <section v-if="needsReview" class="runtime-review">
        <p>草稿已保留。重新读取后核对当前值，不会自动覆盖或重试保存。</p>
        <Button variant="secondary" :loading="reviewing" :disabled="saving" @click="reviewCurrent"
          >读取当前配置</Button
        >
        <template v-if="reviewed">
          <table class="worker-table">
            <thead>
              <tr>
                <th>修改项</th>
                <th>当前保存值</th>
                <th>本次草稿</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="change in reviewedChanges" :key="change.label">
                <th>{{ change.label }}</th>
                <td>{{ change.current }}</td>
                <td>{{ change.draft }}</td>
              </tr>
            </tbody>
          </table>
          <Button variant="secondary" @click="acceptRevision">已核对，继续编辑</Button>
        </template>
      </section>
      <footer class="runtime-save-actions">
        <p class="section-desc">保存只影响后续新执行；不会重置已冻结的时限，也不会停止在途任务。</p>
        <Button
          type="submit"
          :loading="saving"
          :disabled="!dirty || Boolean(validationError) || needsReview"
          >保存运行设置</Button
        >
      </footer>
      <div class="runtime-load-status" role="status">
        <span v-if="saved">配置已保存。</span>
        <span>{{ loadLabel }}</span>
        <span
          v-if="
            !query.error.value &&
            query.data.value?.load_status.state !== 'loaded' &&
            query.data.value?.load_status.message
          "
          >{{ query.data.value.load_status.message }}</span
        >
        <Button variant="ghost" size="sm" :loading="query.isFetching.value" @click="query.refetch()"
          >刷新加载状态</Button
        >
      </div>
    </form>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { ApiError } from '@/api/client'
import {
  patchRuntimeSettings,
  readRuntimeSettings,
  type RuntimeSettingsDocument,
  type RuntimeSettingsPatch,
  type RuntimeSettingsValues,
} from '@/api/runtimeSettings'
import { ErrorState } from '@/components/patterns'
import { Alert, Button, Input, Skeleton } from '@/components/ui'

const query = useQuery({
  queryKey: ['dispatcher-runtime-settings'],
  queryFn: readRuntimeSettings,
  refetchInterval: 10000,
})
const draft = ref<RuntimeSettingsValues | null>(null)
const baseline = ref<RuntimeSettingsValues | null>(null)
const revision = ref('')
const saving = ref(false)
const saved = ref(false)
const saveError = ref('')
const needsReview = ref(false)
const reviewing = ref(false)
const reviewed = ref<RuntimeSettingsDocument | null>(null)
const capacityFields = [
  { key: 'max_workers', label: '总 Worker 并发' },
  { key: 'max_running_projects', label: '同时运行项目数' },
  { key: 'max_project_workers', label: '单项目 Worker 并发' },
] as const
const taskFields = [
  { key: 'bootstrap', label: '初始化' },
  { key: 'reason', label: '决策' },
  { key: 'explore', label: '探索' },
] as const
const concludeFields = [
  { key: 'bootstrap', label: '初始化' },
  { key: 'explore', label: '探索' },
] as const
function values(document: RuntimeSettingsDocument): RuntimeSettingsValues {
  return {
    runtime: { ...document.runtime },
    tasks: {
      bootstrap: { ...document.tasks.bootstrap },
      reason: { ...document.tasks.reason },
      explore: { ...document.tasks.explore },
      report: { ...document.tasks.report },
    },
  }
}
function resetTo(document: RuntimeSettingsDocument) {
  baseline.value = values(document)
  draft.value = values(document)
  revision.value = document.revision
}
watch(
  query.data,
  (document) => {
    if (document && !draft.value) resetTo(document)
  },
  { immediate: true },
)
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(baseline.value))
const validationError = computed(() => {
  if (!draft.value) return ''
  const { runtime, tasks } = draft.value
  const numbers = [...Object.values(runtime), ...Object.values(tasks).flatMap(Object.values)]
  if (numbers.some((value) => !Number.isInteger(value) || value <= 0))
    return '运行参数必须是正整数。'
  if (runtime.max_project_workers > runtime.max_workers)
    return '单项目 Worker 并发不能超过总 Worker 并发。'
  const hardCap = query.data.value?.load_status.hard_cap
  if (hardCap && runtime.max_workers > hardCap)
    return `总 Worker 并发不能超过当前 Dispatcher 槽位上限 ${hardCap}。`
  return ''
})
const loadLabel = computed(() => {
  if (query.error.value) return 'Dispatcher 加载状态未确认。'
  return {
    loaded: 'Dispatcher 已加载。',
    pending: '等待 Dispatcher 加载。',
    failed: 'Dispatcher 加载失败。',
    unknown: 'Dispatcher 加载状态未知。',
  }[query.data.value?.load_status.state || 'unknown']
})
function numberValue(value: string | number | null) {
  return value === null ? Number.NaN : Number(value)
}
function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}
function changedFields(): RuntimeSettingsPatch {
  const patch: RuntimeSettingsPatch = { revision: revision.value }
  if (!draft.value || !baseline.value) return patch
  for (const key of Object.keys(
    draft.value.runtime,
  ) as (keyof RuntimeSettingsValues['runtime'])[]) {
    if (draft.value.runtime[key] !== baseline.value.runtime[key])
      (patch.runtime ??= {})[key] = draft.value.runtime[key]
  }
  for (const role of ['bootstrap', 'reason', 'explore', 'report'] as const) {
    if (draft.value.tasks[role].timeout !== baseline.value.tasks[role].timeout)
      ((patch.tasks ??= {})[role] ??= {}).timeout = draft.value.tasks[role].timeout
  }
  for (const role of ['bootstrap', 'explore'] as const) {
    if (draft.value.tasks[role].conclude_timeout !== baseline.value.tasks[role].conclude_timeout)
      ((patch.tasks ??= {})[role] ??= {}).conclude_timeout =
        draft.value.tasks[role].conclude_timeout
  }
  return patch
}
const reviewedChanges = computed(() => {
  if (!reviewed.value) return []
  const patch = changedFields()
  const runtimeLabels: Record<string, string> = {
    interval: '调度间隔（秒）',
    healthcheck_timeout: '健康检查时限（秒）',
  }
  const roleLabels: Record<string, string> = {
    bootstrap: '初始化',
    reason: '决策',
    explore: '探索',
    report: '报告',
  }
  return [
    ...Object.entries(patch.runtime || {}).map(([key, value]) => ({
      label: capacityFields.find((field) => field.key === key)?.label || runtimeLabels[key] || key,
      current: reviewed.value!.runtime[key as keyof RuntimeSettingsValues['runtime']],
      draft: value,
    })),
    ...Object.entries(patch.tasks || {}).flatMap(([role, fields]) =>
      Object.entries(fields).map(([key, value]) => ({
        label: `${roleLabels[role] ?? role}${key === 'conclude_timeout' ? '收尾时限' : '执行时限'}（秒）`,
        current: (
          reviewed.value!.tasks[role as keyof RuntimeSettingsValues['tasks']] as Record<
            string,
            number
          >
        )[key],
        draft: value,
      })),
    ),
  ]
})
async function save() {
  if (saving.value || !dirty.value || validationError.value || needsReview.value) return
  saving.value = true
  saved.value = false
  saveError.value = ''
  try {
    const document = await patchRuntimeSettings(changedFields())
    resetTo(document)
    saved.value = true
    await query.refetch()
  } catch (error) {
    needsReview.value = !(error instanceof ApiError) || error.status === 409 || error.status >= 500
    reviewed.value = null
    saveError.value =
      !(error instanceof ApiError) || error.status >= 500
        ? '保存结果未确认，请读取当前配置核对后再操作。'
        : errorText(error)
  } finally {
    saving.value = false
  }
}
async function reviewCurrent() {
  reviewing.value = true
  reviewed.value = null
  try {
    reviewed.value = await readRuntimeSettings()
  } catch (error) {
    saveError.value = errorText(error)
  } finally {
    reviewing.value = false
  }
}
function acceptRevision() {
  if (!reviewed.value) return
  revision.value = reviewed.value.revision
  needsReview.value = false
  saveError.value = ''
  reviewed.value = null
}
defineExpose({ dirty, saving })
</script>
