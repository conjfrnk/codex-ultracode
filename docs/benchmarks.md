# Benchmarks

Conductor includes a small benchmark harness so parity work can be measured instead of asserted.

Run the local smoke suite:

```bash
CONDUCTOR_STATE="$(python3 -c 'from conductor_runtime.paths import workspace_state_dir; print(workspace_state_dir())')"
python3 -m conductor_runtime benchmark benchmark-suites/conductor-smoke.json --allow-writes --output "$CONDUCTOR_STATE/benchmarks/conductor-smoke-report.json"
```

The report records task pass/fail, return code, timeout state, duration in milliseconds, redacted stdout/stderr, and truncation flags.

Run the read-only parity evidence smoke suite when you want to validate the benchmark/provenance fixtures without executing repository tests or paid live tools:

```bash
python3 -m conductor_runtime benchmark benchmark-suites/parity-matrix-smoke.json --system conductor
```

`benchmark-suites/parity-matrix-smoke.json` currently runs 191 fixture checks. It covers the parity matrix and five-shape implementation-canary task contracts, current and frozen effort-bound plus legacy campaign contracts, matched and product-profile read-only fixtures/plans, automatic-topology planning, reusable-agent profile/Skill/memory schemas and inspection, deterministic hybrid-memory profile selection, hash-bound same-session profile compaction, static Claude-compatible saved-workflow compilation including guarded argument aliases, structured object fan-out, and bounded multi-stage map-result handoff, structured-output JSON-pointer maps, unauthenticated, environment-authenticated, and sandboxed Skill-local stdio MCP profile inspection, coordinated-team current/legacy evidence, lifecycle/native-tool gates, terminal and merge recovery, routines/services, provider evidence, staged/staged-repair exit and inline-apply interfaces, staged repair, scoring, comparisons, and the remaining public schema/template surfaces. Integrated concurrency, privacy, tamper, recovery, secret-redaction, dynamic scheduling, service, transaction, and native-tool adversarial behavior is exercised separately by provider-free runtime tests. Passing means the local evidence harness is internally consistent. It does not mean Conductor has beaten or matched Claude Code Ultracode.

The runtime test suite separately exercises `balanced-v2` map packetization without paid provider calls. One synthetic case resolves 1,000 validated source items into 25 packets of 40 scopes, preserves a 4,000-token per-call configuration, and records 25 invocations. Adversarial cases cover impossible dynamic grouping before launch, changed member fingerprints, grouped cache invalidation, and targeted packet retry. This proves scheduler and evidence behavior only; it does not measure grouped review quality, provider latency, or cost.

`benchmark-suites/auto-efficiency-tasks.json` is a focused read-only audit fixture for automatic orchestration quality per token. A 2026-07-09 approved run was forced to Sonnet with Ultracode effort and observed `claude-sonnet-5` plus a Haiku helper, with no Opus. Claude Code stopped at its budget gate after `$0.4349514` against a `$0.20` request before producing audit findings. The valid report is failed budget-safety evidence only; it was not retried and provides no comparative quality result. The implemented direct route is therefore supported by local call-graph inspection and deterministic call-count tests, not by that paid audit.

Deterministic progressive-route tests cover both outcomes: a passing first attempt uses one worker and zero planner calls, while a failed first verification causes exactly one planner call, forwards bounded feedback, and executes the escalated graph. Resume tests prove the completed direct attempt is not replayed; detached tests prove the same policy survives the anonymous worker handoff. The matched topology campaign below automates repeated direct/progressive/plan-first execution and aggregation. Its first exact-release cohort is now complete, but one of 15 cohorts is descriptive evidence rather than a routing recommendation.

Preregister matched automatic-topology cohorts:

```bash
python3 -m conductor_runtime write-auto-topology-campaign \
  benchmark-suites/auto-topology-tasks.json \
  "$CONDUCTOR_STATE/benchmarks/auto-topology-campaign.json" \
  --run-id auto-topology-campaign \
  --seed 20260709 \
  --repetitions 3
python3 -m conductor_runtime run-auto-topology-arm \
  "$CONDUCTOR_STATE/benchmarks/auto-topology-campaign.json" \
  benchmark-suites/auto-topology-tasks.json \
  --next --dry-run
python3 -m conductor_runtime status-auto-topology-campaign \
  "$CONDUCTOR_STATE/benchmarks/auto-topology-campaign.json"
```

The current v3 plan contains 15 matched cohorts and 45 isolated arms across five implementation shapes, binds one exact runtime release plus the score-free prompt/success-criteria/evidence contract for each task, and balances first-arm assignment at five direct, five progressive, and five plan-first rows. A live arm reuses the normal `auto` goal runtime but first proves the exact release and execution contract, then adds a durable campaign launch receipt, source-isolated workspace, captured planner receipts, independent hidden evaluation, and release-bound hash-linked result. It requires `auto-topology-campaign-arm`, `model-workflow-execute`, `model-workflow-write`, agent/write capability, and topology-matched parallel capability; it forbids network and destructive access. Frozen v2 and v1 plans remain inspectable but cannot add arms. Status reports release binding/uniformity, per-topology score/success/calls/tokens/latency/cost, matched pairwise deltas, escalation and first-pass rates, and rows where direct-first matched plan-first quality with lower observed tokens. Missing telemetry is incomparable. A recommendation remains false until every arm and repeated pair is complete with full token and cost coverage.

Exact-build campaign `auto-topology-v0127-exact-product` binds runtime `0.127.0` and SHA-256 `64843b8af580e54d1079c094f9c7b22ff035b5db88b207f398ea56eda4f937e9`; its campaign SHA-256 is `0dc38489aa3f79cb028ca67a5da79b7688548f678c948dee1938641121041334`. The first retry-policy cohort ran once in stored progressive, direct, plan-first order without provider retry. Progressive and direct each changed only `policy.py` plus `service.py`, passed four visible and seven held-out tests, and scored 10/10 in one worker call with zero planner/verifier calls. Progressive took 103.104 seconds and 71,390 provider-native gross tokens; direct took 115.811 seconds and 73,309. Plan-first spent one planner call for 82.856 seconds, launched no worker, retained unchanged source, and scored 0/10 with `ModelPlannerProviderError`; token and dollar telemetry were unavailable. Structural audit of the terminal planner output found strict JSON with two deterministic contract violations: boolean shell captures and a model-authored Python test-runner command. Release 0.128 advanced the architect prompt to v6 with exact relative-string capture and no-interpreter/test-runner shell rules; that immutable arm remains 0/10. Status reports one completed cohort, three completed and zero invalid arms, exact-release uniformity, and 42 pending arms. Progressive matched direct quality with slightly lower measured time/tokens in this sample, but one cohort and missing plan-first resources support no topology recommendation.

The first exact-build `0.128.0` v6 arm then ran once without retry. Plan-first quota-ledger used one planner call for 40.750 seconds, no worker or verifier, unchanged source, and 0/10. A private structural trace audit found five inspection calls: two targeted the workspace, while three searched installed Conductor Skill/docs/examples to infer fields absent from the prompt's compact contract. The terminal weighted usage was 20,792 against the unchanged 20,000 ultra cap and no workflow JSON was emitted. Release 0.129 advanced the prompt to v7, generated exact field lists from the validator sets, prohibited those external lookups, and capped pre-JSON workspace inspection at two combined calls. It did not raise caps, add retries, relax validation, or rewrite either failed arm.

Exact-build campaign `auto-topology-v0129-v7-product` binds runtime `0.129.0`, runtime SHA-256 `b1b4d55cf8498abab5844a40cd98e58226f15aa2ae863cff830852047c4e18d3`, and campaign SHA-256 `2947e8bd8b4c48bb42eccfb506ced2708311128917799eb5e30dd24b16b5039c`. Its first plan-first backoff-test-authoring arm ran once after a zero-launch dry run and scored 0/10 in 221.363 seconds with one planning call and zero workers/verifiers. V7 did eliminate external lookup: both inspection calls targeted the workspace, and the final 11,434-character message was strict JSON with exact top-level fields. The call still reached 24,285 weighted tokens, declared profiles as two strings, and expanded one bounded change into six steps with five sequential Codex roles around an enclosing trusted verifier. Release 0.130 advances to v8: exact profile fields come from the validator, adaptive plans omit redundant non-implementation roles, prefer one worker or team, and reserve at most 4,000 weighted tokens for post-inspection reasoning plus JSON. The 20,000 cap is unchanged, the v7 arm remains immutable, and post-v8 recovery remains unclaimed pending a fresh exact-build arm.

Exact-build campaign `auto-topology-v0130-v8-final` binds runtime `0.130.0`, runtime SHA-256 `4489c002c962acf2554f9f9176a9076373fc6db67da62367cde8b5e1306fd4cb`, and campaign SHA-256 `95a7b23561c034731a65ccb5c2c8f2984de77c23eb32585c037b4d463e89b2a1`. Its first direct retry-policy arm ran once and scored 10/10 in one worker call, 82.849 seconds, and 69,567 gross tokens. The stored plan-first companion then ran once and scored 2/10 in 220.481 seconds after three planner and three worker calls. V8 produced a valid manual-gate-plus-write-worker graph on every iteration, with no redundant model roles. All three worker streams nevertheless reached their authored 18/18/22 KiB capture limits before terminal telemetry; private structural traces show completed turns at 8,994/11,677/12,384 weighted tokens against authored 7,000/9,000/10,000 ceilings. The v2 runner also forwarded only the prompt and omitted the public no-test-change acceptance criterion, so the worker changed a test file. Release 0.131 advances to model prompt v9 and campaign/result v3: enclosing-verifier write workers receive the established 24,000-token ceiling, model-authored provider captures have a 256 KiB floor, and every arm receives one hash-bound prompt/criteria/evidence contract. The immutable 2/10 score is unchanged, public token telemetry remains unavailable, and 43 pending arms preclude a topology recommendation.

