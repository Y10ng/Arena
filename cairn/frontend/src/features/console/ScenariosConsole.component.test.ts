import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { PromptGroup, Scenario, ScenarioOptions } from '@/api/types'
import ScenariosConsole from './ScenariosConsole.vue'

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  invalidateQueries: vi.fn(),
  listInfrastructureCapabilities: vi.fn(),
  listInfrastructureImages: vi.fn(),
  listInfrastructureWorkers: vi.fn(),
  listPromptGroups: vi.fn(),
  refetchScenarios: vi.fn(),
  routerPush: vi.fn(),
  showToast: vi.fn(),
  useQuery: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  api: mocks.api,
  listInfrastructureCapabilities: mocks.listInfrastructureCapabilities,
  listInfrastructureImages: mocks.listInfrastructureImages,
  listInfrastructureWorkers: mocks.listInfrastructureWorkers,
  listPromptGroups: mocks.listPromptGroups,
}))

vi.mock('@tanstack/vue-query', () => ({
  useQuery: mocks.useQuery,
  useQueryClient: () => ({ invalidateQueries: mocks.invalidateQueries }),
}))

vi.mock('vue-router', () => ({
  onBeforeRouteLeave: vi.fn(),
  useRouter: () => ({ push: mocks.routerPush }),
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => ({
    globalRefreshKey: 0,
    showToast: mocks.showToast,
  }),
}))

const defaultScenario: Scenario = {
  key: 'default',
  label: '默认探索',
  is_default: true,
  profile_id: 'default',
  image: '',
  prompt_group: 'default',
  knowledge: null,
  origin_template: '',
  goal_template: '保留目标模板',
  cap_add: ['NET_RAW'],
  features: { browser: true },
}

const removableScenario: Scenario = {
  key: 'custom-v2',
  label: '自定义渗透测试',
  is_default: false,
  profile_id: 'default',
  image: '',
  prompt_group: 'default',
  knowledge: null,
  origin_template: null,
  goal_template: null,
  cap_add: [],
  features: {},
}

const promptGroups: PromptGroup[] = [
  {
    key: 'default',
    path: '/prompts/default',
    builtin: true,
    managed: false,
    file_count: 5,
    referenced_by: ['default'],
    overrides_builtin: false,
    capabilities: ['exploration'],
    visibility: 'visible',
    valid: true,
    warnings: [],
  },
  {
    key: 'default-durable-report-v1',
    path: '/prompts/default-durable-report-v1',
    builtin: true,
    managed: false,
    file_count: 1,
    referenced_by: [],
    overrides_builtin: false,
    capabilities: ['durable-report'],
    visibility: 'visible',
    valid: true,
    warnings: [],
  },
  {
    key: 'dual-purpose',
    path: '/prompts/dual-purpose',
    builtin: false,
    managed: true,
    file_count: 6,
    referenced_by: [],
    overrides_builtin: false,
    capabilities: ['exploration', 'durable-report'],
    visibility: 'visible',
    valid: true,
    warnings: [],
  },
  {
    key: 'default-report',
    path: '/prompts/default-report',
    builtin: true,
    managed: false,
    file_count: 6,
    referenced_by: [],
    overrides_builtin: false,
    capabilities: ['exploration'],
    visibility: 'compatibility-only',
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
      cap_add: ['NET_RAW'],
      isolation: 'container',
      completed_action: 'stop',
    },
  ],
  prompt_groups: ['default'],
  knowledge: [],
  images: [],
  cap_add: ['NET_RAW'],
  cap_add_source: 'declared_inventory',
  features: [],
  project_counts: { default: 1, 'custom-v2': 0 },
}

const wrappers: VueWrapper[] = []

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

function buttonByText(text: string) {
  const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === text,
  )
  if (!button) throw new Error(`找不到按钮：${text}`)
  return button
}

function buttonContaining(text: string) {
  const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.includes(text),
  )
  if (!button) throw new Error(`找不到包含文本的按钮：${text}`)
  return button
}

