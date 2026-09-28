import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import GraphSvgEdges from './GraphSvgEdges.vue'
import type { EdgeRoute } from './edgeRouting'
import type { GraphEdge } from './graphTypes'

const edges: GraphEdge[] = [
  {
    id: 'intent:i1',
    source: 'origin',
    target: 'fact:a',
    type: 'intent',
    data: { state: 'exploring', label: '验证授权边界', renderedSourceCount: 2 },
  },
  {
    id: 'reason-input:r1',
    source: 'origin',
    target: 'reason:r1',
    type: 'reason-audit',
    data: { auditDirection: 'input' },
  },
  {
    id: 'reason-output:r1',
    source: 'reason:r1',
    target: 'fact:a',
    type: 'reason-audit',
    data: { auditDirection: 'output' },
  },
]

const routes: Record<string, EdgeRoute> = Object.fromEntries(
  edges.map((edge, index) => [
    edge.id,
    {
      sx: 10,
      sy: 20 + index * 10,
      tx: 110,
      ty: 40 + index * 10,
      labelX: 60,
      labelY: 22 + index * 10,
    },
  ]),
)

function mountEdges() {
  return mount(GraphSvgEdges, {
    props: {
      canvasSize: { width: 400, height: 300 },
      edges,
      edgeCoordsMap: routes,
      showJoinBadges: true,
      showEdgeLabels: true,
      getEdgeGroupClasses: (edge: GraphEdge) => ({ 'is-active': edge.data?.state === 'exploring' }),
      getEdgeColor: () => '#123456',
      getEdgeStrokeWidth: () => 2,
      getEdgeMarker: () => 'url(#arrow-intent)',
      getEdgeJoinBadgeWidth: () => 24,
      getEdgeJoinBadgeText: () => '+2',
      truncateText: (text: string) => text,
    },
  })
}

describe('GraphSvgEdges', () => {
  it('renders direct SVG lines with Intent and Reason dash semantics', () => {
    const wrapper = mountEdges()
    const groups = wrapper.findAll('.edge-group')

    expect(groups).toHaveLength(3)
    groups.forEach((group, index) => {
      const visible = group.get('line.edge-visible-path')
      const route = routes[edges[index].id]
      expect(visible.attributes('x1')).toBe(String(route.sx))
      expect(visible.attributes('y1')).toBe(String(route.sy))
      expect(visible.attributes('x2')).toBe(String(route.tx))
      expect(visible.attributes('y2')).toBe(String(route.ty))
      expect(group.findAll('path')).toHaveLength(0)
    })
    expect(groups[0].get('line.edge-visible-path').attributes('stroke-dasharray')).toBe('none')
    expect(groups[1].get('line.edge-visible-path').attributes('stroke-dasharray')).toBe('3,5')
    expect(groups[2].get('line.edge-visible-path').attributes('stroke-dasharray')).toBe('9,5')
    expect(groups[0].classes()).toContain('is-active')
    expect(groups[0].classes()).toContain('edge-semantic-intent')
    expect(groups[1].classes()).toContain('edge-semantic-reason-input')
    expect(groups[2].classes()).toContain('edge-semantic-reason-output')
    expect(groups[0].findAll('.edge-text')).toHaveLength(2)
    expect(groups[0].text()).toContain('验证授权边界')
    expect(groups[0].find('.edge-join-badge').exists()).toBe(true)
  })

  it('keeps mouse and keyboard edge selection on the wide hit line', async () => {
    const wrapper = mountEdges()
    const hitLine = wrapper.findAll('line.edge-hit-area')[0]

    await hitLine.trigger('click')
    await hitLine.trigger('keydown', { key: 'Enter' })
    await hitLine.trigger('keydown', { key: ' ' })

    expect(wrapper.emitted('selectEdge')).toEqual([[edges[0]], [edges[0]], [edges[0]]])
  })
})
