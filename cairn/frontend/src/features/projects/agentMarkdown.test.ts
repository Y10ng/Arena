import { describe, expect, it } from 'vitest'
import { renderAgentMarkdown } from './agentMarkdown'

describe('聊天 Markdown', () => {
  it('排版基础正文，不执行 HTML、加载图片或生成模型动作链接', () => {
    const html = renderAgentMarkdown('# 标题\n\n- 列表 **重点** 与 `代码`\n\n```js\nconst a = "<script>"\n```\n\n| 项目 | 状态 |\n| --- | --- |\n| 查询 | 完成 |\n\n<script>alert(1)</script>\n\n![远程图片](https://example.test/private.png)\n\n[下载](/projects/p1/report/download)\n\n[外链](https://example.test)')
    expect(html).toContain('<h1>标题</h1>')
    expect(html).toContain('<strong>重点</strong>')
    expect(html).toContain('<code>代码</code>')
    expect(html).toContain('<pre><code class="language-js">')
    expect(html).toContain('<div class="agent-markdown-table"><table>')
    expect(html).not.toMatch(/<(?:script|img|a)\b/)
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(html).toContain('远程图片；未加载')
    expect(html).toContain('下载（/projects/p1/report/download）')
    expect(html).toContain('外链（https://example.test）')
  })
})