Exact-build v3 campaign `auto-topology-v0131-v9-final` binds runtime `0.131.0`, runtime SHA-256 `1ee882f8a353abebf0ecc39edf6f2c3307c4c8be6f845d0d71d25c52db9a5cfc`, and campaign SHA-256 `0ea1dba995ec62ee9f6fe5cc4f94489db0ed1e8b363a975a14a15983421c5bb1`. Its first matched backoff-test-authoring pair ran once each after zero-launch dry runs. Progressive direct-first scored 10/10 in one worker call, 121.377 seconds, and 73,174 measured tokens; it added only the requested test module, passed 16 visible tests, and killed all five hidden mutants. Plan-first spent one architect call for 11.251 seconds, launched no worker, retained unchanged source, and scored 0/10 with `ModelPlannerProviderError`. Structural trace audit found exactly two allowed inspection calls, no assistant workflow message, and 20,283 cumulative gross tokens against the 20,000 hard planner ceiling. Repeated uncached context consumed the final-JSON reserve. Release 0.132 advances to prompt v10 and prohibits architect workspace inspection only when an enclosing verifier exists; the implementation worker still inspects normally, ordinary explicit planning retains two calls, and no cap is raised. The failed arm is not retried or rewritten, 43 arms remain pending, and no recommendation is supported.

Exact-build campaign `auto-topology-v0132-v10-final` binds runtime `0.132.0`, runtime SHA-256 `67472ddd4022e7eec3b54fddd79df3f840a3e0d8f5047e746c23c24ef8c5b38e`, and campaign SHA-256 `d24375bd5fe8613fffacfd33ad7a9ef91037d029f076a165c0c8ad25e7fbc47b`. Its first stored plan-first retry-policy arm ran once after a zero-launch preflight. Two zero-inspection architect turns emitted valid manual-gate-plus-one-worker graphs and launched their workers, recovering the v10 architecture target. Both workers exhausted the unchanged 24,000-token ceiling; the second changed only `policy.py` and `service.py` and passed all four visible tests, so the enclosing goal completed. Independent evaluation found three failures for invalid `completed_attempts`, scored 5/10, and retained the failed quality outcome. The arm took 141.023 seconds across two planner and two worker calls; public token and dollar telemetry were unavailable. It also proved the campaign's `ultra` declaration was not applied to model-authored workers, which ran at `high`, and the scored invalid-input behavior was absent from the public fixture. Release 0.133 pins those workers to the selected model/effort, advances to v11 acceptance-preserving prompts, and documents the scored public contract. The immutable arm is not retried or rescored; 44 arms remain pending and no recommendation is supported.

Exact-build campaign `auto-topology-v0133-v11-final` binds runtime `0.133.0`, runtime SHA-256 `d3b1638050e02c8d9a0495a113d96386369cc230c6c47877c248a0348bacf1cc`, and campaign SHA-256 `7adf8694c558de885e4040c548c191e6d2aee3d1c2106260d72099bce61586c4`. Its first plan-first event-routing arm ran once after zero-launch preflight. All three architect turns emitted two-step graphs and every worker was live-bound to `gpt-5.6-sol` ultra at 24,000 tokens. Every worker exhausted that ceiling; the third produced a three-module repair that passed all four visible tests but omitted the documented list-container check in `dedupe_subscriptions`, so independent evaluation scored 5/10 after 233.415 seconds and six calls. Structural traces showed each worker reading the installed Conductor Skill plus references before the fixture, and the final worker duplicated the enclosing `unittest` check. Release 0.134 advances to v12: the worker call is explicitly complete, external orchestration lookup and duplicate execution checks are forbidden, and a deterministic first-write/static-contract/finalization handoff follows the authored prompt. The immutable arm remains unchanged, 44 arms remain pending, telemetry is incomplete, and no recommendation is supported.

Generate an instruction-only live parity runbook when an operator is preparing comparable Conductor and Claude/Ultracode runs:

```bash
python3 -m conductor_runtime plan-live-parity-runbook benchmark-suites/parity-tasks.json
python3 -m conductor_runtime write-live-parity-runbook benchmark-suites/parity-tasks.json "$CONDUCTOR_STATE/benchmarks/live-parity-runbook.json"
python3 -m conductor_runtime validate-live-parity-runbook "$CONDUCTOR_STATE/benchmarks/live-parity-runbook.json"
```

The runbook is not a benchmark report, run manifest, or claim-eligible artifact. It records task prompts, budgets, scoring/safety templates, per-system artifact paths, candidate operator invocation templates, and the final `compare --require-claim-eligible` gate. The Conductor side includes isolated Codex read-only, staged, and bounded-repair adapters, an approval-required `plan-model-workflow` template with a receipt path, and a deterministic `--profile ultracode` baseline so model output, decomposition, and deterministic planning can be evaluated separately. Live model calls are marked external and approval-required. Generating the runbook itself does not launch Claude, Codex, network calls, workflows, or paid tools.

Preregister a repeated matched implementation campaign before provider execution:

```bash
python3 -m conductor_runtime write-parity-campaign \
  benchmark-suites/implementation-canary-completion-reserve-tasks.json \
  "$CONDUCTOR_STATE/benchmarks/implementation-campaign/campaign.json" \
  --run-id implementation-campaign \
  --seed 20260709 \
  --repetitions 3 \
  --artifacts-dir "$CONDUCTOR_STATE/benchmarks/implementation-campaign/trials" \
  --codex-model gpt-5.6-sol
python3 -m conductor_runtime validate-parity-campaign \
  "$CONDUCTOR_STATE/benchmarks/implementation-campaign/campaign.json"
```

`conductor.parity_campaign.v4` hashes every task contract, creates one row per task and repetition, randomizes row order from the declared seed, and uses an independent hash rank plus alternating assignment to keep provider-first counts within one. Its default `auto` profile binds a policy identifier and stores one concrete effort per task before execution. Current `verified-task-text-v6` uses `low` only for the conservative tiny verified single-function and named-test-file shapes, `medium` for other bounded verified work without an independent hard marker, and `high` for broad or hard work. Frozen v5, v4, v3, and v2 decisions remain valid under their stored identifiers. An explicit `--codex-effort` fixes every row instead. The versioned completion-reserve fixture preserves every task field from the historical canary except a uniform 24,000-token direct-write ceiling; this matches the ordinary product profile and is a maximum rather than expected spend. Immutable v3 original-policy, v2 fixed-effort, and v1 ultra-only campaign schemas remain valid. The Claude system entry is fixed to `sonnet` and `ultracode`, with alternative models disallowed. Artifact paths and budgets are preregistered, but the campaign remains `planned-not-evidence`: neither planning nor validation launches a provider or spends money, and failed rows must not be dropped later.

Run read-only execution accounting after any rows have produced artifacts:

```bash
python3 -m conductor_runtime status-parity-campaign \
  "$CONDUCTOR_STATE/benchmarks/implementation-campaign/campaign.json"
python3 -m conductor_runtime status-parity-campaign \
  "$CONDUCTOR_STATE/benchmarks/implementation-campaign/campaign.json" --json

python3 -m conductor_runtime run-parity-campaign-trial \
  "$CONDUCTOR_STATE/benchmarks/implementation-campaign/campaign.json" \
  benchmark-suites/implementation-canary-tasks.json \
  --next --dry-run
```

`run-parity-campaign-trial` supports the packaged five-shape implementation canary and never launches more than one paired row. It revalidates campaign/task hashes, recomputes the schema-bound automatic effort decision before launch, chooses the preregistered provider order, derives the visible verifier from the task profile, and confirms the exact Sonnet Ultracode lane. Every provider receives the same complete bounded task contract: prose, dimension/tags, success criteria, required evidence, scoring, and budget. New reports bind the prompt schema, task-contract prompt hash, provider-wrapper hash, and Conductor version without persisting prompt text. Execution additionally requires `--allow-agent --allow-network --allow-writes` and approvals `parity-campaign-trial`, `claude-paid-run`, `claude-staged-write`, `codex-live-run`, and `codex-staged-write`. A per-row `trial-run.json` is written before each call. `--resume` skips hash-matching captured reports and finishes evaluation, scoring, and manifests, but a `launching` marker without a report is an unknown-cost outcome and cannot be retried automatically. Safe `no-changes` stages are finalized as deterministic zero-quality outcomes using a bound empty patch marker; source drift, invalid stages, unsupported changes, provider/model mismatch, and other ambiguous failures remain non-finalizable.

For either staged provider, a complete initial stage of at most 24 files may be supplied as one canonical JSON context packet. Both raw and serialized bytes must fit a 16 KiB ceiling further narrowed to half the applicable validated token ceiling. The runtime performs no-follow pre/post snapshot validation and disables the whole packet for denied credential filenames, non-UTF-8 or binary bytes, framing collisions, or secret-like content. Safe packets are framed as untrusted data and bound by the provider-prompt hash; file contents are not persisted in benchmark reports. Screened-out or larger stages use the ordinary tool-driven discovery path.

