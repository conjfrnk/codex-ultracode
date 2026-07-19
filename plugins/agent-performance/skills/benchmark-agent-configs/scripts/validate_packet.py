#!/usr/bin/env python3
"""Validate benchmark packet structure and audience-bound cross-record policy."""

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path


OUTCOMES = [
    "candidate_x_wins",
    "candidate_y_wins",
    "tie",
    "neither_acceptable",
    "inconclusive",
]
REPORT_OUTCOME_KEYS = ["candidate_wins", "tie", "neither_acceptable", "inconclusive"]
CLAIM_TYPES = {"superiority", "non_inferiority", "equivalence", "descriptive"}
ABSOLUTE_STATES = {"acceptable", "unacceptable", "unknown"}
RUN_STATUSES = {
    "not_run",
    "completed",
    "candidate_failure",
    "timeout",
    "over_budget",
    "infrastructure_failure",
}
FAILURE_KEYS = {
    "candidate_failure",
    "timeout",
    "over_budget",
    "infrastructure_failure",
}
CURATION_CHECKS = {
    "clear",
    "solvable",
    "stable",
    "treatment_neutral",
    "leak_free",
    "safe_and_licensed",
    "judgeable",
}
SHARED_CONDITION_FIELDS = {
    "platform_image",
    "tools",
    "network",
    "cache_state",
    "human_intervention",
}
TASK_CONDITION_FIELDS = {"fixture_revision", "permissions", "budget"}
PUBLIC_BUDGET_FIELDS = {"max_minutes", "max_tokens", "max_agents", "max_cost_usd"}
TEMPLATE_FILES = {
    "controller": "controller-manifest.json",
    "public_task": "public-task.json",
    "evaluator": "evaluator-brief.json",
    "assignment": "label-assignments.json",
    "curation": "curation-record.json",
    "run": "run-record.json",
    "judgment": "judgment-record.json",
    "adjudication": "adjudication-record.json",
    "report": "benchmark-report.json",
}
SCHEMAS = {
    "controller": "agent-performance.comparison-control.v4",
    "public_task": "agent-performance.public-task.v2",
    "evaluator": "agent-performance.evaluator-brief.v2",
    "assignment": "agent-performance.label-assignments.v1",
    "curation": "agent-performance.curation-record.v1",
    "run": "agent-performance.run-record.v1",
    "judgment": "agent-performance.judgment-record.v1",
    "adjudication": "agent-performance.adjudication-record.v1",
    "report": "agent-performance.benchmark-report.v2",
}
REQUIRED_FIELDS = {
    "controller": {
        "schema",
        "audience",
        "comparison",
        "candidates",
        "treatment_variables",
        "matched_conditions",
        "tasks",
        "execution",
        "blinding",
        "judging",
        "analysis",
        "known_confounds",
    },
    "public_task": {
        "schema",
        "audience",
        "id",
        "version",
        "task_class",
        "difficulty",
        "tags",
        "fixture",
        "prompt",
        "acceptance_criteria",
        "required_artifacts",
        "permissions",
        "forbidden_actions",
        "terminal_condition",
        "budget",
    },
    "evaluator": {
        "schema",
        "audience",
        "task_id",
        "task_version",
        "public_task_sha256",
        "rubric_version",
        "intent",
        "acceptance_rule",
        "private_checks",
        "anti_solutions",
        "safety_vetoes",
        "outcome_states",
        "decision_rule",
        "rubric_dimensions",
        "evidence_requirements",
        "leakage_checks",
    },
    "assignment": {
        "schema",
        "audience",
        "comparison_id",
        "assignment_seed",
        "assignments",
    },
    "curation": {
        "schema",
        "audience",
        "task_id",
        "task_version",
        "reviewer_id",
        "public_task_sha256",
        "evaluator_brief_sha256",
        "verdict",
        "checks",
        "findings",
        "accepted_at",
    },
    "run": {
        "schema",
        "audience",
        "comparison_id",
        "task_id",
        "task_version",
        "candidate_id",
        "blind_label",
        "replicate",
        "analysis_unit",
        "status",
        "matched_conditions_observed",
        "condition_deviations",
        "artifacts",
        "verification",
        "failure",
        "incidents",
        "resources",
        "retry_of",
        "invalidates",
    },
    "judgment": {
        "schema",
        "audience",
        "comparison_id",
        "task_id",
        "task_version",
        "judgment_id",
        "judge_id",
        "rubric_version",
        "packet_hash",
        "independence_attestation",
        "identity_visible",
        "absolute_acceptability",
        "outcome",
        "findings",
        "vetoes",
        "process_findings",
        "missing_evidence",
        "confidence",
    },
    "adjudication": {
        "schema",
        "audience",
        "comparison_id",
        "task_id",
        "task_version",
        "adjudicator_id",
        "input_judgment_ids",
        "rubric_version",
        "finding_dispositions",
        "absolute_acceptability",
        "final_outcome",
        "vetoes",
        "unresolved_disagreements",
        "eligible_for_aggregation",
        "confidence",
    },
    "report": {
        "schema",
        "audience",
        "comparison_id",
        "comparison_version",
        "claim",
        "denominators",
        "paired_outcomes",
        "task_class_breakdown",
        "replicate_disagreement_count",
        "uncertainty",
        "safety_vetoes",
        "category_regressions",
        "resources",
        "process_findings",
        "harness_findings",
        "exclusions",
        "known_confounds",
        "claim_eligibility",
        "conclusion",
        "limitations",
    },
}
PUBLIC_FORBIDDEN_KEYS = {
    "assignment_record",
    "candidate_id",
    "evaluator_brief",
    "generation_rationale",
    "gold",
    "identity_map",
    "private_checks",
    "provenance",
    "source",
    "task_origin",
}
MAX_JSON_BYTES = 2 * 1024 * 1024


class DuplicateKeyError(ValueError):
    pass


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError("duplicate key %r" % key)
        result[key] = value
    return result


def _read_object(path: Path, label: str, errors: list[str]):
    try:
        if not path.is_file() or path.is_symlink():
            errors.append("%s must be a regular non-symlink file: %s" % (label, path))
            return None, None
        raw = path.read_bytes()
        if len(raw) > MAX_JSON_BYTES:
            errors.append("%s exceeds %d bytes" % (label, MAX_JSON_BYTES))
            return None, None
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateKeyError) as exc:
        errors.append("%s is not valid JSON: %s" % (label, exc))
        return None, None
    if not isinstance(value, dict):
        errors.append("%s must contain a JSON object" % label)
        return None, None
    return value, raw


def _path(value, keys, label, errors, expected_type=None):
    current = value
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            errors.append("%s is missing %s" % (label, ".".join(keys)))
            return None
        current = current[key]
    if expected_type is not None and not isinstance(current, expected_type):
        errors.append("%s field %s has the wrong type" % (label, ".".join(keys)))
        return None
    return current


def _non_empty_string(value, field, errors):
    valid = isinstance(value, str) and bool(value.strip())
    if not valid:
        errors.append("%s must be a non-empty string" % field)
        return False
    return True


