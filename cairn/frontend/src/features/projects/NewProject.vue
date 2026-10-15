<template>
  <Dialog
    :open="open"
    :title="schedule ? '编辑未来配置' : '新建项目'"
    :description="
      schedule
        ? '仅影响尚未生成的项目，已生成项目及其资料不变。'
        : '描述背景与目标，准备资料并选择执行资源。'
    "
    size="lg"
    :dismissible="!isSubmitting && !resourceBusy && !isCancelling && !isSavingDefault"
    :close-on-outside="false"
    @update:open="requestClose"
    @open-auto-focus="focusTitle"
    @close-auto-focus="restoreFocus"
    @escape-key-down="closeSettingsOnEscape"
  >
    <Alert v-if="creationUnknown" tone="warning" title="保存结果未确认">
      请求可能已被服务端接受，不要另建请求或取消可能已消费的资料。可正常导航；正文及原始请求仅在本次页面会话内保留，刷新后只能查询请求结果。
      <p v-if="formError">{{ formError }}</p>
      <p v-if="recoveryMessage">{{ recoveryMessage }}</p>
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
      <div id="np-section-basic" class="creation-content">
        <details
          class="scenario-settings"
          :open="scenarioExpanded"
          @toggle="scenarioExpanded = ($event.target as HTMLDetailsElement).open"
        >
          <summary :aria-label="scenarioExpanded ? '收起探索场景' : '展开探索场景'" :aria-expanded="scenarioExpanded">
            <span class="scenario-name">探索场景</span>
            <span class="summary-action"><ChevronDown :size="14" aria-hidden="true" /></span>
          </summary>
          <div>
            <div v-if="scenariosLoading" class="skeleton-scenarios" aria-label="正在加载场景">
              <Skeleton v-for="i in 2" :key="i" class="skeleton-card" />
            </div>
            <ErrorState
              v-else-if="scenariosError"
              title="场景目录加载失败"
              description="场景目录暂不可用，无法创建项目。"
              @retry="refetchScenarios()"
            />
            <EmptyState
              v-else-if="scenarios.length === 0"
              title="暂无可用场景"
              description="场景目录为空，暂时无法创建项目。"
            />
            <div v-else class="scenarios-grid">
              <button
                v-for="sc in scenarios"
                :key="sc.key"
                type="button"
                class="scenario-card"
                :class="{ selected: form.scenario === sc.key }"
                :aria-pressed="form.scenario === sc.key"
                @click="selectScenario(sc, true)"
              >
                <span class="scenario-header">
                  <span class="scenario-title">{{ sc.label }}</span>
                  <Badge v-if="sc.is_default" tone="info" size="sm">默认项</Badge>
                </span>
              </button>
            </div>
          </div>
        </details>
        <div class="project-identity">
          <FormField label="项目名称" for="np-title" required :error="validationErrors.title">
            <Input
              id="np-title"
              ref="titleInput"
              v-model="form.title"
              placeholder="例如: arena-web-recon"
            />
          </FormField>

          <FormField
            id="np-group"
            class="validation-anchor"
            label="所属项目组"
            for="np-group-select"
            required
            :error="validationErrors.group"
          >
            <div v-if="groupsLoading" aria-label="正在加载项目组">
              <Skeleton class="select-skeleton" />
            </div>
            <ErrorState
              v-else-if="groupsError"
              title="项目组加载失败"
              description="无法确认当前 Tenant 的项目组，暂时无法创建项目。"
              @retry="refetchGroups()"
            />
            <EmptyState v-else-if="groups.length === 0" title="暂无可用项目组">
              <template #description>
                请先前往<router-link to="/projects">项目列表</router-link>创建项目组。
              </template>
            </EmptyState>
            <Select
              v-else
              id="np-group-select"
              v-model="form.group_id"
              :options="groupOptions"
              placeholder="请选择项目组"
              required
              @update:model-value="userEdited = true"
            />
          </FormField>
        </div>

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

        <details ref="materialsDetails" class="project-materials" aria-label="项目资料">
          <summary>
            <Paperclip :size="16" />
            <span class="materials-summary" aria-live="polite"
              >资料 · {{ resourceSummary }} · 资料库 {{ form.library_keys.length
              }}{{ schedule ? ` · 保留原件 ${retainedResourceIds.length}` : '' }}</span
            >
            <span class="summary-action">管理资料 <ChevronDown :size="14" /></span>
          </summary>
          <div class="materials-content">
            <div v-if="schedule" class="selected-libraries">
              <div
                v-for="resource in schedule.resources"
                :key="resource.id"
                class="selected-library"
              >
                <label
                  ><Checkbox
                    :model-value="retainedResourceIds.includes(resource.id)"
                    @update:model-value="retainResource(resource.id, $event)"
                  />
                  {{ resource.path }} · {{ bytes(resource.size_bytes) }} ·
                  {{
                    retainedResourceIds.includes(resource.id) ? '保留服务器原件' : '未来移除'
                  }}</label
                >
              </div>
              <p class="section-desc">
                已有原件无需重新上传；取消保留仅从未来配置移除，不改变历史项目。
              </p>
            </div>
            <ProjectResourceBatchUploader
              ref="resourceUploader"
              v-model="resourceBatch"
              :disabled="
                isSubmitting ||
                creationUnknown ||
                Boolean(createdProjectId || createdScheduleId || creationDeleted) ||
                isCancelling
              "
              @busy="resourceBusy = $event"
              @pending="resourcePending = $event"
              @summary="resourceSummary = $event"
            >
              <template #actions>
                <Button
                  size="sm"
                  variant="secondary"
                  :disabled="
                    isSubmitting ||
                    creationUnknown ||
                    Boolean(createdProjectId || createdScheduleId || creationDeleted) ||
                    isCancelling
                  "
                  @click="libraryPickerOpen = true"
                  >从资料库选择</Button
                >
              </template>
            </ProjectResourceBatchUploader>
            <div v-if="form.library_keys.length" class="selected-libraries">
              <div v-for="key in form.library_keys" :key="key" class="selected-library">
                <span
                  >资料库 · {{ libraryCatalog.find((item) => item.key === key)?.name || key }}</span
                >
                <span v-if="existingLibraryBinding(key)" class="section-desc">{{
                  refreshLibraryKeys.includes(key)
                    ? '保存时更新为当前版本'
                    : `固定 v${existingLibraryBinding(key)!.version}`
                }}</span>
                <Button
                  v-if="existingLibraryBinding(key)"
                  size="sm"
                  variant="ghost"
                  @click="toggleLibraryRefresh(key)"
                  >{{ refreshLibraryKeys.includes(key) ? '保留原版本' : '更新为当前版本' }}</Button
                >
                <Button size="sm" variant="ghost" @click="toggleLibrary(key)">取消关联</Button>
              </div>
            </div>
          </div>
        </details>

        <section
          v-show="settingsPanel === 'execution'"
          id="np-execution-settings"
          class="settings-panel"
          aria-label="执行设置"
        >
          <div class="settings-panel-heading">
            <h3>执行设置</h3>
            <Button size="sm" variant="ghost" @click="closeSettings">完成设置</Button>
          </div>
          <div class="advanced-settings-content">
            <Alert v-if="workerSelectionIssue" tone="warning" title="执行组合需要处理">
              {{ workerSelectionIssue }}
            </Alert>
            <Alert v-if="defaultSaveError" tone="danger" title="默认组合保存未完成">
              {{ defaultSaveError }}
              <template #actions>
                <Button size="sm" variant="secondary" @click="refetchWorkerTemplates()">刷新模板核对</Button>
              </template>
            </Alert>
            <Alert v-if="defaultSaveMessage" tone="success" title="默认组合已保存">
              {{ defaultSaveMessage }}
            </Alert>
            <div v-if="!workersLoading && !workersError && invalidSelectedWorkers.length" class="selected-libraries">
              <div v-for="item in invalidSelectedWorkers" :key="item.id" class="selected-library">
                <span>{{ item.name }} · {{ item.reason }}</span>
                <Button size="sm" variant="ghost" @click="setWorkerSelection(item.id, false)">移除失效绑定</Button>
              </div>
            </div>
            <FormField
              id="np-workers"
              class="api-config-group validation-anchor"
              label="Workers"
              required
            >
              <div v-if="workersLoading" class="worker-loading" aria-label="正在加载 Worker">
                <Skeleton v-for="i in 2" :key="i" class="worker-skeleton" />
              </div>
              <ErrorState
                v-else-if="workersError"
                title="Worker 加载失败"
                description="无法确认当前 Tenant 的可用 Worker。"
                @retry="refetchWorkers()"
              />
              <EmptyState v-else-if="workers.length === 0" title="暂无可用 Worker">
                <template #description>
                  <template v-if="allWorkers.length === 0">
                    当前 Tenant 尚未创建 Worker。请前往
                    <router-link to="/console/workers">Workers</router-link> 新增配置。
                  </template>
                  <template v-else>
                    现有 Worker 配置尚未就绪。请前往
                    <router-link to="/console/workers">Workers</router-link> 查看 Model、Key
                    或启用状态。
                  </template>
                </template>
              </EmptyState>
              <template v-else>
                <FormField label="Worker 模板" for="np-worker-template">
                  <Skeleton v-if="workerTemplatesLoading" class="select-skeleton" />
                  <ErrorState
                    v-else-if="workerTemplatesError"
                    title="Worker 模板加载失败"
                    description="仍可手动选择 Worker。"
                    @retry="refetchWorkerTemplates()"
                  />
                  <Select
                    v-else
                    id="np-worker-template"
                    v-model="selectedTemplateId"
                    :options="workerTemplateOptions"
                    @update:model-value="applyWorkerTemplate"
                  />
                </FormField>
                <div class="template-row">
                  <Button size="sm" variant="secondary" :disabled="!defaultWorkerTemplate || workerTemplatesError || workerTemplatesFetching" @click="restoreDefaultWorkers">恢复默认组合</Button>
                  <Button size="sm" variant="secondary" :loading="isSavingDefault" :disabled="!canSaveDefault" @click="saveDefaultWorkers">保存为默认组合</Button>
                </div>
                <p class="section-desc">默认只用于以后新建，不改变已保存的项目、计划或当前草稿。并发可在 Workers 设置；三个专职 Worker 建议从 1／1／3 起步，单项目总上限建议为 4。</p>
                <div class="worker-options">
                  <div
                    v-for="worker in workers"
                    :key="worker.id"
                    class="worker-option"
                    :class="{
                      selected: form.tenant_worker_ids.includes(worker.id),
                      disabled: isSubmitting,
                    }"
                  >
                    <Checkbox
                      :id="`np-worker-${worker.id}`"
                      :model-value="form.tenant_worker_ids.includes(worker.id)"
                      :disabled="isSubmitting"
                      :aria-label="`选择 Worker ${worker.display_name}`"
                      @update:model-value="setWorkerSelection(worker.id, $event)"
                    />
                    <label class="worker-option-content" :for="`np-worker-${worker.id}`">
                      <span class="worker-option-name">{{ worker.display_name }}</span>
                      <span class="worker-option-meta"
                        >{{ worker.engine }} · {{ workerLabel(worker) }}</span
                      >
                      <span class="worker-option-tasks">{{ worker.task_types.join('/') }}</span>
                    </label>
                  </div>
                </div>
              </template>
            </FormField>

            <div v-if="schedule?.project" class="advanced-subsection">
              <div class="section-title-row compact-title-row">
                <div>
                  <h3>既有期限</h3>
                  <p class="section-desc">未修改时保留原期限；清空输入才明确取消，仅影响未来项目。日期按浏览器本地时区显示。</p>
                </div>
              </div>
              <div class="execution-settings-grid">
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
        <section
          v-show="settingsPanel === 'timing'"
          id="np-timing-settings"
          class="settings-panel"
          aria-label="时间设置"
        >
          <div class="settings-panel-heading">
            <h3>时间设置</h3>
            <Button size="sm" variant="ghost" @click="closeSettings">完成设置</Button>
          </div>
          <ScheduleTiming
            ref="scheduleTiming"
            v-model="scheduleDraft"
            :show-mode="false"
            :locked-mode="Boolean(schedule)"
            :editing="Boolean(schedule)"
            :boundary-hint="boundaryHint"
            :project="previewProject"
          />
        </section>
      </div>
    </fieldset>

    <template #footer>
      <div class="creation-footer">
        <div
          class="creation-settings"
          v-if="!createdProjectId && !createdScheduleId && !creationDeleted"
        >
          <Button
            id="np-execution-trigger"
            class="execution-trigger"
            variant="ghost"
            :disabled="formDisabled"
            :aria-expanded="settingsPanel === 'execution'"
            aria-controls="np-execution-settings"
            @click="toggleSettings('execution')"
          >
            <template #icon><SlidersHorizontal :size="16" /></template>
            <span class="setting-summary"
              ><span>{{ workerSummary }}</span
              ><span class="summary-hint">{{ executionSummary }}</span></span
            >
          </Button>
          <div class="start-settings">
            <Select
              id="schedule-mode"
              :model-value="scheduleDraft.mode"
              :options="modeOptions"
              :disabled="formDisabled || Boolean(schedule && scheduleDraft.mode === 'once')"
              aria-label="启动安排"
              @update:model-value="setScheduleMode"
            />
            <Button
              v-if="scheduleDraft.mode !== 'immediate'"
              id="np-timing-trigger"
              class="timing-trigger"
              variant="ghost"
              :disabled="formDisabled"
              :aria-expanded="settingsPanel === 'timing'"
              aria-controls="np-timing-settings"
              @click="toggleSettings('timing')"
              ><span class="setting-summary"
                ><span>{{ timingSummary }}</span
                ><span class="summary-hint"
                  >{{ scheduleDraft.timezone || '未设置时区' }} · 编辑时间</span
                ></span
              ></Button
            >
          </div>
        </div>
        <p v-if="creationUnknown" class="section-desc">
          保存结果未确认，仅能查询或按原请求恢复；不要换键重复创建。
        </p>
        <p v-else class="section-desc">
          {{
            submitBlockReason ||
            (scheduleDraft.mode === 'immediate'
              ? '资料绑定成功后进入执行调度，不代表模型已启动。'
              : schedule
                ? '保存仅影响未来项目，不会重写已生成项目。'
                : '保存计划后由后台在计划时刻处理，不会立即启动研究。')
          }}
        </p>
        <Alert v-if="formError && !creationUnknown" tone="danger" title="请检查当前操作">{{
          formError
        }}</Alert>
        <div class="creation-footer-actions">
          <Button
            variant="secondary"
            :disabled="isSubmitting || resourceBusy || isCancelling || isSavingDefault"
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
      description="仅关联本次项目，不会删除资料库原件。创建时冻结当前版本。"
      @update:open="libraryPickerOpen = $event"
    >
      <Input v-model="librarySearch" placeholder="搜索资料库" aria-label="搜索资料库" />
      <Skeleton v-if="libraryCatalogLoading" />
      <ErrorState
        v-else-if="libraryCatalogError"
        title="资料库目录加载失败"
        description="可重试；资料库为可选项。"
        @retry="refetchLibraryCatalog()"
      />
      <div v-else-if="filteredLibraries.length" class="library-options">
        <button
          v-for="library in filteredLibraries"
          :key="library.key"
          type="button"
          class="library-option"
          :class="{ selected: form.library_keys.includes(library.key) }"
          :aria-pressed="form.library_keys.includes(library.key)"
          @click="toggleLibrary(library.key)"
        >
          <span class="library-option-title">{{ library.name }}</span>
          <span class="mono"
            >{{ library.key }} · v{{ library.latest_version }} · {{ library.file_count }} 文件</span
          >
        </button>
      </div>
      <EmptyState v-else title="暂无匹配资料库" description="可不选择资料库创建项目。" />
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
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { isNavigationFailure, onBeforeRouteLeave, useRouter } from 'vue-router'
import {
  api,
  ApiError,
  createProject,
  createWorkerTemplate,
  updateWorkerTemplate,
  listLibraryCatalog,
  listProjectGroups,
  listTenantWorkerOptions,
  listWorkerTemplates,
} from '@/api/client'
import type {
  AdvisoryLibrary,
  CreateProjectRequest,
  ProjectGroup,
  ProjectResourceBatch,
  ScenarioCatalogEntry,
  TenantWorker,
  WorkerTemplate,
} from '@/api/types'
import { useUIStore } from '@/stores/ui'
import { ConfirmAction, ErrorState } from '@/components/patterns'
import {
  Alert,
  Badge,
  Button,
  Dialog,
  Checkbox,
  EmptyState,
  FormField,
  Input,
  Select,
  Skeleton,
  Textarea,
  type SelectOption,
} from '@/components/ui'
import ProjectResourceBatchUploader from './ProjectResourceBatchUploader.vue'
import { applyScenarioTemplates, type ScenarioTemplateState } from './newProjectScenarioTemplates'
import ScheduleTiming from './ScheduleTiming.vue'
import {
  draftFromSchedule,
  newScheduleDraft,
  scheduleModeOptions,
  scheduleTimesFromDraft,
  ScheduleTimeError,
  type ScheduleDraft,
} from './scheduleTiming'
import { weekdayLabels } from '@/api/projectSchedules'
import { ChevronDown, Paperclip, SlidersHorizontal } from '@lucide/vue'
import { bytes } from '@/lib/format'
import {
  createCreationRequestId,
  createProjectSchedule,
  updateProjectSchedule,
  getCreationRequest,
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
const queryClient = useQueryClient()
const isSavingDefault = ref(false)
const defaultSaveError = ref('')
const defaultSaveMessage = ref('')

const userEdited = ref(false)
const isCancelling = ref(false)
const discardOpen = ref(false)
const cleanupError = ref('')
const formError = ref('')
const creationUnknown = ref(false)
const creationDeleted = ref(false)
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
  tenant_worker_ids: [] as string[],
  group_id: '',
  library_keys: [] as string[],
  explore_deadline: '',
  authorization_expires_at: '',
})
const workersInitialized = ref(Boolean(props.schedule))
const scheduleDraft = ref(props.schedule ? draftFromSchedule(props.schedule) : newScheduleDraft())
const retainedResourceIds = ref(props.schedule?.resources.map((resource) => resource.id) || [])
const refreshLibraryKeys = ref<string[]>([])
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
const scenarioExpanded = ref(true)
const scheduleTiming = ref<InstanceType<typeof ScheduleTiming> | null>(null)
const materialsDetails = ref<HTMLDetailsElement | null>(null)
const resourceSummary = ref('本地文件 0')
const settingsPanel = ref<'execution' | 'timing' | null>(null)
const formDisabled = computed(
  () =>
    isSavingDefault.value ||
    isSubmitting.value ||
    isCancelling.value ||
    creationUnknown.value ||
    Boolean(createdProjectId.value || createdScheduleId.value || creationDeleted.value),
)
const modeOptions = computed(() =>
  scheduleModeOptions(scheduleDraft.value.mode, Boolean(props.schedule)),
)
const executionSummary = computed(
  () =>
    [
      props.schedule?.project && form.explore_deadline ? `停止 ${form.explore_deadline.replace('T', ' ')}` : '',
      props.schedule?.project && form.authorization_expires_at
        ? `授权到期 ${form.authorization_expires_at.replace('T', ' ')}`
        : '',
    ]
      .filter(Boolean)
      .join(' · ') || '执行设置 · 未设时间边界',
)
const timingSummary = computed(() => {
  const draft = scheduleDraft.value
  const start = draft.startLocal.replace('T', ' ')
  if (draft.mode === 'once') return `${start || '未设置启动时间'} · ${draft.stopMode === 'specified' ? `停止 ${draft.stopLocal.replace('T', ' ') || '未设置'}` : '不限制停止时间'}`
  const days =
    draft.mode === 'weekly'
      ? [...draft.weekdays]
          .sort()
          .map((day) => weekdayLabels[day])
          .join('、') || '未选择执行日'
      : '每天'
  return `${days} ${draft.localTime || '未设置时刻'} · ${start ? `起始 ${start}` : '未设置起始边界'}${draft.endDate ? ` · 结束 ${draft.endDate}` : ''}${draft.maxRuns ? ` · 最多 ${draft.maxRuns} 次` : ''}`
})
async function showSettings(panel: 'execution' | 'timing', anchor?: string) {
  settingsPanel.value = panel
  await nextTick()
  document.getElementById(anchor || `np-${panel}-settings`)?.scrollIntoView({ block: 'start' })
}
function closeSettings() {
  const panel = settingsPanel.value
  settingsPanel.value = null
  if (panel) document.getElementById(`np-${panel}-trigger`)?.focus()
}
function toggleSettings(panel: 'execution' | 'timing') {
  if (settingsPanel.value === panel) closeSettings()
  else void showSettings(panel)
}
function setScheduleMode(value: string | number) {
  scheduleDraft.value.mode = value as ScheduleDraft['mode']
  if (value === 'immediate') closeSettings()
  else void showSettings('timing')
}
function closeSettingsOnEscape(event: KeyboardEvent) {
  if (!settingsPanel.value) return
  event.preventDefault()
  closeSettings()
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
    tenant_worker_ids: [...project.tenant_worker_ids],
    group_id: project.group_id,
    library_keys: props.schedule.library_bindings.map((binding) => binding.library_key),
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
function toggleLibraryRefresh(key: string) {
  userEdited.value = true
  refreshLibraryKeys.value = refreshLibraryKeys.value.includes(key)
    ? refreshLibraryKeys.value.filter((item) => item !== key)
    : [...refreshLibraryKeys.value, key]
}
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
  group: '',
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
  data: workersData,
  isLoading: workersLoading,
  isFetching: workersFetching,
  isError: workersError,
  refetch: refetchWorkers,
} = useQuery<TenantWorker[]>({
  queryKey: ['tenant-worker-options'],
  queryFn: listTenantWorkerOptions,
})
const allWorkers = computed(() => workersData.value || [])
const workers = computed(() => allWorkers.value.filter((worker) => worker.available))

