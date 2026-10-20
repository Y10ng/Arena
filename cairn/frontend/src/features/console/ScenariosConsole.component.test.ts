import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { PromptGroup, Scenario, ScenarioOptions } from '@/api/types'
import ScenariosConsole from './ScenariosConsole.vue'

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  cancelQueries: vi.fn(),
  invalidateQueries: vi.fn(),
  setQueryData: vi.fn(),
  listPromptGroups: vi.fn(),
  routerPush: vi.fn(),
  showToast: vi.fn(),
  useQuery: vi.fn(),
  onBeforeRouteLeave: vi.fn(),
}))
vi.mock('@/api/client', () => ({ api: mocks.api, listPromptGroups: mocks.listPromptGroups }))
vi.mock('@tanstack/vue-query', () => ({
  useQuery: mocks.useQuery,
  useQueryClient: () => ({
    cancelQueries: mocks.cancelQueries,
    invalidateQueries: mocks.invalidateQueries,
    setQueryData: mocks.setQueryData,
  }),
}))
vi.mock('vue-router', () => ({
  onBeforeRouteLeave: mocks.onBeforeRouteLeave,
  useRouter: () => ({ push: mocks.routerPush }),
}))
vi.mock('@/stores/ui', () => ({
  useUIStore: () => ({ globalRefreshKey: 0, showToast: mocks.showToast }),
}))

