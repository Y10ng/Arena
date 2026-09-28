import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { TenantModel } from '@/api/types'
import TenantModelsPage from './TenantModelsPage.vue'

const mocks = vi.hoisted(() => ({
  models: [] as TenantModel[],
  createTenantModel: vi.fn(),
  deleteTenantModel: vi.fn(),
  deleteTenantModelCredential: vi.fn(),
  discoverTenantModelDraft: vi.fn(),
  invalidateQueries: vi.fn(),
  modelsRefetch: vi.fn(),
  showToast: vi.fn(),
  testTenantModelDraft: vi.fn(),
  updateTenantModel: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  createTenantModel: mocks.createTenantModel,
  deleteTenantModel: mocks.deleteTenantModel,
  deleteTenantModelCredential: mocks.deleteTenantModelCredential,
  discoverTenantModelDraft: mocks.discoverTenantModelDraft,
  isRevisionConflict: () => false,
  listTenantModels: vi.fn(),
  testTenantModelDraft: mocks.testTenantModelDraft,
  updateTenantModel: mocks.updateTenantModel,
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => ({ showToast: mocks.showToast }),
}))

vi.mock('@tanstack/vue-query', () => ({
  useQuery: () => ({
    data: ref(mocks.models),
    error: ref(null),
    isPending: ref(false),
    refetch: mocks.modelsRefetch,
  }),
  useQueryClient: () => ({ invalidateQueries: mocks.invalidateQueries }),
}))

const model: TenantModel = {
  id: 'model-a',
  display_name: '主要 Model',
  protocol: 'anthropic-messages',
  base_url: 'https://models.example/v1',
  auth_header: 'authorization-bearer',
  custom_headers: [],
  models_endpoint: null,
  inference_endpoint: null,
  model_name: 'claude-primary',
  enabled: true,
  revision: 7,
  credential_configured: true,
  credential_version: 2,
  credential_key_version: 'key-v2',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
}

const mountedWrappers: Array<ReturnType<typeof mount>> = []

function mountPage() {
  const wrapper = mount(TenantModelsPage, { attachTo: document.body })
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

async function openSelect(ariaLabel: string) {
  const trigger = document.body.querySelector<HTMLButtonElement>(
    `[role="combobox"][aria-label="${ariaLabel}"]`,
  )
  if (!trigger) throw new Error(`找不到 Select：${ariaLabel}`)
  trigger.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0 }))
  trigger.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
  trigger.click()
  await flushPromises()
}

async function selectOption(label: string) {
  const option = [...document.body.querySelectorAll<HTMLElement>('[role="option"]')].find(
    (candidate) => candidate.textContent?.includes(label),
  )
  if (!option) throw new Error(`找不到选项：${label}`)
  option.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
  option.click()
  await flushPromises()
}

beforeEach(() => {
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
  mocks.models = [model]
  mocks.createTenantModel.mockReset().mockResolvedValue(model)
  mocks.deleteTenantModel.mockReset().mockResolvedValue(undefined)
  mocks.deleteTenantModelCredential.mockReset().mockResolvedValue(undefined)
  mocks.discoverTenantModelDraft.mockReset().mockResolvedValue({
    model_names: ['provider-model-a', 'provider-model-b'],
    http_status: 200,
    latency_ms: 42,
    endpoint: 'https://models.example/v1/models',
  })
  mocks.invalidateQueries.mockReset().mockResolvedValue(undefined)
  mocks.modelsRefetch.mockReset().mockResolvedValue({ error: null })
  mocks.showToast.mockReset()
  mocks.testTenantModelDraft.mockReset()
  mocks.updateTenantModel.mockReset().mockResolvedValue(model)
})

afterEach(() => {
  mountedWrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('TenantModelsPage', () => {
  it('uses Cairn Select for protocol, auth header and discovered Provider Model ID', async () => {
    mountPage()
    await flushPromises()

    await openSelect('Model 协议格式')
    await selectOption('OpenAI Responses API')
    await openSelect('Model 认证字段')
    await selectOption('x-api-key: key')

    button('获取模型列表').click()
    await flushPromises()
    await openSelect('Provider Model ID 获取结果')
    await selectOption('provider-model-b')

    button('保存 Model').click()
    await flushPromises()

    expect(mocks.updateTenantModel).toHaveBeenCalledWith(
      model.id,
      expect.objectContaining({
        protocol: 'openai-responses',
        auth_header: 'x-api-key',
        model_name: 'provider-model-b',
        expected_revision: model.revision,
      }),
    )
  })

  it('confirms Model deletion with the captured revision', async () => {
    mountPage()
    await flushPromises()

    button('删除').click()
    await flushPromises()

    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain(
      `删除 Model「${model.display_name}」`,
    )
    button('确认删除 Model').click()
    await flushPromises()

    expect(mocks.deleteTenantModel).toHaveBeenCalledWith(model.id, model.revision)
    expect(mocks.showToast).toHaveBeenCalledWith('Model 已删除', 'success')
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
  })

  it('confirms API Key deletion, preserves the revision and clears plaintext input', async () => {
    mountPage()
    await flushPromises()

    const credentialInput = document.body.querySelector<HTMLInputElement>(
      'input[autocomplete="new-password"]',
    )
    if (!credentialInput) throw new Error('找不到 API Key 输入框')
    credentialInput.value = 'temporary-plaintext-key'
    credentialInput.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()

    button('删除已保存 Key').click()
    await flushPromises()
    expect(document.body.querySelector('[role="alertdialog"]')?.textContent).toContain(
      `删除 Model「${model.display_name}」已保存的 API Key`,
    )
    button('确认删除 Key').click()
    await flushPromises()

    expect(mocks.deleteTenantModelCredential).toHaveBeenCalledWith(model.id, model.revision)
    expect(credentialInput.value).toBe('')
    expect(mocks.showToast).toHaveBeenCalledWith('API Key 已删除', 'success')
    expect(document.body.querySelector('[role="alertdialog"]')).toBeNull()
  })
})
