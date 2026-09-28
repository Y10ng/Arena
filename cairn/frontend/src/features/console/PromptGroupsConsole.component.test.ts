import { flushPromises, mount } from '@vue/test-utils'
import { reactive, ref, unref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { PromptFile, PromptFileContent, PromptGroup } from '@/api/types'
import { Select } from '@/components/ui'
import PromptGroupsConsole from './PromptGroupsConsole.vue'

const promptGroupsQueryData = ref<PromptGroup[]>([])

const mocks = vi.hoisted(() => ({
  promptGroups: [] as PromptGroup[],
  promptFiles: [] as PromptFile[],
  createPromptGroup: vi.fn(),
  deletePromptGroup: vi.fn(),
  listPromptFiles: vi.fn(),
  listPromptGroups: vi.fn(),
  readPromptFile: vi.fn(),
  repairPromptGroup: vi.fn(),
  restorePromptGroup: vi.fn(),
  validatePromptGroup: vi.fn(),
  writePromptFile: vi.fn(),
  promptGroupsRefetch: vi.fn(),
  promptFilesRefetch: vi.fn(),
  invalidateQueries: vi.fn(),
  router: { replace: vi.fn() },
  route: { query: {} as Record<string, unknown> },
  uiStore: { globalRefreshKey: 0, showToast: vi.fn() },
  beforeRouteLeave: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  createPromptGroup: mocks.createPromptGroup,
  deletePromptGroup: mocks.deletePromptGroup,
  listPromptFiles: mocks.listPromptFiles,
  listPromptGroups: mocks.listPromptGroups,
  readPromptFile: mocks.readPromptFile,
  repairPromptGroup: mocks.repairPromptGroup,
  restorePromptGroup: mocks.restorePromptGroup,
  validatePromptGroup: mocks.validatePromptGroup,
  writePromptFile: mocks.writePromptFile,
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

    if (key[0] === 'promptGroups') {
      return {
        data: promptGroupsQueryData,
        isLoading: ref(false),
        isError: ref(false),
        error: ref(null),
        refetch: mocks.promptGroupsRefetch,
      }
    }

    return {
      data: ref(mocks.promptFiles),
      isLoading: ref(false),
      isError: ref(false),
      error: ref(null),
      refetch: mocks.promptFilesRefetch,
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
      '<textarea data-testid="prompt-editor" :value="modelValue" :data-file-name="fileName" :disabled="disabled" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
}))

const promptGroup: PromptGroup = {
  key: 'default',
  path: '/prompts/default',
  builtin: false,
  managed: true,
  file_count: 2,
  referenced_by: [],
  overrides_builtin: false,
  capabilities: ['exploration'],
  visibility: 'visible',
  valid: true,
  warnings: [],
}

const promptFiles: PromptFile[] = [
  {
    name: 'bootstrap.md',
    path: 'bootstrap.md',
    size: 120,
    required_tokens: ['{{ goal }}'],
    missing_tokens: [],
  },
  {
    name: 'reason.md',
    path: 'reason.md',
    size: 180,
    required_tokens: ['{{ goal }}', '{{ origin }}'],
    missing_tokens: ['{{ origin }}'],
  },
]

const promptContents: Record<string, PromptFileContent> = {
  'bootstrap.md': { ...promptFiles[0], content: 'Bootstrap {{ goal }}' },
  'reason.md': { ...promptFiles[1], content: 'Reason {{ goal }} {{ origin }}' },
}

const wrappers: VueWrapper[] = []

function mountPage() {
  const wrapper = mount(PromptGroupsConsole, { attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}

function tabByName(name: string) {
  const tab = [...document.body.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(
    (candidate) => candidate.textContent?.includes(name),
  )
  if (!tab) throw new Error(`找不到 Prompt 文件页签：${name}`)
  return tab
}

function selectTab(name: string) {
  tabByName(name).dispatchEvent(
    new MouseEvent('mousedown', {
      bubbles: true,
      button: 0,
    }),
  )
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
  mocks.promptGroups = [{ ...promptGroup }]
  promptGroupsQueryData.value = mocks.promptGroups
  mocks.promptFiles = promptFiles.map((file) => ({ ...file }))
  mocks.route = reactive({ query: {} as Record<string, unknown> })
  mocks.uiStore.globalRefreshKey = 0

  mocks.createPromptGroup.mockReset()
  mocks.deletePromptGroup.mockReset()
  mocks.listPromptFiles.mockReset().mockResolvedValue(mocks.promptFiles)
  mocks.listPromptGroups.mockReset().mockResolvedValue(mocks.promptGroups)
  mocks.readPromptFile
    .mockReset()
    .mockImplementation((_group: string, path: string) => Promise.resolve(promptContents[path]))
  mocks.repairPromptGroup.mockReset()
  mocks.restorePromptGroup.mockReset()
  mocks.validatePromptGroup.mockReset().mockResolvedValue({
    valid: true,
    missing_files: [],
    missing_tokens: {},
    warnings: [],
  })
  mocks.writePromptFile.mockReset()
  mocks.promptGroupsRefetch.mockReset().mockResolvedValue(undefined)
  mocks.promptFilesRefetch.mockReset().mockResolvedValue(undefined)
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

describe('PromptGroupsConsole Tabs migration', () => {
  it('uses shared controls while retaining domain-specific group navigation', async () => {
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.findAll('.ui-input')).toHaveLength(1)
    expect(wrapper.findAll('.ui-select__trigger')).toHaveLength(1)
    expect(wrapper.findAll('.ui-icon-button')).toHaveLength(2)
    expect(wrapper.findAll('.ui-button').length).toBeGreaterThanOrEqual(4)
    expect(wrapper.find('.form-input').exists()).toBe(false)
    expect(wrapper.find('.form-select').exists()).toBe(false)
    expect(wrapper.find('.btn-primary').exists()).toBe(false)
    expect(wrapper.find('.btn-secondary').exists()).toBe(false)
    expect(wrapper.find('.btn-success').exists()).toBe(false)
    expect(wrapper.find('.btn-danger').exists()).toBe(false)
    expect(wrapper.findAll('button.knowledge-set-item')).toHaveLength(1)
  })

  it('honors the initial focused group without replacing it with the first group', async () => {
    mocks.promptGroups = [
      { ...promptGroup },
      { ...promptGroup, key: 'focused', path: '/prompts/focused' },
    ]
    promptGroupsQueryData.value = mocks.promptGroups
    mocks.route.query = { focus: 'focused' }

    mountPage()
    await flushPromises()

    expect(document.body.textContent).toContain('focused')
    expect(
      [...document.body.querySelectorAll<HTMLButtonElement>('.knowledge-set-item')]
        .find((button) => button.textContent?.includes('focused'))
        ?.classList.contains('active'),
    ).toBe(true)
    expect(mocks.router.replace).not.toHaveBeenCalledWith({ query: { focus: 'default' } })
  })

  it('hides compatibility and test builtins while labeling visible report-only groups', async () => {
    mocks.promptGroups = [
      { ...promptGroup },
      {
        ...promptGroup,
        key: 'default-durable-report-v1',
        path: '/prompts/default-durable-report-v1',
        builtin: true,
        managed: false,
        capabilities: ['durable-report'],
      },
      {
        ...promptGroup,
        key: 'default-report',
        path: '/prompts/default-report',
        builtin: true,
        managed: true,
        overrides_builtin: true,
        visibility: 'compatibility-only',
      },
      {
        ...promptGroup,
        key: 'mock',
        path: '/prompts/mock',
        builtin: true,
        managed: false,
        visibility: 'test-only',
      },
      {
        ...promptGroup,
        key: 'custom-legacy-metadata',
        path: '/prompts/custom-legacy-metadata',
        visibility: 'compatibility-only',
      },
    ]
    promptGroupsQueryData.value = mocks.promptGroups

    const wrapper = mountPage()
    await flushPromises()

    const groupButtons = wrapper.findAll('button.knowledge-set-item')
    expect(groupButtons.map((button) => button.text())).toEqual(
      expect.arrayContaining([
        expect.stringContaining('default'),
        expect.stringContaining('default-durable-report-v1'),
        expect.stringContaining('custom-legacy-metadata'),
      ]),
    )
    expect(groupButtons).toHaveLength(3)
    expect(wrapper.text()).toContain('仅报告')

    const vm = wrapper.vm as unknown as { promptGroups: PromptGroup[] }
    expect(vm.promptGroups.map((group) => group.key)).toEqual([
      'default',
      'default-durable-report-v1',
      'custom-legacy-metadata',
    ])
    expect(wrapper.findComponent(Select).props('options')).toEqual([
      { value: 'default', label: 'default' },
      { value: 'default-durable-report-v1', label: 'default-durable-report-v1' },
      { value: 'custom-legacy-metadata', label: 'custom-legacy-metadata' },
    ])
  })

  it('does not select a hidden group from a normal-console deep link', async () => {
    mocks.promptGroups = [
      { ...promptGroup },
      {
        ...promptGroup,
        key: 'default-report',
        path: '/prompts/default-report',
        builtin: true,
        managed: false,
        visibility: 'compatibility-only',
      },
    ]
    promptGroupsQueryData.value = mocks.promptGroups
    mocks.route.query = { focus: 'default-report' }

    mountPage()
    await flushPromises()

    const buttons = [...document.body.querySelectorAll<HTMLButtonElement>('.knowledge-set-item')]
    expect(buttons).toHaveLength(1)
    expect(buttons[0]?.textContent).toContain('default')
    expect(buttons[0]?.classList.contains('active')).toBe(true)
    expect(mocks.router.replace).toHaveBeenCalledWith({ query: { focus: 'default' } })
  })

  it('renders categorized file tabs with accessible state and validation metadata', async () => {
    mountPage()
    await flushPromises()

    expect(document.body.querySelector('[role="tablist"]')).not.toBeNull()
    expect(document.body.querySelectorAll('[role="tab"]')).toHaveLength(2)
    expect(tabByName('reason.md').getAttribute('aria-selected')).toBe('true')
    expect(tabByName('bootstrap.md').textContent).toContain('启动')
    expect(tabByName('bootstrap.md').textContent).toContain('1 token')
    expect(tabByName('reason.md').textContent).toContain('推理')
    expect(tabByName('reason.md').textContent).toContain('1 缺失')
    expect(document.body.querySelector('[role="tabpanel"]')).not.toBeNull()
  })

  it('keeps the current file selected when dirty changes are not discarded', async () => {
    mountPage()
    await flushPromises()

    const editor = document.body.querySelector<HTMLTextAreaElement>('[data-testid="prompt-editor"]')
    if (!editor) throw new Error('找不到 Prompt 编辑器')
    editor.value = 'Changed draft'
    editor.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()

    vi.mocked(window.confirm).mockReturnValueOnce(false)
    selectTab('bootstrap.md')
    await flushPromises()

    expect(window.confirm).toHaveBeenCalledWith('Prompt 文件存在未保存修改，确定要放弃吗？')
    expect(tabByName('reason.md').getAttribute('aria-selected')).toBe('true')
    expect(mocks.readPromptFile).toHaveBeenCalledTimes(1)
    expect(editor.value).toBe('Changed draft')
  })

  it('retains synchronous dirty confirmation for route leave', async () => {
    mountPage()
    await flushPromises()

    const editor = document.body.querySelector<HTMLTextAreaElement>('[data-testid="prompt-editor"]')
    if (!editor) throw new Error('找不到 Prompt 编辑器')
    editor.value = 'Dirty route draft'
    editor.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()

    vi.mocked(window.confirm).mockReturnValueOnce(false)
    const leaveGuard = mocks.beforeRouteLeave.mock.calls[0]?.[0] as () => boolean
    expect(leaveGuard()).toBe(false)
    expect(window.confirm).toHaveBeenCalledWith('Prompt 文件存在未保存修改，确定要放弃吗？')
  })

  it('commits the selected tab only after the requested file finishes loading', async () => {
    mountPage()
    await flushPromises()

    const pending = deferred<PromptFileContent>()
    mocks.readPromptFile.mockReturnValueOnce(pending.promise)
    selectTab('bootstrap.md')
    await flushPromises()

    expect(tabByName('reason.md').getAttribute('aria-selected')).toBe('true')
    expect(mocks.readPromptFile).toHaveBeenLastCalledWith(
      'default',
      'bootstrap.md',
      expect.any(AbortSignal),
    )

    pending.resolve(promptContents['bootstrap.md'])
    await flushPromises()

    expect(tabByName('bootstrap.md').getAttribute('aria-selected')).toBe('true')
    expect(
      document.body.querySelector<HTMLTextAreaElement>('[data-testid="prompt-editor"]')?.dataset
        .fileName,
    ).toBe('bootstrap.md')
    expect(mocks.router.replace).toHaveBeenLastCalledWith({
      query: { focus: 'default', file: 'bootstrap.md' },
    })
  })

  it('suppresses a stale A to B to A file result and abort error', async () => {
    mountPage()
    await flushPromises()

    const pending = deferred<PromptFileContent>()
    mocks.readPromptFile.mockReturnValueOnce(pending.promise)
    selectTab('bootstrap.md')
    await flushPromises()
    const signal = mocks.readPromptFile.mock.calls.at(-1)?.[2] as AbortSignal

    selectTab('reason.md')
    await flushPromises()
    expect(signal.aborted).toBe(true)

    pending.reject(new DOMException('Aborted', 'AbortError'))
    await flushPromises()

    expect(tabByName('reason.md').getAttribute('aria-selected')).toBe('true')
    expect(mocks.uiStore.showToast).not.toHaveBeenCalledWith(
      expect.stringContaining('读取 Prompt 文件失败'),
      'danger',
    )
  })

  it('suppresses stale validation errors after returning to the prior group', async () => {
    mocks.promptGroups = [
      { ...promptGroup },
      { ...promptGroup, key: 'other', path: '/prompts/other' },
    ]
    promptGroupsQueryData.value = mocks.promptGroups
    mountPage()
    await flushPromises()

    const pending = deferred<{
      valid: boolean
      missing_files: string[]
      missing_tokens: Record<string, string[]>
      warnings: string[]
    }>()
    mocks.validatePromptGroup.mockReturnValueOnce(pending.promise)
    const groupButtons = [
      ...document.body.querySelectorAll<HTMLButtonElement>('.knowledge-set-item'),
    ]
    groupButtons.find((button) => button.textContent?.includes('other'))?.click()
    await flushPromises()
    const signal = mocks.validatePromptGroup.mock.calls.at(-1)?.[1] as AbortSignal

    groupButtons.find((button) => button.textContent?.includes('default'))?.click()
    await flushPromises()
    expect(signal.aborted).toBe(true)

    pending.reject(new Error('late validation failure'))
    await flushPromises()
    expect(mocks.uiStore.showToast).not.toHaveBeenCalledWith(
      expect.stringContaining('late validation failure'),
      'danger',
    )
  })

  it('rolls back focus and file atomically after one dirty rejection', async () => {
    mocks.promptGroups = [
      { ...promptGroup },
      { ...promptGroup, key: 'other', path: '/prompts/other' },
    ]
    promptGroupsQueryData.value = mocks.promptGroups
    mountPage()
    await flushPromises()

    const editor = document.body.querySelector<HTMLTextAreaElement>('[data-testid="prompt-editor"]')
    if (!editor) throw new Error('找不到 Prompt 编辑器')
    editor.value = 'Dirty draft'
    editor.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    vi.mocked(window.confirm).mockReturnValueOnce(false)

    mocks.route.query = { focus: 'other', file: 'bootstrap.md' }
    await flushPromises()

    expect(window.confirm).toHaveBeenCalledTimes(1)
    expect(mocks.router.replace).toHaveBeenLastCalledWith({
      query: { focus: 'default', file: 'reason.md' },
    })
    expect(editor.value).toBe('Dirty draft')
    expect(mocks.readPromptFile).toHaveBeenCalledTimes(1)
  })

  it('keeps manual validation stale when the draft changes while validation is pending', async () => {
    mountPage()
    await flushPromises()

    const pending = deferred<{
      valid: boolean
      missing_files: string[]
      missing_tokens: Record<string, string[]>
      warnings: string[]
    }>()
    mocks.validatePromptGroup.mockReturnValueOnce(pending.promise)
    const validateButton = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('校验模板组'),
    )
    validateButton?.click()
    await flushPromises()

    const editor = document.body.querySelector<HTMLTextAreaElement>('[data-testid="prompt-editor"]')
    if (!editor) throw new Error('找不到 Prompt 编辑器')
    editor.value = 'Changed during validation {{ goal }} {{ origin }}'
    editor.dispatchEvent(new Event('input', { bubbles: true }))
    pending.resolve({ valid: true, missing_files: [], missing_tokens: {}, warnings: [] })
    await flushPromises()

    expect(document.body.textContent).toContain('当前草稿已变更，结果待重新校验')
  })

  it('keeps edits made while a prompt save is pending', async () => {
    mountPage()
    await flushPromises()

    const editor = document.body.querySelector<HTMLTextAreaElement>('[data-testid="prompt-editor"]')
    if (!editor) throw new Error('找不到 Prompt 编辑器')
    editor.value = 'First {{ goal }} {{ origin }}'
    editor.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()

    const pending = deferred<PromptFileContent>()
    mocks.writePromptFile.mockReturnValueOnce(pending.promise)
    const saveButton = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('保存文件'),
    )
    saveButton?.click()
    await flushPromises()

    editor.value = 'Continued {{ goal }} {{ origin }}'
    editor.dispatchEvent(new Event('input', { bubbles: true }))
    pending.resolve({ ...promptContents['reason.md'], content: 'First {{ goal }} {{ origin }}' })
    await flushPromises()

    expect(editor.value).toBe('Continued {{ goal }} {{ origin }}')
    expect(document.body.textContent).toContain('未保存')
  })

  it('creates from goal-driven prompts while guarding duplicate submission and newer input', async () => {
    mocks.promptGroups = [
      { ...promptGroup },
      { ...promptGroup, key: 'goal-driven-v1', path: '/prompts/goal-driven-v1' },
    ]
    promptGroupsQueryData.value = mocks.promptGroups
    mountPage()
    await flushPromises()

    const input = document.body.querySelector<HTMLInputElement>(
      'input[placeholder="new_prompt_group"]',
    )
    const createButton =
      document.body.querySelector<HTMLButtonElement>('button[title="创建模板组"]')
    if (!input || !createButton) throw new Error('找不到模板组创建控件')
    input.value = 'created'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    const pending = deferred<PromptGroup>()
    mocks.createPromptGroup.mockReturnValueOnce(pending.promise)

    createButton.click()
    input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }))
    await flushPromises()
    expect(mocks.createPromptGroup).toHaveBeenCalledTimes(1)
    expect(mocks.createPromptGroup).toHaveBeenCalledWith('created', 'goal-driven-v1')

    input.value = 'next-input'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    pending.resolve({ ...promptGroup, key: 'created', path: '/prompts/created' })
    await flushPromises()

    expect(mocks.promptGroupsRefetch).toHaveBeenCalled()
    expect(input.value).toBe('next-input')
  })

  it('deletes a captured group while locking its editor and enters a remaining group after refetch', async () => {
    const other = { ...promptGroup, key: 'other', path: '/prompts/other' }
    mocks.promptGroups = [{ ...promptGroup }, other]
    promptGroupsQueryData.value = mocks.promptGroups
    const pending = deferred<void>()
    mocks.deletePromptGroup.mockReturnValueOnce(pending.promise)
    mocks.promptGroupsRefetch.mockImplementationOnce(async () => {
      promptGroupsQueryData.value = [other]
    })
    mountPage()
    await flushPromises()

    const trigger = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('删除模板组'),
    )
    trigger?.click()
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain('default')

    const confirm = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('确认删除'),
    )
    confirm?.click()
    confirm?.click()
    await flushPromises()
    expect(mocks.deletePromptGroup).toHaveBeenCalledTimes(1)
    expect(window.confirm).not.toHaveBeenCalled()

    const editor = document.body.querySelector<HTMLTextAreaElement>('[data-testid="prompt-editor"]')
    expect(editor?.disabled).toBe(true)
    pending.resolve()
    await flushPromises()

    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
    expect(document.body.textContent).toContain('other')
    expect(
      [...document.body.querySelectorAll<HTMLButtonElement>('.knowledge-set-item')]
        .find((button) => button.textContent?.includes('other'))
        ?.classList.contains('active'),
    ).toBe(true)
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['promptFiles', 'default'] })
  })

  it.each([
    [
      'repair',
      { valid: false, overrides_builtin: false },
      '修复模板组',
      '确认修复',
      mocks.repairPromptGroup,
    ],
    [
      'restore',
      { valid: false, overrides_builtin: true, builtin: true },
      '恢复内置',
      '确认恢复',
      mocks.restorePromptGroup,
    ],
  ])(
    'keeps the captured %s confirmation open after failure and retries in place',
    async (_kind, overrides, label, confirmLabel, operation) => {
      mocks.promptGroups = [{ ...promptGroup, ...overrides }]
      promptGroupsQueryData.value = mocks.promptGroups
      operation.mockRejectedValueOnce(new Error('first failure')).mockResolvedValueOnce(undefined)
      const wrapper = mountPage()
      await flushPromises()

      const trigger = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
        (button) => button.textContent?.includes(label),
      )
      trigger?.focus()
      trigger?.click()
      await flushPromises()
      expect(window.confirm).not.toHaveBeenCalled()

      const confirm = () =>
        [...document.body.querySelectorAll<HTMLButtonElement>('button')].find((button) =>
          button.textContent?.includes(confirmLabel),
        )
      confirm()?.click()
      await flushPromises()
      expect(document.body.querySelector('[role="alertdialog"]')).not.toBeNull()

      confirm()?.click()
      await flushPromises()
      expect(operation).toHaveBeenCalledTimes(2)
      expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
      expect(wrapper.exists()).toBe(true)
      await new Promise((resolve) => window.setTimeout(resolve, 60))
      expect(document.activeElement).toBe(
        document.body.querySelector('[data-console-action="clone-prompt-group"]'),
      )
    },
  )

  it('cancels a pending dialog focus restore when the page unmounts', async () => {
    const clearTimeoutSpy = vi.spyOn(window, 'clearTimeout')
    const wrapper = mountPage()
    await flushPromises()

    document.body.querySelector<HTMLButtonElement>('button[title="克隆当前模板组"]')?.click()
    await flushPromises()
    const cancel = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.trim() === '取消',
    )
    cancel?.click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()

    const callsBeforeUnmount = clearTimeoutSpy.mock.calls.length
    wrapper.unmount()
    expect(clearTimeoutSpy.mock.calls.length).toBeGreaterThan(callsBeforeUnmount)
  })

  it('clones a frozen source through the dialog, validates input, retries failure, and never uses window.prompt', async () => {
    const other = { ...promptGroup, key: 'other', path: '/prompts/other' }
    mocks.promptGroups = [{ ...promptGroup }, other]
    promptGroupsQueryData.value = mocks.promptGroups
    const promptSpy = vi.spyOn(window, 'prompt')
    const copy = {
      ...promptGroup,
      key: 'default_copy',
      path: '/prompts/default_copy',
    }
    mocks.createPromptGroup
      .mockRejectedValueOnce(new Error('first failure'))
      .mockResolvedValueOnce(copy)
    mocks.promptGroupsRefetch.mockImplementation(async () => {
      if (mocks.createPromptGroup.mock.calls.length >= 2) {
        promptGroupsQueryData.value = [{ ...promptGroup }, other, copy]
      }
    })
    const wrapper = mountPage()
    await flushPromises()

    const trigger = document.body.querySelector<HTMLButtonElement>('button[title="克隆当前模板组"]')
    trigger?.focus()
    trigger?.click()
    await flushPromises()

    const input = document.body.querySelector<HTMLInputElement>('#clone-prompt-group-form input')
    if (!input) throw new Error('找不到克隆键值输入框')
    input.value = 'Bad Key'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    input.form?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flushPromises()
    expect(document.body.textContent).toContain('仅支持小写字母')
    expect(mocks.createPromptGroup).not.toHaveBeenCalled()

    input.value = 'default_copy'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    const otherButton = [
      ...document.body.querySelectorAll<HTMLButtonElement>('.knowledge-set-item'),
    ].find((button) => button.textContent?.includes('other'))
    otherButton?.click()
    input.form?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flushPromises()
    expect(mocks.createPromptGroup).toHaveBeenLastCalledWith('default_copy', 'default')
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
    await new Promise((resolve) => window.setTimeout(resolve, 0))

    const retryButton = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.textContent?.includes('创建副本'),
    )
    await vi.waitFor(() => expect(retryButton?.disabled).toBe(false))
    await (
      wrapper.vm as unknown as { submitClonePromptGroup: () => Promise<void> }
    ).submitClonePromptGroup()
    await flushPromises()
    expect(mocks.createPromptGroup).toHaveBeenCalledTimes(2)
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    await new Promise((resolve) => window.setTimeout(resolve, 60))
    expect(document.activeElement).toBe(trigger)
    expect(promptSpy).not.toHaveBeenCalled()
  })
})