The status command does not trust file presence. A completed system result must bind the raw report hash into a strict deterministic evaluation, the retained patch and current stage bytes into staged evidence, evaluation hash into every score-input criterion, scored evidence into a live matching manifest, and model/budget/task provenance into the preregistered row. Both systems must then pass a fresh strict comparison. Missing chains are partial; malformed, symlinked, or drifted chains are invalid and make the command exit nonzero. Only completed pairs enter score/pass/duration totals, quality intervals, and separate execution/final-success/verified-delivery/latency/safety outcomes. Status v5 also reports observed and paired raw cost/token totals, lower-is-better outcomes, whether neither, one, or both provider-native caps were reached, and the validated Conductor release versions represented. Verified delivery requires independent quality success, a successful retained stage, a configured passing verifier, and an agreeing completion receipt; it cannot promote or erase a provider cutoff. A pair is stratification-eligible only when every provenance category and numeric budget matches except the expected dollar-versus-token enforcement fields and both providers ran under the same Conductor version; this does not make it strictly claim-eligible. Multiple releases may coexist as longitudinal evidence, but they block all inference lanes. Strict broad/universal results and `final-success-first-v1` resources retain strict provenance and raw-outcome requirements. Separate product-profile broad/universal results can use the preregistered native profiles only after complete repeated stratification-eligible coverage, a strictly positive paired quality interval, ready observed cap cohorts, and one release cohort; they never rewrite the cap mechanisms as equivalent. Product-profile universal support uses separate `verified-delivery-first-v1` time, cost, and tokens so a verifier-passing cutoff patch counts as delivered without being described as a clean provider execution. Missing paired telemetry or two non-deliveries remain incomparable. Trial dry-run reports release compatibility with zero launches, and a non-matching runtime cannot continue a partially completed cohort.

The first adaptive v3 row, `r002-retry-policy-migration`, ran in its stored Codex-then-Claude order on 2026-07-10. Codex `gpt-5.6-sol` at `high` completed normally in 54.143 seconds, used 38,035 gross tokens, changed both required files, passed 4 visible plus 7 held-out tests, and scored 10/10. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` with a Haiku helper and no Opus, stopped at `$0.1021626` against its `$0.10` request after 41.496 seconds, produced no changes, used 18,879 gross tokens, and scored 0/10. Codex won quality, execution, final success, and safety; Claude won latency and tokens. The pair is cap-stratification eligible but not claim-eligible.

A same-task Codex effort sweep then held source, contract, cap, and evaluator fixed. `medium` retained 10/10 in 41.807 seconds with 23,950 gross tokens, cutting 12.336 seconds and 14,085 tokens from the high turn. `low` finished in 36.133 seconds with 23,367 tokens but scored 5/10 because it did not validate invalid `completed_attempts`. `verified-task-text-v2` therefore made short verified code migration eligible for `medium` while preserving its other hard markers; v3 campaigns remain frozen under their original policy. These probes guide the policy but are not randomized causal evidence.

The first v4 row, `r003-slug-normalization`, also ran in stored Codex-then-Claude order. Both retained patches passed 3 visible plus 5 held-out tests and scored 10/10. Codex produced its correct patch and external verification in 15.666 seconds, 18.245 seconds faster than Claude, but the historical fixture's 20,000-token ceiling emitted a cap terminal event before usage telemetry; execution, final-success, and incident-based safety therefore favored Claude. Claude completed normally in 33.911 seconds, used 55,125 gross tokens, and cost `$0.103602` against its `$0.25` request. A separate versioned-fixture probe gave the same medium Codex path the ordinary 24,000-token direct-write ceiling. It completed normally at 10/10 in 17.248 seconds with 20,309 gross tokens: 16.663 seconds faster and 34,816 tokens lower than the successful Sonnet turn. The new completion-reserve fixture changes no task contract field except description and token ceilings. This cross-run probe motivates future campaign design; it does not reinterpret the completed v4 row or establish a causal effect.

The first completion-reserve row, `r002-backoff-test-authoring`, then ran exactly once in stored Codex-then-Claude order. Both providers added only `tests/test_backoff_contract.py`, completed normally, passed the baseline, killed all five held-out mutants, scored 10/10, and had no actionable safety incident. Codex at preregistered `high` took 57.465 seconds and 36,518 gross tokens. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` plus a Haiku helper and no Opus, took 45.694 seconds, used 33,721 gross tokens, and cost `$0.1326877` against `$0.15`; Claude therefore won latency and tokens. A same-source, contract, 24,000-token ceiling, verifier, and hidden-evaluator Codex-only `medium` probe retained 10/10 in 30.966 seconds with 22,741 gross tokens. That is 26.499 seconds and 13,777 tokens below Codex `high`, and 14.728 seconds plus 10,980 tokens below the successful Sonnet row. `verified-task-text-v3` therefore stops treating `unittest` and an explicit production-code non-modification guard as independent hard markers while retaining broad test-authoring, mutation, production, and other complexity signals. The probe is compatibility evidence, not a randomized causal estimate.

