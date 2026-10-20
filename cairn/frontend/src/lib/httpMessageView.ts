export interface HttpMessageView {
  language: 'http' | 'json' | 'plaintext'
  pretty: string | null
  reason: string
  limitation: boolean
}

export function httpMessageSummary(raw: string) {
  const request = /^([!#$%&'*+.^_`|~\w-]+) (\S+) HTTP\/\d(?:\.\d)?(?:\r?\n|$)/.exec(raw)
  if (request) {
    const headers = raw.split(/\r?\n\r?\n/, 1)[0] || ''
    const host = /^host:\s*([^\r\n]+?)\s*$/im.exec(headers)?.[1] || ''
    const target = request[2]!
    return { marker: request[1]!, target: host && target.startsWith('/') ? host + target : target }
  }
  const response = /^HTTP\/\d(?:\.\d)? (\d{3})(?: [^\r\n]*)?(?:\r?\n|$)/.exec(raw)
  return { marker: response?.[1] || '', target: '' }
}

export function httpMessageView(raw: string, truncated = false): HttpMessageView {
  const isHttp = Boolean(httpMessageSummary(raw).marker)
  const separator = isHttp ? /\r\n\r\n|\n\n/.exec(raw) : null
  const prefix = separator ? raw.slice(0, separator.index + separator[0].length) : ''
  const body = isHttp ? separator ? raw.slice(prefix.length) : '' : raw
  const jsonBody = /^[\s]*[\[{]/.test(body)
  const language = isHttp ? 'http' : jsonBody ? 'json' : 'plaintext'
  const unavailable = (reason: string, limitation = false): HttpMessageView => ({ language, pretty: null, reason, limitation })
  if (truncated) return unavailable('已载入片段，不能格式化；请读取原件。', true)
  if (raw.includes('�')) return unavailable('文本含替换字符，编码无法确认；保留原文。', true)
  if (isHttp && !separator) return unavailable('没有完整的 HTTP 头部与正文分隔，保留原文。', true)
  const charsets = [...prefix.matchAll(/^content-type:[^\r\n]*?charset\s*=\s*["']?([^\s;"'\r\n]+)/gim)]
  if (charsets.some((match) => !['utf-8', 'utf8', 'us-ascii'].includes(match[1]!.toLowerCase()))) return unavailable('正文编码不适用于 JSON 格式化，保留原文。', true)
  if (/^content-encoding:\s*(?!identity\s*$)\S+/im.test(prefix) || /^content-type:\s*(?:application\/octet-stream|image\/|audio\/|video\/)/im.test(prefix)) return unavailable('编码或二进制正文不适用格式化，请读取原件。', true)
  if (!jsonBody) return unavailable('仅支持完整的 JSON 对象或数组正文；保留原文。')
  try {
    // 仅校验语法，不使用解析值重建正文：重复键、大整数与转义必须原样保留。
    JSON.parse(body)
  } catch {
    return unavailable('JSON 正文不完整或语法无效，保留原文。')
  }
  const tokens = body.match(/"(?:[^"\\]|\\.)*"|[^\s{}\[\],:]+|[{}\[\],:]/g) || []
  let depth = 0
  let formatted = ''
  const newline = () => { formatted += `\n${'  '.repeat(depth)}` }
  for (let index = 0; index < tokens.length; index++) {
    const token = tokens[index]!
    const previous = tokens[index - 1]
    if (token === '{' || token === '[') {
      formatted += token
      depth++
      if (depth > 64) return unavailable('JSON 嵌套过深，不展开格式化视图；保留原文。')
      if (tokens[index + 1] !== (token === '{' ? '}' : ']')) newline()
    } else if (token === '}' || token === ']') {
      depth--
      if (previous !== (token === '}' ? '{' : '[')) newline()
      formatted += token
    } else if (token === ',') {
      formatted += token
      newline()
    } else {
      formatted += token === ':' ? ': ' : token
    }
  }
  return { language, pretty: prefix + formatted, reason: '', limitation: false }
}
