import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, Optional

from tools.evaluate_readonly_diagnostic import (
    READONLY_DIAGNOSTIC_EVALUATION_SCHEMAS,
    READONLY_DIAGNOSTIC_SUITE,
    TASK_SPECS,
    load_readonly_diagnostic_evaluation,
)

from . import __version__
from .benchmark import PARITY_TASKS_SCHEMA, render_parity_task_contract, validate_parity_tasks
from .benchmark import load_benchmark_report
from .build_identity import current_runtime_build_sha256
from .codex_stream import parse_codex_stream
from .codex_progress import load_codex_progress
from .errors import ValidationError
from .model_orchestrator import load_model_workflow_run_receipt
from .model_planner import load_model_workflow_receipt
from .redaction import redact_text
from .security import (
    ensure_dir_no_follow,
    read_regular_file_bytes_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    write_new_text_file_no_follow,
)
from .staged_workspace import snapshot_workspace
from .workflow import SAFE_ID
from .workflow import load_workflow, workflow_fingerprint


READONLY_PARITY_CAMPAIGN_SCHEMA_V1 = "conductor.readonly_parity_campaign.v1"
READONLY_PARITY_CAMPAIGN_SCHEMA = "conductor.readonly_parity_campaign.v2"
READONLY_PARITY_STATUS_SCHEMA = "conductor.readonly_parity_campaign_status.v1"
READONLY_PARITY_LAUNCH_SCHEMA = "conductor.readonly_parity_arm_launch.v1"
READONLY_PARITY_RESULT_SCHEMA = "conductor.readonly_parity_arm_result.v1"
READONLY_PARITY_EVIDENCE_STATUS = "planned-not-evidence"
READONLY_PARITY_RANDOMIZATION = "sha256-balanced-rotation-v1"
READONLY_PARITY_SYSTEMS = ("serial-codex", "native-codex", "claude-sonnet")
READONLY_PARITY_MATCHED_CODEX_TOKEN_CAP = 18000
READONLY_PARITY_PRODUCT_SERIAL_TOKEN_CAP = 20000
READONLY_PARITY_PRODUCT_NATIVE_TOKEN_CAP = 42000
# Backward-compatible public name for the legacy matched campaign ceiling.
READONLY_PARITY_CODEX_TOKEN_CAP = READONLY_PARITY_MATCHED_CODEX_TOKEN_CAP
READONLY_PARITY_BUDGET_PROFILES = ("matched-v1", "product-v1")
READONLY_PARITY_RESOURCE_POLICIES = {
    "matched-v1": "matched-snapshot; failed-arms-retained; unequal-provider-caps-stratified",
    "product-v1": "product-profile-snapshot; per-system-codex-caps; failed-arms-retained; unequal-provider-caps-stratified",
}
READONLY_PARITY_PROFILE_TOKEN_CAPS = {
    "matched-v1": {
        "serial-codex": READONLY_PARITY_MATCHED_CODEX_TOKEN_CAP,
        "native-codex": READONLY_PARITY_MATCHED_CODEX_TOKEN_CAP,
    },
    "product-v1": {
        "serial-codex": READONLY_PARITY_PRODUCT_SERIAL_TOKEN_CAP,
        "native-codex": READONLY_PARITY_PRODUCT_NATIVE_TOKEN_CAP,
    },
}
READONLY_PARITY_NATIVE_THREADS = 1
READONLY_PARITY_CLAUDE_MAX_TURNS = 8
MAX_CAMPAIGN_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_REPETITIONS = 100
MAX_COHORTS = 10000

CAMPAIGN_FIELDS = {
    "schema",
    "name",
    "generated_at_utc",
    "evidence_status",
    "runtime_version",
    "runtime_build_sha256",
    "source",
    "design",
    "systems",
    "cohorts",
    "claim_gate",
}
SOURCE_FIELDS = {
    "parity_tasks_schema",
    "parity_tasks_source",
    "parity_tasks_sha256",
    "suite",
    "task_count",
    "required_dimensions",
    "fixture_profile",
}
DESIGN_FIELDS_V1 = {
    "seed",
    "repetitions",
    "randomization_algorithm",
    "cohort_count",
    "arm_count",
    "system_first_counts",
    "minimum_claim_tasks",
    "minimum_claim_repetitions",
    "resource_policy",
}
DESIGN_FIELDS = DESIGN_FIELDS_V1 | {"budget_profile"}
SYSTEM_FIELDS = {
    "name",
    "provider",
    "model",
    "effort",
    "topology",
    "max_tokens",
    "native_agent_threads",
    "max_turns",
    "cap_enforcement",
    "alternative_main_model_allowed",
}
COHORT_FIELDS = {
    "id",
    "sequence",
    "task_id",
    "repetition",
    "task_contract_sha256",
    "rendered_task_sha256",
    "source_fixture_sha256",
    "budget",
    "arm_order",
    "artifacts",
    "status",
}
ARTIFACT_FIELDS = {
    "root",
    "workspace",
    "runs",
    "workflow",
    "planning_receipt",
    "run_receipt",
    "provider_report",
    "answer",
    "launch",
    "evaluation",
    "result",
}
CLAIM_GATE_FIELDS = {"eligible_for_claims", "required_before_claim"}
LAUNCH_FIELDS = {
    "schema",
    "campaign_sha256",
    "cohort_id",
    "task_id",
    "system",
    "state",
    "source_fixture_sha256",
    "workspace_sha256",
    "provider_artifact_sha256",
    "answer_sha256",
    "result_sha256",
    "updated_at_utc",
}
RESULT_FIELDS = {
    "schema",
    "campaign_sha256",
    "cohort_id",
    "task_id",
    "repetition",
    "system",
    "runtime_version",
    "runtime_build_sha256",
    "generated_at_utc",
    "status",
    "outcome",
    "resources",
    "topology",
    "artifacts",
}
OUTCOME_FIELDS = {
    "provider_status",
    "provider_success",
    "source_preserved",
    "answer_valid",
    "score",
    "max_score",
    "passed",
    "final_success",
}
RESOURCE_FIELDS = {
    "duration_ms",
    "provider_invocations",
    "provider_turns",
    "token_accounting",
    "token_observed",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "gross_tokens",
    "weighted_tokens",
    "cost_observed",
    "cost_usd",
    "cap_enforcement",
    "token_cap",
    "cost_cap_usd",
    "cap_compliant",
    "cap_overshoot",
}
TOPOLOGY_FIELDS = {
    "kind",
    "requested_child_threads",
    "observed_child_threads",
    "topology_executed",
    "helper_models",
}
RESULT_ARTIFACT_FIELDS = {
    "source_fixture_sha256",
    "workspace_before_sha256",
    "workspace_after_sha256",
    "answer_sha256",
    "evaluation_sha256",
    "workflow_sha256",
    "planning_receipt_sha256",
    "run_receipt_sha256",
    "provider_report_sha256",
    "run_state_sha256",
    "codex_progress_sha256",
    "codex_stream_sha256",
}


