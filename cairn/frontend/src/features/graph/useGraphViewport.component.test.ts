import { computed } from 'vue'
import { describe, expect, it } from 'vitest'
import { useGraphViewport } from './useGraphViewport'
import type { GraphNode } from './graphTypes'

function createViewport(
  nodes: GraphNode[] = [
    { id: 'a', position: { x: 100, y: 80 }, data: { nodeWidth: 240, nodeHeight: 150 } },
  ],
) {
  const element = document.createElement('div')
  Object.defineProperties(element, {
    clientWidth: { configurable: true, value: 1000 },
    clientHeight: { configurable: true, value: 600 },
  })
  const viewportElement = computed(() => element)
  return useGraphViewport({
    viewportElement,
    graphBounds: computed(() => ({
      minX: 100,
      minY: 80,
      maxX: 340,
      maxY: 230,
      width: 240,
      height: 150,
    })),
    graphCanvasSize: computed(() => ({ width: 4000, height: 4000 })),
    laidOutNodes: computed(() => nodes),
  })
}

describe('useGraphViewport', () => {
  it('centers world points and nodes through the shared pan state', () => {
    const viewport = createViewport()
    viewport.zoom.value = 1

    viewport.centerWorldPoint(200, 150)
    expect(viewport.pan).toMatchObject({ x: 300, y: 150 })

    viewport.centerNodeInViewport({
      id: 'a',
      position: { x: 100, y: 80 },
      data: { nodeWidth: 240, nodeHeight: 150 },
    })
    expect(viewport.pan).toMatchObject({ x: 280, y: 145 })
  })

  it('zooms around the viewport center and respects interaction bounds', () => {
    const viewport = createViewport()
    viewport.zoom.value = 1
    viewport.pan.x = 0
    viewport.pan.y = 0

    viewport.zoomIn()
    expect(viewport.zoom.value).toBeCloseTo(1.2)
    expect(viewport.pan).toMatchObject({ x: -100, y: -60 })

    for (let index = 0; index < 30; index += 1) viewport.zoomIn()
    expect(viewport.zoom.value).toBe(2.5)
    for (let index = 0; index < 80; index += 1) viewport.zoomOut()
    expect(viewport.zoom.value).toBeGreaterThanOrEqual(0.15)
  })

  it('keeps fitViewport behavior and updates measured viewport size', () => {
    const viewport = createViewport()
    viewport.fitViewport()

    expect(viewport.viewportSize).toMatchObject({ width: 1000, height: 600 })
    expect(viewport.zoom.value).toBe(1.1)
    expect(viewport.pan.x).toBeCloseTo(258)
    expect(viewport.pan.y).toBeCloseTo(129.5)
  })
})