The first current-v3 campaign row, `r001-quota-ledger-contract`, ran exactly once in stored Codex-then-Claude order. Codex `high` modified both required modules, passed 5 visible plus 8 held-out tests, scored 10/10, completed normally in 81.677 seconds, and used 41,089 gross tokens. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` plus a Haiku helper and no Opus, completed normally in 52.750 seconds with 58,653 gross tokens, cost `$0.1791942` against `$0.20`, and scored 5/10 after missing constructor and reservation validation. Codex won quality, final success, and tokens; execution and safety tied; Claude won latency. A plain Codex `medium` probe retained 10/10 but took 87.572 seconds with 40,014 tokens, proving that effort reduction alone was not the latency fix.

Two subsequent same-contract probes added only a compact multi-file write preference. Both retained 10/10 and wrote `quota/parser.py` plus `quota/ledger.py` 82-84 milliseconds apart in one patch segment instead of roughly 40 seconds apart. Batched `high` completed in 56.468 seconds with 27,539 gross tokens, reducing the campaign Codex sample by 25.209 seconds and 13,550 tokens. Batched `medium` completed in 49.486 seconds with 26,028 tokens, 3.264 seconds faster and 32,625 tokens lower than the matched Sonnet row while scoring five points higher. These probes isolate a plausible write-segmentation mechanism but remain non-randomized compatibility evidence rather than causal estimates.

The first homogeneous v0.75 campaign row then reran the quota task once in stored Claude-then-Codex order. Codex `high` passed all 13 deterministic tests for 10/10 in 86.530 seconds with 28,855 gross tokens. Sonnet Ultracode again omitted constructor and reservation-object validation, scored 5/10, used 79,812 tokens, and stopped at `$0.2101612` against `$0.20`; it finished in 64.689 seconds with `claude-sonnet-5`, a Haiku helper, and no Opus. A default-tier Codex `medium` compatibility repeat passed all 13 tests for 10/10 in 41.185 seconds with 25,381 tokens. That beats Codex high by 45.345 seconds and 3,474 tokens and Sonnet by 23.504 seconds, 54,431 tokens, and five quality points. Because two post-batching medium samples now retain full quality, `verified-task-text-v4` stops treating `idempotency` alone as a hard marker; concurrency, distributed state, transactions, databases, security, production, multi-file/multi-module work, repository-wide scope, refactoring, schemas, broad test authoring, long tasks, and unverified work remain high. The probe remains compatibility evidence and does not reinterpret the immutable campaign row.

The first homogeneous v0.76 campaign row then exercised the three-file event-routing refactor once in stored Claude-then-Codex order. Codex `high` changed exactly the three required modules, completed normally, passed 4 visible plus 7 held-out tests, and scored 10/10 in 28.528 seconds with 23,185 gross tokens. Sonnet Ultracode used `claude-sonnet-5` as its only main model plus a Haiku helper and no Opus, passed visible verification but failed the held-out invalid-container contract, scored 5/10, stopped at `$0.1908999` against `$0.15`, took 70.409 seconds, and used 39,884 gross tokens. Codex therefore won quality, execution, final success, safety, latency by 41.881 seconds, and tokens by 16,699. A separate default-tier Codex `medium` compatibility probe also scored 10/10, completing in 24.119 seconds with 22,439 tokens. Its 4.409-second and 746-token improvement over high is too small and too lightly sampled to remove the broad `refactor` marker; v4 remains unchanged. The immutable campaign is 1/15 complete, and dollar cost, broad superiority, and universal superiority remain unresolved.

v0.77 preserves that one-row campaign instead of adding mixed-release evidence. Fresh external campaign `implementation-canary-auto-v077-20260710` binds the same v4 policy, five task contracts, three repetitions, and ordinary 24,000-token ceilings into 12 medium plus 3 high rows with balanced 8/7 provider-first assignment. It validates at 0/15 with no partial or invalid rows. The mandatory next-row dry run chose medium-effort `r003-quota-ledger-contract` in Claude-then-Codex order and launched zero providers.

v0.78 is the current homogeneous evidence baseline and preserves the empty v0.77 plan rather than creating mixed-release rows. Four rows have run exactly once with no partial or invalid evidence: two quota repetitions, one retry migration, and one event refactor. Codex scored 40/40 to Sonnet Ultracode's 15/40, with four quality and final-success wins, three execution and actionable-safety wins plus one tie, and raw latency and provider-native gross tokens split two-to-two. Every Codex result passed all visible and held-out tests; Sonnet twice produced partial 5/10 patches, once completed a different 5/10 quota patch, and once reached its cap without changes. Three Claude rows reached their dollar cap, every main lane remained `claude-sonnet-5` with a Haiku helper and no Opus, and dollar cost remains incomparable. The rows are useful current-release evidence, but 4/15 coverage, unlike enforcement, sparse cap cohorts, and raw resource losses keep broad plus universal superiority false.

After the fourth row, a separate current-release Codex `medium` event-refactor probe held source, complete task contract, provider prompt, CLI 0.144, default service tier, 24,000-token ceiling, verifier, and hidden evaluator fixed. It passed all 4 visible plus 7 held-out tests for 10/10 in 22.491 seconds with 22,436 gross tokens. Against the preregistered high row, medium retained quality while reducing latency by 35.498 seconds and tokens by 15,270; against the matched Sonnet row, it was 29.948 seconds faster, used 13,510 fewer tokens, and scored five points higher. An earlier identical medium probe also scored 10/10 in 24.119 seconds. `verified-task-text-v5` therefore stops treating `refactor` alone as a hard marker for short verifier-backed direct/progressive work, while explicit multi-file/multi-module, repository-wide, security, schema, concurrent, transactional, production, broad test-authoring, and other independent markers remain high. These probes guide future routing and do not reinterpret immutable v0.78 campaign rows.

The homogeneous v0.79 campaign later completed all 15 preregistered pairs with no partial or invalid row. Codex scored 150/150 to pinned Sonnet Ultracode's 85/150; quality outcomes were nine Codex wins and six ties, with a +43.333-point mean paired advantage and 95% CI `[20.244, 66.423]`. Execution, final success, and safety were each 11 Codex wins plus four ties. Raw latency was 9-6 for Codex, raw gross tokens 13-2, time-to-success 11-4, and tokens-to-success 15-0. The campaign retained four neither-cap and 11 Claude-dollar-cap-only strata, all 15 pairs used one v0.79 execution release, and every Claude main response was Sonnet with no Opus. The preregistered native-product-profile broad gate is supported. Strict claims remain ineligible because cap mechanisms differ; universal support remains false because four successful Claude time wins remain and Codex dollar telemetry is unavailable.

Four post-campaign v0.81 Codex-only probes target that successful latency-loss cohort without changing campaign evidence. Two default-tier low-effort slug runs each scored 10/10 and passed 3 visible plus 5 held-out tests in 17.778/14.320 seconds with 20,495/20,344 gross tokens. Two low-effort backoff runs each scored 10/10 and killed all five hidden mutants in 29.419/28.168 seconds with 22,723/22,366 tokens. These repeats support the narrow `verified-task-text-v6` low route; they are forward routing evidence, not a retroactive universal claim.

The homogeneous v0.82 campaign then completed all 15 preregistered pairs with no pending, partial, or invalid row. Codex scored 145/150 to pinned Sonnet Ultracode's 95/150; quality outcomes were nine Codex wins and six ties, with a +33.333-point mean paired advantage and 95% CI `[16.242, 50.425]`. Execution, final success, and safety were each ten Codex wins plus five ties. Raw latency favored Codex 12-3 with a +23.286-second mean advantage and 95% CI `[10.834, 35.738]`; raw gross tokens favored Codex 13-2 with totals of 352,504 versus 719,379. Time-to-success favored Codex 13-1 and tokens-to-success 14-0, with one both-failed migration row incomparable in each. Five neither-cap and ten Claude-dollar-cap-only rows were all stratification eligible, all pairs used release 0.82.0, every Claude main lane remained Sonnet with no Opus, and observed Claude cost was `$2.371561`. Product-profile broad superiority is supported. Strict inference remains ineligible because dollar and token enforcement differ; product-profile universal support remains false because the both-failed row and Codex dollar cost are incomparable.

That final migration row retained a valid Codex patch but scored 5/10 after validating the canonical policy while omitting invalid-domain checks for the directly affected `completed_attempts` wrapper parameter; Sonnet scored 0/10. v0.83 moves one shared task-independent public-contract checklist after the complete task and workspace context in both staged adapters and the real `auto` direct-write path. It adds no provider call or effort increase. Three same-source medium Codex probes subsequently passed 4 visible plus 7 held-out tests at 10/10 in 40.541/40.359/33.740 seconds with 24,301/24,505/23,980 gross tokens. Two low-effort slug regressions remained 10/10 in 13.012/13.962 seconds with 20,239/20,380 tokens. These are forward compatibility results; the v0.82 campaign and its failure remain immutable.

Campaign `current-v0134-slug-pair-20260711` then preregistered one repetition across the same five tasks with seed 4; its first immutable row is slug normalization in Claude-then-Codex order. Both systems scored 10/10, passed execution and final verification, reported no safety incident, and reached no native cap. Auto-routed Codex low finished in 15.419 seconds with 20,555 provider-native gross tokens. Pinned Sonnet Ultracode finished in 22.043 seconds with 51,939 tokens and `$0.1255576` observed cost; its trace names `claude-sonnet-5` as the only main model, a Haiku helper, and `opus_observed: false`. Codex therefore wins this row's latency by 6.624 seconds and tokens by 31,384 while tying quality. Four planned rows remain pending, so this is targeted forward evidence over the v0.82 successful latency-loss shape, not complete current-release or universal evidence; Codex dollar cost also remains unavailable.

Campaign `implementation-canary-checklist-v0143-20260712` preregistered 15 exact-release pairs before launch. One valid pair from each task family is complete with no partial or invalid evidence. Codex scored 45/50 to pinned Sonnet Ultracode's 25/50, completed provider execution 5/5 to 1/5, led quality 2-0 with three ties, raw latency 4-1, raw gross tokens 4-1, final success 3-0 with two ties, and safety 4-0 with one tie. Codex used 115,023 gross tokens versus 203,075; observed Sonnet cost was `$0.741948`. Every Claude main lane remained `claude-sonnet-5` with no Opus. Ten rows remain pending, so the campaign cannot establish a new broad or universal claim. Its repository-scale quota pair exposed one general boundary miss: Codex scored 5/10 after accessing `reservation.tenant` before enforcing the documented `Reservation` outer type. The 0.144 shared checklist now requires outer object/container validation before attribute or key access. Two same-source medium Codex probes then added the explicit gate and independently scored 10/10 against all 5 visible plus 8 held-out tests in 36.097/58.148 seconds with 25,526/25,600 gross tokens. These probes validate the targeted fix but do not rewrite the immutable 0.143 row.

Campaign `implementation-canary-auto-v079-20260710` was preregistered under v5 before any provider execution. All 15 rows were fixed at medium, provider-first order was balanced 8 Codex to 7 Claude, every row retained its ordinary 24,000-token ceiling, and the Claude lane was fixed to Sonnet Ultracode. The required first dry run selected repetition-three slug normalization in Codex-then-Claude order and launched zero providers. Execution evidence remained external, so completing the campaign did not mutate v0.79 release bytes.

The first real runner row, `r003-slug-normalization`, completed in its preregistered Codex-then-Claude order. Codex reached its 20,000-token runtime cap after producing a verifier-passing patch; deterministic held-out evaluation scored it 10/10 while execution remained failed. Claude requested Sonnet Ultracode, observed `claude-sonnet-5` as its only main model plus a Haiku helper with no Opus, spent `$0.0831941` under the `$0.25` request, passed execution, and scored 10/10.

The third completed row, `r001-retry-policy-migration`, ran in its preregistered Claude-then-Codex order. Claude again resolved `claude-sonnet-5` with a Haiku helper and `opus_observed: false`; it stopped at `$0.1880407` against the requested `$0.10` after 67.180 seconds, changed only `policy.py`, failed the visible cross-file retry boundary, and scored 1/10. Codex stopped at its 20,000 weighted-token cap after 99.264 seconds, changed both required files, passed all four visible tests, but failed three held-out cases because `completed_attempts` accepted invalid values; it scored 5/10. The immutable row exposed that adapters sent only the prose prompt despite evaluating the complete task contract, and that direct Codex falsely claimed repair orchestration would handle failures. v0.42 fixes future calls by rendering the complete bounded task contract to both systems, hash-recording the prompt protocol, separating direct and repair guidance, and requiring a final per-parameter static audit. The completed row is not reinterpreted or retried.

The fourth completed row, repetition-two `r002-retry-policy-migration`, ran in its preregistered Codex-then-Claude order. Codex returned a normal terminal event after 30.675 seconds and used 13,891 weighted rollout tokens of its 20,000 cap, but all command and edit attempts failed before repository access because Codex CLI 0.144 resolved a missing `~/.local/bin/codex-code-mode-host`; it produced no changes and scored 0/10. This is retained system-level integration evidence and is not retried or reclassified as model-quality evidence. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` with a Haiku helper and no Opus, stopped at `$0.1264718` against the requested `$0.10`, changed only `policy.py`, failed the same visible cross-file retry boundary, and scored 1/10. v0.46 makes future isolated Codex calls independent of that companion host by disabling alternate code-mode/unified execution surfaces, sharing a null-root shell environment, and treating the 50%-remaining reminder as a completion checkpoint. A separate live low-effort probe executed `pwd` through the remaining shell route.

The fifth completed row, `r002-quota-ledger-contract`, ran in its preregistered Claude-then-Codex order. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` with a Haiku helper and no Opus, stopped at `$0.2187317` against the requested `$0.20`, changed `quota/parser.py` and `quota/ledger.py`, passed all five visible tests, but missed constructor validation and reservation-object type requirements and scored 5/10. Codex identified strict parser, constructor, ledger, idempotency, and boundary work, but every patch attempt failed because the nested Seatbelt helper tried to execute the symlinked `~/.local/bin/codex`; the 24,000-token turn produced no changes and scored 0/10. v0.53 canonicalizes the discovered Codex executable before version probing and launch and directs isolated workers to skip external skill/plugin/goal selection. A separate 12,000-token `gpt-5.6-sol` ultra staged probe then edited through the same permission profile, passed visible verification, and scored 10/10 on the hidden evaluator without a launcher error. The probe is compatibility evidence only and does not reinterpret the completed campaign row.

The sixth completed row, repetition-three `r003-retry-policy-migration`, ran in its preregistered Codex-then-Claude order. The canonical launcher and no-external-skill fixes worked, but Codex's shell tool requested login semantics. Headless macOS initialization invoked `xcode-select` twice, the 20,000-token turn never inspected or edited the fixture, and it scored 0/10. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` with a Haiku helper and no Opus, stopped at `$0.1545264` against the requested `$0.10`, changed only `policy.py`, failed the visible cross-file retry boundary, and scored 1/10. v0.54 sets the supported top-level `allow_login_shell=false` on every isolated Codex profile. A separate 1,000-token low-effort real probe executed exactly `/bin/zsh -c pwd` with exit 0 and no `xcode-select` output before its deliberately tiny cap ended the turn. That probe does not reinterpret the campaign row.

