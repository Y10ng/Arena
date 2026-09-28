import { flushPromises, mount } from '@vue/test-utils'
import { reactive } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { Hint, ProjectDetail } from '@/api/types'
import HintsPage from './HintsPage.vue'

const mocks = vi.hoisted(() => ({
  createProjectHint: vi.fn(),
  loadProject: vi.fn(),
  replaceProjectHints: vi.fn(),
  showToast: vi.fn(),
}))

vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  createProjectHint: mocks.createProjectHint,
}))

const workspaceStore = reactive<{
  currentProjectId: string | null
  projectDetail: ProjectDetail | null
  loadProject: typeof mocks.loadProject
  replaceProjectHints: typeof mocks.replaceProjectHints
}>({
  currentProjectId: 'project-a',
  projectDetail: null,
  loadProject: mocks.loadProject,
  replaceProjectHints: mocks.replaceProjectHints,
})

vi.mock('@/stores/workspace', () => ({
  useWorkspaceStore: () => workspaceStore,
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => reactive({ showToast: mocks.showToast }),
}))

const wrappers: VueWrapper[] = []

function hint(id: string, overrides: Partial<Hint> = {}): Hint {
  return {
    id,
    content: id,
    creator: 'usr-1',
    fact_id: null,
    intent_id: null,
    created_at: '2026-08-21T00:00:00Z',
    ...overrides,
  }
}

function detail(projectId: string, hints: Hint[] = []): ProjectDetail {
  return {
    project: {
      id: projectId,
      title: projectId,
      status: 'active',
      created_at: '2026-08-21T00:00:00Z',
      scenario_key: 'default-copy-v2',
      tenant_workers: [],
      tenant_id: 'tenant-a',
      tenant_name: 'Tenant A',
      assessment_protocol: 'v2',
      report_capability: {
        supported: false,
        policy_version: 0,
        template_profile: null,
        reason: 'report_policy_not_enabled',
      },
    },
    facts: [
      {
        id: 'origin',
        project_id: projectId,
        description: 'Origin',
        creator: 'system',
        created_at: '2026-08-21T00:00:00Z',
      },
      {
        id: 'f001',
        project_id: projectId,
        description: 'A complete fact description that must not be truncated from the option',
        creator: 'system',
        created_at: '2026-08-21T00:00:00Z',
      },
      {
        id: 'goal',
        project_id: projectId,
        description: 'Goal',
        creator: 'system',
        created_at: '2026-08-21T00:00:00Z',
      },
    ],
    intents: [
      {
        id: 'i001',
        project_id: projectId,
        from: ['origin'],
        description: 'A complete intent description',
        creator: 'system',
        created_at: '2026-08-21T00:00:00Z',
      },
    ],
    hints,
    targets: [],
    resources: [],
    library_bindings: [],
  }
}

