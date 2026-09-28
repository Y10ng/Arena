export const SEVERITY_ORDER = [
  { key: 'critical', label: '严重' },
  { key: 'high', label: '高危' },
  { key: 'medium', label: '中危' },
  { key: 'low', label: '低危' },
  { key: 'info', label: '信息' },
] as const

export type SeverityKey = (typeof SEVERITY_ORDER)[number]['key']

export interface SeverityChartSegment {
  key: SeverityKey
  label: string
  count: number
  percentTenths: number
  percentLabel: string
  startAngle: number
  endAngle: number
  path: string
}

export interface SeverityChartModel {
  total: number
  segments: SeverityChartSegment[]
}

const CENTER = 60
const OUTER_RADIUS = 48
const INNER_RADIUS = 29

const normalizeCount = (value: unknown): number => {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return 0
  return Math.trunc(value)
}

const coordinate = (value: number): string => {
  const rounded = Math.round(value * 1000) / 1000
  return Object.is(rounded, -0) ? '0' : String(rounded)
}

const polarPoint = (radius: number, angle: number): [number, number] => {
  const radians = ((angle - 90) * Math.PI) / 180
  return [CENTER + radius * Math.cos(radians), CENTER + radius * Math.sin(radians)]
}

const cleanNumber = (value: number): number => {
  const rounded = Math.round(value * 1000) / 1000
  return Object.is(rounded, -0) ? 0 : rounded
}

export const severitySegmentAnchor = (
  segment: Pick<SeverityChartSegment, 'startAngle' | 'endAngle'>,
): { x: number; y: number } => {
  const [x, y] = polarPoint(OUTER_RADIUS + 4, (segment.startAngle + segment.endAngle) / 2)
  return { x: cleanNumber(x), y: cleanNumber(y) }
}

export const severitySegmentOffset = (
  segment: Pick<SeverityChartSegment, 'startAngle' | 'endAngle'>,
  distance = 5.5,
): { x: number; y: number } => {
  const radians = ((((segment.startAngle + segment.endAngle) / 2) - 90) * Math.PI) / 180
  return {
    x: cleanNumber(Math.cos(radians) * distance),
    y: cleanNumber(Math.sin(radians) * distance),
  }
}

const fullRingPath = (): string =>
  [
    `M ${CENTER} ${CENTER - OUTER_RADIUS}`,
    `A ${OUTER_RADIUS} ${OUTER_RADIUS} 0 1 1 ${CENTER} ${CENTER + OUTER_RADIUS}`,
    `A ${OUTER_RADIUS} ${OUTER_RADIUS} 0 1 1 ${CENTER} ${CENTER - OUTER_RADIUS}`,
    `L ${CENTER} ${CENTER - INNER_RADIUS}`,
    `A ${INNER_RADIUS} ${INNER_RADIUS} 0 1 0 ${CENTER} ${CENTER + INNER_RADIUS}`,
    `A ${INNER_RADIUS} ${INNER_RADIUS} 0 1 0 ${CENTER} ${CENTER - INNER_RADIUS}`,
    'Z',
  ].join(' ')

const ringSegmentPath = (startAngle: number, endAngle: number): string => {
  if (endAngle - startAngle >= 360) return fullRingPath()

  const [outerStartX, outerStartY] = polarPoint(OUTER_RADIUS, startAngle)
  const [outerEndX, outerEndY] = polarPoint(OUTER_RADIUS, endAngle)
  const [innerEndX, innerEndY] = polarPoint(INNER_RADIUS, endAngle)
  const [innerStartX, innerStartY] = polarPoint(INNER_RADIUS, startAngle)
  const largeArc = endAngle - startAngle > 180 ? 1 : 0

  return [
    `M ${coordinate(outerStartX)} ${coordinate(outerStartY)}`,
    `A ${OUTER_RADIUS} ${OUTER_RADIUS} 0 ${largeArc} 1 ${coordinate(outerEndX)} ${coordinate(outerEndY)}`,
    `L ${coordinate(innerEndX)} ${coordinate(innerEndY)}`,
    `A ${INNER_RADIUS} ${INNER_RADIUS} 0 ${largeArc} 0 ${coordinate(innerStartX)} ${coordinate(innerStartY)}`,
    'Z',
  ].join(' ')
}

const allocatePercentTenths = (counts: number[], total: number): number[] => {
  const raw = counts.map((count) => (count * 1000) / total)
  const allocated = raw.map(Math.floor)
  const remaining = 1000 - allocated.reduce((sum, value) => sum + value, 0)

  const order = raw
    .map((value, index) => ({ index, remainder: value - allocated[index] }))
    .sort((left, right) => right.remainder - left.remainder || left.index - right.index)

  for (let index = 0; index < remaining; index += 1) {
    allocated[order[index].index] += 1
  }
  return allocated
}

export const buildSeverityChart = (
  bySeverity: Record<string, number> | null | undefined,
): SeverityChartModel => {
  const rows = SEVERITY_ORDER.map((item) => ({
    ...item,
    count: normalizeCount(bySeverity?.[item.key]),
  })).filter((item) => item.count > 0)

  const total = rows.reduce((sum, item) => sum + item.count, 0)
  if (total === 0) return { total: 0, segments: [] }

  const percentages = allocatePercentTenths(
    rows.map((item) => item.count),
    total,
  )
  let angle = 0
  const segments = rows.map((item, index): SeverityChartSegment => {
    const startAngle = angle
    angle += (item.count / total) * 360
    const endAngle = index === rows.length - 1 ? 360 : angle
    const percentTenths = percentages[index]

    return {
      ...item,
      percentTenths,
      percentLabel: percentTenths === 0 ? '<0.1%' : `${(percentTenths / 10).toFixed(1)}%`,
      startAngle,
      endAngle,
      path: ringSegmentPath(startAngle, endAngle),
    }
  })

  return { total, segments }
}
