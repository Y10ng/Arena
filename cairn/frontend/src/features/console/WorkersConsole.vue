<template>
  <article class="console-page">
    <div class="section-header section-header--actions">
      <div class="header-actions">
        <Button variant="secondary" size="sm" :loading="isRefreshing" @click="refreshAll">
          <template #icon><RefreshCw :size="14" /></template>
          刷新
        </Button>
        <Button
          variant="success"
          size="sm"
          :loading="activeSaving"
          :disabled="!activeDirty || activeReadOnly"
          @click="saveActiveMode"
        >
          <template #icon><Save :size="14" /></template>
          {{ activeSaving ? '保存中...' : '保存' }}
        </Button>
      </div>
    </div>

    <section class="console-card form-partition">
      <div class="partition-header">
        <h2>状态 / Heartbeat</h2>
      </div>
      <Alert v-if="isLoadingSummary" class="state-banner" tone="info">
        <Spinner size="sm" decorative />
        正在加载 dispatcher 状态...
      </Alert>
      <Alert v-else-if="isSummaryError" class="state-banner" tone="danger">
        加载 dispatcher 状态失败：{{ formatError(summaryError) }}
      </Alert>
      <div v-else class="dispatcher-grid">
        <div class="kv-row">
          <span>状态</span>
          <strong>{{ dispatcher.effective_status || dispatcher.last_status || '未连接' }}</strong>
        </div>
        <div class="kv-row">
          <span>dispatcher_id</span>
          <code>{{ dispatcher.last_dispatcher_id || '—' }}</code>
        </div>
        <div class="kv-row">
          <span>heartbeat age</span>
          <code>{{
            dispatcher.heartbeat_age_seconds == null ? '—' : `${dispatcher.heartbeat_age_seconds}s`
          }}</code>
        </div>
        <div class="kv-row">
          <span>heartbeat stale</span>
          <code>{{ dispatcher.heartbeat_stale ? 'yes' : 'no' }}</code>
        </div>
        <div class="kv-row">
          <span>config_sha256</span>
          <code>{{ dispatcher.config_sha256 || dispatcher.config_hash || '—' }}</code>
        </div>
        <div class="kv-row">
          <span>dispatch_yaml_path</span>
          <code>{{ dispatcherYamlPath || '未上报启动配置路径' }}</code>
        </div>
        <div class="kv-row">
          <span>更新时间</span>
          <code>{{ dispatcher.updated_at || '—' }}</code>
        </div>
        <div class="kv-row" v-if="dispatcher.last_error_message">
          <span>错误</span>
          <strong class="text-danger">{{ dispatcher.last_error_message }}</strong>
        </div>
      </div>
    </section>

    <section class="console-card form-partition">
      <div class="partition-header">
        <h2>dispatch.yaml</h2>
      </div>

      <Alert v-if="!yamlWritable" class="state-banner" tone="warning">
        当前 dispatcher 未上报启动 YAML 路径。以下内容来自最近一次上报快照，只读展示。
      </Alert>

      <Tabs
        :model-value="editorMode"
        :options="editorModeOptions"
        activation-mode="manual"
        variant="pill"
        @update:model-value="requestEditorModeChange"
      >
        <template #panel="{ option }">
          <template v-if="option.value === 'form'">
            <Alert v-if="isConfigError" class="state-banner" tone="danger">
              读取结构化配置失败：{{ formatError(configError) }}
            </Alert>
            <Alert v-if="isLoadingConfig && !formConfig" class="state-banner" tone="info">
              <Spinner size="sm" decorative />
              正在读取结构化配置...
            </Alert>
            <div v-else-if="formConfig" class="dispatcher-form" :class="{ readonly: formReadOnly }">
              <fieldset :disabled="formReadOnly">
                <section class="config-section">
                  <h3>Runtime</h3>
                  <div class="form-grid">
                    <FormField
                      v-for="field in runtimeNumberFields"
                      :key="field"
                      v-slot="{ controlAttrs }"
                      class="form-group"
                      :label="field"
                    >
                      <Input
                        v-model="formConfig.runtime[field]"
                        v-bind="controlAttrs"
                        class="mono"
                        type="number"
                        min="1"
                      />
                    </FormField>
                    <FormField v-slot="{ controlAttrs }" class="form-group" label="prompt_group">
                      <Input
                        v-model="formConfig.runtime.prompt_group"
                        v-bind="controlAttrs"
                        class="mono"
                      />
                    </FormField>
                  </div>
                </section>

                <section class="config-section">
                  <h3>Tasks</h3>
                  <div class="task-form-grid">
                    <div v-for="task in taskNames" :key="task" class="mini-panel">
                      <strong>{{ task }}</strong>
                      <FormField
                        v-for="field in taskFields(task)"
                        :key="`${task}:${field}`"
                        v-slot="{ controlAttrs }"
                        class="form-group compact"
                        :label="field"
                      >
                        <Input
                          v-model="formConfig.tasks[task][field]"
                          v-bind="controlAttrs"
                          class="mono"
                          type="number"
                          min="1"
                        />
                      </FormField>
                    </div>
                  </div>
                </section>

                <section class="config-section">
                  <h3>Container</h3>
                  <div class="form-grid">
                    <FormField v-slot="{ controlAttrs }" class="form-group" label="image">
                      <Input
                        v-model="formConfig.container.image"
                        v-bind="controlAttrs"
                        class="mono"
                      />
                    </FormField>
                    <FormField v-slot="{ controlAttrs }" class="form-group" label="network_mode">
                      <Input
                        v-model="formConfig.container.network_mode"
                        v-bind="controlAttrs"
                        class="mono"
                      />
                    </FormField>
                    <FormField
                      v-slot="{ controlAttrs }"
                      class="form-group"
                      label="completed_action"
                    >
                      <Select
                        v-model="formConfig.container.completed_action"
                        v-bind="controlAttrs"
                        class="mono"
                        :options="completedActionOptions"
                      />
                    </FormField>
                    <FormField v-slot="{ controlAttrs }" class="form-group" label="allowed_images">
                      <Input
                        :model-value="joinList(formConfig.container.allowed_images)"
                        v-bind="controlAttrs"
                        class="mono"
                        @update:model-value="
                          formConfig.container.allowed_images = splitList(String($event ?? ''))
                        "
                      />
                    </FormField>
                    <FormField
                      v-slot="{ controlAttrs }"
                      class="form-group"
                      label="cap_add baseline"
                    >
                      <Input
                        :model-value="joinList(formConfig.container.cap_add)"
                        v-bind="controlAttrs"
                        class="mono"
                        @update:model-value="
                          formConfig.container.cap_add = splitList(String($event ?? ''))
                        "
                      />
                    </FormField>
                    <FormField
                      v-slot="{ controlAttrs }"
                      class="form-group"
                      label="legacy execution_profile"
                    >
                      <Input
                        v-model="formConfig.container.execution_profile"
                        v-bind="controlAttrs"
                        class="mono"
                      />
                    </FormField>
                  </div>
                </section>

                <section class="config-section">
                  <div class="section-title-row">
                    <h3>Images</h3>
                    <Button variant="secondary" size="sm" @click="addImage">新增镜像</Button>
                  </div>
                  <div class="repeated-list">
                    <div
                      v-for="(image, index) in formConfig.images"
                      :key="`image:${index}`"
                      class="repeat-row two"
                    >
                      <Input
                        v-model="image.id"
                        class="mono"
                        placeholder="id"
                        aria-label="镜像 ID"
                      />
                      <Input
                        v-model="image.image"
                        class="mono"
                        placeholder="image"
                        aria-label="镜像地址"
                      />
                      <IconButton
                        variant="danger"
                        size="sm"
                        label="删除镜像"
                        @click="removeItem(formConfig.images, Number(index))"
                      >
                        <Trash2 :size="14" />
                      </IconButton>
                    </div>
                  </div>
                </section>

                <section class="config-section">
                  <div class="section-title-row">
                    <h3>Execution Profiles</h3>
                    <Button variant="secondary" size="sm" @click="addProfile">新增画像</Button>
                  </div>
                  <div class="repeated-list">
                    <div
                      v-for="(profile, index) in formConfig.execution_profiles"
                      :key="`profile:${index}`"
                      class="profile-row"
                    >
                      <Input
                        v-model="profile.id"
                        class="mono"
                        placeholder="id"
                        aria-label="画像 ID"
                      />
                      <Input
                        v-model="profile.image_ref"
                        class="mono"
                        placeholder="image_ref"
                        aria-label="画像镜像引用"
                      />
                      <Input
                        v-model="profile.network_policy"
                        class="mono"
                        placeholder="network_policy"
                        aria-label="画像网络策略"
                      />
                      <Input
                        :model-value="joinList(profile.cap_add)"
                        class="mono"
                        placeholder="cap_add"
                        aria-label="画像 capabilities"
                        @update:model-value="profile.cap_add = splitList(String($event ?? ''))"
                      />
                      <Select
                        v-model="profile.isolation"
                        class="mono"
                        aria-label="画像隔离模式"
                        :options="isolationOptions"
                      />
                      <Select
                        v-model="profile.completed_action"
                        class="mono"
                        aria-label="画像完成动作"
                        :options="completedActionOptions"
                      />
                      <IconButton
                        variant="danger"
                        size="sm"
                        label="删除画像"
                        @click="removeItem(formConfig.execution_profiles, Number(index))"
                      >
                        <Trash2 :size="14" />
                      </IconButton>
                    </div>
                  </div>
                </section>
              </fieldset>

              <footer class="editor-footer justify-end">
                <Button
                  variant="secondary"
                  :disabled="!formDirty || isSavingForm"
                  @click="resetForm"
                >
                  重置
                </Button>
                <Button
                  variant="success"
                  :loading="isSavingForm"
                  :disabled="!formDirty || formReadOnly"
                  @click="saveForm"
                >
                  <template #icon><Save :size="14" /></template>
                  {{ isSavingForm ? '保存中...' : '保存表单' }}
                </Button>
              </footer>
            </div>
          </template>

          <template v-else>
            <Alert v-if="isYamlError" class="state-banner" tone="danger">
              读取 dispatch.yaml 失败：{{ formatError(yamlError) }}
            </Alert>
            <MonacoEditor
              v-model="editorYamlContent"
              language="yaml"
              :read-only="yamlReadOnly"
              :min-height="540"
              aria-label="dispatch.yaml 原始内容"
            />
            <footer class="editor-footer justify-end">
              <Button variant="secondary" :disabled="!yamlDirty || isSavingYaml" @click="resetYaml">
                重置
              </Button>
              <Button
                variant="success"
                :loading="isSavingYaml"
                :disabled="!yamlDirty || yamlReadOnly"
                @click="saveYaml"
              >
                <template #icon><Save :size="14" /></template>
                {{ isSavingYaml ? '保存中...' : '保存 YAML' }}
              </Button>
            </footer>
          </template>
        </template>
      </Tabs>
    </section>
  </article>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { RefreshCw, Save, Trash2 } from '@lucide/vue'
