<template>
  <Dialog
    :open="open"
    :title="schedule ? '编辑未来配置' : '新建项目'"
    :description="schedule ? '仅影响尚未生成的项目，已生成项目及其资料不变。' : ''"
    size="editor"
    :dismissible="!isSubmitting && !resourceBusy && !isCancelling"
    :close-on-outside="false"
    :unmount-on-hide="false"
    @update:open="requestClose"
    @open-auto-focus="focusTitle"
    @close-auto-focus="restoreFocus"
  >
    <Alert v-if="creationUnknown" tone="warning" title="保存结果未确认">
      请求可能已被服务端接受，不要另建请求或取消可能已消费的资料。可正常导航；正文及原始请求仅在本次页面会话内保留，刷新后只能查询请求结果。
      <p v-if="formError">{{ formError }}</p>
      <p v-if="recoveryMessage">{{ recoveryMessage }}</p>
      <router-link v-if="workerConfigurationError" to="/console/workers" target="_blank" rel="noopener">前往 Worker 配置</router-link>
      <template #actions>
        <Button
          v-if="pendingSubmission?.operation !== 'update'"
          variant="secondary"
          size="sm"
          :loading="isSubmitting"
          @click="queryCreationResult"
          >查询请求结果</Button
        >
        <Button
          v-if="pendingSubmission"
          variant="secondary"
          size="sm"
          :loading="isSubmitting"
          @click="restoreSubmission"
          >{{
            pendingSubmission.operation === 'update' ? '按原版本恢复保存' : '用原请求恢复提交'
          }}</Button
        >
        <Button variant="secondary" size="sm" @click="emit('update:open', false)"
          >查看列表，保留内容</Button
        >
      </template>
    </Alert>
    <Alert v-if="creationRejected" tone="danger" title="请求被拒绝">
      {{ formError }}。可按原请求重试，或明确修改内容并使用新请求。
      <router-link v-if="workerConfigurationError" to="/console/workers" target="_blank" rel="noopener">前往 Worker 配置</router-link>
      <template #actions>
        <Button size="sm" variant="secondary" :loading="isSubmitting" @click="restoreSubmission">按原请求重试</Button>
        <Button size="sm" variant="secondary" @click="editRejectedSubmission">修改内容并使用新请求</Button>
      </template>
    </Alert>
    <Alert v-if="creationDeleted" tone="warning" title="原请求已保存，但项目已删除">
      不能用此请求重新创建；关联引用已清除，不会恢复提交或跳转到已删除项目。
    </Alert>
    <Alert v-if="createdScheduleId" tone="success" title="计划已保存">
      {{
        scheduleDraft.mode === 'once'
          ? '项目待启动，尚未进入执行调度。'
          : '已保存未来运行计划，尚未创建首轮项目。'
      }}{{ navigationError }}
      <template #actions
        ><Button size="sm" @click="enterCreatedProject">进入计划详情</Button></template
      >
    </Alert>
    <Alert v-if="createdProjectId" tone="success" title="项目已创建，等待调度">
      项目与资料已提交，不需要再次创建。{{ navigationError }}
      <template #actions>
        <Button size="sm" @click="enterCreatedProject">进入已创建项目</Button>
      </template>
    </Alert>
    <Alert v-if="cacheWarning" tone="warning" title="请求引用缓存提示">{{ cacheWarning }}</Alert>
    <fieldset
      ref="formElement"
      class="new-project-form"
      :disabled="formDisabled"
      @input="userEdited = true"
      @change="userEdited = true"
    >
      <div class="creation-content">
        <div id="np-section-basic" class="creation-main">
        <div class="project-identity">
          <FormField label="项目名称" for="np-title" required :error="validationErrors.title">
            <Input
              id="np-title"
              ref="titleInput"
              v-model="form.title"
              placeholder="例如: arena-web-recon"
            />
          </FormField>

          <FormField label="探索场景" for="np-scenario" required>
            <Select
              id="np-scenario"
              :model-value="form.scenario"
              :options="scenarios.map((sc) => ({ value: sc.key, label: sc.label }))"
              :disabled="scenariosLoading || scenariosError || !scenarios.length"
              :placeholder="scenariosLoading ? '正在加载场景…' : '选择场景'"
              @update:model-value="selectScenarioByKey"
            />
          </FormField>
        </div>

        <ErrorState
          v-if="scenariosError"
          title="场景目录加载失败"
          description="场景目录暂不可用，无法创建项目。"
          @retry="refetchScenarios()"
        />
        <EmptyState
          v-else-if="!scenariosLoading && !scenarios.length"
          title="暂无可用场景"
          description="场景目录为空，暂时无法创建项目。"
        />
        <div class="project-brief">
          <FormField
            id="np-origin"
            class="validation-anchor"
            label="Origin（已知信息）"
            for="np-origin-input"
            required
            :error="validationErrors.origin"
          >
            <Textarea
              id="np-origin-input"
              v-model="form.origin"
              placeholder="描述已知信息、研究对象、授权范围与排除项；例如：核验订单审批资料，不访问生产环境。"
              :rows="4"
              @input="scenarioTemplateMeta.origin.userTouched = true"
            />
          </FormField>

          <FormField
            id="np-goal"
            class="validation-anchor"
            label="Goal（目标）"
            for="np-goal-input"
            required
            :error="validationErrors.goal"
          >
            <Textarea
              id="np-goal-input"
              v-model="form.goal"
              placeholder="例如：重点检查普通用户之间的数据隔离及审批流程。"
              :rows="4"
              @input="scenarioTemplateMeta.goal.userTouched = true"
            />
          </FormField>

          <section id="np-materials" class="project-materials" aria-label="项目资料">
            <div class="materials-summary">
              <strong>项目资料</strong>
              <span aria-live="polite">{{ resourceSummary }}{{ schedule ? ` · 保留原件 ${retainedResourceIds.length}` : '' }}</span>
              <label class="safety-toggle">
                <input :checked="safetySelected || safetyPending" type="checkbox" role="switch" aria-label="安全限制"
                  @change="setSafetySelection(($event.target as HTMLInputElement).checked)" />
                安全限制
              </label>
            </div>
            <p v-if="safetyError" class="material-error" role="alert">{{ safetyError }}</p>
            <p v-else-if="safetyPending" class="section-desc">正在加入执行安全约束…</p>
            <div v-if="legacySelectionState !== 'explicit'" class="legacy-materials">
              <p v-if="legacySelectionState === 'preserve'" class="section-desc">
                此计划沿用历史场景默认资料，具体范围未确认；不更改选择时继续保留。
              </p>
              <p v-else class="material-error" role="alert">
                服务未提供计划的资料选择状态，无法确认是否存在历史默认资料。
              </p>
              <label><input v-model="clearLegacyMaterials" type="checkbox" /> 不再使用历史默认资料</label>
              <p v-if="selectionEdited" class="section-desc">修改文件选择后，未来运行将使用新的明确选择。</p>
            </div>
            <section v-if="schedule" class="selected-libraries" aria-label="未来配置保留原件">
              <h3 class="materials-heading">已有原件</h3>
              <div v-for="resource in schedule.resources" :key="resource.id" class="selected-library">
                <label><Checkbox :model-value="retainedResourceIds.includes(resource.id)"
                  @update:model-value="retainResource(resource.id, $event)" />
                  {{ resource.path }} · {{ bytes(resource.size_bytes) }} ·
                  {{ retainedResourceIds.includes(resource.id) ? '保留服务器原件' : '未来移除' }}
                </label>
              </div>
              <p class="section-desc">取消保留仅影响未来配置，不改变历史项目。</p>
            </section>
            <ProjectResourceBatchUploader
              ref="resourceUploader"
              v-model="resourceBatch"
              :disabled="formDisabled"
              @busy="resourceBusy = $event"
              @pending="resourcePending = $event"
              @summary="resourceSummary = $event"
            >
              <template #actions>
                <Button id="np-library-picker-trigger" size="sm" variant="secondary"
                  :disabled="formDisabled" @click="libraryPickerOpen = true">从资料库选择</Button>
              </template>
            </ProjectResourceBatchUploader>
            <section v-if="selections.length || invalidBindings.length" aria-label="已选资料库文件">
              <div v-for="binding in invalidBindings" :key="binding.library_key" class="material-error">
                {{ binding.library_name }} 的原绑定范围未知，无法安全保存。
                <Button size="sm" variant="ghost" @click="removeInvalidBinding(binding.library_key)">移除该绑定</Button>
              </div>
              <div v-for="selection in selections" :key="selection.library_key" class="library-selection-status">
                <template v-if="!selectionManifest(selection)">
                  <span v-if="boundLoading[selection.library_key]">正在读取 {{ selection.library_key }} 的已绑定文件…</span>
                  <span v-else class="material-error">{{ boundErrors[selection.library_key] || `无法读取 ${selection.library_key} 的文件清单` }}</span>
                  <Button v-if="boundErrors[selection.library_key]" size="sm" variant="ghost" @click="loadBoundManifest(selection.library_key)">重试</Button>
                  <Button size="sm" variant="ghost" @click="removeLibrary(selection.library_key)">移除该绑定</Button>
                </template>
                <template v-else-if="selectionManifest(selection)?.files.length === 0">
                  <span>{{ existingLibraryBinding(selection.library_key)?.library_name || selection.library_key }} ({{ selection.library_key }}) · 历史空整库绑定</span>
                  <Button size="sm" variant="ghost" @click="removeLibrary(selection.library_key)">移除该绑定</Button>
                </template>
                <Button size="sm" variant="ghost" :disabled="currentLoading[selection.library_key] || !selectionManifest(selection)"
                  @click="updateLibraryVersion(selection.library_key)">更新为当前资料</Button>
                <span v-if="updateErrors[selection.library_key]" class="material-error" role="alert">{{ updateErrors[selection.library_key] }}</span>
              </div>
              <div v-if="selectedFiles.length" class="selected-file-tags">
                <div v-for="file in selectedFiles" :key="file.identity" class="selected-file-tag">
                  <details>
                    <summary>{{ file.label }}</summary>
                    <span>{{ file.libraryName }} ({{ file.libraryKey }}) · {{ file.path }}</span>
                  </details>
                  <button type="button" :aria-label="`移除 ${file.libraryKey} ${file.path}`"
                    @click="setPaths(file.libraryKey, [file.path], false)"><X :size="14" aria-hidden="true" /></button>
                </div>
              </div>
            </section>
          </section>
        </div>
        </div>

        <aside class="creation-settings" aria-label="启动与代理设置">
        <section id="np-timing-settings" class="creation-section" aria-label="启动安排">
          <div class="creation-section-heading"><h3>启动安排</h3></div>
          <fieldset class="schedule-mode-options" role="radiogroup" aria-label="启动方式">
            <label v-for="option in modeOptions" :key="option.value">
              <input
                type="radio"
                :name="scheduleModeGroup"
                :value="option.value"
                :checked="scheduleDraft.mode === option.value"
                :disabled="formDisabled || Boolean(schedule && scheduleDraft.mode === 'once')"
                :aria-label="option.label"
                @change="setScheduleMode(option.value)"
              />
              <span>{{ modeLabels[option.value as ScheduleDraft['mode']] }}</span>
            </label>
          </fieldset>
          <div v-show="scheduleDraft.mode !== 'immediate' || Boolean(schedule?.project)">
            <ScheduleTiming
              ref="scheduleTiming"
              v-model="scheduleDraft"
              :show-mode="false"
              :locked-mode="Boolean(schedule)"
              :editing="Boolean(schedule)"
              :boundary-hint="boundaryHint"
              :project="previewProject"
            />
            <div v-if="schedule?.project" class="advanced-subsection">
              <div class="section-title-row compact-title-row">
                <div>
                  <h3>既有期限</h3>
                  <p class="section-desc">
                    未修改时保留原期限；清空输入才明确取消，仅影响未来项目。日期按浏览器本地时区显示。
                  </p>
                </div>
              </div>
              <div class="deadline-settings-grid">
                <FormField
                  label="执行停止时间"
                  for="np-explore-deadline"
                  :error="validationErrors.exploreDeadline"
                  hint="可选，留空为不限。到时停止新研究，并有序停止在途执行；不代表真实授权期限。"
                >
                  <Input
                    id="np-explore-deadline"
                    v-model="form.explore_deadline"
                    type="datetime-local"
                  />
                </FormField>
                <FormField
                  label="授权到期时间"
                  for="np-authorization-expires-at"
                  :error="validationErrors.authorizationExpiresAt"
                  hint="可选，留空表示不设到期时间；如实际授权有期限，请填写。与执行停止时间独立。"
                >
                  <Input
                    id="np-authorization-expires-at"
                    v-model="form.authorization_expires_at"
                    type="datetime-local"
                  />
                </FormField>
              </div>
            </div>
          </div>
        </section>
        <section id="np-proxy-settings" class="creation-section" aria-label="代理设置">
          <div class="creation-section-heading"><h3>代理设置</h3></div>
          <TestEgressFields
            v-model="egress"
            :auth-configured="schedule?.project?.test_egress?.auth_configured"
            :disabled="formDisabled"
            :error="egressError"
            @update:model-value="userEdited = true"
          />
        </section>
        </aside>
      </div>
    </fieldset>

    <template #footer>
      <div class="creation-footer">
        <p v-if="creationUnknown" class="section-desc">
          保存结果未确认，仅能查询或按原请求恢复；不要换键重复创建。
        </p>
        <p
          v-else-if="submitBlockReason || scheduleDraft.mode !== 'immediate'"
          class="section-desc creation-footer-notice"
        >
          {{
            submitBlockReason ||
            (schedule
              ? '保存仅影响未来项目，不会重写已生成项目。'
              : '保存计划后在计划时刻处理，不会立即启动研究。')
          }}
        </p>
        <Alert v-if="formError && !creationUnknown && !creationRejected" tone="danger" title="请检查当前操作">
          {{ formError }}
          <router-link
            v-if="workerConfigurationError"
            to="/console/workers"
            target="_blank"
            rel="noopener"
            >前往 Worker 配置</router-link
          >
        </Alert>
        <div class="creation-footer-actions">
          <Button
            variant="secondary"
            :disabled="isSubmitting || resourceBusy || isCancelling"
            @click="requestClose(false)"
            >取消</Button
          >
          <Button :loading="isSubmitting" :disabled="!canSubmitProject" @click="submitProject">{{
            submitLabel
          }}</Button>
        </div>
      </div>
    </template>

    <Dialog
      :open="libraryPickerOpen"
      title="从资料库选择"
      description="选择本次使用的资料文件，取消选择不会删除资料库原件。"
      @update:open="libraryPickerOpen = $event"
      @close-auto-focus="restoreLibraryPickerFocus"
    >
        <Input v-model="librarySearch" placeholder="搜索资料库或文件路径" aria-label="搜索资料库或文件路径" />
        <Skeleton v-if="libraryCatalogLoading && !pickerLibraries.length" />
        <ErrorState
          v-if="libraryCatalogError"
          title="资料库目录加载失败"
          description="可重试；资料库为可选项。"
          @retry="refetchLibraryCatalog()"
        />
        <div v-if="pickerLibraries.length" class="library-tree" aria-label="资料库文件选择">
          <section v-for="library in pickerLibraries" :key="library.key" class="library-tree-root"
            :style="{ marginLeft: library.builtin ? '0' : '20px' }">
            <div class="library-tree-row">
              <button type="button" class="tree-arrow" :disabled="!library.manifest"
                :aria-label="`${isExpanded(library.key,'') ? '折叠' : '展开'} ${library.name}`"
                :aria-expanded="isExpanded(library.key,'')"
                @click="toggleExpanded(library.key,'')">
                <ChevronRight :size="16" :class="{ expanded: isExpanded(library.key,'') || Boolean(librarySearch.trim()) }" />
              </button>
              <Checkbox v-if="library.manifest" :label="library.name"
                :aria-label="`选择资料库 ${library.name} (${library.key})`"
                :model-value="selectionState(library.key, library.manifest.files.map((file) => file.path)).checked"
                :indeterminate="selectionState(library.key, library.manifest.files.map((file) => file.path)).indeterminate"
                :disabled="!library.manifest.files.length"
                @update:model-value="setPaths(library.key, library.manifest!.files.map((file) => file.path), $event, library.manifest!)" />
              <span v-else>{{ library.name }}</span>
              <span v-if="library.manifest" class="tree-meta">{{ library.key }} · {{ library.manifest.files.length }} 文件{{ library.limited ? ' · 已绑定范围' : '' }}</span>
              <Button v-if="existingLibraryBinding(library.key) && !selectionFor(library.key)" size="sm" variant="ghost"
                :disabled="library.loading" @click="browseCurrentManifest(library.key)">查看当前资料</Button>
            </div>
            <p v-if="library.loading" class="tree-status" role="status">正在读取文件清单…</p>
            <div v-else-if="library.error" class="tree-status material-error" role="alert">
              {{ library.error }}
              <Button size="sm" variant="ghost" @click="retryLibrary(library.key)">重试</Button>
            </div>
            <p v-else-if="library.manifest && !library.manifest.files.length" class="tree-status">没有可选文件。</p>
            <div v-for="row in library.rows" :key="row.path" class="library-tree-row"
              :style="{ paddingLeft: `${(row.depth + 1) * 20}px` }">
              <button v-if="row.directory" type="button" class="tree-arrow"
                :aria-label="`${isExpanded(library.key,row.path) ? '折叠' : '展开'} ${row.path}`"
                :aria-expanded="isExpanded(library.key,row.path)"
                @click="toggleExpanded(library.key,row.path)">
                <ChevronRight :size="16" :class="{ expanded: isExpanded(library.key,row.path) || Boolean(librarySearch.trim()) }" />
              </button>
              <span v-else class="tree-arrow-placeholder"></span>
              <Checkbox :label="row.name" :aria-label="`选择${row.directory ? '目录' : '文件'} ${library.key} ${row.path}`"
                :model-value="selectionState(library.key, row.paths).checked"
                :indeterminate="selectionState(library.key, row.paths).indeterminate"
                @update:model-value="setPaths(library.key, row.paths, $event, library.manifest!)" />
            </div>
          </section>
        </div>
        <EmptyState v-else-if="!libraryCatalogLoading && !libraryCatalogError" title="暂无匹配资料库" description="可不选择资料库创建项目。" />
      <template #footer><Button @click="libraryPickerOpen = false">完成选择</Button></template>
    </Dialog>

    <ConfirmAction
      :open="discardOpen"
      title="放弃本次填写？"
      description="未提交内容会丢失，临时上传批次将取消。资料库原件不受影响。"
      confirm-label="放弃并关闭"
      cancel-label="继续填写"
      :loading="isCancelling"
      @update:open="discardOpen = $event"
      @confirm="discardProject"
    >
      <Alert v-if="cleanupError" tone="danger" title="临时批次取消失败"
        >{{ cleanupError }}。内容与批次已保留，可重试清理。</Alert
      >
    </ConfirmAction>
  </Dialog>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, useId, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { isNavigationFailure, onBeforeRouteLeave, useRouter } from 'vue-router'
