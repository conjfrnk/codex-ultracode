# Mode Selection

Choose the lightest safe mode.

## Risk Levels

Low risk:

- small local code fixes,
- tests,
- docs,
- no secrets,
- no production effect.

Medium risk:

- multi-file refactors,
- public API behavior,
- dependencies,
- data handling,
- migrations in draft only.

High risk:

- auth,
- permissions,
- cryptography,
- payments,
- billing,
- production config,
- deployment,
- database writes,
- secrets,
- destructive operations,
- broad automated rewrites.

## Precedence

If a task matches criteria in more than one risk tier (for example, a multi-file refactor of a payments module matches both "multi-file refactors" and "payments"), use the highest matching tier.

## Selection Rules

Use Mode 0 for simple explanations, snippets, or a single obvious one-line edit (such as an isolated typo fix) with no behavior change.

Use Mode 1 if the task is high risk and approval is missing.

Use Mode 2 for small or medium implementation tasks that fit in one session. This is the default for ordinary coding work.

Use Mode 3 when the task needs resumable planning, multiple phases, or traceable decisions that materially reduce risk.

Use Mode 4 when the user asks for agents or when independent streams can reduce error enough to justify the extra token and review cost, and the delegation plan is disclosed.

Use Mode 5 only for many similar independent items and only after approval.

Use Mode 6 only when a Codex Cloud task run (`codex cloud exec` to submit, `codex apply` to review and merge the resulting diff) is available and useful. This is a one-way submit/review flow, not a live session handoff.

When the companion runtime is available, `auto` is the preferred front door for substantial Mode 3–5 work after model-call and execution capabilities are disclosed. Its serial default uses one direct worker and no separate planner. Supply a deterministic verifier whenever practical: automatic effort then uses `medium` for bounded low-complexity work, including short migrations and bounded test additions without another hard signal, while broad or sensitive work stays `high`; explicit `xhigh`/`ultra` remains available when deeper reasoning is justified. Omitted-cap direct workers scale with that effort: read-only uses 4,000/8,000/12,000/16,000/20,000 and writes use 8,000/16,000/24,000/32,000/40,000, so effort controls the hard spend ceiling without adding a call. A framework name or an instruction to leave production code unchanged is not a hard signal by itself. When verification is available and parallel work could materially help, set an explicit worker ceiling: progressive mode tries direct once and spends on planning/decomposition only after verifier failure. Use `--strategy goal` only when planning first is worth the cost, or `direct` when escalation is unwanted. It does not replace Mode 0/2, and task text cannot use it to select capabilities.

When uncertain, choose the safer lower mode and explain the tradeoff.

## Proportionality Gate

Before selecting Mode 3 or Mode 4, estimate the orchestration overhead in plain terms:

- artifact setup and upkeep,
- extra context gathering,
- subagent prompts and integration,
- extra verification or review passes,
- final reporting cost.

Do not choose Mode 3 or Mode 4 when that overhead is likely to exceed the implementation and verification work. Stay in Mode 2 for focused fixes, local refactors, docs changes, and test additions unless the user explicitly asks for durable artifacts or agents.

Use a token/time budget when the user asks for agents, exhaustive review, or parity work. If no budget is given, cap delegation to the smallest useful review set and stop after the first actionable pass.
