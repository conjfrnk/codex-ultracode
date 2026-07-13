import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, List

from .benchmark import PARITY_TASKS_SCHEMA, validate_parity_tasks
from .codex_config import CODEX_REASONING_EFFORTS
from .errors import ValidationError
from .effort_policy import (
    AUTO_EFFORT_POLICY,
    AUTO_EFFORT_POLICY_V5,
    AUTO_EFFORT_POLICY_V4,
    AUTO_EFFORT_POLICY_V3,
    AUTO_EFFORT_POLICY_V2,
    AUTO_REASONING_EFFORT,
    AUTO_REASONING_EFFORTS,
    resolve_auto_reasoning_effort,
)
from .security import (
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    write_new_text_file_no_follow,
)
from .workflow import SAFE_ID


PARITY_CAMPAIGN_SCHEMA = "conductor.parity_campaign.v4"
PARITY_CAMPAIGN_SCHEMA_V3 = "conductor.parity_campaign.v3"
PARITY_CAMPAIGN_SCHEMA_V2 = "conductor.parity_campaign.v2"
PARITY_CAMPAIGN_SCHEMA_V1 = "conductor.parity_campaign.v1"
PARITY_CAMPAIGN_EVIDENCE_STATUS = "planned-not-evidence"
PARITY_CAMPAIGN_RANDOMIZATION = "sha256-rank-alternating-v1"
PARITY_CAMPAIGN_SYSTEMS = ("codex", "claude")
PARITY_CAMPAIGN_EFFORT_POLICIES = {
    AUTO_EFFORT_POLICY,
    AUTO_EFFORT_POLICY_V5,
    AUTO_EFFORT_POLICY_V4,
    AUTO_EFFORT_POLICY_V3,
    AUTO_EFFORT_POLICY_V2,
}
MAX_CAMPAIGN_BYTES = 4 * 1024 * 1024
MAX_CAMPAIGN_REPETITIONS = 100
MAX_CAMPAIGN_TRIALS = 10000
CAMPAIGN_FIELDS = {
    "schema",
    "name",
    "generated_at_utc",
    "evidence_status",
    "source",
    "design",
    "systems",
    "trials",
    "claim_gate",
}
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
    "trial_count",
    "provider_first_counts",
}
DESIGN_EFFORT_POLICY_FIELD = "codex_effort_policy"
SYSTEM_FIELDS = {
    "name",
    "provider",
    "model",
    "effort",
    "alternative_model_allowed",
    "requires_external_tool",
    "requires_paid_approval",
}
TRIAL_FIELDS = {
    "id",
    "sequence",
    "task_id",
    "repetition",
    "task_contract_sha256",
    "budget",
    "system_order",
    "artifacts",
    "status",
}
ARTIFACT_FIELDS = {"report", "stage", "patch", "evaluation", "scored_report", "manifest"}
CLAIM_GATE_FIELDS = {"eligible_for_parity_claims", "required_before_claim"}
TRIAL_BUDGET_FIELDS = {"max_minutes", "max_agents", "max_live_tool_cost", "max_cost_usd", "max_tokens"}