def _require_keys(value, required, label, errors):
    if not isinstance(value, dict):
        errors.append("%s must be an object" % label)
        return False
    missing = sorted(set(required).difference(value))
    if missing:
        errors.append("%s is missing required fields: %s" % (label, ", ".join(missing)))
        return False
    return True


def _find_placeholders(value, path="$"):
    found = []
    if isinstance(value, str) and "replace-me" in value.lower():
        found.append(path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_find_placeholders(item, "%s[%d]" % (path, index)))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_find_placeholders(item, "%s.%s" % (path, key)))
    return found


def _find_forbidden_keys(value, forbidden, path="$"):
    found = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_find_forbidden_keys(item, forbidden, "%s[%d]" % (path, index)))
    elif isinstance(value, dict):
        for key, item in value.items():
            child = "%s.%s" % (path, key)
            if key in forbidden:
                found.append(child)
            found.extend(_find_forbidden_keys(item, forbidden, child))
    return found


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _is_finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_nonnegative_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_number(value, field, errors, minimum=None, strict_minimum=False):
    if not _is_finite_number(value):
        errors.append("%s must be a finite number" % field)
        return False
    if minimum is not None:
        outside = value <= minimum if strict_minimum else value < minimum
        if outside:
            relation = "greater than" if strict_minimum else "at least"
            errors.append("%s must be %s %s" % (field, relation, minimum))
            return False
    return True