const {
  data: groupsData,
  isLoading: groupsLoading,
  isError: groupsError,
  refetch: refetchGroups,
} = useQuery<ProjectGroup[]>({
  queryKey: ['project-groups'],
  queryFn: listProjectGroups,
})
const groups = computed(() => groupsData.value || [])
const groupOptions = computed<SelectOption[]>(() =>
  groups.value.map((group) => ({ value: group.id, label: group.name })),
)
const selectedGroupExists = computed(() => groups.value.some((group) => group.id === form.group_id))

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

const {
  data: workerTemplatesData,
  isLoading: workerTemplatesLoading,
  isFetching: workerTemplatesFetching,
  isError: workerTemplatesError,
  refetch: refetchWorkerTemplates,
} = useQuery<WorkerTemplate[]>({
  queryKey: ['worker-templates'],
  queryFn: listWorkerTemplates,
})
const workerTemplates = computed(() => workerTemplatesData.value || [])
const workerTemplateOptions = computed<SelectOption[]>(() => [
  { value: '__none__', label: '本次自定义' },
  ...workerTemplates.value.map((template) => ({
    value: template.id,
    label: `${template.name}${template.is_default ? ' · 默认' : ''} (${template.tenant_worker_ids.length})`,
  })),
])
const selectedTemplateId = ref('__none__')

