import { describe, expect, it } from 'vitest'

import { reportStateLabel, reportStateMeta } from './reportModel'

describe('reportModel', () => {
  it('maps report lifecycle states without hiding unknown values', () => {
    expect(reportStateLabel('pending')).toBe('等待生成')
    expect(reportStateLabel('running')).toBe('生成中')
    expect(reportStateLabel('ready')).toBe('报告已就绪')
    expect(reportStateLabel('failed')).toBe('生成失败')
    expect(reportStateLabel('not-started')).toBe('等待手动生成')
    expect(reportStateLabel('not-eligible')).toBe('暂停或完成后可生成')
    expect(reportStateLabel('future-report-state')).toBe('future-report-state')
    expect(reportStateMeta('failed')).toEqual({ label: '生成失败', tone: 'danger' })
  })
})
