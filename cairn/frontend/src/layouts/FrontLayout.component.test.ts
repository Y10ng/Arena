import { flushPromises, mount } from '@vue/test-utils'
import { reactive, ref, unref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { ProjectGroup } from '@/api/types'
import FrontLayout from './FrontLayout.vue'

const mocks = vi.hoisted(() => ({
  groups: [] as ProjectGroup[],
  createProjectGroup: vi.fn(),
  deleteProjectGroup: vi.fn(),
  listProjectGroups: vi.fn(),
  groupsRefetch: vi.fn(),
  invalidateQueries: vi.fn(),
  router: {
    push: vi.fn(),
    replace: vi.fn(),
  },
  route: {
    path: '/projects',
    name: 'Projects',
    query: {} as Record<string, unknown>,
    params: {} as Record<string, string>,
  },
  authStore: {
    displayName: 'analyst',
    isSuperAdmin: false,
    session: { tenant_id: 'tenant-a' },
    logout: vi.fn(),
  },
  uiStore: null as unknown as {
    isSidebarCollapsed: boolean
    isCommandPaletteOpen: boolean
    theme: string
    toggleSidebar: ReturnType<typeof vi.fn>
    openCommandPalette: ReturnType<typeof vi.fn>
    closeCommandPalette: ReturnType<typeof vi.fn>
    toggleTheme: ReturnType<typeof vi.fn>
    triggerGlobalRefresh: ReturnType<typeof vi.fn>
    showToast: ReturnType<typeof vi.fn>
  },
  workspaceStore: null as unknown as {
    projectDetail: null | { project: { title: string } }
    loadProject: ReturnType<typeof vi.fn>
    clearProject: ReturnType<typeof vi.fn>
  },
}))

vi.mock('@/api/client', () => ({
  api: vi.fn().mockResolvedValue(undefined),
  createProjectGroup: mocks.createProjectGroup,
  deleteProjectGroup: mocks.deleteProjectGroup,
  listProjectGroups: mocks.listProjectGroups,
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => mocks.authStore,
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => mocks.uiStore,
}))

vi.mock('@/stores/workspace', () => ({
  useWorkspaceStore: () => mocks.workspaceStore,
}))

vi.mock('vue-router', () => ({
  useRoute: () => mocks.route,
  useRouter: () => mocks.router,
}))

vi.mock('@tanstack/vue-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown }) => {
    const resolvedKey = unref(queryKey)
    const key = Array.isArray(resolvedKey) ? resolvedKey : []

    if (key[0] === 'project-groups') {
      return {
        data: ref(mocks.groups),
        refetch: mocks.groupsRefetch,
      }
    }

    return { data: ref(undefined) }
  },
  useQueryClient: () => ({
    invalidateQueries: mocks.invalidateQueries,
  }),
}))

const groups: ProjectGroup[] = [
  {
    id: 'group-a',
    name: 'Red Team',
    owner: 'analyst',
    created_at: '2026-08-20T00:00:00Z',
  },
  {
    id: 'group-b',
    name: 'Blue Team',
    owner: 'analyst',
    created_at: '2026-08-20T01:00:00Z',
  },
]

const wrappers: VueWrapper[] = []
const scrollIntoViewMock = vi.fn()

function mockMobileViewport(matches: boolean) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockImplementation((query: string) => ({
      matches,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  )
}

function mountLayout() {
  const wrapper = mount(FrontLayout, {
    attachTo: document.body,
    global: {
      mocks: {
        $route: mocks.route,
        $router: mocks.router,
      },
      stubs: {
        RouterLink: {
          props: ['to'],
          template: '<a :href="String(to)"><slot /></a>',
        },
        RouterView: { template: '<div data-testid="router-view" />' },
      },
    },
  })
  wrappers.push(wrapper)
  return wrapper
}

function bodyButton(label: string) {
  const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === label,
  )
  if (!button) throw new Error(`找不到按钮：${label}`)
  return button
}

function commandDialog() {
  return [...document.body.querySelectorAll<HTMLElement>('[role="dialog"]')].find((dialog) =>
    dialog.textContent?.includes('命令面板'),
  )
}

function paletteInput() {
  const input = document.body.querySelector<HTMLInputElement>('#command-palette-search')
  if (!input) throw new Error('找不到命令搜索输入框')
  return input
}

function paletteOptions() {
  return [...document.body.querySelectorAll<HTMLButtonElement>('[role="option"]')]
}

async function openCommandPalette() {
  const trigger = document.body.querySelector<HTMLButtonElement>('.btn-command')
  if (!trigger) throw new Error('找不到命令面板触发器')
  trigger.focus()
  trigger.click()
  await flushPromises()
  return trigger
}