const filteredLibraries = computed(() =>
  libraryCatalog.value.filter((library) =>
    `${library.name} ${library.key}`.toLowerCase().includes(librarySearch.value.toLowerCase()),
  ),
)
const defaultWorkerTemplate = computed(() => workerTemplates.value.find((item) => item.is_default))
const invalidSelectedWorkers = computed(() =>
  form.tenant_worker_ids.flatMap((id) => {
    const worker = allWorkers.value.find((item) => item.id === id)
    return worker?.available ? [] : [{
      id,
      name: worker?.display_name || id,
      reason: worker?.unavailable_reason || (worker ? '配置不可用，请检查启用状态、Model 或 Key' : 'Worker 已删除或不属于当前 Tenant'),
    }]
  }),
)
const missingWorkerRoles = computed(() => {
  const roles = new Set(workers.value
    .filter((worker) => form.tenant_worker_ids.includes(worker.id))
    .flatMap((worker) => worker.task_types))
  return (['bootstrap', 'reason', 'explore'] as const).filter((role) => !roles.has(role))
})
const workerSelectionIssue = computed(() => {
  if (workersLoading.value) return ''
  if (workersError.value) return 'Worker 查询失败，请重试加载后确认配置。'
  if (!workersInitialized.value && (workersFetching.value || workerTemplatesFetching.value)) return ''
  if (selectedTemplateId.value !== '__none__' && !workerTemplatesLoading.value) {
    if (workerTemplatesError.value) return '无法确认所选模板，请刷新模板核对，或明确选择本次自定义。'
    if (!workerTemplates.value.some((item) => item.id === selectedTemplateId.value))
      return '所选 Worker 模板已删除；原成员仍保留，请恢复默认或明确选择本次自定义。'
  }
  if (invalidSelectedWorkers.value.length)
    return invalidSelectedWorkers.value.map((item) => `${item.name}：${item.reason}`).join('；')
  if (!form.tenant_worker_ids.length) {
    if (workerTemplatesLoading.value) return ''
    if (workerTemplatesError.value) return '默认组合查询失败，请重试加载模板，或选择本次自定义。'
    return defaultWorkerTemplate.value
      ? '尚未选择 Worker，请主动恢复默认组合或选择本次自定义。'
      : '尚未设置默认组合，请选择 Worker 后保存为默认组合，或仅用于本次。'
  }
  if (missingWorkerRoles.value.length) return `执行组合缺少角色：${missingWorkerRoles.value.join('、')}。请补充支持这些角色的 Worker。`
  return ''
})
const executionNeedsAttention = computed(() => Boolean(workerSelectionIssue.value))
const workerSummary = computed(() => {
  if (workersLoading.value || (!workersInitialized.value && (workersFetching.value || workerTemplatesFetching.value)))
    return '正在加载执行组合'
  if (workerSelectionIssue.value) return workerSelectionIssue.value
  const template = workerTemplates.value.find((item) => item.id === selectedTemplateId.value)
  const names = form.tenant_worker_ids.map((id) => allWorkers.value.find((item) => item.id === id)?.display_name || id)
  return `${template?.is_default ? '默认组合' : '本次组合'} · ${names.join('、')}`
})
const canSaveDefault = computed(
  () =>
    !formDisabled.value &&
    !workersError.value &&
    !workersFetching.value &&
    !workerTemplatesFetching.value &&
    !workerTemplatesError.value &&
    form.tenant_worker_ids.length > 0 &&
    invalidSelectedWorkers.value.length === 0 &&
    missingWorkerRoles.value.length === 0,
)

