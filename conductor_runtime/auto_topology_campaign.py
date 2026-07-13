import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from . import __version__
from .auto_orchestrator import load_auto_orchestration_receipt
from .benchmark import PARITY_TASKS_SCHEMA, validate_parity_tasks
from .build_identity import current_runtime_build_sha256
from .errors import ValidationError
from .goal_loop import load_goal_state
from .model_planner import load_model_workflow_receipt
from .model_orchestrator import load_model_workflow_run_receipt
from .paths import default_generated_dir
from .security import (
    ensure_dir_no_follow,
    read_regular_file_bytes_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    write_new_text_file_no_follow,
)
from .staged_workspace import snapshot_workspace
from .workflow import MAX_AGENT_WORKERS, SAFE_ID


AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V1 = "conductor.auto_topology_campaign.v1"
AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V2 = "conductor.auto_topology_campaign.v2"
AUTO_TOPOLOGY_CAMPAIGN_SCHEMA = "conductor.auto_topology_campaign.v3"
AUTO_TOPOLOGY_STATUS_SCHEMA = "conductor.auto_topology_campaign_status.v2"
AUTO_TOPOLOGY_RESULT_SCHEMA_V1 = "conductor.auto_topology_arm_result.v1"
AUTO_TOPOLOGY_RESULT_SCHEMA_V2 = "conductor.auto_topology_arm_result.v2"
AUTO_TOPOLOGY_RESULT_SCHEMA = "conductor.auto_topology_arm_result.v3"
AUTO_TOPOLOGY_LAUNCH_SCHEMA = "conductor.auto_topology_arm_launch.v1"
AUTO_TOPOLOGY_EVALUATION_SCHEMA = "conductor.implementation_canary_workspace_evaluation.v1"
AUTO_TOPOLOGY_EVIDENCE_STATUS = "planned-not-evidence"
AUTO_TOPOLOGY_RANDOMIZATION = "sha256-rank-rotating-v1"
AUTO_TOPOLOGIES = ("direct", "progressive", "plan-first")
AUTO_TOPOLOGY_SPECS = {
    "direct": {
        "strategy": "direct",
        "planning_policy": "always-direct",
    },
    "progressive": {
        "strategy": "progressive",
        "planning_policy": "direct-first-then-model",
    },
    "plan-first": {
        "strategy": "goal",
        "planning_policy": "model-first",
    },
}
MAX_AUTO_TOPOLOGY_CAMPAIGN_BYTES = 4 * 1024 * 1024
MAX_AUTO_TOPOLOGY_RESULT_BYTES = 2 * 1024 * 1024
MAX_AUTO_TOPOLOGY_EVALUATION_BYTES = 2 * 1024 * 1024
MAX_AUTO_TOPOLOGY_REPETITIONS = 100
MAX_AUTO_TOPOLOGY_COHORTS = 10000
MAX_AUTO_TOPOLOGY_ITERATIONS = 10

CAMPAIGN_FIELDS_V1 = {
    "schema",
    "name",
    "generated_at_utc",
    "evidence_status",
    "source",
    "design",
    "model",
    "topologies",
    "cohorts",
    "claim_gate",
}
CAMPAIGN_FIELDS_V2 = CAMPAIGN_FIELDS_V1 | {"runtime_version", "runtime_build_sha256"}
CAMPAIGN_FIELDS = set(CAMPAIGN_FIELDS_V2)
SOURCE_FIELDS = {
    "parity_tasks_schema",
    "parity_tasks_source",
    "parity_tasks_sha256",
    "suite",
    "task_count",
    "required_dimensions",
}
DESIGN_FIELDS = {
    "seed",
    "repetitions",
    "randomization_algorithm",
    "cohort_count",
    "arm_count",
    "max_iterations",
    "parallel_workers",
    "topology_first_counts",
    "resource_policy",
}
MODEL_FIELDS = {"provider", "name", "effort", "alternative_model_allowed"}
TOPOLOGY_FIELDS = {"name", "strategy", "max_workers", "planning_policy"}
COHORT_FIELDS_V2 = {
    "id",
    "sequence",
    "task_id",
    "repetition",
    "task_contract_sha256",
    "task_prompt_sha256",
    "source_fixture_sha256",
    "held_out_fixture_sha256",
    "budget",
    "arm_order",
    "artifacts",
    "status",
}
COHORT_FIELDS = COHORT_FIELDS_V2 | {"task_execution_sha256"}
ARM_ARTIFACT_FIELDS = {
    "root",
    "workspace",
    "runs",
    "goals",
    "planning",
    "auto_receipt",
    "goal",
    "launch",
    "evaluation",
    "result",
}
CLAIM_GATE_FIELDS = {"eligible_for_topology_claims", "required_before_claim"}
BUDGET_FIELDS = {"max_minutes", "max_agents", "max_live_tool_cost", "max_cost_usd", "max_tokens"}
LAUNCH_FIELDS = {
    "schema",
    "campaign_sha256",
    "cohort_id",
    "task_id",
    "topology",
    "state",
    "source_fingerprint_sha256",
    "auto_receipt_sha256",
    "goal_sha256",
    "updated_at_utc",
}
RESULT_FIELDS_V1 = {
    "schema",
    "campaign_sha256",
    "cohort_id",
    "task_id",
    "repetition",
    "topology",
    "generated_at_utc",
    "status",
    "outcome",
    "resources",
    "progressive",
    "artifacts",
}
RESULT_FIELDS_V2 = RESULT_FIELDS_V1 | {"runtime_version", "runtime_build_sha256"}
RESULT_FIELDS = set(RESULT_FIELDS_V2)
OUTCOME_FIELDS = {
    "auto_status",
    "execution_started",
    "goal_satisfied",
    "score",
    "max_score",
    "passed",
}
RESOURCE_FIELDS = {
    "duration_ms",
    "planning_provider_calls",
    "worker_provider_calls",
    "verifier_provider_calls",
    "total_provider_calls",
    "token_observed",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_observed",
    "cost_usd",
}
PROGRESSIVE_FIELDS = {"escalated", "first_pass_success"}
RESULT_ARTIFACT_FIELDS = {
    "auto_receipt_sha256",
    "goal_sha256",
    "evaluation_sha256",
    "workspace_sha256",
    "runs",
    "model_workflow_receipts",
}
RUN_RECORD_FIELDS = {"run_id", "state_sha256", "workflow_sha256"}
MODEL_WORKFLOW_RECORD_FIELDS = {"kind", "filename", "sha256"}
EVALUATION_FIELDS = {
    "schema",
    "evaluator",
    "task_id",
    "source_fixture_sha256",
    "workspace_sha256",
    "held_out_fixture_sha256",
    "changes",
    "visible",
    "held_out",
    "scope",
    "structure",
    "orchestration_completed",
    "criteria",
    "score",
    "max_score",
    "passed",
    "evaluation_sha256",
}
EXPECTED_CRITERIA = (
    ("held-out-correctness", 5),
    ("visible-test-correctness", 2),
    ("scope-control", 1),
    ("maintainability", 1),
    ("orchestration-completion", 1),
)


def render_auto_topology_task_contract(task: Dict) -> str:
    prompt = str(task["prompt"]).strip()
    criteria = list(task["success_criteria"])
    evidence = list(task["required_evidence"])
    lines = [
        prompt,
        "",
        "Acceptance criteria:",
        *["- %s" % item for item in criteria],
        "",
        "Required evidence:",
        *["- %s" % item for item in evidence],
    ]
    return "\n".join(lines).strip()