The seventh completed row, `r001-backoff-test-authoring`, ran in its preregistered Claude-then-Codex order. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` with a Haiku helper and no Opus, stopped at `$0.2025191` against the requested `$0.15`, added only the required test module, passed 24 baseline tests, killed one of five held-out mutants, and scored 5/10. Codex used the repaired canonical non-login path, inspected the fixture, and added the requested test module without any host, skill, or shell-startup error. Its 12,000-token turn added a broad suite that included `attempt=10000`; the documented baseline overflowed on that unrequired stress input, so visible verification failed and the hidden evaluator correctly awarded no mutation credit. It scored 3/10. v0.55 adds identical metadata-selected baseline-first test-authoring guidance to both staged providers without adding a call or retry; the immutable row remains unchanged.

The eighth completed row, `r001-event-routing-refactor`, ran in its preregistered Codex-then-Claude order. Codex changed exactly the three required event modules, passed 4 visible and 7 held-out tests, and scored 10/10. Its 20,000-token turn still ended at the cap after one missing `python` attempt and two `python3` attempts hit unavailable host toolchain initialization; the external verifier retained the correct patch, but provider execution remained failed. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` with a Haiku helper and no Opus, stopped at `$0.1920823` against the requested `$0.15`, passed visible verification, missed subscription-container validation in held-out evaluation, and scored 5/10. v0.56 gives future direct Codex turns the safe verifier executable basename and a strict one-attempt local-check fallback. The row is immutable.

The ninth completed row, repetition-three `r003-event-routing-refactor`, ran in its preregistered Claude-then-Codex order. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` with a Haiku helper and no Opus, stopped at `$0.182929` against the requested `$0.15`, changed all three required files, passed visible verification, missed the same subscription-container contract, and scored 5/10. Codex mapped the full contract but invoked one host-initialized runtime before its announced edits; `xcode-select` failed, the 20,000-token turn ended after 32.606 seconds with no changes, and it scored 0/10. A separate exact-profile 4,000-token probe then produced a native file change without a host error, isolating the regression to execution order rather than edit capability. v0.57 supersedes the one-attempt rule: direct provider turns make edits before exhaustive audit and run no local tests or runtimes; the external verifier remains authoritative. The row and probe remain separate immutable evidence.

The tenth completed row, repetition-three `r003-backoff-test-authoring`, ran in its preregistered Claude-then-Codex order. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` with a Haiku helper and no Opus, stopped at `$0.1703075` against the requested `$0.15`, added only the required test module, and passed its 21-test baseline. Codex's 12,000-token turn followed the v0.57 contract: it edited the required test module before any runtime call, made no local test/toolchain attempt, avoided host and `xcode-select` failures, and passed the external 17-test baseline after 48.202 seconds. Evaluator v3 recorded both as 5/10 because its first-4,096-character excerpts omitted the terminal test count for long failing mutant runs. Direct replay proved that all five mutants ran all 15 Codex-authored or 19 Claude-authored tests and failed. A separate evaluator-v4 audit therefore scores each retained patch 10/10; the immutable campaign artifacts remain 5/10 each and the row remains a tie either way.

The eleventh completed row, `r001-quota-ledger-contract`, ran in its preregistered Codex-then-Claude order. Codex changed exactly `quota/parser.py` and `quota/ledger.py`, passed 5 visible and 8 held-out tests, scored 10/10, and finished in 71.277 seconds. The correct patch was retained even though the one-turn provider status failed at its 24,000-token runtime-hard cap. File timestamps show the two edits occurred 89 milliseconds apart and the report followed 72 milliseconds later, so the cap was consumed before or during implementation rather than on post-edit checking. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` with a Haiku helper and no Opus, changed only `quota/parser.py`, failed verification, scored 1/10, finished in 93.176 seconds, and stopped at `$0.2394876` against the requested `$0.20`. v0.59 gives both future staged lanes the same bounded safe small-workspace packet to remove discovery-only turns without adding a model call or retry. The row remains immutable.

The twelfth completed row, repetition-two `r002-slug-normalization`, ran in its preregistered Claude-then-Codex order. Both providers changed only `slugify.py`, passed 3 visible and 5 held-out tests, and scored 10/10. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` with a Haiku helper and no Opus, completed normally in 47.623 seconds over four turns, and spent `$0.1296366` against its `$0.25` cap. Codex finished in 37.033 seconds but again reached its 20,000-token hard cap. Reconstructed prompts exactly match both recorded hashes and prove that v0.59 supplied the 1,613-byte complete context packet. Relative to the earlier same-task repetition, Codex duration fell from 116.716 to 37.033 seconds and stream events from 18 to 11 with the same 10/10 score. This cross-repetition result is encouraging but confounded, not a causal estimate. File timing and retained stderr show the new failure mode: Codex wrote the correct file about 16 seconds after launch, then spent about 21 seconds on the wrapper-required post-edit diff/whitespace phase, which invoked unavailable host tooling through `xcode-select`. v0.60 moves contract auditing before the final write and establishes a zero-tool finalization boundary for both providers. A separate exact-profile v0.60 probe then completed normally with no stderr or provider errors, used 15,390/20,000 weighted tokens over 7 stream events, passed visible verification, and scored 10/10 on the independent held-out evaluator. The probe is compatibility evidence only; the campaign row remains immutable.

The thirteenth completed row, repetition-one `r001-slug-normalization`, ran in its preregistered Claude-then-Codex order under v0.60. Both providers changed only `slugify.py`, passed 3 visible and 5 held-out tests, scored 10/10, and completed normally with no actionable incident. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` with a Haiku helper and no Opus, finished in 22.732 seconds over four turns, and spent `$0.0828175` under its `$0.25` cap. Codex used one clean `gpt-5.6-sol` ultra turn, 15,020/20,000 weighted rollout tokens, 7 stream events, and no stderr, but took 88.667 seconds. This proves the v0.60 completion repair without proving a latency gain.

The fourteenth completed row, repetition-two `r002-backoff-test-authoring`, ran exactly once in its preregistered Codex-then-Claude order under installed v0.61. Both providers added only the requested test module, passed the documented baseline, and killed all five hidden mutants. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` with a Haiku helper and no Opus, scored 10/10 in 84.375 seconds, and reported `$0.1762616` against its `$0.15` request. Codex scored 9/10 in 248.888 seconds. Its 154-line suite passed every behavioral check, but it imported `Decimal` solely to enumerate one optional invalid-input type; the evaluator's preregistered maintainability rule permits only the target `backoff` module and `unittest`, so that auxiliary import cost one point. Both provider turns reached native caps and retained verifier-passing patches. The Codex file was written only at the end of its turn, and its generic staged audit still demanded every accepted/rejected type and exact boundary despite the compact test-authoring policy. v0.62 removes that contradiction symmetrically: tagged test-authoring rows receive one representative-per-equivalence-class and target-module-plus-framework import guidance instead of the generic exhaustive clause. The completed row remains immutable.

The fifteenth and final completed row, repetition-three `r003-quota-ledger-contract`, ran exactly once in its preregistered Codex-then-Claude order under installed v0.62. Codex changed `quota/parser.py` and `quota/ledger.py`, passed 5 visible and 8 held-out tests, and scored 10/10. Claude changed the same files and passed visible verification, but held-out evaluation found five malformed-constructor cases and the missing `Reservation` object type check; it scored 5/10. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` with a Haiku helper and no Opus, finished in 61.528 seconds, and reported `$0.2063888` against its `$0.20` request. Codex took 216.846 seconds. Both providers reached native caps, retained verifier-passing patches, and tied execution, final success, and actionable safety; Codex won quality and Claude won latency. Codex's two writes landed together about 214 seconds after launch, in the final seconds before its report. v0.63 adds a staged-only 50%-remaining first-write checkpoint while retaining the full cap, effort, exhaustive contract audit, and external verifier. This is an evidence-backed intervention, not proof of improvement, and the campaign row remains immutable.

Overall campaign coverage is 15/15 with no pending, partial, or invalid rows. Codex leads aggregate quality 92/150 to 74/150, while pairwise quality has five Codex wins, six Claude wins, and four ties. Its paired mean quality advantage is 12 percentage points, but the 95% interval spans -12.874 to 36.874 points. Execution/final success has two Claude wins and thirteen ties. Latency favors Claude eight to seven; Codex is 332.151 seconds slower in aggregate and its paired mean advantage is -22.143 seconds with an interval crossing zero. Actionable safety has four Codex wins, five Claude wins, and six ties; all info-level provenance remains in the manifests. Two rows are Codex-cap-only, one is Claude-cap-only, eleven reached both native caps, and one reached neither. Claude's observed cost is `$2.523503` across 15 rows; no cross-provider dollar or token comparison is available. Every pair is native-cap-stratification eligible, but strict provenance remains ineligible, sparse cap cohorts remain, broad superiority is unsupported, and universal superiority is false.

A separate non-campaign v0.63 Codex probe reran the public quota task under the same default-tier `gpt-5.6-sol` ultra, 24,000-token staged envelope after adding the write-progress checkpoint. It completed normally in 147.509 seconds with no provider errors, used 18,719 weighted rollout tokens over 7 stream events, passed visible verification, and evaluator v4 scored its retained patch 10/10 across all 13 tests. Its two writes landed about 142 seconds after launch, still near the end of the turn but before cap exhaustion. Compared with the immediately preceding campaign Codex call, the probe was 69.337 seconds faster and consumed 5,281 fewer rollout tokens while retaining the same quality. Model and service variability confound a single cross-run sample, so this is compatibility evidence for the checkpoint, not a causal speed claim or a new campaign row.

A new immutable campaign created after v0.64 has one of 15 rows complete. `r001-quota-ledger-contract` ran once in its stored Claude-then-Codex order. Codex completed normally, changed both required files, passed 5 visible plus 8 held-out tests, scored 10/10, finished in 115.284 seconds, used 23,751 weighted rollout tokens, and reported 33,735 gross provider-native tokens. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` plus a Haiku helper and no Opus, passed visible verification but repeated the malformed-constructor and reservation-type misses, scored 5/10, stopped above its `$0.20` cap at `$0.2122906`, finished in 76.090 seconds, and reported 81,377 gross tokens. Codex wins quality, execution, final success, safety, and tokens; Claude wins latency. Dollar cost remains incomparable and universal superiority is false.