import { api, ApiError, createProject, getLibraryCatalogManifest, listLibraryCatalog } from '@/api/client'
import { ChevronRight, X } from '@lucide/vue'
import type {
  AdvisoryLibrary,
  CreateProjectRequest,
  LibraryManifest,
  LibrarySelection,
  ProjectLibraryBinding,
  ProjectResourceBatch,
  ScenarioCatalogEntry,
} from '@/api/types'
import { useUIStore } from '@/stores/ui'
import { ConfirmAction, ErrorState } from '@/components/patterns'
import {
  Alert,
  Button,
  Dialog,
  Checkbox,
  EmptyState,
  FormField,
  Input,
  Select,
  Skeleton,
  Textarea,
} from '@/components/ui'
import ProjectResourceBatchUploader from './ProjectResourceBatchUploader.vue'
import { applyScenarioTemplates, type ScenarioTemplateState } from './newProjectScenarioTemplates'
import ScheduleTiming from './ScheduleTiming.vue'
import TestEgressFields from '@/components/patterns/TestEgressFields.vue'
import { testEgressDraft, testEgressError, testEgressInput } from './testEgressForm'
import {
  draftFromSchedule,
  newScheduleDraft,
  scheduleModeOptions,
  scheduleTimesFromDraft,
  ScheduleTimeError,
  type ScheduleDraft,
} from './scheduleTiming'
import { bytes } from '@/lib/format'
import {
  createCreationRequestId,
  createProjectSchedule,
  updateProjectSchedule,
  getCreationRequest,
  getScheduleLibraryManifest,
  rememberCreationRequest,
  forgetCreationRequest,
  type CreateScheduleRequest,
  type UpdateScheduleRequest,
  type ProjectSchedule,
} from '@/api/projectSchedules'

