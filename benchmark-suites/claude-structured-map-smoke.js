export const meta = {
  name: 'claude-structured-map-smoke',
  description: 'Validate guarded argument aliases and structured parallel fan-out',
  whenToUse: 'Use for bounded per-service analysis from structured arguments',
}

const system = args && args.system
const services = args && args.services

const reviews = (await parallel(
  services.map(service => () => agent(
    'Review ' + service.name + ' in ' + system + '. Responsibilities: ' +
      service.responsibilities,
    {
      label: 'review:' + service.name,
      effort: 'max',
    },
  )),
)).filter(Boolean)

return reviews