def build_parity_campaign(
    parity_tasks: Dict,
    *,
    run_id: str,
    seed: int,
    repetitions: int,
    artifacts_dir: str,
    codex_model: str,
    codex_effort: str = AUTO_REASONING_EFFORT,
) -> Dict:
    public_tasks = _public_parity_tasks(parity_tasks)
    validate_parity_tasks(public_tasks)
    if not isinstance(run_id, str) or not SAFE_ID.match(run_id):
        raise ValidationError("parity campaign run_id must be a safe identifier")
    _validate_int(seed, "parity campaign seed", minimum=0, maximum=2**63 - 1)
    _validate_int(
        repetitions,
        "parity campaign repetitions",
        minimum=1,
        maximum=MAX_CAMPAIGN_REPETITIONS,
    )
    if not isinstance(codex_model, str) or not codex_model.strip() or len(codex_model) > 128:
        raise ValidationError("parity campaign Codex model must be a non-empty bounded string")
    if codex_effort not in AUTO_REASONING_EFFORTS:
        raise ValidationError("parity campaign Codex effort is invalid")
    selected_codex_effort = codex_effort
    artifact_root = _clean_artifact_path(artifacts_dir, "parity campaign artifacts_dir")
    tasks = public_tasks["tasks"]
    if len(tasks) * repetitions > MAX_CAMPAIGN_TRIALS:
        raise ValidationError("parity campaign exceeds the supported trial count")
    rows, provider_orders = _campaign_design(tasks, seed, repetitions)
    trials = []
    for sequence, (repetition, task) in enumerate(rows, start=1):
        trial_id = "r%03d-%s" % (repetition, task["id"])
        trial_root = _join_artifact_path(artifact_root, trial_id)
        artifacts = {
            system: {
                "report": _join_artifact_path(trial_root, "%s-report.json" % system),
                "stage": _join_artifact_path(trial_root, "%s-stage" % system),
                "patch": _join_artifact_path(trial_root, "%s.patch" % system),
                "evaluation": _join_artifact_path(trial_root, "%s-evaluation" % system),
                "scored_report": _join_artifact_path(trial_root, "%s-scored-report.json" % system),
                "manifest": _join_artifact_path(trial_root, "%s-manifest.json" % system),
            }
            for system in PARITY_CAMPAIGN_SYSTEMS
        }
        trials.append(
            {
                "id": trial_id,
                "sequence": sequence,
                "task_id": task["id"],
                "repetition": repetition,
                "task_contract_sha256": _sha256_json(task),
                "codex_effort": resolve_auto_reasoning_effort(
                    task["prompt"],
                    selected_codex_effort,
                    route="adaptive-direct-goal",
                    verifier_kind="command",
                ),
                "budget": task["budget"],
                "system_order": provider_orders[(repetition, task["id"])],
                "artifacts": artifacts,
                "status": "planned",
            }
        )
    first_counts = {
        system: sum(1 for trial in trials if trial["system_order"][0] == system)
        for system in PARITY_CAMPAIGN_SYSTEMS
    }
    source_path = parity_tasks.get("_source_path") if isinstance(parity_tasks, dict) else None
    campaign = {
        "schema": PARITY_CAMPAIGN_SCHEMA,
        "name": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "evidence_status": PARITY_CAMPAIGN_EVIDENCE_STATUS,
        "source": {
            "parity_tasks_schema": PARITY_TASKS_SCHEMA,
            "parity_tasks_source": str(source_path) if source_path else None,
            "parity_tasks_sha256": _sha256_json(public_tasks),
            "suite": public_tasks["name"],
            "task_count": len(tasks),
            "required_dimensions": public_tasks.get("required_dimensions", []),
        },
        "design": {
            "seed": seed,
            "repetitions": repetitions,
            "randomization_algorithm": PARITY_CAMPAIGN_RANDOMIZATION,
            "trial_count": len(trials),
            "provider_first_counts": first_counts,
            DESIGN_EFFORT_POLICY_FIELD: AUTO_EFFORT_POLICY,
        },
        "systems": [
            {
                "name": "codex",
                "provider": "openai",
                "model": codex_model.strip(),
                "effort": selected_codex_effort,
                "alternative_model_allowed": False,
                "requires_external_tool": True,
                "requires_paid_approval": False,
            },
            {
                "name": "claude",
                "provider": "anthropic",
                "model": "sonnet",
                "effort": "ultracode",
                "alternative_model_allowed": False,
                "requires_external_tool": True,
                "requires_paid_approval": True,
            },
        ],
        "trials": trials,
        "claim_gate": {
            "eligible_for_parity_claims": False,
            "required_before_claim": [
                "execute every preregistered trial without dropping failed outcomes",
                "validate every provider report, deterministic evaluation, scored report, and run manifest",
                "use equivalent task contracts, repositories, permissions, and stratified cap-enforcement cohorts",
                "compute paired uncertainty across repeated tasks before making parity or superiority claims",
            ],
        },
    }
    validate_parity_campaign(campaign)
    return campaign


