import { describe, expect, it } from 'vitest'
import {
  GRAPH_NODE_KINDS,
  getGraphEdgePresentation,
  getGraphNodePresentation,
  isGraphEdgeRunning,
  resolveGraphNodeStatus,
} from './graphPresentation'
import type { GraphEdge, GraphNode } from './graphTypes'

describe('graphPresentation', () => {
  it('registers exactly the six real graph node kinds', () => {
    expect(GRAPH_NODE_KINDS).toEqual([
      'origin',
      'goal',
      'fact',
      'pending',
      'pending_group',
      'reason',
    ])
    expect(GRAPH_NODE_KINDS.map((kind) => getGraphNodePresentation(kind).label)).toEqual([
      '起点',
      '目标',
      'Fact 事实',
      '探索意图',
      '待定分支',
      'Reason 决策',
    ])
    GRAPH_NODE_KINDS.forEach((kind) => expect(getGraphNodePresentation(kind).icon).toBeTruthy())
  })

  it('uses live and failure status precedence without inventing business fields', () => {
    const node: GraphNode = {
      id: 'fact-a',
      data: { kind: 'fact', status: 'confirmed', runStatus: 'failed', liveState: 'heartbeat' },
    }
    expect(resolveGraphNodeStatus(node)).toMatchObject({
      key: 'failed',
      label: '失败',
      tone: 'bad',
    })

    node.data!.liveState = 'stale'
    expect(resolveGraphNodeStatus(node)).toMatchObject({
      key: 'stale',
      label: '过期',
      tone: 'warn',
    })

    node.data = { kind: 'pending', status: 'exploring' }
    expect(resolveGraphNodeStatus(node)).toMatchObject({ key: 'exploring', isRunning: true })
    expect(node.data).not.toHaveProperty('priority')
    expect(node.data).not.toHaveProperty('vulnerability')
  })

  it('falls back to the real unknown status text', () => {
    expect(resolveGraphNodeStatus({ id: 'x', data: { status: 'custom-state' } })).toMatchObject({
      key: 'custom-state',
      label: 'custom-state',
      tone: 'neutral',
    })
  })

  it('maps only Intent and Reason audit directions', () => {
    const intent: GraphEdge = { id: 'i', source: 'a', target: 'b', type: 'intent' }
    const input: GraphEdge = {
      id: 'ri',
      source: 'a',
      target: 'r',
      type: 'reason-audit',
      data: { auditDirection: 'input' },
    }
    const output: GraphEdge = {
      id: 'ro',
      source: 'r',
      target: 'b',
      type: 'reason-audit',
      data: { auditDirection: 'output' },
    }
    expect(getGraphEdgePresentation(intent).key).toBe('intent')
    expect(getGraphEdgePresentation(input)).toMatchObject({ key: 'reason-input', dashArray: '3,5' })
    expect(getGraphEdgePresentation(output)).toMatchObject({
      key: 'reason-output',
      dashArray: '9,5',
    })
  })

  it('marks edges running from their real state or target runtime status', () => {
    const edge: GraphEdge = { id: 'i', source: 'a', target: 'b', type: 'intent', data: {} }
    const nodes: GraphNode[] = [{ id: 'b', data: { kind: 'goal', runStatus: 'running' } }]
    expect(isGraphEdgeRunning(edge, nodes)).toBe(true)
    edge.data!.state = 'concluded'
    nodes[0].data!.runStatus = 'succeeded'
    expect(isGraphEdgeRunning(edge, nodes)).toBe(false)
  })
})
