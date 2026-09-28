import { computed, ref } from 'vue'
import {
  cancelProjectResourceBatch,
  createProjectResourceBatch,
  deleteProjectResourceBatchMember,
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
  | 'updating-member'
  | 'deleting-member'
  | 'cancelling'

export const useProjectResourceBatch = (
  initialBatch: ProjectResourceBatch | null = null,
  onBatchChange?: (batch: ProjectResourceBatch | null) => void,
) => {
  const config = ref<ProjectResourceConfig | null>(null)
  const batch = ref<ProjectResourceBatch | null>(initialBatch)
  const operation = ref<ProjectResourceBatchOperation>('')
  const busy = computed(() => operation.value !== '')

  const setBatch = (nextBatch: ProjectResourceBatch | null) => {
    batch.value = nextBatch
    onBatchChange?.(nextBatch)
  }

  const loadConfig = async () => {
    if (config.value) return config.value
    operation.value = 'loading-config'
    try {
      config.value = await getProjectResourceConfig()
      return config.value
    } finally {
      operation.value = ''
    }
  }

  const ensureBatch = async () => {
    if (batch.value) return batch.value
    const created = await createProjectResourceBatch()
    setBatch(created)
    return created
  }

  const uploadFiles = async (
    files: File[],
    existingCount = 0,
    existingBytes = 0,
    existingPaths: string[] = [],
  ) => {
    operation.value = 'uploading-files'
    try {
      const activeConfig = config.value || (await getProjectResourceConfig())
      config.value = activeConfig
      const current = await ensureBatch()
      const knownPaths = new Set([
        ...existingPaths,
        ...current.members.map((member) => member.path),
      ])
      let nextCount = existingCount + current.resource_count
      let nextBytes = existingBytes + current.total_bytes

      for (const file of files) {
        const path = projectResourceFilePath(file)
        const error = projectResourceFileError(file, path, activeConfig)
        if (error) throw new Error(error)
        if (knownPaths.has(path)) throw new Error(`资源路径重复：${path}`)
        nextCount += 1
        nextBytes += file.size
        if (nextCount > activeConfig.limits.max_active_count) {
          throw new Error(`活动资源不能超过 ${activeConfig.limits.max_active_count} 个`)
        }
        if (nextBytes > activeConfig.limits.max_active_total_bytes) {
          throw new Error('活动资源总大小超过服务器配置限制')
        }
        const member = await uploadProjectResourceBatchFile(current.id, file, path)
        knownPaths.add(member.path)
        setBatch({
          ...batch.value!,
          resource_count: batch.value!.resource_count + 1,
          total_bytes: batch.value!.total_bytes + member.size_bytes,
          members: [...batch.value!.members, member],
        })
      }
    } finally {
      operation.value = ''
    }
  }

  const importZip = async (file: File) => {
    operation.value = 'importing-zip'
    try {
      const activeConfig = config.value || (await getProjectResourceConfig())
      config.value = activeConfig
      if (file.size > activeConfig.limits.max_zip_upload_bytes) {
        throw new Error('ZIP 上传大小超过服务器配置限制')
      }
      const current = await ensureBatch()
      setBatch(await importProjectResourceBatchZip(current.id, file))
    } finally {
      operation.value = ''
    }
  }

  const updateMember = async (
    member: ProjectResourceBatchMember,
    changes: UpdateProjectResourceBatchMemberRequest,
  ) => {
    if (!batch.value) return
    const nextPath = changes.path?.trim()
    if (nextPath !== undefined) {
      const error = projectResourcePathError(nextPath, config.value)
      if (error) throw new Error(error)
    }
    operation.value = 'updating-member'
    try {
      const updated = await updateProjectResourceBatchMember(batch.value.id, member.id, {
        ...changes,
        ...(nextPath !== undefined ? { path: nextPath } : {}),
      })
      setBatch({
        ...batch.value,
        members: batch.value.members.map((item) => (item.id === updated.id ? updated : item)),
      })
    } finally {
      operation.value = ''
    }
  }

  const deleteMember = async (member: ProjectResourceBatchMember) => {
    if (!batch.value) return
    operation.value = 'deleting-member'
    try {
      await deleteProjectResourceBatchMember(batch.value.id, member.id)
      setBatch({
        ...batch.value,
        resource_count: batch.value.resource_count - 1,
        total_bytes: batch.value.total_bytes - member.size_bytes,
        members: batch.value.members.filter((item) => item.id !== member.id),
      })
    } finally {
      operation.value = ''
    }
  }

  const cancel = async () => {
    if (!batch.value) return
    operation.value = 'cancelling'
    try {
      await cancelProjectResourceBatch(batch.value.id)
      setBatch(null)
    } finally {
      operation.value = ''
    }
  }

  return {
    config,
    batch,
    operation,
    busy,
    loadConfig,
    uploadFiles,
    importZip,
    updateMember,
    deleteMember,
    cancel,
  }
}
