import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import type { TenantModel } from '@/api/types'
import TenantModelsPanel from './TenantModelsPanel.vue'

const mocks = vi.hoisted(() => ({
  models: [] as TenantModel[],
  createTenantModel: vi.fn(),
  deleteTenantModel: vi.fn(),
  deleteTenantModelCredential: vi.fn(),
  discoverTenantModelDraft: vi.fn(),
  invalidateQueries: vi.fn(),
  modelsRefetch: vi.fn(),
  setQueryData: vi.fn(),
  showToast: vi.fn(),
  testTenantModelDraft: vi.fn(),
  updateTenantModel: vi.fn(),
}))
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ApiError: actual.ApiError,
    isRevisionConflict: actual.isRevisionConflict,
    createTenantModel: mocks.createTenantModel,
    deleteTenantModel: mocks.deleteTenantModel,
    deleteTenantModelCredential: mocks.deleteTenantModelCredential,
    discoverTenantModelDraft: mocks.discoverTenantModelDraft,
    listTenantModels: vi.fn(),
    testTenantModelDraft: mocks.testTenantModelDraft,
    updateTenantModel: mocks.updateTenantModel,
  }
})
vi.mock('@/stores/ui', () => ({ useUIStore: () => ({ showToast: mocks.showToast }) }))
vi.mock('@tanstack/vue-query', () => ({
  useQuery: () => ({
    data: ref(mocks.models),
    error: ref(null),
    isPending: ref(false),
    refetch: mocks.modelsRefetch,
  }),
  useQueryClient: () => ({
    invalidateQueries: mocks.invalidateQueries,
    setQueryData: mocks.setQueryData,
  }),
}))
const model: TenantModel = {
  id: 'model-a',
  display_name: '主要模型',
  protocol: 'openai-chat-completions',
  base_url: 'https://models.example/v1',
  auth_header: 'authorization-bearer',
  custom_headers: [],
  models_endpoint: null,
  inference_endpoint: null,
  model_name: 'primary',
  enabled: true,
  revision: 7,
  credential_configured: true,
  credential_version: 2,
  credential_key_version: 'key-v2',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
}
const wrappers: Array<ReturnType<typeof mount>> = []
function mountPanel() {
  const wrapper = mount(TenantModelsPanel, { attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}
function button(label: string) {
  const target = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === label,
  )
  if (!target) throw new Error(`找不到按钮：${label}`)
  return target
}
function input(label: string) {
  const field = [...document.body.querySelectorAll('label')].find(
    (candidate) => candidate.textContent?.replace('*', '').trim() === label,
  )
  const target = field?.getAttribute('for')
    ? document.getElementById(field.getAttribute('for')!)
    : field?.querySelector('input')
  if (!(target instanceof HTMLInputElement)) throw new Error(`找不到输入：${label}`)
  return target
}
async function setInput(label: string, value: string) {
  const target = input(label)
  target.value = value
  target.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
}
async function edit() {
  button('编辑').click()
  await flushPromises()
}
async function openSelect(label: string) {
  const trigger = document.body.querySelector<HTMLButtonElement>(
    `[role="combobox"][aria-label="${label}"]`,
  )!
  trigger.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0 }))
  trigger.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
  trigger.click()
  await flushPromises()
}
async function selectOption(label: string) {
  const option = [...document.body.querySelectorAll<HTMLElement>('[role="option"]')].find(
    (candidate) => candidate.textContent?.includes(label),
  )!
  option.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }))
  option.click()
  await flushPromises()
}
beforeEach(() => {
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
  mocks.models = [{ ...model }]
  mocks.createTenantModel.mockReset().mockResolvedValue(model)
  mocks.deleteTenantModel.mockReset().mockResolvedValue(undefined)
  mocks.deleteTenantModelCredential
    .mockReset()
    .mockResolvedValue({ ...model, revision: 8, credential_configured: false })
  mocks.discoverTenantModelDraft.mockReset().mockResolvedValue({
    model_names: ['provider-a', 'provider-b'],
    http_status: 200,
    latency_ms: 42,
    endpoint: 'https://models.example/models',
  })
  mocks.invalidateQueries.mockReset().mockResolvedValue(undefined)
  mocks.setQueryData.mockReset()
  mocks.modelsRefetch.mockReset().mockResolvedValue({ data: mocks.models, error: null })
  mocks.showToast.mockReset()
  mocks.testTenantModelDraft.mockReset()
  mocks.updateTenantModel.mockReset().mockResolvedValue(model)
})
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('TenantModelsPanel', () => {
  it('opens an editor only on demand and saves a blank Key without discovery or testing', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(wrapper.text()).not.toContain(model.base_url)
    await edit()
    expect(input('API Key').value).toBe('')
    button('保存模型').click()
    await flushPromises()
    expect(mocks.updateTenantModel).toHaveBeenCalledWith(
      model.id,
      expect.objectContaining({ expected_revision: 7, api_key: undefined }),
    )
    expect(mocks.discoverTenantModelDraft).not.toHaveBeenCalled()
    expect(mocks.testTenantModelDraft).not.toHaveBeenCalled()
    expect(wrapper.emitted('saved')?.[0]).toEqual([model])
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
  })

  it('uses shared Select inside the Dialog for protocol, auth and explicit discovery', async () => {
    mountPanel()
    await edit()
    await openSelect('模型协议格式')
    await selectOption('OpenAI Responses API')
    const advanced = [...document.body.querySelectorAll('details')].find((detail) =>
      detail.textContent?.includes('高级设置'),
    )!
    advanced.open = true
    await openSelect('模型认证字段')
    await selectOption('x-api-key: key')
    button('获取模型列表').click()
    await flushPromises()
    await openSelect('模型 ID 获取结果')
    await selectOption('provider-b')
    button('保存模型').click()
    await flushPromises()
    expect(mocks.updateTenantModel).toHaveBeenCalledWith(
      model.id,
      expect.objectContaining({
        protocol: 'openai-responses',
        auth_header: 'x-api-key',
        model_name: 'provider-b',
        expected_revision: 7,
      }),
    )
  })

  it('retains draft and plaintext Key on rejected save, and confirms cancellation', async () => {
    const wrapper = mountPanel()
    await edit()
    await setInput('名称', '未保存名称')
    await setInput('API Key', 'synthetic-key')
    mocks.updateTenantModel.mockRejectedValueOnce(new ApiError('配置不可保存', 422, {}))
    button('保存模型').click()
    await flushPromises()
    expect(input('名称').value).toBe('未保存名称')
    expect(input('API Key').value).toBe('synthetic-key')
    button('取消').click()
    await flushPromises()
    button('继续编辑').click()
    await flushPromises()
    expect(input('API Key').value).toBe('synthetic-key')
    const leave = wrapper.vm.requestLeave()
    await flushPromises()
    button('放弃修改').click()
    await flushPromises()
    expect(await leave).toBe(true)
    await edit()
    expect(input('API Key').value).toBe('')
  })

  it('keeps the editing revision until an explicit conflict review and preserves fields', async () => {
    mountPanel()
    await edit()
    await setInput('名称', '我的草稿')
    mocks.updateTenantModel.mockRejectedValueOnce(new ApiError('revision conflict', 409, {}))
    button('保存模型').click()
    await flushPromises()
    expect(button('保存模型').disabled).toBe(true)
    mocks.modelsRefetch.mockResolvedValue({
      data: [{ ...model, display_name: '其他会话', revision: 9 }],
      error: null,
    })
    button('读取最新版本').click()
    await flushPromises()
    expect(input('名称').value).toBe('我的草稿')
    button('已核对，保留草稿继续编辑').click()
    await flushPromises()
    button('保存模型').click()
    await flushPromises()
    expect(mocks.updateTenantModel).toHaveBeenLastCalledWith(
      model.id,
      expect.objectContaining({ display_name: '我的草稿', expected_revision: 9 }),
    )
  })

  it('ignores a discovery response after draft changes or closing and reopening', async () => {
    let resolve!: (value: unknown) => void
    mocks.discoverTenantModelDraft.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done
        }),
    )
    const wrapper = mountPanel()
    await edit()
    button('获取模型列表').click()
    await flushPromises()
    await wrapper.vm.requestLeave()
    await wrapper.vm.startCreate()
    await flushPromises()
    resolve({
      model_names: ['stale-model'],
      http_status: 200,
      latency_ms: 1,
      endpoint: 'https://models.example',
    })
    await flushPromises()
    expect(document.body.textContent).not.toContain('stale-model')
    expect(document.body.querySelector('[aria-label="模型 ID 获取结果"]')).toBeNull()
  })

  it('blocks duplicate creation and leaving after an unknown mutation until server review', async () => {
    const wrapper = mountPanel()
    await wrapper.vm.startCreate()
    await flushPromises()
    await setInput('名称', '新模型')
    await setInput('Base URL', 'https://models.example')
    await setInput('模型 ID', 'new')
    mocks.createTenantModel.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    button('保存模型').click()
    await flushPromises()
    expect(button('保存模型').disabled).toBe(true)
    expect(await wrapper.vm.requestLeave()).toBe(false)
    expect(mocks.createTenantModel).toHaveBeenCalledTimes(1)
    button('重新读取模型清单').click()
    await flushPromises()
    button('已核对，保留草稿继续编辑').click()
    await flushPromises()
    expect(button('保存模型').disabled).toBe(false)
    expect(input('名称').value).toBe('新模型')
    expect(mocks.createTenantModel).toHaveBeenCalledTimes(1)
  })

  it('confirms model deletion with captured identity and revision', async () => {
    mountPanel()
    await flushPromises()
    button('删除').click()
    await flushPromises()
    button('确认删除模型').click()
    await flushPromises()
    expect(mocks.deleteTenantModel).toHaveBeenCalledWith(model.id, 7)
    expect(mocks.showToast).toHaveBeenCalledWith('模型已删除', 'success')
  })

  it('removes a saved Key without discarding other model draft changes', async () => {
    mountPanel()
    await edit()
    await setInput('名称', '保留的草稿')
    await setInput('API Key', 'synthetic-key')
    button('删除已保存 Key').click()
    await flushPromises()
    button('确认删除 Key').click()
    await flushPromises()
    expect(mocks.deleteTenantModelCredential).toHaveBeenCalledWith(model.id, 7)
    expect(input('API Key').value).toBe('')
    expect(input('名称').value).toBe('保留的草稿')
    button('保存模型').click()
    await flushPromises()
    expect(mocks.updateTenantModel).toHaveBeenCalledWith(
      model.id,
      expect.objectContaining({
        expected_revision: 8,
        display_name: '保留的草稿',
        api_key: undefined,
      }),
    )
  })
})
