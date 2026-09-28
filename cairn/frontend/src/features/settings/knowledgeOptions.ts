import type { KnowledgeFile, KnowledgeSet } from '@/api/types'

export const BUILTIN_KNOWLEDGE_KEY = 'knowledge_base'

export interface KnowledgeSelectOption {
  value: string | null;
  label: string;
  exists: boolean;
  builtin: boolean;
  fileCount: number;
  referencedBy: string[];
}

const addKnowledgeSet = (values: Map<string, KnowledgeSet>, set: KnowledgeSet) => {
  const key = set.key.trim()
  if (!key || values.has(key)) return
  values.set(key, {
    ...set,
    key,
    path: set.path || '',
    builtin: set.builtin || key === BUILTIN_KNOWLEDGE_KEY,
    file_count: Number.isFinite(set.file_count) ? set.file_count : 0,
    referenced_by: Array.isArray(set.referenced_by) ? [...set.referenced_by] : [],
  })
}

export const knowledgeSetLabel = (key: string) => {
  return key === BUILTIN_KNOWLEDGE_KEY ? '标准知识库' : key
}

export const normalizeKnowledgeSets = (sets: KnowledgeSet[] = []) => {
  const values = new Map<string, KnowledgeSet>()
  addKnowledgeSet(values, {
    key: BUILTIN_KNOWLEDGE_KEY,
    path: '',
    builtin: true,
    file_count: 0,
    referenced_by: [],
  })
  for (const set of sets) addKnowledgeSet(values, set)
  return Array.from(values.values()).sort((a, b) => {
    if (a.key === BUILTIN_KNOWLEDGE_KEY) return -1
    if (b.key === BUILTIN_KNOWLEDGE_KEY) return 1
    return a.key.localeCompare(b.key)
  })
}

export const buildKnowledgeSelectOptions = ({
  sets,
  selectedKnowledge,
}: {
  sets?: KnowledgeSet[];
  selectedKnowledge?: string | null;
}) => {
  const options: KnowledgeSelectOption[] = [
    {
      value: null,
      label: '无知识库（场景独立运行）',
      exists: true,
      builtin: false,
      fileCount: 0,
      referencedBy: [],
    },
    ...normalizeKnowledgeSets(sets).map(set => ({
      value: set.key,
      label: knowledgeSetLabel(set.key),
      exists: true,
      builtin: set.builtin,
      fileCount: set.file_count,
      referencedBy: [...set.referenced_by],
    })),
  ]

  const selected = selectedKnowledge?.trim()
  if (selected && !options.some(option => option.value === selected)) {
    options.push({
      value: selected,
      label: `${selected} (不存在)`,
      exists: false,
      builtin: false,
      fileCount: 0,
      referencedBy: [],
    })
  }

  return options
}

export const isKnowledgeReferenceValid = (key: string | null | undefined, sets: KnowledgeSet[] = []) => {
  if (key === null) return true
  const text = key?.trim()
  if (!text) return false
  return normalizeKnowledgeSets(sets).some(set => set.key === text)
}

export const normalizeKnowledgeFiles = (files: KnowledgeFile[] = []) => {
  return [...files]
    .filter(file => file.path.trim())
    .sort((a, b) => a.path.localeCompare(b.path))
}
