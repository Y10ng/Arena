import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'

const apiMock = vi.fn()
const listProjectTaskRunsMock = vi.fn()
const listProjectTaskRunResultEventsMock = vi.fn()
const routerPush = vi.fn()
const loadProject = vi.fn()
const showToast = vi.fn()

let workspaceStore: {
  currentProjectId: string | null
  projectDetail: {
    project: {
      id: string
      title: string
      status: string
    }
    facts: never[]
    intents: never[]
  } | null
  loadProject: typeof loadProject
}

vi.mock('@/api/client', () => ({
  api: (...args: unknown[]) => apiMock(...args),
  listProjectTaskRuns: (...args: unknown[]) => listProjectTaskRunsMock(...args),
  listProjectTaskRunResultEvents: (...args: unknown[]) =>
    listProjectTaskRunResultEventsMock(...args),
}))

vi.mock('@/stores/workspace', () => ({
  useWorkspaceStore: () => workspaceStore,
}))

vi.mock('@/stores/ui', () => ({
  useUIStore: () => ({ showToast }),
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ push: routerPush }),
}))

import GraphPage from './GraphPage.vue'

const wrappers: VueWrapper[] = []

const graphStubs = {
  GraphToolbar: {
    emits: ['addHint', 'toggleFullscreen', 'resetNodePositions'],
    template: `
      <div>
        <button type="button" data-testid="add-hint" @click="$emit('addHint')">
          添加提示
        </button>
        <button type="button" data-testid="toggle-fullscreen" @click="$emit('toggleFullscreen')">
          切换全屏
        </button>
        <button type="button" data-testid="reset-node-positions" @click="$emit('resetNodePositions')">
          重置节点
        </button>
      </div>
    `,
  },
  GraphViewport: {
    template: '<div data-testid="graph-viewport" />',
  },
  GraphPlaybackControls: {
    template: '<div data-testid="graph-playback" />',
  },
  GraphDetailsDrawer: {
    emits: ['retryFromFact'],
    template: `
      <button
        type="button"
        data-testid="retry-fact"
        @click="$emit('retryFromFact', 'fact-a')"
      >
        换方向重试
      </button>
    `,
  },
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

async function mountGraphPage() {
  const wrapper = mount(GraphPage, {
    attachTo: document.body,
    global: {
      stubs: graphStubs,
    },
  })
  wrappers.push(wrapper)
  await flushPromises()
  apiMock.mockClear()
  return wrapper
}

function buttonByText(text: string, root: ParentNode = document.body) {
  const button = [...root.querySelectorAll<HTMLButtonElement>('button')].find((candidate) =>
    candidate.textContent?.includes(text),
  )
  if (!button) throw new Error(`找不到按钮：${text}`)
  return button
}

function textareaByLabel(label: string) {
  const textarea = document.body.querySelector<HTMLTextAreaElement>(
    `textarea[aria-label="${label}"]`,
  )
  if (!textarea) throw new Error(`找不到文本域：${label}`)
  return textarea
}

function setTextareaValue(textarea: HTMLTextAreaElement, value: string) {
  textarea.value = value
  textarea.dispatchEvent(new Event('input', { bubbles: true }))
}

function pressEnter(textarea: HTMLTextAreaElement, modifier: 'ctrl' | 'meta') {
  textarea.dispatchEvent(
    new KeyboardEvent('keydown', {
      key: 'Enter',
      bubbles: true,
      ctrlKey: modifier === 'ctrl',
      metaKey: modifier === 'meta',
    }),
  )
}

function pressEscape() {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
}

beforeEach(() => {
  workspaceStore = {
    currentProjectId: 'project-a',
    projectDetail: {
      project: {
        id: 'project-a',
        title: '授权边界测试',
        status: 'active',
      },
      facts: [],
      intents: [],
    },
    loadProject,
  }
  apiMock.mockReset()
  apiMock.mockResolvedValue([])
  listProjectTaskRunsMock.mockReset()
  listProjectTaskRunsMock.mockResolvedValue([])
  listProjectTaskRunResultEventsMock.mockReset()
  listProjectTaskRunResultEventsMock.mockResolvedValue([])
  loadProject.mockReset()
  routerPush.mockReset()
  showToast.mockReset()
  window.localStorage.clear()
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
})

describe('GraphPage inline Dialog migration', () => {
  it('loads Reason result events in bounded batches', async () => {
    listProjectTaskRunsMock.mockResolvedValue(
      Array.from({ length: 51 }, (_, index) => ({
        id: `reason-${index}`,
        project_id: 'project-a',
        intent_id: null,
        task_type: 'reason',
        worker_name: 'reason-worker',
        worker_type: 'claudecode',
        status: 'succeeded',
        session_id: null,
        prompt_group: null,
        prompt_version: null,
        execution_profile: null,
        started_at: '2026-08-20T10:00:00Z',
        ended_at: '2026-08-20T10:00:01Z',
        exit_code: 0,
        error_type: null,
        error_message: null,
        assessment_attempt_id: null,
        runtime_context: {},
      })),
    )

    await mountGraphPage()

    expect(listProjectTaskRunResultEventsMock).toHaveBeenCalledTimes(2)
    expect(listProjectTaskRunResultEventsMock.mock.calls[0]?.[1]).toHaveLength(50)
    expect(listProjectTaskRunResultEventsMock.mock.calls[1]?.[1]).toHaveLength(1)
  })

  it('ignores legacy stored positions and resets only the current session override', async () => {
    const storageKey = 'cairn:graph-node-positions:project-a:LR'
    const legacyValue = JSON.stringify({ 'fact-a': { x: 120, y: 80 } })
    window.localStorage.setItem(storageKey, legacyValue)
    const getItem = vi.spyOn(Storage.prototype, 'getItem')
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const removeItem = vi.spyOn(Storage.prototype, 'removeItem')
    const wrapper = await mountGraphPage()

    await wrapper.get('[data-testid="reset-node-positions"]').trigger('click')

    expect(getItem).not.toHaveBeenCalled()
    expect(setItem).not.toHaveBeenCalled()
    expect(removeItem).not.toHaveBeenCalled()
    expect(window.localStorage.getItem(storageKey)).toBe(legacyValue)
  })

  it('keeps both named Dialogs inside the Graph fullscreen host', async () => {
    const wrapper = await mountGraphPage()
    const graphPage = wrapper.get('.graph-page').element

    await wrapper.get('[data-testid="retry-fact"]').trigger('click')
    await flushPromises()

    let dialog = graphPage.querySelector<HTMLElement>('[role="dialog"]')
    expect(dialog).not.toBeNull()
    expect(graphPage.querySelector('.ui-dialog__overlay')).not.toBeNull()
    expect(document.body.querySelectorAll('[role="dialog"]')).toHaveLength(1)
    expect(dialog?.getAttribute('aria-labelledby')).toBeTruthy()
    expect(dialog?.getAttribute('aria-describedby')).toBeTruthy()
    expect(dialog?.textContent).toContain('换方向重试（from fact-a）')
    const retryTextarea = textareaByLabel('新的探索角度或策略')
    expect(document.activeElement).toBe(retryTextarea)

    buttonByText('取消', dialog ?? graphPage).click()
    await flushPromises()
    expect(graphPage.querySelector('[role="dialog"]')).toBeNull()

    await wrapper.get('[data-testid="add-hint"]').trigger('click')
    await flushPromises()

    dialog = graphPage.querySelector<HTMLElement>('[role="dialog"]')
    expect(dialog?.textContent).toContain('添加项目提示')
    expect(dialog?.textContent).toContain('下一轮 reason')
    expect(textareaByLabel('项目提示内容')).not.toBeNull()
  })

  it('preserves retry trimming, duplicate guard, success toast, and reload', async () => {
    const pending = deferred<unknown>()
    apiMock.mockImplementation((method: string) =>
      method === 'GET' ? Promise.resolve([]) : pending.promise,
    )
    const wrapper = await mountGraphPage()

    await wrapper.get('[data-testid="retry-fact"]').trigger('click')
    const textarea = textareaByLabel('新的探索角度或策略')
    setTextareaValue(textarea, '  使用普通用户重放管理请求  ')
    await flushPromises()

    pressEnter(textarea, 'ctrl')
    pressEnter(textarea, 'ctrl')
    await flushPromises()

    expect(apiMock).toHaveBeenCalledTimes(1)
    expect(apiMock).toHaveBeenCalledWith('POST', '/projects/project-a/intents', {
      from: ['fact-a'],
      description: '使用普通用户重放管理请求',
    })
    expect(buttonByText('创建重试').getAttribute('aria-busy')).toBe('true')

    pending.resolve({ id: 'intent-a' })
    await flushPromises()

    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(showToast).toHaveBeenCalledWith(
      '已创建重试 intent，dispatcher 将选不同 worker 探索',
      'success',
    )
    expect(loadProject).toHaveBeenCalledWith('project-a')
  })

  it('preserves project hint payload and error retention', async () => {
    apiMock.mockImplementation((method: string) =>
      method === 'GET' ? Promise.resolve([]) : Promise.reject(new Error('提示写入失败')),
    )
    const wrapper = await mountGraphPage()

    await wrapper.get('[data-testid="add-hint"]').trigger('click')
    const textarea = textareaByLabel('项目提示内容')
    setTextareaValue(textarea, '  优先验证水平越权  ')
    pressEnter(textarea, 'meta')
    await flushPromises()

    expect(apiMock).toHaveBeenCalledWith('POST', '/projects/project-a/hints', {
      content: '优先验证水平越权',
    })
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
    expect(textarea.value).toBe('  优先验证水平越权  ')
    expect(showToast).toHaveBeenCalledWith('提示写入失败', 'danger')
    expect(loadProject).not.toHaveBeenCalled()
    expect(buttonByText('提交提示').disabled).toBe(false)
  })

  it('guards blank and missing-project submissions', async () => {
    const wrapper = await mountGraphPage()

    await wrapper.get('[data-testid="add-hint"]').trigger('click')
    expect(buttonByText('提交提示').disabled).toBe(true)
    pressEnter(textareaByLabel('项目提示内容'), 'ctrl')
    await flushPromises()
    expect(apiMock).not.toHaveBeenCalled()

    buttonByText('取消').click()
    await wrapper.get('[data-testid="retry-fact"]').trigger('click')
    const retry = textareaByLabel('新的探索角度或策略')
    setTextareaValue(retry, '验证错误处理')
    workspaceStore.currentProjectId = null
    buttonByText('创建重试').click()
    await flushPromises()
    expect(apiMock).not.toHaveBeenCalled()
  })

  it('allows pending dismissal without reopening after completion', async () => {
    const pending = deferred<unknown>()
    apiMock.mockImplementation((method: string) =>
      method === 'GET' ? Promise.resolve([]) : pending.promise,
    )
    const wrapper = await mountGraphPage()

    await wrapper.get('[data-testid="add-hint"]').trigger('click')
    setTextareaValue(textareaByLabel('项目提示内容'), '继续检查越权链路')
    await flushPromises()
    buttonByText('提交提示').click()
    await flushPromises()

    document.body.querySelector<HTMLButtonElement>('button[aria-label="关闭添加项目提示"]')?.click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()

    pending.resolve({ id: 'hint-a' })
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(showToast).toHaveBeenCalledWith('已添加项目提示，下一轮 reason 将注入', 'success')
    expect(loadProject).toHaveBeenCalledWith('project-a')
  })

  it('closes the inline Dialog before exiting fixed fullscreen', async () => {
    const wrapper = await mountGraphPage()

    await wrapper.get('[data-testid="toggle-fullscreen"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('.graph-page').classes()).toContain('is-fullscreen')

    await wrapper.get('[data-testid="add-hint"]').trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()

    pressEscape()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(wrapper.get('.graph-page').classes()).toContain('is-fullscreen')

    pressEscape()
    await flushPromises()
    expect(wrapper.get('.graph-page').classes()).not.toContain('is-fullscreen')
  })
})