def build_readonly_parity_campaign(
    parity_tasks: Dict,
    *,
    run_id: str,
    seed: int,
    repetitions: int,
    artifacts_dir: str,
    codex_model: str,
    codex_effort: str,
    budget_profile: str = "matched-v1",
    fixture_root: Optional[Path] = None,
) -> Dict:
    public_tasks = _public_tasks(parity_tasks)
    validate_parity_tasks(public_tasks)
    if public_tasks.get("name") != READONLY_DIAGNOSTIC_SUITE:
        raise ValidationError("read-only parity campaign requires the diagnostic canary suite")
    if {task["id"] for task in public_tasks["tasks"]} != set(TASK_SPECS):
        raise ValidationError("read-only parity campaign requires every diagnostic canary task exactly once")
    _safe_id(run_id, "read-only parity campaign run_id")
    _integer(seed, "read-only parity campaign seed", 0, 2**63 - 1)
    _integer(repetitions, "read-only parity campaign repetitions", 1, MAX_REPETITIONS)
    artifact_root = _clean_artifact_path(artifacts_dir, "read-only parity campaign artifacts_dir")
    model = _non_empty(codex_model.strip() if isinstance(codex_model, str) else codex_model, "Codex model", 128)
    if codex_effort not in {"low", "medium", "high", "xhigh", "ultra"}:
        raise ValidationError("read-only parity campaign Codex effort is invalid")
    budget_profile = _validate_budget_profile(budget_profile)
    task_token_cap = _task_token_cap(budget_profile)
    tasks = public_tasks["tasks"]
    if len(tasks) * repetitions > MAX_COHORTS:
        raise ValidationError("read-only parity campaign exceeds the supported cohort count")
    for task in tasks:
        if task["budget"]["max_tokens"] != task_token_cap:
            raise ValidationError(
                "read-only diagnostic task %s must use the %s token budget"
                % (task["id"], budget_profile)
            )
        if task["budget"]["max_agents"] < 2 or task["budget"]["max_cost_usd"] <= 0:
            raise ValidationError("read-only diagnostic task %s has an invalid live budget" % task["id"])
    resolved_fixture_root = _resolve_fixture_root(parity_tasks, fixture_root)
    fixture_hashes = {}
    for task in tasks:
        source = resolved_fixture_root / TASK_SPECS[task["id"]]["source"]
        if not source.is_dir() or source.is_symlink():
            raise ValidationError("read-only diagnostic source fixture is missing: %s" % task["id"])
        fixture_hashes[task["id"]] = snapshot_workspace(source).tracked_fingerprint_sha256

    rows = _campaign_rows(tasks, seed, repetitions)
    cohorts = []
    for sequence, (repetition, task) in enumerate(rows, start=1):
        cohort_id = "r%03d-%s" % (repetition, task["id"])
        root = _join_artifact_path(artifact_root, cohort_id)
        artifacts = {}
        for system in READONLY_PARITY_SYSTEMS:
            arm = _join_artifact_path(root, system)
            artifacts[system] = {
                "root": arm,
                "workspace": _join_artifact_path(arm, "workspace"),
                "runs": _join_artifact_path(arm, "runs"),
                "workflow": _join_artifact_path(arm, "workflow.json"),
                "planning_receipt": _join_artifact_path(arm, "workflow.json.receipt.json"),
                "run_receipt": _join_artifact_path(arm, "workflow.json.run-receipt.json"),
                "provider_report": _join_artifact_path(arm, "provider-report.json"),
                "answer": _join_artifact_path(arm, "answer.txt"),
                "launch": _join_artifact_path(arm, "launch.json"),
                "evaluation": _join_artifact_path(arm, "evaluation.json"),
                "result": _join_artifact_path(arm, "result.json"),
            }
        cohorts.append(
            {
                "id": cohort_id,
                "sequence": sequence,
                "task_id": task["id"],
                "repetition": repetition,
                "task_contract_sha256": _sha256_json(task),
                "rendered_task_sha256": _sha256_text(render_parity_task_contract(task)),
                "source_fixture_sha256": fixture_hashes[task["id"]],
                "budget": dict(task["budget"]),
                "arm_order": _arm_order(seed, sequence, repetition, task["id"]),
                "artifacts": artifacts,
                "status": "planned",
            }
        )
    first_counts = {
        system: sum(1 for cohort in cohorts if cohort["arm_order"][0] == system)
        for system in READONLY_PARITY_SYSTEMS
    }
    source_path = parity_tasks.get("_source_path") if isinstance(parity_tasks, dict) else None
    campaign = {
        "schema": READONLY_PARITY_CAMPAIGN_SCHEMA,
        "name": run_id,
        "generated_at_utc": _now_utc(),
        "evidence_status": READONLY_PARITY_EVIDENCE_STATUS,
        "runtime_version": __version__,
        "runtime_build_sha256": current_runtime_build_sha256(),
        "source": {
            "parity_tasks_schema": PARITY_TASKS_SCHEMA,
            "parity_tasks_source": str(source_path) if source_path else None,
            "parity_tasks_sha256": _sha256_json(public_tasks),
            "suite": public_tasks["name"],
            "task_count": len(tasks),
            "required_dimensions": list(public_tasks.get("required_dimensions", [])),
            "fixture_profile": "implementation-canary-diagnostic-v1",
        },
        "design": {
            "seed": seed,
            "repetitions": repetitions,
            "budget_profile": budget_profile,
            "randomization_algorithm": READONLY_PARITY_RANDOMIZATION,
            "cohort_count": len(cohorts),
            "arm_count": len(cohorts) * len(READONLY_PARITY_SYSTEMS),
            "system_first_counts": first_counts,
            "minimum_claim_tasks": 3,
            "minimum_claim_repetitions": 2,
            "resource_policy": READONLY_PARITY_RESOURCE_POLICIES[budget_profile],
        },
        "systems": _system_contracts(model, codex_effort, budget_profile),
        "cohorts": cohorts,
        "claim_gate": {
            "eligible_for_claims": False,
            "required_before_claim": [
                "execute every preregistered arm without dropping failures or interrupted outcomes",
                "retain hidden deterministic scoring and unchanged source evidence for every arm",
                "require at least three tasks and two repetitions with one runtime release",
                "require aggregate parent-plus-child Codex accounting and observed native child use",
                "compare quality, latency, gross tokens, weighted Codex tokens, and authoritative cost separately",
                "never treat hard-dollar and hard-token caps as equivalent",
                "run broader write, recovery, safety, and usability cohorts before any broad superiority claim",
            ],
        },
    }
    validate_readonly_parity_campaign(campaign)
    return campaign


