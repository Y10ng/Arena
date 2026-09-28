import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import GraphViewport from './GraphViewport.vue'
import type { GraphNode } from './graphTypes'

let resizeCallback: ResizeObserverCallback | null = null

class ResizeObserverMock {
  constructor(callback: ResizeObserverCallback) {
    resizeCallback = callback
  }

  observe() {}
  unobserve() {}
  disconnect() {}
}

const node: GraphNode = {
  id: 'fact-a',
  type: 'fact',
  position: { x: 20, y: 30 },
  data: {
    kind: 'fact',
    label: '入口事实',
    nodeWidth: 240,
    nodeHeight: 150,
  },
}

beforeEach(() => {
  resizeCallback = null
  vi.stubGlobal('ResizeObserver', ResizeObserverMock)
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockImplementation(function (
    this: HTMLElement,
  ) {
    return Number.parseFloat(this.style.width) || 0
  })
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (
    this: HTMLElement,
  ) {
    return Number.parseFloat(this.style.height) || 0
  })
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('GraphViewport node measurement', () => {
  it('reports border-box dimensions instead of shrinking content-box dimensions', async () => {
    const wrapper = mount(GraphViewport, {
      props: {
        isPanning: false,
        isZooming: false,
        zoomPanStyle: {},
        graphCanvasSize: { width: 800, height: 600 },
        graphBounds: { minX: 20, minY: 30, maxX: 260, maxY: 180, width: 240, height: 150 },
        pan: { x: 0, y: 0 },
        zoom: 1,
        viewportSize: { width: 800, height: 600 },
        laidOutNodes: [node],
        laidOutEdges: [],
        edgeCoordsMap: {},
        selectedNodeId: null,
        draggingNodeId: null,
        manuallyCollapsedNodeIds: new Set<string>(),
        shouldShowJoinBadges: false,
        shouldShowEdgeLabels: false,
        getEdgeGroupClasses: () => ({}),
        getEdgeColor: () => '',
        getEdgeStrokeWidth: () => 1,
        getEdgeMarker: () => '',
        getEdgeJoinBadgeWidth: () => 0,
        getEdgeJoinBadgeText: () => '',
        getNodeTypeLabel: () => 'Fact',
        truncateText: (text: string) => text,
      },
      global: {
        stubs: {
          GraphSvgEdges: true,
        },
      },
    })
    await flushPromises()

    expect(wrapper.get('[aria-label="探索图谱图例"]').text()).toContain('Reason 输出')
    expect(wrapper.findAll('[aria-label="探索图谱图例"] .graph-legend-mark')).toHaveLength(6)
    expect(wrapper.get('button[aria-label="放大图谱"]').element).toBeTruthy()
    expect(wrapper.get('button[aria-label="缩小图谱"]').element).toBeTruthy()
    expect(wrapper.get('button[aria-label="适应视口"]').element).toBeTruthy()

    const viewport = wrapper.get('.viewport-container').element as HTMLElement
    resizeCallback?.(
      [
        {
          target: viewport,
          contentRect: { width: 640, height: 480 },
        } as unknown as ResizeObserverEntry,
      ],
      {} as ResizeObserver,
    )
    await flushPromises()
    expect(wrapper.emitted('viewportSizeChanged')).toHaveLength(1)

    const card = wrapper.get('[data-node-id="fact-a"]').element as HTMLElement
    resizeCallback?.(
      [
        {
          target: card,
          contentRect: { width: 208, height: 118 },
        } as unknown as ResizeObserverEntry,
      ],
      {} as ResizeObserver,
    )
    await flushPromises()

    expect(wrapper.emitted('nodeSizesChanged')).toEqual([
      [{ 'fact-a': { width: 240, height: 150 } }],
    ])

    wrapper.unmount()
  })
})