def _validate_sha256(value, field, errors):
    valid = (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
    if not valid:
        errors.append("%s must be a lowercase SHA-256 digest" % field)
    return valid


def _validate_task_identity(record, label, comparison_id, task_id, task_version, errors):
    if "comparison_id" in record and record.get("comparison_id") != comparison_id:
        errors.append("%s comparison_id must match the controller" % label)
    if record.get("task_id") != task_id or record.get("task_version") != task_version:
        errors.append("%s task_id and task_version must match the public task" % label)


def _validate_absolute_decision(record, outcome_field, label, errors):
    absolute = record.get("absolute_acceptability")
    if not isinstance(absolute, dict) or set(absolute) != {"X", "Y"}:
        errors.append("%s absolute_acceptability must contain X and Y" % label)
        return
    for candidate, state in absolute.items():
        if not isinstance(state, str) or state not in ABSOLUTE_STATES:
            errors.append("%s absolute acceptability for %s is invalid" % (label, candidate))

    vetoes = record.get("vetoes")
    if not isinstance(vetoes, dict) or set(vetoes) != {"X", "Y"}:
        errors.append("%s vetoes must contain X and Y lists" % label)
        vetoes = {}
    else:
        for candidate, entries in vetoes.items():
            if not isinstance(entries, list) or not all(
                isinstance(entry, str) and entry.strip() for entry in entries
            ):
                errors.append("%s vetoes for %s must be a string list" % (label, candidate))
            if entries and absolute.get(candidate) != "unacceptable":
                errors.append("%s vetoes must make %s unacceptable" % (label, candidate))

    outcome = record.get(outcome_field)
    if outcome != "inconclusive" and any(state == "unknown" for state in absolute.values()):
        errors.append("%s decisive outcome cannot have unknown acceptability" % label)
    if outcome == "candidate_x_wins" and absolute.get("X") != "acceptable":
        errors.append("%s candidate_x_wins requires X to be acceptable" % label)
    elif outcome == "candidate_y_wins" and absolute.get("Y") != "acceptable":
        errors.append("%s candidate_y_wins requires Y to be acceptable" % label)
    elif outcome == "tie" and not all(state == "acceptable" for state in absolute.values()):
        errors.append("%s tie requires both candidates to be acceptable" % label)
    elif outcome == "neither_acceptable" and not all(
        state == "unacceptable" for state in absolute.values()
    ):
        errors.append("%s neither_acceptable requires both candidates to be unacceptable" % label)


def _veto_names(record):
    vetoes = record.get("vetoes") if isinstance(record, dict) else None
    if not isinstance(vetoes, dict):
        return []
    return [
        entry
        for entries in vetoes.values()
        if isinstance(entries, list)
        for entry in entries
        if isinstance(entry, str)
    ]


def _validate_schema(records, errors):
    for label, expected in SCHEMAS.items():
        value = records.get(label)
        if value is not None and value.get("schema") != expected:
            errors.append("%s schema must be %s" % (label, expected))


def _validate_required_fields(records, errors):
    for label, required in REQUIRED_FIELDS.items():
        value = records.get(label)
        if value is None:
            continue
        missing = sorted(required.difference(value))
        if missing:
            errors.append("%s is missing required fields: %s" % (label, ", ".join(missing)))


def _validate_controller(controller, errors):
    if controller.get("audience") != "controller-only":
        errors.append("controller audience must be controller-only")
    comparison = _path(controller, ["comparison"], "controller", errors, dict)
    candidates = _path(controller, ["candidates"], "controller", errors, list)
    execution = _path(controller, ["execution"], "controller", errors, dict)
    blinding = _path(controller, ["blinding"], "controller", errors, dict)
    analysis = _path(controller, ["analysis"], "controller", errors, dict)
    judging = _path(controller, ["judging"], "controller", errors, dict)
    tasks = _path(controller, ["tasks"], "controller", errors, list)
    if None in (comparison, candidates, execution, blinding, analysis, judging, tasks):
        return

    _require_keys(
        comparison,
        {
            "id",
            "version",
            "hypothesis",
            "task_classes",
            "primary_outcome",
            "claim",
            "acceptable_regressions",
            "safety_vetoes",
        },
        "controller comparison",
        errors,
    )
    _require_keys(
        execution,
        {
            "replicates_per_candidate_task",
            "analysis_unit",
            "replicate_aggregation",
            "failure_policy",
            "order_balancing",
            "retry_policy",
        },
        "controller execution",
        errors,
    )
    _require_keys(
        blinding,
        {
            "labels",
            "assignment_method",
            "assignment_seed",
            "assignment_record",
            "identity_map",
            "sanitize_identity_markers",
        },
        "controller blinding",
        errors,
    )
    _require_keys(
        judging,
        {
            "minimum_independent_judges",
            "adjudication_rule",
            "outcome_states",
            "result_is_scored_before_process",
        },
        "controller judging",
        errors,
    )
    _require_keys(
        analysis,
        {
            "uncertainty_method",
            "interval_level",
            "ties",
            "inconclusive",
            "exclusions",
            "claim_gate",
        },
        "controller analysis",
        errors,
    )

    _non_empty_string(comparison.get("id"), "controller comparison id", errors)
    _non_empty_string(comparison.get("hypothesis"), "controller comparison hypothesis", errors)
    task_classes = comparison.get("task_classes")
    if not isinstance(task_classes, list) or not task_classes or not all(
        isinstance(task_class, str) and task_class.strip() for task_class in task_classes
    ):
        errors.append("controller comparison task_classes must be a non-empty string list")
    primary_outcome = comparison.get("primary_outcome")
    if not isinstance(primary_outcome, dict):
        errors.append("controller primary_outcome must be an object")
    else:
        _non_empty_string(primary_outcome.get("name"), "controller primary outcome name", errors)
        _non_empty_string(
            primary_outcome.get("definition"),
            "controller primary outcome definition",
            errors,
        )
    if not isinstance(comparison.get("acceptable_regressions"), list):
        errors.append("controller acceptable_regressions must be a list")
    comparison_version = comparison.get("version")
    if not isinstance(comparison_version, int) or isinstance(comparison_version, bool) or comparison_version < 1:
        errors.append("controller comparison version must be a positive integer")

    candidate_ids = []
    if len(candidates) != 2 or not all(isinstance(candidate, dict) for candidate in candidates):
        errors.append("controller candidates must contain exactly two candidate objects")
    else:
        candidate_ids = [candidate.get("candidate_id") for candidate in candidates]
        for candidate in candidates:
            candidate_id = candidate.get("candidate_id")
            _non_empty_string(candidate_id, "controller candidate_id", errors)
            _non_empty_string(candidate.get("system"), "controller candidate system", errors)
            _non_empty_string(
                candidate.get("config_revision"),
                "controller candidate config_revision",
                errors,
            )
            model = candidate.get("model")
            if not isinstance(model, dict):
                errors.append("controller candidate model must be an object")
            else:
                for field in ("provider", "name", "version", "effort"):
                    _non_empty_string(
                        model.get(field),
                        "controller candidate model %s" % field,
                        errors,
                    )
            if not isinstance(candidate.get("skills_or_plugins"), list):
                errors.append("controller candidate skills_or_plugins must be a list")
        valid_candidate_ids = [
            candidate_id
            for candidate_id in candidate_ids
            if isinstance(candidate_id, str) and candidate_id.strip()
        ]
        if len(valid_candidate_ids) == 2 and len(set(valid_candidate_ids)) != 2:
            errors.append("controller candidates must have unique candidate_id values")

    vetoes = comparison.get("safety_vetoes")
    if (
        not isinstance(vetoes, list)
        or not vetoes
        or not all(_non_empty_string(v, "safety veto", errors) for v in vetoes)
    ):
        errors.append("controller safety vetoes must be a non-empty string list")
    elif len(set(vetoes)) != len(vetoes):
        errors.append("controller safety vetoes must be unique")

    claim = comparison.get("claim")
    claim_type_value = claim.get("type") if isinstance(claim, dict) else None
    if not isinstance(claim_type_value, str) or claim_type_value not in CLAIM_TYPES:
        errors.append("controller claim type is invalid")
    else:
        _require_keys(
            claim,
            {
                "type",
                "estimand",
                "estimator",
                "direction",
                "target_candidate_id",
                "comparator_candidate_id",
                "minimum_effect",
                "non_inferiority_margin",
                "equivalence_margin",
            },
            "controller claim",
            errors,
        )
        _non_empty_string(claim.get("estimand"), "controller claim estimand", errors)
        if claim.get("estimator") != "paired_win_rate_difference":
            errors.append("controller claim estimator must be paired_win_rate_difference")
        claim_type = claim["type"]
        target_id = claim.get("target_candidate_id")
        comparator_id = claim.get("comparator_candidate_id")
        _non_empty_string(target_id, "controller claim target_candidate_id", errors)
        _non_empty_string(comparator_id, "controller claim comparator_candidate_id", errors)
        if target_id == comparator_id:
            errors.append("controller claim target and comparator candidates must differ")
        valid_candidate_ids = {
            candidate_id
            for candidate_id in candidate_ids
            if isinstance(candidate_id, str) and candidate_id.strip()
        }
        if (
            isinstance(target_id, str)
            and isinstance(comparator_id, str)
            and {target_id, comparator_id} != valid_candidate_ids
        ):
            errors.append("controller claim target and comparator must name both candidates")
        if claim.get("direction") != "higher_is_better":
            errors.append("controller claim direction must describe target minus comparator")
        if claim_type == "superiority":
            _validate_number(
                claim.get("minimum_effect"),
                "superiority minimum_effect",
                errors,
                minimum=0,
            )
        elif claim_type == "non_inferiority":
            _validate_number(
                claim.get("non_inferiority_margin"),
                "non_inferiority non_inferiority_margin",
                errors,
                minimum=0,
            )
        elif claim_type == "equivalence":
            _validate_number(
                claim.get("equivalence_margin"),
                "equivalence equivalence_margin",
                errors,
                minimum=0,
                strict_minimum=True,
            )

    treatment_variables = controller.get("treatment_variables")
    if not isinstance(treatment_variables, list) or not treatment_variables or not all(
        isinstance(value, str) and value.strip() for value in treatment_variables
    ):
        errors.append("controller treatment_variables must be a non-empty string list")
    matched_conditions = controller.get("matched_conditions")
    if not isinstance(matched_conditions, dict):
        errors.append("controller matched_conditions must be an object")
    elif not SHARED_CONDITION_FIELDS.issubset(matched_conditions):
        errors.append("controller matched_conditions must contain every shared condition")
    else:
        for field in ("platform_image", "network", "cache_state", "human_intervention"):
            _non_empty_string(
                matched_conditions.get(field),
                "controller matched condition %s" % field,
                errors,
            )
        if not isinstance(matched_conditions.get("tools"), list):
            errors.append("controller matched condition tools must be a list")
    if not isinstance(controller.get("known_confounds"), list):
        errors.append("controller known_confounds must be a list")

    replicates = execution.get("replicates_per_candidate_task")
    valid_replicates = (
        isinstance(replicates, int) and not isinstance(replicates, bool) and replicates >= 1
    )
    if not valid_replicates:
        errors.append("replicates_per_candidate_task must be a positive integer")
    if execution.get("analysis_unit") != "task":
        errors.append("execution analysis_unit must be task")
    if valid_replicates and replicates > 1:
        _non_empty_string(execution.get("replicate_aggregation"), "replicate_aggregation", errors)
    failure_policy = execution.get("failure_policy")
    if not isinstance(failure_policy, dict) or set(failure_policy) != FAILURE_KEYS:
        errors.append("failure policy must cover candidate failure, timeout, over budget, and infrastructure failure")
    elif not all(_non_empty_string(value, "failure policy value", errors) for value in failure_policy.values()):
        errors.append("failure policy values must be non-empty")

    if blinding.get("labels") != ["X", "Y"]:
        errors.append("blinding labels must be X and Y")
    _non_empty_string(blinding.get("assignment_method"), "blinding assignment method", errors)
    _non_empty_string(blinding.get("assignment_seed"), "blinding assignment seed", errors)
    _non_empty_string(blinding.get("assignment_record"), "blinding assignment record", errors)
    _non_empty_string(blinding.get("identity_map"), "blinding identity map", errors)
    if not blinding.get("sanitize_identity_markers"):
        errors.append("blinding must sanitize identity markers")

    if judging.get("outcome_states") != OUTCOMES:
        errors.append("controller outcome states are invalid")
    minimum_judges = judging.get("minimum_independent_judges")
    if not isinstance(minimum_judges, int) or isinstance(minimum_judges, bool) or minimum_judges < 1:
        errors.append("controller minimum_independent_judges must be a positive integer")
    _non_empty_string(judging.get("adjudication_rule"), "controller adjudication rule", errors)
    if not judging.get("result_is_scored_before_process"):
        errors.append("controller must score result before process")
    _non_empty_string(analysis.get("uncertainty_method"), "analysis uncertainty method", errors)
    interval_level = analysis.get("interval_level")
    if not _is_finite_number(interval_level) or not 0 < interval_level < 1:
        errors.append("analysis interval_level must be a finite number between zero and one")
    _non_empty_string(analysis.get("claim_gate"), "analysis claim gate", errors)
    for field in ("ties", "inconclusive", "exclusions"):
        _non_empty_string(analysis.get(field), "analysis %s" % field, errors)
    order_balancing = execution.get("order_balancing")
    if not isinstance(order_balancing, dict):
        errors.append("controller order_balancing must be an object")
    else:
        _non_empty_string(order_balancing.get("method"), "order balancing method", errors)
        _non_empty_string(order_balancing.get("seed"), "order balancing seed", errors)
    _non_empty_string(execution.get("retry_policy"), "controller retry_policy", errors)
    if not tasks or not all(isinstance(task, dict) for task in tasks):
        errors.append("controller tasks must contain at least one object")
    else:
        identities = []
        for task in tasks:
            task_id = task.get("id")
            version = task.get("version")
            _non_empty_string(task_id, "controller task id", errors)
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                errors.append("controller task version must be a positive integer")
            if (
                isinstance(task_id, str)
                and task_id.strip()
                and isinstance(version, int)
                and not isinstance(version, bool)
                and version >= 1
            ):
                identities.append((task_id, version))
            task_conditions = task.get("matched_conditions")
            if not isinstance(task_conditions, dict) or not TASK_CONDITION_FIELDS.issubset(
                task_conditions
            ):
                errors.append("controller task must contain every task-level matched condition")
            else:
                _non_empty_string(
                    task_conditions.get("fixture_revision"),
                    "controller task fixture_revision",
                    errors,
                )
                if not isinstance(task_conditions.get("permissions"), list):
                    errors.append("controller task permissions must be a list")
                task_budget = task_conditions.get("budget")
                if not isinstance(task_budget, dict) or set(task_budget) != PUBLIC_BUDGET_FIELDS:
                    errors.append("controller task budget must contain every budget limit")
                else:
                    for field in PUBLIC_BUDGET_FIELDS:
                        if not _is_finite_number(task_budget.get(field)) or task_budget[field] < 0:
                            errors.append("controller task budget %s must be nonnegative" % field)
        if len(set(identities)) != len(identities):
            errors.append("controller tasks must have unique id and version pairs")


def _validate_public_task(public_task, errors):
    if public_task.get("audience") != "candidate":
        errors.append("public task audience must be candidate")
    leaked = _find_forbidden_keys(public_task, PUBLIC_FORBIDDEN_KEYS)
    if leaked:
        errors.append("public task contains private or origin fields: %s" % ", ".join(leaked))
    _non_empty_string(public_task.get("id"), "public task id", errors)
    _non_empty_string(public_task.get("task_class"), "public task task_class", errors)
    _non_empty_string(public_task.get("difficulty"), "public task difficulty", errors)
    _non_empty_string(public_task.get("prompt"), "public task prompt", errors)
    _non_empty_string(public_task.get("terminal_condition"), "public task terminal_condition", errors)
    version = public_task.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("public task version must be a positive integer")
    criteria = public_task.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria or not all(
        isinstance(criterion, str) and criterion.strip() for criterion in criteria
    ):
        errors.append("public task acceptance_criteria must be a non-empty string list")
    tags = public_task.get("tags")
    if not isinstance(tags, list) or not tags or not all(
        isinstance(tag, str) and tag.strip() for tag in tags
    ):
        errors.append("public task tags must be a non-empty string list")
    required_artifacts = public_task.get("required_artifacts")
    if not isinstance(required_artifacts, list) or not required_artifacts or not all(
        isinstance(artifact, str) and artifact.strip() for artifact in required_artifacts
    ):
        errors.append("public task required_artifacts must be a non-empty string list")
    elif len(set(required_artifacts)) != len(required_artifacts):
        errors.append("public task required_artifacts must be unique")
    for field in ("permissions", "forbidden_actions"):
        values = public_task.get(field)
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value.strip() for value in values
        ):
            errors.append("public task %s must be a string list" % field)
    fixture = public_task.get("fixture")
    if not isinstance(fixture, dict):
        errors.append("public task fixture must be an object")
    elif set(fixture) != {"base_revision", "setup"}:
        errors.append("public task fixture must bind base_revision and setup")
    else:
        _non_empty_string(fixture.get("base_revision"), "public fixture base_revision", errors)
        _non_empty_string(fixture.get("setup"), "public fixture setup", errors)
    budget = public_task.get("budget")
    if not isinstance(budget, dict):
        errors.append("public task budget must be an object")
    elif set(budget) != PUBLIC_BUDGET_FIELDS:
        errors.append("public task budget must contain every budget limit")
    else:
        for field in PUBLIC_BUDGET_FIELDS:
            if not _is_finite_number(budget.get(field)) or budget[field] < 0:
                errors.append("public task budget %s must be nonnegative" % field)