def validate_readonly_parity_campaign(campaign: Dict, source: str = "<memory>") -> None:
    root = _object(campaign, CAMPAIGN_FIELDS, source)
    schema = root.get("schema")
    if schema not in {READONLY_PARITY_CAMPAIGN_SCHEMA, READONLY_PARITY_CAMPAIGN_SCHEMA_V1}:
        raise ValidationError(
            "%s schema must be %s or %s"
            % (source, READONLY_PARITY_CAMPAIGN_SCHEMA, READONLY_PARITY_CAMPAIGN_SCHEMA_V1)
        )
    _safe_id(root.get("name"), "%s name" % source)
    _non_empty(root.get("generated_at_utc"), "%s generated_at_utc" % source)
    if root.get("evidence_status") != READONLY_PARITY_EVIDENCE_STATUS:
        raise ValidationError("%s evidence_status is invalid" % source)
    _version(root.get("runtime_version"), "%s runtime_version" % source)
    _sha256(root.get("runtime_build_sha256"), "%s runtime_build_sha256" % source)
    source_value = _object(root.get("source"), SOURCE_FIELDS, "%s source" % source)
    if source_value.get("parity_tasks_schema") != PARITY_TASKS_SCHEMA:
        raise ValidationError("%s parity_tasks_schema is invalid" % source)
    source_path = source_value.get("parity_tasks_source")
    if source_path is not None and not isinstance(source_path, str):
        raise ValidationError("%s parity_tasks_source must be string or null" % source)
    _sha256(source_value.get("parity_tasks_sha256"), "%s parity_tasks_sha256" % source)
    if source_value.get("suite") != READONLY_DIAGNOSTIC_SUITE:
        raise ValidationError("%s suite is invalid" % source)
    if source_value.get("task_count") != len(TASK_SPECS):
        raise ValidationError("%s task_count is invalid" % source)
    dimensions = source_value.get("required_dimensions")
    if not isinstance(dimensions, list) or not dimensions or not all(isinstance(item, str) and item for item in dimensions):
        raise ValidationError("%s required_dimensions are invalid" % source)
    if source_value.get("fixture_profile") != "implementation-canary-diagnostic-v1":
        raise ValidationError("%s fixture_profile is invalid" % source)
    design_fields = DESIGN_FIELDS_V1 if schema == READONLY_PARITY_CAMPAIGN_SCHEMA_V1 else DESIGN_FIELDS
    design = _object(root.get("design"), design_fields, "%s design" % source)
    budget_profile = (
        "matched-v1"
        if schema == READONLY_PARITY_CAMPAIGN_SCHEMA_V1
        else _validate_budget_profile(design.get("budget_profile"))
    )
    seed = _integer(design.get("seed"), "%s seed" % source, 0, 2**63 - 1)
    repetitions = _integer(design.get("repetitions"), "%s repetitions" % source, 1, MAX_REPETITIONS)
    if design.get("randomization_algorithm") != READONLY_PARITY_RANDOMIZATION:
        raise ValidationError("%s randomization algorithm is invalid" % source)
    if design.get("minimum_claim_tasks") != 3 or design.get("minimum_claim_repetitions") != 2:
        raise ValidationError("%s minimum claim design is invalid" % source)
    if design.get("resource_policy") != READONLY_PARITY_RESOURCE_POLICIES[budget_profile]:
        raise ValidationError("%s resource policy is invalid" % source)
    _validate_systems(root.get("systems"), budget_profile, source)
    cohorts = root.get("cohorts")
    if not isinstance(cohorts, list) or not cohorts or len(cohorts) > MAX_COHORTS:
        raise ValidationError("%s cohorts are invalid" % source)
    seen = set()
    task_counts = {task_id: 0 for task_id in TASK_SPECS}
    for index, cohort in enumerate(cohorts, start=1):
        item = _object(cohort, COHORT_FIELDS, "%s cohort" % source)
        _safe_id(item.get("id"), "%s cohort id" % source)
        if item["id"] in seen:
            raise ValidationError("%s cohort ids must be unique" % source)
        seen.add(item["id"])
        if item.get("sequence") != index:
            raise ValidationError("%s cohort sequence is invalid" % source)
        task_id = item.get("task_id")
        if task_id not in TASK_SPECS:
            raise ValidationError("%s cohort task_id is invalid" % source)
        repetition = _integer(item.get("repetition"), "%s repetition" % source, 1, repetitions)
        if item["id"] != "r%03d-%s" % (repetition, task_id):
            raise ValidationError("%s cohort id is inconsistent" % source)
        task_counts[task_id] += 1
        _sha256(item.get("task_contract_sha256"), "%s task contract" % source)
        _sha256(item.get("rendered_task_sha256"), "%s rendered task" % source)
        _sha256(item.get("source_fixture_sha256"), "%s source fixture" % source)
        _validate_budget(item.get("budget"), budget_profile, source)
        expected_order = _arm_order(seed, index, repetition, task_id)
        if item.get("arm_order") != expected_order:
            raise ValidationError("%s cohort arm order does not match deterministic randomization" % source)
        artifacts = item.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != set(READONLY_PARITY_SYSTEMS):
            raise ValidationError("%s cohort artifacts are invalid" % source)
        used_paths = set()
        for system in READONLY_PARITY_SYSTEMS:
            arm = _object(artifacts[system], ARTIFACT_FIELDS, "%s %s artifacts" % (source, system))
            for field, value in arm.items():
                clean = _clean_artifact_path(value, "%s %s %s" % (source, system, field))
                if clean in used_paths:
                    raise ValidationError("%s cohort artifact paths must be unique" % source)
                used_paths.add(clean)
            expected_root_suffix = "%s/%s" % (item["id"], system)
            if not arm["root"].endswith(expected_root_suffix):
                raise ValidationError("%s cohort artifact root is inconsistent" % source)
            if any(not value.startswith(arm["root"] + "/") for key, value in arm.items() if key != "root"):
                raise ValidationError("%s arm artifacts must remain below their root" % source)
        if item.get("status") != "planned":
            raise ValidationError("%s cohort status must remain planned" % source)
    if any(count != repetitions for count in task_counts.values()):
        raise ValidationError("%s task repetition coverage is inconsistent" % source)
    expected_count = len(TASK_SPECS) * repetitions
    if design.get("cohort_count") != expected_count or design.get("arm_count") != expected_count * 3:
        raise ValidationError("%s cohort or arm count is inconsistent" % source)
    expected_first = {
        system: sum(1 for cohort in cohorts if cohort["arm_order"][0] == system)
        for system in READONLY_PARITY_SYSTEMS
    }
    if design.get("system_first_counts") != expected_first or max(expected_first.values()) - min(expected_first.values()) > 1:
        raise ValidationError("%s system-first balance is inconsistent" % source)
    gate = _object(root.get("claim_gate"), CLAIM_GATE_FIELDS, "%s claim_gate" % source)
    if gate.get("eligible_for_claims") is not False:
        raise ValidationError("%s planned campaign cannot be claim-eligible" % source)
    required = gate.get("required_before_claim")
    if not isinstance(required, list) or len(required) < 5 or not all(isinstance(item, str) and item for item in required):
        raise ValidationError("%s claim gate is invalid" % source)


def load_readonly_parity_campaign(path: Path) -> Dict:
    raw = read_regular_text_file_no_follow(path, "read-only parity campaign", MAX_CAMPAIGN_BYTES)
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("%s is not valid campaign JSON: %s" % (path, exc))
    validate_readonly_parity_campaign(value, source=str(path))
    return value


def write_readonly_parity_campaign(campaign: Dict, output: Path) -> Path:
    validate_readonly_parity_campaign(campaign, source=str(output))
    reject_symlink_path(output, "read-only parity campaign output")
    if output.exists():
        raise ValidationError("read-only parity campaign output already exists")
    parent_fd = ensure_dir_no_follow(output.parent, "read-only parity campaign output parent")
    os.close(parent_fd)
    write_new_text_file_no_follow(
        output,
        "read-only parity campaign output",
        json.dumps(campaign, indent=2, sort_keys=True) + "\n",
        sync=True,
    )
    return output


def validate_readonly_parity_launch(value: Dict, source: str = "<memory>") -> None:
    root = _object(value, LAUNCH_FIELDS, source)
    if root.get("schema") != READONLY_PARITY_LAUNCH_SCHEMA:
        raise ValidationError("%s launch schema is invalid" % source)
    _sha256(root.get("campaign_sha256"), "%s campaign_sha256" % source)
    _safe_id(root.get("cohort_id"), "%s cohort_id" % source)
    _safe_id(root.get("task_id"), "%s task_id" % source)
    if root.get("system") not in READONLY_PARITY_SYSTEMS:
        raise ValidationError("%s system is invalid" % source)
    state = root.get("state")
    if state not in {"preparing", "ready", "launching", "captured", "finalized"}:
        raise ValidationError("%s launch state is invalid" % source)
    _sha256(root.get("source_fixture_sha256"), "%s source fixture" % source)
    optional_hashes = ["workspace_sha256", "provider_artifact_sha256", "answer_sha256", "result_sha256"]
    for field in optional_hashes:
        if root.get(field) is not None:
            _sha256(root[field], "%s %s" % (source, field))
    required_by_state = {
        "preparing": set(),
        "ready": {"workspace_sha256"},
        "launching": {"workspace_sha256"},
        "captured": {"workspace_sha256", "provider_artifact_sha256", "answer_sha256"},
        "finalized": set(optional_hashes),
    }[state]
    present = {field for field in optional_hashes if root.get(field) is not None}
    if present != required_by_state:
        raise ValidationError("%s launch hash state is inconsistent" % source)
    _non_empty(root.get("updated_at_utc"), "%s updated_at_utc" % source)


def load_readonly_parity_launch(path: Path) -> Dict:
    raw = read_regular_text_file_no_follow(path, "read-only parity arm launch", MAX_RESULT_BYTES)
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("%s is not valid launch JSON: %s" % (path, exc))
    validate_readonly_parity_launch(value, source=str(path))
    return value


