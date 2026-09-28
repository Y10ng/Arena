import { flushPromises, mount } from '@vue/test-utils'
import { reactive, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { Target } from '@/api/types'
import TargetsPage from './TargetsPage.vue'

const mocks = vi.hoisted(() => ({
  createProjectTarget: vi.fn(),
  getQueryData: vi.fn(),
  invalidateQueries: vi.fn(),
  listProjectTargets: vi.fn(),
  push: vi.fn(),
  refetch: vi.fn(),
  setQueryData: vi.fn(),
  showToast: vi.fn(),
  replaceProjectTargets: vi.fn(),
  updateProjectTarget: vi.fn(),
  useQuery: vi.fn(),
}))

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    createProjectTarget: mocks.createProjectTarget,
    listProjectTargets: mocks.listProjectTargets,
    updateProjectTarget: mocks.updateProjectTarget,
  }
})

vi.mock('@tanstack/vue-query', () => ({
  useQuery: mocks.useQuery,
  useQueryClient: () => ({
    getQueryData: mocks.getQueryData,
    invalidateQueries: mocks.invalidateQueries,
    setQueryData: mocks.setQueryData,
  }),
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mocks.push }),
}))

const workspaceStore = reactive({
  currentProjectId: 'project-a' as string | null,
  projectDetail: { project: { id: 'project-a' }, targets: [] as Target[] },
  replaceProjectTargets: mocks.replaceProjectTargets,
})

const uiStore = reactive({
  globalRefreshKey: 0,
  showToast: mocks.showToast,
})

vi.mock('@/stores/workspace', () => ({
  useWorkspaceStore: () => workspaceStore,
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => uiStore,
}))

let sequence = 0
function target(overrides: Partial<Target> = {}): Target {
  sequence += 1
  return {
    id: `target-${sequence}`,
    project_id: 'project-a',
    value: `asset-${sequence}.example.test`,
    type: 'domain',
    status: 'approved',
    source_fact_id: null,
    discovered_by_run_id: null,
    created_at: `2026-08-${String(sequence).padStart(2, '0')}T00:00:00Z`,
    ...overrides,
  }
}

const queryState = {
  data: ref<Target[] | undefined>([]),
  error: ref<Error | null>(null),
  isError: ref(false),
  isFetching: ref(false),
  isLoading: ref(false),
}

const wrappers: VueWrapper[] = []

function mountPage() {
  const wrapper = mount(TargetsPage, { attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}

function buttonByText(text: string): HTMLButtonElement {
  const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === text,
  )
  if (!button) throw new Error(`找不到按钮：${text}`)
  return button
}

function controlByLabel<T extends HTMLElement>(labelText: string): T {
  const label = [...document.body.querySelectorAll<HTMLLabelElement>('label')].find((candidate) =>
    candidate.textContent?.includes(labelText),
  )
  const control = label?.htmlFor ? document.getElementById(label.htmlFor) : null
  if (!control) throw new Error(`找不到控件：${labelText}`)
  return control as T
}

async function setInputByLabel(labelText: string, value: string) {
  const control = controlByLabel<HTMLInputElement>(labelText)
  control.value = value
  control.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
}

async function selectOption(labelText: string, optionText: string) {
  const control = controlByLabel<HTMLButtonElement>(labelText)
  control.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0 }))
  control.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
  control.click()
  await flushPromises()
  const option = [...document.body.querySelectorAll<HTMLElement>('[role="option"]')].find(
    (candidate) => candidate.textContent?.trim() === optionText,
  )
  if (!option) throw new Error(`找不到选项：${optionText}`)
  option.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
  option.click()
  await flushPromises()
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