def _validate_evaluator(controller, evaluator, errors):
    if evaluator.get("audience") != "evaluator-only":
        errors.append("evaluator audience must be evaluator-only")
    leaked = _find_forbidden_keys(evaluator, {"candidate_id", "identity_map", "assignment_record"})
    if leaked:
        errors.append("evaluator contains controller identity fields: %s" % ", ".join(leaked))
    comparison = controller.get("comparison")
    controller_vetoes = comparison.get("safety_vetoes") if isinstance(comparison, dict) else None
    if evaluator.get("safety_vetoes") != controller_vetoes:
        errors.append("evaluator safety vetoes must exactly match controller safety vetoes")
    if evaluator.get("outcome_states") != OUTCOMES:
        errors.append("evaluator outcome states are invalid")
    _non_empty_string(evaluator.get("rubric_version"), "evaluator rubric_version", errors)
    _non_empty_string(evaluator.get("intent"), "evaluator intent", errors)
    acceptance = evaluator.get("acceptance_rule")
    if not isinstance(acceptance, dict):
        errors.append("evaluator acceptance rule must be an object")
    else:
        _non_empty_string(acceptance.get("summary"), "evaluator acceptance summary", errors)
        checks = acceptance.get("required_checks")
        if not isinstance(checks, list) or not checks or not all(
            isinstance(check, str) and check.strip() for check in checks
        ):
            errors.append("evaluator acceptance rule requires a non-empty string check list")
    decision = evaluator.get("decision_rule")
    if not isinstance(decision, dict):
        errors.append("evaluator decision rule must be an object")
    else:
        for key in (
            "record_absolute_acceptability_first",
            "vetoes_are_non_compensatory",
            "result_is_scored_before_process",
        ):
            if decision.get(key) is not True:
                errors.append("evaluator decision rule must set %s" % key)
    for field in ("private_checks", "rubric_dimensions", "leakage_checks"):
        values = evaluator.get(field)
        if not isinstance(values, list) or not values or not all(
            isinstance(value, str) and value.strip() for value in values
        ):
            errors.append("evaluator %s must be a non-empty string list" % field)
    evidence_requirements = evaluator.get("evidence_requirements")
    required_evidence_flags = {
        "cite_artifact_locations",
        "record_counterevidence",
        "separate_process_findings",
    }
    if (
        not isinstance(evidence_requirements, dict)
        or not required_evidence_flags.issubset(evidence_requirements)
        or not all(evidence_requirements.get(key) is True for key in required_evidence_flags)
    ):
        errors.append("evaluator evidence_requirements must enable every evidence flag")


