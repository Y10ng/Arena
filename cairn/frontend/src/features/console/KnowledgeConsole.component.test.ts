import { flushPromises, mount } from '@vue/test-utils'
import { computed, ref, unref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { KnowledgeFile, KnowledgeFileContent, KnowledgeSet } from '@/api/types'
import KnowledgeConsole from './KnowledgeConsole.vue'

const mocks = vi.hoisted(() => ({
  knowledgeSets: [] as KnowledgeSet[],
  knowledgeSetsQueryData: null as { value: KnowledgeSet[] } | null,
  knowledgeFiles: [] as KnowledgeFile[],
  knowledgeFilesQueryData: null as { value: KnowledgeFile[] } | null,
  treeOverrides: {} as Record<string, KnowledgeFile[]>,
  treeStates: {} as Record<string, 'pending' | 'error'>,
  libraryCatalog: [] as Array<{ key: string; name: string }>,
  createKnowledgeSet: vi.fn(),
  deleteKnowledgeFile: vi.fn(),
  deleteKnowledgeSet: vi.fn(),
  listKnowledgeFiles: vi.fn(),
  listKnowledgeSets: vi.fn(),
  listLibraryCatalog: vi.fn(),
  readKnowledgeFile: vi.fn(),
  readUtf8File: vi.fn(),
  writeKnowledgeFile: vi.fn(),
  knowledgeSetsRefetch: vi.fn(),
  knowledgeFilesRefetch: vi.fn(),
  invalidateQueries: vi.fn(),
  router: { replace: vi.fn() },
  route: { query: {} as Record<string, unknown> },
  uiStore: { globalRefreshKey: 0, showToast: vi.fn() },
  beforeRouteLeave: vi.fn(),
  beforeRouteUpdate: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  createKnowledgeSet: mocks.createKnowledgeSet,
  deleteKnowledgeFile: mocks.deleteKnowledgeFile,
  deleteKnowledgeSet: mocks.deleteKnowledgeSet,
  listKnowledgeFiles: mocks.listKnowledgeFiles,
  listKnowledgeSets: mocks.listKnowledgeSets,
  listLibraryCatalog: mocks.listLibraryCatalog,
  readKnowledgeFile: mocks.readKnowledgeFile,
  writeKnowledgeFile: mocks.writeKnowledgeFile,
}))
vi.mock('@/features/projects/projectResources', () => ({ readUtf8File: mocks.readUtf8File }))
vi.mock('@/stores/ui', () => ({ useUIStore: () => mocks.uiStore }))
vi.mock('vue-router', () => ({
  onBeforeRouteLeave: mocks.beforeRouteLeave,
  onBeforeRouteUpdate: mocks.beforeRouteUpdate,
  useRoute: () => mocks.route,
  useRouter: () => mocks.router,
}))
vi.mock('@tanstack/vue-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown }) => {
    const key = unref(queryKey) as string[]
    const data =
      key[0] === 'knowledgeSets'
        ? mocks.knowledgeSets
        : key[0] === 'library-catalog' ? mocks.libraryCatalog : mocks.knowledgeFiles
    const queryData = ref(data)
    if (key[0] === 'knowledgeSets') {
      mocks.knowledgeSetsQueryData = queryData as { value: KnowledgeSet[] }
    } else if (key[0] === 'knowledgeFiles') {
      mocks.knowledgeFilesQueryData = queryData as { value: KnowledgeFile[] }
    }
    return {
      data: queryData,
      isLoading: ref(false),
      isError: ref(false),
      error: ref(null),
      refetch:
        key[0] === 'knowledgeSets'
          ? mocks.knowledgeSetsRefetch
          : mocks.knowledgeFilesRefetch,
    }
  },
  useQueries: ({ queries }: { queries: unknown }) => computed(() =>
    (unref(queries) as Array<{ queryKey: string[] }>).map(({ queryKey }) => ({
      data: mocks.treeOverrides[queryKey[1]] ?? mocks.knowledgeFilesQueryData?.value ?? mocks.knowledgeFiles,
      isPending: mocks.treeStates[queryKey[1]] === 'pending',
      isError: mocks.treeStates[queryKey[1]] === 'error',
      error: mocks.treeStates[queryKey[1]] === 'error' ? new Error('目录不可用') : null,
    })),
  ),
  useQueryClient: () => ({
    invalidateQueries: mocks.invalidateQueries,
    fetchQuery: ({ queryKey }: { queryKey: string[] }) => mocks.listKnowledgeFiles(queryKey[1]),
  }),
}))
vi.mock('@/components/patterns/TextEditor.vue', () => ({
  default: {
    name: 'TextEditor',
    inheritAttrs: false,
    props: ['modelValue', 'fileName', 'disabled'],
    emits: ['update:modelValue'],
    template:
      '<textarea data-testid="knowledge-editor" :value="modelValue" :data-file-name="fileName" :disabled="disabled" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
}))

const knowledgeSets: KnowledgeSet[] = [
  { key: 'set-a', path: 'library://set-a', builtin: false, safety_path: null, file_count: 3, referenced_by: [] },
  { key: 'set-b', path: 'library://set-b', builtin: false, safety_path: null, file_count: 3, referenced_by: [] },
  { key: 'knowledge_base', path: 'library://knowledge_base', builtin: true, safety_path: '执行安全约束.md', file_count: 1, referenced_by: [] },
]
const knowledgeFiles: KnowledgeFile[] = [
  { name: 'a.md', path: 'a.md', size: 1, warnings: [] },
  { name: 'b.txt', path: 'b.txt', size: 1, warnings: [] },
  { name: 'c.md', path: 'nested/deep/c.md', size: 1, warnings: [] },
]
const contents: Record<string, KnowledgeFileContent> = {
  'a.md': {
    ...knowledgeFiles[0],
    content:
      '# A body\n\n正文与 **加粗**\n\n<script>bad()</script>\n\n![远程图](https://example.invalid/image.png)',
  },
  'b.txt': { ...knowledgeFiles[1], content: 'B body\n  原文空格\n# 不是标题' },
  'nested/deep/c.md': { ...knowledgeFiles[2], content: 'C body' },
}
const wrappers: VueWrapper[] = []
const mountPage = () => {
  const wrapper = mount(KnowledgeConsole, { attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}
const buttonByText = (text: string) => {
  const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.includes(text),
  )
  if (!button) throw new Error(`找不到按钮：${text}`)
  return button
}
const editSelectedFile = async () => {
  buttonByText('编辑').click()
  await flushPromises()
  const editor = document.body.querySelector<HTMLTextAreaElement>(
    '[data-testid="knowledge-editor"]',
  )
  if (!editor) throw new Error('找不到资料库编辑器')
  return editor
}
const setEditor = async (editor: HTMLTextAreaElement, content: string) => {
  editor.value = content
  editor.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
}
const runNodeAction = async (action: string) => {
  await flushPromises()
  let row = document.querySelector<HTMLElement>('.knowledge-navigation-row.active')!
  if (action === 'create-library') row = document.querySelector('[data-library-key="knowledge_base"]')!
  else if (!row.querySelector('.knowledge-tree-toggle')) {
    row = document.querySelector(`[data-library-key="${row.dataset.libraryKey}"]`)!
  }
  row.querySelector<HTMLButtonElement>('[data-console-action]')!.click()
  await flushPromises()
  await selectMenuAction(action === 'upload' ? '上传文本' : action === 'create-library' ? '新建资料库' : '新建文件')
}
const expandLibrary = async (key: string) => {
  const root = document.body.querySelector<HTMLElement>(`.knowledge-navigation-row[data-library-key="${key}"]`)
  if (!root) throw new Error(`找不到资料库：${key}`)
  const toggle = root.querySelector<HTMLButtonElement>('.knowledge-tree-toggle')!
  if (toggle.getAttribute('aria-expanded') === 'false') toggle.click()
  await flushPromises()
}
const openFile = async (path = 'a.md', key = 'set-a') => {
  await expandLibrary(key)
  const parts = path.split('/')
  for (let i = 1; i < parts.length; i++) {
    const directory = parts.slice(0, i).join('/')
    const toggle = [...document.body.querySelectorAll<HTMLButtonElement>(`.knowledge-tree-entry[data-library-key="${key}"] .knowledge-tree-toggle`)]
      .find((element) => element.getAttribute('aria-label') === `展开目录 ${directory}`)
    toggle?.click()
    await flushPromises()
  }
  const file = [...document.body.querySelectorAll<HTMLButtonElement>(`.knowledge-navigation-item[data-knowledge-key="${key}"]`)]
    .find((element) => element.dataset.knowledgePath === path)
  if (!file) throw new Error(`找不到文件：${key} / ${path}`)
  file.click()
  await flushPromises()
}
const fileMenuTrigger = (path: string) => {
  const trigger = [...document.body.querySelectorAll<HTMLButtonElement>('[data-knowledge-key="set-a"]')]
    .find((button) => button.dataset.knowledgePath === path)?.parentElement?.querySelector<HTMLButtonElement>('[data-console-action]')
  if (!trigger) throw new Error(`找不到文件菜单：${path}`)
  return trigger
}
const selectMenuAction = async (label: string) => {
  const item = [...document.body.querySelectorAll<HTMLElement>('[role="menuitem"]')].find(
    (element) => element.textContent?.includes(label),
  )
  if (!item) throw new Error(`找不到菜单操作：${label}`)
  item.click()
  await flushPromises()
}
const openFileDeletion = async (path: string) => {
  const trigger = fileMenuTrigger(path)
  trigger.focus()
  trigger.click()
  await flushPromises()
  await selectMenuAction('删除文件')
  return trigger
}
const deferred = <T>() => {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

describe('固定根目录与当前资料交互', () => {
  it('根默认展开，主体切换展开，箭头不改变单一活动节点，读取不抢焦点', async () => {
    const wrapper = mountPage()
    await flushPromises()
    const root = wrapper.find('.knowledge-library-root')
    expect(root.text()).toBe('根目录')
    const library = wrapper.findAll('.knowledge-library-root').find((node) => node.attributes('title') === 'set-a')!
    await library.trigger('click')
    expect(wrapper.findAll('.knowledge-navigation-row.active')).toHaveLength(1)
    const file = wrapper.find('[data-knowledge-key="set-a"][data-knowledge-path="a.md"]')
    ;(file.element as HTMLElement).focus()
    await file.trigger('click')
    await flushPromises()
    expect(document.activeElement).toBe(file.element)
    expect(wrapper.findAll('.knowledge-navigation-row.active')).toHaveLength(1)
    expect(wrapper.find('.knowledge-reader').text()).toContain('A body')
    const arrow = library.element.parentElement!.querySelector<HTMLButtonElement>('.knowledge-tree-toggle')!
    arrow.click()
    await flushPromises()
    expect(arrow.getAttribute('aria-expanded')).toBe('false')
    library.element.parentElement!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flushPromises()
    expect(arrow.getAttribute('aria-expanded')).toBe('true')
    expect(wrapper.findAll('.knowledge-navigation-row.active')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('写入目标：')
    expect(wrapper.text()).not.toContain('文件与版本信息')
  })

  it('右键与三点共用保护动作，Esc 回焦，系统文件不能删除', async () => {
    mocks.treeOverrides.knowledge_base = [{ name: '执行安全约束.md', path: '执行安全约束.md', size: 1, warnings: [] }]
    const wrapper = mountPage()
    await flushPromises()
    const file = wrapper.find('[data-knowledge-key="knowledge_base"][data-knowledge-path="执行安全约束.md"]')
    ;(file.element as HTMLElement).focus()
    await file.trigger('contextmenu', { clientX: 50, clientY: 90 })
    await flushPromises()
    let item = [...document.querySelectorAll<HTMLElement>('[role="menuitem"]')].find((node) => node.textContent?.includes('删除文件'))!
    expect(item.getAttribute('data-disabled')).not.toBeNull()
    expect(item.textContent).toContain('系统文件不可删除')
    item.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    const more = file.element.parentElement!.querySelector<HTMLButtonElement>('[data-console-action]')!
    more.click()
    await flushPromises()
    item = [...document.querySelectorAll<HTMLElement>('[role="menuitem"]')].find((node) => node.textContent?.includes('删除文件'))!
    expect(item.getAttribute('data-disabled')).not.toBeNull()
    expect(mocks.deleteKnowledgeFile).not.toHaveBeenCalled()
  })
})

beforeEach(() => {
  mocks.knowledgeSets = knowledgeSets.map((set) => ({ ...set }))
  mocks.knowledgeSetsQueryData = null
  mocks.knowledgeFiles = knowledgeFiles.map((file) => ({ ...file }))
  mocks.knowledgeFilesQueryData = null
  mocks.treeOverrides = { knowledge_base: [] }
  mocks.treeStates = {}
  mocks.libraryCatalog = [
    { key: 'set-a', name: '资料 A' },
    { key: 'set-b', name: '资料 B' },
  ]
  mocks.route.query = {}
  mocks.uiStore.globalRefreshKey = 0
  mocks.createKnowledgeSet.mockReset()
  mocks.deleteKnowledgeFile.mockReset()
  mocks.deleteKnowledgeSet.mockReset()
  mocks.listKnowledgeFiles.mockReset().mockResolvedValue(mocks.knowledgeFiles)
  mocks.listKnowledgeSets.mockReset().mockResolvedValue(mocks.knowledgeSets)
  mocks.listLibraryCatalog.mockReset().mockResolvedValue([])
  mocks.readKnowledgeFile
    .mockReset()
    .mockImplementation((_key: string, path: string) => Promise.resolve(contents[path]))
  mocks.readUtf8File.mockReset()
  mocks.writeKnowledgeFile.mockReset()
  mocks.knowledgeSetsRefetch.mockReset().mockResolvedValue(undefined)
  mocks.knowledgeFilesRefetch.mockReset().mockResolvedValue(undefined)
  mocks.invalidateQueries.mockReset().mockResolvedValue(undefined)
  mocks.router.replace.mockReset().mockResolvedValue(undefined)
  mocks.uiStore.showToast.mockReset()
  mocks.beforeRouteLeave.mockReset()
  mocks.beforeRouteUpdate.mockReset()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('KnowledgeConsole reading and request ownership', () => {
  it('defaults to safe Markdown reading and preserves plain UTF-8 source without mounting the editor', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await openFile()
    expect(wrapper.find('.safe-report-markdown h1').text()).toBe('A body')
    expect(wrapper.find('.safe-report-markdown strong').text()).toBe('加粗')
    expect(wrapper.find('.safe-report-markdown script').exists()).toBe(false)
    expect(wrapper.find('.safe-report-markdown img').exists()).toBe(false)
    expect(document.body.querySelector('[data-testid="knowledge-editor"]')).toBeNull()
    buttonByText('查看原文').click()
    await flushPromises()
    expect(wrapper.find('.knowledge-source').text()).toBe(contents['a.md'].content)
    await openFile('b.txt')
    expect(wrapper.find('.knowledge-source').text()).toBe(contents['b.txt'].content)
    expect(wrapper.find('.filter-bar').exists()).toBe(true)
  })

  it('honors focus and uses actual library names and keys for top-level search', async () => {
    mocks.route.query = { focus: 'set-b' }
    mountPage()
    await flushPromises()
    expect(document.body.querySelector('.knowledge-navigation-row.active')?.getAttribute('data-library-key')).toBe('set-b')
    const search = document.body.querySelector<HTMLInputElement>('[aria-label="搜索资料库、文件名或路径"]')!
    search.value = '资料 A'
    search.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    const roots = document.body.querySelectorAll('.knowledge-library-root')
    expect([...roots].map((node) => node.getAttribute('title'))).toEqual(['knowledge_base', 'set-a'])
  })

  it('does not replay an old focus when a newly selected library is saved with continued edits', async () => {
    mocks.route.query = { focus: 'set-a' }
    const created = {
      ...knowledgeSets[0],
      key: 'created',
      path: 'library://created',
      file_count: 0,
    }
    mocks.createKnowledgeSet.mockResolvedValueOnce(created)
    mocks.knowledgeSetsRefetch
      .mockImplementationOnce(async () => {
        mocks.knowledgeSetsQueryData!.value = [...knowledgeSets, created]
      })
      .mockImplementationOnce(async () => {
        mocks.knowledgeSetsQueryData!.value = [...knowledgeSets, { ...created, file_count: 1 }]
      })
    mountPage()
    await flushPromises()
    await runNodeAction('create-library')
    await flushPromises()
    const keyInput = document.body.querySelector<HTMLInputElement>(
      'input[placeholder="new_knowledge_set"]',
    )!
    keyInput.value = 'created'
    keyInput.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    buttonByText('创建资料库').click()
    await flushPromises()
    expect(document.body.querySelector('.knowledge-navigation-row.active')?.getAttribute('data-library-key')).toBe('created')
    await runNodeAction('create-file')
    await flushPromises()
    const editor = document.body.querySelector<HTMLTextAreaElement>(
      '[data-testid="knowledge-editor"]',
    )!
    await setEditor(editor, 'First save')
    const pending = deferred<KnowledgeFileContent>()
    mocks.writeKnowledgeFile.mockReturnValueOnce(pending.promise)
    buttonByText('保存文件').click()
    await flushPromises()
    await setEditor(editor, 'Continued new-library draft')
    pending.resolve({
      name: 'README.md',
      path: 'README.md',
      size: 10,
      warnings: [],
      content: 'First save',
    })
    await flushPromises()
    expect(mocks.route.query.focus).toBe('set-a')
    expect(document.body.querySelector('.knowledge-navigation-row.active')?.getAttribute('data-library-key')).toBe('created')
    expect(editor.isConnected).toBe(true)
    expect(editor.value).toBe('Continued new-library draft')
    expect(mocks.writeKnowledgeFile).toHaveBeenCalledWith('created', 'README.md', 'First save')
    buttonByText('重置').click()
    await flushPromises()
    expect(editor.value).toBe('First save')
  })

  it('keeps the tree visible while reading and shows a failed read until another file opens', async () => {
    const wrapper = mountPage()
    await flushPromises()
    const pending = deferred<KnowledgeFileContent>()
    mocks.readKnowledgeFile.mockReturnValueOnce(pending.promise)
    await openFile()
    expect(wrapper.find('.knowledge-navigation').exists()).toBe(true)
    expect(wrapper.find('.knowledge-reader').text()).toContain('正在读取 a.md')
    pending.reject(new Error('first read rejected'))
    await flushPromises()
    expect(wrapper.find('.knowledge-reader').text()).toContain(
      '读取「a.md」失败：first read rejected',
    )
    await openFile('b.txt')
    expect(wrapper.find('.knowledge-reader').text()).not.toContain('first read rejected')
    expect(wrapper.find('.knowledge-source').text()).toBe(contents['b.txt'].content)
  })

  it('does not let a refresh cancel a newer pending file selection', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await openFile()
    const refresh = deferred<void>()
    mocks.knowledgeSetsRefetch.mockReturnValueOnce(refresh.promise)
    buttonByText('刷新').click()
    await flushPromises()
    const pendingFile = deferred<KnowledgeFileContent>()
    mocks.readKnowledgeFile.mockReturnValueOnce(pendingFile.promise)
    await openFile('b.txt')
    const signal = mocks.readKnowledgeFile.mock.calls.at(-1)?.[2] as AbortSignal
    refresh.resolve()
    await flushPromises()
    expect(signal.aborted).toBe(false)
    expect(mocks.readKnowledgeFile).toHaveBeenCalledTimes(2)
    pendingFile.resolve(contents['b.txt'])
    await flushPromises()
    expect(wrapper.find('.knowledge-source').text()).toBe(contents['b.txt'].content)
  })

  it('keeps current reading when path search hides its navigation entry', async () => {
    mocks.treeOverrides['set-b'] = [knowledgeFiles[1]]
    const wrapper = mountPage()
    await flushPromises()
    await openFile()
    await wrapper.find('input[aria-label="搜索资料库、文件名或路径"]').setValue('deep/c')
    expect(wrapper.find('.safe-report-markdown h1').text()).toBe('A body')
    expect(wrapper.text()).toContain('当前文件不在搜索结果中')
    expect(wrapper.findAll('.knowledge-navigation-item').map((item) => item.text())).toEqual([
      '根目录', '资料 A', 'nested', 'deep', 'c.md',
    ])
  })

  it('searches unopened libraries without changing the current reader', async () => {
    mocks.treeOverrides['set-b'] = [{ name: 'hidden.md', path: 'docs/hidden.md', size: 1, warnings: [] }]
    const wrapper = mountPage()
    await flushPromises()
    await openFile('a.md')
    await expandLibrary('set-b')
    expect(wrapper.find('.safe-report-markdown h1').text()).toBe('A body')
    wrapper.find('.knowledge-navigation-row[data-library-key="set-b"] .knowledge-tree-toggle').element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flushPromises()
    await wrapper.find('input[aria-label="搜索资料库、文件名或路径"]').setValue('hidden.md')
    expect(wrapper.findAll('.knowledge-navigation-item').map((item) => item.text())).toEqual([
      '根目录', '资料 B', 'docs', 'hidden.md',
    ])
    const directoryToggle = wrapper.find('[aria-label="收起目录 docs"]')
    await directoryToggle.trigger('click')
    expect(wrapper.find('[aria-label="展开目录 docs"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('hidden.md')
    await wrapper.find('[aria-label="展开目录 docs"]').trigger('click')
    expect(wrapper.text()).toContain('hidden.md')
    await wrapper.find('[aria-label="收起资料库 资料 B"]').trigger('click')
    expect(wrapper.find('[aria-label="展开资料库 资料 B"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('hidden.md')
    await wrapper.find('[aria-label="展开资料库 资料 B"]').trigger('click')
    expect(wrapper.text()).toContain('hidden.md')
    await wrapper.find('input[aria-label="搜索资料库、文件名或路径"]').setValue('')
    expect(wrapper.find('[aria-label="展开资料库 资料 B"]').exists()).toBe(true)
    expect(wrapper.find('.safe-report-markdown h1').text()).toBe('A body')
  })

  it('does not call incomplete search results an empty result', async () => {
    mocks.treeStates = { 'set-a': 'pending', 'set-b': 'error' }
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('input[aria-label="搜索资料库、文件名或路径"]').setValue('no-such-path')
    expect(wrapper.find('[role="status"]').text()).toContain('正在加载文件')
    expect(wrapper.find('[role="alert"]').text()).toContain('目录不可用')
    expect(wrapper.text()).not.toContain('没有匹配的资料')
  })

  it('captures the node upload target before the file picker returns', async () => {
    mocks.writeKnowledgeFile.mockResolvedValueOnce({ ...contents['a.md'], path: 'upload.md' })
    const wrapper = mountPage()
    await flushPromises()
    await openFile()
    const input = wrapper.find('input[type="file"]').element as HTMLInputElement
    const click = vi.spyOn(input, 'click').mockImplementation(() => {})
    await runNodeAction('upload')
    expect(click).toHaveBeenCalledOnce()
    await openFile('b.txt', 'set-b')
    mocks.readUtf8File.mockResolvedValueOnce('uploaded')
    Object.defineProperty(input, 'files', {
      configurable: true,
      value: [new File(['uploaded'], 'upload.md', { type: 'text/markdown' })],
    })
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()
    expect(mocks.writeKnowledgeFile).toHaveBeenCalledWith('set-a', 'upload.md', 'uploaded')
    expect(wrapper.find('.knowledge-source').text()).toBe(contents['b.txt'].content)
  })

  it('distinguishes same-named libraries when choosing an upload target', async () => {
    mocks.knowledgeSets.push({ key: 'set-c', path: 'library://set-c', builtin: false, safety_path: null, file_count: 0, referenced_by: [] })
    mocks.libraryCatalog = [
      { key: 'set-a', name: '同名资料' },
      { key: 'set-b', name: '同名资料' },
      { key: 'set-c', name: '同名资料 (set-a)' },
    ]
    const wrapper = mountPage()
    await flushPromises()
    const input = wrapper.find('input[type="file"]').element as HTMLInputElement
    const click = vi.spyOn(input, 'click').mockImplementation(() => {})
    const options = [...document.querySelectorAll<HTMLButtonElement>('.knowledge-library-root')]
    expect(options.map((button) => button.textContent?.trim())).toEqual([
      '根目录', '同名资料 (set-a)', '同名资料 (set-b)', '同名资料 (set-a) (set-c)',
    ])
    options.find((button) => button.title === 'set-b')!.click()
    await flushPromises()
    await runNodeAction('upload')
    expect(click).toHaveBeenCalledOnce()
    expect(wrapper.find('.knowledge-navigation-row.active').attributes('data-library-key')).toBe('set-b')
  })

  it('cancels pending file creation when the write target changes', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await openFile('a.md')
    wrapper.find('[data-knowledge-key="set-a"][data-knowledge-path="nested"]').element
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const pending = deferred<KnowledgeFile[]>()
    mocks.listKnowledgeFiles.mockReturnValueOnce(pending.promise)
    await runNodeAction('create-file')
    await flushPromises()
    wrapper.find('.knowledge-navigation-row[data-library-key="set-b"] .knowledge-library-root')
      .element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    pending.resolve(knowledgeFiles)
    await flushPromises()
    expect(wrapper.find('.knowledge-navigation-row.active').attributes('data-library-key')).toBe('set-b')
    expect(wrapper.find('.safe-report-markdown h1').text()).toBe('A body')
    expect(document.body.querySelector('[data-testid="knowledge-editor"]')).toBeNull()
    expect(mocks.writeKnowledgeFile).not.toHaveBeenCalled()
  })

  it('confirms an upload to another directory in the current library', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await openFile('a.md')
    wrapper.find('[data-knowledge-key="set-a"][data-knowledge-path="nested"]').element
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const input = wrapper.find('input[type="file"]').element as HTMLInputElement
    vi.spyOn(input, 'click').mockImplementation(() => {})
    mocks.readUtf8File.mockResolvedValueOnce('uploaded')
    mocks.writeKnowledgeFile.mockResolvedValueOnce({ ...contents['a.md'], path: 'nested/upload.md' })
    await runNodeAction('upload')
    Object.defineProperty(input, 'files', {
      configurable: true,
      value: [new File(['uploaded'], 'upload.md')],
    })
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()
    expect(mocks.writeKnowledgeFile).toHaveBeenCalledWith('set-a', 'nested/upload.md', 'uploaded')
    expect(wrapper.find('.safe-report-markdown h1').text()).toBe('A body')
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith(
      '文件已上传至资料库「set-a」的「nested/upload.md」', 'success',
    )
  })

  it('creates a file under the explicitly selected directory', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await expandLibrary('set-a')
    wrapper.find('.knowledge-navigation-item[data-knowledge-key="set-a"][title="nested"]').element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flushPromises()
    await runNodeAction('create-file')
    await flushPromises()
    expect(document.body.querySelector<HTMLInputElement>('input[placeholder="README.md"]')?.value).toBe('nested/README.md')
    expect(mocks.listKnowledgeFiles).toHaveBeenCalledWith('set-a')
  })

  it('aborts and suppresses a stale A to B to A file read', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await openFile()
    const pending = deferred<KnowledgeFileContent>()
    mocks.readKnowledgeFile.mockReturnValueOnce(pending.promise)
    await openFile('b.txt')
    const signal = mocks.readKnowledgeFile.mock.calls.at(-1)?.[2] as AbortSignal
    await openFile()
    expect(signal.aborted).toBe(true)
    pending.reject(new DOMException('Aborted', 'AbortError'))
    await flushPromises()
    expect(wrapper.find('.safe-report-markdown h1').text()).toBe('A body')
    expect(wrapper.text()).not.toContain('读取「b.txt」失败')
  })

  it('a later file click invalidates a read even before the later read starts', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await expandLibrary('set-a')
    const first = deferred<KnowledgeFileContent>()
    const second = deferred<KnowledgeFileContent>()
    mocks.readKnowledgeFile.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    wrapper.find('[data-knowledge-key="set-a"][data-knowledge-path="a.md"]').element.dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    )
    await flushPromises()
    wrapper.find('[data-knowledge-key="set-a"][data-knowledge-path="b.txt"]').element.dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    )
    first.resolve(contents['a.md'])
    await flushPromises()
    expect(wrapper.find('.knowledge-reader').text()).not.toContain('A body')
    expect(wrapper.find('.knowledge-reader').text()).toContain('正在读取 b.txt')
    second.resolve(contents['b.txt'])
    await flushPromises()
    expect(wrapper.find('.knowledge-source').text()).toBe(contents['b.txt'].content)
  })

  it('keeps a later write target when an earlier file read finishes', async () => {
    const wrapper = mountPage()
    await flushPromises()
    const pending = deferred<KnowledgeFileContent>()
    mocks.readKnowledgeFile.mockReturnValueOnce(pending.promise)
    await openFile('a.md')
    wrapper.find('.knowledge-navigation-row[data-library-key="set-b"] .knowledge-library-root')
      .element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flushPromises()
    pending.resolve(contents['a.md'])
    await flushPromises()
    expect(wrapper.find('.safe-report-markdown h1').text()).toBe('A body')
    expect(wrapper.find('.knowledge-navigation-row.active').attributes('data-library-key')).toBe('set-b')
  })

  it('shows upload failure after the reader has moved to another library', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await openFile('a.md')
    const pending = deferred<string>()
    mocks.readUtf8File.mockReturnValueOnce(pending.promise)
    const input = wrapper.find('input[type="file"]').element as HTMLInputElement
    vi.spyOn(input, 'click').mockImplementation(() => {})
    await runNodeAction('upload')
    Object.defineProperty(input, 'files', {
      configurable: true,
      value: [new File(['draft'], 'upload.md')],
    })
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()
    await openFile('b.txt', 'set-b')
    pending.reject(new Error('read failed'))
    await flushPromises()
    expect(wrapper.find('.knowledge-source').text()).toBe(contents['b.txt'].content)
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith(
      expect.stringContaining('set-a」的「upload.md」失败: read failed'),
      'danger',
    )
    expect(mocks.writeKnowledgeFile).not.toHaveBeenCalled()
  })

  it('does not apply a file result after the library visit changes', async () => {
    const wrapper = mountPage()
    await flushPromises()
    const pending = deferred<KnowledgeFileContent>()
    mocks.readKnowledgeFile.mockReturnValueOnce(pending.promise)
    await openFile()
    await openFile('b.txt', 'set-b')
    pending.resolve(contents['a.md'])
    await flushPromises()
    expect(wrapper.find('.knowledge-source').text()).toBe(contents['b.txt'].content)
    expect(document.body.querySelector('.knowledge-navigation-row.active')?.getAttribute('data-library-key')).toBe('set-b')
  })

  it('文件编辑关闭回到所属编辑或新建按钮，草稿确认保留输入', async () => {
    mountPage()
    await flushPromises()
    await openFile()
    const editTrigger = buttonByText('编辑')
    await editSelectedFile()
    buttonByText('取消').click()
    await flushPromises()
    await vi.waitFor(() => expect(document.activeElement).toBe(editTrigger))

    const editor = await editSelectedFile()
    await setEditor(editor, '未保存的本地资料草稿')
    buttonByText('取消').click()
    await flushPromises()
    buttonByText('继续编辑').click()
    await flushPromises()
    expect(editor.value).toBe('未保存的本地资料草稿')
    expect(editor.isConnected).toBe(true)
    buttonByText('取消').click()
    await flushPromises()
    buttonByText('放弃修改').click()
    await flushPromises()
    await vi.waitFor(() => expect(document.activeElement).toBe(editTrigger))

    const createTrigger = document.querySelector<HTMLButtonElement>('[data-console-action="knowledge-menu:set-a:"]')!
    createTrigger.click()
    await flushPromises()
    await selectMenuAction('文件')
    buttonByText('取消').click()
    await flushPromises()
    await vi.waitFor(() => expect(document.activeElement).toBe(createTrigger))
    expect(mocks.writeKnowledgeFile).not.toHaveBeenCalled()
    expect(mocks.createKnowledgeSet).not.toHaveBeenCalled()
  })

  it('protects editor close, file selection, route focus changes and leaving with one dirty draft', async () => {
    mountPage()
    await flushPromises()
    await openFile()
    const editor = await editSelectedFile()
    await setEditor(editor, 'Dirty route draft')
    const cancelDiscard = async () => {
      await flushPromises()
      buttonByText('继续编辑').click()
      await flushPromises()
    }
    buttonByText('取消').click()
    await cancelDiscard()
    expect(editor.isConnected).toBe(true)
    await openFile('b.txt')
    await cancelDiscard()
    expect(editor.value).toBe('Dirty route draft')
    const leaveGuard = mocks.beforeRouteLeave.mock.calls[0]?.[0] as () => Promise<boolean>
    const updateGuard = mocks.beforeRouteUpdate.mock.calls[0]?.[0] as (to: {
      query: { focus: string }
    }) => Promise<boolean>
    const leaving = leaveGuard()
    await cancelDiscard()
    expect(await leaving).toBe(false)
    const updating = updateGuard({ query: { focus: 'set-b' } })
    await cancelDiscard()
    expect(await updating).toBe(false)
    expect(mocks.writeKnowledgeFile).not.toHaveBeenCalled()
    expect(window.confirm).not.toHaveBeenCalled()
  })

  it('keeps continued edits, rejects duplicate saves and resets to the accepted baseline', async () => {
    mountPage()
    await flushPromises()
    await openFile()
    const editor = await editSelectedFile()
    await setEditor(editor, 'First save')
    const pending = deferred<KnowledgeFileContent>()
    mocks.writeKnowledgeFile.mockReturnValueOnce(pending.promise)
    buttonByText('保存文件').click()
    buttonByText('保存文件').click()
    await flushPromises()
    await setEditor(editor, 'Continued edit')
    const leaveGuard = mocks.beforeRouteLeave.mock.calls[0]?.[0] as () => boolean
    expect(leaveGuard()).toBe(false)
    pending.resolve({ ...contents['a.md'], content: 'First save' })
    await flushPromises()
    expect(editor.isConnected).toBe(true)
    expect(editor.value).toBe('Continued edit')
    expect(mocks.writeKnowledgeFile).toHaveBeenCalledTimes(1)
    buttonByText('重置').click()
    await flushPromises()
    expect(editor.value).toBe('First save')
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['library-catalog'] }, { throwOnError: true })
  })

  it('returns to reading after an unchanged successful save and keeps the editor on failure', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await openFile()
    let editor = await editSelectedFile()
    await setEditor(editor, 'Saved body')
    mocks.writeKnowledgeFile.mockResolvedValueOnce({ ...contents['a.md'], content: 'Saved body' })
    buttonByText('保存文件').click()
    await flushPromises()
    expect(document.body.querySelector('[data-testid="knowledge-editor"]')).toBeNull()
    expect(wrapper.find('.safe-report-markdown').text()).toBe('Saved body')
    editor = await editSelectedFile()
    await setEditor(editor, 'Keep after failure')
    mocks.writeKnowledgeFile.mockRejectedValueOnce(new Error('save rejected'))
    buttonByText('保存文件').click()
    await flushPromises()
    expect(editor.isConnected).toBe(true)
    expect(editor.value).toBe('Keep after failure')
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith(
      expect.stringContaining('save rejected'),
      'danger',
    )
  })

  it('keeps a successful save when the subsequent list refresh fails', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await openFile()
    const editor = await editSelectedFile()
    await setEditor(editor, 'Saved despite refresh failure')
    mocks.writeKnowledgeFile.mockResolvedValueOnce({
      ...contents['a.md'], content: 'Saved despite refresh failure',
    })
    mocks.knowledgeSetsRefetch.mockRejectedValueOnce(new Error('list unavailable'))
    buttonByText('保存文件').click()
    await flushPromises()
    expect(mocks.writeKnowledgeFile).toHaveBeenCalledTimes(1)
    expect(wrapper.find('.safe-report-markdown').text()).toBe('Saved despite refresh failure')
    expect(document.body.querySelector('[data-testid="knowledge-editor"]')).toBeNull()
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith(
      expect.stringContaining('已保存，但部分数据刷新失败: list unavailable。请先刷新核实，勿重复操作。'),
      'warn',
    )
    expect(mocks.uiStore.showToast).not.toHaveBeenCalledWith(expect.stringContaining('保存资料库文件失败'), 'danger')
  })

  it('does not let an upload overwrite a newer directory and file selection', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await openFile()
    const pendingRead = deferred<string>()
    mocks.readUtf8File.mockReturnValueOnce(pendingRead.promise)
    mocks.writeKnowledgeFile.mockResolvedValueOnce({
      name: 'upload.md',
      path: 'upload.md',
      size: 8,
      warnings: [],
      content: 'uploaded',
    })
    const input = wrapper.find('input[type="file"]').element as HTMLInputElement
    vi.spyOn(input, 'click').mockImplementation(() => {})
    await runNodeAction('upload')
    await flushPromises()
    Object.defineProperty(input, 'files', {
      configurable: true,
      value: [new File(['uploaded'], 'upload.md', { type: 'text/markdown' })],
    })
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()
    await openFile('nested/deep/c.md')
    pendingRead.resolve('uploaded')
    await flushPromises()
    expect(mocks.writeKnowledgeFile).toHaveBeenCalledWith('set-a', 'upload.md', 'uploaded')
    expect(wrapper.find('.safe-report-markdown').text()).toBe('C body')
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['knowledgeFiles', 'set-a'] }, { throwOnError: true })
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['library-catalog'] }, { throwOnError: true })
    expect(mocks.uiStore.showToast).not.toHaveBeenCalledWith('文件已上传', 'success')
  })

  it('file row deletion captures A while reading B, cancels to its trigger and keeps B after confirmation', async () => {
    const pending = deferred<void>()
    mocks.deleteKnowledgeFile.mockReturnValueOnce(pending.promise)
    mocks.invalidateQueries.mockImplementation(async ({ queryKey }: { queryKey: string[] }) => {
      if (queryKey[0] === 'knowledgeFiles')
        mocks.knowledgeFilesQueryData!.value = knowledgeFiles.filter((file) => file.path !== 'a.md')
    })
    const wrapper = mountPage()
    await flushPromises()
    await openFile('b.txt')
    const trigger = fileMenuTrigger('a.md')
    const selection = wrapper.find('[data-knowledge-key="set-a"][title="a.md"]').element
    expect(trigger.parentElement).toBe(selection.parentElement)
    expect(wrapper.find('button button').exists()).toBe(false)
    expect(wrapper.find('.knowledge-information').exists()).toBe(false)
    await openFileDeletion('a.md')
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain(
      '确定删除资料库「set-a」中的文件「a.md」',
    )
    expect(wrapper.find('.knowledge-source').text()).toBe(contents['b.txt'].content)
    buttonByText('取消').click()
    await flushPromises()
    await new Promise((resolve) => window.setTimeout(resolve, 60))
    expect(mocks.deleteKnowledgeFile).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(trigger)
    await openFileDeletion('a.md')
    const confirm = buttonByText('确认删除')
    confirm.click()
    confirm.click()
    buttonByText('取消').click()
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain('a.md')
    expect(mocks.deleteKnowledgeFile).toHaveBeenCalledExactlyOnceWith('set-a', 'a.md')
    pending.resolve()
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
    expect(wrapper.find('.knowledge-source').text()).toBe(contents['b.txt'].content)
    expect(mocks.readKnowledgeFile).toHaveBeenCalledTimes(1)
    expect(wrapper.find('.knowledge-navigation-item[title="a.md"]').exists()).toBe(false)
    await new Promise((resolve) => window.setTimeout(resolve, 60))
    expect(document.activeElement).toBe(
      wrapper.find('.knowledge-navigation-item[title="b.txt"]').element,
    )
  })

  it('file row menus open by keyboard through a portal and deleting the current file clears reading', async () => {
    mocks.treeOverrides['set-b'] = [knowledgeFiles[1]]
    mocks.deleteKnowledgeFile.mockResolvedValueOnce(undefined)
    mocks.invalidateQueries.mockImplementation(async ({ queryKey }: { queryKey: string[] }) => {
      if (queryKey[0] === 'knowledgeFiles')
        mocks.knowledgeFilesQueryData!.value = knowledgeFiles.filter(
          (file) => file.path !== 'nested/deep/c.md',
        )
    })
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('input[aria-label="搜索资料库、文件名或路径"]').setValue('c.md')
    await openFile('nested/deep/c.md')
    const trigger = fileMenuTrigger('nested/deep/c.md')
    expect(wrapper.findAll('[data-knowledge-key="set-a"][data-knowledge-path="nested/deep/c.md"]')).toHaveLength(1)
    expect(wrapper.findAll('.knowledge-navigation-item--directory')).toHaveLength(2)
    trigger.focus()
    trigger.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await flushPromises()
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    expect(wrapper.find('.knowledge-navigation [role="menu"]').exists()).toBe(false)
    const item = [...document.body.querySelectorAll<HTMLElement>('[role="menuitem"]')].find((item) => item.textContent?.includes('删除文件'))!
    item.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain(
      'nested/deep/c.md',
    )
    buttonByText('确认删除').click()
    await flushPromises()
    expect(mocks.deleteKnowledgeFile).toHaveBeenCalledExactlyOnceWith('set-a', 'nested/deep/c.md')
    expect(wrapper.find('.knowledge-navigation').exists()).toBe(true)
    expect(wrapper.find('.safe-report-markdown').exists()).toBe(false)
    await new Promise((resolve) => window.setTimeout(resolve, 60))
    expect(document.activeElement).toBe(document.querySelector('.knowledge-library-root'))
  })

  it('file row deletion does not abort a newer pending selection of B', async () => {
    const pendingDelete = deferred<void>()
    const pendingRead = deferred<KnowledgeFileContent>()
    mocks.deleteKnowledgeFile.mockReturnValueOnce(pendingDelete.promise)
    mocks.invalidateQueries.mockImplementation(async ({ queryKey }: { queryKey: string[] }) => {
      if (queryKey[0] === 'knowledgeFiles')
        mocks.knowledgeFilesQueryData!.value = knowledgeFiles.filter((file) => file.path !== 'a.md')
    })
    const wrapper = mountPage()
    await flushPromises()
    await openFile('a.md')
    await openFileDeletion('a.md')
    buttonByText('确认删除').click()
    await flushPromises()
    mocks.readKnowledgeFile.mockReturnValueOnce(pendingRead.promise)
    await openFile('b.txt')
    const signal = mocks.readKnowledgeFile.mock.calls.at(-1)?.[2] as AbortSignal
    pendingDelete.resolve()
    await flushPromises()
    expect(signal.aborted).toBe(false)
    pendingRead.resolve(contents['b.txt'])
    await flushPromises()
    expect(wrapper.find('.knowledge-source').text()).toBe(contents['b.txt'].content)
  })

  it('file row deletion suppresses a stale response and focus after an A to B to A library visit', async () => {
    const pending = deferred<void>()
    mocks.deleteKnowledgeFile.mockReturnValueOnce(pending.promise)
    const wrapper = mountPage()
    await flushPromises()
    await openFile('a.md')
    await openFileDeletion('a.md')
    buttonByText('确认删除').click()
    await flushPromises()
    await openFile('b.txt', 'set-b')
    await openFile('b.txt', 'set-a')
    const search = wrapper.find('input[aria-label="搜索资料库、文件名或路径"]')
      .element as HTMLInputElement
    pending.resolve()
    await flushPromises()
    search.focus()
    await new Promise((resolve) => window.setTimeout(resolve, 60))
    expect(wrapper.find('.knowledge-source').text()).toBe(contents['b.txt'].content)
    expect(mocks.uiStore.showToast).not.toHaveBeenCalledWith('资料库文件已删除', 'success')
    expect(document.activeElement).toBe(search)
  })

  it('file row deletion preserves another file draft and protects a dirty current file', async () => {
    mocks.deleteKnowledgeFile.mockResolvedValueOnce(undefined)
    mountPage()
    await flushPromises()
    await openFile('b.txt')
    const editor = await editSelectedFile()
    await setEditor(editor, 'B unsaved draft')
    await openFileDeletion('a.md')
    buttonByText('确认删除').click()
    await flushPromises()
    expect(mocks.deleteKnowledgeFile).toHaveBeenCalledExactlyOnceWith('set-a', 'a.md')
    expect(window.confirm).not.toHaveBeenCalled()
    expect(editor.isConnected).toBe(true)
    expect(editor.value).toBe('B unsaved draft')
    await openFileDeletion('b.txt')
    buttonByText('确认删除').click()
    await flushPromises()
    expect(mocks.deleteKnowledgeFile).toHaveBeenCalledTimes(1)
    expect(document.body.textContent).toContain('放弃未保存的资料修改')
    buttonByText('继续编辑').click()
    await flushPromises()
    expect(window.confirm).not.toHaveBeenCalled()
    expect(editor.value).toBe('B unsaved draft')
  })

  it.each([
    { builtin: true, referenced_by: [], reason: '根目录不可归档或删除' },
    { builtin: false, referenced_by: ['project-a'], reason: '资料库仍被引用' },
  ])(
    'library menu preserves archive restrictions: $reason',
    async ({ builtin, referenced_by, reason }) => {
      mocks.knowledgeSets[0] = { ...knowledgeSets[0], builtin, referenced_by }
      mountPage()
      await flushPromises()
      document.body.querySelector<HTMLButtonElement>('[data-library-key="set-a"] [data-console-action]')!.click()
      await flushPromises()
      const item = [...document.body.querySelectorAll<HTMLElement>('[role="menuitem"]')].find(
        (element) => element.textContent?.includes('归档资料库'),
      )!
      expect(item.hasAttribute('data-disabled')).toBe(true)
      expect(item.textContent).toContain(reason)
      item.click()
      await flushPromises()
      expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
      expect(mocks.deleteKnowledgeSet).not.toHaveBeenCalled()
    },
  )

  it('archives the captured library, blocks duplicate confirmation and invalidates the real catalog', async () => {
    const pending = deferred<void>()
    mocks.deleteKnowledgeSet.mockReturnValueOnce(pending.promise)
    mountPage()
    await flushPromises()
    const trigger = document.body.querySelector<HTMLButtonElement>('[data-library-key="set-a"] [data-console-action]')!
    trigger.focus()
    trigger.click()
    await flushPromises()
    await selectMenuAction('归档资料库')
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain('set-a')
    const confirm = buttonByText('确认归档')
    confirm.click()
    confirm.click()
    await flushPromises()
    expect(mocks.deleteKnowledgeSet).toHaveBeenCalledTimes(1)
    expect(mocks.deleteKnowledgeSet).toHaveBeenCalledWith('set-a')
    pending.resolve()
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['library-catalog'] }, { throwOnError: true })
    await new Promise((resolve) => window.setTimeout(resolve, 60))
    expect(document.activeElement).toBe(trigger)
  })

  it('keeps a failed file deletion open for retry and cancels focus timers on unmount', async () => {
    mocks.deleteKnowledgeFile
      .mockRejectedValueOnce(new Error('delete rejected'))
      .mockResolvedValueOnce(undefined)
    const wrapper = mountPage()
    await flushPromises()
    await openFile('b.txt')
    await openFileDeletion('a.md')
    buttonByText('确认删除').click()
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain('a.md')
    expect(wrapper.find('.knowledge-source').text()).toBe(contents['b.txt'].content)
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith(
      '删除资料库文件「a.md」失败: delete rejected',
      'danger',
    )
    expect(mocks.uiStore.showToast).not.toHaveBeenCalledWith('资料库文件已删除', 'success')
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const clearTimeoutSpy = vi.spyOn(window, 'clearTimeout')
    buttonByText('确认删除').click()
    await flushPromises()
    expect(mocks.deleteKnowledgeFile).toHaveBeenNthCalledWith(2, 'set-a', 'a.md')
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['library-catalog'] }, { throwOnError: true })
    const callsBeforeUnmount = clearTimeoutSpy.mock.calls.length
    wrapper.unmount()
    expect(clearTimeoutSpy.mock.calls.length).toBeGreaterThan(callsBeforeUnmount)
  })

  it('closes a successful delete confirmation even when the tree refresh fails', async () => {
    mocks.deleteKnowledgeFile.mockResolvedValueOnce(undefined)
    mocks.invalidateQueries.mockImplementation(async ({ queryKey }: { queryKey: string[] }) => {
      if (queryKey[0] === 'knowledgeFiles') throw new Error('tree unavailable')
    })
    mountPage()
    await flushPromises()
    await openFile()
    await openFileDeletion('a.md')
    buttonByText('确认删除').click()
    await flushPromises()
    expect(mocks.deleteKnowledgeFile).toHaveBeenCalledExactlyOnceWith('set-a', 'a.md')
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith(
      expect.stringContaining('已删除，但部分数据刷新失败: tree unavailable。请先刷新核实，勿重复操作。'),
      'warn',
    )
    expect(mocks.uiStore.showToast).not.toHaveBeenCalledWith(expect.stringContaining('删除资料库文件「a.md」失败'), 'danger')
  })

  it('does not select or retry a created library when the list refresh fails', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await openFile()
    mocks.createKnowledgeSet.mockResolvedValueOnce({ ...knowledgeSets[0], key: 'created' })
    mocks.knowledgeSetsRefetch.mockRejectedValueOnce(new Error('list unavailable'))
    await runNodeAction('create-library')
    await flushPromises()
    const input = document.body.querySelector<HTMLInputElement>('input[placeholder="new_knowledge_set"]')!
    input.value = 'created'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    buttonByText('创建资料库').click()
    await flushPromises()
    expect(mocks.createKnowledgeSet).toHaveBeenCalledExactlyOnceWith('created')
    expect(document.body.querySelector('input[placeholder="new_knowledge_set"]')).toBeNull()
    expect(wrapper.find('.safe-report-markdown h1').text()).toBe('A body')
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith(
      expect.stringContaining('资料库「created」已创建，但部分数据刷新失败: list unavailable'),
      'warn',
    )
    expect(mocks.uiStore.showToast).not.toHaveBeenCalledWith(expect.stringContaining('创建资料库失败'), 'danger')
  })

  it('guards duplicate library creation and retains a newer key typed during the request', async () => {
    mountPage()
    await flushPromises()
    await runNodeAction('create-library')
    await flushPromises()
    const input = document.body.querySelector<HTMLInputElement>(
      'input[placeholder="new_knowledge_set"]',
    )!
    input.value = 'created'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    const pending = deferred<KnowledgeSet>()
    mocks.createKnowledgeSet.mockReturnValueOnce(pending.promise)
    buttonByText('创建资料库').click()
    input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }))
    await flushPromises()
    expect(mocks.createKnowledgeSet).toHaveBeenCalledTimes(1)
    input.value = 'next-input'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    pending.resolve({ ...knowledgeSets[0], key: 'created', path: 'library://created' })
    await flushPromises()
    expect(input.isConnected).toBe(true)
    expect(input.value).toBe('next-input')
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['library-catalog'] }, { throwOnError: true })
  })
})
