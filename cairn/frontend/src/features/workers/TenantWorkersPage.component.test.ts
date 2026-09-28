import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { TenantModel, TenantWorker } from '@/api/types'
import TenantWorkersPage from './TenantWorkersPage.vue'

const mocks = vi.hoisted(() => ({
  workers: [] as TenantWorker[],
  models: [] as TenantModel[],
  createTenantWorker: vi.fn(),
  deleteTenantWorker: vi.fn(),
  updateTenantWorker: vi.fn(),
  invalidateQueries: vi.fn(),
  workersRefetch: vi.fn(),
  modelsRefetch: vi.fn(),
  showToast: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  createTenantWorker: mocks.createTenantWorker,
  deleteTenantWorker: mocks.deleteTenantWorker,
  isRevisionConflict: () => false,
  listTenantModels: vi.fn(),
  listTenantWorkers: vi.fn(),
  updateTenantWorker: mocks.updateTenantWorker,
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => ({ showToast: mocks.showToast }),
}))

vi.mock('@tanstack/vue-query', () => ({
  useQuery: ({ queryKey }: { queryKey: string[] }) => {
    const workersQuery = queryKey[0] === 'tenant-workers'
    return {
      data: ref(workersQuery ? mocks.workers : mocks.models),
      error: ref(null),
      isPending: ref(false),
      refetch: workersQuery ? mocks.workersRefetch : mocks.modelsRefetch,
    }
  },
  useQueryClient: () => ({ invalidateQueries: mocks.invalidateQueries }),
}))

const anthropicModelA: TenantModel = {
  id: 'model-a',
  display_name: 'Claude 主模型',
  protocol: 'anthropic-messages',
  base_url: 'https://anthropic.example/v1',
  auth_header: 'x-api-key',
  custom_headers: [],
  models_endpoint: null,
  inference_endpoint: '/messages',
  model_name: 'claude-model-a',
  enabled: true,
  revision: 2,
  credential_configured: true,
  credential_version: 1,
  credential_key_version: 'key-v1',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
}

const anthropicModelB: TenantModel = {
  ...anthropicModelA,
  id: 'model-b',
  display_name: 'Claude 备用模型',
  model_name: 'claude-model-b',
  revision: 3,
}

const openAiModel: TenantModel = {
  ...anthropicModelA,
  id: 'model-openai',
  display_name: 'OpenAI Model',
  protocol: 'openai-responses',
  auth_header: 'authorization-bearer',
  model_name: 'gpt-model',
}

const openAiChatModel: TenantModel = {
  ...openAiModel,
  id: 'model-openai-chat',
  display_name: 'OpenAI Chat Model',
  protocol: 'openai-chat-completions',
}

const worker: TenantWorker = {
  id: 'worker-a',
  display_name: '主要 Worker',
  model_id: anthropicModelA.id,
  model_display_name: anthropicModelA.display_name,
  model_name: anthropicModelA.model_name,
  engine: 'pi',
  task_types: ['reason', 'bootstrap', 'explore'],
  max_running: 2,
  priority: 100,
  enabled: true,
  available: true,
  unavailable_reason: null,
  executor_available: true,
  revision: 7,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
}

const mountedWrappers: Array<ReturnType<typeof mount>> = []

function mountPage() {
  const wrapper = mount(TenantWorkersPage, {
    attachTo: document.body,
    global: {
      stubs: {
        RouterLink: { template: '<a><slot /></a>' },
      },
    },
  })
  mountedWrappers.push(wrapper)
  return wrapper
}

function button(label: string) {
  const target = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === label,
  )
  if (!target) throw new Error(`找不到按钮：${label}`)
  return target
}

beforeEach(() => {
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
  mocks.workers = [worker]
  mocks.models = [anthropicModelA, anthropicModelB, openAiModel, openAiChatModel]
  mocks.createTenantWorker.mockReset().mockResolvedValue(worker)
  mocks.deleteTenantWorker.mockReset().mockResolvedValue(undefined)
  mocks.updateTenantWorker
    .mockReset()
    .mockResolvedValue({ ...worker, model_id: anthropicModelB.id })
  mocks.invalidateQueries.mockReset().mockResolvedValue(undefined)
  mocks.workersRefetch.mockReset().mockResolvedValue({ error: null })
  mocks.modelsRefetch.mockReset().mockResolvedValue({ error: null })
  mocks.showToast.mockReset()
})