def validate_readonly_parity_result(value: Dict, source: str = "<memory>") -> None:
    root = _object(value, RESULT_FIELDS, source)
    if root.get("schema") != READONLY_PARITY_RESULT_SCHEMA:
        raise ValidationError("%s result schema is invalid" % source)
    _sha256(root.get("campaign_sha256"), "%s campaign_sha256" % source)
    _safe_id(root.get("cohort_id"), "%s cohort_id" % source)
    task_id = root.get("task_id")
    if task_id not in TASK_SPECS:
        raise ValidationError("%s task_id is invalid" % source)
    _integer(root.get("repetition"), "%s repetition" % source, 1, MAX_REPETITIONS)
    system = root.get("system")
    if system not in READONLY_PARITY_SYSTEMS:
        raise ValidationError("%s system is invalid" % source)
    _version(root.get("runtime_version"), "%s runtime_version" % source)
    _sha256(root.get("runtime_build_sha256"), "%s runtime_build_sha256" % source)
    _non_empty(root.get("generated_at_utc"), "%s generated_at_utc" % source)
    if root.get("status") != "completed":
        raise ValidationError("%s result status is invalid" % source)
    outcome = _object(root.get("outcome"), OUTCOME_FIELDS, "%s outcome" % source)
    if outcome.get("provider_status") not in {"success", "failed"}:
        raise ValidationError("%s provider_status is invalid" % source)
    for field in ["provider_success", "source_preserved", "answer_valid", "passed", "final_success"]:
        if not isinstance(outcome.get(field), bool):
            raise ValidationError("%s %s must be boolean" % (source, field))
    score = _integer(outcome.get("score"), "%s score" % source, 0, 10)
    if outcome.get("max_score") != 10:
        raise ValidationError("%s max_score is invalid" % source)
    if outcome["provider_success"] != (outcome["provider_status"] == "success"):
        raise ValidationError("%s provider status is inconsistent" % source)
    if outcome["passed"] != bool(outcome["provider_success"] and score >= 7):
        raise ValidationError("%s passed is inconsistent" % source)
    if outcome["final_success"] != bool(outcome["passed"] and outcome["source_preserved"]):
        raise ValidationError("%s final_success is inconsistent" % source)
    resources = _object(root.get("resources"), RESOURCE_FIELDS, "%s resources" % source)
    _validate_resources(resources, system, source)
    if outcome["provider_success"] and resources["cap_compliant"] is False:
        raise ValidationError("%s provider success cannot exceed its observed cap" % source)
    topology = _object(root.get("topology"), TOPOLOGY_FIELDS, "%s topology" % source)
    _validate_topology(topology, system, source)
    artifacts = _object(root.get("artifacts"), RESULT_ARTIFACT_FIELDS, "%s artifacts" % source)
    for field, digest in artifacts.items():
        if digest is not None:
            _sha256(digest, "%s %s" % (source, field))
    required_common = {
        "source_fixture_sha256",
        "workspace_before_sha256",
        "workspace_after_sha256",
        "answer_sha256",
        "evaluation_sha256",
    }
    if any(artifacts[field] is None for field in required_common):
        raise ValidationError("%s common artifact hashes are incomplete" % source)
    codex_fields = {
        "workflow_sha256",
        "planning_receipt_sha256",
        "run_state_sha256",
        "codex_progress_sha256",
        "codex_stream_sha256",
    }
    if system in {"serial-codex", "native-codex"}:
        if artifacts["run_receipt_sha256"] is None or artifacts["provider_report_sha256"] is not None:
            raise ValidationError("%s Codex artifact hashes are inconsistent" % source)
    elif (
        any(artifacts[field] is not None for field in codex_fields)
        or artifacts["run_receipt_sha256"] is not None
        or artifacts["provider_report_sha256"] is None
    ):
        raise ValidationError("%s Claude artifact hashes are inconsistent" % source)


def load_readonly_parity_result(path: Path) -> Dict:
    raw = read_regular_text_file_no_follow(path, "read-only parity arm result", MAX_RESULT_BYTES)
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("%s is not valid result JSON: %s" % (path, exc))
    validate_readonly_parity_result(value, source=str(path))
    return value


def readonly_parity_campaign_status(campaign_path: Path) -> Dict:
    campaign_path = Path(campaign_path)
    campaign = load_readonly_parity_campaign(campaign_path)
    campaign_sha256 = _sha256_file(campaign_path, "read-only parity campaign", MAX_CAMPAIGN_BYTES)
    lifecycle = {"completed": 0, "partial": 0, "pending": 0, "invalid": 0}
    arm_lifecycle = {"completed": 0, "partial": 0, "pending": 0, "invalid": 0}
    cohorts = []
    completed_results = []
    for cohort in campaign["cohorts"]:
        arms = {}
        for system in READONLY_PARITY_SYSTEMS:
            arm = _arm_status(campaign_path, campaign, campaign_sha256, cohort, system)
            arms[system] = arm
            arm_lifecycle[arm["status"]] += 1
            if arm["status"] == "completed":
                completed_results.append(arm["result"])
        statuses = [arms[system]["status"] for system in READONLY_PARITY_SYSTEMS]
        if "invalid" in statuses:
            status = "invalid"
        elif all(item == "completed" for item in statuses):
            status = "completed"
        elif all(item == "pending" for item in statuses):
            status = "pending"
        else:
            status = "partial"
        lifecycle[status] += 1
        cohorts.append(
            {
                "id": cohort["id"],
                "sequence": cohort["sequence"],
                "task_id": cohort["task_id"],
                "repetition": cohort["repetition"],
                "arm_order": list(cohort["arm_order"]),
                "status": status,
                "arms": {
                    system: {key: value for key, value in arms[system].items() if key != "result"}
                    for system in READONLY_PARITY_SYSTEMS
                },
            }
        )
    lifecycle["coverage"] = lifecycle["completed"] / len(campaign["cohorts"])
    system_summary = {
        system: _system_summary([item for item in completed_results if item["system"] == system])
        for system in READONLY_PARITY_SYSTEMS
    }
    result_by_key = {
        (item["cohort_id"], item["system"]): item
        for item in completed_results
    }
    pairwise = []
    for left, right in [
        ("native-codex", "serial-codex"),
        ("serial-codex", "claude-sonnet"),
        ("native-codex", "claude-sonnet"),
    ]:
        pairs = [
            (result_by_key[(cohort["id"], left)], result_by_key[(cohort["id"], right)])
            for cohort in campaign["cohorts"]
            if (cohort["id"], left) in result_by_key and (cohort["id"], right) in result_by_key
        ]
        pairwise.append(_pairwise_summary(left, right, pairs))
    inference = _inference(campaign, lifecycle, arm_lifecycle, completed_results, pairwise)
    return {
        "schema": READONLY_PARITY_STATUS_SCHEMA,
        "campaign": {
            "name": campaign["name"],
            "runtime_version": campaign["runtime_version"],
            "runtime_build_sha256": campaign["runtime_build_sha256"],
            "planned_cohorts": len(campaign["cohorts"]),
            "planned_arms": len(campaign["cohorts"]) * 3,
            "sha256": campaign_sha256,
        },
        "lifecycle": lifecycle,
        "arm_lifecycle": arm_lifecycle,
        "systems": system_summary,
        "pairwise": pairwise,
        "inference": inference,
        "cohorts": cohorts,
    }


def readonly_parity_campaign_sha256(path: Path) -> str:
    return _sha256_file(Path(path), "read-only parity campaign", MAX_CAMPAIGN_BYTES)


def resolve_readonly_arm_paths(campaign_path: Path, cohort: Dict, system: str) -> Dict[str, Path]:
    if system not in READONLY_PARITY_SYSTEMS:
        raise ValidationError("read-only parity system is invalid")
    base = Path(campaign_path).resolve().parent
    return {key: base / value for key, value in cohort["artifacts"][system].items()}


def _arm_status(campaign_path, campaign, campaign_sha256, cohort, system):
    paths = resolve_readonly_arm_paths(campaign_path, cohort, system)
    launch_exists = paths["launch"].exists() or paths["launch"].is_symlink()
    result_exists = paths["result"].exists() or paths["result"].is_symlink()
    any_exists = any(path.exists() or path.is_symlink() for key, path in paths.items() if key != "root")
    if not launch_exists:
        if any_exists:
            return {"status": "invalid", "reason": "artifacts-without-launch", "result": None}
        return {"status": "pending", "reason": "not-started", "result": None}
    try:
        launch = load_readonly_parity_launch(paths["launch"])
        if (
            launch["campaign_sha256"] != campaign_sha256
            or launch["cohort_id"] != cohort["id"]
            or launch["task_id"] != cohort["task_id"]
            or launch["system"] != system
            or launch["source_fixture_sha256"] != cohort["source_fixture_sha256"]
        ):
            raise ValidationError("launch does not match campaign arm")
        if not result_exists:
            return {"status": "partial", "reason": launch["state"], "result": None}
        if launch["state"] != "finalized":
            raise ValidationError("result exists before launch finalization")
        result = load_readonly_parity_result(paths["result"])
        claude_permission_mode = _validate_completed_arm(
            paths,
            campaign,
            campaign_sha256,
            cohort,
            system,
            launch,
            result,
        )
        inference_result = dict(result)
        if claude_permission_mode is not None:
            inference_result["_claude_permission_mode"] = claude_permission_mode
        return {
            "status": "completed",
            "reason": "evidence-complete",
            "score": result["outcome"]["score"],
            "final_success": result["outcome"]["final_success"],
            "result": inference_result,
        }
    except (OSError, ValidationError, ValueError):
        return {"status": "invalid", "reason": "invalid-evidence", "result": None}


