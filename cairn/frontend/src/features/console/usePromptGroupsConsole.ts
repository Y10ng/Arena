import { computed, onScopeDispose, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import {
  createPromptGroup,
  deletePromptGroup,
  listPromptFiles,
  listPromptGroups,
  readPromptFile,
  repairPromptGroup,
  restorePromptGroup,
  validatePromptGroup,
  writePromptFile,
} from '@/api/client'
import type {
  PromptFile,
  PromptFileContent,
  PromptGroup,
  PromptGroupValidationResult,
} from '@/api/types'
import { useUIStore } from '@/stores/ui'
import {
  DEFAULT_PROMPT_REQUIRED_TOKENS,
  missingPromptTokens,
  normalizePromptFiles,
  normalizePromptGroups,
  promptGroupKindLabel,
  promptGroupRoleLabel,
  visiblePromptGroupsForConsole,
} from '@/features/settings/promptGroupOptions'
import { formatError } from './consoleCommon'
import {
  buildPromptFileCategories,
  createPromptFileSnapshot,
  isPromptFileSnapshotDirty,
  promptGroupCapabilities,
} from './promptConsoleModel'

interface PromptDraftSnapshot {
  name: string | null
  content: string
}

const isAbortError = (error: unknown) =>
  typeof error === 'object' && error !== null && 'name' in error && error.name === 'AbortError'

export const usePromptGroupsConsole = () => {
  const uiStore = useUIStore()
  const queryClient = useQueryClient()
  const route = useRoute()
  const router = useRouter()

  const isCreatingPromptGroup = ref(false)
  const isDeletingPromptGroup = ref(false)
  const isLoadingPromptFile = ref(false)
  const isSavingPromptFile = ref(false)
  const isValidatingPromptGroup = ref(false)
  const isRepairingPromptGroup = ref(false)
  const isRestoringPromptGroup = ref(false)
  const selectedPromptGroupKey = ref<string | null>(null)
  const selectedPromptFileName = ref<string | null>(null)
  const promptFileContent = ref('')
  const promptFileSavedSnapshot = ref(createPromptFileSnapshot(null, ''))
  const newPromptGroupKey = ref('')
  const newPromptCloneFrom = ref('goal-driven-v1')
  const selectedPromptValidation = ref<PromptGroupValidationResult | null>(null)
  const promptValidationStale = ref(false)
  const deletePromptGroupTarget = ref<PromptGroup | null>(null)
  const repairPromptGroupTarget = ref<PromptGroup | null>(null)
  const restorePromptGroupTarget = ref<PromptGroup | null>(null)
  const clonePromptGroupSource = ref<PromptGroup | null>(null)
  const clonePromptGroupKey = ref('')
  const clonePromptGroupError = ref('')
  let deletePromptGroupTrigger: HTMLElement | null = null
  let repairPromptGroupTrigger: HTMLElement | null = null
  let restorePromptGroupTrigger: HTMLElement | null = null
  let clonePromptGroupTrigger: HTMLElement | null = null
  const focusRestoreTimers = new Set<number>()

  onScopeDispose(() => {
    if (typeof window !== 'undefined') {
      focusRestoreTimers.forEach((timer) => window.clearTimeout(timer))
    }
    focusRestoreTimers.clear()
  })

  let promptGroupVisitGeneration = 0
  let promptDraftGeneration = 0
  let promptFileReadGeneration = 0
  let promptFileReadController: AbortController | null = null
  let pendingPromptFileName: string | null = null
  let promptValidationGeneration = 0
  let promptValidationController: AbortController | null = null
  let createGeneration = 0
  let deleteGeneration = 0
  let saveGeneration = 0
  let repairGeneration = 0
  let restoreGeneration = 0

  watch(
    [selectedPromptFileName, promptFileContent],
    () => {
      promptDraftGeneration++
    },
    { flush: 'sync' },
  )

  const {
    data: promptGroupsData,
    isLoading: isLoadingPromptGroups,
    isError: isPromptGroupsError,
    error: promptGroupsError,
    refetch: refetchPromptGroups,
  } = useQuery<PromptGroup[]>({
    queryKey: ['promptGroups'],
    queryFn: ({ signal }) => listPromptGroups(signal),
  })

  const {
    data: promptFilesData,
    isLoading: isLoadingPromptFiles,
    isError: isPromptFilesError,
    error: promptFilesError,
    refetch: refetchPromptFiles,
  } = useQuery<PromptFile[]>({
    queryKey: computed(() => ['promptFiles', selectedPromptGroupKey.value || '']),
    queryFn: ({ queryKey, signal }) => {
      const key = String(queryKey[1] || '')
      return key ? listPromptFiles(key, signal) : Promise.resolve([])
    },
    enabled: computed(() => Boolean(selectedPromptGroupKey.value)),
  })

  const allPromptGroups = computed(() => normalizePromptGroups(promptGroupsData.value || []))
  const promptGroups = computed(() => visiblePromptGroupsForConsole(allPromptGroups.value))
  const promptFiles = computed(() => normalizePromptFiles(promptFilesData.value || []))
  const selectedPromptGroup = computed(
    () => promptGroups.value.find((group) => group.key === selectedPromptGroupKey.value) || null,
  )
  const selectedPromptFileSummary = computed(
    () => promptFiles.value.find((file) => file.path === selectedPromptFileName.value) || null,
  )
  const selectedPromptRequiredTokens = computed(() => {
    const file = selectedPromptFileSummary.value
    if (!file || file.required_tokens.length === 0)
      return DEFAULT_PROMPT_REQUIRED_TOKENS[file?.path || ''] || []
    return file.required_tokens
  })
  const selectedPromptMissingTokens = computed(() => {
    return missingPromptTokens(promptFileContent.value, selectedPromptRequiredTokens.value)
  })
  const promptFileSnapshot = () =>
    createPromptFileSnapshot(selectedPromptFileName.value, promptFileContent.value)
  const isPromptFileDirty = computed(() => {
    return isPromptFileSnapshotDirty(
      selectedPromptFileName.value,
      promptFileSnapshot(),
      promptFileSavedSnapshot.value,
    )
  })
  const selectedPromptReferencedBy = computed(() => selectedPromptGroup.value?.referenced_by || [])
  const selectedPromptCapabilities = computed(() =>
    promptGroupCapabilities(selectedPromptGroup.value),
  )
  const selectedPromptCanEdit = computed(() => selectedPromptCapabilities.value.canEdit)
  const canDeleteSelectedPromptGroup = computed(() => selectedPromptCapabilities.value.canDelete)
  const canRestoreSelectedPromptGroup = computed(() => selectedPromptCapabilities.value.canRestore)
  const canRepairSelectedPromptGroup = computed(() => selectedPromptCapabilities.value.canRepair)
  const isPromptDestructivePending = computed(
    () =>
      isDeletingPromptGroup.value || isRepairingPromptGroup.value || isRestoringPromptGroup.value,
  )
  const promptFileCategories = computed(() => buildPromptFileCategories(promptFiles.value))
  const selectedPromptGroupDeleteTitle = computed(() => {
    const group = selectedPromptGroup.value
    if (!group) return '请选择模板组'
    if (group.overrides_builtin) return '覆盖层请使用“恢复内置”操作'
    if (!group.managed) return '内置模板组不可删除'
    if (group.referenced_by.length) return '被场景引用的自定义模板组不可删除'
    return '删除当前模板组'
  })

  const setPromptFileDraft = (file: PromptFileContent | null) => {
    selectedPromptFileName.value = file?.path || null
    promptFileContent.value = file?.content || ''
    promptFileSavedSnapshot.value = promptFileSnapshot()
  }

  const abortPromptFileRead = () => {
    promptFileReadGeneration++
    promptFileReadController?.abort()
    promptFileReadController = null
    pendingPromptFileName = null
    isLoadingPromptFile.value = false
  }

  const abortPromptValidation = () => {
    promptValidationGeneration++
    promptValidationController?.abort()
    promptValidationController = null
    isValidatingPromptGroup.value = false
  }

  const clearPromptFileDraft = () => {
    abortPromptFileRead()
    selectedPromptFileName.value = null
    promptFileContent.value = ''
    promptFileSavedSnapshot.value = promptFileSnapshot()
  }

  const confirmDiscardPromptChanges = () => {
    if (!isPromptFileDirty.value) return true
    return window.confirm('Prompt 文件存在未保存修改，确定要放弃吗？')
  }

  const syncPromptRoute = (group: string | null, file: string | null) => {
    const query = { ...route.query }
    if (group) query.focus = group
    else delete query.focus
    if (file) query.file = file
    else delete query.file
    void router.replace({ query })
  }

  const isCurrentPromptVisit = (key: string, visitGeneration: number) =>
    selectedPromptGroupKey.value === key && promptGroupVisitGeneration === visitGeneration

  const selectPromptGroup = (
    key: string,
    options: { force?: boolean; syncRoute?: boolean } = {},
  ) => {
    if (key === selectedPromptGroupKey.value) return true
    if (!options.force && !confirmDiscardPromptChanges()) return false
    promptGroupVisitGeneration++
    abortPromptValidation()
    selectedPromptGroupKey.value = key
    selectedPromptValidation.value = null
    promptValidationStale.value = false
    clearPromptFileDraft()
    if (options.syncRoute !== false) syncPromptRoute(key, null)
    return true
  }

  const selectPromptFile = async (
    name: string,
    options: { force?: boolean; syncRoute?: boolean } = {},
  ) => {
    const targetGroupKey = selectedPromptGroupKey.value
    if (!targetGroupKey) return false
    if (name === selectedPromptFileName.value) {
      if (pendingPromptFileName && pendingPromptFileName !== name) abortPromptFileRead()
      return true
    }
    if (!options.force && !confirmDiscardPromptChanges()) return false

    const visitGeneration = promptGroupVisitGeneration
    const readGeneration = ++promptFileReadGeneration
    promptFileReadController?.abort()
    const controller = new AbortController()
    promptFileReadController = controller
    pendingPromptFileName = name
    try {
      isLoadingPromptFile.value = true
      const file = await readPromptFile(targetGroupKey, name, controller.signal)
      if (
        readGeneration !== promptFileReadGeneration ||
        !isCurrentPromptVisit(targetGroupKey, visitGeneration)
      )
        return false
      setPromptFileDraft(file)
      if (options.syncRoute !== false) syncPromptRoute(targetGroupKey, file.path)
      return true
    } catch (error: unknown) {
      if (
        readGeneration === promptFileReadGeneration &&
        isCurrentPromptVisit(targetGroupKey, visitGeneration) &&
        !isAbortError(error)
      ) {
        uiStore.showToast(`读取 Prompt 文件失败: ${formatError(error)}`, 'danger')
      }
      return false
    } finally {
      if (
        readGeneration === promptFileReadGeneration &&
        isCurrentPromptVisit(targetGroupKey, visitGeneration)
      ) {
        promptFileReadController = null
        pendingPromptFileName = null
        isLoadingPromptFile.value = false
      }
    }
  }

  watch(
    promptGroups,
    (groups) => {
      if (groups.length === 0) {
        promptGroupVisitGeneration++
        abortPromptValidation()
        selectedPromptGroupKey.value = null
        clearPromptFileDraft()
        return
      }
      if (!selectedPromptGroupKey.value) {
        const focusedKey =
          typeof route.query.focus === 'string'
            ? groups.find((group) => group.key === route.query.focus)?.key
            : undefined
        selectPromptGroup(focusedKey || groups[0].key, { force: true, syncRoute: !focusedKey })
        return
      }
      if (
        !groups.some((group) => group.key === selectedPromptGroupKey.value) &&
        !isPromptFileDirty.value
      ) {
        selectPromptGroup(groups[0].key, { force: true })
      }
    },
    { immediate: true },
  )

  watch(
    promptGroups,
    (groups) => {
      if (groups.some((group) => group.key === newPromptCloneFrom.value)) return
      newPromptCloneFrom.value =
        groups.find((group) => group.key === 'goal-driven-v1')?.key || groups[0]?.key || ''
    },
    { immediate: true },
  )

  watch(
    promptFiles,
    (files) => {
      if (!selectedPromptGroupKey.value) return
      const current = selectedPromptFileName.value
      if (current && files.some((file) => file.path === current)) return
      if (current && isPromptFileDirty.value) return
      const deepLinked =
        typeof route.query.file === 'string'
          ? files.find((file) => file.path === route.query.file)
          : undefined
      const first = deepLinked || files[0]
      if (first) {
        void selectPromptFile(first.path, { force: true })
        return
      }
      clearPromptFileDraft()
    },
    { immediate: true },
  )

  const nextPromptGroupKey = (base: string) => {
    const normalizedBase =
      base
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, '_')
        .replace(/^_+|_+$/g, '') || 'prompt_group'
    const keys = new Set(allPromptGroups.value.map((group) => group.key))
    let candidate = normalizedBase
    let index = 2
    while (keys.has(candidate)) {
      candidate = `${normalizedBase}_${index}`
      index++
    }
    return candidate
  }

  const handleCreatePromptGroup = async (keyOverride?: string, cloneFromOverride?: string) => {
    if (isCreatingPromptGroup.value) return false
    const key = (keyOverride || newPromptGroupKey.value).trim()
    const cloneFrom = (cloneFromOverride || newPromptCloneFrom.value || 'goal-driven-v1').trim()
    if (!key) return false
    const startingVisit = promptGroupVisitGeneration
    const startingDraftGeneration = promptDraftGeneration
    const generation = ++createGeneration
    try {
      isCreatingPromptGroup.value = true
      const created = await createPromptGroup(key, cloneFrom)
      await refetchPromptGroups()
      if (generation !== createGeneration) return false
      uiStore.showToast(`模板组 ${created.key} 已创建`, 'success')
      if (newPromptGroupKey.value.trim() === key) newPromptGroupKey.value = ''
      if (
        startingVisit === promptGroupVisitGeneration &&
        startingDraftGeneration === promptDraftGeneration
      ) {
        selectPromptGroup(created.key, { force: true })
      }
      return true
    } catch (error: unknown) {
      if (generation === createGeneration)
        uiStore.showToast(`创建模板组失败: ${formatError(error)}`, 'danger')
      return false
    } finally {
      if (generation === createGeneration) isCreatingPromptGroup.value = false
    }
  }

  const restoreActionTrigger = (trigger: HTMLElement | null, attempt = 0) => {
    if (!trigger || typeof document === 'undefined') return
    const action = trigger.dataset.consoleAction
    const candidate = trigger.isConnected
      ? trigger
      : (action
          ? document.querySelector<HTMLElement>(`[data-console-action="${action}"]`)
          : null) ||
        document.querySelector<HTMLElement>('[data-console-action="prompt-group-stable"]') ||
        document.querySelector<HTMLElement>('[data-console-action="clone-prompt-group"]')
    if (!candidate) return
    if (attempt > 0 && (!('disabled' in candidate) || !(candidate as HTMLButtonElement).disabled)) {
      candidate.focus()
      return
    }
    if (attempt < 10 && typeof window !== 'undefined') {
      const timer = window.setTimeout(() => {
        focusRestoreTimers.delete(timer)
        if (typeof document !== 'undefined') restoreActionTrigger(trigger, attempt + 1)
      }, 20)
      focusRestoreTimers.add(timer)
    }
  }

  const openClonePromptGroup = (trigger: HTMLElement | null = null) => {
    const source = selectedPromptGroup.value
    if (!source || isCreatingPromptGroup.value) return
    clonePromptGroupSource.value = { ...source }
    clonePromptGroupKey.value = nextPromptGroupKey(`${source.key}_copy`)
    clonePromptGroupError.value = ''
    clonePromptGroupTrigger = trigger
  }

  const handleClonePromptGroupOpenChange = (open: boolean) => {
    if (open || isCreatingPromptGroup.value) return
    const trigger = clonePromptGroupTrigger
    clonePromptGroupSource.value = null
    clonePromptGroupKey.value = ''
    clonePromptGroupError.value = ''
    clonePromptGroupTrigger = null
    restoreActionTrigger(trigger)
  }

  const submitClonePromptGroup = async () => {
    const source = clonePromptGroupSource.value
    const key = clonePromptGroupKey.value.trim()
    if (!source || isCreatingPromptGroup.value) return
    if (!key) {
      clonePromptGroupError.value = '模板组键值不能为空'
      return
    }
    if (!/^[a-z0-9][a-z0-9_-]*$/.test(key)) {
      clonePromptGroupError.value = '仅支持小写字母、数字、下划线和连字符，且必须以字母或数字开头'
      return
    }
    if (allPromptGroups.value.some((group) => group.key === key)) {
      clonePromptGroupError.value = '模板组键值已存在'
      return
    }
    clonePromptGroupError.value = ''
    if (!(await handleCreatePromptGroup(key, source.key))) return
    if (clonePromptGroupSource.value?.key !== source.key) return
    const trigger = clonePromptGroupTrigger
    clonePromptGroupSource.value = null
    clonePromptGroupKey.value = ''
    clonePromptGroupTrigger = null
    restoreActionTrigger(trigger)
  }

  const openDeletePromptGroup = (trigger: HTMLElement | null = null) => {
    const target = selectedPromptGroup.value
    if (
      !target ||
      !promptGroupCapabilities(target).canDelete ||
      isPromptDestructivePending.value ||
      !confirmDiscardPromptChanges()
    )
      return
    deletePromptGroupTarget.value = { ...target }
    deletePromptGroupTrigger = trigger
  }

  const handleDeletePromptGroupOpenChange = (open: boolean) => {
    if (open || isDeletingPromptGroup.value) return
    const trigger = deletePromptGroupTrigger
    deletePromptGroupTarget.value = null
    deletePromptGroupTrigger = null
    restoreActionTrigger(trigger)
  }

  const handleDeletePromptGroup = async () => {
    const targetGroup = deletePromptGroupTarget.value
    if (!targetGroup || isDeletingPromptGroup.value) return
    const targetKey = targetGroup.key
    const visitGeneration = promptGroupVisitGeneration
    const draftGeneration = promptDraftGeneration
    const generation = ++deleteGeneration
    try {
      isDeletingPromptGroup.value = true
      await deletePromptGroup(targetKey)
      await refetchPromptGroups()
      void queryClient.invalidateQueries({ queryKey: ['promptFiles', targetKey] })
      if (generation === deleteGeneration && isCurrentPromptVisit(targetKey, visitGeneration)) {
        uiStore.showToast(`模板组 ${targetKey} 已删除`, 'success')
        // 删除请求进行期间编辑器被锁定，因此可安全清理被删除组，并显式进入仍存在的组。
        if (promptDraftGeneration === draftGeneration) {
          promptGroupVisitGeneration++
          selectedPromptGroupKey.value = null
          selectedPromptValidation.value = null
          clearPromptFileDraft()
          const replacement = promptGroups.value.find((group) => group.key !== targetKey)
          if (replacement) selectPromptGroup(replacement.key, { force: true })
        }
      }
      if (generation === deleteGeneration && deletePromptGroupTarget.value?.key === targetKey) {
        const trigger = deletePromptGroupTrigger
        deletePromptGroupTarget.value = null
        deletePromptGroupTrigger = null
        restoreActionTrigger(trigger)
      }
    } catch (error: unknown) {
      if (generation === deleteGeneration && isCurrentPromptVisit(targetKey, visitGeneration)) {
        uiStore.showToast(`删除模板组失败: ${formatError(error)}`, 'danger')
      }
    } finally {
      if (generation === deleteGeneration) isDeletingPromptGroup.value = false
    }
  }

  const validatePromptGroupTarget = async (targetKey: string, visitGeneration: number) => {
    const draftGeneration = promptDraftGeneration
    const generation = ++promptValidationGeneration
    promptValidationController?.abort()
    const controller = new AbortController()
    promptValidationController = controller
    try {
      isValidatingPromptGroup.value = true
      const result = await validatePromptGroup(targetKey, controller.signal)
      if (
        generation !== promptValidationGeneration ||
        !isCurrentPromptVisit(targetKey, visitGeneration)
      )
        return false
      selectedPromptValidation.value = result
      promptValidationStale.value =
        isPromptFileDirty.value || promptDraftGeneration !== draftGeneration
      if (result.valid) {
        uiStore.showToast('模板组校验通过', 'success')
      } else {
        const missingFiles = result.missing_files.length
          ? `缺文件 ${result.missing_files.join(', ')}`
          : ''
        const missingTokens = Object.entries(result.missing_tokens)
          .filter(([, tokens]) => tokens.length > 0)
          .map(([file, tokens]) => `${file}: ${tokens.join(', ')}`)
          .join('; ')
        uiStore.showToast(
          `模板组校验失败 ${[missingFiles, missingTokens].filter(Boolean).join('；')}`,
          'danger',
        )
      }
      await refetchPromptGroups()
      if (
        generation === promptValidationGeneration &&
        isCurrentPromptVisit(targetKey, visitGeneration)
      ) {
        await refetchPromptFiles()
      }
      return true
    } catch (error: unknown) {
      if (
        generation === promptValidationGeneration &&
        isCurrentPromptVisit(targetKey, visitGeneration) &&
        !isAbortError(error)
      ) {
        uiStore.showToast(`校验模板组失败: ${formatError(error)}`, 'danger')
      }
      return false
    } finally {
      if (
        generation === promptValidationGeneration &&
        isCurrentPromptVisit(targetKey, visitGeneration)
      ) {
        promptValidationController = null
        isValidatingPromptGroup.value = false
      }
    }
  }

  const validateSelectedPromptGroup = async () => {
    const targetKey = selectedPromptGroupKey.value
    if (!targetKey) return false
    return validatePromptGroupTarget(targetKey, promptGroupVisitGeneration)
  }

  watch(selectedPromptGroupKey, (key) => {
    if (key) void validatePromptGroupTarget(key, promptGroupVisitGeneration)
  })

  const openRepairPromptGroup = (trigger: HTMLElement | null = null) => {
    const target = selectedPromptGroup.value
    if (
      !target ||
      !promptGroupCapabilities(target).canRepair ||
      isPromptDestructivePending.value ||
      !confirmDiscardPromptChanges()
    )
      return
    repairPromptGroupTarget.value = { ...target }
    repairPromptGroupTrigger = trigger
  }

  const handleRepairPromptGroupOpenChange = (open: boolean) => {
    if (open || isRepairingPromptGroup.value) return
    const trigger = repairPromptGroupTrigger
    repairPromptGroupTarget.value = null
    repairPromptGroupTrigger = null
    restoreActionTrigger(trigger)
  }

  const handleRepairPromptGroup = async () => {
    const targetGroup = repairPromptGroupTarget.value
    if (!targetGroup || isRepairingPromptGroup.value) return
    const targetKey = targetGroup.key
    const cloneFrom = targetGroup.overrides_builtin ? null : newPromptCloneFrom.value
    const visitGeneration = promptGroupVisitGeneration
    const draftGeneration = promptDraftGeneration
    const generation = ++repairGeneration
    try {
      isRepairingPromptGroup.value = true
      await repairPromptGroup(targetKey, cloneFrom)
      await refetchPromptGroups()
      void queryClient.invalidateQueries({ queryKey: ['promptFiles', targetKey] })
      if (generation === repairGeneration && isCurrentPromptVisit(targetKey, visitGeneration)) {
        uiStore.showToast(`模板组 ${targetKey} 已修复`, 'success')
        if (promptDraftGeneration === draftGeneration) clearPromptFileDraft()
        await refetchPromptFiles()
        await validatePromptGroupTarget(targetKey, visitGeneration)
      }
      if (generation === repairGeneration && repairPromptGroupTarget.value?.key === targetKey) {
        const trigger = repairPromptGroupTrigger
        repairPromptGroupTarget.value = null
        repairPromptGroupTrigger = null
        restoreActionTrigger(trigger)
      }
    } catch (error: unknown) {
      if (generation === repairGeneration && isCurrentPromptVisit(targetKey, visitGeneration)) {
        uiStore.showToast(`修复模板组失败: ${formatError(error)}`, 'danger')
      }
    } finally {
      if (generation === repairGeneration) isRepairingPromptGroup.value = false
    }
  }

  const openRestorePromptGroup = (trigger: HTMLElement | null = null) => {
    const target = selectedPromptGroup.value
    if (
      !target ||
      !promptGroupCapabilities(target).canRestore ||
      isPromptDestructivePending.value ||
      !confirmDiscardPromptChanges()
    )
      return
    restorePromptGroupTarget.value = { ...target }
    restorePromptGroupTrigger = trigger
  }

  const handleRestorePromptGroupOpenChange = (open: boolean) => {
    if (open || isRestoringPromptGroup.value) return
    const trigger = restorePromptGroupTrigger
    restorePromptGroupTarget.value = null
    restorePromptGroupTrigger = null
    restoreActionTrigger(trigger)
  }

  const handleRestorePromptGroup = async () => {
    const targetGroup = restorePromptGroupTarget.value
    if (!targetGroup || isRestoringPromptGroup.value) return
    const targetKey = targetGroup.key
    const visitGeneration = promptGroupVisitGeneration
    const draftGeneration = promptDraftGeneration
    const generation = ++restoreGeneration
    try {
      isRestoringPromptGroup.value = true
      await restorePromptGroup(targetKey)
      await refetchPromptGroups()
      void queryClient.invalidateQueries({ queryKey: ['promptFiles', targetKey] })
      if (generation === restoreGeneration && isCurrentPromptVisit(targetKey, visitGeneration)) {
        uiStore.showToast(`模板组 ${targetKey} 已恢复为内置版本`, 'success')
        if (promptDraftGeneration === draftGeneration) clearPromptFileDraft()
        await refetchPromptFiles()
        await validatePromptGroupTarget(targetKey, visitGeneration)
      }
      if (generation === restoreGeneration && restorePromptGroupTarget.value?.key === targetKey) {
        const trigger = restorePromptGroupTrigger
        restorePromptGroupTarget.value = null
        restorePromptGroupTrigger = null
        restoreActionTrigger(trigger)
      }
    } catch (error: unknown) {
      if (generation === restoreGeneration && isCurrentPromptVisit(targetKey, visitGeneration)) {
        uiStore.showToast(`恢复内置模板组失败: ${formatError(error)}`, 'danger')
      }
    } finally {
      if (generation === restoreGeneration) isRestoringPromptGroup.value = false
    }
  }

  const resetPromptFileDraft = () => {
    if (!selectedPromptFileName.value) return
    const accepted = JSON.parse(promptFileSavedSnapshot.value) as PromptDraftSnapshot
    if (accepted.name !== selectedPromptFileName.value) return
    promptFileContent.value = accepted.content
  }

  const savePromptFile = async () => {
    const targetGroupKey = selectedPromptGroupKey.value
    const targetFileName = selectedPromptFileName.value
    const targetContent = promptFileContent.value
    if (
      !targetGroupKey ||
      !targetFileName ||
      !isPromptFileDirty.value ||
      selectedPromptMissingTokens.value.length > 0
    )
      return
    const visitGeneration = promptGroupVisitGeneration
    const generation = ++saveGeneration
    const acceptedSnapshot = createPromptFileSnapshot(targetFileName, targetContent)
    try {
      isSavingPromptFile.value = true
      const saved = await writePromptFile(targetGroupKey, targetFileName, targetContent)
      void queryClient.invalidateQueries({ queryKey: ['promptFiles', targetGroupKey] })
      await refetchPromptGroups()
      if (
        generation === saveGeneration &&
        isCurrentPromptVisit(targetGroupKey, visitGeneration) &&
        selectedPromptFileName.value === targetFileName
      ) {
        promptFileSavedSnapshot.value = acceptedSnapshot
        if (promptFileSnapshot() === acceptedSnapshot) setPromptFileDraft(saved)
        uiStore.showToast(`Prompt 文件 ${saved.path} 已保存`, 'success')
        await refetchPromptFiles()
        await validatePromptGroupTarget(targetGroupKey, visitGeneration)
      }
    } catch (error: unknown) {
      if (
        generation === saveGeneration &&
        isCurrentPromptVisit(targetGroupKey, visitGeneration) &&
        selectedPromptFileName.value === targetFileName
      ) {
        uiStore.showToast(`保存 Prompt 文件失败: ${formatError(error)}`, 'danger')
      }
    } finally {
      if (generation === saveGeneration) isSavingPromptFile.value = false
    }
  }

  watch(isPromptFileDirty, (dirty) => {
    if (dirty && selectedPromptValidation.value) promptValidationStale.value = true
  })

  watch(
    () => uiStore.globalRefreshKey,
    () => {
      void queryClient.invalidateQueries({ queryKey: ['promptGroups'] })
      void queryClient.invalidateQueries({ queryKey: ['promptFiles'] })
    },
  )

  return {
    canDeleteSelectedPromptGroup,
    canRepairSelectedPromptGroup,
    canRestoreSelectedPromptGroup,
    clonePromptGroupError,
    clonePromptGroupKey,
    clonePromptGroupSource,
    confirmDiscardPromptChanges,
    deletePromptGroupTarget,
    formatError,
    handleClonePromptGroupOpenChange,
    handleCreatePromptGroup,
    handleDeletePromptGroup,
    handleDeletePromptGroupOpenChange,
    handleRepairPromptGroup,
    handleRepairPromptGroupOpenChange,
    handleRestorePromptGroup,
    handleRestorePromptGroupOpenChange,
    isCreatingPromptGroup,
    isDeletingPromptGroup,
    isLoadingPromptFile,
    isLoadingPromptFiles,
    isLoadingPromptGroups,
    isPromptDestructivePending,
    isPromptFileDirty,
    isRepairingPromptGroup,
    isRestoringPromptGroup,
    isPromptFilesError,
    isPromptGroupsError,
    isSavingPromptFile,
    isValidatingPromptGroup,
    newPromptCloneFrom,
    newPromptGroupKey,
    openClonePromptGroup,
    openDeletePromptGroup,
    openRepairPromptGroup,
    openRestorePromptGroup,
    promptFileCategories,
    promptFileContent,
    promptFiles,
    promptFilesError,
    promptGroupKindLabel,
    promptGroupRoleLabel,
    promptGroups,
    promptGroupsError,
    promptValidationStale,
    repairPromptGroupTarget,
    resetPromptFileDraft,
    restorePromptGroupTarget,
    savePromptFile,
    selectPromptFile,
    selectPromptGroup,
    selectedPromptCanEdit,
    selectedPromptFileName,
    selectedPromptGroup,
    selectedPromptGroupDeleteTitle,
    selectedPromptGroupKey,
    selectedPromptMissingTokens,
    selectedPromptReferencedBy,
    selectedPromptValidation,
    submitClonePromptGroup,
    syncPromptRoute,
    validateSelectedPromptGroup,
  }
}
