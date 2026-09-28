const EXTENSION_LANGUAGE: Record<string, string> = {
  bash: 'shell',
  c: 'c',
  cc: 'cpp',
  conf: 'ini',
  cpp: 'cpp',
  cs: 'csharp',
  css: 'css',
  csv: 'plaintext',
  diff: 'diff',
  dockerfile: 'dockerfile',
  env: 'ini',
  go: 'go',
  h: 'cpp',
  html: 'html',
  ini: 'ini',
  java: 'java',
  js: 'javascript',
  json: 'json',
  jsonl: 'json',
  jsx: 'javascript',
  log: 'plaintext',
  lua: 'lua',
  md: 'markdown',
  markdown: 'markdown',
  mjs: 'javascript',
  patch: 'diff',
  php: 'php',
  py: 'python',
  rb: 'ruby',
  rs: 'rust',
  sh: 'shell',
  sql: 'sql',
  toml: 'ini',
  ts: 'typescript',
  tsx: 'typescript',
  txt: 'plaintext',
  xml: 'xml',
  yaml: 'yaml',
  yml: 'yaml',
}

const SPECIAL_FILE_LANGUAGE: Record<string, string> = {
  dockerfile: 'dockerfile',
  makefile: 'shell',
}

export const fileNameFromUri = (pathOrUri = '') => {
  const withoutHash = pathOrUri.split('#')[0]?.split('?')[0] || ''
  const fileName = withoutHash.slice(Math.max(withoutHash.lastIndexOf('/'), withoutHash.lastIndexOf('\\')) + 1)
  try {
    return decodeURIComponent(fileName)
  } catch {
    return fileName
  }
}

export const fileExtension = (pathOrUri = '') => {
  const fileName = fileNameFromUri(pathOrUri).toLowerCase()
  const dotIndex = fileName.lastIndexOf('.')
  if (dotIndex <= 0 || dotIndex === fileName.length - 1) return ''
  return fileName.slice(dotIndex + 1)
}

export const resolveFileLanguage = (pathOrUri = '', artifactKind = '') => {
  const fileName = fileNameFromUri(pathOrUri).toLowerCase()
  const special = SPECIAL_FILE_LANGUAGE[fileName]
  if (special) return special

  const ext = fileExtension(pathOrUri)
  if (ext && EXTENSION_LANGUAGE[ext]) return EXTENSION_LANGUAGE[ext]

  if (artifactKind === 'patch') return 'diff'
  if (artifactKind === 'har') return 'json'
  if (artifactKind === 'raw_output' || artifactKind === 'log' || artifactKind === 'hook_log') return 'plaintext'
  if (artifactKind === 'report') return 'markdown'

  return 'plaintext'
}

export const isMarkdownFile = (pathOrUri = '', artifactKind = '') => {
  return resolveFileLanguage(pathOrUri, artifactKind) === 'markdown'
}

export const languageDisplayName = (language = 'plaintext') => {
  const labels: Record<string, string> = {
    c: 'C',
    cpp: 'C++',
    csharp: 'C#',
    css: 'CSS',
    diff: 'Diff',
    dockerfile: 'Dockerfile',
    go: 'Go',
    html: 'HTML',
    ini: 'INI',
    java: 'Java',
    javascript: 'JavaScript',
    json: 'JSON',
    lua: 'Lua',
    markdown: 'Markdown',
    php: 'PHP',
    plaintext: 'Text',
    python: 'Python',
    ruby: 'Ruby',
    rust: 'Rust',
    shell: 'Shell',
    sql: 'SQL',
    typescript: 'TypeScript',
    xml: 'XML',
    yaml: 'YAML',
  }
  return labels[language] || language
}