const scenario = (key: string, overrides: Partial<Scenario> = {}): Scenario => ({
  key,
  label: key === 'default' ? '默认探索' : '自定义渗透测试',
  is_default: key === 'default',
  profile_id: 'default',
  image: '',
  prompt_group: 'goal-driven-v1',
  knowledge: null,
  origin_template: '  原始背景\n保留换行  ',
  goal_template: '保留目标模板',
  cap_add: [],
  features: { sandbox: true },
  ...overrides,
})
const promptGroups: PromptGroup[] = [
  {
    key: 'goal-driven-v1',
    path: '/prompts/goal-driven-v1',
    builtin: true,
    managed: false,
    file_count: 5,
    referenced_by: ['default'],
    overrides_builtin: false,
    capabilities: ['exploration', 'durable-report'],
    visibility: 'visible',
    valid: true,
    warnings: [],
  },
  {
    key: 'report-only',
    path: '/prompts/report-only',
    builtin: false,
    managed: true,
    file_count: 1,
    referenced_by: [],
    overrides_builtin: false,
    capabilities: ['durable-report'],
    visibility: 'visible',
    valid: true,
    warnings: [],
  },
]
const scenarioOptions: ScenarioOptions = {
  profiles: [
    {
      id: 'default',
      label: '默认画像',
      image_ref: '',
      image: '',
      network_policy: 'default',
      cap_add: [],
      isolation: 'container',
      completed_action: 'stop',
    },
  ],
  prompt_groups: ['goal-driven-v1'],
  knowledge: [],
  images: [],
  cap_add: [],
  cap_add_source: 'declared_inventory',
  features: [
    {
      key: 'sandbox',
      label: '沙箱',
      value_type: 'boolean',
      default: false,
      options: [],
      runtime_driver: false,
      description: '',
    },
  ],
  project_counts: { default: 1, 'custom-v2': 0 },
}
const scenariosData = ref<Scenario[]>([])
const optionsData = ref<ScenarioOptions>(scenarioOptions)
const optionsError = ref(false)
const scenariosError = ref(false)
const wrappers: VueWrapper[] = []
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}
function button(text: string) {
  const found = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (item) => item.textContent?.trim() === text,
  )
  if (!found) throw new Error(`找不到按钮：${text}`)
  return found
}
function labelInput() {
  const found = document.body.querySelector<HTMLInputElement>('input[placeholder="输入场景名称"]')
  if (!found) throw new Error('找不到场景名称输入框')
  return found
}
function inputValue(element: HTMLInputElement | HTMLTextAreaElement, value: string) {
  element.value = value
  element.dispatchEvent(new Event('input', { bubbles: true }))
}
async function mountPage() {
  const wrapper = mount(ScenariosConsole, { attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
async function edit() {
  button('编辑场景').click()
  await flushPromises()
}
function putPayload(): Scenario[] | undefined {
  return mocks.api.mock.calls.find(([method]) => method === 'PUT')?.[2]
}
async function openDeleteConfirmation() {
  button('自定义渗透测试').click()
  await flushPromises()
  button('删除场景').click()
  await flushPromises()
}

beforeEach(() => {
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
  scenariosData.value = [scenario('default'), scenario('custom-v2')]
  optionsData.value = structuredClone(scenarioOptions)
  optionsError.value = false
  scenariosError.value = false
  mocks.api
    .mockReset()
    .mockImplementation((method: string, _path: string, payload: Scenario[]) =>
      Promise.resolve(method === 'PUT' ? payload : { valid: true, scenario_count: payload.length }),
    )
  mocks.cancelQueries.mockReset().mockResolvedValue(undefined)
  mocks.invalidateQueries.mockReset().mockResolvedValue(undefined)
  mocks.setQueryData.mockReset().mockImplementation((_key: string[], value: Scenario[]) => {
    scenariosData.value = value
  })
  mocks.routerPush.mockReset()
  mocks.showToast.mockReset()
  mocks.onBeforeRouteLeave.mockReset()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  mocks.useQuery.mockReset().mockImplementation((options: { queryKey: string[] }) => {
    if (options.queryKey[0] === 'scenarios')
      return {
        data: scenariosData,
        isLoading: ref(false),
        isError: scenariosError,
        error: ref(new Error('列表读取失败')),
      }
    if (options.queryKey[0] === 'scenarioOptions')
      return {
        data: optionsData,
        isLoading: ref(false),
        isError: optionsError,
        error: ref(new Error('执行选项读取失败')),
      }
    if (options.queryKey[0] === 'promptGroups')
      return {
        data: ref(promptGroups),
        isLoading: ref(false),
        isError: ref(false),
        error: ref(null),
      }
    throw new Error('不应请求纯展示基础设施数据')
  })
})
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('探索场景阅读与弹窗草稿', () => {
  it('默认阅读原文，不加载纯展示总览或显示报告开关', async () => {
    const wrapper = await mountPage()
    expect(wrapper.findAll('button.scenario-list-item')).toHaveLength(2)
    expect(wrapper.find('.scenario-template-content').element.textContent).toBe(
      '  原始背景\n保留换行  ',
    )
    expect(wrapper.text()).toContain('保留目标模板')
    expect(document.body.querySelector('input, textarea')).toBeNull()
    expect(wrapper.text()).not.toContain('实时刷新')
    expect(wrapper.text()).not.toContain('手动报告能力')
    expect(wrapper.text()).not.toContain('报告 Prompt Group')
    expect(mocks.useQuery.mock.calls.map(([options]) => options.queryKey[0])).toEqual([
      'scenarios',
      'scenarioOptions',
      'promptGroups',
    ])
    expect(button('删除场景').disabled).toBe(true)
    expect(wrapper.text()).toContain('默认场景不可删除')
  })

  it('新建只创建内存草稿，取消和关闭都不产生 PUT 或占位场景', async () => {
    await mountPage()
    button('新建场景').click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain('新建场景')
    expect(document.body.querySelectorAll('.ui-select__trigger')).toHaveLength(1)
    inputValue(labelInput(), '未保存的新场景')
    button('取消').click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    button('新建场景').click()
    await flushPromises()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(mocks.api).not.toHaveBeenCalled()
    expect(scenariosData.value).toHaveLength(2)
  })

  it('编辑使用独立草稿，取消离开可保稿，确认取消后阅读仍是已保存值', async () => {
    await mountPage()
    await edit()
    inputValue(labelInput(), '未保存名称')
    const origin = document.body.querySelector<HTMLTextAreaElement>('textarea')!
    inputValue(origin, '  新的背景\n\n')
    await flushPromises()
    expect(scenariosData.value[0]?.label).toBe('默认探索')
    vi.mocked(window.confirm).mockReturnValueOnce(false)
    button('取消').click()
    await flushPromises()
    expect(labelInput().value).toBe('未保存名称')
    expect(origin.value).toBe('  新的背景\n\n')
    expect(mocks.onBeforeRouteLeave.mock.calls[0]?.[0]()).toBe(true)
    button('取消').click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(button('编辑场景')).toBeTruthy()
    expect(scenariosData.value[0]?.origin_template).toBe('  原始背景\n保留换行  ')
    expect(mocks.api).not.toHaveBeenCalled()
  })

  it('新建保存才预检和写入，并刷新创建目录缓存', async () => {
    await mountPage()
    button('新建场景').click()
    await flushPromises()
    inputValue(labelInput(), '新的场景')
    const origin = document.body.querySelector<HTMLTextAreaElement>('textarea')!
    inputValue(origin, '  背景\n')
    await flushPromises()
    button('保存场景').click()
    await flushPromises()
    expect(mocks.api.mock.calls.map(([method, path]) => [method, path])).toEqual([
      ['POST', '/scenarios/validate'],
      ['PUT', '/scenarios'],
    ])
    expect(putPayload()).toHaveLength(3)
    expect(putPayload()?.[2]).toMatchObject({
      key: 'custom-scen-1',
      label: '新的场景',
      origin_template: '  背景\n',
      goal_template: null,
      prompt_group: 'goal-driven-v1',
      features: {},
    })
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['scenarioOptions'] })
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['scenario-catalog'] })
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(button('新的场景').getAttribute('aria-pressed')).toBe('true')
  })

  it('刷新不覆盖编辑草稿，预检后写入使用最新其他场景和新增条目', async () => {
    const pending = deferred<unknown>()
    mocks.api.mockImplementation((method: string, _path: string, payload: Scenario[]) =>
      method === 'POST' ? pending.promise : Promise.resolve(payload),
    )
    await mountPage()
    await edit()
    inputValue(labelInput(), '我的草稿')
    await flushPromises()
    button('保存场景').click()
    await flushPromises()
    const refreshedOther = scenario('custom-v2', {
      label: '刷新后的另一场景',
      goal_template: '最新 Goal',
      features: { sandbox: false },
    })
    const added = scenario('another', { label: '后台新增场景' })
    scenariosData.value = [scenario('default', { label: '服务器名称' }), refreshedOther, added]
    await flushPromises()
    expect(labelInput().value).toBe('我的草稿')
    pending.resolve({ valid: true })
    await flushPromises()
    expect(putPayload()?.[0]?.label).toBe('我的草稿')
    expect(putPayload()?.slice(1)).toEqual([refreshedOther, added])
    expect(mocks.cancelQueries).toHaveBeenCalledWith({ queryKey: ['scenarios'] }, { revert: false })
  })

  it.each(['POST', 'PUT'])('%s 失败在对应阶段保留草稿并允许重试', async (failedMethod) => {
    let failed = false
    mocks.api.mockImplementation((method: string, _path: string, payload: Scenario[]) => {
      if (method === failedMethod && !failed) {
        failed = true
        return Promise.reject(new Error('定向失败'))
      }
      return Promise.resolve(method === 'PUT' ? payload : { valid: true })
    })
    await mountPage()
    await edit()
    inputValue(labelInput(), '失败仍保留')
    await flushPromises()
    button('保存场景').click()
    await flushPromises()
    expect(labelInput().value).toBe('失败仍保留')
    const dialog = document.body.querySelector('[role="dialog"]')!
    expect(dialog.textContent).toContain(
      failedMethod === 'POST' ? '保存预检失败：定向失败' : '写入场景失败：定向失败',
    )
    expect(dialog.textContent).not.toContain('保存预检通过')
    expect(mocks.showToast).not.toHaveBeenCalled()
    expect(scenariosData.value[0]?.label).toBe('默认探索')
    button('保存场景').click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(scenariosData.value[0]?.label).toBe('失败仍保留')
  })

  it('写入中阻止重复提交和关闭，避免修改已提交的草稿', async () => {
    const pending = deferred<Scenario[]>()
    mocks.api.mockImplementation((method: string) =>
      method === 'PUT' ? pending.promise : Promise.resolve({ valid: true }),
    )
    await mountPage()
    await edit()
    button('保存场景').click()
    button('保存场景').click()
    await flushPromises()
    expect(mocks.api).toHaveBeenCalledTimes(2)
    expect(button('新建场景').disabled).toBe(true)
    expect(labelInput().disabled).toBe(true)
    expect(button('取消').disabled).toBe(true)
    expect(mocks.onBeforeRouteLeave.mock.calls[0]?.[0]()).toBe(false)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
    pending.resolve(putPayload()!)
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
  })

  it('列表读取失败时保留草稿但阻止整表写入', async () => {
    await mountPage()
    await edit()
    inputValue(labelInput(), '列表恢复后再保存')
    scenariosError.value = true
    await flushPromises()
    expect(button('保存场景').disabled).toBe(true)
    expect(button('新建场景').disabled).toBe(true)
    expect(labelInput().value).toBe('列表恢复后再保存')
    expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain(
      '加载场景列表失败，暂不能保存',
    )
    expect(mocks.api).not.toHaveBeenCalled()
    scenariosError.value = false
    await flushPromises()
    expect(button('保存场景').disabled).toBe(false)
  })

  it('新建键在刷新后被占用时不覆盖该场景', async () => {
    await mountPage()
    button('新建场景').click()
    await flushPromises()
    scenariosData.value = [...scenariosData.value, scenario('custom-scen-1')]
    await flushPromises()
    button('保存场景').click()
    await flushPromises()
    expect(mocks.api).not.toHaveBeenCalled()
    expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain(
      '场景键值已被占用',
    )
  })

  it.each(['removed', 'default'])(
    '保留探索 Prompt 角色过滤与失效画像 %s 的保存阻断',
    async (profileId) => {
      scenariosData.value = [scenario('default', { profile_id: profileId }), scenario('custom-v2')]
      if (profileId === 'default') optionsData.value = { ...scenarioOptions, profiles: [] }
      const wrapper = await mountPage()
      await edit()
      const vm = wrapper.vm as unknown as { scenarioPromptGroupOptions: Array<{ value: string }> }
      expect(vm.scenarioPromptGroupOptions.map((item) => item.value)).toEqual(['goal-driven-v1'])
      expect(button('保存场景').disabled).toBe(true)
      expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain(
        '引用的执行环境画像不存在',
      )
    },
  )
})