const props = defineProps<{
  open: boolean
  returnFocus: HTMLElement | null
  schedule?: ProjectSchedule
}>()
const emit = defineEmits<{
  'update:open': [open: boolean]
  discarded: []
  saved: [schedule: ProjectSchedule]
}>()
const router = useRouter()
const uiStore = useUIStore()

const userEdited = ref(false)
const isCancelling = ref(false)
const discardOpen = ref(false)
const cleanupError = ref('')
const formError = ref('')
const workerConfigurationError = ref(false)
const creationUnknown = ref(false)
const creationRejected = ref(false)
const creationDeleted = ref(false)
// 默认意图只消费一次；用户关闭后，迟到的清单不能重新加入安全文件。
const safetyPending = ref(!props.schedule)
const safetyError = ref('')
let safetyGeneration = 0
const createdProjectId = ref<string | null>(null)
const navigationError = ref('')
const libraryPickerOpen = ref(false)
const librarySearch = ref('')
const resourcePending = ref(false)
const isSubmitting = ref(false)
const titleInput = ref<{ focus: (options?: FocusOptions) => void } | null>(null)
const formElement = ref<HTMLFieldSetElement | null>(null)
const resourceUploader = ref<{ cancel: () => Promise<void> } | null>(null)
const resourceBatch = ref<ProjectResourceBatch | null>(null)
const resourceBusy = ref(false)

const form = reactive({
  title: '',
  origin: '',
  goal: '',
  scenario: '',
  explore_deadline: '',
  authorization_expires_at: '',
})
const egress = ref(testEgressDraft(props.schedule?.project?.test_egress))
const egressError = ref('')
const scheduleDraft = ref(props.schedule ? draftFromSchedule(props.schedule) : newScheduleDraft())
const retainedResourceIds = ref(props.schedule?.resources.map((resource) => resource.id) || [])
watch(
  scheduleDraft,
  () => {
    userEdited.value = true
  },
  { deep: true },
)
const createdScheduleId = ref<string | null>(null)
const recoveryMessage = ref('')
const cacheWarning = ref('')
type PendingSubmission =
  | { operation: 'project'; payload: CreateProjectRequest & { request_id: string } }
  | { operation: 'schedule'; payload: CreateScheduleRequest }
  | { operation: 'update'; scheduleId: string; payload: UpdateScheduleRequest }
const pendingSubmission = ref<PendingSubmission | null>(null)
const scheduleTiming = ref<InstanceType<typeof ScheduleTiming> | null>(null)
const scheduleModeGroup = useId()
const modeLabels: Record<ScheduleDraft['mode'], string> = {
  immediate: '立即', once: '定时', daily: '每日', weekly: '每周',
}

function restoreLibraryPickerFocus(event: Event) {
  event.preventDefault()
  if (props.open) document.getElementById('np-library-picker-trigger')?.focus()
}

