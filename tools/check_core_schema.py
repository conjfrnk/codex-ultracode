#!/usr/bin/env python3
"""Differentially validate the runtime and published core schema."""

import copy
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jsonschema import Draft202012Validator  # noqa: E402

from conductor_runtime.core.workflow import (  # noqa: E402
    SCHEMA,
    validate_workflow,
    workflow_json_schema,
    workflow_template,
)
from conductor_runtime.errors import ValidationError  # noqa: E402


def representative_workflows():
    return [
        workflow_template("read-only"),
        workflow_template("staged-write"),
        {
            "schema": SCHEMA,
            "name": "artifact-and-gate",
            "steps": [
                {"id": "write", "kind": "write_artifact", "output": "result.txt", "content": "value"},
                {"id": "approve", "kind": "manual_gate", "depends_on": ["write"], "approval_id": "approve"},
                {
                    "id": "check",
                    "kind": "shell",
                    "depends_on": ["approve"],
                    "command": ["python3", "-m", "unittest", "-q"],
                    "capture": "check.txt",
                    "writes": False,
                    "destructive": False,
                    "network": False,
                },
            ],
        },
        {
            "schema": SCHEMA,
            "name": "map-and-synthesize",
            "agent_max_tokens": 1000,
            "agent_map_max_total_tokens": 2000,
            "result_artifact": "summary.md",
            "steps": [
                {
                    "id": "map",
                    "kind": "agent_map",
                    "items": ["one"],
                    "item_semantics": "opaque",
                    "prompt_template": "Inspect {item}.",
                    "capture_dir": "packets",
                    "max_tokens": 1000,
                    "max_total_tokens": 2000,
                },
                {
                    "id": "collect",
                    "kind": "collect_results",
                    "source_step": "map",
                    "depends_on": ["map"],
                    "output": "collected.json",
                },
                {
                    "id": "synthesize",
                    "kind": "codex_exec",
                    "depends_on": ["collect"],
                    "context_from": ["collect"],
                    "prompt": "Synthesize the packets.",
                    "capture": "summary.md",
                    "max_tokens": 1000,
                },
            ],
        },
    ]


def explicit_null_mutations():
    """Return inputs that both strict validators must reject."""
    workflows = {workflow["name"]: workflow for workflow in representative_workflows()}
    mutations = []

    def add(label, workflow, step_index, field, replacements=None):
        candidate = copy.deepcopy(workflow)
        step = candidate["steps"][step_index]
        if replacements:
            step.update(replacements)
        step[field] = None
        mutations.append((label, candidate))

    shell = workflows["artifact-and-gate"]
    for field in ("capture", "cwd"):
        add("shell.%s" % field, shell, 2, field)

    codex_workflow = workflows["read-only-review"]
    for field in (
        "completion_verdict",
        "model",
        "output_schema",
        "prompt",
        "prompt_artifact",
        "prompt_file",
    ):
        replacements = {"prompt_file": "prompt.txt"} if field == "prompt" else None
        add("codex_exec.%s" % field, codex_workflow, 0, field, replacements)

    map_workflow = workflows["map-and-synthesize"]
    for field in ("items", "items_artifact", "items_file", "items_pointer", "model", "output_schema"):
        replacements = {"items_file": "items.json"} if field == "items" else None
        add("agent_map.%s" % field, map_workflow, 0, field, replacements)

    return mutations


def main() -> int:
    schema = workflow_json_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    workflows = representative_workflows()
    fixtures = sorted((PROJECT_ROOT / "conductor-workflows" / "core").glob("*.json"))
    import json

    workflows.extend(json.loads(path.read_text(encoding="utf-8")) for path in fixtures)
    for index, workflow in enumerate(workflows):
        validate_workflow(workflow, source="schema parity workflow %d" % index)
        errors = sorted(validator.iter_errors(workflow), key=lambda error: list(error.absolute_path))
        if errors:
            print("Schema parity failed: %s" % errors[0].message, file=sys.stderr)
            return 1
    mutations = explicit_null_mutations()
    for label, workflow in mutations:
        try:
            validate_workflow(workflow, source="explicit-null mutation %s" % label)
        except ValidationError:
            pass
        else:
            print("Runtime parity failed: accepted explicit-null mutation %s" % label, file=sys.stderr)
            return 1
        if not list(validator.iter_errors(workflow)):
            print("Schema parity failed: accepted explicit-null mutation %s" % label, file=sys.stderr)
            return 1
    print(
        "OK: published core schema accepts %d runtime-valid workflows and both validators reject %d null mutations"
        % (len(workflows), len(mutations))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
