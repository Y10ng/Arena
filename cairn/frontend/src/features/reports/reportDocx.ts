import {
  AlignmentType,
  BorderStyle,
  Document,
  ExternalHyperlink,
  HeadingLevel,
  ImageRun,
  LevelFormat,
  Packer,
  Paragraph,
  Table,
  TableCell,
  TableRow,
  TextRun,
  WidthType,
  type IRunOptions,
} from 'docx'
import type Token from 'markdown-it/lib/token.mjs'
import { isStandaloneReportLink, parseReportMarkdown } from './safeReportMarkdown'

import type { ReportImageData } from './reportImages'

type Inline = TextRun | ExternalHyperlink | ImageRun
type DocxImages = Map<string, ImageRun | string>
interface ListContext {
  reference: string
  level: number
  marker: boolean
}

function inlineRuns(tokens: Token[], base: IRunOptions = {}, images: DocxImages = new Map()): Inline[] {
  const runs: Inline[] = []
  const style = { ...base }
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]!
    if (token.type === 'strong_open' || token.type === 'strong_close')
      style.bold = token.type === 'strong_open' || base.bold
    else if (token.type === 'em_open' || token.type === 'em_close')
      style.italics = token.type === 'em_open' || base.italics
    else if (token.type === 's_open' || token.type === 's_close')
      style.strike = token.type === 's_open' || base.strike
    else if (token.type === 'link_open') {
      const end = tokens.findIndex((item, offset) => offset > index && item.type === 'link_close')
      const href = token.attrGet('href') || ''
      const children = inlineRuns(tokens.slice(index + 1, end), style, images)
      if (isStandaloneReportLink(href)) {
        runs.push(new ExternalHyperlink({ link: href, children }))
      } else {
        runs.push(...children)
      }
      index = end
    } else if (token.type === 'hardbreak') {
      runs.push(new TextRun({ ...style, break: 1 }))
    } else if (token.type === 'softbreak') {
      // 与 HTML 中折叠的换行空白一致，不把编辑器软折行变成 Word 强制断行。
      runs.push(new TextRun({ ...style, text: ' ' }))
    } else if (token.type === 'image') {
      const image = images.get(token.attrGet('src') || '')
      runs.push(image instanceof ImageRun ? image : new TextRun({ ...style, text: `［图片：${token.content || '未提供说明'}；${image || '离线导出不加载图片'}］` }))
    } else if (token.type === 'code_inline') {
      runs.push(new TextRun({ ...style, text: token.content, font: 'Courier New' }))
    } else if (token.content) {
      runs.push(new TextRun({ ...style, text: token.content }))
    }
  }
  return runs
}

