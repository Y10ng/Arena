export interface HttpEvidenceSummary {
  startLine: string
  primary: string
  secondary: string
  lineCount: number
  byteCount: number
}

const byteCount = (value: string): number => new TextEncoder().encode(value).byteLength

const headerValue = (lines: string[], name: string): string => {
  const prefix = `${name.toLowerCase()}:`
  const line = lines.find(item => item.toLowerCase().startsWith(prefix))
  return line ? line.slice(line.indexOf(':') + 1).trim() : ''
}

export const summarizeHttpRequest = (raw: string): HttpEvidenceSummary => {
  const lines = raw.split(/\r?\n/)
  const startLine = lines[0]?.trim() || ''
  const match = startLine.match(/^([A-Z]+)\s+(\S+)\s+HTTP\/\d(?:\.\d)?$/i)
  return {
    startLine,
    primary: match?.[1]?.toUpperCase() || 'REQUEST',
    secondary: match?.[2] || headerValue(lines, 'Host'),
    lineCount: lines.length,
    byteCount: byteCount(raw),
  }
}

export const summarizeHttpResponse = (raw: string): HttpEvidenceSummary => {
  const lines = raw.split(/\r?\n/)
  const startLine = lines[0]?.trim() || ''
  const match = startLine.match(/^HTTP\/\d(?:\.\d)?\s+(\d{3})(?:\s+(.*))?$/i)
  const status = match?.[1] || ''
  const reason = match?.[2]?.trim() || ''
  return {
    startLine,
    primary: status ? `${status}${reason ? ` ${reason}` : ''}` : 'RESPONSE',
    secondary: headerValue(lines, 'Content-Type'),
    lineCount: lines.length,
    byteCount: byteCount(raw),
  }
}