import MonacoEditor from '@/components/MonacoEditor.vue'
import { Alert, Button, FormField, IconButton, Input, Select, Spinner, Tabs } from '@/components/ui'
import type { TabsOption } from '@/components/ui'
import {
  dumpYaml,
  formatError,
  joinList,
  runtimeNumberFields,
  splitList,
  taskFields,
  taskNames,
  useDispatcherFormConfig,
} from '@/composables/useDispatcherFormConfig'

type EditorMode = 'form' | 'yaml'

const route = useRoute()
const editorMode = ref<EditorMode>(route.query.mode === 'yaml' ? 'yaml' : 'form')
const editorModeOptions: readonly TabsOption[] = [
  { value: 'form', label: '表单编辑' },
  { value: 'yaml', label: '原始 YAML' },
]
const completedActionOptions = [
  { value: 'stop', label: 'stop' },
  { value: 'remove', label: 'remove' },
] as const
const isolationOptions = [
  { value: 'shared_container', label: 'shared_container' },
  { value: 'shared_local', label: 'shared_local' },
] as const

const {
  dispatcher,
  dispatcherYamlPath,
  yamlWritable,
  isLoadingSummary,
  isSummaryError,
  summaryError,
  isLoadingConfig,
  isConfigError,
  configError,
  isYamlError,
  yamlError,
  isRefreshing,
  isSavingYaml,
  isSavingForm,
  formConfig,
  formDirty,
  formReadOnly,
  yamlDirty,
  yamlReadOnly,
  editorYamlContent,
  refreshAll,
  saveForm,
  saveYaml,
  resetForm,
  resetYaml,
  removeItem,
} = useDispatcherFormConfig()

