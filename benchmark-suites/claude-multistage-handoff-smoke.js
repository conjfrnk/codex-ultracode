export const meta = {
  name: 'claude-multistage-handoff-smoke',
  description: 'Validate bounded map-result handoff into a second review stage',
  whenToUse: 'Use for a two-pass structured audit and challenge workflow',
}

const FINDING_SCHEMA = {
  type: 'object',
  required: ['area', 'summary'],
  properties: {
    area: { type: 'string' },
    summary: { type: 'string' },
  },
}

const findings = (await pipeline(
  ['compiler', 'runtime'],
  area => agent(`Audit the ${area} boundary.`, {
    label: 'audit:' + area,
    schema: FINDING_SCHEMA,
    effort: 'max',
  }),
)).filter(Boolean)

const challenges = await pipeline(
  findings,
  finding => agent(`Challenge ${finding.area}: ${finding.summary}`, {
    label: 'challenge:' + finding.area,
    effort: 'max',
  }),
)

return challenges.filter(Boolean)
