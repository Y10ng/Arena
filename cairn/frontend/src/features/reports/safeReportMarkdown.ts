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
  return !protocol || safeProtocols.has(protocol) || /^arena-image:[a-zA-Z0-9_-]+$/.test(url)
}
markdown.renderer.rules.image = (tokens, index, _options, environment) => {
  const description = tokens[index]?.content || ''
  const source = tokens[index]?.attrGet('src') || ''
  const image = (environment.images as Map<string, { url?: string; error?: string }> | undefined)?.get(source)
  if (image?.url && /^(?:blob:|data:image\/(?:png|jpeg|gif|webp);base64,)/.test(image.url)) {
    return `<img src="${markdown.utils.escapeHtml(image.url)}" alt="${markdown.utils.escapeHtml(description)}" loading="lazy">`
  }
  if (source.startsWith('arena-image:')) return markdown.utils.escapeHtml(`［图片：${description || '证据截图'}；${image?.error || (environment.standalone ? '图片不可用或未获授权；文字与代码证据已保留' : '图片未加载')}］`)
  return markdown.utils.escapeHtml(
    environment.standalone
      ? `［图片：${description || '未提供说明'}；离线导出不加载图片］`
      : description,
  )
}
markdown.renderer.rules.link_open = (tokens, index, options, environment, renderer) => {
  const href = tokens[index]?.attrGet('href') || ''
  if (href.startsWith('arena-image:')) tokens[index]?.attrSet('href', '#')
  if (/^(?:https?:)?\/\//i.test(href)) {
    tokens[index]?.attrSet('target', '_blank')
    tokens[index]?.attrSet('rel', 'noopener noreferrer')
  }
  return defaultLinkOpen(tokens, index, options, environment, renderer)
}

export const isStandaloneReportLink = (href: string) => /^(?:https?:|mailto:)/i.test(href)

export function parseReportMarkdown(content: string) {
  return markdown.parse(content, {})
}

export type ReportImageSources = Map<string, { url?: string; error?: string }>

export function renderStandaloneReportMarkdown(content: string, images?: ReportImageSources) {
  const tokens = parseReportMarkdown(content)
  for (const token of tokens) {
    let keepLink = true
    for (const child of token.children ?? []) {
      if (child.type === 'link_open') keepLink = isStandaloneReportLink(child.attrGet('href') || '')
      if (!keepLink && ['link_open', 'link_close'].includes(child.type)) {
        // 离线交付不把应用内相对地址解释为本机文件路径，正文标签仍完整保留。
        child.type = 'text'
        child.content = ''
      }
    }
  }
  return markdown.renderer.render(tokens, markdown.options, { standalone: true, images })
}

export function renderSafeReportMarkdown(content: string, anchorPrefix?: string, images?: ReportImageSources) {
  const environment = { images }
  const tokens = parseReportMarkdown(content)
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