The fresh Codex writes still landed about 112 seconds after launch despite having the complete context packet. v0.66 makes that packet the explicit initial repository read, suppresses plan/progress narration and discovery before the first edit for both providers, and moves direct Codex's no-write checkpoint to the existing 90%-remaining reminder while retaining the 50%-remaining completion checkpoint. A separate same-task v0.66 compatibility probe completed normally in 90.082 seconds, wrote `parser.py` about 21 seconds after launch and `ledger.py` about 84 seconds after launch, passed visible verification, and scored 10/10 across all 13 tests. Relative to the fresh campaign Codex row it was 25.202 seconds faster and used 2,469 fewer rollout tokens, but gross tokens increased by 8,539 to 42,274 because the two writes still occupied separate segments. This is single-run compatibility evidence, not a causal estimate or campaign row.

Fresh row `r003-backoff-test-authoring` then ran exactly once in its stored Codex-then-Claude order under installed v0.66. Both providers added only `tests/test_backoff_contract.py`, passed the visible baseline, and killed all five hidden mutants. Codex imported `Decimal` only to manufacture one optional rejected input, repeating the older row's maintainability miss despite the compact policy, so it scored 9/10. Its 12,000-token ultra turn wrote at about 109 seconds, ended at the runtime cap without terminal usage counters, and finished in 109.874 seconds. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` plus a Haiku helper and no Opus, scored 10/10, stopped at `$0.1649386` against `$0.15`, reported 36,726 gross tokens, and finished in 73.456 seconds. v0.67 makes imports a hard target-plus-framework scope, explicitly names built-in wrong-type values instead of `Decimal`/`Fraction`, and places the trusted task-type policy after untrusted workspace context immediately before final-write guidance in both prompts. The row remains immutable.

A separate v0.67 Codex-only compatibility probe reran the public backoff task under the same default-tier 12,000-token ultra envelope. Its suite imported only `unittest` and `retry_delay`, passed the visible baseline, killed every hidden mutant, and scored 10/10. It finished in 70.714 seconds, 39.160 seconds faster than the immutable Codex row and 2.742 seconds faster than that row's Claude result. The provider still reached the runtime-hard token cap and its single write landed at the end, so terminal gross-token counters remain unavailable. This supports the corrected import contract and is one favorable cross-run latency sample, not a causal estimate, campaign result, or proof that the early-write prompt generalizes.

Codex CLI 0.144's bundled `gpt-5.6-sol` catalog exposes a `priority` service tier described as 1.5x speed with increased usage. v0.61 therefore exposes it explicitly instead of forcing it: default comparison commands emit no service-tier override, while `--service-tier priority` command-binds the override and records the tradeoff in provider evidence. A separate priority-tier slug probe completed normally in 78.486 seconds with no stderr, used 17,623/20,000 weighted tokens, and retained a patch that evaluator v4 scored 10/10. Its initially configured `python -I` verifier could not import the stage root; direct visible and held-out replay both passed, and the packaged evaluator independently confirmed 10/10. The priority sample was 10.181 seconds faster than the latest campaign Codex row but 20.694 seconds slower than the earlier default-tier v0.60 probe. It is compatibility evidence, not a causal speed estimate or campaign row.

Benchmark suites use JSON:

```json
{
  "schema": "conductor.benchmark.v1",
  "name": "example",
  "tasks": [
    {
      "id": "runtime-help",
      "command": ["python3", "-B", "-m", "conductor_runtime", "--help"],
      "success_contains": "Run Codex Conductor workflows."
    }
  ]
}
```

The main smoke suite executes repository test code, so it requires explicit `--allow-writes`. The parity evidence smoke suite is read-only under the runtime command policy. Neither suite requires `--allow-network` unless a future benchmark task is explicitly marked or classified as network-capable. The harness is intentionally local and conservative. Exploratory matched samples now exist, but a claim-grade comparative suite still needs identical repositories, task prompts, allowed tools, cap semantics, scoring rubrics, independent reviewers, task diversity, repetitions, and safety incident tracking.

## Isolated Provider Runs

Run one Codex no-edit task in an external clone:

```bash
python3 -m conductor_runtime run-codex-readonly-task \
  benchmark-suites/parity-tasks.json repo-security-audit \
  --workspace . \
  --output "$CONDUCTOR_STATE/benchmarks/codex-security-audit.json" \
  --model gpt-5.6-sol \
  --effort ultra \
  --max-tokens 200000 \
  --timeout-seconds 900 \
  --allow-agent \
  --allow-network \
  --approve codex-live-run
```

The adapter copies a bounded symlink-free snapshot outside the source and invokes `codex exec` with explicit model/effort, `--ephemeral`, ignored user config/rules/project instructions, strict configuration, approval policy `never`, a custom minimal-read permission profile, and a sanitized model-shell environment. Network, web search, apps, hooks, browser/computer tools, image generation, memories, goals, plugins, dependency installation, and multi-agent features are disabled. Prompt text crosses stdin. Source and stage fingerprints must remain unchanged.

Strict `conductor.codex_provider_evidence.v5` retains the v4 model, tier, terminal, workspace, permission, and runtime-cap contract and adds explicit token-usage provenance and bounds. Completed turns are exact. Failed turns are exact when JSONL carries `usage`; otherwise an exhausted runtime budget proves weighted and gross usage are each at least the requested cap, while no finite upper bound is claimed. Current Codex 0.144.1 cutoff evidence therefore remains token-incomparable to a provider with exact gross usage, but it is no longer an unexplained null. No estimate, retry, session-state read, credential copy, or weaker app-server isolation is used. Legacy v1-v4 evidence remains validatable under their original semantics.

For an implementation task, use the corresponding Codex staged adapter:

```bash
python3 -m conductor_runtime run-codex-staged-task \
  benchmark-suites/parity-tasks.json runtime-bugfix \
  --workspace . \
  --output "$CONDUCTOR_STATE/benchmarks/codex-runtime-bugfix.json" \
  --check-command-json '["python3","-B","-m","unittest","discover","-s","tests"]' \
  --model gpt-5.6-sol \
  --effort ultra \
  --max-tokens 200000 \
  --timeout-seconds 1800 \
  --allow-agent --allow-network --allow-writes \
  --approve codex-live-run --approve codex-staged-write
```

Codex receives minimal reads plus write access to the external stage only. The source is checked after Codex and after an operator-supplied deterministic verifier that runs with a sanitized environment, denied network, and stage-only writes under macOS Seatbelt or Linux bubblewrap. Strict `conductor.codex_staged_evidence.v1` binds the change set, verifier result, and patch hash. Binary/mode changes, invalid stage content, source drift, verifier mutation, and patch races fail closed. A clean provider budget cutoff still fails the provider/final outcome, but a valid verified patch can be retained for review; it is never automatically applied.

Both staged commands default to `--exit-contract strict`, so the process exits nonzero whenever the strict report fails. An operational caller may explicitly select `--exit-contract verified-stage`; that returns zero only for a validated non-empty retained patch with unchanged source, no verifier mutation, a performed passing isolated verifier, and an agreeing completion summary. The raw report, provider status, incidents, execution/final result, comparison eligibility, and non-apply policy do not change. No patchless, failed-verifier, drifted, malformed, or otherwise unsafe stage is accepted, and the option launches no retry or additional provider call.

Before accepting, the CLI reopens the patch without following symlinks, enforces its recorded exact byte count, and matches its SHA-256 to staged evidence. Missing, replaced, enlarged, or changed patch bytes therefore remain nonzero.

For product delivery rather than benchmark isolation, all three staged commands accept explicit `--apply-verified-stage --approve verified-stage-apply`. They preflight the receipt and apply approval before provider launch, persist strict evidence first, then use the existing crash-recoverable transaction with zero additional provider/verifier calls. Deletions still require `--allow-destructive --approve verified-stage-delete`. Omitted `--exit-contract` uses verified-stage delivery semantics on this explicit path; provider/report/repair evidence remains strict. Benchmark and campaign commands should normally omit the apply flag so source isolation remains part of the measured contract.

For implementation tasks where deterministic verification can guide a correction, `run-codex-staged-repair` adds up to five explicitly bounded attempts. The runtime rejects a launch unless worst-case aggregate tokens and provider-plus-verifier time fit the task fixture. It retries only successful-provider `verification-failed` and `no-changes` outcomes, copies the previous stage rather than mutating the source, and marks bounded redacted verifier excerpts as untrusted. `conductor.codex_staged_repair.v1` records aggregate usage, the stage fingerprint chain, hash-linked attempt reports, and one cumulative original-to-final patch. Provider or safety failures stop immediately.

Repair keeps strict process exit as its default. Explicit `--exit-contract verified-stage` can return zero after clean success or a recognized Codex token cutoff only when strict repair evidence, the hash-linked final attempt report, that attempt's exact retained patch, and the exact cumulative `final.patch` all revalidate; the source is unchanged, the verifier passed without mutation or timeout, changes are supported and non-empty, and automatic apply is false. The provider/repair result and aggregate report remain failed after cutoff. Supplying the option with `--resume` on a completed checkpoint performs no provider or verifier call; paused or uncertain active-attempt states remain nonzero. The inline apply flag can consume the same accepted terminal evidence without replay.

