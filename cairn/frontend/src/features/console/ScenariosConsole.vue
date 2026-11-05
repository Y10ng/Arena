<template>
  <article class="console-page scenario-console">
    <header class="resource-page-heading console-resource-heading">
      <h1 class="sr-only">探索场景</h1>
      <Button
        :disabled="isScenarioMutationPending || isLoadingScenarios || isScenariosError"
        @click="rememberScenarioEditorTrigger($event); addNewScenario()"
      >
        <template #icon><Plus :size="16" /></template>
        新建场景
      </Button>
    </header>

    <Alert v-if="isLoadingScenarios" tone="info"
      ><Spinner size="sm" decorative />正在加载场景...</Alert
    >
    <Alert v-if="isScenariosError" tone="danger" title="加载场景失败">{{
      formatError(scenariosError)
    }}</Alert>

    <div v-if="scenarios.length" class="scenario-workspace">
      <nav v-if="!isReading" class="scenario-directory" aria-label="探索场景">
        <div class="scenario-list-heading" aria-hidden="true">
          <span>名称 / ID</span><span class="scenario-list-meta">探索提示词</span><span class="scenario-list-meta">执行画像</span><span />
        </div>
        <button
          v-for="scenario in scenarios"
          :key="scenario.key"
          :data-scenario-key="scenario.key"
          type="button"
          class="scenario-list-item"
          :class="{ active: selectedScenarioKey === scenario.key }"
          :aria-pressed="selectedScenarioKey === scenario.key"
          @click="openScenario(scenario.key, $event)"
        >
          <span class="scenario-list-identity">
            <span class="sc-title" :title="scenario.label">{{ scenario.label }}</span>
            <Badge v-if="scenario.is_default" tone="neutral">默认</Badge>
            <span class="sc-key mono text-muted" :title="scenario.key">{{ scenario.key }}</span>
          </span>
          <span class="scenario-list-meta mono" :title="scenario.prompt_group"><span class="sr-only">探索提示词：</span>{{ scenario.prompt_group }}</span>
          <span class="scenario-list-meta" :title="scenario.profile_id"><span class="sr-only">执行画像：</span>{{ scenario.profile_id }}</span>
          <ChevronRight :size="16" />
        </button>
      </nav>
      <section
        v-if="isReading && selectedScenario"
        class="scenario-reader"
        aria-labelledby="scenario-title"
      >
        <header class="scenario-reader-header">
          <Button variant="ghost" size="sm" class="scenario-back" @click="returnToScenarios"
            ><template #icon><ArrowLeft :size="16" /></template>返回场景目录</Button
          >
          <h2 id="scenario-title" ref="readerTitle" tabindex="-1">{{ selectedScenario.label }}</h2>
          <Button
            variant="secondary"
            :disabled="isScenarioMutationPending"
            @click="rememberScenarioEditorTrigger($event); editSelectedScenario()"
          >
            <template #icon><Pencil :size="16" /></template>
            编辑场景
          </Button>
        </header>
        <div class="scenario-runtime-summary">
          <dl>
            <div>
              <dt>探索提示词</dt>
              <dd>{{ selectedScenario.prompt_group }}</dd>
            </div>
            <div>
              <dt>执行画像</dt>
              <dd>
                {{ selectedProfile ? profileLabel(selectedProfile) : selectedScenario.profile_id }}
              </dd>
            </div>
          </dl>
          <p v-if="selectedProfile" class="scenario-profile-summary">
            {{ profileSummary(selectedProfile) }}
          </p>
          <template v-if="!isScenarioEditorOpen">
            <Alert v-if="isScenarioOptionsError" tone="danger"
              >加载执行选项失败：{{ formatError(scenarioOptionsError) }}</Alert
            >
            <Alert v-else-if="!selectedScenarioProfileValid" tone="danger"
              >当前场景引用的执行环境画像不存在。</Alert
            >
            <Alert v-if="isPromptGroupsError" tone="danger"
              >加载探索提示词失败：{{ formatError(promptGroupsError) }}</Alert
            >
            <Alert v-else-if="!selectedScenarioPromptGroupValid" tone="danger"
              >当前场景的探索提示词组不存在或不可用。</Alert
            >
          </template>
        </div>
        <section class="scenario-default-content" aria-labelledby="scenario-origin-title">
          <h4 id="scenario-origin-title">Origin 默认内容</h4>
          <div v-if="selectedScenario.origin_template" class="scenario-template-content">
            {{ selectedScenario.origin_template }}
          </div>
          <p v-else class="text-muted">未设置</p>
        </section>
        <section class="scenario-default-content" aria-labelledby="scenario-goal-title">
          <h4 id="scenario-goal-title">Goal 默认内容</h4>
          <div v-if="selectedScenario.goal_template" class="scenario-template-content">
            {{ selectedScenario.goal_template }}
          </div>
          <p v-else class="text-muted">未设置</p>
        </section>
        <footer class="scenario-reader-footer">
          <Button
            variant="danger"
            :disabled="!canDeleteSelectedScenario"
            @click="confirmDeleteScenario"
            >删除场景</Button
          >
          <p v-if="selectedScenarioDeleteReason" class="text-muted">
            {{ selectedScenarioDeleteReason }}
          </p>
        </footer>
      </section>
    </div>
    <EmptyState
      v-else-if="!isLoadingScenarios && !isScenariosError"
      title="还没有场景"
    />

    <Dialog
      :open="isScenarioEditorOpen"
      :title="isCreatingScenario ? '新建场景' : '编辑场景'"
      description=""
      size="lg"
      :dismissible="!isSavingScenarios"
      @update:open="handleEditorOpenChange"
      @close-auto-focus="restoreScenarioEditorFocus"
    >
      <div v-if="scenarioDraft" class="scenario-draft-form">
        <div class="scenario-draft-identification">
          <FormField v-slot="{ controlAttrs }" label="场景名称" required>
            <Input
              v-model="scenarioDraft.label"
              v-bind="controlAttrs"
              :disabled="isSavingScenarios"
              placeholder="输入场景名称"
            />
          </FormField>
          <FormField v-slot="{ controlAttrs }" label="场景键值">
            <Input :model-value="scenarioDraft.key" v-bind="controlAttrs" disabled class="mono" />
          </FormField>
        </div>
        <FormField v-slot="{ controlAttrs }" label="Origin 默认内容">
          <Textarea
            :model-value="scenarioDraft.origin_template || ''"
            v-bind="controlAttrs"
            :disabled="isSavingScenarios"
            class="scenario-template-editor"
            :rows="7"
            @update:model-value="scenarioDraft.origin_template = $event"
          />
        </FormField>
        <FormField v-slot="{ controlAttrs }" label="Goal 默认内容">
          <Textarea
            :model-value="scenarioDraft.goal_template || ''"
            v-bind="controlAttrs"
            :disabled="isSavingScenarios"
            class="scenario-template-editor"
            :rows="7"
            @update:model-value="scenarioDraft.goal_template = $event"
          />
        </FormField>
        <Alert v-if="isScenariosError" tone="danger"
          >加载场景列表失败，暂不能保存：{{ formatError(scenariosError) }}</Alert
        >
        <Alert v-if="isLoadingScenarioOptions || isLoadingPromptGroups" tone="info"
          ><Spinner size="sm" decorative />正在加载执行选项...</Alert
        >
        <Alert v-if="isScenarioOptionsError" tone="danger"
          >加载执行选项失败：{{ formatError(scenarioOptionsError) }}</Alert
        >
        <Alert v-if="isPromptGroupsError" tone="danger"
          >加载探索提示词失败：{{ formatError(promptGroupsError) }}</Alert
        >
        <FormField
          v-slot="{ controlAttrs }"
          label="探索提示词组"
          :error="scenarioDraftPromptGroupValid ? '' : '模板组不存在、不可用或不支持探索'"
        >
          <Select
            v-model="scenarioDraft.prompt_group"
            v-bind="controlAttrs"
            :disabled="isSavingScenarios || isLoadingPromptGroups"
            :invalid="!scenarioDraftPromptGroupValid"
            :options="
              scenarioPromptGroupOptions.map((option) => ({
                value: option.value,
                label: `${option.label}${option.exists && !option.valid ? ' · 校验异常' : ''}`,
              }))
            "
          />
          <Button
            v-if="!scenarioDraftPromptGroupValid"
            variant="link"
            size="sm"
            @click="openPromptManagerForScenario"
            >管理探索提示词</Button
          >
        </FormField>
        <FormField
          v-if="profileSelectionReadOnly"
          label="执行画像"
          :error="scenarioDraftProfileValid ? '' : '引用的执行环境画像不存在'"
        >
          <div class="readonly-profile">
            {{
              availableProfileOptions[0]
                ? profileLabel(availableProfileOptions[0])
                : scenarioDraft.profile_id
            }}
          </div>
        </FormField>
        <FormField
          v-else
          v-slot="{ controlAttrs }"
          label="执行画像"
          :error="scenarioDraftProfileValid ? '' : '引用的执行环境画像不存在'"
        >
          <Select
            v-model="scenarioDraft.profile_id"
            v-bind="controlAttrs"
            :disabled="isSavingScenarios"
            :invalid="!scenarioDraftProfileValid"
            :options="
              availableProfileOptions.map((profile) => ({
                value: profile.id,
                label: profileLabel(profile),
              }))
            "
          />
        </FormField>
        <Checkbox
          v-model="scenarioDraft.is_default"
          :disabled="isSavingScenarios"
          label="设为默认场景"
        />
        <Alert v-if="featureError" tone="danger">{{ featureError }}</Alert>
        <Alert v-if="saveError" tone="danger">{{ saveError }}</Alert>
      </div>
      <template #footer>
        <Button variant="secondary" :disabled="isSavingScenarios" @click="closeScenarioEditor"
          >取消</Button
        >
        <Button
          :loading="isSavingScenarios"
          :disabled="!scenarioDraftCanSave"
          @click="saveScenarios"
          >保存场景</Button
        >
      </template>
    </Dialog>

    <ConfirmAction
      :open="discardConfirmationOpen"
      title="放弃未保存的场景修改？"
      description="已保存的场景不受影响；取消可继续编辑当前草稿。"
      confirm-label="放弃修改"
      cancel-label="继续编辑"
      tone="warning"
      @confirm="resolveDiscard(true)"
      @update:open="!$event && resolveDiscard(false)"
    />
    <ConfirmAction
      :open="deleteScenarioKey !== null"
      title="删除场景"
      :description="deleteScenarioDescription"
      confirm-label="确认删除"
      tone="danger"
      :loading="isDeletingScenario"
      @update:open="handleDeleteScenarioOpenChange"
      @confirm="saveDeleteScenario"
    >
      <Alert v-if="deleteError" tone="danger">{{ deleteError }}</Alert>
    </ConfirmAction>
  </article>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArrowLeft, ChevronRight, Pencil, Plus } from '@lucide/vue'
