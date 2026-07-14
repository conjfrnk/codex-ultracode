# Claude Code Compatibility Notes

Codex Conductor is not an implementation of Claude Code. The optional extras
package can compile a restricted static subset of saved Claude workflows and
can collect strict Sonnet/Ultracode comparison evidence.

The default runtime does not launch Claude, import Claude adapters, or claim
feature or quality equivalence. Compatibility inputs are parsed without
evaluating JavaScript and fail closed on unsupported dynamic behavior.

Paid comparisons require explicit approval, Sonnet, Ultracode effort, a fixed
budget, no automatic retry, and independent verification. Opus is rejected by
the strict adapter.