const submitBlockReason = computed(() => {
  if (creationDeleted.value) return '原请求已保存但项目已删除，不能用此请求重新创建。'
  if (resourceBusy.value) return '资料操作尚未完成，请稍候。'
  if (resourcePending.value) return '仍有未完成或结果未确认的资料，请处理或明确移除后再创建。'
  if (scenariosLoading.value || workersLoading.value || groupsLoading.value)
    return '正在加载创建所需配置。'
  if (scenariosError.value || !selectedScenarioExists.value)
    return '请选择可用场景，或重试加载场景目录。'
  if (!workersInitialized.value && (workersFetching.value || workerTemplatesFetching.value || workerTemplatesLoading.value))
    return '正在加载默认执行组合。'
  if (workerSelectionIssue.value) return workerSelectionIssue.value
  if (!form.tenant_worker_ids.length) return '请展开执行设置，配置本次 Worker。'
  if (groupsError.value || !selectedGroupExists.value) return '请选择有效项目组，或重试加载项目组。'
  return ''
})
const canSubmitProject = computed(
  () =>
    !isSavingDefault.value &&
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

watch(
  [workersFetching, workerTemplatesFetching, workersLoading, workerTemplatesLoading, workersError, workerTemplatesError, workerTemplates, allWorkers, formDisabled],
  () => {
    if (workersInitialized.value || formDisabled.value || workersLoading.value || workerTemplatesLoading.value ||
        workersFetching.value || workerTemplatesFetching.value || workersError.value || workerTemplatesError.value) return
    // 手动资源选择和旧计划已标记初始化；正文编辑不阻止首次默认应用，刷新不覆盖选择。
    workersInitialized.value = true
    const template = defaultWorkerTemplate.value
    if (!template) return
    selectedTemplateId.value = template.id
    form.tenant_worker_ids = [...template.tenant_worker_ids]
  },
  { immediate: true },
)

// 查询加载标志与默认选择可能先后更新，等本轮状态稳定后再显露真正缺资源的错误。
watch(
  executionNeedsAttention,
  async (needsAttention) => {
    if (!needsAttention || formDisabled.value || !props.open) return
    await nextTick()
    if (executionNeedsAttention.value && !formDisabled.value && props.open) void showSettings('execution')
  },
  { immediate: true, flush: 'post' },
)

watch(
  groups,
  () => {
    // 项目组被删除或 Tenant 上下文切换后，不保留已经失效的创建选择。
    if (!props.schedule && form.group_id && !selectedGroupExists.value) form.group_id = ''
    if (selectedGroupExists.value) validationErrors.group = ''
  },
  { immediate: true },
)

watch(
  () => form.group_id,
  () => {
    if (selectedGroupExists.value) validationErrors.group = ''
  },
)

const setWorkerSelection = (workerId: string, selected: boolean) => {
  userEdited.value = true
  workersInitialized.value = true
  selectedTemplateId.value = '__none__'
  defaultSaveMessage.value = ''
  const index = form.tenant_worker_ids.indexOf(workerId)
  if (selected && index < 0) form.tenant_worker_ids.push(workerId)
  if (!selected && index >= 0) form.tenant_worker_ids.splice(index, 1)
}

const workerLabel = (worker: TenantWorker) => worker.model_display_name || worker.model_id

const applyWorkerTemplate = () => {
  userEdited.value = true
  workersInitialized.value = true
  defaultSaveMessage.value = ''
  if (selectedTemplateId.value === '__none__') return
  const template = workerTemplates.value.find((item) => item.id === selectedTemplateId.value)
  if (template) form.tenant_worker_ids = [...template.tenant_worker_ids]
}
function restoreDefaultWorkers() {
  if (!defaultWorkerTemplate.value || formDisabled.value || workerTemplatesError.value) return
  selectedTemplateId.value = defaultWorkerTemplate.value.id
  applyWorkerTemplate()
}
async function saveDefaultWorkers() {
  if (!canSaveDefault.value) return
  isSavingDefault.value = true
  defaultSaveError.value = ''
  defaultSaveMessage.value = ''
  // 选定模板则设其为默认；手工组合复用已有默认，没有默认时才创建模板。
  const template = workerTemplates.value.find((item) => item.id === selectedTemplateId.value) || defaultWorkerTemplate.value
  const input = { tenant_worker_ids: [...form.tenant_worker_ids], is_default: true }
  try {
    const saved = template
      ? await updateWorkerTemplate(template.id, input)
      : await createWorkerTemplate({ ...input, name: '默认 Worker 组合' })
    queryClient.setQueryData<WorkerTemplate[]>(['worker-templates'], (current = []) => [
      ...current.filter((item) => item.id !== saved.id).map((item) => ({ ...item, is_default: false })),
      saved,
    ])
    selectedTemplateId.value = saved.id
    workersInitialized.value = true
    defaultSaveMessage.value = '以后新建自动采用此组合；已有项目和计划不变。'
    await queryClient.invalidateQueries({ queryKey: ['worker-templates'] })
  } catch (error) {
    defaultSaveError.value = error instanceof Error ? error.message : '未收到可确认的保存结果'
    if (!(error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408))
      defaultSaveError.value += '。结果未确认，请刷新模板核对后再决定是否保存，不要盲目重复创建。'
  } finally {
    isSavingDefault.value = false
  }
}

const toggleLibrary = (key: string) => {
  userEdited.value = true
  const index = form.library_keys.indexOf(key)
  if (index >= 0) form.library_keys.splice(index, 1)
  else form.library_keys.push(key)
}

function retainedDeadline(value: string, original?: string | null) {
  if (!value) return null
  return original && value === inputLocalTime(original) ? original : new Date(value).toISOString()
}
function effectiveDeadlines() {
  if (props.schedule?.project) {
    return {
      explore_deadline: retainedDeadline(form.explore_deadline, props.schedule.project.explore_deadline),
      authorization_expires_at: retainedDeadline(form.authorization_expires_at, props.schedule.project.authorization_expires_at),
    }
  }
  return {
    explore_deadline: scheduleDraft.value.mode === 'once'
      ? scheduleTimesFromDraft(scheduleDraft.value).deadline
      : null,
    authorization_expires_at: null,
  }
}
const previewProject = computed<CreateProjectRequest | undefined>(() => {
  if (
    !form.title.trim() ||
    !form.origin.trim() ||
    !form.goal.trim() ||
    !form.group_id ||
    !form.scenario ||
    !form.tenant_worker_ids.length
  )
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
    group_id: form.group_id,
    scenario: form.scenario,
    tenant_worker_ids: [...form.tenant_worker_ids],
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
    ? validateFutureDate(form.explore_deadline, '执行停止时间') : ''
  validationErrors.authorizationExpiresAt = props.schedule?.project
    ? validateFutureDate(form.authorization_expires_at, '授权到期时间') : ''
  if (validationErrors.exploreDeadline || validationErrors.authorizationExpiresAt) {
    valid = false
    firstErrorAnchor = firstErrorAnchor || 'np-execution-settings'
  }

  return { valid, firstErrorAnchor }
}

const submitProject = async () => {
  if (
    isSavingDefault.value ||
    isSubmitting.value ||
    isCancelling.value ||
    creationUnknown.value ||
    creationDeleted.value ||
    createdProjectId.value ||
    createdScheduleId.value ||
    !props.open ||
    libraryPickerOpen.value ||
    discardOpen.value
  )
    return
  formError.value = ''
  if (resourcePending.value) {
    formError.value = '仍有未完成的资料，请补传、核对或明确移除后再创建。'
    if (materialsDetails.value) {
      materialsDetails.value.open = true
      materialsDetails.value.scrollIntoView({ block: 'start' })
    }
    return
  }
  if (!selectedScenarioExists.value) {
    uiStore.showToast(
      scenariosLoading.value ? '场景目录仍在加载，请稍后重试' : '请选择有效的探索场景',
      'danger',
    )
    return
  }
  if (workersLoading.value || workersError.value) {
    uiStore.showToast(
      workersLoading.value ? 'Worker 列表仍在加载，请稍后重试' : '无法确认可用 Worker，请重试加载',
      'danger',
    )
    return
  }
  if (workerSelectionIssue.value || !form.tenant_worker_ids.length ||
      (!workersInitialized.value && (workersFetching.value || workerTemplatesFetching.value))) {
    formError.value = workerSelectionIssue.value || '请等待默认查询完成，或明确选择本次 Worker。'
    await showSettings('execution', 'np-workers')
    return
  }
  if (groupsLoading.value || groupsError.value) {
    uiStore.showToast(
      groupsLoading.value ? '项目组列表仍在加载，请稍后重试' : '无法确认可用项目组，请重试加载',
      'danger',
    )
    return
  }
  if (!selectedGroupExists.value) {
    validationErrors.group = '请选择有效的项目组'
    uiStore.showToast('请选择有效的项目组', 'danger')
    document.getElementById('np-group')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    return
  }

  if (resourceBusy.value) {
    uiStore.showToast('辅助资料仍在上传，请稍后重试', 'danger')
    return
  }

  try {
    const { valid, firstErrorAnchor } = validateForm()
    const timing = scheduleDraft.value.mode !== 'immediate' ? scheduleTiming.value!.validate() : null
    if (!valid || (scheduleDraft.value.mode !== 'immediate' && !timing)) {
      uiStore.showToast('请检查表单必填项和时间安排', 'danger')
      // 只展开首个错误所在区域，最终滚动在同一处完成，避免后续时间错误抢走正文定位。
      const anchor = firstErrorAnchor || 'np-timing-settings'
      if (anchor === 'np-execution-settings') settingsPanel.value = 'execution'
      else if (anchor === 'np-timing-settings') settingsPanel.value = 'timing'
      await nextTick()
      document.getElementById(anchor)?.scrollIntoView({ block: 'start' })
      return
    }

    const rules = timing?.rules
    const {
      request_id: _requestId,
      resource_batch_id: _batchId,
      resources: _resources,
      ...retainedInput
    } = props.schedule?.project || ({} as Partial<CreateProjectRequest>)
    // 旧计划原显式 targets（包括空数组）随原输入保留，新建不补数组或重解释来源。
    const payload: CreateProjectRequest = {
      ...retainedInput,
      title: form.title.trim(),
      origin: form.origin,
      goal: form.goal,
      scenario: form.scenario,
      tenant_worker_ids: [...form.tenant_worker_ids],
      group_id: form.group_id,
      ...effectiveDeadlines(),
      ...(resourceBatch.value ? { resource_batch_id: resourceBatch.value.id } : {}),
      library_keys: [...form.library_keys],
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
          refresh_library_keys: refreshLibraryKeys.value.filter((key) => form.library_keys.includes(key)),
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
        savedSchedule = submission.operation === 'update'
          ? await updateProjectSchedule(submission.scheduleId, submission.payload)
          : await createProjectSchedule(submission.payload)
        createdScheduleId.value = savedSchedule.id
      }
    } catch (error) {
      // 恢复请求的拒绝不证明最初请求未提交，仍保留原键和原内容供查询。
      const definiteRejection =
        error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408
      formError.value = error instanceof Error ? error.message : '未收到可确认的保存结果'
      if (definiteRejection && !creationUnknown.value) {
        if (submission.operation !== 'update') {
          const cache = forgetCreationRequest(submission.payload.request_id)
          if (cache.warning) cacheWarning.value = cache.warning
        }
        pendingSubmission.value = null
      } else creationUnknown.value = true
      return
    }
    creationUnknown.value = false
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
  // 未知结果恢复只使用内存中的原始 payload，绝不读取修改后的表单或换请求 ID。
  await sendPendingSubmission()
}
async function queryCreationResult() {
  const submission = pendingSubmission.value
  if (!submission || submission.operation === 'update' || isSubmitting.value) return
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
      if (creationDeleted.value) pendingSubmission.value = null
      else await enterCreatedProject()
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
  if (open || resourceBusy.value || isSubmitting.value || isCancelling.value || isSavingDefault.value) return
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
    isSavingDefault.value ||
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
const restoreFocus = (event: Event) => {
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
    isSavingDefault.value ||
    resourceBusy.value ||
    isSubmitting.value ||
    isCancelling.value ||
    creationUnknown.value ||
    userEdited.value ||
    resourceBatch.value ||
    resourcePending.value
  ) {
    emit('update:open', true)
    if (!creationUnknown.value && !resourceBusy.value && !isSubmitting.value && !isCancelling.value && !isSavingDefault.value)
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
    (isSavingDefault.value ||
      isSubmitting.value ||
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
  window.removeEventListener('keydown', handleKeydown)
  window.removeEventListener('beforeunload', handleBeforeUnload)
})
</script>

<style src="./NewProject.css" scoped></style>

<style scoped>
.new-project-form {
  min-width: 0;
  margin: var(--space-4) 0 0;
  padding: 0;
  border: 0;
}

.creation-content {
  display: grid;
  gap: var(--space-4);
  min-width: 0;
}
.project-identity {
  display: grid;
  grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
  gap: var(--space-3);
}
.scenario-settings,
.project-materials {
  min-width: 0;
}
.scenario-settings summary,
.project-materials summary {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding-block: var(--space-2);
  cursor: pointer;
  list-style: none;
  font-size: var(--text-md);
}
.scenario-settings summary::-webkit-details-marker,
.project-materials summary::-webkit-details-marker {
  display: none;
}
.scenario-settings summary:focus-visible,
.project-materials summary:focus-visible,
.scenario-card:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.scenario-name {
  font-weight: var(--font-semibold);
}
.summary-action {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  flex-shrink: 0;
  color: var(--text-muted);
  font-size: var(--text-sm);
}
[open] > summary .summary-action svg {
  transform: rotate(180deg);
}
.scenarios-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.scenario-card {
  width: auto;
  min-width: 0;
}
.project-materials {
  border-top: 1px solid var(--border);
}
.materials-summary {
  min-width: 0;
  overflow-wrap: anywhere;
}
.materials-content {
  display: grid;
  gap: var(--space-3);
  padding-top: var(--space-2);
}
.settings-panel {
  min-width: 0;
  padding-top: var(--space-3);
  border-top: 1px solid var(--border);
  scroll-margin-top: var(--space-3);
}
.settings-panel-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.settings-panel-heading h3 {
  margin: 0;
  font-size: var(--text-md);
}
.creation-settings {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: var(--space-3);
}
.execution-trigger,
.timing-trigger {
  justify-content: flex-start;
  text-align: left;
  padding-block: var(--space-2);
}
.setting-summary {
  display: grid;
  gap: var(--space-1);
  white-space: normal;
  overflow-wrap: anywhere;
}
.execution-trigger .setting-summary > span:first-child {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.start-settings {
  display: grid;
  min-width: 0;
}
.creation-footer :deep(.ui-alert) {
  max-height: 140px;
  overflow-y: auto;
}
@media (max-width: 700px) {
  .project-identity,
  .creation-settings,
  .scenarios-grid {
    grid-template-columns: 1fr;
  }
  .scenario-settings summary {
    flex-wrap: wrap;
  }
  .selected-library {
    flex-wrap: wrap;
  }
}

.project-materials h3 {
  margin: 0;
  font-size: var(--text-md);
}

.project-materials > .section-desc {
  margin-bottom: var(--space-3);
}

.selected-libraries {
  display: grid;
  gap: var(--space-2);
  margin-top: var(--space-3);
}

.selected-library {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  overflow-wrap: anywhere;
}

.creation-footer {
  width: 100%;
  display: grid;
  gap: var(--space-3);
}

.creation-footer-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--space-2);
}

.new-project-header {
  max-width: 860px;
  margin-right: auto;
  margin-left: auto;
}

.new-project-workflow {
  width: min(100%, 860px);
  margin: 0 auto;
}

.form-section {
  padding: var(--space-5);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background-color: var(--bg-surface);
  box-shadow: var(--shadow-sm);
}

.section-title-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-4);
}