const activeDirty = computed(() =>
  editorMode.value === 'form' ? formDirty.value : yamlDirty.value,
)
const activeSaving = computed(() =>
  editorMode.value === 'form' ? isSavingForm.value : isSavingYaml.value,
)
const activeReadOnly = computed(() =>
  editorMode.value === 'form' ? formReadOnly.value : yamlReadOnly.value,
)

const setEditorMode = (mode: EditorMode) => {
  if (mode === editorMode.value) return
  if (
    editorMode.value === 'yaml' &&
    yamlDirty.value &&
    !window.confirm('原始 YAML 有未保存修改，切换后将放弃这些修改。继续？')
  )
    return
  if (mode === 'yaml' && formConfig.value && formDirty.value) {
    editorYamlContent.value = dumpYaml(formConfig.value)
  }
  editorMode.value = mode
}

const requestEditorModeChange = (mode: string) => {
  if (mode === 'form' || mode === 'yaml') setEditorMode(mode)
}

const saveActiveMode = () => {
  if (editorMode.value === 'form') return void saveForm()
  return void saveYaml()
}

const addImage = () => {
  formConfig.value?.images.push({ id: nextId('image', formConfig.value.images), image: '' })
}

const addProfile = () => {
  formConfig.value?.execution_profiles.push({
    id: nextId('profile', formConfig.value.execution_profiles),
    image_ref: 'default',
    network_policy: 'none',
    cap_add: [],
    isolation: 'shared_container',
    completed_action: 'stop',
  })
}

