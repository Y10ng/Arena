import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import {
  api,
  listInfrastructureCapabilities,
  listInfrastructureImages,
  listInfrastructureWorkers,
  listPromptGroups,
} from '@/api/client'
import type {
  InfrastructureCapability,
  InfrastructureImage,
  InfrastructureWorker,
  PromptGroup,
  Scenario,
  ScenarioFeatureSchemaItem,
  ScenarioOptions,
  ScenarioValidationResult,
} from '@/api/types'
import { useUIStore } from '@/stores/ui'
import {
  DEFAULT_PROFILE_ID,
  buildAvailableCaps,
  buildAvailableImageOptions,
  buildAvailableProfileOptions,
  ensureDefaultScenario,
  profileLabel,
  profileSummary,
} from '@/features/settings/scenarioOptions'
import {
  featureDraftFromScenario,
  featurePayloadFromDraft,
  invalidCaps,
  isKnownImage,
  validateFeaturesAgainstSchema,
} from '@/features/settings/featuresSchema'
import {
  buildPromptGroupSelectOptions,
  isPromptGroupReferenceValid,
  normalizePromptGroups,
} from '@/features/settings/promptGroupOptions'
import { clonePlain, formatError, formatInfraList } from './consoleCommon'
import {
  normalizeScenarioForm,
  scenarioPayloadFromForm,
  scenarioSnapshot,
} from './scenarioConsoleModel'