const resourceSummary = ref('本地文件 0')
const formDisabled = computed(
  () =>
    isSubmitting.value ||
    isCancelling.value ||
    creationUnknown.value ||
    creationRejected.value ||
    Boolean(createdProjectId.value || createdScheduleId.value || creationDeleted.value),
)
const modeOptions = computed(() =>
  scheduleModeOptions(scheduleDraft.value.mode, Boolean(props.schedule)),
)
function setScheduleMode(value: string | number) {
  scheduleDraft.value.mode = value as ScheduleDraft['mode']
}
const submitLabel = computed(() =>
  props.schedule
    ? '保存未来配置'
    : scheduleDraft.value.mode === 'immediate'
      ? '创建并启动'
      : scheduleDraft.value.mode === 'once'
        ? '保存单次预约'
        : '保存重复计划',
)
const boundaryHint = computed(() =>
  props.schedule?.project && (form.explore_deadline || form.authorization_expires_at)
    ? '执行停止与授权到期仍是原样的绝对边界，可能使计划更早停止；不会按轮次自动续期。'
    : '',
)
if (props.schedule?.project) {
  const project = props.schedule.project
  Object.assign(form, {
    title: project.title,
    origin: project.origin,
    goal: project.goal,
    scenario: project.scenario,
    explore_deadline: inputLocalTime(project.explore_deadline),
    authorization_expires_at: inputLocalTime(project.authorization_expires_at),
  })
}
function inputLocalTime(value?: string | null) {
  if (!value) return ''
  const date = new Date(value)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}
const existingLibraryBinding = (key: string) =>
  props.schedule?.library_bindings.find((binding) => binding.library_key === key)
function retainResource(id: string, keep: boolean) {
  userEdited.value = true
  retainedResourceIds.value = keep
    ? [...retainedResourceIds.value, id]
    : retainedResourceIds.value.filter((item) => item !== id)
}
const scenarioTemplateMeta = reactive({
  selectedKey: props.schedule?.project?.scenario || (null as string | null),
  origin: { userTouched: Boolean(props.schedule), lastAutoApplied: null as string | null },
  goal: { userTouched: Boolean(props.schedule), lastAutoApplied: null as string | null },
})

const validationErrors = reactive({
  title: '',
  origin: '',
  goal: '',
  exploreDeadline: '',
  authorizationExpiresAt: '',
})

const {
  data: scenariosData,
  isLoading: scenariosLoading,
  isError: scenariosError,
  refetch: refetchScenarios,
} = useQuery<ScenarioCatalogEntry[]>({
  queryKey: ['scenario-catalog'],
  queryFn: () => api<ScenarioCatalogEntry[]>('GET', '/scenario-catalog'),
})
const scenarios = computed(() => scenariosData.value || [])
const selectedScenario = computed(() =>
  scenarios.value.find((scenario) => scenario.key === form.scenario),
)
const selectedScenarioExists = computed(() => Boolean(selectedScenario.value))

const {
  data: libraryCatalogData,
  isLoading: libraryCatalogLoading,
  isError: libraryCatalogError,
  refetch: refetchLibraryCatalog,
} = useQuery<AdvisoryLibrary[]>({
  queryKey: ['library-catalog'],
  queryFn: listLibraryCatalog,
})
const libraryCatalog = computed(() => libraryCatalogData.value || [])

// 已保存的选择是唯一权威状态；清单只提供该版本的可选文件元数据。
const originalBindings = props.schedule?.library_bindings || []
const legacySelectionState = !props.schedule || originalBindings.length || props.schedule.library_selection_explicit === true
  ? 'explicit' : props.schedule.library_selection_explicit === false ? 'preserve' : 'unknown'
const selectionEdited = ref(false)
const clearLegacyMaterials = ref(false)
const explicitLibrarySelection = computed(() =>
  legacySelectionState === 'explicit' || selectionEdited.value || clearLegacyMaterials.value,
)
const validBinding = (binding: ProjectLibraryBinding) =>
  originalBindings.filter((item) => item.library_key === binding.library_key).length === 1 &&
  Number.isInteger(binding.version) && binding.version > 0 &&
  Number.isInteger(binding.file_count) && binding.file_count >= 0 &&
  (binding.paths === null ||
    (Array.isArray(binding.paths) && binding.file_count > 0 && binding.paths.length === binding.file_count &&
      binding.paths.every((path) => typeof path === 'string' && path.length > 0) &&
      new Set(binding.paths).size === binding.paths.length))
const invalidBindings = ref(originalBindings.filter((binding) => !validBinding(binding)))
const selections = ref<LibrarySelection[]>(originalBindings.filter(validBinding).map((binding) => ({
  library_key: binding.library_key,
  source: 'saved',
  paths: binding.paths === null ? null : [...binding.paths],
})))
const boundManifests = reactive<Record<string, LibraryManifest>>(Object.create(null))
const currentManifests = reactive<Record<string, LibraryManifest>>(Object.create(null))
const browsedManifests = reactive<Record<string, LibraryManifest>>(Object.create(null))
const boundLoading = reactive<Record<string, boolean>>(Object.create(null))
const currentLoading = reactive<Record<string, boolean>>(Object.create(null))
const boundErrors = reactive<Record<string, string>>(Object.create(null))
const currentErrors = reactive<Record<string, string>>(Object.create(null))
const updateErrors = reactive<Record<string, string>>(Object.create(null))
const boundRequests: Record<string, number> = Object.create(null)
const currentRequests: Record<string, number> = Object.create(null)
const selectionChanges: Record<string, number> = Object.create(null)
let selectionDisposed = false
const selectionFor = (key: string) => selections.value.find((item) => item.library_key === key)
// 已选清单与浏览清单分开，后台刷新不能悄悄接受新的正文摘要。
const selectionManifest = (selection: LibrarySelection) => selection.source === 'current'
  ? currentManifests[selection.library_key]
  : boundManifests[selection.library_key]

function validateManifest(manifest: LibraryManifest, key: string, binding?: ProjectLibraryBinding) {
  if (!manifest || manifest.library_key !== key ||
      !Array.isArray(manifest.files) || manifest.file_count !== manifest.files.length ||
      manifest.files.some((file) => !file || typeof file.path !== 'string' || !file.path ||
        typeof file.size_bytes !== 'number' || typeof file.sha256 !== 'string') ||
      new Set(manifest.files.map((file) => file.path)).size !== manifest.files.length) {
    throw new Error('文件清单不完整或身份不匹配')
  }
  if (binding) {
    const paths = manifest.files.map((file) => file.path)
    if (manifest.file_count !== binding.file_count ||
        (binding.paths !== null && (!Array.isArray(binding.paths) ||
          binding.paths.length !== paths.length || binding.paths.some((path) => !paths.includes(path))))) {
      throw new Error('已绑定文件清单与计划不一致')
    }
  }
}

async function loadBoundManifest(key: string) {
  const binding = existingLibraryBinding(key)
  if (!props.schedule || !binding) return
  const request = boundRequests[key] = (boundRequests[key] || 0) + 1
  boundLoading[key] = true
  boundErrors[key] = ''
  try {
    const manifest = await getScheduleLibraryManifest(props.schedule.id, key)
    validateManifest(manifest, key, binding)
    if (!selectionDisposed && boundRequests[key] === request) {
      boundManifests[key] = manifest
      if (!browsedManifests[key]) browsedManifests[key] = manifest
    }
  } catch (error) {
    if (!selectionDisposed && boundRequests[key] === request)
      boundErrors[key] = error instanceof Error ? error.message : '读取已绑定文件失败'
  } finally {
    if (!selectionDisposed && boundRequests[key] === request) boundLoading[key] = false
  }
}