def build_auto_topology_campaign(
    parity_tasks: Dict,
    *,
    run_id: str,
    seed: int,
    repetitions: int,
    artifacts_dir: str,
    model: str,
    effort: str,
    max_iterations: int,
    parallel_workers: int,
    fixture_root: Optional[Path] = None,
) -> Dict:
    public_tasks = _public_tasks(parity_tasks)
    validate_parity_tasks(public_tasks)
    _safe_id(run_id, "auto topology campaign run_id")
    _integer(seed, "auto topology campaign seed", 0, 2**63 - 1)
    _integer(repetitions, "auto topology campaign repetitions", 1, MAX_AUTO_TOPOLOGY_REPETITIONS)
    _integer(max_iterations, "auto topology campaign max_iterations", 1, MAX_AUTO_TOPOLOGY_ITERATIONS)
    _integer(parallel_workers, "auto topology campaign parallel_workers", 2, MAX_AGENT_WORKERS)
    if not isinstance(model, str) or not model.strip() or len(model) > 128:
        raise ValidationError("auto topology campaign model must be a bounded non-empty string")
    if effort not in {"low", "medium", "high", "xhigh", "ultra"}:
        raise ValidationError("auto topology campaign effort is invalid")
    artifact_root = _clean_artifact_path(artifacts_dir, "auto topology campaign artifacts_dir")
    tasks = public_tasks["tasks"]
    if len(tasks) * repetitions > MAX_AUTO_TOPOLOGY_COHORTS:
        raise ValidationError("auto topology campaign exceeds the supported cohort count")
    for task in tasks:
        if task["budget"]["max_agents"] < parallel_workers:
            raise ValidationError(
                "auto topology task %s max_agents must cover parallel_workers" % task["id"]
            )
    fixture_contracts = _fixture_contracts(tasks, source_path=parity_tasks.get("_source_path"), fixture_root=fixture_root)

    rows, arm_orders = _campaign_design(tasks, seed, repetitions)
    cohorts = []
    for sequence, (repetition, task) in enumerate(rows, start=1):
        cohort_id = "r%03d-%s" % (repetition, task["id"])
        cohort_root = _join_artifact_path(artifact_root, cohort_id)
        artifacts = {}
        for topology in AUTO_TOPOLOGIES:
            arm_root = _join_artifact_path(cohort_root, topology)
            goal_id = "%s-%s" % (cohort_id, topology)
            artifacts[topology] = {
                "root": arm_root,
                "workspace": _join_artifact_path(arm_root, "workspace"),
                "runs": _join_artifact_path(arm_root, "runs"),
                "goals": _join_artifact_path(arm_root, "goals"),
                "planning": _join_artifact_path(arm_root, "planning"),
                "auto_receipt": _join_artifact_path(arm_root, "auto-receipt.json"),
                "goal": _join_artifact_path(arm_root, "goals/%s.json" % goal_id),
                "launch": _join_artifact_path(arm_root, "launch.json"),
                "evaluation": _join_artifact_path(arm_root, "evaluation.json"),
                "result": _join_artifact_path(arm_root, "result.json"),
            }
        cohorts.append(
            {
                "id": cohort_id,
                "sequence": sequence,
                "task_id": task["id"],
                "repetition": repetition,
                "task_contract_sha256": _sha256_json(task),
                "task_prompt_sha256": _sha256_text(task["prompt"].strip()),
                "task_execution_sha256": _sha256_text(
                    render_auto_topology_task_contract(task)
                ),
                "source_fixture_sha256": fixture_contracts[task["id"]]["source"],
                "held_out_fixture_sha256": fixture_contracts[task["id"]]["held_out"],
                "budget": dict(task["budget"]),
                "arm_order": arm_orders[(repetition, task["id"])],
                "artifacts": artifacts,
                "status": "planned",
            }
        )
    first_counts = {
        topology: sum(1 for cohort in cohorts if cohort["arm_order"][0] == topology)
        for topology in AUTO_TOPOLOGIES
    }
    source_path = parity_tasks.get("_source_path") if isinstance(parity_tasks, dict) else None
    campaign = {
        "schema": AUTO_TOPOLOGY_CAMPAIGN_SCHEMA,
        "name": run_id,
        "generated_at_utc": _now_utc(),
        "evidence_status": AUTO_TOPOLOGY_EVIDENCE_STATUS,
        "runtime_version": __version__,
        "runtime_build_sha256": current_runtime_build_sha256(),
        "source": {
            "parity_tasks_schema": PARITY_TASKS_SCHEMA,
            "parity_tasks_source": str(source_path) if source_path else None,
            "parity_tasks_sha256": _sha256_json(public_tasks),
            "suite": public_tasks["name"],
            "task_count": len(tasks),
            "required_dimensions": list(public_tasks.get("required_dimensions", [])),
        },
        "design": {
            "seed": seed,
            "repetitions": repetitions,
            "randomization_algorithm": AUTO_TOPOLOGY_RANDOMIZATION,
            "cohort_count": len(cohorts),
            "arm_count": len(cohorts) * len(AUTO_TOPOLOGIES),
            "max_iterations": max_iterations,
            "parallel_workers": parallel_workers,
            "topology_first_counts": first_counts,
            "resource_policy": "measured-observed; worker rollout caps remain per-call",
        },
        "model": {
            "provider": "openai",
            "name": model.strip(),
            "effort": effort,
            "alternative_model_allowed": False,
        },
        "topologies": [
            {
                "name": topology,
                "strategy": AUTO_TOPOLOGY_SPECS[topology]["strategy"],
                "max_workers": 1 if topology == "direct" else parallel_workers,
                "planning_policy": AUTO_TOPOLOGY_SPECS[topology]["planning_policy"],
            }
            for topology in AUTO_TOPOLOGIES
        ],
        "cohorts": cohorts,
        "claim_gate": {
            "eligible_for_topology_claims": False,
            "required_before_claim": [
                "execute every preregistered arm without dropping failed or interrupted outcomes",
                "retain independent hidden evaluation for every arm",
                "compare only matched task repetitions with complete token and latency observations",
                "label quality-latency-token recommendations separately from all-resource recommendations",
                "require authoritative dollar observations before making an all-resource recommendation",
                "require repeated task evidence and paired uncertainty before either recommendation",
            ],
        },
    }
    validate_auto_topology_campaign(campaign)
    return campaign


def validate_auto_topology_campaign(campaign: Dict, source: str = "<memory>") -> None:
    if not isinstance(campaign, dict):
        raise ValidationError("%s must be an object" % source)
    campaign_schema = campaign.get("schema")
    if campaign_schema == AUTO_TOPOLOGY_CAMPAIGN_SCHEMA:
        fields = CAMPAIGN_FIELDS
    elif campaign_schema == AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V2:
        fields = CAMPAIGN_FIELDS_V2
    elif campaign_schema == AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V1:
        fields = CAMPAIGN_FIELDS_V1
    else:
        raise ValidationError("%s has an unsupported auto topology campaign schema" % source)
    root = _object(campaign, fields, source)
    if campaign_schema in {
        AUTO_TOPOLOGY_CAMPAIGN_SCHEMA,
        AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V2,
    }:
        _version(root.get("runtime_version"), "%s runtime_version" % source)
        _sha256(root.get("runtime_build_sha256"), "%s runtime_build_sha256" % source)
    _safe_id(root.get("name"), "%s name" % source)
    _non_empty(root.get("generated_at_utc"), "%s generated_at_utc" % source)
    if root.get("evidence_status") != AUTO_TOPOLOGY_EVIDENCE_STATUS:
        raise ValidationError("%s evidence_status is invalid" % source)
    source_value = _object(root.get("source"), SOURCE_FIELDS, "%s source" % source)
    if source_value.get("parity_tasks_schema") != PARITY_TASKS_SCHEMA:
        raise ValidationError("%s source parity_tasks_schema is invalid" % source)
    source_path = source_value.get("parity_tasks_source")
    if source_path is not None and not isinstance(source_path, str):
        raise ValidationError("%s source parity_tasks_source must be string or null" % source)
    _sha256(source_value.get("parity_tasks_sha256"), "%s source parity_tasks_sha256" % source)
    _non_empty(source_value.get("suite"), "%s source suite" % source)
    _integer(source_value.get("task_count"), "%s source task_count" % source, 1, MAX_AUTO_TOPOLOGY_COHORTS)
    dimensions = source_value.get("required_dimensions")
    if not isinstance(dimensions, list) or not all(isinstance(item, str) and item for item in dimensions):
        raise ValidationError("%s source required_dimensions must be a string array" % source)

    design = _object(root.get("design"), DESIGN_FIELDS, "%s design" % source)
    _integer(design.get("seed"), "%s design seed" % source, 0, 2**63 - 1)
    _integer(design.get("repetitions"), "%s design repetitions" % source, 1, MAX_AUTO_TOPOLOGY_REPETITIONS)
    _integer(design.get("max_iterations"), "%s design max_iterations" % source, 1, MAX_AUTO_TOPOLOGY_ITERATIONS)
    _integer(design.get("parallel_workers"), "%s design parallel_workers" % source, 2, MAX_AGENT_WORKERS)
    if design.get("randomization_algorithm") != AUTO_TOPOLOGY_RANDOMIZATION:
        raise ValidationError("%s design randomization_algorithm is unsupported" % source)
    if design.get("resource_policy") != "measured-observed; worker rollout caps remain per-call":
        raise ValidationError("%s design resource_policy is invalid" % source)

    model = _object(root.get("model"), MODEL_FIELDS, "%s model" % source)
    if model.get("provider") != "openai" or model.get("alternative_model_allowed") is not False:
        raise ValidationError("%s model authority must remain fixed to OpenAI" % source)
    _non_empty(model.get("name"), "%s model name" % source, 128)
    if model.get("effort") not in {"low", "medium", "high", "xhigh", "ultra"}:
        raise ValidationError("%s model effort is invalid" % source)
    _validate_topologies(root.get("topologies"), design, source)
    cohorts = _validate_cohorts(
        root.get("cohorts"),
        source_value,
        design,
        campaign_schema,
        source,
    )
    expected_first = {
        topology: sum(1 for cohort in cohorts if cohort["arm_order"][0] == topology)
        for topology in AUTO_TOPOLOGIES
    }
    if design.get("topology_first_counts") != expected_first:
        raise ValidationError("%s design topology_first_counts is inconsistent" % source)
    if design.get("cohort_count") != len(cohorts) or design.get("arm_count") != len(cohorts) * 3:
        raise ValidationError("%s design cohort or arm count is inconsistent" % source)
    gate = _object(root.get("claim_gate"), CLAIM_GATE_FIELDS, "%s claim_gate" % source)
    if gate.get("eligible_for_topology_claims") is not False:
        raise ValidationError("%s planned campaign cannot be claim-eligible" % source)
    required = gate.get("required_before_claim")
    if not isinstance(required, list) or not required or not all(isinstance(item, str) and item for item in required):
        raise ValidationError("%s claim_gate required_before_claim must be a non-empty string array" % source)