export const useScenariosConsole = () => {
  const uiStore = useUIStore()
  const queryClient = useQueryClient()
  const router = useRouter()

  const deleteScenarioKey = ref<string | null>(null)
  const isAddingScenario = ref(false)
  const isSavingScenarios = ref(false)
  const isDeletingScenario = ref(false)
  const isScenarioMutationPending = computed(
    () => isAddingScenario.value || isSavingScenarios.value || isDeletingScenario.value,
  )
  const scenarioPreflightMessage = ref('')
  const scenarioPreflightTone = ref<'success' | 'error'>('success')
  const selectedScenarioKey = ref<string | null>(null)
  const selectedScenario = ref<Scenario | null>(null)
  const scenarioFeatureDraft = ref<Record<string, unknown>>({})
  const selectedScenarioSavedSnapshot = ref('')
  let scenarioDraftGeneration = 0

  watch(
    [selectedScenarioKey, selectedScenario, scenarioFeatureDraft],
    () => {
      scenarioDraftGeneration++
    },
    { deep: true, flush: 'sync' },
  )

  const {
    data: scenariosData,
    isLoading: isLoadingScenarios,
    isError: isScenariosError,
    error: scenariosError,
    refetch: refetchScenarios,
  } = useQuery<Scenario[]>({
    queryKey: ['scenarios'],
    queryFn: () => api<Scenario[]>('GET', '/scenarios'),
  })

  const { data: scenarioOptionsData } = useQuery<ScenarioOptions>({
    queryKey: ['scenarioOptions'],
    queryFn: () => api<ScenarioOptions>('GET', '/scenarios/options'),
  })

  const { data: infrastructureWorkersData } = useQuery<InfrastructureWorker[]>({
    queryKey: ['infrastructureWorkers'],
    queryFn: listInfrastructureWorkers,
  })

  const { data: infrastructureImagesData } = useQuery<InfrastructureImage[]>({
    queryKey: ['infrastructureImages'],
    queryFn: listInfrastructureImages,
  })

  const { data: infrastructureCapabilitiesData } = useQuery<InfrastructureCapability[]>({
    queryKey: ['infrastructureCapabilities'],
    queryFn: listInfrastructureCapabilities,
  })

  const {
    data: promptGroupsData,
    isLoading: isLoadingPromptGroups,
    isError: isPromptGroupsError,
  } = useQuery<PromptGroup[]>({
    queryKey: ['promptGroups'],
    queryFn: ({ signal }) => listPromptGroups(signal),
  })

  const scenarios = computed(() => scenariosData.value || [])
  const promptGroups = computed(() => normalizePromptGroups(promptGroupsData.value || []))
  const persistedSelectedScenario = computed(
    () => scenarios.value.find((scenario) => scenario.key === selectedScenarioKey.value) || null,
  )

  const scenarioFeatureSchema = computed<ScenarioFeatureSchemaItem[]>(
    () => scenarioOptionsData.value?.features || [],
  )

  const featureValidation = computed(() => {
    return validateFeaturesAgainstSchema(scenarioFeatureDraft.value, scenarioFeatureSchema.value)
  })

  const featureError = computed(() => featureValidation.value.errors.join('; '))

  const loadScenario = (key: string) => {
    const matched = scenarios.value.find((s) => s.key === key)
    if (!matched) return

    selectedScenarioKey.value = key
    const draft = normalizeScenarioForm(clonePlain(matched))
    selectedScenario.value = draft
    scenarioFeatureDraft.value = featureDraftFromScenario(
      draft.features || {},
      scenarioFeatureSchema.value,
    )
    selectedScenarioSavedSnapshot.value = scenarioSnapshot(draft, scenarioFeatureDraft.value)
  }

  const clearSelectedScenario = () => {
    selectedScenarioKey.value = null
    selectedScenario.value = null
    selectedScenarioSavedSnapshot.value = ''
    scenarioFeatureDraft.value = featureDraftFromScenario({}, scenarioFeatureSchema.value)
  }

  const isScenarioDirty = computed(() => {
    return Boolean(
      selectedScenario.value &&
      scenarioSnapshot(selectedScenario.value, scenarioFeatureDraft.value) !==
        selectedScenarioSavedSnapshot.value,
    )
  })

  const confirmDiscardScenarioChanges = () => {
    if (!isScenarioDirty.value) return true
    return window.confirm('当前场景有未保存修改，确定放弃这些修改吗？')
  }

  const selectScenario = (key: string, options: { force?: boolean } = {}) => {
    if (!options.force && key === selectedScenarioKey.value) return
    if (!options.force && !confirmDiscardScenarioChanges()) return
    loadScenario(key)
  }

  watch(
    scenarios,
    (list) => {
      if (list.length === 0) {
        clearSelectedScenario()
        return
      }

      const selectedKey = selectedScenarioKey.value
      if (!selectedKey) {
        selectScenario(list[0].key, { force: true })
        return
      }
      if (!list.some((s) => s.key === selectedKey)) {
        if (!isScenarioDirty.value) selectScenario(list[0].key, { force: true })
        return
      }

      if (!isScenarioDirty.value) {
        selectScenario(selectedKey, { force: true })
      }
    },
    { immediate: true },
  )

  watch(scenarioFeatureSchema, () => {
    if (selectedScenario.value && !isScenarioDirty.value) {
      scenarioFeatureDraft.value = featureDraftFromScenario(
        selectedScenario.value.features || {},
        scenarioFeatureSchema.value,
      )
      selectedScenarioSavedSnapshot.value = scenarioSnapshot(
        selectedScenario.value,
        scenarioFeatureDraft.value,
      )
    }
  })

  const nextScenarioKey = () => {
    const keys = new Set(scenarios.value.map((s) => s.key))
    let index = 1
    while (keys.has(`custom-scen-${index}`)) {
      index += 1
    }
    return `custom-scen-${index}`
  }

  const infraWorkers = computed(() => infrastructureWorkersData.value || [])
  const infraImages = computed(() => infrastructureImagesData.value || [])
  const infraCapabilities = computed(() => infrastructureCapabilitiesData.value || [])
  const infraWorkerNames = computed(() => infraWorkers.value.map((item) => item.worker_name))
  const infraImageNames = computed(() => infraImages.value.map((item) => item.image))
  const infraCapNames = computed(() => infraCapabilities.value.map((item) => item.name))

  const availableImageOptions = computed(() => {
    const images = infraImages.value.map((item) => item.image)
    if (images.length > 0) return images
    return buildAvailableImageOptions({
      scenarios: scenarios.value,
      options: scenarioOptionsData.value,
      selectedScenario: selectedScenario.value,
    })
  })

  const availableCaps = computed(() => {
    const caps = infraCapabilities.value.map((item) => item.name)
    if (caps.length > 0) return caps
    return buildAvailableCaps({
      scenarios: scenarios.value,
      options: scenarioOptionsData.value,
      selectedScenario: selectedScenario.value,
    })
  })

  const selectedScenarioImageValid = computed(() => {
    if (!selectedScenario.value || availableImageOptions.value.length === 0) return true
    return isKnownImage(selectedScenario.value.image, availableImageOptions.value)
  })

  const selectedScenarioInvalidCaps = computed(() => {
    if (!selectedScenario.value || !scenarioOptionsData.value) return []
    return invalidCaps(selectedScenario.value.cap_add, availableCaps.value)
  })

  const selectedScenarioCapsValid = computed(() => selectedScenarioInvalidCaps.value.length === 0)

  const scenarioPromptGroupOptions = computed(() => {
    return buildPromptGroupSelectOptions({
      groups: promptGroups.value,
      selectedPromptGroup: selectedScenario.value?.prompt_group,
      capability: 'exploration',
    })
  })

  const selectedScenarioPromptGroupValid = computed(() => {
    if (!selectedScenario.value || isLoadingPromptGroups.value || isPromptGroupsError.value)
      return true
    const preserved =
      persistedSelectedScenario.value?.prompt_group === selectedScenario.value.prompt_group
        ? (['compatibility-only', 'test-only'] as const)
        : []
    return isPromptGroupReferenceValid(
      selectedScenario.value.prompt_group,
      promptGroups.value,
      'exploration',
      [...preserved],
    )
  })

  const reportPromptGroup = computed(() => {
    const value = scenarioFeatureDraft.value.report_prompt_group
    return typeof value === 'string' ? value : ''
  })
  const persistedReportPromptGroup = computed(() => {
    const value = persistedSelectedScenario.value?.features?.report_prompt_group
    return typeof value === 'string' ? value : ''
  })
  const reportPromptGroupOptions = computed(() =>
    buildPromptGroupSelectOptions({
      groups: promptGroups.value,
      selectedPromptGroup: reportPromptGroup.value,
      capability: 'durable-report',
    }),
  )
  const reportPolicyEnabled = computed(
    () => scenarioFeatureDraft.value.report_policy_version === '1',
  )
  const selectedReportPromptGroupValid = computed(() => {
    if (!reportPolicyEnabled.value || isLoadingPromptGroups.value || isPromptGroupsError.value)
      return true
    const preserved =
      persistedReportPromptGroup.value === reportPromptGroup.value
        ? (['compatibility-only'] as const)
        : []
    return isPromptGroupReferenceValid(
      reportPromptGroup.value,
      promptGroups.value,
      'durable-report',
      [...preserved],
    )
  })
  const setReportPolicyVersion = (value: string) => {
    scenarioFeatureDraft.value.report_policy_version = value
    if (value !== '1') scenarioFeatureDraft.value.report_prompt_group = ''
  }
  const setReportPromptGroup = (value: string) => {
    scenarioFeatureDraft.value.report_prompt_group = value
  }

  const availableProfileOptions = computed(() => {
    return buildAvailableProfileOptions({
      scenarios: scenarios.value,
      options: scenarioOptionsData.value,
      selectedScenario: selectedScenario.value,
    })
  })

  const availableProfileIds = computed(() =>
    availableProfileOptions.value.map((profile) => profile.id),
  )
  const profileSelectionReadOnly = computed(() => availableProfileOptions.value.length <= 1)

  const selectedScenarioProfileValid = computed(() => {
    if (!selectedScenario.value || availableProfileIds.value.length === 0) return true
    return availableProfileIds.value.includes(selectedScenario.value.profile_id)
  })

  const selectedScenarioCanSave = computed(() => {
    return Boolean(
      !featureError.value &&
      selectedScenarioPromptGroupValid.value &&
      selectedReportPromptGroupValid.value &&
      selectedScenarioProfileValid.value &&
      !isScenarioMutationPending.value,
    )
  })

  const scenarioUsageCount = (key: string) => scenarioOptionsData.value?.project_counts?.[key] || 0
  const selectedScenarioUsageCount = computed(() => {
    if (!selectedScenario.value || !scenarioOptionsData.value) return null
    return scenarioUsageCount(selectedScenario.value.key)
  })
  const canDeleteSelectedScenario = computed(() => {
    return Boolean(
      selectedScenario.value &&
      !selectedScenario.value.is_default &&
      scenarios.value.length > 1 &&
      selectedScenarioUsageCount.value === 0 &&
      !isScenarioMutationPending.value,
    )
  })
  const selectedScenarioDeleteTitle = computed(() => {
    if (!selectedScenario.value) return '请选择场景'
    if (selectedScenario.value.is_default) return '默认场景不可删除，请先将其他场景设为默认'
    if (scenarios.value.length <= 1) return '至少需要保留一个场景'
    if (selectedScenarioUsageCount.value === null) return '正在读取场景使用情况'
    if (selectedScenarioUsageCount.value > 0)
      return `当前有 ${selectedScenarioUsageCount.value} 个项目使用该场景，无法删除`
    return '删除当前场景'
  })

  const openPromptManagerForScenario = () => {
    const key = selectedScenario.value?.prompt_group
    void router.push({
      name: 'ConsolePrompts',
      query: key ? { focus: key } : undefined,
    })
  }

  const addNewScenario = async () => {
    if (isScenarioMutationPending.value || !confirmDiscardScenarioChanges()) return

    const startingDraftGeneration = scenarioDraftGeneration
    const startingSelectedKey = selectedScenarioKey.value
    const newKey = nextScenarioKey()
    const defaultProfile = availableProfileOptions.value[0]?.id || DEFAULT_PROFILE_ID
    const newScen: Scenario = {
      key: newKey,
      label: '自定义安全探索方案',
      is_default: false,
      profile_id: defaultProfile,
      image: '',
      prompt_group: 'goal-driven-v1',
      knowledge: null,
      origin_template: null,
      goal_template: null,
      cap_add: [],
      features: {},
    }

    try {
      isAddingScenario.value = true
      const updatedList = ensureDefaultScenario([
        ...scenarios.value.map((s) => normalizeScenarioForm(clonePlain(s))),
        newScen,
      ])
      await api<Scenario[]>('PUT', '/scenarios', updatedList.map(scenarioPayloadFromForm))
      await refetchScenarios()
      void queryClient.invalidateQueries({ queryKey: ['scenarioOptions'] })
      void queryClient.invalidateQueries({ queryKey: ['scenario-catalog'] })
      if (
        scenarioDraftGeneration === startingDraftGeneration &&
        selectedScenarioKey.value === startingSelectedKey
      ) {
        selectScenario(newKey, { force: true })
      }
      uiStore.showToast('新方案已建立，请在右侧修改详细属性并保存。', 'success')
    } catch (err: unknown) {
      uiStore.showToast(`新建场景失败: ${formatError(err)}`, 'danger')
    } finally {
      isAddingScenario.value = false
    }
  }

  const saveScenarios = async () => {
    if (isScenarioMutationPending.value || !selectedScenario.value || featureError.value) return
    if (!selectedScenarioPromptGroupValid.value) {
      uiStore.showToast('当前场景引用的模板组不存在、不可用或不支持探索', 'danger')
      return
    }
    if (!selectedReportPromptGroupValid.value) {
      uiStore.showToast('当前场景引用的报告模板组不存在、不可用或不支持报告', 'danger')
      return
    }
    if (!selectedScenarioProfileValid.value) {
      uiStore.showToast('当前场景引用的执行环境画像不存在', 'danger')
      return
    }

    const draft = normalizeScenarioForm({
      ...clonePlain(selectedScenario.value),
      knowledge: null,
      features: featurePayloadFromDraft(scenarioFeatureDraft.value, scenarioFeatureSchema.value),
    })

    const updatedList = scenarios.value.map((s) => {
      const scenario = normalizeScenarioForm(clonePlain(s))
      if (scenario.key === draft.key) {
        return draft
      }
      if (draft.is_default) {
        return { ...scenario, is_default: false }
      }
      return scenario
    })
    const payload = ensureDefaultScenario(updatedList).map(scenarioPayloadFromForm)
    const targetKey = draft.key
    const acceptedSnapshot = scenarioSnapshot(selectedScenario.value, scenarioFeatureDraft.value)

    try {
      isSavingScenarios.value = true
      const preflight = await api<ScenarioValidationResult>('POST', '/scenarios/validate', payload)
      if (
        selectedScenarioKey.value === targetKey &&
        selectedScenario.value &&
        scenarioSnapshot(selectedScenario.value, scenarioFeatureDraft.value) === acceptedSnapshot
      ) {
        scenarioPreflightTone.value = 'success'
        scenarioPreflightMessage.value = `保存预检通过：${preflight.scenario_count} 个场景，当前场景影响 ${preflight.project_counts[targetKey] || 0} 个项目。`
      }
      await api<Scenario[]>('PUT', '/scenarios', payload)
      await refetchScenarios()
      void queryClient.invalidateQueries({ queryKey: ['scenarioOptions'] })
      void queryClient.invalidateQueries({ queryKey: ['scenario-catalog'] })
      if (selectedScenarioKey.value === targetKey && selectedScenario.value) {
        selectedScenarioSavedSnapshot.value = acceptedSnapshot
      }
      uiStore.showToast('当前场景配置已成功保存！', 'success')
    } catch (err: unknown) {
      if (selectedScenarioKey.value === targetKey) {
        scenarioPreflightTone.value = 'error'
        scenarioPreflightMessage.value = `保存预检失败：${formatError(err)}`
        uiStore.showToast(`保存场景方案失败: ${formatError(err)}`, 'danger')
      }
    } finally {
      isSavingScenarios.value = false
    }
  }

  const confirmDeleteScenario = () => {
    if (selectedScenario.value && canDeleteSelectedScenario.value) {
      deleteScenarioKey.value = selectedScenario.value.key
    }
  }

  const handleDeleteScenarioOpenChange = (open: boolean) => {
    if (!open && !isDeletingScenario.value) {
      deleteScenarioKey.value = null
    }
  }

  const saveDeleteScenario = async () => {
    if (isScenarioMutationPending.value) return

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

    const startingDraftGeneration = scenarioDraftGeneration
    const startingSelectedKey = selectedScenarioKey.value
    const updatedList = ensureDefaultScenario(
      scenarios.value.filter((s) => s.key !== key).map((s) => normalizeScenarioForm(clonePlain(s))),
    )

    try {
      isDeletingScenario.value = true
      await api<Scenario[]>('PUT', '/scenarios', updatedList.map(scenarioPayloadFromForm))
      await refetchScenarios()
      void queryClient.invalidateQueries({ queryKey: ['scenarioOptions'] })
      void queryClient.invalidateQueries({ queryKey: ['scenario-catalog'] })
      uiStore.showToast('场景已成功删除', 'success')
      if (
        scenarioDraftGeneration === startingDraftGeneration &&
        selectedScenarioKey.value === startingSelectedKey
      ) {
        if (updatedList.length > 0) {
          selectScenario(updatedList[0].key, { force: true })
        } else {
          clearSelectedScenario()
        }
      }
      deleteScenarioKey.value = null
    } catch (err: unknown) {
      uiStore.showToast(`删除场景失败: ${formatError(err)}`, 'danger')
    } finally {
      isDeletingScenario.value = false
    }
  }

  const resetSelectedScenario = () => {
    if (selectedScenarioKey.value) {
      selectScenario(selectedScenarioKey.value, { force: true })
    }
  }

  const deleteTargetScenario = computed(() => {
    if (!deleteScenarioKey.value) return null
    return scenarios.value.find((s) => s.key === deleteScenarioKey.value) || null
  })
  const deleteScenarioDescription = computed(() => {
    return deleteScenarioKey.value
      ? `您确定要永久删除场景配置「${deleteScenarioKey.value}」吗？`
      : '您确定要永久删除此场景配置吗？'
  })

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
    availableCaps,
    canDeleteSelectedScenario,
    availableImageOptions,
    availableProfileOptions,
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
    scenarioFeatureDraft,
    scenarioFeatureSchema,
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
    selectedScenarioCapsValid,
    selectedScenarioDeleteTitle,
    selectedScenarioImageValid,
    selectedScenarioInvalidCaps,
    selectedScenarioKey,
    selectedReportPromptGroupValid,
    selectedScenarioProfileValid,
    selectedScenarioPromptGroupValid,
    setReportPolicyVersion,
    setReportPromptGroup,
    profileLabel,
    profileSelectionReadOnly,
    profileSummary,
  }
}