async function loadCurrentManifest(key: string): Promise<LibraryManifest | undefined> {
  const request = currentRequests[key] = (currentRequests[key] || 0) + 1
  currentLoading[key] = true
  currentErrors[key] = ''
  try {
    const manifest = await getLibraryCatalogManifest(key)
    validateManifest(manifest, key)
    if (selectionDisposed || currentRequests[key] !== request) return undefined
    if (!selectionFor(key) && !browsedManifests[key]) browsedManifests[key] = manifest
    return manifest
  } catch (error) {
    if (!selectionDisposed && currentRequests[key] === request)
      currentErrors[key] = error instanceof Error ? error.message : '读取当前资料失败'
  } finally {
    if (!selectionDisposed && currentRequests[key] === request) currentLoading[key] = false
  }
}

function removeLibrary(key: string) {
  userEdited.value = true
  selectionEdited.value = true
  selectionChanges[key] = (selectionChanges[key] || 0) + 1
  selections.value = selections.value.filter((item) => item.library_key !== key)
  updateErrors[key] = ''
}
function removeInvalidBinding(key: string) {
  userEdited.value = true
  selectionEdited.value = true
  invalidBindings.value = invalidBindings.value.filter((item) => item.library_key !== key)
}
function selectionState(key: string, paths: string[]) {
  const selection = selectionFor(key)
  const manifest = selection && selectionManifest(selection)
  const selected = !selection || !manifest ? 0 : paths.filter((path) =>
    selection.paths === null || selection.paths.includes(path),
  ).length
  return { checked: paths.length > 0 && selected === paths.length, indeterminate: selected > 0 && selected < paths.length }
}
function setPaths(key: string, paths: string[], checked: boolean, supplied?: LibraryManifest) {
  const previous = selectionFor(key)
  const manifest = previous ? selectionManifest(previous) : supplied
  if (!manifest) return
  const available = new Set(manifest.files.map((file) => file.path))
  const selected = new Set(previous ? (previous.paths ?? [...available]) : [])
  for (const path of paths) {
    if (!available.has(path)) continue
    if (checked) selected.add(path)
    else selected.delete(path)
  }
  userEdited.value = true
  selectionEdited.value = true
  selectionChanges[key] = (selectionChanges[key] || 0) + 1
  updateErrors[key] = ''
  if (!selected.size) {
    removeLibrary(key)
    return
  }
  const source = previous?.source ?? (boundManifests[key] === manifest ? 'saved' : 'current')
  const original = existingLibraryBinding(key)
  // 已绑定子集全选只代表保持该范围，不能扩大为原库的全部文件。
  const whole = selected.size === available.size && (source === 'current' || original?.paths === null)
  const next: LibrarySelection = {
    library_key: key, source,
    paths: whole ? null : manifest.files.map((file) => file.path).filter((path) => selected.has(path)),
    ...(source === 'current' ? { expected_files: Object.fromEntries(
      manifest.files.filter((file) => selected.has(file.path)).map((file) => [file.path, file.sha256]),
    ) } : {}),
  }
  if (source === 'current') currentManifests[key] = manifest
  selections.value = [...selections.value.filter((item) => item.library_key !== key), next]
}

async function updateLibraryVersion(key: string) {
  const previous = selectionFor(key)
  if (!previous || !selectionManifest(previous) || currentLoading[key]) return
  updateErrors[key] = ''
  // 旧空整库不能转成空路径数组，也不能借版本更新自动选中新文件。
  if (selectionManifest(previous)!.files.length === 0) {
    updateErrors[key] = '历史空整库绑定不能直接更新，请先移除后重新选择。'
    return
  }
  const manifest = await loadCurrentManifest(key)
  if (!manifest || selectionFor(key) !== previous) {
    if (!manifest && selectionFor(key) === previous) updateErrors[key] = currentErrors[key] || '读取当前资料失败'
    return
  }
  if (!manifest.files.length) {
    updateErrors[key] = '当前资料没有文件，原绑定未改变。'
    return
  }
  const available = new Set(manifest.files.map((file) => file.path))
  const previousPaths = previous.paths ?? selectionManifest(previous)!.files.map((file) => file.path)
  const missing = previousPaths.filter((path) => !available.has(path))
  if (missing.length) {
    updateErrors[key] = `当前资料缺少原选文件：${missing.join('、')}。请先处理原选择，原绑定未改变。`
    return
  }
  userEdited.value = true
  selectionChanges[key] = (selectionChanges[key] || 0) + 1
  selectionEdited.value = true
  browsedManifests[key] = manifest
  currentManifests[key] = manifest
  selections.value = selections.value.map((item) => item === previous ? {
    library_key: key, source: 'current',
    paths: previous.paths === null && previousPaths.length === available.size ? null : [...previousPaths],
    expected_files: Object.fromEntries(manifest.files.filter((file) => previousPaths.includes(file.path))
      .map((file) => [file.path, file.sha256])),
  } : item)
}

async function browseCurrentManifest(key: string) {
  const revision = selectionChanges[key] || 0
  const manifest = await loadCurrentManifest(key)
  if (manifest && !selectionFor(key) && (selectionChanges[key] || 0) === revision)
    browsedManifests[key] = manifest
}

const expandedNodes = ref(new Set<string>())
const nodeId = (key: string, path: string) => JSON.stringify([key, path])
let rootExpandedOnce = false
watch(libraryCatalog, (libraries) => {
  const root = libraries.find((library) => library.builtin)
  if (!root || rootExpandedOnce) return
  rootExpandedOnce = true
  expandedNodes.value = new Set([...expandedNodes.value, nodeId(root.key, '')])
}, { immediate: true })
const isExpanded = (key: string, path: string) =>
  Boolean(librarySearch.value.trim()) || expandedNodes.value.has(nodeId(key, path))
function toggleExpanded(key: string, path: string) {
  const next = new Set(expandedNodes.value)
  const id = nodeId(key, path)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedNodes.value = next
}
interface TreeRow { path: string; name: string; directory: boolean; depth: number; paths: string[] }
function treeRows(key: string, manifest: LibraryManifest, rootMatches: boolean): TreeRow[] {
  const rows = new Map<string, TreeRow>()
  for (const file of manifest.files) {
    const parts = file.path.split('/')
    parts.forEach((name, index) => {
      const path = parts.slice(0, index + 1).join('/')
      const row = rows.get(path) || { path, name, directory: index < parts.length - 1, depth: index, paths: [] }
      row.paths.push(file.path)
      rows.set(path, row)
    })
  }
  const search = librarySearch.value.trim().toLowerCase()
  return [...rows.values()].sort((a, b) =>
    Number(b.path === manifest.safety_path) - Number(a.path === manifest.safety_path) || a.path.localeCompare(b.path),
  ).filter((row) => {
    if (search) return rootMatches || row.path.toLowerCase().includes(search) ||
      row.paths.some((path) => path.toLowerCase().includes(search))
    if (!isExpanded(key,'')) return false
    const parts = row.path.split('/')
    return parts.slice(0, -1).every((_, index) =>
      isExpanded(key,parts.slice(0, index + 1).join('/')),
    )
  })
}
const pickerLibraries = computed(() => {
  const libraries = new Map(libraryCatalog.value.map((item) => [item.key, { key: item.key, name: item.builtin ? '根目录' : item.name, builtin: item.builtin }]))
  for (const selection of selections.value) {
    const binding = existingLibraryBinding(selection.library_key)
    if (!libraries.has(selection.library_key)) libraries.set(selection.library_key, {
      key: selection.library_key, name: binding?.library_name || selection.library_key, builtin: false,
    })
  }
  const search = librarySearch.value.trim().toLowerCase()
  const root = libraryCatalog.value.find((library) => library.builtin)
  return [...libraries.values()].sort((a, b) => Number(b.builtin) - Number(a.builtin)).flatMap((library) => {
    if (!library.builtin && root && !isExpanded(root.key, '')) return []
    const selection = selectionFor(library.key)
    const manifest = selection ? selectionManifest(selection) : browsedManifests[library.key]
    const bound = selection ? selection.source === 'saved' : manifest === boundManifests[library.key]
    const loading = bound ? boundLoading[library.key] || currentLoading[library.key] : currentLoading[library.key]
    const error = bound ? boundErrors[library.key] || currentErrors[library.key] : currentErrors[library.key]
    const limited = bound && existingLibraryBinding(library.key)?.paths !== null
    const rootMatches = !search || `${library.name} ${library.key}`.toLowerCase().includes(search)
    const rows = manifest ? treeRows(library.key, manifest, rootMatches) : []
    if (search && !library.builtin && !rootMatches && !rows.length && manifest) return []
    return [{ ...library, manifest, loading, error, rows, limited }]
  })
})
function retryLibrary(key: string) {
  const selection = selectionFor(key)
  if (existingLibraryBinding(key) && (!selection || selection.source === 'saved') && boundErrors[key]) void loadBoundManifest(key)
  else void browseCurrentManifest(key)
}
watch([libraryPickerOpen, libraryCatalog, selections], () => {
  if (!libraryPickerOpen.value) return
  for (const library of libraryCatalog.value) {
    if (!selectionFor(library.key) && !existingLibraryBinding(library.key) && !browsedManifests[library.key] &&
        !currentLoading[library.key] && !currentErrors[library.key]) void loadCurrentManifest(library.key)
  }
}, { immediate: true })
for (const selection of selections.value) void loadBoundManifest(selection.library_key)