def validate_parity_campaign(campaign: Dict, source: str = "<memory>") -> None:
    if not isinstance(campaign, dict):
        raise ValidationError("%s must contain a JSON object" % source)
    _allowed_fields(campaign, CAMPAIGN_FIELDS, source)
    campaign_schema = campaign.get("schema")
    if campaign_schema not in {
        PARITY_CAMPAIGN_SCHEMA,
        PARITY_CAMPAIGN_SCHEMA_V3,
        PARITY_CAMPAIGN_SCHEMA_V2,
        PARITY_CAMPAIGN_SCHEMA_V1,
    }:
        raise ValidationError(
            "%s schema must be %s, %s, %s, or %s"
            % (
                source,
                PARITY_CAMPAIGN_SCHEMA,
                PARITY_CAMPAIGN_SCHEMA_V3,
                PARITY_CAMPAIGN_SCHEMA_V2,
                PARITY_CAMPAIGN_SCHEMA_V1,
            )
        )
    if not isinstance(campaign.get("name"), str) or not SAFE_ID.match(campaign["name"]):
        raise ValidationError("%s name must be a safe identifier" % source)
    if not isinstance(campaign.get("generated_at_utc"), str) or not campaign["generated_at_utc"]:
        raise ValidationError("%s generated_at_utc must be non-empty" % source)
    if campaign.get("evidence_status") != PARITY_CAMPAIGN_EVIDENCE_STATUS:
        raise ValidationError("%s evidence_status must be %s" % (source, PARITY_CAMPAIGN_EVIDENCE_STATUS))
    source_value = campaign.get("source")
    if not isinstance(source_value, dict):
        raise ValidationError("%s source must be an object" % source)
    _allowed_fields(source_value, SOURCE_FIELDS, "%s source" % source)
    if source_value.get("parity_tasks_schema") != PARITY_TASKS_SCHEMA:
        raise ValidationError("%s source parity_tasks_schema is invalid" % source)
    source_path = source_value.get("parity_tasks_source")
    if source_path is not None and not isinstance(source_path, str):
        raise ValidationError("%s source parity_tasks_source must be string or null" % source)
    _validate_sha256(source_value.get("parity_tasks_sha256"), "%s source parity_tasks_sha256" % source)
    if not isinstance(source_value.get("suite"), str) or not source_value["suite"].strip():
        raise ValidationError("%s source suite must be non-empty" % source)
    _validate_int(source_value.get("task_count"), "%s source task_count" % source, minimum=1)
    dimensions = source_value.get("required_dimensions")
    if not isinstance(dimensions, list) or not all(isinstance(value, str) and value for value in dimensions):
        raise ValidationError("%s source required_dimensions must be a string array" % source)
    design = campaign.get("design")
    if not isinstance(design, dict):
        raise ValidationError("%s design must be an object" % source)
    allowed_design_fields = set(DESIGN_FIELDS)
    if campaign_schema == PARITY_CAMPAIGN_SCHEMA:
        allowed_design_fields.add(DESIGN_EFFORT_POLICY_FIELD)
    _allowed_fields(design, allowed_design_fields, "%s design" % source)
    if campaign_schema == PARITY_CAMPAIGN_SCHEMA:
        if design.get(DESIGN_EFFORT_POLICY_FIELD) not in PARITY_CAMPAIGN_EFFORT_POLICIES:
            raise ValidationError("%s design Codex effort policy is unsupported" % source)
    _validate_int(design.get("seed"), "%s design seed" % source, minimum=0, maximum=2**63 - 1)
    _validate_int(
        design.get("repetitions"),
        "%s design repetitions" % source,
        minimum=1,
        maximum=MAX_CAMPAIGN_REPETITIONS,
    )
    if design.get("randomization_algorithm") != PARITY_CAMPAIGN_RANDOMIZATION:
        raise ValidationError("%s design randomization_algorithm is unsupported" % source)
    systems = _validate_campaign_systems(campaign.get("systems"), campaign_schema, source)
    codex_profile_effort = next(
        item["effort"] for item in campaign["systems"] if item["name"] == "codex"
    )
    trials = _validate_campaign_trials(
        campaign.get("trials"),
        design,
        source_value,
        systems,
        campaign_schema,
        codex_profile_effort,
        source,
    )
    _validate_provider_first_counts(design.get("provider_first_counts"), trials, source)
    gate = campaign.get("claim_gate")
    if not isinstance(gate, dict):
        raise ValidationError("%s claim_gate must be an object" % source)
    _allowed_fields(gate, CLAIM_GATE_FIELDS, "%s claim_gate" % source)
    if gate.get("eligible_for_parity_claims") is not False:
        raise ValidationError("%s planned campaign cannot be claim-eligible" % source)
    required = gate.get("required_before_claim")
    if not isinstance(required, list) or not required or not all(isinstance(value, str) and value for value in required):
        raise ValidationError("%s claim_gate required_before_claim must be a non-empty string array" % source)