describe('探索场景删除限制与确认', () => {
  it('使用中的场景不可删除，使用情况失败也不当作零引用', async () => {
    optionsData.value = { ...scenarioOptions, project_counts: { default: 1, 'custom-v2': 2 } }
    const wrapper = await mountPage()
    button('自定义渗透测试').click()
    await flushPromises()
    expect(button('删除场景').disabled).toBe(true)
    expect(wrapper.text()).toContain('2 个项目使用该场景')
    optionsError.value = true
    await flushPromises()
    expect(button('删除场景').disabled).toBe(true)
    expect(wrapper.text()).toContain('无法读取场景使用情况')
  })

  it('取消零写入，确认后保留其他场景并刷新目录', async () => {
    await mountPage()
    await openDeleteConfirmation()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain('custom-v2')
    button('取消').click()
    await flushPromises()
    expect(mocks.api).not.toHaveBeenCalled()
    button('删除场景').click()
    await flushPromises()
    button('确认删除').click()
    await flushPromises()
    expect(putPayload()).toEqual([scenario('default')])
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['scenario-catalog'] })
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
    expect(button('默认探索默认').getAttribute('aria-pressed')).toBe('true')
  })

  it('删除失败在确认框内可见并允许重试，提交中阻止重复和关闭', async () => {
    const pending = deferred<Scenario[]>()
    mocks.api
      .mockReturnValueOnce(pending.promise)
      .mockImplementation((_method, _path, payload) => Promise.resolve(payload))
    await mountPage()
    await openDeleteConfirmation()
    button('确认删除').click()
    button('确认删除').click()
    await flushPromises()
    expect(mocks.api).toHaveBeenCalledTimes(1)
    expect(button('取消').disabled).toBe(true)
    pending.reject(new Error('场景仍被引用'))
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain(
      '删除场景失败：场景仍被引用',
    )
    button('确认删除').click()
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
  })
})