const selectedFiles = computed(() => {
  const files = selections.value.flatMap((selection) => {
    const manifest = selectionManifest(selection)
    if (!manifest) return []
    return manifest.files.filter((file) => selection.paths === null || selection.paths.includes(file.path))
      .map((file) => ({
        identity: JSON.stringify([selection.library_key, file.path]),
        libraryKey: selection.library_key, libraryName: manifest.library_name,
        path: file.path, name: file.path.split('/').at(-1)!, label: '',
      }))
  })
  for (const file of files) {
    const duplicates = files.filter((other) => other.name === file.name)
    if (duplicates.length === 1) { file.label = file.name; continue }
    const parts = file.path.split('/')
    let suffix = file.name
    for (let count = 2; count <= parts.length; count++) {
      suffix = parts.slice(-count).join('/')
      if (duplicates.every((other) => other === file || !other.path.endsWith(`/${suffix}`) && other.path !== suffix)) break
    }
    const collisions = duplicates.filter((other) => other !== file && (other.path === suffix || other.path.endsWith(`/${suffix}`)))
    file.label = collisions.length ? `${file.libraryName} (${file.libraryKey}) · ${suffix}` : suffix
  }
  return files
})

const safetySelected = computed(() => selections.value.some((selection) => {
  const manifest = selectionManifest(selection)
  return Boolean(manifest?.safety_path && manifest.files.some((file) => file.path === manifest.safety_path) &&
    (selection.paths === null || selection.paths.includes(manifest.safety_path)))
}))
async function setSafetySelection(checked: boolean, automatic = false) {
  const generation = ++safetyGeneration
  safetyPending.value = checked
  safetyError.value = ''
  if (!automatic) userEdited.value = true
  if (!checked) {
    for (const selection of [...selections.value]) {
      const path = selectionManifest(selection)?.safety_path
      if (path) setPaths(selection.library_key, [path], false)
    }
    return
  }
  const root = libraryCatalog.value.find((library) => library.builtin)
  if (!root) {
    safetyError.value = '根目录暂不可用，请重试加载或关闭本次安全资料选择。'
    return
  }
  const previous = selectionFor(root.key)
  const change = selectionChanges[root.key] || 0
  const manifest = previous ? selectionManifest(previous) : await loadCurrentManifest(root.key)
  if (selectionDisposed || generation !== safetyGeneration) return
  if ((selectionChanges[root.key] || 0) !== change) {
    safetyPending.value = false
    return
  }
  if (!manifest?.safety_path || !manifest.files.some((file) => file.path === manifest.safety_path)) {
    safetyError.value = previous?.source === 'saved'
      ? '计划已保存资料不含安全文件；请明确更新该库资料后再选择。'
      : '无法读取执行安全约束，请重试或关闭本次选择。'
    return
  }
  const wasEdited = userEdited.value
  setPaths(root.key, [manifest.safety_path], true, manifest)
  if (automatic) userEdited.value = wasEdited
  safetyPending.value = false
}
let defaultSafetyStarted = false
watch(libraryCatalog, (libraries) => {
  if (props.schedule || defaultSafetyStarted || !safetyPending.value || !libraries.some((item) => item.builtin)) return
  defaultSafetyStarted = true
  void setSafetySelection(true, true)
}, { immediate: true })

const submitBlockReason = computed(() => {
  if (creationDeleted.value) return '原请求已保存但项目已删除，不能用此请求重新创建。'
  if (creationRejected.value) return '请处理已拒绝的原请求。'
  if (safetyPending.value) return safetyError.value || '正在加入执行安全约束，请稍候或关闭本次选择。'
  if (legacySelectionState === 'unknown' && !explicitLibrarySelection.value)
    return '计划的历史资料选择状态未知，请核实或明确不再使用历史默认资料。'
  if (invalidBindings.value.length) return '部分计划绑定范围未知，请核实或移除该绑定。'
  if (selections.value.some((item) => currentLoading[item.library_key])) return '正在读取所选资料库的当前资料，请稍候。'
  if (selections.value.some((item) => !selectionManifest(item))) return '已选资料文件清单未能确认，请重试或移除绑定。'
  if (resourceBusy.value) return '资料操作尚未完成，请稍候。'
  if (resourcePending.value) return '仍有未完成或结果未确认的资料，请处理或明确移除后再创建。'
  if (scenariosLoading.value) return '正在加载创建所需配置。'
  if (scenariosError.value || !selectedScenarioExists.value)
    return '请选择可用场景，或重试加载场景目录。'
  return ''
})
const canSubmitProject = computed(
  () =>
    !isSubmitting.value &&
    !isCancelling.value &&
    !creationUnknown.value &&
    !createdProjectId.value &&
    !createdScheduleId.value &&
    !submitBlockReason.value,
)

const selectScenario = (scenario: ScenarioCatalogEntry, userSelected = false) => {
  if (userSelected) userEdited.value = true
  const currentState: ScenarioTemplateState = {
    selectedKey: scenarioTemplateMeta.selectedKey,
    origin: {
      value: form.origin,
      userTouched: scenarioTemplateMeta.origin.userTouched,
      lastAutoApplied: scenarioTemplateMeta.origin.lastAutoApplied,
    },
    goal: {
      value: form.goal,
      userTouched: scenarioTemplateMeta.goal.userTouched,
      lastAutoApplied: scenarioTemplateMeta.goal.lastAutoApplied,
    },
  }
  const nextState = applyScenarioTemplates(currentState, scenario)

  form.scenario = scenario.key
  form.origin = nextState.origin.value
  form.goal = nextState.goal.value
  scenarioTemplateMeta.selectedKey = nextState.selectedKey
  scenarioTemplateMeta.origin.userTouched = nextState.origin.userTouched
  scenarioTemplateMeta.origin.lastAutoApplied = nextState.origin.lastAutoApplied
  scenarioTemplateMeta.goal.userTouched = nextState.goal.userTouched
  scenarioTemplateMeta.goal.lastAutoApplied = nextState.goal.lastAutoApplied
}

function selectScenarioByKey(key: string | number) {
  const scenario = scenarios.value.find((item) => item.key === key)
  if (scenario) selectScenario(scenario, true)
}

watch(
  scenarios,
  (list) => {
    if (list.length === 0) return
    const selected = list.find((scenario) => scenario.key === form.scenario)
    if (selected) {
      selectScenario(selected)
      return
    }
    if (props.schedule) return
    selectScenario(list.find((scenario) => scenario.is_default) || list[0])
  },
  { immediate: true },
)