def _validate_completed_arm(paths, campaign, campaign_sha256, cohort, system, launch, result):
    claude_permission_mode = None
    if (
        result["campaign_sha256"] != campaign_sha256
        or result["cohort_id"] != cohort["id"]
        or result["task_id"] != cohort["task_id"]
        or result["repetition"] != cohort["repetition"]
        or result["system"] != system
        or result["runtime_version"] != campaign["runtime_version"]
        or result["runtime_build_sha256"] != campaign["runtime_build_sha256"]
    ):
        raise ValidationError("result does not match campaign arm")
    system_config = next(item for item in campaign["systems"] if item["name"] == system)
    if system in {"serial-codex", "native-codex"}:
        if result["resources"]["token_cap"] != system_config["max_tokens"]:
            raise ValidationError("result token cap does not match campaign system contract")
    elif result["resources"]["cost_cap_usd"] != cohort["budget"]["max_cost_usd"]:
        raise ValidationError("result cost cap does not match campaign cohort contract")
    result_hash = _sha256_file(paths["result"], "read-only parity result", MAX_RESULT_BYTES)
    if launch["result_sha256"] != result_hash:
        raise ValidationError("launch result hash is invalid")
    artifacts = result["artifacts"]
    if artifacts["source_fixture_sha256"] != cohort["source_fixture_sha256"]:
        raise ValidationError("result source fixture hash is invalid")
    mapping = {
        "answer_sha256": (paths["answer"], "answer"),
        "evaluation_sha256": (paths["evaluation"], "evaluation"),
        "workflow_sha256": (paths["workflow"], "workflow"),
        "planning_receipt_sha256": (paths["planning_receipt"], "planning receipt"),
        "run_receipt_sha256": (paths["run_receipt"], "run receipt"),
        "provider_report_sha256": (paths["provider_report"], "provider report"),
    }
    for field, (path, label) in mapping.items():
        expected = artifacts[field]
        if expected is None:
            if path.exists() or path.is_symlink():
                raise ValidationError("unexpected %s artifact" % label)
        elif _sha256_file(path, "read-only parity %s" % label, MAX_RESULT_BYTES) != expected:
            raise ValidationError("%s artifact hash is invalid" % label)
    if launch["provider_artifact_sha256"] != (
        artifacts["provider_report_sha256"] or artifacts["run_receipt_sha256"]
    ) or launch["answer_sha256"] != artifacts["answer_sha256"]:
        raise ValidationError("launch capture hashes are invalid")
    evaluation = load_readonly_diagnostic_evaluation(paths["evaluation"])
    if (
        evaluation["schema"] not in READONLY_DIAGNOSTIC_EVALUATION_SCHEMAS
        or evaluation["task_id"] != cohort["task_id"]
        or evaluation["source_fixture_sha256"] != cohort["source_fixture_sha256"]
        or evaluation["answer_sha256"] != artifacts["answer_sha256"]
        or evaluation["provider_success"] != result["outcome"]["provider_success"]
        or evaluation["score"] != result["outcome"]["score"]
        or evaluation["passed"] != result["outcome"]["passed"]
    ):
        raise ValidationError("evaluation does not match result")
    if result["outcome"]["answer_valid"] != (evaluation["parse_status"] != "invalid"):
        raise ValidationError("answer validity does not match evaluation")
    if system == "claude-sonnet":
        report = load_benchmark_report(paths["provider_report"])
        provider_evidence = report.get("results", [{}])[0].get("provider_evidence", {})
        if (
            report.get("suite") != campaign["source"]["suite"]
            or report.get("system") != "claude-sonnet-ultracode"
            or len(report.get("results", [])) != 1
            or report["results"][0].get("id") != cohort["task_id"]
            or report.get("environment", {}).get("task_contract_prompt_sha256")
            != cohort["rendered_task_sha256"]
            or provider_evidence.get("requested", {}).get("model") != "sonnet"
            or provider_evidence.get("requested", {}).get("effort") != "ultracode"
            or provider_evidence.get("requested", {}).get("max_turns") != READONLY_PARITY_CLAUDE_MAX_TURNS
            or provider_evidence.get("requested", {}).get("max_budget_usd")
            != cohort["budget"]["max_cost_usd"]
        ):
            raise ValidationError("Claude provider report does not match campaign arm")
        claude_permission_mode = provider_evidence["requested"]["permission_mode"]
    else:
        receipt = load_model_workflow_run_receipt(paths["run_receipt"])
        expected_run_id = "%s-%s" % (cohort["id"], system)
        if (
            receipt["status"] == "running"
            or receipt["run"]["id"] not in {"", expected_run_id}
            or receipt["task"]["sha256"] != cohort["rendered_task_sha256"]
        ):
            raise ValidationError("Codex run receipt does not match campaign arm")
        if artifacts["planning_receipt_sha256"] is not None:
            load_model_workflow_receipt(paths["planning_receipt"])
        if artifacts["workflow_sha256"] is not None:
            workflow = load_workflow(paths["workflow"])
            _validate_codex_workflow_contract(workflow, campaign, system)
            if receipt["workflow"]["sha256"] != workflow_fingerprint(workflow):
                raise ValidationError("Codex run receipt workflow fingerprint is invalid")
        if artifacts["planning_receipt_sha256"] is not None and (
            receipt["planning"]["receipt_sha256"] != artifacts["planning_receipt_sha256"]
        ):
            raise ValidationError("Codex run receipt planning hash is invalid")
    workspace_sha256 = snapshot_workspace(paths["workspace"]).tracked_fingerprint_sha256
    if workspace_sha256 != artifacts["workspace_after_sha256"]:
        raise ValidationError("workspace changed after result finalization")
    if result["outcome"]["source_preserved"] != (
        artifacts["workspace_before_sha256"] == artifacts["workspace_after_sha256"]
    ):
        raise ValidationError("source preservation outcome is inconsistent")
    run_id = "%s-%s" % (cohort["id"], system)
    run_dir = paths["runs"] / run_id
    run_artifacts = {
        "run_state_sha256": run_dir / "state.json",
        "codex_progress_sha256": _codex_progress_path(run_dir),
        "codex_stream_sha256": run_dir / "logs" / "complete-task.stdout.log",
    }
    for field, path in run_artifacts.items():
        expected = artifacts[field]
        if expected is None:
            if path.exists() or path.is_symlink():
                raise ValidationError("unexpected Codex run artifact")
        elif _sha256_file(path, "read-only parity Codex run artifact", MAX_RESULT_BYTES) != expected:
            raise ValidationError("Codex run artifact hash is invalid")
    if artifacts["codex_progress_sha256"] is not None:
        load_codex_progress(run_artifacts["codex_progress_sha256"])
    if artifacts["codex_stream_sha256"] is not None:
        stream_text = read_regular_text_file_no_follow(
            run_artifacts["codex_stream_sha256"],
            "read-only parity Codex stream",
            MAX_CAMPAIGN_BYTES,
        )
        parsed = parse_codex_stream(stream_text)
        result_path = run_dir / "artifacts" / "direct" / "result.md"
        result_text = None
        if result_path.exists() or result_path.is_symlink():
            result_text = read_regular_text_file_no_follow(
                result_path,
                "read-only parity Codex final message",
                MAX_RESULT_BYTES,
            )
        if not result_text:
            answer = read_regular_text_file_no_follow(
                paths["answer"],
                "read-only parity answer",
                MAX_RESULT_BYTES,
            )
            if answer != redact_text(parsed["output_text"]):
                raise ValidationError("captured Codex answer does not match its strict event stream")
    return claude_permission_mode