def load_parity_campaign(path: Path) -> Dict:
    try:
        campaign = json.loads(
            read_regular_text_file_no_follow(path, "parity campaign", max_bytes=MAX_CAMPAIGN_BYTES)
        )
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    validate_parity_campaign(campaign, source=str(path))
    return campaign


def write_parity_campaign(campaign: Dict, output: Path) -> Path:
    validate_parity_campaign(campaign, source=str(output))
    reject_symlink_path(output, "parity campaign output")
    if output.exists():
        raise ValidationError("parity campaign output already exists: %s" % output)
    parent_fd = ensure_dir_no_follow(output.parent, "parity campaign output parent")
    os.close(parent_fd)
    try:
        write_new_text_file_no_follow(
            output,
            "parity campaign output",
            json.dumps(campaign, indent=2, sort_keys=True) + "\n",
            sync=True,
        )
    except FileExistsError:
        raise ValidationError("parity campaign output already exists: %s" % output)
    return output


def _campaign_design(tasks: List[Dict], seed: int, repetitions: int):
    rows = [(repetition, task) for repetition in range(1, repetitions + 1) for task in tasks]
    ordered = sorted(
        rows,
        key=lambda row: (_rank(seed, "trial", row[0], row[1]["id"]), row[0], row[1]["id"]),
    )
    provider_ranked = sorted(
        rows,
        key=lambda row: (_rank(seed, "provider", row[0], row[1]["id"]), row[0], row[1]["id"]),
    )
    provider_orders = {}
    for index, (repetition, task) in enumerate(provider_ranked):
        first = PARITY_CAMPAIGN_SYSTEMS[index % len(PARITY_CAMPAIGN_SYSTEMS)]
        provider_orders[(repetition, task["id"])] = [
            first,
            *[system for system in PARITY_CAMPAIGN_SYSTEMS if system != first],
        ]
    return ordered, provider_orders


def _validate_campaign_systems(systems, campaign_schema: str, source: str) -> List[str]:
    if not isinstance(systems, list) or len(systems) != 2:
        raise ValidationError("%s systems must contain exactly Codex and Claude" % source)
    names = []
    for system in systems:
        if not isinstance(system, dict):
            raise ValidationError("%s systems must contain objects" % source)
        _allowed_fields(system, SYSTEM_FIELDS, "%s system" % source)
        name = system.get("name")
        if name not in PARITY_CAMPAIGN_SYSTEMS or name in names:
            raise ValidationError("%s systems must contain unique codex and claude entries" % source)
        names.append(name)
        for field in ["provider", "model", "effort"]:
            if not isinstance(system.get(field), str) or not system[field].strip():
                raise ValidationError("%s system %s %s must be non-empty" % (source, name, field))
        for field in ["alternative_model_allowed", "requires_external_tool", "requires_paid_approval"]:
            if not isinstance(system.get(field), bool):
                raise ValidationError("%s system %s %s must be boolean" % (source, name, field))
        if system["alternative_model_allowed"] is not False or system["requires_external_tool"] is not True:
            raise ValidationError("%s system %s authority must remain fixed and external" % (source, name))
        if name == "claude" and (
            system["provider"] != "anthropic"
            or system["model"] != "sonnet"
            or system["effort"] != "ultracode"
            or system["requires_paid_approval"] is not True
        ):
            raise ValidationError("%s Claude campaign lane must be pinned to Sonnet Ultracode" % source)
        if name == "codex":
            allowed_efforts = (
                AUTO_REASONING_EFFORTS
                if campaign_schema in {PARITY_CAMPAIGN_SCHEMA, PARITY_CAMPAIGN_SCHEMA_V3}
                else CODEX_REASONING_EFFORTS
            )
            if (
                system["provider"] != "openai"
                or system["effort"] not in allowed_efforts
                or system["requires_paid_approval"] is not False
            ):
                raise ValidationError("%s Codex campaign lane is invalid" % source)
        if (
            name == "codex"
            and campaign_schema == PARITY_CAMPAIGN_SCHEMA_V1
            and system["effort"] != "ultra"
        ):
            raise ValidationError("%s legacy Codex campaign lane must remain ultra" % source)
    if names != list(PARITY_CAMPAIGN_SYSTEMS):
        raise ValidationError("%s systems must use canonical codex, claude order" % source)
    return names


