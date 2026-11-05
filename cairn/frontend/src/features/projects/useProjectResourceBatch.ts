import { computed, ref } from 'vue'
import {
  ApiError,
  cancelProjectResourceBatch,
  createProjectResourceBatch,
  deleteProjectResourceBatchMember,
  getProjectResourceBatch,
  getProjectResourceConfig,
  importProjectResourceBatchZip,
  updateProjectResourceBatchMember,
  uploadProjectResourceBatchFile,
} from '@/api/client'
import type {
  ProjectResourceBatch,
  ProjectResourceBatchMember,
  ProjectResourceConfig,
  UpdateProjectResourceBatchMemberRequest,
} from '@/api/types'
import {
  projectResourceFileError,
  projectResourceFilePath,
  projectResourcePathError,
} from './projectResources'

export type ProjectResourceBatchOperation =
  | ''
  | 'loading-config'
  | 'uploading-files'
  | 'importing-zip'
  | 'checking-batch'
  | 'updating-member'
  | 'deleting-member'
  | 'cancelling'

export type PendingResourceUpload = {
  id: number
  uploadId: string
  file: File
  path: string
  mode: 'file' | 'zip'
  state: 'waiting' | 'uploading' | 'failed' | 'unknown'
  error: string
}

const explicitlyRejected = (error: unknown) =>
  error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408