import { onBeforeRouteLeave } from 'vue-router'
import { ConfirmAction } from '@/components/patterns'
import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Dialog,
  EmptyState,
  FormField,
  Input,
  Select,
  Spinner,
  Textarea,
} from '@/components/ui'
import { useScenariosConsole } from './useScenariosConsole'
import './console.css'

const {
  discardConfirmationOpen,
  resolveDiscard,
  addNewScenario,
  availableProfileOptions,
  canDeleteSelectedScenario,
  closeScenarioEditor,
  confirmDeleteScenario,
  confirmDiscardScenarioChanges,
  deleteError,
  deleteScenarioDescription,
  deleteScenarioKey,
  editSelectedScenario,
  featureError,
  formatError,
  handleDeleteScenarioOpenChange,
  handleEditorOpenChange,
  isCreatingScenario,
  isDeletingScenario,
  isLoadingPromptGroups,
  isLoadingScenarioOptions,
  isLoadingScenarios,
  isPromptGroupsError,
  isScenarioEditorOpen,
  isScenarioDirty,
  isScenarioMutationPending,
  isSavingScenarios,
  isScenarioOptionsError,
  isScenariosError,
  openPromptManagerForScenario,
  profileLabel,
  profileSelectionReadOnly,
  profileSummary,
  promptGroupsError,
  saveDeleteScenario,
  saveError,
  saveScenarios,
  scenarioDraft,
  scenarioDraftCanSave,
  scenarioDraftProfileValid,
  scenarioDraftPromptGroupValid,
  scenarioOptionsError,
  scenarioPromptGroupOptions,
  scenarios,
  scenariosError,
  selectScenario,
  selectedProfile,
  selectedScenario,
  selectedScenarioDeleteReason,
  selectedScenarioKey,
  selectedScenarioProfileValid,
  selectedScenarioPromptGroupValid,
} = useScenariosConsole()