def load_auto_topology_campaign(path: Path) -> Dict:
    raw = read_regular_text_file_no_follow(path, "auto topology campaign", MAX_AUTO_TOPOLOGY_CAMPAIGN_BYTES)
    try:
        campaign = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    validate_auto_topology_campaign(campaign, source=str(path))
    return campaign


def write_auto_topology_campaign(campaign: Dict, output: Path) -> Path:
    validate_auto_topology_campaign(campaign, source=str(output))
    reject_symlink_path(output, "auto topology campaign output")
    if output.exists():
        raise ValidationError("auto topology campaign output already exists")
    parent_fd = ensure_dir_no_follow(output.parent, "auto topology campaign output parent")
    os.close(parent_fd)
    try:
        write_new_text_file_no_follow(
            output,
            "auto topology campaign output",
            json.dumps(campaign, indent=2, sort_keys=True) + "\n",
            sync=True,
        )
    except FileExistsError:
        raise ValidationError("auto topology campaign output already exists")
    return output


def auto_topology_campaign_status(campaign_path: Path) -> Dict:
    campaign_path = Path(campaign_path)
    campaign = load_auto_topology_campaign(campaign_path)
    campaign_sha256 = _sha256_file(campaign_path, "auto topology campaign", MAX_AUTO_TOPOLOGY_CAMPAIGN_BYTES)
    cohorts = []
    all_arms = []
    lifecycle = {"completed": 0, "partial": 0, "pending": 0, "invalid": 0}
    arm_lifecycle = {"completed": 0, "partial": 0, "pending": 0, "invalid": 0}
    for cohort in campaign["cohorts"]:
        arms = {}
        for topology in AUTO_TOPOLOGIES:
            arm = _arm_status(campaign_path, campaign, campaign_sha256, cohort, topology)
            arms[topology] = arm
            all_arms.append(arm)
            arm_lifecycle[arm["status"]] += 1
        statuses = [arms[topology]["status"] for topology in AUTO_TOPOLOGIES]
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
                "arms": arms,
            }
        )
    planned = len(campaign["cohorts"])
    lifecycle["coverage"] = lifecycle["completed"] / planned if planned else 0.0
    completed_arms = [arm for arm in all_arms if arm["status"] == "completed"]
    topology_summary = {
        topology: _topology_summary([arm for arm in completed_arms if arm["topology"] == topology])
        for topology in AUTO_TOPOLOGIES
    }
    pairwise = [
        _pairwise_summary(cohorts, left, right)
        for left, right in [("direct", "progressive"), ("direct", "plan-first"), ("progressive", "plan-first")]
    ]
    resources = _resource_coverage(completed_arms, arm_lifecycle["completed"])
    inference = _topology_inference(campaign, lifecycle, arm_lifecycle, resources, pairwise, cohorts)
    return {
        "schema": AUTO_TOPOLOGY_STATUS_SCHEMA,
        "campaign": {
            "name": campaign["name"],
            "planned_cohorts": planned,
            "planned_arms": planned * 3,
            "runtime_version": campaign.get("runtime_version"),
            "runtime_build_sha256": campaign.get("runtime_build_sha256"),
            "release_bound": campaign["schema"] != AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V1,
            "sha256": campaign_sha256,
        },
        "lifecycle": lifecycle,
        "arm_lifecycle": arm_lifecycle,
        "topologies": topology_summary,
        "pairwise": pairwise,
        "resources": resources,
        "inference": inference,
        "cohorts": cohorts,
    }


def load_auto_topology_launch(path: Path) -> Dict:
    raw = read_regular_text_file_no_follow(path, "auto topology arm launch", MAX_AUTO_TOPOLOGY_RESULT_BYTES)
    try:
        launch = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    validate_auto_topology_launch(launch, source=str(path))
    return launch


def validate_auto_topology_launch(launch: Dict, source: str = "<memory>") -> None:
    root = _object(launch, LAUNCH_FIELDS, source)
    if root.get("schema") != AUTO_TOPOLOGY_LAUNCH_SCHEMA:
        raise ValidationError("%s launch schema is invalid" % source)
    _sha256(root.get("campaign_sha256"), "%s campaign_sha256" % source)
    _safe_id(root.get("cohort_id"), "%s cohort_id" % source)
    _safe_id(root.get("task_id"), "%s task_id" % source)
    if root.get("topology") not in AUTO_TOPOLOGIES:
        raise ValidationError("%s topology is invalid" % source)
    if root.get("state") not in {"launching", "captured", "finalized"}:
        raise ValidationError("%s launch state is invalid" % source)
    _sha256(root.get("source_fingerprint_sha256"), "%s source fingerprint" % source)
    for field in ["auto_receipt_sha256", "goal_sha256"]:
        value = root.get(field)
        if value is not None:
            _sha256(value, "%s %s" % (source, field))
    if root["state"] == "launching" and (root["auto_receipt_sha256"] is not None or root["goal_sha256"] is not None):
        raise ValidationError("%s launching receipt cannot capture terminal hashes" % source)
    if root["state"] in {"captured", "finalized"} and root["auto_receipt_sha256"] is None:
        raise ValidationError("%s captured launch requires an auto receipt hash" % source)
    _non_empty(root.get("updated_at_utc"), "%s updated_at_utc" % source)


def load_auto_topology_result(path: Path) -> Dict:
    raw = read_regular_text_file_no_follow(path, "auto topology arm result", MAX_AUTO_TOPOLOGY_RESULT_BYTES)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    if not isinstance(result, dict):
        raise ValidationError("%s must be an object" % path)
    schema = result.get("schema")
    if schema == AUTO_TOPOLOGY_RESULT_SCHEMA:
        fields = RESULT_FIELDS
    elif schema == AUTO_TOPOLOGY_RESULT_SCHEMA_V2:
        fields = RESULT_FIELDS_V2
    elif schema == AUTO_TOPOLOGY_RESULT_SCHEMA_V1:
        fields = RESULT_FIELDS_V1
    else:
        raise ValidationError("%s has an unsupported auto topology result schema" % path)
    _object(result, fields, str(path))
    return result


def validate_auto_topology_evaluation(evaluation: Dict, source: str = "<memory>") -> None:
    root = _object(evaluation, EVALUATION_FIELDS, source)
    if root.get("schema") != AUTO_TOPOLOGY_EVALUATION_SCHEMA:
        raise ValidationError("%s evaluation schema is invalid" % source)
    _safe_id(root.get("task_id"), "%s task_id" % source)
    evaluator = root.get("evaluator")
    if evaluator != {"identity": "implementation-canary-workspace-evaluator-v1", "independent": True}:
        raise ValidationError("%s evaluator identity is invalid" % source)
    for field in ["source_fixture_sha256", "workspace_sha256", "held_out_fixture_sha256"]:
        _sha256(root.get(field), "%s %s" % (source, field))
    changes = root.get("changes")
    if not isinstance(changes, dict):
        raise ValidationError("%s changes must be an object" % source)
    for field in ["added", "modified", "deleted", "binary", "mode_changed", "unpatchable"]:
        values = changes.get(field)
        if not isinstance(values, list) or values != sorted(set(values)) or not all(isinstance(item, str) for item in values):
            raise ValidationError("%s changes %s must be sorted unique strings" % (source, field))
    expected_change_count = len(changes["added"]) + len(changes["modified"]) + len(changes["deleted"])
    if changes.get("change_count") != expected_change_count:
        raise ValidationError("%s changes change_count is inconsistent" % source)
    for field in ["visible", "held_out", "scope", "structure"]:
        value = root.get(field)
        if not isinstance(value, dict) or not isinstance(value.get("passed" if field != "structure" else "maintainable"), bool):
            raise ValidationError("%s %s evidence is invalid" % (source, field))
    if not isinstance(root.get("orchestration_completed"), bool):
        raise ValidationError("%s orchestration_completed must be boolean" % source)
    criteria = root.get("criteria")
    if not isinstance(criteria, list) or len(criteria) != len(EXPECTED_CRITERIA):
        raise ValidationError("%s criteria are invalid" % source)
    observed_score = 0
    for item, (criterion_id, maximum) in zip(criteria, EXPECTED_CRITERIA):
        if not isinstance(item, dict) or set(item) != {"id", "score", "max_score", "passed"}:
            raise ValidationError("%s criterion is invalid" % source)
        if item["id"] != criterion_id or item["max_score"] != maximum or not isinstance(item["passed"], bool):
            raise ValidationError("%s criterion contract is invalid" % source)
        expected_score = maximum if item["passed"] else 0
        if item["score"] != expected_score:
            raise ValidationError("%s criterion score is inconsistent" % source)
        observed_score += expected_score
    expected_flags = [
        root["held_out"]["passed"],
        root["visible"]["passed"],
        root["scope"]["passed"],
        bool(root["changes"]["change_count"] > 0 and root["structure"]["maintainable"]),
        bool(root["changes"]["change_count"] > 0 and root["orchestration_completed"]),
    ]
    if [item["passed"] for item in criteria] != expected_flags:
        raise ValidationError("%s criteria do not match evaluation evidence" % source)
    if root.get("score") != observed_score or root.get("max_score") != 10 or root.get("passed") != (observed_score == 10):
        raise ValidationError("%s aggregate score is inconsistent" % source)
    expected_hash = _sha256_json({key: value for key, value in root.items() if key != "evaluation_sha256"})
    if root.get("evaluation_sha256") != expected_hash:
        raise ValidationError("%s evaluation_sha256 is invalid" % source)