export const useProjectResourceBatch = (
  initialBatch: ProjectResourceBatch | null = null,
  onBatchChange?: (batch: ProjectResourceBatch | null) => void,
) => {
  const config = ref<ProjectResourceConfig | null>(null)
  const batch = ref<ProjectResourceBatch | null>(initialBatch)
  const operation = ref<ProjectResourceBatchOperation>('')
  const uploads = ref<PendingResourceUpload[]>([])
  const oversized = ref<{ name: string; limit: number; member: boolean }[]>([])
  const sizeRejected = (file: File, mode: 'file' | 'zip', limits: ProjectResourceConfig['limits']) => {
    const limit = mode === 'zip' ? limits.max_zip_upload_bytes : limits.max_file_bytes
    if (file.size <= limit) return false
    oversized.value.push({ name: file.name, limit, member: false })
    return true
  }
  const batchCreationUnknown = ref(false)
  const unresolvedMemberChange = ref<{
    id: string
    changes: UpdateProjectResourceBatchMemberRequest | null
  } | null>(null)
  const busy = computed(() => operation.value !== '')
  const pending = computed(
    () =>
      uploads.value.length > 0 ||
      batchCreationUnknown.value ||
      Boolean(unresolvedMemberChange.value),
  )
  const hasUnknown = computed(
    () =>
      batchCreationUnknown.value ||
      Boolean(unresolvedMemberChange.value) ||
      uploads.value.some((item) => item.state === 'unknown'),
  )
  let nextUploadId = 0

  const setBatch = (nextBatch: ProjectResourceBatch | null) => {
    batch.value = nextBatch
    onBatchChange?.(nextBatch)
  }

  const syncBatch = (nextBatch: ProjectResourceBatch | null) => {
    if (nextBatch === batch.value) return
    batch.value = nextBatch
    if (!nextBatch) {
      uploads.value = []
      batchCreationUnknown.value = false
      unresolvedMemberChange.value = null
    }
  }

  const loadConfig = async () => {
    if (config.value) return config.value
    if (busy.value) return
    operation.value = 'loading-config'
    try {
      config.value = await getProjectResourceConfig()
      return config.value
    } finally {
      operation.value = ''
    }
  }

  const ensureBatch = async () => {
    if (batch.value) {
      if (batch.value.state !== 'open') throw new Error('临时批次已被消费，不能继续修改')
      return batch.value
    }
    if (batchCreationUnknown.value) throw new Error('临时批次创建结果未确认，不能再次创建批次')
    try {
      const created = await createProjectResourceBatch()
      setBatch(created)
      return created
    } catch (error) {
      if (!explicitlyRejected(error)) batchCreationUnknown.value = true
      throw error
    }
  }

  const addUpload = (file: File, mode: 'file' | 'zip') => {
    const item: PendingResourceUpload = {
      id: ++nextUploadId,
      uploadId: `prm_${crypto.randomUUID().replaceAll('-', '')}`,
      file,
      path: projectResourceFilePath(file),
      mode,
      state: 'waiting',
      error: '',
    }
    uploads.value.push(item)
    return uploads.value[uploads.value.length - 1]!
  }

  const runUpload = async (
    item: PendingResourceUpload,
    existingCount: number,
    existingBytes: number,
    existingPaths: string[],
  ) => {
    item.state = 'uploading'
    item.error = ''
    let requestSent = false
    try {
      const activeConfig = config.value || (await getProjectResourceConfig())
      config.value = activeConfig
      if (sizeRejected(item.file, item.mode, activeConfig.limits)) {
        uploads.value = uploads.value.filter((entry) => entry.id !== item.id)
        return
      }
      if (item.mode !== 'zip') {
        const error = projectResourceFileError(item.file, item.path, activeConfig)
        if (error) throw new Error(error)
        if (
          existingPaths.includes(item.path) ||
          batch.value?.members.some((member) => member.path === item.path)
        ) {
          throw new Error(`资源路径重复：${item.path}`)
        }
        if (
          existingCount + (batch.value?.resource_count || 0) + 1 >
          activeConfig.limits.max_active_count
        ) {
          throw new Error(`活动资源不能超过 ${activeConfig.limits.max_active_count} 个`)
        }
        if (
          existingBytes + (batch.value?.total_bytes || 0) + item.file.size >
          activeConfig.limits.max_active_total_bytes
        ) {
          throw new Error('活动资源总大小超过服务器配置限制')
        }
      }
      const current = await ensureBatch()
      requestSent = true
      if (item.mode === 'zip') {
        setBatch(await importProjectResourceBatchZip(current.id, item.file))
      } else {
        const member = await uploadProjectResourceBatchFile(current.id, item.file, item.path, undefined, undefined, item.uploadId)
        setBatch({
          ...batch.value!,
          resource_count: batch.value!.resource_count + 1,
          total_bytes: batch.value!.total_bytes + member.size_bytes,
          members: [...batch.value!.members, member],
        })
      }
      uploads.value = uploads.value.filter((entry) => entry.id !== item.id)
    } catch (error) {
      const detail = error instanceof ApiError
        ? (error.data as { detail?: { code?: string; limit_bytes: number; category: string; path?: string } })?.detail
        : undefined
      if (error instanceof ApiError && error.status === 413 && detail?.code === 'project_resource_file_too_large') {
        oversized.value.push({ name: detail.path || item.file.name, limit: detail.limit_bytes, member: detail.category === 'zip_entry' })
        uploads.value = uploads.value.filter((entry) => entry.id !== item.id)
        return
      }
      const unknown = batchCreationUnknown.value || (requestSent && !explicitlyRejected(error))
      item.state = unknown ? 'unknown' : 'failed'
      item.error = error instanceof Error ? error.message : '资料操作失败'
      if (unknown) {
        item.error = `结果未确认：${item.error}。请先核对批次，不要重传。`
      }
      throw error
    }
  }

  const uploadFiles = async (
    files: File[],
    existingCount = 0,
    existingBytes = 0,
    existingPaths: string[] = [],
  ) => {
    if (busy.value || hasUnknown.value || !files.length) return
    oversized.value = []
    operation.value = 'uploading-files'
    try {
      const activeConfig = config.value || (await getProjectResourceConfig())
      config.value = activeConfig
      // 整批预检后才入队，拒绝项不会阻塞创建或清掉其他 File。
      const entries = files.filter((file) => !sizeRejected(file, 'file', activeConfig.limits))
        .map((file) => addUpload(file, 'file'))
      for (const item of entries) await runUpload(item, existingCount, existingBytes, existingPaths)
    } finally {
      operation.value = ''
    }
  }

  const importZip = async (file: File) => {
    if (busy.value || hasUnknown.value) return
    oversized.value = []
    operation.value = 'importing-zip'
    try {
      const activeConfig = config.value || (await getProjectResourceConfig())
      config.value = activeConfig
      if (sizeRejected(file, 'zip', activeConfig.limits)) return
      await runUpload(addUpload(file, 'zip'), 0, 0, [])
    } finally {
      operation.value = ''
    }
  }

  const retryUpload = async (
    item: PendingResourceUpload,
    existingCount = 0,
    existingBytes = 0,
    existingPaths: string[] = [],
  ) => {
    if (busy.value || hasUnknown.value || !['waiting', 'failed'].includes(item.state)) return
    oversized.value = []
    operation.value = item.mode === 'zip' ? 'importing-zip' : 'uploading-files'
    try {
      await runUpload(item, existingCount, existingBytes, existingPaths)
    } finally {
      operation.value = ''
    }
  }

  const removeUpload = (item: PendingResourceUpload) => {
    if (busy.value || item.state === 'unknown') return
    uploads.value = uploads.value.filter((entry) => entry.id !== item.id)
  }

  const checkBatch = async () => {
    if (busy.value) return
    if (!batch.value) throw new Error('未收到批次标识，无法核对或清理未知批次；请保留当前上下文')
    operation.value = 'checking-batch'
    try {
      const current = await getProjectResourceBatch(batch.value.id)
      setBatch(current)
      if (current.state !== 'open')
        throw new Error('批次已被消费，不能重传或取消；请核实项目创建结果')
      for (const item of uploads.value.filter(
        (entry) => entry.state === 'unknown' && entry.mode === 'file',
      )) {
        const member = current.members.find(
          (entry) =>
            entry.id === item.uploadId &&
            entry.path === item.path &&
            entry.source === 'file' &&
            entry.size_bytes === item.file.size,
        )
        // 随机请求身份经原批次的租户／所有者鉴权返回，摘要由接收端对实际字节计算。
        // 不按名称猜成功，也不再次把整个本地 File 读入内存。
        if (member)
          uploads.value = uploads.value.filter((entry) => entry.id !== item.id)
      }
      const change = unresolvedMemberChange.value
      if (change) {
        const member = current.members.find((entry) => entry.id === change.id)
        if (
          !change.changes
            ? !member
            : member &&
              Object.entries(change.changes).every(
                ([key, value]) => member[key as keyof ProjectResourceBatchMember] === value,
              )
        ) {
          unresolvedMemberChange.value = null
        }
      }
      // 一次 GET 中缺席不证明原请求已结束；ZIP 也无法只凭成员列表确认本次整体结果。
      if (hasUnknown.value)
        throw new Error(
          '批次已核对，但仍有操作结果未确认。禁止重传或提交；可取消整个临时批次后重新准备资料',
        )
    } finally {
      operation.value = ''
    }
  }

  const updateMember = async (
    member: ProjectResourceBatchMember,
    changes: UpdateProjectResourceBatchMemberRequest,
  ) => {
    if (!batch.value || busy.value || hasUnknown.value) return
    const nextPath = changes.path?.trim()
    if (nextPath !== undefined) {
      const error = projectResourcePathError(nextPath, config.value)
      if (error) throw new Error(error)
    }
    const request = { ...changes, ...(nextPath !== undefined ? { path: nextPath } : {}) }
    operation.value = 'updating-member'
    try {
      const updated = await updateProjectResourceBatchMember(batch.value.id, member.id, request)
      setBatch({
        ...batch.value,
        members: batch.value.members.map((item) => (item.id === updated.id ? updated : item)),
      })
    } catch (error) {
      if (!explicitlyRejected(error))
        unresolvedMemberChange.value = { id: member.id, changes: request }
      throw error
    } finally {
      operation.value = ''
    }
  }

  const deleteMember = async (member: ProjectResourceBatchMember) => {
    if (!batch.value || busy.value || hasUnknown.value) return
    operation.value = 'deleting-member'
    try {
      await deleteProjectResourceBatchMember(batch.value.id, member.id)
      setBatch({
        ...batch.value,
        resource_count: batch.value.resource_count - 1,
        total_bytes: batch.value.total_bytes - member.size_bytes,
        members: batch.value.members.filter((item) => item.id !== member.id),
      })
    } catch (error) {
      if (!explicitlyRejected(error))
        unresolvedMemberChange.value = { id: member.id, changes: null }
      throw error
    } finally {
      operation.value = ''
    }
  }

  const cancel = async () => {
    if (busy.value) throw new Error('资料操作尚未完成，不能取消临时批次')
    if (batchCreationUnknown.value)
      throw new Error('临时批次创建结果未确认，缺少批次标识，无法确认清理成功')
    if (batch.value?.state === 'consumed') throw new Error('临时批次已被消费，不能取消')
    operation.value = 'cancelling'
    try {
      if (batch.value) await cancelProjectResourceBatch(batch.value.id)
      setBatch(null)
      uploads.value = []
      unresolvedMemberChange.value = null
    } finally {
      operation.value = ''
    }
  }

  return {
    config,
    batch,
    operation,
    busy,
    uploads,
    oversized,
    pending,
    hasUnknown,
    syncBatch,
    loadConfig,
    uploadFiles,
    importZip,
    retryUpload,
    removeUpload,
    checkBatch,
    updateMember,
    deleteMember,
    cancel,
  }
}