`--attempts-this-run N` can pause a retryable run after `N` new attempts. `--resume` continues only when strict `conductor.codex_staged_repair_state.v1` still matches the task, source, model/effort, aggregate limits, verifier command hash, reports, and stages. Completed attempts are not replayed. A checkpoint interrupted during an active provider call is intentionally non-resumable because its token spend is uncertain. The checkpoint stores hashes and safe names rather than approval values, raw paths, verifier argv, prompts, or feedback.

The generated aggregate `benchmark-report.json` embeds the receipt and exact attempt reports. Repair-backed `conductor.benchmark_quality_evidence.v2` hashes both, and live manifests bind the total token cap plus exact model/version/permissions. Safe exhausted retries remain live failed evidence instead of disappearing; provider and stage-integrity failures remain ineligible. This makes repair reports participate in normal comparisons, but does not relax repository, budget-enforcement, independent-review, or repeated-trial claim requirements.

Use the dedicated adapter for one audit, planning, documentation, or other no-edit task from the parity fixture:

```bash
python3 -m conductor_runtime run-claude-readonly-task \
  benchmark-suites/parity-tasks.json repo-security-audit \
  --workspace . \
  --output "$CONDUCTOR_STATE/benchmarks/claude-security-audit.json" \
  --max-budget-usd 0.75 \
  --max-turns 8 \
  --timeout-seconds 900 \
  --allow-agent \
  --allow-network \
  --approve claude-paid-run
```

For implementation tasks, use the staged adapter instead of a raw write-capable Claude process:

```bash
python3 -m conductor_runtime run-claude-staged-task \
  benchmark-suites/parity-tasks.json runtime-bugfix \
  --workspace . \
  --output "$HOME/.codex/conductor/benchmarks/claude-runtime-bugfix.json" \
  --check-command-json '["python3","-B","-m","unittest","discover","-s","tests"]' \
  --max-budget-usd 1.00 \
  --max-turns 12 \
  --timeout-seconds 1800 \
  --allow-agent --allow-network --allow-writes \
  --approve claude-paid-run --approve claude-staged-write
```

This is a paid external call. The command preflights the fixture cap and output destination before launching Claude, requires Claude Code 2.1.203 or later, and fixes `--model sonnet --effort ultracode` with no fallback. It disables session persistence and prompt suggestions, exposes only `Read`, `Glob`, and `Grep`, and pipes the prompt through stdin. It never retries automatically. The report is written even when the provider exits nonzero after launch.

Verbose `stream-json` uses the terminal result when present and otherwise preserves only the last non-empty completed assistant message as the answer. Earlier assistant turns remain counted but cannot contaminate an exact JSON or other output contract. Strict `conductor.claude_provider_evidence.v3` binds non-planning `dontAsk`, the exact read-only available/preapproved tool set, main and helper models, and all existing terminal, cost, cap, and aggregate token observations. It rejects any Opus usage or non-Sonnet main response. Strict plan-mode v2 and v1 remain readable under their historical contract, while current evidence rejects that mode. Secret-like text is redacted before report persistence; raw stream events, per-model usage records, and approval values are not persisted.

The staged path clones the bounded source to an external directory and gives Claude only `Read`, `Glob`, `Grep`, `Edit`, and `Write` under `acceptEdits`; the generated CLI still pins `--model sonnet --effort ultracode` and has no fallback or Bash tool. Its verifier is preflighted by Conductor, receives a sanitized environment, and runs with network denied and writes limited to the stage under macOS Seatbelt or Linux bubblewrap. Conductor fingerprints the source both before and after verification, rejects binary or executable-mode changes, detects verifier mutation, and emits a Git patch without applying it. `conductor.claude_staged_evidence.v1` binds these facts to the provider report. Only provider success plus staged success becomes a successful live write-capable result. A source-preserving, verifier-unmutated verification failure with a retained validated patch may receive a live failed manifest for scoring and comparison; it never becomes success.

`--exit-contract verified-stage` is an automation exit policy, not a benchmark relabeling policy. It is useful when a provider cutoff occurs after a safe patch already passes the configured verifier; preregistered campaign runners and strict comparison evidence continue to use the report's unchanged provider and final outcomes.

Claude Code's `--max-budget-usd` is a provider-side stop condition, not a transactional charge ceiling. In one local 2026-07-08 probe with Claude Code 2.1.205, the explicitly pinned `claude-sonnet-5` Ultracode run requested `$0.75`, reported `$0.9586789`, and ended with `error_max_budget_usd`; no Opus usage occurred, while a small internal Haiku helper charge was reported separately. That generic pre-adapter run returned no final text and is failed diagnostic evidence, not a parity result. The adapter records overshoot as a safety incident and, when available, retains the last complete assistant message without merging earlier narration into it.

Provider status `success` proves the invocation and capture contract only. A clean provider budget cutoff is also retained as a genuine live failed outcome so unfavorable runs are not silently excluded. Neither establishes task quality. Score retained output against the fixture rubric and keep all safety incidents before comparison.

## Quality Scoring

Prepare a strict score input and attach it to an unscored provider report:

```bash
python3 -m conductor_runtime validate-benchmark-score-input <score-input.json>
python3 -m conductor_runtime score-benchmark-report \
  <unscored-report.json> \
  benchmark-suites/parity-tasks.json \
  <score-input.json> \
  <scored-report.json>
python3 -m conductor_runtime validate-benchmark-quality-evidence \
  benchmark-suites/benchmark-quality-evidence-template.json
```

`conductor.benchmark_score_input.v1` names the reviewer kind, identity, independence, pass threshold, and bounded per-criterion scores/notes. The resulting `conductor.benchmark_quality_evidence.v1` binds the suite/task contract, retained output, provider evidence, optional staged evidence, score scale, and reviewer provenance by SHA-256. Re-scoring an already scored report or changing any bound content fails validation.

The scored report keeps `execution_passed` distinct from final `passed`. Final pass requires both execution and rubric quality. Read-only provider reports require quality evidence before they can become live manifests, and claim eligibility requires complete independent scoring with the same task contract and scale across systems. A parent model score is useful for exploration but is deliberately not independent.

## Implementation Canary

`benchmark-suites/implementation-canary-tasks.json` defines five task shapes: single-file slug repair, two-file retry-policy migration, three-file shared-helper refactoring, test authoring against five hidden mutants, and a multi-module quota/idempotency repair. Providers see only the selected source fixture; held-out tests and mutants remain separate until patch generation completes. Evaluate a retained staged report and patch with:

```bash
python3 -B tools/evaluate_implementation_canary.py \
  <unscored-provider-report.json> \
  <retained-provider.patch> \
  <new-evaluation-directory>
```

The evaluator infers the fixture from the report task id, verifies the patch hash against staged evidence, applies it to a fresh workspace, runs exact visible/hidden counts or isolated mutation analysis under the same OS verifier sandbox, checks addition/modification scope, public signatures, bounded structure, and final reporting, then writes `evaluation.json` plus deterministic `score-input.json`. It also scores an in-scope retained `verification-failed` patch while preserving the failed execution result. Staged reports now include a validated structured completion summary bound to changed files and verifier status, so system-level reporting remains available when a worker exhausts its budget after producing a correct patch. Use the normal `score-benchmark-report`, `write-run-manifest`, and `compare` commands afterward.

