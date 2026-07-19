# Agent Performance

An installable set of focused Codex skills for coding quality and controlled
agent evaluation. The plugin supplies methods; the host remains responsible
for permissions, execution, delegation, and external side effects.

## Skill map

| Need | Skill |
| --- | --- |
| Hold a complex change to one behavior and ownership model | `change-contract` |
| Establish a causal diagnosis before repair | `debug-from-evidence` |
| Design tests around observable behavior | `write-behavioral-tests` |
| Review a change for consequential defects | `review-code-changes` |
| Remove unjustified complexity without contract drift | `simplify-without-drift` |
| Reconcile completion claims with fresh proof | `verify-before-claiming` |
| Run a matched, blinded, claim-gated comparison | `benchmark-agent-configs` |

The skills are intentionally composable and non-executive. They do not create
another planner, worker hierarchy, approval system, or always-on workflow.
Near misses should remain lightweight: running existing tests does not invoke
test design, a plain architecture explanation is not a code review, and static
feature inspection cannot establish a performance winner.

## Startup identity

For a visible identity before the welcome panel, source `scripts/codex-ultracode.zsh`
from `~/.zshrc`. A bare interactive `codex` launch uses the same availability
detector as the plugin hook. With Codex Conductor enabled, it announces:

> Codex with Ultracode — Conductor orchestration + specialist agent workflows ready.

Without Conductor, it reports a specialist-only installation instead. The
launcher leaves argument-bearing Codex commands unchanged. The plugin's
`SessionStart` command hook is a fallback when the launcher is not used. Codex
defers this hook until the first prompt begins, so it cannot alter the idle
welcome panel. Use `/hooks` to inspect and trust it. The fallback runs only for a
new thread, not resume, clear, or compaction events, and suppresses itself when
the launcher already announced.

## Evaluation and validation

The repository source checkout keeps its evaluator-side routing suite outside
this installable plugin so private expectations are never shipped to a
candidate. It contains direct, restraint, composition, and adversarial prompts
for every skill. From the repository root, validate it with:

```sh
python3 benchmark-suites/agent-performance-skill-routing/validate_skill_evals.py
```

Validate the plugin's public benchmark packet templates from this directory:

```sh
python3 skills/benchmark-agent-configs/scripts/validate_packet.py \
  --templates skills/benchmark-agent-configs/assets
```

These commands prove record mechanics and coverage, not model behavior. A
comparative claim additionally requires matched candidate runs and the claim
gate described by `benchmark-agent-configs`.

## License and provenance

MIT. See [`LICENSE`](LICENSE) and [`ATTRIBUTIONS.md`](ATTRIBUTIONS.md).
