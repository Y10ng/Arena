import MarkdownIt from 'markdown-it'

export interface ReportHeading {
  id: string
  level: number
  title: string
}

const safeProtocols = new Set(['http:', 'https:', 'mailto:'])
const markdown = new MarkdownIt({
  breaks: false,
  html: false,
  linkify: false,
  typographer: false,
})
const defaultValidateLink = markdown.validateLink.bind(markdown)
const defaultLinkOpen =
  markdown.renderer.rules.link_open ??
  ((tokens, index, options, _environment, renderer) => renderer.renderToken(tokens, index, options))

markdown.validateLink = (url) => {
  if (!defaultValidateLink(url)) return false
  const protocol = /^[a-z][a-z\d+.-]*:/i.exec(url)?.[0].toLowerCase()
  return !protocol || safeProtocols.has(protocol)
}
markdown.renderer.rules.image = (tokens, index) =>
  markdown.utils.escapeHtml(tokens[index]?.content || '')
markdown.renderer.rules.link_open = (tokens, index, options, environment, renderer) => {
  const href = tokens[index]?.attrGet('href') || ''
  if (/^(?:https?:)?\/\//i.test(href)) {
    tokens[index]?.attrSet('target', '_blank')
    tokens[index]?.attrSet('rel', 'noopener noreferrer')
  }
  return defaultLinkOpen(tokens, index, options, environment, renderer)
}

export function renderSafeReportMarkdown(content: string, anchorPrefix?: string) {
  const environment = {}
  const tokens = markdown.parse(content, environment)
  const headings: ReportHeading[] = []
  if (anchorPrefix) {
    // 目录和正文共用本次解析的 token，代码围栏中的井号不会成为章节。
    tokens.forEach((token, index) => {
      if (token.type !== 'heading_open') return
      const inline = tokens[index + 1]
      const title = (inline?.children ?? [])
        .map((child) => {
          if (['text', 'code_inline', 'image'].includes(child.type)) return child.content
          return ['softbreak', 'hardbreak'].includes(child.type) ? ' ' : ''
        })
        .join('')
      const id = `${anchorPrefix}-section-${headings.length + 1}`
      token.attrSet('id', id)
      token.attrSet('tabindex', '-1')
      headings.push({ id, level: Number(token.tag.slice(1)), title })
    })
  }
  return { html: markdown.renderer.render(tokens, markdown.options, environment), headings }
}