def _validate_task_binding(controller, public_task, evaluator, raw, template_mode, errors):
    public_id = public_task.get("id")
    public_version = public_task.get("version")
    if evaluator.get("task_id") != public_id or evaluator.get("task_version") != public_version:
        errors.append("public task and evaluator identity must match")
    tasks = controller.get("tasks", [])
    task = next(
        (
            item
            for item in tasks
            if isinstance(item, dict)
            and item.get("id") == public_id
            and item.get("version") == public_version
        ),
        None,
    )
    if task is None:
        errors.append("controller must bind the public task id and version")
        return None
    if evaluator.get("public_task_sha256") != task.get("public_task_sha256"):
        errors.append("controller and evaluator public-task hashes must match")
    task_conditions = task.get("matched_conditions", {})
    public_budget = public_task.get("budget", {})
    frozen_budget = task_conditions.get("budget", {}) if isinstance(task_conditions, dict) else {}
    if isinstance(frozen_budget, dict) and isinstance(public_budget, dict):
        for field in PUBLIC_BUDGET_FIELDS:
            if public_budget.get(field) != frozen_budget.get(field):
                errors.append("public task budget %s must match its controller task" % field)
    public_fixture = public_task.get("fixture", {})
    if (
        isinstance(public_fixture, dict)
        and isinstance(task_conditions, dict)
        and public_fixture.get("base_revision") != task_conditions.get("fixture_revision")
    ):
        errors.append("public task fixture base_revision must match its controller task")
    if public_task.get("permissions") != task_conditions.get("permissions"):
        errors.append("public task permissions must match its controller task")
    if not template_mode:
        public_hash = _sha256(raw["public_task"])
        evaluator_hash = _sha256(raw["evaluator"])
        for field in (
            "curation_record_sha256",
            "public_task_sha256",
            "evaluator_brief_sha256",
            "fixture_sha256",
        ):
            _validate_sha256(task.get(field), "controller task %s" % field, errors)
        if task.get("public_task_sha256") != public_hash:
            errors.append("controller public_task_sha256 does not match the public task bytes")
        if evaluator.get("public_task_sha256") != public_hash:
            errors.append("evaluator public_task_sha256 does not match the public task bytes")
        if task.get("evaluator_brief_sha256") != evaluator_hash:
            errors.append("controller evaluator_brief_sha256 does not match evaluator bytes")
    return task