def _validate_campaign_trials(
    trials,
    design,
    source_value,
    systems,
    campaign_schema: str,
    codex_profile_effort: str,
    source: str,
) -> List[Dict]:
    if not isinstance(trials, list) or not trials or len(trials) > MAX_CAMPAIGN_TRIALS:
        raise ValidationError("%s trials must be a bounded non-empty array" % source)
    if design.get("trial_count") != len(trials):
        raise ValidationError("%s design trial_count must match trials" % source)
    expected_count = source_value["task_count"] * design["repetitions"]
    if len(trials) != expected_count:
        raise ValidationError("%s trials must cover every task repetition" % source)
    task_contracts = {}
    task_budgets = {}
    task_efforts = {}
    seen = set()
    artifact_paths = set()
    for sequence, trial in enumerate(trials, start=1):
        if not isinstance(trial, dict):
            raise ValidationError("%s trials must contain objects" % source)
        allowed_trial_fields = set(TRIAL_FIELDS)
        if campaign_schema in {PARITY_CAMPAIGN_SCHEMA, PARITY_CAMPAIGN_SCHEMA_V3}:
            allowed_trial_fields.add("codex_effort")
        _allowed_fields(trial, allowed_trial_fields, "%s trial" % source)
        task_id = trial.get("task_id")
        repetition = trial.get("repetition")
        expected_id = "r%03d-%s" % (repetition, task_id) if isinstance(repetition, int) else ""
        if not isinstance(task_id, str) or not SAFE_ID.match(task_id):
            raise ValidationError("%s trial task_id must be safe" % source)
        if trial.get("id") != expected_id or trial["id"] in seen:
            raise ValidationError("%s trial id is invalid or duplicated" % source)
        seen.add(trial["id"])
        if trial.get("sequence") != sequence:
            raise ValidationError("%s trial sequence must match array order" % source)
        _validate_int(
            repetition,
            "%s trial repetition" % source,
            minimum=1,
            maximum=design["repetitions"],
        )
        contract_hash = trial.get("task_contract_sha256")
        _validate_sha256(contract_hash, "%s trial task_contract_sha256" % source)
        if task_id in task_contracts and task_contracts[task_id] != contract_hash:
            raise ValidationError("%s task contract hash changed across repetitions" % source)
        task_contracts[task_id] = contract_hash
        if campaign_schema in {PARITY_CAMPAIGN_SCHEMA, PARITY_CAMPAIGN_SCHEMA_V3}:
            resolved_effort = trial.get("codex_effort")
            if resolved_effort not in CODEX_REASONING_EFFORTS:
                raise ValidationError("%s trial codex_effort must be a concrete supported effort" % source)
            if (
                codex_profile_effort != AUTO_REASONING_EFFORT
                and resolved_effort != codex_profile_effort
            ):
                raise ValidationError("%s trial codex_effort must match the fixed campaign effort" % source)
            if task_id in task_efforts and task_efforts[task_id] != resolved_effort:
                raise ValidationError("%s task Codex effort changed across repetitions" % source)
            task_efforts[task_id] = resolved_effort
        _validate_trial_budget(trial.get("budget"), "%s trial budget" % source)
        budget_hash = _sha256_json(trial["budget"])
        if task_id in task_budgets and task_budgets[task_id] != budget_hash:
            raise ValidationError("%s task budget changed across repetitions" % source)
        task_budgets[task_id] = budget_hash
        order = trial.get("system_order")
        if not isinstance(order, list) or sorted(order) != sorted(systems) or len(set(order)) != 2:
            raise ValidationError("%s trial system_order must contain both systems once" % source)
        for artifact_path in _validate_campaign_artifacts(trial.get("artifacts"), systems, source):
            if artifact_path in artifact_paths:
                raise ValidationError("%s trial artifact paths must be unique" % source)
            artifact_paths.add(artifact_path)
        if trial.get("status") != "planned":
            raise ValidationError("%s preregistered trial status must be planned" % source)
    if len(task_contracts) != source_value["task_count"]:
        raise ValidationError("%s task_count does not match distinct trial tasks" % source)
    expected_rows, expected_orders = _campaign_design(
        [{"id": task_id} for task_id in sorted(task_contracts)],
        design["seed"],
        design["repetitions"],
    )
    observed_pairs = [(trial["repetition"], trial["task_id"]) for trial in trials]
    expected_pairs = [(repetition, task["id"]) for repetition, task in expected_rows]
    if observed_pairs != expected_pairs:
        raise ValidationError("%s trial order does not match the declared seed" % source)
    for trial in trials:
        if trial["system_order"] != expected_orders[(trial["repetition"], trial["task_id"])]:
            raise ValidationError("%s provider order does not match the declared seed" % source)
    return trials


