import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import type { GraphEdge, GraphEvidenceRecord, GraphNode } from './graphTypes'

const showToast = vi.fn()
vi.mock('@/stores/ui', () => ({ useUIStore: () => ({ showToast }) }))

import GraphDetailsDrawer from './GraphDetailsDrawer.vue'

const node: GraphNode = {
  id: 'fact-a',
  data: {
    kind: 'fact',
    label: '普通用户可以读取管理接口响应',
    creator: 'worker-a',
    createdAt: '2026-08-20T10:00:00Z',
    taskRunId: 'run-a',
    runStatus: 'failed',
    worker: 'claude-code',
    taskRun: {
      task_type: 'reason',
      error_type: 'protocol_error',
      error_message: 'Worker 结果协议无效',
    },
  },
}

const edge: GraphEdge = {
  id: 'intent-a',
  source: 'fact-a',
  target: 'fact-b',
  data: {
    label: '验证管理接口授权边界',
    worker: 'worker-b',
    creator: 'worker-a',
    createdAt: '2026-08-20T10:05:00Z',
    intent: {
      description: '使用普通用户会话访问管理接口。',
    },
  },
}

const evidence: GraphEvidenceRecord[] = [
  {
    artifact_id: 'artifact-a',
    artifact: { uri: 'artifacts/http/admin-response.txt' },
    created_at: '2026-08-20T10:02:00Z',
  },
]

const wrappers: VueWrapper[] = []

function mountDrawer(props: Partial<InstanceType<typeof GraphDetailsDrawer>['$props']> = {}) {
  const graphPage = document.createElement('section')
  graphPage.className = 'graph-page'
  document.body.appendChild(graphPage)

  const wrapper = mount(GraphDetailsDrawer, {
    attachTo: graphPage,
    props: {
      showNodeDrawer: true,
      showEdgeDrawer: false,
      selectedNode: node,
      selectedEdge: null,
      evidenceList: evidence,
      decisionResult: null,
      getNodeTypeLabel: (value: GraphNode) => value.data?.kind || 'unknown',
      ...props,
    },
  })
  wrappers.push(wrapper)
  return { graphPage, wrapper }
}

function buttonByText(text: string) {
  const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.includes(text),
  )
  if (!button) throw new Error(`找不到按钮：${text}`)
  return button
}

function pressEscape() {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  document.body.innerHTML = ''
  showToast.mockReset()
  vi.unstubAllGlobals()
})

