import { fetchText } from '@/api/client'
import { renderStandaloneReportMarkdown, type ReportImageSources } from './safeReportMarkdown'
import { blobDataUrl, rewriteReportImages, withDisplayImages, type ReportImageData } from './reportImages'

export type ReportExportFormat = 'md' | 'docx' | 'html' | 'tar'
export const reportExportFormats = [
  { value: 'md', label: 'Markdown（.md）' },
  { value: 'tar', label: 'Markdown 证据包（.tar）' },
  { value: 'docx', label: 'Word（.docx）' },
  { value: 'html', label: 'HTML（.html）' },
] as const

export function fetchReportMarkdown(url: string, signal?: AbortSignal) {
  return fetchText(url, signal, 'no-store')
}

export function vulnerabilityReportUrl(
  projectId: string,
  vulnerabilityId: string,
  observationId?: string,
) {
  const query = new URLSearchParams()
  if (observationId) query.set('observation_id', observationId)
  return `/projects/${encodeURIComponent(projectId)}/vulnerabilities/${encodeURIComponent(vulnerabilityId)}/report?${query}`
}

const escapeHtml = (text: string) =>
  text.replace(
    /[&<>"']/g,
    (character) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]!,
  )

export function standaloneReportHtml(markdown: string, title: string, images?: ReportImageSources) {
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>${escapeHtml(title)}</title>
<style>
img{display:block;max-width:100%;height:auto}:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#fff;color:#18202b;font:15px/1.75 system-ui,sans-serif}main{max-width:960px;margin:auto;padding:40px 32px;overflow-wrap:anywhere}h1,h2,h3,h4,h5,h6{line-height:1.4;margin:1.5em 0 .6em}h1{font-size:28px}h2{font-size:22px}h3{font-size:18px}p,ul,ol,blockquote,pre,table{margin:0 0 1em}a{color:#245e9b}blockquote{border-left:3px solid #b9c3cf;padding-left:16px;margin-left:0;color:#445366}pre{padding:16px;background:#f4f6f8;border:1px solid #dce1e7;white-space:pre-wrap;overflow-wrap:anywhere}code{font-family:ui-monospace,monospace;font-size:.9em}table{border-collapse:collapse;width:100%}th,td{border:1px solid #cdd4dd;padding:8px;text-align:left;vertical-align:top}th{background:#f4f6f8}hr{border:0;border-top:1px solid #cdd4dd}@media print{main{max-width:none;padding:0}pre,table{break-inside:auto}h1,h2,h3,h4{break-after:avoid}a{color:inherit}}
</style>
</head>
<body><main>${renderStandaloneReportMarkdown(markdown, images)}</main></body>
</html>`
}

export async function createReportExport(
  markdown: string,
  format: ReportExportFormat,
  title: string,
  imageData: ReportImageData[] = [],
): Promise<Blob> {
  markdown = withDisplayImages(markdown, imageData.map((entry) => entry.image))
  const missing = (id: string, caption: string) => `［图片：${caption || '证据截图'}；${imageData.find((entry) => entry.image.artifact_id === id)?.error || (format === 'md' ? '未附带图片，请使用 Markdown 证据包' : '图片不可用或未获授权；文字与代码证据已保留')}］`
  if (format === 'md') return new Blob([rewriteReportImages(markdown, missing)], { type: 'text/markdown;charset=utf-8' })
  if (format === 'tar') return createMarkdownEvidencePackage(markdown, imageData, missing)
  if (format === 'html') {
    const sources: ReportImageSources = new Map()
    for (const { image, blob, error } of imageData) {
      try { sources.set(`arena-image:${image.artifact_id}`, { url: blob ? await blobDataUrl(blob) : undefined, error }) }
      catch { sources.set(`arena-image:${image.artifact_id}`, { error: '图片无法嵌入；正文与代码证据已保留。' }) }
    }
    return new Blob([standaloneReportHtml(markdown, title, sources)], { type: 'text/html;charset=utf-8' })
  }
  // Word 转换及 docx 仅在选择该格式时加载，不进入普通报告阅读的首包。
  const { createReportDocx } = await import('./reportDocx')
  return createReportDocx(markdown, title, imageData)
}

async function createMarkdownEvidencePackage(markdown: string, images: ReportImageData[], missing: (id: string, caption: string) => string): Promise<Blob> {
  const files: { path: string; data: Blob }[] = []
  const paths = new Map<string, string>()
  for (const { image, blob } of images) {
    if (!blob) continue
    const extension = ({ 'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp' } as Record<string, string>)[blob.type]
    if (!extension) continue
    const path = `images/evidence-${files.length + 1}.${extension}`
    paths.set(image.artifact_id, path)
    files.push({ path, data: blob })
  }
  files.unshift({ path: 'report.md', data: new Blob([rewriteReportImages(markdown, (id, caption) => paths.has(id) ? `![${caption}](${paths.get(id)})` : missing(id, caption))]) })
  // 标准 ustar 仅存储程序生成的相对路径，无需引入归档依赖。
  const parts: BlobPart[] = []
  const encoder = new TextEncoder()
  for (const file of files) {
    const header = new Uint8Array(512)
    const write = (offset: number, value: string) => header.set(encoder.encode(value), offset)
    const octal = (value: number, length: number) => value.toString(8).padStart(length - 1, '0') + '\0'
    write(0, file.path); write(100, octal(0o644, 8)); write(108, octal(0, 8)); write(116, octal(0, 8))
    write(124, octal(file.data.size, 12)); write(136, octal(0, 12)); write(148, '        ')
    write(156, '0'); write(257, 'ustar\0'); write(263, '00')
    write(148, header.reduce((sum, byte) => sum + byte, 0).toString(8).padStart(6, '0') + '\0 ')
    parts.push(header, file.data, new Uint8Array((512 - file.data.size % 512) % 512))
  }
  parts.push(new Uint8Array(1024))
  return new Blob(parts, { type: 'application/x-tar' })
}

export function downloadReportBlob(blob: Blob, filename: string, format: ReportExportFormat) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `${filename.replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_') || '报告'}.${format}`
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
