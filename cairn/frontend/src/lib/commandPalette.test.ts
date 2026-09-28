import { describe, it, assert } from 'vitest';
import { commandsVisibleToRole, commandItemSearchText, filterCommandItems, normalizeCommandQuery } from './commandPalette.js';
import type { CommandItem } from './commandPalette.js';

const items: CommandItem[] = [
  {
    id: 'nav:monitoring',
    kindLabel: '导航',
    title: '打开监控',
    subtitle: '查看调度和错误',
    badge: '监控',
    keywords: ['monitoring', 'dispatcher']
  },
  {
    id: 'fact:f1',
    kindLabel: 'Fact',
    title: '登录接口存在',
    subtitle: 'fact_001',
    badge: '事实',
    keywords: ['proj_001', 'evidence']
  },
  {
    id: 'run:tr1',
    kindLabel: 'Run',
    title: 'tr001 · 探索',
    subtitle: 'codex / running',
    badge: '运行中',
    keywords: ['task_run', 'proj_001']
  }
];

describe('command palette', () => {
  it('normalizeCommandQuery trims and lowercases search input', () => {
    assert.equal(normalizeCommandQuery('  Codex  '), 'codex');
  });

  it('commandItemSearchText includes visible labels and keywords', () => {
    const text = commandItemSearchText(items[0]);
    assert.ok(/打开监控/.test(text));
    assert.ok(/dispatcher/.test(text));
  });

  it('filterCommandItems matches all query terms across item text', () => {
    const result = filterCommandItems(items, 'proj run');
    assert.deepEqual(result.map(item => item.id), ['run:tr1']);
  });

  it('filterCommandItems ranks title matches before keyword-only matches', () => {
    const result = filterCommandItems(items, '监控');
    assert.deepEqual(result.map(item => item.id), ['nav:monitoring']);
  });

  it('filterCommandItems limits returned items', () => {
    const result = filterCommandItems(items, '', 2);
    assert.deepEqual(result.map(item => item.id), ['nav:monitoring', 'fact:f1']);
  });

  it('shows super-admin commands only to super-admin users', () => {
    const commands: CommandItem[] = [
      { id: 'go:projects', title: '任务管理' },
      { id: 'go:console:workers', title: '系统管理', superAdminOnly: true },
    ];

    assert.deepEqual(commandsVisibleToRole(commands, false).map(item => item.id), ['go:projects']);
    assert.deepEqual(commandsVisibleToRole(commands, true).map(item => item.id), ['go:projects', 'go:console:workers']);
  });
});
