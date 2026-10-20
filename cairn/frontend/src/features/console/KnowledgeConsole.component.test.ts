import { flushPromises, mount } from '@vue/test-utils'
import { ref, unref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { KnowledgeFile, KnowledgeFileContent, KnowledgeSet } from '@/api/types'
import { DropdownMenu } from '@/components/ui'
import KnowledgeConsole from './KnowledgeConsole.vue'

const mocks = vi.hoisted(() => ({
  knowledgeSets: [] as KnowledgeSet[],
  knowledgeSetsQueryData: null as { value: KnowledgeSet[] } | null,
  knowledgeFiles: [] as KnowledgeFile[],
  knowledgeFilesQueryData: null as { value: KnowledgeFile[] } | null,
  createKnowledgeSet: vi.fn(),
  deleteKnowledgeFile: vi.fn(),
  deleteKnowledgeSet: vi.fn(),
  listAdvisoryLibraryRevisions: vi.fn(),
  listKnowledgeFiles: vi.fn(),
  listKnowledgeSets: vi.fn(),
  listLibraryCatalog: vi.fn(),
  readKnowledgeFile: vi.fn(),
  readUtf8File: vi.fn(),
  writeKnowledgeFile: vi.fn(),
  knowledgeSetsRefetch: vi.fn(),
  knowledgeFilesRefetch: vi.fn(),
  knowledgeRevisionsRefetch: vi.fn(),
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
  listAdvisoryLibraryRevisions: mocks.listAdvisoryLibraryRevisions,
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
        : key[0] === 'knowledgeRevisions'
          ? []
          : key[0] === 'library-catalog'
            ? [
                { key: 'set-a', name: '资料 A' },
                { key: 'set-b', name: '资料 B' },
              ]
            : mocks.knowledgeFiles
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
          : key[0] === 'knowledgeRevisions'
            ? mocks.knowledgeRevisionsRefetch
            : mocks.knowledgeFilesRefetch,
    }
  },
  useQueryClient: () => ({ invalidateQueries: mocks.invalidateQueries }),
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
  { key: 'set-a', path: 'library://set-a', builtin: false, file_count: 3, referenced_by: [] },
  { key: 'set-b', path: 'library://set-b', builtin: false, file_count: 3, referenced_by: [] },
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
const selectSet = async (key: string) => {
  document.body.querySelector<HTMLButtonElement>('[aria-label="选择资料库"]')?.click()
  await flushPromises()
  const option = [
    ...document.body.querySelectorAll<HTMLButtonElement>('.knowledge-library-option'),
  ].find((candidate) => candidate.textContent?.includes(key))
  if (!option) throw new Error(`找不到资料库：${key}`)
  option.click()
  await flushPromises()
}
const openFile = async (path = 'a.md') => {
  buttonByText(path).click()
  await flushPromises()
}
const fileMenuTrigger = (path: string) => {
  const trigger = [
    ...document.body.querySelectorAll<HTMLButtonElement>('.knowledge-file-menu-trigger'),
  ].find((button) => button.getAttribute('aria-label') === `文件更多操作：${path}`)
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

beforeEach(() => {
  mocks.knowledgeSets = knowledgeSets.map((set) => ({ ...set }))
  mocks.knowledgeSetsQueryData = null
  mocks.knowledgeFiles = knowledgeFiles.map((file) => ({ ...file }))
  mocks.knowledgeFilesQueryData = null
  mocks.route.query = {}
  mocks.uiStore.globalRefreshKey = 0
  mocks.createKnowledgeSet.mockReset()
  mocks.deleteKnowledgeFile.mockReset()
  mocks.deleteKnowledgeSet.mockReset()
  mocks.listAdvisoryLibraryRevisions.mockReset().mockResolvedValue([])
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
  mocks.knowledgeRevisionsRefetch.mockReset().mockResolvedValue(undefined)
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
    expect(document.body.querySelector('[aria-label="选择资料库"]')?.textContent).toContain(
      '资料 B',
    )
    document.body.querySelector<HTMLButtonElement>('[aria-label="选择资料库"]')?.click()
    await flushPromises()
    const search = document.body.querySelector<HTMLInputElement>('[aria-label="搜索资料库"]')!
    search.value = '资料 A'
    search.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    expect(document.body.querySelectorAll('.knowledge-library-option')).toHaveLength(1)
    expect(document.body.querySelector('.knowledge-library-option')?.textContent).toContain('set-a')
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
    wrappers.at(-1)!.findComponent(DropdownMenu).vm.$emit('select', 'create-library')
    await flushPromises()
    const keyInput = document.body.querySelector<HTMLInputElement>(
      'input[placeholder="new_knowledge_set"]',
    )!
    keyInput.value = 'created'
    keyInput.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    buttonByText('创建资料库').click()
    await flushPromises()
    expect(document.body.querySelector('[aria-label="选择资料库"]')?.textContent).toContain(
      'created',
    )
    buttonByText('新建文件').click()
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
    expect(document.body.querySelector('[aria-label="选择资料库"]')?.textContent).toContain(
      'created',
    )
    expect(editor.isConnected).toBe(true)
    expect(editor.value).toBe('Continued new-library draft')
    expect(mocks.writeKnowledgeFile).toHaveBeenCalledWith('created', 'README.md', 'First save')
    buttonByText('重置').click()
    await flushPromises()
    expect(editor.value).toBe('First save')
  })

  it('shows the reading pane while the first file loads and keeps its error visible until returning', async () => {
    const wrapper = mountPage()
    await flushPromises()
    const pending = deferred<KnowledgeFileContent>()
    mocks.readKnowledgeFile.mockReturnValueOnce(pending.promise)
    await openFile()
    expect(wrapper.find('.knowledge-workspace').classes()).toContain('knowledge-workspace--reading')
    expect(wrapper.find('.knowledge-reader').text()).toContain('正在读取 a.md')
    pending.reject(new Error('first read rejected'))
    await flushPromises()
    expect(wrapper.find('.knowledge-workspace').classes()).toContain('knowledge-workspace--reading')
    expect(wrapper.find('.knowledge-reader').text()).toContain(
      '读取「a.md」失败：first read rejected',
    )
    buttonByText('返回文件列表').click()
    await flushPromises()
    expect(wrapper.find('.knowledge-workspace').classes()).not.toContain(
      'knowledge-workspace--reading',
    )
  })

  it('does not let a refresh cancel a newer pending file selection', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await openFile()
    const refresh = deferred<void>()
    mocks.knowledgeSetsRefetch.mockReturnValueOnce(refresh.promise)
    document.body.querySelector<HTMLButtonElement>('[aria-label="刷新资料库"]')!.click()
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
    const wrapper = mountPage()
    await flushPromises()
    await openFile()
    await wrapper.find('input[aria-label="搜索当前库的文件名或路径"]').setValue('deep/c')
    expect(wrapper.find('.safe-report-markdown h1').text()).toBe('A body')
    expect(wrapper.text()).toContain('当前文件不在搜索结果中')
    expect(wrapper.findAll('.knowledge-navigation-item').map((item) => item.text())).toEqual([
      'nested',
      'deep',
      'c.md',
    ])
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

  it('does not apply a file result after the library visit changes', async () => {
    const wrapper = mountPage()
    await flushPromises()
    const pending = deferred<KnowledgeFileContent>()
    mocks.readKnowledgeFile.mockReturnValueOnce(pending.promise)
    await openFile()
    await selectSet('set-b')
    pending.resolve(contents['a.md'])
    await flushPromises()
    expect(wrapper.find('.safe-report-markdown').exists()).toBe(false)
    expect(document.body.querySelector('[aria-label="选择资料库"]')?.textContent).toContain(
      '资料 B',
    )
  })

  it('protects editor close, file selection, route focus changes and leaving with one dirty draft', async () => {
    mountPage()
    await flushPromises()
    await openFile()
    const editor = await editSelectedFile()
    await setEditor(editor, 'Dirty route draft')
    vi.mocked(window.confirm).mockReturnValue(false)
    buttonByText('取消').click()
    await flushPromises()
    expect(editor.isConnected).toBe(true)
    await openFile('b.txt')
    expect(editor.value).toBe('Dirty route draft')
    const leaveGuard = mocks.beforeRouteLeave.mock.calls[0]?.[0] as () => boolean
    const updateGuard = mocks.beforeRouteUpdate.mock.calls[0]?.[0] as (to: {
      query: { focus: string }
    }) => boolean
    expect(leaveGuard()).toBe(false)
    expect(updateGuard({ query: { focus: 'set-b' } })).toBe(false)
    expect(window.confirm).toHaveBeenCalledWith('当前资料库文件有未保存修改，确定放弃这些修改吗？')
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
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['library-catalog'] })
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
    Object.defineProperty(input, 'files', {
      configurable: true,
      value: [new File(['uploaded'], 'upload.md', { type: 'text/markdown' })],
    })
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()
    buttonByText('nested').click()
    await flushPromises()
    buttonByText('deep').click()
    await flushPromises()
    await openFile('c.md')
    pendingRead.resolve('uploaded')
    await flushPromises()
    expect(mocks.writeKnowledgeFile).toHaveBeenCalledWith('set-a', 'upload.md', 'uploaded')
    expect(wrapper.find('.safe-report-markdown').text()).toBe('C body')
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['knowledgeFiles', 'set-a'] })
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['library-catalog'] })
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
    const selection = wrapper.find('.knowledge-navigation-item[title="a.md"]').element
    expect(trigger.parentElement).toBe(selection.parentElement)
    expect(wrapper.find('button button').exists()).toBe(false)
    expect(wrapper.find('.knowledge-information').text()).not.toContain('删除文件')
    expect(wrapper.find('.knowledge-information').text()).not.toContain('归档资料库')
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

  it('file row menus open by keyboard through a portal and deleting the current last file returns to the list', async () => {
    mocks.deleteKnowledgeFile.mockResolvedValueOnce(undefined)
    mocks.invalidateQueries.mockImplementation(async ({ queryKey }: { queryKey: string[] }) => {
      if (queryKey[0] === 'knowledgeFiles')
        mocks.knowledgeFilesQueryData!.value = knowledgeFiles.filter(
          (file) => file.path !== 'nested/deep/c.md',
        )
    })
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('input[aria-label="搜索当前库的文件名或路径"]').setValue('c.md')
    await openFile('c.md')
    buttonByText('返回文件列表').click()
    await flushPromises()
    const trigger = fileMenuTrigger('nested/deep/c.md')
    expect(wrapper.findAll('.knowledge-file-menu-trigger')).toHaveLength(1)
    expect(wrapper.findAll('.knowledge-navigation-item--directory')).toHaveLength(2)
    trigger.focus()
    trigger.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await flushPromises()
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    expect(wrapper.find('.knowledge-navigation [role="menu"]').exists()).toBe(false)
    const item = document.body.querySelector<HTMLElement>('[role="menuitem"]')!
    expect(item.textContent).toContain('删除文件')
    item.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain(
      'nested/deep/c.md',
    )
    buttonByText('确认删除').click()
    await flushPromises()
    expect(mocks.deleteKnowledgeFile).toHaveBeenCalledExactlyOnceWith('set-a', 'nested/deep/c.md')
    expect(wrapper.find('.knowledge-workspace').classes()).not.toContain(
      'knowledge-workspace--reading',
    )
    expect(wrapper.find('.safe-report-markdown').exists()).toBe(false)
    await new Promise((resolve) => window.setTimeout(resolve, 60))
    expect(document.activeElement).toBe(buttonByText('新建文件'))
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
    await selectSet('set-b')
    await selectSet('set-a')
    await openFile('b.txt')
    const search = wrapper.find('input[aria-label="搜索当前库的文件名或路径"]')
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
    vi.mocked(window.confirm).mockReturnValue(false)
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
    expect(window.confirm).toHaveBeenCalledWith('当前资料库文件有未保存修改，确定放弃这些修改吗？')
    expect(editor.value).toBe('B unsaved draft')
  })

  it.each([
    { builtin: true, referenced_by: [], reason: '内置资料库不可归档' },
    { builtin: false, referenced_by: ['project-a'], reason: '资料库已被项目引用，不能归档' },
  ])(
    'library menu preserves archive restrictions: $reason',
    async ({ builtin, referenced_by, reason }) => {
      mocks.knowledgeSets[0] = { ...knowledgeSets[0], builtin, referenced_by }
      mountPage()
      await flushPromises()
      document.body.querySelector<HTMLButtonElement>('[aria-label="资料库更多操作"]')!.click()
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
    const trigger = document.body.querySelector<HTMLButtonElement>('[aria-label="资料库更多操作"]')!
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
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['library-catalog'] })
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
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['library-catalog'] })
    const callsBeforeUnmount = clearTimeoutSpy.mock.calls.length
    wrapper.unmount()
    expect(clearTimeoutSpy.mock.calls.length).toBeGreaterThan(callsBeforeUnmount)
  })

  it('guards duplicate library creation and retains a newer key typed during the request', async () => {
    mountPage()
    await flushPromises()
    wrappers.at(-1)!.findComponent(DropdownMenu).vm.$emit('select', 'create-library')
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
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['library-catalog'] })
  })
})
