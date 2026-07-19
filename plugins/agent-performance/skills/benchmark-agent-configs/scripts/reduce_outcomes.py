#!/usr/bin/env python3
"""Unblind adjudicated task outcomes into candidate-ID aggregate counts."""

import argparse
import json
import sys
from pathlib import Path


MAX_JSON_BYTES = 2 * 1024 * 1024
BLINDED_WINNERS = {"candidate_x_wins": "X", "candidate_y_wins": "Y"}
FIXED_OUTCOMES = {"tie", "neither_acceptable", "inconclusive"}
ABSOLUTE_STATES = {"acceptable", "unacceptable", "unknown"}


class ReductionError(ValueError):
    pass


def _without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ReductionError("duplicate key %r" % key)
        value[key] = item
    return value


def _read_object(path, label):
    if not path.is_file() or path.is_symlink():
        raise ReductionError("%s must be a regular non-symlink file: %s" % (label, path))
    payload = path.read_bytes()
    if len(payload) > MAX_JSON_BYTES:
        raise ReductionError("%s exceeds %d bytes" % (label, MAX_JSON_BYTES))
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_without_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReductionError("%s is not valid JSON: %s" % (label, exc)) from exc
    if not isinstance(value, dict):
        raise ReductionError("%s must contain a JSON object" % label)
    return value


def _contains_placeholder(value):
    if isinstance(value, str):
        return "replace-me" in value.lower()
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_placeholder(item) for item in value.values())
    return False


def _validate_adjudication_decision(adjudication, frozen_vetoes, minimum_judges):
    if adjudication.get("audience") != "adjudicator-output":
        raise ReductionError("adjudication audience must be adjudicator-output")
    input_ids = adjudication.get("input_judgment_ids")
    if (
        not isinstance(input_ids, list)
        or len(input_ids) < minimum_judges
        or not all(isinstance(item, str) and item.strip() for item in input_ids)
        or len(set(input_ids)) != len(input_ids)
    ):
        raise ReductionError("adjudication must bind the required independent judgments")
    outcome = adjudication.get("final_outcome")
    if not isinstance(outcome, str) or outcome not in set(BLINDED_WINNERS).union(
        FIXED_OUTCOMES
    ):
        raise ReductionError("adjudication outcome is invalid")
    absolute = adjudication.get("absolute_acceptability")
    if (
        not isinstance(absolute, dict)
        or set(absolute) != {"X", "Y"}
        or not all(
            isinstance(state, str) and state in ABSOLUTE_STATES
            for state in absolute.values()
        )
    ):
        raise ReductionError("adjudication absolute acceptability is invalid")
    vetoes = adjudication.get("vetoes")
    if not isinstance(vetoes, dict) or set(vetoes) != {"X", "Y"}:
        raise ReductionError("adjudication vetoes must contain X and Y lists")
    for label, entries in vetoes.items():
        if not isinstance(entries, list) or not all(
            isinstance(entry, str) and entry in frozen_vetoes for entry in entries
        ):
            raise ReductionError("adjudication vetoes must use frozen veto names")
        if entries and absolute[label] != "unacceptable":
            raise ReductionError("an adjudication veto must make its candidate unacceptable")
    if outcome != "inconclusive" and "unknown" in absolute.values():
        raise ReductionError("a decisive adjudication cannot have unknown acceptability")
    if outcome == "candidate_x_wins" and absolute["X"] != "acceptable":
        raise ReductionError("candidate_x_wins requires X to be acceptable")
    if outcome == "candidate_y_wins" and absolute["Y"] != "acceptable":
        raise ReductionError("candidate_y_wins requires Y to be acceptable")
    if outcome == "tie" and not all(state == "acceptable" for state in absolute.values()):
        raise ReductionError("tie requires both candidates to be acceptable")
    if outcome == "neither_acceptable" and not all(
        state == "unacceptable" for state in absolute.values()
    ):
        raise ReductionError("neither_acceptable requires both candidates to be unacceptable")


def _controller_contract(controller):
    if controller.get("schema") != "agent-performance.comparison-control.v4":
        raise ReductionError("controller schema is unsupported")
    comparison = controller.get("comparison")
    candidates = controller.get("candidates")
    tasks = controller.get("tasks")
    blinding = controller.get("blinding")
    judging = controller.get("judging")
    if (
        not isinstance(comparison, dict)
        or not isinstance(candidates, list)
        or not isinstance(tasks, list)
        or not isinstance(blinding, dict)
        or not isinstance(judging, dict)
    ):
        raise ReductionError(
            "controller comparison, candidates, tasks, blinding, and judging are required"
        )
    comparison_id = comparison.get("id")
    assignment_seed = blinding.get("assignment_seed")
    frozen_vetoes = comparison.get("safety_vetoes")
    minimum_judges = judging.get("minimum_independent_judges")
    candidate_ids = [
        candidate.get("candidate_id") for candidate in candidates if isinstance(candidate, dict)
    ]
    if (
        not isinstance(comparison_id, str)
        or not comparison_id.strip()
        or not isinstance(assignment_seed, str)
        or not assignment_seed.strip()
        or not isinstance(frozen_vetoes, list)
        or not frozen_vetoes
        or not all(isinstance(veto, str) and veto.strip() for veto in frozen_vetoes)
        or not isinstance(minimum_judges, int)
        or isinstance(minimum_judges, bool)
        or minimum_judges < 1
        or len(candidate_ids) != 2
        or not all(isinstance(candidate_id, str) and candidate_id.strip() for candidate_id in candidate_ids)
        or len(set(candidate_ids)) != 2
    ):
        raise ReductionError("controller comparison and candidate identities are invalid")
    task_identities = []
    for task in tasks:
        if not isinstance(task, dict):
            raise ReductionError("controller tasks must be objects")
        identity = (task.get("id"), task.get("version"))
        if (
            not isinstance(identity[0], str)
            or not identity[0].strip()
            or not isinstance(identity[1], int)
            or isinstance(identity[1], bool)
            or identity[1] < 1
        ):
            raise ReductionError("controller task identity is invalid")
        task_identities.append(identity)
    if not task_identities or len(set(task_identities)) != len(task_identities):
        raise ReductionError("controller tasks must be non-empty and unique")
    return (
        comparison_id,
        candidate_ids,
        task_identities,
        assignment_seed,
        frozen_vetoes,
        minimum_judges,
    )


