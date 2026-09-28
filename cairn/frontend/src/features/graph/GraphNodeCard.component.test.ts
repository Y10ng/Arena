import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import GraphNodeCard from './GraphNodeCard.vue'
import type { GraphNode } from './graphTypes'

const node: GraphNode = {
  id: 'pending:intent-a',
  position: { x: 40, y: 60 },
  data: {
    kind: 'pending',
    conclusion: '验证授权边界',
    summary: '使用普通用户会话访问管理接口，确认服务端是否执行权限检查。',
    status: 'exploring',
    liveState: 'heartbeat',
    worker: 'claude-code',
    nodeWidth: 280,
    nodeHeight: 150,
    evidenceCount: 2,
  },
}

function mountCard(value: GraphNode = node) {
  return mount(GraphNodeCard, {
    props: {
      node: value,
      selected: false,
      dragging: false,
      isCollapsed: false,
      getNodeTypeLabel: () => '探索意图',
      truncateText: (text: string) => text,
    },
  })
}

describe('GraphNodeCard', () => {
  it('renders a real kind icon, status, summary, worker, id, pulse, and visual handles', () => {
    const wrapper = mountCard()

    expect(wrapper.get('.node-card').classes()).toContain('node-kind-pending')
    expect(wrapper.get('.node-kind-icon svg').element).toBeTruthy()
    expect(wrapper.text()).toContain('探索意图')
    expect(wrapper.text()).toContain('心跳正常')
    expect(wrapper.text()).toContain('验证授权边界')
    expect(wrapper.text()).toContain('claude-code')
    expect(wrapper.text()).toContain('pending:intent-a')
    expect(wrapper.find('.node-pulse-dot').exists()).toBe(true)
    expect(wrapper.findAll('.node-handle')).toHaveLength(2)
    expect(
      wrapper
        .findAll('.node-handle')
        .every((handle) => handle.attributes('aria-hidden') === 'true'),
    ).toBe(true)
  })

  it('keeps running, selected, and critical states available together', () => {
    const wrapper = mount(GraphNodeCard, {
      props: {
        node: { ...node, data: { ...node.data, isCriticalPath: true } },
        selected: true,
        dragging: false,
        isCollapsed: false,
        getNodeTypeLabel: () => '探索意图',
        truncateText: (text: string) => text,
      },
    })

    expect(wrapper.get('.node-card').classes()).toEqual(
      expect.arrayContaining(['is-running', 'selected', 'critical']),
    )
  })

  it('preserves click, keyboard, drag, and collapse event contracts', async () => {
    const factNode: GraphNode = { ...node, id: 'fact-a', data: { ...node.data, kind: 'fact' } }
    const wrapper = mountCard(factNode)
    const card = wrapper.get('.node-card')

    await card.trigger('click')
    await card.trigger('keydown', { key: 'Enter' })
    await card.trigger('keydown', { key: ' ' })
    await card.trigger('pointerdown')
    await card.trigger('pointermove')
    await card.trigger('pointerup')
    await card.trigger('pointercancel')
    await wrapper.get('.btn-collapse').trigger('click')

    expect(wrapper.emitted('selectClick')?.[0]?.[1]).toEqual(factNode)
    expect(wrapper.emitted('select')).toEqual([[factNode], [factNode]])
    expect(wrapper.emitted('dragStart')?.[0]?.[1]).toEqual(factNode)
    expect(wrapper.emitted('dragMove')?.[0]?.[1]).toEqual(factNode)
    expect(wrapper.emitted('dragEnd')?.[0]?.[1]).toEqual(factNode)
    expect(wrapper.emitted('dragCancel')?.[0]?.[1]).toEqual(factNode)
    expect(wrapper.emitted('toggleCollapse')).toEqual([['fact-a']])
  })
})