def _validate_optional_records(
    records,
    raw,
    controller,
    public_task,
    evaluator,
    task,
    template_mode,
    errors,
):
    comparison = controller.get("comparison", {})
    comparison_id = comparison.get("id")
    comparison_version = comparison.get("version")
    frozen_vetoes = comparison.get("safety_vetoes", [])
    task_id = public_task.get("id")
    task_version = public_task.get("version")
    downstream_present = any(
        records.get(label) is not None
        for label in ("run", "judgment", "adjudication", "report")
    )
    conditions_drifted = False
    selected_assignment = None

    assignment_record = records.get("assignment")
    if assignment_record is not None:
        if assignment_record.get("audience") != "controller-only":
            errors.append("label assignment audience must be controller-only")
        if assignment_record.get("comparison_id") != comparison_id:
            errors.append("label assignment comparison_id must match the controller")
        if assignment_record.get("assignment_seed") != controller.get("blinding", {}).get(
            "assignment_seed"
        ):
            errors.append("label assignment seed must match the controller")
        assignments = assignment_record.get("assignments")
        if not isinstance(assignments, list) or not assignments or not all(
            isinstance(assignment, dict) for assignment in assignments
        ):
            errors.append("label assignments must contain task assignment objects")
        else:
            controller_tasks = {
                (controller_task.get("id"), controller_task.get("version"))
                for controller_task in controller.get("tasks", [])
            }
            assignment_tasks = []
            candidate_ids = {
                candidate.get("candidate_id") for candidate in controller.get("candidates", [])
            }
            for assignment in assignments:
                assignment_task_id = assignment.get("task_id")
                assignment_task_version = assignment.get("task_version")
                valid_identity = (
                    isinstance(assignment_task_id, str)
                    and assignment_task_id.strip()
                    and isinstance(assignment_task_version, int)
                    and not isinstance(assignment_task_version, bool)
                    and assignment_task_version >= 1
                )
                identity = (assignment_task_id, assignment_task_version) if valid_identity else None
                if identity is None or identity not in controller_tasks:
                    errors.append("label assignment task must exist in the controller")
                if identity is not None:
                    assignment_tasks.append(identity)
                labels = assignment.get("labels")
                if (
                    not isinstance(labels, dict)
                    or list(labels) != ["X", "Y"]
                    or not all(isinstance(value, str) for value in labels.values())
                    or set(labels.values()) != candidate_ids
                ):
                    errors.append(
                        "label assignment X and Y must map exactly to controller candidate IDs"
                    )
                if identity == (task_id, task_version):
                    selected_assignment = assignment
            if len(set(assignment_tasks)) != len(assignment_tasks):
                errors.append("label assignments must have unique task identities")
            if set(assignment_tasks) != controller_tasks:
                errors.append("label assignments must cover every controller task")
            if selected_assignment is None:
                errors.append("label assignments must include the selected public task")

    curation = records.get("curation")
    if curation is not None:
        if curation.get("audience") != "controller-only":
            errors.append("curation audience must be controller-only")
        verdict = curation.get("verdict")
        if not isinstance(verdict, str) or verdict not in {"accept", "revise", "reject"}:
            errors.append("curation verdict is invalid")
        if not template_mode:
            _validate_task_identity(
                curation,
                "curation",
                comparison_id,
                task_id,
                task_version,
                errors,
            )
            public_hash = _sha256(raw["public_task"])
            evaluator_hash = _sha256(raw["evaluator"])
            curation_hash = _sha256(raw["curation"])
            if curation.get("public_task_sha256") != public_hash:
                errors.append("curation public_task_sha256 does not match the public task bytes")
            if curation.get("evaluator_brief_sha256") != evaluator_hash:
                errors.append("curation evaluator_brief_sha256 does not match evaluator bytes")
            if task is not None and task.get("curation_record_sha256") != curation_hash:
                errors.append("controller curation_record_sha256 does not match curation bytes")
            if downstream_present and curation.get("verdict") != "accept":
                errors.append("curation verdict must be accept before downstream records exist")
        if curation.get("verdict") == "accept":
            checks = curation.get("checks")
            if not isinstance(checks, dict) or set(checks) != CURATION_CHECKS:
                errors.append("accepted curation must contain every required curation check")
            elif not all(value is True for value in checks.values()):
                errors.append("accepted curation must have every required check pass")
            _non_empty_string(curation.get("accepted_at"), "accepted curation accepted_at", errors)

    run = records.get("run")
    if run is not None:
        if run.get("audience") != "controller-only":
            errors.append("run audience must be controller-only")
        if run.get("analysis_unit") != "candidate_task_replicate":
            errors.append("run analysis_unit must be candidate_task_replicate")
        blind_label = run.get("blind_label")
        if not isinstance(blind_label, str) or blind_label not in {"X", "Y"}:
            errors.append("run blind_label must be X or Y")
        replicate = run.get("replicate")
        if not isinstance(replicate, int) or isinstance(replicate, bool) or replicate < 1:
            errors.append("run replicate must be a positive integer")
        else:
            declared_replicates = controller.get("execution", {}).get(
                "replicates_per_candidate_task"
            )
            if (
                isinstance(declared_replicates, int)
                and not isinstance(declared_replicates, bool)
                and replicate > declared_replicates
            ):
                errors.append("run replicate exceeds the controller replicate count")
        status = run.get("status")
        if not isinstance(status, str) or status not in RUN_STATUSES:
            errors.append("run status is invalid")
        artifacts = run.get("artifacts")
        if not isinstance(artifacts, dict):
            errors.append("run artifacts must be an object")
            artifacts = {}
        verification = run.get("verification")
        if not isinstance(verification, dict):
            errors.append("run verification must be an object")
            verification = {}
        commands = verification.get("commands")
        exit_statuses = verification.get("exit_statuses")
        warnings_or_skips = verification.get("warnings_or_skips")
        if not isinstance(commands, list) or not all(
            isinstance(command, str) and command.strip() for command in commands
        ):
            errors.append("run verification commands must be a string list")
            commands = []
        if not isinstance(exit_statuses, list) or not all(
            isinstance(code, int) and not isinstance(code, bool) for code in exit_statuses
        ):
            errors.append("run verification exit_statuses must be an integer list")
            exit_statuses = []
        if len(commands) != len(exit_statuses):
            errors.append("run verification commands and exit_statuses must have equal length")
        if not isinstance(warnings_or_skips, list) or not all(
            isinstance(message, str) and message.strip() for message in warnings_or_skips
        ):
            errors.append("run verification warnings_or_skips must be a string list")
            warnings_or_skips = []
        if status == "completed":
            required_artifacts = public_task.get("required_artifacts", [])
            if isinstance(required_artifacts, list):
                missing_artifacts = []
                for artifact in required_artifacts:
                    value = artifacts.get(artifact) if isinstance(artifact, str) else None
                    if value is None or value == "" or value == [] or value == {}:
                        missing_artifacts.append(str(artifact))
                if missing_artifacts:
                    errors.append(
                        "completed run is missing required artifacts: %s"
                        % ", ".join(missing_artifacts)
                    )
            if not commands and not warnings_or_skips:
                errors.append(
                    "completed run must record verification commands or explain their absence"
                )
            if run.get("failure") is not None:
                errors.append("completed run must not retain a failure record")
        elif (
            isinstance(status, str)
            and status in RUN_STATUSES.difference({"not_run", "completed"})
            and run.get("failure") is None
        ):
            errors.append("failed run status requires a failure record")
        observed_conditions = run.get("matched_conditions_observed")
        if not isinstance(observed_conditions, dict):
            errors.append("run matched_conditions_observed must be an object")
        elif not template_mode and status != "not_run":
            frozen_conditions = {
                "shared": controller.get("matched_conditions", {}),
                "task": task.get("matched_conditions", {}) if isinstance(task, dict) else {},
            }
            if set(observed_conditions) != {"shared", "task"}:
                errors.append("run must separate shared and task-level observed conditions")
                conditions_drifted = True
            elif observed_conditions != frozen_conditions:
                conditions_drifted = True
        deviations = run.get("condition_deviations")
        if not isinstance(deviations, list) or not all(
            isinstance(deviation, str) and deviation.strip() for deviation in deviations
        ):
            errors.append("run condition_deviations must be a string list")
            deviations = []
        if conditions_drifted and not deviations:
            errors.append("run matched-condition drift requires a recorded deviation")
        incidents = run.get("incidents")
        if not isinstance(incidents, list):
            errors.append("run incidents must be a list")
        if not isinstance(run.get("resources"), dict):
            errors.append("run resources must be an object")
        if not template_mode:
            _validate_task_identity(
                run,
                "run",
                comparison_id,
                task_id,
                task_version,
                errors,
            )
            candidate_ids = {
                candidate.get("candidate_id")
                for candidate in controller.get("candidates", [])
                if isinstance(candidate, dict) and isinstance(candidate.get("candidate_id"), str)
            }
            if run.get("candidate_id") not in candidate_ids:
                errors.append("run candidate_id must name a controller candidate")
            if selected_assignment is not None:
                labels = selected_assignment.get("labels", {})
                if labels.get(run.get("blind_label")) != run.get("candidate_id"):
                    errors.append("run candidate_id and blind_label must match the task assignment")

    judgment = records.get("judgment")
    if judgment is not None:
        if judgment.get("audience") != "judge-output":
            errors.append("judgment audience must be judge-output")
        _non_empty_string(judgment.get("judgment_id"), "judgment judgment_id", errors)
        if judgment.get("outcome") not in OUTCOMES:
            errors.append("judgment outcome is invalid")
        if judgment.get("independence_attestation") is not True:
            errors.append("judgment must attest independent review")
        if judgment.get("identity_visible") is not False:
            errors.append("judge must not receive candidate identity")
        _validate_absolute_decision(judgment, "outcome", "judgment", errors)
        if any(veto not in frozen_vetoes for veto in _veto_names(judgment)):
            errors.append("judgment vetoes must use the frozen controller vetoes")
        if not template_mode:
            _validate_task_identity(
                judgment,
                "judgment",
                comparison_id,
                task_id,
                task_version,
                errors,
            )
            if judgment.get("rubric_version") != evaluator.get("rubric_version"):
                errors.append("judgment rubric_version must match the evaluator")
            _validate_sha256(judgment.get("packet_hash"), "judgment packet_hash", errors)

    adjudication = records.get("adjudication")
    if adjudication is not None:
        if adjudication.get("audience") != "adjudicator-output":
            errors.append("adjudication audience must be adjudicator-output")
        if adjudication.get("final_outcome") not in OUTCOMES:
            errors.append("adjudication outcome is invalid")
        _validate_absolute_decision(
            adjudication,
            "final_outcome",
            "adjudication",
            errors,
        )
        if any(veto not in frozen_vetoes for veto in _veto_names(adjudication)):
            errors.append("adjudication vetoes must use the frozen controller vetoes")
        if not isinstance(adjudication.get("eligible_for_aggregation"), bool):
            errors.append("adjudication eligible_for_aggregation must be boolean")
        input_ids = adjudication.get("input_judgment_ids")
        if not isinstance(input_ids, list) or not all(
            isinstance(item, str) and item.strip() for item in input_ids
        ):
            errors.append("adjudication input_judgment_ids must be a string list")
        elif not template_mode and not input_ids:
            errors.append("adjudication input_judgment_ids must name at least one judgment")
        elif isinstance(input_ids, list) and len(set(input_ids)) != len(input_ids):
            errors.append("adjudication input_judgment_ids must be unique")
        if not template_mode:
            _validate_task_identity(
                adjudication,
                "adjudication",
                comparison_id,
                task_id,
                task_version,
                errors,
            )
            if adjudication.get("rubric_version") != evaluator.get("rubric_version"):
                errors.append("adjudication rubric_version must match the evaluator")
            if judgment is not None and isinstance(input_ids, list):
                judgment_id = judgment.get("judgment_id")
                if judgment_id not in input_ids:
                    errors.append(
                        "adjudication input_judgment_ids must include the supplied judgment"
                    )
            minimum_judges = controller.get("judging", {}).get("minimum_independent_judges")
            if (
                isinstance(minimum_judges, int)
                and not isinstance(minimum_judges, bool)
                and isinstance(input_ids, list)
                and len(input_ids) < minimum_judges
            ):
                errors.append(
                    "adjudication input_judgment_ids must meet minimum_independent_judges"
                )
    report = records.get("report")
    if report is not None:
        candidate_ids = [
            candidate.get("candidate_id")
            for candidate in controller.get("candidates", [])
            if isinstance(candidate, dict)
        ]
        if report.get("audience") != "report":
            errors.append("report audience must be report")
        if report.get("comparison_id") != comparison_id:
            errors.append("report comparison_id must match the controller")
        if report.get("comparison_version") != comparison_version:
            errors.append("report comparison_version must match the controller")

        paired = report.get("paired_outcomes")
        if not isinstance(paired, dict) or list(paired) != REPORT_OUTCOME_KEYS:
            errors.append(
                "report paired_outcomes must contain candidate_wins, tie, "
                "neither_acceptable, and inconclusive in canonical order"
            )
            paired_valid = False
        else:
            candidate_wins = paired.get("candidate_wins")
            paired_valid = (
                isinstance(candidate_wins, dict)
                and list(candidate_wins) == candidate_ids
                and all(_is_nonnegative_integer(value) for value in candidate_wins.values())
                and all(
                    _is_nonnegative_integer(paired[outcome])
                    for outcome in ("tie", "neither_acceptable", "inconclusive")
                )
            )
            if not paired_valid:
                errors.append(
                    "report paired_outcomes must use controller candidate IDs and nonnegative integer counts"
                )

        denominators = report.get("denominators")
        denominator_keys = {
            "tasks_total",
            "tasks_valid",
            "candidate_runs",
            "invalid_matched_pairs",
        }
        denominators_valid = isinstance(denominators, dict) and all(
            _is_nonnegative_integer(denominators.get(key)) for key in denominator_keys
        )
        if not denominators_valid:
            errors.append("report denominators must contain nonnegative integer counts")
        elif paired_valid:
            paired_total = sum(paired["candidate_wins"].values()) + sum(
                paired[outcome] for outcome in ("tie", "neither_acceptable", "inconclusive")
            )
            if paired_total != denominators["tasks_valid"]:
                errors.append("report paired_outcomes must sum to tasks_valid")
            if not template_mode:
                if denominators["tasks_total"] != len(controller.get("tasks", [])):
                    errors.append("report tasks_total must match the controller task count")
                if (
                    denominators["tasks_valid"] + denominators["invalid_matched_pairs"]
                    != denominators["tasks_total"]
                ):
                    errors.append(
                        "report valid and invalid matched-pair counts must sum to tasks_total"
                    )
                replicate_count = controller.get("execution", {}).get(
                    "replicates_per_candidate_task"
                )
                if isinstance(replicate_count, int) and not isinstance(replicate_count, bool):
                    runs_per_task = len(controller.get("candidates", [])) * replicate_count
                    minimum_runs = denominators["tasks_valid"] * runs_per_task
                    maximum_runs = denominators["tasks_total"] * runs_per_task
                    if not minimum_runs <= denominators["candidate_runs"] <= maximum_runs:
                        errors.append(
                            "report candidate_runs must be bounded by valid and scheduled task attempts"
                        )

        report_claim = report.get("claim")
        controller_claim = comparison.get("claim", {})
        if not isinstance(report_claim, dict):
            errors.append("report claim must be an object")
            report_claim = {}
        if report_claim.get("type") != controller_claim.get("type"):
            errors.append("report claim type must match controller claim type")
        if report_claim.get("estimand") != controller_claim.get("estimand"):
            errors.append("report claim estimand must match controller claim estimand")
        if report_claim.get("estimator") != controller_claim.get("estimator"):
            errors.append("report claim estimator must match controller claim estimator")
        for field in ("target_candidate_id", "comparator_candidate_id"):
            if report_claim.get(field) != controller_claim.get(field):
                errors.append("report claim %s must match the controller claim" % field)
        claim_type = controller_claim.get("type")
        expected_margin = {
            "superiority": controller_claim.get("minimum_effect"),
            "non_inferiority": controller_claim.get("non_inferiority_margin"),
            "equivalence": controller_claim.get("equivalence_margin"),
            "descriptive": None,
        }.get(claim_type)
        if report_claim.get("margin") != expected_margin:
            errors.append("report claim margin must match the controller claim")

        safety_vetoes = report.get("safety_vetoes")
        by_candidate_id = safety_vetoes.get("by_candidate_id") if isinstance(safety_vetoes, dict) else None
        safety_valid = (
            isinstance(safety_vetoes, dict)
            and set(safety_vetoes) == {"total", "by_candidate_id"}
            and _is_nonnegative_integer(safety_vetoes.get("total"))
            and isinstance(by_candidate_id, dict)
            and list(by_candidate_id) == candidate_ids
            and all(_is_nonnegative_integer(value) for value in by_candidate_id.values())
            and safety_vetoes["total"] == sum(by_candidate_id.values())
        )
        if not safety_valid:
            errors.append(
                "report safety_vetoes must total nonnegative counts keyed by controller candidate IDs"
            )
        observed_veto = any(_veto_names(stage_record) for stage_record in (judgment, adjudication))
        if run is not None:
            incidents = run.get("incidents")
            if isinstance(incidents, list):
                for incident in incidents:
                    if isinstance(incident, str) and incident in frozen_vetoes:
                        observed_veto = True
                    elif isinstance(incident, dict) and any(
                        incident.get(field) in frozen_vetoes
                        for field in ("type", "category", "name")
                    ):
                        observed_veto = True
        if observed_veto and safety_valid and safety_vetoes["total"] == 0:
            errors.append("report safety_vetoes must preserve vetoes in supplied stage records")

        uncertainty = report.get("uncertainty")
        interval_valid = False
        if not isinstance(uncertainty, dict):
            errors.append("report uncertainty must be an object")
            uncertainty = {}
        else:
            if uncertainty.get("method") != controller.get("analysis", {}).get(
                "uncertainty_method"
            ):
                errors.append("report uncertainty method must match the controller")
            if uncertainty.get("interval_level") != controller.get("analysis", {}).get(
                "interval_level"
            ):
                errors.append("report interval_level must match the controller")
            interval = [
                uncertainty.get("estimate"),
                uncertainty.get("lower"),
                uncertainty.get("upper"),
            ]
            interval_valid = all(_is_finite_number(value) for value in interval)
            if interval_valid and not interval[1] <= interval[0] <= interval[2]:
                errors.append("report uncertainty must satisfy lower <= estimate <= upper")
                interval_valid = False
            if interval_valid and not all(-1 <= value <= 1 for value in interval):
                errors.append("report win-rate uncertainty values must be between -1 and 1")
                interval_valid = False
            if (
                interval_valid
                and paired_valid
                and denominators_valid
                and denominators["tasks_valid"] > 0
            ):
                target_id = controller_claim.get("target_candidate_id")
                comparator_id = controller_claim.get("comparator_candidate_id")
                expected_estimate = (
                    paired["candidate_wins"].get(target_id, 0)
                    - paired["candidate_wins"].get(comparator_id, 0)
                ) / denominators["tasks_valid"]
                if not math.isclose(
                    uncertainty["estimate"],
                    expected_estimate,
                    rel_tol=0,
                    abs_tol=1e-12,
                ):
                    errors.append(
                        "report uncertainty estimate must equal the target-minus-comparator win-rate difference"
                    )
                    interval_valid = False

        eligibility = report.get("claim_eligibility")
        eligible = None
        if not isinstance(eligibility, dict) or not isinstance(eligibility.get("eligible"), bool):
            errors.append("report claim_eligibility must declare a boolean eligible field")
        else:
            reasons = eligibility.get("reasons")
            if not isinstance(reasons, list) or not all(
                isinstance(reason, str) and reason.strip() for reason in reasons
            ):
                errors.append("report claim eligibility reasons must be a string list")
                reasons = []
            eligible = eligibility.get("eligible")
            if eligible and reasons:
                errors.append("eligible report must not retain claim-ineligibility reasons")
            if not eligible and not reasons:
                errors.append("ineligible report must state at least one reason")
            if eligible:
                if not denominators_valid or denominators.get("tasks_valid", 0) < 1:
                    errors.append("claim eligibility requires at least one valid task")
                if not interval_valid:
                    errors.append("claim eligibility requires a complete finite uncertainty interval")
                if safety_valid and safety_vetoes["total"]:
                    errors.append("claim eligibility requires zero safety vetoes")
                if (
                    adjudication is not None
                    and adjudication.get("eligible_for_aggregation") is not True
                ):
                    errors.append(
                        "claim eligibility cannot include an aggregation-ineligible adjudication"
                    )
                if conditions_drifted:
                    errors.append("claim eligibility requires matched run conditions")
                category_regressions = report.get("category_regressions")
                if not isinstance(category_regressions, list) or category_regressions:
                    errors.append("claim eligibility requires no unresolved category regressions")
                if interval_valid and _is_finite_number(expected_margin):
                    lower = uncertainty["lower"]
                    upper = uncertainty["upper"]
                    if claim_type == "superiority" and lower <= expected_margin:
                        errors.append("superiority claim interval does not clear its margin")
                    elif claim_type == "non_inferiority" and lower <= -expected_margin:
                        errors.append("non_inferiority claim interval does not clear its margin")
                    elif claim_type == "equivalence" and not (
                        lower >= -expected_margin and upper <= expected_margin
                    ):
                        errors.append("equivalence claim interval is outside its margin")

        conclusion = report.get("conclusion")
        if not isinstance(conclusion, dict):
            errors.append("report conclusion must be an object")
        else:
            if conclusion.get("target_candidate_id") != controller_claim.get(
                "target_candidate_id"
            ):
                errors.append("report conclusion target_candidate_id must match the claim")
            _non_empty_string(conclusion.get("statement"), "report conclusion statement", errors)
            expected_status = claim_type if eligible is True else "inconclusive"
            if conclusion.get("status") != expected_status:
                errors.append(
                    "report conclusion status must match claim eligibility and claim type"
                )


