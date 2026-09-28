import { describe, expect, it } from 'vitest'
import { summarizeHttpRequest, summarizeHttpResponse } from './httpEvidence'

describe('HTTP evidence summaries', () => {
  it('parses request method, path and byte count', () => {
    const summary = summarizeHttpRequest('POST /api/login HTTP/1.1\r\nHost: example.test\r\n\r\n{"x":1}')
    expect(summary.primary).toBe('POST')
    expect(summary.secondary).toBe('/api/login')
    expect(summary.lineCount).toBe(4)
    expect(summary.byteCount).toBeGreaterThan(0)
  })

  it('parses response status and content type', () => {
    const summary = summarizeHttpResponse('HTTP/1.1 403 Forbidden\nContent-Type: application/json\n\n{}')
    expect(summary.primary).toBe('403 Forbidden')
    expect(summary.secondary).toBe('application/json')
  })

  it('falls back safely for non-standard text', () => {
    expect(summarizeHttpRequest('curl output').primary).toBe('REQUEST')
    expect(summarizeHttpResponse('').primary).toBe('RESPONSE')
  })
})
