import { describe, expect, it } from 'vitest'

import {
  buildSeverityChart,
  severitySegmentAnchor,
  severitySegmentOffset,
} from './overviewSeverityModel'

const percentTotal = (values: ReturnType<typeof buildSeverityChart>) => {
  return values.segments.reduce((sum, segment) => sum + segment.percentTenths, 0)
}

describe('buildSeverityChart', () => {
  it('returns an empty model for missing or zero-only data', () => {
    expect(buildSeverityChart(undefined)).toEqual({ total: 0, segments: [] })
    expect(buildSeverityChart({ critical: 0, high: 0 })).toEqual({ total: 0, segments: [] })
  })

  it('omits zero severities while preserving canonical order and labels', () => {
    const result = buildSeverityChart({ info: 2, critical: 1, medium: 0 })

    expect(result.segments.map((segment) => [segment.key, segment.label, segment.count])).toEqual([
      ['critical', '严重', 1],
      ['info', '信息', 2],
    ])
  })

  it('renders one severity as a complete ring with an exact percentage', () => {
    const result = buildSeverityChart({ high: 7 })
    const [segment] = result.segments

    expect(result.total).toBe(7)
    expect(segment.startAngle).toBe(0)
    expect(segment.endAngle).toBe(360)
    expect(segment.percentLabel).toBe('100.0%')
    expect(segment.path).toContain('A 48 48 0 1 1')
    expect(segment.path).not.toContain('NaN')
  })

  it('uses deterministic largest-remainder percentages that total 100.0%', () => {
    const result = buildSeverityChart({ critical: 1, high: 1, medium: 1 })

    expect(result.segments.map((segment) => segment.percentLabel)).toEqual([
      '33.4%',
      '33.3%',
      '33.3%',
    ])
    expect(percentTotal(result)).toBe(1000)
  })

  it('keeps exact count-based geometry for highly skewed data', () => {
    const result = buildSeverityChart({ critical: 999, info: 1 })
    const [critical, info] = result.segments

    expect(critical.endAngle).toBeCloseTo(359.64)
    expect(info.startAngle).toBeCloseTo(359.64)
    expect(info.endAngle).toBe(360)
    expect(percentTotal(result)).toBe(1000)
  })

  it('labels nonzero sub-tenth percentages without reporting zero', () => {
    const result = buildSeverityChart({ critical: 9999, info: 1 })
    const info = result.segments.find((segment) => segment.key === 'info')

    expect(info?.count).toBe(1)
    expect(info?.percentTenths).toBe(0)
    expect(info?.percentLabel).toBe('<0.1%')
    expect(percentTotal(result)).toBe(1000)
  })

  it('covers all five categories without gaps or overlaps', () => {
    const result = buildSeverityChart({ critical: 5, high: 4, medium: 3, low: 2, info: 1 })

    expect(result.total).toBe(15)
    expect(result.segments).toHaveLength(5)
    expect(result.segments[0].startAngle).toBe(0)
    expect(result.segments.at(-1)?.endAngle).toBe(360)
    result.segments.slice(1).forEach((segment, index) => {
      expect(segment.startAngle).toBeCloseTo(result.segments[index].endAngle)
    })
    expect(percentTotal(result)).toBe(1000)
  })

  it('normalizes invalid counts without producing invalid SVG paths', () => {
    const result = buildSeverityChart({
      critical: Number.NaN,
      high: Number.POSITIVE_INFINITY,
      medium: -3,
      low: 2.9,
      info: 1,
    })

    expect(result.total).toBe(3)
    expect(result.segments.map((segment) => segment.count)).toEqual([2, 1])
    expect(result.segments.every((segment) => !segment.path.includes('NaN'))).toBe(true)
    expect(result.segments.every((segment) => !segment.path.includes('Infinity'))).toBe(true)
  })

  it('derives a stable tooltip anchor without changing the SVG segment contract', () => {
    const [segment] = buildSeverityChart({ critical: 1, high: 1 }).segments
    const anchor = severitySegmentAnchor(segment)

    expect(Object.keys(segment)).toEqual([
      'key',
      'label',
      'count',
      'percentTenths',
      'percentLabel',
      'startAngle',
      'endAngle',
      'path',
    ])
    expect(anchor.x).toBeCloseTo(112)
    expect(anchor.y).toBeCloseTo(60)
  })

  it('derives a deterministic outward offset along the segment midpoint', () => {
    const [rightSegment, leftSegment] = buildSeverityChart({ critical: 1, high: 1 }).segments

    expect(severitySegmentOffset(rightSegment)).toEqual({ x: 5.5, y: 0 })
    expect(severitySegmentOffset(leftSegment)).toEqual({ x: -5.5, y: 0 })
    expect(Object.is(severitySegmentOffset(rightSegment).y, -0)).toBe(false)
  })
})