def validate_auto_topology_result(
    result: Dict,
    *,
    campaign_path: Path,
    campaign: Dict,
    campaign_sha256: str,
    cohort: Dict,
    topology: str,
) -> Dict:
    source = "auto topology result %s/%s" % (cohort["id"], topology)
    campaign_schema = campaign["schema"]
    legacy = campaign_schema == AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V1
    if legacy:
        result_fields = RESULT_FIELDS_V1
        result_schema = AUTO_TOPOLOGY_RESULT_SCHEMA_V1
    elif campaign_schema == AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V2:
        result_fields = RESULT_FIELDS_V2
        result_schema = AUTO_TOPOLOGY_RESULT_SCHEMA_V2
    else:
        result_fields = RESULT_FIELDS
        result_schema = AUTO_TOPOLOGY_RESULT_SCHEMA
    root = _object(result, result_fields, source)
    expected_identity = {
        "schema": result_schema,
        "campaign_sha256": campaign_sha256,
        "cohort_id": cohort["id"],
        "task_id": cohort["task_id"],
        "repetition": cohort["repetition"],
        "topology": topology,
        "status": "finalized",
    }
    for field, expected in expected_identity.items():
        if root.get(field) != expected:
            raise ValidationError("%s %s is invalid" % (source, field))
    if not legacy:
        if (
            root.get("runtime_version") != campaign["runtime_version"]
            or root.get("runtime_build_sha256") != campaign["runtime_build_sha256"]
        ):
            raise ValidationError("%s runtime release does not match the campaign" % source)
    _non_empty(root.get("generated_at_utc"), "%s generated_at_utc" % source)
    artifacts = _object(root.get("artifacts"), RESULT_ARTIFACT_FIELDS, "%s artifacts" % source)
    arm_paths = resolve_auto_topology_arm_paths(campaign_path, cohort, topology)
    workspace_snapshot = snapshot_workspace(arm_paths["workspace"])
    if artifacts.get("workspace_sha256") != workspace_snapshot.fingerprint_sha256:
        raise ValidationError("%s workspace fingerprint drifted" % source)
    auto_hash = _sha256_file(arm_paths["auto_receipt"], "auto topology auto receipt", MAX_AUTO_TOPOLOGY_RESULT_BYTES)
    if artifacts.get("auto_receipt_sha256") != auto_hash:
        raise ValidationError("%s auto receipt hash is invalid" % source)
    auto_receipt = load_auto_orchestration_receipt(arm_paths["auto_receipt"])
    _validate_auto_receipt_contract(auto_receipt, campaign, cohort, topology, source)
    goal = None
    goal_hash = None
    if arm_paths["goal"].exists() or arm_paths["goal"].is_symlink():
        goal_hash = _sha256_file(arm_paths["goal"], "auto topology goal", MAX_AUTO_TOPOLOGY_RESULT_BYTES)
        goal = load_goal_state(arm_paths["goal"])
        _validate_goal_contract(goal, campaign, cohort, topology, source)
    if artifacts.get("goal_sha256") != goal_hash:
        raise ValidationError("%s goal hash is invalid" % source)
    evaluation_raw = read_regular_text_file_no_follow(
        arm_paths["evaluation"], "auto topology evaluation", MAX_AUTO_TOPOLOGY_EVALUATION_BYTES
    )
    try:
        evaluation = json.loads(evaluation_raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("%s evaluation is invalid JSON: %s" % (source, exc))
    validate_auto_topology_evaluation(evaluation, source="%s evaluation" % source)
    evaluation_file_hash = hashlib.sha256(evaluation_raw.encode("utf-8")).hexdigest()
    if artifacts.get("evaluation_sha256") != evaluation_file_hash:
        raise ValidationError("%s evaluation file hash is invalid" % source)
    if evaluation["task_id"] != cohort["task_id"] or evaluation["workspace_sha256"] != workspace_snapshot.fingerprint_sha256:
        raise ValidationError("%s evaluation is not bound to the arm workspace" % source)
    if (
        evaluation["source_fixture_sha256"] != cohort["source_fixture_sha256"]
        or evaluation["held_out_fixture_sha256"] != cohort["held_out_fixture_sha256"]
    ):
        raise ValidationError("%s evaluation fixture contract drifted" % source)

    resources, run_records, model_workflow_records = _derive_resources(
        goal,
        workspace=arm_paths["workspace"],
        runs_dir=arm_paths["runs"],
        planning_dir=arm_paths["planning"],
        duration_ms=root.get("resources", {}).get("duration_ms") if isinstance(root.get("resources"), dict) else None,
    )
    if artifacts.get("runs") != run_records or artifacts.get("model_workflow_receipts") != model_workflow_records:
        raise ValidationError("%s execution artifact records are inconsistent" % source)
    observed_resources = _object(root.get("resources"), RESOURCE_FIELDS, "%s resources" % source)
    if observed_resources != resources:
        raise ValidationError("%s resource summary is inconsistent" % source)
    outcome = _object(root.get("outcome"), OUTCOME_FIELDS, "%s outcome" % source)
    expected_outcome = {
        "auto_status": auto_receipt["status"],
        "execution_started": bool(auto_receipt["result"]["execution_started"]),
        "goal_satisfied": bool(goal is not None and goal.get("status") == "completed" and auto_receipt["status"] == "completed"),
        "score": evaluation["score"],
        "max_score": evaluation["max_score"],
        "passed": evaluation["passed"],
    }
    if outcome != expected_outcome:
        raise ValidationError("%s outcome is inconsistent" % source)
    progressive = _object(root.get("progressive"), PROGRESSIVE_FIELDS, "%s progressive" % source)
    iterations = goal.get("iterations", []) if isinstance(goal, dict) else []
    expected_progressive = {
        "escalated": topology == "progressive" and resources["planning_provider_calls"] > 0,
        "first_pass_success": bool(goal is not None and goal.get("status") == "completed" and len(iterations) == 1),
    }
    if progressive != expected_progressive:
        raise ValidationError("%s progressive summary is inconsistent" % source)
    return root


def resolve_auto_topology_arm_paths(campaign_path: Path, cohort: Dict, topology: str) -> Dict[str, Path]:
    if topology not in AUTO_TOPOLOGIES:
        raise ValidationError("auto topology name is invalid")
    base = Path(campaign_path).resolve().parent
    resolved = {}
    for key, value in cohort["artifacts"][topology].items():
        path = (base / value).resolve()
        if not _is_relative_to(path, base):
            raise ValidationError("auto topology artifact escapes the campaign directory")
        resolved[key] = path
    return resolved


def auto_topology_campaign_sha256(path: Path) -> str:
    return _sha256_file(path, "auto topology campaign", MAX_AUTO_TOPOLOGY_CAMPAIGN_BYTES)


def _arm_status(
    campaign_path: Path,
    campaign: Dict,
    campaign_sha256: str,
    cohort: Dict,
    topology: str,
) -> Dict:
    arm = {
        "topology": topology,
        "status": "pending",
        "reason": "",
        "outcome": None,
        "resources": None,
        "progressive": None,
    }
    try:
        paths = resolve_auto_topology_arm_paths(campaign_path, cohort, topology)
        present = [key for key, path in paths.items() if path.exists() or path.is_symlink()]
        if not present:
            return arm
        for path in paths.values():
            reject_symlink_path(path, "auto topology arm artifact")
        if not paths["launch"].is_file():
            raise ValidationError("arm artifacts exist without a launch receipt")
        launch = load_auto_topology_launch(paths["launch"])
        if (
            launch["campaign_sha256"] != campaign_sha256
            or launch["cohort_id"] != cohort["id"]
            or launch["task_id"] != cohort["task_id"]
            or launch["topology"] != topology
            or launch["source_fingerprint_sha256"] != cohort["source_fixture_sha256"]
        ):
            raise ValidationError("arm launch receipt does not match the campaign")
        if launch["state"] in {"captured", "finalized"}:
            observed_auto_hash = _sha256_file(
                paths["auto_receipt"],
                "auto topology launch auto receipt",
                MAX_AUTO_TOPOLOGY_RESULT_BYTES,
            )
            observed_goal_hash = (
                _sha256_file(paths["goal"], "auto topology launch goal", MAX_AUTO_TOPOLOGY_RESULT_BYTES)
                if paths["goal"].exists() or paths["goal"].is_symlink()
                else None
            )
            if (
                launch["auto_receipt_sha256"] != observed_auto_hash
                or launch["goal_sha256"] != observed_goal_hash
            ):
                raise ValidationError("arm launch receipt artifact hashes drifted")
        if not paths["result"].is_file():
            arm["status"] = "partial"
            arm["reason"] = "launch-%s" % launch["state"]
            return arm
        result = load_auto_topology_result(paths["result"])
        validated = validate_auto_topology_result(
            result,
            campaign_path=campaign_path,
            campaign=campaign,
            campaign_sha256=campaign_sha256,
            cohort=cohort,
            topology=topology,
        )
        if launch["state"] != "finalized":
            arm["status"] = "partial"
            arm["reason"] = "result-present-launch-%s" % launch["state"]
            return arm
        arm.update(
            {
                "status": "completed",
                "reason": (
                    ""
                    if validated["outcome"]["passed"]
                    else _goal_error_class(paths["goal"])
                ),
                "outcome": validated["outcome"],
                "resources": validated["resources"],
                "progressive": validated["progressive"],
            }
        )
        return arm
    except (OSError, UnicodeError, ValueError, ValidationError) as exc:
        arm["status"] = "invalid"
        arm["reason"] = exc.__class__.__name__
        return arm


def _goal_error_class(path: Path) -> str:
    if not path.is_file():
        return ""
    goal = load_goal_state(path)
    candidates = []
    for iteration in reversed(goal.get("iterations", [])):
        if isinstance(iteration, dict):
            candidates.append(iteration.get("error_class"))
    candidates.append(goal.get("error_class"))
    for value in candidates:
        if (
            isinstance(value, str)
            and len(value) <= 200
            and SAFE_ID.match(value)
        ):
            return value
    return ""


def _derive_resources(
    goal: Optional[Dict],
    *,
    workspace: Path,
    runs_dir: Path,
    planning_dir: Optional[Path],
    duration_ms,
) -> Tuple[Dict, List[Dict], List[Dict]]:
    _integer(duration_ms, "auto topology duration_ms", 0, 10**12)
    planning_calls = 0
    worker_calls = 0
    verifier_calls = 0
    token_complete = True
    cost_complete = True
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    cost_usd = 0.0
    run_records = []
    model_workflow_records = []
    iterations = goal.get("iterations", []) if isinstance(goal, dict) else []
    generated_dir = default_generated_dir(workspace) / "model-workflows"
    planner_kind = goal.get("planner", {}).get("kind") if isinstance(goal, dict) and isinstance(goal.get("planner"), dict) else None
    for iteration in iterations:
        if not isinstance(iteration, dict):
            continue
        planning = iteration.get("planning") if isinstance(iteration.get("planning"), dict) else {}
        expected_calls = (
            0
            if planner_kind == "deterministic-direct-per-iteration"
            else 0
            if planner_kind == "progressive-direct-then-model" and iteration.get("iteration") == 1
            else 1
            if planner_kind in {"progressive-direct-then-model", "model-authored-per-iteration"}
            else 0
        )
        calls = planning.get("provider_calls", expected_calls)
        if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
            raise ValidationError("auto topology goal planning call count is invalid")
        planning_calls += calls
        planning_filename = iteration.get("planning_receipt_filename")
        if isinstance(planning_filename, str) and planning_filename:
            planning_path = (
                Path(planning_dir) / planning_filename
                if planning_dir is not None
                else generated_dir / planning_filename
            )
            receipt = load_model_workflow_receipt(planning_path)
            model_workflow_records.append(
                {
                    "kind": "planning",
                    "filename": planning_filename,
                    "sha256": _sha256_file(planning_path, "auto topology planning receipt", MAX_AUTO_TOPOLOGY_RESULT_BYTES),
                }
            )
            if calls:
                usage = receipt.get("provider", {}).get("usage", {})
                token_complete, cost_complete, input_tokens, output_tokens, total_tokens, cost_usd = _accumulate_usage(
                    usage,
                    calls,
                    token_complete,
                    cost_complete,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    cost_usd,
                )
        elif calls:
            token_complete = False
            cost_complete = False
        run_receipt_filename = iteration.get("run_receipt_filename")
        if isinstance(run_receipt_filename, str) and run_receipt_filename:
            run_receipt_path = (
                Path(planning_dir) / run_receipt_filename
                if planning_dir is not None
                else generated_dir / run_receipt_filename
            )
            load_model_workflow_run_receipt(run_receipt_path)
            model_workflow_records.append(
                {
                    "kind": "run",
                    "filename": run_receipt_filename,
                    "sha256": _sha256_file(
                        run_receipt_path,
                        "auto topology model workflow run receipt",
                        MAX_AUTO_TOPOLOGY_RESULT_BYTES,
                    ),
                }
            )
        run_id = iteration.get("run_id")
        if isinstance(run_id, str) and run_id:
            _safe_id(run_id, "auto topology run_id")
            state_path = Path(runs_dir) / run_id / "state.json"
            if not state_path.exists() and not state_path.is_symlink():
                continue
            raw = read_regular_text_file_no_follow(state_path, "auto topology run state", MAX_AUTO_TOPOLOGY_RESULT_BYTES)
            try:
                state = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValidationError("auto topology run state is invalid JSON: %s" % exc)
            if not isinstance(state, dict) or state.get("schema") != "conductor.run_state.v1":
                raise ValidationError("auto topology run state schema is invalid")
            workflow_path = Path(runs_dir) / run_id / "workflow.json"
            workflow_sha256 = _sha256_file(
                workflow_path,
                "auto topology run workflow",
                MAX_AUTO_TOPOLOGY_RESULT_BYTES,
            )
            run_records.append(
                {
                    "run_id": run_id,
                    "state_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                    "workflow_sha256": workflow_sha256,
                }
            )
            steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
            for step in steps.values():
                if not isinstance(step, dict) or step.get("kind") not in {"codex_exec", "agent_map"}:
                    continue
                invocations = step.get("agent_invocations", 0)
                if not isinstance(invocations, int) or isinstance(invocations, bool) or invocations < 0:
                    raise ValidationError("auto topology worker invocation count is invalid")
                worker_calls += invocations
                if invocations:
                    token_complete, cost_complete, input_tokens, output_tokens, total_tokens, cost_usd = _accumulate_usage(
                        step,
                        invocations,
                        token_complete,
                        cost_complete,
                        input_tokens,
                        output_tokens,
                        total_tokens,
                        cost_usd,
                    )
        check = iteration.get("check") if isinstance(iteration.get("check"), dict) else {}
        calls = check.get("model_provider_calls", 0)
        if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
            raise ValidationError("auto topology verifier call count is invalid")
        verifier_calls += calls
        if calls:
            usage = check.get("usage") if isinstance(check.get("usage"), dict) else {}
            token_complete, cost_complete, input_tokens, output_tokens, total_tokens, cost_usd = _accumulate_usage(
                usage,
                calls,
                token_complete,
                cost_complete,
                input_tokens,
                output_tokens,
                total_tokens,
                cost_usd,
            )
    total_calls = planning_calls + worker_calls + verifier_calls
    if total_calls == 0:
        token_complete = True
        cost_complete = True
    resources = {
        "duration_ms": duration_ms,
        "planning_provider_calls": planning_calls,
        "worker_provider_calls": worker_calls,
        "verifier_provider_calls": verifier_calls,
        "total_provider_calls": total_calls,
        "token_observed": token_complete,
        "input_tokens": input_tokens if token_complete else None,
        "output_tokens": output_tokens if token_complete else None,
        "total_tokens": total_tokens if token_complete else None,
        "cost_observed": cost_complete,
        "cost_usd": round(cost_usd, 12) if cost_complete else None,
    }
    return resources, run_records, model_workflow_records


def derive_auto_topology_resources(
    goal: Optional[Dict],
    *,
    workspace: Path,
    runs_dir: Path,
    planning_dir: Optional[Path] = None,
    duration_ms: int,
) -> Tuple[Dict, List[Dict], List[Dict]]:
    return _derive_resources(
        goal,
        workspace=workspace,
        runs_dir=runs_dir,
        planning_dir=planning_dir,
        duration_ms=duration_ms,
    )


def capture_auto_topology_model_workflow_receipts(
    goal: Optional[Dict], *, workspace: Path, planning_dir: Path
) -> None:
    filenames = []
    for iteration in goal.get("iterations", []) if isinstance(goal, dict) else []:
        if not isinstance(iteration, dict):
            continue
        for field in ["planning_receipt_filename", "run_receipt_filename"]:
            filename = iteration.get(field)
            if isinstance(filename, str) and filename:
                if Path(filename).name != filename:
                    raise ValidationError("auto topology model workflow receipt filename is invalid")
                filenames.append(filename)
    if not filenames:
        return
    directory_fd = ensure_dir_no_follow(planning_dir, "auto topology planning receipt directory")
    os.close(directory_fd)
    source_dir = default_generated_dir(workspace) / "model-workflows"
    for filename in filenames:
        source = source_dir / filename
        target = Path(planning_dir) / filename
        raw = read_regular_file_bytes_no_follow(
            source,
            "auto topology source planning receipt",
            MAX_AUTO_TOPOLOGY_RESULT_BYTES,
        )
        if target.exists() or target.is_symlink():
            observed = read_regular_file_bytes_no_follow(
                target,
                "auto topology captured planning receipt",
                MAX_AUTO_TOPOLOGY_RESULT_BYTES,
            )
            if observed != raw:
                raise ValidationError("auto topology captured planning receipt drifted")
            continue
        write_new_text_file_no_follow(
            target,
            "auto topology captured planning receipt",
            raw.decode("utf-8"),
            sync=True,
        )


def _accumulate_usage(
    usage: Dict,
    calls: int,
    token_complete: bool,
    cost_complete: bool,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    cost_usd: float,
):
    input_value = usage.get("input_tokens")
    output_value = usage.get("output_tokens")
    total_value = usage.get("total_tokens")
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in [input_value, output_value, total_value]):
        token_complete = False
    elif total_value != input_value + output_value:
        token_complete = False
    else:
        input_tokens += input_value
        output_tokens += output_value
        total_tokens += total_value
    cost_value = usage.get("cost_usd")
    if isinstance(cost_value, bool) or not isinstance(cost_value, (int, float)) or cost_value < 0 or not math.isfinite(cost_value):
        cost_complete = False
    else:
        cost_usd += float(cost_value)
    if calls < 1:
        raise ValidationError("auto topology usage requires a positive provider call count")
    return token_complete, cost_complete, input_tokens, output_tokens, total_tokens, cost_usd