describe('GraphDetailsDrawer migration', () => {
  it('renders one inline accessible right-side node Drawer', async () => {
    const { graphPage, wrapper } = mountDrawer()
    await flushPromises()

    const drawer = graphPage.querySelector<HTMLElement>('[role="dialog"]')
    expect(drawer).not.toBeNull()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true)
    expect(drawer?.classList.contains('ui-drawer__content--right')).toBe(true)
    expect(drawer?.classList.contains('ui-drawer__content--md')).toBe(true)
    expect(drawer?.getAttribute('aria-labelledby')).toBeTruthy()
    expect(drawer?.getAttribute('aria-describedby')).toBeTruthy()
    expect(graphPage.querySelector('.ui-drawer__overlay')).not.toBeNull()
    expect(document.body.querySelectorAll('[role="dialog"]')).toHaveLength(1)
    expect(document.body.textContent).toContain('fact-a')
    expect(document.body.textContent).toContain('普通用户可以读取管理接口响应')
    expect(document.body.textContent).toContain('admin-response.txt')
    expect(document.body.textContent).toContain('Worker 结果协议无效')
    expect(wrapper.find('.graph-retry-button.ui-button').exists()).toBe(true)
    expect(wrapper.find('.btn-add-hint').exists()).toBe(false)
  })

  it('gives node selection priority when both drawer flags are active', async () => {
    mountDrawer({
      showNodeDrawer: true,
      showEdgeDrawer: true,
      selectedNode: node,
      selectedEdge: edge,
      decisionResult: {
        outcome: 'Exploring',
        confidence: '95%',
        blocked: false,
        rationale: '继续验证。',
      },
    })
    await flushPromises()

    expect(document.body.querySelectorAll('[role="dialog"]')).toHaveLength(1)
    expect(document.body.textContent).toContain('fact-a')
    expect(document.body.textContent).not.toContain('验证管理接口授权边界')
  })

  it('renders edge decision and Claim audit content', async () => {
    mountDrawer({
      showNodeDrawer: false,
      showEdgeDrawer: true,
      selectedNode: null,
      selectedEdge: edge,
      evidenceList: [],
      decisionResult: {
        outcome: 'Exploring',
        confidence: '95%',
        blocked: true,
        rationale: '需要验证权限检查是否仅存在于前端。',
      },
    })
    await flushPromises()

    expect(document.body.querySelectorAll('[role="dialog"]')).toHaveLength(1)
    expect(document.body.textContent).toContain('intent-a')
    expect(document.body.textContent).toContain('验证管理接口授权边界')
    expect(document.body.textContent).toContain('Exploring')
    expect(document.body.textContent).toContain('Blocked')
    expect(document.body.textContent).toContain('需要验证权限检查是否仅存在于前端。')
    expect(document.body.textContent).toContain('worker-b')
  })

  it('shows both Reason provenance and represented Explore details', async () => {
    const narrativeEdge: GraphEdge = {
      id: 'reason-output:r1:i1',
      source: 'reason:r1',
      target: 'pending:i1',
      type: 'reason-audit',
      data: {
        auditDirection: 'output',
        label: '验证新入口',
        worker: 'reason-worker',
        taskRunId: 'reason-run',
        presentation: {
          role: 'reason-output-intent',
          reasonRunId: 'r1',
          representedIntentEdgeIds: ['intent:i1'],
          narratives: [
            {
              intentId: 'i1',
              canonicalEdgeIds: ['intent:i1'],
              label: '验证新入口',
              state: 'exploring',
              worker: 'explore-worker',
              creator: 'reason-worker',
              taskRunId: 'explore-run',
              isCriticalPath: true,
            },
          ],
        },
      },
    }
    const { wrapper } = mountDrawer({
      showNodeDrawer: false,
      showEdgeDrawer: true,
      selectedNode: null,
      selectedEdge: narrativeEdge,
      evidenceList: [],
    })
    await flushPromises()

    expect(document.body.textContent).toContain('Reason 输出 / Dispatched Explore')
    expect(document.body.textContent).toContain('验证新入口')
    expect(document.body.textContent).toContain('explore-worker')
    expect(document.body.textContent).toContain('Critical path')
    buttonByText('explore-run').click()
    expect(wrapper.emitted('goToRun')).toContainEqual(['explore-run'])
  })

  it('renders real Reason audit fields without Fact-only actions', async () => {
    const reasonNode: GraphNode = {
      id: 'reason:run-reason',
      data: {
        kind: 'reason',
        label: 'Reason 决策 run-reason',
        taskRunId: 'run-reason',
        runStatus: 'succeeded',
        reason: {
          runId: 'run-reason',
          worker: 'reason-worker',
          status: 'succeeded',
          startedAt: '2026-08-20T10:00:00Z',
          endedAt: '2026-08-20T10:00:05Z',
          outcome: 'intents',
          rationale: '优先验证权限边界。',
          confidence: 0.84,
          blockedBy: [],
          dependsOnTargets: ['target-a'],
          rejectedOptions: ['跳过验证'],
          inputNodeIds: ['fact-a'],
          outputNodeIds: ['pending:intent-b'],
          auditAvailable: true,
          errorType: '',
          errorMessage: '',
        },
      },
    }
    const { wrapper } = mountDrawer({
      selectedNode: reasonNode,
      evidenceList: evidence,
    })
    await flushPromises()

    expect(document.body.textContent).toContain('Reason 决策摘要')
    expect(document.body.textContent).toContain('优先验证权限边界。')
    expect(document.body.textContent).toContain('84%')
    expect(document.body.textContent).toContain('fact-a')
    expect(document.body.textContent).toContain('pending:intent-b')
    expect(document.body.textContent).not.toContain('证据列表')
    expect(wrapper.find('.graph-retry-button').exists()).toBe(false)
  })

  it('renders and copies pretty raw JSON only after clipboard success', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    mountDrawer()
    await flushPromises()

    expect(document.body.querySelector('.graph-raw-json')?.textContent).toBe(
      JSON.stringify(node.data, null, 2),
    )
    buttonByText('复制 JSON').click()
    await flushPromises()

    expect(writeText).toHaveBeenCalledWith(JSON.stringify(node.data, null, 2))
    expect(showToast).toHaveBeenCalledWith('原始 JSON 已复制', 'success')
  })

  it('ignores stale clipboard completion after same-selection data refresh', async () => {
    const pendingCopy = deferred<void>()
    const writeText = vi.fn().mockReturnValueOnce(pendingCopy.promise)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    const { wrapper } = mountDrawer()
    await flushPromises()

    buttonByText('复制 JSON').click()
    await wrapper.setProps({
      selectedNode: {
        ...node,
        data: { ...node.data, label: '轮询刷新后的事实内容' },
      },
    })
    pendingCopy.resolve()
    await flushPromises()

    expect(document.body.querySelector('.graph-raw-json')?.textContent).toContain(
      '轮询刷新后的事实内容',
    )
    expect(showToast).not.toHaveBeenCalled()
  })

  it('reports copy failures but ignores stale clipboard completion after selection changes', async () => {
    const pendingCopy = deferred<void>()
    const writeText = vi
      .fn()
      .mockReturnValueOnce(pendingCopy.promise)
      .mockRejectedValueOnce(new Error('denied'))
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    const { wrapper } = mountDrawer()
    await flushPromises()

    buttonByText('复制 JSON').click()
    await wrapper.setProps({
      showNodeDrawer: false,
      showEdgeDrawer: true,
      selectedNode: null,
      selectedEdge: edge,
    })
    pendingCopy.resolve()
    await flushPromises()
    expect(showToast).not.toHaveBeenCalled()

    buttonByText('复制 JSON').click()
    await flushPromises()
    expect(showToast).toHaveBeenCalledWith('复制失败，请手动选择文本复制', 'danger')
  })

  it('does not render an empty Drawer without a valid selection', async () => {
    mountDrawer({ selectedNode: null })
    await flushPromises()

    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
  })

  it('preserves close, run, artifact, and retry event payloads', async () => {
    const { wrapper } = mountDrawer()
    await flushPromises()

    buttonByText('换方向重试').click()
    document.body.querySelector<HTMLButtonElement>('.evidence-row-item')?.click()
    buttonByText('run-a').click()
    await flushPromises()

    expect(wrapper.emitted('retryFromFact')).toContainEqual(['fact-a'])
    expect(wrapper.emitted('previewArtifact')).toContainEqual(['artifact-a'])
    expect(wrapper.emitted('goToRun')).toContainEqual(['run-a'])
    expect(wrapper.emitted('close')).toHaveLength(1)

    document.body.querySelector<HTMLButtonElement>('button[aria-label="关闭图谱节点详情"]')?.click()
    await flushPromises()
    expect(wrapper.emitted('close')).toHaveLength(2)
  })

  it('closes on outside interaction', async () => {
    const { wrapper } = mountDrawer()
    await flushPromises()

    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    document.body.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
    await flushPromises()

    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('keeps fixed fullscreen active while the inline Drawer handles Escape', async () => {
    const fullscreenExit = vi.fn()
    const { graphPage, wrapper } = mountDrawer()
    const handleFullscreenEscape = () => {
      if (!graphPage.querySelector('[role="dialog"], [role="alertdialog"]')) {
        fullscreenExit()
      }
    }
    window.addEventListener('keydown', handleFullscreenEscape)
    await flushPromises()

    pressEscape()
    await flushPromises()

    window.removeEventListener('keydown', handleFullscreenEscape)
    expect(wrapper.emitted('close')).toHaveLength(1)
    expect(fullscreenExit).not.toHaveBeenCalled()
  })
})