def validate(records, raw, template_mode):
    errors = []
    _validate_schema(records, errors)
    _validate_required_fields(records, errors)
    controller = records.get("controller")
    public_task = records.get("public_task")
    evaluator = records.get("evaluator")
    if controller is None or public_task is None or evaluator is None:
        errors.append("controller, public task, and evaluator are required")
        return errors
    _validate_controller(controller, errors)
    _validate_public_task(public_task, errors)
    _validate_evaluator(controller, evaluator, errors)
    if not errors:
        task = _validate_task_binding(
            controller,
            public_task,
            evaluator,
            raw,
            template_mode,
            errors,
        )
        _validate_optional_records(
            records,
            raw,
            controller,
            public_task,
            evaluator,
            task,
            template_mode,
            errors,
        )
    if not template_mode:
        for label, value in records.items():
            placeholders = _find_placeholders(value)
            if placeholders:
                errors.append("%s contains unresolved placeholders: %s" % (label, ", ".join(placeholders)))
    return errors


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--templates", type=Path, help="Validate every starter JSON under this asset directory")
    mode.add_argument("--controller", type=Path, help="Controller manifest for an instantiated comparison")
    parser.add_argument("--public-task", type=Path)
    parser.add_argument("--evaluator", type=Path)
    parser.add_argument("--assignment", type=Path)
    parser.add_argument("--curation", type=Path)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--judgment", type=Path)
    parser.add_argument("--adjudication", type=Path)
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    template_mode = args.templates is not None
    if template_mode:
        paths = {label: args.templates / filename for label, filename in TEMPLATE_FILES.items()}
    else:
        if args.public_task is None or args.evaluator is None:
            print("ERROR: --public-task and --evaluator are required with --controller", file=sys.stderr)
            return 2
        paths = {
            "controller": args.controller,
            "public_task": args.public_task,
            "evaluator": args.evaluator,
            "assignment": args.assignment,
            "curation": args.curation,
            "run": args.run,
            "judgment": args.judgment,
            "adjudication": args.adjudication,
            "report": args.report,
        }
        paths = {label: path for label, path in paths.items() if path is not None}

    errors = []
    records = {}
    raw = {}
    for label, path in paths.items():
        value, payload = _read_object(path, label, errors)
        if value is not None:
            records[label] = value
            raw[label] = payload
    if not errors:
        errors.extend(validate(records, raw, template_mode))
    if errors:
        for error in errors:
            print("ERROR: %s" % error, file=sys.stderr)
        return 1
    print("OK: benchmark %s are mechanically valid" % ("templates" if template_mode else "records"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