def _assignment_map(record, comparison_id, candidate_ids, task_identities, assignment_seed):
    if record.get("schema") != "agent-performance.label-assignments.v1":
        raise ReductionError("label assignment schema is unsupported")
    if record.get("audience") != "controller-only" or record.get("comparison_id") != comparison_id:
        raise ReductionError("label assignments must be controller-only and comparison-bound")
    if record.get("assignment_seed") != assignment_seed:
        raise ReductionError("label assignments must use the frozen controller seed")
    assignments = record.get("assignments")
    if not isinstance(assignments, list):
        raise ReductionError("label assignments must be a list")
    result = {}
    for assignment in assignments:
        if not isinstance(assignment, dict):
            raise ReductionError("each label assignment must be an object")
        identity = (assignment.get("task_id"), assignment.get("task_version"))
        labels = assignment.get("labels")
        if identity not in task_identities:
            raise ReductionError("label assignment names an unknown task")
        if identity in result:
            raise ReductionError("label assignments contain a duplicate task")
        if (
            not isinstance(labels, dict)
            or list(labels) != ["X", "Y"]
            or not all(isinstance(value, str) for value in labels.values())
            or set(labels.values()) != set(candidate_ids)
        ):
            raise ReductionError("each task must map X and Y to the two candidate IDs")
        result[identity] = labels
    if set(result) != set(task_identities):
        raise ReductionError("label assignments must cover every controller task")
    return result


def reduce_outcomes(controller, assignments, adjudications):
    if any(_contains_placeholder(record) for record in [controller, assignments, *adjudications]):
        raise ReductionError("reduction inputs contain unresolved placeholders")
    (
        comparison_id,
        candidate_ids,
        task_identities,
        assignment_seed,
        frozen_vetoes,
        minimum_judges,
    ) = _controller_contract(controller)
    assignment_map = _assignment_map(
        assignments,
        comparison_id,
        candidate_ids,
        task_identities,
        assignment_seed,
    )
    by_task = {}
    for adjudication in adjudications:
        if adjudication.get("schema") != "agent-performance.adjudication-record.v1":
            raise ReductionError("adjudication schema is unsupported")
        if adjudication.get("comparison_id") != comparison_id:
            raise ReductionError("adjudication comparison_id must match the controller")
        adjudication_task_id = adjudication.get("task_id")
        adjudication_task_version = adjudication.get("task_version")
        if (
            not isinstance(adjudication_task_id, str)
            or not adjudication_task_id.strip()
            or not isinstance(adjudication_task_version, int)
            or isinstance(adjudication_task_version, bool)
            or adjudication_task_version < 1
        ):
            raise ReductionError("adjudication task identity is invalid")
        identity = (adjudication_task_id, adjudication_task_version)
        if identity not in assignment_map:
            raise ReductionError("adjudication names an unassigned task")
        if identity in by_task:
            raise ReductionError("adjudications contain a duplicate task")
        _validate_adjudication_decision(adjudication, frozen_vetoes, minimum_judges)
        if not isinstance(adjudication.get("eligible_for_aggregation"), bool):
            raise ReductionError("adjudication eligibility must be boolean")
        by_task[identity] = adjudication
    if set(by_task) != set(task_identities):
        raise ReductionError("one adjudication is required for every controller task")

    candidate_wins = {candidate_id: 0 for candidate_id in candidate_ids}
    fixed_counts = {outcome: 0 for outcome in ("tie", "neither_acceptable", "inconclusive")}
    veto_counts = {candidate_id: 0 for candidate_id in candidate_ids}
    valid_tasks = 0
    invalid_pairs = 0
    for identity in task_identities:
        adjudication = by_task[identity]
        labels = assignment_map[identity]
        for label, entries in adjudication["vetoes"].items():
            if entries:
                veto_counts[labels[label]] += 1
        if adjudication["eligible_for_aggregation"] is not True:
            invalid_pairs += 1
            continue
        valid_tasks += 1
        outcome = adjudication["final_outcome"]
        if outcome in BLINDED_WINNERS:
            candidate_wins[labels[BLINDED_WINNERS[outcome]]] += 1
        else:
            fixed_counts[outcome] += 1

    return {
        "schema": "agent-performance.task-outcome-reduction.v1",
        "comparison_id": comparison_id,
        "denominators": {
            "tasks_total": len(task_identities),
            "tasks_valid": valid_tasks,
            "invalid_matched_pairs": invalid_pairs,
        },
        "paired_outcomes": {
            "candidate_wins": candidate_wins,
            **fixed_counts,
        },
        "safety_vetoes": {
            "total": sum(veto_counts.values()),
            "by_candidate_id": veto_counts,
        },
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller", type=Path, required=True)
    parser.add_argument("--assignments", type=Path, required=True)
    parser.add_argument("--adjudication", type=Path, action="append", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        controller = _read_object(args.controller, "controller")
        assignments = _read_object(args.assignments, "label assignments")
        adjudications = [
            _read_object(path, "adjudication") for path in args.adjudication
        ]
        result = reduce_outcomes(controller, assignments, adjudications)
    except (OSError, ReductionError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