def _system_summary(results):
    scores = [item["outcome"]["score"] for item in results]
    durations = [item["resources"]["duration_ms"] for item in results]
    gross = [item["resources"]["gross_tokens"] for item in results if item["resources"]["token_observed"]]
    weighted = [item["resources"]["weighted_tokens"] for item in results if item["resources"]["weighted_tokens"] is not None]
    costs = [item["resources"]["cost_usd"] for item in results if item["resources"]["cost_observed"]]
    return {
        "completed_arms": len(results),
        "provider_successes": sum(1 for item in results if item["outcome"]["provider_success"]),
        "final_successes": sum(1 for item in results if item["outcome"]["final_success"]),
        "score_mean": _mean(scores),
        "duration_ms_mean": _mean(durations),
        "gross_token_coverage": len(gross),
        "gross_tokens_mean": _mean(gross),
        "weighted_token_coverage": len(weighted),
        "weighted_tokens_mean": _mean(weighted),
        "cost_coverage": len(costs),
        "cost_usd_total": sum(costs) if costs else None,
        "cap_observation_coverage": sum(1 for item in results if item["resources"]["cap_compliant"] is not None),
        "cap_compliant_arms": sum(1 for item in results if item["resources"]["cap_compliant"] is True),
        "cap_overshoot_arms": sum(1 for item in results if item["resources"]["cap_compliant"] is False),
    }


def _pairwise_summary(left, right, pairs):
    score_wins = {"left": 0, "right": 0, "ties": 0}
    success_wins = {"left": 0, "right": 0, "ties": 0}
    duration_wins = {"left": 0, "right": 0, "ties": 0}
    gross_wins = {"left": 0, "right": 0, "ties": 0, "comparable": 0}
    duration_to_success = {"left": 0, "right": 0, "ties": 0, "comparable": 0, "incomparable": 0}
    gross_to_success = {"left": 0, "right": 0, "ties": 0, "comparable": 0, "incomparable": 0}
    score_deltas = []
    for left_result, right_result in pairs:
        _record_higher(score_wins, left_result["outcome"]["score"], right_result["outcome"]["score"])
        _record_higher(success_wins, int(left_result["outcome"]["final_success"]), int(right_result["outcome"]["final_success"]))
        _record_lower(duration_wins, left_result["resources"]["duration_ms"], right_result["resources"]["duration_ms"])
        _record_resource_to_success(
            duration_to_success,
            left_result,
            right_result,
            left_result["resources"]["duration_ms"],
            right_result["resources"]["duration_ms"],
            True,
            True,
        )
        score_deltas.append(left_result["outcome"]["score"] - right_result["outcome"]["score"])
        if left_result["resources"]["token_observed"] and right_result["resources"]["token_observed"]:
            gross_wins["comparable"] += 1
            _record_lower(gross_wins, left_result["resources"]["gross_tokens"], right_result["resources"]["gross_tokens"])
        _record_resource_to_success(
            gross_to_success,
            left_result,
            right_result,
            left_result["resources"]["gross_tokens"],
            right_result["resources"]["gross_tokens"],
            left_result["resources"]["token_observed"],
            right_result["resources"]["token_observed"],
        )
    decisive = score_wins["left"] + score_wins["right"]
    return {
        "left": left,
        "right": right,
        "completed_pairs": len(pairs),
        "quality": {
            **score_wins,
            "mean_score_delta_left_minus_right": _mean(score_deltas),
            "decisive_left_win_rate_ci95": _wilson_ci95(score_wins["left"], decisive) if decisive else None,
        },
        "final_success": success_wins,
        "duration": duration_wins,
        "gross_tokens": gross_wins,
        "duration_to_success": duration_to_success,
        "gross_tokens_to_success": gross_to_success,
    }


def _inference(campaign, lifecycle, arm_lifecycle, results, pairwise):
    complete = arm_lifecycle["completed"] == campaign["design"]["arm_count"] and lifecycle["invalid"] == 0
    minimum_design = (
        campaign["source"]["task_count"] >= campaign["design"]["minimum_claim_tasks"]
        and campaign["design"]["repetitions"] >= campaign["design"]["minimum_claim_repetitions"]
    )
    release_uniform = bool(results) and {
        (item["runtime_version"], item["runtime_build_sha256"]) for item in results
    } == {(campaign["runtime_version"], campaign["runtime_build_sha256"])}
    native = [item for item in results if item["system"] == "native-codex"]
    codex = [item for item in results if item["system"] in {"serial-codex", "native-codex"}]
    claude = [item for item in results if item["system"] == "claude-sonnet"]
    claude_task_mode_comparable = not claude or all(
        item.get("_claude_permission_mode") == "dontAsk" for item in claude
    )
    native_topology_complete = bool(native) and all(item["topology"]["topology_executed"] for item in native)
    codex_usage_complete = bool(codex) and all(item["resources"]["token_observed"] for item in codex)
    cap_accounting_complete = bool(results) and all(
        item["resources"]["cap_compliant"] is not None for item in results
    )
    caps_compliant = cap_accounting_complete and all(
        item["resources"]["cap_compliant"] for item in results
    )
    quality_advantages = []
    for pair in pairwise:
        ci = pair["quality"]["decisive_left_win_rate_ci95"]
        if ci is None:
            continue
        if ci["lower"] > 0.5:
            quality_advantages.append({"winner": pair["left"], "loser": pair["right"], "ci95": ci})
        elif ci["upper"] < 0.5:
            quality_advantages.append({"winner": pair["right"], "loser": pair["left"], "ci95": ci})
    if not claude_task_mode_comparable:
        quality_advantages = [
            item
            for item in quality_advantages
            if "claude-sonnet" not in {item["winner"], item["loser"]}
        ]
    native_serial = next(pair for pair in pairwise if pair["left"] == "native-codex" and pair["right"] == "serial-codex")
    routing_supported = bool(
        complete
        and minimum_design
        and release_uniform
        and native_topology_complete
        and codex_usage_complete
        and caps_compliant
        and any(
            {item["winner"], item["loser"]} == {"native-codex", "serial-codex"}
            for item in quality_advantages
        )
    )
    routing_winner = None
    if routing_supported:
        routing_winner = next(
            item["winner"]
            for item in quality_advantages
            if {item["winner"], item["loser"]} == {"native-codex", "serial-codex"}
        )
    blockers = []
    if not complete:
        blockers.append("campaign coverage is incomplete or invalid")
    if not minimum_design:
        blockers.append("minimum three-task, two-repetition design is not met")
    if not release_uniform:
        blockers.append("completed evidence does not use one preregistered runtime release")
    if not native_topology_complete:
        blockers.append("not every native arm observed a direct child")
    if not codex_usage_complete:
        blockers.append("aggregate Codex token accounting is incomplete")
    if not cap_accounting_complete:
        blockers.append("cap compliance accounting is incomplete")
    elif not caps_compliant:
        blockers.append("one or more provider arms exceeded its observed termination threshold")
    if not claude_task_mode_comparable:
        blockers.append("Claude read-only evidence used behavior-changing plan permission mode")
    blockers.extend(
        [
            "Claude and Codex use unequal provider-dollar and weighted-token termination thresholds",
            "Codex authoritative dollar cost is unavailable",
            "the cohort is read-only and does not cover implementation, recovery, safety, or usability",
        ]
    )
    return {
        "coverage_complete": complete,
        "minimum_repeated_design": minimum_design,
        "release_uniform": release_uniform,
        "native_topology_complete": native_topology_complete,
        "codex_usage_complete": codex_usage_complete,
        "cap_accounting_complete": cap_accounting_complete,
        "caps_compliant": caps_compliant,
        "quality_advantages_supported": bool(complete and minimum_design and release_uniform and quality_advantages),
        "quality_advantages": quality_advantages,
        "routing_recommendation_supported": routing_supported,
        "routing_recommendation": routing_winner,
        "native_vs_serial_completed_pairs": native_serial["completed_pairs"],
        "universal_superiority_supported": False,
        "universal_superiority_blockers": blockers,
    }