function retainedDeadline(value: string, original?: string | null) {
  if (!value) return null
  return original && value === inputLocalTime(original) ? original : new Date(value).toISOString()
}
function effectiveDeadlines() {
  if (props.schedule?.project) {
    return {
      explore_deadline: retainedDeadline(
        form.explore_deadline,
        props.schedule.project.explore_deadline,
      ),
      authorization_expires_at: retainedDeadline(
        form.authorization_expires_at,
        props.schedule.project.authorization_expires_at,
      ),
    }
  }
  return {
    explore_deadline:
      scheduleDraft.value.mode === 'once'
        ? scheduleTimesFromDraft(scheduleDraft.value).deadline
        : null,
    authorization_expires_at: null,
  }
}
const previewProject = computed<CreateProjectRequest | undefined>(() => {
  if (!form.title.trim() || !form.origin.trim() || !form.goal.trim() || !form.scenario)
    return undefined
  let deadlines
  try {
    deadlines = effectiveDeadlines()
  } catch (error) {
    if (error instanceof RangeError || error instanceof ScheduleTimeError) return undefined
    throw error
  }
  return {
    ...deadlines,
    title: form.title,
    origin: form.origin,
    goal: form.goal,
    scenario: form.scenario,
    ...(props.schedule?.project?.targets != null
      ? { targets: props.schedule.project.targets.map((target) => ({ ...target })) }
      : {}),
  }
})

const validateForm = (): { valid: boolean; firstErrorAnchor: string | null } => {
  let valid = true
  let firstErrorAnchor: string | null = null

  if (!form.title.trim()) {
    validationErrors.title = '项目标题不能为空'
    valid = false
    firstErrorAnchor = 'np-section-basic'
  } else {
    validationErrors.title = ''
  }

  if (!form.origin.trim()) {
    validationErrors.origin = '已知信息不能为空'
    valid = false
    firstErrorAnchor = firstErrorAnchor || 'np-origin'
  } else {
    validationErrors.origin = ''
  }

  if (!form.goal.trim()) {
    validationErrors.goal = '目标不能为空'
    valid = false
    firstErrorAnchor = firstErrorAnchor || 'np-goal'
  } else {
    validationErrors.goal = ''
  }

  const validateFutureDate = (value: string, label: string) => {
    if (!value) return ''
    const timestamp = new Date(value).getTime()
    if (!Number.isFinite(timestamp)) return `${label}格式无效`
    if (timestamp <= Date.now()) return `${label}必须晚于当前时间`
    return ''
  }
  validationErrors.exploreDeadline = props.schedule?.project
    ? validateFutureDate(form.explore_deadline, '执行停止时间')
    : ''
  validationErrors.authorizationExpiresAt = props.schedule?.project
    ? validateFutureDate(form.authorization_expires_at, '授权到期时间')
    : ''
  if (validationErrors.exploreDeadline || validationErrors.authorizationExpiresAt) {
    valid = false
    firstErrorAnchor = firstErrorAnchor || 'np-timing-settings'
  }

  egressError.value = testEgressError(egress.value)
  if (egressError.value) {
    valid = false
    firstErrorAnchor = firstErrorAnchor || 'np-proxy-settings'
  }
  return { valid, firstErrorAnchor }
}

const submitProject = async () => {
  if (
    isSubmitting.value ||
    isCancelling.value ||
    creationUnknown.value ||
    creationRejected.value ||
    creationDeleted.value ||
    createdProjectId.value ||
    createdScheduleId.value ||
    !props.open ||
    libraryPickerOpen.value ||
    discardOpen.value
  )
    return
  formError.value = ''
  workerConfigurationError.value = false
  if (safetyPending.value || (legacySelectionState === 'unknown' && !explicitLibrarySelection.value) ||
      invalidBindings.value.length || selections.value.some((item) => currentLoading[item.library_key] || !selectionManifest(item))) {
    formError.value = submitBlockReason.value
    document.getElementById('np-materials')?.scrollIntoView({ block: 'start' })
    return
  }
  if (resourcePending.value) {
    formError.value = '仍有未完成的资料，请补传、核对或明确移除后再创建。'
    document.getElementById('np-materials')?.scrollIntoView({ block: 'start' })
    return
  }
  if (!selectedScenarioExists.value) {
    uiStore.showToast(
      scenariosLoading.value ? '场景目录仍在加载，请稍后重试' : '请选择有效的探索场景',
      'danger',
    )
    return
  }
  if (resourceBusy.value) {
    uiStore.showToast('辅助资料仍在上传，请稍后重试', 'danger')
    return
  }

  try {
    const { valid, firstErrorAnchor } = validateForm()
    const timing =
      scheduleDraft.value.mode !== 'immediate' ? scheduleTiming.value!.validate() : null
    if (!valid || (scheduleDraft.value.mode !== 'immediate' && !timing)) {
      uiStore.showToast('请检查表单必填项和时间安排', 'danger')
      // 只滚动到首个错误所在区域，避免后续时间错误抢走正文定位。
      const anchor = firstErrorAnchor || 'np-timing-settings'
      await nextTick()
      document.getElementById(anchor)?.scrollIntoView({ block: 'start' })
      return
    }

    const rules = timing?.rules
    const {
      request_id: _requestId,
      resource_batch_id: _batchId,
      resources: _resources,
      library_keys: _legacyLibraryKeys,
      library_selections: _oldSelections,
      ...retainedInput
    } = (props.schedule?.project || {}) as Partial<CreateProjectRequest> & { library_keys?: unknown }
    // 旧计划原显式 targets（包括空数组）随原输入保留，新建不补数组或重解释来源。
    const payload: CreateProjectRequest = {
      ...retainedInput,
      test_egress: testEgressInput(egress.value),
      title: form.title.trim(),
      origin: form.origin,
      goal: form.goal,
      scenario: form.scenario,
      ...effectiveDeadlines(),
      ...(resourceBatch.value ? { resource_batch_id: resourceBatch.value.id } : {}),
      ...(explicitLibrarySelection.value ? { library_selections: selections.value.map((item) => ({
        ...item, paths: item.paths === null ? null : [...item.paths],
      })) } : {}),
    }
    if (props.schedule && rules) {
      pendingSubmission.value = {
        operation: 'update',
        scheduleId: props.schedule.id,
        payload: {
          ...rules,
          project: payload,
          version: props.schedule.version,
          resource_ids: [...retainedResourceIds.value],
          ...(legacySelectionState !== 'explicit' && explicitLibrarySelection.value
            ? { library_selection_explicit: true } : {}),
        },
      }
    } else {
      const requestId = createCreationRequestId()
      pendingSubmission.value = rules
        ? { operation: 'schedule', payload: { ...rules, project: payload, request_id: requestId } }
        : { operation: 'project', payload: { ...payload, request_id: requestId } }
    }
  } catch (error) {
    pendingSubmission.value = null
    formError.value = `请求尚未发出：${error instanceof Error ? error.message : '本地准备失败'}。可保留填写内容后重试。`
    return
  }
  await sendPendingSubmission()
}