export async function createReportDocx(markdown: string, title: string, imageData: ReportImageData[] = []): Promise<Blob> {
  const images: DocxImages = new Map()
  for (const { image, blob, error } of imageData) {
    const key = `arena-image:${image.artifact_id}`
    images.set(key, error || '图片无法嵌入；正文与代码证据已保留。')
    if (!blob) continue
    try {
      const type = ({ 'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif' } as const)[blob.type as 'image/png' | 'image/jpeg' | 'image/gif']
      if (!type) continue
      const bitmap = await createImageBitmap(blob)
      const scale = Math.min(1, 600 / bitmap.width, 800 / bitmap.height)
      const transformation = { width: bitmap.width * scale, height: bitmap.height * scale }
      bitmap.close()
      images.set(key, new ImageRun({ type, data: new Uint8Array(await blob.arrayBuffer()), transformation, altText: { title: '证据截图', description: image.caption, name: image.artifact_id } }))
    } catch {
      // 可选展示图失败不影响已有文字、表格与原始代码块交付。
    }
  }
  // XML 1.0 无法表示这些字符；明确失败而非生成损坏文档或偷偷删改证据。
  const invalidXml = /[\u0000-\u0008\u000b\u000c\u000e-\u001f￾￿]/u
  if (invalidXml.test(markdown) || invalidXml.test(title)) {
    throw new Error('正文含 Word 无法表示的控制字符，请使用 Markdown 导出保留原文。')
  }
  const tokens = parseReportMarkdown(markdown)
  const children: (Paragraph | Table)[] = []
  const lists: ListContext[] = []
  const numbering: {
    reference: string
    levels: {
      level: number
      format: typeof LevelFormat.DECIMAL | typeof LevelFormat.BULLET
      text: string
      start?: number
      style: { paragraph: { indent: { left: number; hanging: number } } }
    }[]
  }[] = []
  let quoteDepth = 0
  const addParagraph = (
    runs: Inline[],
    heading?: (typeof HeadingLevel)[keyof typeof HeadingLevel],
    code = false,
  ) => {
    const list = lists.at(-1)
    children.push(
      new Paragraph({
        children: runs,
        heading,
        numbering: list?.marker ? { reference: list.reference, level: list.level } : undefined,
        indent:
          !list?.marker && (lists.length || quoteDepth)
            ? { left: (lists.length + quoteDepth) * 360 }
            : undefined,
        spacing: code ? { before: 180, after: 200, line: 280 } : { after: 160 },
        ...(code
          ? {
              shading: { fill: 'F6F8FA' },
              border: Object.fromEntries(
                ['top', 'bottom', 'left', 'right'].map((side) => [
                  side,
                  { style: BorderStyle.SINGLE, size: 4, color: 'D7DDE5', space: 6 },
                ]),
              ),
              // 报文是一个阅读块，但允许正文自然跨页，不把长包整体挤到下一页。
              keepNext: false,
              keepLines: false,
              widowControl: false,
            }
          : {}),
      }),
    )
    if (list) list.marker = false
  }
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]!
    if (token.type === 'bullet_list_open' || token.type === 'ordered_list_open') {
      if (lists.length > 8) throw new Error('列表层级超出 Word 支持范围，请使用 Markdown 导出。')
      const level = lists.length
      const reference = `report-list-${numbering.length}`
      const ordered = token.type === 'ordered_list_open'
      numbering.push({
        reference,
        levels: [
          {
            level,
            format: ordered ? LevelFormat.DECIMAL : LevelFormat.BULLET,
            text: ordered ? `%${level + 1}.` : '•',
            start: ordered ? Number(token.attrGet('start') || 1) : undefined,
            style: { paragraph: { indent: { left: (level + 1) * 720, hanging: 360 } } },
          },
        ],
      })
      lists.push({ reference, level, marker: false })
    } else if (token.type === 'bullet_list_close' || token.type === 'ordered_list_close') {
      lists.pop()
    } else if (token.type === 'list_item_open') {
      if (lists.at(-1)) lists.at(-1)!.marker = true
    } else if (token.type === 'blockquote_open') {
      quoteDepth += 1
    } else if (token.type === 'blockquote_close') {
      quoteDepth -= 1
    } else if (token.type === 'heading_open' || token.type === 'paragraph_open') {
      const inline = tokens[index + 1]
      const heading =
        token.type === 'heading_open'
          ? HeadingLevel[`HEADING_${token.tag.slice(1)}` as keyof typeof HeadingLevel]
          : undefined
      addParagraph(inlineRuns(inline?.children || [], {}, images), heading)
      index += 2
    } else if (token.type === 'fence' || token.type === 'code_block') {
      const lines = token.content.split(/\r\n|\r|\n/)
      // markdown-it 的围栏 token 保留结束换行，不把它误增成额外空白行。
      if (lines.at(-1) === '') lines.pop()
      addParagraph(
        lines.map(
          (line, lineIndex) =>
            new TextRun({
              text: line,
              break: lineIndex ? 1 : undefined,
              font: 'Courier New',
              size: 20,
            }),
        ),
        undefined,
        true,
      )
    } else if (token.type === 'hr') {
      addParagraph([new TextRun('────────────────')])
    } else if (token.type === 'table_open') {
      const rows: TableRow[] = []
      let cells: TableCell[] = []
      let header = false
      while (++index < tokens.length && tokens[index]!.type !== 'table_close') {
        const cellToken = tokens[index]!
        if (cellToken.type === 'tr_open') cells = []
        if (cellToken.type === 'thead_open') header = true
        if (cellToken.type === 'thead_close') header = false
        if (cellToken.type === 'th_open' || cellToken.type === 'td_open') {
          const alignment = cellToken.attrGet('style')
          cells.push(
            new TableCell({
              children: [
                new Paragraph({
                  children: inlineRuns(tokens[index + 1]?.children || [], { bold: header }, images),
                  alignment: alignment?.includes('center')
                    ? AlignmentType.CENTER
                    : alignment?.includes('right')
                      ? AlignmentType.RIGHT
                      : AlignmentType.LEFT,
                }),
              ],
            }),
          )
        }
        if (cellToken.type === 'tr_close')
          rows.push(new TableRow({ children: cells, tableHeader: header }))
      }
      children.push(new Table({ rows, width: { size: 100, type: WidthType.PERCENTAGE } }))
    } else if (token.type === 'inline') {
      addParagraph(inlineRuns(token.children || [], {}, images))
    }
  }
  const document = new Document({
    title,
    creator: 'Arena',
    styles: {
      default: {
        document: { run: { font: 'Arial', size: 22 }, paragraph: { spacing: { line: 320 } } },
      },
    },
    numbering: { config: numbering },
    sections: [{ children: children.length ? children : [new Paragraph('')] }],
  })
  const bytes = await Packer.toArrayBuffer(document)
  return new Blob([bytes], {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  })
}
