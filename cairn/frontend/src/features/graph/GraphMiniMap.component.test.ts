import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import GraphMiniMap from './GraphMiniMap.vue'
import type { GraphNode } from './graphTypes'

const nodes: GraphNode[] = [
  {
    id: 'origin',
    position: { x: 100, y: 80 },
    data: { kind: 'origin', nodeWidth: 240, nodeHeight: 150 },
  },
  {
    id: 'reason:r1',
    position: { x: 500, y: 100 },
    data: { kind: 'reason', nodeWidth: 280, nodeHeight: 150 },
  },
]

function mountMiniMap() {
  return mount(GraphMiniMap, {
    props: {
      nodes,
      graphBounds: { minX: 100, minY: 80, maxX: 780, maxY: 250, width: 680, height: 170 },
      pan: { x: -50, y: -25 },
      zoom: 0.5,
      viewportSize: { width: 800, height: 500 },
      selectedNodeId: 'reason:r1',
    },
  })
}

describe('GraphMiniMap', () => {
  it('projects real node kinds, selection, and the current visible world rectangle', () => {
    const wrapper = mountMiniMap()
    const projectedNodes = wrapper.findAll('.graph-minimap-node')
    const viewport = wrapper.get('.graph-minimap-viewport')

    expect(projectedNodes).toHaveLength(2)
    expect(projectedNodes[0].classes()).toContain('node-kind-origin')
    expect(projectedNodes[1].classes()).toContain('node-kind-reason')
    expect(projectedNodes[1].classes()).toContain('selected')
    expect(viewport.attributes()).toMatchObject({
      x: '100',
      y: '50',
      width: '1600',
      height: '1000',
    })
  })

  it('only requests centering through the public world-point event', async () => {
    const wrapper = mountMiniMap()
    const svg = wrapper.get('svg').element as SVGSVGElement
    Object.defineProperty(svg, 'getScreenCTM', {
      configurable: true,
      value: () => ({ inverse: () => ({}) }),
    })
    Object.defineProperty(svg, 'createSVGPoint', {
      configurable: true,
      value: () => ({
        x: 0,
        y: 0,
        matrixTransform: () => ({ x: 320, y: 180 }),
      }),
    })

    await wrapper.get('svg').trigger('click', { clientX: 10, clientY: 20 })
    expect(wrapper.emitted('centerWorldPoint')).toEqual([[320, 180]])
  })
})