def _validate_systems(value, budget_profile, source):
    if not isinstance(value, list) or len(value) != 3:
        raise ValidationError("%s systems are invalid" % source)
    systems = {}
    for raw in value:
        item = _object(raw, SYSTEM_FIELDS, "%s system" % source)
        name = item.get("name")
        if name not in READONLY_PARITY_SYSTEMS or name in systems:
            raise ValidationError("%s system name is invalid" % source)
        systems[name] = item
        _non_empty(item.get("model"), "%s system model" % source, 128)
        if item.get("alternative_main_model_allowed") is not False:
            raise ValidationError("%s alternative main models must remain disabled" % source)
    if set(systems) != set(READONLY_PARITY_SYSTEMS):
        raise ValidationError("%s system coverage is invalid" % source)
    serial = systems["serial-codex"]
    native = systems["native-codex"]
    claude = systems["claude-sonnet"]
    expected_caps = READONLY_PARITY_PROFILE_TOKEN_CAPS[budget_profile]
    if (
        serial["provider"] != "openai"
        or native["provider"] != "openai"
        or serial["model"] != native["model"]
        or serial["effort"] != native["effort"]
        or serial["max_tokens"] != expected_caps["serial-codex"]
        or native["max_tokens"] != expected_caps["native-codex"]
        or serial["native_agent_threads"] != 0
        or native["native_agent_threads"] != READONLY_PARITY_NATIVE_THREADS
        or serial["topology"] != "serial"
        or native["topology"] != "native-depth-1"
        or serial["max_turns"] is not None
        or native["max_turns"] is not None
        or serial["cap_enforcement"] != "runtime-weighted-threshold-fail-closed"
        or native["cap_enforcement"] != "runtime-weighted-threshold-fail-closed"
    ):
        raise ValidationError("%s Codex system contract is invalid for %s" % (source, budget_profile))
    if serial["effort"] not in {"low", "medium", "high", "xhigh", "ultra"}:
        raise ValidationError("%s Codex effort is invalid" % source)
    if (
        claude["provider"] != "anthropic"
        or claude["model"] != "sonnet"
        or claude["effort"] != "ultracode"
        or claude["topology"] != "provider-managed"
        or claude["max_tokens"] is not None
        or claude["native_agent_threads"] is not None
        or claude["max_turns"] != READONLY_PARITY_CLAUDE_MAX_TURNS
        or claude["cap_enforcement"] != "provider-dollar-threshold-fail-closed"
    ):
        raise ValidationError("%s Claude Sonnet Ultracode contract is invalid" % source)