function nextId(prefix: string, items: Array<Record<string, unknown>>) {
  const used = new Set(items.map((item) => String(item.id || item.name || '')))
  let index = items.length + 1
  let id = `${prefix}-${index}`
  while (used.has(id)) {
    index += 1
    id = `${prefix}-${index}`
  }
  return id
}
</script>

<style scoped>
.dispatcher-grid {
  display: grid;
  gap: var(--space-2);
}

.kv-row {
  display: grid;
  grid-template-columns: minmax(140px, 220px) minmax(0, 1fr);
  gap: var(--space-3);
  align-items: start;
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--border-subtle);
}

.kv-row span {
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 700;
}

.kv-row code {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.dispatcher-form fieldset {
  display: grid;
  gap: var(--space-5);
  min-width: 0;
  padding: 0;
  border: 0;
}

.dispatcher-form.readonly {
  opacity: 0.78;
}

.config-section {
  display: grid;
  gap: var(--space-3);
  min-width: 0;
  padding-top: var(--space-4);
  border-top: 1px solid var(--border-subtle);
}

.config-section:first-child {
  padding-top: 0;
  border-top: 0;
}

.config-section h3 {
  margin: 0;
  color: var(--text);
  font-size: 13px;
}

.section-title-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-3);
}

.form-grid,
.task-form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--space-3);
}

.mini-panel {
  display: grid;
  gap: var(--space-2);
  align-content: start;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-inset);
}

.form-group.compact {
  gap: 4px;
}

.align-end {
  align-self: end;
  min-height: 36px;
}

.repeated-list {
  display: grid;
  gap: var(--space-2);
}

.repeat-row,
.profile-row {
  display: grid;
  gap: var(--space-2);
  align-items: start;
  min-width: 0;
}

.repeat-row.two {
  grid-template-columns: minmax(120px, 180px) minmax(0, 1fr) 32px;
}

.profile-row {
  grid-template-columns:
    minmax(110px, 150px) minmax(110px, 150px) minmax(120px, 160px) minmax(0, 1fr)
    minmax(150px, 180px) minmax(110px, 140px) 32px;
}

@media (max-width: 960px) {
  .kv-row,
  .repeat-row.two,
  .profile-row {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