def _cohort_execution_sha256(campaign: Dict, cohort: Dict) -> str:
    return (
        cohort["task_execution_sha256"]
        if campaign["schema"] == AUTO_TOPOLOGY_CAMPAIGN_SCHEMA
        else cohort["task_prompt_sha256"]
    )


def _validate_auto_receipt_contract(receipt: Dict, campaign: Dict, cohort: Dict, topology: str, source: str) -> None:
    expected = {
        "direct": ("direct", "adaptive-direct-goal", 1),
        "progressive": ("progressive", "adaptive-progressive-goal", campaign["design"]["parallel_workers"]),
        "plan-first": ("goal", "adaptive-model-goal", campaign["design"]["parallel_workers"]),
    }[topology]
    decision = receipt["decision"]
    if (
        decision.get("strategy_requested") != expected[0]
        or decision.get("route") != expected[1]
        or receipt["limits"].get("max_workers") != expected[2]
        or decision.get("verifier_kind") != "command"
        or decision.get("execution_mode") != "foreground"
        or receipt["task"].get("sha256") != _cohort_execution_sha256(campaign, cohort)
    ):
        raise ValidationError("%s auto receipt topology contract is invalid" % source)
    if receipt["status"] in {"routing", "running"}:
        raise ValidationError("%s auto receipt is not terminal" % source)
    policy = receipt["policy"]
    if (
        policy.get("allow_agent") is not True
        or policy.get("allow_writes") is not True
        or policy.get("allow_destructive") is not False
        or policy.get("allow_network") is not False
        or policy.get("allow_parallel") is not (topology != "direct")
        or policy.get("execution_approval_present") is not True
        or policy.get("write_approval_present") is not True
    ):
        raise ValidationError("%s auto receipt policy contract is invalid" % source)