async function sendPendingSubmission() {
  const submission = pendingSubmission.value
  if (!submission || isSubmitting.value || creationDeleted.value) return
  isSubmitting.value = true
  formError.value = ''
  recoveryMessage.value = ''
  try {
    if (submission.operation !== 'update') {
      const cache = rememberCreationRequest({
        request_id: submission.payload.request_id,
        operation: submission.operation,
      })
      if (cache.warning) cacheWarning.value = cache.warning
    }
    let savedSchedule: ProjectSchedule | undefined
    // 只有业务请求异常能够判为明确拒绝或结果未知；缓存与成功后反馈不拥有这个结局。
    try {
      if (submission.operation === 'project') {
        const response = await createProject(submission.payload)
        createdProjectId.value = response.project.id
      } else {
        savedSchedule =
          submission.operation === 'update'
            ? await updateProjectSchedule(submission.scheduleId, submission.payload)
            : await createProjectSchedule(submission.payload)
        createdScheduleId.value = savedSchedule.id
      }
    } catch (error) {
      // 新合同可能在收据重放前拒绝旧 payload；先按原请求 ID 查询，不能据 422 换键重发。
      if (submission.operation !== 'update' && error instanceof ApiError && error.status === 422) {
        formError.value = error.message
        workerConfigurationError.value = error.message.includes('Worker')
        creationRejected.value = false
        creationUnknown.value = true
        await queryCreationResult(true)
        return
      }
      // 恢复请求的拒绝不证明最初请求未提交，仍保留原键和原内容供查询。
      const definiteRejection =
        error instanceof ApiError &&
        error.status >= 400 &&
        error.status < 500 &&
        error.status !== 408
      formError.value = error instanceof Error ? error.message : '未收到可确认的保存结果'
      workerConfigurationError.value = definiteRejection && formError.value.includes('Worker')
      if (definiteRejection && !creationUnknown.value) {
        if (submission.operation !== 'update' && (error.status === 422 || creationRejected.value)) {
          creationRejected.value = true
        } else {
          if (submission.operation !== 'update') {
            const cache = forgetCreationRequest(submission.payload.request_id)
            if (cache.warning) cacheWarning.value = cache.warning
          }
          pendingSubmission.value = null
        }
      } else {
        creationRejected.value = false
        creationUnknown.value = true
      }
      return
    }
    creationRejected.value = false
    creationUnknown.value = false
    pendingSubmission.value = null
    egress.value = { ...egress.value, username: '', password: '', auth_action: 'keep' }
    resourceBatch.value = null
    if (submission.operation !== 'update') {
      const cache = forgetCreationRequest(submission.payload.request_id)
      if (cache.warning) cacheWarning.value = cache.warning
    }
    try {
      await nextTick()
      if (formElement.value?.parentElement) formElement.value.parentElement.scrollTop = 0
      uiStore.showToast(createdScheduleId.value ? '计划已保存。' : '已创建，等待调度。', 'success')
      if (submission.operation === 'update') emit('saved', savedSchedule!)
      else await enterCreatedProject()
    } catch (error) {
      navigationError.value = `已确认保存，后续页面反馈未完成：${error instanceof Error ? error.message : '无法显示反馈'}。请使用上方入口。`
    }
  } finally {
    isSubmitting.value = false
  }
}
async function restoreSubmission() {
  // 恢复只使用内存中的原始 payload，绝不读取修改后的表单或换请求 ID。
  await sendPendingSubmission()
}
function editRejectedSubmission() {
  const submission = pendingSubmission.value
  if (!creationRejected.value || !submission || submission.operation === 'update' || isSubmitting.value) return
  const cache = forgetCreationRequest(submission.payload.request_id)
  if (cache.warning) cacheWarning.value = cache.warning
  pendingSubmission.value = null
  creationRejected.value = false
  formError.value = ''
}
async function queryCreationResult(fromSubmission: boolean | Event = false) {
  const submission = pendingSubmission.value
  if (!submission || submission.operation === 'update' || (isSubmitting.value && fromSubmission !== true)) return
  isSubmitting.value = true
  try {
    const result = await getCreationRequest({
      request_id: submission.payload.request_id,
      operation: submission.operation,
    })
    if (result.status === 'committed') {
      createdScheduleId.value = result.schedule_id
      createdProjectId.value = result.schedule_id ? null : result.project_id
      creationUnknown.value = false
      creationDeleted.value = !result.schedule_id && !result.project_id
      formError.value = ''
      resourceBatch.value = null
      const cache = forgetCreationRequest(result.request_id)
      if (cache.warning) cacheWarning.value = cache.warning
      pendingSubmission.value = null
      egress.value = { ...egress.value, username: '', password: '', auth_action: 'keep' }
      if (!creationDeleted.value) await enterCreatedProject()
    } else recoveryMessage.value = '尚未查到已提交结果，不代表请求失败；可用原请求恢复提交。'
  } catch (error) {
    recoveryMessage.value = error instanceof Error ? error.message : '查询失败，请稍后重试'
  } finally {
    isSubmitting.value = false
  }
}

const enterCreatedProject = async () => {
  if (!createdProjectId.value && !createdScheduleId.value) return
  navigationError.value = ''
  try {
    const result = await router.push(
      createdScheduleId.value
        ? `/project-schedules/${createdScheduleId.value}`
        : `/projects/${createdProjectId.value}`,
    )
    if (isNavigationFailure(result)) navigationError.value = '跳转未完成，可再次进入已创建项目。'
  } catch (error) {
    navigationError.value = `跳转失败：${error instanceof Error ? error.message : '无法进入项目'}。请使用上方入口。`
  }
}

const requestClose = (open: boolean) => {
  if (open || resourceBusy.value || isSubmitting.value || isCancelling.value) return
  if (createdProjectId.value || createdScheduleId.value || creationDeleted.value) {
    emit('discarded')
  } else if (creationUnknown.value) {
    emit('update:open', false)
  } else if (userEdited.value || resourceBatch.value || resourcePending.value) {
    discardOpen.value = true
  } else {
    emit('discarded')
  }
}

const discardProject = async () => {
  if (
    resourceBusy.value ||
    isSubmitting.value ||
    isCancelling.value ||
    creationUnknown.value ||
    creationDeleted.value ||
    createdProjectId.value ||
    createdScheduleId.value
  )
    return
  isCancelling.value = true
  cleanupError.value = ''
  try {
    await resourceUploader.value?.cancel()
    if (creationRejected.value && pendingSubmission.value && pendingSubmission.value.operation !== 'update') {
      const cache = forgetCreationRequest(pendingSubmission.value.payload.request_id)
      if (cache.warning) cacheWarning.value = cache.warning
    }
    discardOpen.value = false
    emit('discarded')
  } catch (error) {
    cleanupError.value = error instanceof Error ? error.message : '取消临时上传批次失败'
  } finally {
    isCancelling.value = false
  }
}

const focusTitle = (event: Event) => {
  event.preventDefault()
  void nextTick(() => titleInput.value?.focus())
}
watch(
  () => props.open,
  async (open, previous) => {
    if (open || !previous) return
    await nextTick()
    if (props.returnFocus?.isConnected) props.returnFocus.focus()
  },
)
const restoreFocus = (event: Event) => {
  if (props.open) return
  if (props.returnFocus?.isConnected) {
    event.preventDefault()
    props.returnFocus.focus()
  }
}

onBeforeRouteLeave(() => {
  if (createdProjectId.value || createdScheduleId.value || creationDeleted.value) return true
  if (creationUnknown.value && !isSubmitting.value && !resourceBusy.value && !isCancelling.value)
    return true
  if (
    resourceBusy.value ||
    isSubmitting.value ||
    isCancelling.value ||
    creationUnknown.value ||
    userEdited.value ||
    resourceBatch.value ||
    resourcePending.value
  ) {
    emit('update:open', true)
    if (!creationUnknown.value && !resourceBusy.value && !isSubmitting.value && !isCancelling.value)
      discardOpen.value = true
    return false
  }
  return true
})

const handleBeforeUnload = (event: BeforeUnloadEvent) => {
  if (
    !createdProjectId.value &&
    !createdScheduleId.value &&
    !creationDeleted.value &&
    (isSubmitting.value ||
      resourceBusy.value ||
      isCancelling.value ||
      (!creationUnknown.value &&
        (userEdited.value || resourceBatch.value || resourcePending.value)))
  ) {
    event.preventDefault()
    event.returnValue = ''
  }
}
const handleKeydown = (event: KeyboardEvent) => {
  if (
    (event.metaKey || event.ctrlKey) &&
    event.key === 'Enter' &&
    !event.isComposing &&
    event.keyCode !== 229 &&
    props.open &&
    !libraryPickerOpen.value &&
    !discardOpen.value
  ) {
    event.preventDefault()
    void submitProject()
  }
}

onMounted(() => {
  window.addEventListener('keydown', handleKeydown)
  window.addEventListener('beforeunload', handleBeforeUnload)
})

onUnmounted(() => {
  selectionDisposed = true
  window.removeEventListener('keydown', handleKeydown)
  window.removeEventListener('beforeunload', handleBeforeUnload)
})
</script>

<style src="./NewProject.css" scoped></style>
