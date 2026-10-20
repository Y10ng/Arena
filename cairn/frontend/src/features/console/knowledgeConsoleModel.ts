import type { KnowledgeFile } from '@/api/types'

export const ROOT_KNOWLEDGE_DIRECTORY = ''

export interface KnowledgeDirectoryGroup {
  directory: string
  label: string
  depth: number
  files: KnowledgeFile[]
  totalSize: number
}

export interface KnowledgeNavigationEntry {
  kind: 'directory' | 'file'
  path: string
  name: string
  depth: number
  expanded?: boolean
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
    const segments = directory.split('/').filter(Boolean)
    for (let index = 1; index <= segments.length; index++) {
      ensureGroup(segments.slice(0, index).join('/'))
    }
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

export const buildKnowledgeNavigation = (
  files: KnowledgeFile[],
  expandedDirectories: ReadonlySet<string>,
  search = '',
): KnowledgeNavigationEntry[] => {
  const query = search.trim().toLocaleLowerCase()
  const visibleFiles = query
    ? files.filter((file) => file.path.toLocaleLowerCase().includes(query))
    : files
  const groups = buildKnowledgeDirectoryGroups(visibleFiles)
  const children = new Map<string, KnowledgeDirectoryGroup[]>()
  for (const group of groups) {
    if (!group.directory) continue
    const parent = knowledgeFileDirectory(group.directory)
    const siblings = children.get(parent) || []
    siblings.push(group)
    children.set(parent, siblings)
  }

  const entries: KnowledgeNavigationEntry[] = []
  const visit = (directory: string, depth: number) => {
    for (const child of children.get(directory) || []) {
      const expanded = Boolean(query) || expandedDirectories.has(child.directory)
      entries.push({
        kind: 'directory',
        path: child.directory,
        name: knowledgeFileName(child.directory),
        depth,
        expanded,
      })
      if (expanded) visit(child.directory, depth + 1)
    }
    for (const file of groups.find((group) => group.directory === directory)?.files || []) {
      entries.push({ kind: 'file', path: file.path, name: knowledgeFileName(file.path), depth })
    }
  }
  visit(ROOT_KNOWLEDGE_DIRECTORY, 0)
  return entries
}

export const nextKnowledgeFilePath = (directory: string, files: KnowledgeFile[] = []) => {
  const existingPaths = new Set(files.map((file) => file.path))
  const candidate = knowledgeFilePathInDirectory(directory, 'README.md')
  if (!existingPaths.has(candidate)) return candidate

  let index = 2
  while (existingPaths.has(knowledgeFilePathInDirectory(directory, `README-${index}.md`))) {
    index++
  }
  return knowledgeFilePathInDirectory(directory, `README-${index}.md`)
}
