import type { ProjectDetail } from './useGraphModel';

export function buildGraphPlaybackEvents(projectDetail: ProjectDetail) {
  if (!projectDetail) return [];

  const facts = projectDetail.facts || [];
  const intents = projectDetail.intents || [];
  const events: any[] = [];

  facts.forEach(fact => {
    if (fact.id === 'origin' || fact.id === 'goal' || !fact.created_at) return;
    events.push({
      id: `fact:${fact.id}`,
      time: fact.created_at,
      kind: 'fact',
      subjectId: fact.id,
      label: `写入 Fact ${fact.id}`
    });
  });

  intents.forEach(intent => {
    if (intent.created_at) {
      events.push({
        id: `intent-created:${intent.id}`,
        time: intent.created_at,
        kind: 'intent_created',
        subjectId: intent.id,
        label: `提出 Intent ${intent.id}`
      });
    }
    if (intent.to && intent.concluded_at) {
      events.push({
        id: `intent-concluded:${intent.id}`,
        time: intent.concluded_at,
        kind: 'intent_concluded',
        subjectId: intent.id,
        label: `完成 Intent ${intent.id} → ${intent.to}`
      });
    }
  });

  return events
    .filter(event => Number.isFinite(toTime(event.time)))
    .sort((a, b) => {
      const diff = toTime(a.time) - toTime(b.time);
      return diff === 0 ? a.id.localeCompare(b.id) : diff;
    });
}

function toTime(value: any): number {
  if (!value) return Number.POSITIVE_INFINITY;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.POSITIVE_INFINITY;
}