async function mountPage() {
  const wrapper = mount(ScenariosConsole, { attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

async function openDeleteConfirmation() {
  buttonContaining('自定义渗透测试').click()
  await flushPromises()
  buttonByText('删除场景').click()
  await flushPromises()
}

function pressEscape() {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
}

beforeEach(() => {
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined

  mocks.api.mockReset().mockResolvedValue([])
  mocks.invalidateQueries.mockReset().mockResolvedValue(undefined)
  mocks.listInfrastructureCapabilities.mockReset().mockResolvedValue([])
  mocks.listInfrastructureImages.mockReset().mockResolvedValue([])
  mocks.listInfrastructureWorkers.mockReset().mockResolvedValue([])
  mocks.listPromptGroups.mockReset().mockResolvedValue([])
  mocks.refetchScenarios.mockReset().mockResolvedValue(undefined)
  mocks.routerPush.mockReset()
  mocks.showToast.mockReset()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  mocks.useQuery.mockReset().mockImplementation((options: { queryKey: string[] }) => {
    const key = options.queryKey[0]
    if (key === 'scenarios') {
      return {
        data: ref([defaultScenario, removableScenario]),
        isLoading: ref(false),
        isError: ref(false),
        error: ref(null),
        refetch: mocks.refetchScenarios,
      }
    }
    if (key === 'scenarioOptions') return { data: ref(scenarioOptions) }
    if (key === 'promptGroups') {
      return {
        data: ref(promptGroups),
        isLoading: ref(false),
        isError: ref(false),
      }
    }
    return { data: ref([]) }
  })
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('ScenariosConsole shared controls migration', () => {
  it('uses shared form controls while preserving the domain-specific scenario list', async () => {
    const wrapper = await mountPage()

    expect(wrapper.findAll('.ui-form-field')).toHaveLength(7)
    expect(wrapper.findAll('.ui-input')).toHaveLength(2)
    expect(wrapper.findAll('.ui-textarea')).toHaveLength(2)
    expect(wrapper.findAll('.ui-select__trigger')).toHaveLength(3)
    expect(wrapper.text()).toContain('手动报告能力')
    expect(wrapper.text()).toContain('报告 Prompt Group')
    expect(wrapper.findAll('.ui-checkbox')).toHaveLength(1)
    expect(wrapper.findAll('.ui-button').length).toBeGreaterThanOrEqual(4)
    expect(wrapper.find('select').exists()).toBe(false)
    expect(wrapper.find('.btn-danger').exists()).toBe(false)
    expect(wrapper.find('.btn-secondary').exists()).toBe(false)
    expect(wrapper.find('.btn-success').exists()).toBe(false)
    expect(wrapper.findAll('button.scenario-list-item')).toHaveLength(2)
  })

  it('configures report opt-in independently from the exploration Prompt Group', async () => {
    const originalFeatures = defaultScenario.features
    defaultScenario.features = {
      ...originalFeatures,
      report_policy_version: '1',
      report_prompt_group: 'default-durable-report-v1',
    }
    try {
      mocks.api.mockImplementation((method: string, path: string) => {
        if (method === 'POST' && path === '/scenarios/validate') {
          return Promise.resolve({
            scenario_count: 2,
            project_counts: { default: 1, 'custom-v2': 0 },
          })
        }
        return Promise.resolve([])
      })
      await mountPage()

      const reportPromptTrigger =
        document.body.querySelectorAll<HTMLButtonElement>('.ui-select__trigger')[2]
      expect(reportPromptTrigger?.disabled).toBe(false)
      expect(reportPromptTrigger?.textContent).toContain(
        'default-durable-report-v1 (内置 · 仅报告)',
      )

      const labelInput = document.body.querySelector<HTMLInputElement>(
        'input[placeholder="输入场景方案描述标题"]',
      )
      if (!labelInput) throw new Error('找不到场景名称输入框')
      labelInput.value = '报告场景'
      labelInput.dispatchEvent(new Event('input', { bubbles: true }))
      buttonByText('保存当前场景').click()
      await flushPromises()

      const payload = mocks.api.mock.calls.find(
        ([method, path]) => method === 'PUT' && path === '/scenarios',
      )?.[2] as Scenario[]
      expect(payload[0]?.prompt_group).toBe('default')
      expect(payload[0]?.features).toMatchObject({
        report_policy_version: '1',
        report_prompt_group: 'default-durable-report-v1',
      })
    } finally {
      defaultScenario.features = originalFeatures
    }
  })

  it('filters Prompt Group selectors by role and preserves selected compatibility references', async () => {
    const originalPromptGroup = defaultScenario.prompt_group
    const originalFeatures = defaultScenario.features
    defaultScenario.prompt_group = 'default-report'
    defaultScenario.features = {
      ...originalFeatures,
      report_policy_version: '1',
      report_prompt_group: 'default-report',
    }
    try {
      const wrapper = await mountPage()
      const vm = wrapper.vm as unknown as {
        reportPromptGroupOptions: Array<{ value: string; label: string }>
        scenarioPromptGroupOptions: Array<{ value: string; label: string }>
        selectedReportPromptGroupValid: boolean
        selectedScenarioPromptGroupValid: boolean
      }

      expect(vm.scenarioPromptGroupOptions.map((option) => option.value)).toEqual([
        'default',
        'dual-purpose',
        'default-report',
      ])
      expect(vm.scenarioPromptGroupOptions.at(-1)?.label).toContain('兼容保留（仅当前选择）')
      expect(vm.scenarioPromptGroupOptions.map((option) => option.value)).not.toContain(
        'default-durable-report-v1',
      )
      expect(vm.reportPromptGroupOptions.map((option) => option.value)).toEqual([
        'default-durable-report-v1',
        'dual-purpose',
        'default-report',
      ])
      expect(vm.reportPromptGroupOptions.at(-1)?.label).toContain('兼容保留（仅当前选择）')
      expect(vm.selectedScenarioPromptGroupValid).toBe(true)
      expect(vm.selectedReportPromptGroupValid).toBe(true)
    } finally {
      defaultScenario.prompt_group = originalPromptGroup
      defaultScenario.features = originalFeatures
    }
  })

  it('uses the shared empty state when no scenario can be selected', async () => {
    mocks.useQuery.mockImplementation((options: { queryKey: string[] }) => {
      const key = options.queryKey[0]
      if (key === 'scenarios') {
        return {
          data: ref([]),
          isLoading: ref(false),
          isError: ref(false),
          error: ref(null),
          refetch: mocks.refetchScenarios,
        }
      }
      if (key === 'scenarioOptions') return { data: ref(scenarioOptions) }
      if (key === 'promptGroups') {
        return {
          data: ref(promptGroups),
          isLoading: ref(false),
          isError: ref(false),
        }
      }
      return { data: ref([]) }
    })

    const wrapper = await mountPage()

    expect(wrapper.find('.ui-empty-state').exists()).toBe(true)
    expect(wrapper.text()).toContain('请选择场景')
  })
})

describe('ScenariosConsole delete ConfirmAction migration', () => {
  it('opens a semantic confirmation without the handwritten dialog shell', async () => {
    await mountPage()
    await openDeleteConfirmation()

    const dialog = document.body.querySelector<HTMLElement>('[role="alertdialog"]')
    expect(dialog).not.toBeNull()
    expect(dialog?.getAttribute('aria-labelledby')).toBeTruthy()
    expect(dialog?.getAttribute('aria-describedby')).toBeTruthy()
    expect(dialog?.textContent).toContain('删除场景配置')
    expect(dialog?.textContent).toContain('永久删除场景配置「custom-v2」')
    expect(dialog?.textContent).toContain('关联此场景的项目在新创时将无法使用对应配置组')
    expect(document.body.querySelector('.dialog-backdrop')).toBeNull()
    expect(document.body.querySelector('.danger-card')).toBeNull()
  })

  it('preserves the complete replacement payload and success side effects', async () => {
    await mountPage()
    await openDeleteConfirmation()

    buttonByText('确认删除').click()
    await flushPromises()

    expect(mocks.api).toHaveBeenCalledWith('PUT', '/scenarios', [
      {
        ...defaultScenario,
        origin_template: null,
      },
    ])
    expect(mocks.refetchScenarios).toHaveBeenCalledTimes(1)
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['scenarioOptions'] })
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['scenario-catalog'] })
    expect(mocks.showToast).toHaveBeenCalledWith('场景已成功删除', 'success')
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
    expect(buttonContaining('默认探索').getAttribute('aria-pressed')).toBe('true')
  })

  it('blocks duplicate submission and dismissal while pending', async () => {
    const pending = deferred<unknown>()
    mocks.api.mockReturnValue(pending.promise)
    await mountPage()
    await openDeleteConfirmation()

    buttonByText('确认删除').click()
    buttonByText('确认删除').click()
    await flushPromises()

    expect(mocks.api).toHaveBeenCalledTimes(1)
    expect(buttonByText('确认删除').getAttribute('aria-busy')).toBe('true')
    expect(buttonByText('取消').disabled).toBe(true)

    pressEscape()
    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    document.body.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')).not.toBeNull()

    pending.resolve([])
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
  })

  it('retains the confirmation after failure and allows retry', async () => {
    mocks.api.mockRejectedValueOnce(new Error('场景仍被引用')).mockResolvedValueOnce([])
    await mountPage()
    await openDeleteConfirmation()

    buttonByText('确认删除').click()
    await flushPromises()

    expect(document.body.querySelector('[role="alertdialog"]')).not.toBeNull()
    expect(buttonByText('取消').disabled).toBe(false)
    expect(mocks.showToast).toHaveBeenCalledWith('删除场景失败: 场景仍被引用', 'danger')

    buttonByText('确认删除').click()
    await flushPromises()
    expect(mocks.api).toHaveBeenCalledTimes(2)
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
  })
})

describe('ScenariosConsole whole-list ownership', () => {
  it('serializes mutations and preserves edits made while save is pending', async () => {
    const pendingPut = deferred<Scenario[]>()
    mocks.api.mockImplementation((method: string, path: string) => {
      if (method === 'POST' && path === '/scenarios/validate') {
        return Promise.resolve({
          scenario_count: 2,
          project_counts: { default: 1, 'custom-v2': 0 },
        })
      }
      if (method === 'PUT' && path === '/scenarios') return pendingPut.promise
      return Promise.resolve([])
    })
    await mountPage()

    const labelInput = document.body.querySelector<HTMLInputElement>(
      'input[placeholder="输入场景方案描述标题"]',
    )
    if (!labelInput) throw new Error('找不到场景名称输入框')
    labelInput.value = 'Submitted label'
    labelInput.dispatchEvent(new Event('input', { bubbles: true }))
    buttonByText('保存当前场景').click()
    await flushPromises()

    expect(mocks.api).toHaveBeenCalledTimes(2)
    expect(buttonByText('新建场景').disabled).toBe(true)
    labelInput.value = 'Continued label'
    labelInput.dispatchEvent(new Event('input', { bubbles: true }))
    buttonByText('新建场景').click()
    await flushPromises()
    expect(mocks.api).toHaveBeenCalledTimes(2)

    pendingPut.resolve([])
    await flushPromises()

    const putPayload = mocks.api.mock.calls.find(
      ([method, path]) => method === 'PUT' && path === '/scenarios',
    )?.[2] as Scenario[]
    expect(putPayload).toHaveLength(2)
    expect(putPayload[0]).toEqual({
      ...defaultScenario,
      label: 'Submitted label',
      origin_template: null,
      features: {
        assessment_protocol: 'legacy',
        blocker_cooldown_version: '0',
        isolation: 'container',
        minimum_coverage_version: '0',
        network_flow: false,
        outcome_contract_version: '1',
        quality_gate_version: '0',
        report_policy_version: '0',
        report_prompt_group: '',
        sandbox: false,
        surface_manifest_version: '0',
        verifier_contract_version: '0',
      },
    })
    expect(putPayload[1]).toEqual(removableScenario)
    expect(labelInput.value).toBe('Continued label')
    expect(mocks.refetchScenarios).toHaveBeenCalled()
  })

  it('refreshes after add without replacing a newer draft or duplicating the PUT', async () => {
    const pendingPut = deferred<Scenario[]>()
    mocks.api.mockReturnValueOnce(pendingPut.promise)
    await mountPage()

    const addButton = buttonByText('新建场景')
    addButton.click()
    addButton.click()
    await flushPromises()
    expect(mocks.api).toHaveBeenCalledTimes(1)

    const labelInput = document.body.querySelector<HTMLInputElement>(
      'input[placeholder="输入场景方案描述标题"]',
    )
    if (!labelInput) throw new Error('找不到场景名称输入框')
    labelInput.value = 'Newer draft'
    labelInput.dispatchEvent(new Event('input', { bubbles: true }))
    pendingPut.resolve([])
    await flushPromises()

    const payload = mocks.api.mock.calls[0]?.[2] as Scenario[]
    expect(payload).toHaveLength(3)
    expect(payload[0]).toEqual({ ...defaultScenario, origin_template: null })
    expect(payload[1]).toEqual(removableScenario)
    expect(payload[2]).toMatchObject({
      key: 'custom-scen-1',
      is_default: false,
      prompt_group: 'goal-driven-v1',
      origin_template: null,
      goal_template: null,
    })
    expect(labelInput.value).toBe('Newer draft')
    expect(mocks.refetchScenarios).toHaveBeenCalled()
  })
})
