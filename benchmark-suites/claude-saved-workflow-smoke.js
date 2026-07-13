export const meta = {
  name: 'claude-saved-workflow-smoke',
  description: 'Validate static Claude agent and pipeline compatibility',
}

const found = await agent('List every Python file under conductor_runtime.', {
  schema: {
    type: 'object',
    required: ['files'],
    properties: { files: { type: 'array', items: { type: 'string' } } },
  },
})

const reviews = await pipeline(found.files, file =>
  agent(`Review ${file} for one concrete correctness issue.`, { label: file }),
)

return reviews.filter(Boolean)
