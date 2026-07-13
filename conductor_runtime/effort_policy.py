import re

from .errors import ValidationError
from .model_planner import (
    DEFAULT_MODEL_WORKFLOW_REASONING_EFFORT,
    MODEL_WORKFLOW_REASONING_EFFORTS,
    validate_model_workflow_reasoning_effort,
)


AUTO_REASONING_EFFORT = "auto"
AUTO_REASONING_EFFORTS = set(MODEL_WORKFLOW_REASONING_EFFORTS) | {AUTO_REASONING_EFFORT}
AUTO_EFFORT_POLICY = "verified-task-text-v6"
AUTO_EFFORT_POLICY_V5 = "verified-task-text-v5"
AUTO_EFFORT_POLICY_V4 = "verified-task-text-v4"
AUTO_EFFORT_POLICY_V3 = "verified-task-text-v3"
AUTO_EFFORT_POLICY_V2 = "verified-task-text-v2"
AUTO_EFFORT_POLICY_V1 = "verified-task-text-v1"
AUTO_EFFORT_POLICIES = {
    AUTO_EFFORT_POLICY,
    AUTO_EFFORT_POLICY_V5,
    AUTO_EFFORT_POLICY_V4,
    AUTO_EFFORT_POLICY_V3,
    AUTO_EFFORT_POLICY_V2,
    AUTO_EFFORT_POLICY_V1,
}
AUTO_FAST_VERIFIED_EFFORT = "medium"
AUTO_TINY_VERIFIED_EFFORT = "low"
AUTO_SIMPLE_TASK_MAX_CHARS = 1000
AUTO_TINY_VERIFIED_TASK_MAX_CHARS = 400
AUTO_COMPLEXITY_MARKERS = {
    "across the repository",
    "audit",
    "authorization",
    "concurrent",
    "credential",
    "database",
    "dependency",
    "distributed",
    "multi-file",
    "multi-module",
    "mutation testing",
    "parallel",
    "payment",
    "permission",
    "production",
    "repo-wide",
    "repository-scale",
    "repository-wide",
    "schema",
    "security",
    "test authoring",
    "transaction",
    "upgrade",
    "write tests",
}
AUTO_COMPLEXITY_MARKERS_V4 = AUTO_COMPLEXITY_MARKERS | {"refactor"}
AUTO_COMPLEXITY_MARKERS_V3 = AUTO_COMPLEXITY_MARKERS_V4 | {"idempotency"}
AUTO_COMPLEXITY_MARKERS_V2 = AUTO_COMPLEXITY_MARKERS_V3 | {"unittest"}
AUTO_COMPLEXITY_MARKERS_V1 = AUTO_COMPLEXITY_MARKERS_V2 | {"migration"}
AUTO_LOW_RISK_SCOPE_CONTROLS = (
    "do not modify production code",
    "do not change production code",
    "leave production code unchanged",
    "production code must remain unchanged",
    "without changing production code",
    "without modifying production code",
)
AUTO_TINY_TASK_BLOCKERS = {
    "idempotency",
    "migration",
    "refactor",
}
AUTO_FOCUSED_TEST_TARGET = re.compile(
    r"(?<![a-z0-9])add tests (?:in|to) [a-z0-9_./-]+\."
    r"(?:c|cc|cpp|cs|go|java|js|jsx|kt|php|py|rb|rs|ts|tsx)(?![a-z0-9])"
)


def resolve_auto_reasoning_effort(
    task: str,
    requested_effort: str,
    *,
    route: str,
    verifier_kind: str,
    policy: str = AUTO_EFFORT_POLICY,
) -> str:
    if policy not in AUTO_EFFORT_POLICIES:
        raise ValidationError("automatic effort policy is invalid")
    if requested_effort != AUTO_REASONING_EFFORT:
        return validate_model_workflow_reasoning_effort(requested_effort)
    if verifier_kind == "none" or route not in {"adaptive-direct-goal", "adaptive-progressive-goal"}:
        return DEFAULT_MODEL_WORKFLOW_REASONING_EFFORT

    raw_task_text = task.strip().lower()
    if len(raw_task_text) > AUTO_SIMPLE_TASK_MAX_CHARS:
        return DEFAULT_MODEL_WORKFLOW_REASONING_EFFORT
    task_text = raw_task_text
    if policy == AUTO_EFFORT_POLICY_V1:
        markers = AUTO_COMPLEXITY_MARKERS_V1
    elif policy == AUTO_EFFORT_POLICY_V2:
        markers = AUTO_COMPLEXITY_MARKERS_V2
    elif policy == AUTO_EFFORT_POLICY_V3:
        markers = AUTO_COMPLEXITY_MARKERS_V3
        task_text = _remove_low_risk_scope_controls(task_text)
    elif policy == AUTO_EFFORT_POLICY_V4:
        markers = AUTO_COMPLEXITY_MARKERS_V4
        task_text = _remove_low_risk_scope_controls(task_text)
    else:
        markers = AUTO_COMPLEXITY_MARKERS
        task_text = _remove_low_risk_scope_controls(task_text)
    if any(_contains_effort_marker(task_text, marker) for marker in markers):
        return DEFAULT_MODEL_WORKFLOW_REASONING_EFFORT
    if policy == AUTO_EFFORT_POLICY and _is_tiny_verified_task(raw_task_text, task_text):
        return AUTO_TINY_VERIFIED_EFFORT
    return AUTO_FAST_VERIFIED_EFFORT


def _remove_low_risk_scope_controls(task_text: str) -> str:
    for phrase in AUTO_LOW_RISK_SCOPE_CONTROLS:
        task_text = re.sub(
            r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(phrase),
            " ",
            task_text,
        )
    return task_text


def _contains_effort_marker(task_text: str, marker: str) -> bool:
    return re.search(
        r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(marker),
        task_text,
    ) is not None


def _is_tiny_verified_task(raw_task_text: str, normalized_task_text: str) -> bool:
    if len(raw_task_text) > AUTO_TINY_VERIFIED_TASK_MAX_CHARS:
        return False
    if any(_contains_effort_marker(normalized_task_text, marker) for marker in AUTO_TINY_TASK_BLOCKERS):
        return False
    single_function_repair = (
        _contains_effort_marker(normalized_task_text, "the public function signature")
        and _contains_effort_marker(normalized_task_text, "smallest maintainable change")
        and not _contains_effort_marker(normalized_task_text, "both public function signatures")
    )
    focused_test_addition = (
        AUTO_FOCUSED_TEST_TARGET.search(normalized_task_text) is not None
        and _contains_effort_marker(normalized_task_text, "keep the tests deterministic")
        and any(phrase in raw_task_text for phrase in AUTO_LOW_RISK_SCOPE_CONTROLS)
    )
    return single_function_repair or focused_test_addition