def _validate_goal_contract(goal: Dict, campaign: Dict, cohort: Dict, topology: str, source: str) -> None:
    expected_kind = {
        "direct": "deterministic-direct-per-iteration",
        "progressive": "progressive-direct-then-model",
        "plan-first": "model-authored-per-iteration",
    }[topology]
    planner = goal.get("planner") if isinstance(goal.get("planner"), dict) else {}
    task = goal.get("task") if isinstance(goal.get("task"), dict) else {}
    if (
        goal.get("goal_kind") != "adaptive-model-workflow"
        or planner.get("kind") != expected_kind
        or planner.get("model") != campaign["model"]["name"]
        or planner.get("reasoning_effort") != campaign["model"]["effort"]
        or planner.get("max_workers") != (1 if topology == "direct" else campaign["design"]["parallel_workers"])
        or task.get("sha256") != _cohort_execution_sha256(campaign, cohort)
    ):
        raise ValidationError("%s goal topology contract is invalid" % source)


def _topology_summary(arms: List[Dict]) -> Dict:
    scores = [arm["outcome"]["score"] for arm in arms]
    durations = [arm["resources"]["duration_ms"] for arm in arms]
    calls = [arm["resources"]["total_provider_calls"] for arm in arms]
    tokens = [arm["resources"]["total_tokens"] for arm in arms if arm["resources"]["token_observed"]]
    costs = [arm["resources"]["cost_usd"] for arm in arms if arm["resources"]["cost_observed"]]
    return {
        "completed_arms": len(arms),
        "passed_arms": sum(1 for arm in arms if arm["outcome"]["passed"]),
        "goal_satisfied_arms": sum(1 for arm in arms if arm["outcome"]["goal_satisfied"]),
        "score_total": sum(scores),
        "score_mean": _mean(scores),
        "duration_ms_mean": _mean(durations),
        "provider_calls_mean": _mean(calls),
        "token_observed_arms": len(tokens),
        "total_tokens_mean": _mean(tokens),
        "cost_observed_arms": len(costs),
        "cost_usd_mean": _mean(costs),
        "escalated_arms": sum(1 for arm in arms if arm["progressive"]["escalated"]),
        "first_pass_successes": sum(1 for arm in arms if arm["progressive"]["first_pass_success"]),
    }


def _pairwise_summary(cohorts: List[Dict], left: str, right: str) -> Dict:
    pairs = []
    for cohort in cohorts:
        left_arm = cohort["arms"][left]
        right_arm = cohort["arms"][right]
        if left_arm["status"] == right_arm["status"] == "completed":
            pairs.append((left_arm, right_arm))
    score_deltas = [a["outcome"]["score"] - b["outcome"]["score"] for a, b in pairs]
    duration_deltas = [a["resources"]["duration_ms"] - b["resources"]["duration_ms"] for a, b in pairs]
    token_pairs = [
        (a["resources"]["total_tokens"], b["resources"]["total_tokens"])
        for a, b in pairs
        if a["resources"]["token_observed"] and b["resources"]["token_observed"]
    ]
    cost_pairs = [
        (a["resources"]["cost_usd"], b["resources"]["cost_usd"])
        for a, b in pairs
        if a["resources"]["cost_observed"] and b["resources"]["cost_observed"]
    ]
    score_ci = _mean_ci95(score_deltas)
    return {
        "left": left,
        "right": right,
        "completed_pairs": len(pairs),
        "left_quality_wins": sum(1 for delta in score_deltas if delta > 0),
        "right_quality_wins": sum(1 for delta in score_deltas if delta < 0),
        "quality_ties": sum(1 for delta in score_deltas if delta == 0),
        "score_delta_mean": score_ci[0],
        "score_delta_ci95_low": score_ci[1],
        "score_delta_ci95_high": score_ci[2],
        "duration_delta_ms_mean": _mean(duration_deltas),
        "token_comparable_pairs": len(token_pairs),
        "token_delta_mean": _mean([a - b for a, b in token_pairs]),
        "cost_comparable_pairs": len(cost_pairs),
        "cost_delta_usd_mean": _mean([a - b for a, b in cost_pairs]),
    }