def _validate_trial_budget(budget, label: str) -> None:
    if not isinstance(budget, dict):
        raise ValidationError("%s must be an object" % label)
    _allowed_fields(budget, TRIAL_BUDGET_FIELDS, label)
    _validate_int(budget.get("max_minutes"), "%s max_minutes" % label, minimum=1)
    _validate_int(budget.get("max_agents"), "%s max_agents" % label, minimum=0)
    if budget.get("max_live_tool_cost") not in {"none", "low", "medium", "high"}:
        raise ValidationError("%s max_live_tool_cost is invalid" % label)
    if "max_tokens" in budget:
        _validate_int(budget["max_tokens"], "%s max_tokens" % label, minimum=0)
    if "max_cost_usd" in budget:
        value = budget["max_cost_usd"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValidationError("%s max_cost_usd must be a non-negative number" % label)


def _validate_campaign_artifacts(artifacts, systems, source: str) -> List[str]:
    if not isinstance(artifacts, dict) or set(artifacts) != set(systems):
        raise ValidationError("%s trial artifacts must match campaign systems" % source)
    paths = []
    for system in systems:
        values = artifacts[system]
        if not isinstance(values, dict) or set(values) != ARTIFACT_FIELDS:
            raise ValidationError("%s trial artifacts for %s are incomplete" % (source, system))
        for value in values.values():
            paths.append(_clean_artifact_path(value, "%s trial artifact" % source))
    return paths


def _validate_provider_first_counts(counts, trials, source: str) -> None:
    if not isinstance(counts, dict) or set(counts) != set(PARITY_CAMPAIGN_SYSTEMS):
        raise ValidationError("%s provider_first_counts must match systems" % source)
    observed = {
        system: sum(1 for trial in trials if trial["system_order"][0] == system)
        for system in PARITY_CAMPAIGN_SYSTEMS
    }
    if counts != observed or abs(counts["codex"] - counts["claude"]) > 1:
        raise ValidationError("%s provider-first assignment must be balanced" % source)


def _public_parity_tasks(parity_tasks: Dict) -> Dict:
    if not isinstance(parity_tasks, dict):
        return parity_tasks
    return {
        key: value
        for key, value in parity_tasks.items()
        if not str(key).startswith("_")
    }


def _rank(seed: int, lane: str, repetition: int, task_id: str) -> str:
    return hashlib.sha256(("%d:%s:%d:%s" % (seed, lane, repetition, task_id)).encode("utf-8")).hexdigest()


def _sha256_json(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _clean_artifact_path(value, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or "\x00" in value or "\n" in value:
        raise ValidationError("%s must be a bounded single-line path" % label)
    normalized = value.replace("\\", "/")
    if ".." in PurePosixPath(normalized).parts:
        raise ValidationError("%s must not escape its artifact root" % label)
    return normalized.rstrip("/")


def _join_artifact_path(root: str, *parts: str) -> str:
    clean_root = _clean_artifact_path(root, "parity campaign artifact root")
    for part in parts:
        if not isinstance(part, str) or not part or "/" in part or "\\" in part:
            raise ValidationError("parity campaign artifact path component is invalid")
    return clean_root + "/" + "/".join(parts)


def _allowed_fields(value: Dict, allowed: set, label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError("%s contains unknown fields: %s" % (label, ", ".join(unknown)))


def _validate_int(value, label: str, minimum: int, maximum: int = None) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValidationError("%s must be an integer >= %d" % (label, minimum))
    if maximum is not None and value > maximum:
        raise ValidationError("%s must be <= %d" % (label, maximum))


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValidationError("%s must be a lowercase SHA-256 value" % label)