async function typePaletteQuery(query: string) {
  const input = paletteInput()
  input.value = query
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
  return input
}

async function dispatchPaletteShortcut(modifier: 'ctrl' | 'meta') {
  const event = new KeyboardEvent('keydown', {
    key: 'k',
    ctrlKey: modifier === 'ctrl',
    metaKey: modifier === 'meta',
    bubbles: true,
    cancelable: true,
  })
  window.dispatchEvent(event)
  await flushPromises()
  return event
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

async function openGroupMenu(groupName: string) {
  const trigger = document.body.querySelector<HTMLButtonElement>(
    `button[aria-label="项目组选项：${groupName}"]`,
  )
  if (!trigger) throw new Error(`找不到项目组菜单：${groupName}`)
  trigger.click()
  await flushPromises()
  return trigger
}

async function chooseMenuItem(label: string) {
  const item = [...document.body.querySelectorAll<HTMLElement>('[role="menuitem"]')].find(
    (candidate) => candidate.textContent?.includes(label),
  )
  if (!item) throw new Error(`找不到菜单项：${label}`)
  item.click()
  await flushPromises()
}

beforeEach(() => {
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
  HTMLElement.prototype.scrollIntoView = scrollIntoViewMock
  scrollIntoViewMock.mockReset()
  mockMobileViewport(false)

  mocks.groups = groups.map((group) => ({ ...group }))
  mocks.route.path = '/projects'
  mocks.route.name = 'Projects'
  mocks.route.query = {}
  mocks.route.params = {}
  mocks.authStore.displayName = 'analyst'
  mocks.authStore.isSuperAdmin = false
  mocks.authStore.session = { tenant_id: 'tenant-a' }
  const uiStore = reactive({
    isSidebarCollapsed: false,
    isCommandPaletteOpen: false,
    theme: 'dark',
    toggleSidebar: vi.fn(),
    openCommandPalette: vi.fn(() => {
      uiStore.isCommandPaletteOpen = true
    }),
    closeCommandPalette: vi.fn(() => {
      uiStore.isCommandPaletteOpen = false
    }),
    toggleTheme: vi.fn(),
    triggerGlobalRefresh: vi.fn(),
    showToast: vi.fn(),
  })
  mocks.uiStore = uiStore

  mocks.workspaceStore = reactive({
    projectDetail: null,
    loadProject: vi.fn(),
    clearProject: vi.fn(),
  })

  mocks.createProjectGroup.mockReset()
  mocks.deleteProjectGroup.mockReset()
  mocks.listProjectGroups.mockReset().mockResolvedValue(mocks.groups)
  mocks.groupsRefetch.mockReset().mockResolvedValue(undefined)
  mocks.invalidateQueries.mockReset().mockResolvedValue(undefined)
  mocks.router.push.mockReset().mockResolvedValue(undefined)
  mocks.router.replace.mockReset().mockResolvedValue(undefined)
  mocks.authStore.logout.mockReset().mockResolvedValue(undefined)
  mocks.workspaceStore.loadProject.mockReset()
  mocks.workspaceStore.clearProject.mockReset()
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('FrontLayout overlay migration', () => {
  it('renders the extracted sidebar and preserves tenant versus super-admin visibility', async () => {
    const wrapper = mountLayout()
    const sidebar = wrapper.get('aside[aria-label="主导航"]')

    expect(sidebar.text()).toContain('Arena')
    expect(sidebar.get('.brand-logo-mark').text()).toBe('A')
    expect(sidebar.find('a[href="/projects?group=ungrouped"]').exists()).toBe(false)
    expect(sidebar.text()).toContain('仪表盘')
    expect(sidebar.text()).toContain('成果列表')
    expect(sidebar.text()).toContain('Models')
    expect(sidebar.text()).toContain('Workers')
    expect(sidebar.text()).not.toContain('探索场景')
    expect(sidebar.text()).not.toContain('Users')
    expect(sidebar.find('.dispatcher-status').exists()).toBe(false)

    wrapper.unmount()
    mocks.authStore.isSuperAdmin = true
    const adminWrapper = mountLayout()
    const adminSidebar = adminWrapper.get('aside[aria-label="主导航"]')

    expect(adminSidebar.text()).toContain('探索场景')
    expect(adminSidebar.text()).toContain('Users')
    expect(adminSidebar.find('.dispatcher-status').exists()).toBe(true)
  })

  it('uses independent accessible controls for project navigation and group creation', async () => {
    const wrapper = mountLayout()
    const taskToggle = wrapper.get('.nav-group-header-wrapper > .nav-group-header')
    const createButton = wrapper.get('button[aria-label="新建项目组"]')

    expect(taskToggle.element.tagName).toBe('BUTTON')
    expect(taskToggle.attributes('type')).toBe('button')
    expect(createButton.element.tagName).toBe('BUTTON')
    expect(createButton.attributes('type')).toBe('button')
    expect(taskToggle.element.contains(createButton.element)).toBe(false)
    expect(wrapper.find('.nav-group-header button').exists()).toBe(false)

    await createButton.trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain('新建项目组')
  })

  it('provides a reachable mobile close control and closes after mobile navigation only', async () => {
    mockMobileViewport(true)
    const wrapper = mountLayout()
    const closeButton = wrapper.get('button[aria-label="关闭主导航"]')

    await closeButton.trigger('click')
    expect(mocks.uiStore.toggleSidebar).toHaveBeenCalledTimes(1)

    mocks.uiStore.toggleSidebar.mockClear()
    const findingsLink = wrapper.findAll('a').find((link) => link.text() === '成果列表')
    if (!findingsLink) throw new Error('找不到成果列表导航')
    findingsLink.element.addEventListener('click', (event) => event.preventDefault())
    await findingsLink.trigger('click')
    expect(mocks.uiStore.toggleSidebar).toHaveBeenCalledTimes(1)

    mocks.uiStore.toggleSidebar.mockClear()
    mockMobileViewport(false)
    await findingsLink.trigger('click')
    expect(mocks.uiStore.toggleSidebar).not.toHaveBeenCalled()
  })

  it('shows account metadata and logs out through the Popover action', async () => {
    mountLayout()

    const trigger = document.body.querySelector<HTMLButtonElement>('.actor-trigger')
    if (!trigger) throw new Error('找不到账账户菜单触发器')
    expect(trigger.textContent).toContain('A')
    expect(trigger.textContent).toContain('analyst')

    trigger.click()
    await flushPromises()

    const popover = document.body.querySelector<HTMLElement>('[data-reka-popper-content-wrapper]')
    expect(popover?.textContent).toContain('个人账户')
    expect(popover?.textContent).toContain('Tenant tenant-a')

    bodyButton('退出登录').click()
    await flushPromises()

    expect(mocks.authStore.logout).toHaveBeenCalledTimes(1)
    expect(mocks.router.replace).toHaveBeenCalledWith({ name: 'Login' })
  })

  it('trims a project group name, blocks duplicate creation, and navigates after success', async () => {
    const pending = deferred<ProjectGroup>()
    mocks.createProjectGroup.mockReturnValueOnce(pending.promise)
    mountLayout()

    const createTrigger = document.body.querySelector<HTMLElement>('.btn-group-add-inline')
    if (!createTrigger) throw new Error('找不到新建项目组入口')
    createTrigger.click()
    await flushPromises()

    const input = document.body.querySelector<HTMLInputElement>('#project-group-name')
    expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain('新建项目组')
    if (!input) throw new Error('找不到项目组名称输入框')
    expect(document.activeElement).toBe(input)

    input.value = '  Core Group  '
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()

    bodyButton('确认创建').click()
    bodyButton('确认创建').click()
    await flushPromises()

    expect(mocks.createProjectGroup).toHaveBeenCalledTimes(1)
    expect(mocks.createProjectGroup).toHaveBeenCalledWith({ name: 'Core Group' })
    expect(bodyButton('取消').disabled).toBe(true)

    pending.resolve({
      id: 'group-created',
      name: 'Core Group',
      owner: 'analyst',
      created_at: '2026-08-20T02:00:00Z',
    })
    await flushPromises()

    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(mocks.groupsRefetch).toHaveBeenCalledTimes(1)
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['project-groups'] })
    expect(mocks.router.push).toHaveBeenCalledWith('/projects?group=group-created')
  })

  it('keeps the create dialog and entered name when creation fails', async () => {
    mocks.createProjectGroup.mockRejectedValueOnce(new Error('create rejected'))
    mountLayout()

    const createTrigger = document.body.querySelector<HTMLElement>('.btn-group-add-inline')
    if (!createTrigger) throw new Error('找不到新建项目组入口')
    createTrigger.click()
    await flushPromises()

    const input = document.body.querySelector<HTMLInputElement>('#project-group-name')
    if (!input) throw new Error('找不到项目组名称输入框')
    input.value = 'Persistent Group'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()

    bodyButton('确认创建').click()
    await flushPromises()

    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
    expect(input.value).toBe('Persistent Group')
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith('create rejected', 'danger')
  })

  it('opens an accessible danger menu and preserves delete confirmation on failure', async () => {
    const pending = deferred<void>()
    mocks.deleteProjectGroup.mockReturnValueOnce(pending.promise)
    mountLayout()

    await openGroupMenu('Red Team')
    expect(document.body.querySelector('[role="menu"]')).not.toBeNull()
    await chooseMenuItem('删除分组')

    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain('Red Team')
    bodyButton('确认删除').click()
    bodyButton('确认删除').click()
    await flushPromises()

    expect(mocks.deleteProjectGroup).toHaveBeenCalledTimes(1)
    expect(mocks.deleteProjectGroup).toHaveBeenCalledWith('group-a')
    expect(bodyButton('取消').disabled).toBe(true)

    pending.reject(new Error('delete rejected'))
    await flushPromises()

    expect(document.body.querySelector('[role="alertdialog"]')).not.toBeNull()
    expect(mocks.uiStore.showToast).toHaveBeenCalledWith('delete rejected', 'danger')
  })

  it('closes deletion, refreshes queries, and clears a deleted active group route', async () => {
    mocks.route.query = { group: 'group-a' }
    mocks.deleteProjectGroup.mockResolvedValueOnce(undefined)
    mountLayout()

    await openGroupMenu('Red Team')
    await chooseMenuItem('删除分组')
    bodyButton('确认删除').click()
    await flushPromises()

    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
    expect(mocks.router.push).toHaveBeenCalledWith('/projects')
    expect(mocks.groupsRefetch).toHaveBeenCalledTimes(1)
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['projects'] })
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['project-groups'] })
  })

  it('opens a named portalled command Dialog and focuses its combobox', async () => {
    const wrapper = mountLayout()
    const trigger = await openCommandPalette()
    const dialog = commandDialog()
    const input = paletteInput()

    expect(dialog).toBeTruthy()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(dialog?.getAttribute('aria-labelledby')).toBeTruthy()
    expect(dialog?.getAttribute('aria-describedby')).toBeTruthy()
    expect(input.getAttribute('aria-label')).toBe('搜索命令')
    expect(input.getAttribute('aria-controls')).toBe('command-palette-results')
    expect(document.activeElement).toBe(input)
    expect(document.body.querySelector('.command-palette-backdrop')).toBeNull()
    expect(document.body.querySelector('.command-palette')).toBeNull()

    trigger.focus()
    await flushPromises()
    expect(dialog?.contains(document.activeElement)).toBe(true)
  })

  it.each(['ctrl', 'meta'] as const)(
    'toggles with %s+K and restores the exact previously focused element',
    async (modifier) => {
      mountLayout()
      const previousFocus = document.body.querySelector<HTMLButtonElement>('.btn-icon')
      if (!previousFocus) throw new Error('找不到快捷键打开前的焦点元素')
      previousFocus.focus()

      const openEvent = await dispatchPaletteShortcut(modifier)
      expect(openEvent.defaultPrevented).toBe(true)
      expect(commandDialog()).toBeTruthy()
      expect(document.activeElement).toBe(paletteInput())

      const closeEvent = await dispatchPaletteShortcut(modifier)
      expect(closeEvent.defaultPrevented).toBe(true)
      expect(commandDialog()).toBeFalsy()
      expect(document.activeElement).toBe(previousFocus)
    },
  )

  it('scrolls the active command into view when keyboard selection moves down', async () => {
    mountLayout()
    await openCommandPalette()
    const input = paletteInput()
    const options = paletteOptions()

    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await flushPromises()

    expect(input.getAttribute('aria-activedescendant')).toBe(options[1]?.id)
    expect(scrollIntoViewMock).toHaveBeenCalledWith({ block: 'nearest' })
    expect(scrollIntoViewMock.mock.instances.at(-1)).toBe(options[1])
  })

  it('wraps keyboard selection, resets it after filtering, and executes the valid result', async () => {
    mocks.route.params = { id: 'project-a' }
    mountLayout()
    await openCommandPalette()
    const input = paletteInput()

    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true }))
    await flushPromises()
    let options = paletteOptions()
    expect(options.at(-1)?.getAttribute('aria-selected')).toBe('true')

    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await flushPromises()
    options = paletteOptions()
    expect(options[0]?.getAttribute('aria-selected')).toBe('true')

    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await typePaletteQuery('terminal logs')
    options = paletteOptions()
    expect(options).toHaveLength(1)
    expect(options[0]?.textContent).toContain('查看运行终端')
    expect(options[0]?.getAttribute('aria-selected')).toBe('true')
    expect(input.getAttribute('aria-activedescendant')).toBe(options[0]?.id)

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
    )
    await flushPromises()
    expect(mocks.router.push).toHaveBeenCalledWith({
      name: 'WorkspaceRuns',
      params: { id: 'project-a' },
    })
    expect(commandDialog()).toBeFalsy()

    await openCommandPalette()
    expect(paletteInput().value).toBe('')
    await typePaletteQuery('no matching command')
    const navigationCount = mocks.router.push.mock.calls.length
    paletteInput().dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
    )
    await flushPromises()
    expect(document.body.textContent).toContain('没有找到匹配项')
    expect(mocks.router.push).toHaveBeenCalledTimes(navigationCount)
  })

  it('preserves role and project command visibility and supports mouse execution', async () => {
    mocks.authStore.isSuperAdmin = true
    mocks.route.params = { id: 'project-a' }
    mountLayout()
    await openCommandPalette()

    const optionText = paletteOptions().map((option) => option.textContent || '')
    expect(optionText.some((text) => text.includes('转到全局监控大屏'))).toBe(true)
    expect(optionText.some((text) => text.includes('管理 Users'))).toBe(true)

    await typePaletteQuery('运行终端')
    const runsOption = paletteOptions().find((option) =>
      option.textContent?.includes('查看运行终端'),
    )
    if (!runsOption) throw new Error('找不到当前项目运行命令')
    runsOption.click()
    await flushPromises()

    expect(mocks.router.push).toHaveBeenCalledWith({
      name: 'WorkspaceRuns',
      params: { id: 'project-a' },
    })
    expect(commandDialog()).toBeFalsy()
  })

  it('renders semantic breadcrumb ancestors and updates only the project text after loading', async () => {
    mocks.route.name = 'WorkspaceRuns'
    mocks.route.path = '/projects/project-a/runs'
    mocks.route.params = { id: 'project-a' }
    const wrapper = mountLayout()

    const breadcrumb = wrapper.get('nav[aria-label="面包屑"]')
    expect(breadcrumb.find('ol').exists()).toBe(true)
    expect(breadcrumb.findAll('li')).toHaveLength(4)
    expect(breadcrumb.findAll('a').map((link) => link.text())).toEqual([
      'Arena',
      '任务管理',
      'project-a',
    ])
    expect(breadcrumb.findAll('[aria-current="page"]')).toHaveLength(1)
    expect(breadcrumb.get('[aria-current="page"]').text()).toBe('运行日志')
    expect(
      breadcrumb
        .findAll('.separator')
        .every((separator) => separator.attributes('aria-hidden') === 'true'),
    ).toBe(true)

    mocks.workspaceStore.projectDetail = { project: { title: 'Loaded Project' } }
    await flushPromises()

    expect(breadcrumb.findAll('li')).toHaveLength(4)
    expect(breadcrumb.text()).toContain('Loaded Project')
    expect(breadcrumb.text()).not.toContain('project-a')
  })

  it.each([
    { name: 'Forbidden', path: '/forbidden', label: '无权访问' },
    { name: 'NotFound', path: '/missing/path', label: '页面不存在' },
  ])('shows the $name status breadcrumb with one current page', ({ name, path, label }) => {
    mocks.route.name = name
    mocks.route.path = path
    const wrapper = mountLayout()
    const breadcrumb = wrapper.get('nav[aria-label="面包屑"]')

    expect(breadcrumb.text()).toContain(`Arena/${label}`)
    expect(breadcrumb.findAll('[aria-current="page"]')).toHaveLength(1)
    expect(breadcrumb.get('[aria-current="page"]').text()).toBe(label)
  })

  it('hides privileged commands from tenant users and restores trigger focus after Escape', async () => {
    mountLayout()
    const trigger = await openCommandPalette()
    const optionText = paletteOptions().map((option) => option.textContent || '')

    expect(optionText.some((text) => text.includes('转到全局监控大屏'))).toBe(false)
    expect(optionText.some((text) => text.includes('管理 Users'))).toBe(false)
    expect(optionText.some((text) => text.includes('管理 Models'))).toBe(true)
    expect(optionText.some((text) => text.includes('管理 Workers'))).toBe(true)
    expect(optionText.some((text) => text.includes('查看运行终端'))).toBe(false)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()

    expect(commandDialog()).toBeFalsy()
    expect(mocks.uiStore.closeCommandPalette).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(trigger)

    await openCommandPalette()
    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    document.body.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
    await flushPromises()

    expect(commandDialog()).toBeFalsy()
    expect(mocks.uiStore.closeCommandPalette).toHaveBeenCalledTimes(2)
    expect(document.activeElement).toBe(trigger)
  })
})