afterEach(() => {
  mountedWrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('TenantWorkersPage', () => {
  it('allows all three model protocols on a pi Worker and preserves the revision', async () => {
    mountPage()
    await flushPromises()

    const trigger = document.body.querySelector<HTMLButtonElement>('[role="combobox"]')
    expect(trigger?.textContent).toContain(anthropicModelA.display_name)
    trigger?.dispatchEvent(
      new PointerEvent('pointerdown', { bubbles: true, button: 0, ctrlKey: false }),
    )
    trigger?.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
    trigger?.click()
    await flushPromises()

    const options = [...document.body.querySelectorAll<HTMLElement>('[role="option"]')]
    expect(options.map((option) => option.textContent)).toEqual(
      expect.arrayContaining([
        expect.stringContaining(anthropicModelA.display_name),
        expect.stringContaining(anthropicModelB.display_name),
        expect.stringContaining(openAiModel.display_name),
        expect.stringContaining(openAiChatModel.display_name),
      ]),
    )

    const nextOption = options.find((option) =>
      option.textContent?.includes(openAiModel.display_name),
    )
    nextOption?.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
    nextOption?.click()
    await flushPromises()
    button('保存 Worker').click()
    await flushPromises()

    expect(mocks.updateTenantWorker).toHaveBeenCalledWith(
      worker.id,
      expect.objectContaining({
        model_id: openAiModel.id,
        expected_revision: worker.revision,
      }),
    )
  })

  it('creates a fixed pi Worker when only an OpenAI Model exists', async () => {
    mocks.models = [openAiModel]
    const wrapper = mountPage()
    await flushPromises()

    button('新增 Worker').click()
    await flushPromises()
    await wrapper.get('input:not([disabled])').setValue('OpenAI pi Worker')

    expect(document.body.querySelector('[role="combobox"]')?.textContent).toContain(
      openAiModel.display_name,
    )
    expect(
      [...document.body.querySelectorAll<HTMLInputElement>('input[disabled]')].map(
        (input) => input.value,
      ),
    ).toContain('pi')
    expect(document.body.querySelectorAll('[role="combobox"]')).toHaveLength(1)
    expect(button('保存 Worker').disabled).toBe(false)
    button('保存 Worker').click()
    await flushPromises()
    expect(mocks.createTenantWorker).toHaveBeenCalledWith(
      expect.objectContaining({
        display_name: 'OpenAI pi Worker',
        model_id: openAiModel.id,
      }),
    )
  })

  it('keeps historical engine metadata visible without allowing an implicit conversion', async () => {
    mocks.workers = [
      {
        ...worker,
        engine: 'claudecode',
        available: false,
        unavailable_reason: 'engine_unsupported',
      },
    ]
    mountPage()
    await flushPromises()

    expect(document.body.textContent).toContain('历史引擎 claudecode 不受支持')
    expect(document.body.textContent).toContain('请新建 pi Worker')
    expect(
      [...document.body.querySelectorAll<HTMLInputElement>('input[disabled]')].map(
        (input) => input.value,
      ),
    ).toContain('claudecode')
    expect(button('保存 Worker').disabled).toBe(true)
    expect(mocks.updateTenantWorker).not.toHaveBeenCalled()
  })

  it('keeps a new Worker invalid without a configured Model', async () => {
    mocks.models = []
    mountPage()
    await flushPromises()

    button('新增 Worker').click()
    await flushPromises()
    expect(document.body.querySelector('[role="combobox"]')?.textContent).toContain('请选择 Model')
    expect(document.body.textContent).toContain('OpenAI Chat Completions')
    expect(button('保存 Worker').disabled).toBe(true)
  })

  it('uses ConfirmAction for deletion and preserves the revision payload', async () => {
    mountPage()
    await flushPromises()

    button('删除').click()
    await flushPromises()

    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain(
      `删除 Worker「${worker.display_name}」`,
    )
    button('确认删除').click()
    await flushPromises()

    expect(mocks.deleteTenantWorker).toHaveBeenCalledWith(worker.id, worker.revision)
    expect(mocks.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['projects'] })
    expect(mocks.showToast).toHaveBeenCalledWith('Worker 已删除', 'info')
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
  })
})