def _resource_coverage(arms: List[Dict], completed_count: int) -> Dict:
    token_observed = sum(1 for arm in arms if arm["resources"]["token_observed"])
    cost_observed = sum(1 for arm in arms if arm["resources"]["cost_observed"])
    return {
        "completed_arms": completed_count,
        "token_observed_arms": token_observed,
        "token_complete": completed_count > 0 and token_observed == completed_count,
        "cost_observed_arms": cost_observed,
        "cost_complete": completed_count > 0 and cost_observed == completed_count,
    }


def _topology_inference(campaign, lifecycle, arm_lifecycle, resources, pairwise, cohorts) -> Dict:
    shared_reasons = []
    release_bound = campaign["schema"] != AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V1
    release_uniform = release_bound and arm_lifecycle["completed"] > 0
    if not release_bound:
        shared_reasons.append("release-unbound-legacy-campaign")
    if lifecycle["completed"] != campaign["design"]["cohort_count"]:
        shared_reasons.append("campaign-incomplete")
    if lifecycle["invalid"] or arm_lifecycle["invalid"]:
        shared_reasons.append("invalid-evidence")
    if campaign["design"]["repetitions"] < 3:
        shared_reasons.append("insufficient-task-repetitions")
    if not resources["token_complete"]:
        shared_reasons.append("token-observability-incomplete")
    for summary in pairwise:
        if summary["completed_pairs"] < 3:
            shared_reasons.append("insufficient-%s-vs-%s-pairs" % (summary["left"], summary["right"]))
    quality_latency_token_topology = _dominant_topology(pairwise, require_cost=False)
    quality_latency_token_reasons = list(shared_reasons)
    if quality_latency_token_topology is None:
        quality_latency_token_reasons.append("no-quality-latency-token-dominant-topology")
    all_resource_topology = _dominant_topology(pairwise, require_cost=True)
    all_resource_reasons = list(shared_reasons)
    if not resources["cost_complete"]:
        all_resource_reasons.append("cost-observability-incomplete")
    if all_resource_topology is None:
        all_resource_reasons.append("no-all-resource-dominant-topology")
    progressive_repayment_rows = 0
    comparable_repayment_rows = 0
    for cohort in cohorts:
        progressive = cohort["arms"]["progressive"]
        plan_first = cohort["arms"]["plan-first"]
        if progressive["status"] == plan_first["status"] == "completed":
            if progressive["resources"]["token_observed"] and plan_first["resources"]["token_observed"]:
                comparable_repayment_rows += 1
                if (
                    progressive["outcome"]["score"] >= plan_first["outcome"]["score"]
                    and progressive["resources"]["total_tokens"] < plan_first["resources"]["total_tokens"]
                ):
                    progressive_repayment_rows += 1
    all_resource_reasons = list(dict.fromkeys(all_resource_reasons))
    quality_latency_token_reasons = list(dict.fromkeys(quality_latency_token_reasons))
    return {
        "release_bound": release_bound,
        "release_uniform": release_uniform,
        "recommendation_supported": not all_resource_reasons,
        "recommended_topology": all_resource_topology if not all_resource_reasons else None,
        "reasons": all_resource_reasons,
        "quality_latency_token_recommendation_supported": not quality_latency_token_reasons,
        "quality_latency_token_recommended_topology": (
            quality_latency_token_topology if not quality_latency_token_reasons else None
        ),
        "quality_latency_token_reasons": quality_latency_token_reasons,
        "dollar_cost_comparable": resources["cost_complete"],
        "progressive_repayment_comparable_rows": comparable_repayment_rows,
        "progressive_repayment_rows": progressive_repayment_rows,
        "progressive_repayment_rate": (
            progressive_repayment_rows / comparable_repayment_rows if comparable_repayment_rows else None
        ),
        "interpretation": "descriptive matched topology evidence; not a cross-provider superiority claim",
    }


def _dominant_topology(pairwise: List[Dict], *, require_cost: bool = True) -> Optional[str]:
    candidates = []
    for topology in AUTO_TOPOLOGIES:
        strict_improvement = False
        valid = True
        for summary in pairwise:
            if topology not in {summary["left"], summary["right"]}:
                continue
            if summary["completed_pairs"] < 3:
                valid = False
                break
            if topology == summary["left"]:
                checks = [
                    summary["score_delta_ci95_low"] is not None
                    and summary["score_delta_ci95_low"] >= 0,
                    summary["token_delta_mean"] is not None and summary["token_delta_mean"] <= 0,
                    summary["duration_delta_ms_mean"] is not None
                    and summary["duration_delta_ms_mean"] <= 0,
                ]
                if require_cost:
                    checks.append(
                        summary["cost_delta_usd_mean"] is not None
                        and summary["cost_delta_usd_mean"] <= 0
                    )
                resource_deltas = [summary["token_delta_mean"], summary["duration_delta_ms_mean"]]
                if require_cost:
                    resource_deltas.append(summary["cost_delta_usd_mean"])
                strict_improvement = strict_improvement or any(
                    value is not None and value < 0
                    for value in resource_deltas
                ) or (summary["score_delta_mean"] is not None and summary["score_delta_mean"] > 0)
            else:
                checks = [
                    summary["score_delta_ci95_high"] is not None
                    and summary["score_delta_ci95_high"] <= 0,
                    summary["token_delta_mean"] is not None and summary["token_delta_mean"] >= 0,
                    summary["duration_delta_ms_mean"] is not None
                    and summary["duration_delta_ms_mean"] >= 0,
                ]
                if require_cost:
                    checks.append(
                        summary["cost_delta_usd_mean"] is not None
                        and summary["cost_delta_usd_mean"] >= 0
                    )
                resource_deltas = [summary["token_delta_mean"], summary["duration_delta_ms_mean"]]
                if require_cost:
                    resource_deltas.append(summary["cost_delta_usd_mean"])
                strict_improvement = strict_improvement or any(
                    value is not None and value > 0
                    for value in resource_deltas
                ) or (summary["score_delta_mean"] is not None and summary["score_delta_mean"] < 0)
            if not all(checks):
                valid = False
                break
        if valid and strict_improvement:
            candidates.append(topology)
    return candidates[0] if len(candidates) == 1 else None


def _validate_topologies(topologies, design, source: str) -> None:
    if not isinstance(topologies, list) or len(topologies) != 3:
        raise ValidationError("%s topologies must contain direct, progressive, and plan-first" % source)
    names = []
    for item in topologies:
        topology = _object(item, TOPOLOGY_FIELDS, "%s topology" % source)
        name = topology.get("name")
        if name not in AUTO_TOPOLOGIES or name in names:
            raise ValidationError("%s topology names are invalid" % source)
        names.append(name)
        expected = AUTO_TOPOLOGY_SPECS[name]
        if topology.get("strategy") != expected["strategy"] or topology.get("planning_policy") != expected["planning_policy"]:
            raise ValidationError("%s topology %s contract is invalid" % (source, name))
        expected_workers = 1 if name == "direct" else design["parallel_workers"]
        if topology.get("max_workers") != expected_workers:
            raise ValidationError("%s topology %s max_workers is invalid" % (source, name))
    if names != list(AUTO_TOPOLOGIES):
        raise ValidationError("%s topologies must use canonical order" % source)


