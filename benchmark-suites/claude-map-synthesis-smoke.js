export const meta = {
  name: 'claude-map-synthesis-smoke',
  description: 'Collect structured audits and synthesize one ranked report',
  whenToUse: 'Use when many independent findings need one bounded final synthesis',
  phases: [
    { title: 'Audit', detail: 'Inspect independent boundaries' },
    { title: 'Synthesize', detail: 'Rank and deduplicate findings' },
  ],
}

const FINDING_SCHEMA = {
  type: 'object',
  required: ['area', 'summary', 'severity'],
  properties: {
    area: { type: 'string' },
    summary: { type: 'string' },
    severity: { type: 'string' },
  },
}

phase('Audit')
const findings = (await pipeline(
  ['compiler', 'runtime', 'recovery'],
  area => agent(`Audit the ${area} boundary for one concrete defect.`, {
    label: 'audit:' + area,
    schema: FINDING_SCHEMA,
    effort: 'max',
  }),
)).filter(Boolean)

phase('Synthesize')
const report = await agent(
  `Rank, deduplicate, and reconcile these completed findings: ${findings.join('\n---\n')}`,
  {
    schema: {
      type: 'object',
      required: ['summary', 'priorities'],
      properties: {
        summary: { type: 'string' },
        priorities: { type: 'array', items: { type: 'string' } },
      },
    },
    effort: 'max',
  },
)

return report