const isReading = ref(false)
const readerTitle = ref<HTMLElement | null>(null)
let scenarioEditorTrigger: HTMLElement | null = null
const rememberScenarioEditorTrigger = (event: MouseEvent) => {
  scenarioEditorTrigger = event.currentTarget as HTMLElement
}
const restoreScenarioEditorFocus = (event: Event) => {
  event.preventDefault()
  if (scenarioEditorTrigger?.isConnected) scenarioEditorTrigger.focus()
}
let directoryTrigger: HTMLElement | null = null
const openScenario = async (key: string, event: MouseEvent) => {
  directoryTrigger = event.currentTarget as HTMLElement
  selectScenario(key)
  isReading.value = true
  await nextTick()
  readerTitle.value?.focus()
}
const returnToScenarios = async () => {
  if (!(await confirmDiscardScenarioChanges())) return
  isReading.value = false
  await nextTick()
  if (directoryTrigger?.isConnected) directoryTrigger.focus()
  else
    document
      .querySelector<HTMLElement>(
        `.scenario-list-item[data-scenario-key="${CSS.escape(selectedScenarioKey.value || '')}"]`,
      )
      ?.focus()
}
onBeforeRouteLeave(() => confirmDiscardScenarioChanges())
const protectBrowserLeave = (event: BeforeUnloadEvent) => {
  if (!isScenarioDirty.value && !isScenarioMutationPending.value) return
  event.preventDefault()
  event.returnValue = ''
}
onMounted(() => window.addEventListener('beforeunload', protectBrowserLeave))
onBeforeUnmount(() => window.removeEventListener('beforeunload', protectBrowserLeave))
</script>
