export const meta = {
  name: 'claude-parallel-phase-smoke',
  description: 'Validate static Claude ' + 'parallel and phase compatibility',
  whenToUse: 'Use for a bounded ' + 'parallel review smoke test',
  phases: [{ title: 'Code Review', detail: 'Independent bounded review fan-out' }],
}

const TOPICS = [
  { key: 'correctness', prompt: 'Review correctness and edge cases.' },
  { key: 'security', prompt: 'Review trust boundaries and input handling.' },
]

const FINDING_SCHEMA = {
  type: 'object',
  properties: { finding: { type: 'string' } },
  required: ['finding'],
}

phase('Code Review')
const findings = (await parallel(
  TOPICS.map(topic => () => agent(
    topic.prompt + '\nReturn one concrete finding.',
    {
      label: 'review:' + topic.key,
      phase: 'Code Review',
      schema: FINDING_SCHEMA,
      effort: 'max',
    },
  )),
)).filter(Boolean)

return findings
