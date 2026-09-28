import type { KnowledgeFile } from '@/api/types'

export const ROOT_KNOWLEDGE_DIRECTORY = ''

export interface KnowledgeDirectoryGroup {
  directory: string;
  label: string;
  depth: number;
  files: KnowledgeFile[];
  totalSize: number;
}

const cleanPath = (path: string) => path.trim().replace(/^\/+|\/+$/g, '')

export const knowledgeFileDirectory = (path: string) => {
  const normalized = cleanPath(path)
  const separator = normalized.lastIndexOf('/')
  return separator === -1 ? ROOT_KNOWLEDGE_DIRECTORY : normalized.slice(0, separator)
}

export const knowledgeFileName = (path: string) => {
  const normalized = cleanPath(path)
  const separator = normalized.lastIndexOf('/')
  return separator === -1 ? normalized : normalized.slice(separator + 1)
}

export const knowledgeDirectoryLabel = (directory: string) => directory || '根目录'

export const knowledgeFilePathInDirectory = (directory: string, fileName: string) => {
  const normalizedDirectory = cleanPath(directory)
  const normalizedFileName = cleanPath(fileName)
  return normalizedDirectory ? `${normalizedDirectory}/${normalizedFileName}` : normalizedFileName
}

export const buildKnowledgeDirectoryGroups = (files: KnowledgeFile[] = []) => {
  const groups = new Map<string, KnowledgeDirectoryGroup>()
  const ensureGroup = (directory: string) => {
    const normalizedDirectory = cleanPath(directory)
    const existing = groups.get(normalizedDirectory)
    if (existing) return existing
    const group: KnowledgeDirectoryGroup = {
      directory: normalizedDirectory,
      label: knowledgeDirectoryLabel(normalizedDirectory),
      depth: normalizedDirectory ? normalizedDirectory.split('/').length - 1 : 0,
      files: [],
      totalSize: 0,
    }
    groups.set(normalizedDirectory, group)
    return group
  }

  ensureGroup(ROOT_KNOWLEDGE_DIRECTORY)
  for (const file of files) {
    const directory = knowledgeFileDirectory(file.path)
    const group = ensureGroup(directory)
    group.files.push(file)
    group.totalSize += file.size
  }

  return Array.from(groups.values()).sort((a, b) => {
    if (a.directory === ROOT_KNOWLEDGE_DIRECTORY) return -1
    if (b.directory === ROOT_KNOWLEDGE_DIRECTORY) return 1
    return a.directory.localeCompare(b.directory)
  })
}

export const nextKnowledgeFilePath = (directory: string, files: KnowledgeFile[] = []) => {
  const existingPaths = new Set(files.map(file => file.path))
  const candidate = knowledgeFilePathInDirectory(directory, 'README.md')
  if (!existingPaths.has(candidate)) return candidate

  let index = 2
  while (existingPaths.has(knowledgeFilePathInDirectory(directory, `README-${index}.md`))) {
    index++
  }
  return knowledgeFilePathInDirectory(directory, `README-${index}.md`)
}
