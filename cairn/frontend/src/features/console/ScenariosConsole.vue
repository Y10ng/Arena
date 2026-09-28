<template>
  <article class="console-page">
    <div class="section-header section-header--actions">
      <div class="refresh-indicator" title="实时刷新">
        <span class="live-dot live-success animate-pulse-live"></span>
        <span class="refresh-label text-faint">实时刷新</span>
      </div>
    </div>

    <section class="console-card form-partition">
      <div class="partition-header partition-header--actions">
        <Button
          size="sm"
          :loading="isAddingScenario"
          :disabled="isScenarioMutationPending"
          @click="addNewScenario"
        >
          <template #icon>
            <svg
              aria-hidden="true"
              width="12"
              height="12"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2.5"
            >
              <line x1="12" y1="5" x2="12" y2="19"></line>
              <line x1="5" y1="12" x2="19" y2="12"></line>
            </svg>
          </template>
          {{ isAddingScenario ? '新建中...' : '新建场景' }}
        </Button>
      </div>

      <Alert v-if="isLoadingScenarios" class="state-banner" tone="info">
        <Spinner size="sm" decorative />
        正在加载场景配置...
      </Alert>
      <Alert v-else-if="isScenariosError" class="state-banner" tone="danger" title="加载场景失败">
        {{ formatError(scenariosError) }}
      </Alert>
      <div v-else class="scenarios-manager-grid">
        <div class="scenario-infra-summary" v-if="scenarioOptionsData">
          <div class="infra-item">
            <span class="infra-label">Workers</span>
            <span class="infra-values mono">{{ formatInfraList(infraWorkerNames) }}</span>
          </div>
          <div class="infra-item">
            <span class="infra-label">Prompts</span>
            <span class="infra-values mono">{{
              formatInfraList(scenarioOptionsData.prompt_groups)
            }}</span>
          </div>
          <div class="infra-item">
            <span class="infra-label">Images</span>
            <span class="infra-values mono">{{ formatInfraList(infraImageNames) }}</span>
          </div>
          <div class="infra-item">
            <span class="infra-label">Caps</span>
            <span class="infra-values mono">{{ formatInfraList(infraCapNames) }}</span>
            <span class="infra-source">
              {{
                scenarioOptionsData.cap_add_source === 'declared_inventory'
                  ? 'Declared inventory'
                  : scenarioOptionsData.cap_add_source === 'dispatcher_report'
                    ? 'Dispatcher 上报'
                    : 'Unavailable'
              }}
            </span>
          </div>
        </div>

        <Alert
          v-if="scenarioPreflightMessage"
          class="state-banner preflight-banner"
          :tone="scenarioPreflightTone === 'error' ? 'danger' : 'success'"
        >
          {{ scenarioPreflightMessage }}
        </Alert>

        <div class="scenarios-list-sidebar">
          <button
            v-for="sc in scenarios"
            :key="sc.key"
            type="button"
            class="scenario-list-item"
            :class="{ active: selectedScenarioKey === sc.key }"
            :aria-pressed="selectedScenarioKey === sc.key"
            @click="selectScenario(sc.key)"
          >
            <div class="item-top">
              <span class="sc-title">{{ sc.label }}</span>
              <span class="item-badges">
                <span class="default-badge" v-if="sc.is_default">默认</span>
                <span
                  class="usage-badge"
                  :title="`当前 ${scenarioUsageCount(sc.key)} 个项目使用该场景`"
                >
                  {{ scenarioUsageCount(sc.key) }} 项目
                </span>
              </span>
            </div>
            <span class="sc-key mono text-faint">#{{ sc.key }}</span>
          </button>
        </div>

        <div class="scenario-editor-pane" v-if="selectedScenario">
          <FormField
            v-slot="{ controlAttrs }"
            class="form-group"
            label="场景键值 Key (只读)"
            required
          >
            <Input v-model="selectedScenario.key" v-bind="controlAttrs" disabled class="mono" />
          </FormField>

          <FormField v-slot="{ controlAttrs }" class="form-group" label="方案名称 (Label)" required>
            <Input
              v-model="selectedScenario.label"
              v-bind="controlAttrs"
              placeholder="输入场景方案描述标题"
            />
          </FormField>

          <section
            class="scenario-creation-defaults"
            aria-labelledby="scenario-creation-defaults-title"
          >
            <div class="scenario-creation-defaults-header">
              <h3 id="scenario-creation-defaults-title">新建项目默认内容</h3>
              <p class="field-desc text-faint">
                仅用于新建项目时填充 Origin 与 Goal，不属于运行时 Features。
              </p>
            </div>
            <FormField v-slot="{ controlAttrs }" class="form-group" label="Origin 模板">
              <Textarea
                :model-value="selectedScenario.origin_template || ''"
                v-bind="controlAttrs"
                class="scenario-template-editor"
                :rows="6"
                placeholder="留空表示不提供 Origin 默认内容"
                @update:model-value="selectedScenario.origin_template = $event || null"
              />
            </FormField>
            <FormField v-slot="{ controlAttrs }" class="form-group" label="Goal 模板">
              <Textarea
                :model-value="selectedScenario.goal_template || ''"
                v-bind="controlAttrs"
                class="scenario-template-editor"
                :rows="6"
                placeholder="留空表示不提供 Goal 默认内容"
                @update:model-value="selectedScenario.goal_template = $event || null"
              />
            </FormField>
          </section>

          <div class="scenario-runtime-settings-label">
            <h3>运行时配置</h3>
            <p class="field-desc text-faint">以下配置决定项目执行环境、Prompt 与调度行为。</p>
          </div>

          <div class="form-row">
            <FormField
              v-slot="{ controlAttrs }"
              class="form-group half"
              label="提示词模板组 (Prompt Group)"
              :error="selectedScenarioPromptGroupValid ? '' : '模板组不存在、不可用或不支持探索'"
            >
              <Select
                v-model="selectedScenario.prompt_group"
                v-bind="controlAttrs"
                class="mono"
                :invalid="!selectedScenarioPromptGroupValid"
                :options="
                  scenarioPromptGroupOptions.map((option) => ({
                    value: option.value,
                    label: `${option.label}${option.exists && !option.valid ? ' · 校验异常' : ''}`,
                  }))
                "
              />
              <Button
                v-if="!selectedScenarioPromptGroupValid"
                variant="link"
                size="sm"
                @click="openPromptManagerForScenario"
              >
                去管理
              </Button>
            </FormField>
          </div>

          <div class="form-row">
            <FormField v-slot="{ controlAttrs }" class="form-group half" label="手动报告能力">
              <Select
                :model-value="reportPolicyEnabled ? '1' : '0'"
                v-bind="controlAttrs"
                :options="[
                  { value: '0', label: '未启用' },
                  { value: '1', label: '启用 v1' },
                ]"
                @update:model-value="setReportPolicyVersion"
              />
            </FormField>
            <FormField
              v-slot="{ controlAttrs }"
              class="form-group half"
              label="报告 Prompt Group"
              :error="selectedReportPromptGroupValid ? '' : '报告模板组不存在、不可用或不支持报告'"
              hint="仅用于项目完成后的手动 durable 报告，不改变探索 Prompt。"
            >
              <Select
                :model-value="reportPromptGroup"
                v-bind="controlAttrs"
                class="mono"
                :disabled="!reportPolicyEnabled"
                :invalid="!selectedReportPromptGroupValid"
                :options="
                  reportPromptGroupOptions.map((option) => ({
                    value: option.value,
                    label: `${option.label}${option.exists && !option.valid ? ' · 校验异常' : ''}`,
                  }))
                "
                @update:model-value="setReportPromptGroup"
              />
            </FormField>
          </div>

          <div v-if="profileSelectionReadOnly" class="form-group">
            <span class="form-label">执行环境画像 (Execution Profile)</span>
            <div class="readonly-profile">
              <code class="config-chip">{{ selectedScenario.profile_id || 'default' }}</code>
              <span class="text-faint">{{
                availableProfileOptions[0]
                  ? profileSummary(availableProfileOptions[0])
                  : '无额外画像'
              }}</span>
            </div>
            <p class="field-desc text-faint">
              画像来自 dispatch.yaml，决定容器镜像、网络、capability 与隔离策略。
            </p>
          </div>
          <FormField
            v-else
            v-slot="{ controlAttrs }"
            class="form-group"
            label="执行环境画像 (Execution Profile)"
            hint="画像来自 dispatch.yaml，决定容器镜像、网络、capability 与隔离策略。"
            :error="selectedScenarioProfileValid ? '' : '引用的执行环境画像不存在'"
          >
            <Select
              v-model="selectedScenario.profile_id"
              v-bind="controlAttrs"
              class="mono"
              :invalid="!selectedScenarioProfileValid"
              :options="
                availableProfileOptions.map((profile) => ({
                  value: profile.id,
                  label: `${profileLabel(profile)} · ${profileSummary(profile)}`,
                }))
              "
            />
          </FormField>

          <div class="form-group">
            <Checkbox
              v-model="selectedScenario.is_default"
              class="font-bold"
              label="设定为系统默认新建项目场景方案 (Default Scenario)"
            />
          </div>

          <footer class="editor-footer">
            <Button
              variant="danger"
              :disabled="!canDeleteSelectedScenario"
              :title="selectedScenarioDeleteTitle"
              @click="confirmDeleteScenario"
            >
              删除场景
            </Button>
            <div class="footer-actions">
              <Button
                variant="secondary"
                :disabled="!isScenarioDirty"
                title="放弃当前场景未保存修改"
                @click="resetSelectedScenario"
              >
                重置为已保存值
              </Button>
              <Button
                variant="success"
                :loading="isSavingScenarios"
                :disabled="!selectedScenarioCanSave"
                :title="
                  featureError ||
                  (!selectedScenarioPromptGroupValid
                    ? '模板组不存在、不可用或不支持探索'
                    : !selectedReportPromptGroupValid
                      ? '报告模板组不存在、不可用或不支持报告'
                      : !selectedScenarioProfileValid
                        ? '执行环境画像不存在'
                        : '保存当前场景')
                "
                @click="saveScenarios"
              >
                {{ isSavingScenarios ? '保存中...' : '保存当前场景' }}
              </Button>
            </div>
          </footer>
        </div>
        <EmptyState
          v-else
          class="editor-placeholder"
          title="请选择场景"
          description="请在左侧选择一个场景进行编辑，或者新建一个探索方案。"
        />
      </div>
    </section>

    <ConfirmAction
      :open="deleteScenarioKey !== null"
      title="删除场景配置"
      :description="deleteScenarioDescription"
      confirm-label="确认删除"
      tone="danger"
      :loading="isDeletingScenario"
      @update:open="handleDeleteScenarioOpenChange"
      @confirm="saveDeleteScenario"
    >
      <p>警告：删除后，关联此场景的项目在新创时将无法使用对应配置组。</p>
      <p v-if="deleteTargetScenario?.is_default">
        该场景当前为默认场景；删除后会自动把剩余列表第一项设为默认。
      </p>
    </ConfirmAction>
  </article>
