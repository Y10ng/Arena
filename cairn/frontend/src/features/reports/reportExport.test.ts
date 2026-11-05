import { afterEach, describe, expect, it, vi } from 'vitest'
import JSZip from 'jszip'
import { fetchText } from '@/api/client'
import {
  createReportExport,
  fetchReportMarkdown,
  standaloneReportHtml,
  vulnerabilityReportUrl,
} from './reportExport'

vi.mock('@/api/client', () => ({ fetchText: vi.fn() }))

const sample = `# 受控漏洞报告

## 漏洞说明
受控资源读取接口未按当前身份检查资源归属，已记录的对照响应支持越权读取结论。
本次只确认合成记录范围，不推断其他资源均可访问。

第一行中文说明${'  '}
第二行包含 **重点**、*条件* 与 [参考链接](https://example.test/reference)。

## 复现步骤
1. 使用受控身份访问资源。
2. 改为另一合成记录标识，对照响应中的实际内容。

关键节选（请求与响应属于同一交互，响应仅保留支持结果的原文范围）：

\`\`\`http
GET /controlled?q=<value>&x=1 HTTP/1.1
Host: example.test

HTTP/1.1 200 OK
Content-Type: application/json

{"resource_id":"controlled-record","owner":"synthetic-peer"}
\`\`\`

非 HTTP 文本材料：保持原文。

| 项目 | 结果 |
| --- | --- |
| 条件 | 已记录 |

## 修复建议
- 检查权限边界。
- 增加对照验证。

最终尾部-不可丢失 <TAIL>&
`

afterEach(() => vi.unstubAllGlobals())

