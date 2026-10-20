import * as monaco from 'monaco-editor/esm/vs/editor/editor.api.js'

monaco.languages.register({ id: 'http' })
monaco.languages.setMonarchTokensProvider('http', {
  tokenizer: {
    root: [
      [/^([!#$%&'*+.^_`|~\w-]+)( )(\S+)( )(HTTP\/\d(?:\.\d)?)/, ['keyword', '', 'string', '', { token: 'type', next: '@headers' }]],
      [/^(HTTP\/\d(?:\.\d)?)( )(\d{3})(.*)$/, ['type', '', 'number', { token: '', next: '@headers' }]],
    ],
    headers: [
      [/^(Content-Type)(:)(.*\bjson\b.*)$/i, ['keyword', 'delimiter', { token: 'string', next: '@jsonHeaders' }]],
      [/^([^\s:]+)(:)(.*)$/, ['keyword', 'delimiter', '']],
      [/^$/, { token: '', next: '@body' }],
    ],
    jsonHeaders: [
      [/^([^\s:]+)(:)(.*)$/, ['keyword', 'delimiter', '']],
      [/^$/, { token: '', next: '@jsonBody' }],
    ],
    body: [
      [/^\s*[\[{]/, { token: 'delimiter.bracket', next: '@jsonBody' }],
      [/.+/, ''],
    ],
    jsonBody: [
      [/"(?:[^"\\]|\\.)*"(?=\s*:)/, 'keyword'],
      [/"(?:[^"\\]|\\.)*"/, 'string'],
      [/-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/, 'number'],
      [/\b(?:true|false|null)\b/, 'keyword'],
      [/[{}\[\],:]/, 'delimiter'],
    ],
  },
})