beforeEach(() => {
  sequence = 0
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
  workspaceStore.currentProjectId = 'project-a'
  workspaceStore.projectDetail = { project: { id: 'project-a' }, targets: [] }
  uiStore.globalRefreshKey = 0
  queryState.data.value = []
  queryState.error.value = null
  queryState.isError.value = false
  queryState.isFetching.value = false
  queryState.isLoading.value = false

  for (const mock of Object.values(mocks)) mock.mockReset()
  mocks.createProjectTarget.mockResolvedValue(target())
  mocks.getQueryData.mockImplementation(() => queryState.data.value)
  mocks.invalidateQueries.mockResolvedValue(undefined)
  mocks.listProjectTargets.mockResolvedValue([])
  mocks.refetch.mockResolvedValue(undefined)
  mocks.setQueryData.mockImplementation(
    (_key, value: Target[] | ((old: Target[] | undefined) => Target[])) => {
      const next = typeof value === 'function' ? value(queryState.data.value) : value
      queryState.data.value = next
      return next
    },
  )
  mocks.updateProjectTarget.mockImplementation(
    async (_projectId: string, targetId: string, body: { status: Target['status'] }) => {
      const existing = queryState.data.value?.find((item) => item.id === targetId)
      if (!existing) throw new Error('目标不存在')
      return { ...existing, status: body.status }
    },
  )
  mocks.useQuery.mockReturnValue({ ...queryState, refetch: mocks.refetch })
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('TargetsPage', () => {
  it('uses the project-scoped typed query, labeled controls, accessible table, and guarded refresh', async () => {
    queryState.data.value = [
      target({
        id: 'target-a',
        value: 'api.example.test',
        source_fact_id: 'fact-1',
        discovered_by_run_id: 'run-123456789',
      }),
    ]
    const wrapper = mountPage()
    const options = mocks.useQuery.mock.calls[0]?.[0] as {
      queryKey: [string, { value: string | null }]
      queryFn: () => Promise<Target[]>
      enabled: { value: boolean }
    }

    expect(options.queryKey[0]).toBe('workspaceTargets')
    expect(options.queryKey[1].value).toBe('project-a')
    expect(options.enabled.value).toBe(true)
    await options.queryFn()
    expect(mocks.listProjectTargets).toHaveBeenCalledWith('project-a')
    expect(controlByLabel('状态').getAttribute('role')).toBe('combobox')
    expect(controlByLabel('搜索目标值')).toBeInstanceOf(HTMLInputElement)
    expect(wrapper.get('caption').text()).toBe('项目目标范围列表')
    expect(wrapper.findAll('th').every((heading) => heading.attributes('scope') === 'col')).toBe(
      true,
    )
    expect(wrapper.get('input[type="checkbox"]').attributes('aria-label')).toBe(
      '选择目标 api.example.test',
    )
    expect(wrapper.get('tbody tr').attributes('aria-selected')).toBe('false')

    uiStore.globalRefreshKey += 1
    await flushPromises()
    expect(mocks.refetch).toHaveBeenCalledOnce()
    workspaceStore.currentProjectId = null
    await flushPromises()
    uiStore.globalRefreshKey += 1
    await flushPromises()
    expect(mocks.refetch).toHaveBeenCalledOnce()
  })

  it('distinguishes loading, project empty, filtered empty, initial error, and stale refresh failure', async () => {
    queryState.data.value = undefined
    queryState.isLoading.value = true
    const wrapper = mountPage()
    expect(wrapper.get('[aria-label="正在加载目标"]')).toBeDefined()
    expect(wrapper.find('table').exists()).toBe(false)

    queryState.isLoading.value = false
    queryState.data.value = []
    await flushPromises()
    expect(wrapper.text()).toContain('暂无目标')

    queryState.data.value = undefined
    queryState.error.value = new Error('网络不可用')
    queryState.isError.value = true
    await flushPromises()
    expect(wrapper.text()).toContain('目标加载失败')
    expect(wrapper.text()).toContain('网络不可用')
    buttonByText('重试').click()
    expect(mocks.refetch).toHaveBeenCalledOnce()

    queryState.data.value = [target({ value: 'visible.example.test' })]
    queryState.isFetching.value = true
    await flushPromises()
    expect(wrapper.text()).toContain('目标同步失败')
    expect(wrapper.text()).toContain('visible.example.test')
    expect(wrapper.get('[role="status"]').text()).toContain('同步中…')

    queryState.isError.value = false
    queryState.error.value = null
    queryState.isFetching.value = false
    await wrapper.get('input[placeholder="过滤目标值..."]').setValue('missing')
    expect(wrapper.text()).toContain('没有符合筛选条件的目标')
  })

  it('filters and sorts targets, preserves hidden selection, and reconciles deleted target IDs', async () => {
    const older = target({
      id: 'older',
      value: 'Admin.Example.test',
      status: 'approved',
      created_at: '2026-08-01T00:00:00Z',
    })
    const newer = target({
      id: 'newer',
      value: 'api.example.test',
      status: 'discovered',
      created_at: '2026-08-20T00:00:00Z',
    })
    queryState.data.value = [older, newer]
    const wrapper = mountPage()

    expect(wrapper.findAll('.target-value').map((node) => node.text())).toEqual([
      'api.example.test',
      'Admin.Example.test',
    ])
    expect(wrapper.get('[aria-label="目标状态统计"]').text()).toContain('等待授权 1')
    expect(wrapper.get('[aria-label="目标状态统计"]').text()).toContain('探索中 1')

    await wrapper.get('input[placeholder="过滤目标值..."]').setValue('  ADMIN  ')
    expect(wrapper.findAll('.target-value').map((node) => node.text())).toEqual([
      'Admin.Example.test',
    ])
    buttonByText('选择当前筛选结果').click()
    await flushPromises()
    expect(wrapper.text()).toContain('已选择 1 项')

    await selectOption('状态', '等待授权')
    await wrapper.get('input[placeholder="过滤目标值..."]').setValue('')
    expect(wrapper.findAll('.target-value').map((node) => node.text())).toEqual([
      'api.example.test',
    ])
    expect(wrapper.text()).toContain('已选择 1 项')

    queryState.data.value = [newer]
    await flushPromises()
    expect(wrapper.text()).not.toContain('已选择 1 项')
  })

  it('resets filters, selection, create form, and draft when the project changes', async () => {
    queryState.data.value = [target({ id: 'selected', status: 'discovered' })]
    const wrapper = mountPage()
    await selectOption('状态', '等待授权')
    await wrapper.get('input[placeholder="过滤目标值..."]').setValue('asset')
    buttonByText('选择当前筛选结果').click()
    buttonByText('手动添加目标').click()
    await flushPromises()
    await setInputByLabel('目标值 (Value)', 'draft.example.test')
    await selectOption('目标类别', 'URL 路径')

    workspaceStore.currentProjectId = 'project-b'
    workspaceStore.projectDetail = { project: { id: 'project-b' }, targets: [] }
    await flushPromises()

    expect(wrapper.text()).not.toContain('已选择 1 项')
    expect(wrapper.find('#target-create-card').exists()).toBe(false)
    expect(wrapper.get('input[placeholder="过滤目标值..."]').element).toHaveProperty('value', '')
    expect(controlByLabel<HTMLButtonElement>('状态').textContent).toContain('全部')

    buttonByText('手动添加目标').click()
    await flushPromises()
    expect(controlByLabel<HTMLInputElement>('目标值 (Value)').value).toBe('')
    expect(controlByLabel<HTMLButtonElement>('目标类别').textContent).toContain('IP 地址')
  })

  it('creates approved targets with trimmed payload, guards duplicate submit, and syncs cache/store', async () => {
    queryState.data.value = []
    const created = target({ id: 'created', value: 'normalized.example.test', status: 'approved' })
    const request = deferred<Target>()
    mocks.createProjectTarget.mockReturnValue(request.promise)
    const wrapper = mountPage()
    buttonByText('手动添加目标').click()
    await flushPromises()
    await setInputByLabel('目标值 (Value)', '  raw.example.test  ')
    await selectOption('目标类别', '域名 Domain')

    await wrapper.get('form').trigger('submit')
    await wrapper.get('form').trigger('submit')
    expect(mocks.createProjectTarget).toHaveBeenCalledOnce()
    expect(mocks.createProjectTarget).toHaveBeenCalledWith('project-a', {
      value: 'raw.example.test',
      type: 'domain',
      status: 'approved',
    })
    expect(buttonByText('添加并批准').disabled).toBe(true)

    request.resolve(created)
    await flushPromises()
    expect(wrapper.find('#target-create-card').exists()).toBe(false)
    expect(mocks.setQueryData).toHaveBeenCalledWith(['workspaceTargets', 'project-a'], [created])
    expect(mocks.replaceProjectTargets).toHaveBeenCalledWith('project-a', [created])
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['workspaceTargets', 'project-a'],
    })
  })

  it('does not let an earlier project visit clear a new create draft', async () => {
    queryState.data.value = []
    const request = deferred<Target>()
    mocks.createProjectTarget.mockReturnValue(request.promise)
    const wrapper = mountPage()

    buttonByText('手动添加目标').click()
    await flushPromises()
    await setInputByLabel('目标值 (Value)', 'old-visit.example.test')
    await wrapper.get('form').trigger('submit')

    workspaceStore.currentProjectId = 'project-b'
    workspaceStore.projectDetail = { project: { id: 'project-b' }, targets: [] }
    await flushPromises()
    workspaceStore.currentProjectId = 'project-a'
    workspaceStore.projectDetail = { project: { id: 'project-a' }, targets: [] }
    await flushPromises()

    buttonByText('手动添加目标').click()
    await flushPromises()
    await setInputByLabel('目标值 (Value)', 'new-visit.example.test')

    request.resolve(target({ id: 'old-created', value: 'old-visit.example.test' }))
    await flushPromises()

    expect(wrapper.find('#target-create-card').exists()).toBe(true)
    expect(controlByLabel<HTMLInputElement>('目标值 (Value)').value).toBe('new-visit.example.test')
    expect(mocks.setQueryData).not.toHaveBeenCalled()
    expect(mocks.showToast).not.toHaveBeenCalled()
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['workspaceTargets', 'project-a'],
    })
  })

  it('keeps a later project-visit selection when an earlier bulk request completes', async () => {
    const row = target({ id: 'shared-target', value: 'shared.example.test', status: 'discovered' })
    queryState.data.value = [row]
    const request = deferred<Target>()
    mocks.updateProjectTarget.mockReturnValue(request.promise)
    const wrapper = mountPage()

    await wrapper.get('input[type="checkbox"]').setValue(true)
    buttonByText('批量批准').click()
    await flushPromises()

    workspaceStore.currentProjectId = 'project-b'
    workspaceStore.projectDetail = { project: { id: 'project-b' }, targets: [] }
    await flushPromises()
    workspaceStore.currentProjectId = 'project-a'
    workspaceStore.projectDetail = { project: { id: 'project-a' }, targets: [row] }
    await flushPromises()

    await wrapper.get('input[type="checkbox"]').setValue(true)
    expect(wrapper.text()).toContain('已选择 1 项')

    request.resolve({ ...row, status: 'approved' })
    await flushPromises()

    expect(wrapper.text()).toContain('已选择 1 项')
    expect(wrapper.get('input[type="checkbox"]').element).toHaveProperty('checked', true)
    expect(mocks.setQueryData).not.toHaveBeenCalled()
    expect(mocks.showToast).not.toHaveBeenCalled()
  })

  it('keeps the create draft after failure and permits retry', async () => {
    mocks.createProjectTarget.mockRejectedValueOnce(new Error('创建失败'))
    const wrapper = mountPage()
    buttonByText('手动添加目标').click()
    await flushPromises()
    await setInputByLabel('目标值 (Value)', 'retry.example.test')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('#target-create-card').exists()).toBe(true)
    expect(controlByLabel<HTMLInputElement>('目标值 (Value)').value).toBe('retry.example.test')
    expect(mocks.showToast).toHaveBeenCalledWith('创建失败', 'danger')

    mocks.createProjectTarget.mockResolvedValueOnce(
      target({ id: 'retry-created', value: 'retry.example.test' }),
    )
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.createProjectTarget).toHaveBeenCalledTimes(2)
  })

  it('updates one row through the typed helper, guards pending actions, and preserves route queries', async () => {
    const row = target({
      id: 'row-a',
      value: 'row.example.test',
      status: 'discovered',
      source_fact_id: 'fact-a',
      discovered_by_run_id: 'run-abcdefghijk',
    })
    queryState.data.value = [row]
    const request = deferred<Target>()
    mocks.updateProjectTarget.mockReturnValue(request.promise)
    const wrapper = mountPage()

    await wrapper.get('[aria-label="批准目标 row.example.test"]').trigger('click')
    await wrapper.get('[aria-label="拒绝目标 row.example.test"]').trigger('click')
    expect(mocks.updateProjectTarget).toHaveBeenCalledOnce()
    expect(mocks.updateProjectTarget).toHaveBeenCalledWith('project-a', 'row-a', {
      status: 'approved',
    })

    request.resolve({ ...row, status: 'approved' })
    await flushPromises()
    expect(mocks.replaceProjectTargets).toHaveBeenLastCalledWith('project-a', [
      { ...row, status: 'approved' },
    ])

    buttonByText('#fact-a').click()
    buttonByText('run_run-abcd').click()
    expect(mocks.push).toHaveBeenNthCalledWith(1, {
      path: '/projects/project-a',
      query: { nodeId: 'fact-a' },
    })
    expect(mocks.push).toHaveBeenNthCalledWith(2, {
      path: '/projects/project-a/runs',
      query: { runId: 'run-abcdefghijk' },
    })
  })

  it('freezes discovered IDs and reconciles partial selected-bulk success while retaining failures', async () => {
    const first = target({ id: 'first', value: 'first.test', status: 'discovered' })
    const second = target({ id: 'second', value: 'second.test', status: 'discovered' })
    const approved = target({ id: 'approved', value: 'approved.test', status: 'approved' })
    queryState.data.value = [first, second, approved]
    const wrapper = mountPage()

    buttonByText('全部批准').click()
    await flushPromises()
    expect(mocks.updateProjectTarget.mock.calls.map((call) => call.slice(1))).toEqual([
      ['first', { status: 'approved' }],
      ['second', { status: 'approved' }],
    ])

    mocks.updateProjectTarget.mockReset()
    mocks.updateProjectTarget.mockImplementation(async (_projectId, targetId, body) => {
      if (targetId === 'second') throw new Error('second failed')
      const existing = queryState.data.value?.find((item) => item.id === targetId)
      return { ...existing!, status: body.status }
    })
    const selectionInputs = wrapper.findAll<HTMLInputElement>('.target-checkbox-label input')
    expect(selectionInputs).toHaveLength(3)
    await selectionInputs[0]?.setValue(true)
    await selectionInputs[1]?.setValue(true)
    await flushPromises()
    buttonByText('批量拒绝').click()
    await flushPromises()

    expect(mocks.updateProjectTarget).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('已选择 1 项')
    expect(wrapper.get('input[type="checkbox"]:checked').attributes('aria-label')).toBe(
      '选择目标 second.test',
    )
    expect(
      wrapper
        .findAll('tbody tr')
        .find((tableRow) => tableRow.text().includes('second.test'))
        ?.attributes('aria-selected'),
    ).toBe('true')
    expect(mocks.showToast).toHaveBeenLastCalledWith(
      '批量操作部分成功：成功 1 项，失败 1 项',
      'warn',
    )
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['workspaceTargets', 'project-a'],
    })

    mocks.updateProjectTarget.mockReset().mockRejectedValue(new Error('retry failed'))
    buttonByText('批量批准').click()
    await flushPromises()
    expect(mocks.updateProjectTarget).toHaveBeenCalledWith('project-a', 'second', {
      status: 'approved',
    })
    expect(wrapper.text()).toContain('已选择 1 项')
    expect(mocks.showToast).toHaveBeenLastCalledWith('批量操作全部失败，请重试', 'danger')
  })
})