</template>

<script setup lang="ts">
import { onBeforeRouteLeave } from 'vue-router'
import { ConfirmAction } from '@/components/patterns'
import {
  Alert,
  Button,
  Checkbox,
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
  addNewScenario,
  availableProfileOptions,
  canDeleteSelectedScenario,
  confirmDeleteScenario,
  confirmDiscardScenarioChanges,
  deleteScenarioDescription,
  deleteScenarioKey,
  deleteTargetScenario,
  featureError,
  formatError,
  formatInfraList,
  handleDeleteScenarioOpenChange,
  infraCapNames,
  infraImageNames,
  infraWorkerNames,
  isAddingScenario,
  isDeletingScenario,
  isLoadingScenarios,
  isScenarioDirty,
  isScenarioMutationPending,
  isSavingScenarios,
  isScenariosError,
  openPromptManagerForScenario,
  reportPolicyEnabled,
  reportPromptGroup,
  reportPromptGroupOptions,
  resetSelectedScenario,
  saveDeleteScenario,
  saveScenarios,
  profileLabel,
  profileSelectionReadOnly,
  profileSummary,
  scenarioOptionsData,
  scenarioPreflightMessage,
  scenarioPreflightTone,
  scenarioPromptGroupOptions,
  scenarioUsageCount,
  scenarios,
  scenariosError,
  selectScenario,
  selectedScenario,
  selectedScenarioCanSave,
  selectedScenarioDeleteTitle,
  selectedScenarioKey,
  selectedReportPromptGroupValid,
  selectedScenarioProfileValid,
  selectedScenarioPromptGroupValid,
  setReportPolicyVersion,
  setReportPromptGroup,
} = useScenariosConsole()

onBeforeRouteLeave(() => confirmDiscardScenarioChanges())
</script>