.section-title-row h2,
.section-title-row h3,
.resource-title-row h3 {
  margin: 0;
  color: var(--text);
  font-size: 15px;
}

.section-desc {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: 12px;
}

.validation-anchor {
  scroll-margin-top: 16px;
}

.field-hint {
  display: block;
  margin-top: var(--space-1);
  color: var(--text-faint);
  font-size: 11px;
}

.required-mark {
  color: var(--danger);
}

.api-config-group {
  margin-top: var(--space-5);
}

.worker-options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.worker-option {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text);
  cursor: pointer;
  transition:
    border-color var(--dur-fast),
    background var(--dur-fast);
}

.worker-option:hover {
  border-color: var(--border-strong);
}

.worker-option.selected {
  border-color: var(--accent);
  background: var(--accent-dim);
}

.worker-option :deep(.ui-checkbox) {
  position: relative;
  z-index: 1;
}

.worker-option.disabled,
.worker-option.disabled .worker-option-content {
  cursor: not-allowed;
}

.worker-option.disabled {
  opacity: 0.7;
}

.worker-option-content {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  cursor: pointer;
}

.worker-option-content::before {
  position: absolute;
  inset: 0;
  content: '';
}

.worker-option-name {
  font-size: 12px;
  font-weight: 600;
}

.worker-option-meta {
  color: var(--text-muted);
  font-size: 11px;
}

