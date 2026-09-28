import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { TenantWorker, WorkerTemplate } from '@/api/types'
import WorkerTemplatesPanel from './WorkerTemplatesPanel.vue'

const mocks = vi.hoisted(() => ({
  deleteWorkerTemplate: vi.fn(),
  invalidateQueries: vi.fn(),
  showToast: vi.fn(),
  useQuery: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  deleteWorkerTemplate: mocks.deleteWorkerTemplate,
  listTenantWorkers: vi.fn(),
  listWorkerTemplates: vi.fn(),
}))

vi.mock('@tanstack/vue-query', () => ({
  useQuery: mocks.useQuery,
  useQueryClient: () => ({ invalidateQueries: mocks.invalidateQueries }),
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => ({ showToast: mocks.showToast }),
}))

const templates: WorkerTemplate[] = [
  {
    id: 'tpl-1',
    name: '默认模板',
    owner: 'tester',
    tenant_worker_ids: ['worker-1'],
    worker_names: ['Claude Code Worker'],
    created_at: '2026-08-22T00:00:00Z',
  },
]
const workers: TenantWorker[] = [
  {
    id: 'worker-1',
    display_name: 'pi Worker',
    model_id: 'model-1',
    model_display_name: 'Claude',
    model_name: 'claude-fable-5',
    engine: 'pi',
    task_types: ['bootstrap'],
    max_running: 1,
    priority: 100,
    enabled: true,
    available: true,
    unavailable_reason: null,
    executor_available: true,
    revision: 1,
    created_at: '2026-08-22T00:00:00Z',
    updated_at: '2026-08-22T00:00:00Z',
  },
]

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

async function mountPanel() {
  const wrapper = mount(WorkerTemplatesPanel, {
    attachTo: document.body,
    props: { selectedId: 'tpl-1' },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined

  mocks.deleteWorkerTemplate.mockReset().mockResolvedValue(undefined)
  mocks.invalidateQueries.mockReset().mockResolvedValue(undefined)
  mocks.showToast.mockReset()
  mocks.useQuery.mockReset().mockImplementation((options: { queryKey: string[] }) => {
    if (options.queryKey[0] === 'worker-templates') {
      return {
        data: ref(templates),
        isLoading: ref(false),
        isError: ref(false),
        error: ref(null),
      }
    }
    return {
      data: ref(workers),
      isLoading: ref(false),
      isError: ref(false),
      error: ref(null),
    }
  })
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('WorkerTemplatesPanel shared migration', () => {
  it('uses semantic shared controls and emits row selection', async () => {
    const wrapper = await mountPanel()

    const rowButton = wrapper.get<HTMLButtonElement>('.tpl-row-select')
    expect(rowButton.attributes('aria-pressed')).toBe('true')
    expect(rowButton.find('button').exists()).toBe(false)
    expect(wrapper.findAll('.ui-button')).toHaveLength(1)
    expect(wrapper.findAll('.ui-icon-button')).toHaveLength(1)
    expect(wrapper.find('.btn-secondary').exists()).toBe(false)
    expect(wrapper.find('.btn-danger').exists()).toBe(false)
    expect(wrapper.text()).toContain('pi Worker · pi')

    await rowButton.trigger('click')
    expect(wrapper.emitted('select')?.[0]).toEqual([templates[0]])
  })

  it('shows the original historical engine and why the template Worker is unavailable', async () => {
    mocks.useQuery.mockImplementation((options: { queryKey: string[] }) => ({
      data: ref(
        options.queryKey[0] === 'worker-templates'
          ? templates
          : [
              {
                ...workers[0],
                engine: 'claudecode',
                available: false,
                unavailable_reason: 'engine_unsupported',
              },
            ],
      ),
      isLoading: ref(false),
      isError: ref(false),
      error: ref(null),
    }))

    const wrapper = await mountPanel()
    expect(wrapper.text()).toContain('claudecode')
    expect(wrapper.text()).toContain('历史引擎不受支持')
    expect(wrapper.text()).toContain('请新建 pi Worker')
  })

  it('keeps legacy template names readable without claiming an executable binding', async () => {
    mocks.useQuery.mockImplementation((options: { queryKey: string[] }) => ({
      data: ref(
        options.queryKey[0] === 'worker-templates'
          ? [{ ...templates[0], tenant_worker_ids: [] }]
          : workers,
      ),
      isLoading: ref(false),
      isError: ref(false),
      error: ref(null),
    }))

    const wrapper = await mountPanel()
    expect(wrapper.text()).toContain('Claude Code Worker')
    expect(wrapper.text()).toContain('历史模板不可执行，请重新绑定 pi Worker')
  })

  it('guards duplicate deletion and invalidates the template query after success', async () => {
    const pending = deferred<void>()
    mocks.deleteWorkerTemplate.mockReturnValue(pending.promise)
    await mountPanel()

    document.body.querySelector<HTMLButtonElement>('button[title="删除模板"]')?.click()
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')).not.toBeNull()

    buttonByText('确认删除').click()
    buttonByText('确认删除').click()
    await flushPromises()
    expect(mocks.deleteWorkerTemplate).toHaveBeenCalledTimes(1)
    expect(buttonByText('确认删除').getAttribute('aria-busy')).toBe('true')

    pending.resolve()
    await flushPromises()
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['worker-templates'] })
    expect(mocks.showToast).toHaveBeenCalledWith('模板已删除', 'info')
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
  })

  it('distinguishes an empty template list from loading and failure', async () => {
    mocks.useQuery.mockImplementation((options: { queryKey: string[] }) => {
      if (options.queryKey[0] === 'worker-templates') {
        return {
          data: ref([]),
          isLoading: ref(false),
          isError: ref(false),
          error: ref(null),
        }
      }
      return { data: ref(workers) }
    })

    const wrapper = await mountPanel()

    expect(wrapper.find('.ui-empty-state').exists()).toBe(true)
    expect(wrapper.text()).toContain('暂无 Worker 模板')
  })
})