function mountPage() {
  const wrapper = mount(HintsPage, { attachTo: document.body })
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

async function setTextarea(value: string) {
  const textarea = controlByLabel<HTMLTextAreaElement>('提示内容')
  textarea.value = value
  textarea.dispatchEvent(new Event('input', { bubbles: true }))
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
  for (const mock of Object.values(mocks)) mock.mockReset()
  HTMLElement.prototype.hasPointerCapture = () => false
  HTMLElement.prototype.setPointerCapture = () => undefined
  HTMLElement.prototype.releasePointerCapture = () => undefined
  workspaceStore.currentProjectId = 'project-a'
  workspaceStore.projectDetail = detail('project-a')
  mocks.loadProject.mockResolvedValue(undefined)
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('HintsPage', () => {
  it('renders labeled shared controls, an accurate sync status, and accessible hint rows', async () => {
    workspaceStore.projectDetail = detail('project-a', [
      hint('older', { created_at: '2026-08-20T00:00:00Z' }),
      hint('newer', {
        creator: 'system',
        content:
          'Authorization: Bearer allowed-secret\npassword=credential\n<script>window.compromised=true</script>',
        intent_id: 'i001',
        fact_id: 'f001',
        created_at: '2026-08-21T01:00:00Z',
      }),
    ])
    const wrapper = mountPage()

    expect(controlByLabel('目标线索 Intent')).toBeTruthy()
    expect(controlByLabel('目标 Fact 节点')).toBeTruthy()
    expect(controlByLabel('提示内容').getAttribute('required')).not.toBeNull()
    expect(wrapper.text()).toContain('随工作空间每 10 秒同步')
    expect(wrapper.text()).not.toContain('实时刷新')
    expect(wrapper.get('caption').text()).toBe('项目人工提示列表')
    expect(wrapper.findAll('th').every((heading) => heading.attributes('scope') === 'col')).toBe(
      true,
    )
    expect(wrapper.find('tbody tr').text()).toContain('newer')
    expect(wrapper.findAll('tbody tr')[1]?.text()).toContain('older')
    expect(wrapper.text()).toContain('AGENT 自治系统')
    expect(wrapper.text()).toContain('intent i001')
    expect(wrapper.text()).toContain('Authorization: Bearer allowed-secret')
    expect(wrapper.text()).toContain('<script>window.compromised=true</script>')
    expect(document.body.querySelector('script:not([src])')).toBeNull()

    await selectOption(
      '目标 Fact 节点',
      'f001 · A complete fact description that must not be truncated from the option',
    )
    expect(document.body.textContent).not.toContain('origin · Origin')
    expect(document.body.textContent).not.toContain('goal · Goal')
  })

  it('submits exact project, fact, and intent payloads with intent precedence', async () => {
    mocks.createProjectHint.mockImplementation(
      async (_projectId: string, body: { content: string }) =>
        hint(`created-${mocks.createProjectHint.mock.calls.length}`, {
          content: body.content,
          created_at: '2026-08-21T02:00:00Z',
        }),
    )
    const wrapper = mountPage()

    await setTextarea('  token=allowed-secret\npassword=credential  ')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.createProjectHint).toHaveBeenLastCalledWith('project-a', {
      content: 'token=allowed-secret\npassword=credential',
    })

    await setTextarea('fact content')
    await selectOption(
      '目标 Fact 节点',
      'f001 · A complete fact description that must not be truncated from the option',
    )
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.createProjectHint).toHaveBeenLastCalledWith('project-a', {
      content: 'fact content',
      fact_id: 'f001',
    })

    await setTextarea('intent content')
    await selectOption('目标线索 Intent', 'i001 · A complete intent description')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.createProjectHint).toHaveBeenLastCalledWith('project-a', {
      content: 'intent content',
      intent_id: 'i001',
    })
  })

  it('guards keyboard and button submission while a request is pending', async () => {
    const pending = deferred<Hint>()
    mocks.createProjectHint.mockReturnValue(pending.promise)
    mountPage()
    await setTextarea('pending content')
    const textarea = controlByLabel<HTMLTextAreaElement>('提示内容')

    textarea.dispatchEvent(
      new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', ctrlKey: true }),
    )
    textarea.dispatchEvent(
      new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', metaKey: true }),
    )
    buttonByText('提交提示').click()
    await flushPromises()

    expect(mocks.createProjectHint).toHaveBeenCalledOnce()
    expect(textarea.disabled).toBe(true)

    pending.resolve(hint('h001', { content: 'pending content' }))
    await flushPromises()
    expect(mocks.replaceProjectHints).toHaveBeenCalledOnce()
    expect(mocks.showToast).toHaveBeenCalledWith('指引提示已成功添加', 'success')
    expect(mocks.loadProject).toHaveBeenCalledWith('project-a')
    expect(controlByLabel<HTMLTextAreaElement>('提示内容').value).toBe('')
  })

  it('retains the current draft and allows retry after a creation failure', async () => {
    mocks.createProjectHint
      .mockRejectedValueOnce(new Error('hint unavailable'))
      .mockResolvedValueOnce(hint('h001', { content: 'retry content' }))
    const wrapper = mountPage()
    await setTextarea('retry content')

    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(controlByLabel<HTMLTextAreaElement>('提示内容').value).toBe('retry content')
    expect(mocks.showToast).toHaveBeenCalledWith('hint unavailable', 'danger')
    expect(mocks.replaceProjectHints).not.toHaveBeenCalled()
    expect(mocks.loadProject).not.toHaveBeenCalled()

    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.createProjectHint).toHaveBeenCalledTimes(2)
    expect(mocks.replaceProjectHints).toHaveBeenCalledOnce()
  })

  it('isolates stale completion across project changes and A to B to A revisits', async () => {
    const firstVisit = deferred<Hint>()
    mocks.createProjectHint.mockReturnValueOnce(firstVisit.promise)
    mountPage()
    await setTextarea('first A draft')
    buttonByText('提交提示').click()
    await flushPromises()

    workspaceStore.currentProjectId = 'project-b'
    workspaceStore.projectDetail = detail('project-b')
    await flushPromises()
    await setTextarea('B private draft')
    expect(controlByLabel<HTMLTextAreaElement>('提示内容').value).toBe('B private draft')

    workspaceStore.currentProjectId = 'project-a'
    workspaceStore.projectDetail = detail('project-a')
    await flushPromises()
    await setTextarea('second A draft')

    firstVisit.resolve(hint('h-stale', { content: 'first A draft' }))
    await flushPromises()

    expect(controlByLabel<HTMLTextAreaElement>('提示内容').value).toBe('second A draft')
    expect(mocks.replaceProjectHints).not.toHaveBeenCalled()
    expect(mocks.showToast).not.toHaveBeenCalled()
    expect(mocks.loadProject).not.toHaveBeenCalled()
  })

  it('suppresses stale failure feedback and releases only the originating pending token', async () => {
    const stale = deferred<Hint>()
    const current = deferred<Hint>()
    mocks.createProjectHint.mockReturnValueOnce(stale.promise).mockReturnValueOnce(current.promise)
    mountPage()
    await setTextarea('A draft')
    buttonByText('提交提示').click()
    await flushPromises()

    workspaceStore.currentProjectId = 'project-b'
    workspaceStore.projectDetail = detail('project-b')
    await flushPromises()
    await setTextarea('B draft')
    buttonByText('提交提示').click()
    await flushPromises()

    stale.reject(new Error('stale A failure'))
    await flushPromises()
    expect(mocks.showToast).not.toHaveBeenCalled()
    expect(controlByLabel<HTMLTextAreaElement>('提示内容').disabled).toBe(true)

    current.resolve(hint('h-b', { content: 'B draft' }))
    await flushPromises()
    expect(mocks.showToast).toHaveBeenCalledWith('指引提示已成功添加', 'success')
  })
})