.worker-option-tasks {
  color: var(--text-faint);
  font-size: 10px;
}

.worker-loading,
.library-loading {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-2);
}

.worker-skeleton,
.library-skeleton,
.select-skeleton {
  min-height: var(--control-height-lg);
}

.template-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 12px 0;
}

.template-label {
  color: var(--text-muted);
  font-size: 12px;
}

.template-select {
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text);
  font-size: 12px;
}

.summary-hint {
  color: var(--text-faint);
  font-size: 11px;
  font-weight: 400;
}

.advanced-settings-content {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
  padding: 0 var(--space-3) var(--space-3);
}

.advanced-subsection {
  padding-top: var(--space-4);
  border-top: 1px solid var(--border);
}

.compact-title-row {
  margin-bottom: var(--space-3);
}

.execution-settings-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
}

.optional-badge {
  margin-left: 6px;
  padding: 1px 6px;
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  background: var(--accent-dim);
  color: var(--accent);
  font-size: 10px;
  font-weight: 600;
  vertical-align: middle;
}

.library-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-2);
}

.library-option {
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text-muted);
  cursor: pointer;
  text-align: left;
}

.library-option:hover {
  border-color: var(--border-strong);
}

.library-option.selected {
  border-color: var(--accent);
  background: var(--accent-dim);
}

