import { flushPromises, mount } from '@vue/test-utils'
import { reactive } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import FrontLayout from './FrontLayout.vue'

const mocks = vi.hoisted(() => ({
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
    matched: [] as Array<{ path: string }>,
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
  useQueryClient: () => ({ invalidateQueries: mocks.invalidateQueries }),
}))

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

function mountLayout(pageTitle = '页面内容') {
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
          template: '<a :href="typeof to === \'string\' ? to : to.path || to.name"><slot /></a>',
        },
        RouterView: {
          setup: () => ({ pageTitle }),
          template: '<article data-testid="router-view"><h1>{{ pageTitle }}</h1></article>',
        },
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
  const trigger = document.body.querySelector<HTMLButtonElement>('button[aria-label="搜索与命令"]')
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

beforeEach(() => {
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
  HTMLElement.prototype.scrollIntoView = scrollIntoViewMock
  scrollIntoViewMock.mockReset()
  mockMobileViewport(false)

  mocks.route = reactive({
    path: '/projects',
    name: 'Projects',
    query: {},
    params: {},
    matched: [],
  })
  mocks.authStore.displayName = 'analyst'
  mocks.authStore.isSuperAdmin = false
  mocks.authStore.session = { tenant_id: 'tenant-a' }
  const uiStore = reactive({
    isSidebarCollapsed: false,
    isCommandPaletteOpen: false,
    theme: 'dark',
    toggleSidebar: vi.fn(() => {
      uiStore.isSidebarCollapsed = !uiStore.isSidebarCollapsed
    }),
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
    expect(sidebar.get('.brand-link').attributes('href')).toBe('Projects')
    expect(
      sidebar
        .get('section[aria-label="工作"]')
        .findAll('a')
        .map((link) => link.text()),
    ).toEqual(['项目', '仪表盘'])
    expect(
      sidebar
        .get('section[aria-label="成果"]')
        .findAll('a')
        .map((link) => link.text()),
    ).toEqual(['漏洞', '复测', '报告', '流量'])
    expect(
      sidebar
        .get('section[aria-label="研究支持"]')
        .findAll('a')
        .map((link) => link.text()),
    ).toEqual(['计划'])
    expect(document.querySelector('#sidebar-settings')).toBeNull()
    await sidebar.get('button[aria-label="设置"]').trigger('click')
    await flushPromises()
    const settings = document.querySelector('#sidebar-settings')!
    expect([...settings.querySelectorAll('a')].map((link) => link.textContent?.trim())).toEqual(['模型与 Worker', '漏洞复测配置'])
    expect(sidebar.element.contains(settings)).toBe(false)
    expect(settings.closest('[data-side]')?.getAttribute('data-side')).toBe('top')
    expect(settings.querySelector('a[href="Monitoring"]')).toBeNull()
    expect(sidebar.find('.dispatcher-status').exists()).toBe(false)

    wrapper.unmount()
    mocks.authStore.isSuperAdmin = true
    const adminWrapper = mountLayout()
    const adminSidebar = adminWrapper.get('aside[aria-label="主导航"]')

    expect(
      adminSidebar
        .get('section[aria-label="研究支持"]')
        .findAll('a')
        .map((link) => link.text()),
    ).toEqual(['资料库', '探索场景', '计划'])
    expect(document.querySelector('#sidebar-settings')).toBeNull()
    await adminSidebar.get('button[aria-label="设置"]').trigger('click')
    await flushPromises()
    expect([...document.querySelectorAll('#sidebar-settings a')].map((link) => link.textContent?.trim())).toEqual(['模型与 Worker', '漏洞复测配置', '用户管理', '提示词', '报告模板', '运行监控'])
    expect(
      adminSidebar.get('nav[aria-label="应用导航"]').find('a[href="Monitoring"]').exists(),
    ).toBe(false)
    expect(adminSidebar.find('.dispatcher-status').exists()).toBe(false)
  })

  it('navigates to projects without project groups and ignores the retired g n shortcut', async () => {
    const wrapper = mountLayout()
    const tasks = wrapper.get('section[aria-label="工作"] a[href="Projects"]')
    expect(tasks.attributes('href')).toBe('Projects')
    expect(wrapper.find('button[aria-label="新建项目组"]').exists()).toBe(false)
    expect(wrapper.find('[href*="?group="]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('未分组')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'g', bubbles: true }))
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'n', bubbles: true }))
    await flushPromises()
    expect(mocks.router.push).not.toHaveBeenCalled()
  })

  it('keeps keyboard destinations real and respects the monitoring role', async () => {
    mountLayout()
    const chord = async (key: string) => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'g', bubbles: true }))
      window.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }))
      await flushPromises()
    }
    await chord('s')
    expect(mocks.router.push).toHaveBeenLastCalledWith({
      name: 'ConsoleWorkers',
      query: { section: 'models' },
    })
    mocks.router.push.mockClear()
    await chord('m')
    expect(mocks.router.push).not.toHaveBeenCalled()
    mocks.authStore.isSuperAdmin = true
    await chord('m')
    expect(mocks.router.push).toHaveBeenLastCalledWith({ name: 'Monitoring' })
    mocks.route.params = { id: 'project-a' }
    mocks.route.matched = [{ path: '/projects/:id' }]
    await chord('u')
    expect(mocks.router.push).toHaveBeenLastCalledWith('/projects/project-a/runs')
  })

  it('does not intercept editing, composing or dialog single-key shortcuts', async () => {
    mountLayout()
    const select = document.createElement('select')
    document.body.append(select)
    select.focus()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: '[', bubbles: true }))
    select.remove()
    window.dispatchEvent(
      new KeyboardEvent('keydown', { key: '[', isComposing: true, bubbles: true }),
    )
    expect(mocks.uiStore.toggleSidebar).not.toHaveBeenCalled()
    await openCommandPalette()
    const close = document.body.querySelector<HTMLButtonElement>(
      '[role="dialog"] button[aria-label="关闭对话框"]',
    )
    close?.focus()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: '[', bubbles: true }))
    expect(mocks.uiStore.toggleSidebar).not.toHaveBeenCalled()
  })

  it('provides a reachable mobile close control and closes after mobile navigation only', async () => {
    mockMobileViewport(true)
    const wrapper = mountLayout()
    const closeButton = wrapper.get('button[aria-label="关闭主导航"]')

    await closeButton.trigger('click')
    expect(mocks.uiStore.toggleSidebar).toHaveBeenCalledTimes(1)

    mocks.uiStore.toggleSidebar.mockClear()
    mocks.uiStore.isSidebarCollapsed = false
    const findingsLink = wrapper.findAll('a').find((link) => link.text() === '漏洞')
    if (!findingsLink) throw new Error('找不到漏洞导航')
    findingsLink.element.addEventListener('click', (event) => event.preventDefault())
    await findingsLink.trigger('click')
    expect(mocks.uiStore.toggleSidebar).toHaveBeenCalledTimes(1)

    mocks.uiStore.toggleSidebar.mockClear()
    mocks.uiStore.isSidebarCollapsed = false
    mockMobileViewport(false)
    await findingsLink.trigger('click')
    expect(mocks.uiStore.toggleSidebar).not.toHaveBeenCalled()
  })

  it('keeps settings in a Portal from the collapsed sidebar and restores focus on Escape', async () => {
    mocks.authStore.isSuperAdmin = true
    mocks.uiStore.isSidebarCollapsed = true
    const wrapper = mountLayout()
    const settingsButton = wrapper.get('button[aria-label="设置"]')
    ;(settingsButton.element as HTMLButtonElement).focus()
    await settingsButton.trigger('click')
    await flushPromises()
    expect(mocks.uiStore.isSidebarCollapsed).toBe(true)
    expect(document.querySelector('#sidebar-settings')).not.toBeNull()
    expect(wrapper.find('#sidebar-settings').exists()).toBe(false)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(document.querySelector('#sidebar-settings')).toBeNull()
    expect(document.activeElement).toBe(settingsButton.element)

    await settingsButton.trigger('click')
    await flushPromises()
    mocks.route.name = 'Monitoring'
    mocks.route.path = '/monitoring'
    await flushPromises()
    expect(document.querySelector('#sidebar-settings')).toBeNull()
    expect(settingsButton.classes()).toContain('active')
    await settingsButton.trigger('click')
    await flushPromises()
    expect(document.querySelector('#sidebar-settings a[href="Monitoring"]')?.getAttribute('aria-current')).toBe('page')
  })

  it.each([false, true])(
    'refreshes through sidebar utilities and reports query failure: %s',
    async (fails) => {
      if (fails) mocks.invalidateQueries.mockRejectedValueOnce(new Error('refresh failed'))
      const wrapper = mountLayout()
      const refresh = wrapper.get('.sidebar-utilities button[aria-label="刷新页面数据"]')
      await refresh.trigger('click')
      await flushPromises()

      expect(mocks.uiStore.triggerGlobalRefresh).toHaveBeenCalledTimes(1)
      expect(mocks.invalidateQueries).toHaveBeenCalledWith({}, { throwOnError: true })
      expect(refresh.attributes('disabled')).toBeUndefined()
      if (fails) expect(mocks.uiStore.showToast).toHaveBeenCalledWith('刷新失败', 'danger')
      else expect(mocks.uiStore.showToast).not.toHaveBeenCalled()
    },
  )

  it('shows account metadata and logs out through the Popover action', async () => {
    mountLayout()

    const trigger = document.body.querySelector<HTMLButtonElement>('.actor-trigger')
    if (!trigger) throw new Error('找不到账账户菜单触发器')
    expect(trigger.getAttribute('aria-label')).toBe('账户：analyst')
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(trigger.textContent).toContain('analyst')

    trigger.click()
    await flushPromises()

    const popover = document.body.querySelector<HTMLElement>('[data-reka-popper-content-wrapper]')
    expect(popover?.textContent).toContain('个人账户')
    expect(popover?.textContent).toContain('analyst')
    expect(popover?.querySelector('[aria-label="账户信息"]')).toBeTruthy()
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    expect(popover?.textContent).not.toContain('tenant-a')

    bodyButton('退出登录').click()
    await flushPromises()

    expect(mocks.authStore.logout).toHaveBeenCalledTimes(1)
    expect(mocks.router.replace).toHaveBeenCalledWith({ name: 'Login' })
  })

  it('opens a named portalled command Dialog and focuses its combobox', async () => {
    const wrapper = mountLayout()
    const trigger = await openCommandPalette()
    const dialog = commandDialog()
    const input = paletteInput()

    expect(dialog).toBeTruthy()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(dialog?.getAttribute('aria-labelledby')).toBeTruthy()
    expect(dialog?.hasAttribute('aria-describedby')).toBe(false)
    expect(dialog?.querySelector('.ui-dialog__description')).toBeNull()
    expect(dialog?.querySelector('.item-subtitle')).toBeNull()
    expect(dialog?.querySelector('.palette-footer')).toBeNull()
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
    mocks.route.matched = [{ path: '/projects/:id' }]
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
    mocks.route.matched = [{ path: '/projects/:id' }]
    mountLayout()
    await openCommandPalette()

    const optionText = paletteOptions().map((option) => option.textContent || '')
    expect(optionText.some((text) => text.includes('转到全局监控大屏'))).toBe(true)
    await typePaletteQuery('管理用户')
    expect(paletteOptions().some((option) => option.textContent?.includes('管理用户'))).toBe(true)

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

  it('loads project context into the shared header host without repeating breadcrumbs', async () => {
    mocks.route.name = 'WorkspaceRuns'
    mocks.route.path = '/projects/project-a/runs'
    mocks.route.params = { id: 'project-a' }
    mocks.route.matched = [{ path: '/projects/:id' }]
    const wrapper = mountLayout('运行日志')
    expect(mocks.workspaceStore.loadProject).toHaveBeenCalledWith('project-a')
    expect(wrapper.get('#workbench-project-header').isVisible()).toBe(true)
    expect(wrapper.find('nav[aria-label="面包屑"]').exists()).toBe(false)
    expect(wrapper.get('main').text()).toBe('运行日志')
    expect(wrapper.get('main').classes()).toContain('content-viewport--workspace')
  })

  it('keeps the project header host for the Agent workbench without adding shell breadcrumbs', () => {
    mocks.route.name = 'WorkspaceAgent'
    mocks.route.path = '/projects/project-a/agent'
    mocks.route.params = { id: 'project-a' }
    mocks.route.matched = [{ path: '/projects/:id' }]
    const wrapper = mountLayout('项目对话')

    expect(wrapper.get('.app-layout').classes()).toContain('app-layout--agent')
    expect(wrapper.get('header #workbench-project-header').text()).toBe('')
    expect(wrapper.find('nav[aria-label="面包屑"]').exists()).toBe(false)
    expect(wrapper.get('main').text()).toBe('项目对话')
    expect(mocks.workspaceStore.loadProject).toHaveBeenCalledWith('project-a')
  })

  it.each([
    { name: 'Projects', path: '/projects', label: '项目' },
    { name: 'Overview', path: '/', label: '仪表盘' },
    { name: 'ConsoleWorkers', path: '/console/workers', label: '模型与 Worker' },
    { name: 'Forbidden', path: '/forbidden', label: '无权访问' },
    { name: 'NotFound', path: '/missing/path', label: '页面不存在' },
  ])(
    'does not repeat the current top-level location above page content: $name',
    ({ name, path, label }) => {
      mocks.route.name = name
      mocks.route.path = path
      const wrapper = mountLayout(label)

      expect(wrapper.find('nav[aria-label="面包屑"]').exists()).toBe(false)
      expect(wrapper.get('main h1').text()).toBe(label)
      expect(wrapper.findAll('h1')).toHaveLength(1)
      expect(wrapper.get('.topbar').find('h1').exists()).toBe(false)
    },
  )

  it.each([
    {
      name: 'VerificationDetail',
      path: '/verifications/record-a',
      parent: 'Verifications',
      label: '复测',
      title: '复测详情',
    },
    {
      name: 'ProjectSchedule',
      path: '/project-schedules/record-a',
      parent: 'ProjectSchedules',
      label: '计划',
      title: '计划详情',
    },
  ])(
    'retains the navigable parent and current location for $name without loading a project',
    async ({ name, path, parent, label, title }) => {
      mocks.route.name = name
      mocks.route.path = path
      mocks.route.params = { id: 'record-a' }
      mocks.route.matched = [{ path: path.replace('record-a', ':id') }]
      const wrapper = mountLayout(title)
      const breadcrumb = wrapper.get('nav[aria-label="面包屑"]')

      expect(breadcrumb.findAll('ol li')).toHaveLength(2)
      expect(breadcrumb.get('a').text()).toBe(label)
      expect(breadcrumb.get('a').attributes('href')).toBe(parent)
      expect(breadcrumb.get('[aria-current="page"]').text()).toBe(title)
      expect(wrapper.get('main h1').text()).toBe(title)
      expect(mocks.workspaceStore.loadProject).not.toHaveBeenCalled()
      expect(mocks.workspaceStore.clearProject).toHaveBeenCalledTimes(1)
      await openCommandPalette()
      await typePaletteQuery('运行终端')
      expect(paletteOptions()).toHaveLength(0)
    },
  )

  it('hides privileged commands from tenant users and restores trigger focus after Escape', async () => {
    mountLayout()
    const trigger = await openCommandPalette()
    const optionText = paletteOptions().map((option) => option.textContent || '')

    expect(optionText.some((text) => text.includes('转到全局监控大屏'))).toBe(false)
    expect(optionText.some((text) => text.includes('管理用户'))).toBe(false)
    expect(optionText.some((text) => text.includes('模型与 Worker'))).toBe(true)
    expect(optionText.some((text) => text.includes('漏洞复测配置'))).toBe(true)
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
