import EditorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import CssWorker from 'monaco-editor/esm/vs/language/css/css.worker?worker'
import HtmlWorker from 'monaco-editor/esm/vs/language/html/html.worker?worker'
import JsonWorker from 'monaco-editor/esm/vs/language/json/json.worker?worker'
import TsWorker from 'monaco-editor/esm/vs/language/typescript/ts.worker?worker'

type MonacoWorkerFactory = new () => Worker

const workerByLabel: Record<string, MonacoWorkerFactory> = {
  css: CssWorker,
  handlebars: HtmlWorker,
  html: HtmlWorker,
  javascript: TsWorker,
  json: JsonWorker,
  less: CssWorker,
  scss: CssWorker,
  typescript: TsWorker,
}

;(globalThis as {
  MonacoEnvironment?: {
    getWorker: (_moduleId: string, label: string) => Worker
  }
}).MonacoEnvironment = {
  getWorker: (_moduleId: string, label: string) => {
    const WorkerFactory = workerByLabel[label] || EditorWorker
    return new WorkerFactory()
  },
}
