import MarkdownIt from 'markdown-it'

const markdown = new MarkdownIt({ html: false, linkify: false, typographer: false })

// 聊天正文没有附件授权；图片不发起请求，链接只呈现文字，成果动作由服务端字段提供。
markdown.renderer.rules.image = (tokens, index) =>
  markdown.utils.escapeHtml(`［图片：${tokens[index]?.content || '未提供说明'}；未加载］`)
markdown.renderer.rules.table_open = () => '<div class="agent-markdown-table"><table>'
markdown.renderer.rules.table_close = () => '</table></div>'

export function renderAgentMarkdown(content: string) {
  const tokens = markdown.parse(content, {})
  for (const token of tokens) {
    let href = ''
    for (const child of token.children ?? []) {
      if (child.type === 'link_open') {
        href = child.attrGet('href') || ''
        child.type = 'text'
        child.content = ''
      } else if (child.type === 'link_close') {
        child.type = 'text'
        child.content = `（${href}）`
      }
    }
  }
  return markdown.renderer.render(tokens, markdown.options, {})
}
