import { flushPromises, mount } from '@vue/test-utils'
import { ref, unref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { KnowledgeFile, KnowledgeFileContent, KnowledgeSet } from '@/api/types'
import KnowledgeConsole from './KnowledgeConsole.vue'

const mocks = vi.hoisted(() => ({
  knowledgeSets: [] as KnowledgeSet[],
  knowledgeFiles: [] as KnowledgeFile[],
  createKnowledgeSet: vi.fn(),
  deleteKnowledgeFile: vi.fn(),
  deleteKnowledgeSet: vi.fn(),
  listAdvisoryLibraryRevisions: vi.fn(),
  listKnowledgeFiles: vi.fn(),
  listKnowledgeSets: vi.fn(),
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
}))

vi.mock('@/api/client', () => ({
  createKnowledgeSet: mocks.createKnowledgeSet,
  deleteKnowledgeFile: mocks.deleteKnowledgeFile,
  deleteKnowledgeSet: mocks.deleteKnowledgeSet,
  listAdvisoryLibraryRevisions: mocks.listAdvisoryLibraryRevisions,
  listKnowledgeFiles: mocks.listKnowledgeFiles,
  listKnowledgeSets: mocks.listKnowledgeSets,
  readKnowledgeFile: mocks.readKnowledgeFile,
  writeKnowledgeFile: mocks.writeKnowledgeFile,
}))

vi.mock('@/features/projects/projectResources', () => ({
  readUtf8File: mocks.readUtf8File,
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => mocks.uiStore,
}))

vi.mock('vue-router', () => ({
  onBeforeRouteLeave: mocks.beforeRouteLeave,
  useRoute: () => mocks.route,
  useRouter: () => mocks.router,
}))

vi.mock('@tanstack/vue-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown }) => {
    const resolvedKey = unref(queryKey)
    const key = Array.isArray(resolvedKey) ? resolvedKey : []
    if (key[0] === 'knowledgeSets') {
      return {
        data: ref(mocks.knowledgeSets),
        isLoading: ref(false),
        isError: ref(false),
        error: ref(null),
        refetch: mocks.knowledgeSetsRefetch,
      }
    }
    if (key[0] === 'knowledgeRevisions') {
      return {
        data: ref([]),
        isLoading: ref(false),
        refetch: mocks.knowledgeRevisionsRefetch,
      }
    }
    return {
      data: ref(mocks.knowledgeFiles),
      isLoading: ref(false),
      isError: ref(false),
      error: ref(null),
      refetch: mocks.knowledgeFilesRefetch,
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
  {
    key: 'set-a',
    path: '/knowledge/set-a',
    builtin: false,
    file_count: 2,
    referenced_by: [],
  },
  {
    key: 'set-b',
    path: '/knowledge/set-b',
    builtin: false,
    file_count: 2,
    referenced_by: [],
  },
]

const knowledgeFiles: KnowledgeFile[] = [
  { name: 'a.md', path: 'a.md', size: 1, warnings: [] },
  { name: 'b.md', path: 'b.md', size: 1, warnings: [] },
  { name: 'c.md', path: 'nested/c.md', size: 1, warnings: [] },
]

const contents: Record<string, KnowledgeFileContent> = {
  'a.md': { ...knowledgeFiles[0], content: 'A body' },
  'b.md': { ...knowledgeFiles[1], content: 'B body' },
  'nested/c.md': { ...knowledgeFiles[2], content: 'C body' },
}

const wrappers: VueWrapper[] = []

function mountPage() {
  const wrapper = mount(KnowledgeConsole, { attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}

function buttonByText(text: string) {
  const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.includes(text),
  )
  if (!button) throw new Error(`找不到按钮：${text}`)
  return button
}

function deferred<T>() {
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
  mocks.knowledgeFiles = knowledgeFiles.map((file) => ({ ...file }))
  mocks.route.query = {}
  mocks.uiStore.globalRefreshKey = 0

  mocks.createKnowledgeSet.mockReset()
  mocks.deleteKnowledgeFile.mockReset()
  mocks.deleteKnowledgeSet.mockReset()
  mocks.listAdvisoryLibraryRevisions.mockReset().mockResolvedValue([])
  mocks.listKnowledgeFiles.mockReset().mockResolvedValue(mocks.knowledgeFiles)
  mocks.listKnowledgeSets.mockReset().mockResolvedValue(mocks.knowledgeSets)
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
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('KnowledgeConsole request ownership', () => {
  it('uses shared controls while retaining native upload and domain navigation buttons', async () => {
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.findAll('.ui-input')).toHaveLength(2)
    expect(wrapper.findAll('.ui-form-field')).toHaveLength(1)
    expect(wrapper.findAll('.ui-icon-button')).toHaveLength(3)
    expect(wrapper.findAll('.ui-button').length).toBeGreaterThanOrEqual(4)
    expect(wrapper.find('.form-input').exists()).toBe(false)
    expect(wrapper.find('.btn-primary').exists()).toBe(false)
    expect(wrapper.find('.btn-secondary').exists()).toBe(false)
    expect(wrapper.find('.btn-success').exists()).toBe(false)
    expect(wrapper.find('.btn-danger').exists()).toBe(false)
    expect(wrapper.find('input[type="file"]').exists()).toBe(true)
    expect(wrapper.findAll('button.knowledge-set-item').length).toBeGreaterThan(0)
  })

  it('honors the initial focused set', async () => {
    mocks.route.query = { focus: 'set-b' }
    mountPage()
    await flushPromises()

    expect(buttonByText('set-b').classList.contains('active')).toBe(true)
    expect(mocks.router.replace).not.toHaveBeenCalledWith({ query: { focus: 'set-a' } })
  })

  it('aborts and suppresses a stale A to B to A file read', async () => {
    mountPage()
    await flushPromises()

    buttonByText('a.md').click()
    await flushPromises()
    expect(
      document.body.querySelector<HTMLTextAreaElement>('[data-testid="knowledge-editor"]')?.value,
    ).toBe('A body')

    const pending = deferred<KnowledgeFileContent>()
    mocks.readKnowledgeFile.mockReturnValueOnce(pending.promise)
    buttonByText('b.md').click()
    await flushPromises()
    const signal = mocks.readKnowledgeFile.mock.calls.at(-1)?.[2] as AbortSignal

    buttonByText('a.md').click()
    await flushPromises()
    expect(signal.aborted).toBe(true)

    pending.reject(new DOMException('Aborted', 'AbortError'))
    await flushPromises()

    expect(
      document.body.querySelector<HTMLTextAreaElement>('[data-testid="knowledge-editor"]')?.value,
    ).toBe('A body')
    expect(mocks.uiStore.showToast).not.toHaveBeenCalledWith(
      expect.stringContaining('读取资料库文件失败'),
      'danger',
    )
  })

  it('does not apply a file result after the knowledge set visit changes', async () => {
    mountPage()
    await flushPromises()

    const pending = deferred<KnowledgeFileContent>()
    mocks.readKnowledgeFile.mockReturnValueOnce(pending.promise)
    buttonByText('a.md').click()
    await flushPromises()
    buttonByText('set-b').click()
    await flushPromises()

    pending.resolve(contents['a.md'])
    await flushPromises()

    expect(document.body.textContent).toContain('set-b')
    expect(
      document.body.querySelector<HTMLTextAreaElement>('[data-testid="knowledge-editor"]')?.value,
    ).toBe('')
  })

  it('retains synchronous dirty confirmation for selection and route leave', async () => {
    mountPage()
    await flushPromises()
    buttonByText('a.md').click()
    await flushPromises()

    const editor = document.body.querySelector<HTMLTextAreaElement>(
      '[data-testid="knowledge-editor"]',
    )
    if (!editor) throw new Error('找不到资料库编辑器')
    editor.value = 'Dirty route draft'
    editor.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()

    vi.mocked(window.confirm).mockReturnValue(false)
    buttonByText('b.md').click()
    await flushPromises()
    expect(window.confirm).toHaveBeenCalledWith('当前资料库文件有未保存修改，确定放弃这些修改吗？')
    expect(editor.value).toBe('Dirty route draft')

    const leaveGuard = mocks.beforeRouteLeave.mock.calls[0]?.[0] as () => boolean
    expect(leaveGuard()).toBe(false)
    expect(window.confirm).toHaveBeenCalledTimes(2)
  })

  it('keeps continued edits and resets to the accepted save baseline', async () => {
    mountPage()
    await flushPromises()
    buttonByText('a.md').click()
    await flushPromises()

    const editor = document.body.querySelector<HTMLTextAreaElement>(
      '[data-testid="knowledge-editor"]',
    )
    if (!editor) throw new Error('找不到资料库编辑器')
    editor.value = 'First save'
    editor.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()

    const pending = deferred<KnowledgeFileContent>()
    mocks.writeKnowledgeFile.mockReturnValueOnce(pending.promise)
    buttonByText('保存文件').click()
    await flushPromises()

    editor.value = 'Continued edit'
    editor.dispatchEvent(new Event('input', { bubbles: true }))
    pending.resolve({ ...contents['a.md'], content: 'First save' })
    await flushPromises()
    expect(editor.value).toBe('Continued edit')

    buttonByText('重置').click()
    await flushPromises()
    expect(editor.value).toBe('First save')
  })

  it('does not let an upload overwrite a newer directory and file selection', async () => {
    mountPage()
    await flushPromises()
    buttonByText('a.md').click()
    await flushPromises()

    const pendingRead = deferred<string>()
    mocks.readUtf8File.mockReturnValueOnce(pendingRead.promise)
    mocks.writeKnowledgeFile.mockResolvedValueOnce({
      name: 'upload.md',
      path: 'upload.md',
      size: 8,
      warnings: [],
      content: 'uploaded',
    })
    const uploadInput = document.body.querySelector<HTMLInputElement>('input[type="file"]')
    if (!uploadInput) throw new Error('找不到上传控件')
    Object.defineProperty(uploadInput, 'files', {
      configurable: true,
      value: [new File(['uploaded'], 'upload.md', { type: 'text/markdown' })],
    })
    uploadInput.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()

    buttonByText('nested').click()
    await flushPromises()
    expect(
      document.body.querySelector<HTMLTextAreaElement>('[data-testid="knowledge-editor"]')?.value,
    ).toBe('C body')

    pendingRead.resolve('uploaded')
    await flushPromises()

    expect(mocks.writeKnowledgeFile).toHaveBeenCalledWith('knowledge_base', 'upload.md', 'uploaded')
    expect(
      document.body.querySelector<HTMLTextAreaElement>('[data-testid="knowledge-editor"]')?.value,
    ).toBe('C body')
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['knowledgeFiles', 'knowledge_base'],
    })
    expect(mocks.uiStore.showToast).not.toHaveBeenCalledWith('文件已上传', 'success')
  })

  it('deletes the captured knowledge set, blocks duplicate confirmation, and restores trigger focus', async () => {
    const pending = deferred<void>()
    mocks.deleteKnowledgeSet.mockReturnValueOnce(pending.promise)
    mountPage()
    await flushPromises()

    buttonByText('set-a').click()
    await flushPromises()
    const trigger = buttonByText('删除资料库')
    trigger.focus()
    trigger.click()
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain('set-a')

    buttonByText('set-b').click()
    const confirm = buttonByText('确认删除')
    confirm.click()
    confirm.click()
    await flushPromises()
    expect(mocks.deleteKnowledgeSet).toHaveBeenCalledTimes(1)
    expect(mocks.deleteKnowledgeSet).toHaveBeenCalledWith('set-a')
    expect(window.confirm).not.toHaveBeenCalled()

    pending.resolve()
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
    await new Promise((resolve) => window.setTimeout(resolve, 60))
    expect(document.activeElement).toBe(trigger)
  })

  it('keeps file edits made after delete starts and keeps a failed confirmation open for retry', async () => {
    mountPage()
    await flushPromises()
    buttonByText('a.md').click()
    await flushPromises()

    const first = deferred<void>()
    mocks.deleteKnowledgeFile.mockReturnValueOnce(first.promise)
    const trigger = buttonByText('删除文件')
    trigger.click()
    await flushPromises()
    buttonByText('确认删除').click()
    await flushPromises()

    const editor = document.body.querySelector<HTMLTextAreaElement>(
      '[data-testid="knowledge-editor"]',
    )
    if (!editor) throw new Error('找不到资料库编辑器')
    editor.value = 'Edit made while delete is pending'
    editor.dispatchEvent(new Event('input', { bubbles: true }))
    first.reject(new Error('first failure'))
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')).not.toBeNull()
    expect(editor.value).toBe('Edit made while delete is pending')

    const second = deferred<void>()
    mocks.deleteKnowledgeFile.mockReturnValueOnce(second.promise)
    buttonByText('确认删除').click()
    await flushPromises()
    editor.value = 'Newer edit after retry started'
    editor.dispatchEvent(new Event('input', { bubbles: true }))
    second.resolve()
    await flushPromises()

    expect(mocks.deleteKnowledgeFile).toHaveBeenNthCalledWith(1, 'knowledge_base', 'a.md')
    expect(mocks.deleteKnowledgeFile).toHaveBeenNthCalledWith(2, 'knowledge_base', 'a.md')
    expect(editor.value).toBe('Newer edit after retry started')
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
  })

  it('cancels a pending delete focus restore when the page unmounts', async () => {
    mocks.deleteKnowledgeFile.mockResolvedValueOnce(undefined)
    const clearTimeoutSpy = vi.spyOn(window, 'clearTimeout')
    const wrapper = mountPage()
    await flushPromises()
    buttonByText('a.md').click()
    await flushPromises()

    buttonByText('删除文件').click()
    await flushPromises()
    buttonByText('确认删除').click()
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()

    const callsBeforeUnmount = clearTimeoutSpy.mock.calls.length
    wrapper.unmount()
    expect(clearTimeoutSpy.mock.calls.length).toBeGreaterThan(callsBeforeUnmount)
  })

  it('guards duplicate create submission and preserves newer input while refreshing sets', async () => {
    mountPage()
    await flushPromises()

    const input = document.body.querySelector<HTMLInputElement>(
      'input[placeholder="new_knowledge_set"]',
    )
    const createButton =
      document.body.querySelector<HTMLButtonElement>('button[title="创建资料库"]')
    if (!input || !createButton) throw new Error('找不到资料库创建控件')
    input.value = 'created'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    const pending = deferred<KnowledgeSet>()
    mocks.createKnowledgeSet.mockReturnValueOnce(pending.promise)

    createButton.click()
    input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }))
    await flushPromises()
    expect(mocks.createKnowledgeSet).toHaveBeenCalledTimes(1)

    input.value = 'next-input'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    pending.resolve({ ...knowledgeSets[0], key: 'created', path: '/knowledge/created' })
    await flushPromises()

    expect(mocks.knowledgeSetsRefetch).toHaveBeenCalled()
    expect(input.value).toBe('next-input')
  })
})
