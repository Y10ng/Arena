import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { PromptGroup, Scenario, ScenarioOptions } from '@/api/types'
import { useScenariosConsole } from './useScenariosConsole'

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  listPromptGroups: vi.fn(),
  showToast: vi.fn(),
}))
vi.mock('@/api/client', () => ({ api: mocks.api, listPromptGroups: mocks.listPromptGroups }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('@/stores/ui', () => ({
  useUIStore: () => ({ globalRefreshKey: 0, showToast: mocks.showToast }),
}))

const scenario = (key: string, isDefault = false): Scenario => ({
  key,
  label: key,
  is_default: isDefault,
  profile_id: 'default',
  image: '',
  prompt_group: 'goal-driven-v1',
  knowledge: null,
  origin_template: null,
  goal_template: null,
  cap_add: [],
  features: {},
})
const options: ScenarioOptions = {
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
  features: [],
  project_counts: { A: 0, B: 0 },
}
const promptGroup: PromptGroup = {
  key: 'goal-driven-v1',
  path: '/prompts/goal-driven-v1',
  builtin: true,
  managed: false,
  file_count: 5,
  referenced_by: [],
  overrides_builtin: false,
  capabilities: ['exploration'],
  visibility: 'visible',
  valid: true,
  warnings: [],
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((yes) => {
    resolve = yes
  })
  return { promise, resolve }
}

let wrapper: VueWrapper | undefined
let queryClient: QueryClient | undefined
afterEach(() => {
  wrapper?.unmount()
  queryClient?.clear()
  vi.clearAllMocks()
})

describe('场景写入与迟到列表读取隔离', () => {
  it.each(['保存', '删除'] as const)(
    '%s 成功后拒绝 PUT 期间新发 GET 的旧列表，后续提交保留成功结果',
    async (action) => {
      const oldList = [scenario('A', true), scenario('B')]
      let serverList = structuredClone(oldList)
      const pendingPut = deferred<Scenario[]>()
      const pendingRead = deferred<Scenario[]>()
      let delayNextRead = false
      let readSignal: AbortSignal | undefined
      let putCount = 0
      mocks.listPromptGroups.mockResolvedValue([promptGroup])
      mocks.api.mockImplementation(
        (method: string, path: string, payload?: Scenario[], signal?: AbortSignal) => {
          if (method === 'GET' && path === '/scenarios/options') return Promise.resolve(options)
          if (method === 'GET' && path === '/scenarios') {
            if (delayNextRead) {
              delayNextRead = false
              readSignal = signal
              // 模拟读取已经完成但响应迟到；即使请求不响应 abort，也不能覆盖缓存。
              return pendingRead.promise
            }
            return Promise.resolve(structuredClone(serverList))
          }
          if (method === 'POST' && path === '/scenarios/validate')
            return Promise.resolve({ valid: true })
          if (method === 'PUT' && path === '/scenarios') {
            putCount++
            if (putCount === 1) return pendingPut.promise
            serverList = structuredClone(payload!)
            return Promise.resolve(serverList)
          }
          throw new Error(`非预期请求：${method} ${path}`)
        },
      )
      queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false, gcTime: Infinity } },
      })
      let state!: ReturnType<typeof useScenariosConsole>
      wrapper = mount(
        defineComponent({
          setup() {
            state = useScenariosConsole()
            return () => null
          },
        }),
        { global: { plugins: [[VueQueryPlugin, { queryClient }]] } },
      )
      await flushPromises()
      expect(state.scenarios.value).toEqual(oldList)

      let mutation: Promise<void>
      if (action === '保存') {
        state.addNewScenario()
        expect(state.scenarioDraftCanSave.value).toBe(true)
        mutation = state.saveScenarios()
      } else {
        state.selectScenario('B')
        state.confirmDeleteScenario()
        expect(state.deleteScenarioKey.value).toBe('B')
        mutation = state.saveDeleteScenario()
      }
      await flushPromises()
      expect(putCount).toBe(1)
      expect(state.isScenarioMutationPending.value).toBe(true)

      delayNextRead = true
      const refresh = queryClient.invalidateQueries({ queryKey: ['scenarios'] })
      await flushPromises()
      expect(readSignal).toBeDefined()
      expect(readSignal!.aborted).toBe(false)
      expect(queryClient.getQueryState(['scenarios'])?.fetchStatus).toBe('fetching')

      serverList = structuredClone(mocks.api.mock.calls.find(([method]) => method === 'PUT')![2])
      pendingPut.resolve(serverList)
      await mutation
      await flushPromises()
      expect(readSignal!.aborted).toBe(true)
      expect(queryClient.getQueryData(['scenarios'])).toEqual(serverList)
      expect(state.scenarios.value).toEqual(serverList)
      expect(state.isScenarioMutationPending.value).toBe(false)
      expect(mocks.showToast).toHaveBeenCalledWith(expect.any(String), 'success')

      pendingRead.resolve(oldList)
      await refresh
      await flushPromises()
      expect(queryClient.getQueryData(['scenarios'])).toEqual(serverList)
      expect(state.scenarios.value).toEqual(serverList)
      const savedKeys = action === '保存' ? ['A', 'B', 'custom-scen-1'] : ['A']
      expect(state.scenarios.value.map((item) => item.key)).toEqual(savedKeys)

      // 成功后的正常刷新仍可用；随后全表提交不能丢失新增项或恢复删除项。
      await queryClient.invalidateQueries({ queryKey: ['scenarios'] })
      await flushPromises()
      state.selectScenario('A')
      state.editSelectedScenario()
      state.scenarioDraft.value!.label = '更新 A'
      expect(state.scenarioDraftCanSave.value).toBe(true)
      await state.saveScenarios()
      const nextPayload = mocks.api.mock.calls.filter(
        ([method]) => method === 'PUT',
      )[1]![2] as Scenario[]
      expect(nextPayload.map((item) => item.key)).toEqual(savedKeys)
      expect(nextPayload.find((item) => item.key === 'A')?.label).toBe('更新 A')
      expect(queryClient.getQueryData(['scenarios'])).toEqual(nextPayload)
    },
  )
})
