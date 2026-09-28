import { computed, onScopeDispose, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import {
  createKnowledgeSet,
  deleteKnowledgeFile,
  deleteKnowledgeSet,
  listAdvisoryLibraryRevisions,
  listKnowledgeFiles,
  listKnowledgeSets,
  readKnowledgeFile,
  writeKnowledgeFile,
} from '@/api/client'
import type {
  AdvisoryLibraryRevision,
  KnowledgeFile,
  KnowledgeFileContent,
  KnowledgeSet,
} from '@/api/types'
import { useUIStore } from '@/stores/ui'
import {
  normalizeKnowledgeFiles,
  normalizeKnowledgeSets,
} from '@/features/settings/knowledgeOptions'
import { readUtf8File } from '@/features/projects/projectResources'
import { formatError } from './consoleCommon'
import {
  ROOT_KNOWLEDGE_DIRECTORY,
  buildKnowledgeDirectoryGroups,
  knowledgeDirectoryLabel,
  knowledgeFileDirectory,
  knowledgeFileName,
  knowledgeFilePathInDirectory,
  nextKnowledgeFilePath,
} from './knowledgeConsoleModel'

interface KnowledgeDraftSnapshot {
  path: string
  content: string
}

interface KnowledgeFileDeleteTarget {
  key: string
  path: string
}

const isAbortError = (error: unknown) =>
  typeof error === 'object' && error !== null && 'name' in error && error.name === 'AbortError'

export const useKnowledgeConsole = () => {
  const uiStore = useUIStore()
  const queryClient = useQueryClient()

  const isCreatingKnowledgeSet = ref(false)
  const isDeletingKnowledgeSet = ref(false)
  const isLoadingKnowledgeFile = ref(false)
  const isSavingKnowledgeFile = ref(false)
  const isDeletingKnowledgeFile = ref(false)
  const isUploadingKnowledgeFile = ref(false)
  const knowledgeUploadInput = ref<HTMLInputElement | null>(null)
  const selectedKnowledgeKey = ref<string | null>(null)
  const selectedKnowledgeFilePath = ref<string | null>(null)
  const selectedKnowledgeDirectory = ref(ROOT_KNOWLEDGE_DIRECTORY)
  const knowledgeFileDraftPath = ref('')
  const knowledgeFileContent = ref('')
  const knowledgeFileSavedSnapshot = ref('')
  const newKnowledgeSetKey = ref('')
  const deleteKnowledgeSetTarget = ref<KnowledgeSet | null>(null)
  const deleteKnowledgeFileTarget = ref<KnowledgeFileDeleteTarget | null>(null)
  let deleteKnowledgeSetTrigger: HTMLElement | null = null
  let deleteKnowledgeFileTrigger: HTMLElement | null = null
  const focusRestoreTimers = new Set<number>()

  onScopeDispose(() => {
    if (typeof window !== 'undefined') {
      focusRestoreTimers.forEach((timer) => window.clearTimeout(timer))
    }
    focusRestoreTimers.clear()
  })

  let knowledgeSetVisitGeneration = 0
  let knowledgeDraftGeneration = 0
  let knowledgeFileReadGeneration = 0
  let knowledgeFileReadController: AbortController | null = null
  let pendingKnowledgeFilePath: string | null = null
  let createGeneration = 0
  let deleteSetGeneration = 0
  let saveGeneration = 0
  let deleteFileGeneration = 0
  let uploadGeneration = 0

  watch(
    [
      selectedKnowledgeFilePath,
      selectedKnowledgeDirectory,
      knowledgeFileDraftPath,
      knowledgeFileContent,
    ],
    () => {
      knowledgeDraftGeneration++
    },
    { flush: 'sync' },
  )

  const {
    data: knowledgeSetsData,
    isLoading: isLoadingKnowledgeSets,
    isError: isKnowledgeSetsError,
    error: knowledgeSetsError,
    refetch: refetchKnowledgeSets,
  } = useQuery<KnowledgeSet[]>({
    queryKey: ['knowledgeSets'],
    queryFn: ({ signal }) => listKnowledgeSets(signal),
  })

  const {
    data: knowledgeFilesData,
    isLoading: isLoadingKnowledgeFiles,
    isError: isKnowledgeFilesError,
    error: knowledgeFilesError,
    refetch: refetchKnowledgeFiles,
  } = useQuery<KnowledgeFile[]>({
    queryKey: computed(() => ['knowledgeFiles', selectedKnowledgeKey.value || '']),
    queryFn: ({ queryKey, signal }) => {
      const key = String(queryKey[1] || '')
      return key ? listKnowledgeFiles(key, signal) : Promise.resolve([])
    },
    enabled: computed(() => Boolean(selectedKnowledgeKey.value)),
  })

  const {
    data: knowledgeRevisionsData,
    isLoading: isLoadingKnowledgeRevisions,
    refetch: refetchKnowledgeRevisions,
  } = useQuery<AdvisoryLibraryRevision[]>({
    queryKey: computed(() => ['knowledgeRevisions', selectedKnowledgeKey.value || '']),
    queryFn: ({ queryKey, signal }) => {
      const key = String(queryKey[1] || '')
      return key ? listAdvisoryLibraryRevisions(key, signal) : Promise.resolve([])
    },
    enabled: computed(() => Boolean(selectedKnowledgeKey.value)),
  })

  const knowledgeSets = computed(() => normalizeKnowledgeSets(knowledgeSetsData.value || []))
  const knowledgeFiles = computed(() => normalizeKnowledgeFiles(knowledgeFilesData.value || []))
  const knowledgeRevisions = computed(() => knowledgeRevisionsData.value || [])
  const currentKnowledgeRevision = computed(() => knowledgeRevisions.value[0] || null)
  const knowledgeDirectoryGroups = computed(() =>
    buildKnowledgeDirectoryGroups(knowledgeFiles.value),
  )
  const selectedKnowledgeDirectoryGroup = computed(() => {
    return (
      knowledgeDirectoryGroups.value.find(
        (group) => group.directory === selectedKnowledgeDirectory.value,
      ) ||
      knowledgeDirectoryGroups.value[0] ||
      null
    )
  })
  const selectedKnowledgeDirectoryLabel = computed(() =>
    knowledgeDirectoryLabel(selectedKnowledgeDirectory.value),
  )
  const knowledgeFilesInSelectedDirectory = computed(
    () => selectedKnowledgeDirectoryGroup.value?.files || [],
  )
  const selectedKnowledgeSet = computed(() => {
    if (!selectedKnowledgeKey.value) return null
    return knowledgeSets.value.find((set) => set.key === selectedKnowledgeKey.value) || null
  })
  const selectedKnowledgeFileSummary = computed(() => {
    if (!selectedKnowledgeFilePath.value) return null
    return (
      knowledgeFiles.value.find((file) => file.path === selectedKnowledgeFilePath.value) || null
    )
  })
  const knowledgeFileSnapshot = () =>
    JSON.stringify({
      path: knowledgeFileDraftPath.value,
      content: knowledgeFileContent.value,
    })
  const isKnowledgeFileDirty = computed(() => {
    return Boolean(
      selectedKnowledgeKey.value && knowledgeFileSnapshot() !== knowledgeFileSavedSnapshot.value,
    )
  })
  const selectedKnowledgeReferencedBy = computed(
    () => selectedKnowledgeSet.value?.referenced_by || [],
  )
  const canDeleteSelectedKnowledgeSet = computed(() => {
    const set = selectedKnowledgeSet.value
    return Boolean(set && !set.builtin && set.referenced_by.length === 0)
  })

  const setKnowledgeFileDraft = (file: KnowledgeFileContent | null, fallbackPath = '') => {
    selectedKnowledgeFilePath.value = file?.path || null
    knowledgeFileDraftPath.value = file?.path || fallbackPath
    // 始终编辑完整原文，不拆分或重建 front matter、BOM、换行与空白。
    knowledgeFileContent.value = file?.content || ''
    selectedKnowledgeDirectory.value = knowledgeFileDirectory(knowledgeFileDraftPath.value)
    knowledgeFileSavedSnapshot.value = knowledgeFileSnapshot()
  }

  const abortKnowledgeFileRead = () => {
    knowledgeFileReadGeneration++
    knowledgeFileReadController?.abort()
    knowledgeFileReadController = null
    pendingKnowledgeFilePath = null
    isLoadingKnowledgeFile.value = false
  }

  const clearKnowledgeFileDraft = () => {
    abortKnowledgeFileRead()
    selectedKnowledgeFilePath.value = null
    knowledgeFileDraftPath.value = ''
    knowledgeFileContent.value = ''
    knowledgeFileSavedSnapshot.value = knowledgeFileSnapshot()
  }

  const resetKnowledgeFileDraft = () => {
    const accepted = JSON.parse(knowledgeFileSavedSnapshot.value) as KnowledgeDraftSnapshot
    knowledgeFileDraftPath.value = accepted.path
    knowledgeFileContent.value = accepted.content
    selectedKnowledgeDirectory.value = knowledgeFileDirectory(accepted.path)
  }

  const confirmDiscardKnowledgeChanges = () => {
    if (!isKnowledgeFileDirty.value) return true
    return window.confirm('当前资料库文件有未保存修改，确定放弃这些修改吗？')
  }

  const isCurrentKnowledgeVisit = (key: string, visitGeneration: number) =>
    selectedKnowledgeKey.value === key && knowledgeSetVisitGeneration === visitGeneration

  const selectKnowledgeSet = (key: string, options: { force?: boolean } = {}) => {
    if (key === selectedKnowledgeKey.value) return true
    if (!options.force && !confirmDiscardKnowledgeChanges()) return false
    knowledgeSetVisitGeneration++
    selectedKnowledgeKey.value = key
    selectedKnowledgeDirectory.value = ROOT_KNOWLEDGE_DIRECTORY
    clearKnowledgeFileDraft()
    return true
  }

  const selectKnowledgeFile = async (path: string, options: { force?: boolean } = {}) => {
    const key = selectedKnowledgeKey.value
    if (!key) return false
    if (path === selectedKnowledgeFilePath.value) {
      if (pendingKnowledgeFilePath && pendingKnowledgeFilePath !== path) abortKnowledgeFileRead()
      return true
    }
    if (!options.force && !confirmDiscardKnowledgeChanges()) return false

    const visitGeneration = knowledgeSetVisitGeneration
    const readGeneration = ++knowledgeFileReadGeneration
    knowledgeFileReadController?.abort()
    const controller = new AbortController()
    knowledgeFileReadController = controller
    pendingKnowledgeFilePath = path

    try {
      isLoadingKnowledgeFile.value = true
      const file = await readKnowledgeFile(key, path, controller.signal)
      if (
        readGeneration !== knowledgeFileReadGeneration ||
        !isCurrentKnowledgeVisit(key, visitGeneration)
      )
        return false
      setKnowledgeFileDraft(file)
      return true
    } catch (err: unknown) {
      if (
        readGeneration === knowledgeFileReadGeneration &&
        isCurrentKnowledgeVisit(key, visitGeneration) &&
        !isAbortError(err)
      ) {
        uiStore.showToast(`读取资料库文件失败: ${formatError(err)}`, 'danger')
      }
      return false
    } finally {
      if (
        readGeneration === knowledgeFileReadGeneration &&
        isCurrentKnowledgeVisit(key, visitGeneration)
      ) {
        knowledgeFileReadController = null
        pendingKnowledgeFilePath = null
        isLoadingKnowledgeFile.value = false
      }
    }
  }

  const selectKnowledgeDirectory = (directory: string, options: { force?: boolean } = {}) => {
    if (directory === selectedKnowledgeDirectory.value) return true
    if (!options.force && !confirmDiscardKnowledgeChanges()) return false
    selectedKnowledgeDirectory.value = directory
    const firstFile = knowledgeDirectoryGroups.value.find((group) => group.directory === directory)
      ?.files[0]
    if (firstFile) {
      void selectKnowledgeFile(firstFile.path, { force: true })
      return true
    }
    clearKnowledgeFileDraft()
    return true
  }

  watch(
    knowledgeSets,
    (sets) => {
      if (sets.length === 0) {
        knowledgeSetVisitGeneration++
        selectedKnowledgeKey.value = null
        clearKnowledgeFileDraft()
        return
      }
      if (!selectedKnowledgeKey.value) {
        selectKnowledgeSet(sets[0].key, { force: true })
        return
      }
      if (
        !sets.some((set) => set.key === selectedKnowledgeKey.value) &&
        !isKnowledgeFileDirty.value
      ) {
        selectKnowledgeSet(sets[0].key, { force: true })
      }
    },
    { immediate: true },
  )

  watch(selectedKnowledgeKey, () => {
    void refetchKnowledgeFiles()
    void refetchKnowledgeRevisions()
  })

  watch(
    knowledgeDirectoryGroups,
    (groups) => {
      if (!groups.some((group) => group.directory === selectedKnowledgeDirectory.value)) {
        selectedKnowledgeDirectory.value = groups[0]?.directory || ROOT_KNOWLEDGE_DIRECTORY
      }
    },
    { immediate: true },
  )

  const handleCreateKnowledgeSet = async () => {
    if (isCreatingKnowledgeSet.value) return
    const key = newKnowledgeSetKey.value.trim()
    if (!key) return
    const generation = ++createGeneration
    const startingVisit = knowledgeSetVisitGeneration
    const startingDraftGeneration = knowledgeDraftGeneration
    try {
      isCreatingKnowledgeSet.value = true
      await createKnowledgeSet(key)
      await refetchKnowledgeSets()
      void queryClient.invalidateQueries({ queryKey: ['scenarioOptions'] })
      if (generation !== createGeneration) return
      if (newKnowledgeSetKey.value.trim() === key) newKnowledgeSetKey.value = ''
      if (
        startingVisit === knowledgeSetVisitGeneration &&
        startingDraftGeneration === knowledgeDraftGeneration
      ) {
        selectKnowledgeSet(key, { force: true })
      }
      uiStore.showToast('资料库已创建', 'success')
    } catch (err: unknown) {
      if (generation === createGeneration)
        uiStore.showToast(`创建资料库失败: ${formatError(err)}`, 'danger')
    } finally {
      if (generation === createGeneration) isCreatingKnowledgeSet.value = false
    }
  }

  const restoreDeleteTrigger = (trigger: HTMLElement | null, attempt = 0) => {
    if (!trigger || typeof document === 'undefined') return
    const action = trigger.dataset.consoleAction
    const candidate = trigger.isConnected
      ? trigger
      : action
        ? document.querySelector<HTMLElement>(`[data-console-action="${action}"]`)
        : null
    if (!candidate) return
    if (attempt > 0 && (!('disabled' in candidate) || !(candidate as HTMLButtonElement).disabled)) {
      candidate.focus()
      return
    }
    if (attempt < 10 && typeof window !== 'undefined') {
      const timer = window.setTimeout(() => {
        focusRestoreTimers.delete(timer)
        if (typeof document !== 'undefined') restoreDeleteTrigger(trigger, attempt + 1)
      }, 20)
      focusRestoreTimers.add(timer)
    }
  }

  const openDeleteKnowledgeSet = (trigger: HTMLElement | null = null) => {
    const set = selectedKnowledgeSet.value
    if (!set || !canDeleteSelectedKnowledgeSet.value || isDeletingKnowledgeSet.value) return
    deleteKnowledgeSetTarget.value = { ...set }
    deleteKnowledgeSetTrigger = trigger
  }

  const handleDeleteKnowledgeSetOpenChange = (open: boolean) => {
    if (open || isDeletingKnowledgeSet.value) return
    const trigger = deleteKnowledgeSetTrigger
    deleteKnowledgeSetTarget.value = null
    deleteKnowledgeSetTrigger = null
    restoreDeleteTrigger(trigger)
  }

  const handleDeleteKnowledgeSet = async () => {
    const target = deleteKnowledgeSetTarget.value
    if (!target || isDeletingKnowledgeSet.value) return

    const targetKey = target.key
    const visitGeneration = knowledgeSetVisitGeneration
    const generation = ++deleteSetGeneration
    try {
      isDeletingKnowledgeSet.value = true
      await deleteKnowledgeSet(targetKey)
      if (
        generation === deleteSetGeneration &&
        isCurrentKnowledgeVisit(targetKey, visitGeneration)
      ) {
        uiStore.showToast('资料库已删除', 'success')
      }
      await refetchKnowledgeSets()
      void queryClient.invalidateQueries({ queryKey: ['scenarioOptions'] })
      if (generation === deleteSetGeneration && deleteKnowledgeSetTarget.value?.key === targetKey) {
        const trigger = deleteKnowledgeSetTrigger
        deleteKnowledgeSetTarget.value = null
        deleteKnowledgeSetTrigger = null
        restoreDeleteTrigger(trigger)
      }
    } catch (err: unknown) {
      if (
        generation === deleteSetGeneration &&
        isCurrentKnowledgeVisit(targetKey, visitGeneration)
      ) {
        uiStore.showToast(`删除资料库失败: ${formatError(err)}`, 'danger')
      }
    } finally {
      if (generation === deleteSetGeneration) isDeletingKnowledgeSet.value = false
    }
  }

  const createNewKnowledgeFile = () => {
    const key = selectedKnowledgeKey.value
    const directory = selectedKnowledgeDirectory.value
    const files = knowledgeFiles.value
    if (!key || !confirmDiscardKnowledgeChanges()) return
    setKnowledgeFileDraft(null, nextKnowledgeFilePath(directory, files))
  }

  const triggerKnowledgeUpload = () => {
    knowledgeUploadInput.value?.click()
  }

  const handleKnowledgeUpload = async (event: Event) => {
    const input = event.target as HTMLInputElement
    const file = input.files?.[0]
    input.value = ''
    const targetKey = selectedKnowledgeKey.value
    const targetDirectory = selectedKnowledgeDirectory.value
    if (!file || !targetKey || !confirmDiscardKnowledgeChanges()) return

    // 上传目标必须在读取本地文件前固定，避免首个 await 后写入新选择的资料库。
    const targetPath = knowledgeFilePathInDirectory(targetDirectory, file.name)
    const targetSelectedPath = selectedKnowledgeFilePath.value
    const visitGeneration = knowledgeSetVisitGeneration
    const draftGeneration = knowledgeDraftGeneration
    const generation = ++uploadGeneration
    try {
      isUploadingKnowledgeFile.value = true
      const text = await readUtf8File(file)
      await writeKnowledgeFile(targetKey, targetPath, text)
      await refetchKnowledgeSets()
      void queryClient.invalidateQueries({ queryKey: ['knowledgeFiles', targetKey] })
      void queryClient.invalidateQueries({ queryKey: ['knowledgeRevisions', targetKey] })
      if (
        generation === uploadGeneration &&
        isCurrentKnowledgeVisit(targetKey, visitGeneration) &&
        selectedKnowledgeDirectory.value === targetDirectory &&
        selectedKnowledgeFilePath.value === targetSelectedPath &&
        knowledgeDraftGeneration === draftGeneration
      ) {
        setKnowledgeFileDraft({
          name: file.name,
          path: targetPath,
          size: new TextEncoder().encode(text).byteLength,
          updated_at: null,
          warnings: [],
          content: text,
        })
        uiStore.showToast('文件已上传', 'success')
      }
    } catch (err: unknown) {
      if (
        generation === uploadGeneration &&
        isCurrentKnowledgeVisit(targetKey, visitGeneration) &&
        selectedKnowledgeDirectory.value === targetDirectory &&
        selectedKnowledgeFilePath.value === targetSelectedPath &&
        knowledgeDraftGeneration === draftGeneration
      ) {
        uiStore.showToast(`上传文件失败: ${formatError(err)}`, 'danger')
      }
    } finally {
      if (generation === uploadGeneration) isUploadingKnowledgeFile.value = false
    }
  }

  const saveKnowledgeFile = async () => {
    const targetKey = selectedKnowledgeKey.value
    const targetSelectedPath = selectedKnowledgeFilePath.value
    const targetPath = knowledgeFileDraftPath.value.trim()
    const targetContent = knowledgeFileContent.value
    if (!targetKey) return
    if (!targetPath) {
      uiStore.showToast('文件路径不能为空', 'danger')
      return
    }

    const visitGeneration = knowledgeSetVisitGeneration
    const generation = ++saveGeneration
    const acceptedSnapshot = JSON.stringify({ path: targetPath, content: targetContent })
    try {
      isSavingKnowledgeFile.value = true
      const saved = await writeKnowledgeFile(targetKey, targetPath, targetContent)
      await refetchKnowledgeSets()
      void queryClient.invalidateQueries({ queryKey: ['knowledgeFiles', targetKey] })
      void queryClient.invalidateQueries({ queryKey: ['knowledgeRevisions', targetKey] })
      void queryClient.invalidateQueries({ queryKey: ['scenarioOptions'] })
      if (
        generation === saveGeneration &&
        isCurrentKnowledgeVisit(targetKey, visitGeneration) &&
        selectedKnowledgeFilePath.value === targetSelectedPath
      ) {
        selectedKnowledgeFilePath.value = saved.path
        knowledgeFileSavedSnapshot.value = acceptedSnapshot
        if (knowledgeFileSnapshot() === acceptedSnapshot) setKnowledgeFileDraft(saved)
        uiStore.showToast('资料库文件已保存', 'success')
      }
    } catch (err: unknown) {
      if (
        generation === saveGeneration &&
        isCurrentKnowledgeVisit(targetKey, visitGeneration) &&
        selectedKnowledgeFilePath.value === targetSelectedPath
      ) {
        uiStore.showToast(`保存资料库文件失败: ${formatError(err)}`, 'danger')
      }
    } finally {
      if (generation === saveGeneration) isSavingKnowledgeFile.value = false
    }
  }

  const openDeleteKnowledgeFile = (trigger: HTMLElement | null = null) => {
    const key = selectedKnowledgeKey.value
    const path = selectedKnowledgeFilePath.value
    if (!key || !path || isDeletingKnowledgeFile.value) return
    deleteKnowledgeFileTarget.value = { key, path }
    deleteKnowledgeFileTrigger = trigger
  }

  const handleDeleteKnowledgeFileOpenChange = (open: boolean) => {
    if (open || isDeletingKnowledgeFile.value) return
    const trigger = deleteKnowledgeFileTrigger
    deleteKnowledgeFileTarget.value = null
    deleteKnowledgeFileTrigger = null
    restoreDeleteTrigger(trigger)
  }

  const handleDeleteKnowledgeFile = async () => {
    const target = deleteKnowledgeFileTarget.value
    if (!target || isDeletingKnowledgeFile.value) return
    const { key: targetKey, path: targetPath } = target
    const visitGeneration = knowledgeSetVisitGeneration
    const draftGeneration = knowledgeDraftGeneration
    const generation = ++deleteFileGeneration
    try {
      isDeletingKnowledgeFile.value = true
      await deleteKnowledgeFile(targetKey, targetPath)
      await refetchKnowledgeSets()
      void queryClient.invalidateQueries({ queryKey: ['knowledgeFiles', targetKey] })
      void queryClient.invalidateQueries({ queryKey: ['knowledgeRevisions', targetKey] })
      if (
        generation === deleteFileGeneration &&
        isCurrentKnowledgeVisit(targetKey, visitGeneration) &&
        selectedKnowledgeFilePath.value === targetPath
      ) {
        if (knowledgeDraftGeneration === draftGeneration) clearKnowledgeFileDraft()
        uiStore.showToast('资料库文件已删除', 'success')
      }
      if (
        generation === deleteFileGeneration &&
        deleteKnowledgeFileTarget.value?.key === targetKey &&
        deleteKnowledgeFileTarget.value.path === targetPath
      ) {
        const trigger = deleteKnowledgeFileTrigger
        deleteKnowledgeFileTarget.value = null
        deleteKnowledgeFileTrigger = null
        restoreDeleteTrigger(trigger)
      }
    } catch (err: unknown) {
      if (
        generation === deleteFileGeneration &&
        isCurrentKnowledgeVisit(targetKey, visitGeneration) &&
        selectedKnowledgeFilePath.value === targetPath
      ) {
        uiStore.showToast(`删除资料库文件失败: ${formatError(err)}`, 'danger')
      }
    } finally {
      if (generation === deleteFileGeneration) isDeletingKnowledgeFile.value = false
    }
  }

  watch(
    () => uiStore.globalRefreshKey,
    () => {
      void queryClient.invalidateQueries({ queryKey: ['knowledgeSets'] })
      void queryClient.invalidateQueries({ queryKey: ['knowledgeFiles'] })
      void queryClient.invalidateQueries({ queryKey: ['knowledgeRevisions'] })
    },
  )

  return {
    formatError,
    canDeleteSelectedKnowledgeSet,
    clearKnowledgeFileDraft,
    currentKnowledgeRevision,
    confirmDiscardKnowledgeChanges,
    createNewKnowledgeFile,
    deleteKnowledgeFileTarget,
    deleteKnowledgeSetTarget,
    handleCreateKnowledgeSet,
    handleDeleteKnowledgeFile,
    handleDeleteKnowledgeFileOpenChange,
    handleDeleteKnowledgeSet,
    handleDeleteKnowledgeSetOpenChange,
    handleKnowledgeUpload,
    isCreatingKnowledgeSet,
    isDeletingKnowledgeFile,
    isDeletingKnowledgeSet,
    isKnowledgeFileDirty,
    isKnowledgeFilesError,
    isKnowledgeSetsError,
    isLoadingKnowledgeFile,
    isLoadingKnowledgeFiles,
    isLoadingKnowledgeRevisions,
    isLoadingKnowledgeSets,
    isSavingKnowledgeFile,
    isUploadingKnowledgeFile,
    knowledgeFileContent,
    knowledgeDirectoryGroups,
    knowledgeFileDraftPath,
    knowledgeFileName,
    knowledgeFilesInSelectedDirectory,
    knowledgeFiles,
    knowledgeFilesData,
    knowledgeFilesError,
    knowledgeRevisions,
    knowledgeFileSavedSnapshot,
    knowledgeFileSnapshot,
    knowledgeSets,
    knowledgeSetsData,
    knowledgeSetsError,
    knowledgeUploadInput,
    newKnowledgeSetKey,
    openDeleteKnowledgeFile,
    openDeleteKnowledgeSet,
    refetchKnowledgeFiles,
    refetchKnowledgeSets,
    resetKnowledgeFileDraft,
    saveKnowledgeFile,
    selectKnowledgeFile,
    selectKnowledgeDirectory,
    selectKnowledgeSet,
    selectedKnowledgeDirectory,
    selectedKnowledgeDirectoryLabel,
    selectedKnowledgeDirectoryGroup,
    selectedKnowledgeFilePath,
    selectedKnowledgeFileSummary,
    selectedKnowledgeKey,
    selectedKnowledgeReferencedBy,
    selectedKnowledgeSet,
    setKnowledgeFileDraft,
    triggerKnowledgeUpload,
  }
}
