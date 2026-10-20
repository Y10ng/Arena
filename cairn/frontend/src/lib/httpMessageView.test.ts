import { describe, expect, it } from 'vitest'
import { httpMessageSummary, httpMessageView } from './httpMessageView'

const jsonTokens = (text: string) => text.match(/"(?:[^"\\]|\\.)*"|[^\s{}\[\],:]+|[{}\[\],:]/g)

describe('报文派生视图', () => {
  it('只从有效首行和真实 Host 提取摘要，不补造方法、协议或状态', () => {
    expect(httpMessageSummary('GET /sample HTTP/1.1\r\nHost: example.test\r\n\r\n')).toEqual({ marker: 'GET', target: 'example.test/sample' })
    expect(httpMessageSummary('HTTP/1.1 404 Not Found\n\n')).toEqual({ marker: '404', target: '' })
    expect(httpMessageSummary('POST https://example.test/sample HTTP/1.1\nHost: ignored.test\n\n')).toEqual({ marker: 'POST', target: 'https://example.test/sample' })
    expect(httpMessageSummary('普通原文\nHost: example.test')).toEqual({ marker: '', target: '' })
    expect(httpMessageSummary('GET /sample HTTP/1.1\n\nHost: not-a-header.test')).toEqual({ marker: 'GET', target: '/sample' })
  })

  it('区分格式适用性与必须常显的证据限制', () => {
    expect(httpMessageView('HTTP/1.1 200 OK\nContent-Type: text/plain\n\n普通正文').limitation).toBe(false)
    expect(httpMessageView('HTTP/1.1 200 OK\n\n正文 �').limitation).toBe(true)
    expect(httpMessageView('GET /sample HTTP/1.1').limitation).toBe(true)
    expect(httpMessageView('{"x":1}', true).limitation).toBe(true)
  })

  it('保留重复头、重复键、数值字面量及字符串转义，只调整正文空白', () => {
    const prefix = 'HTTP/1.1 200 OK\r\nSet-Cookie: a=1\r\nSet-Cookie: b=2\r\nContent-Type: application/json; charset=utf-8\r\n\r\n'
    const body = '{"id":900719925474099312345,"id":-0,"large":1e400,"escaped":"a \\" b\\n\\u0061","empty":[{},[]]}'
    const raw = prefix + body
    const view = httpMessageView(raw)
    expect(view.language).toBe('http')
    expect(view.pretty?.slice(0, prefix.length)).toBe(prefix)
    expect(jsonTokens(view.pretty!.slice(prefix.length))).toEqual(jsonTokens(body))
    expect(view.pretty).toContain('\n  "id": 900719925474099312345,\n  "id": -0,')
    expect(raw).toBe(prefix + body)
  })

  it('独立 JSON 不补造 HTTP 头，非 HTTP 文本保持文本', () => {
    expect(httpMessageView('{"x":1}')).toEqual({ language: 'json', pretty: '{\n  "x": 1\n}', reason: '', limitation: false })
    expect(httpMessageView('普通记录').language).toBe('plaintext')
    expect(httpMessageView('<script>alert(1)</script>').pretty).toBeNull()
  })

  it('截断、无效 JSON、替换解码、未知编码及二进制不提供 Pretty', () => {
    expect(httpMessageView('{"x":1}', true).pretty).toBeNull()
    for (const raw of [
      '{"x":',
      '{"x":"�"}',
      'HTTP/1.1 200 OK\nContent-Type: application/json; charset=gbk\n\n{"x":1}',
      'HTTP/1.1 200 OK\nContent-Type: application/octet-stream\n\n{"x":1}',
      'HTTP/1.1 200 OK\nContent-Encoding: gzip\n\n{"x":1}',
      'GET / HTTP/1.1\nHost: example.test',
    ]) {
      expect(httpMessageView(raw).pretty).toBeNull()
      expect(httpMessageView(raw).reason).not.toBe('')
    }
  })

  it('LF 分隔及重复键保持原词法，不让深层 JSON 生成巨量缩进', () => {
    const prefix = 'POST /api HTTP/1.1\nContent-Type: application/json\n\n'
    expect(httpMessageView(prefix + '{"x":1,"x":2}').pretty).toBe(prefix + '{\n  "x": 1,\n  "x": 2\n}')
    expect(httpMessageView('['.repeat(65) + '0' + ']'.repeat(65)).pretty).toBeNull()
  })
})
