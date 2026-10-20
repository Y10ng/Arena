import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { api, listPromptGroups } from '@/api/client'
import type { PromptGroup, Scenario, ScenarioOptions } from '@/api/types'
import { useUIStore } from '@/stores/ui'
import {
  DEFAULT_PROFILE_ID,
  buildAvailableProfileOptions,
  ensureDefaultScenario,
  profileLabel,
  profileSummary,
} from '@/features/settings/scenarioOptions'
import { validateFeaturesAgainstSchema } from '@/features/settings/featuresSchema'
import {
  buildPromptGroupSelectOptions,
  isPromptGroupReferenceValid,
  normalizePromptGroups,
} from '@/features/settings/promptGroupOptions'
import { clonePlain, formatError } from './consoleCommon'
import {
  normalizeScenarioForm,
  scenarioPayloadFromForm,
  scenarioSnapshot,
} from './scenarioConsoleModel'

export const useScenariosConsole = () => {
  const uiStore = useUIStore()
  const queryClient = useQueryClient()
  const router = useRouter()
  const selectedScenarioKey = ref<string | null>(null)
  const scenarioDraft = ref<Scenario | null>(null)
  const draftMode = ref<'create' | 'edit'>('edit')
  const savedDraftSnapshot = ref('')
  const saveError = ref('')
  const deleteError = ref('')
  const deleteScenarioKey = ref<string | null>(null)
  const isSavingScenarios = ref(false)
  const isDeletingScenario = ref(false)
  const isScenarioMutationPending = computed(
    () => isSavingScenarios.value || isDeletingScenario.value,
  )
  const isScenarioEditorOpen = computed(() => scenarioDraft.value !== null)
  const isCreatingScenario = computed(() => draftMode.value === 'create')

  const {
    data: scenariosData,
    isLoading: isLoadingScenarios,
    isError: isScenariosError,
    error: scenariosError,
  } = useQuery<Scenario[]>({
    queryKey: ['scenarios'],
    queryFn: ({ signal }) => api<Scenario[]>('GET', '/scenarios', undefined, signal),
  })
  const {
    data: scenarioOptionsData,
    isLoading: isLoadingScenarioOptions,
    isError: isScenarioOptionsError,
    error: scenarioOptionsError,
  } = useQuery<ScenarioOptions>({
    queryKey: ['scenarioOptions'],
    queryFn: () => api<ScenarioOptions>('GET', '/scenarios/options'),
  })
  const {
    data: promptGroupsData,
    isLoading: isLoadingPromptGroups,
    isError: isPromptGroupsError,
    error: promptGroupsError,
  } = useQuery<PromptGroup[]>({
    queryKey: ['promptGroups'],
    queryFn: ({ signal }) => listPromptGroups(signal),
  })

  const scenarios = computed(() => scenariosData.value || [])
  const promptGroups = computed(() => normalizePromptGroups(promptGroupsData.value || []))
  const selectedScenario = computed(
    () => scenarios.value.find((scenario) => scenario.key === selectedScenarioKey.value) || null,
  )
  watch(
    scenarios,
    (list) => {
      if (!list.some((scenario) => scenario.key === selectedScenarioKey.value)) {
        selectedScenarioKey.value = list[0]?.key || null
      }
    },
    { immediate: true },
  )

  const isScenarioDirty = computed(() =>
    Boolean(
      scenarioDraft.value &&
      scenarioSnapshot(scenarioDraft.value, scenarioDraft.value.features) !==
        savedDraftSnapshot.value,
    ),
  )
  const confirmDiscardScenarioChanges = () => {
    if (isScenarioMutationPending.value) return false
    return !isScenarioDirty.value || window.confirm('当前场景有未保存修改，确定放弃这些修改吗？')
  }
  const closeScenarioEditor = () => {
    if (!confirmDiscardScenarioChanges()) return
    scenarioDraft.value = null
    saveError.value = ''
  }
  const handleEditorOpenChange = (open: boolean) => {
    if (!open) closeScenarioEditor()
  }
  const openScenarioEditor = (scenario: Scenario, mode: 'create' | 'edit') => {
    if (isScenarioMutationPending.value || !confirmDiscardScenarioChanges()) return
    scenarioDraft.value = normalizeScenarioForm(clonePlain(scenario))
    draftMode.value = mode
    savedDraftSnapshot.value = scenarioSnapshot(scenarioDraft.value, scenarioDraft.value.features)
    saveError.value = ''
  }
  const selectScenario = (key: string) => {
    selectedScenarioKey.value = key
    deleteError.value = ''
  }
  const editSelectedScenario = () => {
    if (selectedScenario.value) openScenarioEditor(selectedScenario.value, 'edit')
  }
  const addNewScenario = () => {
    const keys = new Set(scenarios.value.map((scenario) => scenario.key))
    let index = 1
    while (keys.has(`custom-scen-${index}`)) index++
    openScenarioEditor(
      {
        key: `custom-scen-${index}`,
        label: '自定义安全探索方案',
        is_default: scenarios.value.length === 0,
        profile_id: scenarioOptionsData.value?.profiles[0]?.id || DEFAULT_PROFILE_ID,
        image: '',
        prompt_group: 'goal-driven-v1',
        knowledge: null,
        origin_template: null,
        goal_template: null,
        cap_add: [],
        features: {},
      },
      'create',
    )
  }

  const featureError = computed(() =>
    scenarioDraft.value
      ? validateFeaturesAgainstSchema(
          scenarioDraft.value.features,
          scenarioOptionsData.value?.features,
        ).errors.join('; ')
      : '',
  )
  const scenarioPromptGroupOptions = computed(() =>
    buildPromptGroupSelectOptions({
      groups: promptGroups.value,
      selectedPromptGroup: scenarioDraft.value?.prompt_group,
      capability: 'exploration',
    }),
  )
  const promptGroupValid = (scenario: Scenario | null) => {
    if (!scenario || isLoadingPromptGroups.value || isPromptGroupsError.value) return true
    const persisted = scenarios.value.find((item) => item.key === scenario.key)
    return isPromptGroupReferenceValid(
      scenario.prompt_group,
      promptGroups.value,
      'exploration',
      persisted?.prompt_group === scenario.prompt_group ? ['compatibility-only', 'test-only'] : [],
    )
  }
  const scenarioDraftPromptGroupValid = computed(() => promptGroupValid(scenarioDraft.value))
  const selectedScenarioPromptGroupValid = computed(() => promptGroupValid(selectedScenario.value))
  const availableProfileOptions = computed(() =>
    buildAvailableProfileOptions({
      scenarios: scenarios.value,
      options: scenarioOptionsData.value,
      selectedScenario: scenarioDraft.value,
    }),
  )
  const profileSelectionReadOnly = computed(() => availableProfileOptions.value.length <= 1)
  const profileValid = (scenario: Scenario | null) =>
    !scenario ||
    !scenarioOptionsData.value ||
    scenarioOptionsData.value.profiles.some((profile) => profile.id === scenario.profile_id)
  const scenarioDraftProfileValid = computed(() => profileValid(scenarioDraft.value))
  const selectedScenarioProfileValid = computed(() => profileValid(selectedScenario.value))
  const selectedProfile = computed(() =>
    availableProfileOptions.value.find(
      (profile) => profile.id === selectedScenario.value?.profile_id,
    ),
  )
  const scenarioDraftCanSave = computed(() =>
    Boolean(
      scenarioDraft.value?.label.trim() &&
      !featureError.value &&
      scenarioDraftPromptGroupValid.value &&
      scenarioDraftProfileValid.value &&
      !isLoadingScenarioOptions.value &&
      !isScenarioOptionsError.value &&
      !isLoadingScenarios.value &&
      !isScenariosError.value &&
      !isLoadingPromptGroups.value &&
      !isPromptGroupsError.value &&
      !isScenarioMutationPending.value,
    ),
  )

  const scenarioUsageCount = (key: string) => scenarioOptionsData.value?.project_counts?.[key] || 0
  const selectedScenarioUsageCount = computed(() =>
    selectedScenario.value && scenarioOptionsData.value
      ? scenarioUsageCount(selectedScenario.value.key)
      : null,
  )
  const canDeleteSelectedScenario = computed(() =>
    Boolean(
      selectedScenario.value &&
      !selectedScenario.value.is_default &&
      scenarios.value.length > 1 &&
      selectedScenarioUsageCount.value === 0 &&
      !isScenariosError.value &&
      !isLoadingScenarios.value &&
      !isScenarioOptionsError.value &&
      !isScenarioMutationPending.value,
    ),
  )
  const selectedScenarioDeleteReason = computed(() => {
    if (!selectedScenario.value) return ''
    if (selectedScenario.value.is_default) return '默认场景不可删除，请先将其他场景设为默认。'
    if (scenarios.value.length <= 1) return '至少需要保留一个场景。'
    if (isScenarioOptionsError.value) return '无法读取场景使用情况，暂不能删除。'
    if (selectedScenarioUsageCount.value === null) return '正在读取场景使用情况。'
    if (selectedScenarioUsageCount.value > 0)
      return `当前有 ${selectedScenarioUsageCount.value} 个项目使用该场景，无法删除。`
    return ''
  })
  const openPromptManagerForScenario = () => {
    const key = scenarioDraft.value?.prompt_group || selectedScenario.value?.prompt_group
    if (!confirmDiscardScenarioChanges()) return
    scenarioDraft.value = null
    void router.push({ name: 'ConsolePrompts', query: key ? { focus: key } : undefined })
  }
  const invalidateScenarioCatalog = () => {
    void queryClient.invalidateQueries({ queryKey: ['scenarioOptions'] })
    void queryClient.invalidateQueries({ queryKey: ['scenario-catalog'] })
  }
  const saveScenarios = async () => {
    if (!scenarioDraftCanSave.value || !scenarioDraft.value) return
    const draft = scenarioPayloadFromForm(clonePlain(scenarioDraft.value))
    const mode = draftMode.value
    const buildPayload = () => {
      const exists = scenarios.value.some((scenario) => scenario.key === draft.key)
      if (mode === 'create' && exists) throw new Error('场景键值已被占用，请取消后重新新建。')
      if (mode === 'edit' && !exists) throw new Error('该场景已不在最新列表中，不能覆盖保存。')
      const list = scenarios.value.map((scenario) =>
        scenario.key === draft.key
          ? draft
          : {
              ...clonePlain(scenario),
              ...(draft.is_default ? { is_default: false } : {}),
            },
      )
      if (mode === 'create') list.push(draft)
      return ensureDefaultScenario(list).map(scenarioPayloadFromForm)
    }
    isSavingScenarios.value = true
    saveError.value = ''
    let stage = '保存预检失败'
    try {
      await api('POST', '/scenarios/validate', buildPayload())
      stage = '写入场景失败'
      // 预检期间可能刷新列表；写入前重新合并，服务端仍会校验实际写入内容。
      await queryClient.cancelQueries({ queryKey: ['scenarios'] }, { revert: false })
      const saved = await api<Scenario[]>('PUT', '/scenarios', buildPayload())
      // PUT 等待期间仍可刷新；先取消迟到读取，再写入权威结果。
      await queryClient.cancelQueries({ queryKey: ['scenarios'] }, { revert: false })
      queryClient.setQueryData(['scenarios'], saved)
      selectedScenarioKey.value = draft.key
      scenarioDraft.value = null
      invalidateScenarioCatalog()
      uiStore.showToast(mode === 'create' ? '场景已创建' : '场景已保存', 'success')
    } catch (err: unknown) {
      saveError.value = `${stage}：${formatError(err)}`
    } finally {
      isSavingScenarios.value = false
    }
  }

  const confirmDeleteScenario = () => {
    if (selectedScenario.value && canDeleteSelectedScenario.value) {
      deleteScenarioKey.value = selectedScenario.value.key
      deleteError.value = ''
    }
  }
  const handleDeleteScenarioOpenChange = (open: boolean) => {
    if (!open && !isDeletingScenario.value) deleteScenarioKey.value = null
  }
  const saveDeleteScenario = async () => {
    if (
      isScenarioMutationPending.value ||
      !scenarioOptionsData.value ||
      isScenarioOptionsError.value
    )
      return
    const key = deleteScenarioKey.value
    const target = scenarios.value.find((scenario) => scenario.key === key)
    if (
      !key ||
      !target ||
      target.is_default ||
      scenarioUsageCount(key) > 0 ||
      scenarios.value.length <= 1
    )
      return
    isDeletingScenario.value = true
    try {
      await queryClient.cancelQueries({ queryKey: ['scenarios'] }, { revert: false })
      const payload = ensureDefaultScenario(
        scenarios.value.filter((scenario) => scenario.key !== key),
      ).map(scenarioPayloadFromForm)
      const saved = await api<Scenario[]>('PUT', '/scenarios', payload)
      await queryClient.cancelQueries({ queryKey: ['scenarios'] }, { revert: false })
      queryClient.setQueryData(['scenarios'], saved)
      invalidateScenarioCatalog()
      uiStore.showToast('场景已成功删除', 'success')
      deleteScenarioKey.value = null
    } catch (err: unknown) {
      deleteError.value = `删除场景失败：${formatError(err)}`
    } finally {
      isDeletingScenario.value = false
    }
  }
  const deleteScenarioDescription = computed(
    () => `确定永久删除场景「${deleteScenarioKey.value || ''}」吗？`,
  )
  watch(
    () => uiStore.globalRefreshKey,
    () => {
      void queryClient.invalidateQueries({ queryKey: ['scenarios'] })
      void queryClient.invalidateQueries({ queryKey: ['scenarioOptions'] })
      void queryClient.invalidateQueries({ queryKey: ['promptGroups'] })
    },
  )

  return {
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
  }
}