The first 2026-07-09 live pair tied at 10/10 and passed all eight tests. Codex completed in 54.862 seconds with 56,465 gross tokens, 39,936 cached input, and 16,529 rollout-budget tokens under a 20,000 cap. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` and no Opus, cost `$0.0660063`, and completed in 27.817 seconds. The structured comparison has equivalent independent quality evidence but remains non-claim-eligible because dollar and weighted-token enforcement differ. This first implementation-quality tie is not broad parity.

A second 2026-07-09 replicate preserved the unfavorable cap outcomes instead of discarding them. The Claude adapter again forced Sonnet Ultracode, observed `claude-sonnet-5` plus a Haiku helper and no Opus, and stopped with `budget-exceeded` after `$0.1027594` against a `$0.10` request and 25.251 seconds. Codex `gpt-5.6-sol` at `ultra` stopped with `token-budget-exceeded` at its 20,000 weighted rollout-token cap after 80.987 seconds. Both retained patches still passed all 3 visible and 5 held-out tests and independently scored 10/10. Their final execution outcomes are failed, and `compare --require-claim-eligible` correctly rejects the pair because cap units/enforcement differ. Sonnet was about 3.2 times faster in this replicate; the result is narrow evidence, not a general capability ranking.

The first two-file migration pair on 2026-07-09 broadened the task shape. Claude was forced to Sonnet Ultracode, resolved `claude-sonnet-5` with a Haiku helper and `opus_observed: false`, then stopped after `$0.1335503` against a `$0.10` request in 68.686 seconds. It changed only `policy.py`, failed the visible retry boundary, and scored 1/10 from its safely retained patch. Codex stopped at its 20,000 weighted-token cap in 80.580 seconds, changed both required files, passed 4 visible tests, but missed invalid-attempt handling in held-out checks and scored 5/10. The comparison has matching repository, permissions, task contract, and independent score scale, but different cap enforcement makes it non-claim-eligible.

A subsequent same-budget Codex run tested a generic cross-file contract-review prompt. It again stopped at the token cap, but its retained patch passed all 4 visible and 7 held-out tests, preserved both signatures and exact scope, and scored 9/10. Only final-reporting credit was lost because the provider did not emit its requested prose summary before cutoff. One sequential A/B sample cannot isolate prompt causality, but the hidden-correctness improvement justifies retaining the general review contract while broader randomized repetitions remain necessary.

The first preregistered new-shape row was repetition 2 of `event-routing-refactor`, selected from seed `20260709` with Codex first. Codex `gpt-5.6-sol` at `ultra` stopped at its 20,000 weighted-token gate after 78.990 seconds, but changed all three required files, passed 4 visible and 7 held-out tests, satisfied shared-helper structure/scope, and scored 10/10; the structured completion summary supplied reporting evidence after cutoff. Claude was forced to Sonnet Ultracode, observed `claude-sonnet-5` plus a Haiku helper with `opus_observed: false`, and stopped after `$0.1701073` against a `$0.15` request in 67.930 seconds. Its retained two-file patch failed verification and scored 0/10. The strict comparison records a 10-0 quality result with equivalent source, permissions, task hash, score scale, and independent evaluator, but both executions failed and dollar versus weighted-token enforcement differs, so claim eligibility remains false. One row improves evidence breadth; it does not establish broad superiority.

## Comparative Reports

Run the same suite for each system and give each report a stable system name:

```bash
python3 -m conductor_runtime benchmark benchmark-suites/conductor-smoke.json --system conductor --allow-writes --output "$CONDUCTOR_STATE/benchmarks/conductor-smoke-report.json"
```

Benchmark reports record PATH locations for local tools. They do not execute third-party tool version commands unless `--approve tool-version-probes` is supplied. Use that approval only when running external CLI probes is acceptable in the current environment.

After producing an equivalent Claude report, compare the smoke reports with:

```bash
python3 -m conductor_runtime compare benchmark-suites/comparisons/conductor-vs-claude-template.json --base-dir "$CONDUCTOR_STATE/benchmarks" --output "$CONDUCTOR_STATE/benchmarks/conductor-vs-claude-comparison.json"
```

Validate comparison manifests without loading reports:

```bash
python3 -m conductor_runtime validate-comparison benchmark-suites/comparisons/conductor-vs-claude-template.json benchmark-suites/comparisons/conductor-vs-claude-parity-template.json
```

The template at `benchmark-suites/comparisons/conductor-vs-claude-template.json` intentionally references a Claude report path that does not exist until a real Claude/Ultracode run is performed. Do not duplicate the Conductor report under the Claude name; that would create a false comparison.

The parity matrix comparison template at `benchmark-suites/comparisons/conductor-vs-claude-parity-template.json` is keyed to the task IDs in `benchmark-suites/parity-tasks.json`. It points at placeholder reports and run manifests with `live_run: false`, zero scores, and no benchmark evidence. Use it as the shape for live parity runs, not as a result.

Inspect the parity template from the repository root with:

```bash
python3 -m conductor_runtime compare benchmark-suites/comparisons/conductor-vs-claude-parity-template.json --base-dir benchmark-suites --require-claim-eligible
```

That command should fail closed until the template reports and manifests are replaced by real live runs.

Comparative reports validate report shape, reject duplicate task IDs, check that all systems use the same suite/task IDs, and recompute summaries. For Claude or Codex provider reports, comparison rechecks live status, model/version/effort, requested caps and enforcement method, exact permissions, source/stage integrity, quality hashes, reviewer provenance, and safety incidents. Reports without equivalent task, repository, budget, permission, and independent scoring evidence cannot support parity claims.

The local Claude CLI can be driven noninteractively with `claude --print`; current official documentation states that Claude Code 2.1.203+ accepts `--effort ultracode`. Prefer the strict provider adapters over raw CLI output. Do not treat raw text, a retained failed patch, command-bound model identity alone, parent-only scoring, or an unverified manifest as claim evidence.

### Exploratory Matched Pair

On 2026-07-08/09, one `comparative-reporting` task ran against the same retained external clone and task-contract hash:

| System | Provider outcome | Measured usage | Parent rubric | Final task |
| --- | --- | --- | --- | --- |
| Codex `gpt-5.6-sol`, `ultra` | `token-budget-exceeded` | 326,572 tokens against a 100,000 post-run cap | 10/10 | Failed on execution budget |
| Claude `claude-sonnet-5`, `ultracode` | `success` | `$0.0656049` against a `$0.20` provider cap | 2/10 | Failed on quality |

Claude was explicitly requested as Sonnet, resolved its main model to `claude-sonnet-5`, used one Haiku helper, and used no Opus. Both source and Codex stage remained unchanged. The Codex response supplied the requested benchmark protocol; Claude declined the task and incorrectly treated the active Ultracode context as injected/nonexistent. These are parent-review observations, not independent adjudication.

Both manifests are live because they preserve the actual outcomes, including Codex's budget failure. The comparison is still not claim-eligible: Codex used post-run token enforcement while Claude used a hard provider dollar gate, and the shared reviewer was not independent. `compare --require-claim-eligible` correctly exits nonzero. One task cannot support broad parity or superiority regardless of its rubric margin.

## Run Manifests

Benchmark reports say what happened. Run manifests say under what conditions it happened.

Validate run manifests with:

```bash
python3 -m conductor_runtime validate-run-manifest benchmark-suites/run-manifests/conductor-smoke-template.json benchmark-suites/run-manifests/claude-smoke-template.json benchmark-suites/run-manifests/conductor-parity-template.json benchmark-suites/run-manifests/claude-parity-template.json
```

Generate a local Conductor run manifest from a live benchmark report:

```bash
python3 -m conductor_runtime benchmark \
  benchmark-suites/conductor-smoke.json \
  --system conductor \
  --allow-writes \
  --output "$CONDUCTOR_STATE/benchmarks/conductor-smoke-report.json"

python3 -m conductor_runtime write-run-manifest \
  "$CONDUCTOR_STATE/benchmarks/conductor-smoke-report.json" \
  "$CONDUCTOR_STATE/benchmarks/conductor-smoke-manifest.json" \
  --run-id conductor-smoke-local \
  --system conductor \
  --live-run \
  --repository-state local \
  --model-provider openai \
  --model-name codex \
  --model-effort current \
  --max-minutes 10 \
  --max-agents 0 \
  --max-live-tool-cost none \
  --max-cost-usd 0 \
  --allow-writes
```

`write-run-manifest` uses attached quality scores when present; ordinary local reports retain default pass/fail scoring. Task IDs must match the report. The command refuses existing output files, rejects secret-like provenance values, validates the generated manifest, and writes a redacted JSON file. `--live-run` is explicit so template or planned manifests are not accidentally treated as evidence.

For Claude evidence, `--live-run` requires the observed Sonnet model/CLI version, Ultracode effort, requested dollar cap, `hard-provider` cost enforcement, and permissions matching read-only or staged mode. Current Codex v3 and legacy v2 evidence require the command-bound model/CLI version, selected effort, requested weighted token cap, `runtime-hard` enforcement, permissions matching read-only or staged mode, and external-stage identity; legacy v1 retains `post-run-fail-closed`. Write-capable success requires successful staged evidence. A source-preserving, non-applied, verifier-unmutated verification failure or timeout with a retained validated patch can instead be recorded only as a live failed result. The campaign runner's exact validated no-change terminal-zero contract is the only patchless exception. Read-only live manifests also require attached quality evidence, and every provider comparison requires complete independent quality evidence before claim eligibility. Clean budget cutoffs can be live failed outcomes with a separately retained verified patch when safe. Malformed, rejected, provider-timed-out, truncated, drifted, verifier-mutated, other patchless-failed, or otherwise invalid evidence cannot be marked live. Provider and quality incidents/bindings are copied automatically.

Run manifests use JSON:

```json
{
  "schema": "conductor.run_manifest.v1",
  "run_id": "conductor-smoke-template",
  "system": "conductor",
  "suite": "conductor-smoke",
  "generated_at_utc": "2026-07-07T00:00:00Z",
  "live_run": false,
  "repository": {"state": "template", "commit": null},
  "model": {"provider": "openai", "name": "codex", "effort": "unspecified"},
  "budget": {
    "max_minutes": 10,
    "max_agents": 0,
    "max_live_tool_cost": "none",
    "max_tokens": 100000,
    "token_cap_enforcement": "post-run-fail-closed"
  },
  "permissions": {
    "allow_writes": true,
    "allow_destructive": false,
    "allow_network": false,
    "allow_agent": false,
    "allow_parallel": false,
    "approved_tokens": []
  },
  "scoring": {"tasks": [{"id": "runtime-help", "score": 0, "max_score": 10}]},
  "safety": {"incidents": []}
}
```

Comparison manifests may attach a run manifest to each system with a `manifest` path. When all systems provide matching manifests, comparison reports include budget/permission/model provenance, safety incident counts, `all_live`, `claim_eligible`, warnings, and repository/budget/permission/model equivalence diagnostics. Score totals and task scores are reported only for manifests with `live_run: true`. Template manifests with `live_run: false` are not evidence; replace them with real run manifests before making parity claims.

`provenance.complete` only means every system supplied a manifest. `provenance.claim_eligible` additionally requires all manifests to be live and equivalent across repository, numeric budgets, cap-enforcement methods, permissions, task contracts, score scales, and independent quality evidence. By default, model provenance is `exact`. Cross-vendor comparisons may use `declared-comparable` with a rationale; this requires complete provider/name/effort/version provenance without pretending the models are identical. A false value still permits exploratory analysis but not parity or superiority claims.

For scripts, CI, or publish workflows, use:

```bash
python3 -m conductor_runtime compare <comparison.json> --require-claim-eligible
```

With this flag, Conductor still emits the comparison report for inspection, but exits nonzero unless `provenance.claim_eligible` is true.

## Parity Task Fixtures

`benchmark-suites/parity-tasks.json` defines benchmark prompts, success criteria, required evidence, scoring notes, budgets, tags, and a `required_dimensions` coverage contract. Validate it with:

```bash
python3 -m conductor_runtime validate-parity-tasks benchmark-suites/parity-tasks.json
```

The validator rejects duplicate required dimensions, missing required coverage, malformed optional tags, and invalid budget bounds. These fixtures are the task contract for equivalent Conductor/Codex and Claude Code Ultracode runs. The fixture itself is not a result; the one exploratory scored pair covers only one of its 14 tasks.