def _validate_cohorts(
    cohorts,
    source_value,
    design,
    campaign_schema: str,
    source: str,
) -> List[Dict]:
    if not isinstance(cohorts, list) or not cohorts or len(cohorts) > MAX_AUTO_TOPOLOGY_COHORTS:
        raise ValidationError("%s cohorts must be a bounded non-empty array" % source)
    expected_count = source_value["task_count"] * design["repetitions"]
    if len(cohorts) != expected_count:
        raise ValidationError("%s cohorts must cover every task repetition" % source)
    seen = set()
    task_contracts = {}
    task_prompts = {}
    task_executions = {}
    source_fixtures = {}
    held_out_fixtures = {}
    task_budgets = {}
    paths = set()
    cohort_fields = (
        COHORT_FIELDS
        if campaign_schema == AUTO_TOPOLOGY_CAMPAIGN_SCHEMA
        else COHORT_FIELDS_V2
    )
    for sequence, raw in enumerate(cohorts, start=1):
        cohort = _object(raw, cohort_fields, "%s cohort" % source)
        task_id = cohort.get("task_id")
        repetition = cohort.get("repetition")
        _safe_id(task_id, "%s cohort task_id" % source)
        _integer(repetition, "%s cohort repetition" % source, 1, design["repetitions"])
        expected_id = "r%03d-%s" % (repetition, task_id)
        if cohort.get("id") != expected_id or expected_id in seen:
            raise ValidationError("%s cohort id is invalid or duplicated" % source)
        seen.add(expected_id)
        if cohort.get("sequence") != sequence:
            raise ValidationError("%s cohort sequence must match array order" % source)
        _sha256(cohort.get("task_contract_sha256"), "%s task contract" % source)
        _sha256(cohort.get("task_prompt_sha256"), "%s task prompt" % source)
        if campaign_schema == AUTO_TOPOLOGY_CAMPAIGN_SCHEMA:
            _sha256(
                cohort.get("task_execution_sha256"),
                "%s task execution" % source,
            )
        _sha256(cohort.get("source_fixture_sha256"), "%s source fixture" % source)
        _sha256(cohort.get("held_out_fixture_sha256"), "%s held-out fixture" % source)
        if task_id in task_contracts and task_contracts[task_id] != cohort["task_contract_sha256"]:
            raise ValidationError("%s task contract changed across repetitions" % source)
        if task_id in task_prompts and task_prompts[task_id] != cohort["task_prompt_sha256"]:
            raise ValidationError("%s task prompt changed across repetitions" % source)
        task_contracts[task_id] = cohort["task_contract_sha256"]
        task_prompts[task_id] = cohort["task_prompt_sha256"]
        if campaign_schema == AUTO_TOPOLOGY_CAMPAIGN_SCHEMA:
            if (
                task_id in task_executions
                and task_executions[task_id] != cohort["task_execution_sha256"]
            ):
                raise ValidationError(
                    "%s task execution contract changed across repetitions" % source
                )
            task_executions[task_id] = cohort["task_execution_sha256"]
        if task_id in source_fixtures and source_fixtures[task_id] != cohort["source_fixture_sha256"]:
            raise ValidationError("%s source fixture changed across repetitions" % source)
        if task_id in held_out_fixtures and held_out_fixtures[task_id] != cohort["held_out_fixture_sha256"]:
            raise ValidationError("%s held-out fixture changed across repetitions" % source)
        source_fixtures[task_id] = cohort["source_fixture_sha256"]
        held_out_fixtures[task_id] = cohort["held_out_fixture_sha256"]
        _validate_budget(cohort.get("budget"), design, "%s cohort budget" % source)
        budget_hash = _sha256_json(cohort["budget"])
        if task_id in task_budgets and task_budgets[task_id] != budget_hash:
            raise ValidationError("%s task budget changed across repetitions" % source)
        task_budgets[task_id] = budget_hash
        order = cohort.get("arm_order")
        if not isinstance(order, list) or tuple(sorted(order)) != tuple(sorted(AUTO_TOPOLOGIES)) or len(set(order)) != 3:
            raise ValidationError("%s cohort arm_order is invalid" % source)
        artifacts = cohort.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != set(AUTO_TOPOLOGIES):
            raise ValidationError("%s cohort artifacts must cover all topologies" % source)
        for topology in AUTO_TOPOLOGIES:
            values = _object(artifacts[topology], ARM_ARTIFACT_FIELDS, "%s cohort artifacts" % source)
            for value in values.values():
                clean = _clean_artifact_path(value, "%s cohort artifact path" % source)
                if clean in paths:
                    raise ValidationError("%s cohort artifact paths must be unique" % source)
                paths.add(clean)
        if cohort.get("status") != "planned":
            raise ValidationError("%s preregistered cohort status must be planned" % source)
    expected_rows, expected_orders = _campaign_design(
        [{"id": task_id} for task_id in sorted(task_contracts)],
        design["seed"],
        design["repetitions"],
    )
    observed = [(cohort["repetition"], cohort["task_id"]) for cohort in cohorts]
    expected = [(repetition, task["id"]) for repetition, task in expected_rows]
    if observed != expected:
        raise ValidationError("%s cohort order does not match the declared seed" % source)
    for cohort in cohorts:
        if cohort["arm_order"] != expected_orders[(cohort["repetition"], cohort["task_id"])]:
            raise ValidationError("%s arm order does not match the declared seed" % source)
    return cohorts


def _validate_budget(budget, design, label: str) -> None:
    root = _object(budget, BUDGET_FIELDS, label)
    _integer(root.get("max_minutes"), "%s max_minutes" % label, 1, 24 * 60)
    _integer(root.get("max_agents"), "%s max_agents" % label, design["parallel_workers"], MAX_AGENT_WORKERS)
    if root.get("max_live_tool_cost") not in {"none", "low", "medium", "high"}:
        raise ValidationError("%s max_live_tool_cost is invalid" % label)
    _integer(root.get("max_tokens"), "%s max_tokens" % label, 0, 10**12)
    cost = root.get("max_cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0 or not math.isfinite(cost):
        raise ValidationError("%s max_cost_usd is invalid" % label)


def _campaign_design(tasks: List[Dict], seed: int, repetitions: int):
    rows = [(repetition, task) for repetition in range(1, repetitions + 1) for task in tasks]
    ordered = sorted(rows, key=lambda row: (_rank(seed, "cohort", row[0], row[1]["id"]), row[0], row[1]["id"]))
    arm_ranked = sorted(rows, key=lambda row: (_rank(seed, "arm", row[0], row[1]["id"]), row[0], row[1]["id"]))
    orders = {}
    for index, (repetition, task) in enumerate(arm_ranked):
        first = AUTO_TOPOLOGIES[index % len(AUTO_TOPOLOGIES)]
        remaining = sorted(
            (topology for topology in AUTO_TOPOLOGIES if topology != first),
            key=lambda topology: _rank(seed, "arm-rest-%s" % topology, repetition, task["id"]),
        )
        orders[(repetition, task["id"])] = [first, *remaining]
    return ordered, orders


def _rank(seed: int, purpose: str, repetition: int, task_id: str) -> str:
    return _sha256_text("%d\0%s\0%d\0%s" % (seed, purpose, repetition, task_id))


def _public_tasks(parity_tasks: Dict) -> Dict:
    if not isinstance(parity_tasks, dict):
        raise ValidationError("parity tasks must contain an object")
    return {key: value for key, value in parity_tasks.items() if not key.startswith("_")}


def _fixture_contracts(tasks: List[Dict], *, source_path, fixture_root: Optional[Path]) -> Dict:
    from tools.evaluate_implementation_canary import TASK_SPECS

    if fixture_root is None:
        if not isinstance(source_path, str) or not source_path:
            raise ValidationError("auto topology campaign requires fixture_root when parity task source is unavailable")
        root = Path(source_path).resolve().parent / "implementation-canary"
    else:
        root = Path(fixture_root).resolve()
    contracts = {}
    for task in tasks:
        spec = TASK_SPECS.get(task["id"])
        if spec is None:
            raise ValidationError("auto topology task %s has no deterministic fixture contract" % task["id"])
        source = root / spec["source"]
        held_out = root / spec["held_out"]
        contracts[task["id"]] = {
            "source": snapshot_workspace(source).fingerprint_sha256,
            "held_out": snapshot_workspace(held_out).fingerprint_sha256,
        }
    return contracts


def _object(value, fields, label: str) -> Dict:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % label)
    unknown = sorted(set(value) - set(fields))
    missing = sorted(set(fields) - set(value))
    if unknown:
        raise ValidationError("%s contains unknown fields: %s" % (label, ", ".join(unknown)))
    if missing:
        raise ValidationError("%s is missing fields: %s" % (label, ", ".join(missing)))
    return value


def _safe_id(value, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.match(value):
        raise ValidationError("%s must be a safe identifier" % label)
    return value


def _integer(value, label: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValidationError("%s must be an integer from %d to %d" % (label, minimum, maximum))
    return value


def _non_empty(value, label: str, maximum: int = 1000) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValidationError("%s must be a bounded non-empty string" % label)
    return value


def _version(value, label: str) -> str:
    if not isinstance(value, str) or len(value) > 32:
        raise ValidationError("%s is invalid" % label)
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValidationError("%s is invalid" % label)
    return value


def _sha256(value, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValidationError("%s must be lowercase SHA-256" % label)
    return value


def _clean_artifact_path(value, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1000:
        raise ValidationError("%s must be a bounded path" % label)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValidationError("%s must be a safe relative POSIX path" % label)
    return path.as_posix()


def _join_artifact_path(root: str, child: str) -> str:
    return _clean_artifact_path((PurePosixPath(root) / PurePosixPath(child)).as_posix(), "artifact path")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path, label: str, max_bytes: int) -> str:
    return hashlib.sha256(read_regular_file_bytes_no_follow(path, label, max_bytes)).hexdigest()


def _mean(values):
    return sum(values) / len(values) if values else None


def _mean_ci95(values):
    if not values:
        return None, None, None
    mean = _mean(values)
    if len(values) == 1:
        return mean, None, None
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    margin = 1.96 * math.sqrt(variance / len(values))
    return mean, mean - margin, mean + margin


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