describe('选定交付正文三格式导出', () => {
  it('授权图片嵌入 HTML、DOCX 和 TAR，缺图仅留说明且不改代码证据', async () => {
    const bytes = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8WQAAAAASUVORK5CYII=', 'base64')
    const imageData = [
      { image: { artifact_id: 'safe-image', caption: '授权图片', content_type: 'image/png' }, blob: new Blob([bytes], { type: 'image/png' }) },
      { image: { artifact_id: 'missing-image', caption: '缺失图片', content_type: 'image/png' }, error: '无权读取图片' },
    ]
    const code = '```http\n![保持原文](arena-image:safe-image)\n```'
    const markdown = `# 主体\n\n${code}\n\n![授权图片](arena-image:safe-image)\n\n![缺失图片](arena-image:missing-image)`
    const html = await (await createReportExport(markdown, 'html', '受控报告', imageData)).text()
    expect(html).toContain(`<img src="data:image/png;base64,${bytes.toString('base64')}"`)
    expect(html).toContain('无权读取图片')
    expect(html).not.toContain('src="arena-image:')
    const md = await (await createReportExport(markdown, 'md', '受控报告', imageData)).text()
    expect(md).toContain(code)
    expect(md).toContain('请使用 Markdown 证据包')
    const tar = new Uint8Array(await (await createReportExport(markdown, 'tar', '受控报告', imageData)).arrayBuffer())
    const entries = new Map<string, Uint8Array>()
    for (let offset = 0; tar[offset];) {
      const name = new TextDecoder().decode(tar.slice(offset, offset + 100)).split('\0')[0]!
      const size = parseInt(new TextDecoder().decode(tar.slice(offset + 124, offset + 136)).replace(/\0/g, ''), 8)
      entries.set(name, tar.slice(offset + 512, offset + 512 + size))
      offset += 512 + Math.ceil(size / 512) * 512
    }
    expect([...entries.keys()]).toEqual(['report.md', 'images/evidence-1.png'])
    expect(entries.get('images/evidence-1.png')).toEqual(new Uint8Array(bytes))
    const packaged = new TextDecoder().decode(entries.get('report.md'))
    expect(packaged).toContain('![授权图片](images/evidence-1.png)')
    expect(packaged).toContain('无权读取图片')
    expect(packaged).toContain(code)
    vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue({ width: 1, height: 1, close: vi.fn() }))
    const docx = await createReportExport(markdown, 'docx', '受控报告', imageData)
    const zip = await JSZip.loadAsync(await docx.arrayBuffer())
    expect(Object.keys(zip.files).filter((name) => name.startsWith('word/media/') && !zip.files[name]?.dir)).toHaveLength(1)
    const xml = await zip.file('word/document.xml')!.async('string')
    expect(xml).toContain('<w:drawing>')
    expect(xml).toContain('无权读取图片')
    expect(xml).toContain('保持原文')
  })

  it('MD 保留已选正文，历史长正文也完整读取且禁缓存', async () => {
    const history = sample + '历史已选正文\n'.repeat(14000) + '历史尾部\n'
    const blob = await createReportExport(history, 'md', '受控报告')
    expect(blob.type).toBe('text/markdown;charset=utf-8')
    expect(blob.size).toBeGreaterThan(65536)
    expect(await blob.text()).toBe(history)
    const whitespace = '\n 原文 \r\n\n'
    expect(await (await createReportExport(whitespace, 'md', '原文')).text()).toBe(whitespace)
    vi.mocked(fetchText).mockResolvedValueOnce(sample)
    const signal = new AbortController().signal
    expect(await fetchReportMarkdown('/projects/p/artifacts/a/content', signal)).toBe(sample)
    expect(fetchText).toHaveBeenCalledWith('/projects/p/artifacts/a/content', signal, 'no-store')
    expect(vulnerabilityReportUrl('项目 /一', 'v/1', 'obs/2')).toBe(
      '/projects/%E9%A1%B9%E7%9B%AE%20%2F%E4%B8%80/vulnerabilities/v%2F1/report?observation_id=obs%2F2',
    )
  })

  it('HTML 是安全独立文档，不执行HTML、不加载图片、不保留离线相对文件链接', async () => {
    const malicious = `\n<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n[危险](javascript:alert(1))\n[文件](file:///etc/passwd)\n[站内](/projects/p)\n[相对](../../secret)\n[协议相对](//example.test/image)\n![图示](https://example.test/remote.png)\n`
    const blob = await createReportExport(sample + malicious, 'html', '<报告 & "标题">')
    const html = await blob.text()
    expect(html).toMatch(/^<!doctype html>/)
    expect(html).toContain('<meta charset="utf-8">')
    expect(html).toContain('<title>&lt;报告 &amp; &quot;标题&quot;&gt;</title>')
    expect(html).toContain('第一行中文说明<br>')
    expect(html).toContain('结论。\n本次只确认合成记录范围')
    expect(html).not.toContain('结论。<br>')
    expect(html).toContain('关键节选（请求与响应属于同一交互')
    expect(html).toContain('最终尾部-不可丢失 &lt;TAIL&gt;&amp;')
    expect(html).toContain('<table>')
    expect(html).toContain('GET /controlled?q=&lt;value&gt;&amp;x=1 HTTP/1.1')
    expect(html).not.toMatch(/<script|<img|onerror="|href="(?:javascript:|file:|\/|\.\.)/i)
    expect(html).toContain('图片：图示；离线导出不加载图片')
    expect(html).toContain('href="https://example.test/reference"')
    expect(html).toContain("default-src 'none'")
    expect(standaloneReportHtml('[本地](./report.md)', '标题')).toContain('<p>本地</p>')
  })

  it('DOCX 是真实OOXML，保留标题、多行、列表、表格、原始HTTP和完整尾部', async () => {
    const blob = await createReportExport(
      sample + '\n![图示](https://example.test/a.png)\n',
      'docx',
      '受控报告',
    )
    expect(blob.type).toBe(
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )
    const bytes = await blob.arrayBuffer()
    expect(new Uint8Array(bytes).slice(0, 2)).toEqual(new Uint8Array([0x50, 0x4b]))
    const zip = await JSZip.loadAsync(bytes)
    expect(zip.file('[Content_Types].xml')).not.toBeNull()
    expect(zip.file('word/styles.xml')).not.toBeNull()
    const xml = await zip.file('word/document.xml')!.async('string')
    expect(xml).toContain('第一行中文说明')
    expect(xml).toContain('第二行包含 ')
    expect(xml).toContain('<w:br/>')
    const paragraphs = xml.match(/<w:p[ >][\s\S]*?<\/w:p>/g) || []
    const softParagraph = paragraphs.find((paragraph) => paragraph.includes('受控资源读取接口'))!
    const hardParagraph = paragraphs.find((paragraph) => paragraph.includes('第一行中文说明'))!
    const packetParagraph = paragraphs.find((paragraph) => paragraph.includes('GET /controlled'))!
    expect(softParagraph).not.toContain('<w:br/>')
    expect(hardParagraph.match(/<w:br\/>/g)).toHaveLength(1)
    expect(packetParagraph).toContain('<w:pBdr>')
    expect(packetParagraph).toContain('w:before="180"')
    expect(packetParagraph).toContain('w:after="200"')
    expect(packetParagraph).toContain('<w:keepNext w:val="false"/>')
    expect(packetParagraph).toContain('<w:keepLines w:val="false"/>')
    expect(xml).toContain('<w:b/>')
    expect(xml).toContain('<w:i/>')
    expect(xml).toContain('<w:tbl>')
    expect(xml).toContain('<w:numPr>')
    expect(xml).toContain('Heading2')
    expect(xml).toContain('GET /controlled?q=&lt;value&gt;&amp;x=1 HTTP/1.1')
    expect(xml).toContain('非 HTTP 文本材料：保持原文。')
    expect(xml).toContain('最终尾部-不可丢失 &lt;TAIL&gt;&amp;')
    expect(xml).toContain('图片：图示；离线导出不加载图片')
    const relationships = await zip.file('word/_rels/document.xml.rels')!.async('string')
    expect(relationships).toContain('https://example.test/reference')
    expect(relationships).not.toContain('https://example.test/a.png')
    expect(zip.file('word/numbering.xml')).not.toBeNull()
  })

  it('Word无法表示的控制字符明确失败，不生成损坏文件或静默改写正文', async () => {
    await expect(createReportExport('受控\u0000原文', 'docx', '报告')).rejects.toThrow('控制字符')
  })
})