.library-option-title {
  color: var(--text);
  font-weight: 700;
}

.resource-title-row {
  display: flex;
  align-items: start;
  justify-content: space-between;
  gap: var(--space-3);
}

.resource-title-row > div:first-child {
  flex: 1;
}

.resource-file-input {
  display: none;
}

.resource-guidance {
  margin-bottom: var(--space-3);
}

.resource-editor-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.resource-editor-row {
  display: grid;
  grid-template-columns: 90px minmax(0, 1fr) 72px 28px;
  align-items: center;
  gap: var(--space-2);
}

.resource-size {
  color: var(--text-faint);
  font-size: 11px;
  text-align: right;
}

.form-bottom-bar {
  display: flex;
  flex-direction: row;
  justify-content: flex-end;
  gap: var(--space-3);
  margin-top: var(--space-4);
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background-color: var(--bg-surface);
}

.btn-link-action {
  border: none;
  background: transparent;
  color: var(--accent);
  cursor: pointer;
  font-size: 12px;
}

@media (max-width: 700px) {
  .form-section {
    padding: var(--space-4);
  }

  .execution-settings-grid,
  .library-options {
    grid-template-columns: 1fr;
  }

  .resource-editor-row {
    grid-template-columns: 80px minmax(0, 1fr) 28px;
  }

  .resource-size {
    display: none;
  }
}
</style>