def _validate_resources(value, system, source):
    _integer(value.get("duration_ms"), "%s duration_ms" % source, 0, 10**12)
    _integer(value.get("provider_invocations"), "%s provider_invocations" % source, 0, 1000)
    turns = value.get("provider_turns")
    if turns is not None:
        _integer(turns, "%s provider_turns" % source, 0, 1000)
    if not isinstance(value.get("token_observed"), bool) or not isinstance(value.get("cost_observed"), bool):
        raise ValidationError("%s resource observation flags are invalid" % source)
    token_fields = ["input_tokens", "cached_input_tokens", "output_tokens", "gross_tokens"]
    if value["token_observed"]:
        for field in token_fields:
            _integer(value.get(field), "%s %s" % (source, field), 0, 10**12)
        if value["cached_input_tokens"] > value["input_tokens"] or value["gross_tokens"] != value["input_tokens"] + value["output_tokens"]:
            raise ValidationError("%s token totals are inconsistent" % source)
    elif any(value.get(field) is not None for field in token_fields):
        raise ValidationError("%s unavailable token totals must be null" % source)
    weighted = value.get("weighted_tokens")
    if system in {"serial-codex", "native-codex"}:
        if value.get("token_accounting") != "local-rollout-aggregate-v1":
            raise ValidationError("%s Codex token accounting is invalid" % source)
        if value["token_observed"]:
            if weighted != value["input_tokens"] - value["cached_input_tokens"] + value["output_tokens"]:
                raise ValidationError("%s weighted token total is inconsistent" % source)
        elif weighted is not None:
            raise ValidationError("%s unavailable weighted token total must be null" % source)
        allowed_caps = {
            "serial-codex": {
                READONLY_PARITY_MATCHED_CODEX_TOKEN_CAP,
                READONLY_PARITY_PRODUCT_SERIAL_TOKEN_CAP,
            },
            "native-codex": {
                READONLY_PARITY_MATCHED_CODEX_TOKEN_CAP,
                READONLY_PARITY_PRODUCT_NATIVE_TOKEN_CAP,
            },
        }
        if (
            value.get("cap_enforcement") != "runtime-weighted-threshold-fail-closed"
            or value.get("token_cap") not in allowed_caps[system]
            or value.get("cost_cap_usd") is not None
        ):
            raise ValidationError("%s Codex cap contract is invalid" % source)
    else:
        if value.get("token_accounting") != "provider-native-gross-v1" or weighted is not None:
            raise ValidationError("%s Claude token accounting is invalid" % source)
        if value.get("cap_enforcement") != "provider-dollar-threshold-fail-closed" or value.get("token_cap") is not None:
            raise ValidationError("%s Claude cap contract is invalid" % source)
        if not isinstance(value.get("cost_cap_usd"), (int, float)) or isinstance(value.get("cost_cap_usd"), bool) or value["cost_cap_usd"] <= 0:
            raise ValidationError("%s Claude cost cap is invalid" % source)
    if value["cost_observed"]:
        if not isinstance(value.get("cost_usd"), (int, float)) or isinstance(value.get("cost_usd"), bool) or value["cost_usd"] < 0 or not math.isfinite(value["cost_usd"]):
            raise ValidationError("%s cost_usd is invalid" % source)
    elif value.get("cost_usd") is not None:
        raise ValidationError("%s unavailable cost must be null" % source)
    cap_compliant = value.get("cap_compliant")
    cap_overshoot = value.get("cap_overshoot")
    if cap_compliant is not None and not isinstance(cap_compliant, bool):
        raise ValidationError("%s cap_compliant must be boolean or null" % source)
    if cap_overshoot is not None and (
        not isinstance(cap_overshoot, (int, float))
        or isinstance(cap_overshoot, bool)
        or cap_overshoot < 0
        or not math.isfinite(cap_overshoot)
    ):
        raise ValidationError("%s cap_overshoot is invalid" % source)
    if system in {"serial-codex", "native-codex"}:
        observed = value["token_observed"]
        expected_overshoot = max(0, weighted - value["token_cap"]) if observed else None
    else:
        observed = value["cost_observed"]
        expected_overshoot = max(0.0, value["cost_usd"] - value["cost_cap_usd"]) if observed else None
    if not observed:
        if cap_compliant is not None or cap_overshoot is not None:
            raise ValidationError("%s unobserved cap evidence must be null" % source)
    elif (
        cap_compliant is not (expected_overshoot == 0)
        or cap_overshoot is None
        or not math.isclose(
            cap_overshoot,
            expected_overshoot,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    ):
        raise ValidationError("%s cap compliance evidence is inconsistent" % source)


def _validate_topology(value, system, source):
    helpers = value.get("helper_models")
    if not isinstance(helpers, list) or helpers != sorted(set(helpers)) or not all(isinstance(item, str) and 0 < len(item) <= 128 for item in helpers):
        raise ValidationError("%s helper models are invalid" % source)
    if not isinstance(value.get("topology_executed"), bool):
        raise ValidationError("%s topology_executed must be boolean" % source)
    if system == "serial-codex":
        expected = ("serial", 0, 0, True)
    elif system == "native-codex":
        observed = value.get("observed_child_threads")
        if isinstance(observed, bool) or not isinstance(observed, int) or not 0 <= observed <= READONLY_PARITY_NATIVE_THREADS:
            raise ValidationError("%s native observed child count is invalid" % source)
        expected = ("native-depth-1", READONLY_PARITY_NATIVE_THREADS, observed, observed >= 1)
    else:
        expected = ("provider-managed", None, None, True)
    observed_tuple = (
        value.get("kind"),
        value.get("requested_child_threads"),
        value.get("observed_child_threads"),
        value.get("topology_executed"),
    )
    if observed_tuple != expected:
        raise ValidationError("%s topology contract is inconsistent" % source)
    if system != "claude-sonnet" and helpers:
        raise ValidationError("%s Codex helper_models must remain empty" % source)


def _validate_codex_workflow_contract(workflow, campaign, system):
    if workflow.get("max_workers") != 1:
        raise ValidationError("read-only parity Codex workflow must remain serial at the Conductor layer")
    steps = workflow.get("steps")
    if not isinstance(steps, list) or len(steps) != 1:
        raise ValidationError("read-only parity Codex workflow must contain exactly one step")
    step = steps[0]
    system_config = next(item for item in campaign["systems"] if item["name"] == system)
    if (
        step.get("id") != "complete-task"
        or step.get("kind") != "codex_exec"
        or step.get("sandbox") != "read-only"
        or step.get("max_tokens") != system_config["max_tokens"]
        or step.get("model") != system_config["model"]
        or step.get("effort") != system_config["effort"]
    ):
        raise ValidationError("read-only parity Codex workflow configuration is invalid")
    native = step.get("native_agents")
    if system == "native-codex":
        if native != {"max_threads": READONLY_PARITY_NATIVE_THREADS}:
            raise ValidationError("read-only parity native workflow authority is invalid")
    elif native is not None:
        raise ValidationError("read-only parity serial workflow must not authorize native agents")


def _system_contracts(model, effort, budget_profile):
    caps = READONLY_PARITY_PROFILE_TOKEN_CAPS[budget_profile]
    return [
        {
            "name": "serial-codex",
            "provider": "openai",
            "model": model,
            "effort": effort,
            "topology": "serial",
            "max_tokens": caps["serial-codex"],
            "native_agent_threads": 0,
            "max_turns": None,
            "cap_enforcement": "runtime-weighted-threshold-fail-closed",
            "alternative_main_model_allowed": False,
        },
        {
            "name": "native-codex",
            "provider": "openai",
            "model": model,
            "effort": effort,
            "topology": "native-depth-1",
            "max_tokens": caps["native-codex"],
            "native_agent_threads": READONLY_PARITY_NATIVE_THREADS,
            "max_turns": None,
            "cap_enforcement": "runtime-weighted-threshold-fail-closed",
            "alternative_main_model_allowed": False,
        },
        {
            "name": "claude-sonnet",
            "provider": "anthropic",
            "model": "sonnet",
            "effort": "ultracode",
            "topology": "provider-managed",
            "max_tokens": None,
            "native_agent_threads": None,
            "max_turns": READONLY_PARITY_CLAUDE_MAX_TURNS,
            "cap_enforcement": "provider-dollar-threshold-fail-closed",
            "alternative_main_model_allowed": False,
        },
    ]


def _campaign_rows(tasks, seed, repetitions):
    rows = [(repetition, task) for repetition in range(1, repetitions + 1) for task in tasks]
    return sorted(rows, key=lambda item: _sha256_text("%d|row|%d|%s" % (seed, item[0], item[1]["id"])))


def _arm_order(seed, sequence, repetition, task_id):
    first = READONLY_PARITY_SYSTEMS[(sequence - 1 + seed % len(READONLY_PARITY_SYSTEMS)) % len(READONLY_PARITY_SYSTEMS)]
    remaining = [system for system in READONLY_PARITY_SYSTEMS if system != first]
    remaining.sort(key=lambda system: _sha256_text("%d|arm|%d|%s|%s" % (seed, repetition, task_id, system)))
    return [first] + remaining


def _resolve_fixture_root(parity_tasks, fixture_root):
    if fixture_root is not None:
        return Path(fixture_root).resolve()
    source = parity_tasks.get("_source_path") if isinstance(parity_tasks, dict) else None
    if source:
        return (Path(source).resolve().parent / "implementation-canary").resolve()
    raise ValidationError("read-only parity campaign fixture_root is required for in-memory tasks")


def _validate_budget(value, budget_profile, source):
    if not isinstance(value, dict) or set(value) != {"max_minutes", "max_agents", "max_live_tool_cost", "max_cost_usd", "max_tokens"}:
        raise ValidationError("%s budget fields are invalid" % source)
    _integer(value.get("max_minutes"), "%s max_minutes" % source, 1, 1440)
    _integer(value.get("max_agents"), "%s max_agents" % source, 2, 1000)
    if value.get("max_live_tool_cost") not in {"low", "medium", "high"}:
        raise ValidationError("%s max_live_tool_cost is invalid" % source)
    cost = value.get("max_cost_usd")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost <= 0 or not math.isfinite(cost):
        raise ValidationError("%s max_cost_usd is invalid" % source)
    if value.get("max_tokens") != _task_token_cap(budget_profile):
        raise ValidationError("%s max_tokens does not match the campaign budget profile" % source)


def _validate_budget_profile(value):
    if value not in READONLY_PARITY_BUDGET_PROFILES:
        raise ValidationError("read-only parity campaign budget profile is invalid")
    return value


def _task_token_cap(budget_profile):
    return max(READONLY_PARITY_PROFILE_TOKEN_CAPS[budget_profile].values())


def _codex_progress_path(run_dir):
    from .codex_progress import codex_step_progress_path_from_run_dir

    return codex_step_progress_path_from_run_dir(run_dir, "complete-task")


def _record_higher(summary, left, right):
    if left > right:
        summary["left"] += 1
    elif right > left:
        summary["right"] += 1
    else:
        summary["ties"] += 1


def _record_lower(summary, left, right):
    if left < right:
        summary["left"] += 1
    elif right < left:
        summary["right"] += 1
    else:
        summary["ties"] += 1


def _record_resource_to_success(summary, left_result, right_result, left, right, left_observed, right_observed):
    left_success = left_result["outcome"]["final_success"]
    right_success = right_result["outcome"]["final_success"]
    if not left_success and not right_success:
        summary["incomparable"] += 1
        return
    if left_success and not right_success:
        if left_observed:
            summary["left"] += 1
            summary["comparable"] += 1
        else:
            summary["incomparable"] += 1
        return
    if right_success and not left_success:
        if right_observed:
            summary["right"] += 1
            summary["comparable"] += 1
        else:
            summary["incomparable"] += 1
        return
    if not left_observed or not right_observed:
        summary["incomparable"] += 1
        return
    summary["comparable"] += 1
    _record_lower(summary, left, right)


def _wilson_ci95(successes, total):
    if total <= 0:
        return None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1.0 + z * z / total
    center = (proportion + z * z / (2.0 * total)) / denominator
    margin = z * math.sqrt((proportion * (1.0 - proportion) + z * z / (4.0 * total)) / total) / denominator
    return {"lower": max(0.0, center - margin), "upper": min(1.0, center + margin), "method": "wilson-score"}


def _public_tasks(tasks):
    return {key: value for key, value in tasks.items() if not str(key).startswith("_")} if isinstance(tasks, dict) else tasks


def _object(value, fields, label):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError("%s has invalid fields" % label)
    return value


def _safe_id(value, label):
    if not isinstance(value, str) or not value or len(value) > 200 or SAFE_ID.fullmatch(value) is None:
        raise ValidationError("%s must be a safe id" % label)
    return value


def _integer(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError("%s is invalid" % label)
    return value


def _non_empty(value, label, maximum=1000):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValidationError("%s must be a bounded non-empty string" % label)
    return value


def _version(value, label):
    if not isinstance(value, str) or len(value) > 32:
        raise ValidationError("%s is invalid" % label)
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValidationError("%s is invalid" % label)
    return value


def _sha256(value, label):
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValidationError("%s must be lowercase SHA-256" % label)
    return value


def _clean_artifact_path(value, label):
    if not isinstance(value, str) or not value or len(value) > 1000:
        raise ValidationError("%s must be a bounded path" % label)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValidationError("%s must be a safe relative POSIX path" % label)
    return path.as_posix()


def _join_artifact_path(root, child):
    return _clean_artifact_path((PurePosixPath(root) / PurePosixPath(child)).as_posix(), "artifact path")


def _sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _sha256_file(path, label, maximum):
    return hashlib.sha256(read_regular_file_bytes_no_follow(path, label, maximum)).hexdigest()


def _mean(values):
    return sum(values) / len(values) if values else None


def _now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _reject_duplicate_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value
