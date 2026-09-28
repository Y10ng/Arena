import type { PromptFile, PromptGroup } from '@/api/types'

const PROMPT_FILE_CATEGORIES = [
  { key: 'reasoning', label: '推理', files: ['reason.md'] },
  { key: 'exploration', label: '探索', files: ['explore.md', 'explore_conclude.md'] },
  { key: 'bootstrap', label: '启动', files: ['bootstrap.md', 'bootstrap_conclude.md'] },
  { key: 'reporting', label: '报告', files: ['report.md'] },
  { key: 'verification', label: '验证', files: ['verifier.md', 'verifier_conclude.md'] },
  { key: 'shared', label: '共享片段', files: ['_output_lang.md'] },
] as const

export const promptGroupCapabilities = (group: PromptGroup | null) => ({
  canEdit: Boolean(group?.managed),
  canDelete: Boolean(group?.managed && !group.overrides_builtin && group.referenced_by.length === 0),
  canRestore: Boolean(group?.overrides_builtin),
  canRepair: Boolean(group?.managed && !group.valid),
})

export const buildPromptFileCategories = (files: PromptFile[]) => {
  const byPath = new Map(files.map(file => [file.path, file]))
  const known = new Set<string>()
  const categories: Array<{ key: string; label: string; files: PromptFile[] }> = PROMPT_FILE_CATEGORIES.map(category => {
    const entries = category.files.flatMap(path => {
      known.add(path)
      const file = byPath.get(path)
      return file ? [file] : []
    })
    return { key: category.key, label: category.label, files: entries }
  }).filter(category => category.files.length > 0)
  const other = files.filter(file => !known.has(file.path))
  if (other.length) categories.push({ key: 'other', label: '其他', files: other })
  return categories
}

export const createPromptFileSnapshot = (name: string | null, content: string) => {
  return JSON.stringify({ name, content })
}

export const isPromptFileSnapshotDirty = (
  selectedFileName: string | null,
  currentSnapshot: string,
  savedSnapshot: string,
) => {
  return Boolean(selectedFileName && currentSnapshot !== savedSnapshot)
}
