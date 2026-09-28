export interface CommandItem {
  id?: string;
  kind?: string;
  kindLabel?: string;
  title?: string;
  subtitle?: string;
  badge?: string;
  keywords?: string[];
  superAdminOnly?: boolean;
  action?: () => void;
  url?: string;
}

export function commandsVisibleToRole(items: CommandItem[], isSuperAdmin: boolean): CommandItem[] {
  return isSuperAdmin ? items : items.filter((item) => !item.superAdminOnly);
}

export function normalizeCommandQuery(value: any): string {
  return String(value || '').trim().toLowerCase();
}

export function commandItemSearchText(item: CommandItem = {}): string {
  return [
    item.kindLabel,
    item.title,
    item.subtitle,
    item.badge,
    ...(Array.isArray(item.keywords) ? item.keywords : []),
  ].filter(Boolean).join(' ');
}

export function filterCommandItems(items: CommandItem[] = [], query = '', limit = 30): CommandItem[] {
  const normalizedQuery = normalizeCommandQuery(query);
  if (!normalizedQuery) return items.slice(0, limit);

  const terms = normalizedQuery.split(/\s+/).filter(Boolean);
  return items
    .map((item, index) => ({ item, index, score: scoreCommandItem(item, terms) }))
    .filter(({ score }) => score >= 0)
    .sort((left, right) => right.score - left.score || left.index - right.index)
    .slice(0, limit)
    .map(({ item }) => item);
}

function scoreCommandItem(item: CommandItem, terms: string[]): number {
  const title = normalizeCommandQuery(item.title);
  const subtitle = normalizeCommandQuery(item.subtitle);
  const badge = normalizeCommandQuery(item.badge);
  const keywords = normalizeCommandQuery(Array.isArray(item.keywords) ? item.keywords.join(' ') : '');
  const allText = normalizeCommandQuery(commandItemSearchText(item));

  if (!terms.every((term) => allText.includes(term))) return -1;

  return terms.reduce((score, term) => {
    if (title.includes(term)) return score + 8;
    if (subtitle.includes(term)) return score + 4;
    if (keywords.includes(term)) return score + 3;
    if (badge.includes(term)) return score + 1;
    return score;
  }, 0);
}
